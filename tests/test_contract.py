"""Contract tests: the mocks validate, the curves behave, and the API serves the contract.

Keep these green all weekend. When the real engine lands, point the same checks at its output.
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.models.schemas import (
    GRID_POINTS,
    SCHEMA_VERSION,
    IdentifyResponse,
    ListingRow,
    MarketScanResponse,
)

MOCK = Path(__file__).resolve().parents[1] / "backend" / "mock"
TIERS = ["fair", "good", "very_good", "mint"]


@pytest.fixture(scope="module")
def scan() -> MarketScanResponse:
    return MarketScanResponse.model_validate_json((MOCK / "mock_market_scan.json").read_text())


def test_identify_mock_validates():
    IdentifyResponse.model_validate_json((MOCK / "mock_identify.json").read_text())


def test_version_and_platforms(scan):
    assert scan.schema_version == SCHEMA_VERSION == "1.1"
    assert set(scan.platform_recommendations) == {"ebay", "vinted"}
    assert all(set(p.platforms) == {"ebay", "vinted"} for p in scan.price_curve_grid["good"])


def test_grid_has_four_tiers_on_one_axis(scan):
    axes = {tuple(p.price_gbp for p in pts) for pts in scan.price_curve_grid.values()}
    assert len(scan.price_curve_grid) == 4 and len(axes) == 1
    assert all(len(pts) == GRID_POINTS for pts in scan.price_curve_grid.values())


@pytest.mark.parametrize("tier", TIERS)
def test_curves_are_monotone(scan, tier):
    pts = scan.price_curve_grid[tier]
    for a, b in zip(pts, pts[1:]):
        # higher list price -> lower sale probability, longer wait
        assert b.prob_sell_7d <= a.prob_sell_7d + 1e-9
        assert b.expected_days_to_sell >= a.expected_days_to_sell - 1e-9
    for p in pts:
        assert p.prob_sell_24h <= p.prob_sell_7d <= p.prob_sell_30d
        assert p.ci_days_low <= p.median_days_to_sell <= p.ci_days_high
        for pp in p.platforms.values():
            assert pp.expected_days_low <= pp.expected_days <= pp.expected_days_high
            assert pp.net_payout_gbp <= p.price_gbp + 1e-9


@pytest.mark.parametrize("tier", TIERS)
def test_surface_monotone(scan, tier):
    s = scan.sale_surface[tier]
    assert s.days == sorted(s.days)
    for row in s.prob:  # more days -> higher probability
        assert all(b >= a - 1e-9 for a, b in zip(row, row[1:]))
    for j in range(len(s.days)):  # higher price -> lower probability
        col = [row[j] for row in s.prob]
        assert all(b <= a + 1e-9 for a, b in zip(col, col[1:]))


def test_better_condition_sells_faster_at_same_price(scan):
    i = GRID_POINTS // 2
    p7 = [scan.price_curve_grid[t][i].prob_sell_7d for t in TIERS]
    assert p7 == sorted(p7)


def test_markers_ordered(scan):
    for m in scan.price_markers.values():
        assert m.fast_sale_gbp <= m.best_value_gbp <= m.patient_gbp


def test_recommendations(scan):
    for rec in scan.platform_recommendations.values():
        assert rec.expected_days_low <= rec.expected_days <= rec.expected_days_high
        assert rec.n_sales <= rec.n_listings or rec.n_listings == 0
    assert scan.best_platform in scan.platform_recommendations


def test_markdown_schedule_drops(scan):
    prices = [s.list_price_gbp for s in scan.markdown_schedule]
    assert prices == sorted(prices, reverse=True) and len(set(prices)) == len(prices)


def test_evidence_rows(scan):
    rows = scan.comparable_listings
    assert len(rows) >= 50
    assert {r.obs_type for r in rows} >= {"active", "sold_interval"}
    assert all(r.start_source != "unknown" for r in rows)


def test_listing_row_rejects_bad_interval():
    with pytest.raises(ValueError):
        ListingRow(listing_id="x", platform="vinted", title="t", price_gbp=10, total_buyer_price_gbp=12,
                   obs_type="sold_interval", t_days=5, t_upper_days=2, observed_at="2026-10-24T19:00:00Z")


def test_fb_marketplace_rejected():
    with pytest.raises(ValueError):
        ListingRow(listing_id="x", platform="fb_marketplace", title="t", price_gbp=10, total_buyer_price_gbp=12,
                   obs_type="active", t_days=1, observed_at="2026-10-24T19:00:00Z")


def test_api_serves_contract():
    c = TestClient(app)
    assert c.get("/api/v1/health").json()["ok"]
    ident = c.post("/api/v1/identify", files={"images": ("a.jpg", b"\xff\xd8fake", "image/jpeg")})
    assert ident.status_code == 200
    item = IdentifyResponse.model_validate(ident.json()).item
    r = c.post("/api/v1/market-scan", json={"item": item.model_dump(mode="json")})
    assert r.status_code == 200, r.text
    MarketScanResponse.model_validate(r.json())
    t = c.post("/api/v1/identify-text", json={"query": "Sony WH-1000XM4 Black"})
    assert t.status_code == 200 and t.json()["source"] == "text"
