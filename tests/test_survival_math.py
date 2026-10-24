"""Model correctness: recover known parameters from simulated listings (maths doc, section 9).

The simulator observes listings the way the real clients do (eBay cohort window, Vinted
left-truncated with a re-check), so passing here means the likelihood handles our sampling.
"""
import numpy as np
import pytest

from backend.engine import survival_model as sm
from backend.engine.simulate import TrueParams, simulate_listings

# deliberately different from the priors (beta1 2.4, k 0.85, beta2 0.3), so the data must do the work
TRUTH = TrueParams(lambda0=6.0, beta1=1.8, beta2=0.5, k=1.1, sigma_p=12.0, theta={"ebay": 0.2, "vinted": -0.25})


@pytest.fixture(scope="module")
def big_fit():
    rows = simulate_listings(TRUTH, n_per_platform=2000, seed=3, vinted_recheck_hours=18.0)
    return sm.fit(rows, anchors=TRUTH.anchor, sigma_p=TRUTH.sigma_p)


def test_recovers_shape_and_price_effect(big_fit):
    se = np.sqrt(np.diag(big_fit.cov))
    for idx, true in ((1, TRUTH.beta1), (2, TRUTH.beta2), (-1, np.log(TRUTH.k))):
        assert abs(big_fit.psi[idx] - true) < 3.5 * se[idx], (idx, big_fit.psi[idx], true, se[idx])


@pytest.mark.parametrize("platform", ["ebay", "vinted"])
def test_expected_days_inside_confidence_range(big_fit, platform):
    for price in (28, 34, 42):
        mean, lo, hi = big_fit.mean_days_range(price, "good", platform)
        true = sm.expected_days(TRUTH.scale(price, "good", platform), TRUTH.k)
        assert lo[0] * 0.9 < true < hi[0] * 1.1, (platform, price, lo[0], true, hi[0])


def test_vinted_without_recheck_is_honestly_uncertain():
    rows = simulate_listings(TRUTH, n_per_platform=1000, seed=4, vinted_recheck_hours=None)
    f = sm.fit(rows, anchors=TRUTH.anchor, sigma_p=TRUTH.sigma_p)
    assert f.n_sales["vinted"] == 0
    _, lo, hi = f.mean_days_range(34, "good", "vinted")
    _, elo, ehi = f.mean_days_range(34, "good", "ebay")
    assert hi[0] / lo[0] > 3 * (ehi[0] / elo[0])  # far wider than eBay's data-backed range


def test_higher_price_means_longer_wait(big_fit):
    lam = big_fit.scale(np.array([20.0, 30.0, 40.0, 50.0]), "good", "ebay")
    assert np.all(np.diff(lam) > 0)


def test_thin_data_falls_back_to_priors():
    rows = simulate_listings(TRUTH, n_per_platform=6, seed=5)
    f = sm.fit(rows)
    assert f.used_priors and np.all(np.isfinite(f.psi)) and np.all(np.isfinite(f.cov))
    assert 0.3 < f.k < 3


def test_estimated_anchors_are_close_to_truth():
    rows = simulate_listings(TRUTH, n_per_platform=2000, seed=6)
    a = sm.estimate_anchors(rows)
    for tier, m in TRUTH.anchor.items():
        assert abs(a[tier] / m - 1) < 0.25, (tier, a[tier], m)  # sold prices sit a bit below asking
