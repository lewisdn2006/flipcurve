"""Censored Weibull AFT survival model: closed forms + penalised (MAP) fit.

Owner: Person 1. The maths is written up in the doc "FlipCurve: The Maths Behind Time-to-Sell"
(sections referenced below).

Model (section 4):
    z_i          = (p_i - M(c_i)) / sigma_p
    ln lambda_i  = eta0 + beta1 * z_i - beta2 * (c_i - 2) + theta_{platform(i)}
    S(t)         = exp(-(t / lambda)^k)
Likelihood (section 5): active -> ln S(t); sold_exact -> ln f(t); sold_interval -> ln[S(a) - S(b)];
every row first seen at age e > 0 also gets - ln S(e) (left truncation: it was only visible
because it survived that long).
Stock rows (a newest-first catalogue read back to age A, e.g. Vinted): listings arrive at a steady
rate and stay visible until they sell, so a visible listing's age has density S(t) / int_0^A S(u) du.
Young-heavy catalogues mean fast sales; evenly spread ages mean slow ones.
Penalties = normal priors (section 6), so thin data falls back smoothly to the priors.
Confidence (section 7): finite-difference Hessian -> covariance -> delta method on ln E[T].
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import minimize
from scipy.special import digamma, gammainc, gammaln
from scipy.special import gamma as gamma_fn

C_REF = 2  # "good"
TIERS = ["fair", "good", "very_good", "mint"]
CIDX = {t: i + 1 for i, t in enumerate(TIERS)}
# Typical price of each condition relative to Good; only used to fill a tier with too few sales.
DEFAULT_TIER_RATIO = {"fair": 0.65, "good": 1.0, "very_good": 1.32, "mint": 1.9}
MIN_SOLD_PER_ANCHOR = 5
PRIOR_MIN_N = 12          # fewer observed sales than this -> flagged as leaning on priors
T_MIN = 1e-3              # days; avoids ln(0) for listings seen the moment they went up
BETA1_MIN = 0.75

# ------------------------------------------------------------------ closed forms (section 3)


def scale(p, anchor, sigma_p, c, lambda0, beta1, beta2, c_ref=C_REF):
    """Weibull scale lambda(p, c) in days, with theta = 0. Vectorised over p."""
    z = (np.asarray(p, dtype=float) - anchor) / sigma_p
    return lambda0 * np.exp(beta1 * z - beta2 * (c - c_ref))


def survival(t, lam, k):
    """P(T > t)."""
    return np.exp(-np.power(np.asarray(t, dtype=float) / lam, k))


def cdf(t, lam, k):
    """P(T <= t): probability of selling within t days."""
    return 1.0 - survival(t, lam, k)


def expected_days(lam, k):
    return lam * gamma_fn(1.0 + 1.0 / k)


def quantile_days(q, lam, k):
    """q-th quantile of time-to-sell, e.g. q=0.5 median, 0.05/0.95 for the 90% range."""
    return lam * np.power(-np.log1p(-q), 1.0 / k)


def discounted_sale_value(lam, k, gamma_per_day, horizon_days=180.0, n=2000):
    """E[exp(-gamma T)] for T ~ Weibull(lam, k), by quadrature on [0, horizon] (section 8).
    Mass beyond the horizon is treated as never selling (value 0)."""
    lam = np.atleast_1d(np.asarray(lam, dtype=float))
    t = np.linspace(0.0, horizon_days, n)[:, None]
    F = cdf(t, lam[None, :], k)
    dF = np.diff(F, axis=0)
    t_mid = 0.5 * (t[1:] + t[:-1])
    return (np.exp(-gamma_per_day * t_mid) * dF).sum(axis=0)


# ------------------------------------------------------------------ priors (section 6)


@dataclass
class Prior:
    beta1: float = 2.4
    beta1_sd: float = 1.0
    k: float = 0.85
    lnk_sd: float = 0.3
    beta2: float = 0.3
    beta2_sd: float = 0.5
    lambda0_days: float = 7.0     # weak: keeps eta0 finite when nothing has sold yet
    eta0_sd: float = 1.5
    tau: float = 0.5              # sd of platform speed terms
    theta_mean: dict = field(default_factory=dict)  # mu_j; missing platforms -> 0


# ------------------------------------------------------------------ data prep (section 9, step 1)


def _get(r, key):
    return r[key] if isinstance(r, dict) else getattr(r, key)


def _tier(r) -> str:
    t = _get(r, "condition_tier")
    t = getattr(t, "value", t)
    return t if t in CIDX else "good"


def estimate_anchors(rows) -> dict[str, float]:
    """M(c): median sold price per condition; thin tiers filled from the others by typical ratios."""
    sold = [r for r in rows if _get(r, "obs_type") != "active" and not _get(r, "is_outlier")]
    by_tier = {t: [float(_get(r, "price_gbp")) for r in sold if _tier(r) == t] for t in TIERS}
    good = {t: float(np.median(v)) for t, v in by_tier.items() if len(v) >= MIN_SOLD_PER_ANCHOR}
    if not good and len(sold) >= 3:  # a few sales spread over tiers: pool them via the typical ratios
        ref = float(np.median([float(_get(r, "price_gbp")) / DEFAULT_TIER_RATIO[_tier(r)] for r in sold]))
        return {t: round(ref * DEFAULT_TIER_RATIO[t], 2) for t in TIERS}
    if not good:  # (almost) nothing sold: use every listing, scaled down a little for survivorship
        prices = [float(_get(r, "price_gbp")) for r in rows if not _get(r, "is_outlier")]
        base = 0.85 * float(np.median(prices)) if prices else 30.0
        return {t: round(base * DEFAULT_TIER_RATIO[t], 2) for t in TIERS}
    # pooled 'Good' level from every sale (condition-adjusted); a tier's own median is used only
    # within 0.7-1.5x of what the pooled level implies (a few pricey collabs made "mint" £157
    # for £30 trainers, 25 Oct)
    ref = float(np.median([float(_get(r, "price_gbp")) / DEFAULT_TIER_RATIO[_tier(r)] for r in sold]))
    out = {}
    for t in TIERS:
        implied = ref * DEFAULT_TIER_RATIO[t]
        out[t] = round(float(np.clip(good.get(t, implied), 0.7 * implied, 1.5 * implied)), 2)
    return out


def estimate_sigma(rows, anchors) -> float:
    """sigma_p: robust spread of sold prices around their condition's anchor, measured relative to
    the anchor (median absolute deviation x 1.4826) and expressed in 'Good' pounds. A few pricey
    special editions used to inflate the IQR-based spread to ~£30 on £30 trainers, which made price
    look irrelevant to speed (25 Oct)."""
    sold = [r for r in rows if _get(r, "obs_type") != "active" and not _get(r, "is_outlier")]
    rel = np.array([float(_get(r, "price_gbp")) / anchors[_tier(r)] - 1.0 for r in sold])
    if len(rel) < 4:
        return 0.3 * anchors["good"]
    mad = 1.4826 * float(np.median(np.abs(rel - np.median(rel))))
    return float(np.clip(mad, 0.1, 0.6) * anchors["good"])


@dataclass
class _Data:
    z: np.ndarray
    cdev: np.ndarray
    j: np.ndarray
    kind: np.ndarray   # 0 active, 1 exact, 2 interval, 3 stock sample (b = window A)
    a: np.ndarray
    b: np.ndarray
    e: np.ndarray      # entry age (left truncation), 0 = seen from the start


def _prepare(rows, platforms, anchors, sigma_p) -> _Data:
    pidx = {p: i for i, p in enumerate(platforms)}
    z, cdev, j, kind, a, b, e = [], [], [], [], [], [], []
    for r in rows:
        if _get(r, "is_outlier"):
            continue
        ot = getattr(_get(r, "obs_type"), "value", _get(r, "obs_type"))
        pl = getattr(_get(r, "platform"), "value", _get(r, "platform"))
        if pl not in pidx or ot == "sold_unknown_time":
            continue
        t1, t2 = _get(r, "t_days"), _get(r, "t_upper_days")
        t0 = float(_get(r, "t_entry_days") or 0.0) if (isinstance(r, dict) and "t_entry_days" in r) or hasattr(r, "t_entry_days") else 0.0
        win = r.get("stock_window_days") if isinstance(r, dict) else getattr(r, "stock_window_days", None)
        if ot == "active" and t1 is not None and win:
            if float(t1) > float(win):
                continue  # older than the window we read back to (e.g. a bumped listing): no information
            k_, aa, bb, t0 = 3, max(float(t1), T_MIN), float(win), 0.0
        elif ot == "active" and t1 is not None:
            k_, aa, bb = 0, max(float(t1), 0.0), np.nan
        elif ot == "sold_exact" and t1 is not None:
            k_, aa, bb = 1, max(float(t1), T_MIN), np.nan
        elif ot == "sold_interval" and t1 is not None and t2 is not None:
            aa, bb = max(float(t1), 0.0), float(t2)
            if bb - aa < 1e-6:  # degenerate interval: treat as an exact sale
                k_, aa, bb = 1, max(aa, T_MIN), np.nan
            else:
                k_ = 2
        else:
            continue
        tier = _tier(r)
        z.append((float(_get(r, "price_gbp")) - anchors[tier]) / sigma_p)
        cdev.append(CIDX[tier] - C_REF)
        j.append(pidx[pl])
        kind.append(k_)
        a.append(aa)
        b.append(bb)
        e.append(min(t0, aa))
    return _Data(*(np.asarray(x, dtype=float if n != 2 and n != 3 else int)
                   for n, x in enumerate([z, cdev, j, kind, a, b, e])))


# ------------------------------------------------------------------ likelihood (section 5)


def _loglik(psi, d: _Data, n_plat: int) -> float:
    eta0, beta1, beta2 = psi[0], psi[1], psi[2]
    theta = psi[3:3 + n_plat]
    k = np.exp(psi[3 + n_plat])
    lnlam = eta0 + beta1 * d.z - beta2 * d.cdev + theta[d.j]
    ll = 0.0
    m = d.kind == 0
    if m.any():
        t = d.a[m]
        pos = t > 0
        ll -= np.exp(k * (np.log(t[pos]) - lnlam[m][pos])).sum()
    m = d.kind == 1
    if m.any():
        lu = np.log(d.a[m]) - lnlam[m]
        ll += (np.log(k) - lnlam[m] + (k - 1) * lu - np.exp(k * lu)).sum()
    m = d.kind == 2
    if m.any():
        aa, bb, ll_i = d.a[m], d.b[m], lnlam[m]
        A = np.where(aa > 0, np.exp(k * (np.log(np.maximum(aa, 1e-12)) - ll_i)), 0.0)
        B = np.exp(k * (np.log(bb) - ll_i))
        # ln[e^-A - e^-B] = -A + ln(1 - e^-(B-A)), computed stably
        ll += (-A + np.log(-np.expm1(-(B - A)) + 1e-300)).sum()
    m = d.kind == 3
    if m.any():
        t, A, ll_i = d.a[m], d.b[m], lnlam[m]
        X = np.exp(k * (np.log(A) - ll_i))
        log_int = ll_i - np.log(k) + gammaln(1.0 / k) + np.log(gammainc(1.0 / k, X) + 1e-300)
        ll += (-np.exp(k * (np.log(t) - ll_i)) - log_int).sum()
    m = d.e > 0  # left truncation: we only saw it because it survived to age e
    if m.any():
        ll += np.exp(k * (np.log(d.e[m]) - lnlam[m])).sum()
    return float(ll)


def _penalty(psi, n_plat: int, prior: Prior, platforms) -> float:
    eta0, beta1, beta2 = psi[0], psi[1], psi[2]
    theta = psi[3:3 + n_plat]
    lnk = psi[3 + n_plat]
    mu = np.array([prior.theta_mean.get(p, 0.0) for p in platforms])
    return float(
        ((theta - mu) ** 2).sum() / (2 * prior.tau ** 2)
        + (beta1 - prior.beta1) ** 2 / (2 * prior.beta1_sd ** 2)
        + (beta2 - prior.beta2) ** 2 / (2 * prior.beta2_sd ** 2)
        + (lnk - np.log(prior.k)) ** 2 / (2 * prior.lnk_sd ** 2)
        + (eta0 - np.log(prior.lambda0_days)) ** 2 / (2 * prior.eta0_sd ** 2)
    )


def _hessian(f, x, h=1e-3):
    n = len(x)
    H = np.zeros((n, n))
    for i in range(n):
        for j in range(i, n):
            ei, ej = np.eye(n)[i] * h, np.eye(n)[j] * h
            H[i, j] = H[j, i] = (f(x + ei + ej) - f(x + ei - ej) - f(x - ei + ej) + f(x - ei - ej)) / (4 * h * h)
    return H


# ------------------------------------------------------------------ fitted model


@dataclass
class FitResult:
    platforms: list[str]
    psi: np.ndarray
    cov: np.ndarray
    anchors: dict[str, float]
    sigma_p: float
    n_obs: int
    n_censored: int
    n_interval: int
    n_exact: int
    n_sales: dict[str, int]
    log_likelihood: float
    used_priors: bool
    converged: bool

    @property
    def k(self) -> float:
        return float(np.exp(self.psi[-1]))

    @property
    def lambda0(self) -> float:
        return float(np.exp(self.psi[0]))

    @property
    def beta1(self) -> float:
        return float(self.psi[1])

    @property
    def beta2(self) -> float:
        return float(self.psi[2])

    @property
    def theta(self) -> dict[str, float]:
        return {p: float(self.psi[3 + i]) for i, p in enumerate(self.platforms)}

    def z(self, price, tier):
        return (np.asarray(price, float) - self.anchors[tier]) / self.sigma_p

    def scale(self, price, tier, platform: str | None = None):
        """lambda in days. platform=None -> pooled: the average of the platforms' speed terms."""
        th = self.theta[platform] if platform else float(np.mean(list(self.theta.values())))
        return np.exp(self.psi[0] + self.beta1 * self.z(price, tier) - self.beta2 * (CIDX[tier] - C_REF) + th)

    def se_log_mean(self, price, tier, platform: str | None = None) -> np.ndarray:
        """Delta-method SE of ln E[T] (section 7). Vectorised over price."""
        z = np.atleast_1d(self.z(price, tier))
        g = np.zeros((len(z), len(self.psi)))
        g[:, 0] = 1.0
        g[:, 1] = z
        g[:, 2] = -(CIDX[tier] - C_REF)
        if platform:
            g[:, 3 + self.platforms.index(platform)] = 1.0
        else:
            g[:, 3:3 + len(self.platforms)] = 1.0 / len(self.platforms)
        k = self.k
        g[:, -1] = -digamma(1 + 1 / k) / k
        return np.sqrt(np.maximum(np.einsum("ni,ij,nj->n", g, self.cov, g), 0.0))

    def mean_days_range(self, price, tier, platform=None, z90=1.645):
        lam = np.atleast_1d(self.scale(price, tier, platform))
        mean = lam * np.exp(gammaln(1 + 1 / self.k))
        se = self.se_log_mean(price, tier, platform)
        return mean, mean * np.exp(-z90 * se), mean * np.exp(z90 * se)


