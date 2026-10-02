"""Ambient diagnostic entry wiring: real CLI, stub providers.

A fixture workspace carries a stub test provider (FAIL/PASS on demand
through declared family contracts), a stub debug provider (diagnostic
coherence + import-test capture), and a target repository with one
native verification obligation. Entry runs the ambient verification
pass followed by the diagnostic handoff; re-entry reuses both epochs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "mncs-env"
sys.path.insert(0, str(ROOT))

VERIFY_COHERENCE_STUB = '''\
import json
import sys
from pathlib import Path


def main(argv):
    request = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    verdicts = []
    queue = []
    for item in request["obligations"]:
        if not item["evidence_present"]:
            verdicts.append({"identity": item["identity"],
                             "status": "new_execution_required",
                             "reason": "no_evidence", "deferred": False,
                             "verdict_known": False, "verdict": "UNKNOWN",
                             "evidence_id": "",
                             "resolved_test_identities": ["t-case-1"],
                             "unresolved_count": 0})
            queue.append(item["identity"])
            continue
        admitted = (item["evidence"]["subject_fingerprint"]
                    == item["current"]["subject_fingerprint"]
                    and bool(item["evidence"]["subject_fingerprint"]))
        status = "current" if admitted else "stale"
        reason = "evidence_current" if admitted else "input_changed"
        verdicts.append({"identity": item["identity"], "status": status,
                         "reason": reason, "deferred": False,
                         "verdict_known": True,
                         "verdict": item["evidence"]["verdict"],
                         "evidence_id": item["evidence"]["evidence_id"],
                         "resolved_test_identities": [],
                         "unresolved_count": 0})
        if not admitted:
            queue.append(item["identity"])
    result = {"schema_version": "mncs.test-verification-coherence/1",
              "verdicts": verdicts,
              "run_queue": queue[:request["max_executions"]],
              "summary": {"obligations": len(verdicts),
                          "current": sum(1 for v in verdicts
                                         if v["status"] == "current"),
                          "queued": len(queue), "deferred": 0,
                          "failed": 0, "unknown": 0,
                          "contradictory": 0, "unsupported": 0,
                          "unresolved": 0, "excluded": 0}}
    Path(argv[1]).write_text(json.dumps(result), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'''

TEST_STUB = '''\
import hashlib
import json
import sys
from pathlib import Path


def main(argv):
    suite = Path(argv[0])
    out = Path(argv[argv.index("--result") + 1])
    failing = "FAIL-MARKER" in suite.read_text(encoding="utf-8")
    verdict = "FAIL" if failing else "PASS"
    sha = hashlib.sha256(suite.read_bytes()).hexdigest()
    document = {
        "schema_version": "mncs.test-result/1",
        "verdict": verdict,
        "classification": "test_failure" if failing else "passed",
        "failure_class": "none",
        "provenance": {"source": {"path": str(suite), "sha256": sha}},
        "execution": {"test_case_identities": ["t-case-1"],
                      "inventory_identity": "inv-1",
                      "run_identity": "run-stub"},
        "native_suite_summary": {"verdict": verdict, "total": 1,
                                 "passed": 0 if failing else 1,
                                 "failed": 1 if failing else 0},
        "tests": [{"id": "t-case-1", "entry": "case",
                   "verdict": verdict,
                   "status": "failed" if failing else "passed",
                   "source": str(suite),
                   "source_span": {"line": 2, "column": 1},
                   "request": {"schema_version": "0.1",
                               "target": {"function": "case",
                                          "module": "fixture.suite"},
                               "arguments": [], "step_budget": 1000},
                   "native_result": {"failure_kind":
                                     "assertion" if failing else "nofailure"}}],
    }
    out.write_text(json.dumps(document), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'''

DIAGNOSTIC_COHERENCE_STUB = '''\
import json
import sys
from pathlib import Path


def decide(item):
    base = {"failure_key": item["failure_key"], "deferred": False,
            "witness_ref": "", "admitted_depth": "minimal"}
    if item["failure_class"] not in ("test_failure", "runtime_failure"):
        return {**base, "status": "unsupported",
                "reason": "failure_not_debuggable", "operation": "none"}
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


def main(argv):
    request = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    diagnostics = [decide(item) for item in request["failures"]]
    queue = [item["failure_key"] for item in diagnostics
             if item["status"] in ("capture_required", "extend_required")]
    result = {"schema_version": "mncs.debug-diagnostic-coherence/1",
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
    Path(argv[1]).write_text(json.dumps(result), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'''

DEBUG_STUB = '''\
import hashlib
import json
import sys
from pathlib import Path


def main(argv):
    # import-test <result> --test-id <id> ... --output <name>
    result = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    test_id = argv[argv.index("--test-id") + 1]
    output = Path(argv[argv.index("--output") + 1])
    if not output.is_absolute():
        output = Path.cwd() / output
    selected = next(item for item in result["tests"]
                    if item.get("id") == test_id)
    digest = hashlib.sha256(json.dumps(
        {"source": selected.get("source"),
         "request": selected.get("request")}, sort_keys=True).encode())
    witness = {
        "schema_version": "mncs.debug-witness/1",
        "witness_id": "mncs:debug:witness:" + digest.hexdigest(),
        "execution_identity": "mncs:debug:execution:" + digest.hexdigest(),
        "outcome": {"failure_class": "test_failure", "status": "test_failure"},
    }
    output.write_text(json.dumps(witness), encoding="utf-8")
    marker = Path.cwd() / "capture-count.txt"
    count = int(marker.read_text().strip()) if marker.exists() else 0
    marker.write_text(str(count + 1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'''

OBLIGATION = {
    "identity": "fixture-entry.debug-suite",
    "title": "Fixture entry suite",
    "lifecycle": "permanent",
    "scope": "local",
    "invalidation_dependencies": ["tests/suite.mncs"],
    "executor": {"provider": "fixture-test", "kind": "native_first_class_test",
                 "entrypoint": "fixture run",
                 "source_paths": ["tests/suite.mncs"],
                 "library_paths": [],
                 "declaration_identities": ["*"],
                 "verifier_identity": "fixture-runner/0.0"},
}


def git(repo: Path, *argv: str) -> None:
    subprocess.run(["git", "-C", str(repo), *argv], check=True,
                   capture_output=True, text=True, timeout=60)


def git_init(repo: Path) -> None:
    for argv in (["init", "-q", "-b", "main"], ["add", "."],
                 ["-c", "user.name=Diagnostics Entry Test",
                  "-c", "user.email=diag-entry@example.invalid",
                  "commit", "-qm", "fixture"]):
        git(repo, *argv)


class DiagnosticsEntryFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix="mncs-diagnostics-entry-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.entry = self.base / "work" / "entry"
        self.entry.mkdir(parents=True)
        self.state = self.base / "state"
        test_provider = self.entry / "fixture-test"
        (test_provider / ".mncs").mkdir(parents=True)
        (test_provider / "tools").mkdir(parents=True)
        (test_provider / "tools" / "coherence.py").write_text(
            VERIFY_COHERENCE_STUB)
        (test_provider / "tools" / "run_suite.py").write_text(TEST_STUB)
        (test_provider / ".mncs" / "project.json").write_text(json.dumps({
            "schema_version": "mncs-family.repository-manifest/v0alpha1",
            "repository": "fixture-test", "contracts": {"provides": []}}))
        (test_provider / "family-semantic-contracts-v1.json").write_text(
            json.dumps({
                "schema_version": "commons.mncs.semantic-contract-declarations/v1",
                "repository_id": "fixture-test", "revision": "1",
                "provides": [
                    {"contract_identity": "mncs.test-verification-coherence/1",
                     "contract_revision": "1",
                     "canonical_entrypoint": "fixture-coherence",
                     "status": "native_canonical",
                     "invocation": {"kind": "python",
                                    "path": "tools/coherence.py"}},
                    {"contract_identity": "mncs.test-result/1",
                     "contract_revision": "1",
                     "canonical_entrypoint": "fixture-test",
                     "status": "native_canonical",
                     "invocation": {"kind": "python",
                                    "path": "tools/run_suite.py"}},
                ],
                "consumes": []}))
        git_init(test_provider)
        debug_provider = self.entry / "fixture-debug"
        (debug_provider / ".mncs").mkdir(parents=True)
        (debug_provider / "tools").mkdir(parents=True)
        (debug_provider / "tools" / "coherence.py").write_text(
            DIAGNOSTIC_COHERENCE_STUB)
        (debug_provider / "tools" / "debug.py").write_text(DEBUG_STUB)
        (debug_provider / ".mncs" / "project.json").write_text(json.dumps({
            "schema_version": "mncs-family.repository-manifest/v0alpha1",
            "repository": "fixture-debug", "contracts": {"provides": []}}))
        (debug_provider / "family-semantic-contracts-v1.json").write_text(
            json.dumps({
                "schema_version": "commons.mncs.semantic-contract-declarations/v1",
                "repository_id": "fixture-debug", "revision": "1",
                "provides": [
                    {"contract_identity": "mncs.debug-diagnostic-coherence/1",
                     "contract_revision": "1",
                     "canonical_entrypoint": "fixture-diagnostic-coherence",
                     "status": "native_canonical",
                     "invocation": {"kind": "python",
                                    "path": "tools/coherence.py"}},
                    {"contract_identity": "mncs.debugger/1",
                     "contract_revision": "1",
                     "canonical_entrypoint": "fixture-debug",
                     "status": "native_canonical",
                     "effects": ["verify"],
                     "invocation": {"kind": "python",
                                    "path": "tools/debug.py"}},
                ],
                "consumes": []}))
        git_init(debug_provider)
        target = self.entry / "target-repo"
        (target / ".mncs").mkdir(parents=True)
        (target / "tests").mkdir(parents=True)
        (target / "tests" / "suite.mncs").write_text(
            "mncs 0.18;\nmodule fixture.suite;\n")
        (target / ".mncs" / "project.json").write_text(json.dumps({
            "schema_version": "mncs-family.repository-manifest/v0alpha1",
            "repository": "target-repo",
            "verification": {"obligation_inventory": ".mncs/obligations.json"},
            "contracts": {"provides": []}}))
        (target / ".mncs" / "obligations.json").write_text(json.dumps({
            "schema_version": "mncs-family.verification-obligation-inventory/v1",
            "repository": "target-repo", "revision": 1,
            "obligations": [OBLIGATION]}))
        git_init(target)
        self.target = target
        (self.entry / ".mncs").mkdir(parents=True)
        self.write_definition({})

    def write_definition(self, diagnostics_knob: dict | None) -> None:
        definition = {
            "name": "fixture-diagnostics", "workspace_root": "..",
            "workspace_scope": {"kind": "workspace",
                                "repositories": ["fixture-test",
                                                 "fixture-debug",
                                                 "target-repo"]},
            "required_capabilities": ["mncs.test-verification-coherence/1",
                                      "mncs.test-result/1",
                                      "mncs.debug-diagnostic-coherence/1",
                                      "mncs.debugger/1"],
            "intent": {"goal": "exercise ambient diagnostics",
                       "repositories": []}}
        if diagnostics_knob is not None:
            definition["diagnostics"] = diagnostics_knob
        (self.entry / ".mncs" / "environment.json").write_text(
            json.dumps(definition))

    def run_cli(self, *argv: str, backend: str = "file"):
        result = subprocess.run(
            [sys.executable, str(CLI), "--state-dir", str(self.state),
             "--persistence", backend, *argv], cwd=str(self.entry),
            capture_output=True, text=True, timeout=180,
            env=dict(os.environ))
        try:
            payload = json.loads(result.stdout or result.stderr)
        except ValueError:
            payload = {"raw_stdout": result.stdout,
                       "raw_stderr": result.stderr}
        return result.returncode, payload

    def enter(self, consumer: str):
        return self.run_cli("enter", "--consumer", consumer)

    def set_failing(self, failing: bool) -> None:
        suite = self.target / "tests" / "suite.mncs"
        text = suite.read_text(encoding="utf-8")
        if failing and "FAIL-MARKER" not in text:
            suite.write_text(text + "# FAIL-MARKER\n", encoding="utf-8")
        elif not failing:
            suite.write_text(text.replace("# FAIL-MARKER\n", ""),
                             encoding="utf-8")

    def capture_count(self, session: str) -> int:
        captures = (self.state / "sessions" / session
                    / "diagnostic-artifacts" / "captures")
        if not captures.is_dir():
            return 0
        return sum(1 for child in captures.iterdir() if child.is_dir())


class EntryWiringTests(DiagnosticsEntryFixture):
    def test_healthy_entry_omits_diagnostic_block(self):
        code, payload = self.enter("diag-healthy")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["verification"]["summary"]["failed"], 0)
        self.assertNotIn("diagnostic", payload)

    def test_failing_entry_captures_tiny_capsule(self):
        self.set_failing(True)
        code, payload = self.enter("diag-fail")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["verification"]["summary"]["failed"], 1)
        self.assertIn("diagnostic", payload)
        summary = payload["diagnostic"]["summary"]
        self.assertEqual(summary["failures"], 1)
        self.assertEqual(summary["captured"], 1)
        self.assertEqual(summary["blockers"], 0)
        self.assertEqual(len(summary["capsule_ids"]), 1)
        self.assertLess(len(json.dumps(payload["diagnostic"])), 1500)

    def test_reentry_reuses_witness_without_recapture(self):
        self.set_failing(True)
        code, payload = self.enter("diag-quiet")
        self.assertEqual(code, 0, payload)
        session = payload["session_id"]
        self.assertEqual(self.capture_count(session), 1)
        code, payload = self.enter("diag-quiet")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["session_id"], session)
        self.assertTrue(payload["diagnostic"]["reused"])
        self.assertEqual(self.capture_count(session), 1)

    def test_suite_edit_recaptures_and_fix_clears(self):
        self.set_failing(True)
        code, payload = self.enter("diag-edit")
        self.assertEqual(code, 0, payload)
        session = payload["session_id"]
        with open(self.target / "tests" / "suite.mncs", "a",
                  encoding="utf-8") as handle:
            handle.write("# edit\n")
        code, payload = self.enter("diag-edit")
        self.assertEqual(code, 0, payload)
        self.assertFalse(payload["diagnostic"]["reused"])
        self.assertEqual(self.capture_count(session), 2)
        self.set_failing(False)
        code, payload = self.enter("diag-edit")
        self.assertEqual(code, 0, payload)
        self.assertNotIn("diagnostic", payload)

    def test_diagnostic_command_reports_extends_and_shows_evidence(self):
        self.set_failing(True)
        code, payload = self.enter("diag-cmd")
        self.assertEqual(code, 0, payload)
        session = payload["session_id"]
        key = payload["diagnostic"]["summary"]["capsule_ids"][0]
        code, payload = self.run_cli("diagnostic", session)
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["reused"])
        code, payload = self.run_cli("diagnostic", session, "--evidence")
        self.assertEqual(code, 0, payload)
        self.assertIsNotNone(payload["evidence"])
        code, payload = self.run_cli("diagnostic", session, "--depth",
                                     "standard", "--only", key)
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["summary"]["depth"], "standard")
        self.assertEqual(self.capture_count(session), 2)

    def test_definition_can_disable_ambient_diagnostics(self):
        self.set_failing(True)
        self.write_definition({"enabled": False})
        code, payload = self.enter("diag-off")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["verification"]["summary"]["failed"], 1)
        self.assertNotIn("diagnostic", payload)

    def test_invalid_knob_fails_entry_closed(self):
        self.write_definition({"max_captures": 99})
        code, payload = self.enter("diag-bad-knob")
        self.assertNotEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
