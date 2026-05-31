"""Property tests for compliance, workflow, settlement and credit modules.

Each test implements exactly one design property, >=100 iterations, tagged.
Library: Python Hypothesis.
"""

from __future__ import annotations

from hypothesis import given, settings, strategies as st

from bbin_platform.schemas import AccessConstruct
from bbin_platform.compliance import (
    Approval,
    ComplianceEngine,
    ExecutionConfirmation,
    GATE_SEQUENCE,
    GateName,
    Recommendation,
    RulesetAmendment,
    Transaction,
    confirmation_matches,
    submission_is_held,
    validate_access_construct,
)
from bbin_platform.workflow import (
    IllegalTransition,
    RecommendationWorkflow,
    SState,
    WState,
    schedule_transition_legal,
    _SCHED_TRANSITIONS,
)
from bbin_platform.settlement import (
    AppendOnlyViolation,
    AuditEntry,
    AuditJournal,
    CreditProfile,
    CurtailmentInstruction,
    LedgerEntry,
    MeterTriplet,
    SettlementLedger,
    apply_curtailment,
    assess_meter,
    bilateral_automation_enabled,
    meter_class_accepted,
    performance_fee,
)

RUN = settings(max_examples=200, deadline=None)


def _full_approvals(project="P1", corridor="C1", mw=100.0,
                    vf="2026-01-01T00:00:00Z", vt="2027-01-01T00:00:00Z",
                    four_eye=True) -> list[Approval]:
    return [
        Approval(g, project, corridor, mw, vf, vt, four_eye_validated=four_eye)
        for g in GATE_SEQUENCE
    ]


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 5: Compliance gate blocks on any
# invalid approval.
# ---------------------------------------------------------------------------
@RUN
@given(
    drop_gate=st.one_of(st.none(), st.sampled_from(GATE_SEQUENCE)),
    requested=st.floats(1, 100, allow_nan=False),
)
def test_property_5_gate_blocks_on_invalid(drop_gate, requested):
    engine = ComplianceEngine()
    approvals = _full_approvals(mw=100.0)
    if drop_gate is not None:
        approvals = [a for a in approvals if a.gate != drop_gate]
    txn = Transaction("T1", "P1", "C1", requested, "2026-06-01T00:00:00Z")
    decision = engine.evaluate(txn, approvals)
    if drop_gate is None:
        assert decision.permitted is True
    else:
        assert decision.permitted is False
        assert decision.blocking_gate == drop_gate
        assert decision.reason.startswith("gate_failed")


