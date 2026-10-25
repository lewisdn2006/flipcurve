"""Turn pages collected by the FlipCurve helper extension into ListingRows.

The extension loads pages in the user's own signed-in browser and sends them here (see
extension/README.md). Each page is {platform, kind, page, url, final_url, status, html, fetched_at}:
  eBay    kind 'active' (newest-listed, has start times) / 'sold' (recently sold)
  Vinted  kind 'catalog' (newest listings) / 'item' (one listing, for its upload age)
          / 'recheck' (a listing we saw in an earlier scan: sold since, or still active?)

Every scan also improves the shared calibrations: eBay active pages add (item number, start
time) points; Vinted item pages add (ID, upload age) samples.

Owner: Person 2.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from backend.scrapers.ebay_calibration import CalibrationStore
from backend.scrapers.ebay_client import build_rows
from backend.scrapers.ebay_parse import parse_cards
from backend.scrapers.orchestrator import cache_path, save_cache
from backend.scrapers.vinted_calibration import VintedCalibration
from backend.scrapers.vinted_client import _row as vinted_row
from backend.scrapers.vinted_client import active_rows as vinted_active_rows
from backend.scrapers.vinted_parse import parse_catalog, parse_item_page

RECHECK_MIN_AGE = timedelta(hours=6)   # re-check Vinted listings first seen at least this long ago
RECHECK_MAX = 8


def _ts(p) -> datetime:
    t = p.get("fetched_at")
    if isinstance(t, datetime):
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    return datetime.fromisoformat(str(t).replace("Z", "+00:00")) if t else datetime.now(timezone.utc)


def ebay_rows(pages: list[dict], query: str, store: CalibrationStore | None = None) -> tuple[list[dict], dict]:
    store = store or CalibrationStore()
    act, sold = [], []
    now = max((_ts(p) for p in pages), default=datetime.now(timezone.utc))
    act_complete = sold_complete = False
    for p in sorted(pages, key=lambda p: (p["kind"], p.get("page") or 0)):
        cards = parse_cards(p["html"], _ts(p))
        if p["kind"] == "active":
            act += cards
            store.add([(c["item_id"], c["start_time"]) for c in cards if c["start_time"]], source=f"ext:{query}")
            act_complete = act_complete or len(cards) < 200
        elif p["kind"] == "sold":
            sold += cards
            sold_complete = sold_complete or len(cards) < 200
    act = list({c["item_id"]: c for c in act}.values())
    sold = list({c["item_id"]: c for c in sold}.values())
    if not act and not sold:
        return [], {"active": 0, "sold": 0}
    rows, stats = build_rows(act, sold, store, now, act_complete, sold_complete,
                             sold_refused=None if sold or not act else "sold pages missing")
    save_cache(query, "ebay", rows, {"stats": stats, "source": "extension"})
    return rows, stats


def _load_vinted_history(query: str) -> dict:
    p = cache_path(query, "vinted")
    if not p.exists():
        return {"seen": {}}
    data = json.loads(p.read_text())
    return {"seen": data.get("seen", {})}


def recheck_ids(query: str, now: datetime | None = None) -> list[str]:
    """Vinted listings from earlier scans worth re-checking now (oldest first)."""
    now = now or datetime.now(timezone.utc)
    seen = _load_vinted_history(query)["seen"]
    due = [(v["first_seen"], k) for k, v in seen.items()
           if not v.get("resolved") and now - datetime.fromisoformat(v["first_seen"]) >= RECHECK_MIN_AGE]
    return [k for _, k in sorted(due)[:RECHECK_MAX]]


def vinted_rows(pages: list[dict], query: str, cal: VintedCalibration | None = None) -> tuple[list[dict], dict]:
    cal = cal or VintedCalibration()
    now = max((_ts(p) for p in pages), default=datetime.now(timezone.utc))
    hist = _load_vinted_history(query)
    seen: dict = hist["seen"]

    cards = []
    for p in pages:
        if p["kind"] == "catalog":
            cards += parse_catalog(p["html"])
    cards = list({c["item_id"]: c for c in cards}.values())
    if cards:
        cal.add_exact(max(int(c["item_id"]) for c in cards), now, slack_minutes=60)
    for p in pages:
        if p["kind"] == "item":
            info = parse_item_page(p["html"], p.get("status", 200))
            if info["age_hours"]:
                cal.add_age(p["url"].rstrip("/").split("/")[-1], info["age_hours"], _ts(p))
    cal.save()

    rows, n_sold, n_active_again = [], 0, 0
    # re-checks: listings first seen earlier; sold since -> a real sale interval
    for p in pages:
        if p["kind"] != "recheck":
            continue
        iid = p["url"].rstrip("/").split("/")[-1]
        rec = seen.get(iid)
        b = cal.bounds(iid)
        if not rec or b is None:
            continue
        info = parse_item_page(p["html"], p.get("status", 200))
        first = datetime.fromisoformat(rec["first_seen"])
        entry = max((first - b.hi).total_seconds() / 86400, 0.0)
        if info["status"] == "sold":
            hi = max((_ts(p) - b.lo).total_seconds() / 86400, entry)
            rows.append({**vinted_row(rec["card"], now), "obs_type": "sold_interval", "t_days": round(entry, 4),
                         "t_upper_days": round(hi, 4), "t_entry_days": round(entry, 4),
                         "start_source": "id_calibration", "listed_at": b.mid.isoformat()})
            rec["resolved"] = "sold"
            n_sold += 1
        elif info["status"] == "active":
            age = max((_ts(p) - b.hi).total_seconds() / 86400, entry)
            rows.append({**vinted_row(rec["card"], now), "obs_type": "active", "t_days": round(age, 4),
                         "t_upper_days": None, "t_entry_days": round(entry, 4),
                         "start_source": "id_calibration", "listed_at": b.mid.isoformat()})
            rec["last_checked"] = _ts(p).isoformat()
            n_active_again += 1
        elif info["status"] == "deleted":
            rec["resolved"] = "deleted"
    rechecked = {r["listing_id"] for r in rows}

    # today's catalogue: survivors seen now (prices, but no speed information on their own)
    fresh = [r for r in vinted_active_rows(cards, cal, now) if r["listing_id"] not in rechecked]
    rows += fresh
    for c in cards:
        seen.setdefault(c["item_id"], {"first_seen": now.isoformat(), "card": c})
    stats = {"catalog": len(cards), "fresh_rows": len(fresh), "rechecked_sold": n_sold,
             "rechecked_active": n_active_again, "calibration_samples": len(cal.samples)}
    save_cache(query, "vinted", rows, {"stats": stats, "source": "extension", "seen": seen})
    return rows, stats


def ingest(pages: list[dict], queries: dict[str, str]) -> tuple[list[dict], dict]:
    by = {"ebay": [p for p in pages if p["platform"] == "ebay"],
          "vinted": [p for p in pages if p["platform"] == "vinted"]}
    rows, stats = [], {}
    if by["ebay"] and queries.get("ebay"):
        r, stats["ebay"] = ebay_rows(by["ebay"], queries["ebay"])
        rows += r
    if by["vinted"] and queries.get("vinted"):
        r, stats["vinted"] = vinted_rows(by["vinted"], queries["vinted"])
        rows += r
    return rows, stats
