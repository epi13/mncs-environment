"""Immutable Store references for Environment-owned evidence artifacts.

Environment domains produce and validate their own evidence. This module
only transports those records through the Store consumer API so a Store
session does not require sidecar write access beside the canonical Store.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .identity import digest_hex
from .store_backend import StoreIntegrityFailure, StoreUnavailable

STORE_EVIDENCE_SCHEMA = b"mncs.environment.session-evidence/1"
STORE_REFERENCE_PREFIX = "mncs-store:"


def _store_methods(session):
    store = session.store
    put = getattr(store, "put_record", None)
    get = getattr(store, "get_record_strict", None)
    if callable(put) and callable(get):
        return put, get
    return None


def is_store_reference(reference: Any) -> bool:
    return isinstance(reference, str) and reference.startswith(STORE_REFERENCE_PREFIX)


def cacheable(session, owner: str, reference: Any) -> bool:
    """Check that a cached evidence reference has a readable durable record."""
    if not isinstance(reference, str) or not reference:
        return False
    if reference.endswith("-evidence-unwritable"):
        return False
    if is_store_reference(reference):
        read(session, owner, reference)
        return True
    if not reference.startswith("sessions/"):
        return False
    root = Path(session.store.state_dir).resolve()
    path = (root / reference).resolve()
    if not path.is_relative_to(root):
        return False
    try:
        return isinstance(json.loads(path.read_text(encoding="utf-8")), dict)
    except (OSError, ValueError):
        return False


def publish(session, owner: str, record: dict[str, Any]) -> str | None:
    """Publish immutable owner evidence, or return None for file sessions.

    The content-derived identity is also the stable retry key. If a caller
    loses the response after Store committed, exact readback reconciles the
    outcome without creating a second authoritative record.
    """
    methods = _store_methods(session)
    if methods is None:
        return None
    put, get = methods
    identity = (f"{session.session_id}:{owner}:{digest_hex(record, length=64)}"
                ).encode()
    existing = get(STORE_EVIDENCE_SCHEMA, identity)
    if existing is None:
        try:
            put(STORE_EVIDENCE_SCHEMA, identity, record)
        except Exception:
            # The publication may have committed before its response was
            # interrupted. Only exact readback resolves that ambiguity.
            existing = get(STORE_EVIDENCE_SCHEMA, identity)
            if existing != record:
                raise
    elif existing != record:
        raise StoreIntegrityFailure(
            "Environment evidence identity resolved to different Store content")
    return STORE_REFERENCE_PREFIX + identity.decode("utf-8")


def read(session, owner: str, reference: str) -> dict[str, Any]:
    """Read one exact immutable Store evidence object for this session."""
    methods = _store_methods(session)
    if methods is None:
        raise StoreUnavailable(
            "Store evidence read requires the strict bound-record provider operation",
            code="provider-operation-unsupported",
        )
    _, get = methods
    identity = reference[len(STORE_REFERENCE_PREFIX):]
    expected_prefix = f"{session.session_id}:{owner}:"
    if not identity.startswith(expected_prefix):
        raise StoreIntegrityFailure(
            "Environment evidence reference belongs to a different session or owner")
    record = get(STORE_EVIDENCE_SCHEMA, identity.encode("utf-8"))
    if record is None:
        raise StoreIntegrityFailure(
            "Environment snapshot references a missing immutable Store evidence record")
    return record
