"""Workspace discovery: repositories, revisions, worktrees, and work facts.

This module reports first-class workspace facts (dirty trees, unknown
branches, foreign worktrees, leases) and never mutates them. Protection
decisions consume these facts in `authority.py`; nothing here deletes,
resets, cleans, or checks out over anything.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

GIT_TIMEOUT_SECONDS = 15
MAX_PORCELAIN_LINES = 200
MAX_REPOS = 500

MAIN_BRANCHES = {"main", "master"}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
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


def _porcelain(repo: Path) -> tuple[list[str], bool]:
    completed = _git(repo, "status", "--porcelain=v1", "-uall")
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


def _worktrees(repo: Path) -> list[dict[str, str]]:
    completed = _git(repo, "worktree", "list", "--porcelain")
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


def inspect_repo(path: Path) -> RepoState | None:
    """Inspect one directory; return None when it is not a git checkout."""
    if not (path / ".git").exists():
        return None
    manifest = _manifest(path)
    head_proc = _git(path, "rev-parse", "HEAD")
    head = head_proc.stdout.strip() if head_proc and head_proc.returncode == 0 else None
    branch_proc = _git(path, "branch", "--show-current")
    branch = branch_proc.stdout.strip() if branch_proc and branch_proc.returncode == 0 else None
    if branch_proc is None:
        git_error: str | None = "git unavailable or timed out"
    elif head is None:
        git_error = "head unresolvable"
    else:
        git_error = None
    lines, truncated = _porcelain(path)
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
        worktrees=_worktrees(path),
        git_error=git_error,
    )


MANAGED_WORKTREES_DIR = ".worktrees"


def discover_workspace(root: Path | str) -> dict[str, Any]:
    """Discover repository checkouts directly under root (non-recursive).

    Managed worktrees at ``<repo>/.worktrees/<name>`` surface as
    first-class records named ``<repo>@<name>`` with ``worktree_of``
    set, so sessions can see and claim them without a family rescan.
    Worktrees stay nested inside their project so sandbox project scope
    remains writable.
    """
    base = Path(root).resolve()
    try:
        candidates = sorted(path for path in base.iterdir() if path.is_dir() and not path.is_symlink())
    except OSError as error:
        return {"root": str(base), "error": str(error), "repositories": []}
    repos: list[dict[str, Any]] = []
    skipped = 0
    for candidate in candidates[:MAX_REPOS]:
        state = inspect_repo(candidate)
        if state is None:
            if candidate.name != MANAGED_WORKTREES_DIR:
                skipped += 1
            continue
        repos.append(state.record())
        for record in _managed_worktrees(candidate):
            repos.append(record)
    return {
        "root": str(base),
        "repository_count": len(repos),
        "non_repository_count": skipped,
        "repositories": repos,
    }


def _managed_worktrees(repo: Path) -> list[dict[str, Any]]:
    """First-class records for worktrees nested in one repository."""
    out: list[dict[str, Any]] = []
    directory = repo / MANAGED_WORKTREES_DIR
    try:
        checkouts = sorted(path for path in directory.iterdir()
                           if path.is_dir() and not path.is_symlink())
    except OSError:
        return out
    for checkout in checkouts[:MAX_REPOS]:
        state = inspect_repo(checkout)
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
