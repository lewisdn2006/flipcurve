// Censored Weibull AFT survival model: closed forms + penalised (MAP) fit.
// Port of backend/engine/survival_model.py; the maths is in "FlipCurve: The Maths Behind
// Time-to-Sell" (sections referenced below). Checked against the Python's fits in
// test/node/engine.test.js.
//
// Model (section 4):
//     z_i          = (p_i - M(c_i)) / sigma_p
//     ln lambda_i  = eta0 + beta1 * z_i - beta2 * (c_i - 2) + theta_{platform(i)}
//     S(t)         = exp(-(t / lambda)^k)
// Likelihood (section 5): active -> ln S(t); sold_exact -> ln f(t); sold_interval -> ln[S(a) - S(b)];
// every row first seen at age e > 0 also gets - ln S(e) (left truncation).
// Stock rows (a newest-first catalogue read back to age A, e.g. Vinted): a visible listing's age has
// density S(t) / int_0^A S(u) du.
// Penalties = normal priors (section 6). Confidence (section 7): finite-difference Hessian ->
// covariance -> delta method on ln E[T].

import { clip, digamma, gammainc, gammaln, invert, linspace, median, minimizeBounded, pinvSym, pyRound } from './numeric.js';

export const C_REF = 2;  // "good"
export const TIERS = ['fair', 'good', 'very_good', 'mint'];
export const CIDX = { fair: 1, good: 2, very_good: 3, mint: 4 };
// Typical price of each condition relative to Good; only used to fill a tier with too few sales.
export const DEFAULT_TIER_RATIO = { fair: 0.65, good: 1.0, very_good: 1.32, mint: 1.9 };
const MIN_SOLD_PER_ANCHOR = 5;
export const PRIOR_MIN_N = 12;  // fewer observed sales than this -> flagged as leaning on priors
const T_MIN = 1e-3;             // days; avoids ln(0) for listings seen the moment they went up
const MIN_ELASTICITY = 1.0;     // at least: 10% dearer -> ~10% longer wait
const BETA1_FLOOR = 0.3;

// ------------------------------------------------------------------ closed forms (section 3)

export const survival = (t, lam, k) => Math.exp(-((t / lam) ** k));
/** P(T <= t): probability of selling within t days. */
export const cdf = (t, lam, k) => 1 - survival(t, lam, k);
export const expectedDays = (lam, k) => lam * Math.exp(gammaln(1 + 1 / k));
/** q-th quantile of time-to-sell, e.g. q=0.5 median, 0.05/0.95 for the 90% range. */
export const quantileDays = (q, lam, k) => lam * (-Math.log1p(-q)) ** (1 / k);

/** E[exp(-gamma T)] for T ~ Weibull(lam, k), by quadrature on [0, horizon] (section 8), for each lam.
 *  Mass beyond the horizon is treated as never selling (value 0). */
export function discountedSaleValue(lams, k, gammaPerDay, horizonDays = 180, n = 2000) {
  const t = linspace(0, horizonDays, n);
  const w = t.slice(1).map((ti, i) => Math.exp(-gammaPerDay * 0.5 * (ti + t[i])));
  return lams.map((lam) => {
    let sum = 0, prev = cdf(t[0], lam, k);
    for (let i = 1; i < n; i++) {
      const F = cdf(t[i], lam, k);
      sum += w[i - 1] * (F - prev);
      prev = F;
    }
    return sum;
  });
}

// ------------------------------------------------------------------ priors (section 6)

export const defaultPrior = () => ({
  beta1: 2.4, beta1_sd: 1.0, k: 0.85, lnk_sd: 0.3, beta2: 0.3, beta2_sd: 0.5,
  lambda0_days: 7.0,  // weak: keeps eta0 finite when nothing has sold yet
  eta0_sd: 1.5, tau: 0.5, theta_mean: {},
});

// ------------------------------------------------------------------ data prep (section 9, step 1)

export const tierOf = (r) => (r.condition_tier in CIDX ? r.condition_tier : 'good');

