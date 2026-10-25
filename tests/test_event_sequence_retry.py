from __future__ import annotations

from typing import Any

import pytest

from mncs_env.session_store import SequenceTaken
from mncs_env.sessions import LifecycleError, Session


class EventStore:
    def __init__(self, session_id: str) -> None:
        self.events = [{"sequence": 1, "type": "session.created"}]
        self.attempted: list[int] = []
        self.event_log_reads = 0
        self.event_tail_reads = 0
        self.snapshot = {
            "session_id": session_id,
            "lifecycle": "active",
            "authenticated_principal_id": "ctrl_" + "b" * 32,
            "environment_id": "renv_test",
            "campaign": {"identity": "cmp_test"},
            "intent": {"identity": "int_test"},
        }

    def load_snapshot(self, session_id: str) -> dict[str, Any]:
        assert session_id == self.snapshot["session_id"]
        return dict(self.snapshot)

    def existing_sequences(self, session_id: str) -> list[int]:
        assert session_id == self.snapshot["session_id"]
        return [int(event["sequence"]) for event in self.events]

    def read_events(self, session_id: str) -> list[dict[str, Any]]:
        assert session_id == self.snapshot["session_id"]
        self.event_log_reads += 1
        return list(self.events)

    def read_event_tail(
        self, session_id: str, *, limit: int,
    ) -> tuple[list[dict[str, Any]], int]:
        assert session_id == self.snapshot["session_id"]
        self.event_tail_reads += 1
        events = sorted(self.events, key=lambda event: event["sequence"])
        return events[-limit:], len(events)

    def save_snapshot(self, session_id: str, snapshot: dict[str, Any]) -> None:
        assert session_id == self.snapshot["session_id"]
        self.snapshot = dict(snapshot)

    def put_event(self, session_id: str, sequence: int, event: dict[str, Any]) -> None:
        assert session_id == self.snapshot["session_id"]
        self.attempted.append(sequence)
        if sequence == 2:
            # This identity is unavailable even though it has no committed
            # log entry, as with a durable orphan manifest in canonical Store.
            raise SequenceTaken("sequence 2 has an immutable staged identity")
        self.events.append(event)


def test_emit_advances_after_a_reserved_unpublished_event_sequence() -> None:
    session_id = "ses_reserved_event_sequence"
    store = EventStore(session_id)
    session = Session(store, session_id)

    emitted = session._emit("session.resumed", "test")

    assert store.attempted == [2, 3]
    assert emitted["sequence"] == 3
    assert [event["sequence"] for event in store.events] == [1, 3]


def _rpc_activity_payload(
    operation: str = "session.inspect", digest: str = "a" * 64,
) -> dict[str, Any]:
    return {
        "schema_version": "mncs.environment.rpc-read-activity/1",
        "operation": operation,
        "outcome_status": "succeeded",
        "exit_code": 0,
        "failure_code": None,
        "request_digest": digest,
        "caller": {
            "authenticated_principal_id": "ctrl_" + "b" * 32,
            "control_grant_id": "c" * 32,
            "process_identity": "proc_" + "d" * 32,
        },
        "context": {
            "repository": "mncs-control-mcp",
            "repository_branch": "campaign/test",
            "repository_head": "e" * 40,
            "environment_id": "renv_test",
            "campaign_id": "cmp_test",
            "work_intent_id": "int_test",
        },
        "measurement_availability": {
            "model_identity": "unavailable",
            "token_usage": "unavailable",
            "control_dispatch_elapsed_ms": "measured",
            "resource_consumption": "unavailable",
            "cache_accounting": "unavailable",
        },
        "measurements": {
            "control_dispatch_elapsed_ms": 12.5,
        },
    }


def _context_response_summary(**overrides: Any) -> dict[str, Any]:
    return {
        "schema_version": "mncs.environment.context-response-summary/1",
        "capture_status": "captured",
        "capture_reason": None,
        "context_schema_version": "mncs.environment.entry-context/1",
        "available_count": 1,
        "listed_available_count": 1,
        "unavailable_count": 0,
        "unknown_count": 0,
        "capabilities_truncated": False,
        "available_identity_digest": "f" * 64,
        "identity_digest_status": "complete",
        **overrides,
    }


