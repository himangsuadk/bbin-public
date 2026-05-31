"""Quantitative modeling suite (Tasks 17-21).

MRJD numerics (offset, softplus positivity, Poisson truncation, log-sum-exp,
half-life), GMM HAC weighting (PSD), Monte Carlo (in-interval jump times,
net-revenue formula, determinism), generation quantiles, and promotion gates.

Properties: 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


# ---------------------------------------------------------------------------
# MRJD parameter domain (Property 32)
# ---------------------------------------------------------------------------


def softplus(x: float) -> float:
    """Numerically stable softplus; strictly positive for finite x."""
    if x > 30:
        return x
    return math.log1p(math.exp(x))


def positive_params(raw: dict[str, float]) -> dict[str, float]:
    """Map raw real parameters to strictly positive domain (kappa, sigma, lambda,
    sigma_J) via softplus (Property 32)."""
    return {k: softplus(v) for k, v in raw.items()}


def select_offset(prices: np.ndarray, margin: float = 1.0) -> float:
    """Versioned offset c such that S_t + c > 0 for all observations (Property 32).
    c = margin - min(price) when min <= 0, else a small positive margin."""
    pmin = float(np.min(prices)) if len(prices) else 0.0
    if pmin <= 0:
        return -pmin + margin
    return margin


def transformed_price(prices: np.ndarray, c: float) -> np.ndarray:
    return np.log(prices + c)


# ---------------------------------------------------------------------------
# Poisson jump-count truncation (Property 33)
# ---------------------------------------------------------------------------


def poisson_truncation_count(lambda_delta: float, tail_threshold: float = 1e-10) -> int:
    """Smallest M such that P(N > M) < tail_threshold for N ~ Poisson(lambda_delta)
    (Property 33)."""
    if lambda_delta <= 0:
        return 0
    # cumulative until tail below threshold
    cdf = 0.0
    pmf = math.exp(-lambda_delta)
    k = 0
    cdf += pmf
    while (1.0 - cdf) >= tail_threshold:
        k += 1
        pmf *= lambda_delta / k
        cdf += pmf
        if k > 10000:  # safety
            break
    return k


# ---------------------------------------------------------------------------
# Log-sum-exp (Property 34)
# ---------------------------------------------------------------------------


def logsumexp(log_terms: np.ndarray) -> float:
    """Stable log(sum(exp(x))) (Property 34)."""
    if len(log_terms) == 0:
        return -math.inf
    m = float(np.max(log_terms))
    if not math.isfinite(m):
        return m
    return m + math.log(float(np.sum(np.exp(log_terms - m))))


# ---------------------------------------------------------------------------
# Half-life (Property 35 support)
# ---------------------------------------------------------------------------


def half_life(kappa: float) -> float:
    """Mean-reversion half-life ln(2)/kappa (Req 14.9)."""
    if kappa <= 0:
        return math.inf
    return math.log(2.0) / kappa


@dataclass
class OUParams:
    kappa: float
    mu: float
    sigma: float


def ou_mle(series: np.ndarray, dt: float = 1.0) -> OUParams:
    """Exact-transition OLS/MLE for an Ornstein-Uhlenbeck process; recovers
    generating kappa/mu/sigma (Property 35, simplified diffusion case)."""
    x = series[:-1]
    y = series[1:]
    n = len(x)
    sx, sy = x.sum(), y.sum()
    sxx, sxy = (x * x).sum(), (x * y).sum()
    denom = n * sxx - sx * sx
    if denom == 0:
        return OUParams(0.0, float(series.mean()), 0.0)
    b = (n * sxy - sx * sy) / denom              # slope = exp(-kappa*dt)
    a = (sy - b * sx) / n
    b = min(max(b, 1e-9), 1 - 1e-9)
    kappa = -math.log(b) / dt
    mu = a / (1 - b)
    resid = y - (a + b * x)
    var_resid = float((resid * resid).mean())
    sigma2 = var_resid * (2 * kappa) / (1 - b * b) if (1 - b * b) > 0 else var_resid
    return OUParams(kappa, mu, math.sqrt(max(sigma2, 0.0)))


# ---------------------------------------------------------------------------
# GMM HAC weighting matrix PSD (Property 36)
# ---------------------------------------------------------------------------


def hac_weighting_matrix(moments: np.ndarray, bandwidth: int) -> np.ndarray:
    """Newey-West HAC covariance estimator. Symmetric PSD by construction with
    Bartlett kernel (Property 36).

    moments: shape (T, k) of moment-condition contributions per observation.
    """
    T, k = moments.shape
    g = moments - moments.mean(axis=0, keepdims=True)
    # lag 0
    s = (g.T @ g) / T
    for lag in range(1, bandwidth + 1):
        if lag >= T:
            break
        w = 1.0 - lag / (bandwidth + 1.0)  # Bartlett weight, ensures PSD
        gamma = (g[lag:].T @ g[:-lag]) / T
        s = s + w * (gamma + gamma.T)
    return s


# ---------------------------------------------------------------------------
# Monte Carlo: in-interval jump times (Property 37)
# ---------------------------------------------------------------------------


def conditional_jump_times(n_jumps: int, delta: float, rng: np.random.Generator) -> np.ndarray:
    """Given N jumps in an interval of length delta, conditional jump times are
    i.i.d. Uniform(0, delta) (Property 37) — never placed at the interval end."""
    if n_jumps <= 0:
        return np.array([])
    return np.sort(rng.uniform(0.0, delta, size=n_jumps))


# ---------------------------------------------------------------------------
# Net-revenue formula (Property 38)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Charges:
    trader_fee: float
    transmission: float
    sna: float
    dsm: float
    reactive: float
    tax_levy: float
    contract_penalty: float

    def total(self) -> float:
        return (self.trader_fee + self.transmission + self.sna + self.dsm
                + self.reactive + self.tax_levy + self.contract_penalty)


def net_revenue(delivered_energy_mwh: float, cleared_price: float, charges: Charges) -> float:
    """Net revenue = delivered_energy * cleared_price - sum(charges) (Property 38)."""
    return delivered_energy_mwh * cleared_price - charges.total()


# ---------------------------------------------------------------------------
# Determinism (Property 40)
# ---------------------------------------------------------------------------


def simulate_paths(seed: int, n_paths: int, n_steps: int) -> np.ndarray:
    """Deterministic seeded simulation: identical seed/inputs => identical output
    (Property 40). Serial vs 'parallel' chunked generation also identical because
    each path draws from an independent child stream keyed by path index."""
    ss = np.random.SeedSequence(seed)
    children = ss.spawn(n_paths)
    out = np.empty((n_paths, n_steps))
    for i, child in enumerate(children):
        rng = np.random.default_rng(child)
        out[i] = rng.standard_normal(n_steps)
    return out


# ---------------------------------------------------------------------------
# Generation quantiles ordering (Property 39)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GenerationQuantiles:
    g_firm90: float   # lower 10th percentile
    g_median: float   # 50th percentile


def generation_quantiles(samples: np.ndarray, plant_capacity_mw: float) -> GenerationQuantiles:
    """G_firm90 (P10) <= G_median (P50); both in [0, capacity] (Property 39)."""
    clipped = np.clip(samples, 0.0, plant_capacity_mw)
    p10 = float(np.percentile(clipped, 10))
    p50 = float(np.percentile(clipped, 50))
    # numerical guard: enforce ordering exactly
    g_firm90 = min(p10, p50)
    return GenerationQuantiles(g_firm90=g_firm90, g_median=p50)


# ---------------------------------------------------------------------------
# Promotion / fallback gates (Property 41) and regime review (Property 42)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelEvaluation:
    beats_naive_wet: bool
    beats_naive_dry: bool
    converged: bool
    stable: bool
    no_critical_dq_defect: bool
    jump_divergence_within_threshold: bool


def may_promote(ev: ModelEvaluation) -> bool:
    """Promote only if all gates pass (Property 41)."""
    return all([
        ev.beats_naive_wet, ev.beats_naive_dry, ev.converged, ev.stable,
        ev.no_critical_dq_defect, ev.jump_divergence_within_threshold,
    ])


@dataclass(frozen=True)
class RegimeConditions:
    kappa_outside_bootstrap: bool
    lambda_outside_bootstrap: bool
    cap_or_floor_changed: bool
    exchange_rule_changed: bool
    hydro_regime_bias_exceeded: bool


def regime_review_flagged(c: RegimeConditions) -> bool:
    """A regime review is flagged iff at least one trigger holds (Property 42)."""
    return any([
        c.kappa_outside_bootstrap, c.lambda_outside_bootstrap,
        c.cap_or_floor_changed, c.exchange_rule_changed,
        c.hydro_regime_bias_exceeded,
    ])
