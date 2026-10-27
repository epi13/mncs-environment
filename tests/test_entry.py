"""Observable fresh-process entry, readiness, discovery, and recovery contracts."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pytest

from mncs_env import (
    campaign_selector,
    capabilities,
    cli,
    entry,
    identity,
    readiness,
    sessions,
    workspace,
)

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


def test_development_definition_tracks_the_current_linked_worktree():
    definition_path = ROOT / ".mncs" / "environment.json"
    definition, _ = entry.select_definition(definition_path, None)

    assert definition.get("working_checkout") == "current"
    checkout = entry.current_working_checkout(definition, definition_path)

    assert checkout is not None
    assert checkout["path"] == str(ROOT.resolve())


def test_capability_observation_definition_has_bounded_scope_and_no_services():
    definition = json.loads(
        (ROOT / ".mncs" / "environment-observation.json").read_text()
    )

    assert definition["working_checkout"] == "current"
    assert definition["workspace_scope"]["repositories"] == [
        "mncs-environment", "mncs-store", "mncs-rights-provenance",
        "mncs-system-monitor",
    ]
    assert definition["services"] == []
    assert definition["intent"]["repositories"] == ["mncs-environment"]

class EntryContractTests(unittest.TestCase):
    def test_project_definition_symlink_is_rejected(self):
        alias = self.project / ".mncs" / "environment-alias.json"
        alias.symlink_to(self.definition.name)
        with self.assertRaisesRegex(entry.EntryError, "symbolic link"):
            entry.select_definition(alias, None)

    def test_declared_invocation_is_not_presented_as_undeclared(self):
        bindings = capabilities.discover_capabilities(self.project)
        binding = next(item for item in bindings if item["capability"] == "fixture.status/1")
        self.assertEqual(binding["entrypoint"], binding["address"])
        self.assertEqual(binding["provenance"]["addressing"], "descriptor")

    def test_selected_service_arguments_do_not_depend_on_cwd(self):
        from types import SimpleNamespace
        second = self.base / "second-fixture"
        second.mkdir()
        context = SimpleNamespace(snapshot={
            "workspace": {"root": str(self.base)},
            "selected_checkouts": {
                "z-fixture": {"path": str(self.project)},
                "a-fixture": {"path": str(second)},
            },
            "bindings": [{"provider": "fixture", "provider_root": str(self.project)}],
        })
        with mock.patch("pathlib.Path.cwd", return_value=Path("/tmp")):
            self.assertEqual(readiness.resolve_arguments(context, ["--config", {"repository": "fixture", "path": ".mncs/environment.json"}]),
                             ["--config", str(self.definition)])
            self.assertEqual(readiness.resolve_arguments(context, ["--workspace", {"workspace_root": True}]),
                             ["--workspace", str(self.base.resolve())])
            roots = json.loads(readiness.resolve_arguments(
                context, [{"selected_repository_roots_json": True}])[0])
            self.assertEqual(roots, [str(second.resolve()), str(self.project.resolve())])
        for reference in ({"repository": "missing", "path": "config"},
                          {"repository": "fixture", "path": "../config"},
                          {"repository": "fixture", "path": "/ambient/config"}):
            with self.assertRaises(ValueError):
                readiness.resolve_arguments(context, [reference])

    def test_environment_root_service_argument_requires_selected_root(self):
        from types import SimpleNamespace
        context = SimpleNamespace(snapshot={"bindings": []})
        with self.assertRaisesRegex(ValueError, "workspace root is absent"):
            readiness.resolve_arguments(context, [{"workspace_root": True}])

    def test_selected_repository_roots_require_exact_contained_checkouts(self):
        from types import SimpleNamespace
        context = SimpleNamespace(snapshot={
            "workspace": {"root": str(self.base)},
            "selected_checkouts": {"fixture": {"path": str(self.project)}},
        })
        self.assertEqual(json.loads(readiness.resolve_arguments(
            context, [{"selected_repository_roots_json": True}])[0]),
            [str(self.project.resolve())])
        context.snapshot["selected_checkouts"]["escape"] = {"path": str(self.base.parent)}
        with self.assertRaisesRegex(ValueError, "escapes"):
            readiness.resolve_arguments(context, [{"selected_repository_roots_json": True}])

    def test_selected_service_argument_rejects_symlink_escape(self):
        from types import SimpleNamespace
        (self.project / "escape").symlink_to(self.base)
        context = SimpleNamespace(snapshot={"bindings": [{"provider": "fixture", "provider_root": str(self.project)}]})
        with self.assertRaisesRegex(ValueError, "escapes"):
            readiness.resolve_arguments(context, [{"repository": "fixture", "path": "escape/config"}])

    def test_service_timeouts_are_provider_declared_and_bounded(self):
        service = {"identity": "fixture", "probe": {"capability": "status"},
                   "reconcile": {"capability": "start"}, "response_schema": "fixture/1",
                   "ready_when": {"/ready": True}, "probe_timeout_seconds": 41,
                   "reconcile_timeout_seconds": 901}
        validated = readiness.validate_requirements({"services": [service]})
        self.assertEqual(validated["services"][0]["probe_timeout_seconds"], 41)
        self.assertEqual(validated["services"][0]["reconcile_timeout_seconds"], 901)
        for field, value in (("probe_timeout_seconds", 121), ("reconcile_timeout_seconds", 3601)):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                readiness.validate_requirements({"services": [{**service, field: value}]})

    def test_service_reconcile_effect_target_names_a_selected_repository(self):
        service = {
            "identity": "forge-resident",
            "probe": {"capability": "forge-status"},
            "reconcile": {
                "capability": "forge-reconcile",
                "effect_target": {"repository": "mncs-reference-studies"},
            },
            "response_schema": "forge.status/1",
            "ready_when": {"/state": "ready"},
        }
        validated = readiness.validate_requirements({"services": [service]})
        self.assertEqual(
            validated["services"][0]["reconcile"]["effect_target"],
            {"repository": "mncs-reference-studies"},
        )
        for target in (
            {"repository": ""},
            {"repository": "selected", "checkout": "/ambient/path"},
        ):
            with self.subTest(target=target), self.assertRaisesRegex(
                ValueError, "effect_target"
            ):
                readiness.validate_requirements({"services": [
                    {**service, "reconcile": {
                        "capability": "forge-reconcile", "effect_target": target
                    }}
                ]})
        with self.assertRaisesRegex(ValueError, "effect_target"):
            readiness.validate_requirements({"services": [
                {**service, "probe": {
                    "capability": "forge-status",
                    "effect_target": {"repository": "selected"},
                }}
            ]})

    def test_service_environment_reference_requires_an_earlier_selected_service(self):
        language = {
            "identity": "language-service",
            "probe": {"capability": "language-status"},
            "response_schema": "language/1",
            "ready_when": {"/ready": True},
        }
        forge = {
            "identity": "forge-resident",
            "probe": {"capability": "forge-status"},
            "response_schema": "forge/1",
            "ready_when": {"/state": "ready"},
            "environment_from_service": {
                "MNLS_SERVICE_SOCKET": {
                    "service": "language-service",
                    "pointer": "/provider_observed/event_transport/socket",
                },
            },
        }
        checked = readiness.validate_requirements({"services": [language, forge]})
        self.assertEqual(
            checked["services"][1]["environment_from_service"],
            forge["environment_from_service"],
        )
        with self.assertRaisesRegex(ValueError, "earlier selected service"):
            readiness.validate_requirements({"services": [forge, language]})

    def test_invoke_routes_mutation_authority_to_selected_effect_repository(self):
        from types import SimpleNamespace

        observed = {}

        class FakeSession:
            def invoke(self, capability, argv, **kwargs):
                observed.update(capability=capability, argv=argv, **kwargs)
                return {
                    "status": "ok", "returncode": 0, "stdout": "{}",
                    "stderr": "", "truncated": False,
                }

            def close(self):
                return None

        args = SimpleNamespace(
            state_dir=self.state,
            persistence="file",
            session="session-id",
            capability="mncs-forge:canonical-vm-work",
            argv=["--workspace", "/selected/workspace"],
            cwd=None,
            timeout=10,
            output_limit_bytes=4096,
            effect_repository="mncs-reference-studies",
        )
        with mock.patch.object(
            cli.sessions_module.Session, "resume", return_value=FakeSession()
        ):
            self.assertEqual(cli.cmd_invoke(args), 0)

        self.assertEqual(observed["effect_target"], {
            "repository": "mncs-reference-studies",
        })
        self.assertEqual(observed["capability"], "mncs-forge:canonical-vm-work")

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
        (self.project / ".mncs" / "project.json").write_text(
            json.dumps({"schema_version": "mncs.project/1", "repository": "fixture"})
            + "\n"
        )
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

    def run_cli(self, *argv, cwd=None, backend="file", env=None):
        # These entry cases own a temporary workspace and state directory.
        # Keep Store-backed cases on that test-owned Store instead of inheriting
        # a protected Control grant that is scoped to the real project checkout.
        process_env = dict(os.environ if env is None else env)
        for key in (
            "MNCS_ENV_PERSISTENCE_TRANSPORT",
            "MNCS_ENV_RPC_SOCKET",
            "MNCS_ENV_RPC_GRANT_FILE",
        ):
            process_env.pop(key, None)
        result = subprocess.run([sys.executable, str(CLI), "--state-dir", str(self.state),
                                 "--persistence", backend, *argv], cwd=cwd or self.project,
                                capture_output=True, text=True, timeout=30, env=process_env)
        payload = json.loads(result.stdout or result.stderr)
        return result.returncode, payload

    def enter(self, *argv, **kwargs):
        return self.run_cli("enter", "--consumer", "agent", *argv, **kwargs)

    def ready(self):
        (self.project / "service-state.json").write_text(json.dumps({"ready": True, "starts": 0}))

    def authorize_fixture_writes(self):
        self.config["intent"]["repositories"] = ["fixture"]
        self.definition.write_text(json.dumps(self.config))
        subprocess.run(["git", "-C", str(self.project), "add", ".mncs/environment.json"],
                       check=True, capture_output=True)
        subprocess.run(["git", "-C", str(self.project), "-c", "user.name=Entry Test",
                        "-c", "user.email=entry@example.invalid", "commit", "-m",
                        "authorize fixture writes"], check=True, capture_output=True)

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
        ambient = context["entry"]["ambient_service_execution"]
        self.assertEqual(
            ambient["scope"], "incremental-owner-pass-scheduler-trace",
        )
        self.assertEqual(ambient["scheduled_pass_count"], 7)
        self.assertEqual(ambient["owner_pass_decision_coverage"], "complete")
        self.assertEqual(ambient["ambient_fulfillment"], "not-established")
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

    def test_authenticated_task_selector_binds_once_and_resumes_exact_campaign(self):
        self.ready()
        principal = "control-principal-task-selector"
        task_identity = "durable-goal-thread-20261012"
        auth_env = {
            **os.environ,
            "MNCS_ENV_AUTH_PRINCIPAL_ID": principal,
            "MNCS_ENV_RPC_REQUEST_ID": "req_task_selector_first_entry",
        }
        code, first = self.run_cli(
            "enter", "--consumer", "task-selector-process-1",
            backend="store", env=auth_env,
        )
        self.assertEqual(code, 0, first)
        campaign_id = first["continuation"]["identity"]

        # A fresh process can continue one uniquely identified campaign
        # without asking the operator to copy its campaign ID.
        auth_env["MNCS_ENV_RPC_REQUEST_ID"] = "req_task_selector_unbound"
        code, bound = self.run_cli(
            "enter", "--consumer", "task-selector-process-2",
            "--task-identity", task_identity,
            backend="store", env=auth_env,
        )
        self.assertEqual(code, 0, bound)
        self.assertEqual(bound["session_id"], first["session_id"])
        self.assertEqual(bound["entry"]["task_selector"]["status"], "bound")

        # A second live campaign in the same exact Environment context must
        # not make a bound task selector ambiguous.
        auth_env["MNCS_ENV_RPC_REQUEST_ID"] = "req_task_selector_other_campaign"
        code, other = self.run_cli(
            "enter", "--consumer", "task-selector-process-3",
            "--campaign-id", "cmp_independent_task_selector_fixture",
            "--new-session", backend="store", env=auth_env,
        )
        self.assertEqual(code, 0, other)
        self.assertNotEqual(other["session_id"], first["session_id"])

        auth_env["MNCS_ENV_RPC_REQUEST_ID"] = "req_task_selector_fresh_process"
        code, resumed = self.run_cli(
            "enter", "--consumer", "task-selector-process-4",
            "--task-identity", task_identity,
            backend="store", env=auth_env,
        )
        self.assertEqual(code, 0, resumed)
        self.assertEqual(resumed["session_id"], first["session_id"])
        self.assertTrue(resumed["entry"]["reused"])
        self.assertEqual(resumed["entry"]["task_selector"]["status"], "resumed")
        self.assertEqual(resumed["continuation"]["identity"], campaign_id)

        # Store retains only the scoped selector digest; it does not retain
        # the external task identity itself.
        selector = campaign_selector.selector_identity(
            task_identity,
            authenticated_principal_id=principal,
            definition_id=identity.environment_id(self.config),
            workspace_root=str(self.project.resolve()),
        )
        store = sessions.open_store(
            self.state, "store", verify_on_open=False, defer_mutation=False
        )
        try:
            record = campaign_selector.read_binding(store, selector)
            self.assertEqual(record["campaign_id"], campaign_id)
            self.assertEqual(record["session_id"], first["session_id"])
            self.assertNotIn("task_identity", record)
        finally:
            store.close()

        auth_env["MNCS_ENV_RPC_REQUEST_ID"] = "req_task_selector_conflicting_campaign"
        code, conflict = self.run_cli(
            "enter", "--consumer", "task-selector-process-5",
            "--campaign-id", "cmp_wrong_campaign_for_selector",
            "--task-identity", task_identity,
            backend="store", env=auth_env,
        )
        self.assertNotEqual(code, 0)
        self.assertEqual(
            conflict["diagnostics"]["code"], "campaign-task-selector-conflict"
        )

    def test_unbound_task_selector_refuses_duplicate_exact_checkout_continuations(self):
        self.ready()
        principal = "control-principal-task-selector-duplicate"
        task_identity = "durable-task-with-duplicate-candidates"
        auth_env = {
            **os.environ,
            "MNCS_ENV_AUTH_PRINCIPAL_ID": principal,
            "MNCS_ENV_RPC_REQUEST_ID": "req_task_duplicate_first",
        }
        code, first = self.run_cli(
            "enter", "--consumer", "duplicate-process-1",
            "--campaign-id", "cmp_duplicate_first",
            backend="store", env=auth_env,
        )
        self.assertEqual(code, 0, first)
        auth_env["MNCS_ENV_RPC_REQUEST_ID"] = "req_task_duplicate_second"
        code, second = self.run_cli(
            "enter", "--consumer", "duplicate-process-2",
            "--campaign-id", "cmp_duplicate_second", "--new-session",
            backend="store", env=auth_env,
        )
        self.assertEqual(code, 0, second)

        auth_env["MNCS_ENV_RPC_REQUEST_ID"] = "req_task_duplicate_lookup"
        code, ambiguous = self.run_cli(
            "enter", "--consumer", "duplicate-process-3",
            "--task-identity", task_identity,
            backend="store", env=auth_env,
        )
        self.assertNotEqual(code, 0)
        diagnostics = ambiguous["diagnostics"]
        self.assertEqual(diagnostics["code"], "campaign-continuation-ambiguous")
        self.assertEqual(
            diagnostics["campaigns"],
            ["cmp_duplicate_first", "cmp_duplicate_second"],
        )
        self.assertEqual(
            set(diagnostics["sessions"]), {first["session_id"], second["session_id"]}
        )
        self.assertEqual(
            {item["session_id"] for item in diagnostics["conflicting_contexts"]},
            {first["session_id"], second["session_id"]},
        )

        selector = campaign_selector.selector_identity(
            task_identity,
            authenticated_principal_id=principal,
            definition_id=identity.environment_id(self.config),
            workspace_root=str(self.project.resolve()),
        )
        store = sessions.open_store(
            self.state, "store", verify_on_open=False, defer_mutation=False
        )
        try:
            self.assertIsNone(campaign_selector.read_binding(store, selector))
        finally:
            store.close()

    def test_task_selector_recovers_session_created_before_binding_publication(self):
        self.ready()
        principal = "control-principal-task-selector-recovery"
        task_identity = "durable-task-after-create-interruption"
        selector = campaign_selector.selector_identity(
            task_identity,
            authenticated_principal_id=principal,
            definition_id=identity.environment_id(self.config),
            workspace_root=str(self.project.resolve()),
        )
        deterministic_campaign = "cmp_task_" + selector[5:]
        auth_env = {
            **os.environ,
            "MNCS_ENV_AUTH_PRINCIPAL_ID": principal,
            "MNCS_ENV_RPC_REQUEST_ID": "req_task_selector_interrupted_create",
        }
        # Simulate a caller stopping after the session object committed but
        # before the task-binding evidence publication.
        code, first = self.run_cli(
            "enter", "--consumer", "selector-recovery-process-1",
            "--campaign-id", deterministic_campaign,
            backend="store", env=auth_env,
        )
        self.assertEqual(code, 0, first)

        auth_env["MNCS_ENV_RPC_REQUEST_ID"] = "req_task_selector_interrupted_create_retry"
        code, resumed = self.run_cli(
            "enter", "--consumer", "selector-recovery-process-2",
            "--task-identity", task_identity,
            backend="store", env=auth_env,
        )
        self.assertEqual(code, 0, resumed)
        self.assertEqual(resumed["session_id"], first["session_id"])
        self.assertEqual(resumed["entry"]["task_selector"]["status"], "bound")

    def test_store_entry_discovers_and_accepts_recipient_handoff_in_fresh_process(self):
        self.ready()
        sender_env = {
            **os.environ,
            "MNCS_ENV_AUTH_PRINCIPAL_ID": "ctrl_handoff_source",
            "MNCS_ENV_RPC_REQUEST_ID": "req_handoff_source_entry",
        }
        code, sender = self.run_cli(
            "enter", "--consumer", "agent", backend="store", env=sender_env
        )
        self.assertEqual(code, 0, sender)

        handoff_env = {
            **os.environ,
            "MNCS_ENV_AUTH_PRINCIPAL_ID": "ctrl_handoff_source",
            "MNCS_ENV_RPC_REQUEST_ID": "req_handoff_create",
        }
        code, handoff = self.run_cli(
            "handoff", sender["session_id"], "--consumer", "agent",
            "--to", "agent", "--to-principal", "ctrl_handoff_receiver",
            backend="store", env=handoff_env,
        )
        self.assertEqual(code, 0, handoff)

        receiver_env = {
            **os.environ,
            "MNCS_ENV_AUTH_PRINCIPAL_ID": "ctrl_handoff_receiver",
            "MNCS_ENV_RPC_REQUEST_ID": "req_handoff_receiver_entry",
        }
        code, receiver = self.run_cli(
            "enter", "--consumer", "agent", backend="store", env=receiver_env
        )
        self.assertEqual(code, 0, receiver)
        self.assertEqual(receiver["session_id"], sender["session_id"])
        self.assertTrue(receiver["entry"]["reused"])
        self.assertEqual(receiver["consumer_id"], "agent")
        self.assertEqual(receiver["lifecycle"], "active")
        self.assertEqual(
            receiver["continuation"]["identity"],
            sender["continuation"]["identity"],
        )

        code, checkpoint = self.run_cli(
            "checkpoint", receiver["session_id"], "--progress", "continued after owner handoff",
            backend="store", env=receiver_env,
        )
        self.assertEqual(code, 0, checkpoint)
        inspect_env = {**receiver_env, "MNCS_ENV_RPC_REQUEST_ID": "req_handoff_inspect"}
        code, inspected = self.run_cli(
            "inspect", receiver["session_id"], "--consumer", "agent",
            backend="store", env=inspect_env,
        )
        self.assertEqual(code, 0, inspected)
        self.assertIn(checkpoint["identity"], inspected["checkpoints"])

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
        operation = first["entry"]["operations"][0]
        self.assertEqual(operation["status"], "not-attempted")
        self.assertEqual(operation["code"], "environment-invocation-not-admitted")
        self.assertFalse((self.project / "service-state.json").exists())
        claim_code, claim = self.run_cli("claims", first["session_id"], "--acquire", "fixture", "--reason", "start fixture service")
        self.assertEqual(claim_code, 3, claim)
        self.assertEqual(claim["diagnostics"]["code"], "authority-denied")
        self.assertFalse(claim["diagnostics"]["publication_completed"])
        code, recovered = self.enter()
        self.assertEqual(code, 5, recovered)
        self.assertEqual(recovered["readiness"]["status"], "blocked")
        self.assertEqual(recovered["session_id"], first["session_id"])
        self.assertEqual(recovered["entry"]["operations"][0]["status"], "not-attempted")
        self.assertFalse((self.project / "service-state.json").exists())

    def test_authorized_entry_reconciles_missing_service_once(self):
        self.authorize_fixture_writes()
        code, first = self.enter()
        self.assertEqual(code, 0, first)
        self.assertEqual(first["readiness"]["status"], "ready")
        self.assertEqual(first["entry"]["operations"][0]["status"], "ok")
        self.assertEqual(first["authority"]["writable"], ["fixture"])
        self.assertEqual(json.loads((self.project / "service-state.json").read_text())["starts"], 1)
        code, second = self.enter()
        self.assertEqual(code, 0, second)
        self.assertEqual(second["session_id"], first["session_id"])
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

    def test_local_definition_selects_its_current_linked_worktree(self):
        project_manifest = self.project / ".mncs" / "project.json"
        project_manifest.write_text(json.dumps({"repository": "fixture"}))
        self.config = {
            "name": "fixture-worktree-development",
            "working_checkout": "current",
            "workspace_scope": {
                "kind": "workspace", "repositories": ["fixture"],
            },
            "intent": {
                "goal": "enter the checkout containing this local definition",
                "repositories": ["fixture"],
            },
        }
        self.definition.write_text(json.dumps(self.config))
        subprocess.run(
            ["git", "-C", str(self.project), "add", ".mncs/project.json",
             ".mncs/environment.json"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.project), "commit", "-m",
             "add local worktree development definition"],
            check=True, capture_output=True,
        )
        checkout = self.project / ".worktrees" / "nested" / "agent-local"
        checkout.parent.mkdir(parents=True)
        subprocess.run(
            ["git", "-C", str(self.project), "worktree", "add", "-b",
             "campaign/agent-local", str(checkout), "HEAD"],
            check=True, capture_output=True,
        )

        code, context = self.enter("--campaign-id", "cmp_local_worktree_entry",
                                   cwd=checkout)
        self.assertEqual(code, 0, context)
        self.assertEqual(context["workspace_root"], str(self.base.resolve()))
        inspect_code, inspected = self.run_cli("inspect", context["session_id"],
                                                cwd=checkout)
        self.assertEqual(inspect_code, 0, inspected)
        self.assertEqual(
            Path(inspected["selected_checkouts"]["fixture"]["path"]),
            checkout.resolve(),
        )
        claim_code, claim = self.run_cli(
            "claims", context["session_id"], "--acquire", "fixture",
            "--reason", "claim the checkout selected by local entry",
            cwd=checkout,
        )
        self.assertEqual(claim_code, 0, claim)
        self.assertEqual(claim["scope"]["kind"], "worktree")
        self.assertEqual(claim["scope"]["checkout"], str(checkout.resolve()))
        self.assertEqual(
            inspected["selected_checkouts"]["fixture"]["checkout_identity"],
            workspace.verify_registered_checkout(
                checkout, repository="fixture", workspace_root=self.base,
                expected_repository_root=self.project,
            )["checkout_identity"],
        )

        resume_code, resumed = self.run_cli(
            "resume", context["session_id"], "--revalidate", cwd=checkout,
        )
        self.assertEqual(resume_code, 0, resumed)
        self.assertEqual(
            Path(resumed["selected_checkouts"]["fixture"]["path"]),
            checkout.resolve(),
        )
        checkpoint_code, checkpoint = self.run_cli(
            "checkpoint", context["session_id"], "--progress",
            "nested registered checkout retained through revalidation",
            cwd=checkout,
        )
        self.assertEqual(checkpoint_code, 0, checkpoint)
        fresh_code, fresh = self.run_cli("inspect", context["session_id"], cwd=checkout)
        self.assertEqual(fresh_code, 0, fresh)
        self.assertEqual(len(fresh["checkpoints"]), 1)
        self.assertEqual(
            Path(fresh["selected_checkouts"]["fixture"]["path"]),
            checkout.resolve(),
        )

    def test_legacy_campaign_reference_matches_only_exact_verified_checkout(self):
        checkout = {
            "repository": "fixture",
            "path": str(self.project.resolve()),
            "workspace_root": str(self.base.resolve()),
            "branch": "main",
            "head": "a" * 40,
            "checkout_identity": "checkout-fixture-main",
            "git_common_directory_identity": "common-fixture",
        }
        legacy = {
            "workspace": {"root": str(self.base.resolve())},
            "campaign": {"repository_refs": [{
                "repository": "fixture",
                "observed_path": "fixture",
                "branch": "main",
                "head": "a" * 40,
            }]},
        }
        self.assertTrue(entry._working_checkout_matches_snapshot(legacy, checkout))

        advanced_head = json.loads(json.dumps(legacy))
        advanced_head["campaign"]["repository_refs"][0]["head"] = "b" * 40
        self.assertTrue(entry._working_checkout_matches_snapshot(advanced_head, checkout))

        # HEAD movement is continuation only when the immutable legacy path
        # still resolves to this already-verified registered checkout.
        advanced_head_without_path = json.loads(json.dumps(advanced_head))
        advanced_head_without_path["campaign"]["repository_refs"][0].pop("observed_path")
        self.assertFalse(
            entry._working_checkout_matches_snapshot(advanced_head_without_path, checkout)
        )

        advanced_head_wrong_identity = json.loads(json.dumps(advanced_head))
        advanced_head_wrong_identity["campaign"]["repository_refs"][0][
            "git_common_directory_identity"
        ] = "common-from-another-repository"
        self.assertFalse(
            entry._working_checkout_matches_snapshot(advanced_head_wrong_identity, checkout)
        )

        changed_branch = json.loads(json.dumps(advanced_head))
        changed_branch["campaign"]["repository_refs"][0]["branch"] = "other-branch"
        self.assertFalse(entry._working_checkout_matches_snapshot(changed_branch, checkout))

        wrong_workspace = json.loads(json.dumps(legacy))
        wrong_workspace["workspace"]["root"] = str(self.base.parent.resolve())
        self.assertFalse(entry._working_checkout_matches_snapshot(wrong_workspace, checkout))

        wrong_path = json.loads(json.dumps(legacy))
        wrong_path["campaign"]["repository_refs"][0]["observed_path"] = "other-fixture"
        self.assertFalse(entry._working_checkout_matches_snapshot(wrong_path, checkout))

        duplicate_reference = json.loads(json.dumps(legacy))
        duplicate_reference["campaign"]["repository_refs"].append(
            dict(duplicate_reference["campaign"]["repository_refs"][0])
        )
        self.assertTrue(entry._working_checkout_matches_snapshot(duplicate_reference, checkout))

        # Multiple worktrees for one repository are valid campaign evidence.
        # The verified current checkout selects its exact path and branch.
        other_worktree = dict(
            legacy["campaign"]["repository_refs"][0],
            observed_path="fixture/.worktrees/other",
            branch="campaign/other-worktree",
            head="b" * 40,
            checkout_identity="checkout-fixture-other",
        )
        separate_worktree = json.loads(json.dumps(legacy))
        separate_worktree["campaign"]["repository_refs"].append(other_worktree)
        self.assertTrue(entry._working_checkout_matches_snapshot(separate_worktree, checkout))

        # The same path cannot carry conflicting canonical checkout identities.
        conflicting_duplicate = json.loads(json.dumps(legacy))
        conflicting_duplicate["campaign"]["repository_refs"].append(
            dict(
                conflicting_duplicate["campaign"]["repository_refs"][0],
                checkout_identity="checkout-from-another-path",
            )
        )
        self.assertFalse(entry._working_checkout_matches_snapshot(conflicting_duplicate, checkout))

        # Branch and HEAD cannot disambiguate a duplicate if neither record
        # identifies its checkout path or canonical checkout identity.
        pathless_duplicates = json.loads(json.dumps(legacy))
        for reference in pathless_duplicates["campaign"]["repository_refs"]:
            reference.pop("observed_path", None)
        pathless_duplicates["campaign"]["repository_refs"].append(
            dict(pathless_duplicates["campaign"]["repository_refs"][0])
        )
        self.assertFalse(entry._working_checkout_matches_snapshot(pathless_duplicates, checkout))
        conflict_details = entry._session_selection_details(
            "ses_pathless_duplicate", pathless_duplicates, checkout,
        )
        self.assertEqual(len(conflict_details["recorded_repository_references"]), 2)
        self.assertEqual(
            {item["branch"] for item in conflict_details["recorded_repository_references"]},
            {"main"},
        )

        # Selection maps are per repository, so an empty map or a selection
        # for another repository may use the exact legacy reference here.
        empty_selection = json.loads(json.dumps(legacy))
        empty_selection["selected_checkouts"] = {}
        self.assertTrue(entry._working_checkout_matches_snapshot(empty_selection, checkout))

        partial_selection = json.loads(json.dumps(legacy))
        partial_selection["selected_checkouts"] = {"other-repository": {
            "branch": "main", "head": "a" * 40,
        }}
        self.assertTrue(entry._working_checkout_matches_snapshot(partial_selection, checkout))

        mismatched_target_selection = json.loads(json.dumps(legacy))
        mismatched_target_selection["selected_checkouts"] = {"fixture": {
            "repository": "fixture",
            "path": str(self.project.resolve()),
            "checkout_identity": "checkout-from-another-worktree",
            "git_common_directory_identity": "common-fixture",
            "branch": "main",
        }}
        self.assertFalse(
            entry._working_checkout_matches_snapshot(mismatched_target_selection, checkout)
        )

        campaign_root = self.base / "campaign-root"
        nested_checkout = campaign_root / "fixture"
        nested_checkout.mkdir(parents=True)
        nested_workspace_snapshot = {
            "workspace": {"root": str(campaign_root.resolve())},
            "campaign": {"repository_refs": [{
                "repository": "fixture",
                "observed_path": "fixture",
                "checkout_identity": "workspace-relative-checkout-identity",
                "git_common_directory_identity": "workspace-relative-common-identity",
                "branch": "main",
                "head": "a" * 40,
            }]},
        }
        nested_working_checkout = {
            **checkout,
            "path": str(nested_checkout.resolve()),
            "workspace_root": str(self.base.resolve()),
            "checkout_identity": "projects-relative-checkout-identity",
            "git_common_directory_identity": "projects-relative-common-identity",
        }
        self.assertTrue(entry._working_checkout_matches_snapshot(
            nested_workspace_snapshot, nested_working_checkout,
        ))

    def test_registered_checkout_identity_survives_branch_rename(self):
        checkout = {
            "repository": "fixture",
            "path": str(self.project.resolve()),
            "workspace_root": str(self.base.resolve()),
            "branch": "campaign/current",
            "head": "b" * 40,
            "checkout_identity": "checkout-fixture-stable",
            "git_common_directory_identity": "common-fixture-stable",
            "manifest_identity": "manifest-fixture-stable",
        }
        snapshot = {
            "workspace": {"root": str(self.base.resolve())},
            "selected_checkouts": {"fixture": {
                "repository": "fixture",
                "path": str(self.project.resolve()),
                "branch": "campaign/previous",
                "head": "a" * 40,
                "checkout_identity": "checkout-fixture-stable",
                "git_common_directory_identity": "common-fixture-stable",
                "manifest_identity": "manifest-fixture-stable",
            }},
        }
        self.assertTrue(entry._working_checkout_identity_matches_snapshot(
            snapshot, checkout,
        ))
        self.assertTrue(entry._working_checkout_matches_snapshot(snapshot, checkout))

        wrong_path = json.loads(json.dumps(snapshot))
        wrong_path["selected_checkouts"]["fixture"]["path"] = str(
            self.project / ".worktrees" / "other"
        )
        self.assertFalse(entry._working_checkout_matches_snapshot(wrong_path, checkout))

        wrong_common_directory = json.loads(json.dumps(snapshot))
        wrong_common_directory["selected_checkouts"]["fixture"][
            "git_common_directory_identity"
        ] = "common-from-another-repository"
        self.assertFalse(entry._working_checkout_matches_snapshot(
            wrong_common_directory, checkout,
        ))

        wrong_checkout_identity = json.loads(json.dumps(snapshot))
        wrong_checkout_identity["selected_checkouts"]["fixture"][
            "checkout_identity"
        ] = "checkout-from-another-worktree"
        self.assertFalse(entry._working_checkout_matches_snapshot(
            wrong_checkout_identity, checkout,
        ))

        wrong_manifest_identity = json.loads(json.dumps(snapshot))
        wrong_manifest_identity["selected_checkouts"]["fixture"][
            "manifest_identity"
        ] = "manifest-from-another-checkout"
        self.assertFalse(entry._working_checkout_matches_snapshot(
            wrong_manifest_identity, checkout,
        ))

        legacy = {
            "workspace": {"root": str(self.base.resolve())},
            "campaign": {"repository_refs": [{
                "repository": "fixture",
                "observed_path": "fixture",
                "branch": "campaign/previous",
                "head": "a" * 40,
            }]},
        }
        self.assertFalse(entry._working_checkout_matches_snapshot(legacy, checkout))

    def test_pathless_legacy_reference_tracks_one_registered_branch_after_commits(self):
        (self.project / ".mncs" / "project.json").write_text(
            json.dumps({"repository": "fixture"}),
        )
        checkout = workspace.verify_registered_checkout(
            self.project, repository="fixture", workspace_root=self.base,
            expected_repository_root=self.project,
        )
        legacy = {
            "workspace": {"root": str(self.base.resolve())},
            "campaign": {"repository_refs": [{
                "repository": "fixture",
                "branch": checkout["branch"],
                "head": "b" * 40,
            }]},
        }
        self.assertNotEqual(legacy["campaign"]["repository_refs"][0]["head"],
                            checkout["head"])
        self.assertTrue(entry._working_checkout_matches_snapshot(legacy, checkout))

        registered = workspace._worktrees(self.project)[0]
        duplicate_branch = dict(registered)
        duplicate_branch["path"] = str(self.base / "fixture" / ".worktrees" / "duplicate")
        with mock.patch.object(
            workspace, "_worktrees", return_value=[registered, duplicate_branch],
        ):
            self.assertFalse(entry._working_checkout_matches_snapshot(legacy, checkout))

    def test_authenticated_campaign_selection_is_scoped_to_registered_checkout(self):
        (self.project / ".mncs" / "project.json").write_text(json.dumps({
            "repository": "fixture",
        }))
        self.config = {
            "name": "fixture-worktree-campaign-selection",
            "working_checkout": "current",
            "workspace_scope": {
                "kind": "workspace", "repositories": ["fixture"],
            },
            "intent": {
                "goal": "continue the campaign attached to this registered checkout",
                "repositories": ["fixture"],
            },
        }
        self.definition.write_text(json.dumps(self.config))
        subprocess.run(
            ["git", "-C", str(self.project), "add", ".mncs/project.json",
             ".mncs/environment.json"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.project), "-c", "user.name=Entry Test",
             "-c", "user.email=entry@example.invalid", "commit", "-m",
             "add checkout-scoped campaign definition"],
            check=True, capture_output=True,
        )
        nested = self.project / ".worktrees" / "registered" / "nested-checkout"
        nested.parent.mkdir(parents=True)
        subprocess.run(
            ["git", "-C", str(self.project), "worktree", "add", "-b",
             "campaign/nested-checkout", str(nested), "HEAD"],
            check=True, capture_output=True,
        )
        auth_env = dict(os.environ, MNCS_ENV_AUTH_PRINCIPAL_ID="principal-checkout-selection")

        main_code, main_first = self.enter(cwd=self.project, env=auth_env)
        nested_code, nested_first = self.enter(cwd=nested, env=auth_env)
        self.assertEqual(main_code, 0, main_first)
        self.assertEqual(nested_code, 0, nested_first)
        self.assertNotEqual(main_first["session_id"], nested_first["session_id"])
        self.assertNotEqual(
            main_first["continuation"]["identity"],
            nested_first["continuation"]["identity"],
        )

        parallel_sessions = []
        for index in range(3):
            parallel = (
                self.project / ".worktrees" / "parallel" / str(index)
                / "registered" / "fixture"
            )
            parallel.parent.mkdir(parents=True)
            subprocess.run(
                ["git", "-C", str(self.project), "worktree", "add", "-b",
                 f"campaign/parallel-{index}", str(parallel), "HEAD"],
                check=True, capture_output=True,
            )
            parallel_code, parallel_entry = self.enter(cwd=parallel, env=auth_env)
            self.assertEqual(parallel_code, 0, parallel_entry)
            parallel_sessions.append((parallel, parallel_entry))
        self.assertEqual(len({
            main_first["session_id"], nested_first["session_id"],
            *(entry["session_id"] for _, entry in parallel_sessions),
        }), 5)

        main_resume_code, main_resume = self.enter(cwd=self.project, env=auth_env)
        nested_resume_code, nested_resume = self.enter(cwd=nested, env=auth_env)
        self.assertEqual(main_resume_code, 0, main_resume)
        self.assertEqual(nested_resume_code, 0, nested_resume)
        self.assertEqual(main_resume["session_id"], main_first["session_id"])
        self.assertEqual(nested_resume["session_id"], nested_first["session_id"])
        for parallel, first_entry in parallel_sessions:
            parallel_resume_code, parallel_resume = self.enter(
                cwd=parallel, env=auth_env,
            )
            self.assertEqual(parallel_resume_code, 0, parallel_resume)
            self.assertEqual(
                parallel_resume["session_id"], first_entry["session_id"],
            )

        # A new task selector adopts the unique main-checkout continuation;
        # the other registered worktrees do not create false ambiguity.
        auth_env["MNCS_ENV_RPC_REQUEST_ID"] = "req_nested_task_selector_main"
        task_code, task_resume = self.enter(
            "--task-identity", "continue-main-checkout", cwd=self.project,
            env=auth_env,
        )
        self.assertEqual(task_code, 0, task_resume)
        self.assertEqual(task_resume["session_id"], main_first["session_id"])
        self.assertEqual(task_resume["entry"]["task_selector"]["status"], "bound")

        wrong_checkout_code, wrong_checkout = self.enter(
            "--campaign-id", nested_first["continuation"]["identity"],
            cwd=self.project, env=auth_env,
        )
        self.assertEqual(wrong_checkout_code, 0, wrong_checkout)
        self.assertEqual(wrong_checkout["session_id"], nested_first["session_id"])

        duplicate_code, duplicate = self.enter(
            "--campaign-id", "cmp_same_checkout_duplicate",
            "--new-session", cwd=self.project, env=auth_env,
        )
        self.assertEqual(duplicate_code, 0, duplicate)
        ambiguous_code, ambiguous = self.enter(cwd=self.project, env=auth_env)
        self.assertEqual(ambiguous_code, 2, ambiguous)
        self.assertEqual(
            ambiguous["diagnostics"]["code"], "campaign-continuation-ambiguous",
        )
        conflicts = ambiguous["diagnostics"]["conflicting_contexts"]
        inspect_code, main_inspected = self.run_cli(
            "inspect", main_first["session_id"], cwd=self.project,
        )
        self.assertEqual(inspect_code, 0, main_inspected)
        self.assertEqual({item["selected_checkout"]["checkout_identity"]
                          for item in conflicts}, {
            main_inspected["selected_checkouts"]["fixture"]["checkout_identity"],
        })
        self.assertEqual({item["selected_checkout"]["path"] for item in conflicts}, {
            str(self.project.resolve()),
        })

        subprocess.run(
            ["git", "-C", str(self.project), "checkout", "-b",
             "campaign/changed-checkout-branch"],
            check=True, capture_output=True,
        )
        auth_env["MNCS_ENV_RPC_REQUEST_ID"] = "req_campaign_checkout_branch_change"
        branch_change_code, branch_change = self.enter(
            cwd=self.project, env=auth_env,
        )
        self.assertEqual(branch_change_code, 2, branch_change)
        self.assertEqual(
            branch_change["diagnostics"]["code"], "campaign-continuation-ambiguous",
        )
        self.assertEqual(
            set(branch_change["diagnostics"]["sessions"]),
            {main_first["session_id"], duplicate["session_id"]},
        )

    def test_explicit_campaign_recovers_its_unique_nested_checkout_from_repo_root(self):
        (self.project / ".mncs" / "project.json").write_text(json.dumps({
            "repository": "fixture",
        }))
        self.config = {
            "name": "fixture-nested-campaign-recovery",
            "working_checkout": "current",
            "workspace_scope": {
                "kind": "workspace", "repositories": ["fixture"],
            },
            "intent": {
                "goal": "continue the campaign attached to its registered checkout",
                "repositories": ["fixture"],
            },
        }
        self.definition.write_text(json.dumps(self.config))
        subprocess.run(
            ["git", "-C", str(self.project), "add", ".mncs/project.json",
             ".mncs/environment.json"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.project), "-c", "user.name=Entry Test",
             "-c", "user.email=entry@example.invalid", "commit", "-m",
             "add nested campaign recovery definition"],
            check=True, capture_output=True,
        )
        nested = self.project / ".worktrees" / "recovery" / "compiler"
        nested.parent.mkdir(parents=True)
        subprocess.run(
            ["git", "-C", str(self.project), "worktree", "add", "-b",
             "campaign/nested-recovery", str(nested), "HEAD"],
            check=True, capture_output=True,
        )
        auth_env = dict(os.environ, MNCS_ENV_AUTH_PRINCIPAL_ID="principal-nested-recovery")
        campaign_id = "cmp_nested_root_continuation"

        nested_code, nested_entry = self.enter(
            "--campaign-id", campaign_id, cwd=nested, env=auth_env,
        )
        self.assertEqual(nested_code, 0, nested_entry)
        nested_inspect_code, nested_inspected = self.run_cli(
            "inspect", nested_entry["session_id"], cwd=nested,
        )
        self.assertEqual(nested_inspect_code, 0, nested_inspected)
        self.assertEqual(
            Path(nested_inspected["selected_checkouts"]["fixture"]["path"]),
            nested.resolve(),
        )

        root_code, root_entry = self.enter(
            "--campaign-id", campaign_id, cwd=self.project, env=auth_env,
        )
        self.assertEqual(root_code, 0, root_entry)
        self.assertEqual(root_entry["session_id"], nested_entry["session_id"])
        root_inspect_code, root_inspected = self.run_cli(
            "inspect", root_entry["session_id"], cwd=self.project,
        )
        self.assertEqual(root_inspect_code, 0, root_inspected)
        self.assertEqual(
            Path(root_inspected["selected_checkouts"]["fixture"]["path"]),
            nested.resolve(),
        )

    def test_unique_authenticated_campaign_recovers_nested_checkout_without_campaign_id(self):
        (self.project / ".mncs" / "project.json").write_text(json.dumps({
            "repository": "fixture",
        }))
        self.config = {
            "name": "fixture-implicit-nested-campaign-recovery",
            "working_checkout": "current",
            "workspace_scope": {
                "kind": "workspace", "repositories": ["fixture"],
            },
            "intent": {
                "goal": "continue the unique campaign attached to its registered checkout",
                "repositories": ["fixture"],
            },
        }
        self.definition.write_text(json.dumps(self.config))
        subprocess.run(
            ["git", "-C", str(self.project), "add", ".mncs/project.json",
             ".mncs/environment.json"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.project), "-c", "user.name=Entry Test",
             "-c", "user.email=entry@example.invalid", "commit", "-m",
             "add implicit nested campaign recovery definition"],
            check=True, capture_output=True,
        )
        nested = self.project / ".worktrees" / "implicit-recovery" / "compiler"
        nested.parent.mkdir(parents=True)
        subprocess.run(
            ["git", "-C", str(self.project), "worktree", "add", "-b",
             "campaign/implicit-nested-recovery", str(nested), "HEAD"],
            check=True, capture_output=True,
        )
        auth_env = dict(
            os.environ,
            MNCS_ENV_AUTH_PRINCIPAL_ID="principal-implicit-nested-recovery",
        )

        nested_code, nested_entry = self.enter(cwd=nested, env=auth_env)
        self.assertEqual(nested_code, 0, nested_entry)
        root_code, root_entry = self.enter(cwd=self.project, env=auth_env)
        self.assertEqual(root_code, 0, root_entry)
        self.assertEqual(root_entry["session_id"], nested_entry["session_id"])
        root_inspect_code, root_inspected = self.run_cli(
            "inspect", root_entry["session_id"], cwd=self.project,
        )
        self.assertEqual(root_inspect_code, 0, root_inspected)
        self.assertEqual(
            Path(root_inspected["selected_checkouts"]["fixture"]["path"]),
            nested.resolve(),
        )

        # A distinct task identity remains independent and cannot be adopted
        # from the unique existing campaign by checkout proximity alone.
        task_code, task_entry = self.enter(
            "--task-identity", "independent-root-task",
            cwd=self.project, env=auth_env,
        )
        self.assertEqual(task_code, 0, task_entry)
        self.assertNotEqual(task_entry["session_id"], nested_entry["session_id"])

    def test_campaign_checkout_accepts_project_environment_definition_by_identity(self):
        (self.project / ".mncs" / "project.json").write_text(json.dumps({
            "repository": "fixture",
        }))
        config = {
            "name": "fixture-project-environment",
            "working_checkout": "current",
            "workspace_scope": {
                "kind": "workspace", "repositories": ["fixture"],
            },
            "intent": {
                "goal": "continue the campaign attached to this project definition",
                "repositories": ["fixture"],
            },
        }
        self.definition.unlink()
        project_definition = self.project / ".mncs" / "project-environment.json"
        project_definition.write_text(json.dumps(config))
        subprocess.run(
            ["git", "-C", str(self.project), "add", "-A", ".mncs"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.project), "-c", "user.name=Entry Test",
             "-c", "user.email=entry@example.invalid", "commit", "-m",
             "add project-owned Environment definition"],
            check=True, capture_output=True,
        )
        nested = self.project / ".worktrees" / "project-environment" / "fixture"
        nested.parent.mkdir(parents=True)
        subprocess.run(
            ["git", "-C", str(self.project), "worktree", "add", "-b",
             "campaign/project-environment", str(nested), "HEAD"],
            check=True, capture_output=True,
        )

        definition_id = identity.environment_id(config)
        current_checkout = workspace.verify_registered_checkout(
            self.project, repository="fixture", workspace_root=self.base,
            expected_repository_root=self.project,
        )
        nested_checkout = workspace.verify_registered_checkout(
            nested, repository="fixture", workspace_root=self.base,
            expected_repository_root=self.project,
            expected_branch="campaign/project-environment",
        )
        self.assertEqual(
            current_checkout["git_common_directory_identity"],
            nested_checkout["git_common_directory_identity"],
        )
        snapshot = {
            "campaign": {"identity": "cmp_project_environment_checkout"},
            "authenticated_principal_id": "principal-project-environment",
            "provenance": {"definition_id": definition_id},
            "workspace": {"root": str(self.base.resolve())},
            "lifecycle": "active",
            "selected_checkouts": {"fixture": nested_checkout},
        }

        class Store:
            def load_snapshot(self, session_id):
                return snapshot if session_id == "ses_project_environment" else None

        def recover_checkout():
            with mock.patch(
                "mncs_env.selection.select", return_value=["ses_project_environment"],
            ):
                return entry._campaign_checkout_from_store(
                    Store(),
                    campaign_id="cmp_project_environment_checkout",
                    authenticated_principal_id="principal-project-environment",
                    definition_id=definition_id,
                    workspace_root=self.base.resolve(),
                    working_checkout=current_checkout,
                )

        recovered = recover_checkout()
        self.assertEqual(Path(recovered["path"]), nested.resolve())
        self.assertEqual(recovered["checkout_identity"], nested_checkout["checkout_identity"])

        # A second regular file with the same verified identity is ambiguous;
        # checkout recovery never chooses between definition paths arbitrarily.
        nested_environment = nested / ".mncs" / "environment.json"
        nested_environment.write_text(json.dumps(config))
        with self.assertRaises(entry.EntryError) as duplicate:
            recover_checkout()
        self.assertEqual(duplicate.exception.diagnostics["code"], "campaign-checkout-invalid")
        self.assertEqual(len(duplicate.exception.diagnostics["matching_definition_paths"]), 2)
        nested_environment.unlink()

        # A mismatched definition and a symlinked project definition cannot
        # lend their identity to this registered checkout.
        nested_config = dict(config, name="different-project-environment")
        nested_project_definition = nested / ".mncs" / "project-environment.json"
        nested_project_definition.write_text(json.dumps(nested_config))
        with self.assertRaises(entry.EntryError) as mismatch:
            recover_checkout()
        self.assertEqual(mismatch.exception.diagnostics["code"], "campaign-checkout-invalid")
        self.assertEqual(mismatch.exception.diagnostics["matching_definition_paths"], [])

        external_definition = self.base / "external-project-environment.json"
        external_definition.write_text(json.dumps(config))
        nested_project_definition.unlink()
        nested_project_definition.symlink_to(external_definition)
        with self.assertRaises(entry.EntryError) as symlink:
            recover_checkout()
        self.assertEqual(symlink.exception.diagnostics["code"], "campaign-checkout-invalid")
        self.assertIn(
            str(nested_project_definition), symlink.exception.diagnostics["invalid_definition_paths"],
        )

    def test_registered_checkout_identity_rejects_symlinks_and_unrelated_clones(self):
        (self.project / ".mncs" / "project.json").write_text(json.dumps({
            "repository": "fixture",
        }))
        subprocess.run(
            ["git", "-C", str(self.project), "add", ".mncs/project.json"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.project), "-c", "user.name=Entry Test",
             "-c", "user.email=entry@example.invalid", "commit", "-m",
             "add repository identity"],
            check=True, capture_output=True,
        )
        nested = self.project / ".worktrees" / "nested" / "fixture"
        nested.parent.mkdir(parents=True)
        subprocess.run(
            ["git", "-C", str(self.project), "worktree", "add", "-b",
             "campaign/nested-fixture", str(nested), "HEAD"],
            check=True, capture_output=True,
        )
        alias = self.project / ".worktrees" / "alias"
        alias.symlink_to(nested, target_is_directory=True)
        with self.assertRaisesRegex(workspace.WorkspaceResolutionError, "symbolic link"):
            workspace.verify_registered_checkout(
                alias, repository="fixture", workspace_root=self.base,
                expected_repository_root=self.project,
            )

        clone = self.project / ".worktrees" / "unregistered-clone"
        subprocess.run(
            ["git", "clone", "--quiet", "--shared", str(self.project), str(clone)],
            check=True, capture_output=True,
        )
        with self.assertRaisesRegex(
            workspace.WorkspaceResolutionError, "not an immediate repository"
        ):
            workspace.verify_registered_checkout(
                clone, repository="fixture", workspace_root=self.base,
                expected_repository_root=self.project,
            )

        current_checkout = workspace.verify_registered_checkout(
            self.project, repository="fixture", workspace_root=self.base,
            expected_repository_root=self.project,
        )
        clone_branch = subprocess.run(
            ["git", "-C", str(clone), "symbolic-ref", "--quiet", "--short", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        clone_identity = workspace.repository_checkout_identity(
            self.base, "fixture", clone,
        )
        self.assertNotEqual(
            current_checkout["git_common_directory_identity"],
            clone_identity["git_common_directory_identity"],
        )
        invalid_snapshot = {
            "campaign": {"identity": "cmp_unregistered_campaign_checkout"},
            "authenticated_principal_id": "principal-invalid-checkout",
            "provenance": {"definition_id": identity.environment_id(self.config)},
            "workspace": {"root": str(self.base.resolve())},
            "lifecycle": "active",
            "selected_checkouts": {"fixture": {
                "repository": "fixture",
                "path": str(clone.resolve()),
                "branch": clone_branch,
                **clone_identity,
            }},
        }

        class InvalidCheckoutStore:
            def load_snapshot(self, session_id):
                self_session = session_id
                return invalid_snapshot if self_session == "ses_invalid_checkout" else None

        with mock.patch(
            "mncs_env.selection.select", return_value=["ses_invalid_checkout"],
        ), self.assertRaises(entry.EntryError) as invalid_recovery:
            entry._campaign_checkout_from_store(
                InvalidCheckoutStore(),
                campaign_id="cmp_unregistered_campaign_checkout",
                authenticated_principal_id="principal-invalid-checkout",
                definition_id=identity.environment_id(self.config),
                workspace_root=self.base.resolve(),
                working_checkout=current_checkout,
            )
        self.assertEqual(
            invalid_recovery.exception.diagnostics["code"],
            "campaign-checkout-invalid",
        )

        with self.assertRaisesRegex(
            workspace.WorkspaceResolutionError, "branch or revision"
        ):
            workspace.verify_registered_checkout(
                nested, repository="fixture", workspace_root=self.base,
                expected_repository_root=self.project,
                expected_branch="campaign/not-the-registered-branch",
            )

    def test_registered_nested_checkout_resolves_through_control_mount_alias(self):
        (self.project / ".mncs" / "project.json").write_text(
            json.dumps(
                {
                    "repository": "fixture",
                }
            )
        )
        subprocess.run(
            ["git", "-C", str(self.project), "add", ".mncs/project.json"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(self.project),
                "-c",
                "user.name=Entry Test",
                "-c",
                "user.email=entry@example.invalid",
                "commit",
                "-m",
                "add identity for aliased checkout",
            ],
            check=True,
            capture_output=True,
        )
        nested = self.project / ".worktrees" / "registered" / "nested-checkout"
        nested.parent.mkdir(parents=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(self.project),
                "worktree",
                "add",
                "-b",
                "campaign/aliased-checkout",
                str(nested),
                "HEAD",
            ],
            check=True,
            capture_output=True,
        )
        registered = next(
            row
            for row in workspace._worktrees(self.project)
            if row.get("path") == str(nested)
        )
        actual_git_directory = subprocess.run(
            [
                "git",
                "-C",
                str(nested),
                "rev-parse",
                "--path-format=absolute",
                "--absolute-git-dir",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        head = subprocess.run(
            ["git", "-C", str(nested), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        branch = "campaign/aliased-checkout"

        sandbox_workspace = self.base / "control-sandbox"
        sandbox_repository = sandbox_workspace / "fixture"
        sandbox_checkout = (
            sandbox_repository / ".worktrees" / "registered" / "nested-checkout"
        )
        sandbox_admin = (
            sandbox_repository / ".git" / "worktrees" / Path(actual_git_directory).name
        )
        sandbox_checkout.mkdir(parents=True)
        sandbox_admin.mkdir(parents=True)
        for checkout in (sandbox_repository, sandbox_checkout):
            (checkout / ".mncs").mkdir(parents=True, exist_ok=True)
            shutil.copy2(self.project / ".mncs" / "project.json", checkout / ".mncs")
        (sandbox_admin / "gitdir").write_text(str(nested / ".git"))
        (sandbox_checkout / ".git").write_text(f"gitdir: {actual_git_directory}\n")

        selected = {"path": sandbox_checkout, "git_directory": sandbox_admin}

        def fake_git(_repo, *args, deadline=None):
            outputs = {
                ("rev-parse", "--show-toplevel"): (str(selected["path"]), 0),
                ("rev-parse", "--path-format=absolute", "--git-common-dir"): (
                    str(sandbox_repository / ".git"),
                    0,
                ),
                ("rev-parse", "--path-format=absolute", "--absolute-git-dir"): (
                    str(selected["git_directory"]),
                    0,
                ),
                ("rev-parse", "HEAD"): (head, 0),
                ("symbolic-ref", "--quiet", "--short", "HEAD"): (branch, 0),
            }
            output, code = outputs.get(tuple(args), ("", 1))
            return subprocess.CompletedProcess(args, code, output, "")

        identity = {
            "checkout_identity": "checkout:aliased-nested",
            "git_common_directory_identity": "git-common:aliased-nested",
            "observed_path": ".worktrees/registered/nested-checkout",
        }
        original_resolve = Path.resolve

        def resolve_from_sandbox(path, strict=False):
            if Path(os.path.abspath(path)) == nested:
                return original_resolve(sandbox_checkout, strict=True)
            return original_resolve(path, strict=strict)

        with (
            mock.patch.object(workspace, "_git", side_effect=fake_git),
            mock.patch.object(workspace, "_worktrees", return_value=[registered]),
            mock.patch.object(
                workspace, "repository_checkout_identity", return_value=identity
            ),
            mock.patch.object(Path, "resolve", new=resolve_from_sandbox),
        ):
            observed = workspace.verify_registered_checkout(
                sandbox_checkout,
                repository="fixture",
                workspace_root=sandbox_workspace,
                expected_repository_root=sandbox_repository,
                expected_branch=branch,
                expected_head=head,
            )
            self.assertEqual(observed["path"], str(sandbox_checkout.resolve()))
            self.assertEqual(observed["branch"], branch)
            self.assertEqual(observed["head"], head)

            forged = sandbox_repository / ".worktrees" / "lookalike" / "fixture"
            forged.mkdir(parents=True)
            (forged / ".mncs").mkdir()
            shutil.copy2(self.project / ".mncs" / "project.json", forged / ".mncs")
            (forged / ".git").write_text(f"gitdir: {actual_git_directory}\n")
            selected["path"] = forged
            with self.assertRaisesRegex(
                workspace.WorkspaceResolutionError,
                "not one unique registered, available worktree",
            ):
                workspace.verify_registered_checkout(
                    forged,
                    repository="fixture",
                    workspace_root=sandbox_workspace,
                    expected_repository_root=sandbox_repository,
                )

    def test_registered_checkout_identity_rejects_manifest_symlink(self):
        external = self.base / "manifest.json"
        external.write_text(json.dumps({"repository": "fixture"}))
        manifest = self.project / ".mncs" / "project.json"
        manifest.unlink()
        manifest.symlink_to(external)
        with self.assertRaisesRegex(
            workspace.WorkspaceResolutionError, "manifest identity cannot traverse"
        ):
            workspace.verify_registered_checkout(
                self.project, repository="fixture", workspace_root=self.base,
                expected_repository_root=self.project,
            )

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
        probed = capabilities.probe_availability(binding)
        self.assertEqual(probed["availability"]["status"], "unknown")
        self.assertEqual(probed["availability"]["code"], "provider-invocation-undeclared")

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
        self.authorize_fixture_writes()
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
        legacy_path = self.state / "sessions" / session_id / "store-provider.json"
        new_path = (self.state / "store" / "environment" / "sessions"
                    / session_id / "store-provider.json")
        legacy_path.parent.mkdir(parents=True)
        legacy_path.write_text(json.dumps(prior))
        snapshot = {"session_id": session_id}
        with mock.patch.object(session_store, "store_provider_from_environment", return_value=selected):
            self.assertTrue(session_store.upgrade_session_store_provider(self.state, snapshot))
            self.assertFalse(session_store.upgrade_session_store_provider(self.state, snapshot))
        upgraded = json.loads(new_path.read_text())
        self.assertEqual(upgraded["runtime_environment"], selected["runtime_environment"])
        self.assertEqual(upgraded["revision"], "original")
        prior["checkout"] = str(self.base / "foreign-store")
        new_path.unlink()
        legacy_path.write_text(json.dumps(prior))
        with mock.patch.object(session_store, "store_provider_from_environment", return_value=selected):
            with self.assertRaisesRegex(ValueError, "does not match"):
                session_store.upgrade_session_store_provider(self.state, snapshot)
        self.assertEqual(json.loads(legacy_path.read_text()), prior)


if __name__ == "__main__":
    unittest.main()


def test_store_entry_lock_reports_read_only_owner_boundary(tmp_path, monkeypatch):
    import errno

    from mncs_env.entry import EntryError, entry_lock
    def denied(*args, **kwargs):
        raise OSError(errno.EROFS, "read-only filesystem")
    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(EntryError) as raised:
        with entry_lock(tmp_path, "store"):
            pass
    assert raised.value.diagnostics["code"] == "direct-filesystem-read-only"
    assert raised.value.diagnostics["transport"] == "store-owner-filesystem"
    assert raised.value.diagnostics["publication_completed"] is False
    assert "write capability" in raised.value.diagnostics["next"]


def test_store_entry_lock_uses_only_the_configured_store_root(tmp_path):
    from mncs_env.entry import entry_lock

    with entry_lock(tmp_path, "store"):
        lock = tmp_path / "store" / "entry-store.lock"
        assert lock.is_file()
        assert not (tmp_path / "entry-store.lock").exists()


def test_file_entry_lock_keeps_explicit_debug_path_recovery(tmp_path, monkeypatch):
    import errno

    from mncs_env.entry import EntryError, entry_lock
    def denied(*args, **kwargs):
        raise OSError(errno.EACCES, "access denied")
    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(EntryError) as raised:
        with entry_lock(tmp_path, "file"):
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
    from types import SimpleNamespace

    from mncs_env.session_store import SnapshotConflict
    from mncs_env.store_backend import StoreBackend

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


def test_read_only_open_defers_store_mutation(tmp_path, monkeypatch):
    from mncs_env import sessions as sessions_module

    calls = {}

    class DeferredStore:
        state_dir = tmp_path

        def load_snapshot(self, session_id):
            return {"session_id": session_id, "snapshot_sequence": 0}

    def recording_open(state_dir, backend="store", **kwargs):
        calls.update(kwargs)
        calls["backend"] = backend
        calls["state_dir"] = state_dir
        return DeferredStore()

    monkeypatch.setattr(sessions_module, "open_store", recording_open)
    session = sessions_module.Session.open(
        state_dir=tmp_path, session_id="ses_deferred", backend="store")
    assert session.session_id == "ses_deferred"
    assert calls.get("defer_mutation") is True


def test_read_snapshot_verifies_only_the_latest_revision_payload():
    import json as json_module
    from types import SimpleNamespace

    from mncs_env.store_backend import SCHEMA_SNAPSHOT, StoreBackend

    payload_reads = []

    class StubStore:
        current_generation = 7

        def domain_bindings_at(self, generation):
            assert generation == 7
            return [
                (SCHEMA_SNAPSHOT, b"ses_x:snap:0000000001"),
                (SCHEMA_SNAPSHOT, b"ses_x:snap:0000000002"),
                (b"other.schema/1", b"ses_x:snap:0000000009"),
            ]

        def find_bound_objects(self, schema, prefix=b""):
            payload_reads.append(bytes(prefix))
            revision = bytes(prefix).decode().split(":snap:")[1]
            return [SimpleNamespace(
                domain_schema=schema, domain_identity=bytes(prefix),
                payload=json_module.dumps({"rev": int(revision)}).encode())]

    backend = StoreBackend.__new__(StoreBackend)
    backend._store = StubStore()
    assert backend.read_snapshot("ses_x") == {"rev": 2}
    assert payload_reads == [b"ses_x:snap:0000000002"]


def test_read_snapshot_falls_back_to_older_decodable_revision():
    import json as json_module
    from types import SimpleNamespace

    from mncs_env.store_backend import SCHEMA_SNAPSHOT, StoreBackend

    class StubStore:
        current_generation = 3

        def domain_bindings_at(self, generation):
            return [
                (SCHEMA_SNAPSHOT, b"ses_y:snap:0000000001"),
                (SCHEMA_SNAPSHOT, b"ses_y:snap:0000000002"),
            ]

        def find_bound_objects(self, schema, prefix=b""):
            payload = b"not-json" if prefix.endswith(b"2") else json_module.dumps({"rev": 1}).encode()
            return [SimpleNamespace(
                domain_schema=schema, domain_identity=bytes(prefix), payload=payload)]

    backend = StoreBackend.__new__(StoreBackend)
    backend._store = StubStore()
    assert backend.read_snapshot("ses_y") == {"rev": 1}


def test_read_snapshot_propagates_payload_integrity_failure():
    from mncs_env.store_backend import SCHEMA_SNAPSHOT, StoreBackend

    class IntegrityFailure(Exception):
        pass

    class StubStore:
        current_generation = 3

        def domain_bindings_at(self, generation):
            return [(SCHEMA_SNAPSHOT, b"ses_z:snap:0000000004")]

        def find_bound_objects(self, schema, prefix=b""):
            raise IntegrityFailure("chunk failed integrity")

    backend = StoreBackend.__new__(StoreBackend)
    backend._store = StubStore()
    with pytest.raises(IntegrityFailure):
        backend.read_snapshot("ses_z")


def test_read_event_tail_materializes_only_selected_payloads():
    import json as json_module
    from types import SimpleNamespace

    from mncs_env.store_backend import SCHEMA_EVENT, StoreBackend

    session_id = "ses_tail"
    prefix = f"{session_id}:evt:".encode()
    identities = [prefix + f"{sequence:010d}".encode() for sequence in range(1, 6)]
    unrelated = (SCHEMA_EVENT, b"ses_other:evt:0000000001")
    payload_reads = []

    class StubStore:
        current_generation = 7

        def domain_bindings_at(self, generation):
            assert generation == 7
            return tuple(
                (SCHEMA_EVENT, identity) for identity in identities
            ) + (unrelated,)

        def get_bound_object(self, schema, identity):
            assert schema == SCHEMA_EVENT
            payload_reads.append(identity)
            sequence = int(identity[len(prefix):])
            return SimpleNamespace(payload=json_module.dumps({
                "session_id": session_id,
                "sequence": sequence,
                "identity": f"evt_{sequence}",
            }).encode())

        def find_bound_objects(self, *_args, **_kwargs):
            raise AssertionError("tail reads must not materialize all session events")

    backend = StoreBackend.__new__(StoreBackend)
    backend._store = StubStore()

    events, event_count = backend.read_event_tail(session_id, limit=2)

    assert event_count == 5
    assert [event["sequence"] for event in events] == [4, 5]
    assert payload_reads == identities[-2:]

    payload_reads.clear()
    page = backend.read_event_page(
        session_id, limit=2, before_sequence=4,
    )
    assert page["event_count"] == 5
    assert [event["sequence"] for event in page["events"]] == [2, 3]
    assert page["truncated"] is True
    assert page["next_before_sequence"] == 2
    assert payload_reads == identities[1:3]

    payload_reads.clear()
    oldest_page = backend.read_event_page(
        session_id, limit=2, before_sequence=2,
    )
    assert [event["sequence"] for event in oldest_page["events"]] == [1]
    assert oldest_page["truncated"] is False
    assert oldest_page["next_before_sequence"] is None
    assert payload_reads == identities[:1]


def test_list_sessions_reads_binding_metadata_only():
    from mncs_env.store_backend import SCHEMA_EVENT, SCHEMA_SNAPSHOT, StoreBackend

    class StubStore:
        current_generation = 11

        def domain_bindings_at(self, generation):
            return [
                (SCHEMA_SNAPSHOT, b"ses_a:snap:0000000001"),
                (SCHEMA_SNAPSHOT, b"ses_a:snap:0000000002"),
                (SCHEMA_SNAPSHOT, b"ses_b:snap:0000000001"),
                (SCHEMA_EVENT, b"ses_a:evt:0000000001"),
            ]

        def find_bound_objects(self, schema, prefix=b""):
            raise AssertionError("list_sessions must not read payloads")

    backend = StoreBackend.__new__(StoreBackend)
    backend._store = StubStore()
    assert backend.list_sessions() == ["ses_a", "ses_b"]


def test_cli_snapshot_conflict_reports_possible_completed_effects(monkeypatch, capsys):
    from types import SimpleNamespace

    from mncs_env import cli
    from mncs_env.session_store import SnapshotConflict
    def collision(args):
        raise SnapshotConflict("session", 4)
    args = SimpleNamespace(state_dir=Path("/tmp/state"), command="invoke", func=collision)
    monkeypatch.setattr(cli, "build_parser", lambda: SimpleNamespace(parse_args=lambda argv: args))
    assert cli.main([]) == 2
    result = json.loads(capsys.readouterr().err)
    assert result["diagnostics"]["code"] == "session-snapshot-conflict"
    assert result["diagnostics"]["capability_may_have_run"] is True
    assert result["diagnostics"]["snapshot_saved"] is False


def test_cli_stale_provider_checkout_guides_reconciliation(monkeypatch, capsys):
    from types import SimpleNamespace

    from mncs_env import cli

    def stale_provider_checkout(args):
        raise ValueError("declared provider checkout head changed")

    args = SimpleNamespace(
        state_dir=Path("/tmp/state"),
        command="claims",
        session="ses_stale_provider",
        func=stale_provider_checkout,
    )
    monkeypatch.setattr(
        cli, "build_parser", lambda: SimpleNamespace(parse_args=lambda argv: args)
    )

    assert cli.main([]) == 2
    result = json.loads(capsys.readouterr().err)
    assert result["diagnostics"]["code"] == "declared-provider-checkout-changed"
    assert "mncs-env reconcile ses_stale_provider" in result["diagnostics"]["next"]
    assert "if unexpected" in result["diagnostics"]["next"]
