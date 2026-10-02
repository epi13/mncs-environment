"""Environment resolution and durable EnvironmentSession lifecycle.

Resolution composes workspace facts, discovered capabilities, authority
projection, and event sources into one inspectable Environment record.
Sessions persist through the session store (Store-backed canonical, file
as debug projection) as structured snapshots plus an append-only event
log, so a different process or consumer resumes from state, not prose.

Concurrency: records are immutable under content-derived identities and
publication uses generation CAS with bounded retries. Two processes
appending events converge on sequence order; a lost race retries, and an
identical re-put is idempotent.
"""

from __future__ import annotations

import json
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import authority as authority_module
from . import capabilities as capabilities_module
from . import claims as claims_module
from . import events as events_module
from . import rights as rights_module
from . import readiness as readiness_module
from . import workspace as workspace_module
from .identity import (
    checkpoint_id,
    digest_hex,
    environment_id,
    handoff_id,
    new_session_id,
    resolved_environment_id,
)
from .intent import parse as parse_intent
from .session_store import (
    SessionStore,
    SequenceTaken,
    open_store,
    store_provider_from_environment,
    write_session_store_provider,
)

SESSION_SCHEMA = "mncs.environment.session/2"
ENVIRONMENT_SCHEMA = "mncs.environment.resolved/1"
CHECKPOINT_SCHEMA = "mncs.environment.checkpoint/1"
HANDOFF_SCHEMA = "mncs.environment.handoff/1"

MAX_EMIT_RETRIES = 16

# Allowed lifecycle transitions. Every transition records a reason; an
# illegal transition raises LifecycleError. Completion paths below match
# this table exactly (audited); complete()/fail() enforce the same sets.
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "defined": ("resolving", "abandoned"),
    "resolving": ("ready", "failed", "abandoned"),
    "ready": ("active", "failed", "abandoned"),
    "active": ("blocked", "waiting", "checkpointed", "handed_off", "completed", "failed", "abandoned"),
    "blocked": ("active", "failed", "abandoned", "completed"),
    "waiting": ("active", "failed", "abandoned", "completed"),
    "checkpointed": ("active", "failed", "abandoned", "completed"),
    "handed_off": ("active", "abandoned"),
    "completed": (),
    "failed": (),
    "abandoned": ("active",),
}


class LifecycleError(Exception):
    """Raised for illegal session transitions or corrupted session state."""


class AuthorityDenied(Exception):
    """Raised when session authority denies a requested action."""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _repo_facts(workspace_view: dict[str, Any]) -> dict[str, dict[str, Any]]:
    facts = {}
    for repo in workspace_view.get("repositories", []):
        signals = workspace_module.foreign_work_signals(repo)
        facts[repo["name"]] = {
            "clean": not repo.get("dirty", False),
            "main_branch": repo.get("branch") in ("main", "master", None),
            "dirty": bool(repo.get("dirty", False)),
            "head": repo.get("head"),
            "branch": repo.get("branch"),
            "path": repo.get("path"),
            "foreign_signals": [signal["kind"] for signal in signals],
        }
    for selected in workspace_view.get("selected_checkouts", {}).values():
        repository = str(selected.get("repository", ""))
        if not repository:
            continue
        clean = bool(selected.get("clean", False))
        facts[repository] = {
            "clean": clean,
            "main_branch": selected.get("branch") in ("main", "master", None),
            "dirty": not clean,
            "head": selected.get("head"),
            "branch": selected.get("branch"),
            "path": selected.get("path"),
            "foreign_signals": [] if clean else ["dirty-tree"],
        }
    return facts


