"""Durable workspace claims: session ownership over scoped resources.

A claim names a repository plus the smallest useful scope: a whole
repository, one worktree checkout, or a set of repo-relative paths. Two
disjoint scopes may coexist; overlapping mutation scopes conflict
deterministically. Claims persist through the session store, so they
survive an individual model process disappearing.

Dirty or foreign checkouts are never silently adopted: acquiring a scope
whose observed facts show unknown work requires an explicit adoption or
recovery basis that records what was adopted. Liveness is derived from
session activity, never renewed by the observer: quiet ownership goes
stale and becomes recoverable, it is never seized silently.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .identity import digest_hex

SCHEMA = "mncs.environment.workspace-claim/2"
LEGACY_SCHEMA = "mncs.environment.workspace-claim/1"

BASIS_EXPLICIT = "explicit-claim"
BASIS_INTENT_SCOPE = "intent-scope"
BASIS_RECOVERY = "recovery"
BASIS_ADOPTION = "explicit-adoption"
BASIS_TRANSFER = "transfer"

ADOPTION_BASES = (BASIS_RECOVERY, BASIS_ADOPTION)

#: Quiet thresholds for derived liveness.
ACTIVE_WINDOW = timedelta(minutes=15)
STALE_WINDOW = timedelta(hours=1)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def normalize_scope(scope: dict[str, Any] | None, repository: str) -> dict[str, Any]:
    """Validate a scope mapping into canonical form (repo-relative paths)."""
    scope = dict(scope or {})
    kind = scope.get("kind", "repository")
    if kind not in ("repository", "worktree", "paths"):
        raise ValueError(f"unknown claim scope kind {kind!r}")
    paths = scope.get("paths")
    if kind == "paths":
        if not isinstance(paths, list) or not paths or not all(
            isinstance(p, str) and p and not p.startswith("/") for p in paths
        ):
            raise ValueError("paths scope needs a non-empty list of repo-relative paths")
        paths = sorted({p.strip("/") for p in paths})
    else:
        paths = None
    checkout = scope.get("checkout")
    if kind == "worktree" and (not isinstance(checkout, str) or not checkout):
        raise ValueError("worktree scope needs a checkout path")
    if kind != "worktree":
        checkout = None
    return {
        "kind": kind,
        "repository": repository,
        "checkout": checkout,
        "branch": scope.get("branch"),
        "paths": paths,
        "exclusive": bool(scope.get("exclusive", kind == "repository")),
    }


def migrate_record(record: dict[str, Any]) -> dict[str, Any]:
    """Upgrade a legacy /1 record to current shape (repository scope)."""
    record = dict(record)
    if record.get("schema_version") == SCHEMA:
        return record
    record["schema_version"] = SCHEMA
    record["scope"] = normalize_scope(None, str(record.get("repository", "")))
    return record


def claim_identity(repository: str, scope: dict[str, Any]) -> str:
    if scope["kind"] == "repository":
        return f"claim:{repository}"
    digest = digest_hex(
        {"kind": scope["kind"], "checkout": scope["checkout"],
         "branch": scope["branch"], "paths": scope["paths"]}
    )[:12]
    return f"claim:{repository}:{scope['kind']}:{digest}"


def _paths_overlap(left: list[str] | None, right: list[str] | None) -> bool:
    """None means whole checkout: overlaps everything."""
    if left is None or right is None:
        return True
    for a in left:
        for b in right:
            if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
                return True
    return False


def scopes_conflict(left: dict[str, Any], right: dict[str, Any]) -> str | None:
    """Deterministic overlap verdict for two scopes on the same repository.

    Returns a reason string on conflict, None when the scopes coexist.
    Checkouts are separate directories: same-branch work across different
    checkouts still shares refs, so whole-checkout or overlapping paths
    on one branch conflict; different branches coexist.
    """
    if left.get("repository") != right.get("repository"):
        return None
    same_checkout = (left.get("checkout") or None) == (right.get("checkout") or None)
    if same_checkout:
        if _paths_overlap(left.get("paths"), right.get("paths")):
            return "same checkout with overlapping paths"
        return None
    if (left.get("branch") or None) != (right.get("branch") or None):
        return None
    if _paths_overlap(left.get("paths"), right.get("paths")):
        return "shared branch with overlapping paths across checkouts"
    return None


def _alive(record: dict[str, Any], now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    if record.get("status") != "held":
        return False
    try:
        return datetime.fromisoformat(str(record["expires_at"])) > now
    except ValueError:
        return False


def active_claims(all_records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Latest live record per claim identity; released/expired drop out."""
    latest: dict[str, dict[str, Any]] = {}
    for raw in all_records:
        if not isinstance(raw, dict):
            continue
        record = migrate_record(raw)
        identity = str(record.get("claim_id", ""))
        if not identity:
            continue
        current = latest.get(identity)
        if current is None or int(record.get("version", 0)) > int(current.get("version", 0)):
            latest[identity] = record
    return {key: record for key, record in latest.items() if _alive(record)}


