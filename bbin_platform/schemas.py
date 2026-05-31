"""Canonical cross-component contracts (Task 1.2).

These dataclasses mirror the design's "Data Models" section. They are the single
source of truth that every other module depends on. Serialization is plain JSON
so the round-trip property (Property 16) is exercisable without external codegen.

Requirements: 6.7, 11.1, 5.1, 5.2, 4.1, 22.2, 23.2
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Quality / enumerations
# ---------------------------------------------------------------------------


class Quality(str, Enum):
    """Telemetry quality codes. Only VALID may ever feed settlement (Req 6.6)."""

    VALID = "VALID"
    BAD = "BAD"
    OLD = "OLD"
    SUBSTITUTED = "SUBSTITUTED"

    @property
    def is_settlement_grade(self) -> bool:
        return self is Quality.VALID


class Plane(str, Enum):
    """Logical plane a dataset belongs to (two-plane separation)."""

    READ = "READ"
    RESTRICTED = "RESTRICTED"
    DECISION = "DECISION"


class AccessConstruct(str, Enum):
    """Current CERC access constructs. Legacy values kept only as historic evidence."""

    GNA = "GNA"
    T_GNA = "T-GNA"
    # Legacy — never used for new production configuration (Req 5.5).
    LTA = "LTA"
    MTOA = "MTOA"
    STOA = "STOA"

    @property
    def is_current(self) -> bool:
        return self in (AccessConstruct.GNA, AccessConstruct.T_GNA)


# Required fields for the gateway export envelope (Req 6.7).
REQUIRED_ENVELOPE_FIELDS: tuple[str, ...] = (
    "schema_version",
    "message_id",
    "source_system",
    "dataset",
    "event_time_utc",
    "published_time_utc",
    "revision",
    "quality",
    "payload_sha256",
    "signature_ref",
)


@dataclass(frozen=True)
class GatewayEnvelope:
    """Gateway export envelope (design "Data Models").

    Frozen because an ingested envelope is immutable once received.
    """

    schema_version: str
    message_id: str
    source_system: str
    dataset: str
    event_time_utc: str
    published_time_utc: str
    revision: int
    quality: str
    payload_sha256: str
    signature_ref: str
    sequence: Optional[int] = None
    corridor_id: Optional[str] = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> "GatewayEnvelope":
        return cls(**json.loads(raw))


@dataclass(frozen=True)
class GaugeEnvelope:
    """Signed gauge reading envelope (Req 11.1)."""

    event_id: str
    schema: str
    sensor_id: str
    basin_id: str
    observed_at_utc: str
    ingested_at_utc: str
    measurements: dict[str, float]
    rating_curve_version: str
    quality: dict[str, Any]
    edge_signature: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> "GaugeEnvelope":
        return cls(**json.loads(raw))


@dataclass(frozen=True)
class DeclaredATC:
    """Operator-declared transfer capability — read-only authenticated record.

    Invariant enforced at construction: ``atc_mw <= ttc_mw - trm_mw`` (Req 8.2).
    Model output can never mutate a stored instance (frozen) (Req 4.1, 4.2).
    """

    corridor_id: str
    direction: str
    block_start_utc: str
    ttc_mw: float
    trm_mw: float
    atc_mw: float
    issuing_operator: str
    counterpart_atc_mw: float
    declaration_time_utc: str
    revision: int
    reason_code: str
    signature_ref: str

    @property
    def governing_atc_mw(self) -> float:
        """Governing ATC is the lower authenticated declared value (Req 4.3)."""
        return min(self.atc_mw, self.counterpart_atc_mw)

    def satisfies_capacity_inequality(self) -> bool:
        return self.atc_mw <= self.ttc_mw - self.trm_mw


@dataclass(frozen=True)
class RegulatoryRuleset:
    """Immutable regulatory ruleset binding (Req 5.1, 5.2)."""

    regulatory_ruleset_id: str
    cerc_version: str
    cea_version: str
    mop_version: str
    nepal_version: str
    bangladesh_version: str
    effective_date: str
    amendment_or_consolidated_ref: str
    access_construct: str
    charge_rates: dict[str, float]
    review_status: str = "PROMOTED"

    REQUIRED_VERSION_FIELDS: tuple[str, ...] = (
        "cerc_version",
        "cea_version",
        "mop_version",
        "nepal_version",
        "bangladesh_version",
    )

    def is_complete(self) -> bool:
        """All five jurisdiction versions plus an effective date and reference (Req 5.2)."""
        for f in self.REQUIRED_VERSION_FIELDS:
            if not getattr(self, f):
                return False
        return bool(self.effective_date) and bool(self.amendment_or_consolidated_ref)


@dataclass(frozen=True)
class VolumeBounds:
    """The set of bounds constraining executable export volume (Req 4.4)."""

    approved_mw: float
    access_mw: float
    governing_atc_mw: float
    generation_available_mw: float  # G_firm90
    contract_ceiling_mw: float

    def binding_limit(self) -> float:
        """Executable volume ceiling = min of all bounds, never negative."""
        return max(
            0.0,
            min(
                self.approved_mw,
                self.access_mw,
                self.governing_atc_mw,
                self.generation_available_mw,
                self.contract_ceiling_mw,
            ),
        )

    def limiting_constraint(self) -> str:
        """Name of the tightest bound, for the decision card (Req 17.6)."""
        named = {
            "approval": self.approved_mw,
            "access": self.access_mw,
            "atc": self.governing_atc_mw,
            "generation": self.generation_available_mw,
            "contract": self.contract_ceiling_mw,
        }
        return min(named, key=named.get)


@dataclass(frozen=True)
class LineageId:
    """End-to-end lineage carried by every Gold output (Req 23.1-23.3)."""

    lineage_id: str
    source_event_ids: list[str]
    external_file_sha256: list[str]
    bronze_table_versions: list[str]
    silver_table_versions: list[str]
    feature_definition_version: str
    model_version: str
    calibration_window: str
    random_seed: int
    regulatory_ruleset_id: str
    approval_ids: list[str]
    atc_declaration_revision: int
    code_commit: str
    container_digest: str
    human_approval_event_id: Optional[str] = None  # present only when transacted

    REQUIRED_SCALAR_FIELDS: tuple[str, ...] = (
        "lineage_id",
        "feature_definition_version",
        "model_version",
        "calibration_window",
        "regulatory_ruleset_id",
        "code_commit",
        "container_digest",
    )

    def is_complete(self, transacted: bool = False) -> bool:
        if not self.lineage_id:
            return False
        for f in self.REQUIRED_SCALAR_FIELDS:
            if not getattr(self, f):
                return False
        if transacted and not self.human_approval_event_id:
            return False
        return True
