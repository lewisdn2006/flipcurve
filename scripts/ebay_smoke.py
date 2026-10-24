"""Live check of the eBay pipeline: calibrate, scan one item, report accuracy and coverage.

Run from repo root on a machine that can reach eBay:
    python scripts/ebay_smoke.py "nike air force 1 07 white" --queries 8
Gentle retry (2 page loads, reuses the saved calibration):
    python scripts/ebay_smoke.py "nike air force 1 07 white" --skip-calibration --pages 1

It writes the calibration to backend/db/ebay_calibration.sqlite (reused next time, so later
runs are faster) and the item's rows to backend/scrapers/cache_seed/<slug>_ebay.json.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.scrapers.ebay_calibration import CALIBRATION_QUERIES, CalibrationStore  # noqa: E402
from backend.scrapers.ebay_client import calibrate, scan  # noqa: E402
from backend.scrapers.fetcher import Blocked, Fetcher  # noqa: E402


def q(xs, p):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(p * len(xs)))], 2) if xs else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--queries", type=int, default=8, help="how many calibration queries to run")
    ap.add_argument("--skip-calibration", action="store_true")
    ap.add_argument("--pages", type=int, default=3, help="max pages each for active and sold (fewer = gentler)")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    store, fetcher = CalibrationStore(), Fetcher()
    print(f"fetcher: {fetcher.kind}; calibration points so far: {store.count()}")
    try:
        if not args.skip_calibration:
            added = calibrate(store, fetcher, CALIBRATION_QUERIES[: args.queries], now=now)
            print(f"calibration: +{added} points, {store.count()} total in {len(store.blocks())} blocks")
        errs = store.leave_one_out_error_hours()
        print(f"calibration accuracy (leave-one-out): median {q(errs, .5)} h, p90 {q(errs, .9)} h, "
              f"within 6 h {round(sum(e <= 6 for e in errs) / len(errs), 3) if errs else None}")
        rows, stats = scan(args.query, store, fetcher, active_pages=args.pages, sold_pages=args.pages, now=now)
    except Blocked as e:
        print(f"\nRefused by eBay: {e}\nThat's eBay saying no for now; try again later or use cached data.")
        sys.exit(2)

    print(f"scan '{args.query}': {stats}")
    sold = [r for r in rows if r["obs_type"] == "sold_interval"]
    mids = [(r["t_days"] + r["t_upper_days"]) / 2 for r in sold]
    widths = [r["t_upper_days"] - r["t_days"] for r in sold]
    print(f"sold time-to-sale (days): p10 {q(mids, .1)}, median {q(mids, .5)}, p90 {q(mids, .9)}; "
          f"interval width median {q(widths, .5)} d")
    act = [r["price_gbp"] for r in rows if r["obs_type"] == "active"]
    sp = [r["price_gbp"] for r in rows if r["obs_type"] != "active"]
    if act and sp:
        a, s = statistics.median(act), statistics.median(sp)
        print(f"active median £{a:.2f} vs sold median £{s:.2f} -> survivorship bias {100 * (a - s) / s:.1f}%")

    slug = re.sub(r"[^a-z0-9]+", "-", args.query.lower()).strip("-")
    out = ROOT / "backend" / "scrapers" / "cache_seed" / f"{slug}_ebay.json"
    out.write_text(json.dumps({"query": args.query, "fetched_at": now.isoformat(), "stats": stats, "rows": rows}, indent=1))
    print(f"saved {len(rows)} rows -> {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
