"""Property tests for the integrity plane.

One design property per test, >=100 iterations, tagged. Library: Hypothesis.
"""

from __future__ import annotations

from hypothesis import given, settings, strategies as st

from bbin_platform.integrity import (
    ChecksumRegistry,
    Manifest,
    RecordSchema,
    SchemaField,
    is_backward_compatible,
    is_backward_transitive,
    sha256_hex,
    verify_against_manifest,
)

RUN = settings(max_examples=200, deadline=None)

blobs = st.binary(min_size=0, max_size=64)


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 19: External files are processed
# only after checksum registration.
# ---------------------------------------------------------------------------
@RUN
@given(file_id=st.text(min_size=1, max_size=8), content=blobs, register_first=st.booleans())
def test_property_19_checksum_registration(file_id, content, register_first):
    reg = ChecksumRegistry()
    if register_first:
        reg.register(file_id, content)
        assert reg.may_process(file_id, content) is True
    else:
        assert reg.may_process(file_id, content) is False


@RUN
@given(file_id=st.text(min_size=1, max_size=8), content=blobs, tamper=blobs)
def test_property_19_tampered_content_rejected(file_id, content, tamper):
    reg = ChecksumRegistry()
    reg.register(file_id, content)
    if tamper != content:
        assert reg.may_process(file_id, tamper) is False


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 20: Schema evolution is accepted
# only when backward-transitive compatible.
# ---------------------------------------------------------------------------
@RUN
@given(add_with_default=st.booleans(), change_type=st.booleans())
def test_property_20_backward_transitive(add_with_default, change_type):
    v1 = RecordSchema((SchemaField("a", "int", False),))
    v2 = RecordSchema((SchemaField("a", "int", False), SchemaField("b", "string", True)))
    history = [v1, v2]

    new_fields = [SchemaField("a", "long" if change_type else "int", False),
                  SchemaField("b", "string", True),
                  SchemaField("c", "int", add_with_default)]
    candidate = RecordSchema(tuple(new_fields))

    result = is_backward_transitive(history, candidate)
    # Compatible only if no type change AND the added field has a default.
    expected = (not change_type) and add_with_default
    assert result == expected


@RUN
@given(_dummy=st.integers())
def test_property_20_removed_field_ok(_dummy):
    old = RecordSchema((SchemaField("a", "int", False), SchemaField("b", "int", False)))
    new = RecordSchema((SchemaField("a", "int", False),))
    assert is_backward_compatible(old, new) is True


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 22: Settlement-grade files verify
# against their checksum manifest.
# ---------------------------------------------------------------------------
@RUN
@given(content=blobs, other=blobs)
def test_property_22_manifest_verification(content, other):
    manifest = Manifest(declared_sha256=sha256_hex(content))
    assert verify_against_manifest(content, manifest) is True
    if other != content:
        assert verify_against_manifest(other, manifest) is False
