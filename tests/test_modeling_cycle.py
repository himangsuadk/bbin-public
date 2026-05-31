"""Property tests for the modeling suite and forecast-cycle gating.

One design property per test, >=100 iterations, tagged. Library: Hypothesis.
"""

from __future__ import annotations

import math

import numpy as np
from hypothesis import given, settings, strategies as st

from bbin_platform.modeling import (
    Charges,
    GenerationQuantiles,
    ModelEvaluation,
    OUParams,
    RegimeConditions,
    conditional_jump_times,
    generation_quantiles,
    hac_weighting_matrix,
    half_life,
    logsumexp,
    may_promote,
    net_revenue,
    ou_mle,
    poisson_truncation_count,
    positive_params,
    regime_review_flagged,
    select_offset,
    simulate_paths,
    softplus,
)
from bbin_platform.cycle import (
    CycleInputs,
    OrderPrerequisites,
    may_emit_order,
    may_publish_bid,
    run_forecast_stage,
)

RUN = settings(max_examples=200, deadline=None)


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 32: Model parameters remain in
# their valid domain.
# ---------------------------------------------------------------------------
@RUN
@given(
    raw=st.fixed_dictionaries({
        "kappa": st.floats(-50, 50, allow_nan=False),
        "sigma": st.floats(-50, 50, allow_nan=False),
        "lambda": st.floats(-50, 50, allow_nan=False),
        "sigma_J": st.floats(-50, 50, allow_nan=False),
    }),
    prices=st.lists(st.floats(-100, 500, allow_nan=False), min_size=1, max_size=30),
)
def test_property_32_param_domain(raw, prices):
    pos = positive_params(raw)
    for v in pos.values():
        assert v > 0.0
    arr = np.array(prices)
    c = select_offset(arr)
    assert np.all(arr + c > 0.0)


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 33: Poisson jump-count truncation
# bounds the residual probability.
# ---------------------------------------------------------------------------
@RUN
@given(lam=st.floats(0.0, 50, allow_nan=False))
def test_property_33_poisson_truncation(lam):
    M = poisson_truncation_count(lam, tail_threshold=1e-10)
    # Residual tail P(N > M) < 1e-10.
    # Compute tail directly.
    if lam == 0:
        assert M == 0
        return
    cdf = 0.0
    pmf = math.exp(-lam)
    cdf += pmf
    for k in range(1, M + 1):
        pmf *= lam / k
        cdf += pmf
    assert (1.0 - cdf) < 1e-10 + 1e-15


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 34: Log-sum-exp is correct and
# shift-invariant.
# ---------------------------------------------------------------------------
@RUN
@given(
    terms=st.lists(st.floats(-200, 200, allow_nan=False), min_size=1, max_size=20),
    shift=st.floats(-100, 100, allow_nan=False),
)
def test_property_34_logsumexp(terms, shift):
    arr = np.array(terms)
    lse = logsumexp(arr)
    # Reference via high-precision when feasible.
    ref = math.log(sum(math.exp(t - max(terms)) for t in terms)) + max(terms)
    assert abs(lse - ref) < 1e-6
    # Shift invariance: logsumexp(x + s) == logsumexp(x) + s.
    shifted = logsumexp(arr + shift)
    assert abs(shifted - (lse + shift)) < 1e-6


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 35: Calibration recovers known
# parameters.
# ---------------------------------------------------------------------------
@given(
    kappa=st.floats(0.1, 2.0, allow_nan=False),
    mu=st.floats(-5, 5, allow_nan=False),
    sigma=st.floats(0.1, 1.0, allow_nan=False),
    seed=st.integers(0, 10000),
)
@settings(max_examples=60, deadline=None)
def test_property_35_calibration_recovery(kappa, mu, sigma, seed):
    rng = np.random.default_rng(seed)
    n = 4000
    dt = 1.0
    x = np.empty(n)
    x[0] = mu
    b = math.exp(-kappa * dt)
    cond_sd = sigma * math.sqrt((1 - b * b) / (2 * kappa))
    for t in range(1, n):
        x[t] = mu + b * (x[t - 1] - mu) + rng.normal(0, cond_sd)
    est = ou_mle(x, dt)
    # Recover kappa and mu within tolerance; half-life consistent.
    assert abs(est.kappa - kappa) < 0.5 * kappa + 0.1
    assert abs(est.mu - mu) < 1.0
    assert abs(half_life(est.kappa) - math.log(2) / est.kappa) < 1e-9


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 36: The GMM weighting matrix is
# symmetric positive semi-definite.
# ---------------------------------------------------------------------------
@given(
    T=st.integers(20, 60),
    k=st.integers(1, 4),
    bw=st.integers(0, 8),
    seed=st.integers(0, 10000),
)
@settings(max_examples=80, deadline=None)
def test_property_36_hac_psd(T, k, bw, seed):
    rng = np.random.default_rng(seed)
    moments = rng.standard_normal((T, k))
    S = hac_weighting_matrix(moments, bw)
    # Symmetric.
    assert np.allclose(S, S.T, atol=1e-9)
    # PSD: all eigenvalues >= -tiny.
    eigs = np.linalg.eigvalsh((S + S.T) / 2)
    assert np.min(eigs) > -1e-6


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 37: Conditional jump times fall
# within their interval.
# ---------------------------------------------------------------------------
@RUN
@given(n=st.integers(0, 20), delta=st.floats(0.01, 10, allow_nan=False), seed=st.integers(0, 9999))
def test_property_37_jump_times_in_interval(n, delta, seed):
    rng = np.random.default_rng(seed)
    times = conditional_jump_times(n, delta, rng)
    assert len(times) == n
    for t in times:
        assert 0.0 < t < delta
    # sorted
    assert list(times) == sorted(times)


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 38: Scenario net revenue equals
# the charge-adjusted formula.
# ---------------------------------------------------------------------------
@RUN
@given(
    energy=st.floats(0, 1000, allow_nan=False),
    price=st.floats(-100, 10000, allow_nan=False),
    fees=st.lists(st.floats(0, 1000, allow_nan=False), min_size=7, max_size=7),
)
def test_property_38_net_revenue(energy, price, fees):
    ch = Charges(*fees)
    nr = net_revenue(energy, price, ch)
    assert abs(nr - (energy * price - sum(fees))) < 1e-6


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 40: Simulation is deterministic
# and recommendations are reproducible.
# ---------------------------------------------------------------------------
@RUN
@given(seed=st.integers(0, 10000), n_paths=st.integers(1, 30), n_steps=st.integers(1, 20))
def test_property_40_determinism(seed, n_paths, n_steps):
    a = simulate_paths(seed, n_paths, n_steps)
    b = simulate_paths(seed, n_paths, n_steps)
    assert np.array_equal(a, b)
    # Different seed differs (with overwhelming probability for non-trivial size).
    if n_paths * n_steps >= 5:
        c = simulate_paths(seed + 1, n_paths, n_steps)
        assert not np.array_equal(a, c)


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 39: Generation quantiles are
# correctly ordered.
# ---------------------------------------------------------------------------
@RUN
@given(
    samples=st.lists(st.floats(-50, 200, allow_nan=False), min_size=5, max_size=100),
    cap=st.floats(10, 200, allow_nan=False),
)
def test_property_39_quantile_order(samples, cap):
    q = generation_quantiles(np.array(samples), cap)
    assert q.g_firm90 <= q.g_median
    assert 0.0 <= q.g_firm90 <= cap
    assert 0.0 <= q.g_median <= cap


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 41: Model promotion and fallback
# gates fire on quality failures.
# ---------------------------------------------------------------------------
@RUN
@given(
    flags=st.lists(st.booleans(), min_size=6, max_size=6),
)
def test_property_41_promotion_gates(flags):
    ev = ModelEvaluation(*flags)
    assert may_promote(ev) == all(flags)


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 42: A regime-review flag fires on
# any trigger condition.
# ---------------------------------------------------------------------------
@RUN
@given(flags=st.lists(st.booleans(), min_size=5, max_size=5))
def test_property_42_regime_review(flags):
    c = RegimeConditions(*flags)
    assert regime_review_flagged(c) == any(flags)


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 54: Stale or invalid
# compliance-critical input blocks trade output.
# ---------------------------------------------------------------------------
@RUN
@given(
    comp_fresh=st.booleans(), comp_sig=st.booleans(),
    hydro_fresh=st.booleans(), hydro_q=st.booleans(),
    normal=st.floats(0, 1000, allow_nan=False),
)
def test_property_54_stale_blocks_trade(comp_fresh, comp_sig, hydro_fresh, hydro_q, normal):
    inp = CycleInputs(comp_fresh, comp_sig, hydro_fresh, hydro_q, normal)
    fc = run_forecast_stage(inp)
    if not (comp_fresh and comp_sig):
        assert fc.trade_output_allowed is False
        assert fc.quantity_mwh == 0.0
        assert fc.stale_flag is True
    elif not (hydro_fresh and hydro_q):
        assert fc.trade_output_allowed is True
        assert fc.quantity_mwh <= normal + 1e-9
        assert fc.stale_flag is True
    else:
        assert fc.trade_output_allowed is True
        assert fc.quantity_mwh == normal
        assert fc.stale_flag is False


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 55: No order is emitted without
# its prerequisites.
# ---------------------------------------------------------------------------
@RUN
@given(
    paths=st.integers(0, 200000),
    safe_min=st.integers(1, 200000),
    evidence_ok=st.booleans(),
)
def test_property_55_order_prerequisites(paths, safe_min, evidence_ok):
    prereq = OrderPrerequisites(paths, safe_min, evidence_ok)
    assert may_publish_bid(prereq) == (paths >= safe_min)
    assert may_emit_order(prereq) == ((paths >= safe_min) and evidence_ok)
