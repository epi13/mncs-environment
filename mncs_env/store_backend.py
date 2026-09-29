"""Store-backed persistence for Environment-owned session objects.

Environment owns the meaning of sessions, events, snapshots, and claims;
Store owns generic durable persistence (content identity, generations,
CAS publication, commit feed). This module is the only place that speaks
the Store API; everything above it uses session-level operations.

Concurrency: every record is immutable under its domain identity, so two
processes never overwrite each other. Publication uses compare-and-swap
on the Store generation with bounded retries; a lost race re-reads and
retries, and an identical re-put is an idempotent DUPLICATE.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

SCHEMA_EVENT = b"mncs.environment.session-event/1"
SCHEMA_SNAPSHOT = b"mncs.environment.session-snapshot/1"
SCHEMA_CLAIM = b"mncs.environment.workspace-claim/1"

MAX_CAS_RETRIES = 16


class StoreUnavailable(Exception):
    """Raised when the mncs-store consumer surface cannot be loaded."""


class StoreIntegrityFailure(Exception):
    """Raised when the Store reports failed verification or recovery need."""


def _load_store_api(store_package_dir: str | Path | None = None):
    package_dir = store_package_dir or os.environ.get("MNCS_STORE_PYTHON")
    if not package_dir:
        here = Path(__file__).resolve()
        candidate = here.parents[2] / "mncs-store" / "python"
        if (candidate / "mncs_store" / "__init__.py").is_file():
            package_dir = str(candidate)
    if package_dir:
        package_path = Path(package_dir).resolve()
        if not (package_path / "mncs_store" / "__init__.py").is_file():
            raise StoreUnavailable(
                f"selected mncs-store Python package is unavailable: {package_path}"
            )
        package_dir = str(package_path)
        existing_package = sys.modules.get("mncs_store")
        existing_file = getattr(existing_package, "__file__", None)
        if existing_file and not Path(existing_file).resolve().is_relative_to(package_path):
            raise StoreUnavailable(
                "a different mncs-store checkout is already loaded in this process"
            )
        if package_dir not in sys.path:
            sys.path.insert(0, package_dir)
    try:
        from mncs_store.embedded import EmbeddedStore  # noqa: E402
        from mncs_store.errors import StoreError, StoreResultCode  # noqa: E402
    except ImportError as error:
        raise StoreUnavailable(
            "mncs-store consumer surface unavailable: bind a selected mncs-store "
            f"checkout or set MNCS_STORE_PYTHON ({error})"
        ) from error
    return EmbeddedStore, StoreError, StoreResultCode


def _event_identity(session_id: str, sequence: int) -> bytes:
    return f"{session_id}:evt:{sequence:010d}".encode("utf-8")


def _snapshot_identity(session_id: str, revision: int) -> bytes:
    return f"{session_id}:snap:{revision:010d}".encode("utf-8")


def _claim_identity(claim_id: str, version: int) -> bytes:
    return f"claim:{claim_id}:{version:010d}".encode("utf-8")


def _checkpoint_identity(session_id: str, checkpoint_id: str) -> bytes:
    return f"{session_id}:chk:{checkpoint_id}".encode("utf-8")


def _handoff_identity(session_id: str, handoff_id: str) -> bytes:
    return f"{session_id}:hff:{handoff_id}".encode("utf-8")


class StoreBackend:
    """Durable session storage through mncs-store persistent objects."""

    def __init__(
        self,
        state_dir: Path | str,
        *,
        verify_on_open: bool = True,
        store_package_dir: str | Path | None = None,
    ):
        EmbeddedStore, StoreError, StoreResultCode = _load_store_api(store_package_dir)
        self._api = (EmbeddedStore, StoreError, StoreResultCode)
        self.store_package_dir = (
            str(Path(store_package_dir).resolve())
            if store_package_dir is not None else None
        )
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / "store"
        self._store = EmbeddedStore(self.path, verify_on_open=verify_on_open)
        recovery = getattr(self._store, "recovery_result", None)
        if recovery is not None and str(recovery) in (
            "StoreResultCode.INTEGRITY_FAILURE",
            "StoreResultCode.RECOVERY_REQUIRED",
            "StoreResultCode.DENIED",
        ):
            raise StoreIntegrityFailure(f"store recovery reported {recovery}")

    def close(self) -> None:
        self._store.close()

    # -- low-level put with CAS retry ------------------------------------

    def _put_immutable(
        self,
        *,
        schema: bytes,
        identity: bytes,
        payload: bytes,
    ):
        _, StoreError, StoreResultCode = self._api
        descriptor = json.dumps(
            {"schema": schema.decode(), "identity": identity.decode()},
            sort_keys=True,
        ).encode()
        last_error: Exception | None = None
        for _ in range(MAX_CAS_RETRIES):
            generation = self._store.current_generation
            try:
                result = self._store.put_bound_object(
                    domain_schema=schema,
                    domain_identity=identity,
                    descriptor=descriptor,
                    payload=payload,
                    expected_generation=generation,
                )
            except StoreError as error:
                if error.code == StoreResultCode.IDENTITY_CONFLICT:
                    raise
                last_error = error
                continue
            if result.code == StoreResultCode.STALE_GENERATION:
                last_error = StoreError(result.code, "generation advanced; retrying")
                continue
            return result
        raise last_error or StoreError(StoreResultCode.CONFLICT, "CAS retries exhausted")

    # -- events ------------------------------------------------------------

    def put_event(self, session_id: str, sequence: int, event: dict[str, Any]):
        from mncs_store.errors import StoreResultCode  # noqa: E402

        payload = json.dumps(event, ensure_ascii=False, sort_keys=True).encode()
        try:
            return self._put_immutable(
                schema=SCHEMA_EVENT,
                identity=_event_identity(session_id, sequence),
                payload=payload,
            )
        except Exception as error:
            if getattr(error, "code", None) == StoreResultCode.IDENTITY_CONFLICT:
                raise _SequenceTaken(session_id, sequence) from error
            raise

    def read_events(self, session_id: str) -> list[dict[str, Any]]:
        prefix = f"{session_id}:evt:".encode()
        found: list[tuple[int, dict[str, Any]]] = []
        for item in self._store.find_bound_objects(SCHEMA_EVENT, prefix):
            try:
                sequence = int(item.domain_identity[len(prefix):])
                found.append((sequence, json.loads(item.payload.decode("utf-8"))))
            except (ValueError, json.JSONDecodeError):
                continue
        found.sort(key=lambda pair: pair[0])
        return [event for _, event in found]

    # -- snapshots -----------------------------------------------------------

    def put_snapshot(self, session_id: str, revision: int, snapshot: dict[str, Any]):
        payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()
        return self._put_immutable(
            schema=SCHEMA_SNAPSHOT,
            identity=_snapshot_identity(session_id, revision),
            payload=payload,
        )

    def read_snapshot(self, session_id: str) -> dict[str, Any] | None:
        prefix = f"{session_id}:snap:".encode()
        best: tuple[int, dict[str, Any]] | None = None
        for item in self._store.find_bound_objects(SCHEMA_SNAPSHOT, prefix):
            try:
                revision = int(item.domain_identity[len(prefix):])
                record = json.loads(item.payload.decode("utf-8"))
            except (ValueError, json.JSONDecodeError):
                continue
            if best is None or revision > best[0]:
                best = (revision, record)
        return best[1] if best else None

    # -- claims ---------------------------------------------------------------

    def put_claim(self, claim: dict[str, Any]):
        claim_id = str(claim.get("claim_id", ""))
        version = int(claim.get("version", 0))
        payload = json.dumps(claim, ensure_ascii=False, sort_keys=True).encode()
        return self._put_immutable(
            schema=SCHEMA_CLAIM,
            identity=_claim_identity(claim_id, version),
            payload=payload,
        )

    def read_claims(self) -> list[dict[str, Any]]:
        out = []
        for item in self._store.find_bound_objects(SCHEMA_CLAIM):
            try:
                out.append(json.loads(item.payload.decode("utf-8")))
            except json.JSONDecodeError:
                continue
        out.sort(key=lambda record: (str(record.get("claim_id", "")), int(record.get("version", 0))))
        return out

    # -- checkpoints / handoffs ---------------------------------------------------

    def put_record(self, schema: bytes, identity: bytes, record: dict[str, Any]):
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True).encode()
        return self._put_immutable(schema=schema, identity=identity, payload=payload)

    def get_record(self, schema: bytes, identity: bytes) -> dict[str, Any] | None:
        from mncs_store.errors import StoreError  # noqa: E402

        try:
            item = next(
                (
                    candidate
                    for candidate in self._store.find_bound_objects(schema, identity)
                    if candidate.domain_identity == identity
                ),
                None,
            )
        except StoreError:
            return None
        if item is None:
            return None
        try:
            record = json.loads(item.payload.decode("utf-8"))
        except json.JSONDecodeError:
            return None
        return record if isinstance(record, dict) else None

    def checkpoint_identity(self, session_id: str, checkpoint_id: str) -> bytes:
        return _checkpoint_identity(session_id, checkpoint_id)

    def handoff_identity(self, session_id: str, handoff_id: str) -> bytes:
        return _handoff_identity(session_id, handoff_id)

    # -- observation / integrity -------------------------------------------------

    def generation(self) -> int:
        return self._store.current_generation

    def objects_at(self, generation: int) -> list[Any] | None:
        objects_at = getattr(self._store, "objects_at", None)
        if not callable(objects_at):
            return None
        return list(objects_at(generation))

    def list_sessions(self) -> list[str]:
        found: set[str] = set()
        for item in self._store.find_bound_objects(SCHEMA_SNAPSHOT):
            try:
                identity = item.domain_identity.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if ":snap:" in identity:
                found.add(identity.split(":snap:")[0])
        return sorted(found)

    def commit_feed(self) -> bytes:
        return self._store.commit_feed()

    def verify(self) -> dict[str, object]:
        return self._store.verify()


class _SequenceTaken(Exception):
    """Raised when another writer committed a different event at a sequence."""

    def __init__(self, session_id: str, sequence: int):
        super().__init__(f"event sequence {sequence} already taken in {session_id}")
        self.session_id = session_id
        self.sequence = sequence
