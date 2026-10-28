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

import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import workspace as workspace_module
from .identity import digest_hex

CANONICAL_BRANCHES = ("main", "master")
DEFAULT_CANONICAL_REF = "origin/main"


class RetireError(Exception):
    """Raised when the repository cannot be inspected safely."""


def _git(repo: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [workspace_module.git_binary(), "-C", str(repo), *args],
        capture_output=True, text=True, timeout=timeout, check=False,
    )


def _execution_git(
    repo: Path,
    sandbox_root: Path | None,
    execution_root: Path | None,
    *args: str,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    """Run Git in the authenticated project alias, with only that repo writable.

    Protected checkouts can store absolute ``/workspace`` paths in linked
    worktree metadata. The Environment owner process sees their backing files
    under a host path instead, so running Git directly there cannot resolve the
    checkout's ``.git`` pointer. Recreate the exact project alias in a nested,
    networkless bubblewrap and mount only the authorized project writable.
    """
    invocation = [workspace_module.git_binary(), *args]
    if sandbox_root is None or execution_root is None:
        return _git(repo, *args, timeout=timeout)

    try:
        host_root = Path(execution_root).resolve(strict=True)
        repository = Path(repo).resolve(strict=True)
        sandbox = Path(os.path.abspath(str(sandbox_root)))
        git_binary = Path(workspace_module.git_binary()).resolve(strict=True)
    except OSError as error:
        return subprocess.CompletedProcess(
            invocation, 126, "", f"restricted Git root is unavailable: {error}",
        )
    if repository != host_root:
        return subprocess.CompletedProcess(
            invocation, 126, "",
            "restricted Git execution requires the exact authorized project root",
        )
    if (sandbox.parent != Path("/workspace") or sandbox.name in {"", ".", ".."}
            or not sandbox.is_absolute()):
        return subprocess.CompletedProcess(
            invocation, 126, "", "restricted Git sandbox root is not one project alias",
        )
    try:
        git_binary.relative_to(Path("/usr"))
    except ValueError:
        return subprocess.CompletedProcess(
            invocation, 126, "",
            "restricted Git executable is outside the read-only system tool root",
        )
    bubblewrap = shutil.which("bwrap", path="/usr/bin:/bin")
    if bubblewrap is None:
        return subprocess.CompletedProcess(
            invocation, 126, "", "bubblewrap is unavailable for restricted Git execution",
        )
    bubblewrap_path = Path(bubblewrap).resolve(strict=True)
    try:
        bubblewrap_path.relative_to(Path("/usr"))
    except ValueError:
        return subprocess.CompletedProcess(
            invocation, 126, "",
            "restricted Git launcher is outside the read-only system tool root",
        )

    command = [
        str(bubblewrap_path),
        "--die-with-parent",
        "--new-session",
        "--unshare-net",
        "--unshare-pid",
        "--cap-drop", "ALL",
    ]
    for root in ("/usr", "/etc", "/lib", "/lib64"):
        if Path(root).exists():
            command.extend(("--ro-bind", root, root))
    command.extend((
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--dir", "/workspace",
        "--bind", str(host_root), str(sandbox),
        "--chdir", str(sandbox),
        "--setenv", "PATH", "/usr/bin:/bin",
        "--setenv", "HOME", "/tmp",
        "--setenv", "LANG", "C.UTF-8",
        "--setenv", "GIT_CONFIG_NOSYSTEM", "1",
        "--setenv", "GIT_CONFIG_GLOBAL", "/dev/null",
        "--setenv", "GIT_OPTIONAL_LOCKS", "0",
        "--setenv", "GIT_TERMINAL_PROMPT", "0",
        str(git_binary),
        *args,
    ))
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={"PATH": "/usr/bin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8"},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return subprocess.CompletedProcess(
            invocation, 126, "", f"restricted Git execution failed: {error}",
        )


def _map_host_checkout(
    path: str | Path,
    sandbox_root: Path | None,
    execution_root: Path | None,
) -> Path | None:
    """Map one owner-visible project path back to its protected alias."""
    if sandbox_root is None or execution_root is None:
        return None
    source = Path(os.path.abspath(str(execution_root)))
    destination = Path(os.path.abspath(str(sandbox_root)))
    candidate = Path(os.path.abspath(str(path)))
    try:
        relative = candidate.relative_to(source)
    except ValueError:
        return None
    mapped = destination / relative
    if not _within_root_without_symlinks(candidate, source):
        return None
    return mapped


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
                       "head": "", "detached": False, "locked": False,
                       "prunable_reason": None}
        elif line.startswith("branch "):
            current["branch"] = line[len("branch "):].strip().removeprefix("refs/heads/")
        elif line.startswith("HEAD "):
            current["head"] = line[len("HEAD "):].strip()
        elif line == "detached":
            current["detached"] = True
        elif line == "locked" or line.startswith("locked "):
            current["locked"] = True
            current["lock_reason"] = line[len("locked"):].strip() or None
        elif line == "prunable" or line.startswith("prunable "):
            current["prunable_reason"] = (
                line[len("prunable"):].strip() or "Git marks this entry prunable"
            )
    if current:
        rows.append(current)
    return rows


def _map_execution_checkout(
    path: str | Path,
    sandbox_root: Path | None,
    execution_root: Path | None,
) -> Path | None:
    if sandbox_root is None or execution_root is None:
        return None
    source = Path(os.path.abspath(str(sandbox_root)))
    destination = Path(os.path.abspath(str(execution_root)))
    candidate = Path(os.path.abspath(str(path)))
    try:
        relative = candidate.relative_to(source)
    except ValueError:
        return None
    mapped = destination / relative
    if not _within_root_without_symlinks(mapped, destination):
        return None
    return mapped


