"""Generate the mock payloads in backend/mock/ and the JSON Schema in contract/ (contract v1.1).

The mock uses the real closed-form survival maths with known parameters, and its evidence
listings come from engine/simulate.py, so every chart the frontend draws is internally
consistent. The confidence ranges use a simple approximation (maths doc, section 6:
SE of theta_j ~ 1 / (k * sqrt(n_sales + d0))) until the real fitter produces a Hessian.

Run from repo root:  python scripts/make_mock.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pydantic.json_schema import models_json_schema  # noqa: E402

from backend.engine import survival_model as sm  # noqa: E402
from backend.engine.platform_config import DEFAULT_GAMMA_PER_DAY, PLATFORMS, SURFACE_DAYS, TAU  # noqa: E402
from backend.engine.simulate import TrueParams, simulate_listings  # noqa: E402
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

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
TIERS = [t.value for t in ConditionTier]
CIDX = {t.value: t.index for t in ConditionTier}
P = TrueParams()
K = P.k
Z90 = 1.645
# pretend sales counts behind each platform's speed estimate (drives the confidence badges)
N_SALES = {"ebay": 41, "vinted": 9}
D0 = 1.0 / (K ** 2 * TAU ** 2)

PRICES = np.round(np.linspace(0.5 * P.anchor["fair"], 1.35 * P.anchor["mint"], GRID_POINTS), 2)

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


def lam_pooled(tier, prices):
    z = (np.asarray(prices, float) - P.anchor[tier]) / P.sigma_p
    return P.lambda0 * np.exp(P.beta1 * z - P.beta2 * (CIDX[tier] - 2))


def lam_platform(tier, prices, platform):
    return lam_pooled(tier, prices) * np.exp(P.theta[platform])


def se_log_days(platform, z):
    """Approximate SE of ln E[T]: platform term from its sales count, plus price-effect uncertainty."""
    se_theta = 1.0 / (K * np.sqrt(N_SALES[platform] + D0))
    return float(np.sqrt(se_theta ** 2 + 0.02 + (0.12 * z) ** 2))


def platform_point(platform, tier, price):
    cfg = PLATFORMS[platform]
    lam = float(lam_platform(tier, [price], platform)[0])
    mean = sm.expected_days(lam, K)
    z = (price - P.anchor[tier]) / P.sigma_p
    se = se_log_days(platform, z)
    sale = price * (1 - cfg["offer_buffer"])
    net = sale * (1 - cfg["seller_fee_pct"]) - cfg["seller_fixed_gbp"]
    return {
        "expected_sale_price_gbp": r(sale),
        "net_payout_gbp": r(net),
        "prob_sell_7d": r(sm.cdf(7, lam, K), 3),
        "expected_days": d(mean),
        "expected_days_low": d(mean * np.exp(-Z90 * se)),
        "expected_days_high": d(mean * np.exp(Z90 * se)),
    }


def value(platform, tier, price, net):
    """net payout x E[exp(-gamma T)]: what 'best' means everywhere in the payload."""
    lam = float(lam_platform(tier, [price], platform)[0])
    return net * float(sm.discounted_sale_value(lam, K, DEFAULT_GAMMA_PER_DAY)[0])


def grid_for(tier):
    lam = lam_pooled(tier, PRICES)
    pts = []
    for i, p in enumerate(PRICES):
        pp = {pl: platform_point(pl, tier, p) for pl in PLATFORMS}
        best = max(pp, key=lambda pl: value(pl, tier, p, pp[pl]["net_payout_gbp"]))
        pts.append({
            "price_gbp": r(p),
            "prob_sell_24h": r(sm.cdf(1, lam[i], K), 3),
            "prob_sell_7d": r(sm.cdf(7, lam[i], K), 3),
            "prob_sell_30d": r(sm.cdf(30, lam[i], K), 3),
            "expected_days_to_sell": d(sm.expected_days(lam[i], K)),
            "median_days_to_sell": d(sm.quantile_days(0.5, lam[i], K)),
            "ci_days_low": d(sm.quantile_days(0.05, lam[i], K)),
            "ci_days_high": d(sm.quantile_days(0.95, lam[i], K)),
            "platforms": pp,
            "best_platform": best,
        })
    return pts


def surface_for(tier):
    lam = lam_pooled(tier, PRICES)
    return {"days": [float(x) for x in SURFACE_DAYS],
            "prob": [[r(sm.cdf(t, l, K), 3) for t in SURFACE_DAYS] for l in lam]}


def best_list_price(tier, platform, grid):
    vals = [value(platform, tier, g["price_gbp"], g["platforms"][platform]["net_payout_gbp"]) for g in grid]
    return grid[int(np.argmax(vals))], max(vals)


def markers_for(tier, grid):
    p24 = [g["price_gbp"] for g in grid if g["prob_sell_24h"] >= 0.5]
    p30 = [g["price_gbp"] for g in grid if g["prob_sell_30d"] >= 0.8]
    best = max((best_list_price(tier, pl, grid) for pl in PLATFORMS), key=lambda x: x[1])[0]
    return {
        "fast_sale_gbp": max(p24) if p24 else grid[0]["price_gbp"],
        "best_value_gbp": best["price_gbp"],
        "patient_gbp": max(p30) if p30 else grid[0]["price_gbp"],
        "gamma_per_day": DEFAULT_GAMMA_PER_DAY,
    }


def confidence(n_sales, low, high):
    if n_sales >= 30 and high < 2 * low:
        return "high"
    if n_sales >= 10:
        return "medium"
    return "low"


def recommendations(tier, grid, listings):
    recs = {}
    for pl in PLATFORMS:
        g, _ = best_list_price(tier, pl, grid)
        v = g["platforms"][pl]
        n_list = sum(1 for x in listings if x["platform"] == pl)
        conf = confidence(N_SALES[pl], v["expected_days_low"], v["expected_days_high"])
        notes = []
        if conf == "low":
            notes.append(f"Only {N_SALES[pl]} observed sales on {pl.title()}; speed estimate leans on the average")
        recs[pl] = {
            "recommended_list_gbp": g["price_gbp"], **{k: v[k] for k in (
                "expected_sale_price_gbp", "net_payout_gbp", "prob_sell_7d",
                "expected_days", "expected_days_low", "expected_days_high")},
            "offer_buffer_pct": r(PLATFORMS[pl]["offer_buffer"] * 100, 1),
            "n_listings": n_list, "n_sales": N_SALES[pl], "confidence": conf, "notes": notes,
        }
    return recs


def markdown(tier, grid, platform, start_price):
    # floor: highest list price with >90% chance of selling within 48h on this platform
    floor_c = [g["price_gbp"] for g in grid
               if sm.cdf(2, float(lam_platform(tier, [g["price_gbp"]], platform)[0]), K) >= 0.9]
    floor = min(max(floor_c) if floor_c else grid[0]["price_gbp"], start_price * 0.9)
    mid = (start_price + floor) / 2
    name = "Vinted" if platform == "vinted" else "eBay"
    return [
        {"day": 0, "list_price_gbp": r(start_price), "platform": platform,
         "action": f"List on {name} at £{start_price:.2f}"},
        {"day": 4, "list_price_gbp": r(mid), "platform": platform,
         "action": f"If unsold, drop to £{mid:.2f} so watchers and favouriters get a price-drop alert"},
        {"day": 10, "list_price_gbp": r(floor), "platform": platform,
         "action": f"Floor: £{floor:.2f} (over 90% chance of selling within 48 hours)"},
    ]


def summary(listings):
    sold = [x for x in listings if x["obs_type"] != "active"]
    active = [x for x in listings if x["obs_type"] == "active"]
    med = lambda xs: float(np.median([x["price_gbp"] for x in xs])) if xs else None  # noqa: E731
    buckets = {}
    for t in TIERS:
        ps = [x["price_gbp"] for x in sold if x["condition_tier"] == t]
        lam = float(lam_pooled(t, [P.anchor[t]])[0])
        q = np.percentile(ps, [25, 50, 75]) if ps else [P.anchor[t]] * 3
        buckets[t] = {"p25": r(q[0]), "median": r(q[1]), "p75": r(q[2]),
                      "avg_days": d(sm.expected_days(lam, K)), "n": len(ps)}
    a, s = med(active), med(sold)
    return {
        "sample_size_sold": len(sold), "sample_size_active": len(active), "sample_size_excluded": 11,
        "active_median_gbp": r(a), "sold_median_gbp": r(s),
        "survivorship_bias_pct": r((a - s) / s * 100, 1),
        "condition_buckets": buckets, "data_source": "mock",
        "platforms_ok": list(PLATFORMS), "fetched_at": NOW.isoformat(),
    }


def build_market_scan():
    listings = simulate_listings(P, n_per_platform=250, now=NOW, seed=7)
    grids = {t: grid_for(t) for t in TIERS}
    markers = {t: markers_for(t, grids[t]) for t in TIERS}
    tier = ITEM["condition_tier"]
    recs = recommendations(tier, grids[tier], listings)
    best_pl = max(recs, key=lambda pl: value(pl, tier, recs[pl]["recommended_list_gbp"], recs[pl]["net_payout_gbp"]))
    n_int = sum(1 for x in listings if x["obs_type"] == "sold_interval")
    return {
        "item_metadata": ITEM,
        "cleaned_image_url": "/static/mock/cleaned_item_01.png",
        "market_summary": summary(listings),
        "model_fit": {"k": K, "lambda0": P.lambda0, "beta1": P.beta1, "beta2": P.beta2, "sigma_p": P.sigma_p,
                      "anchor_gbp": P.anchor, "platform_theta": P.theta, "platform_n_sales": N_SALES,
                      "n_obs": len(listings), "n_censored": len(listings) - n_int, "n_interval": n_int,
                      "used_priors": False, "log_likelihood": None},
        "price_range": {"min_gbp": r(PRICES[0]), "max_gbp": r(PRICES[-1]), "step_gbp": r(PRICES[1] - PRICES[0])},
        "price_curve_grid": grids,
        "sale_surface": {t: surface_for(t) for t in TIERS},
        "price_markers": markers,
        "best_platform": best_pl,
        "platform_recommendations": recs,
        "markdown_schedule": markdown(tier, grids[tier], best_pl, recs[best_pl]["recommended_list_gbp"]),
        "comparable_listings": listings[:500],
        "warnings": ["MOCK DATA: generated by scripts/make_mock.py from simulated listings, not real ones"],
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
    ms = scan.market_summary
    print(f"wrote mocks + schema; listings={len(scan.comparable_listings)} sold={ms.sample_size_sold} "
          f"active={ms.sample_size_active} bias={ms.survivorship_bias_pct}% best={scan.best_platform}")
    for pl, rec in scan.platform_recommendations.items():
        print(f"  {pl}: list £{rec.recommended_list_gbp} -> net £{rec.net_payout_gbp}, "
              f"{rec.expected_days}d ({rec.expected_days_low}-{rec.expected_days_high}), {rec.confidence}")
    print("  markers(good):", scan.price_markers["good"])


if __name__ == "__main__":
    main()