def test_context_response_activity_is_bounded_and_schema_scoped() -> None:
    session_id = "ses_rpc_context_summary"
    store = EventStore(session_id)
    session = Session(store, session_id)
    payload = _rpc_activity_payload("session.context", "a" * 64)
    payload["schema_version"] = "mncs.environment.rpc-read-activity/2"
    payload["context_response_summary"] = _context_response_summary()

    recorded = session.record_rpc_read_activity(
        payload, request_id="req_rpc_context_summary",
        operation_digest="a" * 64,
    )

    assert recorded["status"] == "recorded"
    assert recorded["event"]["payload"]["context_response_summary"] == (
        payload["context_response_summary"]
    )
    malformed = _rpc_activity_payload("session.context", "b" * 64)
    malformed["schema_version"] = "mncs.environment.rpc-read-activity/2"
    malformed["context_response_summary"] = _context_response_summary(
        available_count=2,
    )
    with pytest.raises(ValueError, match="observation is malformed"):
        session.record_rpc_read_activity(
            malformed, request_id="req_rpc_context_bad_summary",
            operation_digest="b" * 64,
        )
    invalid_reason = _rpc_activity_payload("session.context", "d" * 64)
    invalid_reason["schema_version"] = "mncs.environment.rpc-read-activity/2"
    invalid_reason["context_response_summary"] = {
        **_context_response_summary(),
        "capture_status": "unavailable",
        "capture_reason": [],
        "context_schema_version": None,
        "available_count": None,
        "listed_available_count": None,
        "unavailable_count": None,
        "unknown_count": None,
        "capabilities_truncated": None,
        "available_identity_digest": None,
        "identity_digest_status": "unavailable",
    }
    with pytest.raises(ValueError, match="observation is malformed"):
        session.record_rpc_read_activity(
            invalid_reason, request_id="req_rpc_context_bad_reason",
            operation_digest="d" * 64,
        )
    failed_with_capture = _rpc_activity_payload("session.context", "e" * 64)
    failed_with_capture.update({
        "schema_version": "mncs.environment.rpc-read-activity/2",
        "outcome_status": "failed",
        "exit_code": 2,
        "failure_code": "environment-operation-failed",
        "context_response_summary": _context_response_summary(),
    })
    with pytest.raises(ValueError, match="observation is malformed"):
        session.record_rpc_read_activity(
            failed_with_capture, request_id="req_rpc_context_failed_capture",
            operation_digest="e" * 64,
        )
    misplaced = _rpc_activity_payload("session.inspect", "c" * 64)
    misplaced["schema_version"] = "mncs.environment.rpc-read-activity/2"
    misplaced["context_response_summary"] = _context_response_summary()
    with pytest.raises(ValueError, match="observation is malformed"):
        session.record_rpc_read_activity(
            misplaced, request_id="req_rpc_context_wrong_operation",
            operation_digest="c" * 64,
        )


def test_rpc_read_activity_is_idempotent_and_capped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("mncs_env.sessions.MAX_RPC_READ_ACTIVITY_OBSERVATIONS", 2)
    session_id = "ses_rpc_activity_cap"
    store = EventStore(session_id)
    session = Session(store, session_id)

    first = session.record_rpc_read_activity(
        _rpc_activity_payload(), request_id="req_rpc_first",
        operation_digest="a" * 64,
    )
    replay = session.record_rpc_read_activity(
        _rpc_activity_payload(), request_id="req_rpc_first",
        operation_digest="a" * 64,
    )
    second = session.record_rpc_read_activity(
        _rpc_activity_payload("campaign.state", "f" * 64), request_id="req_rpc_second",
        operation_digest="f" * 64,
    )
    capped = session.record_rpc_read_activity(
        _rpc_activity_payload("store.generation", "0" * 64), request_id="req_rpc_third",
        operation_digest="0" * 64,
    )

    assert first["status"] == "recorded"
    assert replay["status"] == "already-recorded"
    assert replay["event"]["identity"] == first["event"]["identity"]
    assert second["event"]["payload"]["observation_ordinal"] == 2
    assert capped == {"status": "capture-limit-reached", "event": None}
    activity_events = [
        event for event in store.events
        if event.get("type") == "environment.rpc.read_observed"
    ]
    assert len(activity_events) == 2
    assert store.snapshot["rpc_read_activity_count"] == 2
    assert store.snapshot["rpc_read_activity_limit_reached"] is True
    assert store.event_log_reads == 0
    assert store.event_tail_reads == 2


