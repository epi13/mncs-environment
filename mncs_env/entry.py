"""Canonical entry: select context, reuse durable work, reconcile, expose it."""

from __future__ import annotations

import errno
import fcntl
from contextlib import contextmanager
from pathlib import Path

from . import actions, diagnostics, doctor, identity, projections, readiness, semantics, sessions, verification, workspace
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
        raise EntryError("Environment state directory is not writable",
                         "entry-state-unwritable", state_dir=str(root),
                         next="pass --state-dir with an explicitly writable directory; reuse that path on subsequent actions") from error
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
    selection = workspace.repository_selection(definition)
    if selection and "mncs-store" in selection:
        store_package = root / "mncs-store" / "python"
    if selection and "mncs-language" in selection:
        language_root = root / "mncs-language"
    for request in definition.get("managed_checkouts", []):
        if request.get("repository") == "mncs-store":
            slug = request.get("name", "")
            if not isinstance(slug, str) or not slug or Path(slug).name != slug or slug in (".", ".."):
                raise EntryError("invalid selected Store checkout name", "store-selection-invalid")
            store_package = root / "mncs-store" / ".worktrees" / slug / "python"
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
    return store_package, store_runtime


def _ambient_verification(session, definition: dict) -> dict:
    """Run the ambient verification pass unless the definition opts out."""
    knob = definition.get("verification", {})
    if knob is None:
        knob = {}
    if not isinstance(knob, dict):
        raise EntryError("environment definition verification knob must be an object",
                         "definition-invalid", next="set verification to an object or omit it")
    if knob.get("enabled", True) is False:
        return {"summary": {"enabled": False, "obligations": 0},
                "reused": False, "evidence": None}
    budget = knob.get("max_executions", verification.DEFAULT_MAX_EXECUTIONS)
    if type(budget) is not int or budget < 1 or budget > verification.MAX_OBLIGATIONS:
        raise EntryError("verification max_executions must be an integer between 1 and 32",
                         "definition-invalid", next="fix the definition verification knob")
    outcome = verification.ambient_pass(session, max_executions=budget)
    return {"summary": outcome["summary"], "reused": outcome["reused"],
            "evidence": outcome.get("evidence")}


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
            and summary.get("blockers", 0) == 0
            and not summary.get("capsule_ids")):
        return None
    return {"summary": summary, "reused": outcome["reused"],
            "evidence": outcome.get("evidence")}


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
            "evidence": outcome.get("evidence")}


def _close_store(store) -> None:
    close = getattr(store, "close", None)
    if callable(close):
        close()


def enter(*, definition: dict, definition_path: Path | None, workspace_root: str,
          state_dir: Path, backend: str, consumer_id: str, consumer_kind: str,
          new_session: bool = False) -> dict:
    # Invalid inputs fail before persistence or provider startup.
    state_dir = Path(state_dir).expanduser().resolve()
    if backend not in ("store", "file") or not consumer_id.strip() or not consumer_kind.strip():
        raise EntryError("entry requires a valid persistence backend and nonempty consumer identity/kind", "entry-invalid")
    root = workspace.validate_workspace_root(workspace_root, definition=definition)
    readiness.validate_requirements(definition)
    parse_intent(definition.get("intent", {"goal": definition.get("goal", "unspecified")}))
    definition_id = identity.environment_id(definition)
    with entry_lock(state_dir, backend) as lock:
        lock_waited = float(lock.get("waited_seconds", 0.0))
        session = None
        # One store handle for the whole entry: session matching, resume or
        # creation, and the ambient pass share it instead of each paying
        # the open cost and re-verifying the same state.
        store_package, store_runtime = (None, None)
        if backend == "store":
            store_package, store_runtime = _selected_store_binding(root, definition)
        store = open_store(state_dir, backend, store_package_dir=store_package, store_runtime=store_runtime)
        try:
            upgraded = False
            has_persistence = (Path(state_dir) / ("store" if backend == "store" else "sessions")).exists()
            if not new_session and has_persistence:
                matches = []
                for session_id in store.list_sessions():
                    snapshot = store.load_snapshot(session_id) or {}
                    if (snapshot.get("consumer_id") == consumer_id
                            and snapshot.get("consumer_kind") == consumer_kind
                            and snapshot.get("provenance", {}).get("definition_id") == definition_id
                            and snapshot.get("workspace", {}).get("root") == str(root)
                            and snapshot.get("lifecycle") in ("active", "blocked", "waiting", "checkpointed", "abandoned")):
                        matches.append(session_id)
                if backend == "store" and len(matches) == 1:
                    upgraded = upgrade_session_store_provider(state_dir, store.load_snapshot(matches[0]))
                if len(matches) > 1:
                    raise EntryError("multiple matching sessions exist; resume a specific session or use --new-session",
                                     "entry-session-ambiguous", sessions=sorted(matches), next="resume <session> --revalidate")
                if matches:
                    session = sessions.Session.resume(state_dir=state_dir, session_id=matches[0],
                                                      backend=backend, store=store)
            reused = session is not None
            if session is None:
                environment = sessions.resolve_environment(definition=definition, workspace_root=root,
                                                            state_dir=state_dir, consumer_id=consumer_id, backend=backend,
                                                            store=store)
                environment["configuration"] = {"source": str(definition_path) if definition_path else "orientation-default",
                                                "name": definition.get("name"), "definition_id": definition_id}
                session = sessions.Session.create(state_dir=state_dir, environment=environment,
                                                  consumer_id=consumer_id, consumer_kind=consumer_kind, backend=backend,
                                                  store=store)
                session.transition("resolving", "enter: resolving environment")
                session.transition("ready", "environment resolved; capability readiness is reported separately")
                session.transition("active", f"consumer {consumer_id} entered")
                remediation = doctor.ambient_pass(session, fresh=True, upgraded_store=upgraded,
                                                  lock_waited=lock_waited)
            else:
                if session.snapshot["lifecycle"] == "checkpointed":
                    session.transition("active", "re-entered checkpointed work")
                remediation = doctor.ambient_pass(session, upgraded_store=upgraded,
                                                  lock_waited=lock_waited)
            result = session.context()
            result["entry"] = {"reused": reused, "revalidation": remediation["revalidation"],
                               "operations": remediation["operations"]}
            result["doctor"] = {"summary": remediation["summary"], "remaining": remediation["remaining"],
                                "remaining_truncated": remediation.get("remaining_truncated", False),
                                "unavailable": remediation.get("unavailable", {"count": 0, "digest": None}),
                                "readiness": remediation["readiness"], "epoch": remediation["digest"],
                                "reused": remediation["reused"],
                                "elapsed_seconds": remediation.get("elapsed_seconds")}
            coherence = projections.ambient_pass(session)
            result["projection"] = {"summary": coherence["summary"],
                                    "reused": coherence["reused"],
                                    "evidence": coherence.get("evidence")}
            external = _ambient_actions(session, definition)
            if external is not None:
                result["actions"] = external
            result["verification"] = _ambient_verification(session, definition)
            diagnostic = _ambient_diagnostics(session, definition)
            if diagnostic is not None:
                result["diagnostic"] = diagnostic
            semantic = _ambient_semantics(session, definition)
            if semantic is not None:
                result["semantics"] = semantic
            return result
        finally:
            if session is not None:
                session.close()
            else:
                _close_store(store)
