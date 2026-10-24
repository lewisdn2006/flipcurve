"""Censored Weibull AFT survival model.

Owner: Person 1. See DECISIONS.md and contract/API_CONTRACT.md before changing the contract.

Model (from the backbone plan, section 3C):
    z            = (p - M(c)) / sigma_p
    lambda(p, c) = lambda0 * exp(beta1 * z - beta2 * (c - c_ref))
    S(t | p, c)  = exp(-(t / lambda)^k)
    E[T]         = lambda * Gamma(1 + 1/k)
    T_q          = lambda * (-ln(1 - q))^(1/k)

This file currently holds only the closed-form pieces (used by scripts/make_mock.py so the
mock payload obeys the same maths). Phase 2 adds: censored log-likelihood (exact, right- and
interval-censored), SciPy fit, prior shrinkage for N < 12, and the grid builder.
"""
from __future__ import annotations

import numpy as np
from scipy.special import gamma as gamma_fn

C_REF = 2  # "good"

# Category priors from the plan; used when fewer than PRIOR_MIN_N clean listings.
PRIOR_K = 0.85
PRIOR_BETA1 = 2.4
PRIOR_MIN_N = 12


def scale(p, anchor, sigma_p, c, lambda0, beta1, beta2, c_ref=C_REF):
    """Weibull scale lambda(p, c) in days. Vectorised over p."""
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
    """E[exp(-gamma T)] for T ~ Weibull(lam, k), by quadrature on [0, horizon].

    Used for p*(gamma) = argmax_p  net(p) * E[exp(-gamma T) | p].
    Mass beyond the horizon is treated as never selling (value 0).
    """
    lam = np.atleast_1d(np.asarray(lam, dtype=float))
    t = np.linspace(0.0, horizon_days, n)[:, None]
    # integrate e^{-gamma t} dF(t) = sum e^{-gamma t_mid} * (F(t_i+1) - F(t_i))
    F = cdf(t, lam[None, :], k)
    dF = np.diff(F, axis=0)
    t_mid = 0.5 * (t[1:] + t[:-1])
    return (np.exp(-gamma_per_day * t_mid) * dF).sum(axis=0)
