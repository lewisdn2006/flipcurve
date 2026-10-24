"""Listings in, MarketScanResponse out: filter -> fit -> curves, markers, platforms, schedule.

    from backend.engine.pipeline import run
    payload = run(item, rows, data_source="live")   # dict that validates as MarketScanResponse

Owner: Person 1.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np

from backend.engine import survival_model as sm
from backend.engine.outlier_filter import filter_rows
from backend.engine.platform_config import DEFAULT_GAMMA_PER_DAY, PLATFORMS, SURFACE_DAYS
from backend.engine.platform_economics import expected_sale_price, markdown_schedule, net_payout, value
from backend.models.schemas import GRID_POINTS, MAX_DAYS, MAX_EVIDENCE_ROWS

TIERS = sm.TIERS


def _r(x, nd=2):
    return float(round(float(x), nd))


def _d(x):
    return _r(min(float(x), MAX_DAYS), 1)


def confidence(n_sales: int, low: float, high: float) -> str:
    """Badge rule (DECISIONS.md D3)."""
    if n_sales == 0:
        return "estimated"
    if n_sales >= 30 and high < 2 * low:
        return "high"
    if n_sales >= 10:
        return "medium"
    return "low"


def price_axis(f: sm.FitResult) -> np.ndarray:
    lo = 0.5 * f.anchors["fair"]
    hi = 1.35 * f.anchors["mint"]
    return np.round(np.linspace(lo, hi, GRID_POINTS), 2)


def _platform_points(f, tier, prices):
    out = {}
    for pl in f.platforms:
        lam = f.scale(prices, tier, pl)
        mean, lo, hi = f.mean_days_range(prices, tier, pl)
        out[pl] = {
            "lam": lam, "mean": mean, "lo": lo, "hi": hi,
            "sale": expected_sale_price(prices, pl), "net": net_payout(prices, pl),
            "p7": sm.cdf(7, lam, f.k), "value": value(prices, lam, f.k, pl),
        }
    return out


def _grid(f, tier, prices, pp):
    lam = f.scale(prices, tier)
    k = f.k
    pts = []
    for i, p in enumerate(prices):
        best = max(f.platforms, key=lambda pl: pp[pl]["value"][i])
        pts.append({
            "price_gbp": _r(p),
            "prob_sell_24h": _r(sm.cdf(1, lam[i], k), 3),
            "prob_sell_7d": _r(sm.cdf(7, lam[i], k), 3),
            "prob_sell_30d": _r(sm.cdf(30, lam[i], k), 3),
            "expected_days_to_sell": _d(sm.expected_days(lam[i], k)),
            "median_days_to_sell": _d(sm.quantile_days(0.5, lam[i], k)),
            "ci_days_low": _d(sm.quantile_days(0.05, lam[i], k)),
            "ci_days_high": _d(sm.quantile_days(0.95, lam[i], k)),
            "platforms": {pl: {
                "expected_sale_price_gbp": _r(pp[pl]["sale"][i]),
                "net_payout_gbp": _r(pp[pl]["net"][i]),
                "prob_sell_7d": _r(pp[pl]["p7"][i], 3),
                "expected_days": _d(pp[pl]["mean"][i]),
                "expected_days_low": _d(pp[pl]["lo"][i]),
                "expected_days_high": _d(pp[pl]["hi"][i]),
            } for pl in f.platforms},
            "best_platform": best,
        })
    return pts


def _markers(grid, pp, platforms):
    p24 = [g["price_gbp"] for g in grid if g["prob_sell_24h"] >= 0.5]
    p30 = [g["price_gbp"] for g in grid if g["prob_sell_30d"] >= 0.8]
    best_pl = max(platforms, key=lambda pl: pp[pl]["value"].max())
    i = int(np.argmax(pp[best_pl]["value"]))
    fast = max(p24) if p24 else grid[0]["price_gbp"]
    best = grid[i]["price_gbp"]
    patient = max(p30) if p30 else grid[0]["price_gbp"]
    # keep the three in order even when the data makes them cross
    fast, best, patient = sorted([fast, best, patient])
    return {"fast_sale_gbp": fast, "best_value_gbp": best, "patient_gbp": patient,
            "gamma_per_day": DEFAULT_GAMMA_PER_DAY}


def _summary(rows, now, data_source, platforms_ok):
    clean = [r for r in rows if not r["is_outlier"]]
    sold = [r for r in clean if r["obs_type"] != "active"]
    active = [r for r in clean if r["obs_type"] == "active"]
    med = lambda xs: float(np.median([x["price_gbp"] for x in xs])) if xs else None  # noqa: E731
    a, s = med(active), med(sold)
    buckets = {}
    for t in TIERS:
        ps = [x["price_gbp"] for x in sold if (x.get("condition_tier") or "good") == t]
        if ps:
            q = np.percentile(ps, [25, 50, 75])
            buckets[t] = {"p25": _r(q[0]), "median": _r(q[1]), "p75": _r(q[2]), "n": len(ps)}
        else:
            buckets[t] = {"p25": 0.0, "median": 0.0, "p75": 0.0, "n": 0}
    return {
        "sample_size_sold": len(sold), "sample_size_active": len(active),
        "sample_size_excluded": len(rows) - len(clean),
        "active_median_gbp": _r(a) if a else None, "sold_median_gbp": _r(s) if s else None,
        "survivorship_bias_pct": _r((a - s) / s * 100, 1) if a and s else None,
        "condition_buckets": buckets, "data_source": data_source,
        "platforms_ok": platforms_ok, "fetched_at": now.isoformat(),
    }


def _evidence(rows, rng_seed=0):
    clean = [r for r in rows if not r["is_outlier"]]
    if len(clean) <= MAX_EVIDENCE_ROWS:
        return clean
    sold = [r for r in clean if r["obs_type"] != "active"][:MAX_EVIDENCE_ROWS // 2]
    act = [r for r in clean if r["obs_type"] == "active"]
    rng = np.random.default_rng(rng_seed)
    keep = rng.choice(len(act), size=min(len(act), MAX_EVIDENCE_ROWS - len(sold)), replace=False)
    return sold + [act[i] for i in sorted(keep)]


def run(item: dict, rows: list[dict], now: datetime | None = None, data_source: str = "live",
        platforms: list[str] | None = None, prior: sm.Prior | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    platforms = platforms or list(PLATFORMS)
    rows = filter_rows(rows, item["search_queries"], item.get("is_broken", False))
    f = sm.fit(rows, platforms=platforms, prior=prior)
    prices = price_axis(f)

    grids, surfaces, markers, pps = {}, {}, {}, {}
    for t in TIERS:
        pps[t] = _platform_points(f, t, prices)
        grids[t] = _grid(f, t, prices, pps[t])
        lam = f.scale(prices, t)
        surfaces[t] = {"days": [float(x) for x in SURFACE_DAYS],
                       "prob": [[_r(sm.cdf(d, l, f.k), 3) for d in SURFACE_DAYS] for l in lam]}
        markers[t] = _markers(grids[t], pps[t], platforms)

    tier = getattr(item["condition_tier"], "value", item["condition_tier"])
    pp = pps[tier]
    n_list = {pl: sum(1 for r in rows if r["platform"] == pl and not r["is_outlier"]) for pl in platforms}
    recs = {}
    for pl in platforms:
        i = int(np.argmax(pp[pl]["value"]))
        lo, hi = float(pp[pl]["lo"][i]), float(pp[pl]["hi"][i])
        conf = confidence(f.n_sales[pl], lo, hi)
        notes = []
        if conf in ("low", "estimated"):
            notes.append(f"{f.n_sales[pl]} observed sales on {'Vinted' if pl == 'vinted' else 'eBay'}; "
                         "speed estimate leans on the average across platforms")
        recs[pl] = {
            "recommended_list_gbp": _r(prices[i]), "expected_sale_price_gbp": _r(pp[pl]["sale"][i]),
            "net_payout_gbp": _r(pp[pl]["net"][i]), "prob_sell_7d": _r(pp[pl]["p7"][i], 3),
            "expected_days": _d(pp[pl]["mean"][i]), "expected_days_low": _d(lo), "expected_days_high": _d(hi),
            "offer_buffer_pct": _r(PLATFORMS[pl]["offer_buffer"] * 100, 1),
            "n_listings": n_list[pl], "n_sales": f.n_sales[pl], "confidence": conf, "notes": notes,
        }
    best_pl = max(platforms, key=lambda pl: pp[pl]["value"].max())
    lam_best = f.scale(prices, tier, best_pl)
    schedule = markdown_schedule(prices, sm.cdf(2, lam_best, f.k), recs[best_pl]["recommended_list_gbp"], best_pl)

    warnings = []
    if f.used_priors:
        warnings.append("Few observed sales: the speed estimates lean on typical values for similar items")
    if not f.converged:
        warnings.append("Model fit did not fully converge; treat the numbers as rough")
    platforms_ok = sorted({r["platform"] for r in rows})
    return {
        "item_metadata": item,
        "market_summary": _summary(rows, now, data_source, platforms_ok),
        "model_fit": {"k": _r(f.k, 4), "lambda0": _r(f.lambda0, 4), "beta1": _r(f.beta1, 4), "beta2": _r(f.beta2, 4),
                      "sigma_p": _r(f.sigma_p, 3), "anchor_gbp": f.anchors,
                      "platform_theta": {p: _r(v, 4) for p, v in f.theta.items()}, "platform_n_sales": f.n_sales,
                      "n_obs": f.n_obs, "n_censored": f.n_censored, "n_interval": f.n_interval,
                      "used_priors": f.used_priors, "log_likelihood": _r(f.log_likelihood, 3)},
        "price_range": {"min_gbp": _r(prices[0]), "max_gbp": _r(prices[-1]), "step_gbp": _r(prices[1] - prices[0])},
        "price_curve_grid": grids,
        "sale_surface": surfaces,
        "price_markers": markers,
        "best_platform": best_pl,
        "platform_recommendations": recs,
        "markdown_schedule": schedule,
        "comparable_listings": _evidence(rows),
        "warnings": warnings,
    }
