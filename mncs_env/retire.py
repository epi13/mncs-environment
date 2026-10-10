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
    branches = local_branches(repo)
    return _assess_registered_worktree(
        repo, target, row, rows, branches, main, canonical,
        claim_records or [], requesting_session,
        path_available=Path(target).is_dir(),
        clean=worktree_clean(repo, Path(target)) if Path(target).is_dir() else False,
    )


def _assess_registered_worktree(
    repo: Path, target: str, row: dict[str, Any], rows: list[dict[str, Any]],
    branches: dict[str, str], main: Path | None, canonical: str,
    claim_records: list[dict[str, Any]], requesting_session: str | None,
    *, path_available: bool, clean: bool,
) -> dict[str, Any]:
    """Apply the retirement contract to one already observed checkout."""
    if main is not None and _same_path(target, main):
        return {"retireable": False, "reason": "main checkout is never retired"}
    if not path_available:
        return {"retireable": False,
                "reason": "directory is gone; use prune, not retire"}
    if not clean:
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
        others = [str(item.get("path")) for item in rows
                  if item.get("branch") == branch and str(item.get("path")) != target]
        if others:
            return {"retireable": False,
                    "reason": f"attached branch {branch} is also checked out at {others[0]}"}
        if str(branch) not in branches:
            return {"retireable": False,
                    "reason": f"attached branch {branch} has no local ref"}
        topology = _branch_topology(repo, str(branch), branches, canonical)
        if not topology["retireable"]:
            return {"retireable": False,
                    "reason": f"attached branch {branch}: {topology['reason']}"}
    blockers = _repo_claims(claim_records, repo.name, requesting_session)
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


def _same_path(left: str | Path, right: str | Path) -> bool:
    return Path(left).resolve(strict=False) == Path(right).resolve(strict=False)


