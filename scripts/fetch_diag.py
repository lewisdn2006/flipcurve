"""Which fetch method gets through to eBay and Vinted from this machine?

    python scripts/fetch_diag.py            # curl_cffi variants + httpx
    python scripts/fetch_diag.py --browser  # also try a real Chromium (needs Playwright)

Prints one line per method and site: OK with how many listings parsed, or the HTTP status.
Then set FLIPCURVE_FETCHER (and FLIPCURVE_IMPERSONATE for curl_cffi) in .env to the winner.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.scrapers.ebay_client import Blocked, Fetcher, search_url  # noqa: E402
from backend.scrapers.ebay_parse import parse_cards  # noqa: E402
from backend.scrapers.vinted_client import catalog_url  # noqa: E402
from backend.scrapers.vinted_parse import parse_catalog  # noqa: E402

SITES = {
    "ebay": (search_url("nike air force 1"), lambda h: len(parse_cards(h, datetime.now(timezone.utc)))),
    "vinted": (catalog_url("nike air force 1"), lambda h: len(parse_catalog(h))),
}


def attempt(label, **kw):
    try:
        f = Fetcher(delay_s=0.5, **kw)
    except Exception as e:  # noqa: BLE001
        print(f"{label:28s} could not start: {type(e).__name__}: {e}")
        return
    for site, (url, count) in SITES.items():
        try:
            html = f.get(url)
            print(f"{label:28s} {site:7s} OK, {count(html)} listings parsed")
        except Blocked as e:
            print(f"{label:28s} {site:7s} blocked ({e.args[0].split(' from ')[0]})")
        except Exception as e:  # noqa: BLE001
            print(f"{label:28s} {site:7s} error: {type(e).__name__}: {str(e)[:80]}")
    if hasattr(f, "close"):
        f.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--browser", action="store_true")
    args = ap.parse_args()
    for imp in ("chrome", "safari", "chrome131", "firefox"):
        attempt(f"curl_cffi impersonate={imp}", kind="curl_cffi", impersonate=imp)
    attempt("httpx", kind="httpx")
    if args.browser:
        attempt("browser (Playwright)", kind="browser")
