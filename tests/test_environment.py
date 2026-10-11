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
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
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
    readiness,
    reconciler,
    rights,
    sessions,
    sources,
    workspace,
)
from mncs_env.session_store import (  # noqa: E402
    SnapshotConflict,
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
    def test_claim_lease_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = open_store(directory, "file")
            for invalid in (0, -1, 169, True):
                with self.subTest(ttl_hours=invalid):
                    with self.assertRaisesRegex(ValueError, "claim TTL"):
                        claims.acquire(
                            store, repository="r", session_id="a", consumer_id="a",
                            basis=claims.BASIS_EXPLICIT, reason="bounded lease",
                            ttl_hours=invalid,
                        )
            record = claims.acquire(
                store, repository="r", session_id="a", consumer_id="a",
                basis=claims.BASIS_EXPLICIT, reason="maximum lease",
                ttl_hours=claims.MAX_TTL_HOURS,
            )
            self.assertEqual(record["status"], "held")

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

    def test_transfer_preserves_bounded_deadline_for_legacy_lease(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = open_store(directory, "file")
            now = datetime.now(timezone.utc)
            acquired = now - timedelta(hours=12)
            record = claims._record(store, {
                "schema_version": claims.SCHEMA,
                "claim_id": "claim:r",
                "version": 1,
                "repository": "r",
                "scope": claims.normalize_scope(None, "r"),
                "session_id": "a",
                "consumer_id": "a",
                "basis": claims.BASIS_EXPLICIT,
                "reason": "legacy overlong fixture",
                "status": "held",
                "acquired_at": acquired.isoformat(timespec="seconds"),
                "expires_at": (now + timedelta(hours=1000)).isoformat(
                    timespec="seconds"),
                "provenance": {"acquired_by": "a"},
            })
            expected_expiry = acquired + timedelta(hours=claims.MAX_TTL_HOURS)

            moved = claims.transfer(store, claim_id=record["claim_id"],
                                    from_session="a", to_session="b",
                                    to_consumer="b", reason="handoff")

            self.assertEqual(moved["session_id"], "b")
            self.assertEqual(moved["expires_at"],
                             expected_expiry.isoformat(timespec="seconds"))
            self.assertEqual(
                claims.lease_diagnostic(moved)["effective_expires_at"],
                expected_expiry.isoformat(timespec="seconds"),
            )
            self.assertLessEqual(
                claims.lease_diagnostic(moved)["effective_duration_hours"],
                claims.MAX_TTL_HOURS,
            )

    def test_transfer_retry_reconciles_lost_response_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = open_store(directory, "file")
            record = claims.acquire(store, repository="r", session_id="a",
                                    consumer_id="a", basis=claims.BASIS_EXPLICIT,
                                    reason="work")

            class InterruptBeforeCommit:
                def __getattr__(self, name):
                    return getattr(store, name)

                def put_claim_batch(self, batch, *, expected_generation):
                    raise OSError("caller interrupted before atomic generation commit")

            with self.assertRaisesRegex(OSError, "before atomic generation"):
                claims.transfer(InterruptBeforeCommit(), claim_id=record["claim_id"],
                                from_session="a", to_session="b", to_consumer="b",
                                reason="resume", request_id="transfer-before-commit")
            live = claims.active_claims(store.read_claims())
            self.assertEqual(live[record["claim_id"]]["session_id"], "a")

            class InterruptAfterCommit:
                fired = False

                def __getattr__(self, name):
                    return getattr(store, name)

                def put_claim_batch(self, batch, *, expected_generation):
                    store.put_claim_batch(batch, expected_generation=expected_generation)
                    if not self.fired:
                        self.fired = True
                        raise OSError("caller interrupted after atomic generation commit")

            interrupted = InterruptAfterCommit()
            with self.assertRaisesRegex(OSError, "after atomic generation"):
                claims.transfer(interrupted, claim_id=record["claim_id"],
                                from_session="a", to_session="b", to_consumer="b",
                                reason="resume", request_id="transfer-retry-proof")
            moved = claims.transfer(store, claim_id=record["claim_id"],
                                    from_session="a", to_session="b", to_consumer="b",
                                    reason="resume", request_id="transfer-retry-proof")
            self.assertEqual(moved["session_id"], "b")
            versions = [row for row in store.read_claims()
                        if row.get("claim_id") == record["claim_id"]]
            matching = [row for row in versions
                        if row.get("provenance", {}).get("transfer_request_id")
                        == "transfer-retry-proof"]
            self.assertEqual(len(matching), 2)
            live = claims.active_claims(versions)
            self.assertEqual(live[record["claim_id"]]["session_id"], "b")

    def test_old_transfer_retry_does_not_return_a_superseded_recipient(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = open_store(directory, "file")
            record = claims.acquire(store, repository="r", session_id="a",
                                    consumer_id="a", basis=claims.BASIS_EXPLICIT,
                                    reason="work")
            claims.transfer(store, claim_id=record["claim_id"],
                            from_session="a", to_session="b", to_consumer="b",
                            reason="first handoff", request_id="first-transfer")
            claims.transfer(store, claim_id=record["claim_id"],
                            from_session="b", to_session="c", to_consumer="c",
                            reason="continuation", request_id="second-transfer")

            with self.assertRaisesRegex(claims.ClaimConflict,
                                        "recipient no longer holds"):
                claims.transfer(store, claim_id=record["claim_id"],
                                from_session="a", to_session="b", to_consumer="b",
                                reason="first handoff", request_id="first-transfer")

            latest = claims.active_claims(store.read_claims())[record["claim_id"]]
            self.assertEqual(latest["session_id"], "c")

    def test_concurrent_transfers_fence_competing_recipients(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        with tempfile.TemporaryDirectory() as directory:
            store = open_store(directory, "file")
            record = claims.acquire(store, repository="r", session_id="a",
                                    consumer_id="a", basis=claims.BASIS_EXPLICIT,
                                    reason="work")

            def attempt(recipient: str):
                try:
                    return claims.transfer(
                        store, claim_id=record["claim_id"], from_session="a",
                        to_session=recipient, to_consumer=recipient,
                        request_id=f"transfer-{recipient}")
                except claims.ClaimConflict as error:
                    return error

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(attempt, ("b", "c")))
            successes = [item for item in outcomes if isinstance(item, dict)]
            conflicts = [item for item in outcomes if isinstance(item, claims.ClaimConflict)]
            self.assertEqual(len(successes), 1)
            self.assertEqual(len(conflicts), 1)
            live = claims.active_claims(store.read_claims())
            self.assertEqual(live[record["claim_id"]]["session_id"], successes[0]["session_id"])

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


class CampaignContinuityTests(unittest.TestCase):
    def test_workspace_observation_refreshes_campaign_repository_head(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "workspace"
            checkout = root / "mncs-environment"
            checkout.mkdir(parents=True)

            def git(*args: str) -> str:
                completed = subprocess.run(
                    ["git", "-C", str(checkout), *args], check=True,
                    text=True, capture_output=True,
                )
                return completed.stdout.strip()

            git("init", "-q", "-b", "main")
            git("config", "user.name", "Environment Test")
            git("config", "user.email", "environment-test@example.invalid")
            source = checkout / "source.mncs"
            source.write_text("module first;\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-q", "-m", "first campaign head")
            first_head = git("rev-parse", "HEAD")

            session = make_session(base / "state")
            try:
                repo = {
                    "name": "mncs-environment", "manifest_repository": "mncs-environment",
                    "path": str(checkout), "head": first_head, "branch": "main",
                    "dirty": False, "dirty_files": [], "dirty_truncated": False,
                }
                workspace_view = {"root": str(root), "repositories": [repo]}
                session.snapshot["workspace"] = workspace_view
                session.snapshot["selected_checkouts"] = {
                    "mncs-environment": {
                        "path": str(checkout), "head": first_head,
                        "branch": "main", "clean": True,
                    },
                }
                campaign = dict(session.snapshot.get("campaign") or {})
                campaign["repository_refs"] = session._campaign_repository_refs({})
                session.snapshot["campaign"] = campaign
                reference = session.snapshot["campaign"]["repository_refs"][0]
                self.assertEqual(reference["head"], first_head)

                source.write_text("module second;\n", encoding="utf-8")
                git("add", ".")
                git("commit", "-q", "-m", "advance campaign head")
                current_head = git("rev-parse", "HEAD")
                current_repo = {**repo, "head": current_head}
                observed_workspace = {
                    "root": str(root), "repositories": [current_repo],
                    "scan": {"status": "complete"},
                }
                with mock.patch.object(
                    sessions.workspace_module, "discover_workspace",
                    return_value=observed_workspace,
                ):
                    session.observe_workspace(root)

                current = next(
                    item for item in session.context()["continuation"]["repositories"]
                    if item["repository"] == "mncs-environment"
                )
                self.assertEqual(current["head"], current_head)
                self.assertEqual(current["branch"], "main")
                self.assertTrue(current["clean"])
                self.assertEqual(current["observation"], "current")
            finally:
                session.close()

    def test_owned_claim_rebuilds_portable_repository_capsule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "workspace"
            checkout = root / "mncs-control-mcp"
            checkout.mkdir(parents=True)

            def git(*args: str) -> str:
                completed = subprocess.run(
                    ["git", "-C", str(checkout), *args], check=True,
                    text=True, capture_output=True,
                )
                return completed.stdout.strip()

            git("init", "-q", "-b", "main")
            git("config", "user.name", "Environment Test")
            git("config", "user.email", "environment-test@example.invalid")
            (checkout / "source.mncs").write_text("module test;\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-q", "-m", "test repository identity")
            head = git("rev-parse", "HEAD")

            state_dir = base / "state"
            store = open_store(state_dir, "file")
            environment = {
                "identity": "env_test_identity",
                "intent": {
                    "identity": "intent_test_identity",
                    "goal": "resume an owned repository campaign",
                    "repositories": ["mncs-control-mcp"],
                    "protected_repositories": [],
                },
                "workspace": {"root": str(root), "repositories": []},
                "selected_checkouts": {},
                "rights": {},
                "authority": {},
            }
            session = sessions.Session.create(
                state_dir=state_dir, environment=environment,
                consumer_id="process-one", consumer_kind="agent", backend="file",
                campaign_id="cmp_test_campaign", authenticated_principal_id="principal-test",
                store=store,
            )
            try:
                session.acquire_claim(
                    "mncs-control-mcp", basis=claims.BASIS_EXPLICIT,
                    reason="test durable campaign checkout association",
                    workspace_root=str(root),
                )
                session.snapshot["campaign"]["repository_refs"] = []
                session._save()
                session.close()
                session = sessions.Session.open(
                    state_dir=state_dir, session_id=session.session_id, backend="file"
                )
                recovered = session.reconcile_campaign_continuity()
                self.assertTrue(recovered["changed"])
                continuation = session.context()["continuation"]
                self.assertEqual(continuation["identity"], "cmp_test_campaign")
                self.assertEqual(
                    continuation["work_intent"]["goal"],
                    "resume an owned repository campaign",
                )
                reference = next(
                    item for item in continuation["repositories"]
                    if item["repository"] == "mncs-control-mcp"
                )
                self.assertEqual(reference["observed_path"], "mncs-control-mcp")
                self.assertEqual(reference["branch"], "main")
                self.assertEqual(reference["head"], head)
                self.assertTrue(reference["clean"])
                self.assertTrue(reference["git_common_directory_identity"].startswith("git-common:"))
                self.assertTrue(reference["checkout_identity"].startswith("checkout:"))
            finally:
                session.close()

    def test_continuation_capsule_projects_latest_checkpoint_and_foreign_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            session = make_session(base / "state")
            try:
                session.checkpoint(progress="older checkpoint", remaining=["old task"])
                latest = session.checkpoint(
                    progress="p" * 1000,
                    remaining=["r" * 300, *[f"pending-{index}" for index in range(10)]],
                )

                private_checkout = base / "foreign-owner-private-checkout"
                private_checkout.mkdir()
                foreign = claims.acquire(
                    session.store,
                    repository="mncs-compiler",
                    session_id="ses_foreign_campaign_owner",
                    consumer_id="compiler-campaign-owner",
                    basis=claims.BASIS_EXPLICIT,
                    reason="continuation capsule projection test",
                    scope={
                        "kind": "worktree",
                        "checkout": str(private_checkout),
                        "branch": "campaign/protected-worktree",
                        "exclusive": True,
                    },
                    checkout_facts={
                        "head": "test-head", "branch": "campaign/protected-worktree",
                        "dirty": False, "foreign_signals": [],
                    },
                )

                # A fresh handle proves the capsule comes from durable Store
                # records rather than only the writer's in-memory state.
                reopened = sessions.Session.open(
                    state_dir=base / "state", session_id=session.session_id, **FAST
                )
                capsule = reopened.context()["continuation"]
                checkpoint = capsule["latest_checkpoint"]
                self.assertEqual(checkpoint["identity"], latest["identity"])
                self.assertEqual(checkpoint["sequence"], 2)
                self.assertEqual(len(checkpoint["progress"]), 320)
                self.assertTrue(checkpoint["progress_truncated"])
                self.assertEqual(checkpoint["remaining_count"], 11)
                self.assertTrue(checkpoint["remaining_truncated"])
                self.assertEqual(len(checkpoint["remaining"]), 8)
                self.assertEqual(len(checkpoint["remaining"][0]), 200)

                self.assertEqual(capsule["foreign_claims_count"], 1)
                self.assertFalse(capsule["foreign_claims_truncated"])
                projected = capsule["foreign_claims"][0]
                self.assertEqual(projected["claim_id"], foreign["claim_id"])
                self.assertEqual(projected["version"], foreign["version"])
                self.assertEqual(projected["owner_session_id"], "ses_foreign_campaign_owner")
                self.assertEqual(projected["owner_consumer_id"], "compiler-campaign-owner")
                self.assertFalse(projected["workspace_related"])
                self.assertEqual(
                    projected["effective_expires_at"],
                    claims.lease_diagnostic(foreign)["effective_expires_at"],
                )
                self.assertEqual(projected["scope"], {
                    "kind": "worktree", "exclusive": True,
                    "branch": "campaign/protected-worktree", "checkout_bound": True,
                })
                self.assertNotIn(str(private_checkout), json.dumps(capsule))
            finally:
                session.close()


class StoreTransportDiagnosticsTests(unittest.TestCase):
    def test_rpc_client_maps_claim_conflicts_from_environment_cli(self) -> None:
        from mncs_env import rpc_client

        with tempfile.TemporaryDirectory() as directory:
            grant = Path(directory) / "grant"
            grant.write_text("test-grant", encoding="ascii")
            with mock.patch.dict(os.environ, {
                "MNCS_ENV_RPC_SOCKET": "/run/test.sock",
                "MNCS_ENV_RPC_GRANT_FILE": str(grant),
            }):
                with mock.patch.object(rpc_client, "_exchange", return_value={
                    "ok": True,
                    "result": {
                        "exit_code": 3,
                        "stdout": "",
                        "stderr": json.dumps({
                            "error": "claim changed concurrently",
                            "diagnostics": {"code": "claim-conflict"},
                        }),
                    },
                }), redirect_stdout(io.StringIO()) as output:
                    code = rpc_client.dispatch(["claims", "ses_test", "--acquire", "r"])
            self.assertEqual(code, 3)
            self.assertEqual(
                json.loads(output.getvalue())["diagnostics"]["code"],
                "publication-conflict",
            )

    def test_rpc_client_preserves_bounded_campaign_continuation_choices(self) -> None:
        from mncs_env import rpc_client

        continuations = [
            {
                "session_id": f"ses_candidate_{index}",
                "lifecycle": "active",
                "consumer_id": f"consumer_{index}",
                "claim_ids": [f"claim:{index}"],
                "repositories": [{"repository": "mncs-environment", "head": "abc"}],
            }
            for index in range(10)
        ]
        failed = {
            "error": "multiple live Environment sessions share this campaign identity",
            "diagnostics": {
                "code": "campaign-continuation-ambiguous",
                "campaign_id": "cmp_restart_test",
                "sessions": [f"ses_candidate_{index}" for index in range(24)],
                "continuations": continuations,
                "next": "resume the intended recorded session",
                "credential": "must not cross the Environment RPC boundary",
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            grant = Path(directory) / "grant"
            grant.write_text("test-grant", encoding="ascii")
            with mock.patch.dict(os.environ, {
                "MNCS_ENV_RPC_SOCKET": "/run/test.sock",
                "MNCS_ENV_RPC_GRANT_FILE": str(grant),
            }):
                with mock.patch.object(rpc_client, "_exchange", return_value={
                    "ok": True,
                    "result": {
                        "exit_code": 2,
                        "stdout": "",
                        "stderr": json.dumps(failed),
                    },
                }), redirect_stdout(io.StringIO()) as output:
                    code = rpc_client.dispatch(["enter"])

        self.assertEqual(code, 2)
        result = json.loads(output.getvalue())
        diagnostics = result["diagnostics"]
        details = diagnostics["domain_details"]
        self.assertEqual(diagnostics["code"], "publication-rejected")
        self.assertEqual(diagnostics["cause_code"], "campaign-continuation-ambiguous")
        self.assertEqual(details["cause_code"], "campaign-continuation-ambiguous")
        self.assertEqual(details["campaign_id"], "cmp_restart_test")
        self.assertEqual(len(details["sessions"]), rpc_client.MAX_DIAGNOSTIC_ITEMS)
        self.assertTrue(details["sessions_truncated"])
        self.assertEqual(len(details["continuations"]), 8)
        self.assertTrue(details["continuations_truncated"])
        self.assertNotIn("credential", json.dumps(result))

    def test_campaign_continuation_diagnostics_have_a_total_size_bound(self) -> None:
        from mncs_env import rpc_client

        detail = {
            "code": "campaign-continuation-ambiguous",
            "campaign_id": "cmp_restart_test",
            "sessions": ["ses_left", "ses_right"],
            "continuations": [
                {"session_id": f"ses_{index}", "extra": {
                    f"field_{field}": "x" * 512 for field in range(32)
                }}
                for index in range(8)
            ],
        }
        bounded = rpc_client._domain_diagnostic_details(
            "campaign-continuation-ambiguous", detail
        )

        self.assertIsNotNone(bounded)
        self.assertLessEqual(
            len(json.dumps(bounded, separators=(",", ":")).encode()),
            rpc_client.MAX_DOMAIN_DIAGNOSTIC_BYTES,
        )
        self.assertTrue(bounded["details_truncated"])
        self.assertNotIn("continuations", bounded)
        self.assertEqual(bounded["sessions"], ["ses_left", "ses_right"])

    def test_foreign_campaign_diagnostics_do_not_forward_session_ids(self) -> None:
        from mncs_env import rpc_client

        bounded = rpc_client._domain_diagnostic_details(
            "campaign-owner-conflict",
            {
                "code": "campaign-owner-conflict",
                "campaign_id": "cmp_foreign_owner",
                "sessions": ["ses_foreign_owner"],
                "continuation": {"session_id": "ses_foreign_owner"},
            },
        )

        self.assertEqual(bounded, {
            "cause_code": "campaign-owner-conflict",
            "campaign_id": "cmp_foreign_owner",
        })

    def test_read_only_filesystem_promotion_has_structured_category(self) -> None:
        import errno
        from mncs_env.store_backend import StoreBackend, StoreUnavailable

        class ReadOnlyOwner:
            read_only = True
            session = object()

        def promote(*_args, **_kwargs):
            raise OSError(errno.EROFS, "Read-only file system")

        backend = StoreBackend.__new__(StoreBackend)
        backend._store = ReadOnlyOwner()
        backend._api = (promote, None, None)
        backend.path = Path("/canonical/store")
        backend._promotion_owner = None
        with self.assertRaises(StoreUnavailable) as raised:
            backend._ensure_writable()
        self.assertEqual(raised.exception.code, "direct-filesystem-read-only")


class WorkspaceTests(unittest.TestCase):
    def test_discovers_real_repositories(self) -> None:
        view = workspace.discover_workspace(FAMILY, repositories=[name for name in ("mncs-atlas", "mncs-compiler", "mncs-language") if (FAMILY / name / ".git").exists()])
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
    def test_forge_readiness_binds_its_language_service_cursor(self) -> None:
        definition = json.loads((ROOT / ".mncs" / "environment.json").read_text())
        service = next(
            item
            for item in definition["services"]
            if item.get("identity") == "mncs-forge:resident-workspace"
        )
        self.assertEqual(
            service["ready_when"],
            {"/state": "ready", "/continuous_consumer/state": "ready"},
        )

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
            bindings = capabilities.discover_capabilities(FAMILY, repository_roots={"mncs-test": test_root, "mncs-language": FAMILY / "mncs-language"})
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

    def test_unresolvable_invocation_names_its_cause(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "demo"
            (repo / "bin").mkdir(parents=True)
            (repo / "bin" / "run.sh").write_text("#!/bin/sh\n")
            entry = {"invocation": {"kind": "executable", "path": "bin/run.sh",
                                    "toolchain": "toolchain/mncs"}}
            resolved = capabilities.descriptor_invocation(entry, repo, root)
            self.assertEqual(resolved["addressing"], "none")
            self.assertIn("toolchain", resolved["detail"])
            binding = capabilities.bind(
                provider="p", capability="c", contract_revision="1",
                entrypoint="undeclared", address=None,
                provenance={"addressing": "none",
                            "addressing_detail": resolved["detail"]})
            probed = capabilities.probe_availability(binding)
            self.assertEqual(probed["availability"]["code"],
                             "provider-invocation-unresolvable")
            self.assertIn("toolchain", probed["availability"]["reason"])
            plain = capabilities.probe_availability(
                capabilities.bind(provider="p", capability="c",
                                  contract_revision="1",
                                  entrypoint="undeclared", address=None))
            self.assertEqual(plain["availability"]["code"],
                             "provider-invocation-undeclared")

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
                    for binding in capabilities.discover_capabilities(FAMILY, repository_roots={"mncs-test": FAMILY / "mncs-test", "mncs-language": FAMILY / "mncs-language"})]
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
        bindings = capabilities.discover_capabilities(FAMILY, repository_roots={"mncs-compiler": FAMILY / "mncs-compiler", "mncs-language": FAMILY / "mncs-language"})
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
    def test_health_blocks_when_selected_compiler_checkout_revision_drifts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "mncs-compiler"
            checkout.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(checkout)], check=True)
            source = checkout / "source.txt"
            source.write_text("first revision\n")
            subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
            subprocess.run([
                "git", "-C", str(checkout), "-c", "user.name=Environment Test",
                "-c", "user.email=environment-test@example.invalid",
                "commit", "-qm", "first revision",
            ], check=True)
            selected_head = subprocess.run(
                ["git", "-C", str(checkout), "rev-parse", "HEAD"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
            executable = root / "compiler-producer"
            executable.write_bytes(b"selected compiler executable")
            capability = "mncs-compiler:compiler-producer"
            binding = {
                "capability": capability,
                "binding_id": "selected-compiler-binding",
                "provider": "mncs-compiler",
                "contract_revision": "1",
                "toolchain_address": str(executable),
                "provenance": {"checkout": {
                    "path": str(checkout), "head": selected_head,
                    "authoritative_head": selected_head, "branch": "main", "clean": True,
                }},
                "availability": {"status": "available", "code": "available"},
            }
            selected = {"mncs-compiler": {
                "path": str(checkout), "head": selected_head,
                "authoritative_head": selected_head, "branch": "main", "clean": True,
            }}
            session = object.__new__(sessions.Session)
            session.session_id = "selection-drift-proof"
            session.snapshot = {
                "environment_id": "environment-proof",
                "execution_roles": {"compiler": {"capability": capability}},
                "execution_compatibility_service": None,
                "execution_stack": {},
                "bindings": [binding],
                "selected_checkouts": selected,
                "toolchain": {},
                "requirements": {"required_capabilities": [], "services": []},
                "service_observations": [],
                "workspace": {"root": str(root), "selection": []},
            }
            observed_workspace = {"root": str(root), "scan": {"status": "complete"},
                                  "repositories": []}
            patches = (
                mock.patch.object(sessions.capabilities_module, "probe_availability",
                                  return_value=binding),
                mock.patch.object(sessions.readiness_module, "probe_services", return_value=[]),
                mock.patch.object(sessions.workspace_module, "discover_workspace",
                                  return_value=observed_workspace),
                mock.patch.object(sessions.Session, "actions", return_value={}),
            )
            with patches[0], patches[1], patches[2], patches[3]:
                current = session.health(live=True)
                self.assertEqual(current["execution_stack"]["selection_status"], "current")
                source.write_text("second revision\n")
                subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
                subprocess.run([
                    "git", "-C", str(checkout), "-c", "user.name=Environment Test",
                    "-c", "user.email=environment-test@example.invalid",
                    "commit", "-qm", "second revision",
                ], check=True)
                stale = session.health(live=True)
            self.assertEqual(stale["execution_stack"]["selection_status"], "stale")
            self.assertEqual(stale["execution_stack"]["compatibility"]["state"], "unproven")
            self.assertEqual(stale["readiness"]["status"], "blocked")
            self.assertIn("selected-checkout-drift:mncs-compiler",
                          stale["readiness"]["blocking"])
            self.assertIn("revision-changed",
                          stale["readiness"]["selection_drift"][0]["reasons"])

    def test_effect_target_resolves_repository_only_to_selected_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "mncs-reference-studies"
            checkout.mkdir()
            session = make_session(root / "state")
            session.snapshot["workspace"] = {"root": str(root), "repositories": []}
            session.snapshot["selected_checkouts"] = {
                "mncs-reference-studies": {
                    "path": str(checkout),
                    "branch": "main",
                }
            }

            repository, scope = session._admit_effect_target(
                "mncs-forge:resident-reconcile",
                {"repository": "mncs-reference-studies"},
            )

            self.assertEqual(repository, "mncs-reference-studies")
            self.assertEqual(scope["kind"], "worktree")
            self.assertEqual(scope["checkout"], str(checkout))
            self.assertEqual(scope["branch"], "main")

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

    def test_readiness_probe_gets_bounded_session_artifact_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = make_session(Path(directory))
            binding = capabilities.bind(
                provider="mncs-doctor", capability="doctor-health",
                contract_revision="1", entrypoint="health", address="/bin/echo",
                effects=["read"],
            )
            binding["availability"] = {"status": "available"}
            session.snapshot["bindings"] = [binding]
            session.snapshot["execution_compatibility_service"] = "doctor-coherence"
            session.snapshot["execution_stack"] = {"identity": "selected-stack-identity"}
            session.snapshot["requirements"] = {
                "services": [{
                    "identity": "doctor-coherence", "required": True,
                    "probe": {"capability": "doctor-health", "argv": ["--smoke"]},
                    "probe_timeout_seconds": 42,
                    "response_schema": "mncs.doctor.compiler-vm/1",
                    "ready_when": {"/status": "pass"},
                    "response_max_bytes": 65536,
                }],
            }
            with mock.patch.object(authority, "evaluate", return_value={"verdict": "allow"}), mock.patch.object(
                readiness.capabilities, "invoke",
                return_value={"status": "ok", "returncode": 0,
                              "stdout": json.dumps({"schema_version": "mncs.doctor.compiler-vm/1",
                                                    "status": "pass",
                                                    "components": {"compiler": {"state": "ready"}}})},
            ) as invoke:
                result = readiness.probe_services(session)
            self.assertEqual(result[0]["status"], "ready")
            self.assertEqual(result[0]["composition_identity"], "selected-stack-identity")
            self.assertEqual(result[0]["response_schema"], "mncs.doctor.compiler-vm/1")
            self.assertEqual(result[0]["provider_components"], {"compiler": {"state": "ready"}})
            self.assertEqual(invoke.call_args.kwargs["output_limit_bytes"], 65536)
            self.assertEqual(invoke.call_args.kwargs["timeout_seconds"], 42)
            environment = invoke.call_args.kwargs["env"]
            artifact_root = Path(environment["MNCS_ENV_SESSION_ARTIFACT_DIR"])
            self.assertEqual(artifact_root.parent.parent.parent.name, "sessions")
            self.assertIn(session.session_id, artifact_root.parts)
            self.assertFalse(artifact_root.exists(), "a read-only readiness probe must not create its cache")

    def test_readiness_preserves_structured_failure_from_nonzero_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = make_session(Path(directory))
            binding = capabilities.bind(
                provider="mncs-doctor", capability="doctor-health",
                contract_revision="1", entrypoint="health", address="/bin/echo",
                effects=["read"],
            )
            binding["availability"] = {"status": "available"}
            session.snapshot["bindings"] = [binding]
            session.snapshot["requirements"] = {
                "services": [{
                    "identity": "doctor-coherence", "required": True,
                    "probe": {"capability": "doctor-health", "argv": ["--smoke"]},
                    "probe_timeout_seconds": 42,
                    "response_schema": "mncs.doctor.compiler-vm/1",
                    "ready_when": {"/status": "pass"},
                }],
            }
            report = {
                "schema_version": "mncs.doctor.compiler-vm/1",
                "status": "fail",
                "reason": "selected Store artifact directory is read-only",
                "components": {"runtime": {"build_origin": {"status": "matches-embedded-inputs"}}},
            }
            with mock.patch.object(authority, "evaluate", return_value={"verdict": "allow"}), mock.patch.object(
                readiness.capabilities, "invoke",
                return_value={"status": "failed", "returncode": 2,
                              "stdout": json.dumps(report), "stderr": ""},
            ):
                result = readiness.probe_services(session)

            self.assertEqual(result[0]["status"], "unavailable")
            self.assertEqual(result[0]["code"], "service-probe-failed")
            self.assertEqual(result[0]["provider_process_status"], "failed")
            self.assertEqual(result[0]["provider_status"], "fail")
            self.assertEqual(result[0]["provider_reason"], "selected Store artifact directory is read-only")
            self.assertIn("read-only", result[0]["reason"])
            self.assertEqual(
                result[0]["provider_components"],
                {"runtime": {"build_origin": {"status": "matches-embedded-inputs"}}},
            )

    def test_readiness_preserves_language_service_recovery_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = make_session(Path(directory))
            binding = capabilities.bind(
                provider="mncs-language-service", capability="resident-status",
                contract_revision="1", entrypoint="status", address="/bin/echo",
                effects=["read"],
            )
            binding["availability"] = {"status": "available"}
            session.snapshot["bindings"] = [binding]
            session.snapshot["requirements"] = {
                "services": [{
                    "identity": "language-service", "required": True,
                    "probe": {"capability": "resident-status", "argv": ["status"]},
                    "response_schema": "mncs.language-service.resident-status/1",
                    "ready_when": {"/ready": True},
                }],
            }
            recovery = {
                "schema_version": "mncs.language-service.recovery/1",
                "disposition": "operator-action-required",
                "code": "socket-bind-denied",
                "socket_path": "/workspace/.mncs/mnls-language-service.sock",
                "automatic_remediation": False,
                "action": "Permit AF_UNIX bind in the selected host context.",
            }
            response = {
                "schema_version": "mncs.language-service.resident-status/1",
                "ready": False,
                "state": "unreachable",
                "detail": "resident socket bind failed: Operation not permitted",
                "recovery": recovery,
            }
            with mock.patch.object(authority, "evaluate", return_value={"verdict": "allow"}), mock.patch.object(
                readiness.capabilities, "invoke",
                return_value={"status": "ok", "returncode": 0,
                              "stdout": json.dumps(response), "stderr": ""},
            ):
                result = readiness.probe_services(session)

            self.assertEqual(result[0]["status"], "degraded")
            self.assertEqual(result[0]["code"], "service-not-ready")
            self.assertEqual(result[0]["provider_state"], "unreachable")
            self.assertEqual(result[0]["provider_detail"], response["detail"])
            self.assertIn("Operation not permitted", result[0]["reason"])
            self.assertEqual(result[0]["provider_recovery"], recovery)

    def test_readiness_passes_selected_service_endpoint_to_dependent_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = make_session(Path(directory))
            services = [
                {
                    "identity": "language-service",
                    "required": True,
                    "probe": {"capability": "language-status"},
                    "response_schema": "mncs.language-service.resident-status/1",
                    "ready_when": {"/ready": True},
                },
                {
                    "identity": "forge-resident",
                    "required": False,
                    "probe": {"capability": "forge-status"},
                    "response_schema": "mncs.forge.resident-status/1",
                    "ready_when": {"/state": "ready"},
                    "environment_from_service": {
                        "MNLS_SERVICE_SOCKET": {
                            "service": "language-service",
                            "pointer": "/provider_observed/event_transport/socket",
                        },
                        "MNLS_SERVICE_STREAM_IDENTITY": {
                            "service": "language-service",
                            "pointer": "/provider_observed/stream_identity",
                        },
                        "MNLS_SERVICE_WORKSPACE_ROOT": {
                            "service": "language-service",
                            "pointer": "/provider_observed/event_transport/workspace",
                        },
                        "MNLS_SERVICE_REPOSITORY_ROOTS_JSON": {
                            "service": "language-service",
                            "pointer": "/provider_observed/workspace_repository_roots",
                        },
                    },
                },
            ]
            session.snapshot["requirements"] = {"services": services}
            session.snapshot["bindings"] = [
                capabilities.probe_availability(
                    capabilities.bind(
                        provider="fixture",
                        capability=capability,
                        contract_revision="1",
                        entrypoint=capability,
                        address="/bin/true",
                        effects=["read"],
                    )
                )
                for capability in ("language-status", "forge-status")
            ]
            language_status = {
                "schema_version": "mncs.language-service.resident-status/1",
                "ready": True,
                "observed": {
                    "event_transport": {
                        "socket": "/selected/workspace/.mncs/mnls.sock",
                        "workspace": "/selected/workspace",
                    },
                    "stream_identity": "mnls-stream-selected",
                    "workspace_repository_roots": ["/selected/workspace/mncs-forge"],
                },
            }
            forge_status = {
                "schema_version": "mncs.forge.resident-status/1",
                "state": "ready",
            }
            with mock.patch.object(
                authority, "evaluate", return_value={"verdict": "allow"}
            ), mock.patch.object(
                readiness.capabilities,
                "invoke",
                side_effect=[
                    {
                        "status": "ok",
                        "returncode": 0,
                        "stdout": json.dumps(language_status),
                    },
                    {
                        "status": "ok",
                        "returncode": 0,
                        "stdout": json.dumps(forge_status),
                    },
                ],
            ) as invoke:
                result = readiness.probe_services(session)

            self.assertEqual([item["status"] for item in result], ["ready", "ready"])
            passed_environment = invoke.call_args_list[1].kwargs["env"]
            self.assertEqual(
                passed_environment["MNLS_SERVICE_SOCKET"],
                "/selected/workspace/.mncs/mnls.sock",
            )
            self.assertEqual(
                passed_environment["MNLS_SERVICE_STREAM_IDENTITY"],
                "mnls-stream-selected",
            )
            self.assertEqual(
                passed_environment["MNLS_SERVICE_REPOSITORY_ROOTS_JSON"],
                '["/selected/workspace/mncs-forge"]',
            )

    def test_readiness_response_limit_is_bounded(self) -> None:
        service = {
            "identity": "doctor-coherence", "probe": {"capability": "doctor-health"},
            "response_schema": "mncs.doctor.compiler-vm/1", "ready_when": {"/status": "pass"},
            "response_max_bytes": capabilities.MAX_OUTPUT_LIMIT_BYTES + 1,
        }
        with self.assertRaisesRegex(ValueError, "response_max_bytes"):
            readiness.validate_requirements({"services": [service]})

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
            session.store = mock.Mock(state_dir=root / "state")
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
            self.assertEqual(selected["MNCS_STORE_ARTIFACT_CACHE"], str(root / "state" / "provider-cache" / "mncs-store"))
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
            # This case isolates path mapping; provide current transport facts
            # for its synthetic checkout and manually supplied claim fixture.
            with mock.patch.object(session, "_refresh_holders"), mock.patch.object(
                sessions.workspace_module, "inspect_repo", return_value=sessions.workspace_module.RepoState(
                    name="MNCS-Commons", path=str(checkout), manifest_repository="MNCS-Commons",
                    manifest_revision=1, branch="campaign/commons-parity", head="fixture", dirty=False)), mock.patch.object(
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
            # This case isolates path mapping; provide current transport facts
            # for its synthetic checkout and manually supplied claim fixture.
            with mock.patch.object(session, "_refresh_holders"), mock.patch.object(
                sessions.workspace_module, "inspect_repo", return_value=sessions.workspace_module.RepoState(
                    name="MNCS-Commons", path=str(checkout), manifest_repository="MNCS-Commons",
                    manifest_revision=1, branch="campaign/commons-parity", head="fixture", dirty=False)), mock.patch.object(
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
                sessions.workspace_module, "inspect_repo", return_value=sessions.workspace_module.RepoState(
                    name="mncs-store", path=str(checkout), manifest_repository="mncs-store",
                    manifest_revision=1, branch="campaign/store", head="fixture", dirty=True)), mock.patch.object(
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

    def test_checkpoint_retry_after_restart_reads_original_operation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            session = make_session(state)
            first = session.checkpoint(
                progress="pause after compiler changes",
                remaining=["resume validation"],
                request_id="req_checkpoint_restart_replay",
            )
            session.close()

            restarted = sessions.Session.open(
                state_dir=state, session_id=session.session_id, **FAST
            )
            restarted.snapshot["artifacts"].append({"identity": "later-work"})
            restarted._save()
            replay = restarted.checkpoint(
                progress="pause after compiler changes",
                remaining=["resume validation"],
                request_id="req_checkpoint_restart_replay",
            )
            self.assertEqual(replay["identity"], first["identity"])
            matching = [record for record in restarted.store.list_checkpoints(
                restarted.session_id
            ) if record.get("request_id") == "req_checkpoint_restart_replay"]
            self.assertEqual(
                [record["identity"] for record in matching], [first["identity"]]
            )
            checkpoint_events = [event for event in restarted._log()
                                 if event.get("type") == "session.checkpointed"
                                 and event.get("payload", {}).get("checkpoint_id")
                                 == first["identity"]]
            self.assertEqual(len(checkpoint_events), 1)
            with self.assertRaisesRegex(
                sessions.LifecycleError, "request identity is already bound"
            ):
                restarted.checkpoint(
                    progress="different operation",
                    remaining=["resume validation"],
                    request_id="req_checkpoint_restart_replay",
                )

    def test_authenticated_cross_principal_handoff_is_fenced_and_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            session = make_session(state)
            session.snapshot["authenticated_principal_id"] = "ctrl_source"
            session.snapshot["campaign"]["principal_id"] = "ctrl_source"
            session._save()
            handoff = session.handoff(
                to_consumer="agent-receiver",
                to_authenticated_principal_id="ctrl_receiver",
                request_id="req_handoff_checkpoint",
            )

            receiver = sessions.Session.open(
                state_dir=state, session_id=session.session_id, **FAST
            )
            with self.assertRaisesRegex(
                sessions.LifecycleError, "recipient principal does not match"
            ):
                receiver.accept_handoff(
                    handoff["identity"], consumer_id="agent-receiver",
                    authenticated_principal_id="ctrl_impostor",
                )

            accepted = receiver.accept_handoff(
                handoff["identity"], consumer_id="agent-receiver",
                authenticated_principal_id="ctrl_receiver",
                request_id="req_accept_handoff",
            )
            self.assertEqual(accepted["previous_consumer"], "tester")
            self.assertEqual(receiver.snapshot["authenticated_principal_id"], "ctrl_receiver")
            self.assertEqual(receiver.snapshot["lifecycle"], "active")
            self.assertIsNone(receiver.snapshot["pending_handoff_id"])

            restarted = sessions.Session.open(
                state_dir=state, session_id=session.session_id, **FAST
            )
            self.assertEqual(
                restarted.accept_handoff(
                    handoff["identity"], consumer_id="agent-receiver",
                    authenticated_principal_id="ctrl_receiver",
                    request_id="req_accept_handoff",
                ),
                accepted,
            )
            with self.assertRaisesRegex(sessions.LifecycleError, "already accepted"):
                restarted.accept_handoff(
                    handoff["identity"], consumer_id="agent-receiver",
                    authenticated_principal_id="ctrl_receiver",
                    request_id="req_accept_handoff_again",
                )

    def test_handoff_accept_retry_repairs_missing_resume_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            session = make_session(state)
            handoff = session.handoff(to_consumer="agent-b")
            receiver = sessions.Session.open(
                state_dir=state, session_id=session.session_id, **FAST
            )
            emit = receiver._emit

            def interrupt_resume_event(event_type, producer, payload=None, causes=None):
                if event_type == "session.resumed":
                    raise RuntimeError("simulated process interruption after owner fence")
                return emit(event_type, producer, payload, causes)

            receiver._emit = interrupt_resume_event
            with self.assertRaisesRegex(RuntimeError, "simulated process interruption"):
                receiver.accept_handoff(
                    handoff["identity"], consumer_id="agent-b",
                    request_id="req_interrupted_accept",
                )

            restarted = sessions.Session.open(
                state_dir=state, session_id=session.session_id, **FAST
            )
            restarted.reconcile_campaign_continuity()
            accepted = restarted.accept_handoff(
                handoff["identity"], consumer_id="agent-b",
                request_id="req_interrupted_accept",
            )
            resumed = [event for event in restarted._log()
                       if event.get("type") == "session.resumed"
                       and event.get("payload", {}).get("handoff_id") == handoff["identity"]]
            self.assertEqual(len(resumed), 1)
            self.assertEqual(accepted["consumer_id"], "agent-b")

    def test_handoff_retry_repairs_publication_and_later_handoff_gets_new_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            session = make_session(state)
            session.snapshot["authenticated_principal_id"] = "ctrl_source"
            session.snapshot["campaign"]["principal_id"] = "ctrl_source"
            session._save()
            emit = session._emit

            def interrupt_handoff_event(event_type, producer, payload=None, causes=None):
                if event_type == "handoff.created":
                    raise RuntimeError("simulated process interruption after handoff fence")
                return emit(event_type, producer, payload, causes)

            session._emit = interrupt_handoff_event
            with self.assertRaisesRegex(RuntimeError, "after handoff fence"):
                session.handoff(
                    to_consumer="agent-receiver",
                    to_authenticated_principal_id="ctrl_receiver",
                    request_id="req_handoff_creation",
                )

            restarted = sessions.Session.resume(
                state_dir=state, session_id=session.session_id, **FAST
            )
            recovered = restarted.handoff(
                to_consumer="agent-receiver",
                to_authenticated_principal_id="ctrl_receiver",
                request_id="req_handoff_creation",
            )
            created_events = [event for event in restarted._log()
                              if event.get("type") == "handoff.created"
                              and event.get("payload", {}).get("handoff_id") == recovered["identity"]]
            self.assertEqual(len(created_events), 1)

            restarted.accept_handoff(
                recovered["identity"], consumer_id="agent-receiver",
                authenticated_principal_id="ctrl_receiver",
                request_id="req_handoff_acceptance",
            )
            next_owner = sessions.Session.open(
                state_dir=state, session_id=session.session_id, **FAST
            )
            later = next_owner.handoff(
                to_consumer="agent-receiver",
                to_authenticated_principal_id="ctrl_receiver",
                request_id="req_handoff_after_acceptance",
            )
            self.assertNotEqual(later["identity"], recovered["identity"])

    def test_competing_handoff_accepts_have_one_store_cas_winner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            session = make_session(state)
            session.snapshot["authenticated_principal_id"] = "ctrl_source"
            session.snapshot["campaign"]["principal_id"] = "ctrl_source"
            session._save()
            handoff = session.handoff(
                to_consumer="agent-receiver",
                to_authenticated_principal_id="ctrl_receiver",
            )
            first = sessions.Session.open(
                state_dir=state, session_id=session.session_id, **FAST
            )
            second = sessions.Session.open(
                state_dir=state, session_id=session.session_id, **FAST
            )
            start = threading.Barrier(2)

            def accept(candidate, request_id):
                start.wait(timeout=10)
                try:
                    candidate.accept_handoff(
                        handoff["identity"], consumer_id="agent-receiver",
                        authenticated_principal_id="ctrl_receiver",
                        request_id=request_id,
                    )
                    return "accepted"
                except SnapshotConflict:
                    return "snapshot-conflict"

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(
                    lambda item: accept(*item),
                    ((first, "req_competing_accept_one"),
                     (second, "req_competing_accept_two")),
                ))
            first.close()
            second.close()

            reread = sessions.Session.open(
                state_dir=state, session_id=session.session_id, **FAST
            )
            self.assertEqual(outcomes.count("accepted"), 1, outcomes)
            self.assertEqual(reread.snapshot["authenticated_principal_id"], "ctrl_receiver")
            self.assertEqual(
                [item["handoff_id"] for item in reread.snapshot["accepted_handoffs"]],
                [handoff["identity"]],
            )

    def test_authenticated_campaign_capsule_survives_fresh_session_object(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            store = open_store(state, "file")
            environment = sessions.resolve_environment(
                definition=definition(), workspace_root=ROOT, state_dir=state,
                consumer_id="agent-first", backend="file", store=store)
            session = sessions.Session.create(
                state_dir=state, environment=environment, consumer_id="agent-first",
                campaign_id="cmp_restart-proof", authenticated_principal_id="principal-test",
                backend="file", store=store)
            session.transition("resolving", "test")
            session.transition("ready", "test")
            session.transition("active", "test")
            identity = session.session_id

            resumed = sessions.Session.open(
                state_dir=state, session_id=identity, backend="file")
            resumed.continue_as("agent-after-restart", "agent")
            context = resumed.context()
            capsule = context["continuation"]
            self.assertEqual(capsule["identity"], "cmp_restart-proof")
            self.assertEqual(context["consumer_id"], "agent-after-restart")
            self.assertIn("agent-first", capsule["previous_consumers"])
            self.assertIn("agent-after-restart",
                          resumed.snapshot["campaign"]["authorized_consumers"])
            self.assertEqual(resumed.snapshot["authenticated_principal_id"], "principal-test")

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

    def test_checkpoint_request_retry_reads_back_one_immutable_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = make_session(Path(directory))
            first = session.checkpoint(
                progress="saved", remaining=["continue"], request_id="req_checkpoint-proof")
            second = session.checkpoint(
                progress="saved", remaining=["continue"], request_id="req_checkpoint-proof")
            self.assertEqual(first, second)
            self.assertEqual(session.snapshot["checkpoints"].count(first["identity"]), 1)
            events = [item for item in session.store.read_events(session.session_id)
                      if item.get("type") == "session.checkpointed"
                      and item.get("payload", {}).get("request_id") == "req_checkpoint-proof"]
            self.assertEqual(len(events), 1)

    def test_campaign_evidence_and_delivery_are_checkpointed_and_reconstructed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            session = make_session(state_dir)
            session.snapshot["campaign"]["identity"] = "cmp_durable_evidence_test"
            session.snapshot["authenticated_principal_id"] = "principal-test"
            session._save()
            commit = "a" * 40
            evidence = [{
                "identity": "ev_environment_delivery",
                "repository": "mncs-environment",
                "commit": commit,
                "path": "evidence/campaign-delivery.json",
                "sha256": "b" * 64,
            }]
            delivery = {
                "status": "delivered",
                "repositories": [{
                    "repository": "mncs-environment",
                    "branch": "main",
                    "head": commit,
                    "status": "pushed",
                    "remote_ref": "origin/main",
                    "remote_head": commit,
                    "evidence_identity": "ev_environment_delivery",
                }],
            }
            checkpoint = session.checkpoint(
                progress="Environment delivery recorded",
                request_id="req_campaign_state_delivery",
                campaign_base_checkpoint="none",
                campaign_evidence=evidence,
                campaign_delivery=delivery,
            )
            self.assertEqual(
                checkpoint["campaign_state"]["recorded_by"],
                {"consumer_id": "tester", "principal_id": "principal-test"},
            )
            self.assertEqual(session.snapshot["campaign"]["delivery"]["status"], "delivered")

            reopened = sessions.Session.open(
                state_dir=state_dir, session_id=session.session_id, **FAST
            )
            capsule = reopened.context()["continuation"]
            self.assertEqual(capsule["evidence"], evidence)
            self.assertEqual(capsule["delivery"]["status"], "delivered")
            self.assertEqual(capsule["campaign_state_checkpoint_id"], checkpoint["identity"])
            self.assertEqual(
                capsule["campaign_state_provenance"],
                {"consumer_id": "tester", "principal_id": "principal-test"},
            )

            # Simulate interruption after the immutable checkpoint was stored
            # but before its campaign projection reached the session snapshot.
            snapshot = reopened.snapshot
            snapshot["campaign"]["evidence"] = []
            snapshot["campaign"]["delivery"] = {"status": "pending"}
            snapshot["campaign"].pop("state_checkpoint_id", None)
            snapshot["campaign"].pop("state_recorded_at", None)
            snapshot["campaign"].pop("state_provenance", None)
            snapshot["snapshot_sequence"] += 1
            reopened.store.save_snapshot(reopened.session_id, snapshot)
            interrupted = sessions.Session.open(
                state_dir=state_dir, session_id=session.session_id, **FAST
            )
            reconciliation = interrupted.reconcile_campaign_continuity()
            self.assertTrue(reconciliation["campaign_state_changed"])
            self.assertEqual(
                interrupted.snapshot["campaign"]["state_checkpoint_id"],
                checkpoint["identity"],
            )
            self.assertEqual(interrupted.snapshot["campaign"]["evidence"], evidence)
            self.assertEqual(interrupted.snapshot["campaign"]["delivery"]["status"], "delivered")

    def test_campaign_checkpoint_retry_is_stable_and_stale_base_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = make_session(Path(directory))
            session.snapshot["campaign"]["identity"] = "cmp_checkpoint_cas_test"
            session._save()
            evidence = [{
                "identity": "ev_first",
                "repository": "mncs-environment",
                "commit": "c" * 40,
                "path": "evidence/first.json",
            }]
            first = session.checkpoint(
                progress="publish campaign metadata",
                request_id="req_campaign_first_state",
                campaign_base_checkpoint="none",
                campaign_evidence=evidence,
            )
            retry = session.checkpoint(
                progress="publish campaign metadata",
                request_id="req_campaign_first_state",
                campaign_base_checkpoint="none",
                campaign_evidence=evidence,
            )
            self.assertEqual(retry, first)
            self.assertEqual(len(session.store.list_checkpoints(session.session_id)), 1)
            with self.assertRaises(sessions.LifecycleError):
                session.checkpoint(
                    progress="stale writer",
                    request_id="req_campaign_stale_state",
                    campaign_base_checkpoint="none",
                    campaign_delivery={"status": "pending", "repositories": []},
                )
            self.assertEqual(len(session.store.list_checkpoints(session.session_id)), 1)
            with self.assertRaises(sessions.LifecycleError):
                session.checkpoint(
                    progress="attempt to remove evidence",
                    request_id="req_campaign_remove_evidence",
                    campaign_base_checkpoint=first["identity"],
                    campaign_evidence=[],
                )
            self.assertEqual(len(session.store.list_checkpoints(session.session_id)), 1)

    def test_campaign_metadata_recovery_after_snapshot_write_interruption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            session = make_session(state_dir)
            session.snapshot["campaign"]["identity"] = "cmp_campaign_crash_test"
            session._save()
            evidence = [{
                "identity": "ev_crash_recovery",
                "repository": "mncs-environment",
                "commit": "d" * 40,
                "path": "evidence/recovered.json",
            }]
            with mock.patch.object(
                session.store, "save_snapshot", side_effect=OSError("injected interruption")
            ):
                with self.assertRaisesRegex(OSError, "injected interruption"):
                    session.checkpoint(
                        progress="crash after immutable publication",
                        request_id="req_campaign_crash_recovery",
                        campaign_base_checkpoint="none",
                        campaign_evidence=evidence,
                    )
            checkpoints = session.store.list_checkpoints(session.session_id)
            self.assertEqual(len(checkpoints), 1)
            self.assertIn("campaign_state", checkpoints[0])
            restarted = sessions.Session.open(
                state_dir=state_dir, session_id=session.session_id, **FAST
            )
            result = restarted.reconcile_campaign_continuity()
            self.assertEqual(
                result["campaign_state_checkpoint_id"], checkpoints[0]["identity"]
            )
            self.assertEqual(restarted.snapshot["campaign"]["evidence"], evidence)

    def test_campaign_evidence_rejects_unverifiable_or_path_escaping_references(self) -> None:
        for reference in (
            {"identity": "ev_bad_path", "repository": "mncs-environment",
             "commit": "a" * 40, "path": "../outside.json"},
            {"identity": "ev_bad_commit", "repository": "mncs-environment",
             "commit": "short", "path": "evidence/file.json"},
            {"identity": "ev_host_path", "repository": "mncs-environment",
             "commit": "a" * 40, "path": "/home/agent/private.json"},
        ):
            with self.subTest(reference=reference):
                with self.assertRaises(ValueError):
                    sessions.normalize_campaign_evidence([reference])

    def test_campaign_entry_reconciles_checkpoint_published_before_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = make_session(Path(directory))
            orphan = {
                "schema_version": "mncs.environment.checkpoint/1",
                "identity": "chk_interrupted-publication",
                "session_id": session.session_id,
                "sequence": 1,
                "progress": "caller interrupted after checkpoint object publication",
                "remaining": ["reconcile"],
                "request_id": "req_interrupted-publication",
                "request_digest": "source-bound-digest",
            }
            session.store.save_checkpoint(session.session_id, orphan)
            result = session.reconcile_campaign_continuity()
            self.assertEqual(result["recovered_checkpoints"], [orphan["identity"]])
            self.assertIn(orphan["identity"], session.snapshot["checkpoints"])
            self.assertIn("campaign.reconciled",
                          [event["type"] for event in session.store.read_events(session.session_id)])

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
            self.assertIn("unavailable_count", context["capabilities"])
            self.assertNotIn("available_capabilities", context)
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

    def test_authenticated_entry_discovers_unique_campaign_without_campaign_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            workspace_root = base / "campaign-root"
            workspace_root.mkdir()
            definition_path = base / "environment.json"
            definition_path.write_text(json.dumps({
                "name": "durable-campaign-discovery",
                "intent": {"goal": "resume a campaign without a session or campaign id"},
            }), encoding="utf-8")
            state = base / "state"
            principal = "control-principal-test"
            env = {"MNCS_ENV_AUTH_PRINCIPAL_ID": principal}

            with mock.patch.dict(os.environ, env):
                first_code, first, first_error = self._run_cli(
                    "--persistence", "file", "--state-dir", str(state), "enter",
                    "--definition", str(definition_path), "--workspace", str(workspace_root),
                    "--consumer", "first-process", "--campaign-id", "cmp_explicit-continuation-test",
                )
                second_code, second, second_error = self._run_cli(
                    "--persistence", "file", "--state-dir", str(state), "enter",
                    "--definition", str(definition_path), "--workspace", str(workspace_root),
                    "--consumer", "restarted-process",
                )

            self.assertEqual(first_code, 0, first_error)
            self.assertEqual(second_code, 0, second_error)
            self.assertEqual(second["session_id"], first["session_id"])
            self.assertTrue(second["entry"]["reused"])
            self.assertEqual(second["continuation"]["identity"], "cmp_explicit-continuation-test")
            self.assertEqual(second["consumer_id"], "restarted-process")
            self.assertIn("first-process", second["continuation"]["previous_consumers"])

    def test_authenticated_entry_refuses_ambiguous_campaign_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            workspace_root = base / "campaign-root"
            workspace_root.mkdir()
            definition_path = base / "environment.json"
            definition_path.write_text(json.dumps({
                "name": "ambiguous-campaign-discovery",
                "intent": {"goal": "do not guess between campaigns"},
            }), encoding="utf-8")
            state = base / "state"
            env = {"MNCS_ENV_AUTH_PRINCIPAL_ID": "control-principal-test"}
            with mock.patch.dict(os.environ, env):
                for campaign_id in ("cmp_discovery_one", "cmp_discovery_two"):
                    code, _, error = self._run_cli(
                        "--persistence", "file", "--state-dir", str(state), "enter",
                        "--definition", str(definition_path), "--workspace", str(workspace_root),
                        "--consumer", "campaign-setup", "--campaign-id", campaign_id,
                        "--new-session",
                    )
                    self.assertEqual(code, 0, error)
                code, result, error = self._run_cli(
                    "--persistence", "file", "--state-dir", str(state), "enter",
                    "--definition", str(definition_path), "--workspace", str(workspace_root),
                    "--consumer", "restarted-process",
                )

            self.assertEqual(code, 2)
            self.assertEqual(result, {})
            diagnostic = json.loads(error)
            self.assertEqual(diagnostic["error"],
                             "multiple durable campaigns match this authenticated consumer and selected environment")
            self.assertEqual(diagnostic["diagnostics"]["code"], "campaign-continuation-ambiguous")
            self.assertEqual(diagnostic["diagnostics"]["campaigns"],
                             ["cmp_discovery_one", "cmp_discovery_two"])

    def test_explicit_campaign_identity_cannot_start_under_changed_definition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            workspace_root = base / "campaign-root"
            workspace_root.mkdir()
            definition_path = base / "environment.json"
            original = {
                "name": "campaign-definition-before-restart",
                "intent": {"goal": "continue one authenticated campaign safely"},
            }
            definition_path.write_text(json.dumps(original), encoding="utf-8")
            state = base / "state"
            principal = "control-principal-test"
            campaign_id = "cmp_definition-change-test"
            env = {"MNCS_ENV_AUTH_PRINCIPAL_ID": principal}

            with mock.patch.dict(os.environ, env):
                first_code, first, first_error = self._run_cli(
                    "--persistence", "file", "--state-dir", str(state), "enter",
                    "--definition", str(definition_path), "--workspace", str(workspace_root),
                    "--consumer", "first-process", "--campaign-id", campaign_id,
                )
                durable = sessions.Session.open(
                    state_dir=state, session_id=first["session_id"], backend="file"
                )
                durable.checkpoint(
                    progress="retained progress before definition change",
                    remaining=["resume the recorded campaign"],
                )
                campaign = dict(durable.snapshot["campaign"])
                campaign["work_intent"] = dict(campaign.get("work_intent") or {})
                campaign["work_intent"]["repositories"] = ["mncs-test-repo"]
                campaign["repository_refs"] = [{"repository": "mncs-test-repo"}]
                durable.snapshot["campaign"] = campaign
                durable._save()
                protected = claims.acquire(
                    durable.store,
                    repository="mncs-test-repo",
                    session_id="ses_other_campaign_owner",
                    consumer_id="other-campaign-consumer",
                    basis=claims.BASIS_EXPLICIT,
                    reason="campaign entry capsule proof",
                )
                durable.close()
                changed = dict(original, name="campaign-definition-after-restart")
                definition_path.write_text(json.dumps(changed), encoding="utf-8")
                second_code, second, second_error = self._run_cli(
                    "--persistence", "file", "--state-dir", str(state), "enter",
                    "--definition", str(definition_path), "--workspace", str(workspace_root),
                    "--consumer", "restarted-process", "--campaign-id", campaign_id,
                )
            with mock.patch.dict(os.environ, {"MNCS_ENV_AUTH_PRINCIPAL_ID": "other-principal"}):
                foreign_code, foreign, foreign_error = self._run_cli(
                    "--persistence", "file", "--state-dir", str(state), "enter",
                    "--definition", str(definition_path), "--workspace", str(workspace_root),
                    "--consumer", "foreign-process", "--campaign-id", campaign_id,
                )

            self.assertEqual(first_code, 0, first_error)
            self.assertEqual(second_code, 2)
            self.assertEqual(second, {})
            diagnostic = json.loads(second_error)
            self.assertEqual(diagnostic["diagnostics"]["code"],
                             "campaign-context-mismatch")
            self.assertEqual(diagnostic["diagnostics"]["session_id"],
                             first["session_id"])
            capsule = diagnostic["diagnostics"]["continuation"]
            self.assertEqual(
                capsule["latest_checkpoint"]["progress"],
                "retained progress before definition change",
            )
            self.assertEqual(capsule["foreign_claims_count"], 1)
            self.assertEqual(
                capsule["foreign_claims"][0]["claim_id"], protected["claim_id"]
            )
            self.assertIn(first["session_id"], diagnostic["diagnostics"]["next"])
            self.assertEqual(foreign_code, 2)
            self.assertEqual(foreign, {})
            self.assertEqual(json.loads(foreign_error)["diagnostics"]["code"],
                             "campaign-owner-conflict")
            session_dirs = list((state / "sessions").glob("ses_*"))
            self.assertEqual([path.name for path in session_dirs], [first["session_id"]])

    def test_work_intent_discovers_campaign_across_definition_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            workspace_root = base / "campaign-root"
            workspace_root.mkdir()
            definition_path = base / "environment.json"
            intent = {"goal": "same durable campaign intent across a definition update"}
            definition_path.write_text(json.dumps({
                "name": "campaign-intent-before-restart", "intent": intent,
            }), encoding="utf-8")
            state = base / "state"
            env = {"MNCS_ENV_AUTH_PRINCIPAL_ID": "control-principal-test"}

            with mock.patch.dict(os.environ, env):
                first_code, first, first_error = self._run_cli(
                    "--persistence", "file", "--state-dir", str(state), "enter",
                    "--definition", str(definition_path), "--workspace", str(workspace_root),
                    "--consumer", "first-process",
                )
                definition_path.write_text(json.dumps({
                    "name": "campaign-intent-after-restart", "intent": intent,
                }), encoding="utf-8")
                second_code, second, second_error = self._run_cli(
                    "--persistence", "file", "--state-dir", str(state), "enter",
                    "--definition", str(definition_path), "--workspace", str(workspace_root),
                    "--consumer", "restarted-process",
                )

            self.assertEqual(first_code, 0, first_error)
            self.assertEqual(second_code, 2)
            self.assertEqual(second, {})
            diagnostic = json.loads(second_error)["diagnostics"]
            self.assertEqual(diagnostic["code"], "campaign-context-mismatch")
            self.assertEqual(diagnostic["session_id"], first["session_id"])
            self.assertEqual(diagnostic["continuation"]["session_id"], first["session_id"])
            session_dirs = list((state / "sessions").glob("ses_*"))
            self.assertEqual([path.name for path in session_dirs], [first["session_id"]])

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

    def test_commons_first_contact_drains_all_bounded_pages_without_emitting_old_entries(self) -> None:
        class Client:
            calls = 0

            @classmethod
            def connect(cls, _path):
                return cls()

            def sync(self, cursor=None, limit=100):
                type(self).calls += 1
                page = type(self).calls
                return {"entries": [{"entryDigest": str(page), "entryType": "record"}],
                        "nextCursor": {"sequence": page},
                        "hasMore": page < 7}

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            socket_path = Path(directory) / "commons.sock"
            socket_path.touch()
            Client.calls = 0
            with mock.patch.object(sources, "_load_commons_client",
                                   return_value=(Client, lambda: directory)):
                first = sources.CommonsSyncSource(socket_path).observe(None)
                self.assertEqual(first.status, "ok")
                self.assertEqual(first.events, [])
                self.assertEqual(json.loads(first.cursor), {"sequence": 7})
                self.assertEqual(Client.calls, 7)

    def test_language_source_omits_unknown_stream_identity(self) -> None:
        import socket
        import threading
        # Stub host enforcing the real dispatch rule: an explicit null
        # stream_identity is rejected; the key must be omitted instead.
        directory = tempfile.mkdtemp(prefix="mnls-stub-")
        path = str(Path(directory) / "lang.sock")
        # Restricted runners may prohibit Unix sockets entirely. Detect the
        # platform boundary before starting a worker that cannot report it.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(path + ".probe")
            except PermissionError as error:
                self.skipTest(f"Unix socket creation denied by runner: {error}")
        Path(path + ".probe").unlink()
        seen: list[tuple] = []
        ready = threading.Event()

        def serve() -> None:
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(path)
            listener.listen(4)
            listener.settimeout(10)
            ready.set()
            for _ in range(4):
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
                    seen.append((request.get("method"), params))
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
        # Each observation converges disk truth before polling, so the
        # wire sequence is refresh, poll, refresh, poll. The original
        # stream-identity rule still holds on the poll calls.
        self.assertEqual([method for method, _ in seen],
                         ["refresh_workspace", "poll_events",
                          "refresh_workspace", "poll_events"])
        polls = [params for method, params in seen
                 if method == "poll_events"]
        self.assertEqual(len(polls), 2)
        self.assertNotIn("stream_identity", polls[0])
        self.assertEqual(polls[1].get("stream_identity"), "stream-1")

    def test_language_source_reset_returns_exact_reconciliation_high_water(self) -> None:
        source = sources.LanguageServiceSource("unused.sock")
        response = {"stream_identity": "stream-new", "after_cursor": 8,
                    "current_cursor": 23, "oldest_cursor": 17,
                    "reset_required": True, "events": []}
        with mock.patch.object(source, "_call", side_effect=[{}, response]):
            result = source.observe(json.dumps({"stream": "stream-old", "cursor": 8}))
        self.assertEqual(result.status, "reset")
        self.assertEqual(json.loads(result.cursor), {"cursor": 23, "stream": "stream-new"})
        self.assertIn("oldest_cursor=17", result.detail)

    def test_language_source_acknowledges_only_last_delivered_page_cursor(self) -> None:
        source = sources.LanguageServiceSource("unused.sock")
        cursor = json.dumps({"stream": "stream-1", "cursor": 0})
        first_response = {
            "stream_identity": "stream-1", "after_cursor": 0,
            "current_cursor": 80, "oldest_cursor": 1,
            "reset_required": False,
            "events": [{"cursor": item, "current": {"uri": f"file:///{item}.mncs",
                                                       "identity": f"doc-{item}"}}
                       for item in range(1, 51)],
        }
        with mock.patch.object(source, "_call", side_effect=[{}, first_response]):
            first = source.observe(cursor)
        self.assertEqual(first.status, "ok")
        self.assertEqual(json.loads(first.cursor), {"stream": "stream-1", "cursor": 50})
        self.assertIn("backlog remains", first.detail)

        second_response = {
            "stream_identity": "stream-1", "after_cursor": 50,
            "current_cursor": 80, "oldest_cursor": 1,
            "reset_required": False,
            "events": [{"cursor": item, "current": {"uri": f"file:///{item}.mncs",
                                                       "identity": f"doc-{item}"}}
                       for item in range(51, 81)],
        }
        with mock.patch.object(source, "_call", side_effect=[{}, second_response]):
            second = source.observe(first.cursor)
        self.assertEqual(second.status, "ok")
        self.assertEqual(json.loads(second.cursor), {"stream": "stream-1", "cursor": 80})
        self.assertEqual(len(first.events) + len(second.events), 80)

    def test_language_source_rejects_malformed_or_mismatched_cursor(self) -> None:
        source = sources.LanguageServiceSource("unused.sock")
        with mock.patch.object(source, "_call") as call:
            result = source.observe("[]")
        self.assertEqual(result.status, "reset")
        call.assert_not_called()

        response = {"stream_identity": "stream-1", "after_cursor": 3,
                    "current_cursor": 3, "oldest_cursor": 1,
                    "reset_required": False, "events": []}
        with mock.patch.object(source, "_call", side_effect=[{}, response]):
            result = source.observe(json.dumps({"stream": "stream-1", "cursor": 2}))
        self.assertEqual(result.status, "unknown")
        self.assertIn("does not match requested cursor", result.detail)


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
            marker = json.loads((state_dir / "store" / "environment" / "sessions"
                                 / session.session_id / "store-provider.json").read_text(encoding="utf-8"))
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
