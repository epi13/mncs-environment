"""Focused routing tests for `mncs-env test` (no live sessions)."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mncs_env import testcmd  # noqa: E402


class FakeSession:
    def __init__(self, snapshot):
        self.snapshot = snapshot


def make_repo(path: Path, *, with_inventory: bool) -> Path:
    dot = path / ".mncs"
    dot.mkdir(parents=True, exist_ok=True)
    inventory = ".mncs/verification-obligations.json" if with_inventory else ""
    (dot / "project.json").write_text(
        json.dumps(
            {
                "repository": path.name,
                "verification": {"obligation_inventory": inventory},
            }
        ),
        encoding="utf-8",
    )
    if with_inventory:
        (dot / "verification-obligations.json").write_text(
            json.dumps({"obligations": []}), encoding="utf-8"
        )
    return path


def make_snapshot(root: Path) -> dict:
    return {
        "workspace": {"root": str(root)},
        "selected_checkouts": {
            "repo-b": {"path": str(root / "repo-b")},
            "repo-a": {"path": str(root / "repo-a")},
        },
        "bindings": [
            {
                "capability": "mncs.test-verify/1",
                "provider": "mncs-test",
                "availability": {"status": "available"},
            }
        ],
    }


class TestCommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mncs-testcmd-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        make_repo(self.root / "repo-a", with_inventory=False)
        make_repo(self.root / "repo-b", with_inventory=True)
        self.session = FakeSession(make_snapshot(self.root))

    def test_explicit_checkout_resolves(self):
        name, checkout = testcmd.resolve_checkout(self.session, "repo-a")
        self.assertEqual(name, "repo-a")
        self.assertEqual(Path(checkout), (self.root / "repo-a").resolve())

    def test_unknown_checkout_lists_candidates(self):
        with self.assertRaises(testcmd.TestRoutingError) as raised:
            testcmd.resolve_checkout(self.session, "repo-zz")
        self.assertEqual(raised.exception.diagnostics["code"], "test-checkout-unknown")
        self.assertEqual(
            raised.exception.diagnostics["candidates"], ["repo-a", "repo-b"]
        )

    def test_bare_run_refuses_with_inventory_candidates(self):
        with self.assertRaises(testcmd.TestRoutingError) as raised:
            testcmd.resolve_checkout(self.session, None)
        self.assertEqual(raised.exception.diagnostics["code"], "test-checkout-required")
        self.assertEqual(raised.exception.diagnostics["candidates"], ["repo-b"])

    def test_missing_provider_binding_explains(self):
        self.session.snapshot["bindings"] = []
        with self.assertRaises(testcmd.TestRoutingError) as raised:
            testcmd.provider_binding(self.session)
        self.assertEqual(raised.exception.diagnostics["code"], "test-provider-missing")

    def test_unavailable_provider_binding_explains(self):
        self.session.snapshot["bindings"][0]["availability"] = {
            "status": "unavailable",
            "reason": "toolchain-missing",
        }
        with self.assertRaises(testcmd.TestRoutingError) as raised:
            testcmd.provider_binding(self.session)
        self.assertEqual(raised.exception.diagnostics["code"], "test-provider-unavailable")

    def test_run_passes_checkout_argv_and_shapes_result(self):
        session = mock.Mock()
        session.snapshot = make_snapshot(self.root)
        session.invoke.return_value = {
            "binding_id": "cap_1",
            "status": "ok",
            "returncode": 0,
            "truncated": False,
            "stdout": "report-text",
            "stderr": "",
        }
        result = testcmd.run_tests(
            session, "repo-b", output_format="json", timeout_seconds=30,
            max_executions=4, no_store=True,
        )
        session.invoke.assert_called_once_with(
            "mncs.test-verify/1",
            ["--format", "json", "--max-executions", "4", "--no-store"],
            cwd=str((self.root / "repo-b").resolve()),
            timeout_seconds=30,
        )
        self.assertEqual(result["repository"], "repo-b")
        self.assertEqual(result["report"], "report-text")
        self.assertEqual(result["returncode"], 0)


if __name__ == "__main__":
    unittest.main()
