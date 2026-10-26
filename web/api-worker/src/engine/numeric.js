// The numpy / scipy pieces the engine uses: special functions, medians and percentiles, a bounded
// minimiser (in place of scipy's L-BFGS-B) and small symmetric-matrix inverses. Checked against
// scipy in test/node/numeric.test.js (tests/golden/maths.json).

// ---- special functions

const LANCZOS = [0.99999999999980993, 676.5203681218851, -1259.1392167224028, 771.32342877765313,
  -176.61502916214059, 12.507343278686905, -0.13857109526572012, 9.9843695780195716e-6, 1.5056327351493116e-7];

/** ln Gamma(x), x > 0 (Lanczos, g = 7). */
export function gammaln(x) {
  if (x < 0.5) return Math.log(Math.PI / Math.abs(Math.sin(Math.PI * x))) - gammaln(1 - x);
  x -= 1;
  let a = LANCZOS[0];
  const t = x + 7.5;
  for (let i = 1; i < 9; i++) a += LANCZOS[i] / (x + i);
  return 0.5 * Math.log(2 * Math.PI) + (x + 0.5) * Math.log(t) - t + Math.log(a);
}

export const gamma = (x) => Math.exp(gammaln(x));

/** psi(x) = d/dx ln Gamma(x), x > 0. */
export function digamma(x) {
  let r = 0;
  while (x < 6) { r -= 1 / x; x += 1; }
  const f = 1 / (x * x);
  return r + Math.log(x) - 0.5 / x
    - f * (1 / 12 - f * (1 / 120 - f * (1 / 252 - f * (1 / 240 - f * (1 / 132 - f * (691 / 32760 - f / 12))))));
}

/** Regularised lower incomplete gamma P(a, x) (scipy.special.gammainc). */
export function gammainc(a, x) {
  if (x <= 0) return 0;
  if (!Number.isFinite(x)) return 1;
  const lnPre = a * Math.log(x) - x - gammaln(a);
  if (x < a + 1) {  // series
    let ap = a, del = 1 / a, sum = del;
    for (let n = 0; n < 10000; n++) {
      ap += 1;
      del *= x / ap;
      sum += del;
      if (Math.abs(del) < Math.abs(sum) * 1e-16) break;
    }
    return Math.min(1, sum * Math.exp(lnPre));
  }
  // continued fraction for Q(a, x) (modified Lentz)
  const tiny = 1e-300;
  let b = x + 1 - a, c = 1 / tiny, d = 1 / b, h = d;
  for (let i = 1; i < 10000; i++) {
    const an = -i * (i - a);
    b += 2;
    d = an * d + b; if (Math.abs(d) < tiny) d = tiny;
    c = b + an / c; if (Math.abs(c) < tiny) c = tiny;
    d = 1 / d;
    const del = d * c;
    h *= del;
    if (Math.abs(del - 1) < 1e-16) break;
  }
  return Math.max(0, 1 - Math.exp(lnPre) * h);
}

// ---- numpy-style statistics

