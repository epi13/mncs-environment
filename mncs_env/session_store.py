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


STORE_PROVIDER_SCHEMA = "mncs.environment.session-store-provider/1"


def store_provider_from_environment(environment: dict[str, Any]) -> dict[str, Any] | None:
    """Project the selected Store checkout into a small resume bootstrap record."""
    selected_checkouts = environment.get("selected_checkouts", {})
    selected = selected_checkouts.get("mncs-store") if isinstance(selected_checkouts, dict) else None
    if selected is None:
        return None
    if not isinstance(selected, dict):
        raise ValueError("selected mncs-store checkout facts must be an object")

    workspace = environment.get("workspace", {})
    workspace_root_value = workspace.get("root") if isinstance(workspace, dict) else None
    checkout_value = selected.get("path")
    if not isinstance(workspace_root_value, str) or not workspace_root_value:
        raise ValueError("selected mncs-store checkout has no Environment workspace root")
    if not isinstance(checkout_value, str) or not checkout_value:
        raise ValueError("selected mncs-store checkout has no provider-owned path")
    revision = selected.get("head")
    if not isinstance(revision, str) or not revision:
        raise ValueError("selected mncs-store checkout has no bound revision")

    workspace_root = Path(workspace_root_value).resolve()
    checkout = Path(checkout_value)
    if not checkout.is_absolute():
        checkout = workspace_root / checkout
    checkout = checkout.resolve()
    if not checkout.is_relative_to(workspace_root):
        raise ValueError("selected mncs-store checkout escapes the Environment workspace")
    python_package = (checkout / "python").resolve()
    if not python_package.is_relative_to(checkout):
        raise ValueError("selected mncs-store Python package escapes its checkout")
    if not (python_package / "mncs_store" / "__init__.py").is_file():
        raise ValueError(f"selected mncs-store package is unavailable at {python_package}")

    return {
        "schema_version": STORE_PROVIDER_SCHEMA,
        "provider": "mncs-store",
        "workspace_root": str(workspace_root),
        "checkout": str(checkout),
        "python_package": str(python_package),
        "revision": revision,
        "authoritative_head": selected.get("authoritative_head"),
        "branch": selected.get("branch"),
        "source_ref": selected.get("source_ref"),
        "clean_at_selection": selected.get("clean"),
    }


def write_session_store_provider(
    state_dir: Path | str, session_id: str, binding: dict[str, Any]
) -> None:
    """Persist Store package routing outside Store so a fresh process can reopen it."""
    payload = {**binding, "session_id": session_id}
    path = Path(state_dir) / "sessions" / session_id / "store-provider.json"
    existing = read_json(path)
    if existing is not None and existing != payload:
        raise ValueError(f"session {session_id} already has a different Store provider binding")
    write_json(path, payload)


def _session_store_package(state_dir: Path | str, session_id: str) -> str | None:
    path = Path(state_dir) / "sessions" / session_id / "store-provider.json"
    payload = read_json(path)
    if payload is None:
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != STORE_PROVIDER_SCHEMA
        or payload.get("session_id") != session_id
        or payload.get("provider") != "mncs-store"
    ):
        raise ValueError(f"session {session_id} has an invalid Store provider binding")
    checkout_value = payload.get("checkout")
    package_value = payload.get("python_package")
    workspace_value = payload.get("workspace_root")
    revision = payload.get("revision")
    if not all(isinstance(value, str) and value for value in
               (checkout_value, package_value, workspace_value, revision)):
        raise ValueError(f"session {session_id} has an incomplete Store provider binding")
    workspace_root = Path(workspace_value).resolve()
    checkout = Path(checkout_value).resolve()
    package = Path(package_value).resolve()
    if not checkout.is_relative_to(workspace_root) or package != (checkout / "python").resolve():
        raise ValueError(f"session {session_id} Store provider binding is outside its checkout")
    if not (package / "mncs_store" / "__init__.py").is_file():
        raise FileNotFoundError(f"bound mncs-store package is unavailable at {package}")
    return str(package)


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

    def list_sessions(self) -> list[str]:
        """Enumerate known session ids (best-effort, backend-specific)."""
        return []

    def objects_at(self, generation: int) -> list[Any] | None:
        """Verified object projection at one generation; None when unsupported."""
        return None


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

    def list_sessions(self) -> list[str]:
        base = self.state_dir / "sessions"
        if not base.is_dir():
            return []
        return sorted(path.name for path in base.iterdir() if path.is_dir())

    def put_claim(self, claim: dict[str, Any]) -> None:
        append_jsonl(self.state_dir / "claims.jsonl", claim)

    def read_claims(self) -> list[dict[str, Any]]:
        records = read_jsonl(self.state_dir / "claims.jsonl")
        records.sort(key=lambda record: (str(record.get("claim_id", "")), int(record.get("version", 0))))
        return records


class StoreSessionStore(SessionStore):
    """Canonical Store-backed persistence (mncs-store persistent objects)."""

    def __init__(
        self,
        state_dir: Path | str,
        *,
        verify_on_open: bool = True,
        store_package_dir: str | Path | None = None,
    ):
        from .store_backend import StoreBackend

        self.state_dir = Path(state_dir)
        self.backend = StoreBackend(
            self.state_dir,
            verify_on_open=verify_on_open,
            store_package_dir=store_package_dir,
        )

    def close(self) -> None:
        self.backend.close()

    def generation(self) -> int:
        return self.backend.generation()

    def objects_at(self, generation: int) -> list[Any] | None:
        objects_at = getattr(self.backend, "objects_at", None)
        if not callable(objects_at):
            return None
        return list(objects_at(generation))

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

    def list_sessions(self) -> list[str]:
        return self.backend.list_sessions()


def open_store(
    state_dir: Path | str,
    backend: str = "store",
    *,
    verify_on_open: bool = True,
    session_id: str | None = None,
    store_package_dir: str | Path | None = None,
) -> SessionStore:
    """Open the canonical Store backend or the explicit file debug projection."""
    if backend == "file":
        return FileSessionStore(state_dir)
    if backend == "store":
        if store_package_dir is None and session_id is not None:
            store_package_dir = _session_store_package(state_dir, session_id)
        return StoreSessionStore(
            state_dir,
            verify_on_open=verify_on_open,
            store_package_dir=store_package_dir,
        )
    raise ValueError(f"unknown session store backend {backend!r}")
