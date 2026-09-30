"""Workspace discovery: repositories, revisions, worktrees, and work facts.

This module reports first-class workspace facts (dirty trees, unknown
branches, foreign worktrees, leases) and never mutates them. Protection
decisions consume these facts in `authority.py`; nothing here deletes,
resets, cleans, or checks out over anything.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

GIT_TIMEOUT_SECONDS = 3
MAX_PORCELAIN_LINES = 200
MAX_REPOS = 500
MAX_ROOT_DIRECTORIES = 64
WORKSPACE_SCAN_TIMEOUT_SECONDS = 10.0

MAIN_BRANCHES = {"main", "master"}


class WorkspaceResolutionError(ValueError):
    """Raised when a workspace cannot be resolved within safe bounds."""

    def __init__(self, message: str, *, diagnostics: dict[str, Any] | None = None):
        super().__init__(message)
        self.diagnostics = diagnostics or {}


def _git(
    repo: Path,
    *args: str,
    deadline: float | None = None,
) -> subprocess.CompletedProcess[str] | None:
    timeout = GIT_TIMEOUT_SECONDS
    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic())
        if timeout <= 0:
            return None
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _manifest(repo: Path) -> dict[str, Any] | None:
    path = repo / ".mncs" / "project.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _porcelain(
    repo: Path,
    *,
    deadline: float | None = None,
) -> tuple[list[str], bool]:
    completed = _git(repo, "status", "--porcelain=v1", "-uall", deadline=deadline)
    if completed is None or completed.returncode != 0:
        return [], False
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    # Managed worktree infrastructure is control-plane bookkeeping, like
    # .git itself: it must not mark the main checkout dirty. Worktrees
    # are observed as their own records.
    lines = [line for line in lines
             if line[3:] != MANAGED_WORKTREES_DIR and
             not line[3:].startswith(MANAGED_WORKTREES_DIR + "/")]
    truncated = len(lines) > MAX_PORCELAIN_LINES
    return lines[:MAX_PORCELAIN_LINES], truncated


def _worktrees(
    repo: Path,
    *,
    deadline: float | None = None,
) -> list[dict[str, str]]:
    completed = _git(repo, "worktree", "list", "--porcelain", deadline=deadline)
    if completed is None or completed.returncode != 0:
        return []
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        if line.startswith("worktree "):
            if current:
                entries.append(current)
            current = {"path": line[len("worktree "):]}
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD "):]
        elif line.startswith("branch "):
            current["branch"] = line[len("branch "):]
        elif line == "bare":
            current["bare"] = "true"
        elif line == "detached":
            current["detached"] = "true"
    if current:
        entries.append(current)
    return entries


@dataclass
class RepoState:
    """Observed facts about one repository checkout."""

    name: str
    path: str
    manifest_repository: str | None
    manifest_revision: int | None
    branch: str | None
    head: str | None
    dirty: bool
    dirty_files: list[str] = field(default_factory=list)
    dirty_truncated: bool = False
    untracked_count: int = 0
    worktrees: list[dict[str, str]] = field(default_factory=list)
    worktree_of: str | None = None
    git_error: str | None = None

    def record(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "worktree_of": self.worktree_of,
            "manifest_repository": self.manifest_repository,
            "manifest_revision": self.manifest_revision,
            "branch": self.branch,
            "head": self.head,
            "dirty": self.dirty,
            "dirty_files": self.dirty_files,
            "dirty_truncated": self.dirty_truncated,
            "untracked_count": self.untracked_count,
            "worktrees": self.worktrees,
            "git_error": self.git_error,
        }


def inspect_repo(path: Path, *, deadline: float | None = None) -> RepoState | None:
    """Inspect one directory; return None when it is not a git checkout."""
    if not (path / ".git").exists():
        return None
    manifest = _manifest(path)
    head_proc = _git(path, "rev-parse", "HEAD", deadline=deadline)
    head = head_proc.stdout.strip() if head_proc and head_proc.returncode == 0 else None
    branch_proc = _git(path, "branch", "--show-current", deadline=deadline)
    branch = branch_proc.stdout.strip() if branch_proc and branch_proc.returncode == 0 else None
    if branch_proc is None:
        git_error: str | None = "git unavailable or timed out"
    elif head is None:
        git_error = "head unresolvable"
    else:
        git_error = None
    lines, truncated = _porcelain(path, deadline=deadline)
    untracked = sum(1 for line in lines if line.startswith("??"))
    return RepoState(
        name=path.name,
        path=str(path),
        manifest_repository=manifest.get("repository") if manifest else None,
        manifest_revision=manifest.get("revision") if manifest else None,
        branch=branch or None,
        head=head,
        dirty=bool(lines),
        dirty_files=lines,
        dirty_truncated=truncated,
        untracked_count=untracked,
        worktrees=_worktrees(path, deadline=deadline),
        git_error=git_error,
    )


MANAGED_WORKTREES_DIR = ".worktrees"


def _candidate_directories(base: Path) -> list[Path]:
    try:
        return sorted(
            path for path in base.iterdir()
            if path.is_dir() and not path.is_symlink()
        )
    except OSError as error:
        raise WorkspaceResolutionError(
            f"workspace root is unreadable: {base} ({error})",
            diagnostics={"code": "workspace-unreadable", "root": str(base)},
        ) from error


def _campaign_definition(definition: dict[str, Any] | None) -> bool:
    if not isinstance(definition, dict):
        return False
    scope = definition.get("workspace_scope")
    return bool(
        isinstance(definition.get("workspace_provider"), dict)
        or isinstance(definition.get("managed_checkouts"), list)
        or (isinstance(scope, dict) and scope.get("kind") == "campaign")
        or definition.get("name") == "mncs-compiler-campaign"
    )


def _root_limit(definition: dict[str, Any] | None) -> int:
    default = 32 if _campaign_definition(definition) else MAX_ROOT_DIRECTORIES
    scope = definition.get("workspace_scope") if isinstance(definition, dict) else None
    requested = scope.get("max_directories") if isinstance(scope, dict) else None
    if isinstance(requested, int) and requested > 0:
        default = requested
    return max(1, min(default, MAX_ROOT_DIRECTORIES))


def repository_selection(definition: dict[str, Any] | None) -> list[str] | None:
    """Explicit immediate checkouts; selection is intent, not a new registry."""
    scope = definition.get("workspace_scope") if isinstance(definition, dict) else None
    names = scope.get("repositories") if isinstance(scope, dict) else None
    if names is None:
        return None
    if (_campaign_definition(definition) or not isinstance(names, list) or not names
            or len(names) > MAX_ROOT_DIRECTORIES or len(set(str(name) for name in names)) != len(names)
            or any(not isinstance(name, str) or not name or name in (".", "..")
                   or Path(name).name != name or "/" in name or "\\" in name for name in names)):
        raise WorkspaceResolutionError("workspace_scope.repositories must select unique immediate checkouts outside campaign provisioning",
                                       diagnostics={"code": "workspace-selection-invalid"})
    return sorted(names)


def _selected_directories(base: Path, repositories: list[str] | None) -> list[Path]:
    if repositories is None:
        return [base] if (base / ".git").exists() else _candidate_directories(base)
    candidates = [base / name for name in repositories]
    for candidate in candidates:
        if candidate.is_symlink() or not candidate.is_dir() or not (candidate / ".git").exists():
            raise WorkspaceResolutionError(f"selected checkout is missing, a symbolic link, or not a Git repository: {candidate}",
                                           diagnostics={"code": "workspace-selected-checkout-unavailable", "path": str(candidate),
                                                        "next": "restore the selected checkout or update workspace_scope.repositories"})
    return candidates


def validate_workspace_root(
    root: Path | str,
    *,
    definition: dict[str, Any] | None = None,
) -> Path:
    """Validate a root before any Git or provider discovery begins.

    A workspace is an explicit boundary, not a family-wide search hint. The
    cheap directory count prevents an accidental Projects-level root from
    launching dozens of Git status/worktree probes.
    """
    raw = Path(root).expanduser()
    if raw.is_symlink():
        raise WorkspaceResolutionError(
            f"workspace root must not be a symbolic link: {raw}",
            diagnostics={"code": "workspace-symlink", "root": str(raw)},
        )
    base = raw.resolve()
    if not base.exists():
        raise WorkspaceResolutionError(
            f"workspace root does not exist: {base}; pass --workspace to an existing campaign root",
            diagnostics={"code": "workspace-missing", "root": str(base)},
        )
    if not base.is_dir():
        raise WorkspaceResolutionError(
            f"workspace root is not a directory: {base}; pass --workspace to a campaign root",
            diagnostics={"code": "workspace-not-directory", "root": str(base)},
        )

    candidates = _selected_directories(base, repository_selection(definition))
    limit = _root_limit(definition)
    if len(candidates) > limit:
        campaign_note = (
            " Compiler campaigns must use their isolated campaign root; do not use the "
            "Projects directory."
            if _campaign_definition(definition) else ""
        )
        raise WorkspaceResolutionError(
            f"workspace root is too broad: {base} contains {len(candidates)} immediate "
            f"directories (safe limit {limit}). Pass --workspace to a campaign-scoped "
            f"root containing only the selected repositories.{campaign_note}",
            diagnostics={
                "code": "workspace-too-broad",
                "root": str(base),
                "candidate_directories": len(candidates),
                "max_candidate_directories": limit,
                "inspected_directories": 0,
                "next": "pass --workspace to the isolated campaign root",
            },
        )

    provider = definition.get("workspace_provider") if isinstance(definition, dict) else None
    if isinstance(provider, dict):
        repository = str(provider.get("repository", ""))
        checkout = str(provider.get("checkout", ""))
        relative = Path(checkout)
        if not repository or not checkout or relative.is_absolute() or ".." in relative.parts:
            raise WorkspaceResolutionError(
                "workspace provider checkout must be a repository-relative path",
                diagnostics={"code": "workspace-provider-invalid", "root": str(base)},
            )
        checkout_path = base / repository / relative
        if checkout_path.is_symlink() or not checkout_path.is_dir():
            raise WorkspaceResolutionError(
                f"campaign root does not contain the declared workspace provider checkout: "
                f"{checkout_path}; pass --workspace to the campaign root that contains it",
                diagnostics={
                    "code": "workspace-provider-missing",
                    "root": str(base),
                    "provider_repository": repository,
                    "provider_checkout": str(relative),
                },
            )

    return base


def discover_workspace(
    root: Path | str,
    *,
    timeout_seconds: float = WORKSPACE_SCAN_TIMEOUT_SECONDS,
    repositories: list[str] | None = None,
) -> dict[str, Any]:
    """Discover repository checkouts directly under root (non-recursive).

    Managed worktrees at ``<repo>/.worktrees/<name>`` surface as
    first-class records named ``<repo>@<name>`` with ``worktree_of``
    set, so sessions can see and claim them without a family rescan.
    Worktrees stay nested inside their project so sandbox project scope
    remains writable.
    """
    started = time.monotonic()
    base = Path(root).expanduser().resolve()
    try:
        candidates = _selected_directories(base, repositories)
    except WorkspaceResolutionError as error:
        return {
            "root": str(base),
            "error": str(error),
            "repositories": [],
            "repository_count": 0,
            "non_repository_count": 0,
            "scan": {
                "status": "invalid",
                "candidate_directories": 0,
                "inspected_directories": 0,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
                "message": str(error),
            },
        }
    except OSError as error:
        return {"root": str(base), "error": str(error), "repositories": []}

    deadline = started + max(0.1, float(timeout_seconds))
    repos: list[dict[str, Any]] = []
    skipped = 0
    inspected = 0
    status = "complete"
    reason = "all immediate directories inspected"
    for candidate in candidates[:MAX_REPOS]:
        if time.monotonic() >= deadline:
            status = "timed_out"
            reason = "workspace scan exceeded its bounded time budget"
            break
        state = inspect_repo(candidate, deadline=deadline)
        inspected += 1
        if state is None:
            if candidate.name != MANAGED_WORKTREES_DIR:
                skipped += 1
            continue
        repos.append(state.record())
        for record in _managed_worktrees(candidate, deadline=deadline):
            repos.append(record)
        if time.monotonic() >= deadline and inspected < len(candidates):
            status = "timed_out"
            reason = "workspace scan exceeded its bounded time budget"
            break
    if inspected < len(candidates) and status == "complete":
        status = "bounded"
        reason = f"workspace scan capped at {MAX_REPOS} directories"
    elapsed_ms = round((time.monotonic() - started) * 1000, 1)
    scan = {
        "status": status,
        "candidate_directories": len(candidates),
        "inspected_directories": inspected,
        "elapsed_ms": elapsed_ms,
        "budget_seconds": max(0.1, float(timeout_seconds)),
        "message": (
            f"inspected {inspected}/{len(candidates)} immediate directories in "
            f"{elapsed_ms / 1000:.2f}s: {reason}"
        ),
    }
    return {
        "schema_version": "mncs.environment.workspace/1",
        "root": str(base),
        "selection": repositories,
        "repository_count": len(repos),
        "non_repository_count": skipped,
        "repositories": repos,
        "scan": scan,
    }


def _managed_worktrees(
    repo: Path,
    *,
    deadline: float | None = None,
) -> list[dict[str, Any]]:
    """First-class records for worktrees nested in one repository."""
    out: list[dict[str, Any]] = []
    directory = repo / MANAGED_WORKTREES_DIR
    try:
        checkouts = sorted(path for path in directory.iterdir()
                           if path.is_dir() and not path.is_symlink())
    except OSError:
        return out
    for checkout in checkouts[:MAX_REPOS]:
        if deadline is not None and time.monotonic() >= deadline:
            break
        state = inspect_repo(checkout, deadline=deadline)
        if state is None:
            continue
        state.worktree_of = repo.name
        record = state.record()
        record["name"] = f"{repo.name}@{checkout.name}"
        out.append(record)
    return out


def foreign_work_signals(repo: dict[str, Any]) -> list[dict[str, str]]:
    """Heuristics marking work another consumer may own (reasons, not verdicts)."""
    signals: list[dict[str, str]] = []
    branch = repo.get("branch")
    if branch and branch not in MAIN_BRANCHES:
        signals.append({"kind": "foreign-branch", "detail": f"on branch {branch}"})
    if repo.get("dirty"):
        signals.append(
            {"kind": "dirty-tree", "detail": f"{len(repo.get('dirty_files', []))} changed paths"}
        )
    for worktree in repo.get("worktrees", [])[1:]:
        signals.append(
            {
                "kind": "linked-worktree",
                "detail": f"{worktree.get('path')} @ {worktree.get('branch', worktree.get('head', '?'))}",
            }
        )
    if repo.get("git_error"):
        signals.append({"kind": "git-unreadable", "detail": str(repo.get("git_error"))})
    return signals
