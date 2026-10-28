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
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

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

# Bump when the acknowledgement rules or persisted cursor meaning changes.
# Older saved cursors are reconciled against a complete capsule before reuse.
SEMANTIC_CONSUMER_PROTOCOL = "mncs.environment.semantic-consumer/2"

SEMANTIC_OBSERVATION_SCHEMA = (
    "mncs.environment.semantic-change-observation/1"
)
MAX_SEMANTIC_OBSERVATION_WORKSPACES = 2
MAX_SEMANTIC_OBSERVATION_EVENTS_PER_WORKSPACE = 2
MAX_SEMANTIC_OBSERVATION_IDENTITIES = 4
MAX_SEMANTIC_OBSERVATION_IDENTITY_LENGTH = 128


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


def _fingerprint(stream: str, provider_observed: dict[str, Any]) -> str:
    return digest_hex({
        "stream": stream,
        "generation": provider_observed.get("generation"),
        "diagnostics_error": provider_observed.get("diagnostics_error"),
        "diagnostics_warning": provider_observed.get("diagnostics_warning"),
        "obligations_fail": provider_observed.get("obligations_fail"),
        "obligations_unknown": provider_observed.get("obligations_unknown"),
    })


def _safe_semantic_identity(value: Any) -> str | None:
    if (not isinstance(value, str) or not value
            or len(value) > MAX_SEMANTIC_OBSERVATION_IDENTITY_LENGTH
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        return None
    return value


def _safe_nonnegative(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= 10**9 else None


def _workspace_relative_path(workspace: Any, uri: Any) -> str | None:
    """Expose a source locator only when its file URI is inside the workspace."""
    if not isinstance(workspace, str) or not isinstance(uri, str):
        return None
    try:
        parsed = urlsplit(uri)
        if (parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}
                or parsed.query or parsed.fragment):
            return None
        root = Path(workspace).resolve(strict=False)
        source = Path(unquote(parsed.path)).resolve(strict=False)
        relative = source.relative_to(root)
    except (OSError, ValueError, TypeError):
        return None
    text = relative.as_posix()
    if (not text or text == "." or len(text) > 512
            or any(ord(char) < 32 or ord(char) == 127 for char in text)):
        return None
    return text


def _compact_identity_group(
    value: Any, *, changes: bool = False,
) -> dict[str, Any]:
    prior_omitted = 0
    declared_count = None
    if isinstance(value, dict):
        raw = value.get("items")
        declared_count = _safe_nonnegative(value.get("count"))
        prior_omitted = _safe_nonnegative(value.get("omitted_count")) or 0
    else:
        raw = value
    raw = raw if isinstance(raw, list) else []
    items: list[dict[str, str] | str] = []
    # Provider contracts already bound these groups. Keep the outer projection
    # defensive as well so malformed or stale snapshots cannot turn a focused
    # response into an unbounded scan.
    for item in raw[:MAX_SEMANTIC_OBSERVATION_IDENTITIES * 2]:
        if changes:
            if not isinstance(item, dict):
                continue
            identity = _safe_semantic_identity(item.get("identity"))
            change = item.get("change")
            if identity is None or change not in {"added", "removed", "changed"}:
                continue
            projected: dict[str, str] | str = {
                "identity": identity,
                "change": change,
            }
        else:
            identity = _safe_semantic_identity(item)
            if identity is None:
                continue
            projected = identity
        if projected in items:
            continue
        if len(items) < MAX_SEMANTIC_OBSERVATION_IDENTITIES:
            items.append(projected)
    declared_count = max(declared_count or 0, len(raw))
    return {
        "count": declared_count,
        "omitted_count": max(
            prior_omitted, declared_count - len(items),
        ),
        "items": items,
    }


def _safe_relative_path(value: Any) -> str | None:
    if (not isinstance(value, str) or not value or len(value) > 512
            or "\\" in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)):
        return None
    path = Path(value)
    if (path.is_absolute() or path.as_posix() == "."
            or any(part in {"", ".", ".."} for part in path.parts)):
        return None
    return path.as_posix()


