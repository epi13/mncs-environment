"""Observable fresh-process entry, readiness, discovery, and recovery contracts."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import pytest
from pathlib import Path
from unittest import mock

from mncs_env import capabilities, entry, readiness, sessions, workspace

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


class EntryContractTests(unittest.TestCase):
    def test_declared_invocation_is_not_presented_as_undeclared(self):
        bindings = capabilities.discover_capabilities(self.project)
        binding = next(item for item in bindings if item["capability"] == "fixture.status/1")
        self.assertEqual(binding["entrypoint"], binding["address"])
        self.assertEqual(binding["provenance"]["addressing"], "descriptor")

    def test_selected_service_arguments_do_not_depend_on_cwd(self):
        from types import SimpleNamespace
        context = SimpleNamespace(snapshot={"bindings": [{"provider": "fixture", "provider_root": str(self.project)}]})
        with mock.patch("pathlib.Path.cwd", return_value=Path("/tmp")):
            self.assertEqual(readiness.resolve_arguments(context, ["--config", {"repository": "fixture", "path": ".mncs/environment.json"}]),
                             ["--config", str(self.definition)])
        for reference in ({"repository": "missing", "path": "config"},
                          {"repository": "fixture", "path": "../config"},
                          {"repository": "fixture", "path": "/ambient/config"}):
            with self.assertRaises(ValueError):
                readiness.resolve_arguments(context, [reference])

    def test_selected_service_argument_rejects_symlink_escape(self):
        from types import SimpleNamespace
        (self.project / "escape").symlink_to(self.base)
        context = SimpleNamespace(snapshot={"bindings": [{"provider": "fixture", "provider_root": str(self.project)}]})
        with self.assertRaisesRegex(ValueError, "escapes"):
            readiness.resolve_arguments(context, [{"repository": "fixture", "path": "escape/config"}])

    def test_provider_diagnostics_preserve_selection_and_observation(self):
        self.provider.write_text('import json\nprint(json.dumps({"schema_version":"fixture.status/1","ready":False,"diagnostics":[{"code":"SELECTED_PROVIDER_MISSING","message":"build the selected provider"}],"selected":{"checkout":"selected"},"observed":{"checkout":"old"}}))\n')
        self.config["services"][0].pop("reconcile")
        self.definition.write_text(json.dumps(self.config))
        code, result = self.run_cli("enter", "--consumer", "diagnostic-test")
        self.assertEqual(code, 5)
        probe = result["readiness"]["services"][0]
        self.assertEqual(probe["provider_diagnostics"][0]["code"], "SELECTED_PROVIDER_MISSING")
        self.assertEqual(probe["provider_selected"]["checkout"], "selected")
        self.assertEqual(probe["provider_observed"]["checkout"], "old")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mncs-entry-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.project = self.base / "fixture"
        self.project.mkdir()
        self.state = self.base / "state with spaces"
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
                       "intent": {"goal": "exercise entry contracts", "repositories": []},
                       "required_capabilities": ["fixture.status/1"],
                       "services": [{"identity": "fixture-service", "required": True,
                                     "probe": {"capability": "fixture.status/1"},
                                     "reconcile": {"capability": "fixture.start/1"},
                                     "response_schema": "fixture.status/1", "ready_when": {"/ready": True}}]}
        self.definition.write_text(json.dumps(self.config))
        for argv in (["init", "-q", "-b", "main"], ["add", "."],
                     ["-c", "user.name=Entry Test", "-c", "user.email=entry@example.invalid", "commit", "-qm", "fixture"]):
            subprocess.run(["git", "-C", str(self.project), *argv], check=True, capture_output=True)

    def run_cli(self, *argv, cwd=None, backend="file"):
        result = subprocess.run([sys.executable, str(CLI), "--state-dir", str(self.state),
                                 "--persistence", backend, *argv], cwd=cwd or self.project,
                                capture_output=True, text=True, timeout=30)
        payload = json.loads(result.stdout or result.stderr)
        return result.returncode, payload

    def enter(self, *argv, **kwargs):
        return self.run_cli("enter", "--consumer", "agent", *argv, **kwargs)

    def ready(self):
        (self.project / "service-state.json").write_text(json.dumps({"ready": True, "starts": 0}))

    def test_clean_entry_reports_identity_project_config_and_probe(self):
        self.ready()
        code, context = self.enter()
        self.assertEqual(code, 0, context)
        self.assertEqual(context["readiness"]["status"], "ready")
        self.assertEqual(context["project_count"], 1)
        self.assertEqual(context["projects"][0]["path"], str(self.project))
        self.assertEqual(context["configuration"]["source"], str(self.definition))
        self.assertFalse(context["entry"]["reused"])
        self.assertEqual(context["entry"]["operations"], [])
        self.assertIn("service-ready", json.dumps(context["readiness"]))

    def test_reentry_reuses_checkpoint_and_terminal_entry_creates_new_work(self):
        self.ready()
        _, first = self.enter()
        session_id = first["session_id"]
        self.run_cli("checkpoint", session_id, "--progress", "durable progress", "--remaining", "continue")
        code, second = self.enter()
        self.assertEqual(code, 0, second)
        self.assertEqual(second["session_id"], session_id)
        self.assertTrue(second["entry"]["reused"])
        self.assertEqual(second["lifecycle"], "active")
        _, inspected = self.run_cli("inspect", session_id)
        self.assertEqual(len(inspected["checkpoints"]), 1)
        self.run_cli("complete", session_id, "--outcome", "done")
        _, third = self.enter()
        self.assertNotEqual(third["session_id"], session_id)

    def test_store_backed_entry_reuses_in_a_fresh_process(self):
        self.ready()
        code, first = self.enter(backend="store")
        self.assertEqual(code, 0, first)
        code, second = self.enter(backend="store")
        self.assertEqual(code, 0, second)
        self.assertEqual(first["session_id"], second["session_id"])
        self.assertTrue(second["entry"]["reused"])

    def test_subdirectory_entry_and_returned_actions_work_from_unrelated_cwd(self):
        self.ready()
        nested = self.project / "src" / "nested"
        nested.mkdir(parents=True)
        code, entered = self.enter(cwd=nested)
        self.assertEqual(code, 0, entered)
        for name in ("health", "inspect", "capabilities"):
            result = subprocess.run(entered["actions"][name]["argv"], cwd=self.base,
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            if name != "capabilities":
                self.assertEqual(payload["session_id"], entered["session_id"])

    def test_explicit_workspace_does_not_inherit_other_current_project_config(self):
        other = self.base / "other"
        other.mkdir()
        code, context = self.enter("--workspace", str(other))
        self.assertEqual(code, 0, context)
        self.assertEqual(context["configuration"]["source"], "orientation-default")
        self.assertEqual(context["workspace_root"], str(other))
        self.assertEqual(context["authority"]["writable"], [])
        self.assertEqual(context["readiness"]["status"], "degraded")

    def test_service_missing_preserves_session_and_enforces_start_authority(self):
        code, first = self.enter()
        self.assertEqual(code, 5, first)
        self.assertEqual(first["lifecycle"], "active")
        self.assertEqual(first["readiness"]["status"], "blocked")
        self.assertEqual(first["entry"]["operations"][0]["status"], "pending-escalation")
        self.assertFalse((self.project / "service-state.json").exists())
        claim_code, claim = self.run_cli("claims", first["session_id"], "--acquire", "fixture", "--reason", "start fixture service")
        self.assertEqual(claim_code, 0, claim)
        code, recovered = self.enter()
        self.assertEqual(code, 0, recovered)
        self.assertEqual(recovered["readiness"]["status"], "ready")
        self.assertEqual(recovered["session_id"], first["session_id"])
        self.enter()
        self.assertEqual(json.loads((self.project / "service-state.json").read_text())["starts"], 1)

    def test_health_is_live_read_only_and_schema_mismatch_is_actionable(self):
        self.ready()
        _, first = self.enter()
        directory = self.state / "sessions" / first["session_id"]
        before = {path.name: path.read_bytes() for path in directory.glob("*.json*")}
        (self.project / "service-state.json").write_text(json.dumps({"schema": "wrong/9", "ready": True, "starts": 0}))
        code, health = self.run_cli("health", first["session_id"])
        self.assertEqual(code, 5, health)
        self.assertEqual(health["readiness"]["observation"], "live")
        self.assertEqual(health["readiness"]["services"][0]["code"], "service-response-incompatible")
        self.assertEqual(before, {path.name: path.read_bytes() for path in directory.glob("*.json*")})
        _, status = self.run_cli("status", first["session_id"])
        self.assertEqual(status["readiness"]["status"], "ready", "status is explicitly a historical snapshot")

    def test_removed_and_restored_provider_bindings_refresh_on_reentry(self):
        self.ready()
        _, first = self.enter()
        self.provider.unlink()
        code, second = self.enter()
        self.assertEqual(code, 5, second)
        self.assertEqual(second["session_id"], first["session_id"])
        self.assertIn("fixture.status/1", second["readiness"]["required_unavailable"])
        self.provider.write_text(PROVIDER)
        code, third = self.enter()
        self.assertEqual(code, 0, third)
        self.assertIn("fixture.status/1", third["entry"]["revalidation"]["changed"])

    def test_optional_service_failure_is_degraded_and_probe_does_not_start_it(self):
        self.config["services"][0].update(required=False)
        self.config["services"][0].pop("reconcile")
        self.definition.write_text(json.dumps(self.config))
        code, context = self.enter()
        self.assertEqual(code, 0, context)
        self.assertEqual(context["readiness"]["status"], "degraded")
        self.assertEqual(context["entry"]["operations"], [])

    def test_invalid_definition_does_not_create_state_and_errors_are_json(self):
        self.config["services"][0]["ready_when"] = {}
        self.definition.write_text(json.dumps(self.config))
        code, error = self.enter()
        self.assertEqual(code, 2)
        self.assertIn("ready_when", error["error"])
        self.assertFalse(self.state.exists())
        code, error = self.enter("--definition", str(self.base / "absent.json"))
        self.assertEqual(code, 2)
        self.assertEqual(error["diagnostics"]["code"], "definition-invalid")

    def test_entry_lock_prevents_duplicate_work_across_processes(self):
        self.ready()
        with entry.entry_lock(self.state, "file"):
            code, error = self.enter()
        self.assertEqual(code, 2)
        self.assertEqual(error["diagnostics"]["code"], "entry-busy")
        _, first = self.enter()
        _, second = self.enter()
        self.assertEqual(first["session_id"], second["session_id"])

    def test_new_session_is_explicit_and_ambiguous_reuse_does_not_guess(self):
        self.ready()
        _, first = self.enter()
        _, second = self.enter("--new-session")
        self.assertNotEqual(first["session_id"], second["session_id"])
        code, error = self.enter()
        self.assertEqual(code, 2)
        self.assertEqual(error["diagnostics"]["code"], "entry-session-ambiguous")

    def test_health_detects_deleted_workspace_and_revalidation_keeps_snapshot(self):
        self.ready()
        _, context = self.enter()
        moved = self.base / "moved"
        self.project.rename(moved)
        code, health = self.run_cli("health", context["session_id"], cwd=self.base)
        self.assertEqual(code, 5, health)
        self.assertIn("workspace-scan-incomplete", health["readiness"]["blocking"])
        code, failure = self.run_cli("reconcile", context["session_id"], cwd=self.base)
        self.assertEqual(code, 2)
        self.assertEqual(failure["diagnostics"]["code"], "workspace-scan-incomplete")
        _, inspected = self.run_cli("inspect", context["session_id"], cwd=self.base)
        self.assertEqual(inspected["workspace"]["repository_count"], 1)

    def test_incomplete_workspace_observation_does_not_erase_previous_facts(self):
        self.ready()
        _, context = self.enter()
        session = sessions.Session.open(state_dir=self.state, session_id=context["session_id"], backend="file")
        before = dict(session.snapshot["workspace"])
        with mock.patch.object(workspace, "discover_workspace", return_value={"scan": {"status": "timed_out"}}):
            with self.assertRaises(workspace.WorkspaceResolutionError):
                session.revalidate()
        self.assertEqual(session.snapshot["workspace"], before)

    def test_executable_presence_checks_permissions_and_bound_toolchain(self):
        binary = self.base / "binary"
        binary.write_text("#!/bin/sh\nexit 0\n")
        binding = capabilities.bind(provider="fixture", capability="fixture", contract_revision="1",
                                    entrypoint="binary", address=str(binary))
        self.assertEqual(capabilities.probe_availability(binding)["availability"]["status"], "unavailable")
        binary.chmod(0o755)
        self.assertEqual(capabilities.probe_availability(binding)["availability"]["status"], "available")
        binding["toolchain_address"] = str(self.base / "missing")
        self.assertEqual(capabilities.probe_availability(binding)["availability"]["code"], "toolchain-missing")

    def test_selected_repositories_enter_a_broad_root_without_scanning_foreign_work(self):
        self.ready()
        for index in range(70):
            (self.base / f"unrelated-{index}").mkdir()
        self.config["workspace_root"] = "../.."
        self.config["workspace_scope"] = {"kind": "workspace", "repositories": ["fixture"]}
        self.definition.write_text(json.dumps(self.config))
        sentinel = self.base / "unrelated-1" / "foreign-work.txt"
        sentinel.write_text("protected work\n")
        code, first = self.enter()
        self.assertEqual(code, 0, first)
        self.assertEqual(first["workspace_root"], str(self.base))
        self.assertEqual(first["project_count"], 1)
        _, inspected = self.run_cli("inspect", first["session_id"])
        self.assertEqual(inspected["workspace"]["scan"]["candidate_directories"], 1)
        self.assertEqual(set(inspected["selected_checkouts"]), {"fixture"})
        code, second = self.enter()
        self.assertEqual(code, 0, second)
        self.assertEqual(second["session_id"], first["session_id"])
        self.assertEqual(sentinel.read_text(), "protected work\n")

    def test_invalid_repository_selection_fails_before_state_is_created(self):
        for selection in (["../fixture"], ["fixture", "fixture"], ["missing"]):
            self.config.update(workspace_root="../..", workspace_scope={"kind": "workspace", "repositories": selection})
            self.definition.write_text(json.dumps(self.config))
            code, error = self.enter()
            self.assertEqual(code, 2, error)
            self.assertTrue(error["diagnostics"]["code"].startswith("workspace-"))
            self.assertFalse(self.state.exists())

    def test_fingerprint_evidence_is_discoverable_but_never_a_guessed_invocation(self):
        (self.project / ".mncs" / "project.json").write_text(json.dumps({
            "repository": "fixture", "contracts": {"provides": [
                {"contract": "evidence", "fingerprint_sources": ["provider.py"]}]}}))
        bindings = capabilities.discover_capabilities(self.project)
        binding = next(item for item in bindings if item["capability"] == "fixture:evidence")
        self.assertIsNone(binding["address"])
        self.assertEqual(capabilities.probe_availability(binding)["availability"]["status"], "unavailable")

    def test_selected_toolchain_loss_does_not_fall_back_to_fingerprint_or_path(self):
        (self.project / ".mncs" / "project.json").write_text(json.dumps({
            "repository": "fixture", "contracts": {"provides": [
                {"contract": "exact", "fingerprint_sources": ["provider.py"],
                 "invocation": {"kind": "python", "path": "provider.py",
                                "toolchain": {"repository": "missing", "path": "tool"}}}]}}))
        binding = next(item for item in capabilities.discover_capabilities(self.project)
                       if item["capability"] == "fixture:exact")
        self.assertIsNone(binding["address"])

    def test_schema_mismatch_does_not_restart_an_existing_service(self):
        self.ready()
        _, first = self.enter()
        self.run_cli("claims", first["session_id"], "--acquire", "fixture", "--reason", "fixture recovery")
        state = self.project / "service-state.json"
        state.write_text(json.dumps({"ready": True, "starts": 0, "schema": "incompatible/9"}))
        code, context = self.enter()
        self.assertEqual(code, 5, context)
        self.assertEqual(context["entry"]["operations"][0]["status"], "not-attempted")
        self.assertEqual(json.loads(state.read_text())["starts"], 0)

    def test_reconciler_sessions_are_scoped_to_their_workspace(self):
        from mncs_env import reconciler
        from mncs_env.session_store import open_store
        store = open_store(self.state, "file")
        self.addCleanup(lambda: getattr(store, "close", lambda: None)())
        first, created = reconciler.open_or_create_session(self.state, store, self.project)
        self.assertTrue(created)
        other = self.base / "other"
        other.mkdir()
        second, created = reconciler.open_or_create_session(self.state, store, other)
        self.assertTrue(created)
        self.assertNotEqual(first.session_id, second.session_id)
        self.assertEqual(reconciler.find_session(self.state, store, self.project).session_id, first.session_id)

    def test_entry_upgrade_preserves_store_routing_and_requires_matching_checkout(self):
        from mncs_env import session_store
        session_id = "ses_existing"
        selected = {"provider": "mncs-store", "workspace_root": str(self.base),
                    "checkout": str(self.base / "mncs-store"), "python_package": str(self.base / "mncs-store" / "python"),
                    "revision": "original", "runtime_environment": {"MNCS_BIN": "selected-runtime"}}
        prior = {key: value for key, value in selected.items() if key != "runtime_environment"}
        prior.update(schema_version="mncs.environment.session-store-provider/1", session_id=session_id)
        path = self.state / "sessions" / session_id / "store-provider.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(prior))
        snapshot = {"session_id": session_id}
        with mock.patch.object(session_store, "store_provider_from_environment", return_value=selected):
            self.assertTrue(session_store.upgrade_session_store_provider(self.state, snapshot))
            self.assertFalse(session_store.upgrade_session_store_provider(self.state, snapshot))
        upgraded = json.loads(path.read_text())
        self.assertEqual(upgraded["runtime_environment"], selected["runtime_environment"])
        self.assertEqual(upgraded["revision"], "original")
        prior["checkout"] = str(self.base / "foreign-store")
        path.write_text(json.dumps(prior))
        with mock.patch.object(session_store, "store_provider_from_environment", return_value=selected):
            with self.assertRaisesRegex(ValueError, "does not match"):
                session_store.upgrade_session_store_provider(self.state, snapshot)
        self.assertEqual(json.loads(path.read_text()), prior)


if __name__ == "__main__":
    unittest.main()


def test_unwritable_entry_state_reports_explicit_recovery(tmp_path, monkeypatch):
    import errno
    from mncs_env.entry import EntryError, entry_lock
    def denied(*args, **kwargs):
        raise OSError(errno.EROFS, "read-only filesystem")
    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(EntryError) as raised:
        with entry_lock(tmp_path, "store"):
            pass
    assert raised.value.diagnostics["code"] == "entry-state-unwritable"
    assert "--state-dir" in raised.value.diagnostics["next"]


def test_selected_store_runtime_suppresses_ambient_artifact_and_restores_process(monkeypatch):
    import os
    from mncs_env.store_backend import _selected_store_runtime
    monkeypatch.setenv("MNCS_STORE_ARTIFACT", "/ambient/unselected-artifact")
    runtime = {"MNCS_STORE_ROOT": "/selected/store", "MNCS_LANGUAGE_ROOT": "/selected/language",
               "MNCS_BIN": "/selected/language/mncs", "MNCS_EMBED_LIB": "/selected/language/embed"}
    with _selected_store_runtime(runtime):
        assert "MNCS_STORE_ARTIFACT" not in os.environ
        assert os.environ["MNCS_BIN"] == runtime["MNCS_BIN"]
    assert os.environ["MNCS_STORE_ARTIFACT"] == "/ambient/unselected-artifact"


def test_selected_store_uses_explicit_writable_derived_cache(tmp_path, monkeypatch):
    import os
    from mncs_env.store_backend import _selected_store_runtime
    monkeypatch.setenv("MNCS_STORE_ARTIFACT_CACHE", "/unwritable/ambient-cache")
    runtime = {"MNCS_STORE_ROOT": "/selected/store", "MNCS_LANGUAGE_ROOT": "/selected/language",
               "MNCS_BIN": "/selected/language/mncs", "MNCS_EMBED_LIB": "/selected/language/embed"}
    with _selected_store_runtime(runtime, tmp_path / "provider-cache" / "mncs-store"):
        assert os.environ["MNCS_STORE_ARTIFACT_CACHE"] == str(tmp_path / "provider-cache" / "mncs-store")
    assert os.environ["MNCS_STORE_ARTIFACT_CACHE"] == "/unwritable/ambient-cache"


def test_snapshot_collision_is_an_environment_conflict(monkeypatch):
    from mncs_env.session_store import SnapshotConflict
    from mncs_env.store_backend import StoreBackend
    from types import SimpleNamespace

    class Error(Exception):
        code = 7

    backend = StoreBackend.__new__(StoreBackend)
    backend._api = (None, Error, SimpleNamespace(IDENTITY_CONFLICT=7))
    def collision(**kwargs):
        raise Error("immutable identity already used")
    monkeypatch.setattr(backend, "_put_immutable", collision)
    with pytest.raises(SnapshotConflict) as raised:
        backend.put_snapshot("session", 4, {"snapshot_sequence": 4})
    assert raised.value.session_id == "session"
    assert raised.value.revision == 4


def test_cli_snapshot_conflict_reports_possible_completed_effects(monkeypatch, capsys):
    from mncs_env import cli
    from mncs_env.session_store import SnapshotConflict
    from types import SimpleNamespace
    def collision(args):
        raise SnapshotConflict("session", 4)
    args = SimpleNamespace(state_dir=Path("/tmp/state"), command="invoke", func=collision)
    monkeypatch.setattr(cli, "build_parser", lambda: SimpleNamespace(parse_args=lambda argv: args))
    assert cli.main([]) == 2
    result = json.loads(capsys.readouterr().err)
    assert result["diagnostics"]["code"] == "session-snapshot-conflict"
    assert result["diagnostics"]["capability_may_have_run"] is True
    assert result["diagnostics"]["snapshot_saved"] is False
