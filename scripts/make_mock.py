"""Generate the frozen mock payloads in backend/mock/ and the JSON Schema in contract/.

The mock uses the real closed-form survival maths with hand-picked parameters, so curves are
internally consistent (monotone, sensible ranges) and the frontend can build against
realistic numbers before the engine exists.

Run from repo root:  python scripts/make_mock.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.engine import survival_model as sm  # noqa: E402
from backend.engine.platform_config import DEFAULT_GAMMA_PER_DAY, PLATFORMS  # noqa: E402
from pydantic.json_schema import models_json_schema  # noqa: E402

from backend.models.schemas import (  # noqa: E402
    GRID_POINTS,
    MAX_DAYS,
    ConditionTier,
    ErrorResponse,
    IdentifyResponse,
    IdentifyTextRequest,
    MarketScanRequest,
    MarketScanResponse,
)

NOW = datetime(2026, 10, 24, 19, 0, tzinfo=timezone.utc)
TIERS = [t.value for t in ConditionTier]
CIDX = {t.value: t.index for t in ConditionTier}

# ---- hand-picked "fitted" parameters for a pair of Nike AF1 '07, UK 9
FIT = dict(k=0.85, lambda0=4.5, beta1=2.4, beta2=0.35, sigma_p=12.0)
ANCHOR = {"fair": 22.0, "good": 34.0, "very_good": 45.0, "mint": 65.0}
# shared axis for all tiers: half the Fair anchor up to 1.35x the Mint anchor
PRICES = np.round(np.linspace(0.5 * ANCHOR['fair'], 1.35 * ANCHOR['mint'], GRID_POINTS), 2)

ITEM = {
    "title": "Nike Air Force 1 '07 Low White",
    "brand": "Nike",
    "model_name": "Air Force 1 '07 Low",
    "category": "Footwear / Trainers",
    "size_or_storage": "UK 9",
    "era_or_year": None,
    "condition_tier": "good",
    "condition_score": 7.4,
    "detected_flaws": ["Light creasing on toebox", "Minor heel sole wear"],
    "search_queries": {"ebay": "nike air force 1 07 white uk 9", "vinted": "air force 1 white 9"},
    "is_broken": False,
}


def r(x, nd=2):
    return float(round(float(x), nd))


def d(x):
    """Duration in days, clipped to MAX_DAYS."""
    return r(min(float(x), MAX_DAYS), 1)


def lam_for(tier, prices, platform=None):
    p = np.asarray(prices, dtype=float)
    m = 1.0
    if platform:
        cfg = PLATFORMS[platform]
        m = cfg["speed_multiplier"]
        p = p / (1.0 - cfg["local_discount"])  # local buyers compare to shipped prices
    lam = sm.scale(p, ANCHOR[tier], FIT["sigma_p"], CIDX[tier], FIT["lambda0"], FIT["beta1"], FIT["beta2"])
    return lam * m


def platform_point(platform, price, tier):
    cfg = PLATFORMS[platform]
    lam = float(lam_for(tier, [price], platform)[0])
    days = sm.expected_days(lam, FIT["k"]) / (1.0 - cfg["flake_prob"])  # flakes restart the clock
    net = price * (1 - cfg["seller_fee_pct"]) - cfg["seller_fixed_gbp"]
    return {
        "list_price_gbp": r(price / (1 - cfg["offer_buffer"])),
        "net_payout_gbp": r(net),
        "expected_days": d(days),
        "prob_sell_7d": r(sm.cdf(7, lam, FIT["k"]) * (1 - cfg["flake_prob"]), 3),
    }


def best_platform(pp):
    return max(pp, key=lambda k: (pp[k]["net_payout_gbp"], -pp[k]["expected_days"]))


def grid_for(tier):
    lam = lam_for(tier, PRICES)
    k = FIT["k"]
    pts = []
    for i, p in enumerate(PRICES):
        pp = {pl: platform_point(pl, p, tier) for pl in PLATFORMS}
        pts.append({
            "price_gbp": r(p),
            "prob_sell_24h": r(sm.cdf(1, lam[i], k), 3),
            "prob_sell_7d": r(sm.cdf(7, lam[i], k), 3),
            "prob_sell_30d": r(sm.cdf(30, lam[i], k), 3),
            "expected_days_to_sell": d(sm.expected_days(lam[i], k)),
            "median_days_to_sell": d(sm.quantile_days(0.5, lam[i], k)),
            "ci_days_low": d(sm.quantile_days(0.05, lam[i], k)),
            "ci_days_high": d(sm.quantile_days(0.95, lam[i], k)),
            "platforms": pp,
            "best_platform": best_platform(pp),
        })
    return pts


def presets_for(tier, grid):
    p24 = [g["price_gbp"] for g in grid if g["prob_sell_24h"] >= 0.5]
    p30 = [g["price_gbp"] for g in grid if g["prob_sell_30d"] >= 0.8]
    # p*(gamma): maximise net * E[exp(-gamma T)] on the best platform at each price
    vals = []
    for g in grid:
        pl = g["best_platform"]
        lam = float(lam_for(tier, [g["price_gbp"]], pl)[0])
        disc = float(sm.discounted_sale_value(lam, FIT["k"], DEFAULT_GAMMA_PER_DAY)[0])
        vals.append(g["platforms"][pl]["net_payout_gbp"] * disc * (1 - PLATFORMS[pl]["flake_prob"]))
    best = grid[int(np.argmax(vals))]["price_gbp"]
    return {
        "sell_24h_gbp": max(p24) if p24 else grid[0]["price_gbp"],
        "best_ev_gbp": best,
        "patient_gbp": max(p30) if p30 else grid[0]["price_gbp"],
        "gamma_per_day": DEFAULT_GAMMA_PER_DAY,
    }


def nearest(grid, price):
    return min(grid, key=lambda g: abs(g["price_gbp"] - price))


def recommendations(grid, strike):
    g = nearest(grid, strike)
    days = {pl: v["expected_days"] for pl, v in g["platforms"].items()}
    fastest = min(days.values())
    recs = {}
    for pl, v in g["platforms"].items():
        cfg = PLATFORMS[pl]
        recs[pl] = {
            "recommended_list_gbp": v["list_price_gbp"],
            "expected_strike_gbp": g["price_gbp"],
            "net_payout_gbp": v["net_payout_gbp"],
            "expected_days": v["expected_days"],
            "offer_buffer_pct": r(cfg["offer_buffer"] * 100, 1),
            "fit_score": int(round(100 * fastest / v["expected_days"])) if pl != "fb_marketplace"
            else int(round(100 * fastest / v["expected_days"] * (1 - cfg["flake_prob"]))),
            "flake_risk_pct": r(cfg["flake_prob"] * 100, 0) if cfg["flake_prob"] else None,
            "notes": ["Local pickup only; ask for a deposit to cut flake risk"] if pl == "fb_marketplace" else [],
        }
    return recs


def markdown(grid, strike, platform):
    buf = PLATFORMS[platform]["offer_buffer"]
    # floor: highest price with >90% chance of selling within 48h
    floor_candidates = [g["price_gbp"] for g in grid
                        if sm.cdf(2, float(lam_for(ITEM["condition_tier"], [g["price_gbp"]])[0]), FIT["k"]) >= 0.9]
    floor = max(floor_candidates) if floor_candidates else grid[0]["price_gbp"]
    floor = min(floor, strike * 0.9)  # the floor must actually be a drop
    mid = (strike + floor) / 2
    lp = lambda s: r(s / (1 - buf))  # noqa: E731
    return [
        {"day": 0, "list_price_gbp": lp(strike), "platform": platform,
         "action": f"List Sunday evening at £{lp(strike):.2f} (includes {buf:.0%} offer buffer)"},
        {"day": 4, "list_price_gbp": lp(mid), "platform": platform,
         "action": f"If unsold, drop to £{lp(mid):.2f} so favouriters get a price-drop alert"},
        {"day": 10, "list_price_gbp": lp(floor), "platform": platform,
         "action": f"Floor: £{lp(floor):.2f} (>90% chance of selling within 48h)"},
    ]


def comparable_listings():
    rows = [
        ("eb1", "ebay", "Nike Air Force 1 07 White UK 9 - worn twice", 36.0, 4.0, "good", "sold_unknown_time", None, None, None),
        ("eb2", "ebay", "Mens Nike AF1 Low Triple White Size 9", 31.0, 3.5, "good", "sold_unknown_time", None, None, None),
        ("eb3", "ebay", "Nike Air Force 1 '07 UK9 Boxed VGC", 44.0, 0.0, "very_good", "sold_unknown_time", None, None, None),
        ("vi1", "vinted", "Air force 1 blanches 9", 45.0, 3.2, "good", "active", 16.0, None, 3),
        ("vi2", "vinted", "Nike AF1 white uk 9 good condition", 30.0, 3.2, "good", "sold_interval", 2.0, 2.9, 11),
        ("vi3", "vinted", "AF1 '07 triple white size 9", 52.0, 3.2, "very_good", "active", 23.5, None, 1),
        ("vi4", "vinted", "air forces 1 uk9 creased", 24.0, 3.2, "fair", "sold_interval", 0.6, 1.4, 9),
        ("eb4", "ebay", "Nike Air Force 1 White UK 9", 48.0, 4.5, "good", "active", 21.0, None, None),
    ]
    out = []
    for lid, pl, title, price, ship, tier, obs, t, tu, fav in rows:
        cfg = PLATFORMS[pl]
        fee = price * cfg["buyer_fee_pct"] + cfg["buyer_fee_fixed_gbp"]
        out.append({
            "listing_id": lid, "platform": pl, "title": title, "url": None,
            "price_gbp": price, "shipping_gbp": ship, "buyer_fee_gbp": r(fee),
            "total_buyer_price_gbp": r(price + ship + fee), "condition_tier": tier,
            "condition_raw": None, "obs_type": obs, "t_days": t, "t_upper_days": tu,
            "listed_at": None, "sold_at": None, "observed_at": NOW.isoformat(),
            "favourite_count": fav, "is_outlier": False, "outlier_reason": None,
        })
    return out


def build_market_scan():
    grids = {t: grid_for(t) for t in TIERS}
    presets = {t: presets_for(t, grids[t]) for t in TIERS}
    tier = ITEM["condition_tier"]
    strike = presets[tier]["best_ev_gbp"]
    recs = recommendations(grids[tier], strike)
    top = max(recs, key=lambda p: recs[p]["fit_score"])
    buckets = {}
    for t in TIERS:
        a = ANCHOR[t]
        lam = float(lam_for(t, [a])[0])
        buckets[t] = {"p25": r(a * 0.82), "median": r(a), "p75": r(a * 1.18),
                      "avg_days": r(sm.expected_days(lam, FIT["k"]), 1), "n": {"fair": 9, "good": 31, "very_good": 26, "mint": 16}[t]}
    active_med, sold_med = 45.0, 34.0
    return {
        "item_metadata": ITEM,
        "cleaned_image_url": "/static/mock/cleaned_item_01.png",
        "market_summary": {
            "sample_size_sold": 38, "sample_size_active": 44, "sample_size_excluded": 7,
            "active_median_gbp": active_med, "sold_median_gbp": sold_med,
            "survivorship_bias_pct": r((active_med - sold_med) / sold_med * 100, 1),
            "condition_buckets": buckets, "data_source": "mock",
            "platforms_ok": ["ebay", "vinted", "fb_marketplace"], "fetched_at": NOW.isoformat(),
        },
        "model_fit": {**FIT, "anchor_gbp": ANCHOR,
                      "platform_multiplier": {p: c["speed_multiplier"] for p, c in PLATFORMS.items()},
                      "n_obs": 82, "n_censored": 44, "used_priors": False, "log_likelihood": -187.4},
        "price_range": {"min_gbp": r(PRICES[0]), "max_gbp": r(PRICES[-1]), "step_gbp": r(PRICES[1] - PRICES[0])},
        "price_curve_grid": grids,
        "presets": presets,
        "platform_recommendations": recs,
        "markdown_schedule": markdown(grids[tier], strike, top),
        "comparable_listings": comparable_listings(),
        "warnings": ["MOCK DATA: generated by scripts/make_mock.py, not from real listings"],
    }


def build_identify():
    return {"item": ITEM, "source": "vision", "cleaned_image_url": "/static/mock/cleaned_item_01.png",
            "confidence": 0.86, "warnings": []}


def main():
    mock_dir = ROOT / "backend" / "mock"
    contract_dir = ROOT / "contract"
    mock_dir.mkdir(exist_ok=True)
    contract_dir.mkdir(exist_ok=True)

    scan = MarketScanResponse.model_validate(build_market_scan())
    ident = IdentifyResponse.model_validate(build_identify())
    (mock_dir / "mock_market_scan.json").write_text(scan.model_dump_json(indent=2))
    (mock_dir / "mock_identify.json").write_text(ident.model_dump_json(indent=2))

    # one combined schema with shared $defs, so the TS generator emits each type once
    names = ["IdentifyResponse", "MarketScanResponse", "MarketScanRequest", "IdentifyTextRequest", "ErrorResponse"]
    models = [IdentifyResponse, MarketScanResponse, MarketScanRequest, IdentifyTextRequest, ErrorResponse]
    _, schema = models_json_schema([(m, "serialization") for m in models], ref_template="#/$defs/{model}")
    schema.update({"title": "FlipCurveApi", "type": "object",
                   "properties": {n: {"$ref": f"#/$defs/{n}"} for n in names}})
    (contract_dir / "schema.json").write_text(json.dumps(schema, indent=2))
    g = scan.price_curve_grid
    print("wrote mocks + schema;", {t: len(v) for t, v in g.items()}, "presets:", scan.presets["good"])


if __name__ == "__main__":
    main()
