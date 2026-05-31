"""Forecast-cycle engine: runs one full cycle and returns a structured result.

This is the reusable core behind both the scheduler and the HTTP control plane.
It wires the verified `bbin_platform` modules into a single deterministic pass
over synthetic-but-realistic inputs and returns a JSON-serializable dictionary
(a decision card plus the compliance / workflow / settlement trail).

No printing, no I/O: pure orchestration so it can be called from a loop or an
HTTP handler and the result rendered however the caller wants.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import numpy as np

from bbin_platform.schemas import DeclaredATC, RegulatoryRuleset, VolumeBounds
from bbin_platform.ingestion import (
    QCThresholds,
    Sample,
    aggregate_15min,
    qc_check,
    screen_snow_feature,
)
from bbin_platform.lakehouse import (
    RatingCurveHistory,
    RatingCurveVersion,
    effective_discharge,
    generation_mw,
)
from bbin_platform.modeling import (
    Charges,
    generation_quantiles,
    half_life,
    net_revenue,
    ou_mle,
    select_offset,
    simulate_paths,
)
from bbin_platform.hard_controls import (
    DeclaredAtcStore,
    EgressEvent,
    MakerCheckerGate,
    RulesetRegistry,
    ScadaEgressGuard,
    WorkflowEvent,
    executable_volume,
    governing_atc,
)
from bbin_platform.compliance import GATE_SEQUENCE, Approval, ComplianceEngine, Transaction
from bbin_platform.workflow import RecommendationWorkflow
from bbin_platform.cycle import (
    CycleInputs,
    OrderPrerequisites,
    may_emit_order,
    may_publish_bid,
    run_forecast_stage,
)
from bbin_platform.settlement import (
    AuditEntry,
    AuditJournal,
    LedgerEntry,
    SettlementLedger,
)


# ---------------------------------------------------------------------------
# Configuration of a plant / corridor the service runs cycles for
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlantConfig:
    plant_id: str
    corridor_id: str
    capacity_mw: float
    turbine_design_flow_m3s: float
    head_m: float
    efficiency: float
    approved_mw: float
    access_mw: float
    contract_ceiling_mw: float
    india_atc_mw: float
    nepal_atc_mw: float
    ttc_mw: float
    trm_mw: float


DEFAULT_PLANTS: tuple[PlantConfig, ...] = (
    PlantConfig(
        plant_id="KALIGANDAKI_A",
        corridor_id="NP_IN_DHALKEBAR_MUZAFFARPUR_400KV",
        capacity_mw=144.0, turbine_design_flow_m3s=300.0, head_m=115.0, efficiency=0.90,
        approved_mw=140.0, access_mw=135.0, contract_ceiling_mw=120.0,
        india_atc_mw=150.0, nepal_atc_mw=130.0, ttc_mw=200.0, trm_mw=40.0,
    ),
    PlantConfig(
        plant_id="MARSYANGDI",
        corridor_id="NP_IN_DHALKEBAR_MUZAFFARPUR_400KV",
        capacity_mw=69.0, turbine_design_flow_m3s=140.0, head_m=85.0, efficiency=0.88,
        approved_mw=67.0, access_mw=65.0, contract_ceiling_mw=60.0,
        india_atc_mw=150.0, nepal_atc_mw=130.0, ttc_mw=200.0, trm_mw=40.0,
    ),
)


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------


@dataclass
class CycleResult:
    cycle_id: str
    plant_id: str
    corridor_id: str
    generated_at_utc: str
    delivery_time_utc: str
    decision: dict[str, Any] = field(default_factory=dict)
    trail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cycle_id": self.cycle_id,
            "plant_id": self.plant_id,
            "corridor_id": self.corridor_id,
            "generated_at_utc": self.generated_at_utc,
            "delivery_time_utc": self.delivery_time_utc,
            "decision": self.decision,
            "trail": self.trail,
        }


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# The cycle
# ---------------------------------------------------------------------------


def run_cycle(cfg: PlantConfig, *, seed: Optional[int] = None,
              delivery_time_utc: str = "2026-06-01T10:00:00Z",
              n_paths: int = 100_000,
              force_stale_hydrology: bool = False) -> CycleResult:
    """Run one full forecast cycle for a plant and return a structured result."""
    if seed is None:
        seed = int(time.time()) ^ hash(cfg.plant_id) & 0x7FFFFFFF
    rng = np.random.default_rng(seed)
    cycle_id = str(uuid.uuid4())

    # --- Step 1: hydrology ingestion ----------------------------------------
    base_epoch = 1_780_000_000
    stages = 2.4 + 0.05 * rng.standard_normal(15)
    samples = [Sample(base_epoch + i * 60, float(s)) for i, s in enumerate(stages)]
    th = QCThresholds(0.0, 6.0, 0.5, 5, 1.0)
    qc_flags = 0
    hist: list[float] = []
    for s in samples:
        qc_flags += int(qc_check(s.value, hist, [], th).any_flag)
        hist.append(s.value)
    mean_stage = aggregate_15min(samples, base_epoch)[0]["mean"]
    curves = RatingCurveHistory([
        RatingCurveVersion("RC-2026", "2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z",
                           a=140.0, b=1.55)])
    disc = curves.convert(mean_stage, delivery_time_utc)
    q_eff = effective_discharge(disc.discharge_m3s, cfg.turbine_design_flow_m3s)
    point_gen = generation_mw(q_eff, cfg.head_m, cfg.efficiency, cfg.capacity_mw)
    snow = screen_snow_feature(cloud_fraction=0.72, raw_snow_index=0.4)

    # --- Step 2: price calibration ------------------------------------------
    true_kappa, true_mu, true_sigma, n, dt = 0.6, 0.0, 0.4, 3000, 1.0
    b = np.exp(-true_kappa * dt)
    csd = true_sigma * np.sqrt((1 - b * b) / (2 * true_kappa))
    y = np.empty(n)
    y[0] = true_mu
    for t in range(1, n):
        y[t] = true_mu + b * (y[t - 1] - true_mu) + rng.normal(0, csd)
    prices = 40.0 + 60.0 * np.sin(np.linspace(0, 6, 200)) + 5 * rng.standard_normal(200)
    prices[10] = -8.0
    offset_c = select_offset(prices)
    est = ou_mle(y, dt)

    # --- Step 3: Monte Carlo -------------------------------------------------
    gen_noise = simulate_paths(seed, n_paths, 1)[:, 0]
    gen_samples = np.clip(point_gen * (1.0 + 0.18 * gen_noise), 0.0, cfg.capacity_mw)
    gq = generation_quantiles(gen_samples, cfg.capacity_mw)
    price_level = 52.0
    price_noise = simulate_paths(seed + 1, n_paths, 1)[:, 0]
    price_samples = price_level * (1.0 + 0.25 * price_noise)
    p10, p50, p90 = (float(np.percentile(price_samples, q)) for q in (10, 50, 90))

    # --- Step 4: declared ATC truth -----------------------------------------
    atc_store = DeclaredAtcStore()
    atc_rec = DeclaredATC(
        corridor_id=cfg.corridor_id, direction="EXPORT", block_start_utc=delivery_time_utc,
        ttc_mw=cfg.ttc_mw, trm_mw=cfg.trm_mw, atc_mw=cfg.india_atc_mw,
        issuing_operator="GRID_INDIA_NLDC", counterpart_atc_mw=cfg.nepal_atc_mw,
        declaration_time_utc=_now_utc(), revision=1, reason_code="DAY_AHEAD",
        signature_ref="nldc-kms-1")
    atc_rec_accepted = atc_rec.satisfies_capacity_inequality()
    if atc_rec_accepted:
        atc_store.declare(atc_rec)
    gov_atc = governing_atc(cfg.india_atc_mw, cfg.nepal_atc_mw)

    # --- Step 5: bid sizing --------------------------------------------------
    bounds = VolumeBounds(
        approved_mw=cfg.approved_mw, access_mw=cfg.access_mw, governing_atc_mw=gov_atc,
        generation_available_mw=gq.g_firm90, contract_ceiling_mw=cfg.contract_ceiling_mw)
    rec_mw = executable_volume(bounds)
    limiting = bounds.limiting_constraint()

    # --- Step 6: compliance + ruleset + maker-checker + egress guard --------
    txn = Transaction(f"TXN-{cycle_id[:8]}", cfg.plant_id, cfg.corridor_id,
                      requested_mw=rec_mw, delivery_time_utc=delivery_time_utc)
    approvals = [Approval(g, cfg.plant_id, cfg.corridor_id, cfg.approved_mw,
                          "2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z", True)
                 for g in GATE_SEQUENCE]
    decision = ComplianceEngine().evaluate(txn, approvals)

    ruleset = RegulatoryRuleset(
        regulatory_ruleset_id="RS-2026-Q2", cerc_version="CBTE-2019-A2-2025",
        cea_version="DA-PROC-2021-A2", mop_version="2018-AUG2024",
        nepal_version="GRIDCODE-2080", bangladesh_version="GRIDCODE-2018",
        effective_date="2026-05-23", amendment_or_consolidated_ref="consolidated-2025",
        access_construct="GNA",
        charge_rates={"transmission": 0.30, "sna": 0.005, "dsm": 0.10, "reactive": 0.02,
                      "tax_levy": 0.18, "trader_fee": 0.07, "losses": 0.04, "exchange": 0.01})

    transmitted = False
    workflow_state = "BLOCKED_BY_COMPLIANCE"
    if decision.permitted:
        reg = RulesetRegistry()
        reg.bind(txn.transaction_id, ruleset)
        wf = RecommendationWorkflow(f"REC-{cycle_id[:8]}")
        wf.validate_compliance()
        wf.maker_approve("trader.alice")
        wf.checker_approve("compliance.bob")
        gate = MakerCheckerGate({"trader.alice", "compliance.bob"})
        events = [WorkflowEvent("MAKER_APPROVED", "trader.alice", wf.recommendation_id),
                  WorkflowEvent("CHECKER_APPROVED", "compliance.bob", wf.recommendation_id)]
        if gate.may_transmit(wf.recommendation_id, events):
            wf.transmit()
        transmitted = wf.transmitted
        workflow_state = wf.state.value

    guard = ScadaEgressGuard()
    bid_egress = guard.evaluate(EgressEvent("https://nvvn-trader.example/bid", "RECOMMEND"))
    ot_egress = guard.evaluate(EgressEvent("https://nea-ldc/scada/breaker", "BREAKER_OPEN"))

    # --- Step 7: fail-closed cycle gates ------------------------------------
    fc = run_forecast_stage(CycleInputs(
        compliance_input_fresh=True, compliance_signature_valid=True,
        hydrology_fresh=not force_stale_hydrology, hydrology_quality_ok=not force_stale_hydrology,
        normal_estimate_mwh=rec_mw))
    prereq = OrderPrerequisites(monte_carlo_paths=n_paths, safe_minimum_paths=50_000,
                                evidence_append_succeeded=True)
    order_emitted = (fc.trade_output_allowed and transmitted and may_emit_order(prereq))

    # --- Step 8: settlement evidence ----------------------------------------
    delivered = fc.quantity_mwh * 0.25
    charges = Charges(0.07 * delivered, 0.30 * delivered, 0.005 * delivered,
                      0.10 * delivered, 0.02 * delivered, 0.18 * delivered, 0.0)
    nr = net_revenue(delivered, p50, charges)
    ledger = SettlementLedger()
    ledger.append(LedgerEntry(f"L-{cycle_id[:8]}", txn.transaction_id, "block-40",
                              delivered, "SETTLEMENT"))
    audit = AuditJournal()
    for act in ("ingest", "calibrate", "scenario", "recommend", "approve", "settle"):
        audit.record(AuditEntry(act, "service", _now_utc(), ("h",), "svc-v1"))

    # --- assemble result -----------------------------------------------------
    result = CycleResult(
        cycle_id=cycle_id, plant_id=cfg.plant_id, corridor_id=cfg.corridor_id,
        generated_at_utc=_now_utc(), delivery_time_utc=delivery_time_utc)
    result.decision = {
        "recommended_mw": round(rec_mw, 2),
        "limiting_constraint": limiting,
        "limit_price_inr_mwh": round(p50, 2),
        "g_median_mw": round(gq.g_median, 2),
        "g_firm90_mw": round(gq.g_firm90, 2),
        "price_p10": round(p10, 2),
        "price_p50": round(p50, 2),
        "price_p90": round(p90, 2),
        "status": "ADVISORY",                 # never EXECUTABLE without a real trader accept
        "order_emitted": bool(order_emitted),
        "stale_fallback": bool(fc.stale_flag),
    }
    result.trail = {
        "seed": seed,
        "hydrology": {
            "qc_flags": qc_flags, "mean_stage_m": round(mean_stage, 3),
            "rating_curve_version": disc.rating_curve_version,
            "effective_discharge_m3s": round(q_eff, 1),
            "point_generation_mw": round(point_gen, 1),
            "snow_feature_flag": snow.flag,
        },
        "price_model": {
            "offset_c": round(offset_c, 2),
            "kappa": round(est.kappa, 4), "mu": round(est.mu, 4), "sigma": round(est.sigma, 4),
            "half_life_blocks": round(half_life(est.kappa), 3),
        },
        "constraints": {
            "india_atc_mw": cfg.india_atc_mw, "nepal_atc_mw": cfg.nepal_atc_mw,
            "governing_atc_mw": gov_atc, "atc_capacity_check_ok": atc_rec_accepted,
            "monte_carlo_paths": n_paths,
        },
        "compliance": {
            "permitted": decision.permitted,
            "gates_checked": [g.value for g in decision.checked_gates],
            "blocking_gate": decision.blocking_gate.value if decision.blocking_gate else None,
            "ruleset_id": ruleset.regulatory_ruleset_id,
        },
        "workflow": {"state": workflow_state, "transmitted": transmitted,
                     "maker": "trader.alice", "checker": "compliance.bob"},
        "egress_guard": {
            "commercial_bid": bid_egress.decision.value,
            "scada_control": ot_egress.decision.value,
            "scada_alerts": len(ot_egress.alerts),
        },
        "settlement": {
            "delivered_mwh": round(delivered, 2),
            "charges_total": round(charges.total(), 2),
            "net_revenue": round(nr, 2),
            "ledger_entries": len(ledger), "audit_records": len(audit),
        },
    }
    return result
