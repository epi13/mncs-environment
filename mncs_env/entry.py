"""Canonical entry: select context, reuse durable work, reconcile, expose it."""

from __future__ import annotations

import errno
import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path

from . import (
    actions,
    campaign_selector,
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
        raw_selected = selected.expanduser()
        try:
            absolute_selected = Path(os.path.abspath(raw_selected))
            selected = raw_selected.resolve(strict=True)
        except OSError as error:
            raise EntryError(
                f"environment definition is unavailable: {error}",
                "definition-invalid",
                path=str(raw_selected),
            ) from error
        if absolute_selected != selected:
            raise EntryError(
                "environment definition path cannot traverse a symbolic link",
                "definition-invalid",
                path=str(raw_selected),
            )
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


def current_working_checkout(
    definition: dict,
    definition_path: Path | None,
) -> dict[str, str] | None:
    """Resolve a project-local definition to the Git checkout that contains it."""
    mode = definition.get("working_checkout")
    if mode is None:
        return None
    if mode != "current":
        raise EntryError(
            "working_checkout supports only the current Git checkout",
            "definition-invalid",
        )
    if definition_path is None:
        raise EntryError(
            "working_checkout=current requires a project-local Environment definition",
            "working-checkout-unavailable",
        )
    raw_path = Path(definition_path).expanduser()
    if (raw_path.is_symlink() or raw_path.parent.name != ".mncs"
            or not raw_path.is_file()):
        raise EntryError(
            "working_checkout=current requires a project-local definition inside .mncs at a Git checkout root",
            "working-checkout-unavailable",
            path=str(raw_path),
        )
    try:
        observed = workspace.verify_registered_checkout(raw_path.parent.parent)
    except workspace.WorkspaceResolutionError as error:
        raise EntryError(
            str(error),
            "working-checkout-unavailable",
            **error.diagnostics,
        ) from error
    return {
        key: observed[key]
        for key in (
            "repository", "path", "workspace_root", "branch", "head",
            "git_common_directory", "manifest_identity", "checkout_identity",
            "git_common_directory_identity", "observed_path",
        )
    }


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
    lock_root = root / "store" if backend == "store" else root
    try:
        root.mkdir(parents=True, exist_ok=True)
        if backend == "store":
            # The persistence service is granted the configured Store root,
            # not its parent state directory. Keep this ephemeral
            # coordination file inside that owner-controlled boundary.
            lock_root.mkdir(parents=True, exist_ok=True)
        handle = (lock_root / f"entry-{backend}.lock").open("a")
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


def _working_checkout_matches_snapshot(
    snapshot: dict,
    working_checkout: dict[str, str] | None,
) -> bool:
    """Match durable continuation to the exact registered checkout when known."""
    if working_checkout is None:
        return True
    repository = working_checkout.get("repository")
    selected_checkouts = snapshot.get("selected_checkouts")
    # Snapshots created before checkout identity was added still carry a
    # repository reference in the campaign. Accept that record only when it
    # names this repository in the same authorized workspace and pins the
    # exact branch and HEAD of the already-verified registered checkout.
    # Selection maps are per repository. A selected checkout for another
    # repository does not invalidate an exact legacy reference for this one.
    # An explicit record for this repository remains authoritative.
    if "selected_checkouts" not in snapshot:
        return _legacy_repository_reference_matches(snapshot, working_checkout)
    if not isinstance(selected_checkouts, dict):
        return False
    if not isinstance(repository, str) or repository not in selected_checkouts:
        return _legacy_repository_reference_matches(snapshot, working_checkout)
    selected = selected_checkouts.get(repository)
    if not isinstance(selected, dict):
        return False
    current_identity = working_checkout.get("checkout_identity")
    selected_identity = selected.get("checkout_identity")
    if isinstance(current_identity, str) and isinstance(selected_identity, str):
        selected_common = selected.get("git_common_directory_identity")
        current_common = working_checkout.get("git_common_directory_identity")
        return (
            current_identity == selected_identity
            and selected.get("repository", repository) == repository
            and (selected.get("branch") or "") == (working_checkout.get("branch") or "")
            and not (
                isinstance(selected_common, str)
                and isinstance(current_common, str)
                and selected_common != current_common
            )
        )

    # Older snapshots predate checkout identities but retain their selected
    # path and branch. Use both as a compatibility match; a missing checkout
    # record remains unknown and is never treated as a match.
    current_path = working_checkout.get("path")
    selected_path = selected.get("path")
    if not isinstance(current_path, str) or not isinstance(selected_path, str):
        return False
    if current_path != selected_path:
        return False
    if (selected.get("branch") or "") != (working_checkout.get("branch") or ""):
        return False
    current_common = working_checkout.get("git_common_directory_identity")
    selected_common = selected.get("git_common_directory_identity")
    return not (
        isinstance(current_common, str)
        and isinstance(selected_common, str)
        and current_common != selected_common
    )


def _implicit_campaign_for_registered_checkout(
    store,
    *,
    authenticated_principal_id: str | None,
    intent_id: str,
    definition_id: str,
    workspace_root: Path,
    working_checkout: dict[str, str] | None,
) -> str | None:
    """Find one owned campaign whose registered worktree matches this checkout.

    A caller entering at a repository root may have an unambiguous campaign
    whose selected worktree is nested below that root. Recover it only when
    the authenticated principal, Environment definition, workspace, Git
    common directory, repository, and lifecycle identify one durable session.
    Independent worktrees remain separate; duplicate sessions for one exact
    checkout are reported instead of selected arbitrarily.
    """
    if (authenticated_principal_id is None or working_checkout is None):
        return None
    repository = working_checkout.get("repository")
    common_identity = working_checkout.get("git_common_directory_identity")
    current_path = working_checkout.get("path")
    common_directory = working_checkout.get("git_common_directory")
    if (not isinstance(repository, str) or not repository
            or not isinstance(common_identity, str) or not common_identity
            or not isinstance(current_path, str)
            or not isinstance(common_directory, str)
            or os.path.abspath(current_path)
            != os.path.abspath(Path(common_directory).parent)):
        return None

    from . import selection

    selector = {
        "principal_id": authenticated_principal_id,
        "work_intent_id": intent_id,
        "definition": definition_id,
        "workspace": str(workspace_root),
        "repository": repository,
        "git_common_directory_identity": common_identity,
        "implicit_campaign_checkout_recovery": True,
    }

    def matches(snapshot: dict) -> bool:
        campaign = snapshot.get("campaign") or {}
        intent = snapshot.get("intent") or {}
        provenance = snapshot.get("provenance") or {}
        selected_checkouts = snapshot.get("selected_checkouts")
        selected = (
            selected_checkouts.get(repository)
            if isinstance(selected_checkouts, dict) else None
        )
        return (
            snapshot.get("authenticated_principal_id") == authenticated_principal_id
            and intent.get("identity") == intent_id
            and provenance.get("definition_id") == definition_id
            and (snapshot.get("workspace") or {}).get("root") == str(workspace_root)
            and snapshot.get("lifecycle") in (
                "active", "blocked", "waiting", "checkpointed", "abandoned"
            )
            and isinstance(campaign.get("identity"), str)
            and isinstance(selected, dict)
            and selected.get("repository", repository) == repository
            and selected.get("git_common_directory_identity") == common_identity
            and isinstance(selected.get("path"), str)
        )

    session_ids = selection.select(
        store,
        selector,
        matches,
    )
    if not session_ids:
        return None

    snapshots = {
        session_id: store.load_snapshot(session_id) or {}
        for session_id in session_ids
    }
    if len(session_ids) > 1:
        duplicate_contexts: set[str] = set()
        current_branch = working_checkout.get("branch")
        for field in ("checkout_identity", "path"):
            grouped: dict[str, list[str]] = {}
            for session_id, snapshot in snapshots.items():
                selected = snapshot.get("selected_checkouts", {}).get(repository, {})
                value = selected.get(field)
                if isinstance(value, str) and value:
                    grouped.setdefault(value, []).append(session_id)
            for sessions_for_context in grouped.values():
                if len(sessions_for_context) <= 1:
                    continue
                if any(
                    snapshots[session_id]
                    .get("selected_checkouts", {})
                    .get(repository, {})
                    .get("branch") == current_branch
                    for session_id in sessions_for_context
                ):
                    duplicate_contexts.update(sessions_for_context)
        if duplicate_contexts:
            duplicates = sorted(duplicate_contexts)
            raise EntryError(
                "multiple live campaign sessions identify the same registered checkout",
                "campaign-continuation-ambiguous",
                campaigns=sorted({
                    str(snapshots[session_id].get("campaign", {}).get("identity"))
                    for session_id in duplicates
                }),
                sessions=duplicates,
                conflicting_contexts=[
                    _session_selection_details(
                        session_id, snapshots[session_id], working_checkout,
                    )
                    for session_id in duplicates
                ],
                next="resume the intended recorded session; Environment will not choose between duplicate checkout owners",
            )
        # More than one distinct worktree is legitimate concurrent work. The
        # caller's exact checkout or task identity must decide which one, if
        # any, is the intended continuation.
        return None

    campaign = snapshots[session_ids[0]].get("campaign", {})
    campaign_id = campaign.get("identity") if isinstance(campaign, dict) else None
    return campaign_id if isinstance(campaign_id, str) and campaign_id else None


def _campaign_checkout_from_store(
    store,
    *,
    campaign_id: str | None,
    authenticated_principal_id: str | None,
    definition_id: str,
    workspace_root: Path,
    working_checkout: dict[str, str] | None,
) -> dict[str, str] | None:
    """Recover one campaign's verified checkout when entry starts at its repo root.

    The campaign id and authenticated principal must already be known. A
    unique durable session may then supply its registered linked-worktree
    identity when the caller's project alias resolves to the repository root.
    Multiple sessions, owners, or worktrees never get an arbitrary choice.
    """
    if (campaign_id is None or authenticated_principal_id is None
            or working_checkout is None):
        return working_checkout
    from . import selection

    session_ids = selection.select(
        store,
        {"campaign_identity": campaign_id, "campaign_continuation": True},
        lambda snapshot: (
            snapshot.get("campaign", {}).get("identity") == campaign_id
            and snapshot.get("authenticated_principal_id") == authenticated_principal_id
            and snapshot.get("provenance", {}).get("definition_id") == definition_id
            and snapshot.get("workspace", {}).get("root") == str(workspace_root)
            and snapshot.get("lifecycle") in (
                "active", "blocked", "waiting", "checkpointed", "abandoned"
            )
        ),
    )
    if len(session_ids) != 1:
        return working_checkout
    snapshot = store.load_snapshot(session_ids[0]) or {}
    if _working_checkout_matches_snapshot(snapshot, working_checkout):
        return working_checkout

    repository = working_checkout.get("repository")
    selected_checkouts = snapshot.get("selected_checkouts")
    selected = (
        selected_checkouts.get(repository)
        if isinstance(selected_checkouts, dict) and isinstance(repository, str)
        else None
    )
    if not isinstance(selected, dict):
        return working_checkout
    selected_path = selected.get("path")
    selected_branch = selected.get("branch")
    if (not isinstance(selected_path, str) or not selected_path
            or not isinstance(selected_branch, str) or not selected_branch
            or selected.get("repository", repository) != repository):
        return working_checkout
    path = Path(selected_path)
    if not path.is_absolute():
        path = workspace_root / path
    try:
        observed = workspace.verify_registered_checkout(
            path,
            repository=repository,
            workspace_root=workspace_root,
            expected_repository_root=working_checkout.get("repository_root"),
            expected_branch=selected_branch,
        )
    except workspace.WorkspaceResolutionError as error:
        raise EntryError(
            "the unique campaign checkout no longer verifies as its recorded registered worktree",
            "campaign-checkout-invalid",
            campaign_id=campaign_id,
            session_id=session_ids[0],
            repository=repository,
            recorded_checkout=selected_path,
            checkout_diagnostics=error.diagnostics,
            next="restore the recorded registered checkout identity or resume from its exact checkout",
        ) from error

    for key in ("checkout_identity", "git_common_directory_identity", "observed_path"):
        recorded = selected.get(key)
        if isinstance(recorded, str) and observed.get(key) != recorded:
            raise EntryError(
                "the unique campaign checkout identity differs from its registered worktree",
                "campaign-checkout-invalid",
                campaign_id=campaign_id,
                session_id=session_ids[0],
                repository=repository,
                recorded_checkout=selected_path,
                identity_field=key,
                recorded_identity=recorded,
                observed_identity=observed.get(key),
                next="inspect the campaign's exact checkout binding before continuing",
            )

    nested_definition = Path(observed["path"]) / ".mncs" / "environment.json"
    if nested_definition.is_symlink() or not nested_definition.is_file():
        raise EntryError(
            "the recorded campaign checkout does not contain a regular Environment definition",
            "campaign-checkout-invalid",
            campaign_id=campaign_id,
            session_id=session_ids[0],
            repository=repository,
            path=str(nested_definition),
        )
    try:
        nested_value = json.loads(nested_definition.read_text(encoding="utf-8"))
        nested_definition_id = identity.environment_id(nested_value)
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise EntryError(
            "the recorded campaign checkout has an invalid Environment definition",
            "campaign-checkout-invalid",
            campaign_id=campaign_id,
            session_id=session_ids[0],
            repository=repository,
            path=str(nested_definition),
        ) from error
    if nested_definition_id != definition_id:
        raise EntryError(
            "the recorded campaign checkout's Environment definition has changed",
            "campaign-checkout-invalid",
            campaign_id=campaign_id,
            session_id=session_ids[0],
            repository=repository,
            recorded_definition_id=definition_id,
            observed_definition_id=nested_definition_id,
        )
    return {
        key: observed[key]
        for key in (
            "repository", "path", "workspace_root", "repository_root", "branch", "head",
            "git_common_directory", "manifest_identity", "checkout_identity",
            "git_common_directory_identity", "observed_path",
        )
    }


def _legacy_repository_reference_matches(
    snapshot: dict,
    working_checkout: dict[str, str],
) -> bool:
    """Resolve a pre-checkout-identity session from its exact recorded revision."""
    repository = working_checkout.get("repository")
    workspace_root = working_checkout.get("workspace_root")
    snapshot_workspace = snapshot.get("workspace")
    campaign = snapshot.get("campaign")
    references = campaign.get("repository_refs") if isinstance(campaign, dict) else None
    if (not isinstance(repository, str) or not repository
            or not isinstance(snapshot_workspace, dict)
            or not isinstance(references, list)):
        return False
    snapshot_root_value = snapshot_workspace.get("root")
    if (not isinstance(snapshot_root_value, str) or not snapshot_root_value
            or not Path(snapshot_root_value).is_absolute()
            or not isinstance(workspace_root, str) or not workspace_root):
        return False
    try:
        snapshot_root = Path(snapshot_root_value).resolve(strict=True)
    except OSError:
        return False
    if os.path.abspath(snapshot_root_value) != str(snapshot_root):
        return False
    same_workspace = str(snapshot_root) == os.path.abspath(workspace_root)
    matches = [item for item in references
               if isinstance(item, dict) and item.get("repository") == repository]
    if len(matches) != 1:
        return False
    reference = matches[0]
    current_branch = working_checkout.get("branch")
    current_head = working_checkout.get("head")
    if (not isinstance(current_branch, str) or not current_branch
            or not isinstance(current_head, str) or not current_head
            or reference.get("branch") != current_branch):
        return False

    recorded_path = reference.get("observed_path") or reference.get("path")
    current_path = working_checkout.get("path")
    exact_recorded_path = False
    if isinstance(recorded_path, str) and isinstance(current_path, str):
        recorded_checkout = Path(recorded_path)
        if not recorded_checkout.is_absolute():
            recorded_checkout = snapshot_root / recorded_checkout
        exact_recorded_path = (
            os.path.abspath(recorded_checkout) == os.path.abspath(current_path)
        )
        if not exact_recorded_path:
            return False
    elif not same_workspace:
        # Without a recorded path there is no way to bridge two workspace
        # roots safely, even when their branch and HEAD happen to match.
        return False

    # The current checkout has already been verified against Git's registered
    # worktree list, its common directory, repository manifest, and authorized
    # workspace. When the legacy record names that exact checkout path and
    # branch, a newer HEAD is normal continuation after commits. Keep the old
    # revision as a fallback identity only when no exact path was recorded.
    if reference.get("head") != current_head and not exact_recorded_path:
        return False

    # Checkout identity hashes include the workspace-relative path. When an
    # older campaign uses a nested workspace, the exact recorded path above
    # is the cross-root identity proof and its hashes are not comparable to
    # the newly verified workspace-relative identities.
    for key in ("checkout_identity", "git_common_directory_identity"):
        recorded = reference.get(key)
        current = working_checkout.get(key)
        if (same_workspace and isinstance(recorded, str)
                and isinstance(current, str) and recorded != current):
            return False
    return True


def _session_selection_details(
    session_id: str,
    snapshot: dict,
    working_checkout: dict[str, str] | None,
) -> dict:
    repository = working_checkout.get("repository") if working_checkout else None
    selected_checkouts = snapshot.get("selected_checkouts")
    selected = (
        selected_checkouts.get(repository)
        if isinstance(selected_checkouts, dict) and isinstance(repository, str)
        else None
    )
    checkout = None
    if isinstance(selected, dict):
        checkout = {
            key: selected.get(key)
            for key in (
                "repository", "path", "checkout_identity",
                "git_common_directory_identity", "branch", "head",
            )
            if selected.get(key) is not None
        }
    return {
        "session_id": session_id,
        "campaign_id": (snapshot.get("campaign") or {}).get("identity"),
        "definition_id": snapshot.get("provenance", {}).get("definition_id"),
        "workspace_root": snapshot.get("workspace", {}).get("root"),
        "selected_checkout": checkout,
    }


def _validate_campaign_identity(store, *, campaign_id: str,
                                authenticated_principal_id: str | None,
                                definition_id: str, workspace_root: Path,
                                selected_session_id: str | None,
                                pending_handoff: tuple | None,
                                working_checkout: dict[str, str] | None = None) -> None:
    """Refuse parallel campaign sessions unless entry selected one exact context.

    A campaign identity is durable across consumer restarts. If it already
    names live work in another Environment definition or workspace, silently
    creating a new session would split its ownership record and can resume the
    wrong authority. A unique session already selected by the authenticated
    principal, campaign, definition, workspace, and registered checkout
    remains resumable when
    unrelated same-principal sessions use other contexts. Duplicate exact
    contexts remain ambiguous, and the existing session id and context are
    returned so the caller can choose the intended continuation.
    """
    if authenticated_principal_id is None:
        return
    from . import selection

    live = selection.select(
        store,
        {"campaign_identity": campaign_id, "campaign_continuation": True},
        lambda snapshot: (
            (snapshot.get("campaign") or {}).get("identity") == campaign_id
            and snapshot.get("lifecycle") not in ("completed", "failed")
        ),
    )
    if not live:
        return
    snapshots = {session_id: store.load_snapshot(session_id) or {}
                 for session_id in live}

    def continuation(session_id: str) -> dict:
        snapshot = snapshots[session_id]
        campaign = snapshot.get("campaign") or {}
        observations = sessions.continuation_observations(
            store, snapshot, session_id
        )
        repositories = []
        repository_refs = campaign.get("repository_refs", [])
        if not isinstance(repository_refs, list):
            repository_refs = []
        for item in repository_refs[:16]:
            if isinstance(item, dict):
                repositories.append({key: item.get(key) for key in (
                    "repository", "observed_path", "checkout_identity",
                    "git_common_directory_identity", "branch", "head", "clean",
                    "observation",
                )})
        checkpoint_ids = snapshot.get("checkpoints", [])
        claim_ids = observations["claim_ids"]
        pressures = campaign.get("unresolved_pressures", [])
        if not isinstance(checkpoint_ids, list):
            checkpoint_ids = []
        if not isinstance(claim_ids, list):
            claim_ids = []
        if not isinstance(pressures, list):
            pressures = []
        persisted_campaign_state = observations.get("campaign_state")
        delivery = (
            persisted_campaign_state.get("delivery")
            if isinstance(persisted_campaign_state, dict)
            else campaign.get("delivery", {"status": "pending"})
        )
        if not isinstance(delivery, dict):
            delivery = {"status": "unknown"}
        result = {
            "session_id": session_id,
            "lifecycle": snapshot.get("lifecycle"),
            "consumer_id": snapshot.get("consumer_id"),
            "definition_id": snapshot.get("provenance", {}).get("definition_id"),
            "workspace_root": snapshot.get("workspace", {}).get("root"),
            "checkpoint_ids": checkpoint_ids[-4:],
            "claim_ids": claim_ids[-16:],
            "repositories": repositories,
            "delivery": delivery,
            "unresolved_pressures": pressures[:8],
            "selected_checkout": _session_selection_details(
                session_id, snapshot, working_checkout,
            )["selected_checkout"],
        }
        if observations["latest_checkpoint"] is not None:
            result["latest_checkpoint"] = observations["latest_checkpoint"]
        if isinstance(persisted_campaign_state, dict):
            result["campaign_state_checkpoint_id"] = persisted_campaign_state.get(
                "checkpoint_id"
            )
            result["evidence"] = persisted_campaign_state.get("evidence", [])
            result["evidence_count"] = persisted_campaign_state.get(
                "evidence_count", len(result["evidence"])
            )
            result["evidence_truncated"] = persisted_campaign_state.get(
                "evidence_truncated", False
            )
        if observations["foreign_claims_count"]:
            result.update({
                "foreign_claims": observations["foreign_claims"],
                "foreign_claims_count": observations["foreign_claims_count"],
                "foreign_claims_truncated": observations["foreign_claims_truncated"],
            })
        return result

    handoff_session = pending_handoff[0] if pending_handoff is not None else None
    foreign = [
        session_id for session_id, snapshot in snapshots.items()
        if snapshot.get("authenticated_principal_id") != authenticated_principal_id
        and session_id != handoff_session
    ]
    if foreign:
        raise EntryError(
            "campaign identity has live work owned by another authenticated principal",
            "campaign-owner-conflict",
            campaign_id=campaign_id,
            sessions=sorted(foreign),
            next="request an explicit owner-issued handoff; Environment will not adopt another principal's campaign",
        )
    if len(live) > 1:
        exact_context = [
            session_id for session_id, snapshot in snapshots.items()
            if snapshot.get("provenance", {}).get("definition_id") == definition_id
            and snapshot.get("workspace", {}).get("root") == str(workspace_root)
            and _working_checkout_matches_snapshot(snapshot, working_checkout)
        ]
        if (len(exact_context) == 1
                and selected_session_id == exact_context[0]):
            return
        raise EntryError(
            "multiple live Environment sessions share this campaign identity",
            "campaign-continuation-ambiguous",
            campaign_id=campaign_id,
            sessions=live,
            continuations=[continuation(session_id) for session_id in live],
            conflicting_contexts=[
                _session_selection_details(session_id, snapshots[session_id], working_checkout)
                for session_id in live
            ],
            next="resume the intended recorded session or assign a distinct campaign identity to independent work",
        )
    only = live[0]
    if selected_session_id == only or handoff_session == only:
        return
    prior = snapshots[only]
    prior_definition = prior.get("provenance", {}).get("definition_id")
    prior_workspace = prior.get("workspace", {}).get("root")
    if (prior_definition == definition_id
            and prior_workspace == str(workspace_root)
            and not _working_checkout_matches_snapshot(prior, working_checkout)):
        raise EntryError(
            "campaign identity is already attached to a different or unknown registered checkout",
            "campaign-checkout-mismatch",
            campaign_id=campaign_id,
            session_id=only,
            current_checkout=working_checkout,
            prior_checkout=_session_selection_details(only, prior, working_checkout)[
                "selected_checkout"
            ],
            continuation=continuation(only),
            next="resume from the recorded checkout or start an independent campaign for this registered worktree",
        )
    if prior_definition != definition_id or prior_workspace != str(workspace_root):
        raise EntryError(
            "campaign identity already belongs to live work in a different Environment definition or workspace",
            "campaign-context-mismatch",
            campaign_id=campaign_id,
            session_id=only,
            lifecycle=prior.get("lifecycle"),
            definition_id=prior_definition,
            workspace=prior_workspace,
            continuation=continuation(only),
            next=(f"resume {only} --revalidate in its recorded Environment, or choose a new campaign identity "
                  "for work under the changed definition or workspace"),
        )
    raise EntryError(
        "campaign identity already has a live session that this entry did not select",
        "campaign-session-active",
        campaign_id=campaign_id,
        session_id=only,
        lifecycle=prior.get("lifecycle"),
        continuation=continuation(only),
        next=(f"resume {only} --revalidate, or choose a new campaign identity "
              "for independent work"),
    )


def enter(*, definition: dict, definition_path: Path | None, workspace_root: str,
          state_dir: Path, backend: str, consumer_id: str, consumer_kind: str,
          new_session: bool = False, campaign_id: str | None = None,
          task_identity: str | None = None,
          working_checkout: dict[str, str] | None = None,
          authenticated_principal_id: str | None = None,
          continuation_request_id: str | None = None) -> dict:
    # Invalid inputs fail before persistence or provider startup.
    state_dir = Path(state_dir).expanduser().resolve()
    if backend not in ("store", "file") or not consumer_id.strip() or not consumer_kind.strip():
        raise EntryError("entry requires a valid persistence backend and nonempty consumer identity/kind", "entry-invalid")
    root = workspace.validate_workspace_root(workspace_root, definition=definition)
    observed_working_checkout = current_working_checkout(definition, definition_path)
    if working_checkout is not None and working_checkout != observed_working_checkout:
        raise EntryError(
            "working checkout context changed before Environment entry",
            "working-checkout-unavailable",
        )
    working_checkout = observed_working_checkout
    if working_checkout is not None:
        if Path(working_checkout["workspace_root"]).resolve() != root:
            raise EntryError(
                "working checkout does not belong to the selected workspace root",
                "working-checkout-unavailable",
                path=working_checkout["path"],
                workspace_root=str(root),
            )
        selected_repositories = workspace.repository_selection(definition)
        if (selected_repositories is None
                or working_checkout["repository"] not in selected_repositories):
            raise EntryError(
                "working checkout repository is outside workspace_scope.repositories",
                "working-checkout-unavailable",
                repository=working_checkout["repository"],
            )
        if any(
            isinstance(item, dict)
            and item.get("repository") == working_checkout["repository"]
            for item in definition.get("managed_checkouts", [])
        ):
            raise EntryError(
                "working_checkout=current cannot override a provider-managed checkout",
                "definition-invalid",
                repository=working_checkout["repository"],
            )
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
    task_selector_identity = None
    if task_identity is not None:
        try:
            task_selector_identity = campaign_selector.selector_identity(
                task_identity,
                authenticated_principal_id=authenticated_principal_id,
                definition_id=definition_id,
                workspace_root=str(root),
            )
        except campaign_selector.TaskSelectorError as error:
            raise EntryError(str(error), error.code) from error
        if new_session:
            raise EntryError(
                "a task selector cannot be combined with independent-session entry",
                "campaign-task-selector-invalid",
                next="select or bind the existing task campaign; use a different task identity for independent work",
            )
    campaign_id = (
        requested_campaign_id
        or (
            "cmp_task_" + task_selector_identity[5:]
            if task_selector_identity is not None else None
        )
        or "cmp_" + identity.digest_hex({
            "intent_id": intent["identity"],
            "definition_id": definition_id,
            "workspace": str(root),
            **({
                "campaign_identity_version": 2,
                "checkout_identity": working_checkout["checkout_identity"],
            } if isinstance(working_checkout, dict)
                and isinstance(working_checkout.get("checkout_identity"), str) else {}),
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
            selected_session_id = None
            task_binding = None
            if task_selector_identity is not None:
                try:
                    task_binding = campaign_selector.read_binding(
                        store, task_selector_identity
                    )
                except campaign_selector.TaskSelectorError as error:
                    raise EntryError(
                        str(error), error.code,
                        next=("inspect the canonical task-selector record; do not create a replacement mapping"
                              if error.code == "store-corruption" else
                              "retry the same task identity and request after Store access is restored"),
                    ) from error
                if task_binding is not None:
                    if (task_binding.get("authenticated_principal_id")
                            != authenticated_principal_id
                            or task_binding.get("definition_id") != definition_id
                            or task_binding.get("workspace_root") != str(root)):
                        raise EntryError(
                            "task selector binding does not match this authenticated Environment context",
                            "campaign-task-selector-conflict",
                        )
                    bound_campaign_id = task_binding.get("campaign_id")
                    if (requested_campaign_id is not None
                            and requested_campaign_id != bound_campaign_id):
                        raise EntryError(
                            "explicit campaign identity conflicts with its durable task selector",
                            "campaign-task-selector-conflict",
                            campaign_id=requested_campaign_id,
                            bound_campaign_id=bound_campaign_id,
                        )
                    campaign_id = str(bound_campaign_id)
                    requested_campaign_id = campaign_id
                    bound_session = store.load_snapshot(
                        str(task_binding.get("session_id", ""))
                    )
                    bound_campaign = (
                        bound_session.get("campaign")
                        if isinstance(bound_session, dict) else None
                    )
                    if (
                        not isinstance(bound_session, dict)
                        or bound_session.get("authenticated_principal_id")
                        != authenticated_principal_id
                        or not isinstance(bound_campaign, dict)
                        or bound_campaign.get("identity") != campaign_id
                        or bound_session.get("provenance", {}).get("definition_id")
                        != definition_id
                        or bound_session.get("workspace", {}).get("root")
                        != str(root)
                    ):
                        raise EntryError(
                            "task selector target no longer matches its recorded Environment owner and context",
                            "campaign-task-selector-target-unavailable",
                            campaign_id=campaign_id,
                            session_id=task_binding.get("session_id"),
                        )
            has_persistence = (Path(state_dir) / ("store" if backend == "store" else "sessions")).exists()
            if not new_session and has_persistence:
                if requested_campaign_id is None and task_selector_identity is None:
                    implicit_campaign_id = _implicit_campaign_for_registered_checkout(
                        store,
                        authenticated_principal_id=authenticated_principal_id,
                        intent_id=intent["identity"],
                        definition_id=definition_id,
                        workspace_root=root,
                        working_checkout=working_checkout,
                    )
                    if implicit_campaign_id is not None:
                        requested_campaign_id = implicit_campaign_id
                        campaign_id = implicit_campaign_id
                working_checkout = _campaign_checkout_from_store(
                    store,
                    campaign_id=requested_campaign_id,
                    authenticated_principal_id=authenticated_principal_id,
                    definition_id=definition_id,
                    workspace_root=root,
                    working_checkout=working_checkout,
                )
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
                            and _working_checkout_matches_snapshot(snapshot, working_checkout)
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
                    if (task_selector_identity is not None and task_binding is None
                            and discovered_campaigns):
                        if (requested_campaign_id is not None
                                and requested_campaign_id not in discovered_campaigns):
                            raise EntryError(
                                "explicit campaign identity does not match the existing continuation selected for this task",
                                "campaign-task-selector-conflict",
                                campaigns=sorted(discovered_campaigns),
                                sessions=sorted(discovered_sessions),
                                requested_campaign_id=requested_campaign_id,
                                next="use the exact existing campaign identity or a different task identity for independent work",
                            )
                        if (len(discovered_campaigns) != 1
                                or len(discovered_sessions) != 1):
                            raise EntryError(
                                "an unbound task identity matches multiple authenticated campaign continuations",
                                "campaign-continuation-ambiguous",
                                campaigns=sorted(discovered_campaigns),
                                sessions=sorted(discovered_sessions),
                                conflicting_contexts=[
                                    _session_selection_details(
                                        session_id,
                                        store.load_snapshot(session_id) or {},
                                        working_checkout,
                                    )
                                    for session_id in discovered_sessions
                                ],
                                next="select the intended campaign when duplicate continuations share this exact checkout",
                            )
                        # The unbound task selector is only a lookup hint. Its
                        # candidate is adopted only when authenticated owner,
                        # Environment definition, workspace, checkout identity,
                        # and lifecycle select exactly one session above.
                        # Binding then makes this choice durable for later
                        # process restarts; disjoint checkouts remain isolated.
                        campaign_id = next(iter(discovered_campaigns))
                    if len(discovered_campaigns) > 1:
                        raise EntryError(
                            "multiple durable campaigns match this authenticated consumer and selected environment",
                            "campaign-continuation-ambiguous",
                            campaigns=sorted(discovered_campaigns),
                            sessions=discovered,
                            conflicting_contexts=[
                                _session_selection_details(
                                    session_id, store.load_snapshot(session_id) or {},
                                    working_checkout,
                                )
                                for session_id in discovered
                            ],
                            next="select the intended campaign identity; Environment will not choose between active campaigns",
                        )
                    if discovered_campaigns:
                        campaign_id = next(iter(discovered_campaigns))
                    elif task_selector_identity is None:
                        # Intent identity is stable across definition changes.
                        # Reuse it to find the prior campaign, then let the
                        # campaign-context guard explain whether that exact
                        # Environment can still be resumed. Do not silently
                        # create a second campaign because a definition moved.
                        intent_selector = {
                            "principal_id": authenticated_principal_id,
                            "work_intent_id": intent["identity"],
                            "workspace": str(root),
                            "campaign_intent_discovery": True,
                        }
                        intent_sessions = selection.select(
                            store,
                            intent_selector,
                            lambda snapshot: (
                                snapshot.get("authenticated_principal_id")
                                == authenticated_principal_id
                                and (snapshot.get("intent") or {}).get("identity")
                                == intent["identity"]
                                and snapshot.get("workspace", {}).get("root")
                                == str(root)
                                and _working_checkout_matches_snapshot(snapshot, working_checkout)
                                and snapshot.get("lifecycle") not in (
                                    "completed", "failed",
                                )
                                and isinstance(
                                    (snapshot.get("campaign") or {}).get("identity"),
                                    str,
                                )
                            ),
                        )
                        intent_campaigns = {
                            str((store.load_snapshot(session_id) or {})
                                .get("campaign", {}).get("identity"))
                            for session_id in intent_sessions
                        }
                        if len(intent_campaigns) > 1:
                            raise EntryError(
                                "multiple live campaigns match this authenticated work intent",
                                "campaign-continuation-ambiguous",
                                campaigns=sorted(intent_campaigns),
                                sessions=sorted(intent_sessions),
                                conflicting_contexts=[
                                    _session_selection_details(
                                        session_id, store.load_snapshot(session_id) or {},
                                        working_checkout,
                                    )
                                    for session_id in intent_sessions
                                ],
                                next="select the intended campaign identity; Environment will not choose between continuations",
                            )
                        if intent_campaigns:
                            campaign_id = next(iter(intent_campaigns))

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
                            and _working_checkout_matches_snapshot(snapshot, working_checkout)
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
                        and _working_checkout_matches_snapshot(snapshot, working_checkout)
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
                if task_binding is not None and selected_session_id is None:
                    raise EntryError(
                        "durable task selector campaign has no resumable session or addressed handoff",
                        "campaign-task-selector-target-unavailable",
                        campaign_id=campaign_id,
                        session_id=task_binding.get("session_id"),
                        next="inspect the recorded session lifecycle; Environment will not create a replacement campaign implicitly",
                    )
                _validate_campaign_identity(
                    store, campaign_id=campaign_id,
                    authenticated_principal_id=authenticated_principal_id,
                    definition_id=definition_id, workspace_root=root,
                    selected_session_id=selected_session_id,
                    pending_handoff=pending_handoff,
                    working_checkout=working_checkout,
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
            elif has_persistence:
                # Independent-session entry still requires a distinct durable
                # campaign identity while same-campaign work is live.
                _validate_campaign_identity(
                    store, campaign_id=campaign_id,
                    authenticated_principal_id=authenticated_principal_id,
                    definition_id=definition_id, workspace_root=root,
                    selected_session_id=None, pending_handoff=None,
                )
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
                                                            working_checkout=working_checkout,
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
            task_selector_status = None
            if task_selector_identity is not None:
                task_selector_status = "resumed" if task_binding is not None else "bound"
                if task_binding is None:
                    actual_campaign = (session.snapshot.get("campaign") or {}).get("identity")
                    if not isinstance(actual_campaign, str):
                        raise EntryError(
                            "selected Environment session has no durable campaign identity",
                            "campaign-identity-invalid",
                        )
                    request_id = continuation_request_id
                    if (not isinstance(request_id, str) or not request_id.startswith("req_")
                            or len(request_id) > 160 or "\x00" in request_id):
                        request_id = "req_task_" + identity.digest_hex({
                            "selector": task_selector_identity,
                            "campaign_id": actual_campaign,
                        }, length=48)
                    try:
                        task_binding = campaign_selector.bind(
                            store,
                            selector=task_selector_identity,
                            authenticated_principal_id=str(authenticated_principal_id),
                            definition_id=definition_id,
                            workspace_root=str(root),
                            campaign_id=actual_campaign,
                            session_id=session.session_id,
                            request_id=request_id,
                        )
                    except campaign_selector.TaskSelectorError as error:
                        raise EntryError(
                            str(error), error.code,
                            campaign_id=actual_campaign,
                            session_id=session.session_id,
                            publication_completed=(
                                False if error.code in (
                                    "campaign-task-selector-conflict",
                                    "campaign-task-selector-invalid",
                                    "campaign-task-selector-unauthenticated",
                                ) else None
                            ),
                            next=("retry the same task identity and request to reconcile the immutable binding"
                                  if error.code == "ambiguous-publication" else
                                  "select the authorized campaign identity once; subsequent entry will use this durable task selector"),
                        ) from error
            blocks, _trace = ambient_tick(session, definition, fresh=not reused,
                upgraded=upgraded, lock_waited=lock_waited)
            remediation = blocks["doctor"]
            result = session.context()
            result["entry"] = {"reused": reused, "revalidation": remediation["revalidation"],
                               "operations": remediation["operations"]}
            if task_selector_status is not None:
                result["entry"]["task_selector"] = {
                    "status": task_selector_status,
                    "campaign_id": (session.snapshot.get("campaign") or {}).get("identity"),
                }
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
