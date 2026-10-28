"""Durable workspace claims: session ownership over scoped resources.

A claim names a repository plus the smallest useful scope: a whole
repository, one worktree checkout, or a set of repo-relative paths. Two
disjoint scopes may coexist; overlapping mutation scopes conflict
deterministically. Claims persist through the session store, so they
survive an individual model process disappearing.

Dirty or foreign checkouts are never silently adopted: acquiring a scope
whose observed facts show unknown work requires an explicit adoption or
recovery basis that records what was adopted. Liveness is derived from
session activity or an explicit owner renewal, never by the observer: quiet
ownership goes stale and becomes recoverable, it is never seized silently.

An overlapping claim classified as recoverable is superseded atomically as
part of an ordinary acquisition. A terminal owner frees every scope, while a
quiet or unknown owner frees only non-exclusive scopes; active, uncertain, or
exclusive quiet ownership still fails closed. Recovery is version-checked,
uses the existing liveness contract, and records `recovered_from` provenance
on both the superseding record and the new claim. The `recovery` basis remains
available when a caller specifically intends recovery. New leases are bounded
to 168 hours; a continuing owner reacquires its exact scope instead of
extending a lease with an unbounded duration.
Legacy records keep their original expiry in the history, but ownership
checks cap an overlong lease at acquisition plus the current maximum.
"""

from __future__ import annotations

import re
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
NON_OWNER_ACTIVITY_EVENT_TYPES = frozenset({"environment.rpc.read_observed"})
MAX_OWNER_ACTIVITY_EVENT_TAIL = 256
MAX_OWNER_LIVENESS_INSPECTIONS = 16

#: Owner lifecycles that free every held scope immediately (dead owners).
TERMINAL_LIFECYCLES = ("completed", "failed")

#: Terminal record status written when a live claim is recovered.
STATUS_SUPERSEDED = "superseded"

# Exclusive claims remain bounded even if the owner stops renewing. A
# continuing owner can reacquire its exact scope, while stale consumers can
# never make a lease effectively permanent with an unbounded TTL.
MAX_TTL_HOURS = 168
MAX_OPERATION_RECOVERY_CLAIMS = 16


def _operation_digest(operation: str, **payload: Any) -> str:
    return digest_hex({"operation": operation, "payload": payload})


def _acquire_request_digest(
    *, repository: str, session_id: str, consumer_id: str, basis: str,
    ttl_hours: int, scope: dict[str, Any],
    checkout_facts: dict[str, Any],
    expected_store_generation: int | None = None,
    owner_recovery_authorization: dict[str, Any] | None = None,
) -> str:
    # ``reason`` is descriptive provenance, not an effect selector. Replaying
    # the same operation identity may improve its note without republishing a
    # different lease; the original committed reason remains authoritative.
    payload = {
        "repository": repository,
        "session_id": session_id,
        "consumer_id": consumer_id,
        "basis": basis,
        "ttl_hours": ttl_hours,
        "scope": scope,
        "checkout_facts": checkout_facts,
    }
    # Keep unguarded request digests stable for existing durable retries.
    if expected_store_generation is not None:
        payload["expected_store_generation"] = expected_store_generation
    if owner_recovery_authorization is not None:
        payload["owner_recovery_authorization"] = owner_recovery_authorization
    return _operation_digest("claim-acquire/1", **payload)


def _release_request_digest(
    *, session_id: str, reason: str, claim_id: str | None,
    repository: str | None,
) -> str:
    return _operation_digest(
        "claim-release/1", session_id=session_id, reason=reason,
        claim_id=claim_id, repository=repository,
    )


def _transfer_request_digest(
    *, claim_id: str, from_session: str, to_session: str,
    to_consumer: str, reason: str,
) -> str:
    return _operation_digest(
        "claim-transfer/1", claim_id=claim_id, from_session=from_session,
        to_session=to_session, to_consumer=to_consumer, reason=reason,
    )


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


def _effective_expiry(record: dict[str, Any]) -> datetime | None:
    """Return the enforceable lease deadline without rewriting its history.

    Old records may declare a lease longer than the current maximum. When
    both timestamps are valid, their authority ends at the earlier of the
    recorded expiry and acquisition plus the maximum lease. If acquisition
    time is missing, retain the recorded expiry and fail closed rather than
    guessing when the lease began.
    """
    expires = _parse_time(record.get("expires_at"))
    if expires is None:
        return None
    acquired = _parse_time(record.get("acquired_at"))
    if acquired is None:
        return expires
    return min(expires, acquired + timedelta(hours=MAX_TTL_HOURS))


def _activity_time(record: dict[str, Any],
                   last_activity_at: Any | None) -> datetime | None:
    """Combine session activity with explicit later claim renewal activity."""
    seen = _parse_time(last_activity_at)
    try:
        renewed = int(record.get("version", 1)) > 1
    except (TypeError, ValueError):
        renewed = False
    renewed = renewed or record.get("basis") in (BASIS_RECOVERY, BASIS_TRANSFER)
    acquired = _parse_time(record.get("acquired_at")) if renewed else None
    if acquired is not None and (seen is None or acquired > seen):
        return acquired
    return seen


def normalize_scope(scope: dict[str, Any] | None, repository: str) -> dict[str, Any]:
    """Validate a scope mapping into canonical form (repo-relative paths)."""
    scope = dict(scope or {})
    kind = scope.get("kind", "repository")
    if kind not in ("repository", "worktree", "paths"):
        raise ValueError(f"unknown claim scope kind {kind!r}")
    paths = scope.get("paths")
    if kind == "paths" or (kind == "worktree" and paths is not None):
        if not isinstance(paths, list) or not paths or not all(
            isinstance(p, str) and p and not p.startswith("/") for p in paths
        ):
            message = (
                "paths scope needs a non-empty list of repo-relative paths"
                if kind == "paths"
                else "worktree path scope needs a non-empty list of repo-relative paths"
            )
            raise ValueError(message)
        paths = sorted({p.strip("/") for p in paths})
    else:
        paths = None
    checkout = scope.get("checkout")
    if kind == "worktree" and (not isinstance(checkout, str) or not checkout):
        raise ValueError("worktree scope needs a checkout path")
    if kind != "worktree":
        checkout = None
    result = {
        "kind": kind,
        "repository": repository,
        "checkout": checkout,
        "branch": scope.get("branch"),
        "paths": paths,
        "exclusive": bool(scope.get("exclusive", kind == "repository")),
    }
    if kind == "worktree":
        for key in (
            "git_common_directory_identity", "checkout_identity",
            "manifest_identity",
        ):
            if key not in scope:
                continue
            value = scope[key]
            if not isinstance(value, str) or not value:
                raise ValueError(f"worktree scope {key} must be nonempty text")
            result[key] = value
    return result


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
         "branch": scope["branch"], "paths": scope["paths"],
         **{key: scope[key] for key in (
             "git_common_directory_identity", "checkout_identity",
             "manifest_identity",
         ) if key in scope}}
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


