from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import ClassVar
from unittest import TestCase, mock

from mncs_env import native_readiness, readiness


def _workspace_root() -> Path | None:
    for parent in Path(__file__).resolve().parents:
        if (parent / "mncs-language").is_dir() and (parent / "mncs-stdlib").is_dir():
            return parent
    return None


def _native_toolchain() -> tuple[Path, Path] | None:
    workspace = _workspace_root()
    if workspace is None:
        return None
    binary_value = os.environ.get("MNCS_BIN")
    binary = Path(binary_value) if binary_value else workspace / "mncs-language" / "target" / "release" / "mncs"
    stdlib = Path(os.environ.get("MNCS_STDLIB_ROOT", workspace / "mncs-stdlib"))
    if not binary.is_file() or not os.access(binary, os.X_OK) or not (stdlib / "library").is_dir():
        return None
    return binary.resolve(), stdlib.resolve()


class NativeReadinessProjectionTests(TestCase):
    def test_native_token_spans_match_independent_pointer_oracle(self) -> None:
        toolchain_paths = _native_toolchain()
        if toolchain_paths is None:
            self.skipTest("selected MNCS release toolchain is unavailable")
        binary, stdlib = toolchain_paths
        workspace = _workspace_root()
        assert workspace is not None

        document = (
            '{"schema_version":"fixture/1","status":"pass",'
            '"nested":{"slash/key":{"tilde~":true}},'
            '"selected":["one","two"],"number":7,'
            '"container":{"value":1},"container":{"other":2}}'
        )

        class Session:
            session_id = "native-readiness-pointer-proof"
            snapshot: ClassVar[dict] = {
                "workspace": {"root": str(workspace)},
                "selected_checkouts": {"mncs-stdlib": {"path": str(stdlib)}},
            }

            def __init__(self, state_dir: Path) -> None:
                self.store = SimpleNamespace(state_dir=state_dir)

            def _selected_runtime_environment(self, _binding: dict) -> dict[str, str]:
                return {"MNCS_BIN": str(binary)}

        pointers = [
            "/schema_version", "/status", "/nested/slash~1key/tilde~0",
            "/selected", "/number", "/container/value", "/missing",
        ]
        with TemporaryDirectory(prefix="mncs-env-readiness-test-") as temporary:
            actual = native_readiness.project_json_pointers(
                document,
                pointers,
                session=Session(Path(temporary)),
                timeout_seconds=30,
            )

        self.assertIsNotNone(actual)
        assert actual is not None
        reference = json.loads(document)
        self.assertEqual(actual["/schema_version"], "fixture/1")
        self.assertEqual(actual["/status"], reference["status"])
        self.assertEqual(actual["/nested/slash~1key/tilde~0"], True)
        self.assertEqual(actual["/selected"], reference["selected"])
        self.assertEqual(actual["/number"], 7)
        self.assertIs(actual["/container/value"], native_readiness.missing_value())
        self.assertIs(actual["/missing"], native_readiness.missing_value())

    def test_native_projection_replaces_probe_tree_projection(self) -> None:
        toolchain_paths = _native_toolchain()
        if toolchain_paths is None:
            self.skipTest("selected MNCS release toolchain is unavailable")
        binary, stdlib = toolchain_paths
        workspace = _workspace_root()
        assert workspace is not None

        class Session:
            session_id = "native-readiness-probe-proof"
            snapshot: ClassVar[dict] = {
                "workspace": {"root": str(workspace)},
                "selected_checkouts": {"mncs-stdlib": {"path": str(stdlib)}},
                "bindings": [{
                    "provider": "fixture-provider",
                    "capability": "fixture:probe",
                    "availability": {"status": "available"},
                    "effects": ["read"],
                    "provenance": {"addressing": "provider"},
                }],
                "requirements": {"services": [{
                    "identity": "fixture:service",
                    "required": True,
                    "probe": {"capability": "fixture:probe", "argv": []},
                    "response_schema": "fixture.readiness/1",
                    "ready_when": {"/status": "pass", "/nested/available": True},
                }]},
            }

            def __init__(self, state_dir: Path) -> None:
                self.store = SimpleNamespace(state_dir=state_dir)

            def _selected_runtime_environment(self, _binding: dict) -> dict[str, str]:
                return {"MNCS_BIN": str(binary)}

        response = json.dumps({
            "schema_version": "fixture.readiness/1",
            "status": "pass",
            "nested": {"available": True},
            "state": "healthy",
            "diagnostics": [{"code": "fixture"}],
        }, separators=(",", ":"))
        with TemporaryDirectory(prefix="mncs-env-readiness-probe-test-") as temporary:
            session = Session(Path(temporary))
            with mock.patch.object(readiness.authority, "evaluate", return_value={"verdict": "allow", "reason": "test"}), \
                 mock.patch.object(readiness.capabilities, "invoke", return_value={
                     "status": "ok", "stdout": response, "stderr": "",
                 }):
                observation = readiness.probe_services(session)[0]

        self.assertEqual(observation["status"], "ready", observation)
        self.assertEqual(observation["observation"], {
            "/status": "pass", "/nested/available": True,
        })
        self.assertEqual(observation["provider_state"], "healthy")
        self.assertEqual(observation["provider_diagnostics"], [{"code": "fixture"}])

    def test_pointer_bounds_and_escaped_json_keys_use_compatibility_projection(self) -> None:
        self.assertFalse(native_readiness._can_project(b"{" + b" " * 16384 + b"}", ["/status"]))
        toolchain_paths = _native_toolchain()
        if toolchain_paths is None:
            self.skipTest("selected MNCS release toolchain is unavailable")
        binary, stdlib = toolchain_paths
        workspace = _workspace_root()
        assert workspace is not None

        class Session:
            session_id = "native-readiness-escaped-key-proof"
            snapshot: ClassVar[dict] = {
                "workspace": {"root": str(workspace)},
                "selected_checkouts": {"mncs-stdlib": {"path": str(stdlib)}},
            }

            def __init__(self, state_dir: Path) -> None:
                self.store = SimpleNamespace(state_dir=state_dir)

            def _selected_runtime_environment(self, _binding: dict) -> dict[str, str]:
                return {"MNCS_BIN": str(binary)}

        with TemporaryDirectory(prefix="mncs-env-readiness-escaped-test-") as temporary:
            result = native_readiness.project_json_pointers(
                '{"schema_version":"fixture/1","st\\u0061tus":"pass"}',
                ["/schema_version", "/status"],
                session=Session(Path(temporary)),
                timeout_seconds=30,
            )
        self.assertIsNone(result)
        with TemporaryDirectory(prefix="mncs-env-readiness-long-key-test-") as temporary:
            result = native_readiness.project_json_pointers(
                '{"schema_version":"fixture/1","' + "x" * 33 + '":"pass"}',
                ["/schema_version", "/" + "x" * 33],
                session=Session(Path(temporary)),
                timeout_seconds=30,
            )
        self.assertIsNone(result)

    def test_native_readiness_summary_matches_host_reference(self) -> None:
        toolchain_paths = _native_toolchain()
        if toolchain_paths is None:
            self.skipTest("selected MNCS release toolchain is unavailable")
        binary, stdlib = toolchain_paths
        workspace = _workspace_root()
        assert workspace is not None

        class Session:
            session_id = "native-readiness-summary-proof"

            def __init__(self, state_dir: Path) -> None:
                self.snapshot = {
                    "workspace": {"root": str(workspace)},
                    "selected_checkouts": {"mncs-stdlib": {"path": str(stdlib)}},
                }
                self.store = SimpleNamespace(state_dir=state_dir)

            def _selected_runtime_environment(self, _binding: dict) -> dict[str, str]:
                return {"MNCS_BIN": str(binary)}

        cases = [
            (
                {
                    "requirements": {
                        "required_capabilities": [
                            "cap.ready", "cap.unavailable", "cap.unknown", "cap.absent", "cap.duplicate",
                        ],
                        "services": [
                            {"identity": "required-missing", "required": True},
                            {"identity": "optional-missing", "required": False},
                            {"identity": "required-present", "required": True},
                        ],
                    },
                    "workspace": {"scan": {"status": "incomplete"}},
                    "last_reconciled_at": "2026-11-02T10:00:00Z",
                },
                [
                    {"capability": "cap.ready", "availability": {"status": "available"}},
                    {"capability": "cap.unavailable", "availability": {"status": "unavailable"}},
                    {"capability": "cap.unknown", "availability": {"status": "unknown"}},
                    {"capability": "cap.duplicate", "availability": {"status": "available"}},
                    {"capability": "cap.duplicate", "availability": {"status": "unavailable"}},
                ],
                [
                    {"identity": "required-present", "required": True, "status": "ready"},
                    {"identity": "optional-status", "required": False, "status": "degraded"},
                ],
                {
                    "status": "blocked",
                    "available_count": 2,
                    "unavailable_count": 2,
                    "unknown_count": 1,
                    "required_unavailable": [
                        "cap.unavailable", "cap.unknown", "cap.absent", "cap.duplicate",
                    ],
                    "required_unknown": ["cap.unknown", "cap.absent"],
                    "blocking": [
                        "cap.unavailable", "cap.unknown", "cap.absent", "cap.duplicate",
                        "required-missing", "workspace-scan-incomplete",
                    ],
                },
            ),
            (
                {
                    "requirements": {"required_capabilities": [], "services": []},
                    "workspace": {"scan": {"status": "complete"}},
                },
                [],
                [],
                {
                    "status": "degraded", "available_count": 0,
                    "unavailable_count": 0, "unknown_count": 0,
                    "required_unavailable": [], "required_unknown": [], "blocking": [],
                },
            ),
            (
                {
                    "requirements": {"required_capabilities": ["cap.ready"], "services": []},
                    "workspace": {"scan": {"status": "complete"}},
                },
                [{"capability": "cap.ready", "availability": {"status": "available"}}],
                [],
                {
                    "status": "ready", "available_count": 1,
                    "unavailable_count": 0, "unknown_count": 0,
                    "required_unavailable": [], "required_unknown": [], "blocking": [],
                },
            ),
            (
                {
                    "requirements": {"required_capabilities": [], "services": []},
                    "workspace": {"scan": {"status": "complete"}},
                },
                [{"capability": "cap.ready", "availability": {"status": "available"}}],
                [{"identity": "optional-service", "required": False, "status": "degraded"}],
                {
                    "status": "degraded", "available_count": 1,
                    "unavailable_count": 0, "unknown_count": 0,
                    "required_unavailable": [], "required_unknown": [], "blocking": [],
                },
            ),
        ]

        with TemporaryDirectory(prefix="mncs-env-readiness-summary-test-") as temporary:
            session = Session(Path(temporary))
            for snapshot, bindings, services, expected in cases:
                with self.subTest(expected_status=expected["status"]):
                    reference = readiness.summarize(
                        snapshot, bindings=bindings, services=services, live=True,
                    )
                    actual = readiness.summarize(
                        snapshot, bindings=bindings, services=services,
                        live=True, session=session,
                    )
                    self.assertEqual(actual, reference)
                    for field, value in expected.items():
                        self.assertEqual(actual[field], value, field)


def test_native_policy_runs_with_read_only_optional_cache(tmp_path):
    import pytest
    selected = _native_toolchain()
    if selected is None:
        pytest.skip("selected native toolchain unavailable")
    binary, stdlib = selected
    state = tmp_path / "state"
    state.mkdir()
    session = SimpleNamespace(
        store=SimpleNamespace(state_dir=state),
        snapshot={"workspace": {"root": str(stdlib.parent)},
                  "selected_checkouts": {"mncs-stdlib": {"path": str(stdlib)}}},
        _selected_runtime_environment=lambda _: {"MNCS_BIN": str(binary)},
    )
    state.chmod(0o555)
    try:
        result = native_readiness.project_json_pointers(
            '{"status":"pass"}', ["/status"], session=session, timeout_seconds=30,
        )
        assert result["/status"] == "pass"
        assert not (state / "native-applications").exists()
    finally:
        state.chmod(0o755)
