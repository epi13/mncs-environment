from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

from mncs_env.briefing import (
    EVENT_CLASSES,
    _relevant_observation,
    build_capsule,
    classify_event,
)
from mncs_env.native_briefing import (
    project_capsule_inputs_native,
    project_events_native,
)


def _native_toolchain() -> tuple[Path, Path] | None:
    workspace = next(
        (
            parent for parent in Path(__file__).resolve().parents
            if (parent / "mncs-language").is_dir()
            and (parent / "mncs-stdlib").is_dir()
        ),
        None,
    )
    if workspace is None:
        return None
    binary = Path(os.environ.get(
        "MNCS_BIN", workspace / "mncs-language" / "target" / "release" / "mncs",
    ))
    stdlib = Path(os.environ.get("MNCS_STDLIB_ROOT", workspace / "mncs-stdlib"))
    if not binary.is_file() or not os.access(binary, os.X_OK):
        return None
    if not (stdlib / "library").is_dir():
        return None
    return binary.resolve(), stdlib.resolve()


class NativeBriefingClassificationTests(unittest.TestCase):
    def test_native_batch_matches_python_reference_for_event_contracts(self) -> None:
        toolchain = _native_toolchain()
        if toolchain is None:
            self.skipTest("selected MNCS release toolchain is unavailable")
        binary, stdlib = toolchain
        workspace = next(
            parent for parent in Path(__file__).resolve().parents
            if (parent / "mncs-language").is_dir()
            and (parent / "mncs-stdlib").is_dir()
        )

        class Session:
            snapshot: ClassVar[dict] = {
                "workspace": {"root": str(workspace)},
                "selected_checkouts": {
                    "mncs-stdlib": {"path": str(stdlib)},
                },
            }

            def __init__(self, state_dir: Path) -> None:
                self.store = SimpleNamespace(state_dir=state_dir)

            def _selected_runtime_environment(self, _binding: dict) -> dict[str, str]:
                return {"MNCS_BIN": str(binary)}

        events = [
            {
                "session_id": "ses_native_brief",
                "sequence": index + 1,
                "type": kind,
                "observed_at": "2026-11-02T12:00:00+00:00",
                "payload": {"operation": "verify", "reason": "bounded reason"},
            }
            for index, kind in enumerate(EVENT_CLASSES)
        ]
        events.extend([
            {
                "session_id": "ses_native_brief", "sequence": 30,
                "type": "invocation.completed", "observed_at": "t30",
                "payload": {"capability": "test/run", "status": "ok", "reason": "old"},
            },
            {
                "session_id": "ses_native_brief", "sequence": 31,
                "type": "invocation.completed", "observed_at": "t31",
                "payload": {"operation": "verify", "outcome_status": "failed"},
            },
            {
                "session_id": "ses_native_brief", "sequence": 32,
                "type": "environment.rpc.read_observed", "observed_at": "t32",
                "payload": {
                    "operation": "campaign.state", "outcome_status": "succeeded",
                    "measurement_availability": {
                        "control_dispatch_elapsed_ms": "measured",
                    },
                    "measurements": {"control_dispatch_elapsed_ms": 31234.317244},
                },
            },
            {
                "session_id": "ses_native_brief", "sequence": 33,
                "type": "authority.coordination-deferred", "observed_at": "t33",
                "payload": {
                    "action": "execute", "repository": "mncs-atlas",
                    "coordination_status": "overlapping-scope-conflict",
                    "required_coordination": "isolated-worktree",
                    "next_step": "select-or-create-a-worktree",
                    "reason": "/private/workspace/target contains private details",
                    "persistent_authority_required": False,
                    "operation_executed": False,
                },
            },
            {
                "session_id": "ses_native_brief", "sequence": 34,
                "type": "authority.coordination-deferred", "observed_at": "t34",
                "payload": {
                    "action": "Execute", "repository": "mncs-atlas",
                    "coordination_status": "bad--key", "next_step": "path/to/file",
                },
            },
            {
                "session_id": "ses_native_brief", "sequence": 35,
                "type": "lease.acquired", "observed_at": "t35",
                "payload": {
                    "capability": None, "operation": 17, "repository": "repo",
                    "reason": None, "summary": "not selected",
                },
            },
            {
                "session_id": "ses_native_brief", "sequence": 36,
                "type": "capability.changed", "observed_at": "t36",
                "payload": {"status": "failed", "summary": "changed"},
            },
            {
                "session_id": "ses_native_brief", "sequence": 37,
                "type": "routine.event", "observed_at": "t37", "payload": {},
            },
        ])
        while len(events) < 200:
            events.append({
                "session_id": "ses_native_brief",
                "sequence": len(events) + 1,
                "type": "routine.event",
                "observed_at": "2026-11-02T12:01:00+00:00",
                "payload": {},
            })

        with TemporaryDirectory(prefix="mncs-env-native-briefing-") as temporary:
            actual = project_events_native(events, Session(Path(temporary)))

        self.assertEqual(len(events), 200)
        self.assertIsNotNone(actual)
        assert actual is not None
        expected = [classify_event(event) for event in events]
        self.assertEqual(actual, expected)
        self.assertNotIn("/private/workspace", str(actual))

    def test_capsule_uses_the_native_classifier_for_a_bounded_batch(self) -> None:
        toolchain = _native_toolchain()
        if toolchain is None:
            self.skipTest("selected MNCS release toolchain is unavailable")
        binary, stdlib = toolchain
        workspace = next(
            parent for parent in Path(__file__).resolve().parents
            if (parent / "mncs-language").is_dir()
            and (parent / "mncs-stdlib").is_dir()
        )
        events = [{
            "session_id": "ses_capsule_native", "sequence": 1,
            "type": "authority.denied", "observed_at": "2026-11-02T12:00:00+00:00",
            "payload": {"reason": "denied", "repository": "mncs-environment"},
        }]
        observations = [{
            "session_id": "ses_reconciler_native", "sequence": 1,
            "observed_at": "2026-11-02T12:00:01+00:00",
            "payload": {
                "kind": "claim.changed",
                "relations": {"repositories": ["mncs-environment"]},
                "observation": "obs_native",
                "subject": "mncs-environment",
                "summary": "repository claim changed",
            },
        }]

        class Session:
            snapshot: ClassVar[dict] = {
                "lifecycle": "active",
                "intent": {
                    "goal": "native capsule proof",
                    "repositories": ["mncs-environment"],
                },
                "workspace": {"root": str(workspace)},
                "selected_checkouts": {
                    "mncs-stdlib": {"path": str(stdlib)},
                },
            }
            session_id = "ses_capsule_native"

            def __init__(self, state_dir: Path) -> None:
                self.store = SimpleNamespace(state_dir=state_dir)

            def _selected_runtime_environment(self, _binding: dict) -> dict[str, str]:
                return {"MNCS_BIN": str(binary)}

            def _log(self) -> list[dict[str, object]]:
                return events

        observed: list[object] = []
        projector = project_capsule_inputs_native

        def project_and_record(batch, observation_batch, session):
            result = projector(batch, observation_batch, session)
            observed.append(result)
            return result

        with TemporaryDirectory(prefix="mncs-env-native-capsule-") as temporary:
            session = Session(Path(temporary))
            with mock.patch(
                "mncs_env.native_briefing.project_capsule_inputs_native",
                side_effect=project_and_record,
            ) as native_call:
                capsule = build_capsule(
                    session, store=None, reconciler_log=observations,
                )

        native_call.assert_called_once()
        self.assertIsNotNone(observed[0])
        self.assertEqual(len(capsule["items"]), 2)
        self.assertEqual(capsule["items"][0]["category"], "safety")
        self.assertEqual(capsule["items"][0]["priority"], 1)
        self.assertEqual(
            capsule["items"][1]["relevance"],
            "touches a repository in this intent",
        )
        self.assertEqual(
            observed[0][1], ["touches a repository in this intent"],
        )

    def test_native_observation_relevance_matches_python_precedence(self) -> None:
        toolchain = _native_toolchain()
        if toolchain is None:
            self.skipTest("selected MNCS release toolchain is unavailable")
        binary, stdlib = toolchain
        workspace = next(
            parent for parent in Path(__file__).resolve().parents
            if (parent / "mncs-language").is_dir()
            and (parent / "mncs-stdlib").is_dir()
        )

        class Session:
            snapshot: ClassVar[dict] = {
                "intent": {
                    "repositories": ["mncs-environment", "mncs-stdlib"],
                    "commons_work": ["work-7"],
                },
                "workspace": {"root": str(workspace)},
                "selected_checkouts": {
                    "mncs-stdlib": {"path": str(stdlib)},
                },
            }
            session_id = "ses_native_observation"

            def __init__(self, state_dir: Path) -> None:
                self.store = SimpleNamespace(state_dir=state_dir)

            def _selected_runtime_environment(self, _binding: dict) -> dict[str, str]:
                return {"MNCS_BIN": str(binary)}

        payloads = [
            {
                "kind": "claim.changed", "severity": "critical",
                "relations": {
                    "session_id": "ses_native_observation",
                    "repositories": ["mncs-environment"],
                    "commons_work": "work-7",
                },
            },
            {"relations": {"repository": "mncs-environment"}},
            {"relations": {"repositories": ["mncs-stdlib"]}},
            {"relations": {"commons_work": "work-7"}},
            {"kind": "claim.changed", "relations": {}},
            {"severity": "warning", "relations": {}},
            {"severity": "critical", "relations": {}},
            {"severity": "info", "relations": {}},
            {"severity": "critical", "relations": None},
            {
                "kind": "claim.changed", "severity": "critical",
                "relations": {
                    "session_id": 8,
                    "uri": "mncs-environment",
                    "commons_work": "work-7",
                },
            },
        ]
        payloads.extend({"relations": {}} for _ in range(40))
        observations = [
            {"payload": payload, "sequence": index + 1}
            for index, payload in enumerate(payloads)
        ]
        with TemporaryDirectory(prefix="mncs-env-native-observation-") as temporary:
            session = Session(Path(temporary))
            actual = project_capsule_inputs_native([], observations, session)

        self.assertIsNotNone(actual)
        assert actual is not None
        self.assertEqual(actual[0], [])
        self.assertEqual(actual[1], [
            _relevant_observation(payload, session) for payload in payloads
        ])
        self.assertEqual(actual[1], [
            "names this session",
            "touches a repository in this intent",
            "touches a repository in this intent",
            "links work this session references",
            "claim state changed somewhere in the family",
            "elevated severity is family-visible",
            "elevated severity is family-visible",
            None,
            None,
            "touches a repository in this intent",
            *([None] * 40),
        ])

    def test_native_observation_batch_falls_back_when_bounds_are_exceeded(self) -> None:
        observations = [{"payload": {"relations": {}}} for _ in range(51)]
        self.assertIsNone(
            project_capsule_inputs_native(
                [], observations, SimpleNamespace(snapshot={}, session_id="ses_bounds"),
            ),
        )
        single_observation = [{"payload": {"relations": {}}}]
        self.assertIsNone(
            project_capsule_inputs_native(
                [], single_observation,
                SimpleNamespace(
                    snapshot={"intent": {"repositories": ["repo"] * 33}},
                    session_id="ses_bounds",
                ),
            ),
        )
        self.assertIsNone(
            project_capsule_inputs_native(
                [], [{"payload": {"relations": {"repository": "x" * 513}}}],
                SimpleNamespace(snapshot={"intent": {}}, session_id="ses_bounds"),
            ),
        )

    def test_out_of_bounds_event_uses_the_established_python_fallback(self) -> None:
        toolchain = _native_toolchain()
        if toolchain is None:
            self.skipTest("selected MNCS release toolchain is unavailable")
        binary, stdlib = toolchain
        workspace = next(
            parent for parent in Path(__file__).resolve().parents
            if (parent / "mncs-language").is_dir()
            and (parent / "mncs-stdlib").is_dir()
        )

        class Session:
            snapshot: ClassVar[dict] = {
                "workspace": {"root": str(workspace)},
                "selected_checkouts": {
                    "mncs-stdlib": {"path": str(stdlib)},
                },
            }
            store = SimpleNamespace(state_dir=Path("/tmp"))

            def _selected_runtime_environment(self, _binding: dict) -> dict[str, str]:
                return {"MNCS_BIN": str(binary)}

        event = {
            "session_id": "ses_fallback", "sequence": 1,
            "type": "lease.acquired", "payload": {"reason": "x" * 513},
        }
        self.assertIsNone(project_events_native([event], Session()))
        self.assertEqual(
            build_capsule(
                type("CapsuleSession", (), {
                    "snapshot": {
                        "lifecycle": "active", "brief_cursor": {"index": 0},
                        "intent": {"goal": "fallback"},
                    },
                    "session_id": "ses_fallback",
                    "_log": staticmethod(lambda: [event]),
                })(),
                store=None,
            )["items"],
            [classify_event(event)],
        )
