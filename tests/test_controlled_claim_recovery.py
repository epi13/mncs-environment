"""Owner-controlled recovery of exact exclusive Store claim generations."""

from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mncs_env import authority, claims, sessions
from mncs_env.session_store import open_store


def owner_authorization(*, owner: dict, requester_session: str,
                        target_scope: dict | None = None,
                        recovery_basis: str = "operator-approved-config") -> dict:
    observation = {
        "schema_version": "mncs.control.host-execution-liveness/2",
        "observed_at": claims.utcnow(),
        "host_boot_id": "00000000-0000-0000-0000-000000000000",
        "control_owner_pid": 12,
        "control_owner_start_ticks": "1200",
        "process_view": "host-procfs",
        "processes_examined": 9,
        "same_uid_processes_examined": 4,
        "user_units_examined": 3,
        "active_jobs": 0,
        "active_owner_grants": 0,
        "revoked_owner_grants": 0,
        "active_owner_requests": 0,
        "current_request_processes_excluded": [],
        "matching_processes": [],
        "matching_user_services": [],
        "resident_services": [],
        "complete": True,
    }
    authorization = {
        "schema_version": "mncs.environment.control-owner-claim-recovery/2",
        "campaign_id": "cmp_recovery_fixture",
        "requester_session_id": requester_session,
        "requester_principal_id": "ctrl_fixture",
        "recovery_basis": recovery_basis,
        "operator_approval_reference": (
            "user-approval:exact-fixture-claim"
            if recovery_basis == "operator-approved-config" else None
        ),
        "claim_id": owner["claim_id"],
        "repository": owner["repository"],
        "expected_claim_version": owner["version"],
        "owner_session_id": owner["session_id"],
        "owner_consumer_id": owner["consumer_id"],
        "owner_scope": owner["scope"],
        "target_scope": target_scope or owner["scope"],
        "host_observation": observation,
        "host_observation_digest": claims.digest_hex(observation, length=64),
    }
    authorization["authorization_id"] = claims.digest_hex({
        key: authorization[key]
        for key in (
            "schema_version", "campaign_id", "requester_session_id",
            "requester_principal_id", "recovery_basis",
            "operator_approval_reference", "owner_scope", "target_scope",
            "claim_id", "repository", "expected_claim_version",
            "owner_session_id", "owner_consumer_id",
        )
    }, length=64)
    return authorization


class ControlledClaimRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mncs-controlled-claim-recovery-")
        self.addCleanup(self.temp.cleanup)
        self.store = open_store(self.temp.name, "file")

    def owner(self) -> dict:
        return claims.acquire(
            self.store, repository="mncs-fixture", session_id="ses_old_owner",
            consumer_id="old-consumer", basis=claims.BASIS_EXPLICIT,
            reason="active exclusive owner",
        )

    def recover(self, owner: dict, requester: str, request_id: str,
                authorization: dict | None = None) -> dict:
        authorization = authorization or owner_authorization(
            owner=owner, requester_session=requester,
        )
        return claims.acquire(
            self.store,
            repository=owner["repository"],
            session_id=requester,
            consumer_id="new-consumer",
            basis=claims.BASIS_RECOVERY,
            reason="explicit Control owner recovery",
            scope=authorization["target_scope"],
            checkout_facts={"dirty": False, "foreign_signals": []},
            workspace_root=self.temp.name,
            request_id=request_id,
            owner_recovery_authorization=authorization,
        )

    def test_recovery_records_host_proof_and_advances_fencing_generation(self) -> None:
        owner = self.owner()
        recovered = self.recover(owner, "ses_new_owner", "req-owner-recovery")
        self.assertEqual(recovered["version"], owner["version"] + 2)
        self.assertEqual(recovered["session_id"], "ses_new_owner")
        proof = recovered["provenance"]["owner_recovery"]
        self.assertEqual(proof["expected_claim_version"], owner["version"])
        self.assertEqual(proof["host_observation"]["process_view"], "host-procfs")
        history = sorted(
            (item for item in self.store.read_claims()
             if item.get("claim_id") == owner["claim_id"]),
            key=lambda item: item["version"],
        )
        self.assertEqual([item["status"] for item in history],
                         ["held", "superseded", "held"])
        self.assertEqual(history[1]["provenance"]["owner_recovery"], proof)

    def test_orphan_repository_scope_narrows_atomically_to_selected_worktree(self) -> None:
        owner = self.owner()
        checkout = Path(self.temp.name) / "compiler-worktree"
        checkout.mkdir()
        target_scope = claims.normalize_scope({
            "kind": "worktree", "checkout": str(checkout),
            "branch": "campaign/recovered", "exclusive": False,
        }, "mncs-fixture")
        proof = owner_authorization(
            owner=owner,
            requester_session="ses_new_owner",
            target_scope=target_scope,
            recovery_basis="same-principal-host-liveness",
        )
        batches: list[tuple[list[dict], int]] = []
        original_put_claim_batch = self.store.put_claim_batch

        def record_batch(claim_records: list[dict], *, expected_generation: int) -> None:
            batches.append((claim_records, expected_generation))
            original_put_claim_batch(
                claim_records, expected_generation=expected_generation,
            )

        self.store.put_claim_batch = record_batch
        before_generation = self.store.claim_generation()

        recovered = self.recover(
            owner, "ses_new_owner", "req-narrow-orphan", proof,
        )

        active = claims.active_claims(self.store.read_claims())
        self.assertNotIn(owner["claim_id"], active)
        self.assertEqual(recovered["scope"], target_scope)
        self.assertNotEqual(recovered["claim_id"], owner["claim_id"])
        self.assertEqual(active[recovered["claim_id"]]["session_id"], "ses_new_owner")
        self.assertEqual(len(batches), 1)
        self.assertEqual(len(batches[0][0]), 2)
        self.assertEqual(batches[0][1], before_generation)

    def test_idle_resident_provider_is_recorded_without_becoming_old_owner(self) -> None:
        owner = self.owner()
        proof = owner_authorization(owner=owner, requester_session="ses_new_owner")
        proof["host_observation"]["resident_services"] = [{
            "pid": 1900,
            "ppid": 1800,
            "start_ticks": "5000",
            "module": "mncs_fixture.server",
            "environment_readable": True,
        }]
        proof["host_observation_digest"] = claims.digest_hex(
            proof["host_observation"], length=64,
        )
        recovered = self.recover(owner, "ses_new_owner", "req-provider-not-owner", proof)
        self.assertEqual(
            recovered["provenance"]["owner_recovery"]["host_observation"][
                "resident_services"
            ],
            proof["host_observation"]["resident_services"],
        )

    def test_wrong_version_and_unapproved_conflicting_claim_fail_closed(self) -> None:
        owner = self.owner()
        proof = owner_authorization(owner=owner, requester_session="ses_new_owner")
        proof["expected_claim_version"] += 1
        proof["authorization_id"] = claims.digest_hex({
            key: proof[key]
            for key in (
                "schema_version", "campaign_id", "requester_session_id",
                "requester_principal_id", "recovery_basis",
                "operator_approval_reference", "owner_scope", "target_scope",
                "claim_id", "repository", "expected_claim_version",
                "owner_session_id", "owner_consumer_id",
            )
        }, length=64)
        with self.assertRaises(claims.ClaimConflict):
            self.recover(owner, "ses_new_owner", "req-wrong-generation", proof)
        self.assertEqual(
            claims.active_claims(self.store.read_claims())[owner["claim_id"]]["session_id"],
            owner["session_id"],
        )

    def test_concurrent_recovery_race_has_one_winner(self) -> None:
        owner = self.owner()
        barrier = threading.Barrier(2)

        def attempt(session_id: str):
            proof = owner_authorization(owner=owner, requester_session=session_id)
            barrier.wait(timeout=10)
            try:
                return self.recover(owner, session_id, f"req-{session_id}", proof)
            except claims.ClaimConflict as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(attempt, ("ses_winner_a", "ses_winner_b")))
        winners = [item for item in outcomes if isinstance(item, dict)]
        failures = [item for item in outcomes if isinstance(item, claims.ClaimConflict)]
        self.assertEqual((len(winners), len(failures)), (1, 1), outcomes)
        current = claims.active_claims(self.store.read_claims())[owner["claim_id"]]
        self.assertEqual(current["session_id"], winners[0]["session_id"])
        self.assertEqual(current["version"], owner["version"] + 2)

    def test_interrupted_recovery_retries_before_and_after_atomic_commit(self) -> None:
        owner = self.owner()
        proof = owner_authorization(owner=owner, requester_session="ses_new_owner")
        real_batch = self.store.put_claim_batch

        def fail_before(batch, *, expected_generation):
            raise RuntimeError("interrupted before atomic claim commit")

        self.store.put_claim_batch = fail_before  # type: ignore[method-assign]
        try:
            with self.assertRaisesRegex(RuntimeError, "before atomic claim"):
                self.recover(owner, "ses_new_owner", "req-before-commit", proof)
        finally:
            self.store.put_claim_batch = real_batch  # type: ignore[method-assign]
        self.assertEqual(
            claims.active_claims(self.store.read_claims())[owner["claim_id"]]["session_id"],
            owner["session_id"],
        )
        self.recover(owner, "ses_new_owner", "req-before-commit", proof)

        # A second isolated repository demonstrates readback after the atomic
        # Store generation advanced but the caller lost its response.
        other = claims.acquire(
            self.store, repository="mncs-fixture-2", session_id="ses_old_owner_2",
            consumer_id="old-consumer-2", basis=claims.BASIS_EXPLICIT,
            reason="active exclusive owner",
        )
        other_proof = owner_authorization(owner=other, requester_session="ses_new_owner_2")
        interrupted = {"done": False}

        def commit_then_interrupt(batch, *, expected_generation):
            real_batch(batch, expected_generation=expected_generation)
            if not interrupted["done"]:
                interrupted["done"] = True
                raise RuntimeError("interrupted after atomic claim commit")

        self.store.put_claim_batch = commit_then_interrupt  # type: ignore[method-assign]
        try:
            with self.assertRaisesRegex(RuntimeError, "after atomic claim"):
                self.recover(other, "ses_new_owner_2", "req-after-commit", other_proof)
        finally:
            self.store.put_claim_batch = real_batch  # type: ignore[method-assign]
        recovered = self.recover(other, "ses_new_owner_2", "req-after-commit", other_proof)
        self.assertEqual(recovered["session_id"], "ses_new_owner_2")

    def test_old_session_mutation_is_denied_by_persisted_fence(self) -> None:
        owner = self.owner()
        proof = owner_authorization(owner=owner, requester_session="ses_new_owner")
        recovered = self.recover(owner, "ses_new_owner", "req-fence", proof)
        context = {
            "subject": "old-owner",
            "writable": [owner["repository"]],
            "protected_repositories": [],
            "denied": [],
            "claim_fences": [{
                "repository": owner["repository"],
                "claim_id": owner["claim_id"],
                "superseded_version": owner["version"],
                "fencing_generation": recovered["version"],
                "recovery_authorization_id": proof["authorization_id"],
            }],
        }
        result = authority.evaluate(
            context, action="write", target=owner["repository"],
            session_id=owner["session_id"], claims=claims.holders(self.store.read_claims()),
        )
        self.assertEqual(result["verdict"], "deny")
        self.assertIn(str(recovered["version"]), result["reason"])

    def test_environment_reconciles_both_sessions_and_continues_idempotently(self) -> None:
        owner_record = self.owner()
        for session_id, consumer, claim_ids in (
            ("ses_old_owner", "old-consumer", [owner_record["claim_id"]]),
            ("ses_new_owner", "new-consumer", []),
        ):
            self.store.save_snapshot(session_id, {
                "session_id": session_id,
                "snapshot_sequence": 1,
                "lifecycle": "active",
                "authenticated_principal_id": "ctrl_fixture",
                "consumer_id": consumer,
                "campaign": {
                    "identity": "cmp_recovery_fixture",
                    "claim_ids": claim_ids,
                    "repository_refs": [],
                },
                "authority": {
                    "subject": consumer,
                    "writable": (["mncs-fixture"]
                                 if session_id == "ses_old_owner" else []),
                    "protected_repositories": [],
                    "denied": [],
                    "claim_holders": {},
                },
            })
        proof = owner_authorization(owner=owner_record, requester_session="ses_new_owner")
        requester = sessions.Session(self.store, "ses_new_owner")

        result = requester.recover_claim_controlled(
            claim_id=owner_record["claim_id"],
            repository=owner_record["repository"],
            owner_session_id=owner_record["session_id"],
            expected_version=owner_record["version"],
            authorization=proof,
            authenticated_principal_id="ctrl_fixture",
            request_id="req_session_recovery_one",
            reason="Control approved recovery",
        )
        retry = requester.recover_claim_controlled(
            claim_id=owner_record["claim_id"],
            repository=owner_record["repository"],
            owner_session_id=owner_record["session_id"],
            expected_version=owner_record["version"],
            authorization=proof,
            authenticated_principal_id="ctrl_fixture",
            request_id="req_session_recovery_two",
            reason="Control approved recovery",
        )

        self.assertEqual(result["status"], "recovered")
        self.assertEqual(result["fencing_generation"], owner_record["version"] + 1)
        self.assertEqual(
            requester.check(action="write", target="mncs-fixture")["verdict"],
            "allow",
        )
        self.assertEqual(retry["claim"], result["claim"])
        old = sessions.Session(self.store, "ses_old_owner")
        self.assertNotIn(owner_record["claim_id"], old.snapshot["campaign"]["claim_ids"])
        fences = old.snapshot["authority"]["claim_fences"]
        self.assertEqual(fences[0]["fencing_generation"], result["fencing_generation"])
        old_events = [event for event in old._log()
                      if event.get("type") == "lease.recovered-away"]
        self.assertEqual(len(old_events), 1)
        new_events = [event for event in requester._log()
                      if event.get("type") == "lease.owner-recovery-reconciled"]
        self.assertEqual(len(new_events), 1)


if __name__ == "__main__":
    unittest.main()
