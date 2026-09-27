"""Durable workspace claims: session ownership over repositories.

A claim names a repository, the owning session and consumer, the
acquisition basis, and a versioned lifecycle (held, released, expired,
transferred). Claims persist through the session store (Store-backed in
production), so they survive an individual model process disappearing. A
second session cannot mutate a claimed resource without a deliberate
transfer or recovery; unknown work is never claimable by inference.

This replaces the advisory file leases: one claim authority, persisted
like every other session object.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .identity import digest_hex

SCHEMA = "mncs.environment.workspace-claim/1"

BASIS_EXPLICIT = "explicit-claim"
BASIS_INTENT_SCOPE = "intent-scope"
BASIS_RECOVERY = "recovery"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _alive(record: dict[str, Any], now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    if record.get("status") != "held":
        return False
    try:
        return datetime.fromisoformat(str(record["expires_at"])) > now
    except ValueError:
        return False


def active_claims(all_records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Latest live record per repository; released/expired claims drop out."""
    latest: dict[str, dict[str, Any]] = {}
    for record in all_records:
        if not isinstance(record, dict):
            continue
        repo = str(record.get("repository", ""))
        if not repo:
            continue
        current = latest.get(repo)
        if current is None or int(record.get("version", 0)) > int(current.get("version", 0)):
            latest[repo] = record
    return {repo: record for repo, record in latest.items() if _alive(record)}


def holders(all_records: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """Map repository -> {session_id, consumer_id, basis} for live claims."""
    return {
        repo: {
            "session_id": str(record.get("session_id", "")),
            "consumer_id": str(record.get("consumer_id", "")),
            "basis": str(record.get("basis", "")),
        }
        for repo, record in active_claims(all_records).items()
    }


class ClaimConflict(Exception):
    """Raised when a repository is already claimed by another live session."""


def acquire(
    store,
    *,
    repository: str,
    session_id: str,
    consumer_id: str,
    basis: str,
    reason: str,
    ttl_hours: int = 24,
) -> dict[str, Any]:
    """Claim a repository; raises ClaimConflict when another session holds it."""
    if basis not in (BASIS_EXPLICIT, BASIS_INTENT_SCOPE, BASIS_RECOVERY):
        raise ValueError(f"unknown claim basis {basis!r}")
    records = store.read_claims()
    live = active_claims(records)
    existing = live.get(repository)
    if existing and existing.get("session_id") != session_id:
        raise ClaimConflict(
            f"{repository} claimed by session {existing.get('session_id')} "
            f"(consumer {existing.get('consumer_id')}, basis {existing.get('basis')})"
        )
    versions = [
        int(record.get("version", 0)) for record in records
        if str(record.get("repository", "")) == repository
    ]
    now = datetime.now(timezone.utc)
    record = {
        "schema_version": SCHEMA,
        "claim_id": f"claim:{repository}",
        "version": (max(versions) + 1) if versions else 1,
        "repository": repository,
        "session_id": session_id,
        "consumer_id": consumer_id,
        "basis": basis,
        "reason": reason,
        "status": "held",
        "acquired_at": now.isoformat(timespec="seconds"),
        "expires_at": (now + timedelta(hours=ttl_hours)).isoformat(timespec="seconds"),
        "provenance": {"acquired_by": consumer_id},
    }
    record["identity"] = "clm_" + digest_hex(
        {key: record[key] for key in sorted(record) if key != "identity"}
    )
    store.put_claim(record)
    return record


def release(
    store, *, repository: str, session_id: str, reason: str = ""
) -> dict[str, Any] | None:
    """Release a held claim; returns the release record, None when nothing held."""
    records = store.read_claims()
    live = active_claims(records)
    existing = live.get(repository)
    if not existing or existing.get("session_id") != session_id:
        return None
    versions = [
        int(record.get("version", 0)) for record in records
        if str(record.get("repository", "")) == repository
    ]
    record = {
        "schema_version": SCHEMA,
        "claim_id": f"claim:{repository}",
        "version": (max(versions) + 1) if versions else 1,
        "repository": repository,
        "session_id": session_id,
        "consumer_id": existing.get("consumer_id", ""),
        "basis": existing.get("basis", ""),
        "reason": reason,
        "status": "released",
        "acquired_at": existing.get("acquired_at", ""),
        "expires_at": existing.get("expires_at", ""),
        "released_at": utcnow(),
        "provenance": {"released_by": session_id},
    }
    record["identity"] = "clm_" + digest_hex(
        {key: record[key] for key in sorted(record) if key != "identity"}
    )
    store.put_claim(record)
    return record
