"""Terminal sessions release their own claims instead of pinning scopes."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

from mncs_env import claims as claims_module
from mncs_env import sessions as sessions_module


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
        self.snapshot = snapshot
        self.claim_records = list(claim_records)
        self.events: list[dict[str, Any]] = []
        self.saved: list[dict[str, Any]] = []

    def load_snapshot(self, session_id: str) -> dict[str, Any]:
        assert self.snapshot.get("session_id") == session_id
        return self.snapshot

    def save_snapshot(self, session_id: str, snapshot: dict[str, Any]) -> None:
        self.saved.append(dict(snapshot))

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


def _session(store: StubStore, session_id: str = "ses_test") -> sessions_module.Session:
    return sessions_module.Session(store, session_id)


def _snapshot(session_id: str = "ses_test") -> dict[str, Any]:
    return {"session_id": session_id, "lifecycle": "active",
            "lifecycle_history": [], "consumer_id": "test-consumer"}


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
            raise IOError("store unavailable")

        store.read_claims = broken  # type: ignore[method-assign]
        session = _session(store)
        completion = session.complete(outcome="done")
        self.assertEqual(session.snapshot["lifecycle"], "completed")
        self.assertIn("store unavailable", completion["claims_release_error"])


if __name__ == "__main__":
    unittest.main()
