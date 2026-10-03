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

Recovery (`recovery` basis) supersedes an overlapping live claim only
when the owner's session state shows it is safe: a terminal (dead)
owner frees every scope, while a merely quiet or unknown owner frees
only non-exclusive scopes. Exclusive repository scopes held by a live
or quiet-but-undead owner still fail closed until TTL expiry. Every
recovery is explicit, version-checked against races, and recorded with
`recovered_from` provenance on both the superseding record and the new
claim.
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

#: Owner lifecycles that free every held scope immediately (dead owners).
TERMINAL_LIFECYCLES = ("completed", "failed")

#: Terminal record status written when a live claim is recovered.
STATUS_SUPERSEDED = "superseded"


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


def _latest_by_identity(all_records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Latest record per claim identity regardless of status."""
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
    return latest


def active_claims(all_records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Latest live record per claim identity; released/expired drop out."""
    return {key: record for key, record in _latest_by_identity(all_records).items()
            if _alive(record)}


def holders(all_records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Map repository -> live holder infos, including each exact scope."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in active_claims(all_records).values():
        scope = record.get("scope", {})
        if not isinstance(scope, dict):
            scope = {"kind": "repository"}
        grouped.setdefault(str(record.get("repository", "")), []).append(
            {
                "claim_id": str(record.get("claim_id", "")),
                "session_id": str(record.get("session_id", "")),
                "consumer_id": str(record.get("consumer_id", "")),
                "basis": str(record.get("basis", "")),
                "scope": scope,
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


def owner_state(store, session_id: str) -> dict[str, Any]:
    """Best-effort owner session state from the authoritative session store.

    Returns known/lifecycle/last_activity_at plus an error marker when the
    owner cannot be determined. Callers fail closed on error: an unreadable
    owner is never treated as dead.
    """
    state: dict[str, Any] = {"session_id": str(session_id), "known": False,
                             "lifecycle": None, "last_activity_at": None,
                             "error": None}
    try:
        snapshot = store.load_snapshot(session_id)
    except Exception:
        state["error"] = "session-state-unreadable"
        return state
    if not isinstance(snapshot, dict):
        return state
    state["known"] = True
    lifecycle = snapshot.get("lifecycle")
    state["lifecycle"] = str(lifecycle) if lifecycle is not None else None
    try:
        events = store.read_events(session_id)
    except Exception:
        state["error"] = "session-events-unreadable"
        return state
    latest: datetime | None = None
    if isinstance(events, list):
        for event in events:
            if not isinstance(event, dict):
                continue
            seen = _parse_time(event.get("observed_at"))
            if seen is not None and (latest is None or seen > latest):
                latest = seen
    if latest is None:
        # Lifecycle transitions are genuine session activity too; a
        # session that recently checkpointed or resumed is demonstrably
        # alive even when its event log is unavailable.
        history = snapshot.get("lifecycle_history")
        if isinstance(history, list):
            for transition in history:
                if not isinstance(transition, dict):
                    continue
                seen = _parse_time(transition.get("at"))
                if seen is not None and (latest is None or seen > latest):
                    latest = seen
    state["last_activity_at"] = (latest.isoformat(timespec="seconds") if latest else None)
    return state


def _freshness(age: timedelta) -> str:
    if age <= ACTIVE_WINDOW:
        return "active"
    if age <= STALE_WINDOW:
        return "idle"
    return "stale"


def classify(record: dict[str, Any], owner: dict[str, Any] | None, *,
             now: datetime | None = None) -> dict[str, Any]:
    """Explain one claim record as live/stale/recoverable/not-recoverable.

    Verdicts `released`, `expired`, and `superseded` are informational (the
    record blocks nothing). A `held`, unexpired record is `live` when its
    owner shows recent activity, `recoverable` when the owner is dead or
    the ownership went quietly stale on a non-exclusive scope, and
    `not-recoverable` otherwise. `now` shifts only the quiet-age
    comparison for inspection; TTL expiry always uses wall-clock time.
    """
    record = migrate_record(dict(record))
    owner = dict(owner or {})
    moment = now or datetime.now(timezone.utc)
    claim_id = str(record.get("claim_id", ""))
    holder = str(record.get("session_id", ""))
    status = record.get("status")
    base = {"claim_id": claim_id, "session_id": holder,
            "owner_known": bool(owner.get("known")),
            "owner_lifecycle": owner.get("lifecycle"),
            "last_activity_at": owner.get("last_activity_at")}
    if status == "released":
        return {**base, "verdict": "released", "freshness": None,
                "reason": "claim was released; it blocks nothing"}
    try:
        expired = datetime.fromisoformat(str(record["expires_at"])) <= moment
    except (KeyError, ValueError):
        expired = True
    if expired:
        return {**base, "verdict": "expired", "freshness": None,
                "reason": "claim TTL elapsed; it blocks nothing"}
    if status == STATUS_SUPERSEDED:
        return {**base, "verdict": "superseded", "freshness": None,
                "reason": "claim was superseded by recovery; it blocks nothing"}
    if status != "held":
        return {**base, "verdict": "not-recoverable", "freshness": None,
                "reason": f"unrecognized claim status {status!r}; failing closed"}
    lifecycle = owner.get("lifecycle")
    if owner.get("error") and lifecycle not in TERMINAL_LIFECYCLES:
        return {**base, "verdict": "not-recoverable", "freshness": None,
                "reason": "owner session state is unreadable; failing closed"}
    if lifecycle in TERMINAL_LIFECYCLES:
        return {**base, "verdict": "recoverable", "freshness": "stale",
                "reason": f"owner session {holder} is {lifecycle}; "
                          "orphaned scope frees before TTL expiry"}
    scope = record.get("scope") or {}
    exclusive = bool(scope.get("exclusive", scope.get("kind") == "repository"))
    seen = _parse_time(owner.get("last_activity_at"))
    if seen is None:
        acquired = _parse_time(record.get("acquired_at"))
        if acquired is None:
            return {**base, "verdict": "not-recoverable", "freshness": None,
                    "reason": "no owner activity and no acquisition time; failing closed"}
        age = moment - acquired
        if owner.get("known"):
            return {**base, "verdict": "not-recoverable",
                    "freshness": _freshness(age),
                    "reason": f"owner session {holder} shows no recorded activity "
                              "and the claim is too fresh to judge"}
        freshness = _freshness(age)
        if freshness != "stale":
            return {**base, "verdict": "not-recoverable", "freshness": freshness,
                    "reason": "owner session is unknown but the claim is too fresh to judge"}
        if exclusive:
            return {**base, "verdict": "not-recoverable", "freshness": freshness,
                    "reason": "owner session is unknown and exclusive scope needs "
                              "a dead owner or TTL expiry"}
        return {**base, "verdict": "recoverable", "freshness": freshness,
                "reason": "owner session is unknown and the claim went quietly "
                          "stale on a non-exclusive scope"}
    freshness = _freshness(moment - seen)
    if freshness in ("active", "idle"):
        return {**base, "verdict": "live", "freshness": freshness,
                "reason": f"owner session {holder} shows activity "
                          f"({owner.get('last_activity_at')}); never seize live scope"}
    if exclusive:
        return {**base, "verdict": "not-recoverable", "freshness": freshness,
                "reason": f"owner session {holder} is quiet but the exclusive scope "
                          "needs a dead owner or TTL expiry"}
    return {**base, "verdict": "recoverable", "freshness": freshness,
            "reason": f"owner session {holder} went quietly stale; "
                      "non-exclusive scope is recoverable"}


def explain(record: dict[str, Any], store, *,
            now: datetime | None = None) -> dict[str, Any]:
    """Classify one record, resolving its owner from the session store."""
    return classify(record, owner_state(store, record.get("session_id", "")),
                    now=now)


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


def _recover(store, victims: list[tuple[str, dict[str, Any]]], *,
             session_id: str, consumer_id: str, reason: str,
             now: datetime | None = None) -> list[dict[str, Any]]:
    """Supersede recoverable victims with version-checked (CAS) writes.

    Every victim is re-verified against a fresh read immediately before
    its superseding write, and every write is re-verified after: a victim
    that advanced (renewal, release, competing recovery) aborts the whole
    recovery with ClaimConflict before any new claim is written.
    """
    from .session_store import ClaimVersionConflict
    fresh = store.read_claims()
    live = active_claims(fresh)
    for victim_id, victim in victims:
        latest = live.get(victim_id)
        if latest is None or int(latest.get("version", 0)) != int(victim.get("version", 0)):
            raise ClaimConflict(
                f"recovery target {victim_id} changed during recovery; "
                "re-evaluate before retrying")
    moment = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    written: list[dict[str, Any]] = []
    for victim_id, victim in victims:
        versions = [int(record.get("version", 0)) for record in fresh
                    if str(record.get("claim_id", "")) == victim_id]
        supersede = {
            "schema_version": SCHEMA,
            "claim_id": victim_id,
            "version": (max(versions) + 1) if versions else 1,
            "repository": str(victim.get("repository", "")),
            "scope": victim.get("scope", {}),
            "session_id": str(victim.get("session_id", "")),
            "consumer_id": str(victim.get("consumer_id", "")),
            "basis": str(victim.get("basis", "")),
            "reason": str(victim.get("reason", "")),
            "status": STATUS_SUPERSEDED,
            "acquired_at": str(victim.get("acquired_at", "")),
            "expires_at": str(victim.get("expires_at", "")),
            "superseded_at": moment,
            "provenance": {"recovered_by": session_id,
                           "recovered_consumer": consumer_id,
                           "recovered_from_version": int(victim.get("version", 0)),
                           "recovery_reason": reason},
        }
        try:
            written.append(_record(store, supersede))
        except ClaimVersionConflict as error:
            raise ClaimConflict(
                f"lost recovery race for {victim_id}: {error}") from error
    confirmed = _latest_by_identity(store.read_claims())
    for ours in written:
        current = confirmed.get(ours["claim_id"])
        if current is None or current.get("identity") != ours.get("identity"):
            raise ClaimConflict(
                f"lost recovery race for {ours['claim_id']}; another session "
                "superseded it concurrently")
    return written


def _conflict_text(claim_id: str, other_id: str, other: dict[str, Any],
                   reason_: str) -> str:
    return (f"{claim_id} overlaps {other_id} held by session "
            f"{other.get('session_id')} (consumer {other.get('consumer_id')}, "
            f"basis {other.get('basis')}): {reason_}")


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
    now: datetime | None = None,
) -> dict[str, Any]:
    """Claim a scope; conflicts and unknown-work adoption fail closed.

    The `recovery` basis additionally supersedes overlapping live claims
    whose owners classify as recoverable (dead, or quietly stale on a
    non-exclusive scope). Every other basis fails closed on any live
    overlap. `now` shifts only recovery quiet-age comparison; TTL expiry
    always uses wall-clock time.
    """
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
    conflicts = []
    recovered: list[dict[str, Any]] = []
    for other_id, other in live.items():
        if other.get("session_id") == session_id:
            continue
        reason_ = scopes_conflict(resolved, other.get("scope", {}))
        if reason_ is not None:
            conflicts.append((other_id, other, reason_))
    if conflicts and basis == BASIS_RECOVERY:
        assessments = []
        for other_id, other, reason_ in conflicts:
            verdict = explain(other, store, now=now)
            assessments.append((other_id, other, reason_, verdict))
        blocked = [item for item in assessments
                   if item[3].get("verdict") != "recoverable"]
        if blocked:
            detail = "; ".join(
                f"{other_id} held by {other.get('session_id')}: "
                f"{verdict.get('verdict')} ({verdict.get('reason')})"
                for other_id, other, _, verdict in blocked)
            raise ClaimConflict(
                f"{claim_id} cannot recover overlapping scope: {detail}")
        recovered = _recover(
            store, [(other_id, other) for other_id, other, _, _ in assessments],
            session_id=session_id, consumer_id=consumer_id, reason=reason,
            now=now)
        records = store.read_claims()
        live = active_claims(records)
        for other_id, other in live.items():
            if other.get("session_id") == session_id:
                continue
            reason_ = scopes_conflict(resolved, other.get("scope", {}))
            if reason_ is not None:
                raise ClaimConflict(
                    _conflict_text(claim_id, other_id, other, reason_) +
                    " (acquired during recovery; re-evaluate before retrying)")
    elif conflicts:
        other_id, other, reason_ = conflicts[0]
        try:
            verdict = explain(other, store, now=now)
            suffix = f" [{verdict.get('verdict')}: {verdict.get('reason')}]"
        except Exception:
            suffix = ""
        raise ClaimConflict(
            _conflict_text(claim_id, other_id, other, reason_) + suffix)
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
    # Record timestamps always use wall-clock time; the `now` parameter
    # shifts only recovery quiet-age comparison, never TTL.
    moment = datetime.now(timezone.utc)
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
        "acquired_at": moment.isoformat(timespec="seconds"),
        "expires_at": (moment + timedelta(hours=ttl_hours)).isoformat(timespec="seconds"),
        "provenance": {"acquired_by": consumer_id},
    }
    if basis in ADOPTION_BASES:
        record["provenance"]["adopted_head"] = facts.get("head")
        record["provenance"]["adopted_dirty"] = facts.get("dirty")
        record["provenance"]["adopted_signals"] = facts.get("foreign_signals")
    if basis == BASIS_RECOVERY and recovered:
        record["provenance"]["recovered_from"] = [
            {"claim_id": str(item["claim_id"]),
             "version": int(item["provenance"]["recovered_from_version"]),
             "superseded_version": int(item["version"]),
             "owner_session": str(item["session_id"])}
            for item in recovered
        ]
    try:
        return _record(store, record)
    except Exception as error:
        from .session_store import ClaimVersionConflict
        if isinstance(error, ClaimVersionConflict):
            raise ClaimConflict(
                f"{claim_id} lost a concurrent acquisition race; retry") from error
        raise


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