def _compact_semantic_event(event: Any, workspace: Any) -> dict[str, Any] | None:
    if not isinstance(event, dict):
        return None
    cursor = _safe_nonnegative(event.get("cursor"))
    generation = _safe_nonnegative(
        event.get("current_generation", event.get("generation")),
    )
    if cursor is None or generation is None:
        return None
    current = event.get("current")
    current = current if isinstance(current, dict) else {}
    source_input = event.get("source")
    source_input = source_input if isinstance(source_input, dict) else {}
    source: dict[str, Any] = {"path_status": "not-resolved-in-workspace"}
    relative_path = _safe_relative_path(source_input.get("relative_path"))
    if relative_path is None:
        relative_path = _workspace_relative_path(workspace, current.get("uri"))
    if relative_path is not None:
        source["relative_path"] = relative_path
        source["path_status"] = "resolved-in-workspace"
    elif source_input.get("path_status") == "not-resolved-in-workspace":
        source["path_status"] = "not-resolved-in-workspace"
    raw_subjects = event.get("semantic_subjects")
    if isinstance(raw_subjects, dict):
        semantic_subjects = _compact_identity_group(raw_subjects, changes=True)
        declared = _safe_nonnegative(raw_subjects.get("count"))
        if declared is not None:
            semantic_subjects["count"] = declared
        semantic_subjects["omitted_count"] = max(
            semantic_subjects["omitted_count"],
            _safe_nonnegative(raw_subjects.get("omitted_count")) or 0,
        )
    else:
        semantic_subjects = _compact_identity_group(
            raw_subjects, changes=True,
        )

    def delta_group(raw: Any) -> dict[str, Any]:
        raw = raw if isinstance(raw, dict) else {}
        added = _compact_identity_group(raw.get("added"))
        resolved = _compact_identity_group(raw.get("resolved"))
        status_changed = _compact_identity_group(raw.get("status_changed"))
        return {
            "complete": raw.get("complete") if type(raw.get("complete")) is bool else None,
            "added": added,
            "resolved": resolved,
            "status_changed": status_changed,
        }

    diagnostics = event.get("diagnostics")
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    obligations = event.get("obligations")
    obligations = obligations if isinstance(obligations, dict) else {}
    result: dict[str, Any] = {
        "cursor": cursor,
        "generation": generation,
        "source": source,
        "affected_document_count": (
            len(event.get("affected_documents"))
            if isinstance(event.get("affected_documents"), list) else 0
        ),
        "semantic_subjects": semantic_subjects,
        "diagnostics": {
            "added": _compact_identity_group(diagnostics.get("added")),
            "resolved": _compact_identity_group(diagnostics.get("resolved")),
        },
        "obligations": delta_group(obligations),
        "impact_complete": (
            event.get("impact_complete")
            if type(event.get("impact_complete")) is bool else None
        ),
        "reconciled": (
            event.get("reconciled")
            if type(event.get("reconciled")) is bool else None
        ),
        "removed": (
            event.get("removed") if type(event.get("removed")) is bool else None
        ),
    }
    impact_identity = _safe_semantic_identity(event.get("impact_identity"))
    if impact_identity is not None:
        result["impact_identity"] = impact_identity
    return result


