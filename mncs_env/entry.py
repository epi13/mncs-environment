"""Canonical entry: select context, reuse durable work, reconcile, expose it."""

from __future__ import annotations

import errno
import fcntl
from contextlib import contextmanager
from pathlib import Path

from . import (
    actions,
    composition,
    context_budget,
    diagnostics,
    doctor,
    family,
    identity,
    projections,
    readiness,
    semantics,
    sessions,
    verification,
    workspace,
)
from . import coherence as incremental
from .intent import parse as parse_intent
from .persist import read_json
from .session_store import open_store, upgrade_session_store_provider


class EntryError(ValueError):
    def __init__(self, message: str, code: str, **details):
        super().__init__(message)
        self.diagnostics = {"code": code, **details}


#: Total budget for waiting on a concurrent entry/reconciliation.
ENTRY_LOCK_WAIT_SECONDS = 15.0
#: Poll interval while waiting for the entry lock.
ENTRY_LOCK_POLL_SECONDS = 0.05


def select_definition(definition_path: Path | None, workspace_root: str | None) -> tuple[dict, Path | None]:
    """Explicit definition, closest local declaration, or read-only orientation.

    Never search siblings or infer a family-wide root. A Git checkout is a
    discovery boundary; entering its subdirectories means entering that project.
    """
    selected = definition_path
    if selected is None:
        start = Path(workspace_root).expanduser().resolve() if workspace_root else Path.cwd()
        for candidate in (start, *start.parents):
            local = candidate / ".mncs" / "environment.json"
            if local.is_file():
                selected = local
                break
            if (candidate / ".git").exists() or workspace_root:
                break
    if selected is not None:
        selected = selected.expanduser().resolve()
        raw = read_json(selected)
        if not isinstance(raw, dict):
            raise EntryError(f"environment definition is missing or not an object: {selected}",
                             "definition-invalid", path=str(selected), next="select an existing --definition JSON object")
        return raw, selected
    root = Path(workspace_root).expanduser().resolve() if workspace_root else Path.cwd()
    if not workspace_root:
        root = next((path for path in (root, *root.parents) if (path / ".git").exists()), root)
    return {"name": "mncs-orientation", "workspace_root": str(root),
            "intent": {"goal": "discover the selected MNCS workspace and available capabilities",
                       "repositories": [], "forbidden_actions": ["write", "mutate", "execute", "publish", "merge", "delete"]}}, None