/** M(c): median sold price per condition; thin tiers filled from the others by typical ratios. */
export function estimateAnchors(rows) {
  const sold = rows.filter((r) => r.obs_type !== 'active' && !r.is_outlier);
  const good = {};
  for (const t of TIERS) {
    const v = sold.filter((r) => tierOf(r) === t).map((r) => Number(r.price_gbp));
    if (v.length >= MIN_SOLD_PER_ANCHOR) good[t] = median(v);
  }
  const adjusted = (rs) => rs.map((r) => Number(r.price_gbp) / DEFAULT_TIER_RATIO[tierOf(r)]);
  const out = (f) => Object.fromEntries(TIERS.map((t) => [t, pyRound(f(t), 2)]));
  if (!Object.keys(good).length && sold.length >= 3) {  // a few sales spread over tiers: pool them via the ratios
    const ref = median(adjusted(sold));
    return out((t) => ref * DEFAULT_TIER_RATIO[t]);
  }
  if (!Object.keys(good).length) {  // (almost) nothing sold: every listing, scaled down a little for survivorship
    const prices = adjusted(rows.filter((r) => !r.is_outlier));
    const base = prices.length ? 0.85 * median(prices) : 30.0;
    return out((t) => base * DEFAULT_TIER_RATIO[t]);
  }
  // pooled 'Good' level from every sale (condition-adjusted); a tier's own median is used only
  // within 0.7-1.5x of what the pooled level implies
  const ref = median(adjusted(sold));
  return out((t) => {
    const implied = ref * DEFAULT_TIER_RATIO[t];
    return clip(t in good ? good[t] : implied, 0.7 * implied, 1.5 * implied);
  });
}

/** sigma_p: robust spread of sold prices around their condition's anchor, relative to the anchor
 *  (MAD x 1.4826), in 'Good' pounds. */
export function estimateSigma(rows, anchors) {
  const sold = rows.filter((r) => r.obs_type !== 'active' && !r.is_outlier);
  const rel = sold.map((r) => Number(r.price_gbp) / anchors[tierOf(r)] - 1);
  if (rel.length < 4) return 0.3 * anchors.good;
  const m = median(rel);
  const mad = 1.4826 * median(rel.map((x) => Math.abs(x - m)));
  return clip(mad, 0.1, 0.6) * anchors.good;
}

/** Rows -> columns: kind 0 active, 1 exact, 2 interval, 3 stock sample (b = window A). */
function prepare(rows, platforms, anchors, sigmaP) {
  const pidx = Object.fromEntries(platforms.map((p, i) => [p, i]));
  const d = { z: [], cdev: [], j: [], kind: [], a: [], b: [], e: [] };
  for (const r of rows) {
    if (r.is_outlier) continue;
    const ot = r.obs_type, pl = r.platform;
    if (!(pl in pidx) || ot === 'sold_unknown_time') continue;
    const t1 = r.t_days, t2 = r.t_upper_days;
    let t0 = 't_entry_days' in r ? Number(r.t_entry_days || 0) : 0;
    const win = r.stock_window_days;
    let k, aa, bb;
    if (ot === 'active' && t1 != null && win) {
      if (Number(t1) > Number(win)) continue;  // older than the window we read back to: no information
      [k, aa, bb, t0] = [3, Math.max(Number(t1), T_MIN), Number(win), 0];
    } else if (ot === 'active' && t1 != null) {
      [k, aa, bb] = [0, Math.max(Number(t1), 0), NaN];
    } else if (ot === 'sold_exact' && t1 != null) {
      [k, aa, bb] = [1, Math.max(Number(t1), T_MIN), NaN];
    } else if (ot === 'sold_interval' && t1 != null && t2 != null) {
      [aa, bb] = [Math.max(Number(t1), 0), Number(t2)];
      if (bb - aa < 1e-6) [k, aa, bb] = [1, Math.max(aa, T_MIN), NaN];  // degenerate interval: an exact sale
      else k = 2;
    } else continue;
    const tier = tierOf(r);
    d.z.push((Number(r.price_gbp) - anchors[tier]) / sigmaP);
    d.cdev.push(CIDX[tier] - C_REF);
    d.j.push(pidx[pl]);
    d.kind.push(k);
    d.a.push(aa);
    d.b.push(bb);
    d.e.push(Math.min(t0, aa));
  }
  return d;
}

// ------------------------------------------------------------------ likelihood (section 5)

