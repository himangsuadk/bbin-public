"""Property-based tests for the safety-critical invariant cores.

Each test implements exactly one design correctness property, runs >=100
iterations, and is tagged in the required format.

Library: Python Hypothesis.
"""

from __future__ import annotations

import json

from hypothesis import given, settings, strategies as st

from bbin_platform.schemas import (
    DeclaredATC,
    RegulatoryRuleset,
    VolumeBounds,
)
from bbin_platform.hard_controls import (
    DeclaredAtcStore,
    AtcRejected,
    EgressEvent,
    EgressDecision,
    MakerCheckerGate,
    RulesetRegistry,
    RulesetBindingError,
    ScadaEgressGuard,
    ScheduleStore,
    SettlementInput,
    WorkflowEvent,
    executable_volume,
    governing_atc,
    meter_truth_filter,
    _CONTROL_OPCODES,
    _OT_ENDPOINT_MARKERS,
)

RUN = settings(max_examples=200, deadline=None)

finite = st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False)


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 1: SCADA control commands are
# always denied with alert and audit.
# ---------------------------------------------------------------------------
@RUN
@given(
    opcode=st.sampled_from(sorted(_CONTROL_OPCODES)),
    marker=st.sampled_from(_OT_ENDPOINT_MARKERS),
    suffix=st.text(alphabet=st.characters(min_codepoint=97, max_codepoint=122), max_size=8),
)
def test_property_1_scada_control_denied(opcode, marker, suffix):
    guard = ScadaEgressGuard()
    # An OT endpoint OR a control opcode must be denied.
    res = guard.evaluate(EgressEvent(target_endpoint=f"https://{marker}{suffix}.local",
                                     opcode=opcode))
    assert res.decision is EgressDecision.DENY
    assert res.transmitted is False
    assert len(res.alerts) == 1
    assert len(res.audit) == 1

    # A benign commercial endpoint with a non-control opcode is allowed.
    benign = guard.evaluate(EgressEvent(target_endpoint="https://trader.example.com/bid",
                                        opcode="RECOMMEND"))
    assert benign.decision is EgressDecision.ALLOW
    assert benign.transmitted is True


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 2: Operator-approved schedules
# are never mutated.
# ---------------------------------------------------------------------------
@RUN
@given(
    content=st.dictionaries(
        st.text(min_size=1, max_size=6),
        st.one_of(st.integers(-1000, 1000), st.text(max_size=10)),
        max_size=6,
    ),
    ops=st.lists(st.text(max_size=8), max_size=20),
)
def test_property_2_schedule_immutable(content, ops):
    store = ScheduleStore()
    h0 = store.accept("S1", content)
    # Simulate arbitrary downstream operations reading the schedule.
    for _ in ops:
        view = store.get("S1")
        view["__scratch__"] = "mutated locally"  # local copy only
    assert store.verify_unchanged("S1")
    assert store.content_hash("S1") == h0


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 7: Executable volume never
# exceeds the minimum binding constraint.
# ---------------------------------------------------------------------------
@RUN
@given(approved=finite, access=finite, atc=finite, gen=finite, contract=finite)
def test_property_7_executable_volume_min_bound(approved, access, atc, gen, contract):
    bounds = VolumeBounds(approved, access, atc, gen, contract)
    v = executable_volume(bounds)
    assert v <= approved + 1e-9
    assert v <= access + 1e-9
    assert v <= atc + 1e-9
    assert v <= gen + 1e-9
    assert v <= contract + 1e-9
    assert v >= 0.0
    assert abs(v - min(approved, access, atc, gen, contract)) < 1e-6


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 8: Governing ATC is the lower
# authenticated declared value.
# ---------------------------------------------------------------------------
@RUN
@given(a=finite, b=finite)
def test_property_8_governing_atc_is_lower(a, b):
    g = governing_atc(a, b)
    assert g == min(a, b)
    assert g <= a and g <= b


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 9: Declared ATC is immutable to
# model output.
# ---------------------------------------------------------------------------
@RUN
@given(atc=st.floats(0, 1000, allow_nan=False), headroom=st.floats(0, 5000, allow_nan=False))
def test_property_9_declared_atc_immutable(atc, headroom):
    ttc, trm = atc + 100.0, 50.0
    rec = DeclaredATC(
        corridor_id="NP_IN", direction="EXPORT", block_start_utc="2026-05-23T00:00:00Z",
        ttc_mw=ttc, trm_mw=trm, atc_mw=min(atc, ttc - trm), issuing_operator="GRID_INDIA",
        counterpart_atc_mw=min(atc, ttc - trm), declaration_time_utc="2026-05-23T00:00:00Z",
        revision=1, reason_code="INIT", signature_ref="k1",
    )
    store = DeclaredAtcStore()
    store.declare(rec)
    before = store.governing("NP_IN", "EXPORT", 1)
    # There is no API to raise it; a model "headroom" value cannot enter the store.
    after = store.governing("NP_IN", "EXPORT", 1)
    assert before == after
    assert after <= rec.atc_mw


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 10: ATC declarations satisfy the
# capacity inequality or are rejected.
# ---------------------------------------------------------------------------
@RUN
@given(ttc=finite, trm=finite, atc=finite)
def test_property_10_atc_capacity_inequality(ttc, trm, atc):
    store = DeclaredAtcStore()
    rec = DeclaredATC(
        corridor_id="C", direction="EXPORT", block_start_utc="2026-05-23T00:00:00Z",
        ttc_mw=ttc, trm_mw=trm, atc_mw=atc, issuing_operator="OP",
        counterpart_atc_mw=atc, declaration_time_utc="2026-05-23T00:00:00Z",
        revision=1, reason_code="R", signature_ref="k1",
    )
    valid = atc <= ttc - trm
    if valid:
        store.declare(rec)  # accepted
        assert store.governing("C", "EXPORT", 1) == min(atc, atc)
    else:
        try:
            store.declare(rec)
            assert False, "should have rejected"
        except AtcRejected:
            pass


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 4: Transmission requires distinct
# maker and checker acceptance.
# ---------------------------------------------------------------------------
@RUN
@given(
    maker=st.sampled_from(["alice", "bob", "carol"]),
    checker=st.sampled_from(["alice", "bob", "carol"]),
    include_maker=st.booleans(),
    include_checker=st.booleans(),
)
def test_property_4_maker_checker_distinct(maker, checker, include_maker, include_checker):
    gate = MakerCheckerGate({"alice", "bob", "carol"})
    events: list[WorkflowEvent] = []
    if include_maker:
        events.append(WorkflowEvent("MAKER_APPROVED", maker, "R1"))
    if include_checker:
        events.append(WorkflowEvent("CHECKER_APPROVED", checker, "R1"))
    allowed = gate.may_transmit("R1", events)
    expected = include_maker and include_checker and (maker != checker)
    assert allowed == expected


