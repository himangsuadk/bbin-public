"""Integrity plane: checksum registration, schema compatibility, manifest verify (Task 2.2).

Properties: 19, 20, 22
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Checksum registration gate (Property 19)
# ---------------------------------------------------------------------------


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class ChecksumRegistry:
    """External files may be processed only after a checksum-registration record
    exists for them (Property 19, Req 19.5)."""

    def __init__(self) -> None:
        self._registered: dict[str, str] = {}  # file_id -> sha256

    def register(self, file_id: str, content: bytes) -> str:
        digest = sha256_hex(content)
        self._registered[file_id] = digest
        return digest

    def is_registered(self, file_id: str) -> bool:
        return file_id in self._registered

    def may_process(self, file_id: str, content: bytes) -> bool:
        """Processing is permitted iff the file was registered first and its
        content matches the registered digest."""
        if file_id not in self._registered:
            return False
        return self._registered[file_id] == sha256_hex(content)


# ---------------------------------------------------------------------------
# Schema backward-transitive compatibility (Property 20)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SchemaField:
    name: str
    type_: str
    has_default: bool


@dataclass(frozen=True)
class RecordSchema:
    fields: tuple[SchemaField, ...]

    def field_map(self) -> dict[str, SchemaField]:
        return {f.name: f for f in self.fields}


def is_backward_compatible(old: RecordSchema, new: RecordSchema) -> bool:
    """A reader using `new` can read data written with `old` (BACKWARD).

    Rules (Avro-style):
    - A field removed in `new` is fine (reader ignores it).
    - A field added in `new` must have a default (reader fills it).
    - A field whose type changed is incompatible.
    """
    old_map = old.field_map()
    new_map = new.field_map()
    # Added fields must have defaults.
    for name, nf in new_map.items():
        if name not in old_map:
            if not nf.has_default:
                return False
        else:
            if nf.type_ != old_map[name].type_:
                return False
    return True


def is_backward_transitive(history: list[RecordSchema], candidate: RecordSchema) -> bool:
    """BACKWARD_TRANSITIVE: the candidate must be backward compatible with every
    prior version, not just the latest (Property 20, Req 19.7)."""
    return all(is_backward_compatible(prev, candidate) for prev in history)


# ---------------------------------------------------------------------------
# Settlement-file manifest verification (Property 22)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Manifest:
    declared_sha256: str


def verify_against_manifest(file_content: bytes, manifest: Manifest) -> bool:
    """A settlement-grade file verifies iff its content hash matches the manifest
    (Property 22, Req 27.1)."""
    return sha256_hex(file_content) == manifest.declared_sha256
