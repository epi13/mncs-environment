"""Regression tests for sectioned session persistence and write-amplification recovery."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from mncs_env import sessions as sessions_module
from mncs_env.session_store import SnapshotConflict, open_store
from mncs_env.sources import StoreReplaySource
from mncs_env.store_backend import (
    SCHEMA_SNAPSHOT,
    SCHEMA_SNAPSHOT_SECTION,
    StoreIntegrityFailure,
    _decompose_snapshot,
    _snapshot_identity,
    _snapshot_section_identity,
)


def _large_environment_fixture(workspace_root: Path) -> dict[str, Any]:
    """Build a realistic multi-repository environment payload with large static sections."""
    repositories = [
        {
            "name": f"mncs-repo-{idx:02d}",
            "manifest_repository": f"mncs-repo-{idx:02d}",
            "path": str(workspace_root / f"mncs-repo-{idx:02d}"),
            "branch": "main",
            "head": f"{idx:040x}",
            "dirty": False,
            "dirty_files": [],
            "worktrees": [
                {
                    "path": str(workspace_root / f"mncs-repo-{idx:02d}"),
                    "head": f"{idx:040x}",
                    "branch": "refs/heads/main",
                }
            ],
        }
        for idx in range(1, 35)
    ]
    bindings = [
        {
            "capability": f"mncs.test.capability.{idx:02d}",
            "provider": f"mncs-repo-{idx:02d}",
            "provider_root": str(workspace_root / f"mncs-repo-{idx:02d}"),
            "contract_revision": "1.0",
            "entrypoint": ["python3", "-m", f"mncs_repo_{idx:02d}"],
            "availability": {"status": "available", "reason": "ready"},
            "description": "x" * 512,
        }
        for idx in range(1, 35)
    ]
    selected_checkouts = {
        f"mncs-repo-{idx:02d}": {
            "repository": f"mncs-repo-{idx:02d}",
            "path": str(workspace_root / f"mncs-repo-{idx:02d}"),
            "branch": "main",
            "head": f"{idx:040x}",
            "clean": True,
            "source_ref": "explicit-workspace-selection",
            "authoritative_head": f"{idx:040x}",
        }
        for idx in range(1, 35)
    }
    repo_facts = {
        f"mncs-repo-{idx:02d}": {
            "name": f"mncs-repo-{idx:02d}",
            "path": str(workspace_root / f"mncs-repo-{idx:02d}"),
            "head": f"{idx:040x}",
            "branch": "main",
            "dirty": False,
        }
        for idx in range(1, 35)
    }
    return {
        "identity": "env_test_sectioned_persistence",
        "environment_id": "env_test_sectioned_persistence",
        "definition_id": "def_test_sectioned_persistence",
        "resolution_inputs_digest": "a" * 64,
        "configuration": {
            "source": "fixture",
            "name": "test-sectioned",
            "definition_id": "def_test_sectioned_persistence",
        },
        "workspace": {
            "root": str(workspace_root),
            "selection": sorted(selected_checkouts),
            "repositories": repositories,
            "selected_checkouts": selected_checkouts,
            "scan": {"status": "complete", "count": len(repositories)},
        },
        "selected_checkouts": selected_checkouts,
        "repo_facts": repo_facts,
        "bindings": bindings,
        "rights": {
            "repositories": {
                name: {"tier": "first-party", "license": "Proprietary", "notes": "y" * 128}
                for name in selected_checkouts
            }
        },
        "authority": {
            "subject": "agent-alpha",
            "protected_repositories": ["mncs-repo-01"],
            "claim_holders": {},
        },
        "intent": {
            "identity": "intent_" + "1" * 16,
            "goal": "Validate sectioned session persistence",
            "repositories": ["mncs-repo-01", "mncs-repo-02"],
            "writable": ["mncs-repo-01"],
            "protected_repositories": [],
        },
        "readiness": {"status": "ready", "unavailable": [], "unknown": []},
        "execution_roles": {},
        "execution_stack": {"roles": {}, "status": "ready"},
    }


def _count_chunk_files(store_dir: Path) -> int:
    chunks_dir = store_dir / "chunks"
    if not chunks_dir.is_dir():
        return 0
    return sum(len(files) for _, _, files in os.walk(chunks_dir))


def _dir_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def _comparable_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``snapshot`` excluding in-memory post-save feed baseline."""
    copied = copy.deepcopy(snapshot)
    copied.pop("last_store_generation", None)
    return copied


