"""Ambient Doctor: epoch validation, terse summaries, remediation gates."""

from __future__ import annotations

import fcntl
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from mncs_env import claims, doctor, entry, readiness, sessions
from mncs_env.session_store import open_store

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "mncs-env"

PROVIDER = '''import json, sys
from pathlib import Path
state = Path("service-state.json")
value = json.loads(state.read_text()) if state.exists() else {"ready": False, "starts": 0}
if sys.argv[1] == "start":
    value.update(ready=True, starts=value["starts"] + 1)
    state.write_text(json.dumps(value))
print(json.dumps({"schema_version": value.get("schema", "fixture.status/1"), **value}))
'''

BROKEN_PROVIDER = '''import json, sys
from pathlib import Path
state = Path("service-state.json")
value = json.loads(state.read_text()) if state.exists() else {"ready": False, "starts": 0}
if sys.argv[1] == "start":
    value.update(starts=value["starts"] + 1)
    state.write_text(json.dumps(value))
print(json.dumps({"schema_version": value.get("schema", "fixture.status/1"), **value}))
'''

STUB_REMEDIATION = '''import json, sys
target = sys.argv[sys.argv.index("--target") + 1]
envelope = {"schema_version": "mncs.remediation/1", "provider": "fixture",
            "provider_version": "test", "scope": {"domain": "repository", "target": target},
            "dry_run": "--dry-run" in sys.argv,
            "summary": {"repaired": 1, "reconciled": 0, "degraded": 0, "blockers": 0},
            "remaining": [], "repairs": [{"id": "safe:stub", "class": "safe_automatic",
            "provider": "fixture", "detail": "stub", "validated": True}],
            "reconciliations": [], "evidence": None,
            "budget": {"max_items": 256, "items_done": 1, "rounds": 1, "exhausted": False},
            "validation": {"passed": True, "errors_before": 1, "errors_after": 0,
            "idempotent_known": True, "idempotent": True},
            "notes": [], "exit_code": 0, "exit_meaning": "healthy: stub"}
print(json.dumps(envelope))
'''


class DoctorFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mncs-doctor-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / "fixture"
        self.project.mkdir()
        self.state = self.base / "state"
        (self.project / ".mncs").mkdir()
        self.definition = self.project / ".mncs" / "environment.json"
        self.manifest = self.project / "family-semantic-contracts-v1.json"
        self.provider = self.project / "provider.py"
        self.provider.write_text(PROVIDER)
        (self.project / ".gitignore").write_text("service-state.json\n")
        self.contracts = {"repository_id": "fixture", "provides": [
            {"contract_identity": "fixture.status/1", "contract_revision": "1",
             "invocation": {"kind": "python", "path": "provider.py", "fixed_argv": ["status"]}, "effects": ["read"]},
            {"contract_identity": "fixture.start/1", "contract_revision": "1",
             "invocation": {"kind": "python", "path": "provider.py", "fixed_argv": ["start"]}, "effects": ["write"]},
        ]}
        self.manifest.write_text(json.dumps(self.contracts))
        self.config = {"name": "fixture-entry", "workspace_root": "..",
                       "intent": {"goal": "exercise doctor contracts", "repositories": []},
                       "required_capabilities": ["fixture.status/1"],
                       "services": [{"identity": "fixture-service", "required": True,
                                     "probe": {"capability": "fixture.status/1"},
                                     "reconcile": {"capability": "fixture.start/1"},
                                     "response_schema": "fixture.status/1", "ready_when": {"/ready": True}}]}
        self.definition.write_text(json.dumps(self.config))
        self.stub = self.project / "stub-remediate.py"
        self.stub.write_text(STUB_REMEDIATION)
        for argv in (["init", "-q", "-b", "main"], ["add", "."],
                    ["-c", "user.name=Doctor Test", "-c", "user.email=doctor@example.invalid",
                     "commit", "-qm", "fixture"]):
            subprocess.run(["git", "-C", str(self.project), *argv], check=True, capture_output=True)

    def run_cli(self, *argv, cwd=None, backend="file"):
        result = subprocess.run([sys.executable, str(CLI), "--state-dir", str(self.state),
                                 "--persistence", backend, *argv], cwd=cwd or self.project,
                                capture_output=True, text=True, timeout=60)
        payload = json.loads(result.stdout or result.stderr)
        return result.returncode, payload

    def enter(self, *argv, **kwargs):
        return self.run_cli("enter", "--consumer", "agent", *argv, **kwargs)

    def fresh_definition(self):
        return json.loads(self.definition.read_text())

    def ready(self):
        (self.project / "service-state.json").write_text(json.dumps({"ready": True, "starts": 0}))