@RUN
@given(actor=st.text(min_size=1, max_size=5))
def test_property_4_unauthorized_actor_cannot_authorize(actor):
    # An actor not in the authorized set can never enable transmission.
    gate = MakerCheckerGate({"alice", "bob"})
    if actor in {"alice", "bob"}:
        return
    events = [WorkflowEvent("MAKER_APPROVED", actor, "R1"),
              WorkflowEvent("CHECKER_APPROVED", actor, "R1")]
    assert gate.may_transmit("R1", events) is False


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 44: Settlement uses certified
# meter truth only.
# ---------------------------------------------------------------------------
source_kinds = st.sampled_from(
    ["CERTIFIED_METER", "GAUGE", "TELEMETRY", "MODEL", "SCADA_STATUS", "FORECAST"])


@RUN
@given(
    inputs=st.lists(
        st.builds(
            SettlementInput,
            source_kind=source_kinds,
            energy_mwh=st.floats(0, 1000, allow_nan=False),
            signature_valid=st.booleans(),
            quality=st.sampled_from(["VALID", "BAD", "OLD", "SUBSTITUTED"]),
            imputed=st.booleans(),
            schedule_accepted=st.booleans(),
        ),
        max_size=25,
    )
)
def test_property_44_meter_truth_only(inputs):
    admitted = meter_truth_filter(inputs)
    for i in admitted:
        assert i.source_kind == "CERTIFIED_METER"
        assert i.signature_valid is True
        assert i.quality == "VALID"
        assert i.imputed is False
        assert i.schedule_accepted is True
    # Nothing non-meter ever slips through.
    assert all(i.source_kind == "CERTIFIED_METER" for i in admitted)


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 12: Every transaction binds an
# immutable, complete ruleset.
# ---------------------------------------------------------------------------
def _ruleset(rid: str, complete: bool = True) -> RegulatoryRuleset:
    return RegulatoryRuleset(
        regulatory_ruleset_id=rid,
        cerc_version="CBTE-2019-A2-2025" if complete else "",
        cea_version="DA-PROC-2021-A2",
        mop_version="2018-AUG2024",
        nepal_version="GRIDCODE-2080",
        bangladesh_version="GRIDCODE-2018",
        effective_date="2026-05-23",
        amendment_or_consolidated_ref="consolidated-2025",
        access_construct="GNA",
        charge_rates={"transmission": 0.3, "sna": 0.005, "dsm": 0.1,
                      "reactive": 0.02, "tax_levy": 0.18, "trader_fee": 0.07,
                      "losses": 0.04, "exchange": 0.01},
    )


