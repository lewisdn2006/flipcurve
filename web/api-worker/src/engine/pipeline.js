// Listings in, MarketScanResponse out: filter -> fit -> curves, markers, platforms, schedule.
// Port of backend/engine/pipeline.py, checked against its saved results in test/node/engine.test.js.
//
//     import { run } from './engine/pipeline.js';
//     const payload = run(item, rows, { dataSource: 'live' });
//
// One difference: when there are more than 500 comparable listings, the active ones shown are an
// evenly spaced sample instead of numpy's seeded random one.

import { DEFAULT_GAMMA_PER_DAY, PLATFORMS, SURFACE_DAYS, expectedSalePrice, markdownSchedule, netPayout, value } from './economics.js';
import { filterRows } from './outliers.js';
import { linspace, median, npRound, percentile, pyRound } from './numeric.js';
import * as sm from './survival.js';

const TIERS = sm.TIERS;
const GRID_POINTS = 50;
const MAX_DAYS = 365.0;          // durations are clipped here; the UI shows 365 as "1 year+"
const MAX_EVIDENCE_ROWS = 500;

const r2 = (x, nd = 2) => pyRound(Number(x), nd);
const days = (x) => r2(Math.min(Number(x), MAX_DAYS), 1);
const argmax = (xs) => xs.reduce((best, v, i) => (v > xs[best] ? i : best), 0);
const maxOf = (xs) => Math.max(...xs);
/** Python's max(items, key=...): the first item with the largest key. */
const maxBy = (items, key) => items.reduce((best, x) => (key(x) > key(best) ? x : best));

/** Badge rule (DECISIONS.md D3). */
export function confidence(nSales, low, high) {
  if (nSales === 0) return 'estimated';
  if (nSales >= 30 && high < 2 * low) return 'high';
  if (nSales >= 10) return 'medium';
  return 'low';
}

const RELATED_MIN = 5;
// With this many sales of the exact item, similar versions stop joining the speed fit
const RELATED_ENOUGH_EXACT_SALES = 30;
// Price level from recent sales: the newest window with this many exact sales sets the price level,
// the recommendation ceilings and the condition ranges. Older sales still count for selling speed.
const RECENT_WINDOWS_DAYS = [30, 45, 90];
const RECENT_MIN_SALES = 10;

/** ISO time (Python's isoformat, with or without a zone; naive = UTC) -> ms, or null. */
export function parseTime(v) {
  if (!v) return null;
  let s = String(v).replace(' ', 'T').replace(/(\.\d{3})\d+/, '$1');
  if (!/(Z|[+-]\d\d:?\d\d)$/.test(s) && /T\d/.test(s)) s += 'Z';
  const t = Date.parse(s);
  return Number.isNaN(t) ? null : t;
}

/** Python's datetime.isoformat() for a UTC moment: 2026-10-25T15:46:37.082000+00:00. */
export function pyIsoformat(ms) {
  const iso = new Date(ms).toISOString();
  const frac = ms % 1000 ? `.${iso.slice(20, 23)}000` : '';
  return `${iso.slice(0, 19)}${frac}+00:00`;
}

/** When a sale happened (or, for a listing, when it was seen). */
function when(r) {
  for (const k of ['sold_at', 'observed_at', 'listed_at']) {
    const t = parseTime(r[k]);
    if (t !== null) return t;
  }
  return null;
}

/** Exact-match rows that set the price level: every listing, but only the most recent sales when there
 *  are enough of them. Returns [rows, days or null, n recent sales, n all sales]. */
export function priceRows(rows, now) {
  const exact = rows.filter((r) => !r.related);
  const sold = exact.filter((r) => r.obs_type !== 'active' && !r.is_outlier);
  for (const d of RECENT_WINDOWS_DAYS) {
    const recent = sold.filter((r) => { const w = when(r); return w !== null && (now - w) / 1000 <= d * 86400; });
    if (recent.length >= RECENT_MIN_SALES) {
      if (recent.length === sold.length) return [exact, null, sold.length, sold.length];  // every sale is recent anyway
      const keep = new Set(recent);
      return [exact.filter((r) => r.obs_type === 'active' || keep.has(r)), d, recent.length, sold.length];
    }
  }
  return [exact, null, sold.length, sold.length];
}

