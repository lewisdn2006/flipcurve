// Per-platform constants (backend/engine/platform_config.py) and the money side of the
// recommendation (backend/engine/platform_economics.py; maths doc, section 8).
//
// expected sale price = list x (1 - offer buffer)
// net payout          = expected sale price x (1 - seller fee) - fixed seller cost
// value               = net payout x E[exp(-gamma T)]   (money now beats money later)

import { discountedSaleValue } from './survival.js';
import { pyRound } from './numeric.js';

// Order matters: the Python's dict order, which decides ties between platforms.
export const PLATFORMS = {
  vinted: { prior_theta: 0.0, offer_buffer: 0.13, seller_fee_pct: 0.0, seller_fixed_gbp: 0.0,
    buyer_fee_pct: 0.05, buyer_fee_fixed_gbp: 0.70 },  // buyer pays 5% + £0.70, verified 24 Oct 2026
  ebay: { prior_theta: 0.0, offer_buffer: 0.04, seller_fee_pct: 0.0, seller_fixed_gbp: 0.0,
    buyer_fee_pct: 0.0, buyer_fee_fixed_gbp: 0.0 },     // UK private sellers pay no selling fees
};
export const DEFAULT_GAMMA_PER_DAY = 0.02;  // urgency for "best value": waiting a month costs ~45%
export const SURFACE_DAYS = [1, 2, 3, 5, 7, 10, 14, 21, 30, 45, 60, 90];

// eBay UK buyer protection fee on private-seller items: £0.10 + 7% of the first £20, 4% of
// £20-£300, 2% of £300-£4,000. Only affects total_buyer_price, not payouts.
const EBAY_BUYER_FEE_TIERS = [[20.0, 0.07], [300.0, 0.04], [4000.0, 0.02]];

export function ebayBuyerFee(price) {
  let fee = 0.10, prev = 0;
  for (const [cap, rate] of EBAY_BUYER_FEE_TIERS) {
    if (price > prev) fee += (Math.min(price, cap) - prev) * rate;
    prev = cap;
  }
  return pyRound(fee, 2);
}

export function buyerFee(price, platform) {
  if (platform === 'ebay') return ebayBuyerFee(price);
  const cfg = PLATFORMS[platform];
  return pyRound(price * cfg.buyer_fee_pct + cfg.buyer_fee_fixed_gbp, 2);
}

export const expectedSalePrice = (prices, platform) => prices.map((p) => p * (1 - PLATFORMS[platform].offer_buffer));

export function netPayout(prices, platform) {
  const cfg = PLATFORMS[platform];
  return expectedSalePrice(prices, platform).map((s) => s * (1 - cfg.seller_fee_pct) - cfg.seller_fixed_gbp);
}

/** net payout x E[exp(-gamma T)] for each list price (with its lambda). */
export function value(prices, lams, k, platform, gamma = DEFAULT_GAMMA_PER_DAY) {
  const disc = discountedSaleValue(lams, k, gamma);
  return netPayout(prices, platform).map((n, i) => n * disc[i]);
}

/** Three steps: list at the best price, drop halfway on day 4, then a floor on day 10. The floor is
 *  the highest price with over 90% chance of selling within 48 hours; if none, the quick-sale price. */
export function markdownSchedule(prices, lam2dayProbs, startPrice, platform, fallbackFloor = null) {
  const ok = prices.filter((_, i) => lam2dayProbs[i] >= 0.9);
  let floor, why;
  if (ok.length) {
    [floor, why] = [Math.max(...ok), 'over 90% chance of selling within 48 hours'];
  } else {
    floor = fallbackFloor ?? startPrice * 0.75;
    why = "the quick-sale price; this item doesn't sell within 48 hours at any sensible price";
  }
  floor = Math.min(floor, startPrice * 0.9);
  const mid = (startPrice + floor) / 2;
  const name = platform === 'vinted' ? 'Vinted' : 'eBay';
  return [
    { day: 0, list_price_gbp: pyRound(startPrice, 2), platform, action: `List on ${name} at £${startPrice.toFixed(2)}` },
    { day: 4, list_price_gbp: pyRound(mid, 2), platform,
      action: `If unsold, drop to £${mid.toFixed(2)} so watchers and favouriters get a price-drop alert` },
    { day: 10, list_price_gbp: pyRound(floor, 2), platform, action: `Floor: £${floor.toFixed(2)} (${why})` },
  ];
}
