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


def test_slow_market_markers_stay_near_real_prices():
    # 25 Oct: in a slow market no price reached the targets and the markers showed the bottom
    # of the chart (£10 for trainers that sell at ~£30)
    rows = simulate_listings(TrueParams(lambda0=60.0, beta1=0.6), n_per_platform=150, now=NOW, seed=4)
    for r in rows:
        r["title"] = "Nike Air Force 1 white"
    out = MarketScanResponse.model_validate(run(ITEM, rows, now=NOW, data_source="seed"))
    for tier, m in out.price_markers.items():
        anchor = out.model_fit.anchor_gbp[tier]
        assert m.fast_sale_gbp <= m.best_value_gbp <= m.patient_gbp
        assert m.fast_sale_gbp > out.price_range.min_gbp + 0.01, (tier, m, anchor)  # never the axis floor
        assert m.best_value_gbp > 0.8 * anchor, (tier, m, anchor)
    assert out.markdown_schedule[-1].list_price_gbp > out.price_range.min_gbp + 0.01


def test_other_colourways_collabs_and_must_terms_are_excluded():
    from backend.engine.outlier_filter import other_item_reason
    q, item = "nike air jordan 1 low triple black", "Nike Air Jordan 1 Low Triple Black UK 10.5"
    assert other_item_reason("Nike Air Jordan 1 Low Black White UK 9", q, item) == "different colour: white"
    assert other_item_reason("Nike Air Jordan 1 Low Triple Black UK 11", q, item) is None
    assert other_item_reason("Nike Air Jordan 1 Low OG SP x Travis Scott Black", q, item) == "collaboration"
    assert other_item_reason("Nike Air Force 1 '07 Low Triple Black", q, item, must_have=["jordan", "low"]) == "missing 'jordan'"
    assert other_item_reason("Jordan 1 Low Travis Scott Phantom Black", q, item, must_not_have=["travis", "mid"]) == "different item: travis"
    # "mid" in must_not_have is left to the related-version logic
    assert other_item_reason("Nike Air Jordan 1 Mid Triple Black", q, item, must_not_have=["mid"]) is None
    # "mint" is a condition, not a colour
    assert other_item_reason("Nike Air Jordan 1 Low Triple Black mint condition", q, item) is None