def project_semantic_observation(value: Any) -> dict[str, Any] | None:
    """Return bounded event evidence with workspace and cursor provenance.

    The projection deliberately excludes source bodies and the provider's
    impact graph. It preserves compiler-owned identities and delta facts so an
    agent can follow up through the bound Language Service when useful.
    """
    if (not isinstance(value, dict)
            or value.get("schema_version") != SEMANTIC_OBSERVATION_SCHEMA
            or value.get("status") != "observed"):
        return None
    raw_workspaces = value.get("workspaces")
    if not isinstance(raw_workspaces, list):
        return None
    projected_workspaces: list[dict[str, Any]] = []
    declared_workspace_count = _safe_nonnegative(
        value.get("workspace_count"),
    )
    if declared_workspace_count is None:
        declared_workspace_count = len(raw_workspaces)
    declared_workspace_count = max(declared_workspace_count, len(raw_workspaces))
    declared_workspace_omitted = _safe_nonnegative(
        value.get("workspaces_omitted_count"),
    ) or 0
    for workspace_row in raw_workspaces[:MAX_SEMANTIC_OBSERVATION_WORKSPACES]:
        if not isinstance(workspace_row, dict):
            continue
        service = _safe_semantic_identity(workspace_row.get("service"))
        stream = _safe_semantic_identity(workspace_row.get("stream_identity"))
        cursor_after = _safe_nonnegative(workspace_row.get("cursor_after"))
        cursor_through = _safe_nonnegative(workspace_row.get("cursor_through"))
        high_water = _safe_nonnegative(workspace_row.get("provider_high_water"))
        if (service is None or stream is None or cursor_after is None
                or cursor_through is None or high_water is None
                or cursor_through < cursor_after or high_water < cursor_through):
            continue
        raw_events = workspace_row.get("events")
        raw_events = raw_events if isinstance(raw_events, list) else []
        declared_event_count = _safe_nonnegative(
            workspace_row.get("event_count"),
        )
        if declared_event_count is None:
            declared_event_count = len(raw_events)
        declared_event_count = max(declared_event_count, len(raw_events))
        events: list[dict[str, Any]] = []
        for event in raw_events[:MAX_SEMANTIC_OBSERVATION_EVENTS_PER_WORKSPACE]:
            compact = _compact_semantic_event(
                event, workspace_row.get("workspace_root"),
            )
            if compact is not None:
                events.append(compact)
        omitted = max(
            _safe_nonnegative(workspace_row.get("events_omitted_count")) or 0,
            declared_event_count - len(events),
        )
        projected_workspaces.append({
            "service": service,
            "stream_identity": stream,
            "cursor_after": cursor_after,
            "cursor_through": cursor_through,
            "provider_high_water": high_water,
            "pending_event_count": high_water - cursor_through,
            "event_count": declared_event_count,
            "events_omitted_count": omitted,
            "events": events,
        })
    workspace_omitted = max(
        declared_workspace_omitted,
        declared_workspace_count - len(projected_workspaces),
        len(raw_workspaces) - len(projected_workspaces),
    )
    event_count = max(
        _safe_nonnegative(value.get("event_count")) or 0,
        sum(row["event_count"] for row in projected_workspaces),
    )
    returned_event_count = sum(len(row["events"]) for row in projected_workspaces)
    event_omitted_count = max(
        _safe_nonnegative(value.get("events_omitted_count")) or 0,
        event_count - returned_event_count,
        sum(row["events_omitted_count"] for row in projected_workspaces),
    )
    pending_event_count = max(
        _safe_nonnegative(value.get("pending_event_count")) or 0,
        sum(row["pending_event_count"] for row in projected_workspaces),
    )
    return {
        "schema_version": SEMANTIC_OBSERVATION_SCHEMA,
        "status": "observed",
        "scope": "selected-language-service-event-pages",
        "coverage_status": (
            "partial-selected-pages"
            if workspace_omitted or event_omitted_count
            else "selected-pages-with-pending-events"
            if pending_event_count else "complete-selected-pages"
        ),
        "workspace_count": declared_workspace_count,
        "workspace_observation_count": len(projected_workspaces),
        "workspaces_omitted_count": workspace_omitted,
        "event_count": event_count,
        "returned_event_count": returned_event_count,
        "events_omitted_count": event_omitted_count,
        "pending_event_count": pending_event_count,
        "workspaces": projected_workspaces,
        "interpretation_limit": (
            "Compiler-owned semantic deltas are selected-session observations; "
            "they do not establish an agent request, correctness verdict, or "
            "that every workspace event is represented when a page is partial."
        ),
    }


def _semantic_observation_for_window(
    service: str, workspace: str, stream: str, after: int,
    events: list[dict[str, Any]], page_cursor: int, high_water: int,
) -> dict[str, Any] | None:
    if not events:
        return None
    return project_semantic_observation({
        "schema_version": SEMANTIC_OBSERVATION_SCHEMA,
        "status": "observed",
        "workspace_count": 1,
        "workspaces": [{
            "service": service,
            "workspace_root": workspace,
            "stream_identity": stream,
            "cursor_after": after,
            "cursor_through": page_cursor,
            "provider_high_water": high_water,
            "event_count": len(events),
            "events": events,
        }],
    })


