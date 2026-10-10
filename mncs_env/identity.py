"""Stable semantic identities for environments, sessions, and records.

Identities are content hashes over canonical JSON, never filesystem paths
or process IDs. Sessions additionally bind a random nonce so two sessions
over the same environment never collide; the nonce is stored, making the
identity reproducible from the persisted record.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def digest_hex(value: Any, length: int = 16) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()[:length]


def environment_id(definition: dict[str, Any]) -> str:
    """Identity of an environment definition (pre-resolution)."""
    material = {key: definition.get(key) for key in sorted(definition)}
    material.pop("identity", None)
    return "env_" + digest_hex({"kind": "environment-definition", "definition": material})


def resolved_environment_id(definition_id: str, resolution_inputs: dict[str, Any]) -> str:
    """Identity of a resolved environment at a point in time."""
    return "renv_" + digest_hex(
        {"kind": "resolved-environment", "definition": definition_id, "inputs": resolution_inputs}
    )


def intent_id(intent: dict[str, Any]) -> str:
    material = {key: intent.get(key) for key in sorted(intent)}
    material.pop("identity", None)
    return "int_" + digest_hex({"kind": "work-intent", "intent": material})


def new_session_id(environment_id_value: str, consumer_id: str, nonce: str | None = None) -> str:
    active = nonce or secrets.token_hex(8)
    return "ses_" + digest_hex(
        {"kind": "session", "environment": environment_id_value, "consumer": consumer_id, "nonce": active}
    )


def checkpoint_id(session_id: str, sequence: int, state_digest: str) -> str:
    return "chk_" + digest_hex(
        {"kind": "checkpoint", "session": session_id, "sequence": sequence, "state": state_digest}
    )


def handoff_id(
    checkpoint_id_value: str,
    from_consumer: str,
    to_consumer: str,
    to_authenticated_principal_id: str | None = None,
    handoff_sequence: int | None = None,
) -> str:
    material = {
        "kind": "handoff",
        "checkpoint": checkpoint_id_value,
        "from": from_consumer,
        "to": to_consumer,
    }
    if to_authenticated_principal_id is not None:
        material["to_principal"] = to_authenticated_principal_id
    if handoff_sequence is not None:
        material["sequence"] = int(handoff_sequence)
    return "hff_" + digest_hex(
        material
    )


def event_id(session_id: str, sequence: int, event_type: str, payload_digest: str) -> str:
    return "evt_" + digest_hex(
        {
            "kind": "event",
            "session": session_id,
            "sequence": sequence,
            "type": event_type,
            "payload": payload_digest,
        }
    )