const RELATED_LABEL = { mid: 'Mid', low: 'Low', high: 'High', hi: 'High', kids: "kids' sizes", pro: 'Pro', max: 'Max',
  plus: 'Plus', mini: 'Mini', ultra: 'Ultra', lite: 'Lite', se: 'SE' };
const label = (g) => RELATED_LABEL[g] || g;

/** Similar-but-different versions join the speed fit with prices rescaled to this item's level; tiny
 *  groups are dropped as outliers. Returns [rows, rows for the fit, {label: n used}]. */
function withRelated(rows, baseRows = null) {
  const exact = rows.filter((r) => !r.related);
  const base = exact.length ? sm.estimateAnchors(baseRows ?? exact).good : null;
  const fitRows = exact.filter((r) => !r.is_outlier);
  const used = {};
  const nExactSales = fitRows.filter((r) => r.obs_type !== 'active').length;
  const groups = [...new Set(rows.filter((r) => r.related && !r.is_outlier).map((r) => r.related))].sort();
  for (const g of groups) {
    const grp = rows.filter((r) => r.related === g && !r.is_outlier);
    if (nExactSales >= RELATED_ENOUGH_EXACT_SALES) {
      for (const r of grp) Object.assign(r, { is_outlier: true, outlier_reason: `similar version (${label(g)}) not needed` });
      continue;
    }
    if (grp.length < RELATED_MIN || base === null) {
      for (const r of grp) Object.assign(r, { is_outlier: true, outlier_reason: `different model (${label(g)})` });
      continue;
    }
    const ratio = base / sm.estimateAnchors(grp).good;
    fitRows.push(...grp.map((r) => ({ ...r, price_gbp: r.price_gbp * ratio })));
    used[label(g)] = grp.length;
  }
  return [rows, fitRows, used];
}

const priceAxis = (f) => linspace(0.5 * f.anchors.fair, 1.35 * f.anchors.mint, GRID_POINTS).map((p) => npRound(p, 2));  // np.round

/** Highest list prices we'll recommend for this condition (best value, patient), from what exact
 *  matches actually sold for, and the lowest quick-sale price (their 25th percentile). */
function priceCeilings(rows, tier, anchors) {
  const ratio = sm.DEFAULT_TIER_RATIO;
  const exact = rows.filter((r) => r.obs_type !== 'active' && !r.is_outlier && !r.related);
  const same = exact.filter((r) => sm.tierOf(r) === tier).map((r) => r.price_gbp);
  const sold = same.length >= 3 ? same : exact.map((r) => r.price_gbp * ratio[tier] / ratio[sm.tierOf(r)]);
  if (sold.length >= 3) {
    const med = median(sold), p90 = percentile(sold, 90), p25 = percentile(sold, 25);
    return [Math.min(1.35 * med, p90), Math.min(1.6 * med, 1.15 * p90), p25];
  }
  return [1.3 * anchors[tier], 1.5 * anchors[tier], 0.75 * anchors[tier]];
}

function platformPoints(f, tier, prices, ceilings = [Infinity, Infinity, 0]) {
  const out = {};
  for (const pl of f.platforms) {
    const lam = f.scale(prices, tier, pl);
    const [mean, lo, hi] = f.meanDaysRange(prices, tier, pl);
    const val = value(prices, lam, f.k, pl);
    out[pl] = {
      lam, mean, lo, hi, sale: expectedSalePrice(prices, pl), net: netPayout(prices, pl),
      p7: lam.map((l) => sm.cdf(7, l, f.k)),
      // prices above what has ever sold aren't candidates for a recommendation
      value: val.map((v, i) => (prices[i] <= ceilings[0] ? v : 0)), okPatient: prices.map((p) => p <= ceilings[1]),
      quickFloor: ceilings[2],
    };
  }
  return out;
}

