"""Ambient semantic coherence: durable cursors over resident providers.

Fake sessions drive semantics.ambient_pass with stubbed provider
invocations, so quiet/current/adopt/reset/degraded behavior and epoch
reuse are exercised hermetically. Live composition against the real
provider is exercised as scenarios.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mncs_env import semantics as semantics_module  # noqa: E402


SERVICE_ID = "mncs-language-service:fixture-docs"
WORKSPACE = "/tmp/mncs-semantics-fixture-docs"


def observation(*, status="ready", stream="mnls-stream-1", cursor=7,
                generation=12, error=0, warning=1, fail=0, unknown=1,
                workspace=WORKSPACE, identity=SERVICE_ID):
    return {
        "identity": identity,
        "status": status,
        "capability": semantics_module.STATUS_CAPABILITY,
        "provider_selected": {"workspace_root": workspace},
        "provider_observed": {
            "generation": generation,
            "stream_identity": stream,
            "event_cursor": cursor,
            "documents": 3,
            "diagnostics_error": error,
            "diagnostics_warning": warning,
            "obligations_fail": fail,
            "obligations_unknown": unknown,
        },
    }


def declared_service(workspace=WORKSPACE):
    return {
        "identity": SERVICE_ID,
        "required": False,
        "probe": {"capability": semantics_module.STATUS_CAPABILITY,
                  "argv": ["--workspace", workspace]},
        "ready_when": {"/ready": True},
        "response_schema": "mncs.language-service.resident-status/1",
    }


def capsule_document(*, stream="mnls-stream-1", cursor=7, generation=12):
    return {
        "schema_version": "mncs.language-service.semantic-capsule/1",
        "status": {"kind": "answered"},
        "stream_identity": stream,
        "current_cursor": cursor,
        "generation": generation,
        "measured": {"diagnostics": 1, "changed_subjects": 0,
                     "obligations": 1, "affected_modules": 0},
        "findings": [
            {"kind": "diagnostic", "relevance": "actionable"},
            {"kind": "obligation", "relevance": "watch"},
        ],
    }


def poll_document(*, stream="mnls-stream-1", cursor=9, reset=False):
    return {
        "schema_version": "mncs.workspace-event-cursor/2",
        "stream_identity": stream,
        "current_cursor": cursor,
        "reset_required": reset,
        "events": [] if reset else [
            {"cursor": 8, "current_generation": 13,
             "diagnostics": {"added": ["MNP016"], "resolved": []},
             "semantic_subjects": [{"identity": "mncs:subject:1"}]},
            {"cursor": 9, "current_generation": 13,
             "diagnostics": {"added": [], "resolved": ["MNP016"]},
             "semantic_subjects": []},
        ],
    }


class FakeSession:
    def __init__(self):
        self.session_id = "ses_semantics_fixture"
        self.snapshot: dict = {
            "requirements": {"services": []},
            "service_observations": [],
            "bindings": [],
        }
        self.saved = 0
        self.invoked: list[tuple[str, list[str]]] = []
        self.poll_document = poll_document()
        self.capsule_document = capsule_document()
        self.fail_capabilities: set[str] = set()

    def _save(self) -> None:
        self.saved += 1

    def bind(self, capability: str) -> None:
        self.snapshot["bindings"].append(
            {"capability": capability,
             "provider": "mncs-language-service",
             "provider_root": "/tmp/mncs-language-service",
             "availability": {"status": "available"}})

    def declare(self, service: dict) -> None:
        self.snapshot["requirements"]["services"].append(service)

    def observe(self, item: dict) -> None:
        self.snapshot["service_observations"].append(item)

    def invoke(self, capability: str, argv: list[str], *,
               cwd=None, timeout_seconds=None,
               output_limit_bytes=None, env=None) -> dict:
        import json
        self.invoked.append((capability, list(argv)))
        if capability in self.fail_capabilities:
            return {"status": "error", "stderr": "stub failure"}
        if capability == semantics_module.POLL_CAPABILITY:
            return {"status": "ok", "stdout": json.dumps(self.poll_document),
                    "stderr": ""}
        if capability == semantics_module.CAPSULE_CAPABILITY:
            return {"status": "ok", "stdout": json.dumps(self.capsule_document),
                    "stderr": ""}
        raise AssertionError(f"unexpected capability {capability}")


class AmbientSemanticsTests(unittest.TestCase):
    def test_undeclared_pass_is_quiet_and_invokes_nothing(self) -> None:
        session = FakeSession()
        outcome = semantics_module.ambient_pass(session)
        self.assertTrue(outcome["reused"])
        self.assertEqual(outcome["summary"]["declared"], 0)
        self.assertEqual(session.invoked, [])
        self.assertEqual(session.saved, 0)

    def test_degraded_service_invokes_nothing_and_names_doctor(self) -> None:
        session = FakeSession()
        session.declare(declared_service())
        session.observe(observation(status="degraded"))
        outcome = semantics_module.ambient_pass(session)
        summary = outcome["summary"]
        self.assertEqual(summary["degraded"], 1)
        self.assertEqual(session.invoked, [])
        # Degraded passes never cache their epoch.
        self.assertNotIn("epoch", session.snapshot.get("semantics", {}))

    def test_first_contact_adopts_through_the_bounded_capsule(self) -> None:
        session = FakeSession()
        session.declare(declared_service())
        session.observe(observation())
        session.bind(semantics_module.POLL_CAPABILITY)
        session.bind(semantics_module.CAPSULE_CAPABILITY)
        outcome = semantics_module.ambient_pass(session)
        summary = outcome["summary"]
        self.assertEqual(summary["adopted"], 1)
        self.assertEqual(summary["actionable"], 1)
        capabilities = [capability for capability, _ in session.invoked]
        self.assertEqual(capabilities, [semantics_module.CAPSULE_CAPABILITY])
        durable = session.snapshot["semantics"]["workspaces"][WORKSPACE]
        self.assertEqual(durable["stream"], "mnls-stream-1")
        self.assertEqual(durable["cursor"], 7)

    def test_empty_stream_baseline_does_not_force_workspace_analysis(self) -> None:
        session = FakeSession()
        session.declare(declared_service())
        session.observe(observation(cursor=0, generation=0, error=0, warning=0,
                                    fail=0, unknown=0))
        session.bind(semantics_module.POLL_CAPABILITY)
        session.bind(semantics_module.CAPSULE_CAPABILITY)
        adopted = semantics_module.ambient_pass(session)
        self.assertEqual(adopted["summary"]["adopted"], 1)
        self.assertEqual(session.invoked, [])
        durable = session.snapshot["semantics"]["workspaces"][WORKSPACE]
        self.assertEqual(durable["stream"], "mnls-stream-1")
        self.assertEqual(durable["cursor"], 0)

        session.snapshot["service_observations"] = [
            observation(stream="mnls-stream-2", cursor=0, generation=0,
                        error=0, warning=0, fail=0, unknown=0)]
        reset = semantics_module.ambient_pass(session)
        self.assertEqual(reset["summary"]["reset"], 1)
        self.assertEqual(session.invoked, [])
        durable = session.snapshot["semantics"]["workspaces"][WORKSPACE]
        self.assertEqual(durable["stream"], "mnls-stream-2")
        self.assertEqual(durable["cursor"], 0)

    def test_quiet_reentry_compares_snapshots_without_invocation(self) -> None:
        session = FakeSession()
        session.declare(declared_service())
        session.observe(observation())
        session.bind(semantics_module.POLL_CAPABILITY)
        session.bind(semantics_module.CAPSULE_CAPABILITY)
        first = semantics_module.ambient_pass(session)
        self.assertFalse(first["reused"])
        session.invoked.clear()
        second = semantics_module.ambient_pass(session)
        self.assertTrue(second["reused"])
        self.assertEqual(second["summary"]["adopted"], 1)
        self.assertEqual(session.invoked, [])

    def test_generation_advance_resumes_the_bounded_window(self) -> None:
        session = FakeSession()
        session.declare(declared_service())
        session.observe(observation())
        session.bind(semantics_module.POLL_CAPABILITY)
        session.bind(semantics_module.CAPSULE_CAPABILITY)
        semantics_module.ambient_pass(session)
        # The resident generation advances on the same stream.
        session.snapshot["service_observations"] = [
            observation(cursor=9, generation=13)]
        session.invoked.clear()
        outcome = semantics_module.ambient_pass(session)
        summary = outcome["summary"]
        self.assertEqual(summary["changed"], 1)
        capabilities = [capability for capability, _ in session.invoked]
        self.assertEqual(capabilities, [semantics_module.POLL_CAPABILITY])
        _, argv = session.invoked[0]
        self.assertIn("mnls-stream-1", argv)
        self.assertIn("7", argv)
        durable = session.snapshot["semantics"]["workspaces"][WORKSPACE]
        self.assertEqual(durable["cursor"], 9)

    def test_stream_change_reconciles_explicitly_through_capsule(self) -> None:
        session = FakeSession()
        session.declare(declared_service())
        session.observe(observation())
        session.bind(semantics_module.POLL_CAPABILITY)
        session.bind(semantics_module.CAPSULE_CAPABILITY)
        semantics_module.ambient_pass(session)
        # The resident restarts into a fresh stream epoch.
        session.snapshot["service_observations"] = [
            observation(stream="mnls-stream-2", cursor=2, generation=14)]
        session.capsule_document = capsule_document(stream="mnls-stream-2",
                                                    cursor=2, generation=14)
        session.invoked.clear()
        outcome = semantics_module.ambient_pass(session)
        summary = outcome["summary"]
        self.assertEqual(summary["reset"], 1)
        capabilities = [capability for capability, _ in session.invoked]
        self.assertEqual(capabilities, [semantics_module.CAPSULE_CAPABILITY])
        durable = session.snapshot["semantics"]["workspaces"][WORKSPACE]
        self.assertEqual(durable["stream"], "mnls-stream-2")

    def test_incomplete_capsule_never_acknowledges_first_contact_or_reset(self) -> None:
        session = FakeSession()
        session.declare(declared_service())
        session.observe(observation())
        session.bind(semantics_module.POLL_CAPABILITY)
        session.bind(semantics_module.CAPSULE_CAPABILITY)
        session.capsule_document = capsule_document()
        session.capsule_document["status"] = {
            "kind": "unsupported", "reason": "workspace analysis is incomplete"}
        session.capsule_document["measured"]["analysis_pending_documents"] = 3

        first = semantics_module.ambient_pass(session)
        self.assertEqual(first["summary"]["unknown"], 1)
        self.assertEqual(session.snapshot["semantics"]["workspaces"], {})

        # Establish an acknowledged old epoch, then prove a new epoch cannot
        # move that durable cursor until its capsule is complete.
        session.capsule_document = capsule_document()
        semantics_module.ambient_pass(session)
        previous = dict(session.snapshot["semantics"]["workspaces"][WORKSPACE])
        session.snapshot["service_observations"] = [
            observation(stream="mnls-stream-2", cursor=2, generation=14)]
        session.capsule_document = capsule_document(stream="mnls-stream-2",
                                                    cursor=2, generation=14)
        session.capsule_document["status"] = {
            "kind": "unsupported", "reason": "workspace analysis is incomplete"}
        session.capsule_document["measured"]["analysis_pending_documents"] = 3
        reset = semantics_module.ambient_pass(session)
        self.assertEqual(reset["summary"]["unknown"], 1)
        self.assertEqual(session.snapshot["semantics"]["workspaces"][WORKSPACE], previous)

    def test_poll_reset_falls_back_to_capsule(self) -> None:
        session = FakeSession()
        session.declare(declared_service())
        session.observe(observation())
        session.bind(semantics_module.POLL_CAPABILITY)
        session.bind(semantics_module.CAPSULE_CAPABILITY)
        semantics_module.ambient_pass(session)
        session.snapshot["service_observations"] = [
            observation(cursor=9, generation=13)]
        session.poll_document = poll_document(reset=True)
        session.invoked.clear()
        outcome = semantics_module.ambient_pass(session)
        self.assertEqual(outcome["summary"]["reset"], 1)
        capabilities = [capability for capability, _ in session.invoked]
        self.assertEqual(capabilities, [semantics_module.POLL_CAPABILITY,
                                        semantics_module.CAPSULE_CAPABILITY])

    def test_missing_delta_capabilities_yield_unknown(self) -> None:
        session = FakeSession()
        session.declare(declared_service())
        session.observe(observation())
        outcome = semantics_module.ambient_pass(session)
        self.assertEqual(outcome["summary"]["unknown"], 1)
        self.assertEqual(session.invoked, [])

    def test_invocation_failure_yields_unknown(self) -> None:
        session = FakeSession()
        session.declare(declared_service())
        session.observe(observation())
        session.bind(semantics_module.POLL_CAPABILITY)
        session.bind(semantics_module.CAPSULE_CAPABILITY)
        session.fail_capabilities.add(semantics_module.CAPSULE_CAPABILITY)
        outcome = semantics_module.ambient_pass(session)
        self.assertEqual(outcome["summary"]["unknown"], 1)

    def test_workspaces_keep_separate_durable_cursors(self) -> None:
        other = "/tmp/mncs-semantics-fixture-other"
        second_service = declared_service(workspace=other)
        second_service["identity"] = "mncs-language-service:fixture-other"
        session = FakeSession()
        session.declare(declared_service())
        session.declare(second_service)
        session.observe(observation())
        session.observe(observation(workspace=other, stream="mnls-stream-9",
                                    cursor=3, generation=4,
                                    identity=second_service["identity"]))
        session.bind(semantics_module.POLL_CAPABILITY)
        session.bind(semantics_module.CAPSULE_CAPABILITY)
        outcome = semantics_module.ambient_pass(session)
        self.assertEqual(outcome["summary"]["adopted"], 2)
        durable = session.snapshot["semantics"]["workspaces"]
        self.assertEqual(set(durable), {WORKSPACE, other})
        self.assertEqual(durable[WORKSPACE]["cursor"], 7)

    def test_malformed_probe_observation_yields_unknown(self) -> None:
        session = FakeSession()
        session.declare(declared_service())
        broken = observation()
        del broken["provider_observed"]
        session.observe(broken)
        session.bind(semantics_module.POLL_CAPABILITY)
        session.bind(semantics_module.CAPSULE_CAPABILITY)
        outcome = semantics_module.ambient_pass(session)
        self.assertEqual(outcome["summary"]["unknown"], 1)
        self.assertEqual(session.invoked, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
