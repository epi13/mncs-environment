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
            return self._invoke_coherence(argv, cwd, env)
        if capability == verification_module.TEST_CAPABILITY:
            return self._invoke_test(argv, cwd, env)
        raise AssertionError(f"unexpected capability {capability}")

    def _invoke_coherence(self, argv: list[str], cwd, env) -> dict:
        workdir = Path(cwd)
        self.last_coherence_env = dict(env or {})
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
                elif (item.get("external_evidence_present")
                      and (item.get("external_evidence") or {}).get("verdict") in ("PASS", "FAIL")):
                    ext = item["external_evidence"]
                    verdicts.append({"identity": item["identity"],
                                     "status": "current",
                                     "reason": "external_evidence_current",
                                     "deferred": False, "verdict_known": True,
                                     "verdict": ext["verdict"],
                                     "evidence_id": ext.get("evidence_id", ""),
                                     "resolved_test_identities": [],
                                     "unresolved_count": 0})
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
        if self.execution_mode == "mutate-mid-run":
            with open(self.repo_suite, "a", encoding="utf-8") as handle:
                handle.write("# mid-run mutation\n")
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
    def test_invalid_only_observation_is_current_without_policy_calls(self):
        self.write_obligations([])
        first = verification_module.ambient_pass(self.session)
        self.assertEqual(first["summary"]["blockers"], 0)
        broken = dict(OBLIGATION)
        broken["executor"] = {"kind": "nope"}
        self.write_obligations([broken])
        second = verification_module.ambient_pass(self.session)
        self.assertFalse(second["reused"])
        self.assertEqual(second["summary"]["blockers"], 1)
        saved = self.session.saved
        self.assertTrue(verification_module.ambient_pass(self.session)["reused"])
        self.assertEqual(self.session.saved, saved)
        self.assertEqual(self.session.coherence_calls, [])
        self.write_obligations([OBLIGATION])
        repaired = verification_module.ambient_pass(self.session)
        self.assertFalse(repaired["reused"])
        self.assertEqual(repaired["summary"]["executed"], 1)

    def test_unselected_repository_obligations_remain_asleep(self):
        self.session.snapshot["selected_checkouts"] = {}
        result = verification_module.ambient_pass(self.session)
        self.assertEqual(result["summary"]["obligations"], 0)
        self.assertEqual(self.session.executions, [])
        self.session.snapshot["selected_checkouts"] = {"fixture-repo": {"path": str(self.repo)}}
        result = verification_module.ambient_pass(self.session)
        self.assertEqual(result["summary"]["executed"], 1)

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
        evidence = verification_module.read_evidence(self.session)["evidence"]
        self.assertEqual(evidence["results"][0]["executor_kind"],
                         "native_first_class_test")

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
        evidence = verification_module.read_evidence(self.session)["evidence"]
        result_by_identity = {
            result["obligation"]: result for result in evidence["results"]
        }
        self.assertEqual(
            result_by_identity["fixture.host-suite"]["executor_kind"],
            "external_integration",
        )

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

    def test_mid_run_mutation_rejects_result(self):
        self.session.repo_suite = str(self.repo / "tests" / "suite.mncs")
        self.session.execution_mode = "mutate-mid-run"
        outcome = verification_module.ambient_pass(self.session)
        self.assertEqual(outcome["summary"]["unknown"], 1)
        self.assertEqual(outcome["summary"]["executed"], 0)
        evidence = verification_module.read_evidence(self.session)["evidence"]
        self.assertEqual(evidence["results"][0]["detail"],
                         "state-changed-during-execution")
        self.assertNotIn("fixture.native-suite",
                         self.session.snapshot.get("verification_state", {}))
        second = verification_module.ambient_pass(self.session)
        self.assertFalse(second["reused"])

    def test_coherence_cache_stays_in_session_state(self):
        verification_module.ambient_pass(self.session)
        cache = self.session.last_coherence_env.get(
            "MNCS_NATIVE_APPLICATION_CACHE_DIR", "")
        self.assertTrue(cache.startswith(str(self.state)))

    def test_libraries_travel_as_env_roots_not_flags(self):
        obligated = json.loads(json.dumps(OBLIGATION))
        obligated["executor"]["library_paths"] = ["tests"]
        self.write_obligations([obligated])
        verification_module.ambient_pass(self.session)
        self.assertEqual(len(self.session.executions), 1)
        self.assertNotIn("--library", self.session.executions[0])
        roots = self.session.last_test_env.get("MNCS_LIBRARY_PATH", "")
        self.assertIn(str(self.repo / "tests"), roots)

    def test_more_than_32_external_obligations_do_not_starve_trailing_native_suites(self):
        for name in ("alpha.mncs", "beta.mncs", "gamma.mncs"):
            (self.repo / "tests" / name).write_text(SUITE_SOURCE)
        externals = []
        for idx in range(40):
            entry = json.loads(json.dumps(HOST_OBLIGATION))
            entry["identity"] = f"a-compiler.ext-{idx:02d}"
            externals.append(entry)
        admitted_ext = json.loads(json.dumps(HOST_OBLIGATION))
        admitted_ext["identity"] = "m-actions.ext-admitted"
        native_suites = []
        for label, filename in (("alpha", "alpha.mncs"),
                                ("beta", "beta.mncs"),
                                ("gamma", "gamma.mncs")):
            suite = json.loads(json.dumps(OBLIGATION))
            suite["identity"] = f"z-test.native-{label}"
            suite["invalidation_dependencies"] = [f"tests/{filename}"]
            suite["executor"]["source_paths"] = [f"tests/{filename}"]
            native_suites.append(suite)
        self.write_obligations(externals + [admitted_ext] + native_suites)
        self.session.snapshot["actions_state"] = {
            "m-actions.ext-admitted": {
                "evidence": {
                    "obligation": "m-actions.ext-admitted",
                    "subject_digest": "sha256:admitted",
                    "run_identity": "9001",
                    "verdict": "passed",
                    "claim": "established",
                    "complete": True,
                },
                "evidence_id": "ext-9001",
                "subject_digest": "sha256:admitted",
            }
        }
        discovered, invalid = verification_module.discover_obligations(self.workspace)
        self.assertEqual(invalid, [])
        self.assertEqual(len(discovered), 44)

        outcome = verification_module.ambient_pass(self.session)
        summary = outcome["summary"]
        self.assertEqual(summary["invalid"], 0)
        self.assertEqual(summary["blockers"], 0)
        self.assertEqual(summary["obligations"], 44)
        self.assertEqual(summary["executed"], 3)
        self.assertEqual(summary["current"], 4)
        self.assertEqual(summary["overflow"], 12)
        self.assertEqual(summary["deferred"], 12)
        self.assertEqual(len(summary["overflow_ids"]), 12)
        self.assertEqual(summary["overflow_ids"][0], "a-compiler.ext-28")
        self.assertEqual(summary["overflow_ids"][-1], "a-compiler.ext-39")

        request = self.session.coherence_calls[-1]
        self.assertEqual(len(request["obligations"]), verification_module.MAX_OBLIGATIONS)
        routed_ids = [item["identity"] for item in request["obligations"]]
        self.assertIn("z-test.native-alpha", routed_ids)
        self.assertIn("z-test.native-beta", routed_ids)
        self.assertIn("z-test.native-gamma", routed_ids)
        self.assertIn("m-actions.ext-admitted", routed_ids)

        evidence = verification_module.read_evidence(self.session)["evidence"]
        self.assertEqual(len(evidence["overflow"]), 12)
        by_id = {row["obligation"]: row for row in evidence["results"]}
        for label in ("alpha", "beta", "gamma"):
            self.assertEqual(by_id[f"z-test.native-{label}"]["outcome"], "passed")
            self.assertTrue(by_id[f"z-test.native-{label}"]["ran"])
        self.assertEqual(by_id["m-actions.ext-admitted"]["outcome"], "passed")
        self.assertEqual(by_id["m-actions.ext-admitted"]["evidence_id"], "ext-9001")
        self.assertEqual(by_id["a-compiler.ext-39"]["outcome"], "deferred")
        self.assertTrue(by_id["a-compiler.ext-39"]["overflow"])
        self.assertEqual(by_id["a-compiler.ext-39"]["reason"], "obligation_window_overflow")

        terse = verification_module.terse(self.session)
        self.assertEqual(terse["verification"]["overflow"], 12)
        self.assertEqual(terse["verification"]["deferred"], 12)


