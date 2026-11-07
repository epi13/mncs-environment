"""Request observation transport over existing events and immutable evidence.

Native demand_correlation owns join/chronology classification. This module
captures owner-known facts, reads strict evidence and transports bounded rows.
No prompts, terminal commands, inference, permissions or utility labels are added.
"""
from __future__ import annotations

import time
import re
import os
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from pathlib import Path
import json

from . import evidence_store, native_readiness
from .identity import canonical_bytes, digest_hex
from .store_backend import StoreIntegrityFailure, StoreUnavailable

OWNER = "decision-context"
MAX_BINDINGS = 256
MAX_REPOSITORIES = 32
MAX_CONTEXT_BYTES = 65536
MAX_ROWS = 16
_ACTIVE: ContextVar[tuple[str, dict | None] | None] = ContextVar("environment_demand", default=None)


def ref(event: dict) -> dict:
    return {key: event[key] for key in ("identity", "sequence", "type")}


def active(session_id: str) -> dict | None:
    value = _ACTIVE.get()
    return value[1] if value is not None and value[0] == session_id else None


@contextmanager
def observing(session_id: str, reference: dict | None):
    token = _ACTIVE.set((session_id, reference))
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def _context(session) -> dict:
    snapshot = session.snapshot
    bindings = snapshot.get("bindings", [])
    repositories = snapshot.get("workspace", {}).get("repositories", [])
    rows = [{key: item.get(key) for key in (
        "binding_id", "capability", "provider", "contract_revision",
    )} | {"availability": (item.get("availability") or {}).get("status", "unknown")}
            for item in bindings[:MAX_BINDINGS] if isinstance(item, dict)]
    repos = []
    selected = snapshot.get("selected_checkouts", {})
    # A family discovery inventory can be much larger than the session's
    # explicitly selected work context. Preserve that scope, not an arbitrary
    # alphabetical prefix of unrelated checkouts.
    outside_scope = 0
    if selected:
        selected_paths = {item.get("path") for item in selected.values() if item.get("path")}
        outside_scope = sum(1 for item in repositories if item.get("path") not in selected_paths)
        # The selected checkout observation is authoritative for this session;
        # family inventory can omit linked worktrees and is not a substitute.
        repositories = [{"name": name, "manifest_repository": name,
                         **{key: item.get(key) for key in ("path", "head", "branch")},
                         "dirty": not item["clean"] if type(item.get("clean")) is bool else None}
                        for name, item in sorted(selected.items())]
    for item in repositories[:MAX_REPOSITORIES]:
        name = item.get("manifest_repository") or item.get("name")
        chosen = selected.get(name, {})
        repos.append({key: item.get(key) for key in (
            "name", "manifest_repository", "path", "branch", "head", "dirty",
        )} | {"selected_identity": chosen.get("checkout_identity") or chosen.get("repository_identity"),
               "observed_facts": snapshot.get("workspace_facts", {}).get(name)})
    return {
        "schema_version": "mncs.environment.decision-context/1",
        "session_id": session.session_id,
        "environment_id": snapshot.get("environment_id"),
        "work_intent_id": (snapshot.get("intent") or {}).get("identity"),
        "campaign_id": (snapshot.get("campaign") or {}).get("identity"),
        "authority_revision": (snapshot.get("authority") or {}).get("revision"),
        "rights_inputs_digest": digest_hex(snapshot.get("rights_context_inputs", {}), length=64),
        "bindings": rows, "repositories": repos,
        "repository_scope": "selected-checkouts" if selected else "recorded-workspace",
        "repositories_outside_scope": outside_scope,
        "repositories_missing_selected": sum(1 for item in selected.values() if not item.get("path")),
        "bindings_omitted": max(0, len(bindings) - MAX_BINDINGS),
        "repositories_omitted": max(0, len(repositories) - MAX_REPOSITORIES),
        "readiness": snapshot.get("readiness", {}).get("status", "not-recorded"),
        "freshness": "last-owner-observation; not-a-live-admission-check",
    }


