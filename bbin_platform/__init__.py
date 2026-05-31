"""BBIN Hydropower Platform — Python reference implementation of the
foundational, safety-critical cores.

This package implements the parts of the design (``.kiro/specs/bbin-hydropower-platform``)
that encode the non-negotiable hard controls and the canonical data contracts:

- ``schemas``        : canonical envelopes, Declared-ATC, decision card, lineage, ruleset.
- ``adapter_core``   : envelope validation, signature verification, sequence-gap detection,
                       idempotent dedup, quarantine routing (Task 3).
- ``hard_controls``  : the pure invariant cores that make the hard controls structurally
                       enforceable — no-SCADA egress guard, schedule immutability,
                       Declared-ATC truth, executable-volume bound, maker-checker gate,
                       meter-truth settlement gate, immutable ruleset binding (Task 4).

The design's full polyglot deployment (Go/Rust/Java services, Kafka, Spark/Delta) is out
of scope for this environment; these modules are the verified, runnable foundation.
"""

__version__ = "0.1.0"
