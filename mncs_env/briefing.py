"""Compact agent update capsules: what changed that matters to this work.

A capsule answers "what changed since my last acknowledged cursor"
with bounded structured facts, ordered safety-first. Structured facts
are authoritative; the one-line summary is a mechanical projection of
counts, never a judgment (no completion percentages, no inferred
intent).

Cursors are durable per-session event indexes. Reconciler-derived
items are recomputed current relevant state (content-identified, so
consumers can dedup); only the own-log cursor advances on ack, and it
never moves backward or jumps silently past the log end.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

#: Events scanned per brief (bounded).
MAX_SCAN_EVENTS = 200

#: Items admitted per capsule (bounded).
MAX_BRIEF_ITEMS = 30

#: Reconciler observations considered per brief (bounded).
MAX_RECONCILER_ITEMS = 50


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Priority order follows the mission: safety first, progress last.
PRIORITY = {
    "safety": 1,
    "ownership": 2,
    "blockers": 3,
    "diagnostics": 4,
    "verification": 5,
    "coordination": 6,
    "provider": 7,
    "resource": 8,
    "progress": 9,
}

#: Session-log event types mapped to (category, relevance reason).
EVENT_CLASSES = {
    "authority.denied": ("safety", "an authority denial names this session"),
    "authority.escalated": ("safety", "an escalation needs a decision"),
    "lease.acquired": ("ownership", "claim ownership changed"),
    "lease.released": ("ownership", "claim ownership changed"),
    "lease.transferred": ("ownership", "claim ownership changed"),
    "invocation.completed": ("verification", "a capability invocation finished"),
    "capability.invoked": ("progress", "work was invoked"),
    "capability.bound": ("provider", "a capability was bound from a provider"),
    "capability.changed": ("diagnostics", "a bound capability changed"),
    "capability.unbound": ("diagnostics", "a bound capability was removed"),
    "capability.available": ("provider", "a capability changed availability"),
    "capability.unavailable": ("provider", "a capability changed availability"),
    "session.checkpointed": ("progress", "a checkpoint bounds prior work"),
    "handoff.created": ("coordination", "a handoff names a new consumer"),
    "session.resumed": ("progress", "participation resumed"),
    "session.completed": ("progress", "lifecycle reached completed"),
    "session.failed": ("diagnostics", "lifecycle reached failed"),
    "session.created": ("progress", "session created"),
    "adapter.observed": ("coordination", "an external adapter reported"),
    "reconciler.observed": ("coordination", "background reconciliation saw a delta"),
    "reconciler.source-reset": ("diagnostics", "a source reset; history may be incomplete"),
}


def classify_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """Project one session-log event to a capsule item (None when routine)."""
    kind = str(event.get("type", ""))
    mapping = EVENT_CLASSES.get(kind)
    if mapping is None:
        return None
    category, reason = mapping
    payload = event.get("payload", {})
    if not isinstance(payload, dict):
        payload = {}
    status = str(payload.get("status", ""))
    if kind == "invocation.completed" and status == "ok":
        category, reason = "progress", "an invocation succeeded"
    item = {
        "id": f"{event.get('session_id')}:{event.get('sequence')}",
        "category": category,
        "priority": PRIORITY[category],
        "kind": kind,
        "subject": str(payload.get("capability", payload.get("repository",
                     payload.get("claim_id", payload.get("action", ""))))),
        "summary": str(payload.get("reason", payload.get("summary", kind))),
        "at": str(event.get("observed_at", "")),
        "relevance": reason,
        "ref": {"session_id": event.get("session_id"),
                "sequence": event.get("sequence")},
    }
    if kind in ("authority.denied", "session.failed") or status == "failed":
        item["priority"] = PRIORITY["safety"] if kind == "authority.denied" else PRIORITY["diagnostics"]
        item["category"] = "safety" if kind == "authority.denied" else "diagnostics"
    return item


def _relevant_observation(payload: dict[str, Any], session) -> str | None:
    """Explain why a reconciler observation matters to this session (or None)."""
    relations = payload.get("relations", {})
    if not isinstance(relations, dict):
        return None
    intent = session.snapshot.get("intent", {})
    repos = set(intent.get("repositories", []))
    work_ids = set(intent.get("commons_work", []))
    if relations.get("session_id") == session.session_id:
        return "names this session"
    subjects = {str(relations.get(k, "")) for k in
                ("session_id", "claim", "repository", "uri") if relations.get(k)}
    mentioned = {str(r) for r in relations.get("repositories", []) or []}
    if repos and (repos & mentioned or repos & subjects):
        return "touches a repository in this intent"
    if work_ids and str(relations.get("commons_work", "")) in work_ids:
        return "links work this session references"
    if payload.get("kind") == "claim.changed":
        return "claim state changed somewhere in the family"
    severity = str(payload.get("severity", ""))
    if severity in ("warning", "critical"):
        return "elevated severity is family-visible"
    return None


def _progress(session, live_claims: list[dict[str, Any]],
              commons_work: dict[str, Any]) -> dict[str, Any]:
    snapshot = session.snapshot
    artifacts = snapshot.get("artifacts", [])
    invocations = [a for a in artifacts if a.get("kind") == "invocation-result"]
    ok = sum(1 for a in invocations if a.get("status") == "ok")
    failed = sum(1 for a in invocations if a.get("status") not in ("ok", None))
    escalations = [e for e in session._log()
                   if e.get("type") == "authority.escalated"][-5:]
    return {
        "lifecycle": snapshot.get("lifecycle"),
        "consumer_id": snapshot.get("consumer_id"),
        "checkpoints": len(snapshot.get("checkpoints", [])),
        "handoffs": len(snapshot.get("handoffs", [])),
        "artifacts": len(artifacts),
        "invocations": {"ok": ok, "failed": failed},
        "claims_held": [
            {"claim_id": c.get("claim_id"), "repository": c.get("repository"),
             "scope_kind": (c.get("scope") or {}).get("kind")}
            for c in live_claims
            if c.get("session_id") == session.session_id],
        "pending_escalations": [
            {"action": (e.get("payload") or {}).get("action"),
             "target": (e.get("payload") or {}).get("target")}
            for e in escalations],
        "linked_commons_work": commons_work,
        "completion": snapshot.get("completion"),
    }


def build_capsule(session, store, *,
                  commons_work: dict[str, Any] | None = None,
                  reconciler_log: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Assemble the current capsule (does not advance any cursor)."""
    log = session._log()
    total = len(log)
    cursor = session.snapshot.get("brief_cursor", {})
    index = int(cursor.get("index", 0)) if isinstance(cursor, dict) else 0
    index = max(0, min(index, total))
    own_items: list[dict[str, Any]] = []
    for event in log[max(0, total - MAX_SCAN_EVENTS):]:
        sequence = event.get("sequence")
        if not isinstance(sequence, int) or sequence <= index:
            continue
        item = classify_event(event)
        if item is not None:
            own_items.append(item)
    cross_items: list[dict[str, Any]] = []
    for event in (reconciler_log or [])[-MAX_RECONCILER_ITEMS:]:
        payload = event.get("payload", {})
        if not isinstance(payload, dict):
            continue
        reason = _relevant_observation(payload, session)
        if reason is None:
            continue
        observation = str(payload.get("observation", ""))
        cross_items.append({
            "id": f"reconciler:{observation or event.get('sequence')}",
            "category": "coordination",
            "priority": PRIORITY["coordination"],
            "kind": str(payload.get("kind", "reconciler.observed")),
            "subject": str(payload.get("subject", "")),
            "summary": str(payload.get("summary", "")),
            "at": str(event.get("observed_at", "")),
            "relevance": reason,
            "ref": {"session_id": event.get("session_id"),
                    "sequence": event.get("sequence"),
                    "observation": observation},
        })
    live_claims = [c for c in store.read_claims()
                   if c.get("status") == "held"] if store is not None else []
    progress = _progress(session, live_claims, commons_work or {})
    items = sorted(own_items + cross_items,
                   key=lambda i: (i["priority"], i["at"]))[:MAX_BRIEF_ITEMS]
    counts: dict[str, int] = {}
    for item in items:
        counts[item["category"]] = counts.get(item["category"], 0) + 1
    summary = (
        f"session {session.session_id} is {progress['lifecycle']}; "
        f"{total - index} new events since cursor {index}, "
        f"{len(items)} relevant items "
        f"({', '.join(f'{v} {k}' for k, v in sorted(counts.items())) or 'none'}); "
        f"{progress['invocations']['ok']} invocations ok, "
        f"{progress['invocations']['failed']} failed, "
        f"{len(progress['claims_held'])} claims held, "
        f"{len(progress['pending_escalations'])} recent escalations."
    )
    return {
        "schema_version": "mncs.environment.update-capsule/1",
        "session_id": session.session_id,
        "at": utcnow(),
        "cursor": {"index": index, "total": total},
        "intent_goal": (session.snapshot.get("intent") or {}).get("goal", ""),
        "progress": progress,
        "items": items,
        "truncated": len(own_items + cross_items) > len(items),
        "summary": summary,
    }


def acknowledge(session, index: int, consumer: str) -> dict[str, Any]:
    """Advance the durable brief cursor (never backward, never past the end)."""
    total = len(session._log())
    previous = session.snapshot.get("brief_cursor", {})
    previous_index = int(previous.get("index", 0)) if isinstance(previous, dict) else 0
    if index <= previous_index:
        return {"cursor": {"index": previous_index, "total": total},
                "note": "cursor not moved backward"}
    if index > total:
        session.snapshot["brief_cursor"] = {"index": total, "by": consumer,
                                            "at": utcnow()}
        session._save()
        return {"cursor": {"index": total, "total": total},
                "note": f"cursor clamped to log end (asked {index})"}
    session.snapshot["brief_cursor"] = {"index": index, "by": consumer,
                                        "at": utcnow()}
    session._save()
    return {"cursor": {"index": index, "total": total}, "note": "acknowledged"}