def _checkout_claims(records: list[dict[str, Any]], repository: str,
                     checkout: str, branch: str | None) -> list[dict[str, Any]]:
    """Return live claims conservatively covering one physical checkout."""
    covered: list[dict[str, Any]] = []
    for record in _repo_claims(records, repository, None):
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
        common_path = Path(common.stdout.strip()).resolve(strict=False)
        if workspace_root is not None:
            try:
                common_relative = common_path.relative_to(workspace_root.resolve()).as_posix()
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
                         rows: list[dict[str, Any]], main: Path | None
                         ) -> dict[str, tuple[str, str]]:
    """Map observed checkout paths to Git-admin identities, not path identities."""
    common = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
    identities: dict[str, tuple[str, str]] = {}
    if common.returncode == 0:
        admin_root = Path(common.stdout.strip()).resolve(strict=False) / "worktrees"
        if admin_root.is_dir():
            for admin in sorted(admin_root.iterdir()):
                gitdir_record = admin / "gitdir"
                try:
                    checkout_gitdir = Path(gitdir_record.read_text(encoding="utf-8").strip())
                except (OSError, UnicodeDecodeError):
                    continue
                checkout = checkout_gitdir.parent.resolve(strict=False)
                identity = "git-worktree:" + digest_hex({
                    "git_common_directory_identity": common_identity,
                    "administrative_identity": admin.name,
                })
                identities[str(checkout)] = (identity, "git-administrative-identity")
    if main is not None:
        identities[str(main.resolve(strict=False))] = (
            "git-worktree:" + digest_hex({
                "git_common_directory_identity": common_identity,
                "administrative_identity": "main-checkout",
            }),
            "main-checkout-role",
        )
    for row in rows:
        observed = str(row.get("path", ""))
        if not any(_same_path(observed, known) for known in identities):
            identities[str(Path(observed).resolve(strict=False))] = (
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
    rows = worktrees(repo)
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
    )
    execution_root_resolved = (
        Path(execution_root).resolve(strict=False) if execution_root is not None else None
    )

    worktree_state: dict[str, dict[str, Any]] = {}
    for row in rows:
        raw_path = str(row.get("path", ""))
        checkout_path = Path(raw_path)
        in_scope = execution_root_resolved is None
        if execution_root_resolved is not None:
            resolved_path = checkout_path.resolve(strict=False)
            in_scope = (resolved_path == execution_root_resolved
                        or execution_root_resolved in resolved_path.parents)
        exists = in_scope and checkout_path.is_dir()
        clean: bool | None = worktree_clean(repo, checkout_path) if exists else None
        head = str(row.get("head") or "")
        branch = row.get("branch")
        branch_ref_exists = isinstance(branch, str) and branch in branches
        head_reachable = bool(head and canonical_ok and is_reachable(repo, head, canonical))
        evidence_paths = _commit_paths(repo, head) if row.get("detached") and head else []
        evidence_relevant = any(path == "evidence" or path.startswith("evidence/")
                                for path in evidence_paths)
        covering = _checkout_claims(records, repo.name, raw_path, branch)
        own_claims = [item for item in covering
                      if item.get("session_id") == requesting_session]
        other_claims = [item for item in covering
                        if item.get("session_id") != requesting_session]
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
        elif main is not None and _same_path(raw_path, main):
            classification = "canonical-checkout"
            reason = "main checkout is never retired"
        elif not head or (branch is None and not row.get("detached")) or (
                branch is not None and not branch_ref_exists):
            classification = "orphaned-or-incomplete-ownership-uncertain"
            reason = "Git worktree metadata has no verifiable head or attached branch ref"
        elif row.get("detached") and evidence_relevant:
            classification = "detached-but-evidence-relevant"
            reason = "detached HEAD's tip commit changes tracked evidence paths"
        elif clean is False:
            classification = "uncommitted-work-retained"
            reason = "checkout has uncommitted or untracked files"
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
        else:
            assessed = _assess_registered_worktree(
                repo, raw_path, row, rows, branches, main, canonical, records,
                requesting_session, path_available=exists, clean=clean is True,
            )
            if assessed["retireable"]:
                classification = "merged-and-safely-retireable"
                reason = assessed["reason"]
            elif branch in branches and _branch_topology(repo, str(branch), branches, canonical)["retireable"]:
                classification = "merged-but-retained"
                reason = assessed["reason"]
            else:
                classification = "unmerged-work-requires-review"
                reason = assessed["reason"]
        worktree_state[raw_path] = {
            "classification": classification,
            "clean": clean,
            "path_available": exists if in_scope else None,
            "within_execution_scope": in_scope,
            "head_reachable": head_reachable if canonical_ok else None,
            "branch_ref_exists": branch_ref_exists if branch is not None else None,
            "evidence_relevant": evidence_relevant if row.get("detached") else None,
            "claim_records": own_claims + other_claims,
            "reason": reason,
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
            covering = _checkout_claims(records, repo.name, path, branch)
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
        topology = _branch_topology(repo, branch, branches, canonical) if canonical_ok else {
            "retireable": False,
            "reason": f"canonical ref {canonical} does not resolve; refusing to judge",
        }
        reachable = is_reachable(repo, head, canonical) if canonical_ok else False
        unique_count = (0 if reachable else
                        cherry_unique_count(repo, canonical, branch) if canonical_ok else None)
        canonical_is_ancestor = (
            _git(repo, "merge-base", "--is-ancestor", canonical, branch).returncode == 0
            if canonical_ok else None
        )
        ahead_behind = (_git(repo, "rev-list", "--left-right", "--count",
                             f"{canonical}...{branch}") if canonical_ok else None)
        behind_count = ahead_count = None
        if ahead_behind is not None and ahead_behind.returncode == 0:
            values = ahead_behind.stdout.split()
            if len(values) == 2:
                behind_count, ahead_count = int(values[0]), int(values[1])
        paths_at_tip = _commit_paths(repo, head)
        evidence_at_tip = sorted(
            path for path in paths_at_tip
            if path == "evidence" or path.startswith("evidence/")
        )
        evidence_only_tip = bool(paths_at_tip) and all(
            path == "evidence" or path.startswith("evidence/") for path in paths_at_tip
        )
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
            "canonical_reachable": reachable if canonical_ok else None,
            "canonical_ahead_count": ahead_count,
            "canonical_behind_count": behind_count,
            "unique_patch_commit_count": unique_count,
            "evidence_paths_at_tip": evidence_at_tip,
            "evidence_only_tip": evidence_only_tip,
            "retireable_after_review": bool(topology["retireable"] and not attached
                                              and not own_claims and not other_claims
                                              and branch not in CANONICAL_BRANCHES),
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
            "clean": facts["clean"],
            "path_available": facts["path_available"],
            "within_execution_scope": facts["within_execution_scope"],
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
        },
        "branches": branch_rows,
        "worktrees": worktree_rows,
    }
