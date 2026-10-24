"""eBay UK fetcher: active + sold searches -> ListingRow dicts, with start times from calibration.

Active listings come from a newest-first search, which shows each listing's exact start time
(obs 'active', start_source 'exact'). Sold listings show only the sold DATE, so their start
comes from the item-number calibration and their time-to-sale becomes an interval
(obs 'sold_interval', start_source 'id_calibration'). Sold rows the calibration can't date
are kept as 'sold_unknown_time' (they still inform the typical-price anchors).

Owner: Person 2.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from backend.engine.platform_config import PLATFORMS
from backend.scrapers.ebay_calibration import CalibrationStore
from backend.scrapers.ebay_parse import UK, condition_tier, parse_cards

BASE = "https://www.ebay.co.uk/sch/i.html"
# Fetching lives in backend/scrapers/fetcher.py (re-exported here for older imports).
from backend.scrapers.fetcher import Blocked, Fetcher  # noqa: E402,F401


def search_url(query: str, page: int = 1, sold: bool = False, uk_only: bool = True) -> str:
    q = {"_nkw": query, "_ipg": 240, "_pgn": page}
    if sold:
        q.update({"LH_Sold": 1, "LH_Complete": 1, "_sop": 13})  # most recently ended first
    else:
        q["_sop"] = 10  # newly listed first: the only sort that shows start times
    if uk_only:
        q["LH_PrefLoc"] = 1
    return f"{BASE}?{urlencode(q)}"


def calibrate(store: CalibrationStore, fetcher: Fetcher, queries: list[str], pages=(1, 3, 6, 10),
              now: datetime | None = None, log=print) -> int:
    """Add (item number, start time) points from newest-first searches. Returns points added."""
    now = now or datetime.now(timezone.utc)
    added = 0
    for q in queries:
        for p in pages:
            try:
                cards = parse_cards(fetcher.get(search_url(q, p, uk_only=False)), now)
            except Blocked as e:
                log(f"  blocked on '{q}' p{p}: {e}")
                return added
            pts = [(c["item_id"], c["start_time"]) for c in cards if c["start_time"]]
            added += store.add(pts, source=f"cal:{q}")
            if len(cards) < 50:
                break  # ran out of results for this query
    return added


def _base_row(c: dict, now: datetime) -> dict:
    cfg = PLATFORMS["ebay"]
    ship = c["shipping_gbp"] or 0.0
    fee = round(c["price_gbp"] * cfg["buyer_fee_pct"] + cfg["buyer_fee_fixed_gbp"], 2)
    return {
        "listing_id": c["item_id"], "platform": "ebay", "title": c["title"], "url": c["url"],
        "price_gbp": c["price_gbp"], "shipping_gbp": ship, "buyer_fee_gbp": fee,
        "total_buyer_price_gbp": round(c["price_gbp"] + ship + fee, 2),
        "condition_tier": condition_tier(c["condition_raw"]), "condition_raw": c["condition_raw"],
        "observed_at": now.isoformat(), "favourite_count": None,
        "best_offer_accepted": c["best_offer_accepted"] or None,
        "is_outlier": False, "outlier_reason": None,
    }


def active_rows(cards: list[dict], now: datetime) -> list[dict]:
    rows = []
    for c in cards:
        if c["start_time"] is None or c["from_abroad"]:
            continue
        age = (now - c["start_time"]).total_seconds() / 86400
        rows.append({**_base_row(c, now), "obs_type": "active", "t_days": round(max(age, 0.0), 4),
                     "t_upper_days": None, "start_source": "exact",
                     "listed_at": c["start_time"].isoformat(), "sold_at": None})
    return rows


def sold_rows(cards: list[dict], store: CalibrationStore, now: datetime) -> list[dict]:
    rows = []
    for c in cards:
        if c["sold_date"] is None or c["from_abroad"]:
            continue
        day_start = datetime(c["sold_date"].year, c["sold_date"].month, c["sold_date"].day, tzinfo=UK)
        day_end = day_start + timedelta(days=1)
        est = store.estimate_start(c["item_id"])
        base = {**_base_row(c, now), "sold_at": day_start.isoformat()}
        if est is not None:
            lo = (day_start - est.hi).total_seconds() / 86400
            hi = (day_end - est.lo).total_seconds() / 86400
            if hi >= 0:
                rows.append({**base, "obs_type": "sold_interval", "t_days": round(max(lo, 0.0), 4),
                             "t_upper_days": round(hi, 4), "start_source": "id_calibration",
                             "listed_at": est.mid.isoformat()})
                continue
        rows.append({**base, "obs_type": "sold_unknown_time", "t_days": None, "t_upper_days": None,
                     "start_source": "unknown", "listed_at": None})
    return rows


def scan(query: str, store: CalibrationStore, fetcher: Fetcher, active_pages: int = 3, sold_pages: int = 3,
         now: datetime | None = None) -> tuple[list[dict], dict]:
    """Fetch active + sold listings for one item query as a COHORT (maths doc, section 5):
    only listings that started after `window_start` are kept, where window_start is the
    earliest time both searches fully cover. Inside the window every listing is seen whether
    it sold or not, so no left truncation (t_entry_days = 0). Active pages also feed calibration."""
    now = now or datetime.now(timezone.utc)
    act_cards, sold_cards = [], []
    act_complete = sold_complete = False
    from backend.scrapers import ebay_api
    if ebay_api.configured():  # official API for active listings when keys are set
        act_cards = ebay_api.BrowseAPI().active_cards(query, pages=active_pages)
        act_complete = len(act_cards) < 200 * active_pages
        store.add([(c["item_id"], c["created_time"]) for c in act_cards if c.get("created_time")],
                  source=f"api:{query}")
    else:
        for p in range(1, active_pages + 1):
            cards = parse_cards(fetcher.get(search_url(query, p)), now)
            act_cards += cards
            store.add([(c["item_id"], c["start_time"]) for c in cards if c["start_time"]], source=f"item:{query}")
            if len(cards) < 200:
                act_complete = True
                break
    sold_refused = None
    for p in range(1, sold_pages + 1):
        try:
            cards = parse_cards(fetcher.get(search_url(query, p, sold=True)), now)
        except Blocked as e:  # keep what we have; don't retry around it
            sold_refused = str(e)
            break
        sold_cards += cards
        if len(cards) < 200:
            sold_complete = True
            break

    rows, stats = build_rows(act_cards, sold_cards, store, now, act_complete, sold_complete, sold_refused)
    stats["fetcher"] = fetcher.kind
    return rows, stats


def build_rows(act_cards: list[dict], sold_cards: list[dict], store: CalibrationStore, now: datetime,
               act_complete: bool = False, sold_complete: bool = False,
               sold_refused: str | None = None) -> tuple[list[dict], dict]:
    """Turn parsed search cards into a cohort of ListingRows (used by scan() and by the
    saved-page importer). Pass *_complete=True only if the pages reached the end of the results."""
    window_start = now - timedelta(days=90)  # eBay's sold search only goes back ~90 days
    starts = [c["start_time"] for c in act_cards if c["start_time"]]
    if not act_complete and starts:
        window_start = max(window_start, min(starts))
    sold_days = [c["sold_date"] for c in sold_cards if c["sold_date"]]
    if not sold_complete and sold_days:
        d = min(sold_days)
        # a listing that started after the window opened and sold must have sold after it too
        window_start = max(window_start, datetime(d.year, d.month, d.day, tzinfo=UK) + timedelta(days=1))

    if sold_refused and not sold_cards:
        # no sold side: active listings are survivors we happened to see, so they carry prices
        # but no speed information (entry age = age now, maths doc section 5)
        a = active_rows(act_cards, now)
        for r in a:
            r["t_entry_days"] = r["t_days"]
        return a, {"active": len(a), "sold": 0, "sold_dated": 0, "coverage": None, "window_days": None,
                   "sold_refused": sold_refused}
    a = [r for r in active_rows(act_cards, now) if datetime.fromisoformat(r["listed_at"]) >= window_start]
    s_all = sold_rows(sold_cards, store, now)
    s = [r for r in s_all if r["obs_type"] != "sold_interval" or datetime.fromisoformat(r["listed_at"]) >= window_start]
    for r in a + s:
        r["t_entry_days"] = 0.0 if r["obs_type"] != "sold_unknown_time" else None
    # (if the sold side was cut short by a refusal, window_start above already reflects how
    #  far back the sold pages we did get reach, so the cohort stays balanced)
    dated = [r for r in s if r["obs_type"] == "sold_interval"]
    stats = {"active": len(a), "sold": len(s), "sold_dated": len(dated),
             "coverage": round(sum(r["obs_type"] == "sold_interval" for r in s_all) / len(s_all), 3) if s_all else None,
             "window_days": round((now - window_start).total_seconds() / 86400, 2)}
    return a + s, stats
