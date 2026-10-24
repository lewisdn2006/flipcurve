"""Build an item's eBay dataset from search pages YOU saved while browsing normally.

eBay serves people browsing normally but often refuses automated page loads, so for the demo
we collect eBay data the human way: open each page in Chrome, save it, then import.

1) Print the pages to open for an item (newest-first active + most-recently-sold, 240 per page):
       python scripts/import_ebay_pages.py urls "nike air force 1 07 white"
2) Open each link in Chrome and save it with Cmd+S -> Format "Webpage, HTML Only"
   into one folder per item, e.g. ~/Downloads/flipcurve_pages/nike-air-force-1-07-white/
3) Import:
       python scripts/import_ebay_pages.py import "nike air force 1 07 white" ~/Downloads/flipcurve_pages/nike-air-force-1-07-white

The time each file was saved is used as "now" for listing ages, so import pages saved within
a short spell of each other. Active pages also add calibration points for dating sold items.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.scrapers.ebay_calibration import CalibrationStore  # noqa: E402
from backend.scrapers.ebay_client import build_rows, search_url  # noqa: E402
from backend.scrapers.ebay_parse import parse_cards  # noqa: E402
from backend.scrapers.orchestrator import save_cache, slug  # noqa: E402


def cmd_urls(query: str, pages: int):
    print(f"Open these, then save each (Cmd+S, 'Webpage, HTML Only') into a folder named {slug(query)}/\n")
    for p in range(1, pages + 1):
        print(f"active p{p}: {search_url(query, p)}")
    for p in range(1, pages + 1):
        print(f"sold   p{p}: {search_url(query, p, sold=True)}")


def cmd_import(query: str, folder: Path):
    files = sorted(list(folder.glob("*.html")) + list(folder.glob("*.htm")))
    if not files:
        print(f"No .html files in {folder}")
        sys.exit(1)
    saved_at = min(datetime.fromtimestamp(f.stat().st_mtime, timezone.utc) for f in files)
    store = CalibrationStore()
    act, sold = [], []
    for f in files:
        cards = parse_cards(f.read_text(errors="ignore"), saved_at)
        n_sold = sum(1 for c in cards if c["sold_date"])
        if n_sold > len(cards) / 2:
            sold += cards
            kind = "sold"
        else:
            act += cards
            added = store.add([(c["item_id"], c["start_time"]) for c in cards if c["start_time"]], source=f"saved:{query}")
            kind = f"active (+{added} calibration points)"
        print(f"{f.name[:60]:60s} {len(cards):4d} listings  {kind}")
        if cards and kind != "sold" and not any(c["start_time"] for c in cards):
            print("   ! no start times on this page: was it sorted by 'Newly listed'? Use the link from `urls`.")
    # de-duplicate (the same listing can appear on two pages)
    act = list({c["item_id"]: c for c in act}.values())
    sold = list({c["item_id"]: c for c in sold}.values())
    rows, stats = build_rows(act, sold, store, saved_at)
    stats.update({"source": "saved_pages", "files": len(files), "saved_at": saved_at.isoformat()})
    save_cache(query, "ebay", rows, {"stats": stats})
    print(f"\n{stats}")
    print(f"saved {len(rows)} rows -> backend/scrapers/cache_seed/{slug(query)}_ebay.json")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("urls")
    u.add_argument("query")
    u.add_argument("--pages", type=int, default=2)
    i = sub.add_parser("import")
    i.add_argument("query")
    i.add_argument("folder", type=Path)
    a = ap.parse_args()
    cmd_urls(a.query, a.pages) if a.cmd == "urls" else cmd_import(a.query, a.folder.expanduser())