def _execution_worktrees(
    repo: Path,
    *,
    sandbox_root: Path | None = None,
    execution_root: Path | None = None,
) -> list[dict[str, Any]]:
    """Map protected project registrations to owner-service project aliases.

    Git stores the absolute path used by ``worktree add`` in its administrative
    metadata. A protected project commonly records ``/workspace/<project>``
    while the owner service addresses the checkout under its configured host
    root. Map only registrations beneath the exact project sandbox root; leave
    every other registration as an observation.
    """
    rows = worktrees(repo)
    for row in rows:
        registered = str(row.get("path", ""))
        mapped = _map_execution_checkout(registered, sandbox_root, execution_root)
        if mapped is None:
            continue
        row["registered_path"] = registered
        row["path"] = str(mapped)
    return rows


def _worktree_admin_identity(
    repo: Path,
    row: dict[str, Any],
    *,
    sandbox_root: Path | None = None,
    execution_root: Path | None = None,
) -> str | None:
    """Return a generation-sensitive identity for one linked checkout.

    Git's administrative directory name identifies a worktree while it is
    registered. Device/inode facts fence a later checkout that reuses the same
    path and branch after the original registration was removed.
    """
    checkout = Path(str(row.get("path", "")))
    marker = checkout / ".git"
    try:
        marker_stat = marker.stat()
        marker_text = marker.read_text(encoding="utf-8").strip()
        common = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
        if common.returncode != 0:
            return None
        common_path = Path(os.path.abspath(common.stdout.strip()))
        if not marker_text.startswith("gitdir:"):
            return None
        admin_name = Path(marker_text.partition(":")[2].strip()).name
        admin = common_path / "worktrees" / admin_name
        admin_stat = admin.stat()
        if not admin.is_dir():
            return None
        admin_parent = admin.resolve(strict=True).parent
        if admin_parent != (common_path / "worktrees").resolve(strict=True):
            return None
        backlink_path = admin / "gitdir"
        backlink = Path(backlink_path.read_text(encoding="utf-8").strip())
        if not backlink.is_absolute():
            backlink = (admin / backlink).resolve(strict=False)
        backlink_checkout = backlink.parent
        backlink_stat = backlink_path.stat()
        commondir_stat = (admin / "commondir").stat()
        registered = str(row.get("registered_path") or row.get("path", ""))
        if row.get("registered_path") is not None:
            backlink_checkout = _map_execution_checkout(
                backlink_checkout, sandbox_root, execution_root,
            )
        if (backlink_checkout is None
                or not _same_path(backlink_checkout, row.get("path", ""))):
            return None
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return digest_hex({
        "repository_common_directory": str(common_path),
        "administrative_directory": admin_name,
        "admin_device": admin_stat.st_dev,
        "admin_inode": admin_stat.st_ino,
        "backlink_device": backlink_stat.st_dev,
        "backlink_inode": backlink_stat.st_ino,
        "commondir_device": commondir_stat.st_dev,
        "commondir_inode": commondir_stat.st_ino,
        "checkout_gitfile_device": marker_stat.st_dev,
        "checkout_gitfile_inode": marker_stat.st_ino,
        "registered_path": registered,
        "branch": row.get("branch"),
        "head": row.get("head"),
        "detached": row.get("detached") is True,
    })


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
    return _worktree_status(repo, path)["clean"] is True


def _worktree_status(
    repo: Path,
    path: Path,
    *,
    registered_path: str | None = None,
    sandbox_root: Path | None = None,
    execution_root: Path | None = None,
) -> dict[str, Any]:
    """Distinguish clean, dirty, and unobservable checkout states."""
    if not Path(path).is_dir():
        return {
            "available": False,
            "clean": None,
            "reason": "checkout path is unavailable in the Environment execution namespace",
        }
    if registered_path is not None and sandbox_root is not None and execution_root is not None:
        mapped = _map_execution_checkout(
            registered_path, sandbox_root, execution_root,
        )
        if mapped is None or not _same_path(mapped, path):
            return {
                "available": True,
                "clean": None,
                "reason": "registered checkout does not map to the authorized project path",
            }
        completed = _execution_git(
            repo, sandbox_root, execution_root,
            "-C", str(sandbox_root), "-C", str(registered_path),
            "status", "--porcelain=v1", "--untracked-files=all",
            "--ignored=matching",
        )
    else:
        completed = _git(repo, "-C", str(path), "status", "--porcelain=v1",
                         "--untracked-files=all", "--ignored=matching")
    if completed.returncode != 0:
        diagnostic = completed.stderr.strip() or completed.stdout.strip()
        reason = "Git could not verify checkout status in the restricted project namespace"
        if diagnostic:
            reason += f": {diagnostic[:400]}"
        return {"available": True, "clean": None, "reason": reason}
    if completed.stdout.strip():
        return {
            "available": True,
            "clean": False,
            "reason": "worktree has uncommitted, untracked, or ignored files",
        }
    return {"available": True, "clean": True, "reason": "checkout is clean"}


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


def _checkout_paths(
    repo: Path,
    branch: str,
    *,
    sandbox_root: Path | None = None,
    execution_root: Path | None = None,
) -> list[str]:
    return [str(row.get("path")) for row in _execution_worktrees(
        repo, sandbox_root=sandbox_root, execution_root=execution_root,
    )
            if row.get("branch") == branch]


