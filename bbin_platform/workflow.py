"""Maker-checker approval workflow state machine (Task 23.6).

Implements the recommendation lifecycle (Req 24.1), distinct maker/checker
authorization (Req 24.2), and invalidation on any adverse pre-transmission event
(Req 24.3, 29.2). Also models the schedule lifecycle for transition validation.

Properties: 6, 49
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Recommendation workflow
# ---------------------------------------------------------------------------


class WState(str, Enum):
    DRAFT_RECOMMENDATION = "DRAFT_RECOMMENDATION"
    COMPLIANCE_VALIDATED = "COMPLIANCE_VALIDATED"
    MAKER_APPROVED = "MAKER_APPROVED"
    CHECKER_APPROVED = "CHECKER_APPROVED"
    TRANSMITTED_TO_TRADER = "TRANSMITTED_TO_TRADER"
    COUNTERPARTY_ACKNOWLEDGED = "COUNTERPARTY_ACKNOWLEDGED"
    INVALIDATED = "INVALIDATED"


# Legal transitions for the recommendation lifecycle.
_REC_TRANSITIONS: dict[WState, set[WState]] = {
    WState.DRAFT_RECOMMENDATION: {WState.COMPLIANCE_VALIDATED, WState.INVALIDATED},
    WState.COMPLIANCE_VALIDATED: {WState.MAKER_APPROVED, WState.INVALIDATED},
    WState.MAKER_APPROVED: {WState.CHECKER_APPROVED, WState.INVALIDATED},
    WState.CHECKER_APPROVED: {WState.TRANSMITTED_TO_TRADER, WState.INVALIDATED},
    WState.TRANSMITTED_TO_TRADER: {WState.COUNTERPARTY_ACKNOWLEDGED},
    WState.COUNTERPARTY_ACKNOWLEDGED: set(),
    WState.INVALIDATED: set(),
}

# Events that invalidate a not-yet-transmitted recommendation (Req 24.3, 29.2).
ADVERSE_EVENTS = {"ATC_REDUCED", "APPROVAL_EXPIRED", "SECURITY_FAILED", "OPERATOR_INSTRUCTION"}


class IllegalTransition(RuntimeError):
    pass


@dataclass
class RecommendationWorkflow:
    recommendation_id: str
    state: WState = WState.DRAFT_RECOMMENDATION
    maker: Optional[str] = None
    checker: Optional[str] = None
    history: list[WState] = field(default_factory=list)

    def _transition(self, target: WState) -> None:
        if target not in _REC_TRANSITIONS[self.state]:
            raise IllegalTransition(f"{self.state.value} -> {target.value}")
        self.history.append(self.state)
        self.state = target

    def validate_compliance(self) -> None:
        self._transition(WState.COMPLIANCE_VALIDATED)

    def maker_approve(self, actor: str) -> None:
        self._transition(WState.MAKER_APPROVED)
        self.maker = actor

    def checker_approve(self, actor: str) -> None:
        if actor == self.maker:
            # separation of duties (Req 24.2)
            raise IllegalTransition("checker must differ from maker")
        self._transition(WState.CHECKER_APPROVED)
        self.checker = actor

    def transmit(self) -> None:
        if self.maker is None or self.checker is None or self.maker == self.checker:
            raise IllegalTransition("transmit requires distinct maker and checker")
        self._transition(WState.TRANSMITTED_TO_TRADER)

    def acknowledge(self) -> None:
        self._transition(WState.COUNTERPARTY_ACKNOWLEDGED)

    def apply_event(self, event: str) -> None:
        """Apply an external event. Adverse events before transmission invalidate."""
        if event in ADVERSE_EVENTS:
            if self.state in (WState.TRANSMITTED_TO_TRADER,
                              WState.COUNTERPARTY_ACKNOWLEDGED):
                return  # already transmitted; handled downstream, not invalidated here
            self.history.append(self.state)
            self.state = WState.INVALIDATED

    @property
    def transmitted(self) -> bool:
        return self.state in (WState.TRANSMITTED_TO_TRADER,
                              WState.COUNTERPARTY_ACKNOWLEDGED)


# ---------------------------------------------------------------------------
# Schedule lifecycle (for Property 49 schedule-transition validation)
# ---------------------------------------------------------------------------


class SState(str, Enum):
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    ACCEPTED = "ACCEPTED"
    CURTAILED = "CURTAILED"
    SUPERSEDED = "SUPERSEDED"


_SCHED_TRANSITIONS: dict[SState, set[SState]] = {
    SState.SUBMITTED: {SState.ACKNOWLEDGED, SState.SUPERSEDED},
    SState.ACKNOWLEDGED: {SState.ACCEPTED, SState.SUPERSEDED},
    SState.ACCEPTED: {SState.CURTAILED, SState.SUPERSEDED},
    SState.CURTAILED: {SState.SUPERSEDED},
    SState.SUPERSEDED: set(),
}


def schedule_transition_legal(src: SState, dst: SState) -> bool:
    return dst in _SCHED_TRANSITIONS[src]