class EpochTests(DoctorFixture):
    def test_fresh_entry_records_epoch_and_reports_doctor(self):
        self.ready()
        code, context = self.enter()
        self.assertEqual(code, 0, context)
        self.assertIn("doctor", context)
        self.assertFalse(context["doctor"]["reused"])
        self.assertEqual(context["doctor"]["summary"]["blockers"], 0)
        self.assertTrue(context["doctor"]["epoch"])

    def test_reentry_hits_epoch_and_skips_revalidation(self):
        self.ready()
        _, first = self.enter()
        code, second = self.enter()
        self.assertEqual(code, 0, second)
        self.assertEqual(second["session_id"], first["session_id"])
        self.assertTrue(second["entry"]["reused"])
        self.assertEqual(second["entry"]["revalidation"]["reprobed"], 0)
        self.assertTrue(second["doctor"]["reused"])
        self.assertEqual(second["doctor"]["epoch"], first["doctor"]["epoch"])

    def test_epoch_hit_writes_only_the_resume_marker(self):
        self.ready()
        _, first = self.enter()
        store = open_store(self.state, "file")
        snap_before = store.load_snapshot(first["session_id"])
        events_before = len(store.read_events(first["session_id"]))
        _, second = self.enter()
        self.assertTrue(second["doctor"]["reused"])
        snap_after = store.load_snapshot(first["session_id"])
        events_after = len(store.read_events(first["session_id"]))
        # Exactly one snapshot save (resume) and one event (session.resumed):
        # no revalidation saves, no service saves, no epoch rewrite.
        self.assertEqual(snap_after["snapshot_sequence"], snap_before["snapshot_sequence"] + 1)
        self.assertEqual(events_after, events_before + 1)

    def test_epoch_miss_on_working_tree_change(self):
        self.ready()
        _, first = self.enter()
        (self.project / "notes.txt").write_text("uncommitted work")
        code, second = self.enter()
        self.assertEqual(code, 0, second)
        self.assertTrue(second["entry"]["reused"])
        self.assertFalse(second["doctor"]["reused"])
        self.assertGreater(second["entry"]["revalidation"]["reprobed"], 0)

    def test_epoch_miss_on_manifest_change(self):
        self.ready()
        _, first = self.enter()
        contracts = json.loads(self.manifest.read_text())
        contracts["provides"][0]["contract_revision"] = "2"
        self.manifest.write_text(json.dumps(contracts))
        subprocess.run(["git", "-C", str(self.project), "-c", "user.name=Doctor Test",
                        "-c", "user.email=doctor@example.invalid",
                        "commit", "-qam", "bump revision"], check=True, capture_output=True)
        _, second = self.enter()
        self.assertFalse(second["doctor"]["reused"])
        self.assertGreater(second["entry"]["revalidation"]["reprobed"], 0)

    def test_epoch_miss_on_provider_substrate_loss(self):
        self.ready()
        _, first = self.enter()
        self.provider.unlink()
        code, second = self.enter()
        self.assertEqual(code, 5, second)
        self.assertFalse(second["doctor"]["reused"])
        self.assertGreater(second["doctor"]["summary"]["blockers"], 0)
        self.assertIn("fixture.status/1", second["readiness"]["required_unavailable"])

    def test_doctor_block_lists_only_blockers_with_compact_unavailable(self):
        self.ready()
        _, first = self.enter()
        healthy = first["doctor"]
        self.assertEqual(healthy["remaining"], [])
        self.assertEqual(healthy["unavailable"]["count"], 0)
        self.provider.unlink()
        code, second = self.enter()
        self.assertEqual(code, 5, second)
        # Actionable delta only: the required capability blocks; the
        # non-required start capability is compact background.
        self.assertIn("fixture.status/1", second["doctor"]["remaining"])
        self.assertNotIn("fixture.start/1", second["doctor"]["remaining"])
        self.assertEqual(second["doctor"]["summary"]["degraded"], 2)
        self.assertEqual(second["doctor"]["unavailable"]["count"], 2)
        digest = second["doctor"]["unavailable"]["digest"]
        self.assertEqual(len(digest), 16)
        int(digest, 16)
        # Full per-capability detail stays retrievable in evidence.
        code, evidence = self.run_cli("doctor", second["session_id"], "--evidence")
        self.assertEqual(code, 0, evidence)
        classified = [entry["capability"] for entry in
                      evidence["evidence"]["classification"]["transient"] +
                      evidence["evidence"]["classification"]["gaps"]]
        self.assertIn("fixture.status/1", classified)
        self.assertIn("fixture.start/1", classified)

    def test_quiet_reentry_doctor_block_is_compact(self):
        self.ready()
        _, first = self.enter()
        code, second = self.enter()
        self.assertEqual(code, 0, second)
        self.assertTrue(second["doctor"]["reused"])
        block = second["doctor"]
        self.assertEqual(block["summary"]["blockers"], 0)
        self.assertEqual(block["remaining"], [])
        self.assertLess(len(json.dumps(block)), 600, json.dumps(block))

    def test_health_serves_epoch_and_live_forces_probes(self):
        self.ready()
        _, first = self.enter()
        code, health = self.run_cli("health", first["session_id"])
        self.assertEqual(code, 0, health)
        self.assertEqual(health["readiness"]["observation"], "epoch")
        code, live = self.run_cli("health", first["session_id"], "--live")
        self.assertEqual(code, 0, live)
        self.assertEqual(live["readiness"]["observation"], "live")

    def test_health_falls_through_to_live_on_world_change(self):
        self.ready()
        _, first = self.enter()
        self.provider.unlink()
        code, health = self.run_cli("health", first["session_id"])
        self.assertEqual(code, 5, health)
        self.assertEqual(health["readiness"]["observation"], "live")
        self.assertIn("fixture.status/1", health["readiness"]["required_unavailable"])

    def test_provider_runtime_state_change_is_detected_despite_valid_epoch(self):
        self.ready()
        _, first = self.enter()
        # service-state.json is gitignored: no fingerprinted input changes,
        # but the declared service is no longer ready.
        (self.project / "service-state.json").write_text(json.dumps({"ready": False, "starts": 0}))
        code, health = self.run_cli("health", first["session_id"])
        self.assertEqual(code, 5, health)
        self.assertEqual(health["readiness"]["observation"], "live")
        # Entry reconciles services without paying for a full
        # revalidation. Recovery itself needs authority the fixture
        # session does not hold, so it escalates instead of running.
        code, second = self.enter()
        self.assertEqual(code, 5, second)
        self.assertFalse(second["doctor"]["reused"])
        self.assertEqual(second["entry"]["revalidation"]["reprobed"], 0)
        self.assertIn("services reconciled", second["entry"]["revalidation"]["note"])
        self.assertEqual(second["entry"]["operations"][0]["status"], "pending-escalation")
        self.assertIn("fixture-service", second["readiness"]["blocking"])

    def test_status_terse_reports_without_store_round_trip(self):
        self.ready()
        _, first = self.enter()
        with mock.patch("mncs_env.session_store.StoreSessionStore.__init__",
                        side_effect=AssertionError("store must not open")):
            fast = doctor.serve_terse_fast(self.state, first["session_id"])
        self.assertIsNotNone(fast)
        self.assertEqual(fast["observation"], "epoch")
        self.assertIn("summary", fast)
        code, terse = self.run_cli("status", first["session_id"], "--terse")
        self.assertEqual(code, 0, terse)
        self.assertEqual(terse["observation"], "epoch")

    def test_status_terse_falls_back_when_epoch_is_stale(self):
        self.ready()
        _, first = self.enter()
        (self.project / "notes.txt").write_text("uncommitted work")
        code, terse = self.run_cli("status", first["session_id"], "--terse")
        self.assertEqual(code, 0, terse)
        self.assertEqual(terse["observation"], "snapshot")

    def test_fileside_epoch_ttl_expiry(self):
        self.ready()
        _, first = self.enter()
        record = doctor.read_fileside_epoch(self.state, first["session_id"])
        self.assertIsNotNone(record)
        record["validated_at"] = "2020-01-01T00:00:00+00:00"
        valid, reason = doctor.validate_fileside_epoch(record)
        self.assertFalse(valid)
        self.assertEqual(reason, "ttl expired")

    def test_doctor_cli_reports_terse_and_evidence(self):
        self.ready()
        _, first = self.enter()
        code, report = self.run_cli("doctor", first["session_id"])
        self.assertEqual(code, 0, report)
        self.assertTrue(report["reused"])
        self.assertEqual(report["summary"]["blockers"], 0)
        self.assertIn("epoch", report)
        code, evidence = self.run_cli("doctor", first["session_id"], "--evidence")
        self.assertEqual(code, 0, evidence)
        self.assertTrue(evidence["history"])
        self.assertIsNotNone(evidence["evidence"])
        self.assertEqual(evidence["evidence"]["schema_version"],
                         "mncs.environment.doctor-evidence/1")

    def test_concurrent_sessions_are_isolated(self):
        self.ready()
        _, first = self.enter()
        _, second = self.run_cli("enter", "--consumer", "agent-b", "--new-session")
        self.assertNotEqual(first["session_id"], second["session_id"])
        _, before = self.run_cli("inspect", second["session_id"])
        code, _ = self.run_cli("doctor", first["session_id"])
        self.assertEqual(code, 0)
        _, after = self.run_cli("inspect", second["session_id"])
        self.assertEqual(after["lifecycle_history"], before["lifecycle_history"])
        self.assertEqual(after["event_count"], before["event_count"])


