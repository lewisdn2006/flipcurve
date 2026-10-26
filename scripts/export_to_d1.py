"""Copy the laptop's saved data into the Worker's D1 database (NO_LAPTOP_PLAN.md, step 2).

    .venv/bin/python scripts/export_to_d1.py          # writes web/api-worker/seed/data.sql
    cd web/api-worker && npx wrangler d1 execute flipcurve-db --remote --file=seed/data.sql

The SQL empties each table first, so running it again replaces the copy instead of adding to
it. Tables are in web/api-worker/migrations/. seed/ is gitignored: it's a copy
of backend/db/ and cache_seed/, which aren't in git either.

Left out: the old "cards" list in one Vinted cache file (nothing reads it back) and
backend/db/browser_profile/ (server-side fetching, which the Worker doesn't do).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.scrapers.orchestrator import CACHE, slug  # noqa: E402

DB = ROOT / "backend" / "db"
OUT = ROOT / "web" / "api-worker" / "seed" / "data.sql"
MAX_STATEMENT = 90_000  # D1 refuses statements over 100 KB
TABLES = ["ebay_calibration_points", "vinted_calibration", "scan_cache", "scan_cache_rows", "vinted_seen",
          "model_cooldowns"]


def sql(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def inserts(table: str, columns: list[str], rows: list[tuple]) -> list[str]:
    """Multi-row INSERTs, each under D1's statement size limit."""
    head = f"INSERT INTO {table} ({', '.join(columns)}) VALUES "
    out, batch, size = [], [], len(head)
    for r in rows:
        values = "(" + ", ".join(sql(v) for v in r) + ")"
        if batch and size + len(values) + 2 > MAX_STATEMENT:
            out.append(head + ",\n".join(batch) + ";")
            batch, size = [], len(head)
        batch.append(values)
        size += len(values) + 2
    if batch:
        out.append(head + ",\n".join(batch) + ";")
    return out


def main() -> None:
    data: dict[str, list[tuple]] = {t: [] for t in TABLES}

    con = sqlite3.connect(DB / "ebay_calibration.sqlite")
    data["ebay_calibration_points"] = con.execute(
        "SELECT item_id, block, start_ts, source FROM points ORDER BY item_id").fetchall()

    vcal = DB / "vinted_calibration.json"
    if vcal.exists():
        data["vinted_calibration"] = [(int(k), lo, hi) for k, (lo, hi) in json.loads(vcal.read_text()).items()]

    for f in sorted(CACHE.glob("*_*.json")):
        d = json.loads(f.read_text())
        platform = f.stem.rsplit("_", 1)[1]
        s = slug(d["query"])
        if f"{s}_{platform}" != f.stem:
            print(f"  note: {f.name} holds query {d['query']!r}, stored under {s}")
        extra = {k: v for k, v in d.items() if k not in ("query", "fetched_at", "rows", "seen", "cards")}
        data["scan_cache"].append((s, platform, d["query"], d["fetched_at"], json.dumps(extra)))
        data["scan_cache_rows"] += [(s, platform, n, json.dumps(r)) for n, r in enumerate(d.get("rows", []))]
        data["vinted_seen"] += [(s, iid, rec["first_seen"], rec.get("last_checked"), rec.get("resolved"),
                                 bool(rec.get("relevant")), json.dumps(rec), n)
                                for n, (iid, rec) in enumerate(d.get("seen", {}).items())]

    cool = DB / "model_cooldowns.json"
    if cool.exists():
        data["model_cooldowns"] = [(m, c["until"], c.get("why"), c.get("since"))
                                   for m, c in json.loads(cool.read_text()).items()]

    columns = {
        "ebay_calibration_points": ["item_id", "block", "start_ts", "source"],
        "vinted_calibration": ["item_id", "lo_ts", "hi_ts"],
        "scan_cache": ["slug", "platform", "query", "fetched_at", "extra_json"],
        "scan_cache_rows": ["slug", "platform", "n", "row_json"],
        "vinted_seen": ["slug", "item_id", "first_seen", "last_checked", "resolved", "relevant", "rec_json", "n"],
        "model_cooldowns": ["model", "until", "why", "since"],
    }
    lines = [f"DELETE FROM {t};" for t in TABLES]
    for t in TABLES:
        lines += inserts(t, columns[t], data[t])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n")
    print(f"wrote {OUT.relative_to(ROOT)} ({OUT.stat().st_size / 1e6:.1f} MB, {len(lines)} statements)")
    for t in TABLES:
        print(f"  {t}: {len(data[t])}")


if __name__ == "__main__":
    main()
