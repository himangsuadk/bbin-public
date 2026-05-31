"""Settlement, credit and audit (Task 25).

Meter-truth reconciliation with discrepancy holds (Req 3.3, 3.4, 9.5), class
0.2S meter acceptance (Req 9.4), curtailment penalty-neutrality (Req 8.7),
append-only ledger/audit (Req 23.4, 23.5, 24.4, 27.2), bounded performance fee
(Req 31.5) and credit-coverage gating (Req 31.4).

Properties: 45, 46, 50, 51, 53, 56, 57
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Meter accuracy class (Property 46)
# ---------------------------------------------------------------------------


_ACCEPTABLE_METER_CLASSES = {"0.2S", "0.2", "AGREED_EQUIVALENT"}


def meter_class_accepted(accuracy_class: str) -> bool:
    """Cross-boundary commercial meters require class 0.2S or agreed equivalent (Req 9.4)."""
    return accuracy_class.strip().upper().replace(" ", "") in {
        c.upper() for c in _ACCEPTABLE_METER_CLASSES
    }


# ---------------------------------------------------------------------------
# Meter divergence / discrepancy holds (Property 45)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MeterTriplet:
    main_mwh: float
    check_mwh: float
    standby_mwh: float
    signature_valid: bool = True
    calibration_valid: bool = True


@dataclass
class MeterAssessment:
    accepted: bool
    invoice_held: bool
    discrepancy_case: bool
    reason: str = ""


def assess_meter(triplet: MeterTriplet, tolerance_mwh: float) -> MeterAssessment:
    """Exclude failed-validation extracts (raise case); hold invoices on
    main/check/standby divergence beyond tolerance (Property 45)."""
    if not triplet.signature_valid or not triplet.calibration_valid:
        return MeterAssessment(accepted=False, invoice_held=True,
                               discrepancy_case=True, reason="validation_failed")
    readings = (triplet.main_mwh, triplet.check_mwh, triplet.standby_mwh)
    spread = max(readings) - min(readings)
    if spread > tolerance_mwh:
        return MeterAssessment(accepted=False, invoice_held=True,
                               discrepancy_case=True, reason="divergence")
    return MeterAssessment(accepted=True, invoice_held=False, discrepancy_case=False)


# ---------------------------------------------------------------------------
# Curtailment penalty-neutrality (Property 50)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CurtailmentInstruction:
    corridor_id: str
    pre_mw: float
    post_mw: float
    reason_category: str


@dataclass
class CurtailmentOutcome:
    preserved_instruction: CurtailmentInstruction
    reallocated_mw: float
    operator_curtailment_penalty: float


def apply_curtailment(instr: CurtailmentInstruction,
                      contract_allocates_penalty: bool,
                      contract_penalty_rate: float = 0.0) -> CurtailmentOutcome:
    """Preserve the operator instruction unchanged; reallocate contract volume;
    no operator-curtailment penalty unless the contract expressly allocates one
    (Property 50, Req 8.7)."""
    reduction = max(0.0, instr.pre_mw - instr.post_mw)
    penalty = (contract_penalty_rate * reduction) if contract_allocates_penalty else 0.0
    return CurtailmentOutcome(
        preserved_instruction=instr,
        reallocated_mw=instr.post_mw,
        operator_curtailment_penalty=penalty,
    )


# ---------------------------------------------------------------------------
# Append-only ledger + audit (Properties 51, 53)
# ---------------------------------------------------------------------------


class AppendOnlyViolation(RuntimeError):
    pass


@dataclass(frozen=True)
class LedgerEntry:
    entry_id: str
    contract_id: str
    block: str
    energy_mwh: float
    kind: str  # "SETTLEMENT" | "ADJUSTMENT"
    adjustment_case_id: Optional[str] = None


class SettlementLedger:
    """Append-only. Existing entries are never modified or deleted; settlement
    adjustments occur only through explicit adjustment-case entries (Property 51)."""

    def __init__(self) -> None:
        self._entries: list[LedgerEntry] = []
        self._ids: set[str] = set()

    def append(self, entry: LedgerEntry) -> None:
        if entry.entry_id in self._ids:
            raise AppendOnlyViolation(f"duplicate ledger entry {entry.entry_id}")
        if entry.kind == "ADJUSTMENT" and not entry.adjustment_case_id:
            raise AppendOnlyViolation("adjustment requires an explicit case id")
        self._entries.append(entry)
        self._ids.add(entry.entry_id)

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def entries(self) -> tuple[LedgerEntry, ...]:
        return tuple(self._entries)


@dataclass(frozen=True)
class AuditEntry:
    action: str
    actor: str
    timestamp_utc: str
    input_hashes: tuple[str, ...]
    model_version: str


class AuditJournal:
    """Append-only WORM journal. Every auditable action produces exactly one
    record (Property 53, Req 23.4)."""

    def __init__(self) -> None:
        self._records: list[AuditEntry] = []

    def record(self, entry: AuditEntry) -> None:
        self._records.append(entry)

    def __len__(self) -> int:
        return len(self._records)

    @property
    def records(self) -> tuple[AuditEntry, ...]:
        return tuple(self._records)


# ---------------------------------------------------------------------------
# Credit coverage gating (Property 57)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CreditProfile:
    counterparty: str
    exposure: float
    credit_limit: float
    lc_or_guarantee_or_escrow_valid: bool


def bilateral_automation_enabled(profile: CreditProfile) -> bool:
    """Bilateral settlement automation requires valid coverage and exposure within
    the counterparty credit limit (Property 57, Req 31.4)."""
    return (
        profile.lc_or_guarantee_or_escrow_valid
        and profile.exposure <= profile.credit_limit
    )


# ---------------------------------------------------------------------------
# Performance fee (Property 56)
# ---------------------------------------------------------------------------


def performance_fee(actual: float, benchmark: float, excluded_effects: float,
                    rate: float, cap: float) -> float:
    """VIR = max(0, actual - benchmark - excluded_effects); fee = min(rate*VIR, cap),
    never negative and never above the cap (Property 56, Req 31.5)."""
    vir = max(0.0, actual - benchmark - excluded_effects)
    return min(rate * vir, cap)