def test_rpc_read_activity_resumes_after_the_previous_capture_limit() -> None:
    from mncs_env import capability_intelligence, sessions

    session_id = "ses_rpc_activity_limit_upgrade"
    store = EventStore(session_id)
    session = Session(store, session_id)
    assert sessions.MAX_RPC_READ_ACTIVITY_OBSERVATIONS == 256
    assert capability_intelligence.MAX_RPC_READ_ACTIVITY_OBSERVATIONS == 256

    for ordinal in range(1, 129):
        digest = f"{ordinal:064x}"
        result = session.record_rpc_read_activity(
            _rpc_activity_payload(digest=digest),
            request_id=f"req_rpc_upgrade_{ordinal}",
            operation_digest=digest,
        )
        assert result["status"] == "recorded"

    # A session produced by the previous 128-record build persists this flag.
    store.snapshot["rpc_read_activity_limit_reached"] = True
    resumed = Session(store, session_id).record_rpc_read_activity(
        _rpc_activity_payload(digest="f" * 64),
        request_id="req_rpc_upgrade_129",
        operation_digest="f" * 64,
    )

    assert resumed["status"] == "recorded"
    assert resumed["event"]["payload"]["observation_ordinal"] == 129
    assert store.snapshot["rpc_read_activity_count"] == 129
    assert store.snapshot["rpc_read_activity_limit_reached"] is False


def test_rpc_read_activity_rejects_request_identity_reuse() -> None:
    session_id = "ses_rpc_activity_conflict"
    store = EventStore(session_id)
    session = Session(store, session_id)
    session.record_rpc_read_activity(
        _rpc_activity_payload(), request_id="req_rpc_shared",
        operation_digest="a" * 64,
    )

    with pytest.raises(LifecycleError, match="different Environment operation"):
        session.record_rpc_read_activity(
            _rpc_activity_payload("campaign.state", "f" * 64),
            request_id="req_rpc_shared", operation_digest="f" * 64,
        )


def test_rpc_read_activity_repairs_a_lost_snapshot_after_event_publication() -> None:
    session_id = "ses_rpc_activity_snapshot_repair"
    store = EventStore(session_id)
    session = Session(store, session_id)
    payload = _rpc_activity_payload()

    first = session.record_rpc_read_activity(
        payload, request_id="req_rpc_snapshot_repair",
        operation_digest="a" * 64,
    )
    store.snapshot.pop("rpc_read_activity_count", None)
    store.snapshot.pop("rpc_read_activity_request_index", None)
    restarted = Session(store, session_id)
    replay = restarted.record_rpc_read_activity(
        payload, request_id="req_rpc_snapshot_repair",
        operation_digest="a" * 64,
    )

    assert replay["status"] == "already-recorded"
    assert replay["event"]["identity"] == first["event"]["identity"]
    assert store.snapshot["rpc_read_activity_count"] == 1
    assert len(store.snapshot["rpc_read_activity_request_index"]) == 1
    assert store.event_log_reads == 0
    assert store.event_tail_reads == 2


def test_rpc_read_activity_index_supports_replay_after_event_leaves_tail() -> None:
    session_id = "ses_rpc_activity_old_replay"
    store = EventStore(session_id)
    session = Session(store, session_id)
    payload = _rpc_activity_payload()

    first = session.record_rpc_read_activity(
        payload, request_id="req_rpc_old_replay", operation_digest="a" * 64,
    )
    store.events.extend(
        {"identity": f"evt_other_{sequence}", "sequence": sequence,
         "type": "session.checkpointed", "payload": {}}
        for sequence in range(10, 310)
    )
    store.event_tail_reads = 0
    restarted = Session(store, session_id)
    replay = restarted.record_rpc_read_activity(
        payload, request_id="req_rpc_old_replay", operation_digest="a" * 64,
    )

    assert replay["status"] == "already-recorded"
    assert replay["event"]["identity"] == first["event"]["identity"]
    assert store.event_log_reads == 0
    assert store.event_tail_reads == 0


def test_persisted_rpc_read_activity_flows_into_session_pressure_projection() -> None:
    from mncs_env.capability_intelligence import _session_activity

    session_id = "ses_rpc_activity_projection"
    store = EventStore(session_id)
    session = Session(store, session_id)
    for request_id, digest in (
        ("req_rpc_projection_one", "a" * 64),
        ("req_rpc_projection_two", "b" * 64),
    ):
        payload = _rpc_activity_payload(digest=digest)
        payload.update({
            "outcome_status": "failed",
            "exit_code": 2,
            "failure_code": "ENVIRONMENT_SESSION_MISSING",
        })
        session.record_rpc_read_activity(
            payload, request_id=request_id, operation_digest=digest,
        )

    activity = _session_activity(session, session.snapshot)

    assert activity["rpc_operation_count"] == 2
    assert len(activity["rpc_failure_candidates"]) == 1
    candidate = activity["rpc_failure_candidates"][0]
    assert candidate["distinct_session_count"] == 1
    assert candidate["recurrence_scope"] == "same-session-selected-event-window"
    assert [
        item["identity"] for item in candidate["supporting_events"]
    ] == [event["identity"] for event in store.events[1:]]
    assert store.event_log_reads == 0
