from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import ClassVar
from unittest import mock

from mncs_env import doctor, native_readiness
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


def _bindings() -> list[dict]:
    return [
        {
            "provider": "mncs-language",
            "capability": "mncs-language:compiler",
            "availability": {
                "status": "available",
                "code": "toolchain-missing",
                "reason": "available status takes precedence",
            },
        },
        {
            "provider": "mncs-language",
            "capability": "mncs-language:lsp",
            "provenance": {
                "kind": "compiler-cli",
                "source": "mncs-language/.mncs/project.json",
                "declared_invocation": True,
            },
            "availability": {
                "status": "unknown",
                "code": "executable-unavailable",
                "reason": "selected executable is missing",
            },
        },
        {
            "provider": "mncs-stdlib",
            "capability": "mncs-stdlib:collections",
            "provenance": {
                "kind": "storage-contract",
                "source": "mncs-stdlib/.mncs/project.json",
                "declared_invocation": False,
            },
            "availability": {
                "status": "unavailable",
                "code": "toolchain-missing",
                "reason": "selected runtime is unavailable",
            },
        },
        {
            "provider": "unsafe provider/path",
            "capability": "provider:declaration-gap",
            "provenance": {
                "kind": "invalid kind!",
                "source": "../outside/project.json",
                "declared_invocation": "unknown",
            },
            "availability": {
                "status": "unknown",
                "code": "provider-invocation-undeclared",
                "reason": "no invocation descriptor",
            },
        },
        {
            "provider": "mncs-control",
            "capability": "mncs-control:protocol",
            "availability": {"status": "unknown", "code": 7, "reason": None},
        },
    ]


class NativeDoctorClassificationTests(unittest.TestCase):
    def test_native_classification_matches_reference_and_is_used(self) -> None:
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

        bindings = _bindings()
        expected = doctor._classify_unavailable_python(bindings)
        from mncs_env import native_doctor

        with TemporaryDirectory(prefix="mncs-env-doctor-native-") as root:
            session = Session(Path(root))
            with mock.patch(
                "mncs_env.native_readiness._run_native_application",
                wraps=native_readiness._run_native_application,
            ) as native_call:
                actual = doctor.classify_unavailable(bindings, session=session)
        self.assertEqual(actual, expected)
        self.assertEqual(native_call.call_count, 1)
        self.assertEqual(
            native_call.call_args.kwargs["descriptor_name"],
            "doctor-unavailable-classification.json",
        )
        self.assertEqual(
            sum(len(actual[key]) for key in ("transient", "gaps")),
            len(bindings) - 1,
        )
        self.assertNotIn("provider", actual["gaps"][0])
        self.assertNotIn("provider_source", actual["gaps"][0])
        self.assertEqual(
            native_doctor.classify_unavailable_native(bindings, session),
            [0, 1, 1, 2, 2],
        )

    def test_missing_toolchain_uses_python_classification(self) -> None:
        bindings = _bindings()
        expected = doctor._classify_unavailable_python(bindings)
        with mock.patch(
            "mncs_env.native_doctor.native_readiness._toolchain_paths",
            return_value=None,
        ), mock.patch(
            "mncs_env.native_doctor.native_readiness._run_native_application",
            side_effect=AssertionError("native runner should not be selected"),
        ):
            actual = doctor.classify_unavailable(
                bindings,
                session=SimpleNamespace(snapshot={}, store=SimpleNamespace(state_dir="/tmp")),
            )
        self.assertEqual(actual, expected)

    def test_selected_native_application_failure_does_not_fall_back(self) -> None:
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

        with mock.patch(
            "mncs_env.native_readiness._run_native_application",
            side_effect=NativeApplicationError("selected native Doctor run failed"),
        ), self.assertRaisesRegex(
            NativeApplicationError, "selected native Doctor run failed",
        ):
            doctor.classify_unavailable(_bindings(), session=Session())


if __name__ == "__main__":
    unittest.main()