def _durable_cursors_current(services: list[dict[str, Any]],
                             observations: dict[str, dict[str, Any]],
                             durable: dict[str, dict[str, Any]]) -> bool:
    """A cached pass is reusable only if every observed stream is drained."""
    for service in services[:MAX_SEMANTIC_WORKSPACES]:
        observation = observations.get(str(service.get("identity")))
        if not isinstance(observation, dict) or observation.get("status") != "ready":
            return False
        selected = observation.get("provider_selected")
        observed = observation.get("provider_observed")
        if not isinstance(selected, dict) or not isinstance(observed, dict):
            return False
        workspace = selected.get("workspace_root")
        stream = observed.get("stream_identity")
        cursor = observed.get("event_cursor")
        if (not isinstance(workspace, str) or not isinstance(stream, str)
                or type(cursor) is not int):
            return False
        saved = durable.get(workspace)
        if (not isinstance(saved, dict)
                or saved.get("consumer_protocol") != SEMANTIC_CONSUMER_PROTOCOL
                or saved.get("stream") != stream
                or type(saved.get("cursor")) is not int
                or saved["cursor"] < cursor
                or saved.get("fingerprint") != _fingerprint(stream, observed)):
            return False
    return True


def _capsule_is_complete(document: dict[str, Any]) -> tuple[bool, str | None]:
    """Only a complete semantic reconciliation can acknowledge a new epoch."""
    status = document.get("status")
    if not isinstance(status, dict) or status.get("kind") != "answered":
        reason = status.get("reason") if isinstance(status, dict) else None
        return False, str(reason or "semantic capsule did not answer completely")
    measured = document.get("measured")
    pending = (int(measured.get("analysis_pending_documents") or 0)
               if isinstance(measured, dict) else 0)
    unresolved = document.get("unresolved")
    if pending:
        return False, f"semantic capsule has {pending} documents without current analysis"
    if isinstance(unresolved, list) and unresolved:
        return False, "semantic capsule reports unresolved workspace state"
    return True, None


def _capsule_matches(document: dict[str, Any], stream: str,
                     minimum_cursor: int) -> tuple[bool, str | None]:
    if document.get("stream_identity") != stream:
        return False, "semantic capsule belongs to a different stream"
    cursor = document.get("current_cursor")
    if type(cursor) is not int or cursor < minimum_cursor:
        return False, "semantic capsule does not cover the observed stream high-water"
    return _capsule_is_complete(document)