def shared_branch_coordination_overlap(
    left: dict[str, Any], right: dict[str, Any],
) -> str | None:
    """Describe overlapping paths on a shared branch without reserving them.

    Separate worktrees have separate working files and indexes. A shared
    attached branch is still relevant coordination context. Git serializes
    ref writes, while Environment separately gates ref-changing execution and
    integration through operation-specific checks. This relation lets policy
    distinguish local proposal work from ref-changing or checkout-retirement
    operations.
    """
    if (left.get("repository") != right.get("repository")
            or left.get("kind") != "worktree"
            or right.get("kind") != "worktree"):
        return None
    if (left.get("checkout") or None) == (right.get("checkout") or None):
        return None
    left_identity = left.get("checkout_identity")
    right_identity = right.get("checkout_identity")
    if (
        not isinstance(left_identity, str) or not left_identity
        or not isinstance(right_identity, str) or not right_identity
        or left_identity == right_identity
    ):
        return None
    left_branch = left.get("branch") or None
    right_branch = right.get("branch") or None
    if left_branch is None or left_branch != right_branch:
        return None
    left_common = left.get("git_common_directory_identity")
    right_common = right.get("git_common_directory_identity")
    if (isinstance(left_common, str) and left_common
            and isinstance(right_common, str) and right_common
            and left_common != right_common):
        return None
    if _paths_overlap(left.get("paths"), right.get("paths")):
        return "shared branch with overlapping paths across checkouts"
    return None


def scopes_conflict(left: dict[str, Any], right: dict[str, Any]) -> str | None:
    """Deterministic overlap verdict for two scopes on the same repository.

    Returns a reason string on conflict, None when the scopes coexist.
    Repository scopes are deliberately broad and conflict with every scope.
    Worktree scopes are isolated by checkout. Across linked checkouts,
    overlapping paths on the same attached branch conflict when their
    observed Git common-directory identity matches, because those checkouts
    share refs. Distinct observed common directories have independent local
    refs even when the branch names match. If either identity is unavailable,
    retain the conservative shared-branch conflict. Detached worktrees have
    private HEADs and no shared branch ref.
    """
    if left.get("repository") != right.get("repository"):
        return None
    if left.get("kind") == "repository" or right.get("kind") == "repository":
        return "repository-wide scope overlaps every checkout"
    # A paths scope has no checkout identity and names repository-relative
    # state. It therefore cannot prove that a linked-worktree mutation is on
    # an independent branch. Conservatively treat intersecting path sets as
    # overlapping while preserving disjoint path sets.
    if left.get("kind") == "paths" or right.get("kind") == "paths":
        if _paths_overlap(left.get("paths"), right.get("paths")):
            return "repository-relative paths overlap"
        return None
    same_checkout = (left.get("checkout") or None) == (right.get("checkout") or None)
    if same_checkout:
        if _paths_overlap(left.get("paths"), right.get("paths")):
            return "same checkout with overlapping paths"
        return None
    left_branch = left.get("branch") or None
    right_branch = right.get("branch") or None
    if left_branch is None or right_branch is None:
        return None
    if left_branch != right_branch:
        return None
    left_common = left.get("git_common_directory_identity")
    right_common = right.get("git_common_directory_identity")
    if (isinstance(left_common, str) and left_common
            and isinstance(right_common, str) and right_common
            and left_common != right_common):
        return None
    if _paths_overlap(left.get("paths"), right.get("paths")):
        return "shared branch with overlapping paths across checkouts"
    return None


