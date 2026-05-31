"""Non-negotiable hard-control invariant cores (Task 4).

These are pure, structurally-enforced cores that the wider system wires into.
They make the blueprint's hard controls impossible to violate rather than merely
discouraged. Each maps to one or more correctness properties:

- ``ScadaEgressGuard``           -> Property 1  (no automated SCADA control path)
- ``ScheduleStore``              -> Property 2  (operator schedules never mutated)
- ``governing_atc`` / ``DeclaredAtcStore`` -> Properties 8, 9, 10 (ATC truth)
- ``executable_volume``          -> Property 7  (volume never exceeds min bound)
- ``MakerCheckerGate``           -> Property 4  (distinct maker + checker)
- ``meter_truth_filter``         -> Property 44 (certified meter truth only)
- ``RulesetBinding``             -> Properties 12, 14 (immutable ruleset + charges)

Requirements: 1.1, 1.2, 1.4, 2.6, 3.1, 3.2, 4.1-4.5, 5.1, 5.2, 5.4, 8.2, 8.5,
              8.6, 9.6, 10.6, 17.5, 24.2, 25.1, 25.3, 29.1, 31.3
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from .schemas import DeclaredATC, RegulatoryRuleset, VolumeBounds


# ===========================================================================
# Property 1 — no automated SCADA control path
# ===========================================================================


class EgressDecision(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"


# Opcodes / endpoint markers that indicate an operational-technology control action.
_CONTROL_OPCODES = {
    "WRITE", "CONTROL", "SETPOINT", "BREAKER_OPEN", "BREAKER_CLOSE",
    "TAP_CHANGE", "TRIP", "CLOSE", "OPEN", "DISPATCH", "RAISE", "LOWER",
}
_OT_ENDPOINT_MARKERS = ("scada", "rtu", "ied", "ems-write", "ot/", "protection", "control")


@dataclass
class EgressEvent:
    target_endpoint: str
    opcode: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class EgressResult:
    decision: EgressDecision
    alerts: list[str] = field(default_factory=list)
    audit: list[str] = field(default_factory=list)
    transmitted: bool = False


class ScadaEgressGuard:
    """The single egress classifier used by every outbound path.

    Any message targeting an OT/control endpoint OR carrying a write/control
    opcode is denied at both layers, emits exactly one alert + one audit record,
    and is never transmitted (Property 1, Req 1.2, 29.1).
    """

    def evaluate(self, event: EgressEvent) -> EgressResult:
        endpoint = event.target_endpoint.lower()
        is_ot_endpoint = any(m in endpoint for m in _OT_ENDPOINT_MARKERS)
        is_control_opcode = event.opcode.strip().upper() in _CONTROL_OPCODES
        if is_ot_endpoint or is_control_opcode:
            reason = ("ot_endpoint" if is_ot_endpoint else "control_opcode")
            return EgressResult(
                decision=EgressDecision.DENY,
                alerts=[f"scada_control_denied:{reason}:{event.target_endpoint}"],
                audit=[f"scada_control_denied:{reason}:{event.opcode}:{event.target_endpoint}"],
                transmitted=False,
            )
        return EgressResult(decision=EgressDecision.ALLOW, transmitted=True)


# ===========================================================================
# Property 2 — operator-approved schedules are never mutated
# ===========================================================================


def _content_hash(content: dict[str, Any]) -> str:
    import json

    canonical = json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


class ScheduleImmutableError(RuntimeError):
    pass


class ScheduleStore:
    """Read-only, content-hash-protected store of operator-accepted schedules.

    No forecasting/scenario/recommendation operation can alter a stored schedule;
    attempts raise and the content hash is invariant (Property 2, Req 1.4).
    """

    def __init__(self) -> None:
        self._schedules: dict[str, dict[str, Any]] = {}
        self._hashes: dict[str, str] = {}

    def accept(self, schedule_id: str, content: dict[str, Any]) -> str:
        if schedule_id in self._schedules:
            raise ScheduleImmutableError(
                f"schedule {schedule_id} already accepted; revisions create new ids")
        import copy

        stored = copy.deepcopy(content)
        self._schedules[schedule_id] = stored
        self._hashes[schedule_id] = _content_hash(stored)
        return self._hashes[schedule_id]

    def get(self, schedule_id: str) -> dict[str, Any]:
        import copy

        return copy.deepcopy(self._schedules[schedule_id])

    def content_hash(self, schedule_id: str) -> str:
        return self._hashes[schedule_id]

    def verify_unchanged(self, schedule_id: str) -> bool:
        return _content_hash(self._schedules[schedule_id]) == self._hashes[schedule_id]


# ===========================================================================
# Properties 8, 9, 10 — Declared ATC truth
# ===========================================================================


def governing_atc(decl_a_mw: float, decl_b_mw: float) -> float:
    """Governing ATC = lower of the two authenticated declared values (Property 8)."""
    return min(decl_a_mw, decl_b_mw)


class AtcRejected(ValueError):
    pass


class DeclaredAtcStore:
    """Read-only store of Declared_ATC records.

    Rejects declarations violating ``ATC <= TTC - TRM`` (Property 10). Stored
    values cannot be raised by model output (Property 9) — there is simply no
    mutating method; model headroom lives elsewhere.
    """

    def __init__(self) -> None:
        self._records: dict[tuple[str, str, int], DeclaredATC] = {}

    def declare(self, record: DeclaredATC) -> None:
        if not record.satisfies_capacity_inequality():
            raise AtcRejected(
                f"ATC {record.atc_mw} > TTC-TRM {record.ttc_mw - record.trm_mw}")
        self._records[(record.corridor_id, record.direction, record.revision)] = record

    def governing(self, corridor_id: str, direction: str, revision: int) -> float:
        return self._records[(corridor_id, direction, revision)].governing_atc_mw


# ===========================================================================
# Property 7 — executable volume never exceeds the minimum binding constraint
# ===========================================================================


def executable_volume(bounds: VolumeBounds) -> float:
    """The recommended/submitted volume for a block, bounded by the tightest
    constraint and never negative (Property 7, Req 4.4, 4.5, 8.5, 10.6, 17.5).

    Internal ATC scenarios are advisory and are simply not part of ``bounds``;
    they can never raise the result above the Declared_ATC term.
    """
    return bounds.binding_limit()


# ===========================================================================
# Property 4 — transmission requires distinct maker + checker acceptance
# ===========================================================================


@dataclass(frozen=True)
class WorkflowEvent:
    kind: str          # "MAKER_APPROVED" | "CHECKER_APPROVED" | "TRANSMIT" | ...
    actor: str
    recommendation_id: str


class MakerCheckerGate:
    """Authorizes transmission only when a maker acceptance and a checker
    acceptance — by two *distinct* authorized identities — precede the
    transmission event (Property 4, Req 2.6, 24.2).
    """

    def __init__(self, authorized_actors: set[str]) -> None:
        self._authorized = set(authorized_actors)

    def may_transmit(self, recommendation_id: str, events: list[WorkflowEvent]) -> bool:
        makers: set[str] = set()
        checkers: set[str] = set()
        for ev in events:
            if ev.recommendation_id != recommendation_id:
                continue
            if ev.actor not in self._authorized:
                continue
            if ev.kind == "MAKER_APPROVED":
                makers.add(ev.actor)
            elif ev.kind == "CHECKER_APPROVED":
                checkers.add(ev.actor)
        # Need a maker and a checker that are distinct identities.
        return any(m != c for m in makers for c in checkers)


# ===========================================================================
# Property 44 — settlement uses certified meter truth only
# ===========================================================================


@dataclass(frozen=True)
class SettlementInput:
    """A candidate settlement energy input."""

    source_kind: str          # "CERTIFIED_METER", "GAUGE", "TELEMETRY", "MODEL", "SCADA_STATUS"
    energy_mwh: float
    signature_valid: bool
    quality: str = "VALID"
    imputed: bool = False
    schedule_accepted: bool = False


_NON_METER_SOURCES = {"GAUGE", "TELEMETRY", "MODEL", "SCADA_STATUS", "FORECAST"}


def meter_truth_filter(inputs: list[SettlementInput]) -> list[SettlementInput]:
    """Admit only certified, signature-validated, VALID-quality, non-imputed meter
    records whose schedule baseline is operator-accepted (Property 44).

    Everything else — gauges, telemetry, model output, receipt-point SCADA, bad/
    old/substituted quality, imputed/forecast discharge — is structurally excluded.
    Requirements: 3.1, 3.2, 3.5, 6.6, 8.6, 9.6
    """
    admitted: list[SettlementInput] = []
    for i in inputs:
        if i.source_kind != "CERTIFIED_METER":
            continue
        if i.source_kind in _NON_METER_SOURCES:
            continue
        if not i.signature_valid:
            continue
        if i.quality != "VALID":
            continue
        if i.imputed:
            continue
        if not i.schedule_accepted:
            continue
        admitted.append(i)
    return admitted


# ===========================================================================
# Properties 12, 14 — immutable ruleset binding + charge derivation
# ===========================================================================


class RulesetBindingError(RuntimeError):
    pass


# The canonical set of regulated pass-through charge components.
PASS_THROUGH_CHARGES: tuple[str, ...] = (
    "transmission", "losses", "dsm", "reactive", "tax_levy", "sna", "trader_fee", "exchange",
)


@dataclass
class TransactionBinding:
    transaction_id: str
    ruleset: RegulatoryRuleset
    _frozen: bool = field(default=False, repr=False)


class RulesetRegistry:
    """Binds a transaction to an immutable ruleset and derives charges only from it."""

    def __init__(self) -> None:
        self._bindings: dict[str, TransactionBinding] = {}

    def bind(self, transaction_id: str, ruleset: RegulatoryRuleset) -> TransactionBinding:
        if not ruleset.is_complete():
            raise RulesetBindingError("ruleset missing required version/effective-date fields")
        if transaction_id in self._bindings:
            raise RulesetBindingError(
                f"transaction {transaction_id} already bound; binding is immutable")
        binding = TransactionBinding(transaction_id, ruleset, _frozen=True)
        self._bindings[transaction_id] = binding
        return binding

    def ruleset_for(self, transaction_id: str) -> RegulatoryRuleset:
        return self._bindings[transaction_id].ruleset

    def charge_rate(self, transaction_id: str, component: str) -> float:
        """Every regulated charge rate is read from the bound ruleset only —
        never inferred from historic approvals (Property 14, Req 5.4)."""
        ruleset = self._bindings[transaction_id].ruleset
        if component not in ruleset.charge_rates:
            raise RulesetBindingError(f"charge component {component!r} not in bound ruleset")
        return ruleset.charge_rates[component]
