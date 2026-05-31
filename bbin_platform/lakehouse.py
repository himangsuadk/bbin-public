"""Medallion lakehouse transforms (Tasks 13-15).

Bronze append-only with quarantine/corrections (Property 43), rating-curve
versioned discharge (Property 24), physical caps/non-negativity (Property 25),
ATC revision windowing (Property 11), leakage-safe features (Property 31),
lineage completeness (Property 52).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .schemas import LineageId


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Bronze append-only (Property 43)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BronzeRecord:
    record_id: str
    raw_bytes: str
    signature_outcome: str  # "VALID" | "INVALID"
    arrival_metadata: dict
    is_correction: bool = False
    corrects_id: Optional[str] = None


class BronzeStore:
    """Append-only. Valid records (even with suspect measurements) are appended
    with their validation outcome; corrections are distinct records; invalid
    signatures go to quarantine; nothing is overwritten/removed (Property 43)."""

    def __init__(self) -> None:
        self._records: list[BronzeRecord] = []
        self._ids: set[str] = set()
        self.quarantine: list[BronzeRecord] = []

    def append(self, rec: BronzeRecord) -> None:
        if rec.signature_outcome != "VALID":
            self.quarantine.append(rec)
            return
        if rec.record_id in self._ids:
            raise RuntimeError(f"bronze is append-only; duplicate {rec.record_id}")
        self._records.append(rec)
        self._ids.add(rec.record_id)

    @property
    def records(self) -> tuple[BronzeRecord, ...]:
        return tuple(self._records)

    def __len__(self) -> int:
        return len(self._records)


# ---------------------------------------------------------------------------
# Rating-curve versioned discharge (Property 24)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RatingCurveVersion:
    version_id: str
    effective_from_utc: str
    effective_to_utc: str
    # discharge = a * stage**b
    a: float
    b: float

    def contains(self, when: str) -> bool:
        t = _parse(when)
        return _parse(self.effective_from_utc) <= t < _parse(self.effective_to_utc)

    def discharge(self, stage_m: float) -> float:
        return self.a * (max(0.0, stage_m) ** self.b)


@dataclass
class DischargeResult:
    discharge_m3s: float
    rating_curve_version: str


class RatingCurveHistory:
    """Holds versioned rating curves; converts using the version effective at the
    observation time and records which version produced each value (Property 24)."""

    def __init__(self, versions: list[RatingCurveVersion]) -> None:
        self._versions = list(versions)

    def convert(self, stage_m: float, observed_at_utc: str) -> DischargeResult:
        for v in self._versions:
            if v.contains(observed_at_utc):
                return DischargeResult(v.discharge(stage_m), v.version_id)
        raise LookupError(f"no rating curve effective at {observed_at_utc}")


# ---------------------------------------------------------------------------
# Physical caps / non-negativity (Property 25)
# ---------------------------------------------------------------------------


def effective_discharge(raw_discharge_m3s: float, turbine_design_flow_m3s: float) -> float:
    """Cap effective discharge at the turbine design flow; never negative (Property 25)."""
    return min(max(0.0, raw_discharge_m3s), turbine_design_flow_m3s)


def generation_mw(eff_discharge_m3s: float, head_m: float, efficiency: float,
                  plant_capacity_mw: float) -> float:
    """P = rho*g*Q*H*eta, capped at plant capacity, non-negative (Property 25)."""
    rho, g = 1000.0, 9.81
    raw = rho * g * max(0.0, eff_discharge_m3s) * max(0.0, head_m) * efficiency / 1e6
    return min(max(0.0, raw), plant_capacity_mw)


# ---------------------------------------------------------------------------
# ATC revision windowing (Property 11)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AtcRevision:
    governing_mw: float
    effective_from_utc: str
    revision: int


def governing_for_block(block_start_utc: str, base: AtcRevision,
                        revision: Optional[AtcRevision]) -> float:
    """A revision affects only blocks at/after its effective time; earlier blocks
    keep the pre-revision value (Property 11)."""
    if revision is None:
        return base.governing_mw
    if _parse(block_start_utc) >= _parse(revision.effective_from_utc):
        return revision.governing_mw
    return base.governing_mw


# ---------------------------------------------------------------------------
# Leakage-safe features at bid time (Property 31)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FeatureInput:
    name: str
    available_at_utc: str
    is_same_block_indian_price: bool = False
    is_imerg_final_run: bool = False


def assemble_leakage_safe(inputs: list[FeatureInput], bid_decision_utc: str,
                          is_operational_backtest: bool = True
                          ) -> list[FeatureInput]:
    """Admit a feature input iff its data was available strictly before the bid
    decision time, it is not a same-block Indian price used for supply features,
    and (in an operational backtest) it is not IMERG Final/revised data (Property 31)."""
    t_bid = _parse(bid_decision_utc)
    safe = []
    for f in inputs:
        if _parse(f.available_at_utc) >= t_bid:
            continue
        if f.is_same_block_indian_price:
            continue
        if is_operational_backtest and f.is_imerg_final_run:
            continue
        safe.append(f)
    return safe


# ---------------------------------------------------------------------------
# Lineage completeness (Property 52)
# ---------------------------------------------------------------------------


def lineage_is_complete(lineage: LineageId, transacted: bool) -> bool:
    """Every Gold output carries a non-empty lineage_id and all required fields;
    transacted outputs additionally carry the human approval event id (Property 52)."""
    return lineage.is_complete(transacted=transacted)
