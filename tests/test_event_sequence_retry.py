from __future__ import annotations

from typing import Any

from mncs_env.session_store import SequenceTaken
from mncs_env.sessions import Session


class EventStore:
    def __init__(self, session_id: str) -> None:
        self.events = [{"sequence": 1, "type": "session.created"}]
        self.attempted: list[int] = []
        self.snapshot = {"session_id": session_id, "lifecycle": "active"}

    def load_snapshot(self, session_id: str) -> dict[str, Any]:
        assert session_id == self.snapshot["session_id"]
        return dict(self.snapshot)

    def existing_sequences(self, session_id: str) -> list[int]:
        assert session_id == self.snapshot["session_id"]
        return [int(event["sequence"]) for event in self.events]

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
