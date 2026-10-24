"""Per-platform constants. PLACEHOLDERS until Person 1 verifies every number marked VERIFY
against each platform's current UK fee page (step 11).

prior_theta        prior mean of the platform speed term theta_j (maths doc, section 6);
                   lambda is multiplied by exp(theta). Replaced by the fitted value as data arrives.
offer_buffer       typical haggle below list price; expected sale price = list * (1 - buffer).
seller_fee_pct     fraction of the sale price taken from the seller.
buyer_fee_*        what the buyer pays on top (affects total_buyer_price, not the seller's cash).
"""

PLATFORMS = {
    "vinted": {
        "prior_theta": 0.0,
        "offer_buffer": 0.13,
        "seller_fee_pct": 0.0,        # Vinted charges sellers nothing; the buyer pays fee + postage
        "seller_fixed_gbp": 0.0,
        "buyer_fee_pct": 0.05,        # verified 24 Oct 2026 from listing prices (5% + £0.70)
        "buyer_fee_fixed_gbp": 0.70,
    },
    "ebay": {
        "prior_theta": 0.0,
        "offer_buffer": 0.04,
        "seller_fee_pct": 0.0,        # verified Oct 2026: UK private sellers pay no selling fees
        "seller_fixed_gbp": 0.0,
        "buyer_fee_pct": 0.0,         # unused: eBay's tiered buyer fee is platform_economics.ebay_buyer_fee
        "buyer_fee_fixed_gbp": 0.0,
    },
}

# Deferred (DECISIONS.md D9). Kept so the numbers aren't lost when FB is added back.
DEFERRED_PLATFORMS = {
    "fb_marketplace": {"prior_theta": -0.36, "offer_buffer": 0.15, "seller_fee_pct": 0.0,
                       "local_discount": 0.12, "flake_prob": 0.25},
}

DEFAULT_GAMMA_PER_DAY = 0.05  # urgency for "best value": a sale in 2 weeks is worth ~half one today
SURFACE_DAYS = [1, 2, 3, 5, 7, 10, 14, 21, 30, 45, 60, 90]  # y-axis of the price x time heatmap
TAU = 0.5  # prior sd of theta_j (maths doc, section 6)
