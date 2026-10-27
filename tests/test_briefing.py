from __future__ import annotations

import unittest

from mncs_env.briefing import build_capsule, classify_event


class EnvironmentRpcBriefTests(unittest.TestCase):
    def test_coordination_deferral_is_actionable_and_excludes_raw_reason(self) -> None:
        event = {
            "session_id": "ses_example",
            "sequence": 8,
            "type": "authority.coordination-deferred",
            "observed_at": "2026-10-26T11:00:00+00:00",
            "payload": {
                "action": "execute",
                "repository": "mncs-atlas",
                "target": "/private/workspace/mncs-atlas/src/shared.mn",
                "reason": (
                    "/private/workspace/mncs-atlas/src/shared.mn overlaps a live claim"
                ),
                "decision_domain": "coordination",
                "coordination_status": "overlapping-scope-conflict",
                "required_coordination": (
                    "non-overlapping-operation-scope-or-isolated-worktree"
                ),
                "next_step": "select-or-create-an-isolated-worktree",
                "persistent_authority_required": False,
                "operation_executed": False,
            },
        }

        class Session:
            session_id = "ses_example"
            snapshot = {
                "lifecycle": "active",
                "intent": {"goal": "advance the compiler"},
            }

            @staticmethod
            def _log() -> list[dict[str, object]]:
                return [event]

        capsule = build_capsule(Session(), store=None)
        self.assertEqual(len(capsule["items"]), 1)
        item = capsule["items"][0]
        self.assertEqual(item["category"], "coordination")
        self.assertEqual(item["subject"], "mncs-atlas")
        self.assertEqual(item["action"], "execute")
        self.assertEqual(
            item["summary"],
            "Operation deferred; next step: select-or-create-an-isolated-worktree",
        )
        self.assertEqual(
            item["next_step"], "select-or-create-an-isolated-worktree",
        )
        self.assertFalse(item["persistent_authority_required"])
        self.assertFalse(item["operation_executed"])
        self.assertNotIn("/private/workspace", str(item))

    def test_read_observation_flows_into_the_bounded_capsule(self) -> None:
        event = {
            "session_id": "ses_example",
            "sequence": 7,
            "type": "environment.rpc.read_observed",
            "observed_at": "2026-10-25T11:00:00+00:00",
            "payload": {
                "operation": "campaign.state",
                "outcome_status": "succeeded",
                "measurement_availability": {
                    "control_dispatch_elapsed_ms": "measured",
                },
                "measurements": {"control_dispatch_elapsed_ms": 42.5},
            },
        }

        class Session:
            def __init__(self) -> None:
                self.session_id = "ses_example"
                self.snapshot = {
                    "lifecycle": "active",
                    "intent": {"goal": "inspect campaign"},
                }

            @staticmethod
            def _log() -> list[dict[str, object]]:
                return [event]

        capsule = build_capsule(Session(), store=None)

        self.assertEqual(capsule["cursor"], {"index": 0, "total": 1})
        self.assertEqual(len(capsule["items"]), 1)
        self.assertEqual(capsule["items"][0]["kind"], "environment.rpc.read_observed")
        self.assertEqual(
            capsule["items"][0]["measurements"],
            {"control_dispatch_elapsed_ms": 42.5},
        )

    def test_success_exposes_operation_and_measured_dispatch_scope(self) -> None:
        item = classify_event({
            "session_id": "ses_example",
            "sequence": 7,
            "type": "environment.rpc.read_observed",
            "observed_at": "2026-10-25T11:00:00+00:00",
            "payload": {
                "operation": "campaign.state",
                "outcome_status": "succeeded",
                "measurement_availability": {
                    "control_dispatch_elapsed_ms": "measured",
                },
                "measurements": {"control_dispatch_elapsed_ms": 31234.317244},
            },
        })

        assert item is not None
        self.assertEqual(item["category"], "progress")
        self.assertEqual(item["subject"], "campaign.state")
        self.assertEqual(item["summary"], "Environment read operation succeeded")
        self.assertEqual(
            item["measurement_availability"],
            {"control_dispatch_elapsed_ms": "measured"},
        )
        self.assertEqual(
            item["measurements"], {"control_dispatch_elapsed_ms": 31234.317},
        )
        self.assertIn("not total agent execution duration", item["measurement_scope"])
        self.assertEqual(item["ref"], {"session_id": "ses_example", "sequence": 7})

    def test_failure_is_diagnostic_and_unavailable_timing_stays_unmeasured(self) -> None:
        item = classify_event({
            "session_id": "ses_example",
            "sequence": 8,
            "type": "environment.rpc.read_observed",
            "payload": {
                "operation": "campaign.state",
                "outcome_status": "failed",
                "failure_code": "store-unavailable",
                "measurement_availability": {
                    "control_dispatch_elapsed_ms": "unavailable",
                },
                "measurements": {},
            },
        })

        assert item is not None
        self.assertEqual(item["category"], "diagnostics")
        self.assertEqual(item["priority"], 4)
        self.assertEqual(item["subject"], "campaign.state")
        self.assertEqual(item["summary"], "Environment read operation failed")
        self.assertEqual(
            item["measurement_availability"],
            {"control_dispatch_elapsed_ms": "unavailable"},
        )
        self.assertNotIn("measurements", item)

    def test_invalid_timing_value_is_not_projected_as_a_measurement(self) -> None:
        for invalid_timing in (float("nan"), 10 ** 1000):
            with self.subTest(invalid_timing=type(invalid_timing).__name__):
                item = classify_event({
                    "session_id": "ses_example",
                    "sequence": 9,
                    "type": "environment.rpc.read_observed",
                    "payload": {
                        "operation": "campaign.state",
                        "outcome_status": "succeeded",
                        "measurement_availability": {
                            "control_dispatch_elapsed_ms": "measured",
                        },
                        "measurements": {
                            "control_dispatch_elapsed_ms": invalid_timing,
                        },
                    },
                })

                assert item is not None
                self.assertNotIn("measurements", item)
