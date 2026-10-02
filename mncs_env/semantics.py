"""Ambient semantic coherence over resident Language Service providers.

This pass composes, never duplicates:

- Doctor already probes declared ``mncs-language-service:resident-status``
  services and persists the probe's ``observed`` block (generation,
  stream, cursor, diagnostic counts) in the service observations. Quiet
  entries compare those persisted observations against durable cursors
  without spawning a single subprocess.
- When the world moved (new generation, unknown baseline, stream reset),
  this pass invokes the read-only ``semantic-poll`` / ``semantic-capsule``
  capabilities for bounded deltas. Lifecycle recovery stays with Doctor;
  verification stays with mncs-test; this pass publishes semantic facts.

Durable state lives in ``snapshot["semantics"]``: one record per
workspace root with the service identity, stream identity, cursor,
generation, and diagnostic fingerprint last observed. A cursor is never
interpreted without its stream identity.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from . import readiness as readiness_module
from .identity import digest_hex

STATUS_CAPABILITY = "mncs-language-service:resident-status"
POLL_CAPABILITY = "mncs-language-service:semantic-poll"
CAPSULE_CAPABILITY = "mncs-language-service:semantic-capsule"

#: Bound on declared semantic workspaces evaluated per pass.
MAX_SEMANTIC_WORKSPACES = 8

#: Bound on poll events pulled per workspace per pass.
POLL_MAX_EVENTS = 32

#: Per-invocation timeout: the ambient pass must stay terse.
INVOCATION_TIMEOUT_SECONDS = 10

#: Bound on retained pass history entries.
MAX_HISTORY = 8


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _declared_services(session) -> list[dict[str, Any]]:
    services = session.snapshot.get("requirements", {}).get("services", [])
    return [service for service in services
            if isinstance(service, dict)
            and isinstance(service.get("probe"), dict)
            and service["probe"].get("capability") == STATUS_CAPABILITY]


def _observations(session) -> dict[str, dict[str, Any]]:
    return {str(item.get("identity")): item
            for item in session.snapshot.get("service_observations", [])
            if isinstance(item, dict)}


def _binding_available(session, capability: str) -> bool:
    for binding in session.snapshot.get("bindings", []):
        if (isinstance(binding, dict) and binding.get("capability") == capability
                and binding.get("availability", {}).get("status") == "available"):
            return True
    return False


def _invoke_json(session, capability: str, argv: list[str]) -> tuple[dict | None, str | None]:
    """Invoke a read capability and parse its stdout JSON.

    Returns (document, None) on success or (None, reason) when the
    invocation cannot produce a document. Escalation and transport
    failures are reasons, never exceptions.
    """
    try:
        result = session.invoke(capability, argv,
                                timeout_seconds=INVOCATION_TIMEOUT_SECONDS,
                                output_limit_bytes=65536)
    except Exception as error:
        return None, f"invocation failed: {error}"
    if result.get("status") == "pending-escalation":
        return None, f"escalation required: {result.get('stderr', '')}"[:300]
    if result.get("status") != "ok":
        detail = (result.get("stderr") or result.get("status") or "")[:300]
        return None, f"provider invocation did not succeed: {detail}"
    import json
    try:
        document = json.loads(result.get("stdout") or "")
    except ValueError as error:
        return None, f"provider stdout is not JSON: {error}"
    if not isinstance(document, dict):
        return None, "provider stdout is not a JSON object"
    return document, None


def _workspace_argv(session, service: dict[str, Any]) -> tuple[list[str] | None, str | None]:
    try:
        argv = readiness_module.resolve_arguments(session, service["probe"].get("argv", []))
    except (ValueError, KeyError) as error:
        return None, f"declared probe argv do not resolve: {error}"
    return argv, None


def _poll_deltas(document: dict[str, Any]) -> dict[str, int]:
    events = document.get("events")
    if not isinstance(events, list):
        return {"events": 0, "diagnostics_added": 0, "diagnostics_resolved": 0,
                "subjects": 0}
    added = resolved = subjects = 0
    for event in events:
        if not isinstance(event, dict):
            continue
        diagnostics = event.get("diagnostics")
        if isinstance(diagnostics, dict):
            values = diagnostics.get("added")
            if isinstance(values, list):
                added += len(values)
            values = diagnostics.get("resolved")
            if isinstance(values, list):
                resolved += len(values)
        values = event.get("semantic_subjects")
        if isinstance(values, list):
            subjects += len(values)
    return {"events": len(events), "diagnostics_added": added,
            "diagnostics_resolved": resolved, "subjects": subjects}


def _capsule_counts(document: dict[str, Any]) -> dict[str, int]:
    findings = document.get("findings")
    actionable = watch = 0
    if isinstance(findings, list):
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            relevance = finding.get("relevance")
            if relevance == "actionable":
                actionable += 1
            elif relevance == "watch":
                watch += 1
    measured = document.get("measured") if isinstance(document.get("measured"), dict) else {}
    return {"actionable": actionable, "watch": watch,
            "admitted": len(findings) if isinstance(findings, list) else 0,
            "diagnostics": int(measured.get("diagnostics") or 0),
            "changed_subjects": int(measured.get("changed_subjects") or 0),
            "obligations": int(measured.get("obligations") or 0)}


def _evaluate_one(session, service: dict[str, Any], observations: dict[str, dict],
                  durable: dict[str, dict]) -> dict[str, Any]:
    """Evaluate one declared workspace. Returns a bounded record."""
    identity = str(service.get("identity"))
    record: dict[str, Any] = {"service": identity, "outcome": "unknown",
                              "workspace": None}
    observation = observations.get(identity)
    if observation is None or observation.get("status") != "ready":
        record["outcome"] = "degraded"
        record["reason"] = ("declared resident service is not ready; "
                            "Doctor owns recovery")
        if isinstance(observation, dict):
            record["detail"] = str(observation.get("reason", ""))[:200]
        return record
    provider_observed = observation.get("provider_observed")
    provider_selected = observation.get("provider_selected")
    if not isinstance(provider_observed, dict) or not isinstance(provider_selected, dict):
        record["outcome"] = "unknown"
        record["reason"] = "probe did not publish an observed/selected block"
        return record
    workspace = provider_selected.get("workspace_root")
    if not isinstance(workspace, str) or not workspace:
        record["outcome"] = "unknown"
        record["reason"] = "probe did not name its workspace root"
        return record
    record["workspace"] = workspace
    stream = provider_observed.get("stream_identity")
    cursor = provider_observed.get("event_cursor")
    generation = provider_observed.get("generation")
    if not isinstance(stream, str) or not stream or not isinstance(cursor, int):
        record["outcome"] = "unknown"
        record["reason"] = "probe observation lacks stream/cursor identity"
        return record
    saved = durable.get(workspace)
    fingerprint = digest_hex({
        "stream": stream, "generation": generation,
        "diagnostics_error": provider_observed.get("diagnostics_error"),
        "diagnostics_warning": provider_observed.get("diagnostics_warning"),
        "obligations_fail": provider_observed.get("obligations_fail"),
        "obligations_unknown": provider_observed.get("obligations_unknown"),
    })
    if (isinstance(saved, dict) and saved.get("stream") == stream
            and int(saved.get("cursor") or 0) >= cursor
            and saved.get("fingerprint") == fingerprint):
        record["outcome"] = "current"
        record["generation"] = generation
        record["cursor"] = cursor
        return record
    argv, problem = _workspace_argv(session, service)
    if argv is None:
        record["outcome"] = "unknown"
        record["reason"] = problem
        return record
    if not _binding_available(session, POLL_CAPABILITY) or not _binding_available(
            session, CAPSULE_CAPABILITY):
        record["outcome"] = "unknown"
        record["reason"] = "semantic-poll/capsule capabilities are not bound"
        return record
    if not isinstance(saved, dict) or saved.get("stream") != stream:
        # First contact or stream reset: reconcile through the bounded
        # capsule, never by replaying a foreign cursor.
        document, problem = _invoke_json(session, CAPSULE_CAPABILITY, argv)
        if document is None:
            record["outcome"] = "unknown"
            record["reason"] = problem
            return record
        record["outcome"] = "adopted" if saved is None else "reset"
        record["counts"] = _capsule_counts(document)
        durable[workspace] = {
            "stream": str(document.get("stream_identity") or stream),
            "cursor": int(document.get("current_cursor") or cursor),
            "generation": document.get("generation", generation),
            "fingerprint": fingerprint,
            "observed_at": utcnow(),
        }
        return record
    # Same stream, new generation: resume the bounded event window.
    poll_argv = [*argv, "--stream", str(saved.get("stream")),
                 "--after", str(int(saved.get("cursor") or 0)),
                 "--max", str(POLL_MAX_EVENTS)]
    document, problem = _invoke_json(session, POLL_CAPABILITY, poll_argv)
    if document is None:
        record["outcome"] = "unknown"
        record["reason"] = problem
        return record
    if document.get("reset_required"):
        adopted, problem = _invoke_json(session, CAPSULE_CAPABILITY, argv)
        if adopted is None:
            record["outcome"] = "unknown"
            record["reason"] = problem
            return record
        record["outcome"] = "reset"
        record["counts"] = _capsule_counts(adopted)
        durable[workspace] = {
            "stream": str(adopted.get("stream_identity") or stream),
            "cursor": int(adopted.get("current_cursor") or cursor),
            "generation": adopted.get("generation", generation),
            "fingerprint": fingerprint,
            "observed_at": utcnow(),
        }
        return record
    record["outcome"] = "changed"
    record["counts"] = _poll_deltas(document)
    durable[workspace] = {
        "stream": stream,
        "cursor": int(document.get("current_cursor") or cursor),
        "generation": generation,
        "fingerprint": fingerprint,
        "observed_at": utcnow(),
    }
    return record


def ambient_pass(session, *, mode: str = "ambient") -> dict[str, Any]:
    """Observe declared resident semantic workspaces without recomputation."""
    started = time.monotonic()
    services = _declared_services(session)
    if not services:
        # Unconfigured definitions carry the smallest possible block:
        # quiet entries must not pay for a pass with nothing to observe.
        summary: dict[str, Any] = {"enabled": True, "declared": 0}
        return {"summary": summary, "reused": True, "evidence": None}
    observations = _observations(session)
    stored = session.snapshot.get("semantics") or {}
    durable = stored.get("workspaces")
    if not isinstance(durable, dict):
        durable = {}
    else:
        durable = {str(key): dict(value) for key, value in durable.items()
                   if isinstance(value, dict)}
    def epoch_inputs_for(current_durable: dict[str, dict]) -> dict[str, Any]:
        identities = sorted(str(service.get("identity")) for service in services)
        return {
            "services": identities,
            "observations": {identity: {
                "status": observations.get(identity, {}).get("status"),
                "observed": observations.get(identity, {}).get("provider_observed"),
            } for identity in identities},
            "durable": current_durable,
        }

    epoch = digest_hex(epoch_inputs_for(durable))
    if mode == "ambient" and stored.get("epoch") == epoch:
        summary = dict(stored.get("summary") or {})
        summary["epoch_reused"] = True
        summary["elapsed_seconds"] = round(time.monotonic() - started, 3)
        return {"summary": summary, "reused": True, "evidence": None}
    records: list[dict[str, Any]] = []
    for service in services[:MAX_SEMANTIC_WORKSPACES]:
        records.append(_evaluate_one(session, service, observations, durable))
    # The stored epoch describes the post-pass world: durable cursors are
    # both an input and an output of the pass, so caching the pre-pass
    # state would never hit after a pass that adopted or advanced.
    epoch = digest_hex(epoch_inputs_for(durable))
    summary = {
        "enabled": True, "declared": len(services), "current": 0, "changed": 0,
        "adopted": 0, "reset": 0, "degraded": 0, "unknown": 0, "actionable": 0,
        "epoch_reused": False,
    }
    for record in records:
        outcome = record.get("outcome")
        if outcome in summary:
            summary[outcome] += 1
        counts = record.get("counts")
        if isinstance(counts, dict):
            summary["actionable"] += int(counts.get("actionable") or 0)
    if len(services) > MAX_SEMANTIC_WORKSPACES:
        summary["truncated"] = len(services) - MAX_SEMANTIC_WORKSPACES
    summary["elapsed_seconds"] = round(time.monotonic() - started, 3)
    # Degraded or unknown passes never cache their epoch: a transient
    # provider state must re-validate on the next pass.
    if mode == "ambient" and summary["degraded"] == 0 and summary["unknown"] == 0:
        session.snapshot["semantics"] = {
            "epoch": epoch, "summary": dict(summary),
            "workspaces": durable, "observed_at": utcnow(),
        }
    else:
        session.snapshot["semantics"] = {
            "summary": dict(summary), "workspaces": durable,
            "observed_at": utcnow(),
        }
    history = list(session.snapshot.get("semantics_history") or [])
    history.append({"at": utcnow(), "mode": mode, "summary": dict(summary)})
    session.snapshot["semantics_history"] = history[-MAX_HISTORY:]
    try:
        session._save()
    except (OSError, ValueError):
        summary["persisted"] = False
    else:
        summary["persisted"] = True
    return {"summary": summary, "reused": False, "evidence": None}
