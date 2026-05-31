"""Property-based tests for the adapter-core ingress pipeline.

Each test implements exactly one design correctness property, runs >=100
iterations, and is tagged in the required format. Library: Python Hypothesis.
"""

from __future__ import annotations

from hypothesis import given, settings, strategies as st

from bbin_platform.schemas import GatewayEnvelope, REQUIRED_ENVELOPE_FIELDS
from bbin_platform.adapter_core import (
    AdapterCore,
    SignatureVerifier,
    compute_payload_sha256,
    validate_envelope,
)

RUN = settings(max_examples=200, deadline=None)

KEY_REF = "kms-key-1"
KEY = b"super-secret-test-key"


def _verifier() -> SignatureVerifier:
    return SignatureVerifier({KEY_REF: KEY})


payloads = st.dictionaries(
    st.text(min_size=1, max_size=6),
    st.one_of(st.integers(-1000, 1000), st.text(max_size=8), st.booleans()),
    max_size=5,
)


def _valid_envelope(payload: dict) -> GatewayEnvelope:
    digest = compute_payload_sha256(payload)
    return GatewayEnvelope(
        schema_version="1.0.0",
        message_id="m-1",
        source_system="NEA_LDC_GATEWAY",
        dataset="cross_border_schedule",
        event_time_utc="2026-05-23T00:00:00Z",
        published_time_utc="2026-05-23T00:00:05Z",
        revision=1,
        quality="VALID",
        payload_sha256=digest,
        signature_ref=KEY_REF,
        sequence=1,
        payload=payload,
    )


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 16: Envelope validation accepts
# complete envelopes and rejects incomplete ones, round-trip preserving.
# ---------------------------------------------------------------------------
@RUN
@given(payload=payloads, drop_field=st.sampled_from(REQUIRED_ENVELOPE_FIELDS))
def test_property_16_envelope_validation_and_roundtrip(payload, drop_field):
    env = _valid_envelope(payload)
    # Complete + correct hash -> valid.
    assert validate_envelope(env).ok is True
    # Round-trip preserves equivalence.
    assert GatewayEnvelope.from_json(env.to_json()) == env

    # Blank out a required field -> invalid, that field reported missing.
    import dataclasses

    broken = dataclasses.replace(env, **{drop_field: "" if isinstance(getattr(env, drop_field), str) else None})
    vr = validate_envelope(broken)
    assert vr.ok is False
    assert drop_field in vr.missing_fields


@RUN
@given(payload=payloads)
def test_property_16_hash_mismatch_rejected(payload):
    env = _valid_envelope(payload)
    import dataclasses

    tampered = dataclasses.replace(env, payload_sha256="deadbeef")
    assert validate_envelope(tampered).ok is False


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 17: Sequence gaps raise an alert
# and an audit record.
# ---------------------------------------------------------------------------
@RUN
@given(sequences=st.lists(st.integers(min_value=1, max_value=50), min_size=1, max_size=15, unique=True))
def test_property_17_sequence_gap_alert(sequences):
    core = AdapterCore(_verifier())
    ordered = sorted(sequences)
    expected_gaps = ordered[-1] - ordered[0] + 1 - len(ordered) > 0

    for i, seq in enumerate(ordered):
        payload = {"seq": seq}
        digest = compute_payload_sha256(payload)
        env = GatewayEnvelope(
            schema_version="1.0.0", message_id=f"m-{seq}", source_system="SYS",
            dataset="telemetry", event_time_utc="t", published_time_utc="t",
            revision=1, quality="VALID", payload_sha256=digest,
            signature_ref=KEY_REF, sequence=seq, payload=payload,
        )
        sig = core._verifier.sign(KEY_REF, digest)
        core.ingest(env, sig)

    gap_alerts = [a for a in core.alerts if a.startswith("sequence_gap")]
    gap_audits = [r for r in core.audit_log if r.action == "sequence_gap"]
    assert (len(gap_alerts) > 0) == expected_gaps
    # Alert count and audit count for gaps stay in lockstep.
    assert len(gap_alerts) == len(gap_audits)


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 18: Processing is idempotent
# under replay and duplication.
# ---------------------------------------------------------------------------
@RUN
@given(
    payload=payloads,
    replays=st.integers(min_value=1, max_value=6),
)
def test_property_18_idempotent_replay(payload, replays):
    core = AdapterCore(_verifier())
    env = _valid_envelope(payload)
    sig = core._verifier.sign(KEY_REF, env.payload_sha256)

    first = core.ingest(env, sig)
    assert first.published is True
    assert first.duplicate is False

    published_count = 1
    for _ in range(replays):
        outcome = core.ingest(env, sig)  # same message_id
        assert outcome.published is False
        assert outcome.duplicate is True
    # Exactly one operational publish regardless of replays.
    publishes = [r for r in core.audit_log if r.action == "published"]
    assert len(publishes) == published_count


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 21: Invalid-signature records are
# quarantined, never operationalized.
# ---------------------------------------------------------------------------
@RUN
@given(payload=payloads, good_sig=st.booleans())
def test_property_21_invalid_signature_quarantined(payload, good_sig):
    core = AdapterCore(_verifier())
    env = _valid_envelope(payload)
    if good_sig:
        sig = core._verifier.sign(KEY_REF, env.payload_sha256)
    else:
        sig = "0" * 64  # wrong signature

    outcome = core.ingest(env, sig)
    if good_sig:
        assert outcome.published is True
        assert outcome.quarantined is False
        assert env not in core.quarantine
    else:
        assert outcome.published is False
        assert outcome.quarantined is True
        assert env in core.quarantine
        # Never placed on an operational topic.
        assert outcome.topic is None
