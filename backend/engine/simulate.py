"""Synthetic marketplace listings drawn from known model parameters (step 8).

Used for: the recovery test (fit simulated data, check the true parameters come back),
the mock payload's evidence scatter, and demo-day fallback data.

It mimics how each platform lets us observe listings (see DECISIONS.md D1):
  eBay    active  -> exact start time from search, obs 'active'
          sold    -> visible for 90 days; start from item-number calibration, sold date is
                     day-only, so obs 'sold_interval' of about +/- half a day
  Vinted  active  -> age from ID calibration, obs 'active'
          sold    -> only seen through a re-check: listed and active at the first scrape,
                     sold by the re-check, so obs 'sold_interval' (age then, age now]
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
    history_days: float = 120.0,
    ebay_sold_window_days: float = 90.0,
    vinted_recheck_hours: float | None = 18.0,
    calibration_error_days: float = 0.25,
    tier_weights=(0.15, 0.45, 0.28, 0.12),
    seed: int = 7,
) -> list[dict]:
    """Return ListingRow-shaped dicts for every listing we would actually be able to see."""
    rng = np.random.default_rng(seed)
    now = now or datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
    rows: list[dict] = []
    for platform in PLATFORMS:
        cfg = PLATFORMS[platform]
        tiers = rng.choice(TIERS, size=n_per_platform, p=tier_weights)
        for i, tier in enumerate(tiers):
            anchor = params.anchor[tier]
            # sellers mostly price a little above the going rate, with a long right tail
            price = float(np.round(anchor * np.exp(rng.normal(0.05, 0.22)), 0) - 0.01)
            price = max(price, 1.99)
            lam = float(params.scale(price, tier, platform))
            T = lam * (-np.log(rng.uniform())) ** (1.0 / params.k)
            start_ago = rng.uniform(0, history_days)          # days before `now`
            listed_at = now - timedelta(days=start_ago)
            sold_ago = start_ago - T                          # > 0 means sold before now
            row = _row(platform, f"{platform[:2]}{i:05d}", tier, price, cfg, now, listed_at, rng)

            if platform == "ebay":
                if sold_ago <= 0:
                    row.update(obs_type="active", t_days=round(start_ago, 3), start_source="exact")
                elif sold_ago <= ebay_sold_window_days:
                    sold_at = now - timedelta(days=sold_ago)
                    sold_day_noon = sold_at.replace(hour=12, minute=0, second=0, microsecond=0)
                    centre = (sold_day_noon - listed_at).total_seconds() / 86400
                    half = 0.5 + calibration_error_days
                    row.update(obs_type="sold_interval", t_days=round(max(0.0, centre - half), 3),
                               t_upper_days=round(max(0.0, centre - half) + 2 * half, 3),
                               start_source="id_calibration", sold_at=sold_day_noon.isoformat(),
                               best_offer_accepted=bool(rng.uniform() < 0.2))
                else:
                    continue  # sold too long ago for eBay's sold search
            else:  # vinted
                if sold_ago <= 0:
                    row.update(obs_type="active", t_days=round(start_ago, 3), start_source="id_calibration")
                elif vinted_recheck_hours is not None:
                    first_scrape_ago = vinted_recheck_hours / 24
                    if start_ago > first_scrape_ago and sold_ago < first_scrape_ago:
                        # active at the first scrape, sold by the re-check
                        row.update(obs_type="sold_interval", t_days=round(start_ago - first_scrape_ago, 3),
                                   t_upper_days=round(start_ago, 3), start_source="id_calibration")
                    else:
                        continue  # sold before we ever saw it
                else:
                    continue
            rows.append(row)
    return rows


def _row(platform, lid, tier, price, cfg, now, listed_at, rng):
    ship = SHIPPING[platform]
    fee = round(price * cfg["buyer_fee_pct"] + cfg["buyer_fee_fixed_gbp"], 2)
    return {
        "listing_id": lid, "platform": platform, "title": f"Simulated {tier.replace('_', ' ')} listing",
        "url": None, "price_gbp": price, "shipping_gbp": ship, "buyer_fee_gbp": fee,
        "total_buyer_price_gbp": round(price + ship + fee, 2), "condition_tier": tier,
        "condition_raw": None, "t_days": None, "t_upper_days": None, "listed_at": listed_at.isoformat(),
        "sold_at": None, "observed_at": now.isoformat(),
        "favourite_count": int(rng.poisson(2)) if platform == "vinted" else None,
        "best_offer_accepted": None, "is_outlier": False, "outlier_reason": None,
    }
