"""Money side of the recommendation (step 11; maths doc, section 8).

expected sale price = list x (1 - offer buffer)
net payout          = expected sale price x (1 - seller fee) - fixed seller cost
value               = net payout x E[exp(-gamma T)]   (money now beats money later)
The best list price on a platform maximises value; the best platform has the highest value.

Owner: Person 1.
"""
from __future__ import annotations

import numpy as np

from backend.engine import survival_model as sm
from backend.engine.platform_config import DEFAULT_GAMMA_PER_DAY, PLATFORMS

# eBay UK buyer protection fee on private-seller items (paid by the buyer; seller pays nothing).
# Tiers as published by fee calculators in 2026: £0.10 + 7% of the first £20, 4% of £20-£300,
# 2% of £300-£4,000. Sources disagree slightly; it only affects total_buyer_price, not payouts.
EBAY_BUYER_FEE_TIERS = [(20.0, 0.07), (300.0, 0.04), (4000.0, 0.02)]


def ebay_buyer_fee(price: float) -> float:
    fee, prev = 0.10, 0.0
    for cap, rate in EBAY_BUYER_FEE_TIERS:
        if price > prev:
            fee += (min(price, cap) - prev) * rate
        prev = cap
    return round(fee, 2)


def buyer_fee(price: float, platform: str) -> float:
    if platform == "ebay":
        return ebay_buyer_fee(price)
    cfg = PLATFORMS[platform]
    return round(price * cfg["buyer_fee_pct"] + cfg["buyer_fee_fixed_gbp"], 2)


def expected_sale_price(list_price, platform: str):
    return np.asarray(list_price, float) * (1 - PLATFORMS[platform]["offer_buffer"])


def net_payout(list_price, platform: str):
    cfg = PLATFORMS[platform]
    return expected_sale_price(list_price, platform) * (1 - cfg["seller_fee_pct"]) - cfg["seller_fixed_gbp"]


def value(list_price, lam, k: float, platform: str, gamma: float = DEFAULT_GAMMA_PER_DAY):
    """net payout x E[exp(-gamma T)], vectorised over list prices (and matching lambdas)."""
    return net_payout(list_price, platform) * sm.discounted_sale_value(lam, k, gamma)


def markdown_schedule(prices: np.ndarray, lam_2day_probs: np.ndarray, start_price: float, platform: str,
                      fallback_floor: float | None = None):
    """Three steps: list at the best price, drop halfway on day 4, then a floor on day 10. The floor
    is the highest price with over 90% chance of selling within 48 hours (lam_2day_probs = P(sell
    within 2 days) per price); if no price gets there, the quick-sale price instead."""
    ok = prices[lam_2day_probs >= 0.9]
    r = lambda x: float(round(x, 2))  # noqa: E731
    if ok.size:
        floor, why = float(ok.max()), "over 90% chance of selling within 48 hours"
    else:
        floor = float(fallback_floor if fallback_floor is not None else start_price * 0.75)
        why = "the quick-sale price; this item doesn't sell within 48 hours at any sensible price"
    floor = min(floor, start_price * 0.9)
    mid = (start_price + floor) / 2
    name = "Vinted" if platform == "vinted" else "eBay"
    return [
        {"day": 0, "list_price_gbp": r(start_price), "platform": platform,
         "action": f"List on {name} at £{start_price:.2f}"},
        {"day": 4, "list_price_gbp": r(mid), "platform": platform,
         "action": f"If unsold, drop to £{mid:.2f} so watchers and favouriters get a price-drop alert"},
        {"day": 10, "list_price_gbp": r(floor), "platform": platform,
         "action": f"Floor: £{floor:.2f} ({why})"},
    ]
