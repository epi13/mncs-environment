"""Unit, failure, and multi-session tests for mncs-environment."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mncs_env import authority, capabilities, events, identity, intent, leases, sessions, workspace


def definition(**overrides):
    base = {"name": "test-env", "intent": {"goal": "test goal"}}
    base.update(overrides)
    return base


def make_session(state_dir, **overrides):
    env = sessions.resolve_environment(
        definition=definition(**overrides),
        workspace_root=ROOT,
        state_dir=state_dir,
        consumer_id="tester",
    )
    session = sessions.Session.create(state_dir=state_dir, environment=env, consumer_id="tester")
    session.transition("resolving", "test")
    session.transition("ready", "test")
    session.transition("active", "test")
    return session


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
    def make_context(self, **overrides):
        raw = {"goal": "g", "protected_repositories": ["mncs-language"],
               "repositories": ["mncs-atlas"], "forbidden_actions": ["delete"]}
        context = authority.build_context(
            subject="tester", intent=intent.parse(raw),
            protected_repos=["mncs-language"], lease_holders={})
        context.update(overrides)
        return context

    def test_protected_write_denied(self) -> None:
        verdict = authority.evaluate(self.make_context(), action="write",
                                     target="mncs-language", session_id="ses_x")
        self.assertEqual(verdict["verdict"], "deny")

    def test_protected_read_allowed(self) -> None:
        verdict = authority.evaluate(self.make_context(), action="read",
                                     target="mncs-language", session_id="ses_x")
        self.assertEqual(verdict["verdict"], "allow")

    def test_forbidden_action_denied(self) -> None:
        verdict = authority.evaluate(self.make_context(), action="delete",
                                     target="mncs-atlas", session_id="ses_x")
        self.assertEqual(verdict["verdict"], "deny")

    def test_lease_holder_blocks_others(self) -> None:
        context = self.make_context(lease_holders={"mncs-atlas": "ses_other"})
        verdict = authority.evaluate(context, action="write", target="mncs-atlas",
                                     session_id="ses_mine")
        self.assertEqual(verdict["verdict"], "deny")
        own = authority.evaluate(context, action="write", target="mncs-atlas",
                                 session_id="ses_other")
        self.assertEqual(own["verdict"], "allow")

    def test_unknown_scope_escalates_not_allows(self) -> None:
        verdict = authority.evaluate(self.make_context(), action="merge",
                                     target="something-new", session_id="ses_x")
        self.assertEqual(verdict["verdict"], "escalate")

    def test_unknown_action_denied(self) -> None:
        verdict = authority.evaluate(self.make_context(), action="teleport",
                                     target="x", session_id="ses_x")
        self.assertEqual(verdict["verdict"], "deny")


class LeaseTests(unittest.TestCase):
    def test_acquire_conflict_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            leases_module = leases
            record = leases_module.acquire(state, repository="r", owner_session="a", reason="work")
            self.assertEqual(record["owner_session"], "a")
            with self.assertRaises(leases_module.LeaseConflict):
                leases_module.acquire(state, repository="r", owner_session="b", reason="other")
            self.assertEqual(leases_module.active_holders(state), {"r": "a"})
            self.assertTrue(leases_module.release(state, repository="r", owner_session="a"))
            self.assertEqual(leases_module.active_holders(state), {})


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


class SessionTests(unittest.TestCase):
    def test_lifecycle_enforced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = make_session(Path(directory))
            self.assertEqual(session.snapshot["lifecycle"], "active")
            with self.assertRaises(sessions.LifecycleError):
                session.transition("ready", "cannot go backwards")
            session.transition("checkpointed", "test")
            session.transition("active", "test")
            session.complete(outcome="done")
            self.assertEqual(session.snapshot["lifecycle"], "completed")
            with self.assertRaises(sessions.LifecycleError):
                sessions.Session.resume(state_dir=directory, session_id=session.session_id)

    def test_checkpoint_handoff_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            session = make_session(state)
            record = session.checkpoint(progress="half", remaining=["rest"])
            self.assertTrue(record["identity"].startswith("chk_"))
            handoff = session.handoff(to_consumer="agent-b", next_actions=["rest"])
            self.assertTrue(handoff["identity"].startswith("hff_"))
            self.assertEqual(session.snapshot["lifecycle"], "handed_off")
            # New process resumes from disk.
            resumed = sessions.Session.resume(state_dir=state, session_id=session.session_id)
            accepted = resumed.accept_handoff(consumer_id="agent-b")
            self.assertEqual(accepted["previous_consumer"], "tester")
            self.assertEqual(resumed.snapshot["lifecycle"], "active")
            # Handoff checkpoints by design: explicit checkpoint first, handoff second.
            self.assertEqual(resumed.snapshot["checkpoints"][0], record["identity"])
            self.assertEqual(len(resumed.snapshot["checkpoints"]), 2)

    def test_events_subscribe_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = make_session(Path(directory))
            subscription = session.subscribe(["session.checkpointed", "authority.denied"])
            session.check(action="delete", target="mncs-language")
            session.checkpoint(progress="x")
            due = session.poll(subscription["subscription_id"])
            types = {event["type"] for event in due}
            self.assertIn("session.checkpointed", types)
            # Cursor advanced: second poll is empty.
            self.assertEqual(session.poll(subscription["subscription_id"]), [])

    def test_malformed_snapshot_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            (state / "sessions" / "ses_bogus").mkdir(parents=True)
            (state / "sessions" / "ses_bogus" / "session.json").write_text("{broken",
                                                                           encoding="utf-8")
            with self.assertRaises(sessions.LifecycleError):
                sessions.Session(state, "ses_bogus")

    def test_multi_session_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            first = make_session(state)
            second = make_session(state)
            self.assertNotEqual(first.session_id, second.session_id)
            first.checkpoint(progress="first")
            self.assertEqual(len(second.snapshot.get("checkpoints", [])), 0)
            first_dir = state / "sessions" / first.session_id
            second_dir = state / "sessions" / second.session_id
            self.assertTrue((first_dir / "events.jsonl").is_file())
            self.assertTrue((second_dir / "events.jsonl").is_file())

    def test_stale_checkpoint_divergence_visible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = make_session(Path(directory))
            record = session.checkpoint(progress="p")
            cursor = record["event_cursor"]
            session.record_observation("adapter.observed", "test-adapter", {"note": "later"})
            reread = sessions.Session(session.state_dir, session.session_id)
            self.assertGreater(len(reread._log()), cursor)


class EventTests(unittest.TestCase):
    def test_unknown_type_becomes_adapter(self) -> None:
        event = events.make(session_id="ses_x", sequence=1, event_type="vendor.blip",
                            producer="vendor")
        self.assertEqual(event["type"], "adapter.observed")
        self.assertEqual(event["provider_type"], "vendor.blip")

    def test_git_poll_adapter(self) -> None:
        made, _ = events.git_poll_events(
            session_id="ses_x", sequence_start=1,
            previous_heads={"a": "sha1"}, current_heads={"a": "sha2", "b": None})
        self.assertEqual(len(made), 1)
        self.assertEqual(made[0]["type"], "workspace.changed")
        self.assertEqual(made[0]["producer"], "adapter:git-poll")


if __name__ == "__main__":
    unittest.main()
