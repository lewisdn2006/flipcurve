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


RELATED_MIN = 5
RELATED_LABEL = {"mid": "Mid", "low": "Low", "high": "High", "hi": "High", "kids": "kids' sizes",
                 "pro": "Pro", "max": "Max", "plus": "Plus", "mini": "Mini", "ultra": "Ultra",
                 "lite": "Lite", "se": "SE", "xl": "XL"}


def _with_related(rows):
    """Similar-but-different versions (filter tags them `related`) join the speed fit with their
    prices rescaled to this item's level (their own median sold or listed price, by condition):
    a Jordan 1 Mid at its usual price is taken to sell about as fast as a Low at the Low's usual
    price. They never set the price level. Tiny groups are dropped as outliers.
    Returns (rows, rows for the fit, {label: n used})."""
    exact = [r for r in rows if not r.get("related")]
    base = sm.estimate_anchors(exact)["good"] if exact else None
    fit_rows = [r for r in exact if not r["is_outlier"]]
    used = {}
    for g in sorted({r["related"] for r in rows if r.get("related") and not r["is_outlier"]}):
        grp = [r for r in rows if r.get("related") == g and not r["is_outlier"]]
        if len(grp) < RELATED_MIN or base is None:
            for r in grp:
                r["is_outlier"], r["outlier_reason"] = True, f"different model ({RELATED_LABEL.get(g, g)})"
            continue
        ratio = base / sm.estimate_anchors(grp)["good"]
        fit_rows += [dict(r, price_gbp=r["price_gbp"] * ratio) for r in grp]
        used[RELATED_LABEL.get(g, g)] = len(grp)
    return rows, fit_rows, used


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


PATIENT_GAMMA_PER_DAY = 0.003  # "happy to wait": a month's wait costs under 10%


def _markers(grid, pp, platforms, anchor, k):
    """Three list prices for three levels of urgency, always quick <= best value <= patient.
    Quick sale: highest price with a 50% chance of selling within a week.
    Best value: maximises payout x E[exp(-gamma T)] at the default urgency (gamma = 1%/day).
    Patient: the same at gamma = 0.3%/day.
    If no price on the chart reaches a 50% chance within a week (a slow market), we don't pretend
    the bottom of the chart works (25 Oct: it showed £10 for £30 trainers): quick sale falls back
    to 15% under the typical sold price for the condition, with a note."""
    prices = np.array([g["price_gbp"] for g in grid])
    best_pl = max(platforms, key=lambda pl: pp[pl]["value"].max())
    best = float(prices[int(np.argmax(pp[best_pl]["value"]))])
    patient_value = pp[best_pl]["net"] * sm.discounted_sale_value(pp[best_pl]["lam"], k, PATIENT_GAMMA_PER_DAY)
    patient = max(float(prices[int(np.argmax(patient_value))]), best)
    p7 = prices[np.array([g["prob_sell_7d"] for g in grid]) >= 0.5]
    note = None
    if p7.size:
        fast = float(p7.max())
    else:
        fast = 0.85 * anchor
        note = ("Even below the usual price this item rarely sells within a week, so the quick-sale "
                "price is set just under what similar ones sold for")
    fast = min(fast, best)
    return {"fast_sale_gbp": _r(fast), "best_value_gbp": _r(best), "patient_gbp": _r(patient),
            "gamma_per_day": DEFAULT_GAMMA_PER_DAY, "note": note}