def test_recommended_prices_stay_within_what_actually_sold():
    # 25 Oct: price looked barely linked to speed, so "best value" ran to the top of the chart
    # (£121 for trainers that sell at £20-60)
    rows = simulate_listings(TrueParams(beta1=0.2), n_per_platform=150, now=NOW, seed=11)
    for r in rows:
        r["title"] = "Nike Air Force 1 white"
    out = MarketScanResponse.model_validate(run(ITEM, rows, now=NOW, data_source="seed"))
    sold = sorted(r["price_gbp"] for r in rows if r["obs_type"] != "active" and r["condition_tier"] == "good")
    med = sold[len(sold) // 2]
    m = out.price_markers["good"]
    assert m.best_value_gbp <= 1.36 * med and m.patient_gbp <= 1.61 * med, (m, med)
    assert out.platform_recommendations["ebay"].recommended_list_gbp <= 1.36 * med


def test_small_shoe_sizes_count_as_kids_for_an_adult_pair():
    from backend.engine.outlier_filter import uk_size, version_mismatch
    assert uk_size("Nike Air Jordan 1 Low UK 10.5") == 10.5 and uk_size("trainers 4.5uk") == 4.5
    q, item = "jordan 1 low triple black", "Nike Air Jordan 1 Low Triple Black UK 10.5 Footwear"
    assert version_mismatch("Nike Air Jordan 1 Low Triple Black UK 4", q, item, footwear=True) == "kids"
    assert version_mismatch("Nike Air Jordan 1 Low Triple Black UK 9", q, item, footwear=True) is None
    assert version_mismatch("Ladies coat UK 6", "coat", "Coat UK 10", footwear=False) is None


def test_tee_and_tshirt_match_and_must_have_relaxes_overlap():
    q = {"ebay": "mki miyuki zoku studio t-shirt white"}
    rows = [row("MKI Miyuki Zoku White Logo Mens Tee XL", 12), row("MKI MIYUKI ZOKU White T Shirt", 14),
            row("Plain white t-shirt", 3)]
    out = {r["title"]: r for r in filter_rows(rows, q, must_have=["mki|miyuki"])}
    assert not out["MKI Miyuki Zoku White Logo Mens Tee XL"]["is_outlier"]
    assert not out["MKI MIYUKI ZOKU White T Shirt"]["is_outlier"]
    assert out["Plain white t-shirt"]["is_outlier"]



def _full(r):
    base = {"shipping_gbp": None, "buyer_fee_gbp": None, "total_buyer_price_gbp": None, "t_upper_days": None,
            "t_entry_days": None, "stock_window_days": None, "related": None, "start_source": "exact",
            "listed_at": None, "sold_at": None, "favourite_count": None, "best_offer_accepted": None,
            "is_outlier": False, "outlier_reason": None, "url": None}
    return {**base, **r}


def _phone_rows(now=NOW):
    """A phone losing value: 40 sales over 90 days, from about £470 ninety days ago to about £400 now,
    plus 30 Pro Max sales at Max prices and some still-listed ones."""
    from datetime import timedelta
    rows = []
    for i in range(40):
        age = 90 * i / 39                        # days since the sale
        price = 400 + 70 * age / 90 + (i % 5 - 2) * 6
        rows.append({"listing_id": f"s{i}", "platform": "ebay", "title": "Apple iPhone 15 Pro 256GB Black Titanium Unlocked",
                     "price_gbp": round(price, 2), "condition_tier": "good", "condition_raw": None, "obs_type": "sold_interval",
                     "t_days": 3.0, "t_upper_days": 4.0, "sold_at": (now - timedelta(days=age)).isoformat(),
                     "observed_at": now.isoformat()})
    for i in range(30):
        rows.append({"listing_id": f"m{i}", "platform": "ebay", "title": "Apple iPhone 15 Pro Max 256GB Black Titanium",
                     "price_gbp": 560 + i, "condition_tier": "good", "condition_raw": None, "obs_type": "sold_interval",
                     "t_days": 1.0, "t_upper_days": 2.0, "sold_at": (now - timedelta(days=i)).isoformat(),
                     "observed_at": now.isoformat()})
    for i in range(20):
        rows.append({"listing_id": f"a{i}", "platform": "ebay", "title": "Apple iPhone 15 Pro 256GB Black Titanium",
                     "price_gbp": 430 + 3 * i, "condition_tier": "good", "condition_raw": None, "obs_type": "active",
                     "t_days": 5.0 + i, "observed_at": now.isoformat()})
    return [_full(r) for r in rows]


PHONE = {**ITEM, "title": "Apple iPhone 15 Pro 256GB Black Titanium", "size_or_storage": "256GB",
         "search_queries": {"ebay": "apple iphone 15 pro 256gb black", "vinted": "iphone 15 pro black"}}


def test_price_level_comes_from_recent_sales():
    out = run(PHONE, _phone_rows(), now=NOW, data_source="seed")
    good = out["market_summary"]["condition_buckets"]["good"]
    assert good["n"] < 40 and good["median"] < 420          # the last month, not the 90-day median (~£435)
    assert any("last 30 days" in w for w in out["warnings"])
    assert out["model_fit"]["anchor_gbp"]["good"] < 420


def test_similar_versions_are_dropped_once_there_are_enough_exact_sales():
    out = run(PHONE, _phone_rows(), now=NOW, data_source="seed")
    assert not any(x.get("related") for x in out["comparable_listings"])
    assert any("similar versions weren't used" in w for w in out["warnings"])
    assert not any("Few exact matches" in w for w in out["warnings"])


def test_similar_versions_still_help_when_exact_sales_are_few():
    rows = [r for r in _phone_rows() if not (r["listing_id"].startswith("s") and int(r["listing_id"][1:]) >= 8)]
    out = run(PHONE, rows, now=NOW, data_source="seed")
    assert any(x.get("related") == "max" for x in out["comparable_listings"])
