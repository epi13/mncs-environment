"""Workspace discovery: repositories, revisions, worktrees, and work facts.

This module reports first-class workspace facts (dirty trees, unknown
branches, foreign worktrees, leases) and never mutates them. Protection
decisions consume these facts in `authority.py`; nothing here deletes,
resets, cleans, or checks out over anything.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .identity import digest_hex

GIT_TIMEOUT_SECONDS = 3
MAX_PORCELAIN_LINES = 200
MAX_REPOS = 500
MAX_ROOT_DIRECTORIES = 64
WORKSPACE_SCAN_TIMEOUT_SECONDS = 10.0

# One git resolution per process: every spawn otherwise pays a full PATH
# search of failed exec probes before reaching the real binary.
_GIT_BINARY: str | None = None


def _git_binary() -> str:
    global _GIT_BINARY
    if _GIT_BINARY is None:
        _GIT_BINARY = shutil.which("git") or "git"
    return _GIT_BINARY


def git_binary() -> str:
    """Process-wide resolved git executable (no per-spawn PATH search)."""
    return _git_binary()


# Per-process inspection cache. Entry consults the same checkouts from
# many passes; the facts are re-observed after every in-process mutation
# (see invalidate_repo_facts), so sharing them cannot hide our own writes.
# Mutation boundaries and change detectors must bypass the cache with
# _refresh=True: cached git flags are never a mutation lease.
_inspect_cache: dict[str, RepoState | None] = {}
_quick_cache: dict[str, dict[str, Any] | None] = {}


def invalidate_repo_facts(path: Path | str) -> None:
    """Drop cached facts for one checkout after mutating it."""
    try:
        key = str(Path(path).resolve())
    except OSError:
        return
    _inspect_cache.pop(key, None)
    _quick_cache.pop(key, None)

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
            [_git_binary(), "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
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
    lines = _filtered_porcelain_names(lines)
    truncated = len(lines) > MAX_PORCELAIN_LINES
    return lines[:MAX_PORCELAIN_LINES], truncated


def _worktrees(
    repo: Path,
    *,
    deadline: float | None = None,
) -> list[dict[str, str]]:
    completed = _git(repo, "worktree", "list", "--porcelain", "-z", deadline=deadline)
    if completed is None or completed.returncode != 0:
        completed = _git(repo, "worktree", "list", "--porcelain", deadline=deadline)
        if completed is None or completed.returncode != 0:
            return []
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    fields = (completed.stdout.split("\x00") if "\x00" in completed.stdout
              else completed.stdout.splitlines())
    for field in fields:
        if not field:
            if current:
                entries.append(current)
                current = {}
        elif field.startswith("worktree "):
            if current:
                entries.append(current)
            current = {"path": field.removeprefix("worktree ")}
        elif field.startswith("HEAD "):
            current["head"] = field[len("HEAD "):]
        elif field.startswith("branch "):
            current["branch"] = field[len("branch "):]
        elif field == "bare":
            current["bare"] = "true"
        elif field == "detached":
            current["detached"] = "true"
        elif field.startswith("locked"):
            current["locked"] = field[len("locked"):].strip() or "true"
        elif field.startswith("prunable"):
            current["prunable"] = field[len("prunable"):].strip() or "true"
    if current:
        entries.append(current)
    return entries


def _registered_worktree_alias_matches(
    checkout: Path,
    repository_root: Path,
    common_directory: Path,
    git_directory: Path,
    registered_path: str,
) -> bool:
    """Match a Git worktree through a verified mount-namespace path alias.

    Control can expose the selected checkout under a scoped ``/workspace``
    path while the linked-worktree registry retains the host checkout path.
    In that case, lexical path equality is unavailable. Git's per-worktree
    admin directory and reciprocal ``gitdir`` record still identify one
    registered checkout; the path relative to its repository root must also
    agree on both sides of the alias.
    """
    git_file = checkout / ".git"
    if git_file.is_symlink() or not git_file.is_file():
        return False
    try:
        git_file_lines = git_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    if len(git_file_lines) != 1:
        return False
    prefix, separator, raw_git_directory = git_file_lines[0].partition(":")
    raw_git_directory = raw_git_directory.strip()
    if prefix != "gitdir" or not separator or not raw_git_directory:
        return False

    host_git_directory = Path(raw_git_directory)
    if not host_git_directory.is_absolute():
        return False
    normalized_host_git_directory = Path(os.path.abspath(host_git_directory))
    if normalized_host_git_directory != host_git_directory:
        return False
    if (
        host_git_directory.parent.name != "worktrees"
        or host_git_directory.parent.parent.name != ".git"
    ):
        return False
    host_repository_root = host_git_directory.parents[2]
    if host_repository_root.name != repository_root.name:
        return False

    worktrees_root = common_directory / "worktrees"
    try:
        relative_admin = git_directory.relative_to(worktrees_root)
        absolute_registered_path = Path(os.path.abspath(registered_path))
        checkout_relative = checkout.relative_to(repository_root)
        registered_relative = absolute_registered_path.relative_to(host_repository_root)
    except (OSError, ValueError):
        return False
    if (
        len(relative_admin.parts) != 1
        or relative_admin.name != host_git_directory.name
        or checkout_relative != registered_relative
        or str(absolute_registered_path) != registered_path
    ):
        return False

    admin_directory = worktrees_root / relative_admin
    admin_backlink = admin_directory / "gitdir"
    if (
        worktrees_root.is_symlink()
        or admin_directory.is_symlink()
        or admin_backlink.is_symlink()
        or not admin_backlink.is_file()
    ):
        return False
    try:
        raw_backlink = admin_backlink.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    backlink = Path(raw_backlink)
    return (
        backlink.is_absolute()
        and Path(os.path.abspath(backlink)) == backlink
        and backlink == absolute_registered_path / ".git"
    )


def verify_registered_checkout(
    checkout: str | Path,
    *,
    repository: str | None = None,
    workspace_root: str | Path | None = None,
    expected_repository_root: str | Path | None = None,
    expected_branch: str | None = None,
    expected_head: str | None = None,
    allow_repository_root_ancestor: bool = False,
) -> dict[str, str]:
    """Verify one canonical, registered Git checkout inside an authorized workspace.

    Git's common directory and worktree registry are the source of checkout
    identity. Directory layout is intentionally not part of the identity, so
    nested registered worktrees work while symlink aliases, unrelated clones,
    and forged MNCS manifests remain rejected. When explicitly enabled, the
    authorized workspace may itself be nested inside the registered
    repository; the checkout must still remain inside that workspace.
    """

    def reject(
        message: str,
        code: str = "working-checkout-unavailable",
        **details: Any,
    ) -> None:
        raise WorkspaceResolutionError(
            message,
            diagnostics={
                "code": code,
                "path": str(checkout),
                **({"repository": repository} if repository else {}),
                **details,
            },
        )

    raw_checkout = Path(checkout).expanduser()
    try:
        absolute_checkout = Path(os.path.abspath(raw_checkout))
        resolved_checkout = absolute_checkout.resolve(strict=True)
    except OSError as error:
        reject(f"Git checkout is unavailable: {error}")
    if absolute_checkout != resolved_checkout:
        reject("Git checkout path traverses a symbolic link")
    if not resolved_checkout.is_dir():
        reject("Git checkout is not a directory")

    top_level = _git(resolved_checkout, "rev-parse", "--show-toplevel")
    common = _git(
        resolved_checkout, "rev-parse", "--path-format=absolute", "--git-common-dir"
    )
    git_directory = _git(
        resolved_checkout, "rev-parse", "--path-format=absolute", "--absolute-git-dir"
    )
    head = _git(resolved_checkout, "rev-parse", "HEAD")
    branch = _git(resolved_checkout, "symbolic-ref", "--quiet", "--short", "HEAD")
    if (
        top_level is None
        or top_level.returncode != 0
        or common is None
        or common.returncode != 0
        or git_directory is None
        or git_directory.returncode != 0
        or head is None
        or head.returncode != 0
        or branch is None
        or branch.returncode not in (0, 1)
    ):
        reject(
            "Git could not establish the checkout, branch, and common-directory identities"
        )
    try:
        observed_top_level = Path(top_level.stdout.strip()).resolve(strict=True)
        raw_common = Path(os.path.abspath(common.stdout.strip()))
        common_directory = raw_common.resolve(strict=True)
        raw_git_directory = Path(os.path.abspath(git_directory.stdout.strip()))
        absolute_git_directory = raw_git_directory.resolve(strict=True)
    except OSError as error:
        reject(f"Git checkout identity could not be resolved: {error}")
    if observed_top_level != resolved_checkout:
        reject("Git top-level path does not match the selected checkout")
    if (
        raw_common != common_directory
        or common_directory.name != ".git"
        or not common_directory.is_dir()
    ):
        reject("Git common directory is not a canonical repository directory")
    if raw_git_directory != absolute_git_directory:
        reject("Git administrative directory traverses a symbolic link")

    repository_root = common_directory.parent
    try:
        if workspace_root is None:
            raw_workspace = repository_root.parent
        else:
            raw_workspace = Path(workspace_root).expanduser()
        absolute_workspace = Path(os.path.abspath(raw_workspace))
        resolved_workspace = absolute_workspace.resolve(strict=True)
    except OSError as error:
        reject(f"authorized workspace root is unavailable: {error}")
    if absolute_workspace != resolved_workspace or not resolved_workspace.is_dir():
        reject("authorized workspace root is not canonical")
    repository_root_ancestor = False
    if repository_root.parent != resolved_workspace:
        if allow_repository_root_ancestor:
            try:
                resolved_workspace.relative_to(repository_root)
                repository_root_ancestor = True
            except ValueError:
                reject(
                    "Git common repository is neither the authorized workspace repository "
                    "nor an ancestor of that workspace"
                )
        else:
            reject("Git common repository is not an immediate repository in the authorized workspace")
    if expected_repository_root is not None:
        try:
            raw_expected_root = Path(expected_repository_root).expanduser()
            absolute_expected_root = Path(os.path.abspath(raw_expected_root))
            resolved_expected_root = absolute_expected_root.resolve(strict=True)
        except OSError as error:
            reject(f"expected repository root is unavailable: {error}")
        if absolute_expected_root != resolved_expected_root or resolved_expected_root != repository_root:
            reject("Git common directory does not match the selected repository root")

    for manifest_root in (repository_root, resolved_checkout):
        manifest_directory = manifest_root / ".mncs"
        manifest_path = manifest_directory / "project.json"
        if manifest_directory.is_symlink() or manifest_path.is_symlink():
            reject("MNCS repository manifest identity cannot traverse a symbolic link")

    actual_repository = repository
    root_manifest = _manifest(repository_root)
    checkout_manifest = _manifest(resolved_checkout)
    if actual_repository is None:
        actual_repository = (
            root_manifest.get("repository") if isinstance(root_manifest, dict) else None
        )
    if (not isinstance(actual_repository, str) or not actual_repository
            or repository_root.name != actual_repository
            or not isinstance(root_manifest, dict)
            or not isinstance(checkout_manifest, dict)
            or root_manifest.get("repository") != actual_repository
            or checkout_manifest.get("repository") != actual_repository):
        reject("Git checkout and repository manifest identities do not match")
    if (not repository_root_ancestor
            and resolved_workspace / actual_repository != repository_root):
        reject("repository manifest identity does not name the selected workspace checkout")

    try:
        relative_checkout = resolved_checkout.relative_to(resolved_workspace)
    except ValueError:
        reject("Git checkout escapes the authorized workspace")
    cursor = resolved_workspace
    for part in relative_checkout.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            reject("Git checkout path traverses a symbolic link")

    registered = []
    worktree_rows = _worktrees(repository_root)
    for row in worktree_rows:
        raw_registered_path = row.get("path")
        if not raw_registered_path:
            continue
        try:
            absolute_registered_path = Path(os.path.abspath(raw_registered_path))
            resolved_registered_path = absolute_registered_path.resolve(strict=False)
        except OSError:
            continue
        direct_match = (
            absolute_registered_path == resolved_registered_path
            and resolved_registered_path == resolved_checkout
        )
        alias_match = (
            resolved_registered_path == resolved_checkout
            and _registered_worktree_alias_matches(
                resolved_checkout,
                repository_root,
                common_directory,
                absolute_git_directory,
                raw_registered_path,
            )
        )
        if direct_match or alias_match:
            registered.append(row)
    if len(registered) != 1 or "prunable" in registered[0]:
        candidates = [
            {
                "path": row.get("path"),
                "branch": row.get("branch"),
                "head": row.get("head"),
                "prunable": row.get("prunable"),
            }
            for row in registered
        ]
        reject(
            "Git checkout is not one unique registered, available worktree",
            registered_worktree_candidates=candidates,
            matching_worktree_count=len(registered),
        )
    registered_head = registered[0].get("head")
    registered_branch = registered[0].get("branch")
    if registered_branch and registered_branch.startswith("refs/heads/"):
        registered_branch = registered_branch[len("refs/heads/"):]
    observed_branch = branch.stdout.strip() if branch.returncode == 0 else None
    observed_head = head.stdout.strip()
    if (registered_head != observed_head
            or registered_branch != observed_branch
            or (expected_branch is not None and expected_branch != (observed_branch or ""))
            or (expected_head is not None and expected_head != observed_head)):
        reject("registered worktree branch or revision does not match the observed checkout")

    identity = repository_checkout_identity(
        resolved_workspace, actual_repository, resolved_checkout
    )
    return {
        "repository": actual_repository,
        "path": str(resolved_checkout),
        "workspace_root": str(resolved_workspace),
        "repository_root": str(repository_root),
        "git_common_directory": str(common_directory),
        "branch": observed_branch or "",
        "head": observed_head,
        "manifest_identity": digest_hex({
            "repository": actual_repository,
            "schema_version": root_manifest.get("schema_version"),
        }),
        **identity,
    }


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


def inspect_repo(path: Path, *, deadline: float | None = None,
                 _refresh: bool = False) -> RepoState | None:
    """Inspect one directory; return None when it is not a git checkout."""
    try:
        key = str(Path(path).resolve())
    except OSError:
        return None
    if not _refresh and key in _inspect_cache:
        return _inspect_cache[key]
    if not (path / ".git").exists():
        _inspect_cache[key] = None
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
    state = RepoState(
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
    # Transient git failures (timeouts) are never cached: a loaded moment
    # must not become a permanent "unavailable" verdict for the process.
    if git_error is None:
        _inspect_cache[key] = state
    return state


def repository_checkout_identity(
    workspace_root: str | Path,
    repository: str,
    checkout: str | Path,
) -> dict[str, str]:
    """Return stable identity facts for one checkout within a workspace.

    The relative checkout and Git common-directory identities distinguish
    linked worktrees while the remote digest helps retain identity when the
    workspace is observed through a different mount namespace.
    """
    root = Path(workspace_root).resolve()
    resolved = Path(checkout).resolve(strict=True)
    observed_path = resolved.relative_to(root).as_posix()
    common = _git(
        resolved, "rev-parse", "--path-format=absolute", "--git-common-dir"
    )
    remote = _git(resolved, "config", "--get", "remote.origin.url")
    common_material: dict[str, Any] = {"repository": repository}
    if common is not None and common.returncode == 0:
        common_path = Path(common.stdout.strip()).resolve()
        try:
            common_relative = common_path.relative_to(root).as_posix()
        except ValueError:
            common_relative = None
        if common_relative is not None:
            common_material["common_directory"] = common_relative
    if remote is not None and remote.returncode == 0 and remote.stdout.strip():
        common_material["remote_identity"] = digest_hex(remote.stdout.strip())
    common_identity = "git-common:" + digest_hex(common_material)
    checkout_identity = "checkout:" + digest_hex({
        "repository": repository,
        "path": observed_path,
        "git_common_directory_identity": common_identity,
    })
    return {
        "checkout_identity": checkout_identity,
        "git_common_directory_identity": common_identity,
        "observed_path": observed_path,
    }


MANAGED_WORKTREES_DIR = ".worktrees"


def _filtered_porcelain_names(lines: list[str]) -> list[str]:
    """Drop managed-worktree control-plane paths from porcelain lines."""
    return [line for line in lines
            if line[3:] != MANAGED_WORKTREES_DIR and
            not line[3:].startswith(MANAGED_WORKTREES_DIR + "/")]


def quick_repo_facts(path: Path, *, _refresh: bool = False) -> dict[str, Any] | None:
    """Cheap exact change-detection facts for one checkout.

    Three bounded git calls (head+branch, tracked status, untracked list)
    capture exactly the facts a full scan would compare for staleness:
    revision, branch, and the complete dirty/untracked content digests.
    Control-plane `.worktrees/` paths are excluded exactly like the full
    scan. Returns None when the checkout is not git-readable; callers must
    treat None as changed (invalidate), never as clean.
    """
    import hashlib

    repo = Path(path)
    try:
        key = str(repo.resolve())
    except OSError:
        return None
    if not _refresh and key in _quick_cache:
        return _quick_cache[key]
    if not repo.is_dir() or not (repo / ".git").exists():
        return None
    identity = _git(repo, "rev-parse", "HEAD", "--abbrev-ref", "HEAD")
    if identity is None or identity.returncode != 0:
        return None
    lines = identity.stdout.splitlines()
    if len(lines) < 2 or not lines[0].strip():
        return None
    branch = lines[1].strip()
    if branch == "HEAD":
        # Detached head: match `branch --show-current` (empty) semantics.
        branch = ""
    tracked = _git(repo, "status", "--porcelain=v1", "--untracked-files=no")
    if tracked is None or tracked.returncode != 0:
        return None
    others = _git(repo, "ls-files", "--others", "--exclude-standard")
    if others is None or others.returncode != 0:
        return None
    tracked_lines = _filtered_porcelain_names(
        [line for line in tracked.stdout.splitlines() if line.strip()])
    untracked_lines = sorted(
        line for line in (entry.strip() for entry in others.stdout.splitlines())
        if line and line != MANAGED_WORKTREES_DIR
        and not line.startswith(MANAGED_WORKTREES_DIR + "/"))
    tracked_digest = hashlib.sha256("\n".join(tracked_lines).encode()).hexdigest()
    untracked_digest = hashlib.sha256("\n".join(untracked_lines).encode()).hexdigest()
    facts = {"head": lines[0].strip(), "branch": branch or None,
             "dirty": bool(tracked_lines) or bool(untracked_lines),
             "tracked_digest": tracked_digest, "untracked_digest": untracked_digest}
    _quick_cache[key] = facts
    return facts


def manifest_content_digest(path: Path) -> dict[str, str | None]:
    """Content hashes for every declaration file discovery reads in a repo."""
    import hashlib

    repo = Path(path)
    files = [".mncs/project.json", "family-semantic-contracts-v1.json"]
    inventory = _obligation_inventory_name(repo)
    if inventory is not None:
        files.append(inventory)
    digests: dict[str, str | None] = {}
    for name in files:
        try:
            raw = (repo / name).read_bytes()
        except OSError:
            digests[name] = None
        else:
            digests[name] = hashlib.sha256(raw).hexdigest()
    return digests


def _obligation_inventory_name(repo: Path) -> str | None:
    """Resolve the verification obligation inventory path (mirrors discovery)."""
    try:
        manifest = json.loads((repo / ".mncs" / "project.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(manifest, dict):
        return None
    verification = manifest.get("verification")
    name = verification.get("obligation_inventory") if isinstance(verification, dict) else None
    if not isinstance(name, str) or not name:
        return None
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    return name


def managed_worktree_names(repo: Path) -> list[str] | None:
    """Sorted managed-checkout names, or None when the listing is unreadable."""
    try:
        return sorted(path.name for path in (Path(repo) / MANAGED_WORKTREES_DIR).iterdir()
                      if path.is_dir() and not path.is_symlink())
    except OSError:
        return None


def top_level_checkout_names(base: Path) -> list[str] | None:
    """Immediate child directories containing `.git` (no git calls)."""
    try:
        return sorted(path.name for path in Path(base).iterdir()
                      if path.is_dir() and not path.is_symlink()
                      and (path / ".git").exists())
    except OSError:
        return None


def _candidate_directories(base: Path) -> list[Path]:
    try:
        return sorted(
            path for path in base.iterdir()
            if path.is_dir() and not path.is_symlink()
            # A workspace-level worktree container is infrastructure, not a
            # repository candidate. Discovery already skips it by name; keep
            # it out of the candidate budget as well so it cannot make an
            # otherwise bounded campaign root fail validation.
            and path.name != MANAGED_WORKTREES_DIR
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