def _summary(rows, now, data_source, platforms_ok):
    clean = [r for r in rows if not r["is_outlier"] and not r.get("related")]
    sold = [r for r in clean if r["obs_type"] != "active"]
    active = [r for r in clean if r["obs_type"] == "active"]
    med = lambda xs: float(np.median([x["price_gbp"] for x in xs])) if xs else None  # noqa: E731
    a, s = med(active), med(sold)
    # survivorship gap within one platform: Vinted asks are lower than eBay sales, so mixing
    # platforms made the gap negative (25 Oct)
    by_pl = {}
    for pl in {r["platform"] for r in sold}:
        ps, pa = [r for r in sold if r["platform"] == pl], [r for r in active if r["platform"] == pl]
        if len(ps) >= 3 and len(pa) >= 3:
            by_pl[pl] = (len(ps), med(pa), med(ps))
    gap = None
    if by_pl:
        _, pa, ps = max(by_pl.values())
        gap = (pa - ps) / ps * 100
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
        "survivorship_bias_pct": _r(gap, 1) if gap is not None else None,
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
    item_text = " ".join(str(item.get(k) or "") for k in ("title", "model_name"))
    rows = filter_rows(rows, item["search_queries"], item.get("is_broken", False), item_text=item_text)
    rows, fit_rows, related = _with_related(rows)
    f = sm.fit(fit_rows, platforms=platforms, prior=prior, anchors=sm.estimate_anchors(
        [r for r in rows if not r.get("related")]))
    prices = price_axis(f)

    grids, surfaces, markers, pps = {}, {}, {}, {}
    for t in TIERS:
        pps[t] = _platform_points(f, t, prices)
        grids[t] = _grid(f, t, prices, pps[t])
        lam = f.scale(prices, t)
        surfaces[t] = {"days": [float(x) for x in SURFACE_DAYS],
                       "prob": [[_r(sm.cdf(d, l, f.k), 3) for d in SURFACE_DAYS] for l in lam]}
        markers[t] = _markers(grids[t], pps[t], platforms, f.anchors[t], f.k)

    tier = getattr(item["condition_tier"], "value", item["condition_tier"])
    pp = pps[tier]
    n_list = {pl: sum(1 for r in fit_rows if r["platform"] == pl) for pl in platforms}
    n_rel_sales = {pl: sum(1 for r in fit_rows if r["platform"] == pl and r.get("related")
                           and r["obs_type"] in ("sold_exact", "sold_interval")) for pl in platforms}
    n_stock = {pl: sum(1 for r in fit_rows if r["platform"] == pl and r.get("stock_window_days"))
               for pl in platforms}
    recs = {}
    for pl in platforms:
        i = int(np.argmax(pp[pl]["value"]))
        lo, hi = float(pp[pl]["lo"][i]), float(pp[pl]["hi"][i])
        conf = confidence(f.n_sales[pl], lo, hi)
        notes = []
        site = "Vinted" if pl == "vinted" else "eBay"
        if conf == "estimated" and n_stock[pl] >= 20:
            conf = "low"
            notes.append(f"No {site} sales seen yet: speed comes from how long the {n_stock[pl]} "
                         f"current {site} listings have been up (scan again later to catch sales)")
        elif conf in ("low", "estimated"):
            notes.append(f"{f.n_sales[pl]} observed sales on {site}; "
                         "speed estimate leans on the average across platforms")
        if n_rel_sales[pl]:
            notes.append(f"{n_rel_sales[pl]} of the {site} sales are similar items "
                         f"({', '.join(related)}), with prices scaled to this one")
        recs[pl] = {
            "recommended_list_gbp": _r(prices[i]), "expected_sale_price_gbp": _r(pp[pl]["sale"][i]),
            "net_payout_gbp": _r(pp[pl]["net"][i]), "prob_sell_7d": _r(pp[pl]["p7"][i], 3),
            "expected_days": _d(pp[pl]["mean"][i]), "expected_days_low": _d(lo), "expected_days_high": _d(hi),
            "offer_buffer_pct": _r(PLATFORMS[pl]["offer_buffer"] * 100, 1),
            "n_listings": n_list[pl], "n_sales": f.n_sales[pl], "confidence": conf, "notes": notes,
        }
    best_pl = max(platforms, key=lambda pl: pp[pl]["value"].max())
    lam_best = f.scale(prices, tier, best_pl)
    schedule = markdown_schedule(prices, sm.cdf(2, lam_best, f.k), recs[best_pl]["recommended_list_gbp"], best_pl,
                                 fallback_floor=markers[tier]["fast_sale_gbp"])

    warnings = []
    if f.used_priors:
        warnings.append("Few observed sales: the speed estimates lean on typical values for similar items")
    if related:
        warnings.append("Few exact matches, so selling speed also uses similar items ("
                        + ", ".join(f"{k}: {v}" for k, v in related.items())
                        + " listings), each scaled to this item's price level")
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