def branch_assessment(repo: Path, branch: str, *, canonical: str = DEFAULT_CANONICAL_REF,
                      claim_records: list[dict[str, Any]] | None = None,
                      requesting_session: str | None = None,
                      sandbox_root: Path | None = None,
                      execution_root: Path | None = None) -> dict[str, Any]:
    """Judge one branch; never mutates. Returns ``{"retireable": bool, "reason": str}``."""
    repo = Path(repo)
    _ensure_repo(repo)
    if branch in CANONICAL_BRANCHES:
        return {"retireable": False, "reason": "canonical branch is never retired"}
    branches = local_branches(repo)
    if branch not in branches:
        return {"retireable": False, "reason": "no such local branch"}
    checked_out = _checkout_paths(
        repo, branch, sandbox_root=sandbox_root, execution_root=execution_root,
    )
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
                        requesting_session: str | None = None,
                        sandbox_root: Path | None = None,
                        execution_root: Path | None = None) -> dict[str, Any]:
    """Judge one worktree; never mutates."""
    repo = Path(repo)
    _ensure_repo(repo)
    target = str(Path(path))
    rows = _execution_worktrees(
        repo, sandbox_root=sandbox_root, execution_root=execution_root,
    )
    row = next((item for item in rows if item.get("path") == target), None)
    if row is None:
        return {"retireable": False, "status": "unknown",
                "reason": "not a registered worktree"}
    status = _worktree_status(
        repo, Path(target),
        registered_path=(
            str(row["registered_path"])
            if isinstance(row.get("registered_path"), str) else None
        ),
        sandbox_root=sandbox_root,
        execution_root=execution_root,
    )
    if not status["available"]:
        reason = (
            "registered checkout is unavailable in the Environment service "
            "namespace; retaining metadata"
            if row.get("registered_path") is not None
            else "directory is gone; use prune, not retire"
        )
        return {
            "retireable": False,
            "status": "unknown",
            "reason": reason,
        }
    if status["clean"] is None:
        return {"retireable": False, "status": "unknown",
                "reason": status["reason"]}
    main = main_worktree(repo)
    branches = local_branches(repo)
    verdict = _assess_registered_worktree(
        repo, target, row, rows, branches, main, canonical,
        claim_records or [], requesting_session,
        path_available=True,
        clean=status["clean"],
    )
    verdict["status"] = "assessed"
    return verdict


