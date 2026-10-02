"""Ambient diagnostic coherence: lifecycle around native policy.

Fake sessions drive ambient_pass with stubbed provider invocations, so
observation/epoch/evidence/capture behavior is exercised hermetically.
The native diagnostic policy itself is covered in mncs-debug; live
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

from mncs_env import diagnostics as diagnostics_module  # noqa: E402


SUITE_SOURCE = 'mncs 0.18;\nmodule fixture.suite;\n'


def git(repo: Path, *argv: str) -> None:
    subprocess.run(["git", "-C", str(repo), *argv], check=True,
                   capture_output=True, text=True, timeout=60)


def git_init(repo: Path) -> None:
    for argv in (["init", "-q", "-b", "main"], ["add", "."],
                 ["-c", "user.name=Diagnostics Test",
                  "-c", "user.email=diagnostics@example.invalid",
                  "commit", "-qm", "fixture"]):
        git(repo, *argv)


class FakeStore:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir


class FakeSession:
    def __init__(self, workspace_root: Path, state_dir: Path):
        self.session_id = "ses_diagnostic_fixture"
        self.snapshot: dict = {
            "workspace": {"root": str(workspace_root)},
            "lifecycle": "active",
            "consumer_id": "diagnostic-test",
            "toolchain": {"binary": "/bin/mncs", "revision": "rev-1",
                          "checkout": "/chk", "repository": "mncs-language"},
            "bindings": [
                {"capability": diagnostics_module.COHERENCE_CAPABILITY,
                 "availability": {"status": "available"}},
                {"capability": diagnostics_module.DEBUG_CAPABILITY,
                 "availability": {"status": "available"}},
                {"capability": diagnostics_module.TEST_CAPABILITY,
                 "availability": {"status": "available"},
                 "provenance": {"adapter_library_paths": []}},
            ],
        }
        self.store = FakeStore(state_dir)
        self.saved = 0
        self.coherence_calls: list[dict] = []
        self.captures: list[list[str]] = []
        self.emitted: list[tuple[str, dict]] = []
        self.coherence_mode = "policy"
        self.capture_mode = "witness"
        self.authority = "allow"
        self.last_capture_env: dict = {}

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

    def check(self, *, action: str, target: str,
              repo_facts=None, scope=None) -> dict:
        if self.authority == "allow":
            return {"verdict": "allow", "reason": "fixture allows verify"}
        return {"verdict": "deny",
                "reason": f"{target} overlaps scope claimed by another session"}

    def invoke(self, capability: str, argv: list[str], *,
               cwd=None, timeout_seconds=None,
               output_limit_bytes=None, env=None) -> dict:
        if capability == diagnostics_module.COHERENCE_CAPABILITY:
            return self._invoke_coherence(argv, cwd, env)
        if capability == diagnostics_module.DEBUG_CAPABILITY:
            return self._invoke_debug(argv, cwd, env)
        raise AssertionError(f"unexpected capability {capability}")

    def _invoke_coherence(self, argv: list[str], cwd, env) -> dict:
        workdir = Path(cwd)
        request = json.loads((workdir / argv[0]).read_text(encoding="utf-8"))
        self.coherence_calls.append(request)
        if self.coherence_mode == "fail":
            return {"status": "error", "stderr": "stub coherence failure"}
        diagnostics = []
        queue = []
        for item in request["failures"]:
            verdict = self._policy(item)
            diagnostics.append(verdict)
            if verdict["status"] in ("capture_required", "extend_required"):
                queue.append(item["failure_key"])
        result = {"schema_version": diagnostics_module.COHERENCE_RESULT_SCHEMA,
                  "diagnostics": diagnostics,
                  "capture_queue": queue[:request["max_captures"]],
                  "summary": {"failures": len(diagnostics),
                              "current": sum(1 for v in diagnostics
                                             if v["status"] == "current"),
                              "queued": len(queue), "deferred": 0,
                              "unsupported": sum(1 for v in diagnostics
                                                 if v["status"] == "unsupported"),
                              "escalated": sum(1 for v in diagnostics
                                               if v["status"] == "escalate")}}
        (workdir / argv[1]).write_text(json.dumps(result), encoding="utf-8")
        return {"status": "ok", "stdout": "", "stderr": ""}

    @staticmethod
    def _policy(item: dict) -> dict:
        base = {"failure_key": item["failure_key"], "deferred": False,
                "witness_ref": "", "admitted_depth": "minimal"}
        if item["failure_class"] not in ("test_failure", "runtime_failure"):
            return {**base, "status": "unsupported",
                    "reason": "failure_not_debuggable",
                    "operation": "none"}
        if not item["provider_available"]:
            return {**base, "status": "unsupported",
                    "reason": "provider_unavailable", "operation": "none"}
        if not item["toolchain_identity"]:
            return {**base, "status": "escalate",
                    "reason": "toolchain_unbound", "operation": "none"}
        if not item["has_source"] or not item["has_request"]:
            return {**base, "status": "escalate",
                    "reason": "evidence_missing", "operation": "none"}
        if not item["evidence_present"]:
            return {**base, "status": "capture_required",
                    "reason": "no_evidence", "operation": "witness"}
        evidence = item["evidence"]
        admitted = (
            evidence["failure_key"] == item["failure_key"]
            and evidence["subject_sha"] == item["subject_sha"]
            and evidence["request_digest"] == item["request_digest"]
            and evidence["toolchain_identity"] == item["toolchain_identity"]
            and evidence["capture_complete"])
        if not admitted:
            return {**base, "status": "capture_required",
                    "reason": "input_changed", "operation": "witness",
                    "witness_ref": evidence["witness_ref"],
                    "admitted_depth": evidence["depth"]}
        rank = {"minimal": 0, "standard": 1, "deep": 2}
        if rank[evidence["depth"]] < rank[item["requested_depth"]]:
            operation = {"standard": "trace", "deep": "replay"}[
                item["requested_depth"]]
            return {**base, "status": "extend_required",
                    "reason": "depth_exceeded", "operation": operation,
                    "witness_ref": evidence["witness_ref"],
                    "admitted_depth": evidence["depth"]}
        return {**base, "status": "current", "reason": "evidence_current",
                "operation": "none",
                "witness_ref": evidence["witness_ref"],
                "admitted_depth": evidence["depth"]}

    def _invoke_debug(self, argv: list[str], cwd, env) -> dict:
        workdir = Path(cwd)
        self.captures.append(list(argv))
        self.last_capture_env = dict(env or {})
        if self.capture_mode == "transport-error":
            raise OSError("stub transport failure")
        if self.capture_mode == "mutate-mid-run":
            with open(self.repo_suite, "a", encoding="utf-8") as handle:
                handle.write("# mid-run mutation\n")
        output = "witness.json"
        if "--output" in argv:
            output = argv[argv.index("--output") + 1]
        if self.capture_mode == "broken":
            witness = {"witness_id": "mncs:debug:witness:broken",
                       "execution_identity": "mncs:debug:execution:broken",
                       "outcome": {"failure_class": "infrastructure_failure"}}
        else:
            witness = {"witness_id": "mncs:debug:witness:fixture-1",
                       "execution_identity": "mncs:debug:execution:fixture-1",
                       "outcome": {"failure_class": "test_failure"}}
        (workdir / output).write_text(json.dumps(witness), encoding="utf-8")
        return {"status": "ok", "stdout": "", "stderr": ""}


OBLIGATION = {
    "identity": "fixture.debug-probe",
    "title": "Fixture failing suite",
    "lifecycle": "permanent",
    "scope": "local",
    "invalidation_dependencies": ["tests/suite.mncs"],
    "executor": {"provider": "fixture-test", "kind": "native_first_class_test",
                 "entrypoint": "fixture run",
                 "source_paths": ["tests/suite.mncs"],
                 "library_paths": ["lib"],
                 "declaration_identities": ["*"],
                 "verifier_identity": "fixture-runner/0.0"},
}


class DiagnosticsFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix="mncs-diagnostics-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.workspace = self.base / "work"
        self.workspace.mkdir(parents=True)
        self.state = self.base / "state"
        repo = self.workspace / "target-repo"
        (repo / ".mncs").mkdir(parents=True)
        (repo / "tests").mkdir(parents=True)
        (repo / "lib").mkdir(parents=True)
        (repo / "tests" / "suite.mncs").write_text(SUITE_SOURCE)
        (repo / ".mncs" / "project.json").write_text(json.dumps({
            "schema_version": "mncs-family.repository-manifest/v0alpha1",
            "repository": "target-repo",
            "verification": {"obligation_inventory": ".mncs/obligations.json"},
            "contracts": {"provides": []}}))
        (repo / ".mncs" / "obligations.json").write_text(json.dumps({
            "schema_version": "mncs-family.verification-obligation-inventory/v1",
            "repository": "target-repo", "revision": 1,
            "obligations": [OBLIGATION]}))
        git_init(repo)
        self.repo = repo
        self.suite = repo / "tests" / "suite.mncs"
        self.session = FakeSession(self.workspace, self.state)
        self.session.repo_suite = self.suite

    def write_result(self, run_tag: str, *, verdict: str = "FAIL",
                     failure_kind: str = "assertion",
                     with_source: bool = True,
                     with_request: bool = True,
                     source_sha: str = "abc123") -> None:
        entry: dict = {"id": "t-case-1", "entry": "case",
                       "verdict": verdict, "status": "failed",
                       "native_result": {"failure_kind": failure_kind},
                       "source_span": {"line": 3, "column": 1}}
        if with_source:
            entry["source"] = str(self.suite)
        if with_request:
            entry["request"] = {"schema_version": "0.1",
                                "target": {"function": "case",
                                           "module": "fixture.suite"},
                                "arguments": [], "step_budget": 1000}
        document = {
            "schema_version": "mncs.test-result/1",
            "verdict": verdict,
            "classification": "test_failure" if verdict == "FAIL" else "passed",
            "failure_class": "none",
            "provenance": {"source": {"path": str(self.suite),
                                      "sha256": source_sha}},
            "execution": {"run_identity": "run-1"},
            "tests": [entry],
        }
        runs = (self.state / "sessions" / self.session.session_id
                / "verification-artifacts" / "runs" / run_tag)
        runs.mkdir(parents=True, exist_ok=True)
        (runs / "result.json").write_text(json.dumps(document))

    def fail_row(self, run_tag: str = "run-tag-1", **kwargs) -> dict:
        self.write_result(run_tag, **kwargs)
        return {"evidence": {}, "verdict": "FAIL",
                "failure_class": "none", "classification": "test_failure",
                "failed_test_ids": [{"id": "t-case-1", "verdict": "FAIL"}],
                "run_tag": run_tag}

    def run_pass(self, **kwargs):
        return diagnostics_module.ambient_pass(self.session, **kwargs)


class LifecycleTests(DiagnosticsFixture):
    def test_healthy_world_skips_native_policy(self):
        self.session.snapshot["verification_state"] = {}
        result = self.run_pass()
        self.assertEqual(result["summary"]["failures"], 0)
        self.assertEqual(result["summary"]["blockers"], 0)
        self.assertEqual(self.session.coherence_calls, [])
        self.assertEqual(self.session.captures, [])

    def test_healthy_reentry_reuses_empty_epoch(self):
        self.session.snapshot["verification_state"] = {}
        first = self.run_pass()
        self.assertFalse(first["reused"])
        second = self.run_pass()
        self.assertTrue(second["reused"])
        self.assertTrue(second["summary"]["epoch_reused"])

    def test_unknown_rows_are_not_failures(self):
        self.session.snapshot["verification_state"] = {
            "fixture.debug-probe": {"verdict": "UNKNOWN", "run_tag": "run-x"}}
        result = self.run_pass()
        self.assertEqual(result["summary"]["failures"], 0)
        self.assertEqual(self.session.coherence_calls, [])

    def test_first_fail_captures_and_records(self):
        self.session.snapshot["verification_state"] = {
            "fixture.debug-probe": self.fail_row()}
        result = self.run_pass()
        summary = result["summary"]
        self.assertEqual(summary["failures"], 1)
        self.assertEqual(summary["captured"], 1)
        self.assertEqual(summary["blockers"], 0)
        self.assertEqual(len(self.session.captures), 1)
        self.assertEqual(len(summary["capsule_ids"]), 1)
        rows = self.session.snapshot["diagnostic_state"]
        self.assertEqual(len(rows), 1)
        key = next(iter(rows))
        self.assertIn("t-case-1", key)
        self.assertTrue(rows[key]["evidence"]["capture_complete"])
        kinds = [event for event, _ in self.session.emitted]
        self.assertIn("diagnostic.captured", kinds)

    def test_quiet_reentry_reuses_witness(self):
        self.session.snapshot["verification_state"] = {
            "fixture.debug-probe": self.fail_row()}
        first = self.run_pass()
        self.assertFalse(first["reused"])
        captures = len(self.session.captures)
        second = self.run_pass()
        self.assertTrue(second["reused"])
        self.assertEqual(len(self.session.captures), captures)

    def test_subject_edit_recaptures(self):
        self.session.snapshot["verification_state"] = {
            "fixture.debug-probe": self.fail_row()}
        self.run_pass()
        with open(self.suite, "a", encoding="utf-8") as handle:
            handle.write("# edit\n")
        # The edited subject produces a fresh verification result row
        # attesting the new content.
        self.session.snapshot["verification_state"] = {
            "fixture.debug-probe": self.fail_row("run-tag-2",
                                                 source_sha="def456")}
        captures = len(self.session.captures)
        result = self.run_pass()
        self.assertFalse(result["reused"])
        self.assertEqual(len(self.session.captures), captures + 1)

    def test_disappeared_failure_clears_active_state(self):
        self.session.snapshot["verification_state"] = {
            "fixture.debug-probe": self.fail_row()}
        self.run_pass()
        self.assertEqual(len(self.session.snapshot["diagnostic_state"]), 1)
        self.session.snapshot["verification_state"] = {
            "fixture.debug-probe": {"verdict": "PASS", "run_tag": "run-tag-1"}}
        result = self.run_pass()
        self.assertEqual(result["summary"]["failures"], 0)
        self.assertEqual(self.session.snapshot["diagnostic_state"], {})
        history = self.session.snapshot["diagnostic_history"]
        self.assertGreaterEqual(len(history), 2)

    def test_mid_run_mutation_rejects_capture(self):
        self.session.snapshot["verification_state"] = {
            "fixture.debug-probe": self.fail_row()}
        self.session.capture_mode = "mutate-mid-run"
        result = self.run_pass()
        self.assertEqual(result["summary"]["captured"], 0)
        self.assertEqual(result["summary"]["blockers"], 1)
        stored = self.session.snapshot["diagnostic_epoch"]
        self.assertTrue(stored["epoch"].startswith("uncached:"))
        kinds = [event for event, _ in self.session.emitted]
        self.assertIn("diagnostic.failed", kinds)

    def test_transport_failure_never_caches(self):
        self.session.snapshot["verification_state"] = {
            "fixture.debug-probe": self.fail_row()}
        self.session.capture_mode = "transport-error"
        result = self.run_pass()
        self.assertEqual(result["summary"]["blockers"], 1)
        stored = self.session.snapshot["diagnostic_epoch"]
        self.assertTrue(stored["epoch"].startswith("uncached:"))

    def test_broken_witness_is_not_evidence(self):
        self.session.snapshot["verification_state"] = {
            "fixture.debug-probe": self.fail_row()}
        self.session.capture_mode = "broken"
        result = self.run_pass()
        self.assertEqual(result["summary"]["captured"], 0)
        self.assertNotIn("diagnostic_state", self.session.snapshot)
        stored = self.session.snapshot["diagnostic_epoch"]
        self.assertTrue(stored["epoch"].startswith("uncached:"))

    def test_foreign_claim_denies_capture(self):
        self.session.snapshot["verification_state"] = {
            "fixture.debug-probe": self.fail_row()}
        self.session.authority = "deny"
        result = self.run_pass()
        self.assertEqual(result["summary"]["captured"], 0)
        detail = self.session.emitted[-1][1].get("detail", "")
        self.assertIn("another session", detail)
        self.assertNotIn("diagnostic_state", self.session.snapshot)

    def test_only_mode_merges_without_pruning(self):
        self.session.snapshot["verification_state"] = {
            "fixture.debug-probe": self.fail_row()}
        self.run_pass()
        key = next(iter(self.session.snapshot["diagnostic_state"]))
        self.session.snapshot["diagnostic_state"]["other::key"] = {
            "evidence": {"failure_key": "other::key"}}
        result = self.run_pass(mode="explicit", only=key)
        self.assertEqual(result["summary"]["failures"], 1)
        rows = self.session.snapshot["diagnostic_state"]
        self.assertIn(key, rows)
        self.assertIn("other::key", rows)

    def test_deeper_depth_extends_recorded_minimal(self):
        self.session.snapshot["verification_state"] = {
            "fixture.debug-probe": self.fail_row()}
        self.run_pass()
        captures = len(self.session.captures)
        self.run_pass(mode="explicit", depth="standard")
        request = self.session.coherence_calls[-1]
        self.assertEqual(request["failures"][0]["requested_depth"],
                         "standard")
        self.assertEqual(len(self.session.captures), captures + 1)
        rows = self.session.snapshot["diagnostic_state"]
        key = next(iter(rows))
        self.assertEqual(rows[key]["depth"], "standard")

    def test_missing_request_escalates_without_capture(self):
        self.session.snapshot["verification_state"] = {
            "fixture.debug-probe": self.fail_row(with_request=False)}
        result = self.run_pass()
        self.assertEqual(result["summary"]["escalated"], 1)
        self.assertEqual(result["summary"]["blockers"], 1)
        self.assertEqual(self.session.captures, [])

    def test_infrastructure_failure_is_unsupported(self):
        self.session.snapshot["verification_state"] = {
            "fixture.debug-probe": self.fail_row(
                failure_kind="infrastructure")}
        result = self.run_pass()
        self.assertEqual(result["summary"]["unsupported"], 1)
        self.assertEqual(result["summary"]["blockers"], 0)
        self.assertEqual(self.session.captures, [])

    def test_provider_unavailable_is_unsupported(self):
        for binding in self.session.snapshot["bindings"]:
            if binding["capability"] == diagnostics_module.DEBUG_CAPABILITY:
                binding["availability"]["status"] = "unavailable"
        self.session.snapshot["verification_state"] = {
            "fixture.debug-probe": self.fail_row()}
        result = self.run_pass()
        self.assertEqual(result["summary"]["unsupported"], 1)
        self.assertEqual(self.session.captures, [])

    def test_coherence_failure_is_a_blocker(self):
        self.session.snapshot["verification_state"] = {
            "fixture.debug-probe": self.fail_row()}
        self.session.coherence_mode = "fail"
        result = self.run_pass()
        self.assertEqual(result["summary"]["blockers"], 1)

    def test_capture_libraries_combine_declared_and_adapter_roots(self):
        adapter = self.base / "adapter-native"
        adapter.mkdir()
        for binding in self.session.snapshot["bindings"]:
            if binding["capability"] == diagnostics_module.TEST_CAPABILITY:
                binding["provenance"]["adapter_library_paths"] = [str(adapter)]
        self.session.snapshot["verification_state"] = {
            "fixture.debug-probe": self.fail_row()}
        self.run_pass()
        argv = self.session.captures[0]
        libraries = [argv[index + 1] for index, value in enumerate(argv)
                     if value == "--library"]
        self.assertIn(str(self.repo / "lib"), libraries)
        self.assertIn(str(adapter), libraries)

    def test_capture_cache_stays_in_session_state(self):
        self.session.snapshot["verification_state"] = {
            "fixture.debug-probe": self.fail_row()}
        self.run_pass()
        cache = self.session.last_capture_env.get(
            "MNCS_NATIVE_APPLICATION_CACHE_DIR", "")
        self.assertTrue(cache.startswith(str(self.state)))

    def test_failure_class_mapping(self):
        cases = {
            "assertion": "test_failure",
            "setup": "test_failure",
            "runtime": "runtime_failure",
            "compile": "compile_failure",
            "timeout": "timeout",
            "unsupported": "unsupported",
            "infrastructure": "infrastructure_failure",
            "nofailure": "test_failure",
            "mystery": "test_failure",
        }
        for kind, expected in cases.items():
            entry = {"native_result": {"failure_kind": kind}}
            document = {"classification": "test_failure",
                        "failure_class": "none"}
            self.assertEqual(
                diagnostics_module._failure_class(entry, document), expected,
                kind)
        self.assertEqual(
            diagnostics_module._failure_class(
                {}, {"classification": "compile_failure"}),
            "compile_failure")
        self.assertEqual(
            diagnostics_module._failure_class(
                {}, {"classification": "wat", "failure_class": "wat"}),
            "unsupported")

    def test_terse_stays_compact(self):
        self.session.snapshot["verification_state"] = {
            "fixture.debug-probe": self.fail_row()}
        self.run_pass()
        terse = diagnostics_module.terse(self.session)
        self.assertEqual(terse["diagnostic"]["failures"], 1)
        self.assertEqual(terse["diagnostic"]["captured"], 1)
        self.assertLess(len(json.dumps(terse)), 1200)

    def test_full_evidence_retrievable(self):
        self.session.snapshot["verification_state"] = {
            "fixture.debug-probe": self.fail_row()}
        self.run_pass()
        payload = diagnostics_module.read_evidence(self.session)
        self.assertIsNotNone(payload["evidence"])
        self.assertEqual(payload["evidence"]["results"][0]["outcome"],
                         "captured")

    def test_stale_row_for_removed_obligation_is_ignored(self):
        self.session.snapshot["verification_state"] = {
            "fixture.removed": self.fail_row()}
        result = self.run_pass()
        self.assertEqual(result["summary"]["failures"], 0)
        self.assertEqual(self.session.coherence_calls, [])
        self.assertEqual(self.session.captures, [])


if __name__ == "__main__":
    unittest.main()