def requested(session, capability: str, request_surface: str | None) -> dict:
    start = time.perf_counter_ns()
    context = _context(session)
    from .invocation_context import SESSION_INVOKE_REQUEST_SURFACE
    surface = request_surface if request_surface == SESSION_INVOKE_REQUEST_SURFACE else (
        "session-api-unattributed" if request_surface is None else "unsupported-surface"
    )
    safe_capability = capability if isinstance(capability, str) and re.fullmatch(r"[A-Za-z0-9_:/@.\-]{1,256}", capability) else "invalid-capability"
    payload: dict[str, Any] = {
        "capability": safe_capability,
        "parent_request_id": os.environ.get("MNCS_ENV_RPC_REQUEST_ID") if re.fullmatch(r"req_[A-Za-z0-9_-]{1,76}", os.environ.get("MNCS_ENV_RPC_REQUEST_ID", "")) else None,
        "request_surface": surface,
        "snapshot_sequence": session.snapshot.get("snapshot_sequence"),
        "snapshot_updated_at": session.snapshot.get("updated_at"),
        "actor_kind": session.snapshot.get("consumer_kind", "unknown"),
        "ambient_owner_pass": None,
        "context_status": "unavailable", "context_reference": None,
        "context_digest": None, "context_bytes": 0,
    }
    # The existing owner-pass context distinguishes ambient work without
    # claiming that an unmarked request was a manual agent intervention.
    from .invocation_context import current_ambient_owner_pass_context
    payload["ambient_owner_pass"] = current_ambient_owner_pass_context(session.session_id)
    raw = canonical_bytes(context)
    if len(raw) <= MAX_CONTEXT_BYTES:
        try:
            reference = evidence_store.publish(session, OWNER, context)
            if reference is None:  # Existing file-debug event log, no new store.
                payload["context_inline"] = context
            payload.update(context_reference=reference, context_digest=digest_hex(context, length=64),
                           context_bytes=len(raw), context_status="recorded")
        except StoreUnavailable:
            payload["context_status"] = "store-unavailable"
    else:
        payload["context_status"] = "over-budget"
    payload["observation_wall_ns"] = time.perf_counter_ns() - start
    return session._emit("demand.observed", session.snapshot.get("consumer_id", "unknown"), payload)


def finished(session, opportunity: dict, result: dict | None, error: Exception | None = None, *, refused: bool = False) -> dict:
    payload = {
        "demand_reference": ref(opportunity),
        "provider_request_id": (result or {}).get("provider_request_id"),
        "admission_reference": (result or {}).get("event_reference"),
        "status": (result or {}).get("status", "refused" if refused else "failed" if error is not None else "unknown"),
        "error_type": type(error).__name__ if error is not None else None,
        "delivery": "result-prepared" if result is not None else "no-result",
        "agent_receipt": "not-established", "usefulness": "not-established",
    }
    return session._emit("demand.finished", "environment", payload, causes=[opportunity["identity"]])


def _read_context(session, payload: dict) -> dict | None:
    if payload.get("context_status") != "recorded":
        return None
    reference = payload.get("context_reference")
    context = (evidence_store.read(session, OWNER, reference)
               if evidence_store.is_store_reference(reference) else payload.get("context_inline"))
    if not isinstance(context, dict) or len(canonical_bytes(context)) > MAX_CONTEXT_BYTES:
        return None
    if (context.get("schema_version") != "mncs.environment.decision-context/1"
            or context.get("session_id") != session.session_id
            or digest_hex(context, length=64) != payload.get("context_digest")):
        raise StoreIntegrityFailure("decision context does not match its exact event")
    return context


def _payload(event: dict | None) -> dict:
    return event.get("payload", {}) if isinstance(event, dict) else {}


def _demand_id(event: dict | None) -> str:
    reference = _payload(event).get("demand_reference") or {}
    return reference.get("identity", "") if isinstance(reference, dict) else ""


def _verification_link(session, event: dict | None) -> bool:
    if event is None or event.get("producer") != "environment":
        return False
    payload = _payload(event)
    if payload.get("result_kind") != "observed" or not payload.get("evidence_id"):
        return False
    reference = payload.get("verification_evidence")
    try:
        if evidence_store.is_store_reference(reference):
            evidence = evidence_store.read(session, "verification", reference)
        else:
            prefix = f"sessions/{session.session_id}/verification-artifacts/evidence/"
            if not isinstance(reference, str) or not reference.startswith(prefix):
                return False
            root = Path(session.store.state_dir).resolve()
            path = (root / reference).resolve()
            if not path.is_relative_to(root) or path.stat().st_size > 1024 * 1024:
                return False
            evidence = json.loads(path.read_text())
        if evidence.get("schema_version") != "mncs.environment.verification/1":
            return False
        if digest_hex(evidence, length=64) != payload.get("verification_evidence_digest"):
            return False
        matches = [r for r in evidence.get("results", [])
                   if r.get("evidence_id") == payload.get("evidence_id")
                   and r.get("obligation") == payload.get("obligation")]
        if len(matches) != 1:
            return False
        recorded = matches[0].get("verification_event", {})
        expected = {k: v for k, v in payload.items()
                    if k not in {"verification_evidence", "verification_evidence_digest"}}
        return recorded.get("type") == event.get("type") and recorded.get("payload") == expected
    except (OSError, ValueError, StoreUnavailable, StoreIntegrityFailure):
        return False