class ValidationReasonTests(DoctorFixture):
    def open_session(self, session_id):
        return sessions.Session.resume(state_dir=self.state, session_id=session_id, backend="file")

    def test_precise_single_reasons_per_dimension(self):
        self.ready()
        _, first = self.enter()
        session = sessions.Session.open(state_dir=self.state, session_id=first["session_id"],
                                        backend="file")
        try:
            epoch = session.snapshot["doctor"]["epoch"]
            valid, reasons = doctor.validate_epoch(session, epoch)
            self.assertTrue(valid, reasons)
            # Lifecycle flip alone invalidates.
            session.snapshot["lifecycle"] = "blocked"
            valid, reasons = doctor.validate_epoch(session, epoch)
            self.assertEqual(reasons, ["lifecycle"])
            session.snapshot["lifecycle"] = "active"
            # Consumer flip alone invalidates.
            session.snapshot["consumer_id"] = "impostor"
            valid, reasons = doctor.validate_epoch(session, epoch)
            self.assertEqual(reasons, ["consumer_id"])
            session.snapshot["consumer_id"] = "agent"
            # A bare observation appends exactly one event reason.
            session.record_observation("adapter.observed", "test-probe", {})
            valid, reasons = doctor.validate_epoch(session, epoch)
            self.assertEqual(reasons, ["events"])
        finally:
            session.close()

    def test_claim_change_invalidates_without_touching_our_log(self):
        self.ready()
        _, first = self.enter()
        _, second = self.run_cli("enter", "--consumer", "agent-b", "--new-session")
        other = self.open_session(second["session_id"])
        try:
            other.acquire_claim("fixture", reason="parallel work")
        finally:
            other.close()
        session = sessions.Session.open(state_dir=self.state, session_id=first["session_id"],
                                        backend="file")
        try:
            valid, reasons = doctor.validate_epoch(session, session.snapshot["doctor"]["epoch"])
            self.assertFalse(valid)
            self.assertEqual(reasons, ["claims"])
        finally:
            session.close()

    def test_committed_manifest_change_reports_repo_and_manifests(self):
        self.ready()
        _, first = self.enter()
        contracts = json.loads(self.manifest.read_text())
        contracts["provides"][0]["contract_revision"] = "2"
        self.manifest.write_text(json.dumps(contracts))
        subprocess.run(["git", "-C", str(self.project), "-c", "user.name=Doctor Test",
                        "-c", "user.email=doctor@example.invalid",
                        "commit", "-qam", "bump revision"], check=True, capture_output=True)
        session = sessions.Session.open(state_dir=self.state, session_id=first["session_id"],
                                        backend="file")
        try:
            valid, reasons = doctor.validate_epoch(session, session.snapshot["doctor"]["epoch"])
            self.assertFalse(valid)
            # The commit moved HEAD (repo-changed) and the declaration hash (manifests).
            self.assertEqual(reasons, ["repo-changed:fixture", "manifests:fixture"])
        finally:
            session.close()


