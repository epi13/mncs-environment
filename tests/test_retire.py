"""Retirement only removes proven-unneeded branches and worktrees."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from mncs_env import claims as claims_module
from mncs_env import retire as retire_module


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=60)


def _future() -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(timespec="seconds")


def _claim(session_id: str, repository: str, **scope: Any) -> dict[str, Any]:
    full = {"kind": "repository", "repository": repository, "checkout": None,
            "branch": None, "paths": None, "exclusive": True}
    full.update(scope)
    return {
        "schema_version": claims_module.SCHEMA,
        "claim_id": f"claim:{repository}",
        "version": 1,
        "repository": repository,
        "scope": full,
        "session_id": session_id,
        "consumer_id": "test-consumer",
        "basis": claims_module.BASIS_EXPLICIT,
        "reason": "test",
        "status": "held",
        "acquired_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        "expires_at": _future(),
        "provenance": {"acquired_by": "test-consumer"},
    }


class RetireTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mncs-retire-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.origin = self.base / "origin.git"
        _git(self.base, "init", "--bare", "--initial-branch=main", str(self.origin))
        self.repo = self.base / "demo"
        _git(self.base, "clone", str(self.origin), str(self.repo))
        _git(self.repo, "config", "user.email", "test@local")
        _git(self.repo, "config", "user.name", "test")
        _git(self.repo, "config", "commit.gpgsign", "false")
        (self.repo / "file.txt").write_text("one\n")
        _git(self.repo, "add", "file.txt")
        _git(self.repo, "commit", "-qm", "initial")
        _git(self.repo, "push", "-q", "origin", "main")
        self.name = self.repo.name

    def _worktree(self, slug: str, branch: str, start: str = "main") -> Path:
        target = self.repo / ".worktrees" / slug
        result = _git(self.repo, "worktree", "add", "-b", branch,
                      str(target.relative_to(self.repo)), start)
        self.assertEqual(result.returncode, 0, result.stderr)
        return target

    def test_checked_out_branch_refuses_even_when_merged(self):
        self._worktree("feat", "feature/x")
        _git(self.repo, "push", "-q", "origin", "feature/x:main")
        _git(self.repo, "fetch", "-q", "origin")
        verdict = retire_module.branch_assessment(self.repo, "feature/x")
        self.assertFalse(verdict["retireable"])
        self.assertIn("checked out", verdict["reason"])

    def test_unmerged_branch_refuses(self):
        target = self._worktree("feat", "feature/x")
        (target / "work.txt").write_text("wip\n")
        _git(target, "add", "work.txt")
        _git(target, "commit", "-qm", "wip")
        verdict = retire_module.branch_assessment(self.repo, "feature/x")
        self.assertFalse(verdict["retireable"])
        self.assertIn("checked out", verdict["reason"])

    def test_branch_without_checkout_and_unique_commits_refuses(self):
        target = self._worktree("feat", "feature/x")
        (target / "work.txt").write_text("wip\n")
        _git(target, "add", "work.txt")
        _git(target, "commit", "-qm", "wip")
        _git(self.repo, "worktree", "remove", "--force", str(target))
        verdict = retire_module.branch_assessment(self.repo, "feature/x")
        self.assertFalse(verdict["retireable"])
        self.assertIn("unique commit", verdict["reason"])

    def test_merged_branch_without_checkout_retires(self):
        target = self._worktree("feat", "feature/x")
        _git(self.repo, "worktree", "remove", str(target))
        result = retire_module.retire_branch(self.repo, "feature/x")
        self.assertTrue(result["retired"], result)
        self.assertNotIn("feature/x", retire_module.local_branches(self.repo))

    def test_canonical_branch_never_retires(self):
        verdict = retire_module.branch_assessment(self.repo, "main")
        self.assertFalse(verdict["retireable"])
        self.assertIn("canonical", verdict["reason"])

    def test_other_session_claim_blocks_branch(self):
        target = self._worktree("feat", "feature/x")
        _git(self.repo, "worktree", "remove", str(target))
        records = [_claim("ses_other", self.name)]
        verdict = retire_module.branch_assessment(
            self.repo, "feature/x", claim_records=records,
            requesting_session="ses_mine")
        self.assertFalse(verdict["retireable"])
        self.assertIn("live claims", verdict["reason"])

    def test_own_session_claim_does_not_block(self):
        target = self._worktree("feat", "feature/x")
        _git(self.repo, "worktree", "remove", str(target))
        records = [_claim("ses_mine", self.name)]
        result = retire_module.retire_branch(
            self.repo, "feature/x", claim_records=records,
            requesting_session="ses_mine")
        self.assertTrue(result["retired"], result)

    def test_clean_worktree_retires(self):
        target = self._worktree("feat", "feature/x")
        _git(self.repo, "push", "-q", "origin", "feature/x:main")
        _git(self.repo, "fetch", "-q", "origin")
        result = retire_module.retire_worktree(self.repo, target)
        self.assertTrue(result["retired"], result)
        self.assertFalse(target.exists())
        # The branch itself is kept; only the checkout is removed.
        self.assertIn("feature/x", retire_module.local_branches(self.repo))

    def test_dirty_worktree_refuses(self):
        target = self._worktree("feat", "feature/x")
        (target / "dirty.txt").write_text("uncommitted\n")
        verdict = retire_module.worktree_assessment(self.repo, target)
        self.assertFalse(verdict["retireable"])
        self.assertIn("uncommitted", verdict["reason"])

    def test_main_checkout_never_retires(self):
        verdict = retire_module.worktree_assessment(self.repo, self.repo)
        self.assertFalse(verdict["retireable"])
        self.assertIn("main checkout", verdict["reason"])

    def test_unmerged_branch_worktree_refuses(self):
        target = self._worktree("feat", "feature/x")
        (target / "work.txt").write_text("wip\n")
        _git(target, "add", "work.txt")
        _git(target, "commit", "-qm", "wip")
        verdict = retire_module.worktree_assessment(self.repo, target)
        self.assertFalse(verdict["retireable"])
        self.assertIn("feature/x", verdict["reason"])

    def test_claimed_checkout_refuses(self):
        target = self._worktree("feat", "feature/x")
        records = [_claim("ses_other", self.name, kind="worktree",
                          checkout=str(target), branch="feature/x")]
        verdict = retire_module.worktree_assessment(
            self.repo, target, claim_records=records,
            requesting_session="ses_mine")
        self.assertFalse(verdict["retireable"])
        self.assertIn("live claims", verdict["reason"])

    def test_missing_directory_needs_prune(self):
        import shutil

        target = self._worktree("feat", "feature/x")
        shutil.rmtree(target)
        verdict = retire_module.worktree_assessment(self.repo, target)
        self.assertFalse(verdict["retireable"])
        self.assertIn("prune", verdict["reason"])
        pruned = retire_module.prune_worktrees(self.repo)
        self.assertTrue(pruned["retired"], pruned)
        paths = [row["path"] for row in retire_module.worktrees(self.repo)]
        self.assertNotIn(str(target), paths)

    def test_dry_run_changes_nothing(self):
        target = self._worktree("feat", "feature/x")
        _git(self.repo, "worktree", "remove", str(target))
        before = retire_module.local_branches(self.repo)
        result = retire_module.retire_branch(self.repo, "feature/x", dry_run=True)
        self.assertFalse(result["retired"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(retire_module.local_branches(self.repo), before)


if __name__ == "__main__":
    unittest.main()
