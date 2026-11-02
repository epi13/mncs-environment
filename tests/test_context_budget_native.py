from __future__ import annotations

import copy
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

from mncs_env import context_budget
from mncs_env.native_readiness import NativeApplicationError


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


class NativeContextBudgetTests(unittest.TestCase):
    def test_missing_toolchain_uses_python_without_preparing_native_handles(self) -> None:
        context = {
            "family": {
                "summary": {"status": "ready"},
                "capsule": {"attention": [{"id": str(i)} for i in range(5)]},
                "detail": "x" * 1800,
            },
        }
        budget = context_budget.validate({})
        with TemporaryDirectory(prefix="mncs-env-no-native-budget-") as root:
            session = SimpleNamespace(
                session_id="python-context-budget",
                state_dir=Path(root),
            )
            expected = context_budget._apply_python(
                session, copy.deepcopy(context), budget,
                context_budget._blocks(copy.deepcopy(context)),
            )
            with mock.patch(
                "mncs_env.native_context_budget.selected_toolchain_paths",
                return_value=None,
            ), mock.patch(
                "mncs_env.context_budget._native_materials",
                side_effect=AssertionError("native preparation should be skipped"),
            ):
                actual = context_budget.apply(session, copy.deepcopy(context), budget)
        self.assertEqual(actual, expected)

    def test_native_plan_matches_reference_and_is_used_by_context_consumer(self) -> None:
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
            session_id = "native-context-budget"

            def __init__(self, state_dir: Path) -> None:
                self.state_dir = state_dir
                self.store = SimpleNamespace(state_dir=state_dir)

            def _selected_runtime_environment(self, _binding: dict) -> dict[str, str]:
                return {"MNCS_BIN": str(binary)}

        attention_case = {
            "family": {
                "summary": {"status": "ready"},
                "capsule": {"attention": [{"id": str(i)} for i in range(5)]},
            },
        }
        aggregate_case = {
            key: {
                "summary": {"reason": "x" * 550},
                "capsule": {"attention": []},
            }
            for key in context_budget.LAYERS
        }
        minimal_handle_case = {
            "family": {
                "summary": {
                    name: "x" * 32
                    for name in (
                        "blockers", "status", "obligations", "failed",
                        "pending", "current", "degraded", "escalated",
                    )
                },
                "capsule": {"attention": []},
                "detail": "y" * 1000,
            },
        }
        budget = context_budget.validate({})
        minimal_budget = context_budget.validate({
            "context_budget": {
                "healthy_bytes": 512,
                "degraded_bytes": 512,
            },
        })
        cases = [attention_case, aggregate_case, minimal_handle_case]
        from mncs_env import native_context_budget

        with TemporaryDirectory(prefix="mncs-env-native-budget-") as actual_root, \
                TemporaryDirectory(prefix="mncs-env-python-budget-") as reference_root:
            actual_session = Session(Path(actual_root))
            reference_session = Session(Path(reference_root))
            actual_results = []
            with mock.patch(
                "mncs_env.native_context_budget.native_readiness._run_native_application",
                wraps=native_context_budget.native_readiness._run_native_application,
            ) as native_call:
                for index, source in enumerate(cases):
                    case_budget = minimal_budget if index == 2 else budget
                    actual = context_budget.apply(
                        actual_session, copy.deepcopy(source), case_budget,
                    )
                    actual_results.append(actual)
                    expected = context_budget._apply_python(
                        reference_session, copy.deepcopy(source), case_budget,
                        context_budget._blocks(copy.deepcopy(source)),
                    )
                    self.assertEqual(actual, expected)
            self.assertEqual(native_call.call_count, len(cases))
            self.assertEqual(
                native_call.call_args_list[0].kwargs["descriptor_name"],
                "context-budget-plan.json",
            )
            self.assertTrue(actual_results[0]["family"].get("budgeted"))
            compacted = sum(
                "budgeted" in block for block in actual_results[1].values()
            )
            self.assertGreater(compacted, 0)
            self.assertLess(compacted, len(context_budget.LAYERS))
            self.assertEqual(
                set(actual_results[2]["family"]["summary"]),
                {"blockers", "status"},
            )

    def test_native_application_failure_does_not_fall_back_to_python(self) -> None:
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
            session_id = "native-context-budget-failure"
            state_dir = Path("/tmp")

            def _selected_runtime_environment(self, _binding: dict) -> dict[str, str]:
                return {"MNCS_BIN": str(binary)}

        context = {
            "family": {
                "summary": {"blockers": 1, "reason": "x" * 2000},
                "capsule": {"attention": []},
            },
        }
        with mock.patch(
            "mncs_env.native_context_budget.native_readiness._run_native_application",
            side_effect=NativeApplicationError("selected native run failed"),
        ), self.assertRaisesRegex(NativeApplicationError, "selected native run failed"):
            context_budget.apply(Session(), context, context_budget.validate({}))
