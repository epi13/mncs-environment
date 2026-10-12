"""Authenticated task selectors for durable Environment campaigns.

A task selector is an opaque lookup key supplied by a durable caller context.
It is scoped to the authenticated principal, resolved Environment definition,
and workspace. It never grants authority: the bound campaign still passes the
normal principal, rights, lifecycle, and claim checks on every entry.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .identity import digest_hex

SCHEMA = "mncs.environment.campaign-task-binding/1"
MAX_TASK_IDENTITY_LENGTH = 256
ADMITTED_BY = frozenset({
    "mncs-environment.entry",
    "mncs-environment.project-continuation",
})


class TaskSelectorError(ValueError):
    """Invalid, conflicting, or unavailable durable task selector state."""

    def __init__(self, message: str, code: str = "campaign-task-selector-invalid"):
        super().__init__(message)
        self.code = code


def selector_identity(
    task_identity: str,
    *,
    authenticated_principal_id: str,
    definition_id: str,
    workspace_root: str,
) -> str:
    if (not isinstance(task_identity, str) or not task_identity.strip()
            or len(task_identity) > MAX_TASK_IDENTITY_LENGTH
            or "\x00" in task_identity):
        raise TaskSelectorError(
            "task identity must be bounded nonempty text"
        )
    if (not isinstance(authenticated_principal_id, str)
            or not authenticated_principal_id.strip()
            or len(authenticated_principal_id) > 256
            or "\x00" in authenticated_principal_id):
        raise TaskSelectorError(
            "task selection requires an authenticated Environment principal",
            "campaign-task-selector-unauthenticated",
        )
    if (not isinstance(definition_id, str) or not definition_id.startswith("env_")
            or not isinstance(workspace_root, str) or not workspace_root):
        raise TaskSelectorError("task selector Environment scope is invalid")
    return "tsel_" + digest_hex({
        "kind": "authenticated-environment-task-selector/1",
        "task_identity": task_identity,
        "authenticated_principal_id": authenticated_principal_id,
        "definition_id": definition_id,
        "workspace_root": workspace_root,
    }, length=64)


def evidence_id(selector: str) -> str:
    if not isinstance(selector, str) or not re.fullmatch(r"tsel_[0-9a-f]{64}", selector):
        raise TaskSelectorError("task selector identity is malformed")
    return "ev_campaign_task_" + selector[5:]


def _read(store: Any, selector: str) -> dict[str, Any] | None:
    read = getattr(store, "read_evidence", None)
    if not callable(read):
        raise TaskSelectorError(
            "selected persistence provider does not support durable task selectors",
            "persistence-service-unavailable",
        )
    try:
        record = read(evidence_id(selector))
    except (AttributeError, NotImplementedError) as error:
        raise TaskSelectorError(
            "selected persistence provider does not support durable task selectors",
            "persistence-service-unavailable",
        ) from error
    except Exception as error:
        code = getattr(error, "code", None)
        code_name = getattr(code, "name", code)
        detail_code = (
            "store-corruption"
            if "corrupt" in str(code_name).lower()
            or "integrity" in type(error).__name__.lower()
            else "persistence-service-unavailable"
        )
        raise TaskSelectorError(
            f"durable task selector could not be read: {error}",
            detail_code,
        ) from error
    if record is None:
        return None
    if (not isinstance(record, dict)
            or record.get("schema_version") != SCHEMA
            or record.get("selector_identity") != selector
            or record.get("evidence_id") != evidence_id(selector)):
        raise TaskSelectorError(
            "durable task selector record is malformed or has conflicting identity",
            "store-corruption",
        )
    for key in ("authenticated_principal_id", "definition_id", "workspace_root",
                "campaign_id", "session_id", "request_id", "operation_digest"):
        if not isinstance(record.get(key), str) or not record[key]:
            raise TaskSelectorError(
                f"durable task selector record lacks {key}",
                "store-corruption",
            )
    if (not record["campaign_id"].startswith("cmp_")
            or not record["session_id"].startswith("ses_")
            or not record["request_id"].startswith("req_")):
        raise TaskSelectorError(
            "durable task selector record identity is invalid", "store-corruption")
    expected_operation = digest_hex({
        "selector_identity": selector,
        "authenticated_principal_id": record["authenticated_principal_id"],
        "definition_id": record["definition_id"],
        "workspace_root": record["workspace_root"],
        "campaign_id": record["campaign_id"],
        "session_id": record["session_id"],
    }, length=64)
    if record["operation_digest"] != expected_operation:
        raise TaskSelectorError(
            "durable task selector payload digest is invalid", "store-corruption")
    provenance = record.get("provenance")
    if (
        not isinstance(record.get("bound_at"), str)
        or not isinstance(provenance, dict)
        or provenance.get("admitted_by") not in ADMITTED_BY
        or provenance.get("requested_by_authenticated_principal_id")
        != record["authenticated_principal_id"]
        or provenance.get("environment_session_id") != record["session_id"]
        or provenance.get("request_id") != record["request_id"]
    ):
        raise TaskSelectorError(
            "durable task selector provenance is invalid", "store-corruption")
    return record


def read_binding(store: Any, selector: str) -> dict[str, Any] | None:
    """Read and structurally validate the immutable selector binding."""
    return _read(store, selector)


def bind(
    store: Any,
    *,
    selector: str,
    authenticated_principal_id: str,
    definition_id: str,
    workspace_root: str,
    campaign_id: str,
    session_id: str,
    request_id: str,
    admitted_by: str = "mncs-environment.entry",
) -> dict[str, Any]:
    """Publish one immutable selector-to-campaign binding with safe retry."""
    if (not isinstance(request_id, str) or not request_id.startswith("req_")
            or len(request_id) > 160 or "\x00" in request_id):
        raise TaskSelectorError("task selector request identity is invalid")
    if (not campaign_id.startswith("cmp_") or not session_id.startswith("ses_")):
        raise TaskSelectorError("task selector target identity is invalid")
    if not isinstance(admitted_by, str) or admitted_by not in ADMITTED_BY:
        raise TaskSelectorError("task selector admission owner is invalid")
    operation_digest = digest_hex({
        "selector_identity": selector,
        "authenticated_principal_id": authenticated_principal_id,
        "definition_id": definition_id,
        "workspace_root": workspace_root,
        "campaign_id": campaign_id,
        "session_id": session_id,
    }, length=64)
    target = {
        "schema_version": SCHEMA,
        "evidence_id": evidence_id(selector),
        "selector_identity": selector,
        "authenticated_principal_id": authenticated_principal_id,
        "definition_id": definition_id,
        "workspace_root": workspace_root,
        "campaign_id": campaign_id,
        "session_id": session_id,
        "request_id": request_id,
        "operation_digest": operation_digest,
    }

    def same_target(existing: dict[str, Any]) -> bool:
        return all(existing.get(key) == target[key] for key in (
            "schema_version", "evidence_id", "selector_identity",
            "authenticated_principal_id", "definition_id", "workspace_root",
            "campaign_id", "session_id", "operation_digest",
        ))

    existing = _read(store, selector)
    if existing is not None:
        if same_target(existing):
            return existing
        raise TaskSelectorError(
            "task identity is already bound to another Environment campaign",
            "campaign-task-selector-conflict",
        )

    record = {
        **target,
        "bound_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "provenance": {
            "requested_by_authenticated_principal_id": authenticated_principal_id,
            "admitted_by": admitted_by,
            "environment_session_id": session_id,
            "request_id": request_id,
        },
    }
    write = getattr(store, "write_evidence", None)
    if not callable(write):
        raise TaskSelectorError(
            "selected persistence provider does not support durable task selectors",
            "persistence-service-unavailable",
        )
    try:
        written = write(evidence_id(selector), record)
    except Exception as error:
        # A caller can lose the service response after the immutable Store
        # object commits. Readback distinguishes that outcome from a lost
        # publication without creating a second mapping.
        try:
            committed = _read(store, selector)
        except Exception as read_error:
            raise TaskSelectorError(
                "task selector publication outcome is ambiguous; retry the same request identity",
                "ambiguous-publication",
            ) from read_error
        if committed is not None and same_target(committed):
            return committed
        if committed is not None:
            raise TaskSelectorError(
                "task identity was concurrently bound to another Environment campaign",
                "campaign-task-selector-conflict",
            ) from error
        raise TaskSelectorError(
            "task selector publication outcome is ambiguous; retry the same request identity",
            "ambiguous-publication",
        ) from error
    if not isinstance(written, dict) or not same_target(written):
        # Providers may acknowledge an immutable write while returning no
        # readback. Verify through the read contract before claiming success.
        committed = _read(store, selector)
        if committed is None or not same_target(committed):
            raise TaskSelectorError(
                "task selector publication was not confirmed by Store readback",
                "ambiguous-publication",
            )
        return committed
    return written
