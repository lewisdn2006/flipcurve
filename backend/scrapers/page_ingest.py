"""Turn pages collected by the FlipCurve helper extension into ListingRows.

The extension loads pages in the user's own signed-in browser and sends them here (see
extension/README.md). Each page is {platform, kind, page, url, final_url, status, html, fetched_at}:
  eBay    kind 'active' (newest-listed, has start times) / 'sold' (recently sold)
  Vinted  kind 'catalog' (newest listings) / 'item' (one listing, for its upload age)
          / 'recheck' (a listing we saw in an earlier scan: sold since, or still active?)
The extension also re-checks listings in the background (due_rechecks / apply_background_rechecks).

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


# ------------------------------------------------------------------ Vinted re-check history
# Every Vinted listing a scan sees is remembered in that query's cache file under "seen":
#   {first_seen, card, relevant?, last_checked?, last_active?, resolved?: sold|deleted, sold_seen_at?}
# Re-checks (during a scan, or by the extension in the background every couple of hours) open a
# listing again: still for sale -> last_active; "Sold" -> a real sale between last_active and
# now. Every scan turns that history into rows, so sales found in the background count at once.

RECHECK_MIN_AGE = timedelta(hours=3)    # first re-check once a listing has been seen this long
RECHECK_EVERY = timedelta(hours=6)      # then at most this often
TRACK_FOR = timedelta(days=30)          # stop following a listing after this
SOLD_ROWS_FOR = timedelta(days=90)      # sales found within this window feed the model
RECHECK_MAX = 6                         # re-checks folded into one scan


def _dt(s: str) -> datetime:
    t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _iid(url: str) -> str:
    return url.rstrip("/").split("/")[-1].split("-")[0].split("?")[0]


def _load_vinted_cache(query: str) -> dict:
    p = cache_path(query, "vinted")
    return json.loads(p.read_text()) if p.exists() else {"rows": [], "seen": {}}


def _load_vinted_history(query: str) -> dict:
    return {"seen": _load_vinted_cache(query).get("seen", {})}


def _due(seen: dict, now: datetime, need_relevant: bool) -> list[tuple]:
    out = []
    for iid, rec in seen.items():
        if rec.get("resolved") or (need_relevant and not rec.get("relevant")):
            continue
        first = _dt(rec["first_seen"])
        if now - first < RECHECK_MIN_AGE or now - first > TRACK_FOR:
            continue
        last = rec.get("last_checked")
        if last and now - _dt(last) < RECHECK_EVERY:
            continue
        out.append((last or "", rec["first_seen"], iid))
    return sorted(out)  # never checked first, then the longest since a check


def recheck_ids(query: str, now: datetime | None = None) -> list[str]:
    """Vinted listings from earlier scans of this query worth re-checking during this scan."""
    now = now or datetime.now(timezone.utc)
    seen = _load_vinted_history(query)["seen"]
    due = _due(seen, now, need_relevant=True) or _due(seen, now, need_relevant=False)
    return [iid for *_, iid in due[:RECHECK_MAX]]


def due_rechecks(limit: int = 20, now: datetime | None = None) -> list[dict]:
    """Across every scanned item: listings the extension should re-check in the background.
    Only listings that matched the item in their scan ("relevant"), so the budget isn't spent on
    the Air Force 1s a Jordan search turns up."""
    now = now or datetime.now(timezone.utc)
    due = []
    for path in sorted(CACHE_DIR().glob("*_vinted.json")):
        data = json.loads(path.read_text())
        q = data.get("query")
        due += [(last, first, q, iid) for last, first, iid in _due(data.get("seen", {}), now, need_relevant=True)]
    due.sort()
    return [{"id": iid, "query": q} for _, _, q, iid in due[:limit]]


def apply_recheck(rec: dict, page: dict) -> str:
    """Update one listing's history from a re-check page; returns active / sold / deleted / unknown."""
    info = parse_item_page(page["html"], page.get("status", 200))
    when = _ts(page).isoformat()
    rec["last_checked"] = when
    if info["status"] == "sold":
        rec["resolved"], rec["sold_seen_at"] = "sold", when
    elif info["status"] == "active":
        rec["last_active"] = when
    elif info["status"] == "deleted":
        rec["resolved"] = "deleted"
    return info["status"]