@RUN
@given(over_request=st.floats(101, 1000, allow_nan=False))
def test_property_5_over_quantum_blocked(over_request):
    engine = ComplianceEngine()
    txn = Transaction("T1", "P1", "C1", over_request, "2026-06-01T00:00:00Z")
    decision = engine.evaluate(txn, _full_approvals(mw=100.0))
    assert decision.permitted is False


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 3: Recommendations are advisory
# until an authorization event exists.
# ---------------------------------------------------------------------------
@RUN
@given(authorize=st.booleans())
def test_property_3_advisory_until_authorized(authorize):
    txn = Transaction("T1", "P1", "C1", 10.0, "2026-06-01T00:00:00Z")
    rec = Recommendation("R1", txn)
    assert rec.is_executable is False
    assert rec.status == "ADVISORY"
    if authorize:
        rec.authorized = True
        assert rec.is_executable is True
        assert rec.status == "EXECUTABLE"


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 48: Four-eye validation is
# required before an approval becomes active.
# ---------------------------------------------------------------------------
@RUN
@given(four_eye=st.booleans())
def test_property_48_four_eye_required(four_eye):
    engine = ComplianceEngine()
    approvals = _full_approvals(four_eye=four_eye)
    txn = Transaction("T1", "P1", "C1", 10.0, "2026-06-01T00:00:00Z")
    decision = engine.evaluate(txn, approvals)
    # DA approval gate cannot pass without four-eye validation.
    assert decision.permitted is four_eye
    if not four_eye:
        assert decision.blocking_gate == GateName.DA_APPROVAL


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 13: Ruleset amendment holds
# affected future submissions.
# ---------------------------------------------------------------------------
@RUN
@given(
    promoted=st.booleans(),
    a_start=st.integers(1, 10), a_len=st.integers(1, 10),
    d_start=st.integers(1, 10), d_len=st.integers(1, 10),
)
def test_property_13_amendment_holds(promoted, a_start, a_len, d_start, d_len):
    def day(n): return f"2026-06-{n:02d}T00:00:00Z"
    amend = RulesetAmendment("RS", day(a_start), day(a_start + a_len),
                             review_promoted=promoted)
    held = submission_is_held(amend, day(d_start), day(d_start + d_len))
    overlap = (a_start < d_start + d_len) and (d_start < a_start + a_len)
    assert held == (overlap and not promoted)


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 15: New configurations use
# GNA/T-GNA and preserve legacy records.
# ---------------------------------------------------------------------------
@RUN
@given(construct=st.sampled_from(list(AccessConstruct)), is_new=st.booleans())
def test_property_15_gna_for_new_configs(construct, is_new):
    ok = validate_access_construct(construct, is_new)
    if is_new:
        assert ok == construct.is_current
    else:
        assert ok is True  # historic records of any construct retained


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 47: Execution confirmations match
# an existing recommendation and active approval.
# ---------------------------------------------------------------------------
@RUN
@given(known_rec=st.booleans(), covered=st.booleans())
def test_property_47_confirmation_matching(known_rec, covered):
    txn = Transaction("T1", "P1", "C1", 50.0, "2026-06-01T00:00:00Z")
    recs = {"R1": Recommendation("R1", txn)}
    approvals = _full_approvals(mw=100.0)
    conf = ExecutionConfirmation(
        exchange_txn_id="X1",
        recommendation_id="R1" if known_rec else "UNKNOWN",
        approval_project_id="P1", approval_corridor_id="C1",
        cleared_mw=50.0 if covered else 5000.0,
    )
    matched = confirmation_matches(conf, recs, approvals)
    assert matched == (known_rec and covered)


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 6: A pending recommendation is
# invalidated by any adverse pre-transmission event.
# ---------------------------------------------------------------------------
@RUN
@given(
    event=st.sampled_from(["ATC_REDUCED", "APPROVAL_EXPIRED", "SECURITY_FAILED",
                           "OPERATOR_INSTRUCTION", "BENIGN_EVENT"]),
    advance_to=st.sampled_from(["draft", "validated", "maker", "checker", "transmitted"]),
)
def test_property_6_adverse_event_invalidates(event, advance_to):
    wf = RecommendationWorkflow("R1")
    if advance_to in ("validated", "maker", "checker", "transmitted"):
        wf.validate_compliance()
    if advance_to in ("maker", "checker", "transmitted"):
        wf.maker_approve("alice")
    if advance_to in ("checker", "transmitted"):
        wf.checker_approve("bob")
    if advance_to == "transmitted":
        wf.transmit()

    pre_transmit = not wf.transmitted
    wf.apply_event(event)

    adverse = event in {"ATC_REDUCED", "APPROVAL_EXPIRED", "SECURITY_FAILED",
                        "OPERATOR_INSTRUCTION"}
    if adverse and pre_transmit:
        assert wf.state == WState.INVALIDATED
    elif adverse and not pre_transmit:
        assert wf.state != WState.INVALIDATED  # already transmitted, handled downstream


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 49: State machines accept only
# legal transitions.
# ---------------------------------------------------------------------------
@RUN
@given(src=st.sampled_from(list(SState)), dst=st.sampled_from(list(SState)))
def test_property_49_schedule_transitions(src, dst):
    legal = schedule_transition_legal(src, dst)
    assert legal == (dst in _SCHED_TRANSITIONS[src])


