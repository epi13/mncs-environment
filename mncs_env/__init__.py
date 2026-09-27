"""Canonical machine-native environment entry point (generic infrastructure).

`mncs_env` composes MNCS services; it never reimplements their domain
semantics. Host responsibilities here are filesystem discovery, process
boundaries, serialization, Git inspection, local IPC-free persistence,
and adapter plumbing. Planning, execution, memory, rights, diagnosis,
testing, and provenance policy stay in their owning repositories.
"""

from .identity import (
    checkpoint_id,
    environment_id,
    event_id,
    handoff_id,
    intent_id,
    new_session_id,
)

__all__ = [
    "checkpoint_id",
    "environment_id",
    "event_id",
    "handoff_id",
    "intent_id",
    "new_session_id",
]