def _alive(record: dict[str, Any], now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    if record.get("status") != "held":
        return False
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    expires = _effective_expiry(record)
    return expires is not None and expires > now


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


def latest_claims(all_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the latest state of each claim, omitting earlier versions."""
    return list(_latest_by_identity(all_records).values())


def latest_record_for(
    records: list[dict[str, Any]], claim_id: str,
) -> dict[str, Any] | None:
    """Return the latest version of one claim from a bounded history read."""
    if not isinstance(claim_id, str) or not claim_id:
        return None
    return _latest_by_identity(records).get(claim_id)


def active_claims(all_records: list[dict[str, Any]], *,
                  now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """Latest live record per claim identity; released/expired drop out."""
    return {key: record for key, record in _latest_by_identity(all_records).items()
            if _alive(record, now)}


def recover_stale_conflicts_for_operation(
    store, *, repository: str, session_id: str, consumer_id: str,
    scope: dict[str, Any], reason: str,
    cause_event_reference: dict[str, Any],
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """CAS-supersede only recoverable conflicts for one bounded operation.

    This removes stale, non-exclusive coordination records without creating a
    replacement owner claim. The caller must already have validated writable
    rights and an exact registered operation scope, and must enforce the
    operation's own concurrency contract. Active, uncertain, malformed, and
    quiet exclusive claims remain fail-closed.
    """
    if (not isinstance(repository, str) or not repository
            or len(repository) > 160 or "\x00" in repository
            or not isinstance(session_id, str) or not session_id
            or len(session_id) > 160 or "\x00" in session_id
            or not isinstance(consumer_id, str) or not consumer_id
            or len(consumer_id) > 160 or "\x00" in consumer_id
            or not isinstance(reason, str) or not reason
            or len(reason) > 512 or "\x00" in reason):
        raise ValueError("operation claim recovery identity or reason is invalid")
    resolved = normalize_scope(scope, repository)
    if resolved.get("kind") != "worktree" or resolved.get("exclusive"):
        raise ValueError(
            "operation claim recovery requires a nonexclusive exact worktree scope"
        )
    if (
        not isinstance(cause_event_reference, dict)
        or set(cause_event_reference) != {"identity", "sequence", "type"}
        or not isinstance(cause_event_reference.get("identity"), str)
        or not re.fullmatch(
            r"evt_[A-Za-z0-9][A-Za-z0-9_-]{0,155}",
            cause_event_reference["identity"],
        )
        or type(cause_event_reference.get("sequence")) is not int
        or cause_event_reference["sequence"] < 1
        or cause_event_reference.get("type") not in {
            "authority.denied", "authority.coordination-deferred",
        }
    ):
        raise ValueError(
            "operation claim recovery requires its exact authority decision event"
        )
    for _ in range(16):
        generation = store.claim_generation()
        records = store.read_claims()
        if store.claim_generation() != generation:
            continue
        live = active_claims(records, now=now)
        conflicts: list[tuple[str, dict[str, Any]]] = []
        for claim_id, record in live.items():
            if (record.get("repository") != repository
                    or record.get("session_id") == session_id):
                continue
            raw_scope = record.get("scope")
            if not isinstance(raw_scope, dict):
                raise ClaimConflict(
                    f"{claim_id} has malformed live scope and blocks operation recovery")
            try:
                existing_scope = normalize_scope(raw_scope, repository)
            except (TypeError, ValueError) as error:
                raise ClaimConflict(
                    f"{claim_id} has invalid live scope and blocks operation recovery: {error}") from error
            overlap = scopes_conflict(resolved, existing_scope)
            if overlap is None:
                continue
            conflicts.append((claim_id, record))
            if len(conflicts) > MAX_OPERATION_RECOVERY_CLAIMS:
                raise ClaimConflict(
                    "operation scope overlaps more than 16 claims; "
                    "bounded stale-claim recovery deferred")
        if not conflicts:
            return []
        assessments = [
            (claim_id, record, explain(record, store, now=now))
            for claim_id, record in conflicts
        ]
        blocked = [
            (claim_id, record, assessment)
            for claim_id, record, assessment in assessments
            if assessment.get("verdict") != "recoverable"
        ]
        if blocked:
            claim_id, record, assessment = blocked[0]
            raise ClaimConflict(
                f"{claim_id} held by {record.get('session_id')} remains "
                f"non-recoverable ({assessment.get('verdict')}): "
                f"{assessment.get('reason')}")
        recovered = _recovery_records(
            records,
            [(claim_id, record) for claim_id, record, _ in assessments],
            session_id=session_id,
            consumer_id=consumer_id,
            reason=reason,
            operation_cause_event_reference=cause_event_reference,
        )
        try:
            store.put_claim_batch(recovered, expected_generation=generation)
            return recovered
        except ClaimBatchConflict:
            continue
        except ClaimVersionConflict as error:
            raise ClaimConflict(
                f"operation recovery lost the claim version race: {error}") from error
    raise ClaimConflict(
        "claim set kept advancing during operation recovery; retry after reconciling")


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
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    expires = _effective_expiry(record)
    if expires is None or expires <= moment:
        return "expired"
    if status != "held":
        return str(status)
    seen = _activity_time(record, last_activity_at)
    if seen is None:
        return "stale"
    age = moment - seen
    if age <= ACTIVE_WINDOW:
        return "active"
    if age <= STALE_WINDOW:
        return "idle"
    return "stale"


def owner_state(
    store, session_id: str, *, require_bounded_events: bool = False,
) -> dict[str, Any]:
    """Best-effort owner session state from the authoritative session store.

    Returns known/lifecycle/last_activity_at plus an error marker when the
    owner cannot be determined. Callers fail closed on error: an unreadable
    owner is never treated as dead. Read-only RPC observations remain in the
    evidence log but do not prove that the session owner is active: another
    authenticated session may have inspected this session.
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
        page_reader = getattr(store, "read_event_page", None)
        tail_reader = getattr(store, "read_event_tail", None)
        if require_bounded_events:
            # The base SessionStore contract can implement these APIs by
            # materializing the complete event history. Entry projections
            # must not silently turn a bounded liveness summary into an
            # unbounded history scan on providers without indexed reads.
            from .session_store import SessionStore

            store_type = type(store)
            page_implementation = getattr(store_type, "read_event_page", None)
            tail_implementation = getattr(store_type, "read_event_tail", None)
            has_bounded_page = (
                callable(page_implementation)
                and page_implementation is not SessionStore.read_event_page
            )
            has_bounded_tail = (
                callable(tail_implementation)
                and tail_implementation is not SessionStore.read_event_tail
            )
            if not has_bounded_page and not has_bounded_tail:
                state["error"] = "bounded-session-event-access-unavailable"
                return state
        if callable(page_reader):
            page = page_reader(
                session_id, limit=MAX_OWNER_ACTIVITY_EVENT_TAIL,
            )
            if (not isinstance(page, dict)
                    or not isinstance(page.get("events"), list)
                    or type(page.get("event_count")) is not int
                    or page["event_count"] < 0
                    or type(page.get("truncated")) is not bool):
                raise ValueError("invalid owner event page")
            events = page["events"]
            event_count = page["event_count"]
            event_tail_truncated = (
                page["truncated"] or event_count > len(events)
            )
        else:
            if callable(tail_reader):
                events, event_count = tail_reader(
                    session_id, limit=MAX_OWNER_ACTIVITY_EVENT_TAIL,
                )
            else:
                events = store.read_events(session_id)
                event_count = len(events) if isinstance(events, list) else -1
            if (not isinstance(events, list) or type(event_count) is not int
                    or event_count < len(events)):
                raise ValueError("invalid owner event tail")
            event_tail_truncated = event_count > MAX_OWNER_ACTIVITY_EVENT_TAIL
        if len(events) != min(event_count, MAX_OWNER_ACTIVITY_EVENT_TAIL):
            event_tail_truncated = True
    except Exception:
        state["error"] = "session-events-unreadable"
        return state
    latest: datetime | None = None
    owner_activity_observed = False
    if isinstance(events, list):
        for event in events:
            if (not isinstance(event, dict)
                    or event.get("type") in NON_OWNER_ACTIVITY_EVENT_TYPES):
                continue
            seen = _parse_time(event.get("observed_at"))
            if seen is not None:
                owner_activity_observed = True
                if latest is None or seen > latest:
                    latest = seen
    if len(events) != min(event_count, MAX_OWNER_ACTIVITY_EVENT_TAIL):
        state["error"] = "owner-event-payloads-incomplete"
    elif event_tail_truncated and not owner_activity_observed:
        # Without a relevant event in the selected tail, older activity may
        # have been omitted. Do not turn an incomplete read into stale-owner
        # evidence; classify will fail closed for nonterminal sessions.
        state["error"] = "owner-activity-not-established-from-truncated-tail"
    elif latest is None:
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


def _lease_diagnostic(record: dict[str, Any]) -> dict[str, Any]:
    acquired = _parse_time(record.get("acquired_at"))
    expires = _parse_time(record.get("expires_at"))
    if acquired is None or expires is None:
        return {
            "acquired_at": record.get("acquired_at"),
            "expires_at": record.get("expires_at"),
            "effective_expires_at": (expires.isoformat(timespec="seconds")
                                      if expires is not None else None),
            "duration_hours": None,
            "effective_duration_hours": None,
            "maximum_hours": MAX_TTL_HOURS,
            "policy": "unverifiable",
        }
    duration = (expires - acquired).total_seconds() / 3600
    effective_expires = _effective_expiry(record)
    assert effective_expires is not None
    effective_duration = (effective_expires - acquired).total_seconds() / 3600
    if duration <= 0:
        policy = "invalid-range"
    elif duration <= MAX_TTL_HOURS:
        policy = "within-current-maximum"
    else:
        policy = "exceeds-current-maximum"
    return {
        "acquired_at": acquired.isoformat(timespec="seconds"),
        "expires_at": expires.isoformat(timespec="seconds"),
        "effective_expires_at": effective_expires.isoformat(timespec="seconds"),
        "duration_hours": round(duration, 3),
        "effective_duration_hours": round(effective_duration, 3),
        "maximum_hours": MAX_TTL_HOURS,
        "policy": policy,
    }


def lease_diagnostic(record: dict[str, Any]) -> dict[str, Any]:
    """Return the recorded and enforceable expiry for a claim lease."""
    return _lease_diagnostic(migrate_record(dict(record)))


def classify(record: dict[str, Any], owner: dict[str, Any] | None, *,
             now: datetime | None = None) -> dict[str, Any]:
    """Explain one claim record as live/stale/recoverable/not-recoverable.

    Verdicts `released`, `expired`, and `superseded` are informational (the
    record blocks nothing). A `held`, unexpired record is `live` when its
    owner shows recent activity, `recoverable` when the owner is dead or
    the ownership went quietly stale on a non-exclusive scope, and
    `not-recoverable` otherwise. `now` supplies a deterministic inspection
    time for both lease and quiet-age classification; claim acquisition itself
    always uses wall-clock time.
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
            "last_activity_at": owner.get("last_activity_at"),
            "lease": _lease_diagnostic(record)}
    if status == "released":
        return {**base, "verdict": "released", "freshness": None,
                "reason": "claim was released; it blocks nothing"}
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    expires = _effective_expiry(record)
    if expires is None or expires <= moment:
        recorded_expires = _parse_time(record.get("expires_at"))
        bounded = (
            expires is not None
            and recorded_expires is not None
            and expires < recorded_expires
        )
        return {**base, "verdict": "expired", "freshness": None,
                "reason": ("claim lease elapsed at its bounded maximum; it blocks nothing"
                           if bounded else "claim TTL elapsed; it blocks nothing")}
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
    seen = _activity_time(record, owner.get("last_activity_at"))
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


def owner_liveness_summary(
    record: dict[str, Any],
    store,
    owner_states: dict[str, dict[str, Any]],
    *,
    now: datetime | None = None,
    is_active: bool | None = None,
    owner_session_limit: int = MAX_OWNER_LIVENESS_INSPECTIONS,
    require_bounded_events: bool = False,
) -> dict[str, Any]:
    """Project advisory owner freshness independently from claim presence.

    ``is_active`` describes whether the claim is present in the caller's
    current active-claim view; it does not describe owner process liveness.
    This function never changes, acquires, or recovers a claim.
    """
    if type(owner_session_limit) is not int or owner_session_limit < 0:
        raise ValueError("owner session inspection limit must be non-negative")
    active = record.get("live") is True if is_active is None else is_active
    if active is not True:
        explanation = classify(record, {}, now=now)
        return {
            "inspection_status": "not-needed-nonblocking",
            "freshness": explanation.get("freshness"),
            "recovery_eligibility": explanation.get("verdict", "unknown"),
            "recommended_operation": "refresh-scope-and-acquire",
        }

    session_id = str(record.get("session_id") or "")
    if session_id not in owner_states:
        if len(owner_states) >= owner_session_limit:
            return {
                "inspection_status": "inspection-limit-reached",
                "freshness": None,
                "recovery_eligibility": "unknown",
                "recommended_operation": "inspect-owner-state-before-acquiring",
            }
        owner_states[session_id] = owner_state(
            store, session_id, require_bounded_events=require_bounded_events,
        )
    owner = owner_states[session_id]
    explanation = classify(record, owner, now=now)
    if owner.get("error"):
        inspection_status = "owner-state-unavailable"
    elif owner.get("known") is not True:
        inspection_status = "owner-unknown"
    else:
        inspection_status = "observed"
    recovery = explanation.get("verdict", "unknown")
    next_operation = {
        "recoverable": "retry-exact-scoped-acquisition",
        "live": "use-disjoint-scope-or-wait",
        "not-recoverable": "preserve-scope-and-inspect-owner",
    }.get(recovery, "refresh-scope-and-acquire")
    return {
        "inspection_status": inspection_status,
        "freshness": explanation.get("freshness"),
        "recovery_eligibility": recovery,
        "recommended_operation": next_operation,
    }


class ClaimConflict(Exception):
    """Raised when a scope overlaps another live session's scope."""


class ClaimAdoptionRequired(Exception):
    """Raised when a checkout shows unknown work and the basis is ordinary."""

    def __init__(self, message: str, facts: dict[str, Any] | None = None):
        super().__init__(message)
        self.facts = facts or {}


def _validate_owner_recovery_authorization(
    authorization: dict[str, Any], *, repository: str, claim_id: str,
    owner_session_id: str, requester_session_id: str,
    target_scope: dict[str, Any],
) -> None:
    """Validate the narrow Control owner attestation used for live-owner recovery.

    This attestation is accepted only by the Environment/Control owner API;
    the ordinary ``mncs-env claims --basis recovery`` surface never accepts
    caller-supplied liveness evidence. Control binds the record to an exact
    source claim, target scope, session, claim generation, and host observation.
    """
    required = {
        "schema_version", "authorization_id", "campaign_id",
        "requester_session_id", "requester_principal_id", "recovery_basis",
        "operator_approval_reference", "claim_id", "repository",
        "expected_claim_version", "owner_session_id", "owner_consumer_id",
        "owner_scope", "target_scope", "host_observation",
        "host_observation_digest",
    }
    if set(authorization) != required:
        raise ValueError("owner recovery authorization fields are invalid")
    authorization_schema = authorization.get("schema_version")
    if authorization_schema not in {
        "mncs.environment.control-owner-claim-recovery/2",
        "mncs.environment.control-owner-claim-recovery/3",
    }:
        raise ValueError("owner recovery authorization schema is unsupported")
    if (authorization.get("repository") != repository
            or authorization.get("claim_id") != claim_id
            or authorization.get("owner_session_id") != owner_session_id
            or authorization.get("requester_session_id") != requester_session_id):
        raise ValueError("owner recovery authorization does not match the exact claim transition")
    if (not isinstance(authorization.get("owner_consumer_id"), str)
            or not authorization["owner_consumer_id"]):
        raise ValueError("owner recovery consumer identity is invalid")
    if (type(authorization.get("expected_claim_version")) is not int
            or authorization["expected_claim_version"] < 1):
        raise ValueError("owner recovery authorization claim version is invalid")
    for field in ("campaign_id", "requester_principal_id"):
        value = authorization.get(field)
        if not isinstance(value, str) or not value.strip() or len(value) > 256 or "\x00" in value:
            raise ValueError(f"owner recovery authorization {field} is invalid")
    basis = authorization.get("recovery_basis")
    approval = authorization.get("operator_approval_reference")
    if basis == "same-principal-host-liveness":
        if approval is not None:
            raise ValueError("automatic owner recovery cannot claim an operator approval")
    elif basis == "operator-approved-config":
        if (not isinstance(approval, str) or not approval.strip()
                or len(approval) > 256 or "\x00" in approval):
            raise ValueError("operator-approved recovery needs its configured approval reference")
    else:
        raise ValueError("owner recovery basis is unsupported")
    owner_scope = normalize_scope(authorization.get("owner_scope"), repository)
    authorized_target_scope = normalize_scope(authorization.get("target_scope"), repository)
    if (claim_identity(repository, owner_scope) != claim_id
            or authorized_target_scope != normalize_scope(target_scope, repository)
            or scopes_conflict(owner_scope, authorized_target_scope) is None):
        raise ValueError("owner recovery scope transition is not the exact authorized overlap")
    observation = authorization.get("host_observation")
    if not isinstance(observation, dict):
        raise TypeError("owner recovery host observation must be an object")
    observation_fields = {
        "schema_version", "observed_at", "host_boot_id", "control_owner_pid",
        "control_owner_start_ticks", "process_view", "processes_examined",
        "same_uid_processes_examined", "user_units_examined", "active_jobs",
        "active_owner_grants", "revoked_owner_grants", "active_owner_requests",
        "current_request_processes_excluded", "matching_processes",
        "matching_user_services", "resident_services", "complete",
    }
    if authorization_schema == "mncs.environment.control-owner-claim-recovery/3":
        observation_fields.add("operator_approved_resident_agent_hosts")
    if set(observation) != observation_fields:
        raise ValueError("owner recovery host observation fields are invalid")
    expected_observation_schema = (
        "mncs.control.host-execution-liveness/3"
        if authorization_schema == "mncs.environment.control-owner-claim-recovery/3"
        else "mncs.control.host-execution-liveness/2"
    )
    if (observation.get("schema_version") != expected_observation_schema
            or observation.get("process_view") != "host-procfs"
            or observation.get("complete") is not True
            or observation.get("active_jobs") != 0
            or observation.get("active_owner_grants") != 0
            or observation.get("active_owner_requests") != 0
            or observation.get("matching_processes") != []
            or observation.get("matching_user_services") != []):
        raise ValueError("owner recovery requires a complete host observation with no live former-owner execution")
    excluded = observation.get("current_request_processes_excluded")
    if (not isinstance(excluded, list) or len(excluded) > 128
            or any(not isinstance(item, dict)
                   or set(item) != {"pid", "start_ticks"}
                   or type(item.get("pid")) is not int or item["pid"] <= 0
                   or not isinstance(item.get("start_ticks"), str)
                   or not item["start_ticks"]
                   for item in excluded)):
        raise ValueError("owner recovery current-request process identities are invalid")
    resident_services = observation.get("resident_services")
    if (not isinstance(resident_services, list) or len(resident_services) > 64
            or any(not isinstance(item, dict)
                   or set(item) != {
                       "pid", "ppid", "start_ticks", "module", "environment_readable",
                   }
                   or type(item.get("pid")) is not int or item["pid"] <= 0
                   or type(item.get("ppid")) is not int or item["ppid"] <= 0
                   or not isinstance(item.get("start_ticks"), str)
                   or not item["start_ticks"]
                   or not isinstance(item.get("module"), str)
                   or not item["module"].endswith(".server")
                   or type(item.get("environment_readable")) is not bool
                   for item in resident_services)):
        raise ValueError("owner recovery resident integration service identities are invalid")
    resident_agent_hosts = observation.get(
        "operator_approved_resident_agent_hosts", [],
    )
    if (not isinstance(resident_agent_hosts, list)
            or len(resident_agent_hosts) > 32
            or any(not isinstance(item, dict)
                   or set(item) != {"pid", "start_ticks", "process_kind"}
                   or type(item.get("pid")) is not int or item["pid"] <= 0
                   or not isinstance(item.get("start_ticks"), str)
                   or not item["start_ticks"]
                   or item.get("process_kind") != "managed-codex-app-server"
                   for item in resident_agent_hosts)
            or len({(item["pid"], item["start_ticks"])
                    for item in resident_agent_hosts}) != len(resident_agent_hosts)):
        raise ValueError("owner recovery resident agent host identities are invalid")
    if (resident_agent_hosts
            and authorization.get("recovery_basis") != "operator-approved-config"):
        raise ValueError(
            "only operator-approved claim recovery may exclude a shared agent host"
        )
    if (type(observation.get("control_owner_pid")) is not int
            or observation["control_owner_pid"] <= 0
            or not isinstance(observation.get("control_owner_start_ticks"), str)
            or not observation["control_owner_start_ticks"]
            or not isinstance(observation.get("host_boot_id"), str)
            or not re.fullmatch(r"[0-9a-f-]{36}", observation["host_boot_id"])):
        raise ValueError("owner recovery host identity is invalid")
    if (type(observation.get("revoked_owner_grants")) is not int
            or observation["revoked_owner_grants"] < 0):
        raise ValueError("owner recovery revoked-grant count is invalid")
    if any(type(observation.get(field)) is not int or observation[field] < 0
           for field in ("processes_examined", "same_uid_processes_examined",
                         "user_units_examined")):
        raise ValueError("owner recovery host inspection counts are invalid")
    observed_at = _parse_time(observation.get("observed_at"))
    if observed_at is None:
        raise ValueError("owner recovery host observation timestamp is invalid")
    age = datetime.now(timezone.utc) - observed_at
    if age < timedelta(seconds=-30) or age > timedelta(minutes=2):
        raise ValueError("owner recovery host observation is not current")
    expected_observation_digest = digest_hex(observation, length=64)
    if authorization.get("host_observation_digest") != expected_observation_digest:
        raise ValueError("owner recovery host observation digest is invalid")
    stable_authorization = {
        key: authorization[key]
        for key in (
            "schema_version", "campaign_id", "requester_session_id",
            "requester_principal_id", "recovery_basis",
            "operator_approval_reference", "owner_scope", "target_scope",
            "claim_id", "repository", "expected_claim_version",
            "owner_session_id", "owner_consumer_id",
        )
    }
    expected_authorization_id = digest_hex(stable_authorization, length=64)
    if authorization.get("authorization_id") != expected_authorization_id:
        raise ValueError("owner recovery authorization identity is invalid")


def _with_identity(record: dict[str, Any]) -> dict[str, Any]:
    record = dict(record)
    record["identity"] = "clm_" + digest_hex(
        {key: record[key] for key in sorted(record) if key != "identity"}
    )
    return record


def _record(store, record: dict[str, Any]) -> dict[str, Any]:
    record = _with_identity(record)
    store.put_claim(record)
    return record


def _recovery_records(records: list[dict[str, Any]],
                      victims: list[tuple[str, dict[str, Any]]], *,
                      session_id: str, consumer_id: str,
                      reason: str,
                      owner_recovery_authorization: dict[str, Any] | None = None,
                      operation_cause_event_reference: dict[str, Any] | None = None,
                      ) -> list[dict[str, Any]]:
    """Prepare superseding records for the caller's atomic claim batch."""
    moment = datetime.now(timezone.utc).isoformat(timespec="seconds")
    written: list[dict[str, Any]] = []
    for victim_id, victim in victims:
        versions = [int(record.get("version", 0)) for record in records
                    if str(record.get("claim_id", "")) == victim_id]
        provenance: dict[str, Any] = {
            "recovered_by": session_id,
            "recovered_consumer": consumer_id,
            "recovered_from_version": int(victim.get("version", 0)),
            "recovery_reason": reason,
        }
        if owner_recovery_authorization is not None:
            provenance["owner_recovery"] = owner_recovery_authorization
        if operation_cause_event_reference is not None:
            provenance["operation_cause_event_reference"] = dict(
                operation_cause_event_reference,
            )
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
            "provenance": provenance,
        }
        written.append(_with_identity(supersede))
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
    request_id: str | None = None,
    expected_store_generation: int | None = None,
    owner_recovery_authorization: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Claim a scope; conflicts and unknown-work adoption fail closed.

    Any overlapping claim that already classifies as recoverable (terminal
    owner, or quietly stale non-exclusive scope) is superseded in the same
    compare-and-swap publication as the new claim. Active, uncertain, and
    quiet exclusive claims still fail closed for every basis. `now` shifts
    only recovery quiet-age comparison; TTL expiry always uses wall-clock
    time.
    """
    allowed = (BASIS_EXPLICIT, BASIS_INTENT_SCOPE, BASIS_RECOVERY,
               BASIS_ADOPTION, BASIS_TRANSFER)
    if basis not in allowed:
        raise ValueError(f"unknown claim basis {basis!r}")
    if type(ttl_hours) is not int or not 1 <= ttl_hours <= MAX_TTL_HOURS:
        raise ValueError(f"claim TTL must be an integer from 1 to {MAX_TTL_HOURS} hours")
    if (expected_store_generation is not None
            and (type(expected_store_generation) is not int
                 or expected_store_generation < 0)):
        raise ValueError("expected Store generation must be a non-negative integer")
    resolved = normalize_scope(scope, repository)
    if owner_recovery_authorization is not None:
        _validate_owner_recovery_authorization(
            owner_recovery_authorization,
            repository=repository,
            claim_id=str(owner_recovery_authorization.get("claim_id", "")),
            owner_session_id=str(owner_recovery_authorization.get("owner_session_id", "")),
            requester_session_id=session_id,
            target_scope=resolved,
        )
        if basis != BASIS_RECOVERY:
            raise ValueError("owner-authorized recovery requires the recovery claim basis")
        if request_id is None:
            raise ValueError("owner-authorized recovery requires a stable request identity")
    if resolved["kind"] == "worktree":
        from pathlib import Path
        checkout = Path(str(resolved["checkout"]))
        if workspace_root and workspace_root not in ("", "."):
            root = Path(workspace_root).resolve()
            if root not in (checkout.resolve(), *checkout.resolve().parents):
                raise ValueError("worktree checkout escapes the workspace root")
        if not checkout.is_dir():
            raise ValueError(f"worktree checkout {checkout} is not a directory")
    if request_id is not None and (
        not isinstance(request_id, str) or not request_id
        or len(request_id) > 160 or "\x00" in request_id
    ):
        raise ValueError("claim request identity must be bounded nonempty text")
    facts = checkout_facts or {}
    request_digest = _acquire_request_digest(
        repository=repository, session_id=session_id, consumer_id=consumer_id,
        basis=basis, ttl_hours=ttl_hours, scope=resolved,
        checkout_facts=facts,
        expected_store_generation=expected_store_generation,
        owner_recovery_authorization=owner_recovery_authorization,
    )
    from .session_store import ClaimBatchConflict, ClaimVersionConflict

    claim_id = claim_identity(repository, resolved)
    for _ in range(16):
        generation = store.claim_generation()
        records = store.read_claims()
        if store.claim_generation() != generation:
            if expected_store_generation is not None:
                raise ClaimBatchConflict(
                    generation, store.claim_generation(), stage="generation-read",
                    supplied_generation=expected_store_generation,
                )
            continue
        if request_id is not None:
            matching = [record for record in records
                        if record.get("provenance", {}).get(
                            "operation_request_id") == request_id]
            if matching:
                requested = max(
                    matching, key=lambda record: int(record.get("version", 0)))
                provenance = requested.get("provenance", {})
                prior_digest = provenance.get("operation_request_digest")
                if prior_digest is not None:
                    if prior_digest != request_digest:
                        raise ClaimConflict(
                            "claim request identity is already bound to another payload")
                elif (requested.get("consumer_id") != consumer_id
                      or requested.get("basis") != basis):
                    raise ClaimConflict(
                        "claim request identity is already bound to another transition")
                latest = _latest_by_identity(records).get(
                    str(requested.get("claim_id", "")))
                if (latest is not None
                        and latest.get("identity") == requested.get("identity")
                        and requested.get("status") == "held"
                        and requested.get("session_id") == session_id
                        and str(requested.get("repository", "")) == repository
                        and requested.get("scope") == resolved):
                    return requested
                raise ClaimConflict(
                    "claim request identity is already bound to another transition")
        if (expected_store_generation is not None
                and generation != expected_store_generation):
            raise ClaimBatchConflict(
                expected_store_generation, generation,
                stage="generation-precondition",
                supplied_generation=expected_store_generation,
            )
        live = active_claims(records)
        conflicts = []
        for other_id, other in live.items():
            if other.get("session_id") == session_id:
                continue
            reason_ = scopes_conflict(resolved, other.get("scope", {}))
            if reason_ is not None:
                conflicts.append((other_id, other, reason_))
        recovered: list[dict[str, Any]] = []
        if owner_recovery_authorization is not None:
            recovery = owner_recovery_authorization
            target_id = str(recovery["claim_id"])
            target_version = int(recovery["expected_claim_version"])
            target_owner = str(recovery["owner_session_id"])
            target = live.get(target_id)
            if (target is None
                    or target.get("status") != "held"
                    or target.get("session_id") != target_owner
                    or int(target.get("version", 0)) != target_version
                    or target.get("repository") != repository
                    or target.get("scope") != recovery.get("owner_scope")
                    or recovery.get("target_scope") != resolved):
                raise ClaimConflict(
                    "owner-authorized recovery no longer matches the exact held claim generation")
            if len(conflicts) != 1 or conflicts[0][0] != target_id:
                raise ClaimConflict(
                    "owner-authorized recovery cannot supersede unrelated overlapping claims")
            recovered = _recovery_records(
                records, [(target_id, target)], session_id=session_id,
                consumer_id=consumer_id, reason=reason,
                owner_recovery_authorization=recovery,
            )
        elif conflicts:
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
                    f"{claim_id} cannot acquire overlapping scope: {detail}")
            recovered = _recovery_records(
                records,
                [(other_id, other)
                 for other_id, other, _, _ in assessments],
                session_id=session_id, consumer_id=consumer_id,
                reason=reason,
            )
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
            int(existing.get("version", 0))
            for existing in [*records, *recovered]
            if str(existing.get("claim_id", "")) == claim_id
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
            "expires_at": (moment + timedelta(hours=ttl_hours)).isoformat(
                timespec="seconds"),
            "provenance": {"acquired_by": consumer_id},
        }
        if request_id is not None:
            record["provenance"]["operation_request_id"] = request_id
            record["provenance"]["operation_request_digest"] = request_digest
        if expected_store_generation is not None:
            record["provenance"]["expected_store_generation"] = expected_store_generation
        if basis in ADOPTION_BASES:
            record["provenance"]["adopted_head"] = facts.get("head")
            record["provenance"]["adopted_dirty"] = facts.get("dirty")
            record["provenance"]["adopted_signals"] = facts.get("foreign_signals")
        if recovered:
            record["provenance"]["recovered_from"] = [
                {"claim_id": str(item["claim_id"]),
                 "version": int(item["provenance"]["recovered_from_version"]),
                 "superseded_version": int(item["version"]),
                 "owner_session": str(item["session_id"])}
                for item in recovered
            ]
            record["provenance"]["recovery_mode"] = (
                "owner-liveness-classified-and-cas-superseded"
            )
            if owner_recovery_authorization is not None:
                record["provenance"]["owner_recovery"] = owner_recovery_authorization
        record = _with_identity(record)
        try:
            store.put_claim_batch(
                [*recovered, record], expected_generation=generation)
            return record
        except ClaimBatchConflict as error:
            if expected_store_generation is not None:
                raise ClaimBatchConflict(
                    error.expected_generation, error.observed_generation,
                    stage="store-cas",
                    supplied_generation=expected_store_generation,
                ) from error
            continue
        except ClaimVersionConflict as error:
            raise ClaimConflict(
                f"{claim_id} lost a concurrent acquisition race; retry") from error
    raise ClaimConflict(
        f"{claim_id} kept advancing during acquisition; "
        "re-evaluate before retrying")


def release(
    store, *, session_id: str, reason: str = "",
    claim_id: str | None = None, repository: str | None = None,
    request_id: str | None = None,
) -> list[dict[str, Any]]:
    """Release own held claims by identity (or every own scope on a repo)."""
    if not claim_id and not repository:
        raise ValueError("release needs claim_id or repository")
    import secrets

    from .session_store import ClaimBatchConflict, ClaimVersionConflict

    request_id = request_id or "release_" + secrets.token_hex(16)
    if not isinstance(request_id, str) or not request_id or len(request_id) > 160 or "\x00" in request_id:
        raise ValueError("release request identity must be bounded nonempty text")
    request_digest = _release_request_digest(
        session_id=session_id, reason=reason, claim_id=claim_id,
        repository=repository,
    )
    for _ in range(16):
        generation = store.claim_generation()
        records = store.read_claims()
        completed = [record for record in records
                     if record.get("provenance", {}).get("release_request_id") == request_id]
        if completed:
            for record in completed:
                provenance = record.get("provenance", {})
                prior_digest = provenance.get("release_request_digest")
                if prior_digest is not None:
                    if prior_digest != request_digest:
                        raise ClaimConflict(
                            "claim release request identity is already bound to another payload")
                elif (record.get("session_id") != session_id
                      or (claim_id is not None
                          and str(record.get("claim_id")) != claim_id)
                      or (repository is not None
                          and str(record.get("repository")) != repository)
                      or record.get("reason") != reason):
                    raise ClaimConflict(
                        "claim release request identity is already bound to another transition")
            return completed
        if store.claim_generation() != generation:
            continue
        live = active_claims(records)
        targets = [
            record for record in live.values()
            if record.get("session_id") == session_id
            and (claim_id is None or str(record.get("claim_id")) == claim_id)
            and (repository is None or str(record.get("repository")) == repository)
        ]
        if not targets:
            return []
        released: list[dict[str, Any]] = []
        for existing in targets:
            versions = [
                int(record.get("version", 0)) for record in records
                if str(record.get("claim_id", "")) == str(existing.get("claim_id"))
            ]
            record = {
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
                "provenance": {
                    "released_by": session_id,
                    "release_request_id": request_id,
                    "release_request_digest": request_digest,
                },
            }
            record["identity"] = "clm_" + digest_hex(
                {key: record[key] for key in sorted(record) if key != "identity"}
            )
            released.append(record)
        try:
            store.put_claim_batch(released, expected_generation=generation)
            return released
        except ClaimBatchConflict:
            continue
        except ClaimVersionConflict as error:
            raise ClaimConflict(str(error)) from error
    raise ClaimConflict("claim set kept advancing during release; retry after reconciling ownership")


def transfer(
    store, *, claim_id: str, from_session: str, to_session: str,
    to_consumer: str, reason: str = "", request_id: str | None = None,
) -> dict[str, Any]:
    """Atomically close one owner and open the next under Store CAS.

    The request id makes a retry after a lost response a readback operation:
    the committed recipient record is returned without creating a second
    transfer. Store-backed sessions publish both versions in one generation.
    A transfer moves ownership of the same bounded lease, so its original
    acquisition time and deadline remain stable. The session-level transfer
    event records when the transition was observed; the immutable claim batch
    stays byte-stable when the same request is retried after an interrupted
    publish.
    """
    import secrets

    from .session_store import ClaimBatchConflict, ClaimVersionConflict

    request_id = request_id or "transfer_" + secrets.token_hex(16)
    if not isinstance(request_id, str) or not request_id or len(request_id) > 160 or "\x00" in request_id:
        raise ValueError("transfer request identity must be bounded nonempty text")
    request_digest = _transfer_request_digest(
        claim_id=claim_id, from_session=from_session, to_session=to_session,
        to_consumer=to_consumer, reason=reason,
    )
    for _ in range(16):
        generation = store.claim_generation()
        records = store.read_claims()
        # A previous attempt may have committed both records before its
        # caller was interrupted. Confirm recipient and source exactly.
        completed = [row for row in records
                     if row.get("provenance", {}).get("transfer_request_id") == request_id]
        if completed:
            for row in completed:
                prior_digest = row.get("provenance", {}).get(
                    "transfer_request_digest")
                if prior_digest is not None and prior_digest != request_digest:
                    raise ClaimConflict(
                        "transfer request identity is already bound to another payload")
            matching_claim = [row for row in completed
                              if str(row.get("claim_id", "")) == claim_id]
            received = [row for row in completed
                        if row.get("status") == "held"
                        and row.get("session_id") == to_session
                        and row.get("claim_id") == claim_id]
            released = [row for row in completed
                        if row.get("status") == "released"
                        and row.get("session_id") == from_session
                        and row.get("claim_id") == claim_id]
            if len(completed) != 2 or len(matching_claim) != 2:
                raise ClaimConflict(
                    "transfer request identity is already bound to another transition")
            if received and released:
                committed = max(received,
                                key=lambda row: int(row.get("version", 0)))
                source = released[0]
                if (committed.get("consumer_id") != to_consumer
                        or committed.get("basis") != BASIS_TRANSFER
                        or committed.get("reason") != reason
                        or committed.get("provenance", {}).get("transferred_from")
                        != from_session
                        or source.get("reason")
                        != f"transferred to {to_session}: {reason}"
                        or source.get("provenance", {}).get("transferred_to")
                        != to_session):
                    raise ClaimConflict(
                        "transfer request identity is already bound to another transition")
                latest = _latest_by_identity(records).get(claim_id)
                if (latest is not None
                        and latest.get("identity") == committed.get("identity")):
                    return committed
                raise ClaimConflict(
                    f"transfer request {request_id} committed, but its recipient "
                    f"no longer holds {claim_id}; inspect the latest claim version")
            raise ClaimConflict("transfer request identity is already bound to another transition")
        if store.claim_generation() != generation:
            continue
        live = active_claims(records)
        existing = live.get(claim_id)
        if existing is None or existing.get("session_id") != from_session:
            raise ClaimConflict(f"{claim_id} is not held by session {from_session}")
        versions = [int(row.get("version", 0)) for row in records
                    if str(row.get("claim_id", "")) == claim_id]
        latest_version = max(versions, default=0)
        now = datetime.now(timezone.utc)
        expires = _effective_expiry(existing)
        if expires is None:
            acquired = _parse_time(existing.get("acquired_at"))
            if acquired is None:
                raise ClaimConflict(
                    f"{claim_id} has no stable lease origin; renew it before transfer"
                )
            expires = acquired + timedelta(hours=MAX_TTL_HOURS)
        if expires <= now:
            raise ClaimConflict(f"{claim_id} expired before transfer publication")
        release_record = {
            "schema_version": SCHEMA,
            "claim_id": claim_id,
            "version": latest_version + 1,
            "repository": str(existing.get("repository")),
            "scope": existing.get("scope", {}),
            "session_id": from_session,
            "consumer_id": str(existing.get("consumer_id", "")),
            "basis": str(existing.get("basis", "")),
            "reason": f"transferred to {to_session}: {reason}",
            "status": "released",
            "acquired_at": str(existing.get("acquired_at", "")),
            "expires_at": str(existing.get("expires_at", "")),
            "provenance": {
                "released_by": from_session,
                "transferred_to": to_session,
                "transfer_request_id": request_id,
                "transfer_request_digest": request_digest,
                "transferred_from_version": int(existing.get("version", 0)),
            },
        }
        recipient = {
            "schema_version": SCHEMA,
            "claim_id": claim_id,
            "version": latest_version + 2,
            "repository": str(existing.get("repository")),
            "scope": existing.get("scope", {}),
            "session_id": to_session,
            "consumer_id": to_consumer,
            "basis": BASIS_TRANSFER,
            "reason": reason,
            "status": "held",
            # Transfer moves the current owner of the same bounded lease; it
            # does not restart that lease. Keeping its original start and
            # deadline also makes the batch payload stable when a caller
            # retries the same request after an interrupted Store publish.
            "acquired_at": str(existing.get("acquired_at", "")),
            "expires_at": expires.isoformat(timespec="seconds"),
            "provenance": {
                "transferred_from": from_session,
                "transferred_from_version": int(existing.get("version", 0)),
                "transferred_by": from_session,
                "transfer_request_id": request_id,
                "transfer_request_digest": request_digest,
            },
        }
        for record in (release_record, recipient):
            record["identity"] = "clm_" + digest_hex(
                {key: record[key] for key in sorted(record) if key != "identity"}
            )
        try:
            store.put_claim_batch(
                [release_record, recipient], expected_generation=generation
            )
            return recipient
        except ClaimBatchConflict:
            continue
        except ClaimVersionConflict as error:
            raise ClaimConflict(str(error)) from error
    raise ClaimConflict("claim set kept advancing during transfer; retry after reconciling ownership")
