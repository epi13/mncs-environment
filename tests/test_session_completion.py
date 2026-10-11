"""Terminal sessions release their own claims instead of pinning scopes."""

from __future__ import annotations

import copy
import unittest
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest import mock

from mncs_env import claims as claims_module
from mncs_env import sessions as sessions_module
from mncs_env.session_store import ClaimBatchConflict


def _future(hours: int = 24) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(timespec="seconds")


def _past(hours: int = 1) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


def _record(session_id: str, repository: str, *, version: int = 1,
            status: str = "held", expires: str | None = None,
            kind: str = "repository") -> dict[str, Any]:
    return {
        "schema_version": claims_module.SCHEMA,
        "claim_id": f"claim:{repository}",
        "version": version,
        "repository": repository,
        "scope": {"kind": kind, "repository": repository, "checkout": None,
                  "branch": None, "paths": None, "exclusive": True},
        "session_id": session_id,
        "consumer_id": "test-consumer",
        "basis": claims_module.BASIS_EXPLICIT,
        "reason": "test",
        "status": status,
        "acquired_at": _past(2),
        "expires_at": expires if expires is not None else _future(),
        "provenance": {"acquired_by": "test-consumer"},
    }


class StubStore:
    """Dict-backed store surface used by Session completion paths."""

    def __init__(self, snapshot: dict[str, Any], claim_records: list[dict[str, Any]]):
        self.snapshot = copy.deepcopy(snapshot)
        self.claim_records = list(claim_records)
        self.events: list[dict[str, Any]] = []
        self.saved: list[dict[str, Any]] = []
        self.save_count = 0
        self.fail_save_calls: set[int] = set()

    def load_snapshot(self, session_id: str) -> dict[str, Any]:
        assert self.snapshot.get("session_id") == session_id
        return copy.deepcopy(self.snapshot)

    def save_snapshot(self, session_id: str, snapshot: dict[str, Any]) -> None:
        self.save_count += 1
        if self.save_count in self.fail_save_calls:
            raise InjectedInterruption(f"snapshot save {self.save_count}")
        self.snapshot = copy.deepcopy(snapshot)
        self.saved.append(copy.deepcopy(snapshot))

    def read_events(self, session_id: str) -> list[dict[str, Any]]:
        return list(self.events)

    def existing_sequences(self, session_id: str) -> list[int]:
        return [event.get("sequence", 0) for event in self.events]

    def put_event(self, session_id: str, sequence: int, event: dict[str, Any]) -> None:
        self.events.append(event)

    def read_claims(self) -> list[dict[str, Any]]:
        return [dict(record) for record in self.claim_records]

    def put_claim(self, claim: dict[str, Any]) -> None:
        self.claim_records.append(dict(claim))

    def claim_generation(self) -> int:
        return len(self.claim_records)

    def put_claim_batch(
        self, claims: list[dict[str, Any]], *, expected_generation: int
    ) -> None:
        observed = self.claim_generation()
        if observed != expected_generation:
            raise ClaimBatchConflict(expected_generation, observed)
        self.claim_records.extend(dict(claim) for claim in claims)


def _session(store: StubStore, session_id: str = "ses_test") -> sessions_module.Session:
    return sessions_module.Session(store, session_id)


def _snapshot(session_id: str = "ses_test") -> dict[str, Any]:
    return {"session_id": session_id, "lifecycle": "active",
            "lifecycle_history": [], "consumer_id": "test-consumer",
            "authenticated_principal_id": "principal-test"}


class InjectedInterruption(BaseException):
    """Model process termination at a durable publication boundary."""