function loglik(psi, d, nPlat) {
  const [eta0, beta1, beta2] = psi;
  const k = Math.exp(psi[3 + nPlat]);
  const lnk = Math.log(k);
  const lgk = gammaln(1 / k);
  let ll = 0;
  for (let i = 0; i < d.kind.length; i++) {
    const lnlam = eta0 + beta1 * d.z[i] - beta2 * d.cdev[i] + psi[3 + d.j[i]];
    const a = d.a[i];
    switch (d.kind[i]) {
      case 0:
        if (a > 0) ll -= Math.exp(k * (Math.log(a) - lnlam));
        break;
      case 1: {
        const lu = Math.log(a) - lnlam;
        ll += lnk - lnlam + (k - 1) * lu - Math.exp(k * lu);
        break;
      }
      case 2: {
        const A = a > 0 ? Math.exp(k * (Math.log(Math.max(a, 1e-12)) - lnlam)) : 0;
        const B = Math.exp(k * (Math.log(d.b[i]) - lnlam));
        ll += -A + Math.log(-Math.expm1(-(B - A)) + 1e-300);  // ln[e^-A - e^-B], computed stably
        break;
      }
      case 3: {
        const X = Math.exp(k * (Math.log(d.b[i]) - lnlam));
        const logInt = lnlam - lnk + lgk + Math.log(gammainc(1 / k, X) + 1e-300);
        ll += -Math.exp(k * (Math.log(a) - lnlam)) - logInt;
        break;
      }
      default:
    }
    if (d.e[i] > 0) ll += Math.exp(k * (Math.log(d.e[i]) - lnlam));  // left truncation
  }
  return ll;
}

function penalty(psi, nPlat, prior, platforms) {
  const [eta0, beta1, beta2] = psi;
  const lnk = psi[3 + nPlat];
  let th = 0;
  platforms.forEach((p, i) => { th += (psi[3 + i] - (prior.theta_mean[p] || 0)) ** 2; });
  return th / (2 * prior.tau ** 2)
    + (beta1 - prior.beta1) ** 2 / (2 * prior.beta1_sd ** 2)
    + (beta2 - prior.beta2) ** 2 / (2 * prior.beta2_sd ** 2)
    + (lnk - Math.log(prior.k)) ** 2 / (2 * prior.lnk_sd ** 2)
    + (eta0 - Math.log(prior.lambda0_days)) ** 2 / (2 * prior.eta0_sd ** 2);
}

function hessian(f, x, h = 1e-3) {
  const n = x.length;
  const H = Array.from({ length: n }, () => new Array(n).fill(0));
  const at = (di, dj, i, j) => {
    const y = x.slice();
    y[i] += di;
    y[j] += dj;
    return f(y);
  };
  for (let i = 0; i < n; i++) {
    for (let j = i; j < n; j++) {
      H[i][j] = H[j][i] = (at(h, h, i, j) - at(h, -h, i, j) - at(-h, h, i, j) + at(-h, -h, i, j)) / (4 * h * h);
    }
  }
  return H;
}

// ------------------------------------------------------------------ fitted model

export class FitResult {
  constructor(fields) {
    Object.assign(this, fields);
  }

  get k() { return Math.exp(this.psi.at(-1)); }
  get lambda0() { return Math.exp(this.psi[0]); }
  get beta1() { return this.psi[1]; }
  get beta2() { return this.psi[2]; }
  get theta() { return Object.fromEntries(this.platforms.map((p, i) => [p, this.psi[3 + i]])); }

  z(price, tier) { return (price - this.anchors[tier]) / this.sigma_p; }

  /** lambda in days for each price. platform null -> pooled: the average of the platforms' speed terms. */
  scale(prices, tier, platform = null) {
    const th = this.theta;
    const t = platform ? th[platform] : Object.values(th).reduce((s, v) => s + v, 0) / this.platforms.length;
    return prices.map((p) => Math.exp(this.psi[0] + this.beta1 * this.z(p, tier) - this.beta2 * (CIDX[tier] - C_REF) + t));
  }

