"""Vinted item ID -> listing time calibration (DECISIONS.md D1).

Vinted item IDs are one global counter, but its rate varies (slower at night and weekends),
so a single linear rate is off by days. Instead we keep samples (id, earliest start, latest
start) from item pages' "Uploaded ... ago" text and from the newest listings, and bracket any
other ID between its neighbours: the counter only goes up, so an ID's start lies between the
bounds of the samples either side of it.

Owner: Person 2.
"""
from __future__ import annotations

import bisect
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_PATH = Path(__file__).resolve().parents[1] / "db" / "vinted_calibration.json"


@dataclass(frozen=True)
class StartBounds:
    lo: datetime   # earliest plausible listing time
    hi: datetime   # latest plausible listing time
    mid: datetime


class VintedCalibration:
    def __init__(self, path: Path | str = DEFAULT_PATH):
        self.path = Path(path)
        # item_id -> (earliest start ts, latest start ts)
        self.samples: dict[int, tuple[float, float]] = {}
        if self.path.exists():
            self.samples = {int(k): tuple(v) for k, v in json.loads(self.path.read_text()).items()}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({str(k): v for k, v in sorted(self.samples.items())}))

    def add_age(self, item_id: int | str, age_hours: tuple[float, float], observed_at: datetime):
        """An item page said it was uploaded between age_hours[0] and age_hours[1] before observed_at."""
        lo = observed_at - timedelta(hours=age_hours[1])
        hi = observed_at - timedelta(hours=age_hours[0])
        self.samples[int(item_id)] = (lo.timestamp(), hi.timestamp())

    def add_exact(self, item_id: int | str, when: datetime, slack_minutes: float = 10):
        """E.g. the top of a newest-first page, seen within minutes of being listed."""
        s = slack_minutes * 60
        self.samples[int(item_id)] = (when.timestamp() - s, when.timestamp())

    def _envelope(self):
        """Monotone counter => start(n) >= every older sample's earliest bound (running max)
        and <= every newer sample's latest bound (running min from the other end)."""
        ids = sorted(self.samples)
        lo_env, run = [], float("-inf")
        for i in ids:
            run = max(run, self.samples[i][0])
            lo_env.append(run)
        hi_env, run = [0.0] * len(ids), float("inf")
        for k in range(len(ids) - 1, -1, -1):
            run = min(run, self.samples[ids[k]][1])
            hi_env[k] = run
        return ids, lo_env, hi_env

    def bounds(self, item_id: int | str) -> StartBounds | None:
        ids, lo_env, hi_env = self._envelope()
        n = int(item_id)
        if len(ids) < 2 or n < ids[0] or n > ids[-1]:
            return None
        j = bisect.bisect_left(ids, n)
        if ids[j] == n:
            lo, hi = lo_env[j], hi_env[j]
        else:
            lo, hi = lo_env[j - 1], hi_env[j]  # older side bounds from below, newer side from above
        if hi < lo:  # samples disagree (display rounding); widen to cover both
            lo, hi = hi, lo
        to_dt = lambda x: datetime.fromtimestamp(x, timezone.utc)  # noqa: E731
        return StartBounds(to_dt(lo), to_dt(hi), to_dt((lo + hi) / 2))