def project(session, events: list[dict]) -> dict:
    demands = [e for e in events if e.get("type") == "demand.observed"]
    selected = demands[-MAX_ROWS:]
    rows, native_rows = [], []
    contexts: dict[str, dict] = {}
    context_bytes = 0
    context_records_omitted = 0
    verified_contexts: dict[tuple, dict | None] = {}
    for event in selected:
        identity = event["identity"]
        linked = [e for e in events if _demand_id(e) == identity]
        starts = [e for e in linked if e.get("type") == "capability.invoked"]
        ends = [e for e in linked if e.get("type") == "invocation.completed"]
        results = [e for e in linked if e.get("type") in ("verification.verified", "verification.failed")]
        finishes = [e for e in linked if e.get("type") == "demand.finished"]
        first = starts[0] if starts else None
        end = ends[0] if ends else None
        outcome = results[0] if results else None
        context, context_status = None, "unavailable"
        try:
            key = (_payload(event).get("context_reference"), _payload(event).get("context_digest"))
            if evidence_store.is_store_reference(key[0]) and key in verified_contexts:
                context = verified_contexts[key]
            else:
                context = _read_context(session, _payload(event))
                if evidence_store.is_store_reference(key[0]):
                    verified_contexts[key] = context
            if context is not None:
                context_status = "integrity-verified"
        except (StoreUnavailable, StoreIntegrityFailure, OSError, ValueError):
            context_status = "unavailable-or-integrity-failure"
        if context is not None:
            context_id = _payload(event).get("context_digest")
            size = len(canonical_bytes(context))
            if context_id not in contexts:
                if context_bytes + size <= 32768:
                    contexts[context_id] = context
                    context_bytes += size
                else:
                    context_records_omitted += 1
        candidates = context.get("bindings", []) if context else []
        requested_binding = [b for b in candidates if b.get("capability") == _payload(event).get("capability")]
        binding = requested_binding[0].get("binding_id", "") if len(requested_binding) == 1 else ""
        native_rows.append({
            "demand": identity, "binding": binding,
            "start_demand": _demand_id(first), "end_demand": _demand_id(end),
            "start_binding": _payload(first).get("binding_id") or "",
            "end_binding": _payload(end).get("binding_id") or "",
            "start_request": _payload(first).get("provider_request_id") or "",
            "end_request": _payload(end).get("provider_request_id") or "",
            "result_demand": _demand_id(outcome),
            "result_request": _payload(outcome).get("provider_request_id") or "",
            "demand_sequence": event["sequence"],
            "start_sequence": first["sequence"] if first else 0,
            "end_sequence": end["sequence"] if end else 0,
            "result_sequence": outcome["sequence"] if outcome else 0,
            "starts": len(starts), "completions": len(ends), "results": len(results),
            "context_present": context is not None,
            "context_complete": bool(context is not None and not context.get("bindings_omitted") and not context.get("repositories_omitted") and not context.get("repositories_missing_selected")),
            "observed_result": _verification_link(session, outcome),
        })
        rows.append({
            "demand_event": ref(event), "request": {k: v for k, v in _payload(event).items() if k != "context_inline"},
            "context_digest": _payload(event).get("context_digest"), "context_status": context_status,
            "invocation_events": [ref(e) for e in starts],
            "completion_events": [ref(e) for e in ends],
            "verification_events": [ref(e) | {"event_type": e["type"], "outcome": _payload(e).get("outcome", "not-recorded"), "evidence_id": _payload(e).get("evidence_id"), "verification_evidence": _payload(e).get("verification_evidence"), "evidence_integrity": _verification_link(session, e)} for e in results],
            "finish_events": [ref(e) | {"status": _payload(e).get("status"), "delivery": _payload(e).get("delivery"), "admission_reference": _payload(e).get("admission_reference")} for e in finishes],
            "operation_status": _payload(end).get("status", "not-recorded-in-window"),
            "output_capture": _payload(end).get("output_capture"),
            "agent_receipt": "not-established", "usefulness": "not-established",
        })
    status = "not-recorded-in-window" if not rows else "native-unavailable"
    paths = native_readiness._toolchain_paths(session) if rows else None
    if paths is not None:
        try:
            answer = native_readiness._run_native_application(
                session=session, binary=paths[0], stdlib=paths[1],
                descriptor_name="demand-correlation.json",
                request={"schema_version": "mncs.environment.demand-correlation-request/1", "rows": native_rows},
                expected_schema="mncs.environment.demand-correlation-result/1", timeout_seconds=30,
            )
            decisions = answer.get("decisions")
            if not isinstance(decisions, list) or len(decisions) != len(rows):
                raise native_readiness.NativeApplicationError("native demand row count mismatch")
            for row, decision in zip(rows, decisions, strict=True):
                if (not isinstance(decision, dict)
                        or type(decision.get("operation_matched")) is not bool
                        or type(decision.get("verification_linked")) is not bool
                        or type(decision.get("reason")) is not int
                        or not 0 <= decision["reason"] <= 5):
                    raise native_readiness.NativeApplicationError("invalid native demand decision shape")
                row["correlation"] = decision
            status = "native-projected"
        except (native_readiness.NativeApplicationError, OSError, ValueError):
            status = "native-unavailable"
    return {
        "schema_version": "mncs.environment.decision-opportunities/1", "status": status,
        "scope": "Environment-bound capability requests in selected event window; not every agent decision",
        "records": rows, "contexts": contexts, "context_bytes": context_bytes,
        "context_records_omitted": context_records_omitted,
        "recorded_demand_count": len(demands),
        "records_omitted": max(0, len(demands) - len(rows)),
        "eligible_need_denominator": None, "agent_burden": "not-measured",
        "fulfillment": "not-established", "avoided_work": "not-established",
        "orphan_observation_count": sum(1 for e in events if _demand_id(e) and _demand_id(e) not in {d["identity"] for d in demands}),
    }
