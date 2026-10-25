"""Outlier filter, economics and the full pipeline."""
from datetime import datetime, timezone

import pytest

from backend.engine.outlier_filter import filter_rows
from backend.engine.pipeline import confidence, run
from backend.engine.platform_economics import ebay_buyer_fee, net_payout
from backend.engine.simulate import TrueParams, simulate_listings
from backend.models.schemas import MarketScanResponse

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
ITEM = {"title": "Nike Air Force 1 '07 Low White", "brand": "Nike", "model_name": None, "category": None,
        "size_or_storage": "UK 9", "era_or_year": None, "condition_tier": "good", "condition_score": 7.0,
        "detected_flaws": [], "search_queries": {"ebay": "nike air force 1 white", "vinted": "air force 1 white"},
        "is_broken": False}


def row(title, price, platform="ebay", tier="good", cond=None):
    return {"listing_id": title[:8], "platform": platform, "title": title, "price_gbp": price,
            "condition_tier": tier, "condition_raw": cond, "obs_type": "active", "t_days": 1.0}


def test_filter_keywords_relevance_and_price():
    base = [row(f"Nike Air Force 1 white UK {s}", 30 + s) for s in range(4, 13)]
    rows = base + [row("Nike Air Force 1 white box only", 5), row("Nike Air Force 1 white spares", 8),
                   row("Adidas Samba black", 30), row("Nike Air Force 1 white UK 9 rare", 400)]
    out = {r["title"]: r for r in filter_rows(rows, ITEM["search_queries"])}
    assert out["Nike Air Force 1 white box only"]["outlier_reason"] == "keyword: box only"
    assert out["Nike Air Force 1 white spares"]["outlier_reason"] == "keyword: spares"
    assert out["Adidas Samba black"]["outlier_reason"] == "not a close title match"
    assert "above" in [r for r in filter_rows(rows, ITEM["search_queries"]) if r["price_gbp"] == 400][0]["outlier_reason"]
    assert not any(out[r["title"]]["is_outlier"] for r in base)


def test_broken_item_keeps_repair_listings():
    rows = [row("Nike Air Force 1 white spares", 8)]
    assert not filter_rows(rows, ITEM["search_queries"], item_is_broken=True)[0]["is_outlier"]


def test_ebay_buyer_fee_tiers():
    assert ebay_buyer_fee(20) == pytest.approx(1.50)
    assert ebay_buyer_fee(50) == pytest.approx(2.70)


def test_net_payout_applies_offer_buffer():
    assert net_payout(100.0, "vinted") == pytest.approx(87.0)
    assert net_payout(100.0, "ebay") == pytest.approx(96.0)


def test_confidence_rule():
    assert confidence(0, 1, 9) == "estimated"
    assert confidence(5, 3, 4) == "low"
    assert confidence(15, 3, 4) == "medium"
    assert confidence(40, 3, 4) == "high"
    assert confidence(40, 2, 9) == "medium"


def test_pipeline_end_to_end():
    rows = simulate_listings(TrueParams(), n_per_platform=200, now=NOW, seed=8)
    for r in rows:
        r["title"] = "Nike Air Force 1 white"
    out = MarketScanResponse.model_validate(run(ITEM, rows, now=NOW, data_source="seed"))
    assert out.best_platform in ("ebay", "vinted")
    assert out.platform_recommendations["ebay"].n_sales > 50
    assert out.market_summary.survivorship_bias_pct > 0  # unsold listings are priced higher
    m = out.price_markers["good"]
    assert m.fast_sale_gbp <= m.best_value_gbp <= m.patient_gbp


def test_cheaper_platform_is_not_flagged_as_outliers():
    ebay = [row(f"Nike Air Force 1 white {i}", 34 + i % 5) for i in range(12)]
    vinted = [dict(row(f"Nike Air Force 1 white v{i}", 21 + i % 4, platform="vinted")) for i in range(12)]
    out = filter_rows(ebay + vinted, {"ebay": "nike air force 1 white", "vinted": "air force 1 white"})
    assert not any(r["is_outlier"] for r in out)
