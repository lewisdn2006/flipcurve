"""Per-platform constants. PLACEHOLDERS for the mock: Person 1 verifies every number
against each platform's current UK fee page in Phase 3 (step 17) before the demo.

speed_multiplier   lambda_platform = lambda * m  (smaller = sells faster). Replaced by a
                   fitted value when both platforms have enough data.
offer_buffer       typical haggle below list price; list = strike / (1 - buffer).
seller_fee_pct     fraction of strike taken from the seller.
local_discount     FB/Gumtree buyers expect a cheaper price than shipped platforms, so a
                   local strike p "feels like" p / (1 - local_discount) to the shipped market.
flake_prob         chance an agreed local buyer never turns up.
"""

PLATFORMS = {
    "vinted": {
        "speed_multiplier": 0.85,
        "offer_buffer": 0.13,
        "seller_fee_pct": 0.0,        # VERIFY: buyer pays buyer-protection fee + postage
        "seller_fixed_gbp": 0.0,
        "buyer_fee_pct": 0.05,        # VERIFY
        "buyer_fee_fixed_gbp": 0.70,  # VERIFY
        "local_discount": 0.0,
        "flake_prob": 0.0,
    },
    "ebay": {
        "speed_multiplier": 1.0,
        "offer_buffer": 0.04,
        "seller_fee_pct": 0.0,        # VERIFY: UK private-seller fee rules
        "seller_fixed_gbp": 0.0,
        "buyer_fee_pct": 0.0,         # VERIFY: eBay UK buyer protection fee
        "buyer_fee_fixed_gbp": 0.0,
        "local_discount": 0.0,
        "flake_prob": 0.0,
    },
    "fb_marketplace": {
        "speed_multiplier": 0.70,
        "offer_buffer": 0.15,
        "seller_fee_pct": 0.0,        # local cash pickup
        "seller_fixed_gbp": 0.0,
        "buyer_fee_pct": 0.0,
        "buyer_fee_fixed_gbp": 0.0,
        "local_discount": 0.12,
        "flake_prob": 0.25,
    },
}

DEFAULT_GAMMA_PER_DAY = 0.05  # "Best EV" preset urgency: a sale in 2 weeks is worth ~50% of one today