@contextmanager
def entry_lock(state_dir: Path | str, backend: str, *, wait_seconds: float = ENTRY_LOCK_WAIT_SECONDS):
    """Serialize entry selection and provider reconciliation across processes.

    A contended lock waits boundedly instead of failing immediately, so
    concurrent agents entering at once serialize rather than collide. The
    lock is never seized or broken: after the budget expires the waiter
    reports entry-busy. Yields the seconds waited for remediation records.
    """
    import time

    root = Path(state_dir).expanduser().resolve()
    try:
        root.mkdir(parents=True, exist_ok=True)
        handle = (root / f"entry-{backend}.lock").open("a")
    except OSError as error:
        if error.errno not in (errno.EACCES, errno.EPERM, errno.EROFS):
            raise
        if backend == "store" and error.errno == errno.EROFS:
            raise EntryError(
                "canonical Store filesystem is read-only to the Environment persistence owner",
                "direct-filesystem-read-only",
                transport="store-owner-filesystem",
                publication_completed=False,
                next="the authorized persistence owner must receive its configured Store write capability; do not redirect canonical state",
            ) from error
        next_action = (
            "pass --state-dir with an explicitly writable debug path; reuse that path on subsequent actions"
            if backend == "file"
            else "inspect the persistence owner's state-directory access; canonical Store operations require the configured owner path"
        )
        raise EntryError(
            "Environment state directory is not writable",
            "entry-state-unwritable",
            state_dir=str(root),
            publication_completed=False,
            next=next_action,
        ) from error
    with handle:
        started = time.monotonic()
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as error:
                if time.monotonic() - started >= wait_seconds:
                    raise EntryError("another entry or reconciliation is in progress; retry when it finishes",
                                     "entry-busy", state_dir=str(root),
                                     next="retry the same command") from error
                time.sleep(ENTRY_LOCK_POLL_SECONDS)
        try:
            yield {"waited_seconds": time.monotonic() - started}
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _selected_store_binding(root: Path, definition: dict) -> tuple:
    """Resolve the definition-selected Store package and MNCS runtime."""
    store_package = None
    language_root = None
    stdlib_root = None
    selection = workspace.repository_selection(definition)
    if selection and "mncs-store" in selection:
        store_package = root / "mncs-store" / "python"
    if selection and "mncs-language" in selection:
        language_root = root / "mncs-language"
    if selection and "mncs-stdlib" in selection:
        stdlib_root = root / "mncs-stdlib"
    for request in definition.get("managed_checkouts", []):
        if request.get("repository") == "mncs-store":
            slug = request.get("name", "")
            if not isinstance(slug, str) or not slug or Path(slug).name != slug or slug in (".", ".."):
                raise EntryError("invalid selected Store checkout name", "store-selection-invalid")
            store_package = root / "mncs-store" / ".worktrees" / slug / "python"
        if request.get("repository") == "mncs-stdlib":
            slug = request.get("name", "")
            if not isinstance(slug, str) or not slug or Path(slug).name != slug or slug in (".", ".."):
                raise EntryError("invalid selected stdlib checkout name", "store-selection-invalid")
            stdlib_root = root / "mncs-stdlib" / ".worktrees" / slug
        if request.get("repository") == "mncs-language":
            slug = request.get("name", "")
            if not isinstance(slug, str) or not slug or Path(slug).name != slug or slug in (".", ".."):
                raise EntryError("invalid selected Language checkout name", "store-selection-invalid")
            language_root = root / "mncs-language" / ".worktrees" / slug
    store_runtime = None
    if store_package is not None and language_root is not None:
        candidates = (language_root / "target" / "release" / "mncs", language_root / "target" / "debug" / "mncs")
        binary = next((candidate for candidate in candidates if candidate.is_file()), None)
        if binary is None or not (binary.parent / "libmncs_embed.so").is_file():
            raise EntryError("selected Language compiler/embed runtime is unavailable for Store entry",
                             "store-runtime-unavailable", next="build the selected provider runtime before retrying entry")
        store_runtime = {"MNCS_STORE_ROOT": str(store_package.parent.resolve()),
                         "MNCS_LANGUAGE_ROOT": str(language_root.resolve()), "MNCS_BIN": str(binary.resolve()),
                         "MNCS_EMBED_LIB": str((binary.parent / "libmncs_embed.so").resolve())}
    if store_runtime is not None and stdlib_root is not None:
        from .toolchain import selected_stdlib_root
        try:
            store_runtime["MNCS_STDLIB_ROOT"] = str(selected_stdlib_root(root, {"path": str(stdlib_root)}))
        except ValueError as error:
            raise EntryError(str(error), "store-runtime-unavailable") from error
    return store_package, store_runtime


def _ambient_verification(session, definition: dict) -> dict | None:
    """Run the ambient verification pass unless the definition opts out."""
    knob = definition.get("verification", {})
    if knob is None:
        knob = {}
    if not isinstance(knob, dict):
        raise EntryError("environment definition verification knob must be an object",
                         "definition-invalid", next="set verification to an object or omit it")
    if knob.get("enabled", True) is False:
        return {"summary": {"enabled": False, "obligations": 0},
                "reused": False, "evidence": None,
                "operation_status": "complete"}
    budget = knob.get("max_executions", verification.DEFAULT_MAX_EXECUTIONS)
    if type(budget) is not int or budget < 1 or budget > verification.MAX_OBLIGATIONS:
        raise EntryError("verification max_executions must be an integer between 1 and 32",
                         "definition-invalid", next="fix the definition verification knob")
    outcome = verification.ambient_pass(session, max_executions=budget)
    if outcome["summary"].get("obligations", 0) == 0 and outcome["summary"].get("blockers", 0) == 0:
        return None
    return {"summary": outcome["summary"], "reused": outcome["reused"],
            "evidence": outcome.get("evidence"),
            "operation_status": outcome.get("operation_status", "complete")}