def fit(rows, platforms=("ebay", "vinted"), prior: Prior | None = None,
        anchors: dict | None = None, sigma_p: float | None = None) -> FitResult:
    """Fit the penalised censored Weibull AFT model to ListingRow-shaped rows."""
    prior = prior or Prior()
    platforms = list(platforms)
    anchors = anchors or estimate_anchors(rows)
    sigma_p = sigma_p or estimate_sigma(rows, anchors)
    d = _prepare(rows, platforms, anchors, sigma_p)
    n_plat = len(platforms)

    def objective(psi):
        return -_loglik(psi, d, n_plat) + _penalty(psi, n_plat, prior, platforms)

    # start (section 9, step 3): eta0 from the median age of listings, priors elsewhere
    ages = d.a[d.a > 0]
    eta_start = np.log(np.median(ages)) if len(ages) else np.log(prior.lambda0_days)
    x0 = np.concatenate([[eta_start, prior.beta1, prior.beta2],
                         [prior.theta_mean.get(p, 0.0) for p in platforms], [np.log(prior.k)]])
    # beta1 >= 0.75: a listing priced one spread above the market sells ~2x slower at least. With
    # mixed listings the data can make price look almost irrelevant, and then every "best price"
    # ran to the top of the chart (25 Oct). k kept in a sane range too.
    bounds = [(None, None), (BETA1_MIN, 8.0), (None, None)] + [(None, None)] * n_plat + [(np.log(0.3), np.log(3.0))]
    x0[1] = max(x0[1], BETA1_MIN)
    res = minimize(objective, x0, method="L-BFGS-B", bounds=bounds)
    H = _hessian(objective, res.x)
    # parameters sitting on a bound are treated as fixed: the curvature there isn't a valid
    # uncertainty (it made every range 0-365 days when beta1 hit its floor, 25 Oct)
    free = np.array([not ((lo is not None and abs(x - lo) < 1e-6) or (hi is not None and abs(x - hi) < 1e-6))
                     for x, (lo, hi) in zip(res.x, bounds)])
    cov = np.zeros_like(H)
    Hf = H[np.ix_(free, free)]
    try:
        cf = np.linalg.inv(Hf)
        if np.any(np.diag(cf) < 0):
            raise np.linalg.LinAlgError
    except np.linalg.LinAlgError:
        cf = np.linalg.pinv(Hf)
        cf = (cf + cf.T) / 2
        np.fill_diagonal(cf, np.abs(np.diag(cf)))
    cov[np.ix_(free, free)] = cf
    sales_mask = (d.kind == 1) | (d.kind == 2)
    n_sales = {p: int(((d.j == i) & sales_mask).sum()) for i, p in enumerate(platforms)}
    return FitResult(
        platforms=platforms, psi=res.x, cov=cov, anchors=anchors, sigma_p=sigma_p,
        n_obs=len(d.kind), n_censored=int((d.kind == 0).sum()), n_interval=int((d.kind == 2).sum()),
        n_exact=int((d.kind == 1).sum()), n_sales=n_sales, log_likelihood=_loglik(res.x, d, n_plat),
        used_priors=sum(n_sales.values()) < PRIOR_MIN_N, converged=bool(res.success),
    )