def holders(all_records: list[dict[str, Any]]) -> dict[str, list[dict[str, str]]]:
    """Map repository -> live holder infos (multiple scopes may coexist)."""
    grouped: dict[str, list[dict[str, str]]] = {}
    for record in active_claims(all_records).values():
        scope = record.get("scope", {})
        grouped.setdefault(str(record.get("repository", "")), []).append(
            {
                "claim_id": str(record.get("claim_id", "")),
                "session_id": str(record.get("session_id", "")),
                "consumer_id": str(record.get("consumer_id", "")),
                "basis": str(record.get("basis", "")),
                "scope_kind": str(scope.get("kind", "repository")),
            }
        )
    return grouped


def liveness(
    record: dict[str, Any],
    last_activity_at: Any | None,
    now: datetime | None = None,
) -> str:
    """Derived ownership freshness: active|idle|stale|expired|released.

    No write renews this; only genuine session activity (events) moves
    it back toward active. Stale ownership is recoverable, never seized.
    """
    status = record.get("status")
    if status == "released":
        return "released"
    moment = now or datetime.now(timezone.utc)
    try:
        expired = datetime.fromisoformat(str(record["expires_at"])) <= moment
    except ValueError:
        expired = True
    if expired:
        return "expired"
    if status != "held":
        return str(status)
    seen = _parse_time(last_activity_at)
    if seen is None:
        return "stale"
    age = moment - seen
    if age <= ACTIVE_WINDOW:
        return "active"
    if age <= STALE_WINDOW:
        return "idle"
    return "stale"


class ClaimConflict(Exception):
    """Raised when a scope overlaps another live session's scope."""


class ClaimAdoptionRequired(Exception):
    """Raised when a checkout shows unknown work and the basis is ordinary."""

    def __init__(self, message: str, facts: dict[str, Any] | None = None):
        super().__init__(message)
        self.facts = facts or {}


def _record(store, record: dict[str, Any]) -> dict[str, Any]:
    record["identity"] = "clm_" + digest_hex(
        {key: record[key] for key in sorted(record) if key != "identity"}
    )
    store.put_claim(record)
    return record


def acquire(
    store,
    *,
    repository: str,
    session_id: str,
    consumer_id: str,
    basis: str,
    reason: str,
    ttl_hours: int = 24,
    scope: dict[str, Any] | None = None,
    checkout_facts: dict[str, Any] | None = None,
    workspace_root: str | None = None,
) -> dict[str, Any]:
    """Claim a scope; conflicts and unknown-work adoption fail closed."""
    allowed = (BASIS_EXPLICIT, BASIS_INTENT_SCOPE, BASIS_RECOVERY,
               BASIS_ADOPTION, BASIS_TRANSFER)
    if basis not in allowed:
        raise ValueError(f"unknown claim basis {basis!r}")
    resolved = normalize_scope(scope, repository)
    if resolved["kind"] == "worktree":
        from pathlib import Path
        checkout = Path(str(resolved["checkout"]))
        if workspace_root and workspace_root not in ("", "."):
            root = Path(workspace_root).resolve()
            if root not in (checkout.resolve(), *checkout.resolve().parents):
                raise ValueError("worktree checkout escapes the workspace root")
        if not checkout.is_dir():
            raise ValueError(f"worktree checkout {checkout} is not a directory")
    records = store.read_claims()
    live = active_claims(records)
    claim_id = claim_identity(repository, resolved)
    for other_id, other in live.items():
        if other.get("session_id") == session_id:
            continue
        reason_ = scopes_conflict(resolved, other.get("scope", {}))
        if reason_ is not None:
            raise ClaimConflict(
                f"{claim_id} overlaps {other_id} held by session "
                f"{other.get('session_id')} (consumer {other.get('consumer_id')}, "
                f"basis {other.get('basis')}): {reason_}"
            )
    facts = checkout_facts or {}
    dirty = bool(facts.get("dirty")) or bool(facts.get("foreign_signals"))
    if dirty and basis not in ADOPTION_BASES and not any(
        other.get("session_id") == session_id for other in live.values()
        if str(other.get("repository", "")) == repository
    ):
        raise ClaimAdoptionRequired(
            f"{repository} checkout shows unknown work "
            f"(dirty={facts.get('dirty')}, signals={facts.get('foreign_signals')}); "
            "acquire with explicit-adoption or recovery basis",
            facts={"dirty": facts.get("dirty"),
                   "foreign_signals": facts.get("foreign_signals"),
                   "head": facts.get("head"), "branch": facts.get("branch")},
        )
    versions = [
        int(record.get("version", 0)) for record in records
        if str(record.get("claim_id", "")) == claim_id
    ]
    now = datetime.now(timezone.utc)
    record = {
        "schema_version": SCHEMA,
        "claim_id": claim_id,
        "version": (max(versions) + 1) if versions else 1,
        "repository": repository,
        "scope": resolved,
        "session_id": session_id,
        "consumer_id": consumer_id,
        "basis": basis,
        "reason": reason,
        "status": "held",
        "acquired_at": now.isoformat(timespec="seconds"),
        "expires_at": (now + timedelta(hours=ttl_hours)).isoformat(timespec="seconds"),
        "provenance": {"acquired_by": consumer_id},
    }
    if basis in ADOPTION_BASES:
        record["provenance"]["adopted_head"] = facts.get("head")
        record["provenance"]["adopted_dirty"] = facts.get("dirty")
        record["provenance"]["adopted_signals"] = facts.get("foreign_signals")
    return _record(store, record)