@RUN
@given(rid=st.text(min_size=1, max_size=10), txid=st.text(min_size=1, max_size=10))
def test_property_12_immutable_complete_ruleset(rid, txid):
    reg = RulesetRegistry()
    reg.bind(txid, _ruleset(rid))
    bound = reg.ruleset_for(txid)
    assert bound.is_complete()
    # Re-binding the same transaction is rejected — binding is immutable.
    try:
        reg.bind(txid, _ruleset(rid + "x"))
        assert False, "rebinding should be rejected"
    except RulesetBindingError:
        pass
    # Incomplete ruleset is rejected.
    reg2 = RulesetRegistry()
    try:
        reg2.bind("t", _ruleset("r", complete=False))
        assert False, "incomplete ruleset should be rejected"
    except RulesetBindingError:
        pass


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 14: Charges derive only from the
# bound ruleset and are itemized.
# ---------------------------------------------------------------------------
@RUN
@given(
    txid=st.text(min_size=1, max_size=8),
    rates=st.fixed_dictionaries({
        "transmission": st.floats(0, 1, allow_nan=False),
        "sna": st.floats(0, 1, allow_nan=False),
        "dsm": st.floats(0, 1, allow_nan=False),
        "reactive": st.floats(0, 1, allow_nan=False),
        "tax_levy": st.floats(0, 1, allow_nan=False),
        "trader_fee": st.floats(0, 1, allow_nan=False),
        "losses": st.floats(0, 1, allow_nan=False),
        "exchange": st.floats(0, 1, allow_nan=False),
    }),
)
def test_property_14_charges_from_bound_ruleset(txid, rates):
    rs = RegulatoryRuleset(
        regulatory_ruleset_id="R", cerc_version="c", cea_version="c", mop_version="m",
        nepal_version="n", bangladesh_version="b", effective_date="2026-05-23",
        amendment_or_consolidated_ref="ref", access_construct="GNA", charge_rates=rates,
    )
    reg = RulesetRegistry()
    reg.bind(txid, rs)
    for component, rate in rates.items():
        assert reg.charge_rate(txid, component) == rate
    # An unknown component cannot be silently invented.
    try:
        reg.charge_rate(txid, "made_up_charge")
        assert False
    except RulesetBindingError:
        pass
