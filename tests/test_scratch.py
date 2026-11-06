"""Disposable workspaces survive continuation and retire at terminal lifecycle."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from mncs_env import native_readiness, scratch
from mncs_env.session_store import FileSessionStore
from mncs_env.sessions import LifecycleError, Session


class ScratchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mncs-scratch-test-")
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name)
        self.store = FileSessionStore(self.state)
        self.store.save_snapshot("ses_test", {
            "session_id": "ses_test", "consumer_id": "scratch-test", "lifecycle": "active",
            "authenticated_principal_id": "principal-test", "lifecycle_history": [],
        })
        self.session = Session(self.store, "ses_test")

    def test_allocation_is_private_idempotent_and_survives_reopen(self):
        first = self.session.allocate_scratch("cargo")
        selected = Path(first["path"])
        self.assertEqual(selected.stat().st_mode & 0o777, 0o700)
        self.assertNotIn("token", first)
        (selected / "build.o").write_bytes(b"disposable")
        reopened = Session(FileSessionStore(self.state), "ses_test")
        self.assertEqual(reopened.allocate_scratch("cargo")["resource_id"], first["resource_id"])
        reopened.checkpoint(progress="keep building", remaining=["finish"])
        self.assertTrue((selected / "build.o").exists())

    def test_completion_cleans_only_registered_workspaces_and_can_replay(self):
        allocated = self.session.allocate_scratch("experiment")
        selected = Path(allocated["path"])
        (selected / "target").mkdir()
        (selected / "target/output").write_bytes(b"object")
        evidence = self.state / "evidence.json"
        evidence.write_text("durable")
        unregistered = selected.parent / "unregistered"
        unregistered.mkdir()
        (unregistered / "keep").write_text("keep")
        completion = self.session.complete(outcome="done", request_id="finish-scratch")
        self.assertFalse(selected.exists())
        self.assertEqual(completion["scratch_cleanup"]["pending"], [])
        self.assertEqual(evidence.read_text(), "durable")
        self.assertTrue((unregistered / "keep").exists())
        self.assertEqual(self.session.complete(outcome="done", request_id="finish-scratch"), completion)
        with self.assertRaises(LifecycleError):
            self.session.allocate_scratch("late")

    def test_failure_cleans_owned_builds(self):
        selected = Path(self.session.allocate_scratch("failed-build")["path"])
        (selected / "partial.o").write_bytes(b"partial")
        result = self.session.fail(reason="compiler failed")
        self.assertFalse(selected.exists())
        self.assertEqual(result["scratch_cleanup"]["pending"], [])

    def test_visible_process_defers_cleanup_then_completion_retry_releases_it(self):
        selected = Path(self.session.allocate_scratch("running")["path"])
        worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], cwd=selected)
        try:
            completion = self.session.complete(outcome="done", request_id="running-finish")
            self.assertTrue(selected.exists())
            self.assertEqual(completion["scratch_cleanup"]["pending"][0]["pids"], [worker.pid])
        finally:
            worker.terminate()
            worker.wait(timeout=5)
        reopened = Session(FileSessionStore(self.state), "ses_test")
        result = reopened.complete(retry_pending=True)
        self.assertFalse(selected.exists())
        self.assertEqual(result["scratch_cleanup"]["pending"], [])

    def test_replaced_directory_and_changed_marker_are_preserved(self):
        allocated = self.session.allocate_scratch("replaced")
        selected = Path(allocated["path"])
        moved = selected.with_name("original")
        selected.rename(moved)
        selected.mkdir(mode=0o700)
        (selected / scratch.MARKER).write_bytes((moved / scratch.MARKER).read_bytes())
        report = self.session.cleanup_scratch()
        self.assertIn("identity changed", report["pending"][0]["reason"])
        self.assertTrue(selected.exists())
        allocated = self.session.allocate_scratch("marker")
        marker = Path(allocated["path"]) / scratch.MARKER
        marker.write_text("{}")
        report = self.session.cleanup_scratch()
        self.assertTrue(any("ownership marker" in row["reason"] for row in report["pending"]))

    def test_device_number_alias_keeps_inode_and_private_root_checks(self):
        allocated = self.session.allocate_scratch("device-alias")
        selected = Path(allocated["path"])
        record = self.session.snapshot["scratch_workspaces"]["device-alias"]
        record["device"] += 1000
        self.session._save()

        report = self.session.cleanup_scratch()
        self.assertEqual(report["pending"], [])
        self.assertIn(record["resource_id"], report["released"])
        self.assertFalse(selected.exists())

    def test_device_number_alias_rejects_a_different_session_root_filesystem(self):
        allocated = self.session.allocate_scratch("device-mount")
        selected = Path(allocated["path"])
        record = self.session.snapshot["scratch_workspaces"]["device-mount"]
        record["device"] += 1000
        real_lstat = Path.lstat
        alternate_parent = SimpleNamespace(
            st_mode=stat.S_IFDIR | 0o700,
            st_uid=os.getuid(),
            st_dev=record["device"],
        )

        def lstat(path):
            if path == selected.parent:
                return alternate_parent
            return real_lstat(path)

        with mock.patch.object(Path, "lstat", autospec=True, side_effect=lstat):
            with self.assertRaisesRegex(ValueError, "filesystem identity changed"):
                scratch.verify(self.state, "ses_test", record)

    def test_symlink_workspace_does_not_delete_its_target(self):
        selected = Path(self.session.allocate_scratch("linked")["path"])
        selected.rename(selected.with_name("original"))
        foreign = self.state / "foreign"
        foreign.mkdir()
        (foreign / "keep").write_text("keep")
        selected.symlink_to(foreign, target_is_directory=True)
        report = self.session.cleanup_scratch()
        self.assertEqual(report["pending"][0]["status"], "preserved")
        self.assertEqual((foreign / "keep").read_text(), "keep")

    def test_interrupted_publication_keeps_a_recoverable_ownership_intent(self):
        original = self.session._save
        calls = 0
        def interrupt_ready_save():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt("publication interrupted")
            original()
        with mock.patch.object(self.session, "_save", side_effect=interrupt_ready_save):
            with self.assertRaises(KeyboardInterrupt):
                self.session.allocate_scratch("interrupted")
        reopened = Session(FileSessionStore(self.state), "ses_test")
        record = reopened.scratch_workspaces()["workspaces"][0]
        self.assertEqual(record["status"], "allocating")
        self.assertTrue(Path(record["path"]).exists())
        result = reopened.complete(outcome="cancelled")
        self.assertEqual(result["scratch_cleanup"]["pending"], [])
        self.assertFalse(Path(record["path"]).exists())

    def test_principal_label_and_namespace_guards(self):
        with self.assertRaises(LifecycleError):
            self.session.allocate_scratch("cargo", authenticated_principal_id="foreign")
        with self.assertRaises(ValueError):
            self.session.allocate_scratch("../outside")
        outside = self.state / "outside"
        outside.mkdir()
        (self.state / "scratch").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.session.allocate_scratch("cargo")
        self.assertEqual(list(outside.iterdir()), [])

    def test_cli_uses_the_persisted_session_from_another_cwd(self):
        script = Path(__file__).resolve().parents[1] / "scripts/mncs-env"
        prefix = [sys.executable, str(script), "--state-dir", str(self.state), "--persistence", "file"]
        result = subprocess.run([*prefix, "scratch", "ses_test", "--allocate", "cli"],
                                cwd="/tmp", capture_output=True, text=True, check=True)
        selected = Path(json.loads(result.stdout)["path"])
        self.assertTrue(selected.exists())
        subprocess.run([*prefix, "complete", "ses_test", "--outcome", "done"],
                       cwd="/tmp", capture_output=True, text=True, check=True)
        self.assertFalse(selected.exists())

    def test_native_descriptors_share_one_toolchain_storage_budget(self):
        commands = []
        session = SimpleNamespace(store=SimpleNamespace(state_dir=self.state),
                                  _selected_runtime_environment=lambda _: {})
        def execute(command, **options):
            commands.append((command, options["env"]))
            Path(options["cwd"], "result.json").write_text(json.dumps({"schema_version": "fixture/1"}))
            return subprocess.CompletedProcess(command, 0, "", "")
        with (mock.patch.dict(os.environ, {}, clear=True),
              mock.patch.object(native_readiness, "_descriptor", return_value={}),
              mock.patch.object(native_readiness.subprocess, "run", side_effect=execute)):
            for name in ["readiness-summary.json", "readiness-tokens.json"]:
                native_readiness._run_native_application(
                    session=session, binary=Path("/selected/mncs"), stdlib=Path("/selected/library"),
                    descriptor_name=name, request={}, expected_schema="fixture/1", timeout_seconds=1,
                )
        roots = [Path(command[command.index("--cache-dir") + 1]) for command, _ in commands]
        self.assertEqual(roots[0], roots[1])
        self.assertEqual(len(roots[0].name), 16)
        for _, environment in commands:
            self.assertEqual(environment["MNCS_NATIVE_APPLICATION_CACHE_STORAGE_ROOT"], str(roots[0].parent))


if __name__ == "__main__":
    unittest.main()
