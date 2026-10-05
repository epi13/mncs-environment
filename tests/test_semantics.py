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


def poll_document(*, stream="mnls-stream-1", cursor=9, after=7, reset=False, events=None):
    rows = events if events is not None else ([] if reset else [
        {"cursor": 8, "current_generation": 13,
         "diagnostics": {"added": ["MNP016"], "resolved": []},
         "semantic_subjects": [{"identity": "mncs:subject:1"}],
         "impact_complete": True, "obligations": {"complete": True}},
        {"cursor": 9, "current_generation": 13,
         "diagnostics": {"added": [], "resolved": ["MNP016"]},
         "semantic_subjects": [], "impact_complete": True,
         "obligations": {"complete": True}},
    ])
    return {
        "schema_version": "mncs.workspace-event-cursor/2",
        "stream_identity": stream,
        "after_cursor": after,
        "current_cursor": cursor,
        "oldest_cursor": 1,
        "reset_required": reset,
        "events": rows,
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
        self.capsule_documents: dict[str, dict] = {}
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
            workspace = argv[argv.index("--workspace") + 1] if "--workspace" in argv else ""
            document = self.capsule_documents.get(workspace, self.capsule_document)
            return {"status": "ok", "stdout": json.dumps(document),
                    "stderr": ""}
        raise AssertionError(f"unexpected capability {capability}")


class AmbientSemanticsTests(unittest.TestCase):
    def test_complete_document_reconciliation_supersedes_only_earlier_same_document_events(self) -> None:
        uri = "file:///workspace/removed.mncs"
        incomplete = {
            "cursor": 1, "current": {"uri": uri},
            "semantic_subjects": [], "impact_complete": False,
            "obligations": {"complete": False},
        }
        removal = {
            "cursor": 2, "current": {"uri": uri},
            "affected_documents": [{"uri": uri}],
            "semantic_subjects": [{"identity": "mncs:subject:1", "change": "removed"}],
            "impact_complete": True, "obligations": {"complete": True},
            "reconciled": True, "removed": True,
            "supersedes_through_cursor": 1,
        }
        document = poll_document(cursor=2, after=0, events=[incomplete, removal])
        valid, reason, effective, page_cursor, high_water = semantics_module._poll_window(
            document, "mnls-stream-1", 0)
        self.assertTrue(valid, reason)
        self.assertEqual([event["cursor"] for event in effective], [2])
        self.assertEqual(page_cursor, 2)
        self.assertEqual(high_water, 2)

        unrelated = dict(removal)
        unrelated["current"] = {"uri": "file:///workspace/other.mncs"}
        unrelated["affected_documents"] = [{"uri": "file:///workspace/other.mncs"}]
        document["events"] = [incomplete, unrelated]
        valid, reason, _, _, _ = semantics_module._poll_window(
            document, "mnls-stream-1", 0)
        self.assertFalse(valid)
        self.assertEqual(reason, "semantic event impact is incomplete")

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
        self.assertEqual(outcome["operation_status"], "retry")
        self.assertEqual(session.invoked, [])
        # Degraded passes never cache their epoch.
        self.assertNotIn("epoch", session.snapshot.get("semantics", {}))

    def test_first_contact_replays_complete_retained_stream_without_capsule(self) -> None:
        session = FakeSession()
        session.declare(declared_service())
        session.observe(observation())
        session.bind(semantics_module.POLL_CAPABILITY)
        session.bind(semantics_module.CAPSULE_CAPABILITY)
        rows = [{"cursor": cursor, "current_generation": 12,
                 "diagnostics": {"added": [], "resolved": []},
                 "semantic_subjects": [{"identity": f"subject:{cursor}"}],
                 "impact_complete": True, "obligations": {"complete": True}}
                for cursor in range(1, 8)]
        session.poll_document = poll_document(cursor=7, after=0, events=rows)
        session.capsule_document["status"] = {
            "kind": "unsupported", "reason": "whole-workspace analysis is pending"}
        session.capsule_document["measured"]["analysis_pending_documents"] = 537
        outcome = semantics_module.ambient_pass(session)
        summary = outcome["summary"]
        self.assertEqual(summary["changed"], 1)
        self.assertEqual(outcome["operation_status"], "complete")
        self.assertEqual(summary["semantic_events"], 7)
        self.assertEqual(summary["semantic_subjects"], 7)
        capabilities = [capability for capability, _ in session.invoked]
        self.assertEqual(capabilities, [semantics_module.POLL_CAPABILITY])
        durable = session.snapshot["semantics"]["workspaces"][WORKSPACE]
        self.assertEqual(durable["stream"], "mnls-stream-1")
        self.assertEqual(durable["cursor"], 7)

    def test_first_contact_capsule_fallback_requires_complete_retained_history(self) -> None:
        session = FakeSession()
        session.declare(declared_service())
        session.observe(observation())
        session.bind(semantics_module.POLL_CAPABILITY)
        session.bind(semantics_module.CAPSULE_CAPABILITY)
        session.poll_document = poll_document(cursor=7, after=0, reset=True, events=[])
        outcome = semantics_module.ambient_pass(session)
        self.assertEqual(outcome["summary"]["adopted"], 1)
        self.assertEqual([cap for cap, _ in session.invoked], [
            semantics_module.POLL_CAPABILITY, semantics_module.CAPSULE_CAPABILITY])
        self.assertEqual(session.snapshot["semantics"]["workspaces"][WORKSPACE]["cursor"], 7)

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

    def test_incomplete_poll_retains_cursor_until_capsule_reconciles(self) -> None:
        session = FakeSession()
        session.declare(declared_service())
        session.observe(observation())
        session.bind(semantics_module.POLL_CAPABILITY)
        session.bind(semantics_module.CAPSULE_CAPABILITY)
        semantics_module.ambient_pass(session)
        previous = dict(session.snapshot["semantics"]["workspaces"][WORKSPACE])
        session.snapshot["service_observations"] = [observation(cursor=9, generation=13)]
        session.poll_document = poll_document(events=[
            {"cursor": 8, "current_generation": 13,
             "diagnostics": {"added": [], "resolved": []},
             "semantic_subjects": [], "impact_complete": False,
             "obligations": {"complete": False}},
            {"cursor": 9, "current_generation": 13,
             "diagnostics": {"added": [], "resolved": []},
             "semantic_subjects": [], "impact_complete": True,
             "obligations": {"complete": True}},
        ])
        session.capsule_document = capsule_document(cursor=9, generation=13)
        session.capsule_document["status"] = {
            "kind": "unsupported", "reason": "resident analysis is incomplete"}
        session.capsule_document["measured"]["analysis_pending_documents"] = 3

        blocked = semantics_module.ambient_pass(session)
        self.assertEqual(blocked["summary"]["unknown"], 1)
        self.assertEqual(blocked["operation_status"], "retry")
        self.assertEqual(session.snapshot["semantics"]["workspaces"][WORKSPACE], previous)
        self.assertEqual(session.snapshot["semantics"]["workspaces"][WORKSPACE]["cursor"], 7)

        session.capsule_document = capsule_document(cursor=9, generation=13)
        reconciled = semantics_module.ambient_pass(session)
        self.assertEqual(reconciled["summary"]["reset"], 1)
        self.assertEqual(session.snapshot["semantics"]["workspaces"][WORKSPACE]["cursor"], 9)

    def test_full_poll_page_acknowledges_only_its_last_event(self) -> None:
        session = FakeSession()
        session.declare(declared_service())
        session.observe(observation())
        session.bind(semantics_module.POLL_CAPABILITY)
        session.bind(semantics_module.CAPSULE_CAPABILITY)
        semantics_module.ambient_pass(session)

        rows = [{"cursor": cursor, "current_generation": 13,
                 "diagnostics": {"added": [], "resolved": []},
                 "semantic_subjects": [], "impact_complete": True,
                 "obligations": {"complete": True}}
                for cursor in range(8, 40)]
        session.snapshot["service_observations"] = [observation(cursor=40, generation=13)]
        session.poll_document = poll_document(cursor=40, after=7, events=rows)
        first_page = semantics_module.ambient_pass(session)
        self.assertEqual(first_page["summary"]["changed"], 1)
        self.assertEqual(session.snapshot["semantics"]["workspaces"][WORKSPACE]["cursor"], 39)

        session.poll_document = poll_document(cursor=40, after=39, events=[
            {"cursor": 40, "current_generation": 13,
             "diagnostics": {"added": [], "resolved": []},
             "semantic_subjects": [], "impact_complete": True,
             "obligations": {"complete": True}},
        ])
        second_page = semantics_module.ambient_pass(session)
        self.assertEqual(second_page["summary"]["changed"], 1)
        self.assertEqual(session.snapshot["semantics"]["workspaces"][WORKSPACE]["cursor"], 40)

    def test_old_consumer_cursor_requires_complete_reconciliation(self) -> None:
        session = FakeSession()
        session.declare(declared_service())
        session.observe(observation())
        session.bind(semantics_module.POLL_CAPABILITY)
        session.bind(semantics_module.CAPSULE_CAPABILITY)
        session.snapshot["semantics"] = {"workspaces": {WORKSPACE: {
            "stream": "mnls-stream-1", "cursor": 7, "generation": 12,
            "fingerprint": semantics_module._fingerprint(
                "mnls-stream-1", observation()["provider_observed"]),
        }}}
        session.capsule_document["status"] = {
            "kind": "unsupported", "reason": "resident analysis is incomplete"}
        session.capsule_document["measured"]["analysis_pending_documents"] = 1

        outcome = semantics_module.ambient_pass(session)
        self.assertEqual(outcome["operation_status"], "retry")
        self.assertEqual([capability for capability, _ in session.invoked],
                         [semantics_module.CAPSULE_CAPABILITY])
        durable = session.snapshot["semantics"]["workspaces"][WORKSPACE]
        self.assertNotIn("consumer_protocol", durable)
        self.assertEqual(durable["cursor"], 7)

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
        self.assertEqual(capabilities, [semantics_module.POLL_CAPABILITY,
                                        semantics_module.CAPSULE_CAPABILITY])
        durable = session.snapshot["semantics"]["workspaces"][WORKSPACE]
        self.assertEqual(durable["stream"], "mnls-stream-2")

    def test_stream_change_replays_complete_retained_new_epoch(self) -> None:
        session = FakeSession()
        session.declare(declared_service())
        session.observe(observation())
        session.bind(semantics_module.POLL_CAPABILITY)
        session.bind(semantics_module.CAPSULE_CAPABILITY)
        semantics_module.ambient_pass(session)

        session.snapshot["service_observations"] = [
            observation(stream="mnls-stream-2", cursor=2, generation=14)]
        rows = [{"cursor": cursor, "current_generation": 14,
                 "diagnostics": {"added": [], "resolved": []},
                 "semantic_subjects": [{"identity": f"new-epoch:{cursor}"}],
                 "impact_complete": True, "obligations": {"complete": True}}
                for cursor in (1, 2)]
        session.poll_document = poll_document(
            stream="mnls-stream-2", cursor=2, after=0, events=rows)
        session.capsule_document["status"] = {
            "kind": "unsupported", "reason": "whole-workspace analysis is pending"}
        session.invoked.clear()

        outcome = semantics_module.ambient_pass(session)
        self.assertEqual(outcome["summary"]["changed"], 1)
        self.assertEqual(outcome["summary"]["semantic_subjects"], 2)
        self.assertEqual([cap for cap, _ in session.invoked], [
            semantics_module.POLL_CAPABILITY])
        durable = session.snapshot["semantics"]["workspaces"][WORKSPACE]
        self.assertEqual(durable["stream"], "mnls-stream-2")
        self.assertEqual(durable["cursor"], 2)

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
        self.assertEqual(first["operation_status"], "retry")
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
        session.capsule_document = capsule_document(cursor=9, generation=13)
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
        session.capsule_documents[other] = capsule_document(
            stream="mnls-stream-9", cursor=3, generation=4)
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
