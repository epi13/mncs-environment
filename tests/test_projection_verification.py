"""Generation-bound verification: PASS comes from evidence, never rendering.

Deterministic rendering proves same-inputs/same-bytes; it never proves
the canonical source is verified. These tests pin the boundary: FAIL
withholds, UNKNOWN awaits, PASS authorizes only its exact subject, and
executors that escape confinement produce no evidence at all.
"""

import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent))

from test_projections import CLI, NEED_MNCS, ProjectionFixture, git  # noqa: E402

from mncs_env import authority as authority_module  # noqa: E402
from mncs_env import capabilities as capabilities_module  # noqa: E402
from mncs_env import projection_store as store_module  # noqa: E402
from mncs_env import projections as projections_module  # noqa: E402
from mncs_env import projection_verification as verification_module  # noqa: E402
from mncs_env.session_store import open_store  # noqa: E402


class EnvelopeTests(unittest.TestCase):
    def test_envelope_verdicts(self):
        parse = verification_module._verdict_from_envelope
        self.assertEqual(parse('{"verdict": "pass"}'), 1)
        self.assertEqual(parse('{"verdict": "PASS"}'), 1)
        self.assertEqual(parse('{"verdict": "failed"}'), 0)
        self.assertEqual(parse('{"verdict": "unknown"}'), 2)
        self.assertEqual(parse('{"verdict": 1}'), 1)
        self.assertEqual(parse('{"verdict": 0}'), 0)
        self.assertIsNone(parse("not json"))
        self.assertIsNone(parse('{"verdict": "maybe"}'))
        self.assertIsNone(parse("[]"))

    def test_evidence_identity_binds_subject(self):
        subject = {"head": "abc", "branch": "main",
                   "input_digest": "sha256:1", "worktree": "sha256:2"}
        first = verification_module.evidence_id_for("cap", "rev", subject)
        again = verification_module.evidence_id_for("cap", "rev", dict(subject))
        self.assertEqual(first, again)
        moved = dict(subject, head="def")
        self.assertNotEqual(
            first, verification_module.evidence_id_for("cap", "rev", moved))
        self.assertNotEqual(
            first, verification_module.evidence_id_for("cap", "other",
                                                       subject))


class VerifyAuthorityTests(unittest.TestCase):
    def _context(self, **overrides):
        intent = {"repositories": ["target-doc"]}
        intent.update(overrides.pop("intent", {}))
        return authority_module.build_context(
            subject="tester", intent=intent, protected_repos=[],
            **overrides)

    def test_verify_allowed_on_dirty_own_branch(self):
        context = self._context()
        verdict = authority_module.evaluate(
            context, action="verify", target="target-doc",
            session_id="ses-a", claims={},
            repo_facts={"target-doc": {"branch": "campaign/x",
                                       "dirty": True}},
            scope={"kind": "worktree", "checkout": "/tmp/x",
                   "branch": "campaign/x"})
        self.assertEqual(verdict["verdict"], "allow", verdict)

    def test_verify_denied_on_foreign_claim_overlap(self):
        holders = {"target-doc": [{
            "session_id": "foreign-session",
            "scope": {"kind": "repository", "repository": "target-doc"}}]}
        context = self._context(claim_holders=holders)
        verdict = authority_module.evaluate(
            context, action="verify", target="target-doc",
            session_id="ses-a", claims=holders,
            repo_facts={"target-doc": {"branch": "main", "dirty": False}},
            scope={"kind": "worktree", "checkout": "/tmp/x"})
        self.assertEqual(verdict["verdict"], "deny", verdict)

    def test_verify_denied_on_protected_repository(self):
        context = authority_module.build_context(
            subject="tester", intent={"repositories": ["target-doc"]},
            protected_repos=["target-doc"])
        verdict = authority_module.evaluate(
            context, action="verify", target="target-doc",
            session_id="ses-a", claims={}, repo_facts={}, scope=None)
        self.assertEqual(verdict["verdict"], "deny", verdict)

    def test_verify_denied_when_forbidden(self):
        context = self._context(intent={"forbidden_actions": ["verify"]})
        verdict = authority_module.evaluate(
            context, action="verify", target="target-doc",
            session_id="ses-a", claims={}, repo_facts={}, scope=None)
        self.assertEqual(verdict["verdict"], "deny", verdict)

    def test_effects_require_verify_action(self):
        self.assertEqual(
            authority_module.required_action_for_effects(["verify"]),
            "verify")
        self.assertEqual(
            authority_module.required_action_for_effects(["read"]),
            "read")


