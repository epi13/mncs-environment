"""Session persistence interface with Store and file implementations.

The Store backend is canonical: immutable event/snapshot/claim objects,
generation CAS, and a commit feed. The file backend is an explicit
import/export/debug projection with identical semantics over JSON files;
it must never become a second authority.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .persist import append_jsonl, read_json, read_jsonl, write_json


class SequenceTaken(Exception):
    """Another writer committed a different record at this sequence/revision."""


class SessionStore:
    """Persistence contract for one state directory."""

    def load_snapshot(self, session_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def save_snapshot(self, session_id: str, snapshot: dict[str, Any]) -> None:
        raise NotImplementedError

    def read_events(self, session_id: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def put_event(self, session_id: str, sequence: int, event: dict[str, Any]) -> None:
        """Persist one sequenced event; raise SequenceTaken on foreign conflict."""
        raise NotImplementedError

    def existing_sequences(self, session_id: str) -> list[int]:
        raise NotImplementedError

    def save_checkpoint(self, session_id: str, record: dict[str, Any]) -> None:
        raise NotImplementedError

    def load_checkpoint(self, session_id: str, checkpoint_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def save_handoff(self, session_id: str, record: dict[str, Any]) -> None:
        raise NotImplementedError

    def load_handoff(self, session_id: str, handoff_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def put_claim(self, claim: dict[str, Any]) -> None:
        raise NotImplementedError

    def read_claims(self) -> list[dict[str, Any]]:
        raise NotImplementedError


class FileSessionStore(SessionStore):
    """Debug/import/export projection over JSON files (not canonical)."""

    def __init__(self, state_dir: Path | str):
        self.state_dir = Path(state_dir)

    def _directory(self, session_id: str) -> Path:
        return self.state_dir / "sessions" / session_id

    def load_snapshot(self, session_id: str) -> dict[str, Any] | None:
        record = read_json(self._directory(session_id) / "session.json")
        return record if isinstance(record, dict) else None

    def save_snapshot(self, session_id: str, snapshot: dict[str, Any]) -> None:
        write_json(self._directory(session_id) / "session.json", snapshot)

    def read_events(self, session_id: str) -> list[dict[str, Any]]:
        return read_jsonl(self._directory(session_id) / "events.jsonl")

    def put_event(self, session_id: str, sequence: int, event: dict[str, Any]) -> None:
        for existing in self.read_events(session_id):
            if existing.get("sequence") == sequence and existing != event:
                raise SequenceTaken(f"sequence {sequence} already taken")
            if existing == event:
                return
        append_jsonl(self._directory(session_id) / "events.jsonl", event)

    def existing_sequences(self, session_id: str) -> list[int]:
        return sorted(
            int(event["sequence"]) for event in self.read_events(session_id)
            if isinstance(event.get("sequence"), int)
        )

    def save_checkpoint(self, session_id: str, record: dict[str, Any]) -> None:
        write_json(self._directory(session_id) / "checkpoints" / f"{record['identity']}.json", record)

    def load_checkpoint(self, session_id: str, checkpoint_id: str) -> dict[str, Any] | None:
        record = read_json(self._directory(session_id) / "checkpoints" / f"{checkpoint_id}.json")
        return record if isinstance(record, dict) else None

    def save_handoff(self, session_id: str, record: dict[str, Any]) -> None:
        write_json(self._directory(session_id) / "handoffs" / f"{record['identity']}.json", record)

    def load_handoff(self, session_id: str, handoff_id: str) -> dict[str, Any] | None:
        record = read_json(self._directory(session_id) / "handoffs" / f"{handoff_id}.json")
        return record if isinstance(record, dict) else None

    def put_claim(self, claim: dict[str, Any]) -> None:
        append_jsonl(self.state_dir / "claims.jsonl", claim)

    def read_claims(self) -> list[dict[str, Any]]:
        records = read_jsonl(self.state_dir / "claims.jsonl")
        records.sort(key=lambda record: (str(record.get("claim_id", "")), int(record.get("version", 0))))
        return records


class StoreSessionStore(SessionStore):
    """Canonical Store-backed persistence (mncs-store persistent objects)."""

    def __init__(self, state_dir: Path | str, *, verify_on_open: bool = True):
        from .store_backend import StoreBackend

        self.state_dir = Path(state_dir)
        self.backend = StoreBackend(self.state_dir, verify_on_open=verify_on_open)

    def close(self) -> None:
        self.backend.close()

    def generation(self) -> int:
        return self.backend.generation()

    def load_snapshot(self, session_id: str) -> dict[str, Any] | None:
        return self.backend.read_snapshot(session_id)

    def save_snapshot(self, session_id: str, snapshot: dict[str, Any]) -> None:
        revision = int(snapshot.get("snapshot_sequence", 0))
        self.backend.put_snapshot(session_id, revision, snapshot)

    def read_events(self, session_id: str) -> list[dict[str, Any]]:
        return self.backend.read_events(session_id)

    def put_event(self, session_id: str, sequence: int, event: dict[str, Any]) -> None:
        from .store_backend import _SequenceTaken

        try:
            self.backend.put_event(session_id, sequence, event)
        except _SequenceTaken as error:
            raise SequenceTaken(str(error)) from error

    def existing_sequences(self, session_id: str) -> list[int]:
        return sorted(
            int(event["sequence"]) for event in self.read_events(session_id)
            if isinstance(event.get("sequence"), int)
        )

    def save_checkpoint(self, session_id: str, record: dict[str, Any]) -> None:
        from .store_backend import SCHEMA_SNAPSHOT

        self.backend.put_record(
            SCHEMA_SNAPSHOT,
            self.backend.checkpoint_identity(session_id, record["identity"]),
            record,
        )

    def load_checkpoint(self, session_id: str, checkpoint_id: str) -> dict[str, Any] | None:
        from .store_backend import SCHEMA_SNAPSHOT

        return self.backend.get_record(
            SCHEMA_SNAPSHOT, self.backend.checkpoint_identity(session_id, checkpoint_id)
        )

    def save_handoff(self, session_id: str, record: dict[str, Any]) -> None:
        from .store_backend import SCHEMA_SNAPSHOT

        self.backend.put_record(
            SCHEMA_SNAPSHOT,
            self.backend.handoff_identity(session_id, record["identity"]),
            record,
        )

    def load_handoff(self, session_id: str, handoff_id: str) -> dict[str, Any] | None:
        from .store_backend import SCHEMA_SNAPSHOT

        return self.backend.get_record(
            SCHEMA_SNAPSHOT, self.backend.handoff_identity(session_id, handoff_id)
        )

    def put_claim(self, claim: dict[str, Any]) -> None:
        self.backend.put_claim(claim)

    def read_claims(self) -> list[dict[str, Any]]:
        return self.backend.read_claims()


def open_store(
    state_dir: Path | str, backend: str = "store", *, verify_on_open: bool = True
) -> SessionStore:
    """Open a session store; 'file' is the debug projection, 'store' canonical."""
    if backend == "file":
        return FileSessionStore(state_dir)
    if backend == "store":
        return StoreSessionStore(state_dir, verify_on_open=verify_on_open)
    raise ValueError(f"unknown session store backend {backend!r}")
