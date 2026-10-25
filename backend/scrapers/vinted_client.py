"""Vinted UK fetcher: catalogue search -> active ListingRow dicts with ages from ID calibration,
plus the optional re-check that turns listings seen earlier into sold intervals.

Fetching goes through backend/scrapers/fetcher.py (real browser by default, no attempts to
get around a refusal). If Vinted refuses, the orchestrator uses the cached results.

Owner: Person 2.
"""
from __future__ import annotations

import numpy as np

from datetime import datetime, timezone
from urllib.parse import urlencode

from backend.scrapers.fetcher import Blocked, Fetcher
from backend.scrapers.vinted_calibration import VintedCalibration
from backend.scrapers.vinted_parse import CONDITION_TIER, parse_catalog, parse_item_page

BASE = "https://www.vinted.co.uk"
SHIPPING_GBP = 3.20  # typical UK postage shown at checkout; VERIFY per category if it matters


def catalog_url(query: str, page: int = 1) -> str:
    return f"{BASE}/catalog?{urlencode({'search_text': query, 'order': 'newest_first', 'page': page})}"


def fetch_item(fetcher: Fetcher, item_id: str) -> dict:
    try:
        html = fetcher.get(f"{BASE}/items/{item_id}")
        return parse_item_page(html, 200)
    except Blocked as e:
        if "HTTP 404" in str(e):
            return parse_item_page(None, 404)
        raise


def calibrate_from_pages(cal: VintedCalibration, fetcher: Fetcher, item_ids: list[str],
                         now: datetime | None = None) -> int:
    """Open a spread of item pages and record their 'Uploaded ... ago' bounds."""
    now = now or datetime.now(timezone.utc)
    added = 0
    for iid in item_ids:
        info = fetch_item(fetcher, iid)
        if info["age_hours"]:
            cal.add_age(iid, info["age_hours"], now)
            added += 1
    cal.save()
    return added


STOCK_MIN_ROWS = 20
STOCK_MAX_DAYS = 120.0


def _row(c: dict, now: datetime) -> dict:
    return {
        "listing_id": c["item_id"], "platform": "vinted", "title": c["title"], "url": c["url"],
        "price_gbp": c["price_gbp"], "shipping_gbp": SHIPPING_GBP, "buyer_fee_gbp": c["buyer_fee_gbp"],
        "total_buyer_price_gbp": round(c["price_gbp"] + SHIPPING_GBP + c["buyer_fee_gbp"], 2),
        "condition_tier": CONDITION_TIER.get(c["condition_raw"]), "condition_raw": c["condition_raw"],
        "observed_at": now.isoformat(), "favourite_count": c["favourite_count"],
        "best_offer_accepted": None, "is_outlier": False, "outlier_reason": None, "sold_at": None,
    }


def active_rows(cards: list[dict], cal: VintedCalibration, now: datetime) -> list[dict]:
    rows = []
    for c in cards:
        b = cal.bounds(c["item_id"])
        if b is None:
            continue
        # censored at the youngest plausible age: "unsold for at least this long" stays true
        age_min = max((now - b.hi).total_seconds() / 86400, 0.0)
        # a single scrape only shows survivors: entry age = age now (left truncation). On its own
        # that says nothing about speed; the stock window below is what does.
        rows.append({**_row(c, now), "obs_type": "active", "t_days": round(age_min, 4), "t_upper_days": None,
                     "t_entry_days": round(age_min, 4), "start_source": "id_calibration",
                     "listed_at": b.mid.isoformat()})
    # Newest-first catalogue read back to age A: the age spread of what's still listed tells us how
    # fast things go (stock sampling, survival_model). It needs a day or more to average over.
    # Ages here use the best-guess start (interpolated), not the youngest bound.
    if len(rows) >= STOCK_MIN_ROWS:
        ages = {r["listing_id"]: max((now - datetime.fromisoformat(r["listed_at"])).total_seconds() / 86400, 0.0)
                for r in rows}
        # A = how far back the pages reach: the typical age of the last 5% of cards in catalogue
        # order (newest first). A percentile of all ages cut it short when a rarely listed item's
        # pages went back a year (25 Oct); bumped old listings scattered earlier don't move this.
        in_order = [ages[r["listing_id"]] for r in rows]
        window = float(np.median(in_order[-max(3, len(in_order) // 20):]))
        # Listings still up after months are mostly forgotten ones (a niche tee's pages reached
        # back 650 days, 25 Oct); past STOCK_MAX_DAYS they say more about sellers than speed.
        window = min(window, STOCK_MAX_DAYS)
        if window >= 1.0:
            for r in rows:
                a = round(ages[r["listing_id"]], 4)
                r.update(t_days=a, t_entry_days=a, stock_window_days=round(window, 4))
    return rows


def scan(query: str, cal: VintedCalibration, fetcher: Fetcher, pages: int = 2, calibrate_n: int = 12,
         now: datetime | None = None) -> tuple[list[dict], list[dict], dict]:
    """Returns (rows, raw cards, stats). Keep the raw cards: a later recheck() turns them into sales."""
    now = now or datetime.now(timezone.utc)
    cards = []
    for p in range(1, pages + 1):
        got = parse_catalog(fetcher.get(catalog_url(query, p)))
        cards += got
        if len(got) < 90:
            break
    if cards:
        ids = sorted(int(c["item_id"]) for c in cards)
        cal.add_exact(ids[-1], now, slack_minutes=60)  # the newest listing is from the last hour or so
        # open a spread of item pages across the ID range to calibrate older listings
        step = max(1, len(ids) // max(calibrate_n, 1))
        calibrate_from_pages(cal, fetcher, [str(i) for i in ids[::step][:calibrate_n]], now)
    rows = active_rows(cards, cal, now)
    return rows, cards, {"cards": len(cards), "rows": len(rows), "calibration_samples": len(cal.samples)}


def recheck(first_cards: list[dict], first_seen: datetime, cal: VintedCalibration, fetcher: Fetcher,
            now: datetime | None = None) -> tuple[list[dict], dict]:
    """Re-open listings scraped at `first_seen`. Any now sold -> sold in (age then, age now]."""
    now = now or datetime.now(timezone.utc)
    rows, counts = [], {"active": 0, "sold": 0, "deleted": 0, "unknown": 0}
    for c in first_cards:
        info = fetch_item(fetcher, c["item_id"])
        counts[info["status"]] += 1
        b = cal.bounds(c["item_id"])
        if b is None:
            continue
        entry = max((first_seen - b.hi).total_seconds() / 86400, 0.0)  # age when first seen (youngest bound)
        if info["status"] == "sold":
            hi = max((now - b.lo).total_seconds() / 86400, entry)
            rows.append({**_row(c, now), "obs_type": "sold_interval", "t_days": round(entry, 4),
                         "t_upper_days": round(hi, 4), "t_entry_days": round(entry, 4),
                         "start_source": "id_calibration", "listed_at": b.mid.isoformat()})
        elif info["status"] == "active":
            age_min = max((now - b.hi).total_seconds() / 86400, entry)
            rows.append({**_row(c, now), "obs_type": "active", "t_days": round(age_min, 4),
                         "t_upper_days": None, "t_entry_days": round(entry, 4),
                         "start_source": "id_calibration", "listed_at": b.mid.isoformat()})
    return rows, counts