class ExecutorBindingTests(unittest.TestCase):
    def _repo(self, root: Path, executor: dict) -> None:
        (root / ".mncs").mkdir(parents=True)
        (root / "tools").mkdir(parents=True)
        (root / "tools" / "check.py").write_text("print('ok')\n")
        inventory = {"repository": "demo", "obligations": [{
            "identity": "demo.check", "executor": executor}]}
        (root / ".mncs" / "verification.json").write_text(
            json.dumps(inventory))
        manifest = {"repository": "demo",
                    "verification": {
                        "obligation_inventory": ".mncs/verification.json"}}
        (root / ".mncs" / "project.json").write_text(json.dumps(manifest))

    def _binding(self, executor: dict):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self._repo(root, executor)
            bindings = capabilities_module._from_verification_inventory(root)
            self.assertEqual(len(bindings), 1, bindings)
            return bindings[0]

    def _executor(self, **overrides):
        base: dict = {"provider": "demo", "kind": "external_integration",
                      "argv": ["python3", "tools/check.py"],
                      "working_directory": ".", "timeout_seconds": 60}
        base.update(overrides)
        return base

    def test_verify_attestation_binds_verify_effects(self):
        binding = self._binding(self._executor(
            effects=["verify"], ephemeral_roots=[".mncs/cache"]))
        self.assertEqual(binding["effects"], ["verify"])
        self.assertEqual(
            binding["provenance"]["ephemeral_roots"], [".mncs/cache"])

    def test_undeclared_executor_keeps_write_effects(self):
        binding = self._binding(self._executor())
        self.assertEqual(binding["effects"], ["write"])

    def test_escaping_ephemeral_root_rejected(self):
        binding = self._binding(self._executor(
            effects=["verify"], ephemeral_roots=["../escape"]))
        self.assertEqual(binding["effects"], ["write"])
        self.assertEqual(binding["provenance"]["ephemeral_roots"], [])

    def test_absolute_ephemeral_root_rejected(self):
        binding = self._binding(self._executor(
            effects=["verify"], ephemeral_roots=["/tmp/escape"]))
        self.assertEqual(binding["effects"], ["write"])


class SharedRowTests(unittest.TestCase):
    def test_stale_adopt_is_rejected_without_regression(self):
        with tempfile.TemporaryDirectory() as raw:
            store = open_store(Path(raw), "file")
            session = types.SimpleNamespace(session_id="ses-a",
                                            store=store)
            first = store_module.write_row(
                store, {"projection": "p", "canonical_gen": 1,
                        "observed_gen": 0, "canonical_digest": "sha256:1",
                        "verdict": 2, "status": 1, "defer_count": 0},
                expected_version=0)
            self.assertEqual(first["version"], 1)
            # A second session adopts generation 1 from the same base.
            winner = dict(first)
            published = store_module.write_row(
                store, {**winner, "observed_gen": 1, "verdict": 1,
                        "evidence_id": "vev_winner", "status": 0},
                expected_version=1)
            self.assertEqual(published["version"], 2)
            # The stale loser replays its own adopt against version 1.
            rows: dict = {}
            adopted = projections_module._adopt_shared(
                session, rows, "p", first, 1, "sha256:1",
                "sha256:render", 1, "vev_loser", None)
            self.assertFalse(adopted)
            latest = store_module.read_row(store, "p")
            self.assertEqual(latest["observed_gen"], 1)
            self.assertEqual(latest["evidence_id"], "vev_winner")
            self.assertEqual(latest["version"], 2)

    def test_replay_of_own_write_is_idempotent(self):
        with tempfile.TemporaryDirectory() as raw:
            store = open_store(Path(raw), "file")
            row = {"projection": "p", "canonical_gen": 1,
                   "observed_gen": 1, "canonical_digest": "sha256:1",
                   "rendered_digest": "sha256:r", "verdict": 1,
                   "evidence_id": "vev_1", "status": 0, "defer_count": 0}
            first = store_module.write_row(store, row, expected_version=0)
            again = store_module.write_row(store, row, expected_version=0)
            self.assertEqual(again["version"], first["version"])

    def test_evidence_conflict_refuses_different_payload(self):
        with tempfile.TemporaryDirectory() as raw:
            store = open_store(Path(raw), "file")
            record = {"evidence_id": "vev_x", "verdict": 1}
            store_module.write_evidence(store, record)
            store_module.write_evidence(store, dict(record))
            with self.assertRaises(store_module.EvidenceConflict):
                store_module.write_evidence(
                    store, {"evidence_id": "vev_x", "verdict": 0})