class EntryMechanicsTests(DoctorFixture):
    def test_entry_opens_store_exactly_once(self):
        self.ready()
        calls = []
        real_open = entry.open_store
        definition = json.loads(self.definition.read_text())
        with mock.patch.object(entry, "open_store",
                               side_effect=lambda *a, **k: (calls.append(1), real_open(*a, **k))[1]):
            with mock.patch("mncs_env.sessions.open_store",
                            side_effect=AssertionError("session must reuse the entry store")):
                first = entry.enter(definition=definition, definition_path=self.definition,
                                    workspace_root=str(self.base), state_dir=self.state,
                                    backend="file", consumer_id="agent", consumer_kind="agent")
                self.assertFalse(first["entry"]["reused"])
                second = entry.enter(definition=definition, definition_path=self.definition,
                                     workspace_root=str(self.base), state_dir=self.state,
                                     backend="file", consumer_id="agent", consumer_kind="agent")
                self.assertTrue(second["entry"]["reused"])
        self.assertEqual(len(calls), 2)

    def test_entry_lock_waits_for_release(self):
        self.state.mkdir(parents=True, exist_ok=True)
        handle = (self.state / "entry-file.lock").open("a")
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        timer = threading.Timer(0.3, lambda: fcntl.flock(handle, fcntl.LOCK_UN))
        timer.start()
        try:
            started = time.monotonic()
            with entry.entry_lock(self.state, "file", wait_seconds=5) as lock:
                waited = time.monotonic() - started
            self.assertGreaterEqual(waited, 0.25)
            self.assertGreaterEqual(lock["waited_seconds"], 0.25)
        finally:
            timer.cancel()
            handle.close()

    def test_entry_lock_budget_expires_without_seizing(self):
        self.state.mkdir(parents=True, exist_ok=True)
        handle = (self.state / "entry-file.lock").open("a")
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            started = time.monotonic()
            with self.assertRaises(entry.EntryError) as raised:
                with entry.entry_lock(self.state, "file", wait_seconds=0.2):
                    pass
            self.assertEqual(raised.exception.diagnostics["code"], "entry-busy")
            self.assertLess(time.monotonic() - started, 5)
        finally:
            handle.close()


