"""Synthetic marketplace listings drawn from known model parameters (step 8).

Used for: the recovery test (fit simulated data, check the true parameters come back),
the mock payload's evidence scatter, and demo-day fallback data.

It mimics how the real clients observe listings (DECISIONS.md D1, maths doc section 5):
  eBay    cohort window: every listing that STARTED in the last `ebay_window_days` is seen,
          sold or not (active search gives exact starts; sold search + item-number
          calibration dates the sold ones). Active -> 'active'; sold -> 'sold_interval'
          (sold date is day-only, plus calibration error). No truncation (entry age 0).
  Vinted  left-truncated: a first scrape sees only listings still active then, at age a0.
          A re-check `vinted_recheck_hours` later finds each one sold ('sold_interval'
          (a0, a1]) or still active ('active' at a1). Every row has t_entry_days = a0, so
          the fit conditions on having survived to a0. With no re-check, rows are active
          with t_entry = t and carry no speed information - which is the honest answer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import numpy as np

from backend.engine.platform_config import PLATFORMS
from backend.engine.survival_model import C_REF

TIERS = ["fair", "good", "very_good", "mint"]
CIDX = {t: i + 1 for i, t in enumerate(TIERS)}
SHIPPING = {"ebay": 3.50, "vinted": 3.20}


@dataclass
class TrueParams:
    lambda0: float = 4.5
    beta1: float = 2.4
    beta2: float = 0.35
    k: float = 0.85
    sigma_p: float = 12.0
    anchor: dict = field(default_factory=lambda: {"fair": 22.0, "good": 34.0, "very_good": 45.0, "mint": 65.0})
    theta: dict = field(default_factory=lambda: {"ebay": 0.08, "vinted": -0.10})

    def scale(self, price, tier, platform):
        z = (np.asarray(price, float) - self.anchor[tier]) / self.sigma_p
        return self.lambda0 * np.exp(self.beta1 * z - self.beta2 * (CIDX[tier] - C_REF) + self.theta[platform])


def simulate_listings(
    params: TrueParams,
    n_per_platform: int = 400,
    now: datetime | None = None,
    ebay_window_days: float = 30.0,
    vinted_window_days: float = 30.0,
    vinted_recheck_hours: float | None = 18.0,
    calibration_error_days: float = 0.25,
    tier_weights=(0.15, 0.45, 0.28, 0.12),
    seed: int = 7,
) -> list[dict]:
    """Return ListingRow-shaped dicts for every listing the clients would actually see.
    n_per_platform = listings that STARTED in each platform's window."""
    rng = np.random.default_rng(seed)
    now = now or datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
    rows: list[dict] = []
    for platform in PLATFORMS:
        cfg = PLATFORMS[platform]
        window = ebay_window_days if platform == "ebay" else vinted_window_days
        recheck = (vinted_recheck_hours or 0.0) / 24
        tiers = rng.choice(TIERS, size=n_per_platform, p=tier_weights)
        for i, tier in enumerate(tiers):
            anchor = params.anchor[tier]
            # sellers mostly price a little above the going rate, with a long right tail
            price = max(float(np.round(anchor * np.exp(rng.normal(0.05, 0.22)), 0) - 0.01), 1.99)
            lam = float(params.scale(price, tier, platform))
            T = lam * (-np.log(rng.uniform())) ** (1.0 / params.k)
            age_now = rng.uniform(0, window) + (recheck if platform == "vinted" else 0.0)
            listed_at = now - timedelta(days=age_now)
            row = _row(platform, f"{platform[:2]}{i:05d}", tier, price, cfg, now, listed_at, rng)

            if platform == "ebay":
                if T >= age_now:
                    row.update(obs_type="active", t_days=round(age_now, 4), start_source="exact")
                else:
                    sold_at = listed_at + timedelta(days=T)
                    day_start = sold_at.replace(hour=0, minute=0, second=0, microsecond=0)
                    lo = (day_start - listed_at).total_seconds() / 86400 - calibration_error_days
                    hi = lo + 1.0 + 2 * calibration_error_days
                    row.update(obs_type="sold_interval", t_days=round(max(lo, 0.0), 4),
                               t_upper_days=round(max(hi, 0.0), 4), start_source="id_calibration",
                               sold_at=day_start.isoformat(), best_offer_accepted=bool(rng.uniform() < 0.2))
            else:  # vinted
                a0 = age_now - recheck          # age at the first scrape
                if T <= a0:
                    continue                    # sold before we ever saw it: invisible
                if vinted_recheck_hours is None:
                    row.update(obs_type="active", t_days=round(a0, 4), t_entry_days=round(a0, 4),
                               start_source="id_calibration")
                elif T <= age_now:
                    row.update(obs_type="sold_interval", t_days=round(a0, 4), t_upper_days=round(age_now, 4),
                               t_entry_days=round(a0, 4), start_source="id_calibration")
                else:
                    row.update(obs_type="active", t_days=round(age_now, 4), t_entry_days=round(a0, 4),
                               start_source="id_calibration")
            rows.append(row)
    return rows


def _row(platform, lid, tier, price, cfg, now, listed_at, rng):
    ship = SHIPPING[platform]
    fee = round(price * cfg["buyer_fee_pct"] + cfg["buyer_fee_fixed_gbp"], 2)
    return {
        "listing_id": lid, "platform": platform, "title": f"Simulated {tier.replace('_', ' ')} listing",
        "url": None, "price_gbp": price, "shipping_gbp": ship, "buyer_fee_gbp": fee,
        "total_buyer_price_gbp": round(price + ship + fee, 2), "condition_tier": tier,
        "condition_raw": None, "t_days": None, "t_upper_days": None, "t_entry_days": None,
        "listed_at": listed_at.isoformat(), "sold_at": None, "observed_at": now.isoformat(),
        "favourite_count": int(rng.poisson(2)) if platform == "vinted" else None,
        "best_offer_accepted": None, "is_outlier": False, "outlier_reason": None,
    }
