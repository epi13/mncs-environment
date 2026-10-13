"""Retirement only removes proven-unneeded branches and worktrees."""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock

from mncs_env import claims as claims_module
from mncs_env import cli as cli_module
from mncs_env import retire as retire_module
from mncs_env import sessions as sessions_module
from mncs_env.session_store import open_store


class SimulatedProcessExit(BaseException):
    """Stop between Git mutation and durable receipt publication."""


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, timeout=60, check=False)


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


def _nested_namespace_limit(
    result: dict[str, Any] | subprocess.CompletedProcess[str],
) -> bool:
    reason = (
        str(result.get("reason", ""))
        if isinstance(result, dict)
        else result.stderr
    )
    return (
        "bwrap: Creating new namespace failed" in reason
        and "ENOSPC" in reason
    )


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

    def _make_worktree_retireable(self, branch: str) -> None:
        pushed = _git(self.repo, "push", "-q", "origin", f"{branch}:main")
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        fetched = _git(self.repo, "fetch", "-q", "origin")
        self.assertEqual(fetched.returncode, 0, fetched.stderr)

    def _protected_alias(self, target: Path) -> Path:
        """Model a checkout whose Git admin pointers were written in bwrap."""
        gitfile = (target / ".git").read_text(encoding="utf-8").strip()
        self.assertTrue(gitfile.startswith("gitdir: "), gitfile)
        admin = Path(gitfile.removeprefix("gitdir: ")).resolve()
        sandbox_root = Path("/workspace") / self.repo.name
        sandbox_target = sandbox_root / target.relative_to(self.repo)
        sandbox_admin = sandbox_root / ".git" / "worktrees" / admin.name
        (admin / "gitdir").write_text(
            str(sandbox_target / ".git") + "\n", encoding="utf-8",
        )
        (target / ".git").write_text(
            f"gitdir: {sandbox_admin}\n", encoding="utf-8",
        )
        return sandbox_root

    def _retirement_cli_args(self, target: Path, request_id: str) -> argparse.Namespace:
        session_id = "ses_retirement_restart_test"
        state_dir = self.base / "environment-state"
        store = open_store(state_dir, "file")
        store.save_snapshot(session_id, {
            "schema_version": "mncs.environment.session/5",
            "session_id": session_id,
            "consumer_id": "consumer-retirement-test",
            "authenticated_principal_id": "principal-retirement-test",
            "lifecycle": "active",
            "snapshot_sequence": 1,
            "workspace": {"root": str(self.base)},
            "campaign": {"identity": "cmp_retirement_restart_test"},
        })
        return argparse.Namespace(
            session=session_id, repository=self.name, canonical="origin/main",
            execution_root=None, sandbox_root=None, inventory=False,
            dry_run=False, prune=False, branch=[], worktree=[str(target)],
            state_dir=state_dir, persistence="file", request_id=request_id,
            func=cli_module.cmd_retire,
        )

    def _invoke_retirement(self, args: argparse.Namespace,
                           request_id: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(
            sessions_module.Session, "check",
            return_value={"verdict": "allow", "reason": "fixture claim"},
        ), mock.patch.dict(os.environ, {"MNCS_ENV_RPC_REQUEST_ID": request_id}), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            result = cli_module.cmd_retire(args)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_retirement_replays_after_git_remove_before_receipt(self):
        target = self._worktree("restart-complete", "feature/restart-complete")
        self._make_worktree_retireable("feature/restart-complete")
        args = self._retirement_cli_args(target, "req_retire_after_remove")
        actual = retire_module.retire_worktree

        def interrupt_after_remove(*call_args, **kwargs):
            result = actual(*call_args, **kwargs)
            if result.get("retired") is not True:
                raise AssertionError(result)
            raise SimulatedProcessExit("caller exited after Git completed")

        with mock.patch.object(
            retire_module, "retire_worktree", side_effect=interrupt_after_remove,
        ), self.assertRaises(SimulatedProcessExit):
            self._invoke_retirement(args, "req_retire_after_remove")

        result, stdout, stderr = self._invoke_retirement(
            args, "req_retire_after_remove",
        )
        self.assertEqual(result, 0, stderr + stdout)
        response = json.loads(stdout)
        self.assertEqual(response["completion_status"], "completed")
        self.assertTrue(response["results"][0]["reconciled"])
        self.assertFalse(target.exists())
        restarted = sessions_module.Session.open(
            state_dir=args.state_dir, session_id=args.session, backend="file",
        )
        log = restarted._log()
        self.assertEqual(sum(event.get("type") ==
                             "environment.retirement.requested" for event in log), 1)
        self.assertEqual(sum(event.get("type") ==
                             "environment.retirement.target-completed" for event in log), 1)
        self.assertEqual(sum(event.get("type") ==
                             "environment.retirement.completed" for event in log), 1)
        with mock.patch.object(
            retire_module, "retire_worktree",
            side_effect=AssertionError("completed request must replay its receipt"),
        ):
            replay_result, replay_stdout, replay_stderr = self._invoke_retirement(
                args, "req_retire_after_remove",
            )
        self.assertEqual(replay_result, 0, replay_stderr)
        self.assertEqual(json.loads(replay_stdout), {
            "replayed_request": True, **response,
        })

    def test_retirement_retries_when_process_exits_before_git_mutation(self):
        target = self._worktree("restart-before", "feature/restart-before")
        self._make_worktree_retireable("feature/restart-before")
        args = self._retirement_cli_args(target, "req_retire_before_mutation")
        with mock.patch.object(
            retire_module, "retire_worktree",
            side_effect=SimulatedProcessExit("stopped before Git mutation"),
        ), self.assertRaises(SimulatedProcessExit):
            self._invoke_retirement(args, "req_retire_before_mutation")
        self.assertTrue(target.is_dir())

        result, stdout, stderr = self._invoke_retirement(
            args, "req_retire_before_mutation",
        )
        self.assertEqual(result, 0, stderr + stdout)
        self.assertFalse(target.exists())

    def test_retirement_retry_preserves_split_checkout_registration(self):
        target = self._worktree("restart-split", "feature/restart-split")
        self._make_worktree_retireable("feature/restart-split")
        args = self._retirement_cli_args(target, "req_retire_split_state")

        def interrupt_between_registration_and_checkout(*_args, **_kwargs):
            gitfile = (target / ".git").read_text(encoding="utf-8").strip()
            admin = Path(gitfile.removeprefix("gitdir: "))
            if not admin.is_absolute():
                admin = (target / admin).resolve()
            import shutil
            shutil.rmtree(admin)
            raise SimulatedProcessExit("registration disappeared before checkout")

        with mock.patch.object(
            retire_module, "retire_worktree",
            side_effect=interrupt_between_registration_and_checkout,
        ), self.assertRaises(SimulatedProcessExit):
            self._invoke_retirement(args, "req_retire_split_state")

        result, stdout, stderr = self._invoke_retirement(args, "req_retire_split_state")
        self.assertEqual(result, 3, stderr)
        response = json.loads(stdout)
        self.assertEqual(response["completion_status"], "pending")
        self.assertEqual(response["results"][0]["status"], "unknown")
        self.assertTrue(target.is_dir())

    def test_retirement_retry_does_not_remove_a_reused_checkout_path(self):
        target = self._worktree("restart-reused", "feature/restart-reused")
        self._make_worktree_retireable("feature/restart-reused")
        args = self._retirement_cli_args(target, "req_retire_reused_path")

        def replace_checkout(*_args, **_kwargs):
            import shutil
            gitfile = (target / ".git").read_text(encoding="utf-8").strip()
            admin = Path(gitfile.removeprefix("gitdir: "))
            if not admin.is_absolute():
                admin = (target / admin).resolve()
            shutil.rmtree(target)
            shutil.rmtree(admin)
            created = _git(self.repo, "worktree", "add", str(target),
                           "feature/restart-reused")
            if created.returncode != 0:
                raise AssertionError(created.stderr)
            raise SimulatedProcessExit("new checkout reused the old path")

        with mock.patch.object(
            retire_module, "retire_worktree", side_effect=replace_checkout,
        ), self.assertRaises(SimulatedProcessExit):
            self._invoke_retirement(args, "req_retire_reused_path")

        result, stdout, stderr = self._invoke_retirement(
            args, "req_retire_reused_path",
        )
        self.assertEqual(result, 3, stderr)
        response = json.loads(stdout)
        self.assertEqual(response["results"][0]["status"], "unknown")
        self.assertTrue(target.is_dir())
        self.assertIn("feature/restart-reused", retire_module.local_branches(self.repo))

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

    def test_branch_retirement_uses_compare_and_delete(self):
        target = self._worktree("cas", "feature/cas")
        _git(self.repo, "worktree", "remove", str(target))
        head = retire_module.local_branches(self.repo)["feature/cas"]
        tree = _git(self.repo, "rev-parse", "main^{tree}").stdout.strip()
        new_head = _git(self.repo, "commit-tree", tree, "-m", "concurrent branch update").stdout.strip()
        actual_git = retire_module._git

        def race_after_assessment(repo, *args, **kwargs):
            if args[:2] == ("update-ref", "-d"):
                raced = _git(self.repo, "update-ref", "refs/heads/feature/cas", new_head)
                self.assertEqual(raced.returncode, 0, raced.stderr)
            return actual_git(repo, *args, **kwargs)

        with mock.patch.object(retire_module, "_git", side_effect=race_after_assessment):
            result = retire_module.retire_branch(
                self.repo, "feature/cas", expected_head=head,
            )
        self.assertFalse(result["retired"])
        self.assertIn("git refused", result["reason"])
        self.assertEqual(retire_module.local_branches(self.repo)["feature/cas"], new_head)

    def test_cli_requires_retirement_authority_before_branch_mutation(self):
        class Session:
            def __init__(self):
                self.calls = []
                self.snapshot = {"consumer_id": "test-consumer", "campaign": {}}
                self.session_id = "ses_mine"

            def check(self, **kwargs):
                self.calls.append(kwargs)
                return {"verdict": "escalate", "reason": "claim required"}

            def request_event(self, *_args):
                return None

            def close(self):
                pass

        class Store:
            def read_claims(self):
                return []

        session = Session()
        args = argparse.Namespace(
            session="ses_mine", repository=self.name, canonical="origin/main",
            execution_root=None, inventory=False, dry_run=False, prune=False,
            branch=["feature/x"], worktree=[], state_dir=self.base,
            persistence="store", sandbox_root=None, request_id=None,
        )
        error = io.StringIO()
        with mock.patch.object(cli_module, "_open", return_value=session), \
                mock.patch.object(cli_module, "_resolve_retire_repo", return_value=self.repo), \
                mock.patch.object(cli_module, "open_store", return_value=Store()), \
                mock.patch.object(cli_module, "close_store"), \
                mock.patch.object(retire_module, "retire_branch") as retire_branch, \
                redirect_stderr(error):
            result = cli_module.cmd_retire(args)

        self.assertEqual(result, 3)
        self.assertEqual(session.calls, [{
            "action": "retire", "target": self.name,
            "scope": {"kind": "repository"},
        }])
        retire_branch.assert_not_called()
        self.assertIn('"mutation_performed": false', error.getvalue())

    def test_cli_authorizes_worktree_by_exact_checkout_scope(self):
        target = self._worktree("owned", "feature/owned")

        class Session:
            def __init__(self):
                self.calls = []
                self.events = []
                self.snapshot = {"consumer_id": "test-consumer", "campaign": {}}
                self.session_id = "ses_mine"

            def check(self, **kwargs):
                self.calls.append(kwargs)
                return {"verdict": "allow", "reason": "claim covers target"}

            def request_event(self, event_type, request_id, operation_digest):
                return next((event for event in self.events
                             if event["type"] == event_type
                             and event["payload"].get("request_id") == request_id
                             and event["payload"].get("request_operation_digest")
                             == operation_digest), None)

            def record_observation(self, event_type, producer, payload, *,
                                   request_id=None, operation_digest=None):
                event = {"type": event_type, "producer": producer, "payload": {
                    **payload, "request_id": request_id,
                    "request_operation_digest": operation_digest,
                    "request_event_digest": f"digest-{len(self.events)}",
                }}
                self.events.append(event)
                return event

            def close(self):
                pass

        class Store:
            def read_claims(self):
                return []

        session = Session()
        args = argparse.Namespace(
            session="ses_mine", repository=self.name, canonical="origin/main",
            execution_root=None, inventory=False, dry_run=False, prune=False,
            branch=[], worktree=[str(target)], state_dir=self.base,
            persistence="store", sandbox_root=None, request_id=None,
        )
        with mock.patch.object(cli_module, "_open", return_value=session), \
                mock.patch.object(cli_module, "_resolve_retire_repo", return_value=self.repo), \
                mock.patch.object(cli_module, "open_store", return_value=Store()), \
                mock.patch.object(cli_module, "close_store"), \
                mock.patch.object(retire_module, "retire_worktree", return_value={
                    "target": str(target), "kind": "worktree", "retired": True,
                    "status": "retired", "reason": "removed", "dry_run": False,
                }) as retire_worktree:
            result = cli_module.cmd_retire(args)

        self.assertEqual(result, 0)
        self.assertEqual(session.calls, [{
            "action": "retire", "target": self.name,
            "scope": {"kind": "worktree", "checkout": str(target.resolve())},
        }] * 2)
        retire_worktree.assert_called_once()

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
        self.assertEqual(result["status"], "retired")
        self.assertFalse(target.exists())
        # The branch itself is kept; only the checkout is removed.
        self.assertIn("feature/x", retire_module.local_branches(self.repo))

    def test_protected_git_registration_maps_to_host_checkout(self):
        target = self._worktree("mapped", "feature/mapped")
        sandbox_root = self._protected_alias(target)
        verdict = retire_module.worktree_assessment(
            self.repo, target, sandbox_root=sandbox_root,
            execution_root=self.repo,
        )
        report = retire_module.inventory_repository(
            self.repo, workspace_root=self.base,
            execution_root=self.repo, sandbox_root=sandbox_root,
        )
        mapped = next(row for row in report["worktrees"] if row["path"] == str(target))
        self.assertEqual(mapped["registered_path"], "/workspace/demo/.worktrees/mapped")
        self.assertEqual(mapped["checkout_identity_basis"], "git-administrative-identity")
        if _nested_namespace_limit(verdict):
            self.assertEqual(verdict["status"], "unknown", verdict)
            self.assertFalse(verdict["retireable"], verdict)
            self.assertEqual(
                mapped["classification"],
                "unavailable-in-current-execution-namespace",
            )
            self.assertTrue(target.exists())
            return
        self.assertTrue(verdict["retireable"], verdict)
        self.assertEqual(mapped["classification"], "merged-and-safely-retireable")

    def test_protected_git_worktree_retires_inside_exact_project_alias(self):
        target = self._worktree("mapped-retire", "feature/mapped-retire")
        sandbox_root = self._protected_alias(target)
        identity = retire_module.retirement_target_state(
            self.repo, "worktree", str(target), sandbox_root=sandbox_root,
            execution_root=self.repo,
        )
        self.assertEqual(identity["status"], "available", identity)

        result = retire_module.retire_worktree(
            self.repo, target, sandbox_root=sandbox_root,
            execution_root=self.repo, expected_identity=identity["identity"],
        )

        if _nested_namespace_limit(result):
            self.assertFalse(result["retired"], result)
            self.assertEqual(result["status"], "unknown", result)
            self.assertTrue(target.exists())
            self.assertIn("feature/mapped-retire", retire_module.local_branches(self.repo))
            self.assertIn(
                "/workspace/demo/.worktrees/mapped-retire",
                [row["path"] for row in retire_module.worktrees(self.repo)],
            )
            return
        self.assertTrue(result["retired"], result)
        self.assertEqual(result["status"], "retired")
        self.assertFalse(target.exists())
        self.assertIn("feature/mapped-retire", retire_module.local_branches(self.repo))
        self.assertEqual(
            [row for row in retire_module.worktrees(self.repo)
             if row.get("path") == "/workspace/demo/.worktrees/mapped-retire"],
            [],
        )

    def test_protected_git_status_preserves_dirty_and_ignored_files(self):
        _git(self.repo, "config", "core.excludesFile", "")
        (self.repo / ".gitignore").write_text("ignored-output/\n", encoding="utf-8")
        _git(self.repo, "add", ".gitignore")
        _git(self.repo, "commit", "-qm", "ignore test output")
        _git(self.repo, "push", "-q", "origin", "main")
        _git(self.repo, "fetch", "-q", "origin")
        target = self._worktree("mapped-dirty", "feature/mapped-dirty")
        sandbox_root = self._protected_alias(target)
        (target / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
        dirty = retire_module.worktree_assessment(
            self.repo, target, sandbox_root=sandbox_root,
            execution_root=self.repo,
        )
        if _nested_namespace_limit(dirty):
            self.assertFalse(dirty["retireable"], dirty)
            self.assertEqual(dirty["status"], "unknown", dirty)
            self.assertTrue((target / "dirty.txt").exists())
            (target / "dirty.txt").unlink()
            ignored = target / "ignored-output" / "keep.txt"
            ignored.parent.mkdir()
            ignored.write_text("retained\n", encoding="utf-8")
            protected = retire_module.worktree_assessment(
                self.repo, target, sandbox_root=sandbox_root,
                execution_root=self.repo,
            )
            self.assertFalse(protected["retireable"], protected)
            self.assertEqual(protected["status"], "unknown", protected)
            self.assertTrue(ignored.exists())
            self.assertTrue(target.exists())
            return
        self.assertFalse(dirty["retireable"])
        self.assertIn("uncommitted, untracked, or ignored", dirty["reason"])

        (target / "dirty.txt").unlink()
        ignored = target / "ignored-output" / "keep.txt"
        ignored.parent.mkdir()
        ignored.write_text("retained\n", encoding="utf-8")
        protected = retire_module.worktree_assessment(
            self.repo, target, sandbox_root=sandbox_root,
            execution_root=self.repo,
        )
        self.assertFalse(protected["retireable"])
        self.assertIn("uncommitted, untracked, or ignored", protected["reason"])
        self.assertTrue(ignored.exists())

    def test_protected_git_status_is_unknown_when_bubblewrap_is_unavailable(self):
        target = self._worktree("mapped-unavailable", "feature/mapped-unavailable")
        sandbox_root = self._protected_alias(target)
        with mock.patch.object(retire_module.shutil, "which", return_value=None):
            verdict = retire_module.worktree_assessment(
                self.repo, target, sandbox_root=sandbox_root,
                execution_root=self.repo,
            )
        self.assertFalse(verdict["retireable"])
        self.assertEqual(verdict["status"], "unknown")
        self.assertIn("bubblewrap is unavailable", verdict["reason"])

    def test_restricted_git_namespace_cannot_access_home_store_or_fails_closed(self):
        git_launcher = retire_module.shutil.which("env", path="/usr/bin:/bin")
        self.assertIsNotNone(git_launcher)
        with mock.patch.object(
            retire_module.workspace_module, "git_binary", return_value=git_launcher,
        ):
            result = retire_module._execution_git(
                self.repo,
                Path("/workspace/demo"),
                self.repo,
                "python3", "-c",
                "import os; assert not os.path.exists('/home'); "
                "assert not os.path.exists('/home/epi13/.local/share/mncs-environment/store')",
            )
        if _nested_namespace_limit(result):
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("max_*_namespaces exceeded", result.stderr)
            return
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unavailable_mapped_checkout_is_preserved_for_reconciliation(self):
        rows = retire_module.worktrees(self.repo)
        head = _git(self.repo, "rev-parse", "main").stdout.strip()
        rows.append({
            "path": "/workspace/demo/.worktrees/unavailable",
            "branch": None,
            "head": head,
            "detached": True,
            "locked": False,
            "prunable_reason": None,
        })
        target = self.repo / ".worktrees" / "unavailable"
        with mock.patch.object(retire_module, "worktrees", return_value=rows):
            result = retire_module.retire_worktree(
                self.repo, target, sandbox_root=Path("/workspace/demo"),
                execution_root=self.repo,
            )
            report = retire_module.inventory_repository(
                self.repo, workspace_root=self.base,
                execution_root=self.repo, sandbox_root=Path("/workspace/demo"),
            )
        self.assertFalse(result["retired"])
        self.assertEqual(result["status"], "unknown")
        self.assertIn("unavailable in the Environment service namespace", result["reason"])
        checkout = next(row for row in report["worktrees"] if row["path"] == str(target))
        self.assertEqual(checkout["classification"], "unavailable-in-current-execution-namespace")
        self.assertFalse(checkout["path_available"])

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

    def test_inventory_is_read_only_and_classifies_unique_work(self):
        target = self._worktree("feat", "feature/x")
        (target / "implementation.txt").write_text("candidate work\n")
        _git(target, "add", "implementation.txt")
        _git(target, "commit", "-qm", "candidate implementation")
        report = retire_module.inventory_repository(
            self.repo, workspace_root=self.base,
        )
        branch = next(row for row in report["branches"] if row["branch"] == "feature/x")
        checkout = next(row for row in report["worktrees"] if row["path"] == str(target))
        self.assertEqual(report["schema_version"], "mncs.environment.worktree-inventory/1")
        self.assertTrue(report["dry_run"])
        self.assertFalse(report["mutation_performed"])
        self.assertEqual(branch["classification"], "unmerged-with-unique-commits")
        self.assertEqual(checkout["classification"], "unmerged-work-requires-review")
        self.assertEqual(checkout["checkout_identity_basis"], "git-administrative-identity")
        self.assertTrue(checkout["checkout_identity"].startswith("git-worktree:"))
        self.assertTrue(target.is_dir())
        self.assertIn("feature/x", retire_module.local_branches(self.repo))

    def test_inventory_protects_foreign_claim_and_reports_unavailable_scope(self):
        target = self._worktree("protected", "feature/protected")
        records = [_claim("ses_foreign", self.name, kind="worktree",
                          checkout=str(target), branch="feature/protected")]
        report = retire_module.inventory_repository(
            self.repo, claim_records=records, requesting_session="ses_current",
            workspace_root=self.base,
        )
        checkout = next(row for row in report["worktrees"] if row["path"] == str(target))
        self.assertEqual(checkout["classification"], "protected-by-another-consumer")
        self.assertEqual(checkout["live_claims"][0]["session_id"], "ses_foreign")

        scoped = retire_module.inventory_repository(
            self.repo, workspace_root=self.base, execution_root=self.base / "unrelated-root",
        )
        checkout = next(row for row in scoped["worktrees"] if row["path"] == str(target))
        self.assertEqual(checkout["classification"], "outside-current-execution-scope")
        self.assertIsNone(checkout["clean"])
        self.assertIsNone(checkout["path_available"])
        self.assertEqual(checkout["access_disposition"], "outside-current-execution-scope")
        original = next(row for row in report["worktrees"] if row["path"] == str(target))
        self.assertEqual(checkout["checkout_identity"], original["checkout_identity"])
        self.assertIn("feature/protected", retire_module.local_branches(self.repo))

    def test_inventory_keeps_unchecked_branch_when_any_foreign_repo_claim_is_live(self):
        retired_candidate = self._worktree("candidate", "feature/candidate")
        _git(self.repo, "worktree", "remove", str(retired_candidate))
        foreign_checkout = self._worktree("foreign", "feature/foreign")
        records = [_claim("ses_foreign", self.name, kind="worktree",
                          checkout=str(foreign_checkout), branch="feature/foreign")]
        report = retire_module.inventory_repository(
            self.repo, claim_records=records, requesting_session="ses_current",
            workspace_root=self.base,
        )
        branch = next(row for row in report["branches"]
                      if row["branch"] == "feature/candidate")
        self.assertEqual(branch["classification"], "protected-by-another-consumer")
        self.assertFalse(branch["retireable_after_review"])

    def test_inventory_does_not_probe_checkouts_already_protected_by_repo_claim(self):
        records = [_claim("ses_foreign", self.name)]
        with mock.patch.object(
            retire_module, "_worktree_status",
            side_effect=AssertionError("checkout probed"),
        ):
            report = retire_module.inventory_repository(
                self.repo, claim_records=records, requesting_session="ses_current",
                workspace_root=self.base,
            )
        self.assertTrue(report["worktrees"])
        self.assertTrue(all(row["classification"] == "protected-by-another-consumer"
                            for row in report["worktrees"]))
        self.assertTrue(all(row["clean"] is None for row in report["worktrees"]))
        self.assertTrue(all(row["access_disposition"] == "not-probed-protected"
                            for row in report["worktrees"]))

    def test_inventory_identifies_detached_checkout_with_evidence_tip(self):
        target = self.base / "detached-evidence"
        added = _git(self.repo, "worktree", "add", "--detach", str(target), "main")
        self.assertEqual(added.returncode, 0, added.stderr)
        evidence = target / "evidence" / "proof.json"
        evidence.parent.mkdir()
        evidence.write_text("{}\n")
        _git(target, "add", "evidence/proof.json")
        _git(target, "commit", "-qm", "retain proof evidence")
        report = retire_module.inventory_repository(
            self.repo, workspace_root=self.base,
        )
        checkout = next(row for row in report["worktrees"] if row["path"] == str(target))
        self.assertTrue(checkout["detached"])
        self.assertEqual(checkout["classification"], "detached-but-evidence-relevant")
        self.assertTrue(checkout["evidence_relevant"])
        self.assertTrue(target.is_dir())

    def test_inventory_retains_patch_equivalent_evidence_reference(self):
        candidate = self._worktree("evidence-candidate", "feature/evidence")
        evidence = candidate / "evidence" / "proof.json"
        evidence.parent.mkdir()
        evidence.write_text('{"proof":true}\n')
        _git(candidate, "add", "evidence/proof.json")
        _git(candidate, "commit", "-qm", "candidate evidence")
        _git(self.repo, "worktree", "remove", str(candidate))
        main_evidence = self.repo / "evidence" / "proof.json"
        main_evidence.parent.mkdir()
        main_evidence.write_text('{"proof":true}\n')
        _git(self.repo, "add", "evidence/proof.json")
        _git(self.repo, "commit", "-qm", "delivered evidence")
        _git(self.repo, "push", "-q", "origin", "main")
        _git(self.repo, "fetch", "-q", "origin")
        report = retire_module.inventory_repository(
            self.repo, workspace_root=self.base,
        )
        branch = next(row for row in report["branches"]
                      if row["branch"] == "feature/evidence")
        self.assertEqual(branch["unique_patch_commit_count"], 0)
        self.assertEqual(branch["classification"], "merged-and-retained-for-evidence")
        self.assertFalse(branch["retireable_after_review"])
        self.assertEqual(branch["evidence_paths_at_tip"], ["evidence/proof.json"])


if __name__ == "__main__":
    unittest.main()
