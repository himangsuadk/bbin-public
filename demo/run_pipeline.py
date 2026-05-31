"""End-to-end demonstration pipeline for the BBIN Hydropower Platform.

This is the missing *entrypoint* that wires the verified modules together and
runs one full forecast cycle on synthetic-but-realistic data, printing a real
bid decision card and the compliance/settlement trail.

It is a DEMO, not production: the data is synthetic, there is no Kafka/Spark/
broker, and no legal instruments are executed. It exists to let you watch the
already-verified logic produce an actual decision end to end.

Run:
    $env:PYTHONPATH = "<repo>\bbin-platform"
    python -m demo.run_pipeline
"""

from __future__ import annotations

import numpy as np

from bbin_platform.schemas import (
    DeclaredATC,
    RegulatoryRuleset,
    VolumeBounds,
)
from bbin_platform.ingestion import (
    QCThresholds,
    Sample,
    aggregate_15min,
    identify_lag,
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
    conditional_jump_times,
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
from bbin_platform.compliance import (
    GATE_SEQUENCE,
    Approval,
    ComplianceEngine,
    Transaction,
)
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


# A fixed seed makes the whole demo reproducible (Property 40).
SEED = 20260523
RNG = np.random.default_rng(SEED)

PLANT = "KALIGANDAKI_A"
CORRIDOR = "NP_IN_DHALKEBAR_MUZAFFARPUR_400KV"
PLANT_CAPACITY_MW = 144.0
DELIVERY_TIME = "2026-06-01T10:00:00Z"


def hr(title: str) -> None:
    print("\n" + "=" * 74)
    print(f"  {title}")
    print("=" * 74)


def step1_ingest_hydrology() -> dict:
    """Synthetic gauge stage -> QC -> rating curve -> effective discharge -> MW."""
    hr("STEP 1  Hydrology ingestion (gauge -> QC -> rating curve -> generation)")

    # Synthetic 1-minute stage readings around 2.4 m over a 15-minute block.
    base_epoch = 1_780_000_000
    stages = 2.4 + 0.05 * RNG.standard_normal(15)
    samples = [Sample(base_epoch + i * 60, float(s)) for i, s in enumerate(stages)]

    th = QCThresholds(min_stage=0.0, max_stage=6.0, max_rate_of_change=0.5,
                      frozen_repeat_count=5, neighbor_max_delta=1.0)
    flagged = 0
    hist: list[float] = []
    for s in samples:
        f = qc_check(s.value, hist, [], th)
        flagged += int(f.any_flag)
        hist.append(s.value)
    agg = aggregate_15min(samples, base_epoch)
    mean_stage = agg[0]["mean"]

    # Rating curve effective on the delivery date: Q = a * h^b.
    curves = RatingCurveHistory([
        RatingCurveVersion("RC-2026", "2026-01-01T00:00:00Z",
                           "2027-01-01T00:00:00Z", a=140.0, b=1.55),
    ])
    disc = curves.convert(mean_stage, DELIVERY_TIME)
    q_eff = effective_discharge(disc.discharge_m3s, turbine_design_flow_m3s=300.0)
    gen = generation_mw(q_eff, head_m=115.0, efficiency=0.90,
                        plant_capacity_mw=PLANT_CAPACITY_MW)

    # Snow auxiliary feature, cloud-screened (non-blocking).
    snow = screen_snow_feature(cloud_fraction=0.72, raw_snow_index=0.4)

    print(f"  gauge samples           : {len(samples)} (1-min), QC flags: {flagged}")
    print(f"  15-min mean stage        : {mean_stage:.3f} m")
    print(f"  rating curve version     : {disc.rating_curve_version}")
    print(f"  raw discharge            : {disc.discharge_m3s:.1f} m3/s")
    print(f"  effective discharge      : {q_eff:.1f} m3/s (capped at design 300)")
    print(f"  point generation         : {gen:.1f} MW (capacity {PLANT_CAPACITY_MW})")
    print(f"  snow feature (cloud 0.72): {snow.flag} (auxiliary, non-blocking)")
    return {"point_generation_mw": gen}


def step2_calibrate_price() -> dict:
    """Synthetic transformed-price series -> exact-OU calibration -> half-life."""
    hr("STEP 2  Price model calibration (arithmetic transformed-price MRJD / OU)")

    # Simulate an OU price-residual series with known parameters, then recover.
    true_kappa, true_mu, true_sigma = 0.6, 0.0, 0.4
    n, dt = 3000, 1.0
    b = np.exp(-true_kappa * dt)
    csd = true_sigma * np.sqrt((1 - b * b) / (2 * true_kappa))
    y = np.empty(n)
    y[0] = true_mu
    for t in range(1, n):
        y[t] = true_mu + b * (y[t - 1] - true_mu) + RNG.normal(0, csd)

    # Demonstrate the price offset selection on a price series that dips below 0.
    prices = 40.0 + 60.0 * np.sin(np.linspace(0, 6, 200)) + 5 * RNG.standard_normal(200)
    prices[10] = -8.0  # a negative clearing print
    c = select_offset(prices)

    est = ou_mle(y, dt)
    print(f"  offset c (keeps S+c>0)   : {c:.2f}  (min price {prices.min():.2f})")
    print(f"  true   kappa/mu/sigma    : {true_kappa:.3f} / {true_mu:.3f} / {true_sigma:.3f}")
    print(f"  fitted kappa/mu/sigma    : {est.kappa:.3f} / {est.mu:.3f} / {est.sigma:.3f}")
    print(f"  mean-reversion half-life : {half_life(est.kappa):.2f} blocks")
    return {"kappa": est.kappa, "mu": est.mu, "sigma": est.sigma,
            "price_level": 52.0}  # INR/kWh-equiv synthetic clearing level


def step3_monte_carlo(point_gen_mw: float, price_level: float) -> dict:
    """Joint generation + price scenarios -> firm/median generation, expected price."""
    hr("STEP 3  Monte Carlo scenarios (deterministic, seeded)")

    n_paths = 100_000
    # Generation samples: lognormal-ish spread around the point estimate, capped.
    gen_noise = simulate_paths(SEED, n_paths, 1)[:, 0]   # standard normal per path
    gen_samples = np.clip(point_gen_mw * (1.0 + 0.18 * gen_noise), 0.0, PLANT_CAPACITY_MW)
    gq = generation_quantiles(gen_samples, PLANT_CAPACITY_MW)

    # Price samples around the calibrated level.
    price_noise = simulate_paths(SEED + 1, n_paths, 1)[:, 0]
    price_samples = price_level * (1.0 + 0.25 * price_noise)
    p10, p50, p90 = (float(np.percentile(price_samples, q)) for q in (10, 50, 90))

    # In-interval jump times demo (Property 37): 3 jumps in one 15-min block.
    jt = conditional_jump_times(3, delta=900.0, rng=RNG)

    print(f"  scenario paths           : {n_paths:,} (seed {SEED})")
    print(f"  G_median                 : {gq.g_median:.1f} MW")
    print(f"  G_firm90 (P10)           : {gq.g_firm90:.1f} MW  <-  used as available generation")
    print(f"  price P10/P50/P90        : {p10:.1f} / {p50:.1f} / {p90:.1f}")
    print(f"  sample jump times (s)    : {np.round(jt, 1).tolist()} (all within 0..900)")
    return {"g_firm90": gq.g_firm90, "g_median": gq.g_median,
            "price_p10": p10, "price_p50": p50, "price_p90": p90}


def step4_constraints() -> dict:
    """Declared ATC truth + the binding-constraint volume bound."""
    hr("STEP 4  Corridor constraints (declared ATC truth + volume bound)")

    atc_store = DeclaredAtcStore()
    rec = DeclaredATC(
        corridor_id=CORRIDOR, direction="EXPORT", block_start_utc=DELIVERY_TIME,
        ttc_mw=200.0, trm_mw=40.0, atc_mw=150.0, issuing_operator="GRID_INDIA_NLDC",
        counterpart_atc_mw=130.0, declaration_time_utc="2026-05-31T18:00:00Z",
        revision=1, reason_code="DAY_AHEAD", signature_ref="nldc-kms-1",
    )
    atc_store.declare(rec)  # rejects if ATC > TTC - TRM
    gov = governing_atc(rec.atc_mw, rec.counterpart_atc_mw)
    print(f"  India ATC / Nepal ATC    : {rec.atc_mw:.0f} / {rec.counterpart_atc_mw:.0f} MW")
    print(f"  capacity check ATC<=TTC-TRM: {rec.atc_mw:.0f} <= {rec.ttc_mw - rec.trm_mw:.0f}  OK")
    print(f"  governing ATC (lower)    : {gov:.0f} MW")
    return {"governing_atc_mw": gov}


def step5_bid(gen: dict, constraints: dict, price: dict) -> dict:
    """Apply the hard volume bound to produce the recommended volume."""
    hr("STEP 5  Bid sizing (executable-volume bound = min of all constraints)")

    bounds = VolumeBounds(
        approved_mw=140.0,                       # CEA designated-authority approval
        access_mw=135.0,                         # GNA/T-GNA access grant
        governing_atc_mw=constraints["governing_atc_mw"],
        generation_available_mw=gen["g_firm90"], # firm generation, not median
        contract_ceiling_mw=120.0,               # bilateral contract ceiling
    )
    vol = executable_volume(bounds)
    limiting = bounds.limiting_constraint()
    print(f"  approved / access        : {bounds.approved_mw:.0f} / {bounds.access_mw:.0f} MW")
    print(f"  governing ATC            : {bounds.governing_atc_mw:.0f} MW")
    print(f"  G_firm90 / contract      : {bounds.generation_available_mw:.1f} / {bounds.contract_ceiling_mw:.0f} MW")
    print(f"  -> recommended volume    : {vol:.1f} MW  (limiting constraint: {limiting})")
    print(f"  limit price (P50)        : {gen['price_p50']:.1f} INR/MWh-equiv")
    return {"recommended_mw": vol, "limiting": limiting,
            "limit_price": gen["price_p50"]}


def step6_compliance_and_workflow(bid: dict) -> dict:
    """Seven compliance gates -> ruleset binding -> maker-checker -> egress guard."""
    hr("STEP 6  Compliance gates, ruleset binding, maker-checker, egress guard")

    txn = Transaction("TXN-0001", PLANT, CORRIDOR,
                      requested_mw=bid["recommended_mw"], delivery_time_utc=DELIVERY_TIME)
    approvals = [
        Approval(g, PLANT, CORRIDOR, approved_mw=140.0,
                 valid_from_utc="2026-01-01T00:00:00Z",
                 valid_to_utc="2027-01-01T00:00:00Z", four_eye_validated=True)
        for g in GATE_SEQUENCE
    ]
    decision = ComplianceEngine().evaluate(txn, approvals)
    print(f"  seven gates              : {'PERMITTED' if decision.permitted else 'BLOCKED'}"
          f"  ({len(decision.checked_gates)}/7 checked)")
    if not decision.permitted:
        print(f"  blocking gate            : {decision.blocking_gate}")
        return {"transmitted": False}

    # Bind an immutable regulatory ruleset.
    ruleset = RegulatoryRuleset(
        regulatory_ruleset_id="RS-2026-Q2", cerc_version="CBTE-2019-A2-2025",
        cea_version="DA-PROC-2021-A2", mop_version="2018-AUG2024",
        nepal_version="GRIDCODE-2080", bangladesh_version="GRIDCODE-2018",
        effective_date="2026-05-23", amendment_or_consolidated_ref="consolidated-2025",
        access_construct="GNA",
        charge_rates={"transmission": 0.30, "sna": 0.005, "dsm": 0.10, "reactive": 0.02,
                      "tax_levy": 0.18, "trader_fee": 0.07, "losses": 0.04, "exchange": 0.01},
    )
    reg = RulesetRegistry()
    reg.bind(txn.transaction_id, ruleset)
    print(f"  ruleset bound            : {ruleset.regulatory_ruleset_id} (immutable)")

    # Maker-checker workflow (two distinct authorized identities).
    wf = RecommendationWorkflow("REC-0001")
    wf.validate_compliance()
    wf.maker_approve("trader.alice")
    wf.checker_approve("compliance.bob")
    gate = MakerCheckerGate({"trader.alice", "compliance.bob"})
    events = [WorkflowEvent("MAKER_APPROVED", "trader.alice", "REC-0001"),
              WorkflowEvent("CHECKER_APPROVED", "compliance.bob", "REC-0001")]
    may_tx = gate.may_transmit("REC-0001", events)
    wf.transmit()
    print(f"  maker / checker          : trader.alice / compliance.bob (distinct)")
    print(f"  maker-checker gate       : {'AUTHORIZED' if may_tx else 'DENIED'}")
    print(f"  workflow state           : {wf.state.value}")

    # The single egress guard must allow a commercial bid but deny any OT control.
    guard = ScadaEgressGuard()
    bid_egress = guard.evaluate(EgressEvent("https://nvvn-trader.example/bid", "RECOMMEND"))
    ot_attempt = guard.evaluate(EgressEvent("https://nea-ldc/scada/breaker", "BREAKER_OPEN"))
    print(f"  egress: commercial bid   : {bid_egress.decision.value} (transmitted={bid_egress.transmitted})")
    print(f"  egress: SCADA control    : {ot_attempt.decision.value} "
          f"(alerts={len(ot_attempt.alerts)}, audited={len(ot_attempt.audit)})")
    return {"transmitted": may_tx and wf.transmitted, "ruleset": reg, "txn": txn}


def step7_cycle_gates(bid: dict) -> bool:
    """Fail-closed cycle gates: stale input blocks; prerequisites for an order."""
    hr("STEP 7  Forecast-cycle fail-closed gates")

    fc = run_forecast_stage(CycleInputs(
        compliance_input_fresh=True, compliance_signature_valid=True,
        hydrology_fresh=True, hydrology_quality_ok=True,
        normal_estimate_mwh=bid["recommended_mw"]))
    prereq = OrderPrerequisites(monte_carlo_paths=100_000, safe_minimum_paths=50_000,
                                evidence_append_succeeded=True)
    print(f"  trade output allowed     : {fc.trade_output_allowed} (stale={fc.stale_flag})")
    print(f"  may publish bid          : {may_publish_bid(prereq)} (paths >= safe minimum)")
    print(f"  may emit order           : {may_emit_order(prereq)} (evidence appended)")
    return fc.trade_output_allowed and may_emit_order(prereq)


def step8_settlement(bid: dict, price: dict) -> None:
    """Per-scenario net revenue + append-only ledger and audit journal."""
    hr("STEP 8  Settlement evidence (net revenue, append-only ledger + audit)")

    delivered = bid["recommended_mw"] * 0.25  # one 15-min block, MWh
    charges = Charges(trader_fee=0.07 * delivered, transmission=0.30 * delivered,
                      sna=0.005 * delivered, dsm=0.10 * delivered,
                      reactive=0.02 * delivered, tax_levy=0.18 * delivered,
                      contract_penalty=0.0)
    nr = net_revenue(delivered, bid["limit_price"], charges)

    ledger = SettlementLedger()
    ledger.append(LedgerEntry("L-0001", "TXN-0001", "block-40", delivered, "SETTLEMENT"))
    audit = AuditJournal()
    for act in ("ingest", "calibrate", "scenario", "recommend", "approve", "settle"):
        audit.record(AuditEntry(act, "pipeline", DELIVERY_TIME, ("h",), "demo-v1"))

    print(f"  delivered energy (block) : {delivered:.2f} MWh")
    print(f"  itemized charges total   : {charges.total():.2f}")
    print(f"  net revenue (scenario)   : {nr:.2f}")
    print(f"  ledger entries           : {len(ledger)} (append-only)")
    print(f"  audit records            : {len(audit)} (append-only WORM)")


def main() -> None:
    print("\nBBIN HYDROPOWER PLATFORM  -  END-TO-END DEMO CYCLE")
    print(f"plant={PLANT}  corridor={CORRIDOR}  delivery={DELIVERY_TIME}")
    print("NOTE: synthetic data; no broker/legal instruments; demonstrates verified logic.")

    hydro = step1_ingest_hydrology()
    price = step2_calibrate_price()
    gen = step3_monte_carlo(hydro["point_generation_mw"], price["price_level"])
    cons = step4_constraints()
    bid = step5_bid(gen, cons, price)
    wf = step6_compliance_and_workflow(bid)
    order_ok = step7_cycle_gates(bid)
    step8_settlement(bid, price)

    hr("DECISION CARD")
    print(f"  plant / corridor         : {PLANT} / {CORRIDOR}")
    print(f"  delivery block           : {DELIVERY_TIME}")
    print(f"  recommended volume       : {bid['recommended_mw']:.1f} MW")
    print(f"  limiting constraint      : {bid['limiting']}")
    print(f"  limit price              : {bid['limit_price']:.1f} INR/MWh-equiv")
    print(f"  G_median / G_firm90      : {gen['g_median']:.1f} / {gen['g_firm90']:.1f} MW")
    print(f"  price P10/P50/P90        : {gen['price_p10']:.1f} / {gen['price_p50']:.1f} / {gen['price_p90']:.1f}")
    print(f"  compliance + maker/checker: {'PASSED' if wf.get('transmitted') else 'BLOCKED'}")
    print(f"  order emitted            : {'YES' if order_ok and wf.get('transmitted') else 'NO'}")
    print("\n  This recommendation is ADVISORY. In production it would require an")
    print("  authorized NVVN trader to accept it before becoming an order.\n")


if __name__ == "__main__":
    main()
