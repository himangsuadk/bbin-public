"""Forecast-cycle orchestration with fail-closed gating (Task 27).

Encodes the cycle-safety rules: stale/invalid compliance-critical input blocks
trade output (Property 54); no order without its prerequisites (Property 55).

Properties: 54, 55
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# ---------------------------------------------------------------------------
# Property 54 — stale/invalid compliance-critical input blocks trade output
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CycleInputs:
    compliance_input_fresh: bool
    compliance_signature_valid: bool
    hydrology_fresh: bool
    hydrology_quality_ok: bool
    normal_estimate_mwh: float


@dataclass(frozen=True)
class CycleForecast:
    trade_output_allowed: bool
    quantity_mwh: float
    stale_flag: bool


def run_forecast_stage(inp: CycleInputs) -> CycleForecast:
    """Block trade output when a compliance-critical input is stale or fails
    signature validation; produce a conservative stale-flagged fallback on
    stale/low-quality hydrology (Property 54)."""
    compliance_ok = inp.compliance_input_fresh and inp.compliance_signature_valid
    if not compliance_ok:
        return CycleForecast(trade_output_allowed=False, quantity_mwh=0.0, stale_flag=True)

    hydro_ok = inp.hydrology_fresh and inp.hydrology_quality_ok
    if not hydro_ok:
        # conservative fallback: never exceeds the normal estimate, flagged stale
        fallback = min(inp.normal_estimate_mwh, inp.normal_estimate_mwh * 0.5)
        return CycleForecast(trade_output_allowed=True, quantity_mwh=fallback, stale_flag=True)

    return CycleForecast(trade_output_allowed=True,
                         quantity_mwh=inp.normal_estimate_mwh, stale_flag=False)


# ---------------------------------------------------------------------------
# Property 55 — no order without its prerequisites
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderPrerequisites:
    monte_carlo_paths: int
    safe_minimum_paths: int
    evidence_append_succeeded: bool


def may_publish_bid(prereq: OrderPrerequisites) -> bool:
    """A bid recommendation is published only if the Monte Carlo stage reached the
    benchmarked safe-minimum scenario count (Property 55, first clause)."""
    return prereq.monte_carlo_paths >= prereq.safe_minimum_paths


def may_emit_order(prereq: OrderPrerequisites) -> bool:
    """An order-interface event is emitted only if the bid could be published AND
    the regulatory-evidence append succeeded (Property 55, second clause)."""
    return may_publish_bid(prereq) and prereq.evidence_append_succeeded
