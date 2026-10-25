"""Junk and outlier removal before fitting (step 10).

Rows are never deleted: each gets is_outlier / outlier_reason, so the evidence drawer can show
what was excluded and why. Three passes:
  1. Keywords: "box only", "spares", bundles, fakes... (repair words allowed if the item is broken)
  2. Relevance: the title must share at least 70% of the search query's meaningful words.
     A different version of the item (Mid when we searched Low, Pro / Max / Mini when the search
     didn't say so, kids' sizes when the item isn't kids') is not thrown away but tagged
     `related` ("mid", "kids"...): the pipeline uses it for selling speed with its price rescaled,
     never for the price level. (Found 25 Oct: eBay's "air jordan 1 low triple black" search was
     ~75% Jordan 1 Mid and ~25% kids' sizes.)
  3. Price: robust (median/MAD) fences on condition-adjusted log price, per platform

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
# Mutually exclusive versions: if the search names one, a title naming another is a different item
EXCLUSIVE = [{"low", "mid", "high", "hi"}]
# Model-tier words: a title with one the search doesn't have is a different (usually pricier) item
TIER_WORDS = {"pro", "max", "plus", "mini", "ultra", "lite", "se", "xl", "og", "sp"}
KIDS = {"gs", "junior", "junior's", "kids", "kid's", "infant", "infants", "toddler", "baby", "youth",
        "boys", "girls", "td", "ps", "child", "childs", "children", "children's", "jnr", "bgs"}
# Colour words: a title naming a colour the item doesn't have is another colourway (25 Oct:
# "nike air jordan 1 low triple black" matched Panda, Game Royal, Travis Scott... at 70% overlap)
COLOURS = {"black", "white", "red", "blue", "royal", "navy", "green", "grey", "gray", "pink", "purple", "lilac",
           "orange", "yellow", "brown", "beige", "tan", "cream", "sail", "ivory", "gold", "silver",
           "bronze", "teal", "turquoise", "olive", "khaki", "burgundy", "maroon", "violet",
           "multi", "multicolour", "multicolor", "panda", "bred", "chicago", "mocha", "smoke",
           "volt", "lime", "coral", "peach", "camo", "rose", "aqua", "cyan", "magenta", "neon"}
# words that are versions (handled as `related`), so must_not_have doesn't throw those away
VERSION_WORDS = set().union(*EXCLUSIVE) | TIER_WORDS | KIDS
# share of the query's words a title needs (0.5 let 'Air Force 1 Triple Black' through for
# 'jordan 1 low triple black'; 25 Oct)
RELEVANCE = 0.7
FENCE_K = 3.0
TIER_RATIO = {"fair": 0.65, "good": 1.0, "very_good": 1.32, "mint": 1.9}  # = survival_model.DEFAULT_TIER_RATIO
MIN_FOR_FENCES = 8


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", text.lower()) if len(w) > 1 and w not in STOP}


def _mark(r: dict, reason: str):
    if not r.get("is_outlier"):
        r["is_outlier"], r["outlier_reason"] = True, reason


def _all_words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9']+", (text or "").lower()))


def version_mismatch(title: str, query: str, item_text: str = "") -> str | None:
    """Which different version of the searched item this title is ("mid", "pro", "kids"), or None."""
    t, q = _all_words(title), _all_words(query) | _all_words(item_text)
    for group in EXCLUSIVE:
        mine = q & group
        if mine and not (t & mine):
            other = t & (group - mine)
            if other:
                return sorted(other)[0]
    extra = (t & TIER_WORDS) - q
    if extra:
        return sorted(extra)[0]
    if (t & KIDS) and not (q & KIDS):
        return "kids"
    return None


def _has(text_words: list[str], term: str) -> bool:
    """term may be several words ("iphone 13") and alternatives ("triple black|all black")."""
    padded = " " + " ".join(text_words) + " "
    return any(" " + " ".join(_ordered_words(alt)) + " " in padded for alt in term.split("|") if alt.strip())


def _ordered_words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", (text or "").lower().replace("/", " "))


def other_item_reason(title: str, query: str, item_text: str = "", must_have=(), must_not_have=()) -> str | None:
    """Why this title is a different item (not just a different version), or None."""
    words = _ordered_words(title)
    for term in must_have:
        if not _has(words, term):
            return f"missing '{term.split('|')[0]}'"
    for term in must_not_have:
        if set(_ordered_words(term)) <= VERSION_WORDS:
            continue  # Mid / kids / Pro... are kept as related versions instead
        if _has(words, term):
            return f"different item: {term}"
    q_all = _all_words(query) | _all_words(item_text)
    if "x" in words and "x" not in q_all:  # "Travis Scott x Nike ...": a collaboration
        return "collaboration"
    mine = q_all & COLOURS
    if mine:
        other = (set(words) & COLOURS) - mine
        if other:
            return f"different colour: {sorted(other)[0]}"
    return None


def filter_rows(rows: list[dict], queries: dict[str, str], item_is_broken: bool = False,
                item_text: str = "", must_have=(), must_not_have=()) -> list[dict]:
    """queries: platform -> search query used; item_text: the item's title/model; must_have /
    must_not_have: the identifier's title rules. Returns the same rows (copied) with outliers marked."""
    rows = [dict(r, is_outlier=False, outlier_reason=None, related=None) for r in rows]
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
        if q and len(q & _words(r["title"])) < RELEVANCE * len(q):
            _mark(r, "not a close title match")
            continue
        why = other_item_reason(r["title"], queries.get(r["platform"], ""), item_text, must_have, must_not_have)
        if why:
            _mark(r, why)
            continue
        r["related"] = version_mismatch(r["title"], queries.get(r["platform"], ""), item_text)

    # Price fences, per platform (Vinted is usually cheaper than eBay; pooling them flagged
    # ordinary Vinted listings, 24 Oct) and per related version (a Mid has its own level).
    # Prices are first put on a common footing with the typical condition ratios (per-condition
    # medians were fragile: two pricey "new" collabs made the mint median £157, 25 Oct), then a
    # robust centre and spread (median / MAD) flag anything 3 spreads away.
    for platform, rel in {(r["platform"], r.get("related")) for r in rows}:
        keep = [r for r in rows if r["platform"] == platform and r.get("related") == rel and not r["is_outlier"]]
        if len(keep) < MIN_FOR_FENCES:
            continue
        z = np.array([np.log(r["price_gbp"] / TIER_RATIO.get(r.get("condition_tier") or "good", 1.0)) for r in keep])
        centre = float(np.median(z))
        spread = max(1.4826 * float(np.median(np.abs(z - centre))), 0.15)
        for r, zi in zip(keep, z):
            if zi < centre - FENCE_K * spread:
                _mark(r, "price far below similar listings")
            elif zi > centre + FENCE_K * spread:
                _mark(r, "price far above similar listings")
    return rows
