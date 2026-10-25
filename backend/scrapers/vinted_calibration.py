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
    mid: datetime  # best guess: interpolated by ID between neighbouring samples, inside [lo, hi]


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

    def consistent_ids(self) -> list[int]:
        """Sample IDs that agree with a counter that only goes up.

        A bumped listing shows its bump time as "Uploaded", so an old ID can claim a very
        recent upload (seen 24 Oct: ID 10482200093 "27 min ago" while newer IDs said days).
        Two samples conflict when the older ID's earliest start is after the newer ID's
        latest start. Greedily drop the sample in the most conflicts until none remain."""
        ids = sorted(self.samples)
        alive = set(ids)
        while True:
            counts = {i: 0 for i in alive}
            srt = sorted(alive)
            for a_pos, a in enumerate(srt):
                for b in srt[a_pos + 1:]:
                    if self.samples[a][0] > self.samples[b][1]:
                        counts[a] += 1
                        counts[b] += 1
            worst = max(counts, key=counts.get, default=None)
            if worst is None or counts[worst] == 0:
                return sorted(alive)
            alive.discard(worst)

    def _envelope(self):
        """Monotone counter => start(n) >= every older sample's earliest bound (running max)
        and <= every newer sample's latest bound (running min from the other end)."""
        ids = self.consistent_ids()
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
        # point estimate: the counter runs at a fairly steady rate over a day or two, so interpolate
        # between the neighbouring samples' centres (25 Oct: bounds alone were ~36 h wide and
        # put most of a catalogue page at the same age)
        if ids[j] == n:
            est = sum(self.samples[n]) / 2
        else:
            a, b = ids[j - 1], ids[j]
            ca, cb = sum(self.samples[a]) / 2, sum(self.samples[b]) / 2
            est = ca + (n - a) / (b - a) * (cb - ca)
        est = min(max(est, lo), hi)
        to_dt = lambda x: datetime.fromtimestamp(x, timezone.utc)  # noqa: E731
        return StartBounds(to_dt(lo), to_dt(hi), to_dt(est))
