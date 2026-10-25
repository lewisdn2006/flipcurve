"""Junk and outlier removal before fitting (step 10).

Rows are never deleted: each gets is_outlier / outlier_reason, so the evidence drawer can show
what was excluded and why. Three passes:
  1. Keywords: "box only", "spares", bundles, fakes... (repair words allowed if the item is broken)
  2. Relevance: the title must share at least half of the search query's meaningful words
  3. Price: two-sided Tukey fences on log price relative to its condition's median, per platform

Owner: Person 1.
"""
from __future__ import annotations

import re

import numpy as np

NEGATIVE = ["box only", "empty box", "case only", "bundle", "joblot", "job lot", "lot of", "replica",
            "fake", "not genuine", "unbranded", "inspired", "laces only", "insoles only", "x2 pairs",
            "wanted", "swap"]
REPAIR = ["broken", "faulty", "spares", "repair", "for parts", "not working", "parts only"]
STOP = {"the", "a", "an", "and", "or", "for", "with", "of", "in", "size", "uk", "eu", "us", "mens",
        "men's", "womens", "women's", "new", "used"}
TUKEY_K = 1.5
MIN_FOR_FENCES = 8


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", text.lower()) if len(w) > 1 and w not in STOP}


def _mark(r: dict, reason: str):
    if not r.get("is_outlier"):
        r["is_outlier"], r["outlier_reason"] = True, reason


def filter_rows(rows: list[dict], queries: dict[str, str], item_is_broken: bool = False) -> list[dict]:
    """queries: platform -> search query used. Returns the same rows (copied) with outliers marked."""
    rows = [dict(r, is_outlier=False, outlier_reason=None) for r in rows]
    bad_words = NEGATIVE + ([] if item_is_broken else REPAIR)
    for r in rows:
        title = r["title"].lower()
        hit = next((w for w in bad_words if w in title), None)
        if hit:
            _mark(r, f"keyword: {hit}")
            continue
        cond = (r.get("condition_raw") or "").lower()
        if not item_is_broken and ("parts" in cond or "not working" in cond):
            _mark(r, "condition: for parts")
            continue
        q = _words(queries.get(r["platform"], ""))
        if q and len(q & _words(r["title"])) < 0.5 * len(q):
            _mark(r, "not a close title match")

    # price fences on log(price / condition median), per platform: Vinted is usually cheaper than
    # eBay, so pooling them flagged ordinary Vinted listings as "far below" (found 24 Oct).
    # Tiers are pooled within a platform so thin tiers still get checked.
    for platform in {r["platform"] for r in rows}:
        keep = [r for r in rows if r["platform"] == platform and not r["is_outlier"]]
        if len(keep) < MIN_FOR_FENCES:
            continue
        tiers: dict[str, list[float]] = {}
        for r in keep:
            tiers.setdefault(r.get("condition_tier") or "good", []).append(r["price_gbp"])
        med = {t: float(np.median(v)) for t, v in tiers.items()}
        resid = np.array([np.log(r["price_gbp"] / med[r.get("condition_tier") or "good"]) for r in keep])
        q1, q3 = np.percentile(resid, [25, 75])
        spread = max(q3 - q1, 0.15)  # floor: a tight cluster shouldn't make normal prices look extreme
        lo, hi = q1 - TUKEY_K * spread, q3 + TUKEY_K * spread
        for r, z in zip(keep, resid):
            if z < lo:
                _mark(r, "price far below similar listings")
            elif z > hi:
                _mark(r, "price far above similar listings")
    return rows