class FakeV2Session(FakeSession):
    """Fake session implementing exact `verification_coherence_v2.mncs` closure_admits."""

    def __init__(self, workspace_root: Path, state_dir: Path):
        super().__init__(workspace_root, state_dir)
        self.snapshot["bindings"].insert(
            0,
            {"capability": verification_module.COHERENCE_CAPABILITY_V2,
             "availability": {"status": "available"}},
        )
        self.mid_run_mutate_path: Path | None = None

    def invoke(self, capability: str, argv: list[str], *,
               cwd=None, timeout_seconds=None,
               output_limit_bytes=None, env=None) -> dict:
        if capability == verification_module.COHERENCE_CAPABILITY_V2:
            return self._invoke_coherence_v2(argv, cwd, env)
        return super().invoke(capability, argv, cwd=cwd,
                              timeout_seconds=timeout_seconds,
                              output_limit_bytes=output_limit_bytes, env=env)

    def _invoke_test(self, argv: list[str], cwd, env) -> dict:
        if self.mid_run_mutate_path is not None:
            with open(self.mid_run_mutate_path, "a", encoding="utf-8") as handle:
                handle.write("\n// mid-run library edit\n")
            self.mid_run_mutate_path = None
        return super()._invoke_test(argv, cwd, env)

    @staticmethod
    def _legacy_admits(ev: dict, cur: dict) -> bool:
        keys = (
            "definition_identity", "subject_identity", "subject_fingerprint",
            "executor_identity", "invalidation_identity", "toolchain_identity",
            "inventory_identity", "repository_revision", "repository_fingerprint",
        )
        return all(ev.get(k) == cur.get(k) for k in keys)

    @staticmethod
    def _closure_admits(ev: dict, cur: dict) -> bool:
        if not (ev.get("closure_trusted") is True and cur.get("closure_trusted") is True):
            return False
        if not ev.get("closure_identity") or not cur.get("closure_identity"):
            return False
        keys = (
            "closure_fileset", "closure_identity",
            "definition_identity", "subject_identity", "subject_fingerprint",
            "executor_identity", "invalidation_identity", "toolchain_identity",
            "inventory_identity",
        )
        return all(ev.get(k) == cur.get(k) for k in keys)

    def _invoke_coherence_v2(self, argv: list[str], cwd, env) -> dict:
        workdir = Path(cwd)
        request = json.loads((workdir / argv[0]).read_text(encoding="utf-8"))
        assert request["schema_version"] == verification_module.COHERENCE_REQUEST_SCHEMA_V2
        self.coherence_calls.append(request)
        verdicts = []
        queue = []
        for item in request["obligations"]:
            if not item["evidence_present"]:
                if item["executor_kind"] == "native_first_class_test" and item["runnable_native"]:
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
                ev = item["evidence"]
                cur = item["current"]
                if self._legacy_admits(ev, cur):
                    verdicts.append({"identity": item["identity"],
                                     "status": "current",
                                     "reason": "evidence_current",
                                     "deferred": False, "verdict_known": True,
                                     "verdict": ev["verdict"],
                                     "evidence_id": ev["evidence_id"],
                                     "resolved_test_identities": [],
                                     "unresolved_count": 0})
                elif self._closure_admits(ev, cur):
                    verdicts.append({"identity": item["identity"],
                                     "status": "current",
                                     "reason": "closure_current",
                                     "deferred": False, "verdict_known": True,
                                     "verdict": ev["verdict"],
                                     "evidence_id": ev["evidence_id"],
                                     "resolved_test_identities": [],
                                     "unresolved_count": 0})
                else:
                    verdicts.append({"identity": item["identity"],
                                     "status": "stale",
                                     "reason": "input_changed",
                                     "deferred": False, "verdict_known": True,
                                     "verdict": ev["verdict"],
                                     "evidence_id": ev["evidence_id"],
                                     "resolved_test_identities": ["t-case-1"],
                                     "unresolved_count": 0})
                    queue.append(item["identity"])
        result = {
            "schema_version": verification_module.COHERENCE_RESULT_SCHEMA,
            "verdicts": verdicts,
            "run_queue": queue[:request["max_executions"]],
            "summary": {
                "obligations": len(verdicts),
                "current": sum(1 for v in verdicts if v["status"] == "current"),
                "queued": len(queue), "deferred": 0, "failed": 0, "unknown": 0,
                "contradictory": 0, "unsupported": 0, "unresolved": 0, "excluded": 0,
            },
        }
        (workdir / argv[1]).write_text(json.dumps(result), encoding="utf-8")
        return {"status": "ok", "stdout": "", "stderr": ""}


