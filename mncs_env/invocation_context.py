"""Scoped provenance for capability calls made by ambient Environment work."""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

AMBIENT_OWNER_PASS_CONTEXT_SCHEMA = (
    "mncs.environment.ambient-owner-pass-context/1"
)
_CONTEXT: ContextVar[dict[str, str] | None] = ContextVar(
    "mncs_environment_ambient_owner_pass_context", default=None,
)


@contextmanager
def ambient_owner_pass_context(
    session_id: str, owner_pass: str, service_execution_id: str,
) -> Iterator[None]:
    """Label synchronous capability events emitted inside one owner pass."""
    if (not isinstance(session_id, str) or not session_id
            or len(session_id) > 160
            or not isinstance(owner_pass, str)
            or re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", owner_pass) is None
            or not isinstance(service_execution_id, str)
            or re.fullmatch(r"ase_[0-9a-f]{32}", service_execution_id) is None):
        raise ValueError("invalid ambient owner-pass execution context")
    value = {
        "schema_version": AMBIENT_OWNER_PASS_CONTEXT_SCHEMA,
        "owner_pass": owner_pass,
        "service_execution_id": service_execution_id,
    }
    token = _CONTEXT.set({"session_id": session_id, **value})
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def current_ambient_owner_pass_context(
    session_id: str,
) -> dict[str, str] | None:
    """Return the active context only for the Environment session that set it."""
    value = _CONTEXT.get()
    if (not isinstance(value, dict) or value.get("session_id") != session_id
            or value.get("schema_version") != AMBIENT_OWNER_PASS_CONTEXT_SCHEMA):
        return None
    owner_pass = value.get("owner_pass")
    execution_id = value.get("service_execution_id")
    if (not isinstance(owner_pass, str)
            or re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", owner_pass) is None
            or not isinstance(execution_id, str)
            or re.fullmatch(r"ase_[0-9a-f]{32}", execution_id) is None):
        return None
    return {
        "schema_version": AMBIENT_OWNER_PASS_CONTEXT_SCHEMA,
        "owner_pass": owner_pass,
        "service_execution_id": execution_id,
    }
