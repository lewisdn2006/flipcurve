"""eBay item-number -> listing start time calibration (DECISIONS.md D1).

eBay item numbers come in ~100 parallel blocks, identified by their first three digits.
Within a block, numbers rise with time. Newest-first searches show each active listing's
exact start time, so every one we see is a calibration point (block, item number, time).
For any other item number - in particular a SOLD listing, which shows no start time - we
bracket it between its two neighbours in the same block and return that time interval.

Tested on 24 Oct 2026: neighbour interpolation, median error ~4 min, 88-96% within 6 h.

Calibration points come from any category: a block maps to dates the same way everywhere,
so slow categories (typewriters, Meccano...) let us reach months back cheaply.

Owner: Person 2.
"""
from __future__ import annotations

import bisect
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parents[1] / "db" / "ebay_calibration.sqlite"

# Slow-moving categories reach far back in time within a few pages; busy ones fill in the
# recent days densely. Mix both. Each query is searched newest-first.
CALIBRATION_QUERIES = [
    "typewriter", "meccano set", "vintage slide rule", "brass door knocker", "fountain pen",
    "vinyl record jazz", "film camera", "pocket watch", "board game", "lego set",
    "football shirt", "nike trainers", "iphone", "playstation game", "kitchen aid mixer",
    "dining chair", "road bike", "cast iron pan", "wool jumper", "headphones",
]

# Slack added to each end of a bracket: ~2-4% of points are out of order (relists etc.).
BRACKET_SLACK_DAYS = 0.1
BLOCK_DIGITS = 3


@dataclass(frozen=True)
class StartEstimate:
    lo: datetime          # earliest plausible start
    hi: datetime          # latest plausible start
    mid: datetime         # linear interpolation between the neighbours
    gap_days: float       # width of the neighbour bracket (calibration density here)


def block_of(item_id: str | int) -> str:
    return str(item_id)[:BLOCK_DIGITS]


class CalibrationStore:
    def __init__(self, path: Path | str = DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS points (
            item_id INTEGER PRIMARY KEY, block TEXT NOT NULL, start_ts REAL NOT NULL, source TEXT)""")
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_block ON points(block, item_id)")
        self._cache: dict[str, tuple[list[int], list[float]]] = {}

    def add(self, points: list[tuple[str, datetime]], source: str = "search") -> int:
        """points: (item_id, start_time). Returns how many rows were new."""
        before = self.count()
        self.db.executemany(
            "INSERT OR IGNORE INTO points(item_id, block, start_ts, source) VALUES (?,?,?,?)",
            [(int(i), block_of(i), t.timestamp(), source) for i, t in points],
        )
        self.db.commit()
        self._cache.clear()
        return self.count() - before

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM points").fetchone()[0]

    def blocks(self) -> dict[str, int]:
        return dict(self.db.execute("SELECT block, COUNT(*) FROM points GROUP BY block").fetchall())

    def _block(self, block: str) -> tuple[list[int], list[float]]:
        if block not in self._cache:
            rows = self.db.execute(
                "SELECT item_id, start_ts FROM points WHERE block=? ORDER BY item_id", (block,)).fetchall()
            self._cache[block] = ([r[0] for r in rows], [r[1] for r in rows])
        return self._cache[block]

    def estimate_start(self, item_id: str | int, min_points: int = 5) -> StartEstimate | None:
        """Bracket an item's start time between its neighbours. None if outside the calibration."""
        ids, ts = self._block(block_of(item_id))
        n = int(item_id)
        if len(ids) < min_points or n < ids[0] or n > ids[-1]:
            return None
        j = bisect.bisect_left(ids, n)
        if ids[j] == n:  # we saw this listing ourselves
            t = datetime.fromtimestamp(ts[j], timezone.utc)
            return StartEstimate(t, t, t, 0.0)
        a_id, b_id, a_t, b_t = ids[j - 1], ids[j], ts[j - 1], ts[j]
        mid = a_t + (b_t - a_t) * (n - a_id) / (b_id - a_id)
        slack = BRACKET_SLACK_DAYS * 86400
        lo, hi = min(a_t, b_t) - slack, max(a_t, b_t) + slack
        to_dt = lambda x: datetime.fromtimestamp(x, timezone.utc)  # noqa: E731
        return StartEstimate(to_dt(lo), to_dt(hi), to_dt(mid), abs(b_t - a_t) / 86400)

    def leave_one_out_error_hours(self) -> list[float]:
        """Accuracy check: predict each point from its two neighbours; returns absolute errors."""
        errs = []
        for block in self.blocks():
            ids, ts = self._block(block)
            for j in range(1, len(ids) - 1):
                pred = ts[j - 1] + (ts[j + 1] - ts[j - 1]) * (ids[j] - ids[j - 1]) / (ids[j + 1] - ids[j - 1])
                errs.append(abs(pred - ts[j]) / 3600)
        return errs
