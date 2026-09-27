"""Typed environment events, subscriptions, and replay cursors.

Events are typed observations correlated to a session with causal
references. Delivery here is log-based: every event is appended to the
session log with a sequence number; subscriptions track cursors into that
log, so an absent consumer replays what it missed instead of
rediscovering the world. Polling adapters are explicit adapters, never
the canonical semantic.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .identity import digest_hex, event_id

SCHEMA = "mncs.environment.event/1"

# Canonical event vocabulary. Producers must use these types; unknown
# provider occurrences arrive as adapter events with the provider's own
# name preserved in the payload, never silently remapped.
TYPES = (
    "session.created",
    "session.resumed",
    "session.checkpointed",
    "session.handed_off",
    "session.completed",
    "session.failed",
    "session.blocked",
    "session.unblocked",
    "intent.attached",
    "workspace.changed",
    "workspace.protection_raised",
    "capability.bound",
    "capability.available",
    "capability.unavailable",
    "capability.invoked",
    "invocation.completed",
    "authority.denied",
    "authority.escalated",
    "lease.acquired",
    "lease.released",
    "checkpoint.created",
    "handoff.created",
    "pressure.recorded",
    "adapter.observed",
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def make(
    *,
    session_id: str,
    sequence: int,
    event_type: str,
    producer: str,
    payload: dict[str, Any] | None = None,
    causes: list[str] | None = None,
) -> dict[str, Any]:
    """Build a typed event record (sequence assigned by the session log)."""
    body = dict(payload or {})
    record = {
        "schema_version": SCHEMA,
        "sequence": sequence,
        "type": event_type if event_type in TYPES else "adapter.observed",
        "provider_type": event_type if event_type not in TYPES else None,
        "producer": producer,
        "session_id": session_id,
        "payload": body,
        "causes": list(causes or []),
        "observed_at": utcnow(),
    }
    record["identity"] = event_id(session_id, sequence, record["type"], digest_hex(body))
    return record


def subscribe(
    *,
    subscription_id: str,
    session_id: str,
    event_types: list[str],
    source_filter: str | None = None,
    cursor: int = 0,
) -> dict[str, Any]:
    return {
        "subscription_id": subscription_id,
        "session_id": session_id,
        "event_types": list(event_types),
        "source_filter": source_filter,
        "cursor": cursor,
    }


def matches(subscription: dict[str, Any], event: dict[str, Any]) -> bool:
    if event.get("session_id") != subscription.get("session_id"):
        return False
    if event.get("sequence", 0) <= subscription.get("cursor", 0):
        return False
    wanted = subscription.get("event_types", [])
    if "*" in wanted or event.get("type") in wanted:
        source_filter = subscription.get("source_filter")
        if source_filter and event.get("producer") != source_filter:
            return False
        return True
    return False


def deliverable(
    subscription: dict[str, Any], log: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return due events and an advanced subscription (pure; caller persists)."""
    due = [event for event in log if matches(subscription, event)]
    advanced = dict(subscription)
    if due:
        advanced["cursor"] = max(event["sequence"] for event in due)
    return due, advanced


def git_poll_events(
    *,
    session_id: str,
    sequence_start: int,
    previous_heads: dict[str, str | None],
    current_heads: dict[str, str | None],
) -> tuple[list[dict[str, Any]], int]:
    """Adapter: head changes between two workspace observations become events.

    Explicitly an adapter (polling), not canonical provider events. Callers
    record these with producer "adapter:git-poll".
    """
    events: list[dict[str, Any]] = []
    sequence = sequence_start
    for repo in sorted(set(previous_heads) | set(current_heads)):
        before, after = previous_heads.get(repo), current_heads.get(repo)
        if before != after:
            events.append(
                make(
                    session_id=session_id,
                    sequence=sequence,
                    event_type="workspace.changed",
                    producer="adapter:git-poll",
                    payload={"repository": repo, "previous_head": before, "current_head": after},
                )
            )
            sequence += 1
    return events, sequence