class RepositoryRemediationTests(DoctorFixture):
    def open_session(self, session_id):
        return sessions.Session.resume(state_dir=self.state, session_id=session_id, backend="file")

    def inject_stub_binding(self, session, script=None):
        from mncs_env import capabilities
        stub = self.stub
        if script is not None:
            stub = self.project / "stub-remediate-variant.py"
            stub.write_text(script)
            subprocess.run(["git", "-C", str(self.project), "-c", "user.name=Doctor Test",
                            "-c", "user.email=doctor@example.invalid",
                            "add", stub.name], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(self.project), "-c", "user.name=Doctor Test",
                            "-c", "user.email=doctor@example.invalid",
                            "commit", "-qm", "variant stub"], check=True, capture_output=True)
        binding = capabilities.bind(provider="fixture", capability="fixture:repository-remediation",
                                    contract_revision="test", entrypoint="stub-remediate",
                                    address="python:" + str(stub), effects=["write"],
                                    provider_root=str(self.project))
        binding["availability"] = {"status": "available", "reason": "test stub",
                                   "code": "executable-present", "verification": "substrate",
                                   "observed_at": "2026-01-01T00:00:00+00:00"}
        session.snapshot["bindings"].append(binding)
        session._save()
        return self.stub

    def test_missing_provider_is_refused(self):
        self.ready()
        _, first = self.enter()
        session = self.open_session(first["session_id"])
        try:
            with self.assertRaises(doctor.RemediationRefused) as raised:
                doctor.remediate_repository(session, "fixture")
            self.assertEqual(raised.exception.diagnostics["code"], "remediation-provider-missing")
        finally:
            session.close()

    def test_unclaimed_checkout_is_refused(self):
        self.ready()
        _, first = self.enter()
        session = self.open_session(first["session_id"])
        try:
            self.inject_stub_binding(session)
            with self.assertRaises(doctor.RemediationRefused) as raised:
                doctor.remediate_repository(session, "fixture")
            self.assertEqual(raised.exception.diagnostics["code"], "remediation-claim-required")
        finally:
            session.close()

    def test_unknown_work_without_adoption_is_refused(self):
        self.ready()
        _, first = self.enter()
        session = self.open_session(first["session_id"])
        try:
            self.inject_stub_binding(session)
            session.acquire_claim("fixture", reason="claimed work")
            (self.project / "uncommitted.txt").write_text("another agent's draft")
            with self.assertRaises(claims.ClaimAdoptionRequired):
                doctor.remediate_repository(session, "fixture")
        finally:
            session.close()

    def test_claimed_checkout_remediates_and_records(self):
        self.ready()
        _, first = self.enter()
        session = self.open_session(first["session_id"])
        try:
            self.inject_stub_binding(session)
            session.acquire_claim("fixture", reason="claimed work")
            result = doctor.remediate_repository(session, "fixture")
            self.assertEqual(result["summary"]["repaired"], 1)
            self.assertEqual(result["remaining"], [])
            history = session.snapshot["doctor"]["history"]
            self.assertEqual(history[-1]["repository"], "fixture")
            kinds = [event["type"] for event in session._log()]
            self.assertIn("doctor.repository-remediated", kinds)
        finally:
            session.close()

    def test_adopted_dirty_checkout_remediates(self):
        self.ready()
        _, first = self.enter()
        session = self.open_session(first["session_id"])
        try:
            self.inject_stub_binding(session)
            (self.project / "uncommitted.txt").write_text("adopted draft")
            session.acquire_claim("fixture", basis=claims.BASIS_ADOPTION, reason="adopting draft",
                                  checkout_facts={"dirty": True, "foreign_signals": ["dirty-tree"],
                                                  "head": "test", "branch": "main"})
            result = doctor.remediate_repository(session, "fixture", dry_run=True)
            self.assertTrue(result["dry_run"])
        finally:
            session.close()

    def test_envelope_is_accepted_despite_nonzero_provider_exit(self):
        self.ready()
        _, first = self.enter()
        session = self.open_session(first["session_id"])
        try:
            script = STUB_REMEDIATION + "\nimport sys\nsys.exit(1)\n"
            self.inject_stub_binding(session, script=script)
            session.acquire_claim("fixture", reason="claimed work")
            result = doctor.remediate_repository(session, "fixture")
            self.assertEqual(result["summary"]["repaired"], 1)
            self.assertEqual(result["provider_status"], "failed")
        finally:
            session.close()

    def test_missing_envelope_is_refused(self):
        self.ready()
        _, first = self.enter()
        session = self.open_session(first["session_id"])
        try:
            self.inject_stub_binding(session, script="print('not json')\n")
            session.acquire_claim("fixture", reason="claimed work")
            with self.assertRaises(doctor.RemediationRefused) as raised:
                doctor.remediate_repository(session, "fixture")
            self.assertEqual(raised.exception.diagnostics["code"], "remediation-envelope-invalid")
        finally:
            session.close()

    def test_incompatible_envelope_is_refused(self):
        self.ready()
        _, first = self.enter()
        session = self.open_session(first["session_id"])
        try:
            script = STUB_REMEDIATION.replace("mncs.remediation/1", "mncs.remediation/2")
            self.inject_stub_binding(session, script=script)
            session.acquire_claim("fixture", reason="claimed work")
            with self.assertRaises(doctor.RemediationRefused) as raised:
                doctor.remediate_repository(session, "fixture")
            self.assertEqual(raised.exception.diagnostics["code"], "remediation-envelope-incompatible")
            self.assertEqual(raised.exception.diagnostics["schema"], "mncs.remediation/2")
        finally:
            session.close()

    def test_wrong_scope_domain_is_refused(self):
        self.ready()
        _, first = self.enter()
        session = self.open_session(first["session_id"])
        try:
            script = STUB_REMEDIATION.replace('"domain": "repository"', '"domain": "registry"')
            self.inject_stub_binding(session, script=script)
            session.acquire_claim("fixture", reason="claimed work")
            with self.assertRaises(doctor.RemediationRefused) as raised:
                doctor.remediate_repository(session, "fixture")
            self.assertEqual(raised.exception.diagnostics["code"], "remediation-scope-unsupported")
        finally:
            session.close()

    def test_scope_echo_mismatch_is_refused(self):
        self.ready()
        _, first = self.enter()
        session = self.open_session(first["session_id"])
        try:
            script = STUB_REMEDIATION.replace('"target": target', '"target": "/elsewhere"')
            self.inject_stub_binding(session, script=script)
            session.acquire_claim("fixture", reason="claimed work")
            with self.assertRaises(doctor.RemediationRefused) as raised:
                doctor.remediate_repository(session, "fixture")
            self.assertEqual(raised.exception.diagnostics["code"], "remediation-envelope-invalid")
        finally:
            session.close()


