"""Compliance engine: seven sequential approval gates and ruleset control (Task 23).

Implements the transaction approval/access workflow gates (Req 10.1-10.7), the
advisory-until-authorized classification (Req 2.1), ruleset amendment holds
(Req 5.3), GNA/T-GNA enforcement with legacy preservation (Req 5.5), four-eye
approval activation (Req 7.2) and execution-confirmation matching (Req 7.7).

Properties: 3, 5, 13, 15, 47, 48
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from .schemas import AccessConstruct


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------


class GateName(str, Enum):
    NEPAL_PERMISSION = "nepal_permission"
    DA_APPROVAL = "da_approval"
    LICENSED_TRADER = "licensed_trader"
    CONNECTIVITY_ACCESS = "connectivity_access"
    CBTL_READINESS = "cbtl_readiness"
    SYSTEM_OPERATION_ATC = "system_operation_atc"
    SETTLEMENT = "settlement"


# The seven gates in their mandatory sequence.
GATE_SEQUENCE: tuple[GateName, ...] = (
    GateName.NEPAL_PERMISSION,
    GateName.DA_APPROVAL,
    GateName.LICENSED_TRADER,
    GateName.CONNECTIVITY_ACCESS,
    GateName.CBTL_READINESS,
    GateName.SYSTEM_OPERATION_ATC,
    GateName.SETTLEMENT,
)


@dataclass(frozen=True)
class Approval:
    """A single approval covering a project/corridor/quantum for a validity window."""

    gate: GateName
    project_id: str
    corridor_id: str
    approved_mw: float
    valid_from_utc: str
    valid_to_utc: str
    four_eye_validated: bool = True  # gate-2 (trader approval) requires this to activate
    superseded: bool = False

    def active_at(self, when: str) -> bool:
        if self.superseded:
            return False
        t = _parse(when)
        return _parse(self.valid_from_utc) <= t < _parse(self.valid_to_utc)

    def covers(self, project_id: str, corridor_id: str, requested_mw: float) -> bool:
        return (
            self.project_id == project_id
            and self.corridor_id == corridor_id
            and requested_mw <= self.approved_mw + 1e-9
        )


@dataclass(frozen=True)
class Transaction:
    transaction_id: str
    project_id: str
    corridor_id: str
    requested_mw: float
    delivery_time_utc: str


@dataclass
class ComplianceDecision:
    permitted: bool
    blocking_gate: Optional[GateName] = None
    reason: str = ""
    checked_gates: list[GateName] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Compliance engine
# ---------------------------------------------------------------------------


class ComplianceEngine:
    """Validates a transaction against the seven sequential gates.

    Permits transmission iff every required gate has an approval that is present,
    active at delivery time, covers the transaction attributes, and (for the
    trader gate) is four-eye validated. Otherwise blocks and records the reason
    (Property 5).
    """

    def evaluate(self, txn: Transaction, approvals: list[Approval]) -> ComplianceDecision:
        checked: list[GateName] = []
        by_gate: dict[GateName, list[Approval]] = {}
        for a in approvals:
            by_gate.setdefault(a.gate, []).append(a)

        for gate in GATE_SEQUENCE:
            checked.append(gate)
            candidates = by_gate.get(gate, [])
            match = None
            for a in candidates:
                if not a.active_at(txn.delivery_time_utc):
                    continue
                if not a.covers(txn.project_id, txn.corridor_id, txn.requested_mw):
                    continue
                if gate == GateName.DA_APPROVAL and not a.four_eye_validated:
                    # approval not yet active until four-eye validation (Req 7.2)
                    continue
                match = a
                break
            if match is None:
                return ComplianceDecision(
                    permitted=False, blocking_gate=gate,
                    reason=f"gate_failed:{gate.value}", checked_gates=checked)

        return ComplianceDecision(permitted=True, checked_gates=checked)


# ---------------------------------------------------------------------------
# Recommendation status (Property 3)
# ---------------------------------------------------------------------------


@dataclass
class Recommendation:
    recommendation_id: str
    transaction: Transaction
    authorized: bool = False  # flips only when an authorization event is recorded

    @property
    def is_executable(self) -> bool:
        return self.authorized

    @property
    def status(self) -> str:
        return "EXECUTABLE" if self.authorized else "ADVISORY"


# ---------------------------------------------------------------------------
# Ruleset amendment hold (Property 13)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RulesetAmendment:
    ruleset_id: str
    effective_from_utc: str
    effective_to_utc: str
    review_promoted: bool = False


def submission_is_held(amendment: RulesetAmendment,
                       delivery_from_utc: str, delivery_to_utc: str) -> bool:
    """A future submission is held iff an unpromoted amendment's effective window
    overlaps the contracted delivery period (Property 13, Req 5.3)."""
    if amendment.review_promoted:
        return False
    a0, a1 = _parse(amendment.effective_from_utc), _parse(amendment.effective_to_utc)
    d0, d1 = _parse(delivery_from_utc), _parse(delivery_to_utc)
    overlaps = a0 < d1 and d0 < a1
    return overlaps


# ---------------------------------------------------------------------------
# GNA/T-GNA enforcement (Property 15)
# ---------------------------------------------------------------------------


def validate_access_construct(construct: AccessConstruct, is_new_config: bool) -> bool:
    """A newly configured production transaction must use GNA or T-GNA (Req 5.5).
    Legacy constructs are accepted only as historic (not-new) evidence."""
    if is_new_config:
        return construct.is_current
    return True  # historic records of any construct are retained as evidence


# ---------------------------------------------------------------------------
# Execution-confirmation matching (Property 47)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionConfirmation:
    exchange_txn_id: str
    recommendation_id: str
    approval_project_id: str
    approval_corridor_id: str
    cleared_mw: float


def confirmation_matches(conf: ExecutionConfirmation,
                         recommendations: dict[str, Recommendation],
                         approvals: list[Approval]) -> bool:
    """Accept a confirmation iff it matches an originating recommendation and an
    active covering approval (Property 47, Req 7.7)."""
    rec = recommendations.get(conf.recommendation_id)
    if rec is None:
        return False
    for a in approvals:
        if (a.gate == GateName.DA_APPROVAL
                and a.project_id == conf.approval_project_id
                and a.corridor_id == conf.approval_corridor_id
                and a.active_at(rec.transaction.delivery_time_utc)
                and a.four_eye_validated
                and conf.cleared_mw <= a.approved_mw + 1e-9):
            return True
    return False