@NEED_MNCS
class AmbientVerificationTests(ProjectionFixture):
    def _evidence_records(self, consumer_payload) -> list:
        session_id = consumer_payload["session_id"]
        code, payload = self.run_cli("projections", session_id, "--evidence")
        self.assertEqual(code, 0, payload)
        evidence = payload.get("evidence", payload)
        return evidence.get("results", [])

    def test_fail_verdict_withholds_reconciliation(self):
        code, first = self.enter(
            "withhold", extra_env={"MNCS_FIXTURE_VERIFY_MODE": "fail"})
        self.assertEqual(code, 0, first)
        summary = self.projection_summary(first)
        self.assertEqual(summary["reconciled"], 0, summary)
        self.assertEqual(summary["pending"], 2, summary)
        for record in self._evidence_records(first):
            self.assertEqual(record.get("verdict"), 0, record)
            self.assertIsNotNone(record.get("evidence_id"), record)
        # The FAIL is immutable for its subject: re-entry replays the
        # refusal instead of re-running or passing.
        code, replay = self.enter("withhold")
        self.assertEqual(code, 0, replay)
        self.assertEqual(self.projection_summary(replay)["pending"], 2)
        # Recovery needs a new subject: a fresh commit re-verifies and
        # converges.
        git(self.target_doc, "commit", "--allow-empty", "-qm", "retry")
        git(self.target_region, "commit", "--allow-empty", "-qm", "retry")
        code, second = self.enter("withhold")
        self.assertEqual(code, 0, second)
        summary = self.projection_summary(second)
        self.assertEqual(summary["pending"], 0, summary)

    def test_old_pass_does_not_authorize_new_generation(self):
        code, _ = self.enter("generations")
        self.assertEqual(code, 0)
        (self.target_doc / "docs" / "rfcs" / "0002.md").write_text(
            "# RFC 0002\n")
        self.commit_all(self.target_doc, "second RFC")
        code, second = self.enter(
            "generations", extra_env={"MNCS_FIXTURE_VERIFY_MODE": "fail"})
        self.assertEqual(code, 0, second)
        summary = self.projection_summary(second)
        # The new generation fails its own verification even though the
        # previous generation passed; nothing is reconciled for it.
        self.assertIn("target-doc:index", summary["pending_ids"], summary)
        git(self.target_doc, "commit", "--allow-empty", "-qm", "retry")
        code, third = self.enter("generations")
        self.assertEqual(code, 0, third)
        summary = self.projection_summary(third)
        self.assertNotIn("target-doc:index", summary["pending_ids"], summary)

    def test_unknown_without_obligations_never_passes(self):
        target = self.entry / "target-plain"
        root = self._write_target("target-plain", "whole-file",
                                  "docs/plain.generated.md")
        manifest = json.loads(
            (root / ".mncs" / "project.json").read_text())
        del manifest["projections"][0]["verification"]
        (root / ".mncs" / "project.json").write_text(json.dumps(manifest))
        self.commit_all(root, "drop verification")
        definition_path = self.entry / ".mncs" / "environment.json"
        definition = json.loads(definition_path.read_text())
        definition["workspace_scope"]["repositories"].append("target-plain")
        definition_path.write_text(json.dumps(definition))
        code, first = self.enter("undeclared")
        self.assertEqual(code, 0, first)
        records = {record["projection"]: record
                   for record in self._evidence_records(first)}
        record = records["target-plain:index"]
        self.assertEqual(record.get("outcome"), "deferred", record)
        self.assertEqual(record.get("verdict"), 2, record)
        self.assertEqual(record.get("verification"),
                         "verification-undeclared", record)

    def test_adopted_row_carries_source_identity(self):
        code, first = self.enter("source-id")
        self.assertEqual(code, 0, first)
        store = open_store(self.state, "file")
        found = store.read_projection_row("target-doc:index")
        self.assertIsNotNone(found, "no shared row adopted")
        _version, row = found
        source = row.get("source") or {}
        self.assertTrue(source.get("head"), row)
        self.assertEqual(source.get("repository"), "target-doc", row)
        self.assertEqual(source.get("input_digest"),
                         row.get("canonical_digest"), row)
        # Projection generations stay counters; provenance lives in
        # source + evidence, never overloaded onto the integers.
        self.assertIsInstance(row.get("observed_gen"), int)
        self.assertTrue(row.get("evidence_id"), row)
        self.assertIn(row.get("evidence_id"),
                      row.get("evidence_ids") or [], row)

    def test_second_session_sees_shared_current_state(self):
        code, first = self.enter("session-a")
        self.assertEqual(code, 0, first)
        summary = self.projection_summary(first)
        self.assertEqual(summary["pending"], 0, summary)
        code, second = self.enter("session-b")
        self.assertEqual(code, 0, second)
        summary = self.projection_summary(second)
        self.assertEqual(summary["pending"], 0, summary)
        self.assertEqual(summary["reconciled"], 0, summary)
        for record in self._evidence_records(second):
            self.assertEqual(record.get("outcome"), "current", record)

    def test_dirty_branch_verifies_but_still_defers_mutation(self):
        code, _ = self.enter("dirty-verify")
        self.assertEqual(code, 0)
        (self.target_doc / "docs" / "rfcs" / "0002.md").write_text(
            "# RFC 0002 uncommitted\n")
        code, second = self.enter("dirty-verify")
        self.assertEqual(code, 0, second)
        summary = self.projection_summary(second)
        self.assertIn("target-doc:index", summary["pending_ids"], summary)
        records = {record["projection"]: record
                   for record in self._evidence_records(second)}
        record = records["target-doc:index"]
        # Evidence was produced for the dirty subject (PASS), yet the
        # mutation itself deferred on foreign/dirty classification.
        self.assertEqual(record.get("verdict"), 1, record)
        self.assertIsNotNone(record.get("evidence_id"), record)

    def test_mid_render_input_mutation_aborts_commit(self):
        before_doc = (self.target_doc / "docs/out.generated.md").read_bytes()
        code, first = self.enter(
            "race", extra_env={"MNCS_FIXTURE_RENDER_MODE": "mutate-inputs"})
        self.assertEqual(code, 0, first)
        summary = self.projection_summary(first)
        # B-derived bytes are never recorded as digest A: both commits
        # abort and no output is touched.
        self.assertEqual(summary["reconciled"], 0, summary)
        self.assertEqual(summary["pending"], 2, summary)
        for record in self._evidence_records(first):
            self.assertEqual(record.get("outcome"), "deferred", record)
            self.assertIn("moved-before-commit",
                          str(record.get("gate_reason", "")), record)
        self.assertEqual(
            (self.target_doc / "docs/out.generated.md").read_bytes(),
            before_doc)
        # Removing the racing file restores digest A; the next pass
        # converges instead of replaying the abort.
        (self.target_doc / "docs" / "rfcs" / "zzz-race.md").unlink()
        (self.target_region / "docs" / "rfcs" / "zzz-race.md").unlink()
        code, second = self.enter("race")
        self.assertEqual(code, 0, second)
        summary = self.projection_summary(second)
        self.assertEqual(summary["pending"], 0, summary)

    def test_unconfined_executor_produces_no_evidence(self):
        script = self.target_doc / "tools" / "verify_projection.py"
        script.write_text(
            "import json\n"
            "Path = __import__('pathlib').Path\n"
            "Path('UNCONFINED.txt').write_text('escape\\n')\n"
            "print(json.dumps({'verdict': 'pass'}))\n")
        self.commit_all(self.target_doc, "rogue verifier")
        code, first = self.enter("confined")
        self.assertEqual(code, 0, first)
        records = {record["projection"]: record
                   for record in self._evidence_records(first)}
        record = records["target-doc:index"]
        self.assertEqual(record.get("outcome"), "deferred", record)
        self.assertEqual(record.get("verdict"), 2, record)
        self.assertEqual(record.get("verification"), "unconfined", record)
        self.assertIsNone(record.get("evidence_id"), record)


