"""Ambient verification entry wiring: real CLI, stub providers.

A fixture workspace carries a stub test provider (coherence + suite
execution through declared family contracts) and a target repository
with one native verification obligation. Entry runs the ambient
verification pass; re-entry reuses the epoch; edits invalidate.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "mncs-env"
sys.path.insert(0, str(ROOT))

from mncs_env import entry as entry_module  # noqa: E402

COHERENCE_STUB = '''\
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
        if admitted:
            verdicts.append({"identity": item["identity"],
                             "status": "current",
                             "reason": "evidence_current",
                             "deferred": False, "verdict_known": True,
                             "verdict": item["evidence"]["verdict"],
                             "evidence_id": item["evidence"]["evidence_id"],
                             "resolved_test_identities": [],
                             "unresolved_count": 0})
        else:
            verdicts.append({"identity": item["identity"],
                             "status": "stale", "reason": "input_changed",
                             "deferred": False, "verdict_known": True,
                             "verdict": item["evidence"]["verdict"],
                             "evidence_id": item["evidence"]["evidence_id"],
                             "resolved_test_identities": ["t-case-1"],
                             "unresolved_count": 0})
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
import json
import sys
from pathlib import Path


def main(argv):
    out = Path(argv[argv.index("--result") + 1])
    document = {
        "schema_version": "mncs.test-result/1",
        "verdict": "PASS", "classification": "passed",
        "failure_class": "none",
        "execution": {"test_case_identities": ["t-case-1"],
                      "inventory_identity": "inv-1",
                      "run_identity": "run-stub"},
        "native_suite_summary": {"verdict": "PASS", "total": 1,
                                 "passed": 1, "failed": 0},
        "tests": [{"id": "t-case-1", "entry": "case",
                   "verdict": "PASS", "status": "passed"}],
    }
    out.write_text(json.dumps(document), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'''

OBLIGATION = {
    "identity": "fixture-entry.native-suite",
    "title": "Fixture entry suite",
    "lifecycle": "permanent",
    "scope": "repository_canonical",
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
                 ["-c", "user.name=Verification Entry Test",
                  "-c", "user.email=verify-entry@example.invalid",
                  "commit", "-qm", "fixture"]):
        git(repo, *argv)


class VerificationEntryFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix="mncs-verification-entry-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.entry = self.base / "work" / "entry"
        self.entry.mkdir(parents=True)
        self.state = self.base / "state"
        provider = self.entry / "fixture-test"
        (provider / ".mncs").mkdir(parents=True)
        (provider / "tools").mkdir(parents=True)
        (provider / "tools" / "coherence.py").write_text(COHERENCE_STUB)
        (provider / "tools" / "run_suite.py").write_text(TEST_STUB)
        (provider / ".mncs" / "project.json").write_text(json.dumps({
            "schema_version": "mncs-family.repository-manifest/v0alpha1",
            "repository": "fixture-test", "contracts": {"provides": []}}))
        (provider / "family-semantic-contracts-v1.json").write_text(
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
        git_init(provider)
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

    def write_definition(self, verification_knob: dict | None) -> None:
        definition = {
            "name": "fixture-verification", "workspace_root": "..",
            "workspace_scope": {"kind": "workspace",
                                "repositories": ["fixture-test", "target-repo"]},
            "required_capabilities": ["mncs.test-verification-coherence/1",
                                      "mncs.test-result/1"],
            "intent": {"goal": "exercise ambient verification",
                       "repositories": []}}
        if verification_knob is not None:
            definition["verification"] = verification_knob
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

    def commit_all(self, repo: Path, message: str):
        git(repo, "add", "-A")
        git(repo, "-c", "user.name=Verification Entry Test",
            "-c", "user.email=verify-entry@example.invalid",
            "commit", "-qm", message)


class EntryWiringTests(VerificationEntryFixture):
    def test_ambient_verification_preserves_retry_and_completed_domain_outcomes(self):
        with patch.object(entry_module.verification, "ambient_pass", return_value={
                "summary": {"obligations": 1, "blockers": 1},
                "reused": False, "evidence": "ev-retry",
                "operation_status": "retry"}):
            result = entry_module._ambient_verification(object(), {})
            self.assertEqual(result["operation_status"], "retry")
        with patch.object(entry_module.verification, "ambient_pass", return_value={
                "summary": {"obligations": 1, "blockers": 1},
                "reused": False, "evidence": "ev-domain-fail",
                "operation_status": "complete"}):
            result = entry_module._ambient_verification(object(), {})
            self.assertEqual(result["operation_status"], "complete")

    def test_first_entry_verifies_and_reentry_is_quiet(self):
        code, payload = self.enter("verify-wiring")
        self.assertEqual(code, 0, payload)
        summary = payload["verification"]["summary"]
        self.assertEqual(summary["executed"], 1)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["blockers"], 0)
        session = payload["session_id"]
        code, payload = self.enter("verify-wiring")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["session_id"], session)
        self.assertTrue(payload["verification"]["reused"])

    def test_suite_edit_reverifies_only_after_change(self):
        code, payload = self.enter("verify-edit")
        self.assertEqual(code, 0, payload)
        session = payload["session_id"]
        (self.target / "tests" / "suite.mncs").write_text(
            "mncs 0.18;\nmodule fixture.suite;\n# edit\n")
        code, payload = self.enter("verify-edit")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["session_id"], session)
        self.assertFalse(payload["verification"]["reused"])
        self.assertEqual(payload["verification"]["summary"]["executed"], 1)

    def test_verify_command_reports_and_shows_evidence(self):
        code, payload = self.enter("verify-cmd")
        self.assertEqual(code, 0, payload)
        session = payload["session_id"]
        code, payload = self.run_cli("verify", session)
        self.assertEqual(code, 0, payload)
        self.assertTrue(payload["reused"])
        code, payload = self.run_cli("verify", session, "--evidence")
        self.assertEqual(code, 0, payload)
        self.assertIsNotNone(payload["evidence"])

    def test_verify_only_runs_one_obligation(self):
        code, payload = self.enter("verify-only")
        self.assertEqual(code, 0, payload)
        session = payload["session_id"]
        code, payload = self.run_cli(
            "verify", session, "--only", "fixture-entry.native-suite")
        self.assertEqual(code, 0, payload)
        self.assertEqual(payload["summary"]["obligations"], 1)

    def test_definition_can_disable_ambient_verification(self):
        self.write_definition({"enabled": False})
        code, payload = self.enter("verify-off")
        self.assertEqual(code, 0, payload)
        self.assertFalse(payload["verification"]["summary"]["enabled"])

    def test_invalid_knob_fails_entry_closed(self):
        self.write_definition({"max_executions": 99})
        code, payload = self.enter("verify-bad-knob")
        self.assertNotEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