class SessionPersistenceAmplificationTests(unittest.TestCase):
    """Verify sectioned session snapshots, deduplication, and full session semantics."""

    def test_create_session_persists_sectioned_snapshot_and_roundtrips(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            workspace_root = root / "workspace"
            workspace_root.mkdir()
            store = open_store(state_dir, "store", verify_on_open=False, defer_mutation=False)
            try:
                env = _large_environment_fixture(workspace_root)
                principal = "ctrl_" + "a" * 32
                session = sessions_module.Session.create(
                    state_dir=state_dir,
                    environment=env,
                    consumer_id="agent-alpha",
                    authenticated_principal_id=principal,
                    store=store,
                )
                self.assertEqual(session.snapshot["snapshot_sequence"], 1)

                # Verify that the raw `:snap:0000000001` object stored in Store is a compact
                # envelope referencing content-addressed `SCHEMA_SNAPSHOT_SECTION` bindings.
                raw_snap = store.backend._store.get_bound_object(
                    SCHEMA_SNAPSHOT,
                    _snapshot_identity(session.session_id, 1),
                )
                raw_envelope = json.loads(raw_snap.payload.decode("utf-8"))
                self.assertIn("_mncs_snapshot_sections_v1", raw_envelope)
                self.assertLess(len(raw_snap.payload), 8192)
                sections_manifest = raw_envelope["_mncs_snapshot_sections_v1"]
                self.assertIn("workspace.repositories", sections_manifest)
                self.assertIn("bindings", sections_manifest)
                self.assertIn("repo_facts", sections_manifest)
                self.assertIn("rights", sections_manifest)
                self.assertIn("campaign.repository_refs", sections_manifest)

                # Replay source must ignore internal snapshot sections.
                replay = StoreReplaySource(store, session.session_id)
                observations = replay.observe(None).events
                self.assertFalse(
                    any(obs.subject.startswith("snap-sec:") for obs in observations)
                )

                # Reopening reconstructs the full snapshot identically.
                reopened = sessions_module.Session.open(
                    state_dir=state_dir,
                    session_id=session.session_id,
                    store=store,
                )
                self.assertEqual(
                    _comparable_snapshot(reopened.snapshot),
                    _comparable_snapshot(session.snapshot),
                )
            finally:
                store.close()

    def test_successive_mutations_and_checkpoints_reuse_sections_without_chunk_growth(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            workspace_root = root / "workspace"
            workspace_root.mkdir()
            store = open_store(state_dir, "store", verify_on_open=False, defer_mutation=False)
            try:
                env = _large_environment_fixture(workspace_root)
                principal = "ctrl_" + "b" * 32
                session = sessions_module.Session.create(
                    state_dir=state_dir,
                    environment=env,
                    consumer_id="agent-alpha",
                    authenticated_principal_id=principal,
                    store=store,
                )
                session.transition("resolving", "enter: resolving", save=False)
                session.transition("ready", "enter: ready", save=False)
                session.transition("active", "enter: active")

                store_dir = state_dir / "store"
                section_bindings_before = [
                    ident
                    for schema, ident in store.backend._store.domain_bindings_at(
                        store.generation()
                    )
                    if schema == SCHEMA_SNAPSHOT_SECTION
                ]
                chunks_before = _count_chunk_files(store_dir)
                bytes_before = _dir_bytes(store_dir / "chunks")

                # Perform 10 routine RPC activity updates and 10 checkpoints.
                for idx in range(1, 11):
                    digest = f"{idx:064x}"
                    session.record_rpc_read_activity(
                        {
                            "schema_version": "mncs.environment.rpc-read-activity/1",
                            "operation": "session.inspect",
                            "outcome_status": "succeeded",
                            "exit_code": 0,
                            "failure_code": None,
                            "request_digest": digest,
                            "caller": {
                                "authenticated_principal_id": principal,
                                "control_grant_id": "c" * 32,
                                "process_identity": "proc_" + "d" * 32,
                            },
                            "context": {
                                "repository": "mncs-repo-01",
                                "repository_branch": "main",
                                "repository_head": f"{1:040x}",
                                "environment_id": session.snapshot["environment_id"],
                                "campaign_id": session.snapshot["campaign"]["identity"],
                                "work_intent_id": session.snapshot["intent"]["identity"],
                            },
                            "measurement_availability": {
                                "model_identity": "unavailable",
                                "token_usage": "unavailable",
                                "control_dispatch_elapsed_ms": "measured",
                                "resource_consumption": "unavailable",
                                "cache_accounting": "unavailable",
                            },
                            "measurements": {
                                "control_dispatch_elapsed_ms": 5.0,
                            },
                        },
                        request_id=f"req_rpc_activity_{idx:03d}",
                        operation_digest=digest,
                    )
                    session.checkpoint(
                        progress=f"step {idx}",
                        remaining=[f"step {idx + 1}"],
                        request_id=f"req_chk_{idx:03d}",
                    )

                section_bindings_after = [
                    ident
                    for schema, ident in store.backend._store.domain_bindings_at(
                        store.generation()
                    )
                    if schema == SCHEMA_SNAPSHOT_SECTION
                ]
                # Static sections did not change, so zero new section bindings were created.
                self.assertEqual(set(section_bindings_after), set(section_bindings_before))

                chunks_delta = _count_chunk_files(store_dir) - chunks_before
                bytes_delta = _dir_bytes(store_dir / "chunks") - bytes_before
                # 10 RPC updates (20 writes) + 10 checkpoints (30 writes) = 50 small writes
                self.assertEqual(chunks_delta, 50)
                self.assertLess(bytes_delta, 250_000)

                reopened = sessions_module.Session.open(
                    state_dir=state_dir,
                    session_id=session.session_id,
                    store=store,
                )
                self.assertEqual(
                    _comparable_snapshot(reopened.snapshot),
                    _comparable_snapshot(session.snapshot),
                )
                self.assertEqual(len(reopened.store.list_checkpoints(session.session_id)), 10)
            finally:
                store.close()

    def test_handoff_acceptance_completion_and_historical_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            workspace_root = root / "workspace"
            workspace_root.mkdir()
            store = open_store(state_dir, "store", verify_on_open=False, defer_mutation=False)
            try:
                env = _large_environment_fixture(workspace_root)
                principal_a = "ctrl_" + "c" * 32
                principal_b = "ctrl_" + "d" * 32
                session = sessions_module.Session.create(
                    state_dir=state_dir,
                    environment=env,
                    consumer_id="agent-alpha",
                    authenticated_principal_id=principal_a,
                    store=store,
                )
                session.transition("resolving", "enter: resolving", save=False)
                session.transition("ready", "enter: ready", save=False)
                session.transition("active", "enter: active")

                hff = session.handoff(
                    to_consumer="agent-beta",
                    to_authenticated_principal_id=principal_b,
                    notes=["handoff note"],
                    blockers=[],
                    next_actions=["complete work"],
                    request_id="req_handoff_test_1",
                )
                self.assertEqual(session.snapshot["lifecycle"], "handed_off")

                # Re-open before accepting handoff to verify handed_off state persistence.
                reopened_mid = sessions_module.Session.open(
                    state_dir=state_dir,
                    session_id=session.session_id,
                    store=store,
                )
                self.assertEqual(
                    _comparable_snapshot(reopened_mid.snapshot),
                    _comparable_snapshot(session.snapshot),
                )

                # Mock revalidate's workspace scan for synthetic fixture paths.
                with unittest.mock.patch.object(
                    reopened_mid, "revalidate", return_value={"changed": [], "reused": 34}
                ):
                    accepted = reopened_mid.accept_handoff(
                        hff["identity"],
                        consumer_id="agent-beta",
                        authenticated_principal_id=principal_b,
                        request_id="req_accept_handoff_test_1",
                    )
                self.assertEqual(accepted["consumer_id"], "agent-beta")
                self.assertEqual(reopened_mid.snapshot["lifecycle"], "active")
                self.assertEqual(
                    reopened_mid.snapshot["authenticated_principal_id"], principal_b
                )

                completion = reopened_mid.complete(
                    outcome="succeeded",
                    summary="completed after handoff",
                    request_id="req_complete_test_1",
                    authenticated_principal_id=principal_b,
                )
                self.assertEqual(completion["outcome"], "succeeded")
                self.assertEqual(reopened_mid.snapshot["lifecycle"], "completed")

                final_reopened = sessions_module.Session.open(
                    state_dir=state_dir,
                    session_id=session.session_id,
                    store=store,
                )
                self.assertEqual(
                    _comparable_snapshot(final_reopened.snapshot),
                    _comparable_snapshot(reopened_mid.snapshot),
                )
            finally:
                store.close()

    def test_cross_session_deduplication_and_snapshot_conflict_detection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            workspace_root = root / "workspace"
            workspace_root.mkdir()
            store = open_store(state_dir, "store", verify_on_open=False, defer_mutation=False)
            try:
                env = _large_environment_fixture(workspace_root)
                s1 = sessions_module.Session.create(
                    state_dir=state_dir,
                    environment=copy.deepcopy(env),
                    consumer_id="agent-one",
                    authenticated_principal_id="ctrl_" + "1" * 32,
                    store=store,
                )
                bytes_after_s1 = _dir_bytes(state_dir / "store" / "chunks")

                # Creating a second session in the same workspace deduplicates all shared
                # static sections (`workspace.repositories`, `bindings`, `repo_facts`, `rights`).
                s2 = sessions_module.Session.create(
                    state_dir=state_dir,
                    environment=copy.deepcopy(env),
                    consumer_id="agent-two",
                    authenticated_principal_id="ctrl_" + "2" * 32,
                    store=store,
                )
                bytes_for_s2 = _dir_bytes(state_dir / "store" / "chunks") - bytes_after_s1
                self.assertLess(bytes_for_s2, 15_000)
                self.assertEqual(
                    s1.snapshot["workspace"]["repositories"],
                    s2.snapshot["workspace"]["repositories"],
                )

                # Concurrent conflicting save at the same revision raises SnapshotConflict.
                conflicting = copy.deepcopy(s1.snapshot)
                conflicting["consumer_id"] = "conflicting-consumer"
                with self.assertRaises(SnapshotConflict):
                    store.save_snapshot(s1.session_id, conflicting)
            finally:
                store.close()

    def test_repeated_writes_across_store_generation_checkpoint_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            workspace_root = root / "workspace"
            workspace_root.mkdir()
            store = open_store(state_dir, "store", verify_on_open=False, defer_mutation=False)
            try:
                env = _large_environment_fixture(workspace_root)
                principal = "ctrl_" + "e" * 32
                session = sessions_module.Session.create(
                    state_dir=state_dir,
                    environment=env,
                    consumer_id="agent-alpha",
                    authenticated_principal_id=principal,
                    store=store,
                )
                session.transition("resolving", "enter: resolving", save=False)
                session.transition("ready", "enter: ready", save=False)
                session.transition("active", "enter: active")

                # Advance past Store GENERATION_CHECKPOINT_INTERVAL (256)
                for idx in range(1, 90):
                    session.checkpoint(
                        progress=f"gen boundary step {idx}",
                        request_id=f"req_gen_chk_{idx:03d}",
                    )
                self.assertGreater(store.generation(), 260)

                # Verify cold reopen across the 256-generation checkpoint boundary.
                cold_store = open_store(
                    state_dir, "store", verify_on_open=True, defer_mutation=True
                )
                try:
                    reopened = sessions_module.Session.open(
                        state_dir=state_dir,
                        session_id=session.session_id,
                        store=cold_store,
                    )
                    self.assertEqual(
                        _comparable_snapshot(reopened.snapshot),
                        _comparable_snapshot(session.snapshot),
                    )
                finally:
                    cold_store.close()
            finally:
                store.close()

    def test_interrupted_sectioned_persistence_recovers_and_detects_corrupted_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            workspace_root = root / "workspace"
            workspace_root.mkdir()
            store = open_store(state_dir, "store", verify_on_open=False, defer_mutation=False)
            try:
                env = _large_environment_fixture(workspace_root)
                principal = "ctrl_" + "f" * 32
                session = sessions_module.Session.create(
                    state_dir=state_dir,
                    environment=env,
                    consumer_id="agent-alpha",
                    authenticated_principal_id=principal,
                    store=store,
                )
                gen_before = store.generation()

                # Mutate a large section so put_snapshot uses put_bound_objects batch commit,
                # and inject an interruption before head commit.
                mutated = copy.deepcopy(session.snapshot)
                mutated["snapshot_sequence"] = 2
                mutated["rights"]["extra_note"] = "z" * 1024

                def fail_before_gen(stage: str) -> None:
                    if stage == "before_generation_publication":
                        raise RuntimeError("simulated crash before generation publication")

                store.backend._store._failpoint = fail_before_gen
                with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                    store.save_snapshot(session.session_id, mutated)
                store.backend._store._failpoint = None
            finally:
                store.close()

            # Reopen Store (running automatic recovery) and verify clean state at gen_before.
            recovered_store = open_store(
                state_dir, "store", verify_on_open=True, defer_mutation=False
            )
            try:
                self.assertEqual(recovered_store.generation(), gen_before)
                reopened = sessions_module.Session.open(
                    state_dir=state_dir,
                    session_id=session.session_id,
                    store=recovered_store,
                )
                self.assertEqual(reopened.snapshot["snapshot_sequence"], 1)

                # Retry saving the mutated snapshot and verify it succeeds cleanly.
                recovered_store.save_snapshot(session.session_id, mutated)
                reopened_v2 = sessions_module.Session.open(
                    state_dir=state_dir,
                    session_id=session.session_id,
                    store=recovered_store,
                )
                self.assertEqual(
                    _comparable_snapshot(reopened_v2.snapshot),
                    _comparable_snapshot(mutated),
                )

                # Verify that a snapshot with a tampered section digest raises StoreIntegrityFailure.
                envelope_bytes, _ = _decompose_snapshot(mutated)
                bad_envelope = json.loads(envelope_bytes.decode("utf-8"))
                bad_envelope["_mncs_snapshot_sections_v1"]["rights"]["sha256"] = "0" * 64
                recovered_store.backend._put_immutable(
                    schema=SCHEMA_SNAPSHOT,
                    identity=_snapshot_identity(session.session_id, 3),
                    payload=json.dumps(bad_envelope, sort_keys=True).encode("utf-8"),
                )
                with self.assertRaises(StoreIntegrityFailure):
                    recovered_store.load_snapshot(session.session_id)
            finally:
                recovered_store.close()

    def test_legacy_monolithic_snapshot_compatibility_and_upgrade_on_save(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            workspace_root = root / "workspace"
            workspace_root.mkdir()
            store = open_store(state_dir, "store", verify_on_open=False, defer_mutation=False)
            try:
                env = _large_environment_fixture(workspace_root)
                principal = "ctrl_" + "9" * 32
                session = sessions_module.Session.create(
                    state_dir=state_dir,
                    environment=env,
                    consumer_id="agent-legacy",
                    authenticated_principal_id=principal,
                    store=store,
                )
                legacy_snapshot = copy.deepcopy(session.snapshot)
                legacy_session_id = "ses_legacy0000000001"
                legacy_snapshot["session_id"] = legacy_session_id
                legacy_snapshot["snapshot_sequence"] = 1

                # Write a legacy monolithic snapshot directly without section decomposition.
                monolithic_bytes = json.dumps(
                    legacy_snapshot, ensure_ascii=False, sort_keys=True
                ).encode("utf-8")
                self.assertGreater(len(monolithic_bytes), 50_000)
                store.backend._put_immutable(
                    schema=SCHEMA_SNAPSHOT,
                    identity=_snapshot_identity(legacy_session_id, 1),
                    payload=monolithic_bytes,
                )

                # Opening the legacy session reads the monolithic payload transparently.
                opened_legacy = sessions_module.Session.open(
                    state_dir=state_dir,
                    session_id=legacy_session_id,
                    store=store,
                )
                self.assertEqual(opened_legacy.snapshot, legacy_snapshot)

                # Resuming and saving upgrades revision 2 to sectioned representation.
                resumed_legacy = sessions_module.Session.resume(
                    state_dir=state_dir,
                    session_id=legacy_session_id,
                    store=store,
                    request_id="req_resume_legacy_1",
                )
                self.assertEqual(resumed_legacy.snapshot["snapshot_sequence"], 2)
                raw_rev2 = store.backend._store.get_bound_object(
                    SCHEMA_SNAPSHOT,
                    _snapshot_identity(legacy_session_id, 2),
                )
                self.assertLess(len(raw_rev2.payload), 8192)
                self.assertIn(
                    "_mncs_snapshot_sections_v1",
                    json.loads(raw_rev2.payload.decode("utf-8")),
                )

                # Reopening reads the upgraded revision 2 accurately.
                reopened_after_upgrade = sessions_module.Session.open(
                    state_dir=state_dir,
                    session_id=legacy_session_id,
                    store=store,
                )
                self.assertEqual(
                    _comparable_snapshot(reopened_after_upgrade.snapshot),
                    _comparable_snapshot(resumed_legacy.snapshot),
                )
            finally:
                store.close()

    def test_projection_ambient_pass_deduplicates_structure_and_diagnosis_invocations(self):
        import hashlib
        from types import SimpleNamespace
        from mncs_env import capabilities as capabilities_module
        from mncs_env import projections as projections_module
        from mncs_env import projection_sources as semantic_sources
        from mncs_env import structure as structure_module

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            owner = root / "structure-owner"
            (owner / "profiles").mkdir(parents=True)
            (owner / "schemas").mkdir()
            (owner / ".mncs").mkdir()
            profile = {
                "schema_version": "mncds.project-structure-profile/1",
                "profile": "fixture",
                "required_paths": [],
                "artifact_classes": [],
                "rules": [],
            }
            profile_bytes = json.dumps(profile).encode("utf-8")
            profile_digest = "sha256:" + hashlib.sha256(profile_bytes).hexdigest()
            (owner / "profiles/profile.json").write_bytes(profile_bytes)
            (owner / "schemas/project-structure-profile-1.schema.json").write_text(
                json.dumps({"type": "object"})
            )
            (owner / ".mncs/project.json").write_text(
                json.dumps(
                    {
                        "contracts": {
                            "provides": [{"contract": "projection-health"}]
                        }
                    }
                )
            )
            declarations = []
            for repo_name in ("repo-a", "repo-b"):
                checkout = root / repo_name
                (checkout / ".mncs").mkdir(parents=True)
                (checkout / ".mncs/project.json").write_text(
                    json.dumps(
                        {
                            "structure_profile": {
                                "repository": "structure-owner",
                                "path": "profiles/profile.json",
                                "identity": profile_digest,
                            }
                        }
                    )
                )
                for idx in range(3):
                    declarations.append(
                        {
                            "id": f"{repo_name}:view-{idx}",
                            "repository": repo_name,
                            "checkout": str(checkout),
                            "output": f"docs/view-{idx}.md",
                            "schema_version": semantic_sources.SCHEMA,
                            "provider_capability": "fixture:render",
                        }
                    )

            state_dir = root / "state"
            state_dir.mkdir()
            invoke_calls: list[tuple[str, list[str]]] = []

            class FakeSession:
                def __init__(self, *, fail_provider: bool):
                    self.session_id = "ses_proj_dedup_fixture"
                    self.store = SimpleNamespace(read_claims=lambda: [])
                    self.fail_provider = fail_provider
                    self.snapshot = {
                        "workspace": {"root": str(root)},
                        "selected_checkouts": {
                            "structure-owner": {"path": str(owner)},
                            "repo-a": {"path": str(root / "repo-a")},
                            "repo-b": {"path": str(root / "repo-b")},
                        },
                        "bindings": [
                            {
                                "capability": capabilities_module.DOCTOR_PROJECTION_HEALTH,
                                "binding_id": "cap_doctor_proj_health",
                                "provider_root": str(owner),
                                "availability": {"status": "available"},
                            }
                        ],
                    }

                def _binding(self, capability: str):
                    for item in self.snapshot.get("bindings", []):
                        if item.get("capability") == capability:
                            return item
                    raise KeyError(capability)

                def invoke(self, capability, argv, **kwargs):
                    invoke_calls.append((capability, list(argv)))
                    if self.fail_provider:
                        return {"status": "failed", "returncode": 1, "stdout": ""}
                    payload = json.loads(argv[argv.index("--facts-json") + 1])
                    if "structure" in payload:
                        return {
                            "status": "ok",
                            "returncode": 0,
                            "stdout": json.dumps(
                                {
                                    "schema_version": "mncs.projection-structure/1",
                                    "state": "pass",
                                }
                            ),
                        }
                    return {
                        "status": "ok",
                        "returncode": 0,
                        "stdout": json.dumps(
                            {
                                "schema_version": "mncs.projection-health/1",
                                "code": 0,
                                "diagnostic": None,
                            }
                        ),
                    }

                def _save(self):
                    return None

            session_fail = FakeSession(fail_provider=True)
            with (
                unittest.mock.patch.object(
                    projections_module, "_workspace_root", return_value=root
                ),
                unittest.mock.patch.object(
                    projections_module,
                    "discover_selected_declarations",
                    return_value=(declarations, []),
                ),
                unittest.mock.patch.object(
                    projections_module, "declaration_digest", return_value="sha256:decl"
                ),
                unittest.mock.patch.object(
                    projections_module, "repo_facts", return_value={"head": "abc"}
                ),
                unittest.mock.patch.object(
                    projections_module, "claim_facts", return_value={}
                ),
                unittest.mock.patch.object(
                    projections_module,
                    "_read_shared_row",
                    return_value={"version": 1, "observed_gen": 0, "canonical_gen": 0, "defer_count": 0},
                ),
                unittest.mock.patch.object(projections_module, "_retain_shared"),
                unittest.mock.patch.object(
                    projections_module, "_write_evidence", return_value="ev:1"
                ),
                unittest.mock.patch.object(projections_module, "_append_session_evidence"),
            ):
                projections_module.ambient_pass(session_fail, mode="ambient")

            # 6 declarations across 2 repos with failing structure & diagnosis must invoke
            # DOCTOR_PROJECTION_HEALTH at most once for structure and once for diagnose.
            self.assertEqual(len(invoke_calls), 2)

            # Successful structure check across 2 repos (6 declarations) invokes once and
            # populates per-checkout structure_observations for both repositories.
            invoke_calls.clear()
            session_ok = FakeSession(fail_provider=False)
            with (
                unittest.mock.patch.object(
                    projections_module, "_workspace_root", return_value=root
                ),
                unittest.mock.patch.object(
                    projections_module,
                    "discover_selected_declarations",
                    return_value=(declarations, []),
                ),
                unittest.mock.patch.object(
                    projections_module, "declaration_digest", return_value="sha256:decl"
                ),
                unittest.mock.patch.object(
                    projections_module,
                    "_reconcile_one",
                    return_value={"projection": "p", "outcome": "current"},
                ),
                unittest.mock.patch.object(
                    projections_module, "_write_evidence", return_value="ev:2"
                ),
                unittest.mock.patch.object(projections_module, "_append_session_evidence"),
            ):
                projections_module.ambient_pass(session_ok, mode="ambient")

            self.assertEqual(len(invoke_calls), 1)
            self.assertEqual(
                set(session_ok.snapshot["structure_observations"].keys()),
                {str(root / "repo-a"), str(root / "repo-b")},
            )

            # Standalone direct calls outside ambient_pass remain uncached by pass state.
            invoke_calls.clear()
            projections_module.diagnose_projection(
                session_ok, declarations[0], [0, 1, 1], [1, 1, 1]
            )
            projections_module.diagnose_projection(
                session_ok, declarations[1], [0, 1, 1], [1, 1, 1]
            )
            self.assertEqual(len(invoke_calls), 2)


if __name__ == "__main__":
    unittest.main()