@NEED_MNCS
class WatchTests(ProjectionFixture):
    def _watch(self, session_id: str, seconds: str, iterations: int):
        env = dict(os.environ)
        env.update(self.env)
        return subprocess.Popen(
            [sys.executable, str(CLI),
             "--state-dir", str(self.state), "--persistence", "file",
             "projections", session_id, "--watch", seconds,
             "--watch-iterations", str(iterations)],
            cwd=str(self.entry), stdout=subprocess.PIPE, text=True, env=env)

    def test_quiet_watch_reuses_epochs_without_renders(self):
        code, first = self.enter("watch-quiet")
        self.assertEqual(code, 0, first)
        session_id = first["session_id"]
        with self._watch(session_id, "1", 3) as child:
            stdout, _ = child.communicate(timeout=120)
        self.assertEqual(child.returncode, 0, stdout)
        lines = [json.loads(line) for line in stdout.splitlines()
                 if line.strip()]
        self.assertEqual(len(lines), 3, stdout)
        for tick in lines:
            self.assertTrue(tick["reused"], tick)
            self.assertEqual(tick["summary"]["pending"], 0, tick)
            self.assertEqual(tick["summary"]["reconciled"], 0, tick)

    def test_watch_notices_canonical_change_without_reentry(self):
        code, first = self.enter("watch-change")
        self.assertEqual(code, 0, first)
        session_id = first["session_id"]
        with self._watch(session_id, "1", 6) as child:
            import time as _time
            _time.sleep(2.0)
            (self.target_doc / "docs" / "rfcs" / "0002.md").write_text(
                "# RFC 0002 during watch\n")
            self.commit_all(self.target_doc, "watch RFC")
            stdout, _ = child.communicate(timeout=120)
        self.assertEqual(child.returncode, 0, stdout)
        lines = [json.loads(line) for line in stdout.splitlines()
                 if line.strip()]
        self.assertEqual(len(lines), 6, stdout)
        reconciled = [tick for tick in lines
                      if tick["summary"]["reconciled"] >= 1]
        self.assertTrue(reconciled, stdout)
        self.assertEqual(lines[-1]["summary"]["pending"], 0, stdout)
        from test_projections import fixture_bytes
        self.assertEqual(
            (self.target_doc / "docs/out.generated.md").read_bytes(),
            fixture_bytes(self.target_doc / "docs" / "rfcs"))

    def test_watch_reconsiders_after_claim_release(self):
        code, first = self.enter("watch-claim")
        self.assertEqual(code, 0, first)
        session_id = first["session_id"]
        self.acquire_foreign("target-doc", ["docs/out.generated.md"])
        (self.target_doc / "docs" / "rfcs" / "0002.md").write_text(
            "# RFC 0002 blocked\n")
        self.commit_all(self.target_doc, "blocked RFC")
        with self._watch(session_id, "1", 6) as child:
            import time as _time
            _time.sleep(2.0)
            self.release_foreign("target-doc")
            stdout, _ = child.communicate(timeout=120)
        self.assertEqual(child.returncode, 0, stdout)
        lines = [json.loads(line) for line in stdout.splitlines()
                 if line.strip()]
        self.assertEqual(len(lines), 6, stdout)
        pending_seen = any("target-doc:index" in tick["summary"].get(
            "pending_ids", []) for tick in lines)
        self.assertTrue(pending_seen, stdout)
        self.assertEqual(lines[-1]["summary"]["pending"], 0, stdout)


if __name__ == "__main__":
    unittest.main()