def _ambient_actions(session, definition: dict) -> dict | None:
    """Run the ambient external-evidence pass; None when quietly current.

    Actions are reactive: steady states (no route, unavailable subject,
    current evidence) carry no actions block at all. The pass itself
    always runs ahead of verification so newly admitted receipts feed
    the verification epoch in the same entry.
    """
    knob = definition.get("actions", {})
    if knob is None:
        knob = {}
    if not isinstance(knob, dict):
        raise EntryError("environment definition actions knob must be an object",
                         "definition-invalid", next="set actions to an object or omit it")
    if knob.get("enabled", True) is False:
        return None
    outcome = actions.ambient_pass(session)
    summary = outcome["summary"]
    if (summary.get("pending", 0) == 0 and summary.get("eligible", 0) == 0
            and not summary.get("capsule_ids")
            and (summary.get("blockers", 0) == 0
                 or summary.get("obligations", 0) == 0)):
        # Zero obligations with a coherence failure is toolchain news,
        # already owned by Doctor/verification — not routing news.
        return None
    return {"summary": summary, "reused": outcome["reused"],
            "evidence": outcome.get("evidence")}


def _ambient_family(session, definition: dict) -> dict | None:
    """Run the ambient family-collaboration pass; None when quiet.

    The pass always observes (presence + drift classification are
    read-only); repair converges only inside this session's own
    claimed checkouts. The capsule surfaces only when something is
    relevant: new changes, reconciliations, or attention items.
    """
    knob = definition.get("family", {})
    if knob is None:
        knob = {}
    if not isinstance(knob, dict):
        raise EntryError("environment definition family knob must be an object",
                         "definition-invalid", next="set family to an object or omit it")
    if knob.get("enabled", True) is False:
        return None
    outcome = family.ambient_pass(
        session, converge_repairs=knob.get("converge", True) is not False)
    summary = outcome["summary"]
    if (summary.get("relevant", 0) == 0 and summary.get("reconciled", 0) == 0
            and summary.get("deferred", 0) == 0
            and summary.get("escalated", 0) == 0
            and not summary.get("attention")):
        return None
    return {"capsule": family.capsule(session), "summary": summary,
            "reused": outcome["reused"]}


def _ambient_diagnostics(session, definition: dict) -> dict | None:
    """Run the ambient diagnostic pass; None when there is nothing to explain.

    Diagnostics are reactive: a healthy world carries no diagnostic block
    at all, so normal entry pays effectively zero diagnostic context.
    """
    knob = definition.get("diagnostics", {})
    if knob is None:
        knob = {}
    if not isinstance(knob, dict):
        raise EntryError("environment definition diagnostics knob must be an object",
                         "definition-invalid", next="set diagnostics to an object or omit it")
    if knob.get("enabled", True) is False:
        return None
    budget = knob.get("max_captures", diagnostics.DEFAULT_MAX_CAPTURES)
    if type(budget) is not int or budget < 1 or budget > diagnostics.MAX_FAILURES:
        raise EntryError("diagnostics max_captures must be an integer between 1 and 32",
                         "definition-invalid", next="fix the definition diagnostics knob")
    outcome = diagnostics.ambient_pass(session, max_captures=budget)
    summary = outcome["summary"]
    if summary.get("failures", 0) == 0 and summary.get("blockers", 0) == 0:
        return None
    return {"summary": summary, "reused": outcome["reused"],
            "evidence": outcome.get("evidence")}


def _ambient_semantics(session, definition: dict) -> dict | None:
    """Run the ambient semantic pass; None when no workspace is declared.

    Like diagnostics, semantics is quiet by default: definitions that
    declare no resident service carry no semantic block at all, so
    normal entry pays effectively zero semantic context.
    """
    knob = definition.get("semantics", {})
    if knob is None:
        knob = {}
    if not isinstance(knob, dict):
        raise EntryError("environment definition semantics knob must be an object",
                         "definition-invalid", next="set semantics to an object or omit it")
    if knob.get("enabled", True) is False:
        return None
    outcome = semantics.ambient_pass(session)
    if outcome["summary"].get("declared", 0) == 0:
        return None
    return {"summary": outcome["summary"], "reused": outcome["reused"],
            "evidence": outcome.get("evidence"),
            "operation_status": outcome.get("operation_status", "complete")}


def _close_store(store) -> None:
    close = getattr(store, "close", None)
    if callable(close):
        close()


