"""Rights-context admission, safe evidence reads, and incremental invalidation."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from mncs_env import events, rights, sessions
from mncs_env.session_store import open_store

PROFILE_ID = "mncs-rights-provenance/native-open@0.1.0"


def _manifest() -> dict:
    return {
        "schema_version": "0.3.0",
        "artifact": {"id": "test-project", "class": "source-code"},
        "provenance": {
            "origin_classification": "origin-uncertain",
            "participants": [],
            "process_evidence": [],
        },
        "rights": {
            "distribution_license": "Apache-2.0",
            "copyright_status": "unresolved",
            "rights_basis": "unknown-needs-review",
            "third_party_material": "unknown",
            "sources": [],
        },
        "review": {
            "technical_validation": "not-run",
            "provenance_validation": "incomplete",
            "human_acceptance": "not-reviewed",
        },
    }


def _governance() -> dict:
    return {
        "schema_version": "mncs.rights.agent-governance/0.1.0",
        "profile_id": PROFILE_ID,
        "profile_version": 1,
        "rights": {
            "external_material": "verify",
            "incompatible_rights": "resolve-before-inclusion",
            "unknown_rights": "investigate",
        },
        "provenance": {
            "lineage": "preserve",
            "transformations": "record",
            "external_inputs": "identify",
            "unverifiable_claims": "distinguish",
        },
        "agent_governance": {
            "copying_external_implementations": "authorization-required",
            "independent_implementation": "permitted-when-lawful",
            "rights_conflict": "explain-and-escalate",
            "provenance_loss": "diagnose-and-recover",
        },
    }


def _binding(scope: list[str] | None = None) -> dict:
    return {
        "schema_version": "mncs.environment.rights-context-binding/2",
        "manifest": "RIGHTS.json",
        "manifest_scope": "workspace",
        "governance_profile": "governance.json",
        "profile_id": PROFILE_ID,
        "authority": {
            "identity": "mncs-environment",
            "basis": "explicit-environment-owner-binding",
            "source_ref": ".mncs/environment.json#/rights_context",
            "scope": scope or ["workspace"],
        },
        "external_metadata_authoritative": False,
        "context_budget_bytes": 8192,
    }


def _write_sources(root: Path) -> None:
    (root / "RIGHTS.json").write_text(json.dumps(_manifest()) + "\n")
    (root / "governance.json").write_text(json.dumps(_governance()) + "\n")


class RightsContextTests(unittest.TestCase):
    def test_project_environment_binds_its_own_rights_manifest(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        project_definition = json.loads(
            (repo_root / ".mncs" / "project-environment.json").read_text()
        )
        binding = project_definition["rights_context"]
        environment_binding = json.loads(
            (repo_root / ".mncs" / "environment.json").read_text()
        )["rights_context"]
        self.assertEqual(environment_binding["manifest"], "RIGHTS.json")
        self.assertEqual(environment_binding["manifest_scope"], "working-checkout")
        self.assertEqual(environment_binding["authority"]["scope"], ["mncs-environment"])
        self.assertEqual(binding["manifest"], environment_binding["manifest"])

        with tempfile.TemporaryDirectory() as directory:
            family = Path(directory)
            project = family / "mncs-environment"
            project.mkdir()
            (project / "RIGHTS.json").write_bytes((repo_root / "RIGHTS.json").read_bytes())
            governance = family / "mncs-rights-provenance" / "governance"
            governance.mkdir(parents=True)
            (governance / "agent-governance.json").write_text(
                json.dumps(_governance()) + "\n"
            )

            resolved = rights.load_agent_context(
                binding, family, working_checkout_root=project
            )

        context = resolved["context"]
        self.assertEqual(resolved["inputs"]["status"], "available")
        self.assertEqual(
            resolved["inputs"]["manifest"]["reference"],
            "RIGHTS.json",
        )
        self.assertEqual(resolved["inputs"]["manifest"]["source_root"], str(project))
        self.assertEqual(context["rights"]["distribution_license"], "Apache-2.0")
        self.assertEqual(context["rights"]["copyright_status"], "unresolved")
        self.assertEqual(context["provenance"]["evidence_state"], "declared-unverified")
        self.assertEqual(context["governance"]["status"], "authorized")
        self.assertEqual(
            context["governance"]["authority"]["scope"], ["mncs-environment"]
        )
        self.assertLess(len(json.dumps(context).encode("utf-8")), 8192)

    def test_environment_admission_projects_compact_authorized_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_sources(root)
            state = root / "state"
            store = open_store(state, "file", verify_on_open=False)
            environment = sessions.resolve_environment(
                definition={"name": "rights-test", "rights_context": _binding()},
                workspace_root=root,
                state_dir=state,
                consumer_id="test-agent",
                backend="file",
                store=store,
            )
            session = sessions.Session.create(
                state_dir=state, environment=environment, consumer_id="test-agent",
                backend="file", store=store,
            )

            context = session.context()["rights_context"]
            self.assertEqual(context["schema_version"], "mncs.rights-provenance.agent-context/1")
            self.assertEqual(context["rights"]["distribution_license"], "Apache-2.0")
            self.assertEqual(context["rights"]["compatibility"]["status"], "unknown")
            self.assertEqual(context["provenance"]["evidence_state"], "declared-unverified")
            self.assertEqual(context["governance"]["status"], "authorized")
            self.assertIn(
                "rights-basis-unknown",
                {item["code"] for item in context["diagnostics"]},
            )
            self.assertEqual(
                session.inspect()["rights_context"]["identity"], context["identity"]
            )
            self.assertEqual(
                [item["relation"] for item in context["provenance"]["projection"]["relationships"]],
                ["generated-from", "generated-from", "referenced", "transformed-by"],
            )
            self.assertLess(len(json.dumps(context)), 8192)

    def test_working_checkout_manifest_is_read_from_selected_repository(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            family = Path(directory)
            checkout = family / "repo-a"
            (checkout / ".git").mkdir(parents=True)
            family_manifest = _manifest()
            family_manifest["rights"]["distribution_license"] = "MIT"
            (family / "RIGHTS.json").write_text(json.dumps(family_manifest))
            (checkout / "RIGHTS.json").write_text(json.dumps(_manifest()))
            (family / "governance.json").write_text(json.dumps(_governance()))
            binding = _binding(["repo-a"])
            binding["manifest_scope"] = "working-checkout"
            binding["authority"]["identity"] = "repo-a"
            state = family / "state"
            store = open_store(state, "file", verify_on_open=False)
            environment = sessions.resolve_environment(
                definition={
                    "name": "rights-selected-checkout",
                    "workspace_scope": {"repositories": ["repo-a"]},
                    "rights_context": binding,
                },
                workspace_root=family,
                state_dir=state,
                consumer_id="test-agent",
                backend="file",
                store=store,
            )
            session = sessions.Session.create(
                state_dir=state, environment=environment, consumer_id="test-agent",
                backend="file", store=store,
            )
            context = session.context()["rights_context"]

        self.assertEqual(context["rights"]["distribution_license"], "Apache-2.0")
        self.assertEqual(
            session.snapshot["rights_context_inputs"]["manifest"]["source_root"],
            str(checkout),
        )

    def test_policy_file_change_emits_incremental_context_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_sources(root)
            state = root / "state"
            store = open_store(state, "file", verify_on_open=False)
            environment = sessions.resolve_environment(
                definition={"name": "rights-test", "rights_context": _binding()},
                workspace_root=root, state_dir=state, consumer_id="test-agent",
                backend="file", store=store,
            )
            session = sessions.Session.create(
                state_dir=state, environment=environment, consumer_id="test-agent",
                backend="file", store=store,
            )
            original = session.context()["rights_context"]["identity"]

            with patch(
                "mncs_rights_provenance.agent_context.build_agent_context",
                side_effect=AssertionError("unchanged rights inputs must reuse the projection"),
            ):
                stable = session.revalidate(workspace_root=root)
            self.assertFalse(stable["rights_context_changed"])
            self.assertTrue(session.snapshot["rights_context_inputs"]["resolution_reused"])
            self.assertEqual(session.context()["rights_context"]["identity"], original)

            profile = _governance()
            profile["profile_version"] = 2
            (root / "governance.json").write_text(json.dumps(profile) + "\n")
            changed = session.revalidate(workspace_root=root)
            self.assertTrue(changed["rights_context_changed"])
            self.assertNotEqual(session.context()["rights_context"]["identity"], original)
            changes = [event for event in session._log() if event["type"] == "rights.context.changed"]
            self.assertEqual(len(changes), 1)
            self.assertEqual(changes[0]["payload"]["previous_identity"], original)

    def test_license_resolver_change_invalidates_cached_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_sources(root)
            state = root / "state"
            store = open_store(state, "file", verify_on_open=False)
            environment = sessions.resolve_environment(
                definition={"name": "rights-test", "rights_context": _binding()},
                workspace_root=root, state_dir=state, consumer_id="test-agent",
                backend="file", store=store,
            )
            session = sessions.Session.create(
                state_dir=state, environment=environment, consumer_id="test-agent",
                backend="file", store=store,
            )
            original = session.context()["rights_context"]["identity"]
            original_resolver = session.snapshot["rights_context_inputs"]["license_resolver"]["identity"]

            with patch(
                "mncs_rights_provenance.license_resolution._CANDIDATE_REGISTRY_DIGEST",
                "sha256:" + "a" * 64,
            ):
                changed = session.revalidate(workspace_root=root)
                context = session.context()["rights_context"]

            self.assertTrue(changed["rights_context_changed"])
            self.assertNotEqual(
                session.snapshot["rights_context_inputs"]["license_resolver"]["identity"],
                original_resolver,
            )
            self.assertNotEqual(context["identity"], original)
            self.assertEqual(context["sources"]["license_resolver"]["sha256"],
                             session.snapshot["rights_context_inputs"]["license_resolver"]["identity"])
            changes = [event for event in session._log() if event["type"] == "rights.context.changed"]
            self.assertEqual(len(changes), 1)
            self.assertEqual(changes[0]["payload"]["previous_identity"], original)

    def test_concurrent_agents_keep_distinct_authorized_scopes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_sources(root)
            for repository in ("repo-a", "repo-b"):
                (root / repository / ".git").mkdir(parents=True)

            def enter(repository: str) -> dict:
                state = root / ("state-" + repository)
                store = open_store(state, "file", verify_on_open=False)
                environment = sessions.resolve_environment(
                    definition={
                        "name": "rights-test-" + repository,
                        "intent": {"goal": "work in " + repository, "repositories": [repository]},
                        "workspace_scope": {
                            "kind": "workspace", "repositories": ["repo-a", "repo-b"],
                        },
                        "rights_context": _binding([repository]),
                    },
                    workspace_root=root,
                    state_dir=state,
                    consumer_id="agent-" + repository,
                    backend="file",
                    store=store,
                )
                session = sessions.Session.create(
                    state_dir=state, environment=environment,
                    consumer_id="agent-" + repository, backend="file", store=store,
                )
                return session.context()

            with ThreadPoolExecutor(max_workers=2) as pool:
                a_future = pool.submit(enter, "repo-a")
                b_future = pool.submit(enter, "repo-b")
                a, b = a_future.result(), b_future.result()

            self.assertEqual(a["writable_repositories"], ["repo-a"])
            self.assertEqual(b["writable_repositories"], ["repo-b"])
            self.assertEqual(a["rights_context"]["governance"]["authority"]["scope"], ["repo-a"])
            self.assertEqual(b["rights_context"]["governance"]["authority"]["scope"], ["repo-b"])
            self.assertNotEqual(a["rights_context"]["identity"], b["rights_context"]["identity"])

    def test_path_escape_and_symlink_are_reviewed_without_reading_external_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root.parent / "untrusted-rights-context.json"
            outside.write_text(json.dumps(_manifest()))
            try:
                escaped = rights.load_agent_context(
                    {**_binding(), "manifest": "../untrusted-rights-context.json"}, root
                )
                self.assertIn(
                    "rights-manifest-unavailable",
                    {item["code"] for item in escaped["context"]["diagnostics"]},
                )
                self.assertEqual(escaped["context"]["rights"]["declaration_state"], "unknown")

                link = root / "RIGHTS.json"
                os.symlink(outside, link)
                (root / "governance.json").write_text(json.dumps(_governance()))
                linked = rights.load_agent_context(_binding(), root)
                self.assertIn(
                    "rights-manifest-unavailable",
                    {item["code"] for item in linked["context"]["diagnostics"]},
                )
            finally:
                outside.unlink(missing_ok=True)

    def test_unconfigured_context_does_not_create_a_parallel_default_policy(self) -> None:
        self.assertEqual(
            rights.load_agent_context(None, "."),
            {"context": None, "inputs": {"status": "unconfigured"}},
        )

    def test_incremental_event_is_in_environment_vocabulary(self) -> None:
        event = events.make(
            session_id="ses_rights", sequence=1,
            event_type="rights.context.changed", producer="mncs-rights-provenance",
        )
        self.assertEqual(event["type"], "rights.context.changed")
        self.assertIsNone(event["provider_type"])

    def test_binding_contract_rejects_external_authority_and_legacy_free_form_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_sources(root)
            for binding in (
                {**_binding(), "external_metadata_authoritative": True},
                {**_binding(), "authorized": True},
                {**_binding(), "context_budget_bytes": 1024},
            ):
                result = rights.load_agent_context(binding, root)
                self.assertEqual(result["inputs"]["status"], "review")
                self.assertEqual(result["context"]["governance"]["status"], "unavailable")
                self.assertIn(
                    "rights-context-binding-invalid",
                    {item["code"] for item in result["context"]["diagnostics"]},
                )

    def test_v1_binding_is_rejected_until_authoritative_definition_is_upgraded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_sources(root)
            binding = _binding()
            binding["schema_version"] = "mncs.environment.rights-context-binding/1"
            binding.pop("manifest_scope")

            result = rights.load_agent_context(binding, root)

        self.assertEqual(result["inputs"]["status"], "review")
        self.assertIn(
            "rights-context-binding-invalid",
            {item["code"] for item in result["context"]["diagnostics"]},
        )


if __name__ == "__main__":
    unittest.main()