  /** Delta-method SE of ln E[T] (section 7), for each price. */
  seLogMean(prices, tier, platform = null) {
    const n = this.psi.length, np_ = this.platforms.length, k = this.k;
    return prices.map((p) => {
      const g = new Array(n).fill(0);
      g[0] = 1;
      g[1] = this.z(p, tier);
      g[2] = -(CIDX[tier] - C_REF);
      if (platform) g[3 + this.platforms.indexOf(platform)] = 1;
      else for (let i = 0; i < np_; i++) g[3 + i] = 1 / np_;
      g[n - 1] = -digamma(1 + 1 / k) / k;
      let v = 0;
      for (let i = 0; i < n; i++) for (let j = 0; j < n; j++) v += g[i] * this.cov[i][j] * g[j];
      return Math.sqrt(Math.max(v, 0));
    });
  }

  meanDaysRange(prices, tier, platform = null, z90 = 1.645) {
    const lam = this.scale(prices, tier, platform);
    const se = this.seLogMean(prices, tier, platform);
    const mean = lam.map((l) => l * Math.exp(gammaln(1 + 1 / this.k)));
    return [mean, mean.map((m, i) => m * Math.exp(-z90 * se[i])), mean.map((m, i) => m * Math.exp(z90 * se[i]))];
  }
}

export const beta1Min = (anchors, sigmaP) => Math.max(BETA1_FLOOR, MIN_ELASTICITY * sigmaP / anchors.good);

/** Fit the penalised censored Weibull AFT model to ListingRow-shaped rows. */
export function fit(rows, { platforms = ['ebay', 'vinted'], prior = defaultPrior(), anchors = null, sigmaP = null } = {}) {
  anchors = anchors || estimateAnchors(rows);
  sigmaP = sigmaP || estimateSigma(rows, anchors);
  const d = prepare(rows, platforms, anchors, sigmaP);
  const nPlat = platforms.length;
  const objective = (psi) => -loglik(psi, d, nPlat) + penalty(psi, nPlat, prior, platforms);

  // start (section 9, step 3): eta0 from the median age of listings, priors elsewhere
  const ages = d.a.filter((a) => a > 0);
  const etaStart = ages.length ? Math.log(median(ages)) : Math.log(prior.lambda0_days);
  const x0 = [etaStart, prior.beta1, prior.beta2, ...platforms.map((p) => prior.theta_mean[p] || 0), Math.log(prior.k)];
  // Price must matter at least a little (elasticity >= 1 at the typical price); k kept sane too.
  const bMin = beta1Min(anchors, sigmaP);
  const bounds = [[null, null], [bMin, 8.0], [null, null], ...platforms.map(() => [null, null]), [Math.log(0.3), Math.log(3.0)]];
  x0[1] = Math.max(x0[1], bMin);
  const res = minimizeBounded(objective, x0, bounds);
  const H = hessian(objective, res.x);
  // parameters sitting on a bound are treated as fixed: the curvature there isn't a valid uncertainty
  const free = res.x.map((x, i) => !((bounds[i][0] != null && Math.abs(x - bounds[i][0]) < 1e-6)
    || (bounds[i][1] != null && Math.abs(x - bounds[i][1]) < 1e-6)));
  const idx = free.map((f, i) => (f ? i : -1)).filter((i) => i >= 0);
  const Hf = idx.map((i) => idx.map((j) => H[i][j]));
  let cf = invert(Hf);
  if (!cf || cf.some((row, i) => row[i] < 0)) {
    cf = pinvSym(Hf);
    cf = cf.map((row, i) => row.map((v, j) => (i === j ? Math.abs(v) : (v + cf[j][i]) / 2)));
  }
  const cov = res.x.map(() => new Array(res.x.length).fill(0));
  idx.forEach((i, a) => idx.forEach((j, b) => { cov[i][j] = cf[a][b]; }));
  const nSales = Object.fromEntries(platforms.map((p, i) =>
    [p, d.kind.filter((kk, r) => d.j[r] === i && (kk === 1 || kk === 2)).length]));
  const count = (kk) => d.kind.filter((x) => x === kk).length;
  return new FitResult({
    platforms, psi: res.x, cov, anchors, sigma_p: sigmaP,
    n_obs: d.kind.length, n_censored: count(0), n_interval: count(2), n_exact: count(1), n_sales: nSales,
    log_likelihood: loglik(res.x, d, nPlat),
    used_priors: Object.values(nSales).reduce((s, v) => s + v, 0) < PRIOR_MIN_N,
    converged: res.converged,
  });
}