def _assess_registered_worktree(
    repo: Path, target: str, row: dict[str, Any], rows: list[dict[str, Any]],
    branches: dict[str, str], main: Path | None, canonical: str,
    claim_records: list[dict[str, Any]], requesting_session: str | None,
    *, path_available: bool, clean: bool,
    topology_cache: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Apply the retirement contract to one already observed checkout."""
    if main is not None and _same_path(target, main):
        return {"retireable": False, "reason": "main checkout is never retired"}
    if not path_available:
        return {"retireable": False,
                "reason": "directory is gone; use prune, not retire"}
    if not clean:
        return {
            "retireable": False,
            "reason": "worktree has uncommitted, untracked, or ignored files",
        }
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
        others = [str(item.get("path")) for item in rows
                  if item.get("branch") == branch and str(item.get("path")) != target]
        if others:
            return {"retireable": False,
                    "reason": f"attached branch {branch} is also checked out at {others[0]}"}
        if str(branch) not in branches:
            return {"retireable": False,
                    "reason": f"attached branch {branch} has no local ref"}
        topology = ((topology_cache or {}).get(str(branch))
                    or _branch_topology(repo, str(branch), branches, canonical))
        if not topology["retireable"]:
            return {"retireable": False,
                    "reason": f"attached branch {branch}: {topology['reason']}"}
    blockers = _repo_claims(claim_records, repo.name, requesting_session)
    if _checkout_claimed(blockers, target):
        holders = sorted({str(item.get("consumer_id", "?")) for item in blockers})
        return {"retireable": False,
                "reason": f"live claims by other sessions: {', '.join(holders)}"}
    return {"retireable": True, "reason": "clean and free of conflicting live claims"}


def retire_branch(repo: Path, branch: str, *, canonical: str = DEFAULT_CANONICAL_REF,
                  claim_records: list[dict[str, Any]] | None = None,
                  requesting_session: str | None = None,
                  dry_run: bool = False,
                  sandbox_root: Path | None = None,
                  execution_root: Path | None = None,
                  expected_head: str | None = None) -> dict[str, Any]:
    """Delete one merged local branch after strict assessment."""
    verdict = branch_assessment(repo, branch, canonical=canonical,
                                claim_records=claim_records,
                                requesting_session=requesting_session,
                                sandbox_root=sandbox_root,
                                execution_root=execution_root)
    if not verdict["retireable"]:
        return {"target": branch, "kind": "branch", "retired": False,
                "reason": verdict["reason"], "dry_run": dry_run}
    if dry_run:
        return {"target": branch, "kind": "branch", "retired": False,
                "reason": "dry run: would delete", "dry_run": True}
    current_head = local_branches(Path(repo)).get(branch)
    if expected_head is not None and current_head != expected_head:
        return {"target": branch, "kind": "branch", "retired": False,
                "status": "unknown",
                "reason": "branch tip changed after retirement intent",
                "dry_run": False}
    if current_head is None:
        return {"target": branch, "kind": "branch", "retired": False,
                "status": "unknown", "reason": "branch ref disappeared before mutation",
                "dry_run": False}
    # Delete the ref with an expected old object id. This retains Git's
    # conditional-ref semantics if another writer changes the branch after
    # assessment.
    completed = _git(
        Path(repo), "update-ref", "-d", f"refs/heads/{branch}", current_head,
    )
    if completed.returncode != 0:
        return {"target": branch, "kind": "branch", "retired": False,
                "status": "mutation-failed",
                "reason": f"git refused: {completed.stderr.strip()}", "dry_run": False}
    return {"target": branch, "kind": "branch", "retired": True,
            "reason": verdict["reason"], "dry_run": False}


def retire_worktree(repo: Path, path: str | Path, *,
                    canonical: str = DEFAULT_CANONICAL_REF,
                    claim_records: list[dict[str, Any]] | None = None,
                    requesting_session: str | None = None,
                    dry_run: bool = False,
                    sandbox_root: Path | None = None,
                    execution_root: Path | None = None,
                    expected_identity: str | None = None) -> dict[str, Any]:
    """Remove one spent worktree (its branch, if any, is kept)."""
    verdict = worktree_assessment(repo, path, canonical=canonical,
                                  claim_records=claim_records,
                                  requesting_session=requesting_session,
                                  sandbox_root=sandbox_root,
                                  execution_root=execution_root)
    if not verdict["retireable"]:
        return {"target": str(path), "kind": "worktree", "retired": False,
                "status": verdict.get("status", "assessed"),
                "reason": verdict["reason"], "dry_run": dry_run}
    if dry_run:
        return {"target": str(path), "kind": "worktree", "retired": False,
                "status": "would-retire",
                "reason": "dry run: would remove", "dry_run": True}
    if expected_identity is not None:
        current = retirement_target_state(
            repo, "worktree", str(path),
            sandbox_root=sandbox_root, execution_root=execution_root,
        )
        if current.get("identity") != expected_identity:
            return {
                "target": str(path), "kind": "worktree", "retired": False,
                "status": "unknown",
                "reason": "registered checkout identity changed before mutation",
                "dry_run": False,
            }
    mapped_target = _map_host_checkout(path, sandbox_root, execution_root)
    if sandbox_root is not None and execution_root is not None:
        if mapped_target is None:
            return {
                "target": str(path), "kind": "worktree", "retired": False,
                "status": "unknown",
                "reason": "checkout does not map to the authorized project namespace",
                "dry_run": False,
            }
        completed = _execution_git(
            Path(repo), sandbox_root, execution_root,
            "-C", str(sandbox_root), "worktree", "remove", str(mapped_target),
        )
    else:
        completed = _git(Path(repo), "worktree", "remove", str(path))
    if completed.returncode != 0:
        return {"target": str(path), "kind": "worktree", "retired": False,
                "status": "mutation-failed",
                "reason": f"git refused: {completed.stderr.strip()}", "dry_run": False}
    return {"target": str(path), "kind": "worktree", "retired": True,
            "status": "retired", "reason": verdict["reason"], "dry_run": False}


def prune_worktrees(
    repo: Path,
    *,
    dry_run: bool = False,
    sandbox_root: Path | None = None,
    execution_root: Path | None = None,
    expected_candidates: list[str] | None = None,
) -> dict[str, Any]:
    """Drop administrative records whose directories are already gone.

    ``git worktree prune`` only removes metadata for missing
    directories; it cannot delete files or checked-out branches.
    """
    repo = Path(repo)
    _ensure_repo(repo)
    def git_prune(*flags: str) -> subprocess.CompletedProcess[str]:
        if sandbox_root is not None and execution_root is not None:
            return _execution_git(
                repo, sandbox_root, execution_root,
                "-C", str(sandbox_root), "worktree", "prune", *flags,
            )
        return _git(repo, "worktree", "prune", *flags)

    if dry_run:
        completed = git_prune("--dry-run", "-v")
        if completed.returncode != 0:
            return {"target": str(repo), "kind": "prune", "retired": False,
                    "status": "unknown",
                    "reason": f"git could not assess prune candidates: {completed.stderr.strip()}",
                    "dry_run": True}
        return {"target": str(repo), "kind": "prune", "retired": False,
                "reason": "dry run", "dry_run": True,
                "output": completed.stdout.strip()}
    if expected_candidates is not None:
        observed = prune_worktrees(
            repo, dry_run=True,
            sandbox_root=sandbox_root, execution_root=execution_root,
        )
        if observed.get("status") == "unknown":
            return {**observed, "dry_run": False}
        lines = [line.strip() for line in str(observed.get("output", "")).splitlines()
                 if line.strip()]
        if any(line not in expected_candidates for line in lines):
            return {"target": str(repo), "kind": "prune", "retired": False,
                    "status": "unknown",
                    "reason": "new prune candidates appeared after retirement intent",
                    "dry_run": False}
    completed = git_prune("-v")
    if completed.returncode != 0:
        return {"target": str(repo), "kind": "prune", "retired": False,
                "reason": f"git refused: {completed.stderr.strip()}", "dry_run": False}
    return {"target": str(repo), "kind": "prune", "retired": True,
            "reason": "pruned", "dry_run": False, "output": completed.stdout.strip()}


def retirement_target_state(
    repo: Path,
    kind: str,
    target: str,
    *,
    sandbox_root: Path | None = None,
    execution_root: Path | None = None,
) -> dict[str, Any]:
    """Capture the exact Git state needed to reconcile one retirement retry."""
    repo = Path(repo)
    common = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common.returncode != 0:
        return {"status": "unknown", "reason": "Git common directory is unavailable"}
    common_path = Path(os.path.abspath(common.stdout.strip()))
    repository_identity = digest_hex({
        "common_directory": str(common_path),
        "repository_name": repo.name,
    })
    if kind == "worktree":
        target_path = Path(os.path.abspath(target))
        rows = _execution_worktrees(
            repo, sandbox_root=sandbox_root, execution_root=execution_root,
        )
        row = next((item for item in rows
                    if _same_path(item.get("path", ""), target_path)), None)
        exists = target_path.is_dir()
        if row is None:
            return {
                "status": "available" if not exists else "ambiguous",
                "repository_identity": repository_identity,
                "path_exists": exists,
                "registered": False,
                "identity": None,
            }
        identity = _worktree_admin_identity(
            repo, row, sandbox_root=sandbox_root, execution_root=execution_root,
        ) if exists else None
        return {
            "status": "available" if exists and identity else "unknown",
            "repository_identity": repository_identity,
            "path_exists": exists,
            "registered": True,
            "identity": identity,
            "branch": row.get("branch"),
            "head": row.get("head"),
            "detached": row.get("detached") is True,
            "registered_path": row.get("registered_path") or row.get("path"),
        }
    if kind == "branch":
        head = local_branches(repo).get(target)
        rows = _execution_worktrees(
            repo, sandbox_root=sandbox_root, execution_root=execution_root,
        )
        checkouts = sorted(
            str(row.get("path")) for row in rows if row.get("branch") == target
        )
        return {
            "status": "available" if head is not None else "missing",
            "repository_identity": repository_identity,
            "branch": target,
            "head": head,
            "checked_out_paths": checkouts,
        }
    if kind == "prune":
        result = prune_worktrees(
            repo, dry_run=True,
            sandbox_root=sandbox_root, execution_root=execution_root,
        )
        if result.get("status") == "unknown":
            return {"status": "unknown", "reason": result.get("reason")}
        output = result.get("output")
        if not isinstance(output, str):
            return {"status": "unknown", "reason": "Git prune candidates are unavailable"}
        candidates = sorted(line.strip() for line in output.splitlines() if line.strip())
        return {
            "status": "available",
            "repository_identity": repository_identity,
            "candidates": candidates,
        }
    return {"status": "unknown", "reason": f"unsupported retirement target kind: {kind}"}


def reconcile_retirement_target(
    repo: Path,
    kind: str,
    target: str,
    expected: dict[str, Any],
    *,
    sandbox_root: Path | None = None,
    execution_root: Path | None = None,
) -> dict[str, Any]:
    """Decide whether a pending request is complete, retryable, or ambiguous."""
    current = retirement_target_state(
        repo, kind, target,
        sandbox_root=sandbox_root, execution_root=execution_root,
    )
    if current.get("status") == "unknown":
        return {"state": "unknown", "reason": current.get("reason", "target state is unavailable")}
    if current.get("repository_identity") != expected.get("repository_identity"):
        return {"state": "unknown", "reason": "Git common-directory identity changed"}
    if kind == "worktree":
        if (current.get("registered") is False
                and current.get("path_exists") is False):
            return {"state": "completed", "reason": "checkout and Git registration are absent"}
        if (current.get("registered") is not True
                or current.get("path_exists") is not True):
            return {"state": "unknown", "reason": "checkout and registration disagree; preserving both states"}
        if (not expected.get("identity")
                or current.get("identity") != expected.get("identity")):
            return {"state": "unknown", "reason": "registered checkout identity changed"}
        return {"state": "retry", "reason": "the original registered checkout remains"}
    if kind == "branch":
        if current.get("head") is None:
            if current.get("checked_out_paths"):
                return {"state": "unknown", "reason": "branch ref is absent while a checkout still names it"}
            return {"state": "completed", "reason": "branch ref is absent"}
        if current.get("head") != expected.get("head"):
            return {"state": "unknown", "reason": "branch tip changed after retirement intent"}
        if current.get("checked_out_paths"):
            return {"state": "unknown", "reason": "branch became checked out after retirement intent"}
        return {"state": "retry", "reason": "the original branch ref remains"}
    if kind == "prune":
        previous = expected.get("candidates")
        remaining = current.get("candidates")
        if not isinstance(previous, list) or not isinstance(remaining, list):
            return {"state": "unknown", "reason": "prune candidate identity is invalid"}
        if not remaining:
            return {"state": "completed", "reason": "all recorded stale registrations are absent"}
        if any(item not in previous for item in remaining):
            return {"state": "unknown", "reason": "new prune candidates appeared after retirement intent"}
        return {"state": "retry", "reason": "recorded stale registrations remain"}
    return {"state": "unknown", "reason": f"unsupported retirement target kind: {kind}"}


def _same_path(left: str | Path, right: str | Path) -> bool:
    return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(os.path.abspath(str(right)))


def _within_root_without_symlinks(path: str | Path, root: str | Path) -> bool:
    """Check a grant boundary lexically, rejecting symlinked path components."""
    absolute = Path(os.path.abspath(str(path)))
    boundary = Path(os.path.abspath(str(root)))
    try:
        relative = absolute.relative_to(boundary)
    except ValueError:
        return False
    current = boundary
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return False
    return True


def _checkout_claims(repository_claims: list[dict[str, Any]], checkout: str,
                     branch: str | None) -> list[dict[str, Any]]:
    """Return live claims conservatively covering one physical checkout."""
    covered: list[dict[str, Any]] = []
    for record in repository_claims:
        scope = record.get("scope", {})
        kind = scope.get("kind", "repository") if isinstance(scope, dict) else "repository"
        if kind in ("repository", "paths"):
            # Retirement currently protects every linked checkout when a path
            # claim is live because the affected checkout cannot be proven.
            covered.append(record)
        elif kind == "worktree" and isinstance(scope, dict):
            claimed_path = scope.get("checkout")
            claimed_branch = scope.get("branch")
            if ((isinstance(claimed_path, str) and _same_path(claimed_path, checkout))
                    or (branch and claimed_branch == branch)):
                covered.append(record)
    return covered


def _commit_paths(repo: Path, commit: str) -> list[str]:
    completed = _git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", commit)
    if completed.returncode != 0:
        return []
    return sorted(path for path in completed.stdout.splitlines() if path)


def _repository_identity(repo: Path, workspace_root: Path | None) -> dict[str, Any]:
    common = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    common_path: Path | None = None
    common_relative: str | None = None
    material: dict[str, Any] = {"repository": repo.name}
    if common.returncode == 0:
        common_path = Path(os.path.abspath(common.stdout.strip()))
        if workspace_root is not None:
            try:
                common_relative = common_path.relative_to(
                    Path(os.path.abspath(str(workspace_root)))
                ).as_posix()
            except ValueError:
                pass
        if common_relative is not None:
            material["common_directory"] = common_relative
    remote = _git(repo, "config", "--get", "remote.origin.url")
    if remote.returncode == 0 and remote.stdout.strip():
        # Remote URLs can contain credentials; persist only their digest.
        material["remote_identity"] = digest_hex(remote.stdout.strip())
    elif common_path is not None and common_relative is None:
        material["common_directory_identity"] = digest_hex(str(common_path))
    return {
        "identity": "git-common:" + digest_hex(material),
        "common_directory_relative": common_relative,
    }


def _worktree_identities(repo: Path, common_identity: str,
                         rows: list[dict[str, Any]], main: Path | None,
                         *, sandbox_root: Path | None = None,
                         execution_root: Path | None = None,
                         ) -> dict[str, tuple[str, str]]:
    """Map observed checkout paths to Git-admin identities, not path identities."""
    common = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    identities: dict[str, tuple[str, str]] = {}
    if common.returncode == 0:
        admin_root = Path(os.path.abspath(common.stdout.strip())) / "worktrees"
        if admin_root.is_dir():
            for admin in sorted(admin_root.iterdir()):
                gitdir_record = admin / "gitdir"
                try:
                    checkout_gitdir = Path(gitdir_record.read_text(encoding="utf-8").strip())
                except (OSError, UnicodeDecodeError):
                    continue
                checkout = Path(os.path.abspath(str(checkout_gitdir.parent)))
                mapped = _map_execution_checkout(
                    checkout, sandbox_root, execution_root,
                )
                if mapped is not None:
                    checkout = mapped
                identity = "git-worktree:" + digest_hex({
                    "git_common_directory_identity": common_identity,
                    "administrative_identity": admin.name,
                })
                identities[str(checkout)] = (identity, "git-administrative-identity")
    if main is not None:
        identities[str(Path(os.path.abspath(str(main))))] = (
            "git-worktree:" + digest_hex({
                "git_common_directory_identity": common_identity,
                "administrative_identity": "main-checkout",
            }),
            "main-checkout-role",
        )
    for row in rows:
        observed = str(row.get("path", ""))
        if not any(_same_path(observed, known) for known in identities):
            identities[str(Path(os.path.abspath(observed)))] = (
                "git-worktree:" + digest_hex({
                    "git_common_directory_identity": common_identity,
                    "path_observation": observed,
                }),
                "path-observation-fallback",
            )
    return identities


def inventory_repository(
    repo: Path,
    *,
    canonical: str = DEFAULT_CANONICAL_REF,
    claim_records: list[dict[str, Any]] | None = None,
    requesting_session: str | None = None,
    workspace_root: Path | None = None,
    execution_root: Path | None = None,
    sandbox_root: Path | None = None,
) -> dict[str, Any]:
    """Return a read-only branch/worktree disposition.

    Missing and out-of-scope paths are observations only. This operation never
    prunes Git metadata or removes a branch or worktree.
    """
    repo = Path(repo).resolve()
    _ensure_repo(repo)
    records = claim_records or []
    repository_claims = _repo_claims(records, repo.name, None)
    branches = local_branches(repo)
    rows = _execution_worktrees(
        repo, sandbox_root=sandbox_root, execution_root=execution_root,
    )
    common_identity = _repository_identity(repo, workspace_root)
    canonical_ok = canonical_resolves(repo, canonical)
    canonical_head = None
    if canonical_ok:
        resolved = _git(repo, "rev-parse", "--verify", f"{canonical}^{{commit}}")
        if resolved.returncode == 0:
            canonical_head = resolved.stdout.strip()
    main = main_worktree(repo)
    checkout_identities = _worktree_identities(
        repo, common_identity["identity"], rows, main,
        sandbox_root=sandbox_root, execution_root=execution_root,
    )
    execution_root_resolved = (
        Path(os.path.abspath(str(execution_root))) if execution_root is not None else None
    )

    branch_facts: dict[str, dict[str, Any]] = {}
    for branch, head in branches.items():
        behind_count = ahead_count = unique_count = None
        reachable: bool | None = None
        canonical_is_ancestor: bool | None = None
        paths_at_tip: list[str] = []
        if canonical_ok:
            relation = _git(repo, "rev-list", "--left-right", "--count",
                            f"{canonical}...{branch}")
            if relation.returncode == 0:
                values = relation.stdout.split()
                if len(values) == 2:
                    behind_count, ahead_count = int(values[0]), int(values[1])
                    reachable = ahead_count == 0
                    canonical_is_ancestor = behind_count == 0
                    unique_count = (0 if reachable
                                    else cherry_unique_count(repo, canonical, branch))
                    # An ancestry-unique commit can still be patch-equivalent
                    # to a delivered change; inspect its file identity for
                    # evidence-only branches as well.
                    if ahead_count > 0:
                        paths_at_tip = _commit_paths(repo, head)
            if reachable is None:
                topology = {"retireable": False,
                            "reason": f"cannot compare branch with {canonical}"}
            elif not reachable and unique_count is None:
                topology = {"retireable": False,
                            "reason": f"tip is not reachable from {canonical} and patch-equivalence is unmeasurable"}
            elif unique_count and unique_count > 0:
                topology = {"retireable": False,
                            "reason": f"tip carries {unique_count} unique commit(s) not in {canonical}"}
            else:
                topology = {"retireable": True, "reason": "merged into " + canonical}
        else:
            topology = {"retireable": False,
                        "reason": f"canonical ref {canonical} does not resolve; refusing to judge"}
        evidence_at_tip = sorted(
            path for path in paths_at_tip
            if path == "evidence" or path.startswith("evidence/")
        )
        evidence_only_tip = bool(paths_at_tip) and all(
            path == "evidence" or path.startswith("evidence/") for path in paths_at_tip
        )
        branch_facts[branch] = {
            "topology": topology,
            "canonical_reachable": reachable,
            "canonical_is_ancestor": canonical_is_ancestor,
            "behind_count": behind_count,
            "ahead_count": ahead_count,
            "unique_patch_commit_count": unique_count,
            "evidence_paths_at_tip": evidence_at_tip,
            "evidence_only_tip": evidence_only_tip,
        }
    topology_cache = {name: facts["topology"] for name, facts in branch_facts.items()}

    worktree_state: dict[str, dict[str, Any]] = {}
    for row in rows:
        raw_path = str(row.get("path", ""))
        checkout_path = Path(raw_path)
        branch = row.get("branch")
        covering = _checkout_claims(repository_claims, raw_path, branch)
        own_claims = [item for item in covering
                      if item.get("session_id") == requesting_session]
        other_claims = [item for item in covering
                        if item.get("session_id") != requesting_session]
        in_scope = execution_root_resolved is None
        if execution_root_resolved is not None:
            in_scope = _within_root_without_symlinks(raw_path, execution_root_resolved)
        ownership_protected = bool(own_claims or other_claims)
        exists = (in_scope and checkout_path.is_dir()) if not ownership_protected else None
        checkout_status = (
            _worktree_status(
                repo, checkout_path,
                registered_path=(
                    str(row["registered_path"])
                    if isinstance(row.get("registered_path"), str) else None
                ),
                sandbox_root=sandbox_root,
                execution_root=execution_root,
            )
            if exists else None
        )
        clean = checkout_status["clean"] if checkout_status is not None else None
        if not in_scope:
            access_disposition = "outside-current-execution-scope"
        elif ownership_protected:
            access_disposition = "not-probed-protected"
        elif not exists:
            access_disposition = "unavailable-in-current-execution-namespace"
        else:
            access_disposition = "available"
        head = str(row.get("head") or "")
        branch_ref_exists = isinstance(branch, str) and branch in branches
        if isinstance(branch, str) and branch in branch_facts:
            head_reachable = branch_facts[branch]["canonical_reachable"]
        else:
            head_reachable = bool(head and canonical_ok and is_reachable(repo, head, canonical))
        evidence_paths = (
            _commit_paths(repo, head) if row.get("detached") and head
            else branch_facts.get(str(branch), {}).get("evidence_paths_at_tip", [])
        )
        evidence_relevant = any(path == "evidence" or path.startswith("evidence/")
                                for path in evidence_paths)
        if other_claims:
            classification = "protected-by-another-consumer"
            reason = "covered by one or more live claims held by other sessions"
        elif own_claims:
            classification = "active-and-owned"
            reason = "covered by a live claim held by the requesting session"
        elif not in_scope:
            classification = "outside-current-execution-scope"
            reason = "registered path is outside the authorized inspection root; not probed"
        elif not exists:
            classification = "unavailable-in-current-execution-namespace"
            reason = "registered path is not available here; absence is not treated as permission to prune"
        elif clean is None:
            classification = "unavailable-in-current-execution-namespace"
            reason = (checkout_status or {}).get(
                "reason", "Git could not verify checkout state in this execution namespace",
            )
        elif main is not None and _same_path(raw_path, main):
            classification = "canonical-checkout"
            reason = "main checkout is never retired"
        elif not head or (branch is None and not row.get("detached")) or (
                branch is not None and not branch_ref_exists):
            classification = "orphaned-or-incomplete-ownership-uncertain"
            reason = "Git worktree metadata has no verifiable head or attached branch ref"
        elif clean is False:
            classification = "uncommitted-work-retained"
            reason = "checkout has uncommitted, untracked, or ignored files"
        elif row.get("detached") and evidence_relevant:
            classification = "detached-but-evidence-relevant"
            reason = "detached HEAD's tip commit changes tracked evidence paths"
        elif row.get("detached") and not head_reachable:
            classification = "detached-unmerged-requires-review"
            reason = "detached HEAD is not reachable from the canonical ref"
        elif row.get("detached"):
            classification = "detached-canonical-retained"
            reason = "detached HEAD is canonical-reachable; retain until evidence need is confirmed"
        elif not canonical_ok:
            classification = "canonical-ref-unavailable"
            reason = f"canonical ref {canonical} does not resolve"
        elif branch in CANONICAL_BRANCHES:
            classification = "canonical-checkout"
            reason = "canonical branch checkout is retained"
        elif evidence_relevant and head_reachable:
            classification = "merged-and-retained-for-evidence"
            reason = "canonical-reachable tip commit changes tracked evidence paths"
        else:
            assessed = _assess_registered_worktree(
                repo, raw_path, row, rows, branches, main, canonical, records,
                requesting_session, path_available=exists is True, clean=clean is True,
                topology_cache=topology_cache,
            )
            if assessed["retireable"]:
                classification = "merged-and-safely-retireable"
                reason = assessed["reason"]
            elif branch in branches and topology_cache.get(str(branch), {}).get("retireable"):
                classification = "merged-but-retained"
                reason = assessed["reason"]
            else:
                classification = "unmerged-work-requires-review"
                reason = assessed["reason"]
        worktree_state[raw_path] = {
            "classification": classification,
            "clean": clean,
            "path_available": exists if in_scope and not ownership_protected else None,
            "within_execution_scope": in_scope,
            "access_disposition": access_disposition,
            "head_reachable": head_reachable if canonical_ok else None,
            "branch_ref_exists": branch_ref_exists if branch is not None else None,
            "evidence_relevant": evidence_relevant if row.get("detached") else None,
            "claim_records": own_claims + other_claims,
            "reason": reason,
            "checkout_status_reason": (
                checkout_status.get("reason") if checkout_status is not None else None
            ),
        }

    checkout_map: dict[str, list[str]] = {}
    for row in rows:
        if row.get("branch"):
            checkout_map.setdefault(str(row["branch"]), []).append(str(row["path"]))
    branch_rows: list[dict[str, Any]] = []
    for branch, head in sorted(branches.items()):
        attached = checkout_map.get(branch, [])
        own_claims: list[dict[str, Any]] = []
        other_claims: list[dict[str, Any]] = []
        for path in attached or [str(repo)]:
            covering = _checkout_claims(repository_claims, path, branch)
            own_claims.extend(item for item in covering
                              if item.get("session_id") == requesting_session)
            other_claims.extend(item for item in covering
                                if item.get("session_id") != requesting_session)
        own_claims = list({str(item.get("claim_id")): item for item in own_claims}.values())
        # The branch retirement contract blocks while any other session holds
        # a live repository claim, even when that claim names another checkout.
        other_claims = [item for item in repository_claims
                        if item.get("session_id") != requesting_session]
        other_claims = list({str(item.get("claim_id")): item for item in other_claims}.values())
        facts = branch_facts[branch]
        topology = facts["topology"]
        reachable = facts["canonical_reachable"]
        unique_count = facts["unique_patch_commit_count"]
        canonical_is_ancestor = facts["canonical_is_ancestor"]
        behind_count = facts["behind_count"]
        ahead_count = facts["ahead_count"]
        evidence_at_tip = facts["evidence_paths_at_tip"]
        evidence_only_tip = facts["evidence_only_tip"]
        if branch in CANONICAL_BRANCHES:
            classification = "canonical-branch"
            reason = "canonical branch is never retired"
        elif other_claims:
            classification = "protected-by-another-consumer"
            reason = "covered by one or more live claims held by other sessions"
        elif own_claims:
            classification = "active-and-owned"
            reason = "covered by a live claim held by the requesting session"
        elif not canonical_ok:
            classification = "canonical-ref-unavailable"
            reason = topology["reason"]
        elif topology["retireable"]:
            if attached:
                retained_evidence = any(
                    worktree_state.get(path, {}).get("evidence_relevant")
                    or worktree_state.get(path, {}).get("clean") is False
                    for path in attached
                )
                classification = ("merged-and-retained-for-evidence" if retained_evidence
                                  else "merged-but-checked-out")
                reason = "tip is canonical-equivalent but remains checked out"
            else:
                if evidence_at_tip:
                    classification = "merged-and-retained-for-evidence"
                    reason = "canonical-equivalent tip changes tracked evidence paths"
                else:
                    classification = "merged-and-safely-retireable"
                    reason = "merged branch has no checkout or foreign live claim"
        elif unique_count is None:
            classification = "unclassifiable-topology"
            reason = topology["reason"]
        elif unique_count > 0 and evidence_only_tip:
            classification = "unmerged-evidence-relevant"
            reason = f"unique commit(s) include evidence-only tip changes: {', '.join(evidence_at_tip)}"
        elif unique_count == 0:
            classification = "patch-equivalent-delivered"
            reason = "no unique patch remains, but Git ancestry differs"
        elif canonical_is_ancestor:
            classification = "unmerged-with-unique-commits"
            reason = f"branch has {unique_count} commit(s) not in {canonical}"
        else:
            classification = "independently-diverged"
            reason = f"branch and {canonical} diverged; branch has {unique_count} unique commit(s)"
        branch_rows.append({
            "branch": branch,
            "head": head,
            "classification": classification,
            "reason": reason,
            "checked_out_at": attached,
            "canonical_reachable": reachable,
            "canonical_ahead_count": ahead_count,
            "canonical_behind_count": behind_count,
            "unique_patch_commit_count": unique_count,
            "evidence_paths_at_tip": evidence_at_tip,
            "evidence_only_tip": evidence_only_tip,
            "retireable_after_review": classification == "merged-and-safely-retireable",
            "live_claims": own_claims + other_claims,
        })

    worktree_rows: list[dict[str, Any]] = []
    for row in rows:
        path = str(row.get("path", ""))
        facts = worktree_state[path]
        identity_pair = next((value for known, value in checkout_identities.items()
                              if _same_path(path, known)), None)
        identity, identity_basis = identity_pair or (None, "unknown")
        worktree_rows.append({
            "path": path,
            "registered_path": row.get("registered_path", path),
            "checkout_identity": identity,
            "checkout_identity_basis": identity_basis,
            "branch": row.get("branch"),
            "head": row.get("head"),
            "detached": bool(row.get("detached")),
            "locked": bool(row.get("locked")),
            "lock_reason": row.get("lock_reason"),
            "git_prunable_reason": row.get("prunable_reason"),
            "classification": facts["classification"],
            "reason": facts["reason"],
            "checkout_status_reason": facts["checkout_status_reason"],
            "clean": facts["clean"],
            "path_available": facts["path_available"],
            "within_execution_scope": facts["within_execution_scope"],
            "access_disposition": facts["access_disposition"],
            "canonical_reachable": facts["head_reachable"],
            "branch_ref_exists": facts["branch_ref_exists"],
            "evidence_relevant": facts["evidence_relevant"],
            "live_claims": facts["claim_records"],
            "retireable_after_review": facts["classification"] == "merged-and-safely-retireable",
        })

    def counts(items: list[dict[str, Any]]) -> dict[str, int]:
        result: dict[str, int] = {}
        for item in items:
            label = str(item.get("classification", "unknown"))
            result[label] = result.get(label, 0) + 1
        return result

    return {
        "schema_version": "mncs.environment.worktree-inventory/1",
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session_id": requesting_session,
        "dry_run": True,
        "mutation_performed": False,
        "repository": {
            "name": repo.name,
            "path": str(repo),
            "git_common_directory_identity": common_identity["identity"],
            "git_common_directory_relative": common_identity["common_directory_relative"],
            "main_worktree": str(main) if main else None,
        },
        "canonical": {"ref": canonical, "resolves": canonical_ok, "head": canonical_head},
        "execution_scope": {
            "root": str(execution_root_resolved) if execution_root_resolved else None,
            "mode": "authorized-root" if execution_root_resolved else "current-process-namespace",
        },
        "summary": {
            "branch_count": len(branch_rows),
            "worktree_count": len(worktree_rows),
            "branches_by_classification": counts(branch_rows),
            "worktrees_by_classification": counts(worktree_rows),
            "worktrees_by_access_disposition": counts([
                {"classification": row["access_disposition"]} for row in worktree_rows
            ]),
        },
        "branches": branch_rows,
        "worktrees": worktree_rows,
    }
