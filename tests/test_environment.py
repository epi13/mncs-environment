"""Unit, failure, concurrency, and multi-session tests for mncs-environment.

Unless marked otherwise, tests run against the canonical Store backend in
a temporary state directory, so the suite itself proves Store-backed
persistence, CAS-safe sequences, and restart/resume.
"""

from __future__ import annotations

import argparse
import json
import stat
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mncs_env import (  # noqa: E402
    authority,
    capabilities,
    claims,
    cli,
    events,
    identity,
    intent,
    rights,
    sessions,
    workspace,
)
from mncs_env.session_store import open_store  # noqa: E402


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
            self.assertEqual(claims.holders(store.read_claims())["r"]["session_id"], "a")
            released = claims.release(store, repository="r", session_id="a")
            self.assertIsNotNone(released)
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
        self.assertEqual(claims.holders([stale, live])["r"]["session_id"], "b")


class WorkspaceTests(unittest.TestCase):
    def test_discovers_real_repositories(self) -> None:
        view = workspace.discover_workspace(ROOT.parent)
        names = {repo["name"] for repo in view["repositories"]}
        self.assertIn("mncs-atlas", names)
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
        bindings = capabilities.discover_capabilities(ROOT.parent)
        by_provider = {binding["provider"] for binding in bindings}
        self.assertIn("mncs-test", by_provider)
        self.assertTrue(all(binding["binding_id"].startswith("cap_") for binding in bindings))

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


FAMILY = ROOT.parent
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
            state = Path(directory)
            store = open_store(state, "store", verify_on_open=False)
            env = sessions.resolve_environment(
                definition=definition(), workspace_root=FAMILY,
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

    def test_compiler_binding_visible_with_manifest_tests(self) -> None:
        bindings = capabilities.discover_capabilities(FAMILY)
        matches = [b for b in bindings if b["provider"] == "mncs-compiler"]
        self.assertTrue(matches, "mncs-compiler binding not discovered")
        declared = matches[0]["provenance"].get("manifest_tests", [])
        self.assertTrue(any(t.get("test") == "compiler-front-end" for t in declared))


class SessionTests(unittest.TestCase):
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
        args = argparse.Namespace(workspace=None, definition=str(path))
        resolved = cli.resolve_workspace_root(args, raw)
        self.assertEqual(Path(resolved).resolve(), FAMILY.resolve())


if __name__ == "__main__":
    unittest.main()