@RUN
@given(actor_same=st.booleans())
def test_property_49_workflow_rejects_illegal(actor_same):
    wf = RecommendationWorkflow("R1")
    # Cannot maker-approve before compliance validation.
    try:
        wf.maker_approve("alice")
        assert False, "illegal transition should raise"
    except IllegalTransition:
        pass
    wf.validate_compliance()
    wf.maker_approve("alice")
    checker = "alice" if actor_same else "bob"
    if actor_same:
        try:
            wf.checker_approve(checker)
            assert False
        except IllegalTransition:
            pass
    else:
        wf.checker_approve(checker)
        wf.transmit()
        assert wf.transmitted


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 46: Commercial cross-boundary
# meters require class 0.2S accuracy.
# ---------------------------------------------------------------------------
@RUN
@given(cls=st.sampled_from(["0.2S", "0.2", "AGREED_EQUIVALENT", "0.5", "1.0", "1S", ""]))
def test_property_46_meter_class(cls):
    accepted = meter_class_accepted(cls)
    assert accepted == (cls.strip().upper().replace(" ", "") in {"0.2S", "0.2", "AGREED_EQUIVALENT"})


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 45: Meter divergence and
# validation failures hold invoices and raise cases.
# ---------------------------------------------------------------------------
@RUN
@given(
    main=st.floats(0, 1000, allow_nan=False),
    spread=st.floats(0, 100, allow_nan=False),
    tol=st.floats(0.1, 50, allow_nan=False),
    sig_ok=st.booleans(),
    cal_ok=st.booleans(),
)
def test_property_45_meter_divergence(main, spread, tol, sig_ok, cal_ok):
    triplet = MeterTriplet(main_mwh=main, check_mwh=main + spread,
                           standby_mwh=main, signature_valid=sig_ok,
                           calibration_valid=cal_ok)
    a = assess_meter(triplet, tol)
    # Compute the actual reading spread exactly as the code does, to avoid
    # float-precision mismatch between `spread` and `(main + spread) - main`.
    readings = (triplet.main_mwh, triplet.check_mwh, triplet.standby_mwh)
    actual_spread = max(readings) - min(readings)
    if not sig_ok or not cal_ok:
        assert a.accepted is False and a.invoice_held and a.discrepancy_case
    elif actual_spread > tol:
        assert a.accepted is False and a.invoice_held and a.discrepancy_case
    else:
        assert a.accepted is True and not a.invoice_held and not a.discrepancy_case


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 50: Curtailment is preserved and
# penalty-neutral unless contractually allocated.
# ---------------------------------------------------------------------------
@RUN
@given(
    pre=st.floats(0, 500, allow_nan=False),
    post=st.floats(0, 500, allow_nan=False),
    allocates=st.booleans(),
    rate=st.floats(0, 10, allow_nan=False),
)
def test_property_50_curtailment_penalty_neutral(pre, post, allocates, rate):
    instr = CurtailmentInstruction("C1", pre, post, "GRID_SECURITY")
    out = apply_curtailment(instr, allocates, rate)
    # Instruction preserved unchanged.
    assert out.preserved_instruction == instr
    assert out.reallocated_mw == post
    if not allocates:
        assert out.operator_curtailment_penalty == 0.0
    else:
        expected = rate * max(0.0, pre - post)
        assert abs(out.operator_curtailment_penalty - expected) < 1e-6


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 51: Ledger and audit stores are
# append-only and immutable.
# ---------------------------------------------------------------------------
@RUN
@given(
    n=st.integers(1, 20),
    adjust_without_case=st.booleans(),
)
def test_property_51_append_only(n, adjust_without_case):
    ledger = SettlementLedger()
    for i in range(n):
        ledger.append(LedgerEntry(f"e{i}", "C1", f"b{i}", float(i), "SETTLEMENT"))
    assert len(ledger) == n
    # Duplicate id rejected (no overwrite).
    try:
        ledger.append(LedgerEntry("e0", "C1", "b0", 0.0, "SETTLEMENT"))
        assert False
    except AppendOnlyViolation:
        pass
    # Adjustment without an explicit case id rejected (no silent mutation).
    if adjust_without_case:
        try:
            ledger.append(LedgerEntry("adj", "C1", "b0", 1.0, "ADJUSTMENT"))
            assert False
        except AppendOnlyViolation:
            pass
    else:
        ledger.append(LedgerEntry("adj", "C1", "b0", 1.0, "ADJUSTMENT",
                                  adjustment_case_id="CASE-1"))
        assert len(ledger) == n + 1


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 53: Every auditable action
# produces an audit record.
# ---------------------------------------------------------------------------
@RUN
@given(actions=st.lists(st.sampled_from(["access", "export", "recommend", "approve",
                                         "adjust"]), max_size=30))
def test_property_53_audit_record_per_action(actions):
    journal = AuditJournal()
    for i, act in enumerate(actions):
        journal.record(AuditEntry(act, "actor", f"2026-06-01T00:00:{i:02d}Z",
                                  (f"h{i}",), "model-v1"))
    assert len(journal) == len(actions)
    for r in journal.records:
        assert r.actor and r.timestamp_utc and r.input_hashes and r.model_version


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 57: Bilateral settlement
# automation requires valid credit coverage.
# ---------------------------------------------------------------------------
@RUN
@given(
    exposure=st.floats(0, 1000, allow_nan=False),
    limit=st.floats(0, 1000, allow_nan=False),
    coverage=st.booleans(),
)
def test_property_57_credit_coverage(exposure, limit, coverage):
    profile = CreditProfile("BPDB", exposure, limit, coverage)
    enabled = bilateral_automation_enabled(profile)
    assert enabled == (coverage and exposure <= limit)


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 56: Performance fee follows the
# bounded VIR formula.
# ---------------------------------------------------------------------------
@RUN
@given(
    actual=st.floats(-1000, 5000, allow_nan=False),
    benchmark=st.floats(-1000, 5000, allow_nan=False),
    excluded=st.floats(0, 2000, allow_nan=False),
    rate=st.floats(0, 1, allow_nan=False),
    cap=st.floats(0, 1000, allow_nan=False),
)
def test_property_56_performance_fee(actual, benchmark, excluded, rate, cap):
    fee = performance_fee(actual, benchmark, excluded, rate, cap)
    vir = max(0.0, actual - benchmark - excluded)
    assert fee == min(rate * vir, cap)
    assert fee >= 0.0
    assert fee <= cap + 1e-9