class CompletionReleasesClaimsTests(unittest.TestCase):
    def test_complete_releases_own_live_claims(self):
        store = StubStore(_snapshot(), [
            _record("ses_test", "mncs-demo"),
            _record("ses_other", "mncs-other"),
        ])
        completion = _session(store).complete(outcome="done")
        self.assertEqual(completion["claims_released"], ["claim:mncs-demo"])
        self.assertIsNone(completion["claims_release_error"])
        live = claims_module.active_claims(store.read_claims())
        self.assertEqual(
            {(r["session_id"], r["repository"]) for r in live.values()},
            {("ses_other", "mncs-other")})

    def test_fail_releases_own_live_claims(self):
        store = StubStore(_snapshot(), [_record("ses_test", "mncs-demo")])
        result = _session(store).fail(reason="boom")
        self.assertEqual(result["claims_released"], ["claim:mncs-demo"])
        self.assertEqual(claims_module.active_claims(store.read_claims()), {})

    def test_complete_ignores_expired_and_foreign_claims(self):
        store = StubStore(_snapshot(), [
            _record("ses_test", "mncs-old", expires=_past(1)),
            _record("ses_other", "mncs-demo"),
        ])
        completion = _session(store).complete(outcome="done")
        self.assertEqual(completion["claims_released"], [])
        self.assertIsNone(completion["claims_release_error"])
        live = claims_module.active_claims(store.read_claims())
        self.assertEqual({r["session_id"] for r in live.values()}, {"ses_other"})

    def test_complete_survives_claim_store_fault(self):
        store = StubStore(_snapshot(), [_record("ses_test", "mncs-demo")])

        def broken() -> list[dict[str, Any]]:
            raise OSError("store unavailable")

        store.read_claims = broken  # type: ignore[method-assign]
        session = _session(store)
        completion = session.complete(outcome="done")
        self.assertEqual(session.snapshot["lifecycle"], "completed")
        self.assertIn("store unavailable", completion["claims_release_error"])

    def test_completion_request_replays_from_terminal_snapshot(self):
        store = StubStore(_snapshot(), [_record("ses_test", "mncs-demo")])
        session = _session(store)
        first = session.complete(
            outcome="done", summary="verified", request_id="req_complete_once",
            authenticated_principal_id="principal-test",
            consumer_id="consumer-first",
        )
        saves_after_first = store.save_count

        restarted = sessions_module.Session.open(
            state_dir=".", session_id="ses_test", store=store,
        )
        replay = restarted.complete(
            outcome="done", summary="verified", request_id="req_complete_once",
            authenticated_principal_id="principal-test",
            consumer_id="consumer-restarted",
        )

        self.assertEqual(replay, first)
        self.assertEqual(replay["request_id"], "req_complete_once")
        self.assertEqual(replay["requested_by"]["consumer_id"], "consumer-first")
        self.assertEqual(store.save_count, saves_after_first)
        self.assertEqual(
            sum(event.get("type") == "session.completed" for event in store.events), 1,
        )
        with self.assertRaisesRegex(
            sessions_module.LifecycleError, "different request identity",
        ):
            restarted.complete(
                outcome="other", summary="verified", request_id="req_complete_once",
                authenticated_principal_id="principal-test",
            )

    def test_completion_recovers_before_intent_transition_and_final_snapshot(self):
        for fail_save_call in (1, 2, 3):
            with self.subTest(fail_save_call=fail_save_call):
                store = StubStore(
                    _snapshot(), [_record("ses_test", "mncs-demo")],
                )
                store.fail_save_calls = {fail_save_call}
                initial = _session(store)
                with self.assertRaises(InjectedInterruption):
                    initial.complete(
                        outcome="done", summary="verified",
                        request_id=f"req_completion_stage_{fail_save_call}",
                        authenticated_principal_id="principal-test",
                    )

                store.fail_save_calls.clear()
                recovered = sessions_module.Session.open(
                    state_dir=".", session_id="ses_test", store=store,
                )
                if fail_save_call == 1:
                    # The intent never reached Store, so the same stable
                    # request can be submitted again as a new operation.
                    completion = recovered.complete(
                        outcome="done", summary="verified",
                        request_id="req_completion_stage_1",
                        authenticated_principal_id="principal-test",
                        consumer_id="consumer-after-restart",
                    )
                else:
                    completion = recovered.complete(
                        retry_pending=True,
                        authenticated_principal_id="principal-test",
                        consumer_id="consumer-after-restart",
                    )

                self.assertEqual(completion["outcome"], "done")
                self.assertEqual(completion["request_id"],
                                 f"req_completion_stage_{fail_save_call}")
                self.assertEqual(recovered.snapshot["lifecycle"], "completed")
                self.assertEqual(
                    completion["claims_released"], ["claim:mncs-demo"],
                )
                self.assertIsNone(completion["claims_release_error"])
                self.assertEqual(
                    sum(event.get("type") == "session.completed"
                        for event in store.events),
                    1,
                )

    def test_completion_retry_recovers_a_crash_between_claim_releases(self):
        store = StubStore(_snapshot(), [
            _record("ses_test", "mncs-alpha"),
            _record("ses_test", "mncs-beta"),
        ])
        original_release = claims_module.release
        release_count = 0

        def interrupt_after_first_release(*args, **kwargs):
            nonlocal release_count
            result = original_release(*args, **kwargs)
            release_count += 1
            if release_count == 1:
                raise InjectedInterruption("claim release published")
            return result

        with (
            mock.patch.object(
                claims_module, "release", side_effect=interrupt_after_first_release,
            ),
            self.assertRaises(InjectedInterruption),
        ):
            _session(store).complete(
                outcome="done", request_id="req_completion_claim_batch",
                authenticated_principal_id="principal-test",
            )

        restarted = sessions_module.Session.open(
            state_dir=".", session_id="ses_test", store=store,
        )
        completion = restarted.complete(
            retry_pending=True, authenticated_principal_id="principal-test",
        )

        self.assertEqual(
            completion["claims_released"], ["claim:mncs-alpha", "claim:mncs-beta"],
        )
        self.assertEqual(claims_module.active_claims(store.read_claims()), {})
        release_events = [
            event for event in store.events if event.get("type") == "lease.released"
        ]
        self.assertEqual(len(release_events), 2)
        self.assertEqual(
            len({event["payload"]["release_request_id"] for event in release_events}), 2,
        )
        self.assertEqual(
            sum(event.get("type") == "session.completed" for event in store.events), 1,
        )

    def test_retry_pending_is_fenced_to_original_authenticated_principal(self):
        store = StubStore(_snapshot(), [])
        store.fail_save_calls = {2}
        with self.assertRaises(InjectedInterruption):
            _session(store).complete(
                outcome="done", request_id="req_completion_principal_fence",
                authenticated_principal_id="principal-test",
            )
        store.fail_save_calls.clear()
        restarted = sessions_module.Session.open(
            state_dir=".", session_id="ses_test", store=store,
        )
        with self.assertRaisesRegex(
            sessions_module.LifecycleError, "principal does not match",
        ):
            restarted.complete(
                retry_pending=True,
                authenticated_principal_id="principal-other",
            )

    def test_retry_pending_rejects_modified_stored_request(self):
        store = StubStore(_snapshot(), [])
        store.fail_save_calls = {2}
        with self.assertRaises(InjectedInterruption):
            _session(store).complete(
                outcome="done", request_id="req_completion_integrity",
                authenticated_principal_id="principal-test",
            )
        store.fail_save_calls.clear()
        store.snapshot["completion_pending"]["outcome"] = "modified"
        restarted = sessions_module.Session.open(
            state_dir=".", session_id="ses_test", store=store,
        )
        with self.assertRaisesRegex(
            sessions_module.LifecycleError, "failed integrity validation",
        ):
            restarted.complete(
                retry_pending=True,
                authenticated_principal_id="principal-test",
            )

    def test_retry_pending_releases_claims_after_transient_store_fault(self):
        store = StubStore(_snapshot(), [_record("ses_test", "mncs-demo")])
        original_read_claims = store.read_claims

        def broken_read_claims():
            raise OSError("transient Store read fault")

        store.read_claims = broken_read_claims  # type: ignore[method-assign]
        first = _session(store).complete(
            outcome="done", request_id="req_completion_claim_retry",
            authenticated_principal_id="principal-test",
        )
        self.assertIn("transient Store read fault", first["claims_release_error"])

        store.read_claims = original_read_claims  # type: ignore[method-assign]
        restarted = sessions_module.Session.open(
            state_dir=".", session_id="ses_test", store=store,
        )
        recovered = restarted.complete(
            retry_pending=True,
            authenticated_principal_id="principal-test",
        )
        self.assertEqual(recovered["claims_released"], ["claim:mncs-demo"])
        self.assertIsNone(recovered["claims_release_error"])
        self.assertEqual(claims_module.active_claims(store.read_claims()), {})
        self.assertEqual(
            sum(event.get("type") == "session.completed" for event in store.events), 1,
        )

    def test_identical_request_ids_from_different_sessions_are_isolated(self):
        store = StubStore(
            _snapshot("ses_first"), [_record("ses_first", "mncs-demo")],
        )
        first = sessions_module.Session(store, "ses_first")
        first.complete(
            outcome="done", request_id="req_reused_by_sessions",
            authenticated_principal_id="principal-test",
        )

        store.snapshot = _snapshot("ses_second")
        claims_module.acquire(
            store,
            repository="mncs-demo",
            session_id="ses_second",
            consumer_id="consumer-second",
            basis=claims_module.BASIS_EXPLICIT,
            reason="test request identity isolation",
        )
        second = sessions_module.Session(store, "ses_second")
        completion = second.complete(
            outcome="done", request_id="req_reused_by_sessions",
            authenticated_principal_id="principal-test",
        )

        self.assertEqual(completion["claims_released"], ["claim:mncs-demo"])
        self.assertEqual(
            claims_module.active_claims(store.read_claims()), {},
        )


if __name__ == "__main__":
    unittest.main()
