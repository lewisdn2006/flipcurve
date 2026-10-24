"""Does fetching work from this machine right now?

    python scripts/fetch_diag.py            # real browser (Playwright) and plain requests

Prints one line per method and site: OK with how many listings parsed, or the refusal.
A refusal is the site saying no; we don't try to get around it. The app falls back to
cached data, so build the demo cache ahead of time (scripts/ebay_smoke.py, vinted_smoke.py).
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.scrapers.ebay_client import search_url  # noqa: E402
from backend.scrapers.ebay_parse import parse_cards  # noqa: E402
from backend.scrapers.fetcher import Blocked, Fetcher  # noqa: E402
from backend.scrapers.vinted_client import catalog_url  # noqa: E402
from backend.scrapers.vinted_parse import parse_catalog  # noqa: E402

SITES = {
    "ebay": (search_url("nike air force 1"), lambda h: len(parse_cards(h, datetime.now(timezone.utc)))),
    "vinted": (catalog_url("nike air force 1"), lambda h: len(parse_catalog(h))),
}

if __name__ == "__main__":
    for kind in ("browser", "httpx"):
        try:
            f = Fetcher(kind=kind)
        except Exception as e:  # noqa: BLE001
            print(f"{kind:8s} could not start: {type(e).__name__}: {e}")
            continue
        if f.kind != kind:
            print(f"{kind:8s} not available (is Playwright installed?)")
            continue
        for site, (url, count) in SITES.items():
            try:
                print(f"{kind:8s} {site:7s} OK, {count(f.get(url))} listings parsed")
            except Blocked as e:
                print(f"{kind:8s} {site:7s} refused ({e.args[0].split(' from ')[0]})")
        f.close()
