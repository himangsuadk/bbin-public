"""Adapter-core ingress pipeline (Task 3).

Every adapter applies this common pipeline before publishing to an operational
topic:

    validate envelope -> verify signature -> check sequence -> dedup -> classify plane

Signature failures route to quarantine and are *never* placed on an operational
topic (Property 21). Sequence gaps raise an alert + audit record (Property 17).
Replay/duplication is idempotent (Property 18).

Requirements: 6.7, 6.8, 6.9, 11.2, 19.4, 20.5, 28.1
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .schemas import REQUIRED_ENVELOPE_FIELDS, GatewayEnvelope, Plane


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    missing_fields: tuple[str, ...] = ()
    reason: str = ""


@dataclass
class AuditRecord:
    """A single append-only audit entry (Property 53)."""

    action: str
    actor: str
    detail: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Envelope validation (Property 16)
# ---------------------------------------------------------------------------


def validate_envelope(env: GatewayEnvelope) -> ValidationResult:
    """An envelope is valid iff every required field is present and non-empty,
    and the payload hash matches the declared digest.

    Requirements: 6.7
    """
    missing: list[str] = []
    for f in REQUIRED_ENVELOPE_FIELDS:
        value = getattr(env, f, None)
        if value is None or (isinstance(value, str) and value.strip() == ""):
            missing.append(f)
    if missing:
        return ValidationResult(ok=False, missing_fields=tuple(missing),
                                reason="missing_required_fields")
    if not _payload_hash_matches(env):
        return ValidationResult(ok=False, reason="payload_hash_mismatch")
    return ValidationResult(ok=True)


def compute_payload_sha256(payload: dict[str, Any]) -> str:
    import json

    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _payload_hash_matches(env: GatewayEnvelope) -> bool:
    return compute_payload_sha256(env.payload) == env.payload_sha256


# ---------------------------------------------------------------------------
# Signature verification (Property 21, 22)
# ---------------------------------------------------------------------------


class SignatureVerifier:
    """Verifies a detached HMAC signature over the payload digest.

    In production this is replaced by the NEA/counterparty PKI; the HMAC stand-in
    preserves the *property* (valid iff content matches and key is correct) so the
    quarantine logic is testable without a real KMS.
    """

    def __init__(self, keys: dict[str, bytes]) -> None:
        self._keys = dict(keys)

    def sign(self, signature_ref: str, payload_sha256: str) -> str:
        key = self._keys[signature_ref]
        return hmac.new(key, payload_sha256.encode("utf-8"), hashlib.sha256).hexdigest()

    def verify(self, signature_ref: str, payload_sha256: str, signature: str) -> bool:
        key = self._keys.get(signature_ref)
        if key is None:
            return False
        expected = hmac.new(key, payload_sha256.encode("utf-8"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# Sequence-gap detection (Property 17)
# ---------------------------------------------------------------------------


class SequenceTracker:
    """Detects gaps in per-(source_system, dataset) sequence numbers."""

    def __init__(self) -> None:
        self._last: dict[tuple[str, str], int] = {}

    def observe(self, source_system: str, dataset: str, sequence: int) -> list[int]:
        """Return the list of missing sequence numbers introduced by this observation."""
        key = (source_system, dataset)
        last = self._last.get(key)
        missing: list[int] = []
        if last is not None and sequence > last + 1:
            missing = list(range(last + 1, sequence))
        if last is None or sequence > last:
            self._last[key] = sequence
        return missing


# ---------------------------------------------------------------------------
# Idempotent dedup (Property 18)
# ---------------------------------------------------------------------------


class Deduplicator:
    """Idempotent by message_id / event_id. Replays and duplicates are no-ops."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def is_new(self, message_id: str) -> bool:
        if message_id in self._seen:
            return False
        self._seen.add(message_id)
        return True

    def seen(self, message_id: str) -> bool:
        return message_id in self._seen


# ---------------------------------------------------------------------------
# Plane classification
# ---------------------------------------------------------------------------


_RESTRICTED_DATASETS = {
    "cross_border_schedule",
    "interface_meter_extract",
    "equipment_state",
    "atc_view",
    "transfer_capability_declaration",
    "schedule_submission",
    "schedule_acceptance",
    "curtailment_instruction",
}


def classify_plane(dataset: str) -> Plane:
    if dataset in _RESTRICTED_DATASETS:
        return Plane.RESTRICTED
    return Plane.READ


# ---------------------------------------------------------------------------
# Ingress pipeline
# ---------------------------------------------------------------------------


@dataclass
class IngressOutcome:
    published: bool
    topic: Optional[str]
    quarantined: bool
    duplicate: bool
    missing_sequences: tuple[int, ...]
    audit: list[AuditRecord] = field(default_factory=list)
    reason: str = ""


class AdapterCore:
    """Common ingress applied by every adapter."""

    def __init__(self, verifier: SignatureVerifier,
                 publisher: Optional[Callable[[str, GatewayEnvelope], None]] = None) -> None:
        self._verifier = verifier
        self._seq = SequenceTracker()
        self._dedup = Deduplicator()
        self._publisher = publisher or (lambda topic, env: None)
        self.quarantine: list[GatewayEnvelope] = []
        self.audit_log: list[AuditRecord] = []
        self.alerts: list[str] = []

    def ingest(self, env: GatewayEnvelope, signature: str, actor: str = "gateway") -> IngressOutcome:
        audit: list[AuditRecord] = []

        # 1. Structural validation.
        vr = validate_envelope(env)
        if not vr.ok:
            self.quarantine.append(env)
            rec = AuditRecord("ingest_rejected", actor,
                              {"message_id": env.message_id, "reason": vr.reason})
            self.audit_log.append(rec)
            audit.append(rec)
            return IngressOutcome(False, None, True, False, (), audit, vr.reason)

        # 2. Signature verification — failures quarantine, never operationalized.
        if not self._verifier.verify(env.signature_ref, env.payload_sha256, signature):
            self.quarantine.append(env)
            rec = AuditRecord("signature_failed", actor,
                              {"message_id": env.message_id})
            self.audit_log.append(rec)
            audit.append(rec)
            return IngressOutcome(False, None, True, False, (), audit, "signature_failed")

        # 3. Dedup — replays/duplicates are idempotent no-ops.
        if not self._dedup.is_new(env.message_id):
            rec = AuditRecord("duplicate_ignored", actor, {"message_id": env.message_id})
            self.audit_log.append(rec)
            audit.append(rec)
            return IngressOutcome(False, None, False, True, (), audit, "duplicate")

        # 4. Sequence-gap detection (only when a sequence is present).
        missing: list[int] = []
        if env.sequence is not None:
            missing = self._seq.observe(env.source_system, env.dataset, env.sequence)
            if missing:
                alert = f"sequence_gap:{env.source_system}:{env.dataset}:{missing}"
                self.alerts.append(alert)
                rec = AuditRecord("sequence_gap", actor,
                                  {"source_system": env.source_system,
                                   "dataset": env.dataset, "missing": missing})
                self.audit_log.append(rec)
                audit.append(rec)

        # 5. Classify plane and publish.
        plane = classify_plane(env.dataset)
        topic = f"{plane.value.lower()}.{env.dataset}.v1"
        self._publisher(topic, env)
        rec = AuditRecord("published", actor,
                          {"message_id": env.message_id, "topic": topic})
        self.audit_log.append(rec)
        audit.append(rec)
        return IngressOutcome(True, topic, False, False, tuple(missing), audit)