def _poll_window(document: dict[str, Any], stream: str, after: int
                 ) -> tuple[bool, str | None, list[dict[str, Any]], int, int]:
    """Validate a bounded poll page before any durable cursor can advance."""
    current = document.get("current_cursor")
    events = document.get("events")
    if document.get("schema_version") != "mncs.workspace-event-cursor/2":
        return False, "semantic poll returned an unsupported cursor schema", [], after, after
    if document.get("stream_identity") != stream:
        return False, "semantic poll belongs to a different stream", [], after, after
    if type(document.get("after_cursor")) is not int or document["after_cursor"] != after:
        return False, "semantic poll does not match the requested cursor", [], after, after
    if type(current) is not int or current < after:
        return False, "semantic poll high-water is invalid", [], after, after
    if document.get("reset_required") is not False:
        return False, "semantic poll requires stream reconciliation", [], after, current
    if not isinstance(events, list) or len(events) > POLL_MAX_EVENTS:
        return False, "semantic poll page is malformed or exceeds its bound", [], after, current
    page_cursor = after
    for event in events:
        if (not isinstance(event, dict) or type(event.get("cursor")) is not int
                or event["cursor"] != page_cursor + 1 or event["cursor"] > current):
            return False, "semantic poll event cursors are invalid", [], after, current
        page_cursor = event["cursor"]
        supersedes = event.get("supersedes_through_cursor", 0)
        if (type(supersedes) is not int or supersedes < 0
                or supersedes >= event["cursor"]
                or (supersedes and event.get("reconciled") is not True)):
            return False, "semantic event supersession proof is invalid", events, page_cursor, current
    if current > after and not events:
        return False, "semantic poll high-water advanced without replay events", [], after, current
    if events and page_cursor < current and len(events) < POLL_MAX_EVENTS:
        return False, "semantic poll page ends before the provider high-water", events, page_cursor, current

    def complete(event: dict[str, Any]) -> bool:
        obligations = event.get("obligations")
        subjects = event.get("semantic_subjects")
        return (event.get("impact_complete") is True
                and isinstance(obligations, dict)
                and obligations.get("complete") is True
                and isinstance(subjects, list) and len(subjects) <= 64)

    def event_uris(event: dict[str, Any]) -> set[str]:
        uris: set[str] = set()
        current_source = event.get("current")
        if isinstance(current_source, dict) and isinstance(current_source.get("uri"), str):
            uris.add(current_source["uri"])
        affected = event.get("affected_documents")
        if isinstance(affected, list):
            uris.update(item["uri"] for item in affected
                        if isinstance(item, dict) and isinstance(item.get("uri"), str))
        return uris

    effective: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        if complete(event):
            effective.append(event)
            continue
        uris = event_uris(event)
        superseded = any(
            later.get("reconciled") is True
            and later.get("supersedes_through_cursor", 0) >= event["cursor"]
            and complete(later)
            and bool(uris & event_uris(later))
            for later in events[index + 1:]
        )
        if not superseded:
            return False, "semantic event impact is incomplete", events, page_cursor, current
    return True, None, effective, page_cursor, current


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
    fingerprint = _fingerprint(stream, provider_observed)
    if (isinstance(saved, dict)
            and saved.get("consumer_protocol") == SEMANTIC_CONSUMER_PROTOCOL
            and saved.get("stream") == stream
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
        if cursor == 0:
            # A newly established stream with high-water zero has no
            # semantic changes to replay. Adopt that exact empty window
            # from the provider's status identity; do not invoke the
            # workspace-wide capsule, which would force analysis of every
            # selected source file merely to establish a baseline.
            record["outcome"] = "adopted" if saved is None else "reset"
            record["counts"] = {"actionable": 0, "watch": 0, "admitted": 0,
                                "diagnostics": 0, "changed_subjects": 0,
                                "obligations": 0}
            record["baseline"] = "empty-provider-window"
            durable[workspace] = {
                "stream": stream,
                "cursor": 0,
                "generation": generation,
                "fingerprint": fingerprint,
                "consumer_protocol": SEMANTIC_CONSUMER_PROTOCOL,
                "observed_at": utcnow(),
            }
            return record
        # A new consumer can establish this epoch without analyzing the whole
        # workspace when the provider still retains the complete stream from
        # cursor 1. Replay from zero is safe only with the exact stream
        # identity and a contiguous, fully covered page; otherwise fall back
        # to a complete capsule as the authority for expired history.
        poll_argv = [*argv, "--stream", stream, "--after", "0",
                     "--max", str(POLL_MAX_EVENTS)]
        replay, replay_problem = _invoke_json(session, POLL_CAPABILITY, poll_argv)
        replay_ok = False
        if replay is not None:
            replay_ok, replay_reason, events, page_cursor, high_water = _poll_window(
                replay, stream, 0)
        else:
            replay_reason = replay_problem
            events, page_cursor, high_water = [], 0, cursor
        if replay_ok:
            record["outcome"] = "changed"
            record["baseline"] = "complete-retained-stream"
            record["counts"] = _poll_deltas({"events": events})
            record["replay_high_water"] = high_water
            semantic_observation = _semantic_observation_for_window(
                identity, workspace, stream, 0, events, page_cursor, high_water,
            )
            if semantic_observation is not None:
                record["semantic_observation"] = semantic_observation
            durable[workspace] = {
                "stream": stream,
                "cursor": page_cursor,
                "generation": generation,
                "fingerprint": fingerprint,
                "consumer_protocol": SEMANTIC_CONSUMER_PROTOCOL,
                "observed_at": utcnow(),
            }
            return record

        # Stream reset, expired history, or incomplete event impact: a
        # complete capsule is required before this consumer can acknowledge
        # the observed high-water. Never apply the previous epoch's cursor.
        document, problem = _invoke_json(session, CAPSULE_CAPABILITY, argv)
        if document is None:
            record["outcome"] = "unknown"
            record["reason"] = problem or replay_reason
            return record
        complete, reason = _capsule_matches(document, stream, cursor)
        if not complete:
            record["outcome"] = "unknown"
            record["reason"] = reason
            record["replay_reason"] = replay_reason
            record["cursor_retained"] = int(saved.get("cursor") or 0) if isinstance(saved, dict) else None
            return record
        record["outcome"] = "adopted" if saved is None else "reset"
        record["counts"] = _capsule_counts(document)
        durable[workspace] = {
            "stream": stream,
            "cursor": int(document["current_cursor"]),
            "generation": document.get("generation", generation),
            "fingerprint": fingerprint,
            "consumer_protocol": SEMANTIC_CONSUMER_PROTOCOL,
            "observed_at": utcnow(),
        }
        return record

    if saved.get("consumer_protocol") != SEMANTIC_CONSUMER_PROTOCOL:
        # Older consumers acknowledged any syntactically valid poll result.
        # Reconcile current semantic state before trusting their cursor.
        document, problem = _invoke_json(session, CAPSULE_CAPABILITY, argv)
        if document is None:
            record["outcome"] = "unknown"
            record["reason"] = problem
            return record
        complete, reason = _capsule_matches(document, stream, cursor)
        if not complete:
            record["outcome"] = "unknown"
            record["reason"] = reason
            record["cursor_retained"] = int(saved.get("cursor") or 0)
            return record
        record["outcome"] = "reset"
        record["baseline"] = "consumer-protocol-reconciliation"
        record["counts"] = _capsule_counts(document)
        durable[workspace] = {
            "stream": stream,
            "cursor": int(document["current_cursor"]),
            "generation": document.get("generation", generation),
            "fingerprint": fingerprint,
            "consumer_protocol": SEMANTIC_CONSUMER_PROTOCOL,
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
        complete, reason = _capsule_matches(adopted, stream, cursor)
        if not complete:
            record["outcome"] = "unknown"
            record["reason"] = reason
            record["cursor_retained"] = int(saved.get("cursor") or 0)
            return record
        record["outcome"] = "reset"
        record["counts"] = _capsule_counts(adopted)
        durable[workspace] = {
            "stream": stream,
            "cursor": int(adopted["current_cursor"]),
            "generation": adopted.get("generation", generation),
            "fingerprint": fingerprint,
            "consumer_protocol": SEMANTIC_CONSUMER_PROTOCOL,
            "observed_at": utcnow(),
        }
        return record

    complete, reason, events, page_cursor, high_water = _poll_window(
        document, stream, int(saved.get("cursor") or 0))
    if not complete:
        adopted, problem = _invoke_json(session, CAPSULE_CAPABILITY, argv)
        if adopted is None:
            record["outcome"] = "unknown"
            record["reason"] = problem or reason
            record["cursor_retained"] = int(saved.get("cursor") or 0)
            return record
        reconciled, reconcile_reason = _capsule_matches(adopted, stream, high_water)
        if not reconciled:
            record["outcome"] = "unknown"
            record["reason"] = reconcile_reason or reason
            record["cursor_retained"] = int(saved.get("cursor") or 0)
            return record
        record["outcome"] = "reset"
        record["baseline"] = "incomplete-window-reconciliation"
        record["counts"] = _capsule_counts(adopted)
        durable[workspace] = {
            "stream": stream,
            "cursor": int(adopted["current_cursor"]),
            "generation": adopted.get("generation", generation),
            "fingerprint": fingerprint,
            "consumer_protocol": SEMANTIC_CONSUMER_PROTOCOL,
            "observed_at": utcnow(),
        }
        return record
    if not events:
        record["outcome"] = "current"
        record["generation"] = generation
        record["cursor"] = high_water
    else:
        record["outcome"] = "changed"
        record["counts"] = _poll_deltas({"events": events})
        semantic_observation = _semantic_observation_for_window(
            identity, workspace, stream, int(saved.get("cursor") or 0),
            events, page_cursor, high_water,
        )
        if semantic_observation is not None:
            record["semantic_observation"] = semantic_observation
    durable[workspace] = {
        "stream": stream,
        # A full page can have more events waiting. Acknowledge only the
        # last event whose complete semantic impact this pass processed.
        "cursor": page_cursor,
        "generation": generation,
        "fingerprint": fingerprint,
        "consumer_protocol": SEMANTIC_CONSUMER_PROTOCOL,
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
        return {"summary": summary, "reused": True, "evidence": None,
                "operation_status": "complete"}
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
    if (mode == "ambient" and stored.get("epoch") == epoch
            and _durable_cursors_current(services, observations, durable)):
        summary = dict(stored.get("summary") or {})
        summary["epoch_reused"] = True
        summary["elapsed_seconds"] = round(time.monotonic() - started, 3)
        operation_status = ("retry" if summary.get("degraded", 0)
                            or summary.get("unknown", 0) else "complete")
        result = {"summary": summary, "reused": True, "evidence": None,
                  "operation_status": operation_status}
        semantic_observation = project_semantic_observation(
            stored.get("semantic_observation"),
        )
        if semantic_observation is not None:
            result["semantic_observation"] = semantic_observation
        return result
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
        "semantic_events": 0, "semantic_subjects": 0,
        "diagnostics_added": 0, "diagnostics_resolved": 0,
        "epoch_reused": False,
    }
    for record in records:
        outcome = record.get("outcome")
        if outcome in summary:
            summary[outcome] += 1
        counts = record.get("counts")
        if isinstance(counts, dict):
            summary["actionable"] += int(counts.get("actionable") or 0)
            summary["semantic_events"] += int(counts.get("events") or 0)
            summary["semantic_subjects"] += int(counts.get("subjects") or 0)
            summary["diagnostics_added"] += int(counts.get("diagnostics_added") or 0)
            summary["diagnostics_resolved"] += int(counts.get("diagnostics_resolved") or 0)
    if len(services) > MAX_SEMANTIC_WORKSPACES:
        summary["truncated"] = len(services) - MAX_SEMANTIC_WORKSPACES
    summary["elapsed_seconds"] = round(time.monotonic() - started, 3)
    raw_workspace_observations = [
        record["semantic_observation"]["workspaces"][0]
        for record in records
        if isinstance(record.get("semantic_observation"), dict)
        and isinstance(record["semantic_observation"].get("workspaces"), list)
        and record["semantic_observation"]["workspaces"]
    ]
    observation_sources = [
        record["semantic_observation"]
        for record in records
        if isinstance(record.get("semantic_observation"), dict)
    ]
    semantic_observation = project_semantic_observation({
        "schema_version": SEMANTIC_OBSERVATION_SCHEMA,
        "status": "observed",
        "workspace_count": sum(
            row.get("workspace_count", 0) for row in observation_sources
            if type(row.get("workspace_count")) is int
            and row.get("workspace_count") >= 0
        ),
        "workspaces_omitted_count": sum(
            row.get("workspaces_omitted_count", 0)
            for row in observation_sources
            if type(row.get("workspaces_omitted_count")) is int
            and row.get("workspaces_omitted_count") >= 0
        ),
        "event_count": sum(
            row.get("event_count", 0) for row in observation_sources
            if type(row.get("event_count")) is int
            and row.get("event_count") >= 0
        ),
        "events_omitted_count": sum(
            row.get("events_omitted_count", 0) for row in observation_sources
            if type(row.get("events_omitted_count")) is int
            and row.get("events_omitted_count") >= 0
        ),
        "pending_event_count": sum(
            row.get("pending_event_count", 0) for row in observation_sources
            if type(row.get("pending_event_count")) is int
            and row.get("pending_event_count") >= 0
        ),
        "workspaces": raw_workspace_observations,
    }) if raw_workspace_observations else None
    # Degraded or unknown passes never cache their epoch: a transient
    # provider state must re-validate on the next pass.
    if mode == "ambient" and summary["degraded"] == 0 and summary["unknown"] == 0:
        semantic_state = {
            "epoch": epoch, "summary": dict(summary),
            "workspaces": durable, "observed_at": utcnow(),
        }
        if semantic_observation is not None:
            semantic_state["semantic_observation"] = semantic_observation
        session.snapshot["semantics"] = semantic_state
    else:
        semantic_state = {
            "summary": dict(summary), "workspaces": durable,
            "observed_at": utcnow(),
        }
        if semantic_observation is not None:
            semantic_state["semantic_observation"] = semantic_observation
        session.snapshot["semantics"] = semantic_state
    history = list(session.snapshot.get("semantics_history") or [])
    history.append({"at": utcnow(), "mode": mode, "summary": dict(summary)})
    session.snapshot["semantics_history"] = history[-MAX_HISTORY:]
    try:
        session._save()
    except (OSError, ValueError):
        summary["persisted"] = False
    else:
        summary["persisted"] = True
    operation_status = ("retry" if summary["degraded"] or summary["unknown"]
                        else "complete")
    result = {"summary": summary, "reused": False, "evidence": None,
              "operation_status": operation_status}
    if semantic_observation is not None:
        result["semantic_observation"] = semantic_observation
    return result
