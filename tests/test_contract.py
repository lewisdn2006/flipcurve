"""Contract tests: the mocks validate, the curves behave, and the API serves the contract.

Keep these green all weekend. When the real engine lands, point the same checks at its output.
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend.models.schemas import (
    GRID_POINTS,
    IdentifyResponse,
    ListingRow,
    MarketScanResponse,
)

MOCK = Path(__file__).resolve().parents[1] / "backend" / "mock"


@pytest.fixture(scope="module")
def scan() -> MarketScanResponse:
    return MarketScanResponse.model_validate_json((MOCK / "mock_market_scan.json").read_text())


def test_identify_mock_validates():
    IdentifyResponse.model_validate_json((MOCK / "mock_identify.json").read_text())


def test_grid_has_four_tiers_on_one_axis(scan):
    axes = {tuple(p.price_gbp for p in pts) for pts in scan.price_curve_grid.values()}
    assert len(scan.price_curve_grid) == 4 and len(axes) == 1
    assert all(len(pts) == GRID_POINTS for pts in scan.price_curve_grid.values())


@pytest.mark.parametrize("tier", ["fair", "good", "very_good", "mint"])
def test_curves_are_monotone(scan, tier):
    pts = scan.price_curve_grid[tier]
    for a, b in zip(pts, pts[1:]):
        # higher price -> lower sale probability, longer wait
        assert b.prob_sell_7d <= a.prob_sell_7d + 1e-9
        assert b.expected_days_to_sell >= a.expected_days_to_sell - 1e-9
    for p in pts:
        # longer horizon -> higher probability; 90% range brackets the median
        assert p.prob_sell_24h <= p.prob_sell_7d <= p.prob_sell_30d
        assert p.ci_days_low <= p.median_days_to_sell <= p.ci_days_high


def test_better_condition_sells_faster_at_same_price(scan):
    i = GRID_POINTS // 2
    p7 = [scan.price_curve_grid[t][i].prob_sell_7d for t in ["fair", "good", "very_good", "mint"]]
    assert p7 == sorted(p7)


def test_presets_ordered(scan):
    for pr in scan.presets.values():
        assert pr.sell_24h_gbp <= pr.best_ev_gbp <= pr.patient_gbp


def test_markdown_schedule_drops(scan):
    prices = [s.list_price_gbp for s in scan.markdown_schedule]
    assert prices == sorted(prices, reverse=True) and len(set(prices)) == len(prices)


def test_listing_row_rejects_bad_interval():
    with pytest.raises(ValueError):
        ListingRow(listing_id="x", platform="vinted", title="t", price_gbp=10, total_buyer_price_gbp=12,
                   obs_type="sold_interval", t_days=5, t_upper_days=2, observed_at="2026-10-24T19:00:00Z")


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