class ResolvedClosureV2Tests(VerificationFixture):
    def setUp(self):
        super().setUp()
        (self.repo / "src" / "lib").mkdir(parents=True)
        (self.repo / "src" / "lib" / "shared.mncs").write_text(
            "mncs 0.18;\nmodule lib.shared.v1;\nfn s() -> (r: u64) { return 1; }\n"
        )
        (self.repo / "src" / "lib" / "only_a.mncs").write_text(
            "mncs 0.18;\nmodule lib.only_a.v1;\nuse lib.shared.v1 as shared;\nfn a() -> (r: u64) { return shared.s(); }\n"
        )
        (self.repo / "src" / "lib" / "only_b.mncs").write_text(
            "mncs 0.18;\nmodule lib.only_b.v1;\nuse lib.shared.v1 as shared;\nfn b() -> (r: u64) { return shared.s(); }\n"
        )
        (self.repo / "tests" / "suite_a.mncs").write_text(
            "mncs 0.18;\nmodule fixture.suite_a;\nuse lib.only_a.v1 as only_a;\n"
        )
        (self.repo / "tests" / "suite_b.mncs").write_text(
            "mncs 0.18;\nmodule fixture.suite_b;\nuse lib.only_b.v1 as only_b;\n"
        )
        self.ob_a = {
            "identity": "fixture.suite-a",
            "title": "Suite A",
            "lifecycle": "permanent",
            "scope": "repository_canonical",
            "invalidation_dependencies": ["tests/suite_a.mncs"],
            "executor": {
                "provider": "mncs-test", "kind": "native_first_class_test",
                "entrypoint": "mncs-test run",
                "source_paths": ["tests/suite_a.mncs"],
                "library_paths": ["src"],
                "declaration_identities": ["*"],
                "verifier_identity": "mncs-test-runner/0.2.1",
            },
        }
        self.ob_b = {
            "identity": "fixture.suite-b",
            "title": "Suite B",
            "lifecycle": "permanent",
            "scope": "repository_canonical",
            "invalidation_dependencies": ["tests/suite_b.mncs"],
            "executor": {
                "provider": "mncs-test", "kind": "native_first_class_test",
                "entrypoint": "mncs-test run",
                "source_paths": ["tests/suite_b.mncs"],
                "library_paths": ["src"],
                "declaration_identities": ["*"],
                "verifier_identity": "mncs-test-runner/0.2.1",
            },
        }
        self.write_obligations([self.ob_a, self.ob_b])
        git(self.repo, "add", ".")
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=t@invalid",
            "commit", "-qm", "add two suites with shared import")
        self.session = FakeV2Session(self.workspace, self.state)

    def test_v2_suite_only_and_shared_import_selective_invalidation(self):
        # 1. Cold pass runs both suites.
        first = verification_module.ambient_pass(self.session)
        self.assertEqual(first["summary"]["executed"], 2)
        self.assertEqual(first["summary"]["current"], 2)

        # 2. Unchanged re-entry reuses epoch with zero provider launches.
        coherence_before = len(self.session.coherence_calls)
        execs_before = len(self.session.executions)
        second = verification_module.ambient_pass(self.session)
        self.assertTrue(second["reused"])
        self.assertEqual(len(self.session.coherence_calls), coherence_before)
        self.assertEqual(len(self.session.executions), execs_before)

        # 3. Suite-only comment edit reruns ONLY suite_a; suite_b stays closure_current.
        (self.repo / "tests" / "suite_a.mncs").write_text(
            "mncs 0.18;\nmodule fixture.suite_a;\nuse lib.only_a.v1 as only_a;\n// comment\n"
        )
        third = verification_module.ambient_pass(self.session)
        self.assertFalse(third["reused"])
        self.assertEqual(third["summary"]["executed"], 1)
        self.assertEqual(third["summary"]["current"], 2)
        by_id = {r["obligation"]: r for r in verification_module.read_evidence(self.session)["evidence"]["results"]}
        self.assertTrue(by_id["fixture.suite-a"].get("ran"))
        self.assertFalse(by_id["fixture.suite-b"].get("ran", False))
        self.assertEqual(by_id["fixture.suite-b"]["reason"], "closure_current")

        # 4. Private imported module edit (not listed in invalidation_dependencies)
        #    reruns ONLY suite_a; suite_b stays closure_current.
        (self.repo / "src" / "lib" / "only_a.mncs").write_text(
            "mncs 0.18;\nmodule lib.only_a.v1;\nuse lib.shared.v1 as shared;\nfn a() -> (r: u64) { return shared.s() + 1; }\n"
        )
        fourth = verification_module.ambient_pass(self.session)
        self.assertEqual(fourth["summary"]["executed"], 1)
        by_id = {r["obligation"]: r for r in verification_module.read_evidence(self.session)["evidence"]["results"]}
        self.assertTrue(by_id["fixture.suite-a"].get("ran"))
        self.assertEqual(by_id["fixture.suite-b"]["reason"], "closure_current")

        # 5. Shared transitive import edit reruns BOTH suites.
        (self.repo / "src" / "lib" / "shared.mncs").write_text(
            "mncs 0.18;\nmodule lib.shared.v1;\nfn s() -> (r: u64) { return 2; }\n"
        )
        fifth = verification_module.ambient_pass(self.session)
        self.assertEqual(fifth["summary"]["executed"], 2)
        self.assertEqual(fifth["summary"]["current"], 2)

    def test_v2_untrusted_closure_falls_back_conservatively(self):
        first = verification_module.ambient_pass(self.session)
        self.assertEqual(first["summary"]["executed"], 2)
        # Add an unresolved import to suite_b -> closure_trusted becomes False.
        (self.repo / "tests" / "suite_b.mncs").write_text(
            "mncs 0.18;\nmodule fixture.suite_b;\nuse lib.does_not_exist.v1;\n"
        )
        # suite_a still has trusted closure -> stays closure_current;
        # suite_b has untrusted closure -> falls back to legacy dirty check -> stale!
        second = verification_module.ambient_pass(self.session)
        self.assertEqual(second["summary"]["executed"], 1)
        req = self.session.coherence_calls[-1]
        cur_by_id = {o["identity"]: o["current"] for o in req["obligations"]}
        self.assertTrue(cur_by_id["fixture.suite-a"]["closure_trusted"])
        self.assertFalse(cur_by_id["fixture.suite-b"]["closure_trusted"])
        self.assertEqual(cur_by_id["fixture.suite-b"]["closure_identity"], "")

    def test_v2_mid_run_external_library_mutation_rejected(self):
        # Put an imported library outside `self.repo` via adapter_library_paths.
        ext_lib = self.base / "ext-lib"
        (ext_lib / "ext").mkdir(parents=True)
        ext_mod = ext_lib / "ext" / "helper.mncs"
        ext_mod.write_text("mncs 0.18;\nmodule ext.helper.v1;\n")
        for b in self.session.snapshot["bindings"]:
            if b["capability"] == verification_module.TEST_CAPABILITY:
                b["provenance"] = {"adapter_library_paths": [str(ext_lib)]}
        (self.repo / "tests" / "suite_a.mncs").write_text(
            "mncs 0.18;\nmodule fixture.suite_a;\nuse ext.helper.v1;\n"
        )
        self.write_obligations([self.ob_a])
        self.session.mid_run_mutate_path = ext_mod
        outcome = verification_module.ambient_pass(self.session)
        self.assertEqual(outcome["operation_status"], "retry")
        self.assertEqual(outcome["summary"]["blockers"], 2)
        ev = verification_module.read_evidence(self.session)["evidence"]
        self.assertEqual(ev["results"][0]["detail"], "state-changed-during-execution")

    def test_v2_toolchain_or_selection_change_invalidates_closure(self):
        verification_module.ambient_pass(self.session)
        # Change toolchain revision -> invalidates both suites even on clean repo.
        self.session.snapshot["toolchain"]["revision"] = "rev-2"
        second = verification_module.ambient_pass(self.session)
        self.assertEqual(second["summary"]["executed"], 2)
        # Change selected tests pattern on suite_a only -> invalidates only suite_a.
        self.ob_a["executor"]["declaration_identities"] = ["t-case-1"]
        self.write_obligations([self.ob_a, self.ob_b])
        third = verification_module.ambient_pass(self.session)
        self.assertEqual(third["summary"]["executed"], 1)


if __name__ == "__main__":
    unittest.main()