def ambient_tick(session, definition, *, fresh=False, upgraded=False, lock_waited=0.0):
    """One entry/resident orchestration path with the same owner passes."""
    runners = {
        "doctor": lambda: doctor.ambient_pass(session, fresh=fresh,
            upgraded_store=upgraded, lock_waited=lock_waited),
        "semantics": lambda: _ambient_semantics(session, definition),
        "actions": lambda: _ambient_actions(session, definition),
        "verification": lambda: _ambient_verification(session, definition),
        "diagnostics": lambda: _ambient_diagnostics(session, definition),
        "family": lambda: _ambient_family(session, definition),
        "projections": lambda: projections.ambient_pass(session),
    }
    return incremental.tick(session, definition, runners, fresh=fresh)


def enter(*, definition: dict, definition_path: Path | None, workspace_root: str,
          state_dir: Path, backend: str, consumer_id: str, consumer_kind: str,
          new_session: bool = False, campaign_id: str | None = None,
          authenticated_principal_id: str | None = None,
          continuation_request_id: str | None = None) -> dict:
    # Invalid inputs fail before persistence or provider startup.
    state_dir = Path(state_dir).expanduser().resolve()
    if backend not in ("store", "file") or not consumer_id.strip() or not consumer_kind.strip():
        raise EntryError("entry requires a valid persistence backend and nonempty consumer identity/kind", "entry-invalid")
    root = workspace.validate_workspace_root(workspace_root, definition=definition)
    try:
        budget = context_budget.validate(definition)
    except ValueError as error:
        raise EntryError(str(error), "definition-invalid") from error
    requirements = readiness.validate_requirements(definition)
    composition.validate(definition.get("execution_roles", {}))
    composition.validate_compatibility_service(
        definition.get("execution_compatibility_service"), requirements.get("services", []))
    parse_intent(definition.get("intent", {"goal": definition.get("goal", "unspecified")}))
    definition_id = identity.environment_id(definition)
    intent = parse_intent(definition.get("intent", {"goal": definition.get("goal", "unspecified")}))
    requested_campaign_id = campaign_id or definition.get("campaign_id")
    campaign_id = (
        requested_campaign_id
        or "cmp_" + identity.digest_hex({
            "intent_id": intent["identity"],
            "definition_id": definition_id,
            "workspace": str(root),
        })
    )
    if (not isinstance(campaign_id, str) or not campaign_id.strip()
            or len(campaign_id) > 160 or "\x00" in campaign_id):
        raise EntryError("campaign identity must be bounded nonempty text", "campaign-identity-invalid")
    if authenticated_principal_id is not None and (
        not isinstance(authenticated_principal_id, str)
        or not authenticated_principal_id.strip()
        or len(authenticated_principal_id) > 256
    ):
        raise EntryError("authenticated Environment principal is invalid", "principal-invalid")
    with entry_lock(state_dir, backend) as lock:
        lock_waited = float(lock.get("waited_seconds", 0.0))
        session = None
        from . import selection

        # One store handle for the whole entry: session matching, resume or
        # creation, and the ambient pass share it instead of each paying
        # the open cost and re-verifying the same state.
        store_package, store_runtime = (None, None)
        if backend == "store":
            store_package, store_runtime = _selected_store_binding(root, definition)
        store = open_store(state_dir, backend, store_package_dir=store_package, store_runtime=store_runtime, defer_mutation=True)
        try:
            upgraded = False
            pending_handoff = None
            has_persistence = (Path(state_dir) / ("store" if backend == "store" else "sessions")).exists()
            if not new_session and has_persistence:
                discovered_campaigns: set[str] = set()
                discovered_sessions: list[str] = []
                if authenticated_principal_id is not None and requested_campaign_id is None:
                    discovery_selector = {
                        "principal_id": authenticated_principal_id,
                        "definition": definition_id,
                        "workspace": str(root),
                        "campaign_discovery": True,
                    }
                    discovered = selection.select(
                        store,
                        discovery_selector,
                        lambda snapshot: (
                            snapshot.get("provenance", {}).get("definition_id") == definition_id
                            and snapshot.get("workspace", {}).get("root") == str(root)
                            and snapshot.get("authenticated_principal_id") == authenticated_principal_id
                            and snapshot.get("lifecycle") in (
                                "active", "blocked", "waiting", "checkpointed", "abandoned"
                            )
                            and isinstance(snapshot.get("campaign", {}).get("identity"), str)
                        ),
                    )
                    discovered_campaigns = {
                        str((store.load_snapshot(session_id) or {}).get("campaign", {}).get("identity"))
                        for session_id in discovered
                    }
                    discovered_sessions = list(discovered)
                    if len(discovered_campaigns) > 1:
                        raise EntryError(
                            "multiple durable campaigns match this authenticated consumer and selected environment",
                            "campaign-continuation-ambiguous",
                            campaigns=sorted(discovered_campaigns),
                            sessions=discovered,
                            next="select the intended campaign identity; Environment will not choose between active campaigns",
                        )
                    if discovered_campaigns:
                        campaign_id = next(iter(discovered_campaigns))

                if authenticated_principal_id is not None:
                    handoff_selector = {
                        "recipient_principal_id": authenticated_principal_id,
                        "definition": definition_id,
                        "workspace": str(root),
                        "pending_handoff_discovery": True,
                    }
                    pending_ids = selection.select(
                        store,
                        handoff_selector,
                        lambda snapshot: (
                            snapshot.get("provenance", {}).get("definition_id") == definition_id
                            and snapshot.get("workspace", {}).get("root") == str(root)
                            and snapshot.get("lifecycle") == "handed_off"
                            and (
                                isinstance(snapshot.get("pending_handoff_id"), str)
                                or ("pending_handoff_id" not in snapshot
                                    and bool(snapshot.get("handoffs", [])))
                            )
                        ),
                    )
                    addressed: list[tuple[str, str, dict, dict]] = []
                    for pending_session_id in pending_ids:
                        pending_snapshot = store.load_snapshot(pending_session_id) or {}
                        handoff_identity = pending_snapshot.get("pending_handoff_id")
                        if not isinstance(handoff_identity, str):
                            previous_handoffs = pending_snapshot.get("handoffs") or []
                            handoff_identity = previous_handoffs[-1] if previous_handoffs else None
                        if not isinstance(handoff_identity, str):
                            continue
                        handoff_ids = pending_snapshot.get("handoffs")
                        if not isinstance(handoff_ids, list) or handoff_identity not in handoff_ids:
                            continue
                        handoff_record = store.load_handoff(pending_session_id, handoff_identity)
                        if not isinstance(handoff_record, dict):
                            continue
                        source_principal = pending_snapshot.get("authenticated_principal_id")
                        pending_campaign = (pending_snapshot.get("campaign") or {}).get("identity")
                        if (
                            handoff_record.get("identity") != handoff_identity
                            or handoff_record.get("session_id") != pending_session_id
                            or not isinstance(source_principal, str)
                            or not source_principal
                            or handoff_record.get("from_authenticated_principal_id") != source_principal
                            or handoff_record.get("to_authenticated_principal_id") != authenticated_principal_id
                            or not isinstance(pending_campaign, str)
                            or (requested_campaign_id is not None
                                and pending_campaign != requested_campaign_id)
                        ):
                            continue
                        addressed.append((pending_session_id, handoff_identity,
                                          pending_snapshot, handoff_record))
                    if len(addressed) > 1:
                        raise EntryError(
                            "multiple owner-issued handoffs match this authenticated Environment entry",
                            "campaign-handoff-ambiguous",
                            sessions=sorted(item[0] for item in addressed),
                            campaigns=sorted({str(item[2]["campaign"]["identity"])
                                              for item in addressed}),
                            next="select the intended campaign identity; Environment will not choose between handoffs",
                        )
                    if addressed:
                        candidate = addressed[0]
                        candidate_campaign = str(candidate[2]["campaign"]["identity"])
                        if (requested_campaign_id is None and discovered_campaigns
                                and candidate_campaign not in discovered_campaigns):
                            raise EntryError(
                                "an existing campaign and an owner-issued handoff both match this Environment",
                                "campaign-continuation-ambiguous",
                                campaigns=sorted((*discovered_campaigns, candidate_campaign)),
                                sessions=sorted((*discovered_sessions, candidate[0])),
                                next="select the intended campaign identity; Environment will not choose between continuations",
                            )
                        if (candidate[3].get("to_consumer") != consumer_id
                                or candidate[3].get("to_consumer_kind", "agent") != consumer_kind):
                            raise EntryError(
                                "the pending handoff is addressed to a different consumer identity",
                                "campaign-handoff-recipient-mismatch",
                                session_id=candidate[0],
                                handoff_id=candidate[1],
                                consumer_id=candidate[3].get("to_consumer"),
                                consumer_kind=candidate[3].get("to_consumer_kind", "agent"),
                                next="enter with the consumer identity named by the owner-issued handoff",
                            )
                        pending_handoff = candidate
                        if requested_campaign_id is None:
                            campaign_id = candidate_campaign
                selector = ({'campaign_id': campaign_id,
                             'principal_id': authenticated_principal_id,
                             'definition': definition_id, 'workspace': str(root)}
                            if authenticated_principal_id is not None else
                            {'consumer': consumer_id, 'kind': consumer_kind,
                             'definition': definition_id, 'workspace': str(root)})
                def matches_snapshot(snapshot):
                    common = (
                        snapshot.get("provenance", {}).get("definition_id") == definition_id
                        and snapshot.get("workspace", {}).get("root") == str(root)
                    )
                    lifecycle = snapshot.get("lifecycle") in (
                        "active", "blocked", "waiting", "checkpointed", "abandoned"
                    )
                    if authenticated_principal_id is not None:
                        return (common and lifecycle
                                and snapshot.get("campaign", {}).get("identity") == campaign_id
                                and snapshot.get("authenticated_principal_id") == authenticated_principal_id)
                    return (common and lifecycle
                            and snapshot.get("consumer_id") == consumer_id
                            and snapshot.get("consumer_kind") == consumer_kind)
                matches = selection.select(store, selector, matches_snapshot)
                if authenticated_principal_id is not None:
                    foreign_owner = selection.select(
                        store,
                        {"campaign_id": campaign_id, "definition": definition_id,
                         "workspace": str(root), "principal_scope": "all"},
                        lambda snapshot: (
                            snapshot.get("campaign", {}).get("identity") == campaign_id
                            and snapshot.get("provenance", {}).get("definition_id") == definition_id
                            and snapshot.get("workspace", {}).get("root") == str(root)
                            and snapshot.get("lifecycle") not in ("completed", "failed")
                            and snapshot.get("authenticated_principal_id") != authenticated_principal_id
                        ),
                    )
                    if foreign_owner:
                        unexpected_owners = [
                            session_id for session_id in foreign_owner
                            if pending_handoff is None or session_id != pending_handoff[0]
                        ]
                        if unexpected_owners:
                            raise EntryError(
                                "campaign is owned by a different authenticated principal",
                                "campaign-owner-conflict", sessions=sorted(unexpected_owners),
                                next="request an explicit handoff from the recorded owner",
                            )
                if len(matches) > 1:
                    raise EntryError("multiple matching sessions exist; resume a specific session or use --new-session",
                                     "entry-session-ambiguous", sessions=sorted(matches), next="resume <session> --revalidate")
                if pending_handoff is not None and matches:
                    raise EntryError(
                        "an existing session and an owner-issued handoff both match this campaign",
                        "campaign-continuation-ambiguous",
                        campaigns=[campaign_id],
                        sessions=sorted((*matches, pending_handoff[0])),
                        next="select the intended session; Environment will not choose between continuations",
                    )
                selected_session_id = (
                    pending_handoff[0] if pending_handoff is not None
                    else matches[0] if len(matches) == 1 else None
                )
                if backend == "store" and selected_session_id is not None:
                    upgraded = upgrade_session_store_provider(
                        state_dir, store.load_snapshot(selected_session_id)
                    )
                if pending_handoff is not None:
                    session_id, handoff_identity, _, _ = pending_handoff
                    session = sessions.Session.open(
                        state_dir=state_dir, session_id=session_id,
                        backend=backend, store=store,
                    )
                    try:
                        session.accept_handoff(
                            handoff_identity,
                            consumer_id=consumer_id,
                            consumer_kind=consumer_kind,
                            authenticated_principal_id=authenticated_principal_id,
                            request_id=continuation_request_id,
                        )
                    except sessions.LifecycleError as error:
                        raise EntryError(
                            f"owner-issued handoff could not be accepted: {error}",
                            "campaign-handoff-rejected",
                            session_id=session_id,
                            handoff_id=handoff_identity,
                            next="reconcile the current handoff and session owner before retrying",
                        ) from error
                    session.reconcile_campaign_continuity()
                elif matches:
                    session = sessions.Session.open(state_dir=state_dir, session_id=matches[0],
                                                      backend=backend, store=store)
                    session.reconcile_campaign_continuity()
            reused = session is not None
            if session is None:
                if authenticated_principal_id is not None and not new_session:
                    # A matching campaign identity that lacks an authenticated
                    # owner is not silently adopted. Existing label-based
                    # sessions remain inspectable and need an explicit handoff.
                    unbound = selection.select(
                        store,
                        {"campaign_id": campaign_id, "definition": definition_id,
                         "workspace": str(root), "principal_scope": "unbound"},
                        lambda snapshot: (
                            snapshot.get("campaign", {}).get("identity") == campaign_id
                            and snapshot.get("provenance", {}).get("definition_id") == definition_id
                            and snapshot.get("workspace", {}).get("root") == str(root)
                            and not snapshot.get("authenticated_principal_id")
                            and snapshot.get("lifecycle") not in ("completed", "failed")
                        ),
                    )
                    if unbound:
                        raise EntryError(
                            "campaign exists without authenticated continuation provenance",
                            "campaign-continuation-unverified", sessions=sorted(unbound),
                            next="create an explicit handoff through the recorded session owner",
                        )
                environment = sessions.resolve_environment(definition=definition, workspace_root=root,
                                                            state_dir=state_dir, consumer_id=consumer_id, backend=backend,
                                                            store=store)
                environment["configuration"] = {"source": str(definition_path) if definition_path else "orientation-default",
                                                "name": definition.get("name"), "definition_id": definition_id}
                session = sessions.Session.create(state_dir=state_dir, environment=environment,
                                                  consumer_id=consumer_id, consumer_kind=consumer_kind, backend=backend,
                                                  campaign_id=campaign_id,
                                                  authenticated_principal_id=authenticated_principal_id,
                                                  store=store)
                session.transition("resolving", "enter: resolving environment")
                session.transition("ready", "environment resolved; capability readiness is reported separately")
                session.transition("active", f"consumer {consumer_id} entered")
            elif authenticated_principal_id is not None:
                if session.snapshot.get("authenticated_principal_id") != authenticated_principal_id:
                    raise EntryError("campaign continuation principal does not match",
                                     "campaign-owner-conflict")
                session.continue_as(consumer_id, consumer_kind)
                if session.snapshot["lifecycle"] in ("checkpointed", "abandoned"):
                    session.transition("active", "authenticated campaign continuation")
            elif session.snapshot["lifecycle"] in ("checkpointed", "abandoned"):
                session.transition("active", "re-entered durable work")
            blocks, _trace = ambient_tick(session, definition, fresh=not reused,
                upgraded=upgraded, lock_waited=lock_waited)
            remediation = blocks["doctor"]
            result = session.context()
            result["entry"] = {"reused": reused, "revalidation": remediation["revalidation"],
                               "operations": remediation["operations"]}
            result["doctor"] = {"summary": remediation["summary"], "remaining": remediation["remaining"],
                                "remaining_truncated": remediation.get("remaining_truncated", False),
                                "unavailable": remediation.get("unavailable", {"count": 0, "digest": None}),
                                "readiness": remediation["readiness"], "epoch": remediation["digest"],
                                "reused": remediation["reused"],
                                "elapsed_seconds": remediation.get("elapsed_seconds")}
            projected = blocks["projections"]
            if any(projected["summary"].get(key, 0) for key in
                   ("current", "pending", "reconciled", "blockers", "invalid")):
                result["projection"] = {"summary": projected["summary"],
                                        "reused": projected["reused"],
                                        "evidence": projected.get("evidence")}
            for name, key in (("actions", "external_evidence"), ("verification", "verification"),
                              ("diagnostics", "diagnostic"), ("semantics", "semantics"),
                              ("family", "family")):
                if blocks[name] is not None:
                    result[key] = blocks[name]
            return context_budget.apply(session, result, budget)
        finally:
            if session is not None:
                session.close()
            else:
                _close_store(store)
