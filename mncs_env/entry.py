"""Canonical entry: select context, reuse durable work, reconcile, expose it."""

from __future__ import annotations

import fcntl
from contextlib import contextmanager
from pathlib import Path

from . import identity, readiness, sessions, workspace
from .persist import read_json
from .session_store import open_store, upgrade_session_store_provider
from .intent import parse as parse_intent


class EntryError(ValueError):
    def __init__(self, message: str, code: str, **details):
        super().__init__(message)
        self.diagnostics = {"code": code, **details}


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
def entry_lock(state_dir: Path | str, backend: str):
    """Serialize entry selection and provider reconciliation across processes."""
    root = Path(state_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    with (root / f"entry-{backend}.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise EntryError("another entry or reconciliation is in progress; retry when it finishes",
                             "entry-busy", state_dir=str(root), next="retry the same command") from error
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


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
    with entry_lock(state_dir, backend):
        session = None
        has_persistence = (Path(state_dir) / ("store" if backend == "store" else "sessions")).exists()
        if not new_session and has_persistence:
            store_package = None
            language_root = None
            if backend == "store":
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
            store = open_store(state_dir, backend, store_package_dir=store_package, store_runtime=store_runtime)
            try:
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
                    upgrade_session_store_provider(state_dir, store.load_snapshot(matches[0]))
            finally:
                close = getattr(store, "close", None)
                if callable(close):
                    close()
            if len(matches) > 1:
                raise EntryError("multiple matching sessions exist; resume a specific session or use --new-session",
                                 "entry-session-ambiguous", sessions=sorted(matches), next="resume <session> --revalidate")
            if matches:
                session = sessions.Session.resume(state_dir=state_dir, session_id=matches[0], backend=backend)
        reused = session is not None
        try:
            if session is None:
                environment = sessions.resolve_environment(definition=definition, workspace_root=root,
                                                            state_dir=state_dir, consumer_id=consumer_id, backend=backend)
                environment["configuration"] = {"source": str(definition_path) if definition_path else "orientation-default",
                                                "name": definition.get("name"), "definition_id": definition_id}
                session = sessions.Session.create(state_dir=state_dir, environment=environment,
                                                  consumer_id=consumer_id, consumer_kind=consumer_kind, backend=backend)
                session.transition("resolving", "enter: resolving environment")
                session.transition("ready", "environment resolved; capability readiness is reported separately")
                session.transition("active", f"consumer {consumer_id} entered")
                revalidation = {"reprobed": len(environment["bindings"]), "changed": []}
            else:
                revalidation = session.revalidate()
                if session.snapshot["lifecycle"] == "checkpointed":
                    session.transition("active", "re-entered checkpointed work")
            reconciliation = readiness.reconcile_services(session)
            result = session.context()
            result["entry"] = {"reused": reused, "revalidation": revalidation,
                               "operations": reconciliation["operations"]}
            return result
        finally:
            if session is not None:
                session.close()
