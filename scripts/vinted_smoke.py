"""Live check of the Vinted pipeline.

First scrape (saves cards for a later re-check):
    python scripts/vinted_smoke.py "nike air force 1"
Re-check later (turns listings that have since sold into sold intervals):
    python scripts/vinted_smoke.py "nike air force 1" --recheck

Writes backend/scrapers/cache_seed/<slug>_vinted.json and the calibration to
backend/db/vinted_calibration.json.
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

from backend.scrapers.fetcher import Blocked, Fetcher  # noqa: E402
from backend.scrapers.vinted_calibration import VintedCalibration  # noqa: E402
from backend.scrapers.vinted_client import recheck, scan  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--pages", type=int, default=2)
    ap.add_argument("--recheck", action="store_true")
    args = ap.parse_args()

    slug = re.sub(r"[^a-z0-9]+", "-", args.query.lower()).strip("-")
    out = ROOT / "backend" / "scrapers" / "cache_seed" / f"{slug}_vinted.json"
    now = datetime.now(timezone.utc)
    cal, fetcher = VintedCalibration(), Fetcher(delay_s=1.0)
    print(f"fetcher: {fetcher.kind}; calibration samples so far: {len(cal.samples)}")
    try:
        if args.recheck:
            saved = json.loads(out.read_text())
            first_seen = datetime.fromisoformat(saved["fetched_at"])
            rows, counts = recheck(saved["cards"], first_seen, cal, fetcher, now=now)
            hours = (now - first_seen).total_seconds() / 3600
            print(f"re-check after {hours:.1f} h: {counts}")
            saved.update({"rechecked_at": now.isoformat(), "recheck_counts": counts, "rows": rows})
            out.write_text(json.dumps(saved, indent=1))
        else:
            rows, cards, stats = scan(args.query, cal, fetcher, pages=args.pages, now=now)
            print(f"scan '{args.query}': {stats}")
            ages = [r["t_days"] for r in rows]
            if ages:
                print(f"active listing ages (days): median {statistics.median(ages):.1f}, max {max(ages):.1f}")
            out.write_text(json.dumps({"query": args.query, "fetched_at": now.isoformat(), "stats": stats,
                                       "cards": cards, "rows": rows}, indent=1))
    except Blocked as e:
        print(f"\nRefused by Vinted: {e}\nThat's Vinted saying no for now; try again later or use cached data.")
        sys.exit(2)
    print(f"saved {len(rows)} rows -> {out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