class RecoveryBackoffTests(DoctorFixture):
    def open_session(self, session_id):
        return sessions.Session.resume(state_dir=self.state, session_id=session_id, backend="file")

    def broken_session(self):
        self.provider.write_text(BROKEN_PROVIDER)
        self.ready()
        _, first = self.enter()
        (self.project / "service-state.json").write_text(json.dumps({"ready": False, "starts": 0}))
        return sessions.Session.resume(state_dir=self.state, session_id=first["session_id"], backend="file")

    def refuse_gate(self, *, base, max_delay, attempts, elapsed):
        raise AssertionError("gate must not be consulted for a fresh failure identity")

    def test_first_failure_attempts_without_consulting_gate(self):
        session = self.broken_session()
        try:
            calls = []
            with mock.patch.object(session, "invoke",
                                   side_effect=lambda *a, **k: (calls.append(a), {"status": "ok", "stdout": "{}",
                                                                                 "stderr": ""})[1]):
                result = readiness.reconcile_services(session, retry_gate=self.refuse_gate)
            self.assertEqual(len(calls), 1)
            self.assertEqual(result["operations"][0]["status"], "ok")
            backoff = session.snapshot["doctor"]["recovery_backoff"]
            self.assertEqual(len(backoff), 1)
            entry = next(iter(backoff.values()))
            self.assertEqual(entry["attempts"], 1)
            self.assertEqual(entry["service"], "fixture-service")
            # Live observation stays truthful: still degraded, still blocking.
            self.assertEqual(session.snapshot["service_observations"][0]["status"], "degraded")
            self.assertIn("fixture-service", result["readiness"]["blocking"])
        finally:
            session.close()

    def test_unchanged_failure_suppresses_recovery_but_not_observation(self):
        session = self.broken_session()
        try:
            calls = []
            fake_invoke = lambda *a, **k: (calls.append(a), {"status": "ok", "stdout": "{}",
                                                             "stderr": ""})[1]
            with mock.patch.object(session, "invoke", side_effect=fake_invoke):
                readiness.reconcile_services(session, retry_gate=self.refuse_gate)
                suppress = lambda *, base, max_delay, attempts, elapsed: (
                    False, {"native": True, "attempts": attempts, "elapsed_secs": elapsed,
                            "delay_secs": 60, "retry_in_secs": 60 - elapsed})
                result = readiness.reconcile_services(session, retry_gate=suppress)
            self.assertEqual(len(calls), 1, "suppressed run must not invoke recovery")
            operation = result["operations"][0]
            self.assertEqual(operation["status"], "suppressed")
            self.assertTrue(operation["failure_identity"].startswith("fri_"))
            backoff = session.snapshot["doctor"]["recovery_backoff"]
            self.assertEqual(next(iter(backoff.values()))["attempts"], 1)
            self.assertEqual(session.snapshot["service_observations"][0]["status"], "degraded")
            self.assertIn("fixture-service", result["readiness"]["blocking"])
        finally:
            session.close()

    def test_readmitted_recovery_attempts_and_counts_again(self):
        session = self.broken_session()
        try:
            calls = []
            fake_invoke = lambda *a, **k: (calls.append(a), {"status": "ok", "stdout": "{}",
                                                             "stderr": ""})[1]
            admit = lambda *, base, max_delay, attempts, elapsed: (
                True, {"native": True, "attempts": attempts, "elapsed_secs": elapsed})
            with mock.patch.object(session, "invoke", side_effect=fake_invoke):
                readiness.reconcile_services(session, retry_gate=self.refuse_gate)
                result = readiness.reconcile_services(session, retry_gate=admit)
            self.assertEqual(len(calls), 2)
            self.assertEqual(result["operations"][0]["status"], "ok")
            self.assertTrue(result["operations"][0]["retry"]["eligible"])
            backoff = session.snapshot["doctor"]["recovery_backoff"]
            self.assertEqual(next(iter(backoff.values()))["attempts"], 2)
        finally:
            session.close()

    def test_explicit_recovery_bypasses_suppression(self):
        session = self.broken_session()
        try:
            calls = []
            fake_invoke = lambda *a, **k: (calls.append(a), {"status": "ok", "stdout": "{}",
                                                             "stderr": ""})[1]
            suppress = lambda *, base, max_delay, attempts, elapsed: (False, {"native": True})
            with mock.patch.object(session, "invoke", side_effect=fake_invoke):
                readiness.reconcile_services(session, retry_gate=self.refuse_gate)
                result = readiness.reconcile_services(session, force_recovery=True,
                                                      retry_gate=suppress)
            self.assertEqual(len(calls), 2)
            self.assertEqual(result["operations"][0]["status"], "ok")
            self.assertTrue(result["operations"][0]["retry"]["forced"])
        finally:
            session.close()

    def test_rotated_identity_is_immediately_eligible(self):
        session = self.broken_session()
        try:
            calls = []
            fake_invoke = lambda *a, **k: (calls.append(a), {"status": "ok", "stdout": "{}",
                                                             "stderr": ""})[1]
            with mock.patch.object(session, "invoke", side_effect=fake_invoke):
                readiness.reconcile_services(session, retry_gate=self.refuse_gate)
                for item in session.snapshot["bindings"]:
                    if item["capability"] == "fixture.start/1":
                        item["contract_revision"] = "2"
                result = readiness.reconcile_services(session, retry_gate=self.refuse_gate)
            self.assertEqual(len(calls), 2, "rotated identity must re-attempt without gate")
            self.assertEqual(result["operations"][0]["status"], "ok")
        finally:
            session.close()

    def test_resolved_service_drops_backoff(self):
        session = self.broken_session()
        try:
            with mock.patch.object(session, "invoke",
                                   return_value={"status": "ok", "stdout": "{}", "stderr": ""}):
                readiness.reconcile_services(session, retry_gate=self.refuse_gate)
            self.assertEqual(len(session.snapshot["doctor"]["recovery_backoff"]), 1)
            (self.project / "service-state.json").write_text(json.dumps({"ready": True, "starts": 1}))
            with mock.patch.object(session, "invoke",
                                   return_value={"status": "ok", "stdout": "{}", "stderr": ""}):
                result = readiness.reconcile_services(session, retry_gate=self.refuse_gate)
            self.assertEqual(result["operations"], [])
            self.assertEqual(session.snapshot["doctor"]["recovery_backoff"], {})
        finally:
            session.close()

    def test_pending_escalation_is_never_counted_or_suppressed(self):
        session = self.broken_session()
        try:
            pending = {"status": "pending-escalation", "stdout": "", "stderr": "escalation required"}
            calls = []
            fake_invoke = lambda *a, **k: (calls.append(a), dict(pending))[1]
            suppress = lambda *, base, max_delay, attempts, elapsed: (False, {"native": True})
            with mock.patch.object(session, "invoke", side_effect=fake_invoke):
                first = readiness.reconcile_services(session, retry_gate=suppress)
                second = readiness.reconcile_services(session, retry_gate=suppress)
            self.assertEqual(first["operations"][0]["status"], "pending-escalation")
            self.assertEqual(second["operations"][0]["status"], "pending-escalation")
            self.assertEqual(len(calls), 2, "escalations must stay visible every run")
            self.assertEqual(session.snapshot["doctor"].get("recovery_backoff", {}), {})
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main()
