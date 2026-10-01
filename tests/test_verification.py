"""Ambient verification coherence: lifecycle around native policy.

Fake sessions drive ambient_pass with stubbed provider invocations, so
measurement/epoch/evidence/queue behavior is exercised hermetically.
The native coherence policy itself is covered in mncs-test; live
composition against the real workspace is exercised as scenarios.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mncs_env import verification as verification_module  # noqa: E402


SUITE_SOURCE = 'mncs 0.18;\nmodule fixture.suite;\n'


def git(repo: Path, *argv: str) -> None:
    subprocess.run(["git", "-C", str(repo), *argv], check=True,
                   capture_output=True, text=True, timeout=60)


def git_init(repo: Path) -> None:
    for argv in (["init", "-q", "-b", "main"], ["add", "."],
                 ["-c", "user.name=Verification Test",
                  "-c", "user.email=verification@example.invalid",
                  "commit", "-qm", "fixture"]):
        git(repo, *argv)


class FakeStore:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir


class FakeSession:
    def __init__(self, workspace_root: Path, state_dir: Path):
        self.session_id = "ses_verify_fixture"
        self.snapshot: dict = {
            "workspace": {"root": str(workspace_root)},
            "lifecycle": "active",
            "consumer_id": "verify-test",
            "toolchain": {"binary": "/bin/mncs", "revision": "rev-1",
                          "checkout": "/chk", "repository": "mncs-language"},
            "bindings": [
                {"capability": verification_module.COHERENCE_CAPABILITY,
                 "availability": {"status": "available"}},
                {"capability": verification_module.TEST_CAPABILITY,
                 "availability": {"status": "available"}},
            ],
        }
        self.store = FakeStore(state_dir)
        self.saved = 0
        self.coherence_calls: list[dict] = []
        self.executions: list[list[str]] = []
        self.emitted: list[tuple[str, dict]] = []
        self.coherence_mode = "queue-fresh"
        self.execution_mode = "pass"

    def _emit(self, event_type: str, producer: str,
              payload: dict | None = None, causes=None) -> dict:
        self.emitted.append((event_type, dict(payload or {})))
        return {"type": event_type}

    def _binding(self, capability: str) -> dict:
        for binding in self.snapshot.get("bindings", []):
            if binding.get("capability") == capability:
                return binding
        raise KeyError(capability)

    def _save(self) -> None:
        self.saved += 1

    def invoke(self, capability: str, argv: list[str], *,
               cwd=None, timeout_seconds=None,
               output_limit_bytes=None, env=None) -> dict:
        if capability == verification_module.COHERENCE_CAPABILITY:
            return self._invoke_coherence(argv, cwd)
        if capability == verification_module.TEST_CAPABILITY:
            return self._invoke_test(argv, cwd, env)
        raise AssertionError(f"unexpected capability {capability}")

    def _invoke_coherence(self, argv: list[str], cwd) -> dict:
        workdir = Path(cwd)
        request = json.loads((workdir / argv[0]).read_text(encoding="utf-8"))
        self.coherence_calls.append(request)
        if self.coherence_mode == "fail":
            return {"status": "error", "stderr": "stub coherence failure"}
        verdicts = []
        queue = []
        for item in request["obligations"]:
            if not item["evidence_present"]:
                if "native_first_class_test" in item["executor_kind"] and item["runnable_native"]:
                    verdicts.append({"identity": item["identity"],
                                     "status": "new_execution_required",
                                     "reason": "no_evidence", "deferred": False,
                                     "verdict_known": False, "verdict": "UNKNOWN",
                                     "evidence_id": "",
                                     "resolved_test_identities": ["t-case-1"],
                                     "unresolved_count": 0})
                    queue.append(item["identity"])
                else:
                    verdicts.append({"identity": item["identity"],
                                     "status": "not_selected",
                                     "reason": "executor_not_runnable",
                                     "deferred": False, "verdict_known": False,
                                     "verdict": "UNKNOWN", "evidence_id": "",
                                     "resolved_test_identities": [],
                                     "unresolved_count": 0})
            else:
                admitted = (
                    item["evidence"]["subject_fingerprint"]
                    and item["evidence"]["subject_fingerprint"]
                    == item["current"]["subject_fingerprint"]
                )
                if admitted:
                    verdicts.append({"identity": item["identity"],
                                     "status": "current",
                                     "reason": "evidence_current",
                                     "deferred": False,
                                     "verdict_known": True,
                                     "verdict": item["evidence"]["verdict"],
                                     "evidence_id": item["evidence"]["evidence_id"],
                                     "resolved_test_identities": [],
                                     "unresolved_count": 0})
                else:
                    verdicts.append({"identity": item["identity"],
                                     "status": "stale",
                                     "reason": "input_changed",
                                     "deferred": False,
                                     "verdict_known": True,
                                     "verdict": item["evidence"]["verdict"],
                                     "evidence_id": item["evidence"]["evidence_id"],
                                     "resolved_test_identities": ["t-case-1"],
                                     "unresolved_count": 0})
                    queue.append(item["identity"])
        result = {"schema_version": verification_module.COHERENCE_RESULT_SCHEMA,
                  "verdicts": verdicts, "run_queue": queue[:request["max_executions"]],
                  "summary": {"obligations": len(verdicts),
                              "current": sum(1 for verdict in verdicts
                                             if verdict["status"] == "current"),
                              "queued": len(queue), "deferred": 0,
                              "failed": 0, "unknown": 0,
                              "contradictory": 0,
                              "unsupported": sum(1 for verdict in verdicts
                                                 if verdict["reason"] == "executor_not_runnable"),
                              "unresolved": 0, "excluded": 0}}
        (workdir / argv[1]).write_text(json.dumps(result), encoding="utf-8")
        return {"status": "ok", "stdout": "", "stderr": ""}

    def _invoke_test(self, argv: list[str], cwd, env) -> dict:
        workdir = Path(cwd)
        self.executions.append(list(argv))
        self.last_test_env = dict(env or {})
        if self.execution_mode == "transport-error":
            raise OSError("stub transport failure")
        if self.execution_mode == "fail":
            verdict, failure_class = "FAIL", "test_failure"
        else:
            verdict, failure_class = "PASS", "none"
        document = {
            "schema_version": "mncs.test-result/1",
            "verdict": verdict,
            "classification": "passed" if verdict == "PASS" else "failed",
            "failure_class": failure_class,
            "execution": {"test_case_identities": ["t-case-1"],
                          "inventory_identity": "inv-1",
                          "run_identity": "run-1"},
            "native_suite_summary": {"verdict": verdict, "total": 1,
                                     "passed": 1 if verdict == "PASS" else 0,
                                     "failed": 0 if verdict == "PASS" else 1},
            "tests": [{"id": "t-case-1", "entry": "case",
                       "verdict": verdict, "status": "passed"}],
        }
        (workdir / "result.json").write_text(json.dumps(document), encoding="utf-8")
        return {"status": "ok", "stdout": "", "stderr": ""}


OBLIGATION = {
    "identity": "fixture.native-suite",
    "title": "Fixture native suite stays verified",
    "lifecycle": "permanent",
    "scope": "repository_canonical",
    "invalidation_dependencies": ["tests/suite.mncs"],
    "executor": {"provider": "mncs-test", "kind": "native_first_class_test",
                 "entrypoint": "mncs-test run",
                 "source_paths": ["tests/suite.mncs"],
                 "library_paths": [],
                 "declaration_identities": ["*"],
                 "verifier_identity": "mncs-test-runner/0.2.1"},
}

HOST_OBLIGATION = {
    "identity": "fixture.host-suite",
    "title": "Host suite is out of ambient scope",
    "lifecycle": "permanent",
    "scope": "repository_canonical",
    "invalidation_dependencies": [],
    "executor": {"provider": "mncs-test", "kind": "external_integration",
                 "entrypoint": "pytest tests/"},
}


class VerificationFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mncs-verification-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.workspace = self.base / "work"
        self.workspace.mkdir()
        self.repo = self.workspace / "fixture-repo"
        (self.repo / ".mncs").mkdir(parents=True)
        (self.repo / "tests").mkdir(parents=True)
        (self.repo / "tests" / "suite.mncs").write_text(SUITE_SOURCE)
        (self.repo / ".mncs" / "project.json").write_text(json.dumps({
            "schema_version": "mncs-family.repository-manifest/v0alpha1",
            "repository": "fixture-repo",
            "verification": {"obligation_inventory": ".mncs/obligations.json"}}))
        self.write_obligations([OBLIGATION])
        git_init(self.repo)
        self.state = self.base / "state"
        self.session = FakeSession(self.workspace, self.state)

    def write_obligations(self, obligations: list[dict]) -> None:
        (self.repo / ".mncs" / "obligations.json").write_text(json.dumps({
            "schema_version": "mncs-family.verification-obligation-inventory/v1",
            "repository": "fixture-repo", "revision": 1,
            "obligations": obligations}))

    def commit_all(self, message: str) -> None:
        git(self.repo, "add", ".")
        git(self.repo, "-c", "user.name=Verification Test",
            "-c", "user.email=verification@example.invalid",
            "commit", "-qm", message)


class DiscoveryTests(VerificationFixture):
    def test_discovers_native_obligation(self):
        obligations, invalid = verification_module.discover_obligations(self.workspace)
        self.assertEqual(invalid, [])
        self.assertEqual([item["declaration"]["identity"] for item in obligations],
                         ["fixture.native-suite"])

    def test_invalid_obligation_reported_not_run(self):
        broken = dict(OBLIGATION)
        broken["executor"] = {"kind": "nope"}
        self.write_obligations([broken])
        obligations, invalid = verification_module.discover_obligations(self.workspace)
        self.assertEqual(obligations, [])
        self.assertEqual(len(invalid), 1)
        self.assertEqual(invalid[0]["reason"], "executor-kind-unknown")


class AmbientPassTests(VerificationFixture):
    def test_first_pass_executes_and_records(self):
        outcome = verification_module.ambient_pass(self.session)
        summary = outcome["summary"]
        self.assertFalse(outcome["reused"])
        self.assertEqual(summary["executed"], 1)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["blockers"], 0)
        self.assertEqual(len(self.session.executions), 1)
        rows = self.session.snapshot["verification_state"]
        row = rows["fixture.native-suite"]
        self.assertEqual(row["verdict"], "PASS")
        self.assertTrue(row["evidence"]["evidence_id"])
        self.assertEqual(row["inventory_test_identities"], ["t-case-1"])

    def test_quiet_reentry_reuses_epoch(self):
        first = verification_module.ambient_pass(self.session)
        self.assertFalse(first["reused"])
        calls = len(self.session.coherence_calls)
        runs = len(self.session.executions)
        second = verification_module.ambient_pass(self.session)
        self.assertTrue(second["reused"])
        self.assertTrue(second["summary"]["epoch_reused"])
        self.assertEqual(len(self.session.coherence_calls), calls)
        self.assertEqual(len(self.session.executions), runs)

    def test_subject_edit_invalidates_only_affected(self):
        verification_module.ambient_pass(self.session)
        (self.repo / "tests" / "suite.mncs").write_text(SUITE_SOURCE + "# edit\n")
        outcome = verification_module.ambient_pass(self.session)
        self.assertFalse(outcome["reused"])
        self.assertEqual(outcome["summary"]["executed"], 1)
        self.assertEqual(len(self.session.executions), 2)

    def test_unrelated_repo_change_does_not_invalidate(self):
        self.write_obligations([OBLIGATION, HOST_OBLIGATION])
        verification_module.ambient_pass(self.session)
        other = self.workspace / "other-repo"
        other.mkdir()
        (other / "note.txt").write_text("unrelated")
        outcome = verification_module.ambient_pass(self.session)
        self.assertTrue(outcome["reused"])
        self.assertEqual(len(self.session.executions), 1)

    def test_host_obligation_unsupported_never_executes(self):
        self.write_obligations([OBLIGATION, HOST_OBLIGATION])
        outcome = verification_module.ambient_pass(self.session)
        self.assertEqual(outcome["summary"]["unsupported"], 1)
        self.assertEqual(outcome["summary"]["executed"], 1)
        self.assertEqual(len(self.session.executions), 1)

    def test_failed_suite_surfaces_delta(self):
        self.session.execution_mode = "fail"
        outcome = verification_module.ambient_pass(self.session)
        summary = outcome["summary"]
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["blockers"], 1)
        self.assertEqual(summary["failed_ids"], ["fixture.native-suite"])
        terse = verification_module.terse(self.session)
        self.assertEqual(terse["verification"]["failed"], 1)
        self.assertEqual(terse["verification"]["failed_ids"], ["fixture.native-suite"])
        self.assertTrue(terse["verification"]["evidence"])

    def test_recorded_fail_is_current_knowledge(self):
        self.session.execution_mode = "fail"
        verification_module.ambient_pass(self.session)
        self.session.execution_mode = "pass"
        second = verification_module.ambient_pass(self.session)
        self.assertTrue(second["reused"])
        self.assertEqual(second["summary"]["failed"], 1)

    def test_transport_failure_never_caches(self):
        self.session.execution_mode = "transport-error"
        first = verification_module.ambient_pass(self.session)
        self.assertEqual(first["summary"]["unknown"], 1)
        self.session.execution_mode = "pass"
        second = verification_module.ambient_pass(self.session)
        self.assertFalse(second["reused"])
        self.assertEqual(second["summary"]["executed"], 1)

    def test_coherence_failure_is_a_blocker(self):
        self.session.coherence_mode = "fail"
        outcome = verification_module.ambient_pass(self.session)
        self.assertEqual(outcome["summary"]["blockers"], 1)
        self.assertFalse(outcome["reused"])
        self.assertNotIn("verification_state", self.session.snapshot)

    def test_full_evidence_retrievable(self):
        verification_module.ambient_pass(self.session)
        bundle = verification_module.read_evidence(self.session)
        self.assertIsNotNone(bundle["evidence"])
        self.assertEqual(len(bundle["history"]), 1)

    def test_terse_stays_compact(self):
        verification_module.ambient_pass(self.session)
        terse = verification_module.terse(self.session)
        self.assertLess(len(json.dumps(terse)), 1024)

    def test_pass_emits_verified_event(self):
        verification_module.ambient_pass(self.session)
        kinds = [kind for kind, _payload in self.session.emitted]
        self.assertIn("verification.verified", kinds)

    def test_fail_emits_failed_event(self):
        self.session.execution_mode = "fail"
        verification_module.ambient_pass(self.session)
        kinds = [kind for kind, _payload in self.session.emitted]
        self.assertIn("verification.failed", kinds)

    def test_second_pass_replays_recorded_inventory(self):
        verification_module.ambient_pass(self.session)
        (self.repo / "tests" / "suite.mncs").write_text(SUITE_SOURCE + "# edit\n")
        verification_module.ambient_pass(self.session)
        second = self.session.coherence_calls[1]
        item = second["obligations"][0]
        self.assertTrue(item["evidence_present"])
        self.assertEqual(item["inventory_test_identities"], ["t-case-1"])
        self.assertFalse(item["inventory_truncated"])

    def test_libraries_travel_as_env_roots_not_flags(self):
        obligated = json.loads(json.dumps(OBLIGATION))
        obligated["executor"]["library_paths"] = ["tests"]
        self.write_obligations([obligated])
        verification_module.ambient_pass(self.session)
        self.assertEqual(len(self.session.executions), 1)
        self.assertNotIn("--library", self.session.executions[0])
        roots = self.session.last_test_env.get("MNCS_LIBRARY_PATH", "")
        self.assertIn(str(self.repo / "tests"), roots)


if __name__ == "__main__":
    unittest.main()