function grid(f, tier, prices, pp) {
  const lam = f.scale(prices, tier);
  const k = f.k;
  return prices.map((p, i) => ({
    price_gbp: r2(p),
    prob_sell_24h: r2(sm.cdf(1, lam[i], k), 3),
    prob_sell_7d: r2(sm.cdf(7, lam[i], k), 3),
    prob_sell_30d: r2(sm.cdf(30, lam[i], k), 3),
    expected_days_to_sell: days(sm.expectedDays(lam[i], k)),
    median_days_to_sell: days(sm.quantileDays(0.5, lam[i], k)),
    ci_days_low: days(sm.quantileDays(0.05, lam[i], k)),
    ci_days_high: days(sm.quantileDays(0.95, lam[i], k)),
    platforms: Object.fromEntries(f.platforms.map((pl) => [pl, {
      expected_sale_price_gbp: r2(pp[pl].sale[i]), net_payout_gbp: r2(pp[pl].net[i]), prob_sell_7d: r2(pp[pl].p7[i], 3),
      expected_days: days(pp[pl].mean[i]), expected_days_low: days(pp[pl].lo[i]), expected_days_high: days(pp[pl].hi[i]),
    }])),
    best_platform: maxBy(f.platforms, (pl) => pp[pl].value[i]),
  }));
}

const PATIENT_GAMMA_PER_DAY = 0.005;  // "happy to wait": a month's wait costs ~15%

/** Three list prices for three levels of urgency, always quick <= best value <= patient. */
function markers(gridPts, pp, platforms, anchor, k) {
  const prices = gridPts.map((g) => g.price_gbp);
  const bestPl = maxBy(platforms, (pl) => maxOf(pp[pl].value));
  const best = prices[argmax(pp[bestPl].value)];
  const disc = sm.discountedSaleValue(pp[bestPl].lam, k, PATIENT_GAMMA_PER_DAY);
  const patientValue = pp[bestPl].net.map((n, i) => (pp[bestPl].okPatient[i] ? n * disc[i] : 0));
  const patient = Math.max(prices[argmax(patientValue)], best);
  const p7 = prices.filter((_, i) => gridPts[i].prob_sell_7d >= 0.5);
  let fast, note = null;
  if (p7.length) fast = maxOf(p7);
  else {
    fast = 0.85 * anchor;
    note = 'Even below the usual price this item rarely sells within a week, so the quick-sale '
      + 'price is set just under what similar ones sold for';
  }
  fast = Math.min(Math.max(fast, pp[bestPl].quickFloor), best);
  return { fast_sale_gbp: r2(fast), best_value_gbp: r2(best), patient_gbp: r2(patient), gamma_per_day: DEFAULT_GAMMA_PER_DAY, note };
}

function summary(rows, nowMs, dataSource, platformsOk, bucketRows = null) {
  const clean = rows.filter((r) => !r.is_outlier && !r.related);
  const sold = clean.filter((r) => r.obs_type !== 'active');
  const active = clean.filter((r) => r.obs_type === 'active');
  const med = (xs) => (xs.length ? median(xs.map((x) => x.price_gbp)) : null);
  const a = med(active), s = med(sold);
  // survivorship gap within one platform, like for like on condition
  const norm = (xs) => median(xs.map((x) => x.price_gbp / sm.DEFAULT_TIER_RATIO[sm.tierOf(x)]));
  let pick = null;
  for (const pl of new Set(sold.map((r) => r.platform))) {
    const ps = sold.filter((r) => r.platform === pl), pa = active.filter((r) => r.platform === pl);
    if (ps.length >= 5 && pa.length >= 5) {
      const cand = [ps.length, norm(pa), norm(ps)];
      // Python's max() over (n, active, sold) tuples
      if (!pick || cand[0] > pick[0] || (cand[0] === pick[0] && (cand[1] > pick[1] || (cand[1] === pick[1] && cand[2] > pick[2])))) pick = cand;
    }
  }
  const gap = pick ? (pick[1] - pick[2]) / pick[2] * 100 : null;
  // condition ranges from the same (recent) sales that set the price level
  const bsold = (bucketRows ?? clean).filter((r) => !r.is_outlier && !r.related && r.obs_type !== 'active');
  const buckets = {};
  for (const t of TIERS) {
    const ps = bsold.filter((x) => (x.condition_tier || 'good') === t).map((x) => x.price_gbp);
    buckets[t] = ps.length
      ? { p25: r2(percentile(ps, 25)), median: r2(percentile(ps, 50)), p75: r2(percentile(ps, 75)), n: ps.length }
      : { p25: 0.0, median: 0.0, p75: 0.0, n: 0 };
  }
  return {
    sample_size_sold: sold.length, sample_size_active: active.length, sample_size_excluded: rows.length - clean.length,
    active_median_gbp: a ? r2(a) : null, sold_median_gbp: s ? r2(s) : null,
    survivorship_bias_pct: gap !== null ? r2(gap, 1) : null,
    condition_buckets: buckets, data_source: dataSource, platforms_ok: platformsOk, fetched_at: pyIsoformat(nowMs),
  };
}