export function median(xs) {
  if (!xs.length) return NaN;
  const s = Float64Array.from(xs).sort();
  const m = s.length >> 1;
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

/** np.percentile with the default linear interpolation; q in 0-100. */
export function percentile(xs, q) {
  const s = Float64Array.from(xs).sort();
  const pos = (s.length - 1) * q / 100;
  const lo = Math.floor(pos), hi = Math.ceil(pos);
  return s[lo] + (s[hi] - s[lo]) * (pos - lo);
}

export function linspace(a, b, n) {
  if (n === 1) return [a];
  const step = (b - a) / (n - 1);
  return Array.from({ length: n }, (_, i) => (i === n - 1 ? b : a + i * step));
}

export const clip = (x, lo, hi) => Math.min(Math.max(x, lo), hi);

/** Python's round(x, nd): rounds the exact value of the double (47.775 is stored as 47.77499..., so
 *  47.77), with exact ties going to the even digit. toFixed also rounds the exact value, but sends
 *  ties away from zero, so ties are checked separately on the full decimal expansion. */
export function pyRound(x, nd = 0) {
  if (!Number.isFinite(x) || Math.abs(x) >= 1e21) return x;
  const exact = Math.abs(x).toFixed(100);
  const dot = exact.indexOf('.');
  const tail = exact.slice(dot + 1 + nd);
  if (tail[0] === '5' && /^50*$/.test(tail)) {  // an exact tie: half to even
    const kept = exact.slice(0, dot + 1 + nd).replace('.', '');
    const down = Number(kept.slice(0, -1) + kept.at(-1)) / 10 ** nd;
    const last = Number(kept.at(-1));
    const r = last % 2 === 0 ? down : down + 10 ** -nd;
    return Math.sign(x) * Number(r.toFixed(nd));
  }
  return Number(x.toFixed(nd));
}

/** numpy's np.round(x, nd): scales, then rounds half to even (np.rint) on the scaled double. */
export function npRound(x, nd = 0) {
  const m = 10 ** nd;
  const y = x * m;
  const r = Math.abs(y % 1) === 0.5 ? 2 * Math.round(y / 2) : Math.round(y);
  return r / m;
}

// ---- bounded minimiser

/**
 * Minimise f over a box (bounds: [[lo|null, hi|null], ...]) with projected L-BFGS and numerical
 * gradients: what scipy.optimize.minimize(method="L-BFGS-B") does for the model fit, run to a
 * tighter tolerance so the answer depends on the problem, not on where either code stopped.
 */
export function minimizeBounded(f, x0, bounds, { maxIter = 1000, gtol = 1e-9, ftol = 1e-15, memory = 10 } = {}) {
  const n = x0.length;
  const lo = bounds.map((b) => (b[0] == null ? -Infinity : b[0]));
  const hi = bounds.map((b) => (b[1] == null ? Infinity : b[1]));
  const project = (x) => x.map((v, i) => clip(v, lo[i], hi[i]));
  const grad = (x) => x.map((v, i) => {
    const h = 1e-6 * Math.max(1, Math.abs(v));
    const up = Math.min(v + h, hi[i]), dn = Math.max(v - h, lo[i]);
    const xu = x.slice(), xd = x.slice();
    xu[i] = up; xd[i] = dn;
    return (f(xu) - f(xd)) / (up - dn);
  });
  const dot = (a, b) => a.reduce((s, v, i) => s + v * b[i], 0);
  let x = project(x0), fx = f(x), g = grad(x);
  const S = [], Y = [];
  let converged = false, iter = 0;
  for (; iter < maxIter; iter++) {
    const fixed = x.map((v, i) => (v <= lo[i] && g[i] > 0) || (v >= hi[i] && g[i] < 0));
    const pg = g.map((v, i) => (fixed[i] ? 0 : v));
    if (Math.max(...pg.map(Math.abs)) < gtol) { converged = true; break; }
    // two-loop recursion on the free variables
    const q = pg.slice(), alpha = [];
    for (let k = S.length - 1; k >= 0; k--) {
      const a = dot(S[k], q) / dot(Y[k], S[k]);
      alpha[k] = a;
      for (let i = 0; i < n; i++) q[i] -= a * Y[k][i];
    }
    const gammaK = S.length ? dot(S.at(-1), Y.at(-1)) / dot(Y.at(-1), Y.at(-1)) : 1 / Math.max(1, Math.sqrt(dot(pg, pg)));
    let d = q.map((v) => v * gammaK);
    for (let k = 0; k < S.length; k++) {
      const b = dot(Y[k], d) / dot(Y[k], S[k]);
      for (let i = 0; i < n; i++) d[i] += S[k][i] * (alpha[k] - b);
    }
    d = d.map((v, i) => (fixed[i] ? 0 : -v));
    if (dot(d, pg) >= 0) { d = pg.map((v) => -v); S.length = 0; Y.length = 0; }
    // backtracking line search along the projected path
    let t = 1, xn, fn;
    for (;;) {
      xn = project(x.map((v, i) => v + t * d[i]));
      fn = f(xn);
      if (fn <= fx + 1e-4 * dot(g, xn.map((v, i) => v - x[i]))) break;
      t *= 0.5;
      if (t < 1e-20) { xn = x; fn = fx; break; }
    }
    if (xn === x) { converged = true; break; }  // no further progress possible
    const gn = grad(xn);
    const s = xn.map((v, i) => v - x[i]), y = gn.map((v, i) => v - g[i]);
    if (dot(s, y) > 1e-12 * dot(y, y)) {
      S.push(s); Y.push(y);
      if (S.length > memory) { S.shift(); Y.shift(); }
    }
    const drop = fx - fn;
    x = xn; fx = fn; g = gn;
    if (drop <= ftol * Math.max(Math.abs(fx), Math.abs(fn), 1)) { converged = true; break; }
  }
  return { x, fun: fx, converged, iterations: iter };
}

// ---- small symmetric matrices (n <= ~10)

/** Inverse by Gauss-Jordan with partial pivoting; null if singular (np.linalg.inv raises). */
export function invert(A) {
  const n = A.length;
  const M = A.map((row, i) => [...row, ...Array.from({ length: n }, (_, j) => (i === j ? 1 : 0))]);
  for (let c = 0; c < n; c++) {
    let p = c;
    for (let r = c + 1; r < n; r++) if (Math.abs(M[r][c]) > Math.abs(M[p][c])) p = r;
    if (M[p][c] === 0 || !Number.isFinite(M[p][c])) return null;
    [M[c], M[p]] = [M[p], M[c]];
    const piv = M[c][c];
    for (let j = 0; j < 2 * n; j++) M[c][j] /= piv;
    for (let r = 0; r < n; r++) {
      if (r === c || M[r][c] === 0) continue;
      const fct = M[r][c];
      for (let j = 0; j < 2 * n; j++) M[r][j] -= fct * M[c][j];
    }
  }
  return M.map((row) => row.slice(n));
}

/** Eigen-decomposition of a symmetric matrix (Jacobi): {values, vectors (columns)}. */
function eigSym(A) {
  const n = A.length;
  const a = A.map((r) => r.slice());
  const v = Array.from({ length: n }, (_, i) => Array.from({ length: n }, (_, j) => (i === j ? 1 : 0)));
  for (let sweep = 0; sweep < 100; sweep++) {
    let off = 0;
    for (let i = 0; i < n; i++) for (let j = i + 1; j < n; j++) off += a[i][j] ** 2;
    if (off < 1e-30) break;
    for (let p = 0; p < n; p++) {
      for (let q = p + 1; q < n; q++) {
        if (Math.abs(a[p][q]) < 1e-300) continue;
        const theta = (a[q][q] - a[p][p]) / (2 * a[p][q]);
        const t = Math.sign(theta || 1) / (Math.abs(theta) + Math.sqrt(theta * theta + 1));
        const c = 1 / Math.sqrt(t * t + 1), s = t * c;
        for (let k = 0; k < n; k++) {
          const akp = a[k][p], akq = a[k][q];
          a[k][p] = c * akp - s * akq; a[k][q] = s * akp + c * akq;
        }
        for (let k = 0; k < n; k++) {
          const apk = a[p][k], aqk = a[q][k];
          a[p][k] = c * apk - s * aqk; a[q][k] = s * apk + c * aqk;
        }
        for (let k = 0; k < n; k++) {
          const vkp = v[k][p], vkq = v[k][q];
          v[k][p] = c * vkp - s * vkq; v[k][q] = s * vkp + c * vkq;
        }
      }
    }
  }
  return { values: a.map((r, i) => r[i]), vectors: v };
}

/** Moore-Penrose pseudo-inverse of a symmetric matrix (np.linalg.pinv, rcond 1e-15). */
export function pinvSym(A) {
  const n = A.length;
  const { values, vectors } = eigSym(A);
  const cut = 1e-15 * Math.max(...values.map(Math.abs));
  const out = Array.from({ length: n }, () => new Array(n).fill(0));
  values.forEach((lam, k) => {
    if (Math.abs(lam) <= cut) return;
    for (let i = 0; i < n; i++) for (let j = 0; j < n; j++) out[i][j] += vectors[i][k] * vectors[j][k] / lam;
  });
  return out;
}
