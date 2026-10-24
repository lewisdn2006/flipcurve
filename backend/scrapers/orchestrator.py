"""Get listings for one item: live (both platforms in parallel, with a time budget) -> cached
seed files -> nothing. The API then runs the engine on whatever came back.

Cache files live in backend/scrapers/cache_seed/<slug>_<platform>.json, written by the live
path and by scripts/ebay_smoke.py / vinted_smoke.py. FLIPCURVE_OFFLINE=1 skips live fetching
(use it for the demo if the venue Wi-Fi is bad).

Owner: Person 2.
"""
from __future__ import annotations

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
from datetime import datetime, timezone
from pathlib import Path

CACHE = Path(__file__).resolve().parent / "cache_seed"
LIVE_BUDGET_S = float(os.getenv("FLIPCURVE_LIVE_BUDGET_S", "25"))


def slug(q: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", q.lower()).strip("-")


def cache_path(query: str, platform: str) -> Path:
    return CACHE / f"{slug(query)}_{platform}.json"


def load_cache(query: str, platform: str) -> list[dict] | None:
    p = cache_path(query, platform)
    if not p.exists():
        return None
    data = json.loads(p.read_text())
    return data.get("rows") or None


def save_cache(query: str, platform: str, rows: list[dict], extra: dict | None = None):
    CACHE.mkdir(parents=True, exist_ok=True)
    payload = {"query": query, "fetched_at": datetime.now(timezone.utc).isoformat(), "rows": rows, **(extra or {})}
    cache_path(query, platform).write_text(json.dumps(payload, indent=1))


def _live_ebay(query: str) -> list[dict]:
    from backend.scrapers.ebay_calibration import CalibrationStore
    from backend.scrapers.ebay_client import Fetcher, scan
    rows, stats = scan(query, CalibrationStore(), Fetcher())
    save_cache(query, "ebay", rows, {"stats": stats})
    return rows


def _live_vinted(query: str) -> list[dict]:
    from backend.scrapers.ebay_client import Fetcher
    from backend.scrapers.vinted_calibration import VintedCalibration
    from backend.scrapers.vinted_client import scan
    old = cache_path(query, "vinted")
    rows, cards, stats = scan(query, VintedCalibration(), Fetcher(delay_s=1.0))
    # keep an earlier re-check's sold rows if we have them: a fresh scrape alone has no speed data
    if old.exists():
        prev = json.loads(old.read_text())
        if prev.get("rechecked_at"):
            return prev["rows"]
    save_cache(query, "vinted", rows, {"stats": stats, "cards": cards})
    return rows


LIVE = {"ebay": _live_ebay, "vinted": _live_vinted}


def gather(queries: dict[str, str], platforms: list[str], allow_live: bool = True) -> tuple[list[dict], str, list[str]]:
    """Returns (rows, data_source, warnings). data_source: 'live', 'seed' (cache) or 'mock' (nothing)."""
    live_ok = allow_live and os.getenv("FLIPCURVE_OFFLINE", "0") != "1"
    rows, warnings, used_live, used_cache = [], [], False, False
    results: dict[str, list[dict] | None] = {}
    if live_ok:
        with ThreadPoolExecutor(max_workers=len(platforms)) as ex:
            futs = {p: ex.submit(LIVE[p], queries[p]) for p in platforms if p in LIVE}
            for p, fut in futs.items():
                try:
                    results[p] = fut.result(timeout=LIVE_BUDGET_S)
                    used_live = True
                except FutTimeout:
                    warnings.append(f"{p}: live search took too long, used saved data")
                except Exception as e:  # noqa: BLE001 - blocked, network, parse: all fall back to cache
                    warnings.append(f"{p}: live search failed ({type(e).__name__}), used saved data")
    for p in platforms:
        got = results.get(p)
        if not got:
            got = load_cache(queries[p], p)
            if got:
                used_cache = True
            else:
                warnings.append(f"{p}: no listings available")
        rows += got or []
    source = "live" if used_live and not used_cache else ("seed" if rows else "mock")
    return rows, source, warnings