def _workspace_change_facts(
    workspace_view: dict[str, Any],
    selected_checkouts: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return bounded checkout-state facts used by workspace change events."""
    repositories = workspace_view.get("repositories", [])

    def facts_for(repo: dict[str, Any] | None, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
        fallback = fallback or {}
        if repo is None:
            return {
                "branch": fallback.get("branch"),
                "clean": False,
                "missing": True,
                "dirty_path_count": None,
                "dirty_paths_digest": None,
                "dirty_truncated": False,
            }
        dirty_paths = repo.get("dirty_files", [])
        if not isinstance(dirty_paths, list):
            dirty_paths = []
        truncated = bool(repo.get("dirty_truncated", False))
        return {
            "branch": repo.get("branch"),
            "clean": not bool(repo.get("dirty", False)),
            "missing": False,
            "dirty_path_count": len(dirty_paths),
            "dirty_paths_digest": digest_hex({"paths": dirty_paths, "truncated": truncated}),
            "dirty_truncated": truncated,
        }

    if selected_checkouts:
        result: dict[str, dict[str, Any]] = {}
        for name, selected in selected_checkouts.items():
            selected_path = Path(str(selected.get("path", "")))
            if not selected_path.is_absolute():
                selected_path = Path(str(workspace_view.get("root", "."))) / selected_path
            repo = next(
                (item for item in repositories
                 if Path(str(item.get("path", ""))).resolve() == selected_path.resolve()),
                None,
            )
            state = facts_for(repo, selected)
            state["head"] = repo.get("head") if repo is not None else selected.get("head")
            result[name] = state
        return result

    return {
        str(repo.get("name")): {**facts_for(repo), "head": repo.get("head")}
        for repo in repositories
        if repo.get("name")
    }


def _provider_managed_checkouts(
    definition: dict[str, Any],
    workspace_root: str | Path,
    workspace_view: dict[str, Any],
) -> tuple[dict[str, Path], dict[str, dict[str, Any]]]:
    """Ask the declared workspace provider to select exact campaign checkouts.

    Environment only composes the provider capability and validates its
    bounded result shape. Git revision resolution and worktree mutations
    stay inside the provider.
    """
    selection = definition.get("managed_checkouts")
    provider = definition.get("workspace_provider")
    if selection is None and provider is None:
        return {}, {}
    if not isinstance(selection, list) or not selection:
        raise ValueError("workspace_provider requires a non-empty managed_checkouts list")
    if not isinstance(provider, dict):
        raise ValueError("managed_checkouts requires a declared workspace_provider")

    root = Path(workspace_root).resolve()
    repository = str(provider.get("repository", ""))
    checkout = str(provider.get("checkout", ""))
    capability = str(provider.get("capability", ""))
    expected_head = provider.get("revision")
    if not repository or not checkout or not capability or not isinstance(expected_head, str):
        raise ValueError("workspace_provider needs repository, checkout, capability, and pinned revision")
    relative = Path(checkout)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("workspace_provider.checkout must be repository-relative")
    provider_root = (root / repository / relative).resolve()
    try:
        provider_root.relative_to(root)
    except ValueError as error:
        raise ValueError("workspace provider checkout escapes workspace root") from error
    if (root / repository / relative).is_symlink() or not provider_root.is_dir():
        raise ValueError("workspace provider checkout is missing or a symbolic link")
    provider_record = next(
        (item for item in workspace_view.get("repositories", [])
         if Path(str(item.get("path", ""))).resolve() == provider_root),
        None,
    )
    if provider_record is None:
        raise ValueError("workspace provider checkout was not observed by the workspace provider")
    if provider_record.get("dirty"):
        raise ValueError("workspace provider checkout is dirty; refusing to bind it")
    if expected_head and provider_record.get("head") != expected_head:
        raise ValueError(
            f"workspace provider revision mismatch: expected {expected_head}, "
            f"observed {provider_record.get('head')}"
        )

    provider_bindings = capabilities_module.discover_capabilities(
        root,
        repository_roots={repository: provider_root},
        checkout_facts={repository: {
            "path": str(provider_root), "branch": provider_record.get("branch"),
            "head": provider_record.get("head"), "clean": True,
            "source_ref": "session-pinned-provider",
            "authoritative_head": provider_record.get("head"),
        }},
    )
    binding = next((item for item in provider_bindings if item.get("capability") == capability), None)
    if binding is None:
        raise ValueError(f"workspace provider does not declare capability {capability}")
    binding = capabilities_module.probe_availability(binding)
    if binding.get("availability", {}).get("status") != "available":
        raise ValueError(f"workspace provider capability is unavailable: {capability}")

    requests: list[dict[str, str]] = []
    expected: dict[str, dict[str, str]] = {}
    for item in selection:
        if not isinstance(item, dict):
            raise ValueError("managed_checkouts entries must be objects")
        name = str(item.get("repository", ""))
        slug = str(item.get("name", ""))
        branch = str(item.get("branch", ""))
        source_ref = str(item.get("source_ref", "origin/main"))
        if not name or not slug or not branch:
            raise ValueError("managed_checkouts entries need repository, name, and branch")
        if name in expected:
            raise ValueError(f"duplicate managed checkout request for {name}")
        requests.append({"repository": name, "name": slug, "branch": branch,
                         "source_ref": source_ref})
        expected[name] = {"path": f"{name}/.worktrees/{slug}",
                          "branch": branch, "source_ref": source_ref}

    invocation = capabilities_module.invoke(
        binding,
        ["prepare", "--workspace-root", str(root), "--requests-json",
         json.dumps(requests, separators=(",", ":"))],
        cwd=provider_root,
        timeout_seconds=180,
        output_limit_bytes=256 * 1024,
    )
    if invocation.get("status") != "ok":
        raise ValueError(
            f"workspace provider failed ({invocation.get('status')}): "
            f"{str(invocation.get('stderr', ''))[:1000]}"
        )
    try:
        result = json.loads(str(invocation.get("stdout", "")))
    except json.JSONDecodeError as error:
        raise ValueError("workspace provider returned invalid JSON") from error
    rows = result.get("selected_checkouts") if isinstance(result, dict) else None
    if not isinstance(rows, list) or {str(row.get("repository")) for row in rows if isinstance(row, dict)} != set(expected):
        raise ValueError("workspace provider returned an incomplete checkout closure")

    roots: dict[str, Path] = {}
    facts: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = str(row["repository"])
        requirement = expected[name]
        if row.get("path") != requirement["path"]:
            raise ValueError(f"workspace provider selected an unexpected path for {name}")
        if row.get("branch") != requirement["branch"] or row.get("source_ref") != requirement["source_ref"]:
            raise ValueError(f"workspace provider selected an unexpected branch or ref for {name}")
        if row.get("clean") is not True or row.get("head") != row.get("authoritative_head"):
            raise ValueError(f"workspace provider did not select a clean authoritative checkout for {name}")
        selected_root = (root / requirement["path"]).resolve()
        try:
            selected_root.relative_to(root)
        except ValueError as error:
            raise ValueError(f"workspace provider path escapes root for {name}") from error
        if (root / requirement["path"]).is_symlink() or not selected_root.is_dir():
            raise ValueError(f"workspace provider returned missing or symbolic checkout for {name}")
        roots[name] = selected_root
        facts[name] = dict(row)

    pinned = definition.get("pinned_checkouts", [])
    if not isinstance(pinned, list):
        raise ValueError("pinned_checkouts must be a list")
    for item in pinned:
        if not isinstance(item, dict):
            raise ValueError("pinned_checkouts entries must be objects")
        name = str(item.get("repository", ""))
        checkout_path = str(item.get("checkout", ""))
        revision = item.get("revision")
        relative_checkout = Path(checkout_path)
        if not name or not checkout_path or not isinstance(revision, str):
            raise ValueError("pinned_checkouts entries need repository, checkout, and revision")
        if relative_checkout.is_absolute() or ".." in relative_checkout.parts:
            raise ValueError("pinned checkout paths must be repository-relative")
        selected_root = (root / name / relative_checkout).resolve()
        if (root / name / relative_checkout).is_symlink() or not selected_root.is_dir():
            raise ValueError(f"pinned checkout is missing or a symbolic link: {name}")
        record = next(
            (candidate for candidate in workspace_view.get("repositories", [])
             if Path(str(candidate.get("path", ""))).resolve() == selected_root),
            None,
        )
        if record is None or record.get("dirty") or record.get("head") != revision:
            raise ValueError(f"pinned checkout is not clean at its declared revision: {name}")
        if name in roots:
            raise ValueError(f"repository appears more than once in selected checkout closure: {name}")
        roots[name] = selected_root
        facts[name] = {
            "repository": name,
            "path": str(selected_root),
            "branch": record.get("branch"),
            "head": record.get("head"),
            "clean": True,
            "source_ref": "session-pinned-checkout",
            "authoritative_head": revision,
        }

    session_checkout = definition.get("session_checkout")
    if session_checkout is not None:
        if not isinstance(session_checkout, dict):
            raise ValueError("session_checkout must be an object")
        name = str(session_checkout.get("repository", ""))
        checkout_path = str(session_checkout.get("checkout", ""))
        relative_checkout = Path(checkout_path)
        if not name or not checkout_path or relative_checkout.is_absolute() or ".." in relative_checkout.parts:
            raise ValueError("session_checkout needs a repository-relative checkout")
        selected_root = (root / name / relative_checkout).resolve()
        if (root / name / relative_checkout).is_symlink() or not selected_root.is_dir():
            raise ValueError("session checkout is missing or a symbolic link")
        record = next(
            (candidate for candidate in workspace_view.get("repositories", [])
             if Path(str(candidate.get("path", ""))).resolve() == selected_root),
            None,
        )
        if record is None or record.get("dirty"):
            raise ValueError("session checkout is not an observed clean checkout")
        if name in roots:
            raise ValueError(f"repository appears more than once in selected checkout closure: {name}")
        roots[name] = selected_root
        facts[name] = {
            "repository": name,
            "path": str(selected_root),
            "branch": record.get("branch"),
            "head": record.get("head"),
            "clean": True,
            "source_ref": "running-session-checkout",
            "authoritative_head": record.get("head"),
        }

    provider_fact = {
        "repository": repository,
        "path": str(provider_root),
        "branch": provider_record.get("branch"),
        "head": provider_record.get("head"),
        "clean": True,
        "source_ref": "session-pinned-provider",
        "authoritative_head": provider_record.get("head"),
        "capability": capability,
    }
    roots[repository] = provider_root
    facts[repository] = provider_fact
    return roots, facts


def resolve_environment(
    *,
    definition: dict[str, Any],
    workspace_root: str | Path,
    state_dir: str | Path,
    consumer_id: str,
    store: SessionStore | None = None,
    backend: str = "store",
    verify_on_open: bool = True,
) -> dict[str, Any]:
    """Resolve a declarative environment definition into an inspectable world."""
    resolved_root = workspace_module.validate_workspace_root(
        workspace_root, definition=definition
    )
    requirements = readiness_module.validate_requirements(definition)
    repository_selection = workspace_module.repository_selection(definition)
    workspace_view = workspace_module.discover_workspace(resolved_root, repositories=repository_selection)
    scan = workspace_view.get("scan", {})
    if workspace_view.get("error") or scan.get("status") != "complete":
        message = workspace_view.get("error") or scan.get(
            "message", "workspace scan did not complete"
        )
        raise workspace_module.WorkspaceResolutionError(
            f"workspace resolution stopped safely: {message}",
            diagnostics={
                "code": "workspace-scan-incomplete",
                "root": str(resolved_root),
                "scan": scan,
            },
        )
    selected_roots, selected_facts = _provider_managed_checkouts(
        definition, resolved_root, workspace_view
    )
    provisioned = bool(selected_roots)
    if repository_selection is not None:
        for name in repository_selection:
            path = resolved_root / name
            record = next(repo for repo in workspace_view["repositories"] if Path(repo["path"]) == path)
            selected_roots[name] = path
            selected_facts[name] = {"repository": name, "path": str(path), "head": record.get("head"),
                                    "branch": record.get("branch"), "clean": not record.get("dirty"),
                                    "source_ref": "explicit-workspace-selection", "authoritative_head": record.get("head")}
    toolchain = None
    if selected_roots:
        # Reconcile the workspace view after provider provisioning so the
        # resolved session includes the exact checkouts it selected.
        if provisioned:
            workspace_view = workspace_module.discover_workspace(resolved_root)
        scan = workspace_view.get("scan", {})
        if workspace_view.get("error") or scan.get("status") != "complete":
            message = workspace_view.get("error") or scan.get(
                "message", "workspace scan did not complete"
            )
            raise workspace_module.WorkspaceResolutionError(
                f"workspace resolution stopped safely after provider selection: {message}",
                diagnostics={
                    "code": "workspace-scan-incomplete",
                    "root": str(resolved_root),
                    "scan": scan,
                },
            )
        workspace_view["selected_checkouts"] = selected_facts
        language_root = selected_roots.get("mncs-language")
        language_binary = None
        if language_root is not None:
            candidates = (
                language_root / "target" / "release" / "mncs",
                language_root / "target" / "debug" / "mncs",
            )
            language_binary = next((path for path in candidates if path.is_file()), candidates[-1])
            language_facts = selected_facts["mncs-language"]
            toolchain = {
                "repository": "mncs-language",
                "checkout": str(language_root),
                "revision": language_facts.get("head"),
                "binary": str(language_binary) if language_binary.is_file() else None,
                "status": "available" if language_binary.is_file() else "unavailable",
            }
        discovered = capabilities_module.discover_capabilities(
            resolved_root,
            repository_roots=selected_roots,
            checkout_facts=selected_facts,
            language_binary=language_binary,
        )
    else:
        discovered = capabilities_module.discover_capabilities(resolved_root)
    toolchain = None
    language_root = selected_roots.get("mncs-language")
    language_record = selected_facts.get("mncs-language")
    if not selected_roots:
        language_record = next((repo for repo in workspace_view.get("repositories", [])
                                if (repo.get("manifest_repository") or repo.get("name")) == "mncs-language"), None)
        if language_record:
            language_root = Path(language_record["path"])
    if language_root is not None:
        candidates = (
            language_root / "target" / "release" / "mncs",
            language_root / "target" / "debug" / "mncs",
        )
        selected_binary = next((path for path in candidates if path.is_file()), candidates[-1])
        toolchain = {
            "repository": "mncs-language",
            "checkout": str(language_root),
            "revision": language_record.get("head"),
            "binary": str(selected_binary) if selected_binary.is_file() else None,
            "status": "available" if selected_binary.is_file() else "unavailable",
        }
    bindings = [capabilities_module.probe_availability(binding) for binding in discovered]
    unavailable = [
        {"provider": binding["provider"], "capability": binding["capability"],
         "reason": binding["availability"]["reason"]}
        for binding in bindings
        if binding["availability"]["status"] != "available"
    ]
    owns_store = store is None
    backend_store = store
    if backend_store is None:
        store_provider = (
            store_provider_from_environment({
                "workspace": {"root": str(resolved_root)},
                "selected_checkouts": selected_facts,
                "toolchain": toolchain,
            })
            if backend == "store"
            else None
        )
        backend_store = open_store(
            state_dir,
            backend,
            verify_on_open=verify_on_open,
            store_package_dir=(
                store_provider.get("python_package") if store_provider is not None else None
            ),
            store_runtime=(
                store_provider.get("runtime_environment")
                if store_provider is not None else None
            ),
        )
    intent = parse_intent(definition.get("intent", {"goal": definition.get("goal", "unspecified")}))
    protected = sorted(
        set(intent.get("protected_repositories", []))
        | set(definition.get("protected_repositories", []))
    )
    try:
        live_claims = claims_module.active_claims(backend_store.read_claims())
    finally:
        if owns_store:
            close = getattr(backend_store, "close", None)
            if callable(close):
                close()
    claim_holders: dict[str, list[dict[str, Any]]] = {}
    for record in live_claims.values():
        claim_holders.setdefault(str(record.get("repository", "")), []).append(
            {
                "claim_id": str(record.get("claim_id", "")),
                "session_id": str(record.get("session_id", "")),
                "consumer_id": str(record.get("consumer_id", "")),
                "basis": str(record.get("basis", "")),
                "scope": record.get("scope", {"kind": "repository"}),
            }
        )
    repo_names = [repo["name"] for repo in workspace_view.get("repositories", [])]
    rights = rights_module.evaluate_rights(
        workspace_repos=repo_names,
        records=rights_module.load_claim_records(definition, resolved_root),
    )
    authority_context = authority_module.build_context(
        subject=consumer_id,
        intent=intent,
        protected_repos=protected,
        claim_holders=claim_holders,
    )
    inputs = {
        "workspace_root": str(resolved_root),
        "repository_heads": {
            (name if selected_roots else repo["name"]): (
                selected_facts[name].get("head") if selected_roots else repo.get("head")
            )
            for name, repo in (
                selected_roots.items() if selected_roots
                else ((item["name"], item) for item in workspace_view.get("repositories", []))
            )
        },
        "binding_ids": sorted(binding["binding_id"] for binding in bindings),
        "intent": intent["identity"],
        "claim_holders": claim_holders,
    }
    definition_id = environment_id(definition)
    environment = {
        "schema_version": ENVIRONMENT_SCHEMA,
        "identity": resolved_environment_id(definition_id, inputs),
        "definition_id": definition_id,
        "configuration": {"name": definition.get("name"), "definition_id": definition_id},
        "requirements": requirements,
        "resolved_at": utcnow(),
        "consumer_id": consumer_id,
        "workspace": workspace_view,
        "selected_checkouts": selected_facts,
        "toolchain": toolchain,
        "repo_facts": _repo_facts(workspace_view),
        "bindings": bindings,
        "unavailable_capabilities": unavailable,
        "intent": intent,
        "protected_repositories": protected,
        "authority": authority_context,
        "claim_holders": claim_holders,
        "rights": rights,
        "event_sources": [
            {"kind": "session-log", "replay": True},
            {"kind": "adapter:git-poll", "replay": False,
             "note": "polling adapter; canonical provider events are a pressure"},
        ],
        "resolution_inputs_digest": digest_hex(inputs),
    }
    return environment


class Session:
    """A durable session bound to a session store (snapshot + event log)."""

    def __init__(self, store: SessionStore, session_id: str):
        # Sessions always open through create/open/resume below.
        self.store = store
        self.session_id = session_id
        snapshot = store.load_snapshot(session_id)
        if not isinstance(snapshot, dict) or snapshot.get("session_id") != session_id:
            raise LifecycleError(f"session {session_id} has no readable snapshot")
        self.snapshot = snapshot
        self._next_seq_hint: int | None = None
        self._log_cache: list[dict[str, Any]] | None = None
        self._log_cache_key: Any = None
        # Store-generation baseline for observe_store; None means this
        # handle has not adopted yet (adopts silently on first observe).
        self._feed_generation: int | None = None

    def close(self) -> None:
        close = getattr(self.store, "close", None)
        if callable(close):
            close()

    @property
    def state_dir(self) -> Path:
        return getattr(self.store, "state_dir", Path("."))

    # -- construction ----------------------------------------------------

    @classmethod
    def create(
        cls,
        *,
        state_dir: Path | str,
        environment: dict[str, Any],
        consumer_id: str,
        consumer_kind: str = "agent",
        backend: str = "store",
        verify_on_open: bool = True,
        store: SessionStore | None = None,
    ) -> "Session":
        rights_module.check_enter(
            [repo["name"] for repo in environment.get("workspace", {}).get("repositories", [])],
            environment.get("rights", {}),
        )
        session_id = new_session_id(environment["identity"], consumer_id)
        store_provider = (
            store_provider_from_environment(environment)
            if backend == "store"
            else None
        )
        if store is not None and store_provider is not None:
            selected_package = Path(str(store_provider["python_package"])).resolve()
            actual_package = getattr(
                getattr(store, "backend", None), "store_package_dir", None
            )
            if actual_package is None or Path(str(actual_package)).resolve() != selected_package:
                raise ValueError(
                    "provided Store handle is not bound to the session-selected mncs-store checkout"
                )
            selected_runtime = store_provider.get("runtime_environment")
            actual_runtime = getattr(
                getattr(store, "backend", None), "store_runtime", None
            )
            if selected_runtime is not None and actual_runtime != selected_runtime:
                raise ValueError(
                    "provided Store handle is not bound to the session-selected MNCS toolchain"
                )
        store = store or open_store(
            state_dir,
            backend,
            verify_on_open=verify_on_open,
            store_package_dir=(
                store_provider.get("python_package") if store_provider is not None else None
            ),
            store_runtime=(
                store_provider.get("runtime_environment")
                if store_provider is not None else None
            ),
        )
        if store_provider is not None:
            write_session_store_provider(state_dir, session_id, store_provider)
        selected_checkouts = environment.get("selected_checkouts", {})
        workspace_heads = (
            {name: selected.get("head") for name, selected in selected_checkouts.items()}
            if selected_checkouts
            else {
                repo["name"]: repo.get("head")
                for repo in environment.get("workspace", {}).get("repositories", [])
            }
        )
        snapshot = {
            "schema_version": SESSION_SCHEMA,
            "session_id": session_id,
            "environment_id": environment["identity"],
            "environment_digest": digest_hex(environment),
            "consumer_id": consumer_id,
            "consumer_kind": consumer_kind,
            "lifecycle": "defined",
            "lifecycle_history": [{"state": "defined", "reason": "session created", "at": utcnow()}],
            "intent": environment.get("intent"),
            "authority": environment.get("authority"),
            "rights": environment.get("rights", {}),
            "workspace": environment.get("workspace", {}),
            "selected_checkouts": environment.get("selected_checkouts", {}),
            "toolchain": environment.get("toolchain"),
            "configuration": environment.get("configuration", {}),
            "requirements": environment.get("requirements", {}),
            "service_observations": [],
            "repo_facts": environment.get("repo_facts", {}),
            "claim_holders": environment.get("claim_holders", {}),
            "bindings": environment.get("bindings", []),
            "workspace_heads": workspace_heads,
            "workspace_facts": _workspace_change_facts(
                environment.get("workspace", {}), selected_checkouts),
            "subscriptions": [],
            "artifacts": [],
            "decisions": [],
            "pressures": [],
            "checkpoints": [],
            "handoffs": [],
            "completion": None,
            "created_at": utcnow(),
            "updated_at": utcnow(),
            "snapshot_sequence": 0,
            "provenance": {
                "created_by": consumer_id,
                "definition_id": environment.get("definition_id"),
                "resolution_inputs_digest": environment.get("resolution_inputs_digest"),
            },
        }
        store.save_snapshot(session_id, snapshot)
        instance = cls(store, session_id)
        instance._emit("session.created", "environment", {"consumer_id": consumer_id})
        instance._save()
        return instance

    @classmethod
    def open(
        cls, *, state_dir: Path | str, session_id: str, backend: str = "store",
        verify_on_open: bool = True, store: SessionStore | None = None,
    ) -> "Session":
        """Read-only open: loads state without appending any event."""
        store = store or open_store(
            state_dir,
            backend,
            verify_on_open=verify_on_open,
            session_id=session_id,
        )
        return cls(store, session_id)

    @classmethod
    def resume(
        cls, *, state_dir: Path | str, session_id: str, backend: str = "store",
        verify_on_open: bool = True, store: SessionStore | None = None,
    ) -> "Session":
        """Resume active participation: transitions abandoned sessions, logs resume."""
        instance = cls.open(
            state_dir=state_dir, session_id=session_id, backend=backend,
            verify_on_open=verify_on_open, store=store)
        if instance.snapshot.get("lifecycle") in ("completed", "failed"):
            raise LifecycleError(f"session {session_id} is terminal and cannot resume")
        if instance.snapshot.get("lifecycle") == "abandoned":
            instance.transition("active", "resumed from abandoned")
        instance._emit("session.resumed", "environment",
                       {"consumer_id": instance.snapshot.get("consumer_id")})
        instance._save()
        return instance

    # -- persistence helpers ----------------------------------------------

    def _save(self) -> None:
        # Monotonic save sequence: every save is a new immutable Store
        # object. Concurrent participants can still race on the same revision;
        # persistence rejects differing contents rather than overwriting them.
        self.snapshot["snapshot_sequence"] = int(self.snapshot.get("snapshot_sequence", 0)) + 1
        self.snapshot["updated_at"] = utcnow()
        self.store.save_snapshot(self.session_id, self.snapshot)
        # Own write: re-baseline the store-feed observer so observe_store
        # only ever reports activity from outside this session.
        self._refresh_feed_baseline()

    def _cache_key(self) -> Any:
        generation = getattr(self.store, "generation", None)
        if callable(generation):
            try:
                return ("store", generation())
            except Exception:
                return None
        return None

    def _log(self) -> list[dict[str, Any]]:
        key = self._cache_key()
        if key is not None and key == self._log_cache_key and self._log_cache is not None:
            return self._log_cache
        log = self.store.read_events(self.session_id)
        if key is not None:
            self._log_cache = log
            self._log_cache_key = key
        return log

    def _invalidate_log(self) -> None:
        self._log_cache = None
        self._log_cache_key = None

    def _emit(
        self,
        event_type: str,
        producer: str,
        payload: dict[str, Any] | None = None,
        causes: list[str] | None = None,
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for _ in range(MAX_EMIT_RETRIES):
            if self._next_seq_hint is None:
                sequences = self.store.existing_sequences(self.session_id)
                self._next_seq_hint = (max(sequences) + 1) if sequences else 1
            sequence = self._next_seq_hint
            event = events_module.make(
                session_id=self.session_id,
                sequence=sequence,
                event_type=event_type,
                producer=producer,
                payload=payload,
                causes=causes,
            )
            try:
                self.store.put_event(self.session_id, sequence, event)
                self._invalidate_log()
                self._next_seq_hint = sequence + 1
                return event
            except SequenceTaken as error:
                last_error = error
                self._next_seq_hint = None
                continue
        raise LifecycleError(f"event log unwritable after retries: {last_error}")

    # -- lifecycle ---------------------------------------------------------

    def transition(self, to_state: str, reason: str) -> dict[str, Any]:
        current = self.snapshot.get("lifecycle")
        allowed = TRANSITIONS.get(current, ())
        if to_state not in allowed:
            raise LifecycleError(f"illegal transition {current} -> {to_state}: {reason}")
        self.snapshot["lifecycle"] = to_state
        self.snapshot["lifecycle_history"].append({"state": to_state, "reason": reason, "at": utcnow()})
        self._save()
        return {"from": current, "to": to_state, "reason": reason}

    # -- intent / authority / capabilities ----------------------------------

    def attach_intent(self, raw_intent: dict[str, Any]) -> dict[str, Any]:
        intent = parse_intent(raw_intent)
        self.snapshot["intent"] = intent
        self._emit("intent.attached", self.snapshot.get("consumer_id", "unknown"),
                   {"intent_id": intent["identity"]})
        self._save()
        return intent

    def provision_checkouts(
        self,
        provider_capability: str,
        requests: list[dict[str, str]],
        *,
        timeout_seconds: int = 180,
    ) -> dict[str, Any]:
        """Extend this session's exact checkout closure through its provider.

        Environment composes the provider contract and validates the selected
        facts. The provider remains responsible for resolving Git revisions
        and creating or selecting worktrees. New checkouts become part of this
        session's authority and capability-binding closure only after the
        provider result agrees with a fresh workspace observation.
        """
        if self.snapshot.get("lifecycle") != "active":
            raise LifecycleError("checkout provisioning requires an active session")
        if not requests:
            raise ValueError("checkout provisioning requires at least one request")
        workspace_root_value = self.snapshot.get("workspace", {}).get("root")
        if not isinstance(workspace_root_value, str) or not workspace_root_value:
            raise ValueError("session has no resolved workspace root")
        workspace_root = Path(workspace_root_value).resolve()

        binding = self._binding(provider_capability)
        provider_repository = str(binding.get("provider", ""))
        selected_provider = self.snapshot.get("selected_checkouts", {}).get(provider_repository)
        if not provider_repository or not isinstance(selected_provider, dict):
            raise AuthorityDenied("checkout provider is not bound to a selected session checkout")
        provider_path = Path(str(selected_provider.get("path", "")))
        if not provider_path.is_absolute():
            provider_path = workspace_root / provider_path
        provider_path = provider_path.resolve()
        if provider_path != Path(str(binding.get("provider_root", ""))).resolve():
            raise AuthorityDenied("checkout provider binding does not match its selected checkout")
        try:
            provider_path.relative_to(workspace_root)
        except ValueError as error:
            raise AuthorityDenied("checkout provider escapes the session workspace") from error

        expected: dict[str, dict[str, str]] = {}
        normalized_requests: list[dict[str, str]] = []
        protected = set(self.snapshot.get("intent", {}).get("protected_repositories", []))
        selected = self.snapshot.get("selected_checkouts", {})
        for request in requests:
            if not isinstance(request, dict):
                raise ValueError("checkout requests must be objects")
            repository = str(request.get("repository", ""))
            name = str(request.get("name", ""))
            branch = str(request.get("branch", ""))
            source_ref = str(request.get("source_ref", "origin/main"))
            if not repository or not name or not branch:
                raise ValueError("checkout requests need repository, name, and branch")
            if repository in protected:
                raise AuthorityDenied(f"{repository} is protected by this session intent")
            if repository in selected:
                raise ValueError(f"{repository} is already selected in this session")
            if repository in expected:
                raise ValueError(f"duplicate checkout request for {repository}")
            if Path(name).is_absolute() or ".." in Path(name).parts or "/" in name or "\\" in name:
                raise ValueError("checkout name must be a single safe path component")
            relative_path = f"{repository}/.worktrees/{name}"
            expected[repository] = {
                "path": relative_path,
                "branch": branch,
                "source_ref": source_ref,
            }
            normalized_requests.append({
                "repository": repository,
                "name": name,
                "branch": branch,
                "source_ref": source_ref,
            })

        self._refresh_holders()
        self.snapshot.setdefault("authority", {})["claim_holders"] = dict(
            self.snapshot.get("claim_holders", {})
        )
        invocation = self.invoke(
            provider_capability,
            ["prepare", "--workspace-root", str(workspace_root), "--requests-json",
             json.dumps(normalized_requests, separators=(",", ":"))],
            cwd=provider_path,
            timeout_seconds=timeout_seconds,
            output_limit_bytes=256 * 1024,
        )
        if invocation.get("status") != "ok" or invocation.get("returncode") != 0:
            raise ValueError(
                f"checkout provider failed ({invocation.get('status')}): "
                f"{str(invocation.get('stderr', ''))[:1000]}"
            )
        try:
            result = json.loads(str(invocation.get("stdout", "")))
        except json.JSONDecodeError as error:
            raise ValueError("checkout provider returned invalid JSON") from error
        rows = result.get("selected_checkouts") if isinstance(result, dict) else None
        if (not isinstance(rows, list)
                or {str(row.get("repository")) for row in rows if isinstance(row, dict)}
                != set(expected)):
            raise ValueError("checkout provider returned an incomplete checkout selection")

        workspace = workspace_module.discover_workspace(workspace_root)
        if workspace.get("error") or workspace.get("scan", {}).get("status") != "complete":
            raise workspace_module.WorkspaceResolutionError(
                "workspace revalidation stopped safely; keeping the previous complete observation",
                diagnostics={"code": "workspace-scan-incomplete", "root": str(workspace_root),
                             "scan": workspace.get("scan", {}), "next": "check workspace availability, then reconcile the session"})
        repositories = workspace.get("repositories", [])
        selected_facts: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("checkout provider returned a malformed selection")
            repository = str(row.get("repository", ""))
            requirement = expected[repository]
            if row.get("path") != requirement["path"]:
                raise ValueError(f"checkout provider selected an unexpected path for {repository}")
            if (row.get("branch") != requirement["branch"]
                    or row.get("source_ref") != requirement["source_ref"]):
                raise ValueError(f"checkout provider selected an unexpected branch or ref for {repository}")
            if (row.get("clean") is not True or not row.get("head")
                    or row.get("head") != row.get("authoritative_head")):
                raise ValueError(
                    f"checkout provider did not select a clean authoritative checkout for {repository}"
                )
            selected_path = (workspace_root / requirement["path"]).resolve()
            try:
                selected_path.relative_to(workspace_root)
            except ValueError as error:
                raise ValueError(f"selected checkout escapes workspace for {repository}") from error
            if (workspace_root / requirement["path"]).is_symlink():
                raise ValueError(f"selected checkout is a symbolic link for {repository}")
            observed = next(
                (item for item in repositories
                 if Path(str(item.get("path", ""))).resolve() == selected_path),
                None,
            )
            if observed is None or observed.get("dirty") or observed.get("head") != row.get("head"):
                raise ValueError(
                    f"fresh workspace observation disagrees with provider selection for {repository}"
                )
            selected_facts[repository] = {
                **row,
                "path": requirement["path"],
                "missing": False,
            }

        raw_intent = {
            key: value for key, value in self.snapshot.get("intent", {}).items()
            if key not in ("identity", "schema_version")
        }
        raw_intent["repositories"] = sorted(
            set(raw_intent.get("repositories", [])) | set(selected_facts)
        )
        updated_intent = parse_intent(raw_intent)
        merged = {**selected, **selected_facts}
        workspace["selected_checkouts"] = merged
        self.snapshot["intent"] = updated_intent
        self.snapshot["selected_checkouts"] = merged
        self.snapshot["workspace"] = workspace
        self.snapshot["repo_facts"] = _repo_facts(workspace)
        self.snapshot["authority"] = authority_module.build_context(
            subject=self.snapshot.get("consumer_id", "unknown"),
            intent=updated_intent,
            protected_repos=self.snapshot.get("protected_repositories", []),
            claim_holders=self.snapshot.get("claim_holders", {}),
        )
        self.snapshot["workspace_heads"] = {
            **self.snapshot.get("workspace_heads", {}),
            **{name: facts.get("head") for name, facts in selected_facts.items()},
        }
        self.snapshot["workspace_facts"] = _workspace_change_facts(workspace, merged)

        language_root = None
        language_facts = merged.get("mncs-language")
        if isinstance(language_facts, dict):
            language_root = Path(str(language_facts.get("path", "")))
            if not language_root.is_absolute():
                language_root = workspace_root / language_root
        if language_root is not None:
            candidates = (
                language_root / "target" / "release" / "mncs",
                language_root / "target" / "debug" / "mncs",
            )
            selected_binary = next((path for path in candidates if path.is_file()), candidates[-1])
            self.snapshot["toolchain"] = {
                "repository": "mncs-language",
                "checkout": str(language_root.resolve()),
                "revision": language_facts.get("head"),
                "binary": str(selected_binary) if selected_binary.is_file() else None,
                "status": "available" if selected_binary.is_file() else "unavailable",
            }
        self.revalidate()
        self._emit(
            "workspace.closure.extended", "environment",
            {"provider": provider_capability,
             "repositories": sorted(selected_facts),
             "checkouts": [
                 {"repository": name, "path": facts["path"], "head": facts["head"],
                  "branch": facts["branch"]}
                 for name, facts in sorted(selected_facts.items())
             ]},
        )
        self._save()
        return {"selected_checkouts": list(selected_facts.values()), "bindings": self.snapshot.get("bindings", [])}

    def _canonical_scope(
        self, repository: str, scope: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if not scope or scope.get("kind") != "worktree":
            return scope
        checkout = scope.get("checkout")
        if not isinstance(checkout, str) or not checkout:
            return scope
        path = Path(checkout)
        workspace_root = self.snapshot.get("workspace", {}).get("root")
        if not path.is_absolute() and isinstance(workspace_root, str) and workspace_root:
            path = Path(workspace_root) / path
        normalized = dict(scope)
        normalized["checkout"] = str(path.resolve())
        return normalized

    def check(
        self,
        *,
        action: str,
        target: str,
        repo_facts: dict[str, dict[str, Any]] | None = None,
        scope: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        repository = target.split("/")[0] if "/" in target else target
        normalized_scope = self._canonical_scope(repository, scope)
        if action in ("write", "mutate", "execute", "publish", "merge", "delete"):
            # Current entry observations are not a mutation lease. Re-read
            # claims and the exact selected target at the authority boundary;
            # broad discovery is unnecessary on a quiet observational entry.
            self._refresh_holders()
            selected = self.snapshot.get("selected_checkouts", {}).get(repository)
            if repo_facts is None and isinstance(selected, dict) and selected.get("path"):
                root = Path(self.snapshot.get("workspace", {}).get("root", "."))
                inspected = workspace_module.inspect_repo((root / selected["path"]).resolve())
                if inspected is None or inspected.git_error:
                    raise LifecycleError("selected checkout observation unavailable at mutation boundary")
                record = inspected.record()
                record["name"] = repository
                repo_facts = {**self.snapshot.get("repo_facts", {}),
                              **_repo_facts({"repositories": [record]})}
        verdict = authority_module.evaluate(
            self.snapshot.get("authority", {}),
            action=action,
            target=target,
            session_id=self.session_id,
            claims=self.snapshot.get("claim_holders", {}),
            repo_facts=repo_facts if repo_facts is not None else self.snapshot.get("repo_facts", {}),
            scope=normalized_scope,
        )
        if verdict["verdict"] == "deny":
            self._emit("authority.denied", "environment",
                       {"action": action, "target": target, "reason": verdict["reason"]})
        elif verdict["verdict"] == "escalate":
            self._emit("authority.escalated", "environment",
                       {"action": action, "target": target, "reason": verdict["reason"]})
        return verdict

    def last_activity_at(self) -> str | None:
        """Newest event observation time, or None for a virgin session."""
        log = self._log()
        if not log:
            return None
        return str(log[-1].get("observed_at"))

    def _reconciler_log(self, limit: int = 100) -> list[dict[str, Any]]:
        """Bounded tail of the reconciler session log for cross-session items."""
        from . import reconciler as reconciler_module
        try:
            session_ids = self.store.list_sessions()
        except Exception:
            return []
        for session_id in session_ids:
            if session_id == self.session_id:
                continue
            try:
                other = Session(self.store, session_id)
            except Exception:
                continue
            if other.snapshot.get("consumer_id") == reconciler_module.RECONCILER_CONSUMER:
                return other._log()[-limit:]
        return []

    def brief(self, *, commons_work: dict[str, Any] | None = None) -> dict[str, Any]:
        """Compact update capsule: relevant delta + progress, cursor untouched."""
        from . import briefing as briefing_module
        return briefing_module.build_capsule(
            self, self.store, commons_work=commons_work,
            reconciler_log=self._reconciler_log())

    def catchup(self) -> dict[str, Any]:
        """Resume-time capsule: what changed while this consumer was away."""
        return self.brief()

    def updates(self, since: int | None = None) -> dict[str, Any]:
        """Classified event delta since an index (defaults to brief cursor)."""
        from . import briefing as briefing_module
        log = self._log()
        total = len(log)
        cursor = self.snapshot.get("brief_cursor", {})
        index = int(cursor.get("index", 0)) if isinstance(cursor, dict) else 0
        if since is not None:
            index = max(0, min(int(since), total))
        items = []
        for event in log[index:]:
            item = briefing_module.classify_event(event)
            if item is not None:
                items.append(item)
        return {"session_id": self.session_id, "index": index, "total": total,
                "items": items[:briefing_module.MAX_BRIEF_ITEMS],
                "truncated": len(items) > briefing_module.MAX_BRIEF_ITEMS}

    def ack(self, index: int) -> dict[str, Any]:
        """Acknowledge the brief cursor at an event index."""
        from . import briefing as briefing_module
        return briefing_module.acknowledge(
            self, index, str(self.snapshot.get("consumer_id", "unknown")))

    def _binding(self, capability: str) -> dict[str, Any]:
        for binding in self.snapshot.get("bindings", []):
            if binding.get("capability") == capability or binding.get("binding_id") == capability:
                return binding
        raise AuthorityDenied(f"no binding for capability {capability!r} in this session")

    def _selected_runtime_environment(self, binding: dict[str, Any]) -> dict[str, str]:
        selected_checkouts = self.snapshot.get("selected_checkouts", {})
        if not isinstance(selected_checkouts, dict) or not all(
            selected_checkouts.get(repository) for repository in ("mncs-store", "mncs-language")
        ):
            return {}
        provider = store_provider_from_environment({
            "workspace": self.snapshot.get("workspace", {}),
            "selected_checkouts": selected_checkouts,
            "toolchain": self.snapshot.get("toolchain"),
        })
        if provider is None:
            return {}
        runtime = provider.get("runtime_environment")
        if not isinstance(runtime, dict):
            return {}
        contextual = {
            **runtime,
            "MNCS_STORE_PYTHON": provider["python_package"],
            "MNCS_STORE_ARTIFACT_CACHE": str(self.store.state_dir.resolve() / "provider-cache" / "mncs-store"),
            "MNCS_LANGUAGE_CHECKOUT": runtime["MNCS_LANGUAGE_ROOT"],
        }
        declared = set((binding.get("fixed_env") or {}).keys())
        declared_toolchain_env = binding.get("toolchain_env")
        if isinstance(declared_toolchain_env, str) and declared_toolchain_env:
            declared.add(declared_toolchain_env)
        return {key: value for key, value in contextual.items() if key not in declared}

    def invoke(
        self,
        capability: str,
        argv: list[str],
        *,
        cwd: str | Path | None = None,
        timeout_seconds: int | None = None,
        output_limit_bytes: int = capabilities_module.DEFAULT_OUTPUT_LIMIT_BYTES,
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Invoke a bound capability. Deny and escalate NEVER spawn a process."""
        output_limit_bytes = capabilities_module.validate_output_limit_bytes(
            output_limit_bytes
        )
        binding = self._binding(capability)
        state_root = self.store.state_dir.resolve()
        artifact_directory = (
            state_root / "sessions" / self.session_id / "artifacts"
            / digest_hex(capability)
        ).resolve()
        if not artifact_directory.is_relative_to(state_root):
            raise LifecycleError("session artifact directory escapes the Environment state root")
        artifact_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        invocation_env = dict(env or {})
        invocation_env.update(self._selected_runtime_environment(binding))
        invocation_env["MNCS_ENV_SESSION_ARTIFACT_DIR"] = str(artifact_directory)
        if binding.get("availability", {}).get("status") != "available":
            raise AuthorityDenied(
                f"capability {capability!r} is not available: "
                f"{binding.get('availability', {}).get('reason')}"
            )
        verdict = self.check(action="invoke", target=capability)
        if verdict["verdict"] == "deny":
            raise AuthorityDenied(verdict["reason"])
        if verdict["verdict"] == "escalate":
            return self._pending(capability, argv, "invoke", verdict["reason"])
        required = authority_module.required_action_for_effects(binding.get("effects", ["read"]))
        if required != "read":
            provider = str(binding.get("provider", capability))
            checkout = binding.get("provenance", {}).get("checkout", {})
            checkout_path = checkout.get("path") or binding.get("provider_root")
            effect_scope = None
            if isinstance(checkout_path, str) and checkout_path:
                selected_checkout = None
                workspace_root = self.snapshot.get("workspace", {}).get("root")
                raw_selected_path = Path(checkout_path)
                if not raw_selected_path.is_absolute() and workspace_root:
                    raw_selected_path = Path(str(workspace_root)) / raw_selected_path
                selected_path = raw_selected_path.resolve()
                for repository, record in self.snapshot.get("selected_checkouts", {}).items():
                    raw_selected = Path(str(record.get("path", "")))
                    if not raw_selected.is_absolute() and workspace_root:
                        raw_selected = Path(str(workspace_root)) / raw_selected
                    if raw_selected.resolve() == selected_path:
                        provider = str(repository)
                        selected_checkout = record
                        break
                effect_scope = {"kind": "worktree", "checkout": str(selected_path)}
                branch = checkout.get("branch") or (
                    selected_checkout.get("branch") if selected_checkout else None
                )
                if isinstance(branch, str) and branch:
                    effect_scope["branch"] = branch
            effect_verdict = self.check(
                action=required, target=provider, scope=effect_scope)
            if effect_verdict["verdict"] == "deny":
                raise AuthorityDenied(effect_verdict["reason"])
            if effect_verdict["verdict"] == "escalate":
                return self._pending(capability, argv, required, effect_verdict["reason"])
        self._emit("capability.invoked", self.snapshot.get("consumer_id", "unknown"),
                   {"capability": capability, "argv": argv})
        result = capabilities_module.invoke(
            binding, argv, cwd=cwd, timeout_seconds=timeout_seconds,
            output_limit_bytes=output_limit_bytes, env=invocation_env)
        self.snapshot.setdefault("artifacts", []).append(
            {"kind": "invocation-result", "capability": capability,
             "status": result["status"],
             "artifact_directory": str(artifact_directory), "at": utcnow()}
        )
        self._emit("invocation.completed", binding.get("provider", "unknown"),
                   {"capability": capability, "status": result["status"],
                    "returncode": result["returncode"]})
        self._save()
        return result

    def _pending(self, capability: str, argv: list[str], action: str, reason: str) -> dict[str, Any]:
        self._emit("authority.escalated", "environment",
                   {"action": action, "target": capability, "reason": reason, "argv": argv})
        self._save()
        return {
            "binding_id": None,
            "capability": capability,
            "status": "pending-escalation",
            "returncode": None,
            "stdout": "",
            "stderr": f"escalation required, nothing executed: {reason}",
            "truncated": False,
        }

    # -- claims ---------------------------------------------------------------

    def _refresh_holders(self) -> None:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for record in claims_module.active_claims(self.store.read_claims()).values():
            grouped.setdefault(str(record.get("repository", "")), []).append(
                {
                    "claim_id": str(record.get("claim_id", "")),
                    "session_id": str(record.get("session_id", "")),
                    "consumer_id": str(record.get("consumer_id", "")),
                    "basis": str(record.get("basis", "")),
                    "scope": record.get("scope", {"kind": "repository"}),
                }
            )
        self.snapshot["claim_holders"] = grouped
        self.snapshot.setdefault("authority", {})["claim_holders"] = dict(grouped)

    def acquire_claim(
        self, repository: str, *, basis: str = claims_module.BASIS_EXPLICIT,
        reason: str = "", ttl_hours: int = 24,
        scope: dict[str, Any] | None = None,
        checkout_facts: dict[str, Any] | None = None,
        workspace_root: str | None = None,
    ) -> dict[str, Any]:
        selected = self.snapshot.get("selected_checkouts", {}).get(repository)
        if selected is not None:
            if scope and scope.get("kind") == "worktree":
                scope = self._canonical_scope(repository, scope)
                requested = Path(str(scope.get("checkout", ""))).resolve()
                observed = Path(str(selected.get("path", "")))
                if not observed.is_absolute():
                    root = self.snapshot.get("workspace", {}).get("root")
                    if root:
                        observed = Path(str(root)) / observed
                observed = observed.resolve()
                if requested != observed:
                    raise ValueError(
                        f"claim checkout does not match the session-selected checkout for {repository}"
                    )
                scope = dict(scope)
                scope["checkout"] = str(observed)
                scope.setdefault("branch", selected.get("branch"))
            if checkout_facts is None:
                clean = bool(selected.get("clean", False))
                checkout_facts = {
                    "head": selected.get("head"),
                    "branch": selected.get("branch"),
                    "dirty": not clean,
                    "foreign_signals": [] if clean else ["dirty-tree"],
                }
        if workspace_root is None:
            workspace_root = self.snapshot.get("workspace", {}).get("root")
        record = claims_module.acquire(
            self.store, repository=repository, session_id=self.session_id,
            consumer_id=self.snapshot.get("consumer_id", "unknown"),
            basis=basis, reason=reason, ttl_hours=ttl_hours,
            scope=scope, checkout_facts=checkout_facts,
            workspace_root=workspace_root,
        )
        self._refresh_holders()
        self._emit("lease.acquired", self.snapshot.get("consumer_id", "unknown"),
                   {"repository": repository, "basis": basis,
                    "claim_id": record["identity"],
                    "scope": record.get("scope", {})})
        self._save()
        return record

    def release_claim(self, repository: str, reason: str = "",
                      claim_id: str | None = None) -> bool:
        released = claims_module.release(
            self.store, session_id=self.session_id, reason=reason,
            claim_id=claim_id, repository=repository or None)
        if not released:
            return False
        self._refresh_holders()
        self._emit("lease.released", self.snapshot.get("consumer_id", "unknown"),
                   {"repository": repository, "claim_id": claim_id,
                    "released": len(released)})
        self._save()
        return True

    def transfer_claim(self, claim_id: str, to_session: str,
                       to_consumer: str, reason: str = "") -> dict[str, Any]:
        record = claims_module.transfer(
            self.store, claim_id=claim_id, from_session=self.session_id,
            to_session=to_session, to_consumer=to_consumer, reason=reason)
        self._refresh_holders()
        self._emit("lease.transferred", self.snapshot.get("consumer_id", "unknown"),
                   {"claim_id": claim_id, "to_session": to_session})
        self._save()
        return record

    # -- events / subscriptions ---------------------------------------------

    def subscribe(self, event_types: list[str], source_filter: str | None = None) -> dict[str, Any]:
        subscription = events_module.subscribe(
            subscription_id=f"sub_{digest_hex([self.session_id, event_types, source_filter])[:12]}",
            session_id=self.session_id,
            event_types=event_types,
            source_filter=source_filter,
            cursor=len(self._log()),
        )
        subscriptions = [sub for sub in self.snapshot.get("subscriptions", [])
                         if sub["subscription_id"] != subscription["subscription_id"]]
        subscriptions.append(subscription)
        self.snapshot["subscriptions"] = subscriptions
        self._save()
        return subscription

    def poll(self, subscription_id: str) -> list[dict[str, Any]]:
        for subscription in self.snapshot.get("subscriptions", []):
            if subscription["subscription_id"] == subscription_id:
                due, advanced = events_module.deliverable(subscription, self._log())
                subscription["cursor"] = advanced["cursor"]
                self._save()
                return due
        raise LifecycleError(f"unknown subscription {subscription_id}")

    def record_observation(
        self, event_type: str, producer: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return self._emit(event_type, producer, payload)

    def observe_workspace(self, workspace_root: str | Path) -> list[dict[str, Any]]:
        """Adapter poll: selected checkout revision and state changes become events."""
        workspace = workspace_module.discover_workspace(
            workspace_root, repositories=self.snapshot.get("workspace", {}).get("selection"))
        if workspace.get("error") or workspace.get("scan", {}).get("status") != "complete":
            raise workspace_module.WorkspaceResolutionError(
                "workspace revalidation stopped safely; keeping the previous complete observation",
                diagnostics={"code": "workspace-scan-incomplete", "root": str(workspace_root),
                             "scan": workspace.get("scan", {}), "next": "check workspace availability, then reconcile the session"})
        if self.snapshot.get("selected_checkouts"):
            current_checkouts: dict[str, dict[str, Any]] = {}
            for name, previous_checkout in self.snapshot["selected_checkouts"].items():
                selected_path = Path(str(previous_checkout.get("path", "")))
                if not selected_path.is_absolute():
                    selected_path = Path(workspace_root) / selected_path
                match = next(
                    (item for item in workspace.get("repositories", [])
                     if Path(str(item.get("path", ""))).resolve() == selected_path.resolve()),
                    None,
                )
                if match is None:
                    current_checkouts[name] = {
                        **previous_checkout, "head": None, "clean": False,
                        "missing": True,
                    }
                else:
                    current_checkouts[name] = {
                        **previous_checkout,
                        "head": match.get("head"),
                        "branch": match.get("branch"),
                        "clean": not bool(match.get("dirty")),
                        "missing": False,
                    }
            current = {name: item.get("head") for name, item in current_checkouts.items()}
            self.snapshot["selected_checkouts"] = current_checkouts
        else:
            current = {
                repo["name"]: repo.get("head")
                for repo in workspace.get("repositories", [])
            }
        previous = self.snapshot.get("workspace_heads", {})
        previous_states = self.snapshot.get("workspace_facts")
        if not isinstance(previous_states, dict):
            previous_states = _workspace_change_facts(
                self.snapshot.get("workspace", {}),
                self.snapshot.get("selected_checkouts", {}),
            )
        current_states = _workspace_change_facts(
            workspace, current_checkouts if self.snapshot.get("selected_checkouts") else None)
        events, _ = events_module.git_poll_events(
            session_id=self.session_id,
            sequence_start=len(self._log()) + 1,
            previous_heads=previous,
            current_heads=current,
            previous_states=previous_states,
            current_states=current_states,
        )
        for event in events:
            self._emit(event["type"], event["producer"], event["payload"])
        self.snapshot["workspace_heads"] = current
        self.snapshot["workspace_facts"] = current_states
        if self.snapshot.get("selected_checkouts"):
            workspace["selected_checkouts"] = self.snapshot["selected_checkouts"]
        self.snapshot["workspace"] = workspace
        self.snapshot["repo_facts"] = _repo_facts(workspace)
        self._save()
        return events

    def _feed_generation_now(self) -> int | None:
        generation = getattr(self.store, "generation", None)
        if not callable(generation):
            return None
        try:
            return int(generation())
        except Exception:
            return None

    def _refresh_feed_baseline(self) -> None:
        current = self._feed_generation_now()
        if current is None:
            return
        self._feed_generation = current
        self.snapshot["last_store_generation"] = current

    def observe_store(self) -> dict[str, Any] | None:
        """Read the Store generation: an external advance becomes an event.

        Every session write re-baselines through ``_save``, so a delta
        observed here is Store activity from outside this session by
        construction. A fresh handle adopts the current generation
        silently: resume already re-reads snapshot, log, and claims, so
        nothing is lost by not reporting the downtime delta as an event.
        """
        current = self._feed_generation_now()
        if current is None:
            return None
        if self._feed_generation is None:
            self._feed_generation = current
            self.snapshot["last_store_generation"] = current
            self._save()
            return None
        if current == self._feed_generation:
            return None
        previous = self._feed_generation
        event = self._emit("adapter.observed", "adapter:store-feed",
                           {"generation": current, "previous": previous,
                            "note": "store generation advanced outside this session"})
        self._save()
        return event

    def revalidate(self) -> dict[str, Any]:
        """Refresh selected providers, reprobe bindings, and reconcile claims."""
        workspace_root = self.snapshot.get("workspace", {}).get("root")
        if workspace_root:
            self.observe_workspace(workspace_root)

        old_bindings = list(self.snapshot.get("bindings", []))
        selected = self.snapshot.get("selected_checkouts", {})
        discovered = old_bindings
        if workspace_root and selected:
            roots: dict[str, Path] = {}
            facts: dict[str, dict[str, Any]] = {}
            for repository, record in sorted(selected.items()):
                raw_path = Path(str(record.get("path", "")))
                path = raw_path if raw_path.is_absolute() else Path(workspace_root) / raw_path
                path = path.resolve()
                try:
                    path.relative_to(Path(workspace_root).resolve())
                except ValueError:
                    continue
                if record.get("missing") or path.is_symlink() or not path.is_dir():
                    continue
                roots[str(repository)] = path
                facts[str(repository)] = dict(record, path=str(path))
            language_binary: Path | str = "/nonexistent/mncs-language/mncs"
            if "mncs-language" in roots:
                candidates = (
                    roots["mncs-language"] / "target" / "release" / "mncs",
                    roots["mncs-language"] / "target" / "debug" / "mncs",
                )
                language_binary = next(
                    (item for item in candidates if item.is_file()), candidates[-1]
                )
            discovered = capabilities_module.discover_capabilities(
                workspace_root,
                repository_roots=roots,
                checkout_facts=facts,
                language_binary=language_binary,
            )
        elif workspace_root:
            discovered = capabilities_module.discover_capabilities(workspace_root)

        toolchain = self.snapshot.get("toolchain")
        if isinstance(toolchain, dict):
            checkout = Path(str(toolchain.get("checkout", "")))
            candidates = (checkout / "target" / "release" / "mncs", checkout / "target" / "debug" / "mncs")
            binary = next((path for path in candidates if path.is_file()), None)
            language = self.snapshot.get("selected_checkouts", {}).get("mncs-language", {})
            self.snapshot["toolchain"] = {**toolchain, "binary": str(binary) if binary else None,
                                          "status": "available" if binary else "unavailable",
                                          "revision": language.get("head", toolchain.get("revision"))}

        prior_by_key = {(item.get("provider"), item.get("capability")): item
                        for item in old_bindings}
        current_by_key = {(item.get("provider"), item.get("capability")): item
                          for item in discovered}
        binding_fields = (
                "contract_revision", "entrypoint", "address", "effects", "toolchain_address",
                "toolchain_env", "fixed_argv", "fixed_env", "timeout_seconds", "working_directory", "provenance",
            "provider_root",
        )
        report: dict[str, Any] = {
            "reprobed": 0, "changed": [], "bound": [], "unbound": []
        }
        for key, binding in current_by_key.items():
            previous = prior_by_key.get(key)
            if previous is None:
                report["bound"].append(binding.get("capability"))
                self._emit("capability.bound", "environment",
                           {"capability": binding.get("capability"),
                            "provider": binding.get("provider"),
                            "provider_root": binding.get("provider_root")})
            elif any(previous.get(field) != binding.get(field) for field in binding_fields):
                self._emit("capability.changed", "environment",
                           {"capability": binding.get("capability"),
                            "provider": binding.get("provider"),
                            "previous_revision": previous.get("contract_revision"),
                            "current_revision": binding.get("contract_revision"),
                            "provider_root": binding.get("provider_root")})
        for key, previous in prior_by_key.items():
            if key not in current_by_key:
                report["unbound"].append(previous.get("capability"))
                self._emit("capability.unbound", "environment",
                           {"capability": previous.get("capability"),
                            "provider": previous.get("provider"),
                            "provider_root": previous.get("provider_root")})

        new_bindings = []
        for binding in discovered:
            before = prior_by_key.get((binding.get("provider"), binding.get("capability")), {})
            before_status = before.get("availability", {}).get("status")
            fresh = capabilities_module.probe_availability(binding)
            new_bindings.append(fresh)
            report["reprobed"] += 1
            if fresh["availability"]["status"] != before_status:
                report["changed"].append(binding["capability"])
                self._emit(
                    "capability.available" if fresh["availability"]["status"] == "available"
                    else "capability.unavailable",
                    "environment",
                    {"capability": binding["capability"], "previous": before_status},
                )
        self.snapshot["bindings"] = new_bindings
        holders = claims_module.holders(self.store.read_claims())
        if holders != self.snapshot.get("claim_holders", {}):
            self._emit("adapter.observed", "environment",
                       {"note": "claim holders changed", "holders": holders})
        self.snapshot["claim_holders_detailed"] = holders
        self.snapshot["claim_holders"] = holders
        self.snapshot.setdefault("authority", {})["claim_holders"] = dict(holders)
        self._save()
        return report

    # -- checkpoint / handoff / completion ------------------------------------

    def checkpoint(self, *, progress: str = "", remaining: list[str] | None = None) -> dict[str, Any]:
        sequence = len(self.snapshot.get("checkpoints", [])) + 1
        state_digest = digest_hex(
            {"intent": self.snapshot.get("intent"), "artifacts": self.snapshot.get("artifacts"),
             "decisions": self.snapshot.get("decisions"), "heads": self.snapshot.get("workspace_heads")}
        )
        record = {
            "schema_version": CHECKPOINT_SCHEMA,
            "identity": checkpoint_id(self.session_id, sequence, state_digest),
            "session_id": self.session_id,
            "sequence": sequence,
            "progress": progress,
            "remaining": list(remaining or []),
            "intent_id": (self.snapshot.get("intent") or {}).get("identity"),
            "environment_id": self.snapshot.get("environment_id"),
            "event_cursor": len(self._log()),
            "artifacts": list(self.snapshot.get("artifacts", [])),
            "unresolved": list(self.snapshot.get("pressures", [])),
            "revalidate_on_resume": ["bindings", "workspace-heads", "claims", "authority"],
            "created_at": utcnow(),
            "created_by": self.snapshot.get("consumer_id"),
        }
        self.store.save_checkpoint(self.session_id, record)
        self.snapshot.setdefault("checkpoints", []).append(record["identity"])
        if self.snapshot.get("lifecycle") == "active":
            self.transition("checkpointed", f"checkpoint {sequence}")
            self.transition("active", "resumed after checkpoint")
        self._emit("session.checkpointed", self.snapshot.get("consumer_id", "unknown"),
                   {"checkpoint_id": record["identity"], "progress": progress})
        self._save()
        return record

    def handoff(
        self,
        *,
        to_consumer: str,
        notes: list[str] | None = None,
        blockers: list[str] | None = None,
        next_actions: list[str] | None = None,
    ) -> dict[str, Any]:
        checkpoint = self.checkpoint(progress="handoff", remaining=next_actions)
        record = {
            "schema_version": HANDOFF_SCHEMA,
            "identity": handoff_id(checkpoint["identity"], self.snapshot.get("consumer_id", ""), to_consumer),
            "checkpoint_id": checkpoint["identity"],
            "session_id": self.session_id,
            "from_consumer": self.snapshot.get("consumer_id"),
            "to_consumer": to_consumer,
            "to_consumer_kind": "agent",
            "notes": list(notes or []),
            "blockers": list(blockers or []),
            "next_actions": list(next_actions or []),
            "created_at": utcnow(),
        }
        self.store.save_handoff(self.session_id, record)
        self.snapshot.setdefault("handoffs", []).append(record["identity"])
        if self.snapshot.get("lifecycle") == "active":
            self.transition("handed_off", f"handoff to {to_consumer}")
        self._emit("handoff.created", self.snapshot.get("consumer_id", "unknown"),
                   {"handoff_id": record["identity"], "to_consumer": to_consumer})
        self._save()
        return record

    def accept_handoff(self, handoff_id: str, *, consumer_id: str,
                       consumer_kind: str = "agent") -> dict[str, Any]:
        """Accept a handoff as the intended recipient, revalidating everything.

        Rejects unknown handoffs, wrong recipients, and stale assumptions:
        workspace facts, claims, capability availability, and authority are
        recomputed and divergence is recorded before continuation.
        """
        record = self.store.load_handoff(self.session_id, handoff_id)
        if record is None:
            raise LifecycleError(f"unknown handoff {handoff_id} for session {self.session_id}")
        if record.get("session_id") != self.session_id:
            raise LifecycleError("handoff belongs to another session")
        if record.get("to_consumer") != consumer_id:
            raise LifecycleError(
                f"handoff {handoff_id} is addressed to {record.get('to_consumer')}, "
                f"not {consumer_id}"
            )
        previous = self.snapshot.get("consumer_id")
        self.snapshot["consumer_id"] = consumer_id
        self.snapshot["consumer_kind"] = consumer_kind
        self.snapshot["authority"] = dict(self.snapshot.get("authority", {}))
        self.snapshot["authority"]["subject"] = consumer_id
        divergence = self.revalidate()
        if self.snapshot.get("lifecycle") == "handed_off":
            self.transition("active", f"handoff {handoff_id} accepted by {consumer_id} (was {previous})")
        self._emit("session.resumed", consumer_id,
                   {"previous_consumer": previous, "handoff_id": handoff_id,
                    "revalidation": divergence})
        self._save()
        return {"previous_consumer": previous, "consumer_id": consumer_id,
                "handoff_id": handoff_id, "divergence": divergence}

    def _release_own_claims(self, reason: str) -> dict[str, Any]:
        """Release every live claim held by this session.

        Terminal sessions must not pin scopes until TTL expiry: a
        finished agent's claims would otherwise block the next agent
        for up to 24h. Release failures never block the terminal
        transition itself (TTL expiry remains the backstop); they are
        recorded on the completion payload instead.
        """
        released: list[str] = []
        error: str | None = None
        try:
            live = claims_module.active_claims(self.store.read_claims())
            repositories = sorted({
                str(record.get("repository", ""))
                for record in live.values()
                if record.get("session_id") == self.session_id
                and record.get("repository")
            })
            for repository in repositories:
                for record in claims_module.release(
                    self.store, session_id=self.session_id,
                    reason=reason, repository=repository,
                ):
                    released.append(str(record.get("claim_id", "")))
        except Exception as exc:  # noqa: BLE001 - recorded, never raised
            error = f"{type(exc).__name__}: {exc}"
        if released:
            self._refresh_holders()
            self._emit("lease.released", self.snapshot.get("consumer_id", "unknown"),
                       {"released": released, "reason": reason})
        return {"released": released, "error": error}

    def complete(self, *, outcome: str, summary: str = "") -> dict[str, Any]:
        if self.snapshot.get("lifecycle") not in ("active", "checkpointed", "waiting", "blocked"):
            raise LifecycleError("only a live session can complete")
        self.transition("completed", outcome)
        claims_report = self._release_own_claims(f"session completed: {outcome}")
        self.snapshot["completion"] = {"outcome": outcome, "summary": summary, "at": utcnow(),
                                       "claims_released": claims_report["released"],
                                       "claims_release_error": claims_report["error"]}
        self._emit("session.completed", self.snapshot.get("consumer_id", "unknown"),
                   {"outcome": outcome, "claims_released": claims_report["released"]})
        self._save()
        return self.snapshot["completion"]

    def fail(self, *, reason: str) -> dict[str, Any]:
        if self.snapshot.get("lifecycle") not in (
            "active", "resolving", "ready", "blocked", "waiting", "checkpointed"
        ):
            raise LifecycleError("session cannot fail from its current state")
        self.transition("failed", reason)
        claims_report = self._release_own_claims(f"session failed: {reason}")
        self._emit("session.failed", self.snapshot.get("consumer_id", "unknown"),
                   {"reason": reason, "claims_released": claims_report["released"]})
        self._save()
        return {"reason": reason, "claims_released": claims_report["released"],
                "claims_release_error": claims_report["error"]}

    # -- inspection ------------------------------------------------------------

    def context(self) -> dict[str, Any]:
        """Return the bounded first-use context for this session.

        The context intentionally projects only the facts a consumer needs to
        orient itself. ``inspect`` remains the detailed state/binding path.
        This method is read-only and does not advance cursors or append events.
        """
        intent = self.snapshot.get("intent") or {}
        authority = self.snapshot.get("authority") or {}
        protected = sorted(set(authority.get("protected_repositories", [])))
        writable = sorted(
            set(authority.get("writable", [])) - set(protected)
        )
        available: list[dict[str, Any]] = []
        unavailable = 0
        for binding in self.snapshot.get("bindings", []):
            availability = binding.get("availability") or {}
            if availability.get("status") == "available":
                available.append({
                    "capability": binding.get("capability"),
                    "provider": binding.get("provider"),
                    "contract_revision": binding.get("contract_revision"),
                })
            else:
                unavailable += 1

        max_capabilities = 20
        session_id = self.session_id
        return {
            "schema_version": "mncs.environment.entry-context/1",
            "session_id": session_id,
            "lifecycle": self.snapshot.get("lifecycle"),
            "environment_id": self.snapshot.get("environment_id"),
            "consumer_id": self.snapshot.get("consumer_id"),
            "configuration": self.snapshot.get("configuration", {}),
            "state_dir": str(self.state_dir.expanduser().resolve()),
            "persistence": "store" if hasattr(self.store, "backend") else "file",
            "readiness": readiness_module.summarize(self.snapshot),
            "service_operations": self.snapshot.get("service_operations", []),
            "doctor": self.doctor_summary(),
            "projects": [{"repository": repo.get("manifest_repository") or repo.get("name"),
                          "path": repo.get("path"), "branch": repo.get("branch"),
                          "dirty": repo.get("dirty"), "head": repo.get("head")}
                         for repo in self.snapshot.get("workspace", {}).get("repositories", [])][:20],
            "project_count": self.snapshot.get("workspace", {}).get("repository_count", 0),
            "toolchain": self.snapshot.get("toolchain"),
            "workspace_root": (self.snapshot.get("workspace") or {}).get("root"),
            "work_intent": {
                "identity": intent.get("identity"),
                "goal": intent.get("goal", ""),
            },
            "writable_repositories": writable,
            "protected_repositories": protected,
            "authority": {
                "subject": authority.get("subject"),
                "readable": list(authority.get("readable", [])),
                "writable": writable,
                "invocable": list(authority.get("invocable", [])),
                "protected_repositories": protected,
                "escalation_required": list(authority.get("escalation_required", [])),
            },
            "capabilities": {
                "available": available[:max_capabilities],
                "available_count": len(available),
                "unavailable_count": unavailable,
                "truncated": len(available) > max_capabilities,
            },
            "actions": self.actions(),
            "next_commands": [shlex.join(self.actions()[
                "reconcile" if readiness_module.summarize(self.snapshot)["blocking"] else "capabilities"
            ]["argv"])],
        }

    def actions(self) -> dict[str, Any]:
        """Executable argv addressing, preserving state/backend across cwd changes."""
        prefix = [sys.executable, str(Path(__file__).resolve().parents[1] / "scripts" / "mncs-env"),
                  "--state-dir", str(self.state_dir.expanduser().resolve()),
                  "--persistence", "store" if hasattr(self.store, "backend") else "file"]
        return {name: {"argv": [*prefix, name, self.session_id]} for name in
                ("health", "reconcile", "inspect", "capabilities")}

    def health(self, *, live: bool = False) -> dict[str, Any]:
        """Read-only inspection; no participation event or cursor advancement.

        By default a valid doctor epoch answers from the validated snapshot
        (labeled `observation: epoch`) without rescanning or rebinding, but
        declared services are always probed live: provider runtime state is
        observable only by probing. Any doubt falls through to the live
        path. `live=True` forces full live probes.
        """
        if not live:
            from . import doctor as doctor_module
            epoch = self.snapshot.get("doctor", {}).get("epoch") if isinstance(
                self.snapshot.get("doctor"), dict) else None
            if isinstance(epoch, dict):
                valid, _ = doctor_module.validate_epoch(self, epoch)
                if valid:
                    match, services = doctor_module.live_services_match(self)
                    if match:
                        fresh = [capabilities_module.probe_availability(binding)
                                 for binding in self.snapshot.get("bindings", [])]
                        summary = readiness_module.summarize(
                            self.snapshot, bindings=fresh, services=services, live=False)
                        summary["observation"] = "epoch"
                        summary["epoch"] = epoch.get("digest")
                        return {"session_id": self.session_id,
                                "environment_id": self.snapshot.get("environment_id"),
                                "readiness": summary,
                                "unavailable_capabilities": [
                                    {"capability": item["capability"], **item["availability"]}
                                    for item in fresh if item["availability"]["status"] != "available"],
                                "doctor": doctor_module.terse(self),
                                "actions": self.actions()}
        fresh = [capabilities_module.probe_availability(binding) for binding in self.snapshot.get("bindings", [])]
        services = readiness_module.probe_services(self, bindings=fresh)
        snapshot = dict(self.snapshot)
        snapshot["workspace"] = workspace_module.discover_workspace(
            self.snapshot.get("workspace", {}).get("root", "."),
            repositories=self.snapshot.get("workspace", {}).get("selection"))
        return {"session_id": self.session_id, "environment_id": self.snapshot.get("environment_id"),
                "readiness": readiness_module.summarize(snapshot, bindings=fresh, services=services, live=True),
                "unavailable_capabilities": [{"capability": item["capability"], **item["availability"]}
                                             for item in fresh if item["availability"]["status"] != "available"],
                "actions": self.actions()}

    def reconcile(self) -> dict[str, Any]:
        """Refresh discovery and delegate declared service recovery, then verify."""
        from . import doctor as doctor_module
        previous_bindings = [dict(binding) for binding in self.snapshot.get("bindings", [])]
        previous_services = [dict(item) for item in self.snapshot.get("service_observations", [])]
        revalidation = self.revalidate()
        result = readiness_module.reconcile_services(self, force_recovery=True)
        repairs = doctor_module.repair_delta(previous_bindings, self.snapshot.get("bindings", []),
                                              previous_services,
                                              self.snapshot.get("service_observations", []))
        report = doctor_module.record_epoch(
            self, repairs=repairs,
            reconciliations=[{"id": "reconcile:explicit", "class": "bounded_reconciliation",
                              "provider": "mncs-environment",
                              "detail": "explicit reconcile: full revalidation and service recovery",
                              "validated": True}],
            operations=result["operations"], revalidation=revalidation)
        return {**self.context(), "reconciliation": {"revalidation": revalidation, **result},
                "doctor": report}

    def status(self) -> dict[str, Any]:
        """Read-only alias for the compact session context."""
        return self.context()

    def doctor_summary(self) -> dict[str, Any] | None:
        """Last terse doctor state, or None before the first ambient pass."""
        from . import doctor as doctor_module
        if not isinstance(self.snapshot.get("doctor"), dict):
            return None
        return doctor_module.terse(self)

    def inspect(self) -> dict[str, Any]:
        log = self._log()
        unavailable = [
            {
                "provider": binding.get("provider"),
                "capability": binding.get("capability"),
                "reason": (binding.get("availability") or {}).get("reason"),
            }
            for binding in self.snapshot.get("bindings", [])
            if (binding.get("availability") or {}).get("status") != "available"
        ]
        return {
            "session_id": self.session_id,
            "lifecycle": self.snapshot.get("lifecycle"),
            "consumer_id": self.snapshot.get("consumer_id"),
            "consumer_kind": self.snapshot.get("consumer_kind"),
            "environment_id": self.snapshot.get("environment_id"),
            "configuration": self.snapshot.get("configuration", {}),
            "requirements": self.snapshot.get("requirements", {}),
            "readiness": readiness_module.summarize(self.snapshot),
            "intent": self.snapshot.get("intent"),
            "authority": self.snapshot.get("authority"),
            "rights": self.snapshot.get("rights", {}),
            "workspace": self.snapshot.get("workspace", {}),
            "selected_checkouts": self.snapshot.get("selected_checkouts", {}),
            "repo_facts": self.snapshot.get("repo_facts", {}),
            "toolchain": self.snapshot.get("toolchain"),
            "claim_holders": self.snapshot.get("claim_holders", {}),
            "unavailable_capabilities": unavailable,
            "bindings": [dict(binding) for binding in self.snapshot.get("bindings", [])],
            "binding_toolchains": [
                {
                    "provider": binding.get("provider"),
                    "capability": binding.get("capability"),
                    "address": binding.get("address"),
                    "toolchain_address": binding.get("toolchain_address"),
                    "toolchain_env": binding.get("toolchain_env"),
                }
                for binding in self.snapshot.get("bindings", [])
                if binding.get("toolchain_address")
            ],
            "event_count": len(log),
            "latest_events": log[-5:],
            "checkpoints": self.snapshot.get("checkpoints", []),
            "handoffs": self.snapshot.get("handoffs", []),
            "artifacts": self.snapshot.get("artifacts", []),
            "coherence": {key: value for key, value in self.snapshot.get("coherence", {}).items()
                          if key in ("schema_version", "stable", "store_cursor", "policy_identity",
                                     "policy_receipt", "last_trace", "deadlines", "result_refs",
                                     "repositories", "artifacts", "libraries")},
            "completion": self.snapshot.get("completion"),
            "lifecycle_history": self.snapshot.get("lifecycle_history", []),
        }
