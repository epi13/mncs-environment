"""Retire merged branches and spent worktrees with strict preconditions.

Creation without retirement is how branches and worktrees accumulate:
the provider creates ``.worktrees/<slug>`` checkouts and campaign
branches, agents push, and nothing ever deletes the temporary refs.
This module is the missing retirement half, owned by the session
lifecycle: it only ever removes state that is *proven* unneeded, and
it refuses loudly otherwise.

A branch retires only when every precondition holds:

* its tip is reachable from the canonical ref (default
  ``origin/main``) or contributes no unique patch (``git cherry`` is
  empty of ``+`` lines, covering squash/rebase delivery);
* it is not checked out in any worktree;
* it is not a canonical branch name (``main``/``master``);
* no *other* session holds a live claim on the repository.

A worktree retires only when:

* it is a registered worktree whose directory exists (missing
  directories are ``prune``, not ``retire``);
* its tree is clean;
* it is not the main checkout;
* no other session holds a live claim covering it;
* its branch (when attached) also retires, is canonical, or the
  checkout is detached at a canonical-reachable commit.

Execution revalidates through git's own safe flags (``branch -d``,
plain ``worktree remove``), so a race between the check and the
mutation fails closed. Remote branches are never touched: deleting
published refs stays an explicit ``git push --delete`` after
verification.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

CANONICAL_BRANCHES = ("main", "master")
DEFAULT_CANONICAL_REF = "origin/main"


class RetireError(Exception):
    """Raised when the repository cannot be inspected safely."""


def _git(repo: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, timeout=timeout,
    )


def _ensure_repo(repo: Path) -> None:
    completed = _git(repo, "rev-parse", "--is-inside-work-tree")
    if completed.returncode != 0 or completed.stdout.strip() != "true":
        raise RetireError(f"not a git worktree: {repo}")


def local_branches(repo: Path) -> dict[str, str]:
    """Map local branch name -> tip sha."""
    completed = _git(repo, "for-each-ref", "--format=%(refname:short) %(objectname)",
                     "refs/heads")
    if completed.returncode != 0:
        raise RetireError(f"cannot list branches: {completed.stderr.strip()}")
    branches: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        name, _, sha = line.partition(" ")
        if name and sha:
            branches[name] = sha
    return branches


def worktrees(repo: Path) -> list[dict[str, Any]]:
    """Parse ``git worktree list --porcelain`` into row dicts."""
    completed = _git(repo, "worktree", "list", "--porcelain")
    if completed.returncode != 0:
        raise RetireError(f"cannot list worktrees: {completed.stderr.strip()}")
    rows: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    for line in completed.stdout.splitlines():
        if line.startswith("worktree "):
            if current:
                rows.append(current)
            current = {"path": line[len("worktree "):].strip(), "branch": None,
                       "head": "", "detached": False}
        elif line.startswith("branch "):
            current["branch"] = line[len("branch "):].strip().removeprefix("refs/heads/")
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD "):].strip()
        elif line == "detached":
            current["detached"] = True
    if current:
        rows.append(current)
    return rows


def main_worktree(repo: Path) -> Path | None:
    """Resolve the main checkout path (the holder of the common dir)."""
    completed = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if completed.returncode != 0:
        return None
    common = Path(completed.stdout.strip())
    return common.parent if common.name == ".git" else None


def is_reachable(repo: Path, commit: str, canonical: str) -> bool:
    """True when ``commit`` is an ancestor of ``canonical``."""
    completed = _git(repo, "merge-base", "--is-ancestor", commit, canonical)
    return completed.returncode == 0


def cherry_unique_count(repo: Path, canonical: str, branch: str) -> int | None:
    """Count ``+`` lines of ``git cherry`` (None when unmeasurable)."""
    completed = _git(repo, "cherry", canonical, branch)
    if completed.returncode != 0:
        return None
    return sum(1 for line in completed.stdout.splitlines() if line.startswith("+"))


def worktree_clean(repo: Path, path: Path) -> bool:
    completed = _git(repo, "-C", str(path), "status", "--porcelain=v1",
                     "--untracked-files=all")
    if completed.returncode != 0:
        return False
    return not completed.stdout.strip()


def canonical_resolves(repo: Path, canonical: str) -> bool:
    completed = _git(repo, "rev-parse", "--verify", "--quiet", f"{canonical}^{{commit}}")
    return completed.returncode == 0


def _repo_claims(claim_records: list[dict[str, Any]], repository: str,
                 requesting_session: str | None) -> list[dict[str, Any]]:
    """Live claims on ``repository`` held by sessions other than the requester."""
    from . import claims as claims_module

    live = claims_module.active_claims(claim_records)
    return [record for record in live.values()
            if str(record.get("repository", "")) == repository
            and str(record.get("session_id", "")) != (requesting_session or "\0")]


def _checkout_claimed(blockers: list[dict[str, Any]], checkout: str) -> bool:
    for record in blockers:
        scope = record.get("scope", {})
        if not isinstance(scope, dict):
            return True
        kind = scope.get("kind", "repository")
        if kind == "repository":
            return True
        if kind == "worktree" and scope.get("checkout") == checkout:
            return True
        if kind == "paths":
            return True
    return False


def _branch_topology(repo: Path, branch: str, branches: dict[str, str],
                     canonical: str) -> dict[str, Any]:
    """Judge merged-ness only (existence and checkouts are judged by callers)."""
    if not canonical_resolves(repo, canonical):
        return {"retireable": False,
                "reason": f"canonical ref {canonical} does not resolve; refusing to judge"}
    tip = branches[branch]
    if not is_reachable(repo, tip, canonical):
        unique = cherry_unique_count(repo, canonical, branch)
        if unique is None:
            return {"retireable": False,
                    "reason": "tip is not reachable from "
                            f"{canonical} and patch-equivalence is unmeasurable"}
        if unique > 0:
            return {"retireable": False,
                    "reason": f"tip carries {unique} unique commit(s) not in {canonical}"}
    return {"retireable": True, "reason": "merged into " + canonical}


def _checkout_paths(repo: Path, branch: str) -> list[str]:
    return [str(row.get("path")) for row in worktrees(repo)
            if row.get("branch") == branch]


def branch_assessment(repo: Path, branch: str, *, canonical: str = DEFAULT_CANONICAL_REF,
                      claim_records: list[dict[str, Any]] | None = None,
                      requesting_session: str | None = None) -> dict[str, Any]:
    """Judge one branch; never mutates. Returns ``{"retireable": bool, "reason": str}``."""
    repo = Path(repo)
    _ensure_repo(repo)
    if branch in CANONICAL_BRANCHES:
        return {"retireable": False, "reason": "canonical branch is never retired"}
    branches = local_branches(repo)
    if branch not in branches:
        return {"retireable": False, "reason": "no such local branch"}
    checked_out = _checkout_paths(repo, branch)
    if checked_out:
        return {"retireable": False,
                "reason": f"checked out at {checked_out[0]}"}
    topology = _branch_topology(repo, branch, branches, canonical)
    if not topology["retireable"]:
        return topology
    blockers = _repo_claims(claim_records or [], repo.name, requesting_session)
    if blockers:
        holders = sorted({str(item.get("consumer_id", "?")) for item in blockers})
        return {"retireable": False,
                "reason": f"live claims by other sessions: {', '.join(holders)}"}
    return {"retireable": True, "reason": "merged into " + canonical}


def worktree_assessment(repo: Path, path: str | Path, *,
                        canonical: str = DEFAULT_CANONICAL_REF,
                        claim_records: list[dict[str, Any]] | None = None,
                        requesting_session: str | None = None) -> dict[str, Any]:
    """Judge one worktree; never mutates."""
    repo = Path(repo)
    _ensure_repo(repo)
    target = str(Path(path))
    rows = worktrees(repo)
    row = next((item for item in rows if item.get("path") == target), None)
    if row is None:
        return {"retireable": False, "reason": "not a registered worktree"}
    main = main_worktree(repo)
    if main is not None and Path(target) == main:
        return {"retireable": False, "reason": "main checkout is never retired"}
    if not Path(target).is_dir():
        return {"retireable": False,
                "reason": "directory is gone; use prune, not retire"}
    if not worktree_clean(repo, Path(target)):
        return {"retireable": False, "reason": "worktree has uncommitted work"}
    if not canonical_resolves(repo, canonical):
        return {"retireable": False,
                "reason": f"canonical ref {canonical} does not resolve; refusing to judge"}
    branch = row.get("branch")
    if branch is None:
        if not row.get("head") or not is_reachable(repo, str(row["head"]), canonical):
            return {"retireable": False,
                    "reason": "detached HEAD is not reachable from " + canonical}
    elif branch not in CANONICAL_BRANCHES:
        # The checkout under retirement does not block its own branch;
        # checkouts anywhere else do. Claims are evaluated once below
        # against the checkout, so only topology is judged here.
        others = [path for path in _checkout_paths(repo, str(branch)) if path != target]
        if others:
            return {"retireable": False,
                    "reason": f"attached branch {branch} is also checked out at {others[0]}"}
        branches = local_branches(repo)
        if str(branch) not in branches:
            return {"retireable": False,
                    "reason": f"attached branch {branch} has no local ref"}
        topology = _branch_topology(repo, str(branch), branches, canonical)
        if not topology["retireable"]:
            return {"retireable": False,
                    "reason": f"attached branch {branch}: {topology['reason']}"}
    blockers = _repo_claims(claim_records or [], repo.name, requesting_session)
    if _checkout_claimed(blockers, target):
        holders = sorted({str(item.get("consumer_id", "?")) for item in blockers})
        return {"retireable": False,
                "reason": f"live claims by other sessions: {', '.join(holders)}"}
    return {"retireable": True, "reason": "clean and unclaimed"}


def retire_branch(repo: Path, branch: str, *, canonical: str = DEFAULT_CANONICAL_REF,
                  claim_records: list[dict[str, Any]] | None = None,
                  requesting_session: str | None = None,
                  dry_run: bool = False) -> dict[str, Any]:
    """Delete one merged local branch after strict assessment."""
    verdict = branch_assessment(repo, branch, canonical=canonical,
                                claim_records=claim_records,
                                requesting_session=requesting_session)
    if not verdict["retireable"]:
        return {"target": branch, "kind": "branch", "retired": False,
                "reason": verdict["reason"], "dry_run": dry_run}
    if dry_run:
        return {"target": branch, "kind": "branch", "retired": False,
                "reason": "dry run: would delete", "dry_run": True}
    completed = _git(Path(repo), "branch", "-d", branch)
    if completed.returncode != 0:
        return {"target": branch, "kind": "branch", "retired": False,
                "reason": f"git refused: {completed.stderr.strip()}", "dry_run": False}
    return {"target": branch, "kind": "branch", "retired": True,
            "reason": verdict["reason"], "dry_run": False}


def retire_worktree(repo: Path, path: str | Path, *,
                    canonical: str = DEFAULT_CANONICAL_REF,
                    claim_records: list[dict[str, Any]] | None = None,
                    requesting_session: str | None = None,
                    dry_run: bool = False) -> dict[str, Any]:
    """Remove one spent worktree (its branch, if any, is kept)."""
    verdict = worktree_assessment(repo, path, canonical=canonical,
                                  claim_records=claim_records,
                                  requesting_session=requesting_session)
    if not verdict["retireable"]:
        return {"target": str(path), "kind": "worktree", "retired": False,
                "reason": verdict["reason"], "dry_run": dry_run}
    if dry_run:
        return {"target": str(path), "kind": "worktree", "retired": False,
                "reason": "dry run: would remove", "dry_run": True}
    completed = _git(Path(repo), "worktree", "remove", str(path))
    if completed.returncode != 0:
        return {"target": str(path), "kind": "worktree", "retired": False,
                "reason": f"git refused: {completed.stderr.strip()}", "dry_run": False}
    return {"target": str(path), "kind": "worktree", "retired": True,
            "reason": verdict["reason"], "dry_run": False}


def prune_worktrees(repo: Path, *, dry_run: bool = False) -> dict[str, Any]:
    """Drop administrative records whose directories are already gone.

    ``git worktree prune`` only removes metadata for missing
    directories; it cannot delete files or checked-out branches.
    """
    repo = Path(repo)
    _ensure_repo(repo)
    if dry_run:
        completed = _git(repo, "worktree", "prune", "--dry-run", "-v")
        return {"target": str(repo), "kind": "prune", "retired": False,
                "reason": "dry run", "dry_run": True,
                "output": completed.stdout.strip()}
    completed = _git(repo, "worktree", "prune", "-v")
    if completed.returncode != 0:
        return {"target": str(repo), "kind": "prune", "retired": False,
                "reason": f"git refused: {completed.stderr.strip()}", "dry_run": False}
    return {"target": str(repo), "kind": "prune", "retired": True,
            "reason": "pruned", "dry_run": False, "output": completed.stdout.strip()}
