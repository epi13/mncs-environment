"""Ambient external-evidence coherence: lifecycle around native policy.

Fake sessions drive ambient_pass with stubbed provider invocations, so
observation/epoch/evidence/dispatch behavior is exercised hermetically.
The native external-evidence policy itself is covered in mncs-actions;
live composition against the real workspace is exercised as scenarios.
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

from mncs_env import actions as actions_module  # noqa: E402
from mncs_env import authority as authority_module  # noqa: E402


OBLIGATION = "ob.external-one"


def git(repo: Path, *argv: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *argv], check=True,
        capture_output=True, text=True, timeout=60)
    return completed.stdout


def git_init(repo: Path) -> None:
    for argv in (["init", "-q", "-b", "main"], ["add", "."],
                 ["-c", "user.name=Actions Test",
                  "-c", "user.email=actions@example.invalid",
                  "commit", "-qm", "fixture"]):
        git(repo, *argv)


def write_repo(root: Path, name: str, *, external: dict | None = None,
               dirty: bool = False) -> Path:
    repo = root / name
    (repo / ".mncs").mkdir(parents=True, exist_ok=True)
    (repo / ".mncs" / "project.json").write_text(json.dumps({
        "schema_version": "mncs-family.repository-manifest/v0alpha1",
        "repository": name,
        "verification": {"obligation_inventory": ".mncs/verification-obligations.json"},
    }), encoding="utf-8")
    executor: dict = {"provider": name, "kind": "external_integration",
                      "entrypoint": "make check"}
    if external is not None:
        executor["external"] = external
    (repo / ".mncs" / "verification-obligations.json").write_text(json.dumps({
        "schema_version": "mncs-family.verification-obligation-inventory/v1",
        "repository": name,
        "obligations": [{
            "identity": OBLIGATION if name == "fixture-repo" else f"{name}.ob",
            "lifecycle": "permanent",
            "executor": executor,
        }],
    }), encoding="utf-8")
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    git_init(repo)
    if dirty:
        (repo / "README.md").write_text("dirty\n", encoding="utf-8")
    return repo


def publish(repo: Path) -> None:
    """Pretend the fixture HEAD is published via a local remote ref."""
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")


BINDING = {"repository": "o/fixture-repo", "workflow": "ci.yml",
           "artifact": "mncs-obligation-evidence",
           "check_identity": OBLIGATION}


class FakeStore:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir


class FakeSession:
    def __init__(self, workspace_root: Path, state_dir: Path):
        self.session_id = "ses_actions_fixture"
        self.snapshot: dict = {
            "workspace": {"root": str(workspace_root)},
            "lifecycle": "active",
            "consumer_id": "actions-test",
            "toolchain": {"binary": "/bin/mncs", "revision": "rev-1",
                          "checkout": "/chk", "repository": "mncs-language"},
            "bindings": [
                {"capability": actions_module.COHERENCE_CAPABILITY,
                 "availability": {"status": "available"}},
                {"capability": actions_module.REMOTE_CAPABILITY,
                 "availability": {"status": "available"}},
                {"capability": actions_module.DISPATCH_CAPABILITY,
                 "availability": {"status": "available"},
                 "provider": "mncs-actions", "effects": ["delegate"]},
            ],
        }
        self.store = FakeStore(state_dir)
        self.saved = 0
        self.coherence_calls: list[dict] = []
        self.remote_calls: list[list[str]] = []
        self.dispatch_calls: list[list[str]] = []
        self.emitted: list[tuple[str, dict]] = []
        self.coherence_mode = "policy"
        self.remote_mode: str | dict = "no-runs"
        self.dispatch_mode = "dispatched"
        self.revision = "rev-fixture"

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
        return {"verdict": "allow", "reason": "fixture allows"}

    def invoke(self, capability: str, argv: list[str], *,
               cwd=None, timeout_seconds=None,
               output_limit_bytes=None, env=None) -> dict:
        if capability == actions_module.COHERENCE_CAPABILITY:
            return self._invoke_coherence(argv, cwd)
        if capability == actions_module.REMOTE_CAPABILITY:
            return self._invoke_remote(argv, cwd)
        if capability == actions_module.DISPATCH_CAPABILITY:
            return self._invoke_dispatch(argv)
        raise AssertionError(f"unexpected capability {capability}")

    def _invoke_coherence(self, argv: list[str], cwd) -> dict:
        workdir = Path(cwd)
        request = json.loads((workdir / argv[0]).read_text(encoding="utf-8"))
        self.coherence_calls.append(request)
        if self.coherence_mode == "fail":
            return {"status": "error", "stderr": "stub coherence failure"}
        decisions = []
        queue = []
        for item in request["obligations"]:
            verdict = self._policy(item)
            decisions.append(verdict)
            if verdict["status"] == "dispatch_eligible":
                queue.append(item["obligation"])
        result = {"schema_version": actions_module.COHERENCE_RESULT_SCHEMA,
                  "decisions": decisions,
                  "dispatch_queue": queue[:request["max_dispatches"]],
                  "summary": {"obligations": len(decisions),
                              "current": sum(1 for v in decisions
                                             if v["status"] == "current"),
                              "dispatch_queued": len(queue),
                              "pending": sum(1 for v in decisions
                                             if v["status"] == "delegated_pending"),
                              "deferred": 0,
                              "unsupported": sum(1 for v in decisions
                                                 if v["status"] in ("no_route", "unavailable")),
                              "escalated": sum(1 for v in decisions
                                               if v["status"] == "escalate")}}
        (workdir / argv[1]).write_text(json.dumps(result), encoding="utf-8")
        return {"status": "ok", "stdout": "", "stderr": ""}

    @staticmethod
    def _policy(item: dict) -> dict:
        base = {"obligation": item["obligation"], "deferred": False,
                "verdict": "none", "claim": "none", "run_identity": ""}
        if not item["routable_kind"]:
            return {**base, "status": "no_route",
                    "reason": "non_routable_kind", "operation": "none"}
        if not item["has_workflow_binding"]:
            return {**base, "status": "no_route",
                    "reason": "no_workflow_binding", "operation": "none"}
        if item["explicit_only"]:
            return {**base, "status": "escalate",
                    "reason": "explicit_only_effects", "operation": "none"}
        if item["subject_state"] == "dirty":
            return {**base, "status": "unavailable",
                    "reason": "dirty_subject", "operation": "none"}
        if item["subject_state"] == "clean_unpublished":
            return {**base, "status": "unavailable",
                    "reason": "unpublished_subject", "operation": "none"}
        if item["subject_state"] != "clean":
            return {**base, "status": "unavailable",
                    "reason": "unknown_subject", "operation": "none"}
        evidence = item["evidence"]
        if item["evidence_present"] and evidence["obligation"] == item["obligation"] \
                and evidence["subject_digest"] == item["subject_digest"] \
                and evidence["complete"] and evidence["claim"] == "established":
            return {**base, "status": "current",
                    "reason": "evidence_current", "operation": "none",
                    "verdict": evidence["verdict"], "claim": evidence["claim"],
                    "run_identity": evidence["run_identity"]}
        run = item["run"]
        if item["run_present"] and run["subject_digest"] == item["subject_digest"] \
                and run["state"] == "in_progress":
            return {**base, "status": "delegated_pending",
                    "reason": "run_pending", "operation": "subscribe",
                    "run_identity": run["run_identity"]}
        if item["run_present"] and run["subject_digest"] == item["subject_digest"] \
                and run["state"] == "completed":
            return {**base, "status": "delegated_pending",
                    "reason": "run_completed_unadmitted",
                    "operation": "subscribe",
                    "run_identity": run["run_identity"]}
        if item["attempts_exhausted"]:
            return {**base, "status": "deferred",
                    "reason": "attempts_exhausted", "operation": "none"}
        if not item["remote_available"]:
            return {**base, "status": "deferred",
                    "reason": "remote_unavailable", "operation": "none"}
        return {**base, "status": "dispatch_eligible",
                "reason": "no_evidence", "operation": "dispatch"}

    def _invoke_remote(self, argv: list[str], cwd) -> dict:
        self.remote_calls.append(list(argv))
        mode = self.remote_mode
        if isinstance(mode, dict):
            envelope = mode.get(argv[0], {"transport": "ok"})
            if argv[0] == "fetch":
                Path(argv[argv.index("--output-dir") + 1]).mkdir(
                    parents=True, exist_ok=True)
            return {"status": "ok", "stdout": json.dumps(envelope), "stderr": ""}
        if argv[0] == "status":
            if mode == "auth-missing":
                return {"status": "ok", "stdout": json.dumps(
                    {"transport": "auth_missing", "detail": "not logged in"}),
                    "stderr": ""}
            if mode == "workflow-missing":
                return {"status": "ok", "stdout": json.dumps(
                    {"transport": "workflow_missing", "detail": "no workflow"}),
                    "stderr": ""}
            if mode == "in-progress":
                return {"status": "ok", "stdout": json.dumps(
                    {"transport": "ok", "runs": [{
                        "run_id": 4242, "head_sha": self.revision,
                        "status": "in_progress", "conclusion": None,
                        "workflow_path": ".github/workflows/ci.yml",
                        "created_at": "2026-10-02T00:00:00Z"}]}), "stderr": ""}
            if mode == "completed-pass":
                return {"status": "ok", "stdout": json.dumps(
                    {"transport": "ok", "runs": [{
                        "run_id": 4343, "head_sha": self.revision,
                        "status": "completed", "conclusion": "success",
                        "workflow_path": ".github/workflows/ci.yml",
                        "created_at": "2026-10-02T00:00:00Z"}]}), "stderr": ""}
            return {"status": "ok",
                    "stdout": json.dumps({"transport": "ok", "runs": []}),
                    "stderr": ""}
        if argv[0] == "fetch":
            Path(argv[argv.index("--output-dir") + 1]).mkdir(
                parents=True, exist_ok=True)
            return {"status": "ok",
                    "stdout": json.dumps({"transport": "ok", "files": []}),
                    "stderr": ""}
        if argv[0] == "validate":
            return {"status": "ok", "stdout": json.dumps(
                {"transport": "ok", "valid": True, "projected": {
                    "claim_status": "ESTABLISHED", "verdict": "PASS",
                    "check_id": OBLIGATION, "check_verdict": "PASS",
                    "repository": "o/fixture-repo", "revision": self.revision,
                    "run_id": "4343", "workflow": "CI"}}), "stderr": ""}
        raise AssertionError(f"unexpected remote argv {argv}")

    def _invoke_dispatch(self, argv: list[str]) -> dict:
        self.dispatch_calls.append(list(argv))
        if self.dispatch_mode == "escalated":
            return {"status": "pending-escalation", "stderr": "delegate requires escalation"}
        if self.dispatch_mode == "claim-held":
            return {"status": "ok", "stdout": json.dumps(
                {"transport": "claim_held", "detail": "already claimed"}),
                "stderr": ""}
        if self.dispatch_mode == "advanced":
            return {"status": "ok", "stdout": json.dumps(
                {"transport": "ok", "identity": "dispatch-1",
                 "run_identity": "4545",
                 "run": {"run_id": 4545, "head_sha": "some-other-sha"}}),
                "stderr": ""}
        return {"status": "ok", "stdout": json.dumps(
            {"transport": "ok", "identity": "dispatch-1",
             "run_identity": "4545",
             "run": {"run_id": 4545, "head_sha": self.revision}}), "stderr": ""}


class ActionsLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="actions-test-")
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        self.state = root / "state"
        self.state.mkdir()

    def _session(self) -> FakeSession:
        return FakeSession(self.workspace, self.state)

    def test_no_external_obligations_is_quiet(self) -> None:
        session = self._session()
        outcome = actions_module.ambient_pass(session)
        summary = outcome["summary"]
        self.assertEqual(summary["obligations"], 0)
        self.assertEqual(summary["blockers"], 0)
        self.assertEqual(session.remote_calls, [])
        self.assertEqual(session.dispatch_calls, [])

    def test_unbound_obligation_has_no_route_without_remote(self) -> None:
        write_repo(self.workspace, "fixture-repo")
        session = self._session()
        outcome = actions_module.ambient_pass(session)
        summary = outcome["summary"]
        self.assertEqual(summary["obligations"], 1)
        self.assertEqual(session.remote_calls, [])
        evidence = json.loads(
            (self.state / outcome["evidence"]).read_text(encoding="utf-8"))
        self.assertEqual(evidence["results"][0]["status"], "no_route")
        self.assertEqual(evidence["results"][0]["reason"], "no_workflow_binding")

    def test_dirty_subject_is_unavailable_without_remote(self) -> None:
        write_repo(self.workspace, "fixture-repo", external=dict(BINDING),
                   dirty=True)
        session = self._session()
        outcome = actions_module.ambient_pass(session)
        evidence = json.loads(
            (self.state / outcome["evidence"]).read_text(encoding="utf-8"))
        self.assertEqual(evidence["results"][0]["status"], "unavailable")
        self.assertEqual(evidence["results"][0]["reason"], "dirty_subject")
        self.assertEqual(session.remote_calls, [])

    def test_unpublished_revision_is_unavailable_without_remote(self) -> None:
        write_repo(self.workspace, "fixture-repo", external=dict(BINDING))
        session = self._session()
        outcome = actions_module.ambient_pass(session)
        evidence = json.loads(
            (self.state / outcome["evidence"]).read_text(encoding="utf-8"))
        self.assertEqual(evidence["results"][0]["status"], "unavailable")
        self.assertEqual(evidence["results"][0]["reason"], "unpublished_subject")
        self.assertEqual(session.remote_calls, [])

    def test_missing_evidence_is_eligible_with_delegate_request(self) -> None:
        repo = write_repo(self.workspace, "fixture-repo",
                          external=dict(BINDING))
        publish(repo)
        session = self._session()
        session.revision = git(repo, "rev-parse", "HEAD").strip()
        outcome = actions_module.ambient_pass(session)
        summary = outcome["summary"]
        self.assertEqual(summary["eligible"], 1)
        self.assertEqual(summary["delegate_requests"], [OBLIGATION])
        self.assertEqual(session.dispatch_calls, [])
        self.assertTrue(any(call[0] == "status" for call in session.remote_calls))

    def test_ambient_never_dispatches(self) -> None:
        repo = write_repo(self.workspace, "fixture-repo",
                          external=dict(BINDING))
        publish(repo)
        session = self._session()
        session.revision = git(repo, "rev-parse", "HEAD").strip()
        session.dispatch_mode = "dispatched"
        outcome = actions_module.ambient_pass(session)
        self.assertEqual(outcome["summary"]["dispatched"], 0)
        self.assertEqual(session.dispatch_calls, [])

    def test_in_flight_run_is_pending_without_redispatch(self) -> None:
        repo = write_repo(self.workspace, "fixture-repo",
                          external=dict(BINDING))
        publish(repo)
        session = self._session()
        session.revision = git(repo, "rev-parse", "HEAD").strip()
        session.remote_mode = "in-progress"
        outcome = actions_module.ambient_pass(session)
        summary = outcome["summary"]
        self.assertEqual(summary["pending"], 1)
        self.assertEqual(summary["eligible"], 0)
        self.assertEqual(session.dispatch_calls, [])
        row = session.snapshot["actions_state"][OBLIGATION]
        self.assertEqual(row["run"]["run_identity"], "4242")

    def test_completed_run_admits_staged_evidence(self) -> None:
        repo = write_repo(self.workspace, "fixture-repo",
                          external=dict(BINDING))
        publish(repo)
        session = self._session()
        session.revision = git(repo, "rev-parse", "HEAD").strip()
        session.remote_mode = "completed-pass"
        outcome = actions_module.ambient_pass(session)
        summary = outcome["summary"]
        self.assertEqual(summary["current"], 1)
        row = session.snapshot["actions_state"][OBLIGATION]
        self.assertEqual(row["evidence"]["verdict"], "passed")
        self.assertEqual(row["evidence"]["run_identity"], "4343")
        kinds = [kind for kind, _payload in session.emitted]
        self.assertIn("external.admitted", kinds)

    def test_receipt_mismatch_never_admits(self) -> None:
        repo = write_repo(self.workspace, "fixture-repo",
                          external=dict(BINDING))
        publish(repo)
        session = self._session()
        revision = git(repo, "rev-parse", "HEAD").strip()
        session.revision = revision
        session.remote_mode = {
            "status": {"transport": "ok", "runs": [{
                "run_id": 4343, "head_sha": revision, "status": "completed",
                "conclusion": "success",
                "workflow_path": ".github/workflows/ci.yml",
                "created_at": "2026-10-02T00:00:00Z"}]},
            "fetch": {"transport": "ok", "files": []},
            "validate": {"transport": "ok", "valid": True, "projected": {
                "claim_status": "ESTABLISHED", "verdict": "PASS",
                "check_id": "some-other-check", "check_verdict": "PASS",
                "repository": "o/fixture-repo", "revision": revision,
                "run_id": "4343", "workflow": "CI"}},
        }
        outcome = actions_module.ambient_pass(session)
        summary = outcome["summary"]
        self.assertEqual(summary["current"], 0)
        self.assertEqual(summary["eligible"], 1)
        row = session.snapshot.get("actions_state", {}).get(OBLIGATION, {})
        self.assertNotIn("evidence", row)

    def test_explicit_dispatch_executes_with_grant(self) -> None:
        repo = write_repo(self.workspace, "fixture-repo",
                          external=dict(BINDING))
        publish(repo)
        session = self._session()
        session.revision = git(repo, "rev-parse", "HEAD").strip()
        outcome = actions_module.ambient_pass(session, mode="explicit",
                                              dispatch=True)
        summary = outcome["summary"]
        self.assertEqual(summary["dispatched"], 1)
        self.assertEqual(len(session.dispatch_calls), 1)
        row = session.snapshot["actions_state"][OBLIGATION]
        self.assertEqual(row["run"]["run_identity"], "4545")

    def test_dispatch_escalation_burns_no_attempt(self) -> None:
        repo = write_repo(self.workspace, "fixture-repo",
                          external=dict(BINDING))
        publish(repo)
        session = self._session()
        session.revision = git(repo, "rev-parse", "HEAD").strip()
        session.dispatch_mode = "escalated"
        outcome = actions_module.ambient_pass(session, mode="explicit",
                                              dispatch=True)
        summary = outcome["summary"]
        self.assertEqual(summary["dispatched"], 0)
        evidence = json.loads(
            (self.state / outcome["evidence"]).read_text(encoding="utf-8"))
        self.assertEqual(evidence["results"][0]["detail"], "escalation-required")
        row = session.snapshot["actions_state"][OBLIGATION]
        self.assertEqual(row.get("attempts") or {}, {})

    def test_claim_held_dedupes_without_attempt(self) -> None:
        repo = write_repo(self.workspace, "fixture-repo",
                          external=dict(BINDING))
        publish(repo)
        session = self._session()
        session.revision = git(repo, "rev-parse", "HEAD").strip()
        session.dispatch_mode = "claim-held"
        outcome = actions_module.ambient_pass(session, mode="explicit",
                                              dispatch=True)
        evidence = json.loads(
            (self.state / outcome["evidence"]).read_text(encoding="utf-8"))
        self.assertEqual(evidence["results"][0]["outcome"], "deduped")
        row = session.snapshot["actions_state"][OBLIGATION]
        self.assertEqual(row.get("attempts") or {}, {})

    def test_epoch_reuse_skips_remote(self) -> None:
        repo = write_repo(self.workspace, "fixture-repo",
                          external=dict(BINDING))
        publish(repo)
        session = self._session()
        session.revision = git(repo, "rev-parse", "HEAD").strip()
        first = actions_module.ambient_pass(session)
        self.assertFalse(first["reused"])
        calls = len(session.remote_calls)
        self.assertGreater(calls, 0)
        second = actions_module.ambient_pass(session)
        self.assertTrue(second["reused"])
        self.assertEqual(len(session.remote_calls), calls)

    def test_subject_change_invalidates_evidence(self) -> None:
        repo = write_repo(self.workspace, "fixture-repo",
                          external=dict(BINDING))
        publish(repo)
        session = self._session()
        session.revision = git(repo, "rev-parse", "HEAD").strip()
        session.remote_mode = "completed-pass"
        first = actions_module.ambient_pass(session)
        self.assertEqual(first["summary"]["current"], 1)
        (repo / "NEXT.md").write_text("next\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "-c", "user.name=Actions Test",
            "-c", "user.email=actions@example.invalid",
            "commit", "-qm", "second")
        publish(repo)
        session.revision = git(repo, "rev-parse", "HEAD").strip()
        session.remote_mode = "no-runs"
        second = actions_module.ambient_pass(session)
        self.assertFalse(second["reused"])
        self.assertEqual(second["summary"]["current"], 0)
        self.assertEqual(second["summary"]["eligible"], 1)

    def test_workflow_missing_caches_no_route(self) -> None:
        repo = write_repo(self.workspace, "fixture-repo",
                          external=dict(BINDING))
        publish(repo)
        session = self._session()
        session.revision = git(repo, "rev-parse", "HEAD").strip()
        session.remote_mode = "workflow-missing"
        first = actions_module.ambient_pass(session)
        evidence = json.loads(
            (self.state / first["evidence"]).read_text(encoding="utf-8"))
        self.assertEqual(evidence["results"][0]["status"], "no_route")
        calls = len(session.remote_calls)
        self.assertGreater(calls, 0)
        # A fresh session object over the same snapshot must not re-probe:
        # the missing binding is cached for this declaration.
        again = FakeSession(self.workspace, self.state)
        again.snapshot.update(session.snapshot)
        again.revision = session.revision
        again.remote_mode = "workflow-missing"
        actions_module.ambient_pass(again)
        self.assertEqual(again.remote_calls, [])

    def test_auth_failure_defers_then_retries_after_ttl(self) -> None:
        repo = write_repo(self.workspace, "fixture-repo",
                          external=dict(BINDING))
        publish(repo)
        session = self._session()
        session.revision = git(repo, "rev-parse", "HEAD").strip()
        session.remote_mode = "auth-missing"
        actions_module.ambient_pass(session)
        row = session.snapshot["actions_state"][OBLIGATION]
        self.assertEqual(row.get("remote_error"), "auth_missing")
        row["remote_error_at"] = 0.0
        session.snapshot["actions_state"][OBLIGATION] = row
        session.remote_mode = "no-runs"
        calls = len(session.remote_calls)
        outcome = actions_module.ambient_pass(session, mode="explicit")
        self.assertEqual(outcome["summary"]["eligible"], 1)
        self.assertGreater(len(session.remote_calls), calls)

    def test_terminal_stage_failure_stays_eligible_without_refetch(self) -> None:
        repo = write_repo(self.workspace, "fixture-repo",
                          external=dict(BINDING))
        publish(repo)
        session = self._session()
        revision = git(repo, "rev-parse", "HEAD").strip()
        session.revision = revision
        session.remote_mode = {
            "status": {"transport": "ok", "runs": [{
                "run_id": 4343, "head_sha": revision, "status": "completed",
                "conclusion": "success",
                "workflow_path": ".github/workflows/ci.yml",
                "created_at": "2026-10-02T00:00:00Z"}]},
            "fetch": {"transport": "artifact_missing", "detail": "expired"},
        }
        first = actions_module.ambient_pass(session)
        self.assertEqual(first["summary"]["eligible"], 1)
        row = session.snapshot["actions_state"][OBLIGATION]
        self.assertEqual(row["stage_failed"]["run_identity"], "4343")
        calls_after_first = len(session.remote_calls)
        second = actions_module.ambient_pass(session)
        self.assertEqual(second["summary"]["eligible"], 1)
        # The dead run is recognized without re-fetching its artifacts:
        # only the cheap status observation repeats, never fetch/validate.
        kinds = [call[0] for call in session.remote_calls[calls_after_first:]]
        self.assertNotIn("fetch", kinds)
        self.assertNotIn("validate", kinds)
        calls = len(session.remote_calls)
        third = actions_module.ambient_pass(session)
        self.assertTrue(third["reused"])
        self.assertEqual(len(session.remote_calls), calls)

    def test_superseding_run_clears_stage_failure(self) -> None:
        repo = write_repo(self.workspace, "fixture-repo",
                          external=dict(BINDING))
        publish(repo)
        session = self._session()
        revision = git(repo, "rev-parse", "HEAD").strip()
        session.revision = revision
        session.remote_mode = {
            "status": {"transport": "ok", "runs": [{
                "run_id": 4343, "head_sha": revision, "status": "completed",
                "conclusion": "success",
                "workflow_path": ".github/workflows/ci.yml",
                "created_at": "2026-10-02T00:00:00Z"}]},
            "fetch": {"transport": "artifact_missing", "detail": "expired"},
        }
        actions_module.ambient_pass(session)
        session.remote_mode = "in-progress"
        outcome = actions_module.ambient_pass(session, mode="explicit")
        self.assertEqual(outcome["summary"]["pending"], 1)
        row = session.snapshot["actions_state"][OBLIGATION]
        self.assertNotIn("stage_failed", row)
        self.assertEqual(row["run"]["run_identity"], "4242")

    def test_dispatch_refuses_sha_without_ref(self) -> None:
        repo = write_repo(self.workspace, "fixture-repo",
                          external=dict(BINDING))
        publish(repo)
        first = git(repo, "rev-parse", "HEAD").strip()
        (repo / "NEXT.md").write_text("next\n", encoding="utf-8")
        git(repo, "add", ".")
        git(repo, "-c", "user.name=Actions Test",
            "-c", "user.email=actions@example.invalid",
            "commit", "-qm", "second")
        publish(repo)
        git(repo, "checkout", "-q", first)
        session = self._session()
        session.revision = first
        outcome = actions_module.ambient_pass(session, mode="explicit",
                                              dispatch=True)
        evidence = json.loads(
            (self.state / outcome["evidence"]).read_text(encoding="utf-8"))
        self.assertEqual(evidence["results"][0]["detail"], "no-dispatch-ref")
        row = session.snapshot["actions_state"][OBLIGATION]
        self.assertEqual(row.get("attempts") or {}, {})
        self.assertEqual(session.dispatch_calls, [])

    def test_subject_advanced_during_dispatch_burns_attempt(self) -> None:
        repo = write_repo(self.workspace, "fixture-repo",
                          external=dict(BINDING))
        publish(repo)
        session = self._session()
        session.revision = git(repo, "rev-parse", "HEAD").strip()
        session.dispatch_mode = "advanced"
        outcome = actions_module.ambient_pass(session, mode="explicit",
                                              dispatch=True)
        evidence = json.loads(
            (self.state / outcome["evidence"]).read_text(encoding="utf-8"))
        self.assertEqual(evidence["results"][0]["detail"],
                         "subject-advanced-during-dispatch")
        row = session.snapshot["actions_state"][OBLIGATION]
        attempts = row.get("attempts") or {}
        self.assertEqual(sum(attempts.values()), 1)

    def test_repeated_dispatch_failures_reach_cap(self) -> None:
        repo = write_repo(self.workspace, "fixture-repo",
                          external=dict(BINDING))
        publish(repo)
        session = self._session()
        session.revision = git(repo, "rev-parse", "HEAD").strip()
        session.dispatch_mode = "advanced"
        for _ in range(4):
            outcome = actions_module.ambient_pass(
                session, mode="explicit", dispatch=True)
        evidence = json.loads(
            (self.state / outcome["evidence"]).read_text(encoding="utf-8"))
        self.assertEqual(evidence["results"][0]["outcome"], "deferred")
        row = session.snapshot["actions_state"][OBLIGATION]
        attempts = row.get("attempts") or {}
        self.assertEqual(sum(attempts.values()), 3)
        self.assertEqual(len(session.dispatch_calls), 3)

    def test_external_admission_projection(self) -> None:
        rows = {OBLIGATION: {
            "evidence": {"obligation": OBLIGATION,
                         "subject_digest": "sha256:s",
                         "run_identity": "4343", "verdict": "failed",
                         "claim": "established", "complete": True},
            "evidence_id": "ext-1", "subject_digest": "sha256:s"}}
        admission = actions_module.external_admission(rows, OBLIGATION)
        assert admission is not None
        self.assertEqual(admission["verdict"], "FAIL")
        self.assertEqual(admission["evidence_id"], "ext-1")
        stale = {OBLIGATION: {
            "evidence": {"obligation": OBLIGATION,
                         "subject_digest": "sha256:old",
                         "run_identity": "4343", "verdict": "passed",
                         "claim": "established", "complete": True},
            "evidence_id": "ext-1", "subject_digest": "sha256:new"}}
        self.assertIsNone(actions_module.external_admission(stale, OBLIGATION))
        open_claim = {OBLIGATION: {
            "evidence": {"obligation": OBLIGATION,
                         "subject_digest": "sha256:s",
                         "run_identity": "4343", "verdict": "passed",
                         "claim": "not_established", "complete": True},
            "evidence_id": "ext-1", "subject_digest": "sha256:s"}}
        self.assertIsNone(
            actions_module.external_admission(open_claim, OBLIGATION))

    def test_terse_and_evidence_shapes(self) -> None:
        session = self._session()
        actions_module.ambient_pass(session)
        capsule = actions_module.terse(session)
        self.assertIn("actions", capsule)
        self.assertIn("delegate_requests", capsule["actions"])
        trail = actions_module.read_evidence(session)
        self.assertIn("evidence", trail)
        self.assertIn("history", trail)


class DelegateAuthorityTest(unittest.TestCase):
    def _context(self, holders: dict) -> dict:
        return authority_module.build_context(
            subject="ses_fixture",
            intent={"repositories": ["mncs-actions"]},
            protected_repos=[], claim_holders=holders)

    def test_delegate_effect_maps_to_delegate_action(self) -> None:
        self.assertEqual(
            authority_module.required_action_for_effects(["delegate"]),
            "delegate")
        self.assertEqual(
            authority_module.required_action_for_effects(["read", "delegate"]),
            "delegate")
        self.assertEqual(
            authority_module.required_action_for_effects(["subscribe"]),
            "subscribe")

    def test_delegate_allowed_by_repository_claim(self) -> None:
        context = self._context({"mncs-actions": [{
            "session_id": "ses_fixture",
            "scope": {"kind": "repository", "repository": "mncs-actions"}}]})
        verdict = authority_module.evaluate(
            context, action="delegate", target="mncs-actions",
            session_id="ses_fixture")
        self.assertEqual(verdict["verdict"], "allow")

    def test_delegate_escalates_without_claim(self) -> None:
        context = self._context({})
        verdict = authority_module.evaluate(
            context, action="delegate", target="mncs-actions",
            session_id="ses_fixture")
        self.assertEqual(verdict["verdict"], "escalate")

    def test_delegate_denies_foreign_claim_scope(self) -> None:
        context = self._context({"mncs-actions": [{
            "session_id": "ses_other",
            "scope": {"kind": "repository", "repository": "mncs-actions"}}]})
        verdict = authority_module.evaluate(
            context, action="delegate", target="mncs-actions",
            session_id="ses_fixture")
        self.assertEqual(verdict["verdict"], "escalate")


if __name__ == "__main__":
    unittest.main()