def history_rows(seen: dict, cal: VintedCalibration, now: datetime, ids=None, skip_active=()) -> list[dict]:
    """Rows from re-check history: a sale between the last time we saw it for sale and the check
    that found it sold; or survival up to the last time it was still for sale. Ages use the
    listing's estimated start (ID calibration); entry age = when we first saw it."""
    rows = []
    for iid, rec in seen.items():
        if ids is not None and iid not in ids:
            continue
        b = cal.bounds(iid)
        if b is None or "card" not in rec:
            continue
        age = lambda t: max((_dt(t) - b.mid).total_seconds() / 86400, 0.0)  # noqa: E731
        entry = age(rec["first_seen"])
        a = max(age(rec.get("last_active") or rec["first_seen"]), entry)
        base = {**vinted_row(rec["card"], now), "start_source": "id_calibration", "listed_at": b.mid.isoformat()}
        if rec.get("resolved") == "sold" and now - _dt(rec["sold_seen_at"]) <= SOLD_ROWS_FOR:
            hi = max(age(rec["sold_seen_at"]), a + 1e-3)
            rows.append({**base, "obs_type": "sold_interval", "t_days": round(a, 4), "t_upper_days": round(hi, 4),
                         "t_entry_days": round(entry, 4), "sold_at": rec["sold_seen_at"]})
        elif not rec.get("resolved") and rec.get("last_active") and iid not in skip_active and a > entry:
            rows.append({**base, "obs_type": "active", "t_days": round(a, 4), "t_upper_days": None,
                         "t_entry_days": round(entry, 4)})
    return rows


def vinted_rows(pages: list[dict], query: str, cal: VintedCalibration | None = None) -> tuple[list[dict], dict]:
    cal = cal or VintedCalibration()
    now = max((_ts(p) for p in pages), default=datetime.now(timezone.utc))
    data = _load_vinted_cache(query)
    seen: dict = data.get("seen", {})

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
                cal.add_age(_iid(p["url"]), info["age_hours"], _ts(p))
    cal.save()

    outcome = {"active": 0, "sold": 0, "deleted": 0, "unknown": 0}
    for p in pages:
        if p["kind"] == "recheck" and _iid(p["url"]) in seen:
            outcome[apply_recheck(seen[_iid(p["url"])], p)] += 1

    today = {c["item_id"] for c in cards}
    sold_ids = {k for k, v in seen.items() if v.get("resolved") == "sold"}
    hist = history_rows(seen, cal, now, skip_active=today)
    # today's catalogue: what's still listed (stock sampling gives speed; see vinted_client)
    fresh = [r for r in vinted_active_rows(cards, cal, now) if r["listing_id"] not in sold_ids]
    rows = hist + fresh
    for c in cards:
        seen.setdefault(c["item_id"], {"first_seen": now.isoformat(), "card": c})
    stats = {"catalog": len(cards), "fresh_rows": len(fresh), "rechecked_sold": outcome["sold"],
             "rechecked_active": outcome["active"], "history_sales": sum(r["obs_type"] == "sold_interval" for r in hist),
             "calibration_samples": len(cal.samples)}
    save_cache(query, "vinted", rows, {"stats": stats, "source": "extension", "seen": seen})
    return rows, stats


def mark_relevant(query: str, ids: list[str]):
    """After a scan: remember which Vinted listings matched the item, so background re-checks
    only follow those."""
    p = cache_path(query, "vinted")
    if not p.exists() or not ids:
        return
    data = json.loads(p.read_text())
    for iid in ids:
        if iid in data.get("seen", {}):
            data["seen"][iid]["relevant"] = True
    p.write_text(json.dumps(data, indent=1))


def apply_background_rechecks(results: list[dict], cal: VintedCalibration | None = None) -> dict:
    """results: [{query, page}] from the extension's background run. Updates each query's history
    and its saved rows, so "Use saved data" and the next scan both see the sales."""
    cal = cal or VintedCalibration()
    by_q: dict[str, list[dict]] = {}
    for r in results:
        by_q.setdefault(r["query"], []).append(r["page"])
    out = {"active": 0, "sold": 0, "deleted": 0, "unknown": 0, "skipped": 0}
    for q, pages in by_q.items():
        p = cache_path(q, "vinted")
        if not p.exists():
            out["skipped"] += len(pages)
            continue
        data = json.loads(p.read_text())
        seen = data.get("seen", {})
        touched = set()
        for page in pages:
            iid = _iid(page["url"])
            if iid not in seen:
                out["skipped"] += 1
                continue
            out[apply_recheck(seen[iid], page)] += 1
            touched.add(iid)
        now = max(_ts(pg) for pg in pages)
        keep = [r for r in data.get("rows", []) if r["listing_id"] not in touched]
        data["rows"] = keep + history_rows(seen, cal, now, ids=touched)
        data["seen"] = seen
        p.write_text(json.dumps(data, indent=1))
    return out


def CACHE_DIR():
    from backend.scrapers import orchestrator
    return orchestrator.CACHE


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