function evidence(rows) {
  const clean = rows.filter((r) => !r.is_outlier);
  if (clean.length <= MAX_EVIDENCE_ROWS) return clean;
  const sold = clean.filter((r) => r.obs_type !== 'active').slice(0, MAX_EVIDENCE_ROWS / 2);
  const act = clean.filter((r) => r.obs_type === 'active');
  const m = Math.min(act.length, MAX_EVIDENCE_ROWS - sold.length);
  return sold.concat(Array.from({ length: m }, (_, i) => act[Math.floor(i * act.length / m)]));
}

/**
 * item: ItemSpec; rows: ListingRows. now: ms or ISO time (default: now); dataSource: "live" | "seed" | ...
 * onFit: called with the FitResult (tests compare it with the Python's).
 */
export function run(item, rows, { now = null, dataSource = 'live', platforms = Object.keys(PLATFORMS), prior = undefined, onFit = null } = {}) {
  const nowMs = now === null ? Date.now() : typeof now === 'number' ? now : parseTime(now);
  const itemText = ['title', 'model_name', 'category', 'size_or_storage'].map((k) => String(item[k] ?? '')).join(' ');
  rows = filterRows(rows, item.search_queries, item.is_broken || false,
    { itemText, mustHave: item.must_have || [], mustNotHave: item.must_not_have || [] });
  const [prows, recentDays, nRecent, nSoldAll] = priceRows(rows, nowMs);
  let fitRows, related;
  [rows, fitRows, related] = withRelated(rows, prows);
  const droppedRelated = [...new Set(rows.map((r) => r.outlier_reason || '').filter((x) => x.endsWith('not needed')))].sort();
  const f = sm.fit(fitRows, { platforms, prior, anchors: sm.estimateAnchors(prows) });
  if (onFit) onFit(f);
  const prices = priceAxis(f);

  const grids = {}, surfaces = {}, marks = {}, pps = {};
  for (const t of TIERS) {
    pps[t] = platformPoints(f, t, prices, priceCeilings(prows, t, f.anchors));
    grids[t] = grid(f, t, prices, pps[t]);
    surfaces[t] = { days: SURFACE_DAYS.map(Number), prob: f.scale(prices, t).map((l) => SURFACE_DAYS.map((d) => r2(sm.cdf(d, l, f.k), 3))) };
    marks[t] = markers(grids[t], pps[t], platforms, f.anchors[t], f.k);
  }

  const tier = item.condition_tier;
  const pp = pps[tier];
  const count = (pred) => Object.fromEntries(platforms.map((pl) => [pl, fitRows.filter((r) => r.platform === pl && pred(r)).length]));
  const nList = count(() => true);
  const nRelSales = count((r) => r.related && ['sold_exact', 'sold_interval'].includes(r.obs_type));
  const nStock = count((r) => r.stock_window_days);
  const recs = {};
  for (const pl of platforms) {
    const i = argmax(pp[pl].value);
    const lo = pp[pl].lo[i], hi = pp[pl].hi[i];
    let conf = confidence(f.n_sales[pl], lo, hi);
    const notes = [];
    const site = pl === 'vinted' ? 'Vinted' : 'eBay';
    if (conf === 'estimated' && nStock[pl] >= 20) {
      conf = 'low';
      notes.push(`No ${site} sales seen yet: speed comes from how long the ${nStock[pl]} `
        + `current ${site} listings have been up (scan again later to catch sales)`);
    } else if (conf === 'estimated') {
      notes.push(`No ${site} sales seen yet: speed leans on the other platform and typical values`);
    } else if (conf === 'low') {
      notes.push(`Only ${f.n_sales[pl]} ${site} sales seen, so treat the timing as rough`);
    }
    if (nRelSales[pl]) {
      notes.push(`${nRelSales[pl]} of the ${site} sales are similar items (${Object.keys(related).join(', ')}), with prices scaled to this one`);
    }
    recs[pl] = {
      recommended_list_gbp: r2(prices[i]), expected_sale_price_gbp: r2(pp[pl].sale[i]),
      net_payout_gbp: r2(pp[pl].net[i]), prob_sell_7d: r2(pp[pl].p7[i], 3),
      expected_days: days(pp[pl].mean[i]), expected_days_low: days(lo), expected_days_high: days(hi),
      median_days: days(sm.quantileDays(0.5, pp[pl].lam[i], f.k)),
      offer_buffer_pct: r2(PLATFORMS[pl].offer_buffer * 100, 1),
      n_listings: nList[pl], n_sales: f.n_sales[pl], confidence: conf, notes,
    };
  }
  const bestPl = maxBy(platforms, (pl) => maxOf(pp[pl].value));
  const lamBest = f.scale(prices, tier, bestPl);
  const schedule = markdownSchedule(prices, lamBest.map((l) => sm.cdf(2, l, f.k)), recs[bestPl].recommended_list_gbp, bestPl,
    marks[tier].fast_sale_gbp);

  const warnings = [];
  if (recentDays) {
    warnings.push(`Prices are set from the ${nRecent} sales in the last ${recentDays} days (of ${nSoldAll}), `
      + 'since older sales may reflect a higher value; older ones still count for selling speed');
  }
  if (droppedRelated.length) {
    warnings.push(`Enough sales of this exact item (${fitRows.filter((r) => r.obs_type !== 'active').length}), `
      + "so similar versions weren't used: " + droppedRelated.map((d) => d.replace(' not needed', '')).join(', '));
  }
  if (f.used_priors) warnings.push('Few observed sales: the speed estimates lean on typical values for similar items');
  if (Object.keys(related).length) {
    warnings.push('Few exact matches, so selling speed also uses similar items ('
      + Object.entries(related).map(([k, v]) => `${k}: ${v}`).join(', ')
      + " listings), each scaled to this item's price level");
  }
  if (f.beta1 <= sm.beta1Min(f.anchors, f.sigma_p) + 1e-3) {
    warnings.push('Within the usual price range, price made little visible difference to how fast these '
      + 'sold, so the suggested prices stay within the range recent sales actually reached');
  }
  if (!f.converged) warnings.push('Model fit did not fully converge; treat the numbers as rough');
  const platformsOk = [...new Set(rows.map((r) => r.platform))].sort();
  return {
    item_metadata: item,
    market_summary: summary(rows, nowMs, dataSource, platformsOk, prows),
    model_fit: {
      k: r2(f.k, 4), lambda0: r2(f.lambda0, 4), beta1: r2(f.beta1, 4), beta2: r2(f.beta2, 4),
      sigma_p: r2(f.sigma_p, 3), anchor_gbp: f.anchors,
      platform_theta: Object.fromEntries(Object.entries(f.theta).map(([p, v]) => [p, r2(v, 4)])), platform_n_sales: f.n_sales,
      n_obs: f.n_obs, n_censored: f.n_censored, n_interval: f.n_interval, used_priors: f.used_priors,
      log_likelihood: r2(f.log_likelihood, 3),
    },
    price_range: { min_gbp: r2(prices[0]), max_gbp: r2(prices.at(-1)), step_gbp: r2(prices[1] - prices[0]) },
    price_curve_grid: grids,
    sale_surface: surfaces,
    price_markers: marks,
    best_platform: bestPl,
    platform_recommendations: recs,
    markdown_schedule: schedule,
    comparable_listings: evidence(rows),
    warnings,
  };
}
