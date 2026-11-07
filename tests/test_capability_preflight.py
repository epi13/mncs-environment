"""Operation advice must expose requirements without executing or authorizing."""
from __future__ import annotations

import copy
import subprocess
from pathlib import Path
from unittest import mock

from mncs_env import capabilities, cli, sessions
from mncs_env.session_store import open_store
from test_doctor import DoctorFixture


class PreflightTests(DoctorFixture):
    def session(self):
        self.ready()
        _, context = self.enter()
        return sessions.Session(open_store(self.state, "file"), context["session_id"])

    def test_missing_and_narrow_claims_are_discovered_without_failed_execution(self):
        session = self.session()
        (self.project / "notes.txt").write_text("fixture draft requiring explicit ownership")
        session.revalidate()
        with mock.patch.object(capabilities, "invoke", side_effect=AssertionError("provider ran")):
            missing = session.capability_preflight("fixture.start/1")
        self.assertFalse(missing["authorizes_execution"])
        self.assertEqual(missing["effects"], ["write"])
        self.assertEqual(missing["effect_scope"]["checkout"], str(self.project))
        self.assertEqual(missing["status"], "requirements-unmet", missing)
        session.acquire_claim("fixture", basis="explicit-adoption",
                              scope={"kind": "paths", "paths": ["provider.py"],
                                               "checkout": str(self.project), "branch": "main"})
        narrow = session.capability_preflight("fixture.start/1")
        self.assertEqual(narrow["status"], "requirements-unmet", narrow)
        claims_before = copy.deepcopy(session.store.read_claims())
        session.capability_preflight("fixture.start/1")
        self.assertEqual(session.store.read_claims(), claims_before)
        session.acquire_claim("fixture", basis="explicit-adoption",
                              scope={"kind": "worktree", "checkout": str(self.project),
                                               "branch": "main"})
        ready = session.capability_preflight("fixture.start/1")
        self.assertEqual(ready["status"], "requirements-observed", ready)
        self.assertFalse(any(e["type"] == "demand.observed" for e in session._log()))
        self.assertFalse(any(e["type"] == "capability.invoked" for e in session._log()))
        self.assertEqual(session.invoke("fixture.start/1", [])["status"], "ok")

    def test_changed_authority_after_advice_still_refuses_execution(self):
        session = self.session()
        self.assertEqual(session.capability_preflight("fixture.status/1")["status"],
                         "requirements-observed")
        session.snapshot["authority"]["denied"] = ["invoke"]
        with mock.patch.object(capabilities, "invoke", side_effect=AssertionError("provider ran")):
            with self.assertRaises(sessions.AuthorityDenied):
                session.invoke("fixture.status/1", [])

    def test_provider_symlink_is_context_failure_without_claim_recovery(self):
        session = self.session()
        alias = self.base / "alias"
        alias.symlink_to(self.project, target_is_directory=True)
        for binding in session.snapshot["bindings"]:
            binding["provider_root"] = str(alias)
            binding.setdefault("provenance", {})["checkout"] = {"path": str(alias)}
        before = copy.deepcopy(session.store.read_claims())
        report = session.capability_preflight("fixture.start/1")
        self.assertEqual(report["status"], "context-unavailable", report)
        self.assertEqual(session.store.read_claims(), before)

    def test_cli_exposes_operation_advice_on_existing_capability_surface(self):
        args = cli.build_parser().parse_args([
            "capabilities", "ses_example", "--preflight", "fixture.start/1",
            "--effect-repository", "fixture"])
        self.assertEqual(args.preflight, "fixture.start/1")
        self.assertEqual(args.effect_repository, "fixture")

    def test_capability_inventory_does_not_read_unrelated_session_history(self):
        session = self.session()
        expected = copy.deepcopy(session.snapshot["bindings"])
        args = cli.build_parser().parse_args(["capabilities", session.session_id])
        with (mock.patch.object(cli, "_open", return_value=session),
              mock.patch.object(session.store, "read_events", side_effect=AssertionError("unrelated history read")),
              mock.patch.object(cli, "out") as output):
            self.assertEqual(cli.cmd_capabilities(args), 0)
        self.assertEqual(output.call_args.args[0], expected)

    def test_inventory_order_cannot_select_an_incidental_linked_checkout(self):
        session = self.session()
        linked = self.base / "linked"
        subprocess.run(["git", "-C", str(self.project), "worktree", "add", "-qb", "proposal", str(linked)],
                       check=True, capture_output=True)
        primary = {"name": "fixture", "manifest_repository": "fixture",
                   "path": str(self.project), "branch": "main"}
        alternative = {"name": "fixture@linked", "manifest_repository": "fixture",
                       "path": str(linked), "branch": "proposal"}
        session.snapshot["selected_checkouts"] = {}
        for rows in ([alternative, primary], [primary, alternative]):
            session.snapshot["workspace"]["repositories"] = rows
            self.assertEqual(session._effect_checkout("fixture", self.base)[0], self.project)
        session.snapshot["workspace"]["repositories"] = [
            alternative, {**primary, "name": "fixture@main"}]
        self.assertEqual(session._effect_checkout("fixture", self.base), (None, None))
        session.snapshot["selected_checkouts"] = {"fixture": {"path": str(linked), "branch": "proposal"}}
        self.assertEqual(session._effect_checkout("fixture", self.base)[0], linked)
