"""Shared durable projection rows and verification evidence.

One durable observed state per projection identity, visible to every
session sharing a state directory — not one independent truth per
agent session. Session snapshots may cache these rows for epochs and
presentation, but this module (backed by Store, mirrored on the file
debug backend) is the semantic owner.

Concurrency follows the Store immutable-version discipline: each write
publishes a new row version; a writer that based its decision on a
stale version gets ProjectionConflict carrying the latest row and must
re-plan instead of overwriting. Verification evidence is immutable and
content-addressed: identical payloads are idempotent, differing
payloads under one identity are refused.
"""

from __future__ import annotations

from typing import Any

SCHEMA = "mncs.environment.projection-state/1"
EVIDENCE_SCHEMA = "mncs.environment.verification-evidence/1"


class ProjectionConflict(Exception):
    """Another session advanced the row this decision was based on."""

    def __init__(self, projection_id: str, latest: dict[str, Any] | None):
        super().__init__(
            f"projection {projection_id} advanced concurrently")
        self.projection_id = projection_id
        self.latest = latest


class EvidenceConflict(Exception):
    """A different payload already exists under this evidence identity."""


class SharedStoreUnavailable(Exception):
    """The shared projection backend cannot be reached right now.

    Callers fail closed: no adoption, no PASS, durable pending. The
    condition is transient (lock contention, missing backend, torn
    state), so epochs must not cache passes that hit it."""


def new_row(projection_id: str) -> dict[str, Any]:
    """Blank shared row: nothing canonical, nothing observed, no waits."""
    return {
        "schema_version": SCHEMA,
        "projection": projection_id,
        "version": 0,
        "canonical_gen": 0,
        "observed_gen": 0,
        "canonical_digest": None,
        "source": None,
        "rendered_digest": None,
        "verdict": 2,
        "evidence_id": None,
        "status": 2,
        "wait": 0,
        "defer_count": 0,
        "updated_by": None,
        "updated_at": None,
    }


def read_row(store: Any, projection_id: str) -> dict[str, Any]:
    """Latest shared row; a blank row when nothing was ever recorded."""
    try:
        found = store.read_projection_row(projection_id)
    except (AttributeError, OSError, ValueError) as error:
        raise SharedStoreUnavailable(
            f"projection rows unreadable: {error}") from error
    if found is None:
        return new_row(projection_id)
    version, row = found
    merged = new_row(projection_id)
    if isinstance(row, dict):
        merged.update(row)
    merged["version"] = int(version)
    merged["projection"] = projection_id
    return merged


def _payload_equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
    skip = {"version", "updated_by", "updated_at"}
    keys = (set(left) | set(right)) - skip
    return all(left.get(key) == right.get(key) for key in keys)


def write_row(store: Any, row: dict[str, Any],
              *, expected_version: int) -> dict[str, Any]:
    """Publish a new row version; refuse when the base version moved.

    Atomicity comes from the backend immutable put: two writers basing
    on version N race for identity `<id>:N+1` and exactly one wins.
    Replaying our own write after a crash is idempotent when the
    latest row carries our payload.
    """
    projection_id = str(row.get("projection", ""))
    published = dict(row)
    published["schema_version"] = SCHEMA
    published["projection"] = projection_id
    published["version"] = int(expected_version) + 1
    try:
        store.write_projection_row(
            projection_id, published["version"], published)
    except (AttributeError, OSError, ValueError) as error:
        raise SharedStoreUnavailable(
            f"projection rows unwritable: {error}") from error
    except ProjectionConflict as conflict:
        latest = conflict.latest if isinstance(
            conflict.latest, dict) else read_row(store, projection_id)
        if (int(latest.get("version", -1)) == published["version"]
                and _payload_equal(latest, published)):
            return latest
        raise ProjectionConflict(projection_id, latest) from conflict
    return published


def read_versions(store: Any) -> dict[str, int]:
    """Latest row version per projection (epoch invalidation vector)."""
    try:
        versions = store.read_projection_versions()
    except (AttributeError, OSError, ValueError):
        return {}
    if not isinstance(versions, dict):
        return {}
    return {str(key): int(value) for key, value in versions.items()
            if isinstance(value, int)}


def read_evidence(store: Any, evidence_id: str) -> dict[str, Any] | None:
    """Immutable verification evidence by content-derived identity."""
    try:
        found = store.read_evidence(evidence_id)
    except (AttributeError, OSError, ValueError) as error:
        raise SharedStoreUnavailable(
            f"verification evidence unreadable: {error}") from error
    return found if isinstance(found, dict) else None


def write_evidence(store: Any, record: dict[str, Any]) -> dict[str, Any]:
    """Record immutable evidence; identical rewrites are idempotent."""
    stored = dict(record)
    stored["schema_version"] = EVIDENCE_SCHEMA
    try:
        return store.write_evidence(str(stored["evidence_id"]), stored)
    except (AttributeError, OSError, ValueError) as error:
        raise SharedStoreUnavailable(
            f"verification evidence unwritable: {error}") from error