def release(
    store, *, session_id: str, reason: str = "",
    claim_id: str | None = None, repository: str | None = None,
) -> list[dict[str, Any]]:
    """Release own held claims by identity (or every own scope on a repo)."""
    if not claim_id and not repository:
        raise ValueError("release needs claim_id or repository")
    records = store.read_claims()
    live = active_claims(records)
    targets = [
        record for record in live.values()
        if record.get("session_id") == session_id
        and (claim_id is None or str(record.get("claim_id")) == claim_id)
        and (repository is None or str(record.get("repository")) == repository)
    ]
    released: list[dict[str, Any]] = []
    for existing in targets:
        versions = [
            int(record.get("version", 0)) for record in records
            if str(record.get("claim_id", "")) == str(existing.get("claim_id"))
        ]
        released.append(_record(store, {
            "schema_version": SCHEMA,
            "claim_id": str(existing.get("claim_id")),
            "version": (max(versions) + 1) if versions else 1,
            "repository": str(existing.get("repository")),
            "scope": existing.get("scope", {}),
            "session_id": session_id,
            "consumer_id": str(existing.get("consumer_id", "")),
            "basis": str(existing.get("basis", "")),
            "reason": reason,
            "status": "released",
            "acquired_at": str(existing.get("acquired_at", "")),
            "expires_at": str(existing.get("expires_at", "")),
            "released_at": utcnow(),
            "provenance": {"released_by": session_id},
        }))
    return released


def transfer(
    store, *, claim_id: str, from_session: str, to_session: str,
    to_consumer: str, reason: str = "",
) -> dict[str, Any]:
    """Explicit ownership transfer: closes the old record, opens the new one."""
    records = store.read_claims()
    live = active_claims(records)
    existing = live.get(claim_id)
    if existing is None or existing.get("session_id") != from_session:
        raise ClaimConflict(f"{claim_id} is not held by session {from_session}")
    release(store, session_id=from_session, claim_id=claim_id,
            reason=f"transferred to {to_session}: {reason}")
    versions = [
        int(record.get("version", 0)) for record in store.read_claims()
        if str(record.get("claim_id", "")) == claim_id
    ]
    now = datetime.now(timezone.utc)
    try:
        expires = datetime.fromisoformat(str(existing.get("expires_at", "")))
    except ValueError:
        expires = now + timedelta(hours=24)
    return _record(store, {
        "schema_version": SCHEMA,
        "claim_id": claim_id,
        "version": (max(versions) + 1) if versions else 1,
        "repository": str(existing.get("repository")),
        "scope": existing.get("scope", {}),
        "session_id": to_session,
        "consumer_id": to_consumer,
        "basis": BASIS_TRANSFER,
        "reason": reason,
        "status": "held",
        "acquired_at": now.isoformat(timespec="seconds"),
        "expires_at": expires.isoformat(timespec="seconds"),
        "provenance": {"transferred_from": from_session,
                       "transferred_by": from_session},
    })
