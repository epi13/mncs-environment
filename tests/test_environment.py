"""Unit, failure, concurrency, and multi-session tests for mncs-environment.

Unless marked otherwise, tests run against the canonical Store backend in
a temporary state directory, so the suite itself proves Store-backed
persistence, CAS-safe sequences, and restart/resume.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TEST_FAMILY = os.environ.get("MNCS_TEST_FAMILY")
FAMILY = Path(_TEST_FAMILY).expanduser().resolve() if _TEST_FAMILY else next(
    (candidate for candidate in ROOT.parents
     if (candidate / "mncs-language").is_dir()
     and (candidate / "mncs-compiler").is_dir()),
    ROOT.parent,
)

from mncs_env import (  # noqa: E402
    authority,
    briefing,
    capabilities,
    claims,
    cli,
    events,
    identity,
    intent,
    reconciler,
    rights,
    sessions,
    sources,
    workspace,
)
from mncs_env.session_store import (  # noqa: E402
    open_store,
    store_provider_from_environment,
    write_session_store_provider,
)


def definition(**overrides):
    base = {"name": "test-env", "intent": {"goal": "test goal"}}
    base.update(overrides)
    return base


FAST = {"verify_on_open": False}


def make_session(state_dir, **overrides):
    from mncs_env.session_store import open_store
    store = open_store(state_dir, "store", verify_on_open=False)
    env = sessions.resolve_environment(
        definition=definition(**overrides),
        workspace_root=ROOT,
        state_dir=state_dir,
        consumer_id="tester",
        store=store,
    )
    session = sessions.Session.create(
        state_dir=state_dir, environment=env, consumer_id="tester", store=store)
    session.transition("resolving", "test")
    session.transition("ready", "test")
    session.transition("active", "test")
    return session


def context_for(**overrides):
    raw = {"goal": "g", "protected_repositories": ["mncs-language"],
           "repositories": ["mncs-atlas"], "forbidden_actions": ["delete"]}
    context = authority.build_context(
        subject="tester", intent=intent.parse(raw),
        protected_repos=["mncs-language"], claim_holders={})
    context.update(overrides)
    return context


CLEAN_FACTS = {"mncs-atlas": {"clean": True, "main_branch": True, "foreign_signals": []}}
DIRTY_FACTS = {"mncs-atlas": {"clean": False, "main_branch": True, "foreign_signals": ["dirty-tree"]}}


class IdentityTests(unittest.TestCase):
    def test_stable_definition_identity(self) -> None:
        first = identity.environment_id({"name": "x", "intent": {}})
        second = identity.environment_id({"intent": {}, "name": "x"})
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("env_"))

    def test_session_ids_unique_but_reproducible(self) -> None:
        first = identity.new_session_id("env_abc", "agent-a")
        second = identity.new_session_id("env_abc", "agent-a")
        self.assertNotEqual(first, second)
        self.assertEqual(
            identity.new_session_id("env_abc", "agent-a", nonce="n"),
            identity.new_session_id("env_abc", "agent-a", nonce="n"),
        )

    def test_no_path_or_pid_in_identities(self) -> None:
        value = identity.environment_id({"path": str(ROOT), "pid": 12345})
        self.assertNotIn(str(ROOT), value)
        self.assertNotIn("12345", value)


class IntentTests(unittest.TestCase):
    def test_minimal_intent_valid(self) -> None:
        parsed = intent.parse({"goal": "do work"})
        self.assertTrue(parsed["identity"].startswith("int_"))
        self.assertEqual(parsed["protected_repositories"], [])

    def test_malformed_intent_rejected(self) -> None:
        for raw in ({}, {"goal": ""}, {"goal": 5}, {"goal": "x", "requirements": "y"},
                    {"goal": "x", "schema_version": "wrong"}):
            with self.assertRaises(intent.IntentError):
                intent.parse(raw)


class AuthorityTests(unittest.TestCase):
    def test_protected_write_denied(self) -> None:
        verdict = authority.evaluate(context_for(), action="write",
                                     target="mncs-language", session_id="ses_x",
                                     repo_facts=CLEAN_FACTS)
        self.assertEqual(verdict["verdict"], "deny")

    def test_protected_read_allowed(self) -> None:
        verdict = authority.evaluate(context_for(), action="read",
                                     target="mncs-language", session_id="ses_x")
        self.assertEqual(verdict["verdict"], "allow")

    def test_forbidden_action_denied(self) -> None:
        verdict = authority.evaluate(context_for(), action="delete",
                                     target="mncs-atlas", session_id="ses_x",
                                     repo_facts=CLEAN_FACTS)
        self.assertEqual(verdict["verdict"], "deny")

    def test_named_repo_is_not_ownership(self) -> None:
        # Intent names mncs-atlas, but dirty facts mean no implicit ownership.
        verdict = authority.evaluate(context_for(), action="write", target="mncs-atlas",
                                     session_id="ses_x", repo_facts=DIRTY_FACTS)
        self.assertEqual(verdict["verdict"], "escalate")

    def test_owned_clean_repo_mutable(self) -> None:
        verdict = authority.evaluate(context_for(), action="write", target="mncs-atlas",
                                     session_id="ses_x", repo_facts=CLEAN_FACTS)
        self.assertEqual(verdict["verdict"], "allow")

    def test_claim_grants_dirty_repo(self) -> None:
        verdict = authority.evaluate(
            context_for(), action="write", target="mncs-atlas", session_id="ses_mine",
            claims={"mncs-atlas": "ses_mine"}, repo_facts=DIRTY_FACTS)
        self.assertEqual(verdict["verdict"], "allow")

    def test_competing_claim_denies(self) -> None:
        verdict = authority.evaluate(
            context_for(), action="write", target="mncs-atlas", session_id="ses_mine",
            claims={"mncs-atlas": "ses_other"}, repo_facts=CLEAN_FACTS)
        self.assertEqual(verdict["verdict"], "deny")

    def test_releasing_claim_changes_authority(self) -> None:
        before = authority.evaluate(
            context_for(), action="write", target="mncs-atlas", session_id="ses_mine",
            claims={"mncs-atlas": "ses_other"}, repo_facts=CLEAN_FACTS)
        self.assertEqual(before["verdict"], "deny")
        after = authority.evaluate(
            context_for(), action="write", target="mncs-atlas", session_id="ses_mine",
            claims={}, repo_facts=CLEAN_FACTS)
        self.assertEqual(after["verdict"], "allow")

    def test_unknown_scope_escalates_not_allows(self) -> None:
        verdict = authority.evaluate(context_for(), action="merge",
                                     target="something-new", session_id="ses_x")
        self.assertEqual(verdict["verdict"], "escalate")

    def test_unknown_action_denied(self) -> None:
        verdict = authority.evaluate(context_for(), action="teleport",
                                     target="x", session_id="ses_x")
        self.assertEqual(verdict["verdict"], "deny")


class ClaimTests(unittest.TestCase):
    def test_acquire_conflict_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = open_store(directory, "file")
            record = claims.acquire(store, repository="r", session_id="a",
                                    consumer_id="a", basis=claims.BASIS_EXPLICIT, reason="work")
            self.assertEqual(record["status"], "held")
            with self.assertRaises(claims.ClaimConflict):
                claims.acquire(store, repository="r", session_id="b",
                               consumer_id="b", basis=claims.BASIS_EXPLICIT, reason="other")
            self.assertEqual(claims.holders(store.read_claims())["r"][0]["session_id"], "a")
            released = claims.release(store, session_id="a", repository="r")
            self.assertEqual(len(released), 1)
            self.assertEqual(claims.holders(store.read_claims()), {})
            # After release a competitor may acquire.
            second = claims.acquire(store, repository="r", session_id="b",
                                    consumer_id="b", basis=claims.BASIS_EXPLICIT, reason="now")
            self.assertEqual(second["version"], 3)

    def test_expired_claim_drops_out(self) -> None:
        stale = {"claim_id": "claim:r", "version": 1, "repository": "r",
                 "session_id": "a", "status": "held",
                 "expires_at": "2000-01-01T00:00:00+00:00"}
        live = {"claim_id": "claim:r", "version": 2, "repository": "r",
                "session_id": "b", "status": "held",
                "expires_at": "2999-01-01T00:00:00+00:00"}
        self.assertEqual(claims.holders([stale]), {})
        self.assertEqual(claims.holders([stale, live])["r"][0]["session_id"], "b")

    def test_disjoint_path_scopes_coexist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = open_store(directory, "file")
            first = claims.acquire(
                store, repository="r", session_id="a", consumer_id="a",
                basis=claims.BASIS_EXPLICIT, reason="paths-a",
                scope={"kind": "paths", "paths": ["src/a"]})
            second = claims.acquire(
                store, repository="r", session_id="b", consumer_id="b",
                basis=claims.BASIS_EXPLICIT, reason="paths-b",
                scope={"kind": "paths", "paths": ["src/b"]})
            self.assertNotEqual(first["claim_id"], second["claim_id"])
            self.assertEqual(len(claims.holders(store.read_claims())["r"]), 2)
            with self.assertRaises(claims.ClaimConflict):
                claims.acquire(
                    store, repository="r", session_id="c", consumer_id="c",
                    basis=claims.BASIS_EXPLICIT, reason="overlap",
                    scope={"kind": "paths", "paths": ["src/a/deep"]})

    def test_whole_repo_conflicts_with_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = open_store(directory, "file")
            claims.acquire(
                store, repository="r", session_id="a", consumer_id="a",
                basis=claims.BASIS_EXPLICIT, reason="paths",
                scope={"kind": "paths", "paths": ["src"]})
            with self.assertRaises(claims.ClaimConflict):
                claims.acquire(store, repository="r", session_id="b",
                               consumer_id="b", basis=claims.BASIS_EXPLICIT,
                               reason="whole")

    def test_dirty_checkout_requires_adoption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = open_store(directory, "file")
            facts = {"dirty": True, "head": "abc", "branch": "main", "foreign_signals": []}
            with self.assertRaises(claims.ClaimAdoptionRequired):
                claims.acquire(store, repository="r", session_id="a",
                               consumer_id="a", basis=claims.BASIS_EXPLICIT,
                               reason="takeover", checkout_facts=facts)
            adopted = claims.acquire(
                store, repository="r", session_id="a", consumer_id="a",
                basis=claims.BASIS_ADOPTION, reason="documented takeover",
                checkout_facts=facts)
            self.assertEqual(adopted["provenance"]["adopted_head"], "abc")
            self.assertTrue(adopted["provenance"]["adopted_dirty"])

    def test_transfer_moves_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = open_store(directory, "file")
            record = claims.acquire(store, repository="r", session_id="a",
                                    consumer_id="a", basis=claims.BASIS_EXPLICIT,
                                    reason="work")
            moved = claims.transfer(store, claim_id=record["claim_id"],
                                    from_session="a", to_session="b",
                                    to_consumer="b", reason="handoff")
            self.assertEqual(moved["basis"], claims.BASIS_TRANSFER)
            self.assertEqual(moved["session_id"], "b")
            live = claims.active_claims(store.read_claims())
            self.assertEqual(live[record["claim_id"]]["session_id"], "b")

    def test_liveness_derives_from_activity(self) -> None:
        record = {"status": "held", "expires_at": "2999-01-01T00:00:00+00:00"}
        self.assertEqual(claims.liveness(record, None), "stale")
        self.assertEqual(
            claims.liveness(record, claims.utcnow()), "active")
        self.assertEqual(
            claims.liveness({"status": "released", "expires_at": record["expires_at"]},
                            claims.utcnow()),
            "released")
        self.assertEqual(
            claims.liveness({"status": "held", "expires_at": "2000-01-01T00:00:00+00:00"},
                            claims.utcnow()),
            "expired")

    def test_legacy_records_migrate_to_repository_scope(self) -> None:
        legacy = {"claim_id": "claim:r", "version": 1, "repository": "r",
                  "session_id": "a", "status": "held",
                  "expires_at": "2999-01-01T00:00:00+00:00"}
        live = claims.active_claims([legacy])
        self.assertEqual(live["claim:r"]["scope"]["kind"], "repository")


class WorkspaceTests(unittest.TestCase):
    def test_discovers_real_repositories(self) -> None:
        view = workspace.discover_workspace(FAMILY)
        names = {repo["name"] for repo in view["repositories"]}
        if (FAMILY / "mncs-atlas").is_dir():
            self.assertIn("mncs-atlas", names)
        else:
            self.assertIn("mncs-compiler", names)
        self.assertIn("mncs-language", names)
        for repo in view["repositories"]:
            self.assertIn("branch", repo)
            self.assertIn("dirty", repo)

    def test_foreign_signals(self) -> None:
        signals = workspace.foreign_work_signals(
            {"name": "x", "branch": "agent-work", "dirty": True, "dirty_files": ["a"],
             "worktrees": [{}, {"path": "/tmp/w", "branch": "other"}]})
        kinds = {signal["kind"] for signal in signals}
        self.assertEqual(kinds, {"foreign-branch", "dirty-tree", "linked-worktree"})


class CapabilityTests(unittest.TestCase):
    def test_discovers_real_declarations(self) -> None:
        test_root = FAMILY / "mncs-test"
        if not (test_root / ".mncs" / "project.json").is_file():
            worktrees = test_root / ".worktrees"
            candidates = sorted(worktrees.glob("*/.mncs/project.json"))
            if candidates:
                test_root = candidates[0].parent.parent
        if test_root != FAMILY / "mncs-test" and test_root.is_dir():
            bindings = capabilities.discover_capabilities(
                FAMILY, repository_roots={"mncs-test": test_root},
            )
        else:
            bindings = capabilities.discover_capabilities(FAMILY)
        by_provider = {binding["provider"] for binding in bindings}
        self.assertIn("mncs-test", by_provider)
        self.assertTrue(all(binding["binding_id"].startswith("cap_") for binding in bindings))

    def test_selected_checkout_is_the_complete_capability_binding_set(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for checkout, repository in (("ordinary", "stale-provider"),
                                         ("managed", "campaign-provider")):
                repo = root / checkout
                (repo / ".mncs").mkdir(parents=True)
                (repo / "provider.py").write_text("print('provider')\n", encoding="utf-8")
                (repo / ".mncs" / "project.json").write_text(json.dumps({
                    "repository": repository,
                    "contracts": {"provides": [{
                        "contract": "example.capability/1",
                        "fingerprint_sources": ["provider.py"],
                    }]},
                }), encoding="utf-8")

            bindings = capabilities.discover_capabilities(
                root,
                repository_roots={"campaign-provider": root / "managed"},
                checkout_facts={"campaign-provider": {
                    "path": "managed", "head": "current-revision", "branch": "campaign/test",
                    "clean": True,
                }},
            )

            self.assertEqual([item["provider"] for item in bindings], ["campaign-provider"])
            self.assertEqual(bindings[0]["provider_root"], str(root / "managed"))
            self.assertEqual(
                bindings[0]["provenance"]["checkout"]["head"], "current-revision"
            )

    def test_semantic_contract_binds_selected_executable_and_fixed_argv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "managed"
            executable = repo / "tools" / "fixture-cli"
            executable.parent.mkdir(parents=True)
            executable.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$@\"\n", encoding="utf-8"
            )
            executable.chmod(0o755)
            (repo / "family-semantic-contracts-v1.json").write_text(json.dumps({
                "schema_version": "commons.mncs.semantic-contract-declarations/v1",
                "repository_id": "campaign-provider",
                "revision": "1",
                "provides": [{
                    "contract_identity": "fixture.impact/1",
                    "contract_revision": "7",
                    "exported_identity": "fixture:impact",
                    "canonical_entrypoint": "fixture-cli impact",
                    "invocation": {
                        "kind": "executable",
                        "path": "tools/fixture-cli",
                        "fixed_argv": ["impact"],
                    },
                    "effects": ["read"],
                }],
                "consumes": [],
            }), encoding="utf-8")
            facts = {"campaign-provider": {
                "path": str(repo), "head": "exact-head", "branch": "campaign/test",
                "clean": True,
            }}

            bindings = capabilities.discover_capabilities(
                root,
                repository_roots={"campaign-provider": repo},
                checkout_facts=facts,
            )
            binding = next(item for item in bindings if item["capability"] == "fixture.impact/1")

            self.assertEqual(binding["address"], str(executable.resolve()))
            self.assertEqual(binding["fixed_argv"], ["impact"])
            self.assertEqual(binding["provenance"]["checkout"]["head"], "exact-head")
            result = capabilities.invoke(binding, ["source.mncs"])

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["stdout"].splitlines(), ["impact", "source.mncs"])

    def test_manifest_test_binds_fixed_command_to_selected_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stale = root / "ordinary"
            selected = root / "managed"
            fake_bin = root / "bin"
            for repo in (stale, selected):
                (repo / ".mncs").mkdir(parents=True)
            fake_bin.mkdir()
            cargo = fake_bin / "cargo"
            cargo.write_text(
                "#!/bin/sh\nprintf '%s\\n' \"$PWD\" \"$@\" \"$MNCS_TEST_MARKER\"\n",
                encoding="utf-8",
            )
            cargo.chmod(0o755)
            (selected / ".mncs" / "project.json").write_text(json.dumps({
                "repository": "mncs-language",
                "contracts": {
                    "provides": [],
                    "tests": [{
                        "test": "language-tests-mncs-embed",
                        "covers": ["compiler-runtime"],
                        "command": {
                            "argv": ["cargo", "test", "--package", "mncs-embed"],
                            "timeout_seconds": 42,
                            "environment": {"MNCS_TEST_MARKER": "from-manifest"},
                        },
                    }],
                },
            }), encoding="utf-8")

            with mock.patch.object(
                capabilities.shutil, "which",
                side_effect=lambda name: str(cargo) if name == "cargo" else None,
            ):
                bindings = capabilities.discover_capabilities(
                    root,
                    repository_roots={"mncs-language": selected},
                    checkout_facts={"mncs-language": {
                        "path": str(selected), "head": "campaign-head",
                        "branch": "campaign/language", "clean": True,
                    }},
                )

            binding = next(
                item for item in bindings
                if item["capability"] == "mncs-language:test/language-tests-mncs-embed"
            )
            self.assertEqual(binding["address"], str(cargo))
            self.assertEqual(binding["fixed_argv"], ["test", "--package", "mncs-embed"])
            self.assertEqual(binding["fixed_env"], {"MNCS_TEST_MARKER": "from-manifest"})
            self.assertEqual(binding["effects"], ["write"])
            self.assertEqual(binding["timeout_seconds"], 42)
            self.assertEqual(binding["working_directory"], str(selected.resolve()))
            self.assertEqual(binding["provenance"]["checkout"]["head"], "campaign-head")
            result = capabilities.invoke(binding, [])
            self.assertEqual(result["status"], "ok")
            self.assertEqual(
                result["stdout"].splitlines(),
                [str(selected.resolve()), "test", "--package", "mncs-embed", "from-manifest"],
            )
            overridden = capabilities.invoke(binding, [], env={"MNCS_TEST_MARKER": "explicit"})
            self.assertEqual(overridden["stdout"].splitlines()[-1], "explicit")

    def test_manifest_test_rejects_unsafe_fixed_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "selected"
            (repo / ".mncs").mkdir(parents=True)
            (repo / ".mncs" / "project.json").write_text(json.dumps({
                "repository": "mncs-language",
                "contracts": {"provides": [], "tests": [{
                    "test": "unsafe-env",
                    "command": {"argv": ["python3", "-m", "unittest"],
                                "environment": {"PATH": "/tmp/untrusted"}},
                }]},
            }), encoding="utf-8")
            bindings = capabilities.discover_capabilities(root, repository_roots={"mncs-language": repo})
            self.assertFalse(any(item["capability"].endswith("test/unsafe-env") for item in bindings))

    def test_manifest_test_toolchain_binds_only_the_selected_repository_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stale = root / "mncs-language"
            selected = root / "language-worktree"
            repo = root / "commons-worktree"
            for path in (stale, selected, repo):
                path.mkdir()
            (repo / ".mncs").mkdir()
            (repo / ".mncs" / "project.json").write_text(json.dumps({
                "repository": "mncs-commons",
                "contracts": {"provides": [], "tests": [{
                    "test": "uses-selected-language",
                    "command": {
                        "argv": ["python3", "-c", "import os; print(os.environ['MNCS_LANGUAGE_ROOT'])"],
                        "toolchain": {"repository": "mncs-language", "path": "."},
                        "toolchain_env": "MNCS_LANGUAGE_ROOT",
                    },
                }]},
            }), encoding="utf-8")
            selected_roots = {"mncs-commons": repo, "mncs-language": selected}
            bindings = capabilities.discover_capabilities(root, repository_roots=selected_roots)
            binding = next(item for item in bindings if item["capability"].endswith("test/uses-selected-language"))
            self.assertEqual(binding["toolchain_address"], str(selected.resolve()))
            result = capabilities.invoke(binding, [])
            self.assertEqual(result["stdout"].strip(), str(selected.resolve()))

            missing_language_roots = {"mncs-commons": repo}
            unavailable = capabilities.discover_capabilities(root, repository_roots=missing_language_roots)
            self.assertFalse(any(
                item["capability"].endswith("test/uses-selected-language")
                for item in unavailable
            ))

    def test_toolchain_descriptor_binds_the_selected_repository_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stale_language = root / "mncs-language"
            stale_language.mkdir()
            selected_language = root / "mncs-language-worktree"
            selected_language.mkdir()
            commons = root / "commons-worktree"
            commons.mkdir()
            (commons / "pressure_cli.py").write_text("print('pressure')\n", encoding="utf-8")
            (commons / "family-semantic-contracts-v1.json").write_text(json.dumps({
                "repository_id": "mncs-commons",
                "provides": [{
                    "contract_identity": "mncs.pressure-registry/1",
                    "contract_revision": "1",
                    "exported_identity": "mncs-commons:pressure-registry",
                    "canonical_entrypoint": "mncs-commons pressure",
                    "effects": ["write"],
                    "invocation": {
                        "kind": "python",
                        "path": "pressure_cli.py",
                        "toolchain": {"repository": "mncs-language", "path": "."},
                        "toolchain_env": "MNCS_LANGUAGE_CHECKOUT",
                    },
                }],
            }), encoding="utf-8")

            bindings = capabilities.discover_capabilities(
                root,
                repository_roots={
                    "mncs-commons": commons,
                    "mncs-language": selected_language,
                },
            )

            binding = next(
                item for item in bindings
                if item["capability"] == "mncs.pressure-registry/1"
            )
            self.assertEqual(binding["toolchain_address"], str(selected_language))
            self.assertEqual(binding["toolchain_env"], "MNCS_LANGUAGE_CHECKOUT")
            self.assertEqual(binding["effects"], ["write"])
            self.assertNotEqual(binding["toolchain_address"], str(stale_language))

    def test_declared_toolchain_never_falls_back_to_ambient_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            language = root / "mncs-language"
            test_repo = root / "mncs-test"
            language.mkdir()
            (test_repo / "bin").mkdir(parents=True)
            executable = test_repo / "bin" / "mncs-test"
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
            (test_repo / "family-semantic-contracts-v1.json").write_text(json.dumps({
                "repository_id": "mncs-test",
                "provides": [{
                    "contract_identity": "mncs.test-result/1",
                    "contract_revision": "1",
                    "canonical_entrypoint": "mncs test",
                    "invocation": {
                        "kind": "executable",
                        "path": "bin/mncs-test",
                        "toolchain": {
                            "repository": "mncs-language",
                            "path": "target/release/mncs",
                        },
                        "toolchain_env": "MNCS",
                    },
                }],
            }), encoding="utf-8")

            bindings = capabilities.discover_capabilities(
                root,
                repository_roots={"mncs-test": test_repo, "mncs-language": language},
            )
            binding = next(item for item in bindings
                           if item["capability"] == "mncs.test-result/1")
            self.assertIsNone(binding["address"])
            self.assertIsNone(binding["toolchain_address"])

    def test_verification_executor_binds_from_selected_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stale = root / "ordinary"
            selected = root / "campaign"
            for repo in (stale, selected):
                (repo / ".mncs").mkdir(parents=True)
                (repo / "tools").mkdir()
                (repo / "tools" / "check.py").write_text(
                    "import json, sys; print(json.dumps(sys.argv[1:]))\n",
                    encoding="utf-8",
                )
                (repo / "tools" / "check.sh").write_text(
                    "printf '%s\\n' \"$@\"\n", encoding="utf-8")
                (repo / ".mncs" / "project.json").write_text(json.dumps({
                    "repository": "mncs-compiler",
                    "verification": {"obligation_inventory": ".mncs/verification.json"},
                }), encoding="utf-8")
                (repo / ".mncs" / "verification.json").write_text(json.dumps({
                    "repository": "mncs-compiler",
                    "revision": 3,
                    "obligations": [{
                        "identity": "mncs-compiler.example-check",
                        "executor": {
                            "provider": "mncs-compiler",
                            "kind": "external_integration",
                            "entrypoint": "python3 tools/check.py stable",
                            "argv": ["python3", "tools/check.py", "stable"],
                            "working_directory": ".",
                            "timeout_seconds": 42,
                        },
                    }, {
                        "identity": "mncs-compiler.example-bootstrap",
                        "executor": {
                            "provider": "mncs-compiler",
                            "kind": "external_integration",
                            "entrypoint": "bash tools/check.sh stable",
                            "argv": ["bash", "tools/check.sh", "stable"],
                            "working_directory": ".",
                            "timeout_seconds": 43,
                        },
                    }],
                }), encoding="utf-8")

            bindings = capabilities.discover_capabilities(
                root,
                repository_roots={"mncs-compiler": selected},
                checkout_facts={"mncs-compiler": {
                    "path": str(selected), "head": "campaign-head", "clean": True,
                }},
            )
            capability = "mncs-compiler:verification-executor/mncs-compiler.example-check"
            binding = next(item for item in bindings if item["capability"] == capability)
            self.assertEqual(binding["provider_root"], str(selected.resolve()))
            self.assertEqual(binding["address"], f"python:{selected.resolve()}/tools/check.py")
            self.assertEqual(binding["timeout_seconds"], 42)

            result = capabilities.invoke(binding, ["campaign"])

            self.assertEqual(result["status"], "ok")
            self.assertEqual(json.loads(result["stdout"]), ["stable", "campaign"])

            bash_capability = "mncs-compiler:verification-executor/mncs-compiler.example-bootstrap"
            bash_binding = next(item for item in bindings if item["capability"] == bash_capability)
            self.assertEqual(bash_binding["address"], shutil.which("bash"))
            self.assertEqual(
                bash_binding["fixed_argv"],
                [str(selected.resolve() / "tools" / "check.sh"), "stable"],
            )
            self.assertEqual(bash_binding["timeout_seconds"], 43)
            bash_result = capabilities.invoke(bash_binding, ["campaign"])
            self.assertEqual(bash_result["status"], "ok")
            self.assertEqual(bash_result["stdout"].splitlines(), ["stable", "campaign"])

    def test_probe_marks_availability(self) -> None:
        probed = capabilities.probe_availability(
            capabilities.bind(provider="p", capability="c", contract_revision="1",
                              entrypoint="e", address="/bin/sh"))
        self.assertEqual(probed["availability"]["status"], "available")
        missing = capabilities.probe_availability(
            capabilities.bind(provider="p", capability="c", contract_revision="1",
                              entrypoint="e", address="/nonexistent"))
        self.assertEqual(missing["availability"]["status"], "unavailable")

    def test_invoke_envelope(self) -> None:
        binding = capabilities.probe_availability(
            capabilities.bind(provider="p", capability="c", contract_revision="1",
                              entrypoint="e", address="/bin/echo"))
        result = capabilities.invoke(binding, ["hello"])
        self.assertEqual(result["status"], "ok")
        self.assertIn("hello", result["stdout"])
        with self.assertRaises(capabilities.CapabilityError):
            capabilities.invoke(
                capabilities.bind(provider="p", capability="c", contract_revision="1",
                                  entrypoint="e", address=None), [])

    def test_output_is_bounded(self) -> None:
        binding = capabilities.probe_availability(
            capabilities.bind(provider="p", capability="c", contract_revision="1",
                              entrypoint="e", address="/bin/sh"))
        result = capabilities.invoke(
            binding, ["-c", "yes x | head -c 500000"], output_limit_bytes=65536)
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["stdout"].encode("utf-8")), 32768 + 1024)

    def test_output_limit_can_be_increased_within_the_bound(self) -> None:
        binding = capabilities.probe_availability(
            capabilities.bind(provider="p", capability="c", contract_revision="1",
                              entrypoint="e", address="/bin/sh"))
        result = capabilities.invoke(
            binding, ["-c", "yes x | head -c 100000"], output_limit_bytes=262144)
        self.assertFalse(result["truncated"])
        self.assertEqual(len(result["stdout"].encode("utf-8")), 100000)
        with self.assertRaises(capabilities.CapabilityError):
            capabilities.invoke(
                binding, ["-c", "echo unreachable"],
                output_limit_bytes=capabilities.MAX_OUTPUT_LIMIT_BYTES + 1)

    @unittest.skipUnless(sys.platform.startswith("linux"), "POSIX process-group behavior")
    def test_timeout_keeps_partial_output_and_kills_child_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "child-survived-timeout"
            child = (
                "import pathlib,time; time.sleep(1.3); "
                f"pathlib.Path({str(marker)!r}).write_text('alive'); time.sleep(10)"
            )
            parent = (
                "import subprocess,sys,time; "
                f"subprocess.Popen([sys.executable,'-c',{child!r}], "
                "stdout=sys.stdout,stderr=sys.stderr); "
                "print('started',flush=True); time.sleep(10)"
            )
            binding = capabilities.bind(
                provider="p", capability="c", contract_revision="1",
                entrypoint="python -c <bounded child fixture>", address=sys.executable,
            )
            started = time.monotonic()
            result = capabilities.invoke(binding, ["-c", parent], timeout_seconds=1)
            elapsed = time.monotonic() - started

            self.assertEqual(result["status"], "timeout")
            self.assertIn("started", result["stdout"])
            self.assertLess(elapsed, 2.5)
            time.sleep(0.6)
            self.assertFalse(marker.exists(), "timed-out child process survived its group")

    def test_descriptor_invocation_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "demo"
            (repo / "bin").mkdir(parents=True)
            exe = repo / "bin" / "run.sh"
            exe.write_text("#!/bin/sh\n")
            tool = root / "toolchain" / "mncs"
            tool.parent.mkdir(parents=True)
            tool.write_text("#!/bin/sh\n")
            entry = {"canonical_entrypoint": "demo run",
                     "invocation": {"kind": "executable", "path": "bin/run.sh",
                                    "toolchain": "toolchain/mncs",
                                    "toolchain_env": "MNCS"}}
            resolved = capabilities.descriptor_invocation(entry, repo, root)
            self.assertEqual(resolved["addressing"], "descriptor")
            self.assertEqual(resolved["address"], str(exe))
            self.assertEqual(resolved["toolchain_address"], str(tool))
            self.assertEqual(resolved["toolchain_env"], "MNCS")
            missing = capabilities.descriptor_invocation(
                {"invocation": {"kind": "executable", "path": "nope"}}, repo, root)
            self.assertEqual(missing["addressing"], "none")
            self.assertIsNone(missing["address"])
            plain = capabilities.descriptor_invocation({}, repo, root)
            self.assertEqual(plain["addressing"], "none")

    def test_toolchain_env_exported(self) -> None:
        binding = capabilities.probe_availability(
            capabilities.bind(provider="p", capability="c", contract_revision="1",
                              entrypoint="e", address="/bin/sh",
                              toolchain_address="/opt/tool", toolchain_env="DEMO_TOOL"))
        result = capabilities.invoke(binding, ["-c", "echo $DEMO_TOOL"])
        self.assertIn("/opt/tool", result["stdout"])
        override = capabilities.invoke(
            binding, ["-c", "echo $DEMO_TOOL"], env={"DEMO_TOOL": "/other"})
        self.assertIn("/other", override["stdout"])


class CampaignSelectionTests(unittest.TestCase):
    def test_provider_capability_selects_exact_clean_checkout_closure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider_root = root / "mncs-control-mcp" / ".worktrees" / "control"
            compiler_root = root / "mncs-compiler" / ".worktrees" / "compiler"
            provider_root.mkdir(parents=True)
            compiler_root.mkdir(parents=True)
            binding = capabilities.bind(
                provider="mncs-control-mcp",
                capability="mncs-control-mcp:workspace.worktree.prepare",
                contract_revision="1",
                entrypoint="provider-owned",
                address="/provider/worktree_cli.py",
                provider_root=str(provider_root),
            )
            provider_record = {
                "path": str(provider_root), "head": "provider-revision",
                "branch": "campaign/control", "dirty": False,
            }
            response = {"selected_checkouts": [{
                "repository": "mncs-compiler",
                "path": "mncs-compiler/.worktrees/compiler",
                "branch": "campaign/compiler",
                "head": "authoritative-revision",
                "clean": True,
                "source_ref": "origin/main",
                "authoritative_head": "authoritative-revision",
            }]}
            definition_ = {
                "workspace_provider": {
                    "repository": "mncs-control-mcp",
                    "checkout": ".worktrees/control",
                    "capability": "mncs-control-mcp:workspace.worktree.prepare",
                    "revision": "provider-revision",
                },
                "managed_checkouts": [{
                    "repository": "mncs-compiler", "name": "compiler",
                    "branch": "campaign/compiler", "source_ref": "origin/main",
                }],
            }
            with (
                mock.patch.object(capabilities, "discover_capabilities", return_value=[binding]) as discover,
                mock.patch.object(capabilities, "probe_availability", side_effect=lambda item: {
                    **item, "availability": {"status": "available"}
                }),
                mock.patch.object(capabilities, "invoke", return_value={
                    "status": "ok", "stdout": json.dumps(response), "stderr": ""
                }) as invoke,
            ):
                roots, facts = sessions._provider_managed_checkouts(
                    definition_, root,
                    {"repositories": [provider_record]},
                )

            self.assertEqual(roots["mncs-compiler"], compiler_root)
            self.assertEqual(roots["mncs-control-mcp"], provider_root)
            self.assertEqual(facts["mncs-compiler"]["head"], "authoritative-revision")
            self.assertEqual(facts["mncs-control-mcp"]["head"], "provider-revision")
            self.assertEqual(discover.call_args.kwargs["repository_roots"], {
                "mncs-control-mcp": provider_root,
            })
            self.assertEqual(invoke.call_args.args[0]["provider_root"], str(provider_root))

    def test_provider_refuses_dirty_or_unpinned_provider_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider_root = root / "mncs-control-mcp" / ".worktrees" / "control"
            provider_root.mkdir(parents=True)
            definition_ = {
                "workspace_provider": {
                    "repository": "mncs-control-mcp", "checkout": ".worktrees/control",
                    "capability": "mncs-control-mcp:workspace.worktree.prepare",
                    "revision": "expected",
                },
                "managed_checkouts": [{
                    "repository": "mncs-compiler", "name": "compiler",
                    "branch": "campaign/compiler", "source_ref": "origin/main",
                }],
            }
            with mock.patch.object(capabilities, "discover_capabilities") as discover:
                with self.assertRaisesRegex(ValueError, "provider checkout is dirty"):
                    sessions._provider_managed_checkouts(
                        definition_, root,
                        {"repositories": [{
                            "path": str(provider_root), "head": "expected",
                            "branch": "campaign/control", "dirty": True,
                        }]},
                    )
                discover.assert_not_called()


TOOLCHAIN_SOURCE = FAMILY / "mncs-test" / "tests" / "self_suite.mncs"
TOOLCHAIN_BIN = FAMILY / "mncs-test" / "bin" / "mncs-test"
LANGUAGE_LIB = FAMILY / "mncs-language" / "library"
TEST_NATIVE_LIB = FAMILY / "mncs-test" / "native"


@unittest.skipUnless(TOOLCHAIN_SOURCE.is_file() and TOOLCHAIN_BIN.is_file()
                     and LANGUAGE_LIB.is_dir() and TEST_NATIVE_LIB.is_dir(),
                     "family toolchain checkout required")
class ToolchainTests(unittest.TestCase):
    def test_mncs_test_binding_is_descriptor_addressed(self) -> None:
        bindings = [capabilities.probe_availability(binding)
                    for binding in capabilities.discover_capabilities(FAMILY)]
        matches = [b for b in bindings if b["capability"] == "mncs.test-result/1"]
        self.assertTrue(matches, "mncs.test-result/1 not discovered")
        binding = matches[0]
        self.assertEqual(binding["provenance"].get("addressing"), "descriptor")
        self.assertEqual(binding["availability"]["status"], "available")
        self.assertTrue(Path(str(binding["toolchain_address"])).is_file())

    def test_session_invokes_real_toolchain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            scoped = Path(directory) / "workspace"
            scoped.mkdir()
            for repository in ("mncs-test", "mncs-language"):
                subprocess.run(["git", "clone", "--shared", "--quiet",
                                str(FAMILY / repository), str(scoped / repository)], check=True)
            binary = scoped / "mncs-language" / "target" / "release" / "mncs"
            binary.parent.mkdir(parents=True)
            shutil.copy2(FAMILY / "mncs-language" / "target" / "release" / "mncs", binary)
            store = open_store(state, "store", verify_on_open=False)
            env = sessions.resolve_environment(
                definition=definition(), workspace_root=scoped,
                state_dir=state, consumer_id="tester", store=store)
            session = sessions.Session.create(
                state_dir=state, environment=env, consumer_id="tester", store=store)
            session.transition("resolving", "test")
            session.transition("ready", "test")
            session.transition("active", "test")
            result = session.invoke(
                "mncs.test-result/1",
                [str(TOOLCHAIN_SOURCE), "--library", str(LANGUAGE_LIB),
                 "--library", str(TEST_NATIVE_LIB), "--format", "text"],
                timeout_seconds=180)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["returncode"], 0)
            self.assertIn("PASS", result["stdout"])

    def test_compiler_binding_visible_with_verification_inventory(self) -> None:
        bindings = capabilities.discover_capabilities(FAMILY)
        matches = [b for b in bindings if b["provider"] == "mncs-compiler"]
        self.assertTrue(matches, "mncs-compiler binding not discovered")
        manifest = next(
            (b for b in matches
             if b["capability"] == "mncs-compiler:compiler-next-generation"),
            None,
        )
        self.assertIsNotNone(manifest, "mncs-compiler manifest binding missing")
        assert manifest is not None
        self.assertEqual(manifest["provenance"].get("manifest_tests", []), [])
        capability = (
            "mncs-compiler:verification-executor/"
            "mncs-compiler.frontend-differential"
        )
        binding = next(
            (b for b in matches if b["capability"] == capability), None)
        self.assertIsNotNone(binding, f"{capability} not discovered")
        assert binding is not None
        compiler_root = (FAMILY / "mncs-compiler").resolve()
        self.assertEqual(binding["provider"], "mncs-compiler")
        self.assertEqual(binding["entrypoint"], "python3 tools/test_frontend.py")
        self.assertEqual(
            binding["address"],
            f"python:{compiler_root / 'tools' / 'test_frontend.py'}")
        self.assertIsNone(binding["toolchain_address"])
        self.assertIsNone(binding["toolchain_env"])
        self.assertEqual(binding["timeout_seconds"], 300)
        self.assertEqual(binding["working_directory"], str(compiler_root))
        self.assertEqual(binding["provider_root"], str(compiler_root))
        self.assertEqual(
            binding["provenance"].get("addressing"),
            "declared-verification-inventory")
        self.assertEqual(
            binding["provenance"].get("source"),
            ".mncs/verification-obligations.json")
        self.assertEqual(
            binding["provenance"].get("obligation_identity"),
            "mncs-compiler.frontend-differential")


class SessionTests(unittest.TestCase):
    def test_provision_checkouts_extends_authority_and_binds_exact_provider_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            provider_root = root / "mncs-control-mcp" / ".worktrees" / "campaign-control"
            selected_root = root / "mncs-fabric" / ".worktrees" / "campaign-fabric"
            provider_root.mkdir(parents=True)
            selected_root.mkdir(parents=True)
            session = make_session(
                root / "state",
                intent={"goal": "extend checkout closure", "repositories": ["mncs-control-mcp"]},
            )
            session.snapshot["workspace"] = {"root": str(root), "repositories": []}
            session.snapshot["selected_checkouts"] = {
                "mncs-control-mcp": {
                    "repository": "mncs-control-mcp",
                    "path": str(provider_root),
                    "branch": "campaign/control",
                    "head": "control-head",
                    "clean": True,
                }
            }
            capability = "mncs-control-mcp:workspace.worktree.prepare"
            provider_binding = capabilities.bind(
                provider="mncs-control-mcp",
                capability=capability,
                contract_revision="2",
                entrypoint="mncs-control-worktrees prepare",
                address="python:" + str(provider_root / "worktree_cli.py"),
                effects=["write"],
                provider_root=str(provider_root),
                provenance={"checkout": {"path": str(provider_root), "branch": "campaign/control"}},
            )
            provider_binding["availability"] = {"status": "available"}
            session.snapshot["bindings"] = [provider_binding]
            provider_result = {
                "selected_checkouts": [{
                    "repository": "mncs-fabric",
                    "path": "mncs-fabric/.worktrees/campaign-fabric",
                    "branch": "campaign/fabric",
                    "head": "fabric-head",
                    "clean": True,
                    "source_ref": "origin/main",
                    "authoritative_head": "fabric-head",
                }]
            }
            observed = {
                "root": str(root),
                "scan": {"status": "complete"},
                "repositories": [
                    {"name": "mncs-control-mcp@campaign-control", "path": str(provider_root),
                     "head": "control-head", "branch": "campaign/control", "dirty": False,
                     "dirty_files": [], "dirty_truncated": False},
                    {"name": "mncs-fabric@campaign-fabric", "path": str(selected_root),
                     "head": "fabric-head", "branch": "campaign/fabric", "dirty": False,
                     "dirty_files": [], "dirty_truncated": False},
                ],
            }
            output_binding = capabilities.bind(
                provider="mncs-fabric",
                capability="mncs-fabric:test/fabric-suite",
                contract_revision="3",
                entrypoint="python3 -m pytest",
                address="python:" + str(selected_root / "tests.py"),
                provider_root=str(selected_root),
                provenance={"checkout": {"path": str(selected_root), "head": "fabric-head"}},
            )
            with (
                mock.patch.object(session, "invoke", return_value={
                    "status": "ok", "returncode": 0, "stdout": json.dumps(provider_result),
                    "stderr": "",
                }) as invoke,
                mock.patch.object(sessions.workspace_module, "discover_workspace", return_value=observed),
                mock.patch.object(sessions.capabilities_module, "discover_capabilities",
                                  return_value=[output_binding]),
                mock.patch.object(sessions.capabilities_module, "probe_availability",
                                  side_effect=lambda item: {**item, "availability": {"status": "available"}}),
            ):
                result = session.provision_checkouts(
                    capability,
                    [{"repository": "mncs-fabric", "name": "campaign-fabric",
                      "branch": "campaign/fabric", "source_ref": "origin/main"}],
                )

            invoke.assert_called_once()
            self.assertIn("mncs-fabric", session.snapshot["authority"]["writable"])
            self.assertEqual(
                session.snapshot["selected_checkouts"]["mncs-fabric"]["path"],
                "mncs-fabric/.worktrees/campaign-fabric",
            )
            self.assertTrue(any(
                binding["provider_root"] == str(selected_root)
                and binding["capability"] == "mncs-fabric:test/fabric-suite"
                for binding in result["bindings"]
            ))

    def test_provision_checkouts_rejects_provider_from_unselected_workspace_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected_provider = root / "mncs-control-mcp" / ".worktrees" / "campaign-control"
            stale_provider = root / "mncs-control-mcp"
            selected_provider.mkdir(parents=True)
            stale_provider.mkdir(exist_ok=True)
            session = make_session(root / "state")
            session.snapshot["workspace"] = {"root": str(root), "repositories": []}
            session.snapshot["selected_checkouts"] = {
                "mncs-control-mcp": {"path": str(selected_provider), "head": "selected-head"}
            }
            capability = "mncs-control-mcp:workspace.worktree.prepare"
            binding = capabilities.bind(
                provider="mncs-control-mcp", capability=capability, contract_revision="2",
                entrypoint="mncs-control-worktrees prepare", address="python:unused",
                provider_root=str(stale_provider),
            )
            binding["availability"] = {"status": "available"}
            session.snapshot["bindings"] = [binding]
            with mock.patch.object(session, "invoke") as invoke:
                with self.assertRaises(sessions.AuthorityDenied):
                    session.provision_checkouts(
                        capability,
                        [{"repository": "mncs-fabric", "name": "campaign-fabric",
                          "branch": "campaign/fabric"}],
                    )
            invoke.assert_not_called()

    def test_observe_workspace_reports_dirty_selected_checkout_without_head_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "mncs-language" / ".worktrees" / "campaign"
            checkout.mkdir(parents=True)
            session = make_session(root / "state")
            repository = "mncs-language"
            selected = {
                "repository": repository,
                "path": str(checkout),
                "branch": "campaign/parity",
                "head": "same-head",
                "clean": True,
            }
            clean_repo = {
                "name": "mncs-language@campaign", "path": str(checkout),
                "head": "same-head", "branch": "campaign/parity",
                "dirty": False, "dirty_files": [], "dirty_truncated": False,
            }
            clean_workspace = {"root": str(root), "repositories": [clean_repo]}
            session.snapshot["selected_checkouts"] = {repository: selected}
            session.snapshot["workspace"] = clean_workspace
            session.snapshot["workspace_heads"] = {repository: "same-head"}
            session.snapshot["workspace_facts"] = sessions._workspace_change_facts(
                clean_workspace, {repository: selected})

            dirty_repo = {
                **clean_repo, "dirty": True,
                "dirty_files": [" M crates/mncs-embed/src/lib.rs"],
            }
            with mock.patch.object(
                sessions.workspace_module, "discover_workspace",
                return_value={"root": str(root), "repositories": [dirty_repo], "scan": {"status": "complete"}},
            ):
                changes = session.observe_workspace(root)

            self.assertEqual(len(changes), 1)
            self.assertEqual(changes[0]["type"], "workspace.changed")
            self.assertEqual(changes[0]["payload"]["repository"], repository)
            self.assertEqual(changes[0]["payload"]["previous_head"], "same-head")
            self.assertEqual(changes[0]["payload"]["current_head"], "same-head")
            self.assertIn("clean", changes[0]["payload"]["changes"])
            self.assertFalse(session.snapshot["selected_checkouts"][repository]["clean"])

            with mock.patch.object(
                sessions.workspace_module, "discover_workspace",
                return_value={"root": str(root), "repositories": [dirty_repo], "scan": {"status": "complete"}},
            ):
                self.assertEqual(session.observe_workspace(root), [])

    def test_lifecycle_table_sweep(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = make_session(Path(directory))
            # Every declared transition succeeds; representative illegal ones fail.
            for current, destinations in sessions.TRANSITIONS.items():
                for destination in destinations:
                    with tempfile.TemporaryDirectory() as other:
                        peer = make_session(Path(other))
                        peer.snapshot["lifecycle"] = current
                        peer.snapshot["lifecycle_history"] = []
                        peer.transition(destination, "sweep")
                        self.assertEqual(peer.snapshot["lifecycle"], destination)
            illegal = [("defined", "active"), ("active", "ready"), ("completed", "active"),
                       ("failed", "active"), ("ready", "completed"), ("handed_off", "completed")]
            for current, destination in illegal:
                with tempfile.TemporaryDirectory() as other:
                    peer = make_session(Path(other))
                    peer.snapshot["lifecycle"] = current
                    with self.assertRaises(sessions.LifecycleError):
                        peer.transition(destination, "illegal")

    def test_complete_and_fail_match_table(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = make_session(Path(directory))
            session.transition("blocked", "test")
            session.complete(outcome="done")
            self.assertEqual(session.snapshot["lifecycle"], "completed")
        with tempfile.TemporaryDirectory() as directory:
            session = make_session(Path(directory))
            session.fail(reason="nope")
            self.assertEqual(session.snapshot["lifecycle"], "failed")
            with self.assertRaises(sessions.LifecycleError):
                session.complete(outcome="too late")

    def test_escalate_never_executes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            session = make_session(state)
            canary = state / "canary.txt"
            binding = capabilities.probe_availability(
                capabilities.bind(provider="p", capability="canary", contract_revision="1",
                                  entrypoint="e", address="/bin/sh", effects=["write"]))
            session.snapshot["bindings"].append(binding)
            session.snapshot["repo_facts"] = {}
            result = session.invoke("canary", ["-c", f"echo planted > {canary}"])
            self.assertEqual(result["status"], "pending-escalation")
            self.assertFalse(canary.exists())

    def test_deny_never_executes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            session = make_session(state)
            canary = state / "canary-deny.txt"
            binding = capabilities.probe_availability(
                capabilities.bind(provider="mncs-language", capability="guarded",
                                  contract_revision="1", entrypoint="e",
                                  address="/bin/sh", effects=["write"]))
            session.snapshot["bindings"].append(binding)
            session.snapshot["authority"]["protected_repositories"] = ["mncs-language"]
            with self.assertRaises(sessions.AuthorityDenied):
                session.invoke("guarded", ["-c", f"echo planted > {canary}"])
            self.assertFalse(canary.exists())

    def test_allow_executes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = make_session(Path(directory))
            binding = capabilities.probe_availability(
                capabilities.bind(provider="p", capability="echoer", contract_revision="1",
                                  entrypoint="e", address="/bin/echo", effects=["read"]))
            session.snapshot["bindings"].append(binding)
            result = session.invoke("echoer", ["hi"])
            self.assertEqual(result["status"], "ok")

    def test_session_invoke_forwards_bounded_output_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = make_session(Path(directory))
            binding = capabilities.probe_availability(
                capabilities.bind(provider="p", capability="echoer", contract_revision="1",
                                  entrypoint="e", address="/bin/echo", effects=["read"]))
            session.snapshot["bindings"].append(binding)
            with mock.patch.object(
                sessions.capabilities_module, "invoke",
                return_value={"status": "ok", "returncode": 0, "stdout": "ok"},
            ) as invoke:
                result = session.invoke("echoer", ["hi"], output_limit_bytes=131072)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(invoke.call_args.kwargs["output_limit_bytes"], 131072)
            artifact_directory = Path(
                invoke.call_args.kwargs["env"]["MNCS_ENV_SESSION_ARTIFACT_DIR"]
            )
            self.assertTrue(artifact_directory.is_dir())
            self.assertEqual(artifact_directory.parent.parent.parent.name, "sessions")
            self.assertIn(session.session_id, artifact_directory.parts)
            self.assertEqual(
                session.snapshot["artifacts"][-1]["artifact_directory"],
                str(artifact_directory),
            )
            with self.assertRaises(capabilities.CapabilityError):
                session.invoke(
                    "echoer", ["hi"],
                    output_limit_bytes=capabilities.MAX_OUTPUT_LIMIT_BYTES + 1)

    def test_cli_exposes_bounded_output_limit(self) -> None:
        parsed = cli.build_parser().parse_args([
            "invoke", "ses_fixture", "example", "--output-limit-bytes", "131072",
        ])
        self.assertEqual(parsed.output_limit_bytes, 131072)

    def test_invocation_environment_uses_selected_store_and_language_toolchains(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            store_checkout = root / "mncs-store" / ".worktrees" / "campaign"
            language_checkout = root / "mncs-language" / ".worktrees" / "campaign"
            package_init = store_checkout / "python" / "mncs_store" / "__init__.py"
            package_init.parent.mkdir(parents=True)
            package_init.write_text("", encoding="utf-8")
            binary = language_checkout / "target" / "debug" / "mncs"
            embed = binary.parent / "libmncs_embed.so"
            embed.parent.mkdir(parents=True)
            binary.write_bytes(b"selected compiler")
            embed.write_bytes(b"selected embed library")

            session = sessions.Session.__new__(sessions.Session)
            session.snapshot = {
                "workspace": {"root": str(root)},
                "selected_checkouts": {
                    "mncs-store": {"path": str(store_checkout), "head": "store-revision"},
                    "mncs-language": {"path": str(language_checkout), "head": "language-revision"},
                },
                "toolchain": {
                    "checkout": str(language_checkout), "revision": "language-revision",
                    "binary": str(binary), "status": "available",
                },
            }

            selected = session._selected_runtime_environment({})
            self.assertEqual(selected["MNCS_BIN"], str(binary.resolve()))
            self.assertEqual(selected["MNCS_EMBED_LIB"], str(embed.resolve()))
            self.assertEqual(selected["MNCS_LANGUAGE_ROOT"], str(language_checkout.resolve()))
            self.assertEqual(selected["MNCS_STORE_ROOT"], str(store_checkout.resolve()))
            self.assertEqual(
                selected["MNCS_STORE_PYTHON"],
                str((store_checkout / "python").resolve()),
            )
            self.assertEqual(
                session._selected_runtime_environment({"toolchain_env": "MNCS_BIN"}),
                {key: value for key, value in selected.items() if key != "MNCS_BIN"},
            )

    def test_store_inspection_accepts_selected_session(self) -> None:
        parsed = cli.build_parser().parse_args([
            "store", "--session", "ses_fixture", "--verify",
        ])
        self.assertEqual(parsed.session, "ses_fixture")
        with (
            mock.patch.object(cli, "open_store") as open_selected,
            mock.patch.object(cli, "out"),
        ):
            open_selected.return_value.verify.return_value = {"status": "ok"}
            self.assertEqual(cli.cmd_store(parsed), 0)
        open_selected.assert_called_once_with(
            parsed.state_dir, "store", session_id="ses_fixture",
        )

    def test_claim_management_opens_selected_session_store(self) -> None:
        parsed = cli.build_parser().parse_args([
            "claims", "ses_fixture", "--acquire", "mncs-environment",
        ])
        with (
            mock.patch.object(cli, "open_store") as open_selected,
            mock.patch.object(cli.sessions_module.Session, "resume") as resume,
            mock.patch.object(cli, "out"),
        ):
            cli.cmd_claims(parsed)
        open_selected.assert_called_once_with(
            parsed.state_dir, "store", session_id="ses_fixture",
        )
        resume.assert_called_once()

    def test_revalidate_preserves_scoped_claim_holders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = make_session(Path(directory))
            scope = {"kind": "paths", "paths": ["src/compiler/project.mncs"]}
            claim = session.acquire_claim(
                "mncs-compiler", scope=scope, reason="test exact scoped claim")

            report = session.revalidate()

            self.assertEqual(report["changed"], [])
            holders = session.snapshot["claim_holders"]["mncs-compiler"]
            self.assertEqual(holders[0]["claim_id"], claim["claim_id"])
            self.assertEqual(holders[0]["scope"], claim["scope"])
            self.assertEqual(session.snapshot["claim_holders_detailed"],
                             session.snapshot["claim_holders"])
            self.assertEqual(session.snapshot["authority"]["claim_holders"],
                             session.snapshot["claim_holders"])

    def test_provider_effects_independently_authorized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = make_session(Path(directory))
            binding = capabilities.probe_availability(
                capabilities.bind(provider="mncs-atlas", capability="writer",
                                  contract_revision="1", entrypoint="e",
                                  address="/bin/echo", effects=["write"]))
            session.snapshot["bindings"].append(binding)
            session.snapshot["authority"]["protected_repositories"] = ["mncs-atlas"]
            with self.assertRaises(sessions.AuthorityDenied):
                session.invoke("writer", ["hi"])

    def test_provider_write_uses_exact_selected_worktree_claim_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = make_session(Path(directory))
            checkout = "/selected/MNCS-Commons/.worktrees/campaign"
            binding = capabilities.probe_availability(
                capabilities.bind(
                    provider="mncs-commons", capability="campaign-writer",
                    contract_revision="1", entrypoint="e", address="/bin/echo",
                    effects=["write"], provider_root=checkout,
                    provenance={"checkout": {
                        "path": checkout, "branch": "campaign/commons-parity",
                    }},
                ))
            session.snapshot["bindings"].append(binding)
            session.snapshot["selected_checkouts"] = {"MNCS-Commons": {
                "path": checkout, "branch": "campaign/commons-parity",
            }}
            session.snapshot["claim_holders"]["MNCS-Commons"] = [{
                "session_id": session.session_id,
                "scope": {
                    "kind": "worktree", "repository": "MNCS-Commons",
                    "checkout": checkout, "branch": "campaign/commons-parity",
                    "paths": None, "exclusive": False,
                },
            }]
            with mock.patch.object(
                sessions.capabilities_module, "invoke",
                return_value={"status": "ok", "returncode": 0, "stdout": "ok"},
            ) as invoke:
                result = session.invoke("campaign-writer", ["fixture"])
            self.assertEqual(result["status"], "ok")
            self.assertEqual(invoke.call_count, 1)

    def test_provider_effect_resolves_relative_checkout_from_campaign_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "MNCS-Commons" / ".worktrees" / "campaign"
            checkout.mkdir(parents=True)
            session = make_session(root)
            session.snapshot["workspace"]["root"] = str(root)
            session.snapshot["selected_checkouts"] = {"MNCS-Commons": {
                "path": "MNCS-Commons/.worktrees/campaign",
                "branch": "campaign/commons-parity",
            }}
            binding = capabilities.probe_availability(
                capabilities.bind(
                    provider="mncs-commons", capability="campaign-writer",
                    contract_revision="1", entrypoint="e", address="/bin/echo",
                    effects=["write"], provider_root=str(checkout),
                    provenance={"checkout": {
                        "path": "MNCS-Commons/.worktrees/campaign",
                        "branch": "campaign/commons-parity",
                    }},
                ))
            session.snapshot["bindings"].append(binding)
            session.snapshot["claim_holders"]["MNCS-Commons"] = [{
                "session_id": session.session_id,
                "scope": {
                    "kind": "worktree", "repository": "MNCS-Commons",
                    "checkout": str(checkout.resolve()),
                    "branch": "campaign/commons-parity",
                    "paths": None, "exclusive": False,
                },
            }]
            with mock.patch.object(
                sessions.capabilities_module, "invoke",
                return_value={"status": "ok", "returncode": 0, "stdout": "ok"},
            ) as invoke:
                result = session.invoke("campaign-writer", ["fixture"])
            self.assertEqual(result["status"], "ok")
            self.assertEqual(invoke.call_count, 1)

    def test_relative_worktree_claim_matches_bound_absolute_provider_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "worktrees" / "campaign"
            checkout.mkdir(parents=True)
            session = make_session(root)
            session.snapshot["workspace"]["root"] = str(root)
            session.snapshot["selected_checkouts"] = {"mncs-store": {
                "path": "worktrees/campaign", "branch": "campaign/store",
            }}
            binding = capabilities.probe_availability(
                capabilities.bind(
                    provider="mncs-store", capability="campaign-writer",
                    contract_revision="1", entrypoint="e", address="/bin/echo",
                    effects=["write"], provider_root=str(checkout),
                    provenance={"checkout": {
                        "path": str(checkout), "branch": "campaign/store",
                    }},
                ))
            session.snapshot["bindings"].append(binding)
            claim = session.acquire_claim(
                "mncs-store", basis=claims.BASIS_ADOPTION,
                reason="adopt the campaign-owned selected checkout",
                scope={"kind": "worktree", "checkout": "worktrees/campaign"},
                checkout_facts={"head": "abc", "branch": "campaign/store",
                                "dirty": True, "foreign_signals": ["campaign changes"]},
            )
            self.assertEqual(claim["scope"]["checkout"], str(checkout.resolve()))
            self.assertEqual(claim["scope"]["branch"], "campaign/store")
            with mock.patch.object(
                sessions.capabilities_module, "invoke",
                return_value={"status": "ok", "returncode": 0, "stdout": "ok"},
            ) as invoke:
                result = session.invoke("campaign-writer", ["fixture"])
            self.assertEqual(result["status"], "ok")
            self.assertEqual(invoke.call_count, 1)

    def test_checkpoint_handoff_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            session = make_session(state)
            record = session.checkpoint(progress="half", remaining=["rest"])
            self.assertTrue(record["identity"].startswith("chk_"))
            handoff = session.handoff(to_consumer="agent-b", next_actions=["rest"])
            self.assertTrue(handoff["identity"].startswith("hff_"))
            self.assertEqual(session.snapshot["lifecycle"], "handed_off")
            resumed = sessions.Session.resume(
                state_dir=state, session_id=session.session_id,
                store=session.store)
            accepted = resumed.accept_handoff(handoff["identity"], consumer_id="agent-b")
            self.assertEqual(accepted["previous_consumer"], "tester")
            self.assertEqual(resumed.snapshot["lifecycle"], "active")
            self.assertEqual(resumed.snapshot["checkpoints"][0], record["identity"])

    def test_handoff_validates_recipient(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            session = make_session(state)
            handoff = session.handoff(to_consumer="agent-b")
            resumed = sessions.Session.open(state_dir=state, session_id=session.session_id, **FAST)
            with self.assertRaises(sessions.LifecycleError):
                resumed.accept_handoff(handoff["identity"], consumer_id="agent-impostor")
            with self.assertRaises(sessions.LifecycleError):
                resumed.accept_handoff("hff_nonexistent", consumer_id="agent-b")

    def test_inspection_appends_no_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            session = make_session(state)
            count = len(session._log())
            opened = sessions.Session.open(state_dir=state, session_id=session.session_id, **FAST)
            opened.inspect()
            opened.inspect()["bindings"]
            opened.inspect()["authority"]
            opened.inspect()["latest_events"]
            self.assertEqual(len(opened._log()), count)

    def test_events_subscribe_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = make_session(Path(directory))
            subscription = session.subscribe(["session.checkpointed", "authority.denied"])
            session.check(action="delete", target="mncs-language")
            session.checkpoint(progress="x")
            due = session.poll(subscription["subscription_id"])
            types = {event["type"] for event in due}
            self.assertIn("session.checkpointed", types)
            self.assertEqual(session.poll(subscription["subscription_id"]), [])

    def test_concurrent_appends_converge(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            session = make_session(state)
            first = sessions.Session.open(state_dir=state, session_id=session.session_id, **FAST)
            second = sessions.Session.open(state_dir=state, session_id=session.session_id, **FAST)
            base = len(first._log())
            for index in range(10):
                first.record_observation("adapter.observed", f"writer-a-{index}", {})
                second.record_observation("adapter.observed", f"writer-b-{index}", {})
            reread = sessions.Session.open(state_dir=state, session_id=session.session_id, **FAST)
            log = reread._log()
            sequences = [event["sequence"] for event in log]
            self.assertEqual(len(log), base + 20)
            self.assertEqual(sorted(sequences), list(range(1, base + 21)))

    def test_malformed_snapshot_rejected(self) -> None:
        store = open_store(tempfile.mkdtemp(), "file")
        with self.assertRaises(sessions.LifecycleError):
            sessions.Session(store, "ses_bogus")

    def test_multi_session_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            first = make_session(state)
            second = make_session(state)
            self.assertNotEqual(first.session_id, second.session_id)
            first.checkpoint(progress="first")
            self.assertEqual(len(second.snapshot.get("checkpoints", [])), 0)

    def test_restart_preserves_store_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            session = make_session(state)
            checkpoint = session.checkpoint(progress="before restart")
            identity = session.session_id
            del session
            reopened = sessions.Session.resume(state_dir=state, session_id=identity)
            self.assertIn(checkpoint["identity"], reopened.snapshot["checkpoints"])
            self.assertEqual(reopened.snapshot["intent"]["goal"], "test goal")

    def test_file_backend_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            env = sessions.resolve_environment(
                definition=definition(), workspace_root=ROOT, state_dir=state,
                consumer_id="tester", store=open_store(state, "file"))
            session = sessions.Session.create(
                state_dir=state, environment=env, consumer_id="tester", backend="file")
            session.transition("resolving", "t")
            session.transition("ready", "t")
            session.transition("active", "t")
            checkpoint = session.checkpoint(progress="file")
            reopened = sessions.Session.open(state_dir=state, session_id=session.session_id,
                                             backend="file")
            self.assertIn(checkpoint["identity"], reopened.snapshot["checkpoints"])


class EventTests(unittest.TestCase):
    def test_unknown_type_becomes_adapter(self) -> None:
        event = events.make(session_id="ses_x", sequence=1, event_type="vendor.blip",
                            producer="vendor")
        self.assertEqual(event["type"], "adapter.observed")
        self.assertEqual(event["provider_type"], "vendor.blip")

    def test_store_feed_advance_becomes_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            session = make_session(state)
            self.assertIsNone(session.observe_store())
            # An external writer advances the Store generation outside the session.
            peer = open_store(state, "store", verify_on_open=False)
            try:
                peer.put_claim({"schema_version": "mncs.environment.claim/1",
                                "claim_id": "claim:external", "version": 1,
                                "repository": "external", "session_id": "other",
                                "consumer_id": "other", "basis": "explicit-claim",
                                "reason": "probe", "status": "held",
                                "acquired_at": "2026-01-01T00:00:00",
                                "expires_at": "2026-01-02T00:00:00",
                                "provenance": {}, "identity": "clm_probe"})
            finally:
                peer.close()
            event = session.observe_store()
            self.assertIsNotNone(event)
            self.assertEqual(event["type"], "adapter.observed")
            self.assertEqual(event["producer"], "adapter:store-feed")
            self.assertIsNone(session.observe_store())
            # A fresh handle adopts silently: resume re-reads state, so the
            # downtime delta is never reported as an external event.
            reopened = sessions.Session.open(
                state_dir=state, session_id=session.session_id, store=session.store)
            self.assertIsNone(reopened.observe_store())

    def test_git_poll_adapter(self) -> None:
        made, _ = events.git_poll_events(
            session_id="ses_x", sequence_start=1,
            previous_heads={"a": "sha1"}, current_heads={"a": "sha2", "b": None})
        self.assertEqual(len(made), 1)
        self.assertEqual(made[0]["type"], "workspace.changed")
        self.assertEqual(made[0]["producer"], "adapter:git-poll")


class RightsTests(unittest.TestCase):
    def _workspace(self, directory):
        root = Path(directory) / "ws"
        (root / "demo-repo" / ".git").mkdir(parents=True)
        return root

    def _resolve(self, state_dir, workspace_root, records):
        store = open_store(state_dir, "store", verify_on_open=False)
        env = sessions.resolve_environment(
            definition=definition(rights_claims=records),
            workspace_root=workspace_root,
            state_dir=state_dir,
            consumer_id="tester",
            store=store,
        )
        return store, env

    def _verified(self, subject, **extra):
        record = {"subject": subject, "claimant": "epi13", "license": "Apache-2.0",
                  "evidence": ["LICENSE"], "verified": True}
        record.update(extra)
        return record

    def test_clear_gate_enters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._workspace(directory)
            state = Path(directory) / "state"
            store, env = self._resolve(state, root, [self._verified("demo-repo")])
            self.assertEqual(env["rights"]["overall"], "clear")
            session = sessions.Session.create(
                state_dir=state, environment=env, consumer_id="tester", store=store)
            self.assertEqual(session.snapshot["rights"]["overall"], "clear")

    def test_revoked_claim_blocks_enter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._workspace(directory)
            state = Path(directory) / "state"
            store, env = self._resolve(
                state, root, [self._verified("demo-repo", revoked=True)])
            self.assertEqual(env["rights"]["overall"], "blocked")
            with self.assertRaises(rights.RightsBlocked):
                sessions.Session.create(
                    state_dir=state, environment=env, consumer_id="tester", store=store)

    def test_missing_library_reviews_never_passes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = self._workspace(directory)
            state = Path(directory) / "state"
            with mock.patch.dict(sys.modules, {"mncs_rights_provenance": None}):
                store, env = self._resolve(state, root, [self._verified("demo-repo")])
            self.assertFalse(env["rights"]["available"])
            self.assertEqual(env["rights"]["overall"], "review")
            # Review gates work but do not block entry.
            sessions.Session.create(
                state_dir=state, environment=env, consumer_id="tester", store=store)


class CLITests(unittest.TestCase):
    def test_relative_workspace_resolves_against_definition_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            definition_file = Path(directory) / "defs" / "env.json"
            definition_file.parent.mkdir(parents=True)
            args = argparse.Namespace(workspace=None, definition=str(definition_file))
            resolved = cli.resolve_workspace_root(
                args, {"workspace_root": "../family"})
            self.assertEqual(resolved, str(Path(directory) / "family"))

    def test_explicit_workspace_wins_and_absolute_passes_through(self) -> None:
        args = argparse.Namespace(workspace="/explicit", definition="env.json")
        self.assertEqual(cli.resolve_workspace_root(args, {}), "/explicit")
        args = argparse.Namespace(workspace=None, definition="env.json")
        self.assertEqual(
            cli.resolve_workspace_root(args, {"workspace_root": "/abs"}), "/abs")

    def test_shipped_campaign_definition_is_portable(self) -> None:
        path = ROOT / "examples" / "compiler-campaign" / "environment.json"
        self.assertTrue(path.is_file())
        raw = json.loads(path.read_text())
        self.assertNotIn("/home", json.dumps(raw))
        args = argparse.Namespace(workspace=str(FAMILY), definition=str(path))
        resolved = cli.resolve_workspace_root(args, raw)
        self.assertEqual(Path(resolved).resolve(), FAMILY.resolve())
        args.workspace = None
        with self.assertRaisesRegex(ValueError, "explicit --workspace"):
            cli.resolve_workspace_root(args, raw)


class EntryFrictionTests(unittest.TestCase):
    """Entry safety and context-shape regressions use only temporary state."""

    @staticmethod
    def _campaign_definition(path: Path) -> dict:
        definition = {
            "name": "mncs-compiler-campaign",
            "workspace_scope": {
                "kind": "campaign", "selection": "explicit", "max_directories": 4
            },
            "intent": {
                "goal": "exercise a scoped compiler campaign entry",
                "repositories": ["mncs-language", "mncs-compiler"],
                "protected_repositories": ["mncs-memory"],
            },
        }
        path.write_text(json.dumps(definition), encoding="utf-8")
        return definition

    @staticmethod
    def _run_cli(*args: str) -> tuple[int, dict, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(list(args))
        payload = json.loads(stdout.getvalue()) if stdout.getvalue().strip() else {}
        return code, payload, stderr.getvalue()

    def test_broad_campaign_root_rejected_before_state_or_git_scan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Projects"
            root.mkdir()
            for index in range(5):
                (root / f"repository-{index}").mkdir()
            definition_path = Path(directory) / "environment.json"
            definition = self._campaign_definition(definition_path)
            state = Path(directory) / "state"

            with self.assertRaisesRegex(
                workspace.WorkspaceResolutionError,
                r"too broad.*safe limit.*campaign-scoped",
            ) as raised:
                sessions.resolve_environment(
                    definition=definition,
                    workspace_root=root,
                    state_dir=state,
                    consumer_id="entry-test",
                )

            self.assertEqual(raised.exception.diagnostics["code"], "workspace-too-broad")
            self.assertFalse(state.exists(), "invalid resolution must not create session state")

    def test_slow_workspace_scan_returns_bounded_progress_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(3):
                (root / f"candidate-{index}").mkdir()

            def slow_inspection(_path: Path, *, deadline: float | None = None):
                time.sleep(0.06)
                return None

            with mock.patch.object(workspace, "inspect_repo", side_effect=slow_inspection):
                view = workspace.discover_workspace(root, timeout_seconds=0.01)

            self.assertEqual(view["scan"]["status"], "timed_out")
            self.assertLessEqual(view["scan"]["inspected_directories"], 2)
            self.assertIn("inspected", view["scan"]["message"])
            self.assertIn("bounded time budget", view["scan"]["message"])

    def test_valid_campaign_entry_is_compact_and_uses_isolated_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            workspace_root = base / "campaign-root"
            workspace_root.mkdir()
            definition_path = base / "environment.json"
            self._campaign_definition(definition_path)
            state = base / "isolated-state"
            shared = base / "shared-state"
            shared.mkdir()
            sentinel = shared / "claims.jsonl"
            sentinel.write_text("unrelated\n", encoding="utf-8")

            code, context, error = self._run_cli(
                "--persistence", "file", "--state-dir", str(state), "enter",
                "--definition", str(definition_path), "--workspace", str(workspace_root),
                "--consumer", "entry-test",
            )

            self.assertEqual(code, 0, error)
            self.assertEqual(context["lifecycle"], "active")
            self.assertTrue(context["session_id"].startswith("ses_"))
            self.assertEqual(context["workspace_root"], str(workspace_root.resolve()))
            self.assertEqual(
                context["work_intent"]["goal"],
                "exercise a scoped compiler campaign entry",
            )
            self.assertEqual(context["writable_repositories"], ["mncs-compiler", "mncs-language"])
            self.assertEqual(context["protected_repositories"], ["mncs-memory"])
            self.assertIn("available", context["capabilities"])
            self.assertIn("unavailable_capability_count", context)
            self.assertTrue(context["next_commands"])
            self.assertFalse((shared / "sessions").exists())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "unrelated\n")

            inspect_code, detailed, inspect_error = self._run_cli(
                "--persistence", "file", "--state-dir", str(state), "inspect",
                context["session_id"],
            )
            self.assertEqual(inspect_code, 0, inspect_error)
            self.assertIn("intent", detailed)
            self.assertIn("authority", detailed)
            self.assertIn("workspace", detailed)
            self.assertIn("bindings", detailed)
            self.assertIn("lifecycle_history", detailed)
            self.assertGreater(detailed["event_count"], 0)
            self.assertGreater(len(json.dumps(detailed)), len(json.dumps(context)))

    def test_status_and_context_are_read_only_and_documented(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            workspace_root = base / "campaign-root"
            workspace_root.mkdir()
            definition_path = base / "environment.json"
            self._campaign_definition(definition_path)
            state = base / "isolated-state"
            code, entered, error = self._run_cli(
                "--persistence", "file", "--state-dir", str(state), "enter",
                "--definition", str(definition_path), "--workspace", str(workspace_root),
                "--consumer", "entry-test",
            )
            self.assertEqual(code, 0, error)
            session_id = entered["session_id"]
            claims_before = (state / "claims.jsonl").read_bytes() if (state / "claims.jsonl").exists() else b""
            events_before = (state / "sessions" / session_id / "events.jsonl").read_bytes()

            status_code, status, status_error = self._run_cli(
                "--persistence", "file", "--state-dir", str(state), "status", session_id,
            )
            context_code, context, context_error = self._run_cli(
                "--persistence", "file", "--state-dir", str(state), "context", session_id,
            )
            self.assertEqual(status_code, 0, status_error)
            self.assertEqual(context_code, 0, context_error)
            self.assertEqual(status, context)
            self.assertEqual(
                events_before,
                (state / "sessions" / session_id / "events.jsonl").read_bytes(),
            )
            self.assertEqual(
                claims_before,
                (state / "claims.jsonl").read_bytes() if (state / "claims.jsonl").exists() else b"",
            )
            readme = (ROOT / "README.md").read_text(encoding="utf-8")
            self.assertIn("mncs-env", readme)
            self.assertIn("status <session>", readme)

    def test_status_does_not_change_unrelated_session_or_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            workspace_root = base / "workspace"
            workspace_root.mkdir()
            state = base / "state"
            store = open_store(state, "file")
            environment = sessions.resolve_environment(
                definition=definition(
                    intent={"goal": "unrelated session", "repositories": ["repo-a"]}
                ),
                workspace_root=workspace_root,
                state_dir=state,
                consumer_id="unrelated-agent",
                store=store,
            )
            session = sessions.Session.create(
                state_dir=state, environment=environment,
                consumer_id="unrelated-agent", backend="file", store=store,
            )
            session.transition("resolving", "test")
            session.transition("ready", "test")
            session.transition("active", "test")
            claim = session.acquire_claim("repo-a", reason="unrelated claim")
            session_path = state / "sessions" / session.session_id / "session.json"
            claims_path = state / "claims.jsonl"
            session_before = session_path.read_bytes()
            claims_before = claims_path.read_bytes()

            code, _, error = self._run_cli(
                "--persistence", "file", "--state-dir", str(state), "status", session.session_id,
            )
            self.assertEqual(code, 0, error)
            self.assertEqual(session_path.read_bytes(), session_before)
            self.assertEqual(claims_path.read_bytes(), claims_before)
            self.assertEqual(claim["session_id"], session.session_id)


class SourcesTests(unittest.TestCase):
    def test_git_source_baselines_then_reports_no_change(self) -> None:
        source = sources.GitHeadsSource(ROOT)
        first = source.observe(None)
        self.assertEqual(first.status, "ok")
        self.assertEqual(first.events, [])
        self.assertIsNotNone(first.cursor)
        second = source.observe(first.cursor)
        self.assertEqual(second.status, "ok")
        self.assertEqual(second.events, [])

    def test_store_replay_baselines_and_needs_backend(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = open_store(Path(directory), "store", verify_on_open=False)
            source = sources.StoreReplaySource(store, "ses_mine:")
            first = source.observe(None)
            self.assertEqual(first.status, "ok")
            self.assertEqual(first.events, [])
            file_store = open_store(Path(directory) / "f", "file")
            missing = sources.StoreReplaySource(file_store, "ses_mine:")
            self.assertEqual(missing.observe(None).status, "unknown")

    def test_absent_providers_report_unknown(self) -> None:
        commons = sources.CommonsSyncSource("/nonexistent-commons.sock")
        result = commons.observe(None)
        self.assertEqual(result.status, "unknown")
        language = sources.LanguageServiceSource(None)
        self.assertEqual(language.observe(None).status, "unknown")

    def test_language_source_omits_unknown_stream_identity(self) -> None:
        import socket
        import threading
        # Stub host enforcing the real dispatch rule: an explicit null
        # stream_identity is rejected; the key must be omitted instead.
        directory = tempfile.mkdtemp(prefix="mnls-stub-")
        path = str(Path(directory) / "lang.sock")
        seen: list[dict] = []
        ready = threading.Event()

        def serve() -> None:
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(path)
            listener.listen(2)
            listener.settimeout(10)
            ready.set()
            for _ in range(2):
                try:
                    connection, _ = listener.accept()
                except OSError:
                    return
                with connection:
                    data = b""
                    while b"\n" not in data:
                        chunk = connection.recv(65536)
                        if not chunk:
                            break
                        data += chunk
                    try:
                        request = json.loads(data.decode("utf-8"))
                    except ValueError:
                        continue
                    params = request.get("params", {})
                    seen.append(params)
                    if ("stream_identity" in params
                            and params["stream_identity"] is None):
                        payload = {"id": 1, "ok": False, "result": None,
                                   "error": "invalid parameter stream_identity"}
                    else:
                        payload = {
                            "id": 1, "ok": True,
                            "result": {"stream_identity": "stream-1",
                                       "after_cursor": 0, "current_cursor": 0,
                                       "oldest_cursor": 1,
                                       "reset_required": False, "events": []},
                        }
                    connection.sendall(
                        (json.dumps(payload) + "\n").encode("utf-8"))
            listener.close()

        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        self.assertTrue(ready.wait(timeout=10))
        try:
            source = sources.LanguageServiceSource(path)
            first = source.observe(None)
            self.assertEqual(first.status, "ok")
            self.assertTrue(first.cursor)
            second = source.observe(first.cursor)
            self.assertEqual(second.status, "ok")
        finally:
            worker.join(timeout=10)
        self.assertEqual(len(seen), 2)
        self.assertNotIn("stream_identity", seen[0])
        self.assertEqual(seen[1].get("stream_identity"), "stream-1")


class ReconcilerTests(unittest.TestCase):
    def _setup(self, directory):
        state = Path(directory)
        store = open_store(state, "store", verify_on_open=False)
        session, created = reconciler.open_or_create_session(state, store, ROOT)
        self.assertTrue(created)
        return state, store, session

    def test_first_run_baselines_without_noise(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state, store, session = self._setup(directory)
            report = reconciler.reconcile_once(
                session, store, ROOT, language_socket=None,
                commons_socket="/nonexistent-commons.sock")
            self.assertEqual(report["observations"], [])
            self.assertEqual(report["resets"], [])
            self.assertIn("commons-sync", [u["source"] for u in report["unknown"]])
            health = reconciler.session_health(session)
            self.assertEqual(health["consumer_id"], "environment-reconciler")
            self.assertIn("store-replay", health["cursors"])

    def test_external_claim_becomes_one_observation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state, store, session = self._setup(directory)
            reconciler.reconcile_once(session, store, ROOT)
            peer = open_store(state, "store", verify_on_open=False)
            try:
                peer.put_claim({"schema_version": "mncs.environment.claim/1",
                                "claim_id": "claim:ext", "version": 1,
                                "repository": "ext", "session_id": "other",
                                "consumer_id": "other", "basis": "explicit-claim",
                                "reason": "probe", "status": "held",
                                "acquired_at": "2026-01-01T00:00:00",
                                "expires_at": "2026-01-02T00:00:00",
                                "provenance": {}, "identity": "clm_probe"})
            finally:
                peer.close()
            report = reconciler.reconcile_once(session, store, ROOT)
            kinds = [o["kind"] for o in report["observations"]]
            self.assertIn("claim.changed", kinds)
            # Dedup: a second pass sees nothing new and writes nothing.
            before = store.generation()
            again = reconciler.reconcile_once(session, store, ROOT)
            self.assertEqual(again["observations"], [])
            self.assertFalse(again["persisted"])
            self.assertEqual(store.generation(), before)

    def test_restart_resumes_cursor_without_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state, store, session = self._setup(directory)
            reconciler.reconcile_once(session, store, ROOT)
            first_id = session.session_id
            store.close()
            reopened_store = open_store(state, "store", verify_on_open=False)
            try:
                resumed, created = reconciler.open_or_create_session(
                    state, reopened_store, ROOT)
                self.assertFalse(created)
                self.assertEqual(resumed.session_id, first_id)
                report = reconciler.reconcile_once(resumed, reopened_store, ROOT)
                self.assertEqual(report["observations"], [])
            finally:
                reopened_store.close()

    def test_restart_recovers_missed_event_once(self) -> None:
        # An event landing after the last persisted cycle (quiet adoption)
        # must replay exactly once after restart, never silently dropped.
        with tempfile.TemporaryDirectory() as directory:
            state, store, session = self._setup(directory)
            first = reconciler.reconcile_once(session, store, ROOT)
            self.assertEqual(first["observations"], [])
            self.assertTrue(first["persisted"])
            peer = open_store(state, "store", verify_on_open=False)
            try:
                peer.put_claim({"schema_version": "mncs.environment.claim/1",
                                "claim_id": "claim:missed", "version": 1,
                                "repository": "missed", "session_id": "other",
                                "consumer_id": "other", "basis": "explicit-claim",
                                "reason": "restart probe", "status": "held",
                                "acquired_at": "2026-01-01T00:00:00",
                                "expires_at": "2027-01-02T00:00:00",
                                "provenance": {}, "identity": "clm_missed"})
            finally:
                peer.close()
            store.close()
            reopened = open_store(state, "store", verify_on_open=False)
            try:
                resumed, created = reconciler.open_or_create_session(
                    state, reopened, ROOT)
                self.assertFalse(created)
                report = reconciler.reconcile_once(resumed, reopened, ROOT)
                kinds = [o["kind"] for o in report["observations"]]
                self.assertIn("claim.changed", kinds)
                again = reconciler.reconcile_once(resumed, reopened, ROOT)
                self.assertEqual(again["observations"], [])
            finally:
                reopened.close()

    def test_janitor_denies_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _, _, session = self._setup(directory)
            verdict = session.check(action="write", target="mncs-test")
            self.assertEqual(verdict["verdict"], "deny")


class BriefingTests(unittest.TestCase):
    def test_brief_ack_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = make_session(Path(directory))
            capsule = session.brief()
            self.assertEqual(capsule["cursor"]["index"], 0)
            self.assertIn("active", capsule["summary"])
            session.checkpoint(progress="halfway")
            capsule = session.brief()
            self.assertTrue(any(i["kind"] == "session.checkpointed"
                                for i in capsule["items"]))
            total = capsule["cursor"]["total"]
            acked = session.ack(total)
            self.assertEqual(acked["cursor"]["index"], total)
            self.assertEqual(session.brief()["items"], [])
            back = session.ack(0)
            self.assertEqual(back["cursor"]["index"], total)
            clamped = session.ack(total + 100)
            self.assertEqual(clamped["cursor"]["index"], total)
            self.assertIn("clamped", clamped["note"])

    def test_denial_surfaces_safety_first(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = make_session(Path(directory))
            session.check(action="write", target="mncs-language")
            capsule = session.brief()
            safety = [i for i in capsule["items"] if i["category"] == "safety"]
            self.assertTrue(safety)
            self.assertEqual(capsule["items"][0]["category"], "safety")


class SelectedStoreProviderTests(unittest.TestCase):
    @staticmethod
    def _environment(root: Path, checkout: Path) -> dict:
        package = checkout / "python"
        init = package / "mncs_store" / "__init__.py"
        init.parent.mkdir(parents=True, exist_ok=True)
        init.write_text("", encoding="utf-8")
        return {
            "identity": "env_selected_store_test",
            "workspace": {"root": str(root), "repositories": []},
            "selected_checkouts": {
                "mncs-store": {
                    "path": str(checkout), "head": "abc123",
                    "authoritative_head": "abc123", "branch": "campaign/store",
                    "source_ref": "session-pinned-checkout", "clean": True,
                }
            },
        }

    def test_resolution_opens_selected_store_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            checkout = root / "mncs-store" / ".worktrees" / "campaign"
            checkout.mkdir(parents=True)
            self._environment(root, checkout)
            workspace_view = {
                "root": str(root),
                "repositories": [{
                    "name": "mncs-store@campaign", "path": str(checkout),
                    "head": "abc123", "branch": "campaign/store", "dirty": False,
                }],
                "scan": {"status": "complete"},
            }

            class ClaimStore:
                closed = False

                def read_claims(self):
                    return []

                def close(self):
                    self.closed = True

            backend = ClaimStore()
            with (
                mock.patch.object(sessions.workspace_module, "discover_workspace",
                                  return_value=workspace_view),
                mock.patch.object(sessions, "_provider_managed_checkouts", return_value=(
                    {"mncs-store": checkout},
                    {"mncs-store": {
                        "repository": "mncs-store", "path": "mncs-store/.worktrees/campaign",
                        "head": "abc123", "authoritative_head": "abc123",
                        "branch": "campaign/store", "clean": True,
                    }},
                )),
                mock.patch.object(capabilities, "discover_capabilities", return_value=[]),
                mock.patch.object(sessions, "open_store", return_value=backend) as opened,
            ):
                environment = sessions.resolve_environment(
                    definition={}, workspace_root=root,
                    state_dir=Path(directory) / "state", consumer_id="selected-store-test",
                )

            self.assertEqual(
                Path(str(opened.call_args.kwargs["store_package_dir"])).resolve(),
                (checkout / "python").resolve(),
            )
            self.assertTrue(backend.closed)
            self.assertEqual(environment["selected_checkouts"]["mncs-store"]["head"], "abc123")

    def test_create_persists_selected_store_for_fresh_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            checkout = root / "mncs-store" / ".worktrees" / "campaign"
            checkout.mkdir(parents=True)
            environment = self._environment(root, checkout)
            state_dir = Path(directory) / "state"
            selected_store = open_store(state_dir, "file")
            with mock.patch("mncs_env.sessions.open_store", return_value=selected_store) as opened:
                session = sessions.Session.create(
                    state_dir=state_dir, environment=environment,
                    consumer_id="selected-store-test",
                )

            self.assertEqual(
                Path(str(opened.call_args.kwargs["store_package_dir"])).resolve(),
                (checkout / "python").resolve(),
            )
            marker = json.loads((state_dir / "sessions" / session.session_id
                                 / "store-provider.json").read_text(encoding="utf-8"))
            self.assertEqual(marker["revision"], "abc123")
            self.assertEqual(marker["python_package"], str((checkout / "python").resolve()))
            session.close()

    def test_session_reopens_with_exact_selected_store_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace_root = root / "workspace"
            checkout = workspace_root / "mncs-store" / ".worktrees" / "campaign"
            package = checkout / "python"
            init = package / "mncs_store" / "__init__.py"
            init.parent.mkdir(parents=True)
            init.write_text("", encoding="utf-8")

            environment = {
                "workspace": {"root": str(workspace_root)},
                "selected_checkouts": {
                    "mncs-store": {
                        "path": str(checkout.relative_to(workspace_root)),
                        "head": "abc123",
                        "authoritative_head": "abc123",
                        "branch": "campaign/store",
                        "source_ref": "origin/main",
                        "clean": True,
                    }
                },
            }
            binding = store_provider_from_environment(environment)
            self.assertIsNotNone(binding)
            assert binding is not None
            self.assertEqual(binding["python_package"], str(package.resolve()))

            state_dir = root / "state"
            session_id = "ses_store_provider_test"
            write_session_store_provider(state_dir, session_id, binding)
            with mock.patch("mncs_env.session_store.StoreSessionStore") as store_ctor:
                open_store(
                    state_dir,
                    "store",
                    verify_on_open=False,
                    session_id=session_id,
                )
            store_ctor.assert_called_once_with(
                state_dir,
                verify_on_open=False,
                store_package_dir=str(package.resolve()),
                store_runtime=None,
            )

    def test_campaign_store_provider_binds_exact_language_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            store_checkout = root / "mncs-store" / ".worktrees" / "campaign"
            language_checkout = root / "mncs-language" / ".worktrees" / "campaign"
            environment = self._environment(root, store_checkout)
            binary = language_checkout / "target" / "release" / "mncs"
            embed = binary.parent / "libmncs_embed.so"
            embed.parent.mkdir(parents=True)
            binary.write_bytes(b"selected compiler")
            embed.write_bytes(b"selected embed library")
            environment["selected_checkouts"]["mncs-language"] = {
                "path": str(language_checkout), "head": "language-revision",
            }
            environment["toolchain"] = {
                "repository": "mncs-language", "checkout": str(language_checkout),
                "revision": "language-revision", "binary": str(binary),
                "status": "available",
            }

            binding = store_provider_from_environment(environment)
            assert binding is not None
            expected_runtime = {
                "MNCS_STORE_ROOT": str(store_checkout.resolve()),
                "MNCS_LANGUAGE_ROOT": str(language_checkout.resolve()),
                "MNCS_BIN": str(binary.resolve()),
                "MNCS_EMBED_LIB": str(embed.resolve()),
            }
            self.assertEqual(binding["runtime_environment"], expected_runtime)

            state_dir = Path(directory) / "state"
            session_id = "ses_exact_toolchain_test"
            write_session_store_provider(state_dir, session_id, binding)
            with mock.patch("mncs_env.session_store.StoreSessionStore") as store_ctor:
                open_store(state_dir, "store", session_id=session_id)
            store_ctor.assert_called_once_with(
                state_dir, verify_on_open=True,
                store_package_dir=str((store_checkout / "python").resolve()),
                store_runtime=expected_runtime,
            )

    def test_campaign_store_provider_refuses_missing_language_toolchain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "workspace"
            store_checkout = root / "mncs-store" / ".worktrees" / "campaign"
            language_checkout = root / "mncs-language" / ".worktrees" / "campaign"
            environment = self._environment(root, store_checkout)
            environment["selected_checkouts"]["mncs-language"] = {
                "path": str(language_checkout), "head": "language-revision",
            }

            with self.assertRaisesRegex(ValueError, "refusing an ambient Store compiler"):
                store_provider_from_environment(environment)


if __name__ == "__main__":
    unittest.main()
