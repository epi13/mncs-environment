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

import errno
import heapq
import json
import os
import sys
import threading
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import workspace as workspace_module

SCHEMA_EVENT = b"mncs.environment.session-event/1"
SCHEMA_SNAPSHOT = b"mncs.environment.session-snapshot/1"
SCHEMA_CLAIM = b"mncs.environment.workspace-claim/1"
SCHEMA_PROJECTION = b"mncs.environment.projection-state/1"
SCHEMA_PROJECTION_EVIDENCE = b"mncs.environment.verification-evidence/1"

MAX_CAS_RETRIES = 16
_STORE_RUNTIME_LOCK = threading.RLock()
_STORE_RUNTIME_KEYS = (
    "MNCS_STORE_ROOT",
    "MNCS_LANGUAGE_ROOT",
    "MNCS_BIN",
    "MNCS_EMBED_LIB",
)


@contextmanager
def _selected_store_runtime(runtime: dict[str, str] | None, cache_dir: Path | None = None):
    """Open a Store session with its Environment-selected MNCS toolchain."""
    if runtime is None:
        yield
        return
    if not set(_STORE_RUNTIME_KEYS) <= set(runtime) or set(runtime) - {*_STORE_RUNTIME_KEYS, "MNCS_STDLIB_ROOT"}:
        raise ValueError("selected Store runtime must bind the complete MNCS toolchain")
    with _STORE_RUNTIME_LOCK:
        # A raw artifact override bypasses the selected source/compiler cache
        # identity. It belongs to standalone bootstrap, never selected entry.
        scoped_keys = (*_STORE_RUNTIME_KEYS, "MNCS_STDLIB_ROOT", "MNCS_STORE_ARTIFACT", "MNCS_STORE_ARTIFACT_CACHE")
        previous = {key: os.environ.get(key) for key in scoped_keys}
        try:
            os.environ.update(runtime)
            if "MNCS_STDLIB_ROOT" not in runtime:
                os.environ.pop("MNCS_STDLIB_ROOT", None)
            os.environ.pop("MNCS_STORE_ARTIFACT", None)
            if cache_dir is not None:
                os.environ["MNCS_STORE_ARTIFACT_CACHE"] = str(cache_dir.resolve())
            yield
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


class StoreUnavailable(Exception):
    """Raised when the mncs-store consumer surface cannot be loaded."""

    def __init__(self, message: str, *, code: str = "store-unavailable") -> None:
        self.code = code
        super().__init__(message)


class StoreIntegrityFailure(Exception):
    """Raised when the Store reports failed verification or recovery need."""


def _store_package_for_checkout(checkout: Path) -> Path | None:
    """Sibling Store fallback, following linked worktrees to their main."""
    direct = checkout.parent / "mncs-store" / "python"
    if (direct / "mncs_store" / "__init__.py").is_file():
        return direct
    completed = workspace_module._git(checkout, "rev-parse",
                                      "--git-common-dir")
    if completed is None or completed.returncode != 0:
        return None
    common = Path(completed.stdout.strip())
    if not common.is_absolute():
        common = checkout / common
    main_checkout = common.parent if common.name == ".git" else common
    candidate = main_checkout.parent / "mncs-store" / "python"
    if (candidate / "mncs_store" / "__init__.py").is_file():
        return candidate
    return None


@lru_cache(maxsize=1)
def _workspace_store_package() -> Path | None:
    """Sibling Store fallback that also works from linked worktrees."""
    here = Path(__file__).resolve()
    return _store_package_for_checkout(here.parents[1])


def _load_store_api(store_package_dir: str | Path | None = None):
    package_dir = store_package_dir or os.environ.get("MNCS_STORE_PYTHON")
    if not package_dir:
        fallback = _workspace_store_package()
        if fallback is not None:
            package_dir = str(fallback)
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


def _projection_identity(projection_id: str, version: int) -> bytes:
    return f"{projection_id}:{int(version):010d}".encode("utf-8")


def _projection_prefix(projection_id: str) -> bytes:
    return f"{projection_id}:".encode("utf-8")


def _evidence_identity(evidence_id: str) -> bytes:
    return str(evidence_id).encode("utf-8")


class StoreBackend:
    """Durable session storage through mncs-store persistent objects."""

    def __init__(
        self,
        state_dir: Path | str,
        *,
        verify_on_open: bool = True,
        store_package_dir: str | Path | None = None,
        store_runtime: dict[str, str] | None = None,
        defer_mutation: bool = False,
    ):
        EmbeddedStore, StoreError, StoreResultCode = _load_store_api(store_package_dir)
        self._api = (EmbeddedStore, StoreError, StoreResultCode)
        self.store_package_dir = (
            str(Path(store_package_dir).resolve())
            if store_package_dir is not None else None
        )
        self.store_runtime = dict(store_runtime) if store_runtime is not None else None
        self.state_dir = Path(state_dir)
        self.path = self.state_dir / "store"
        self._promotion_owner = None
        self._verify_on_open = verify_on_open
        with _selected_store_runtime(self.store_runtime, self.state_dir / "provider-cache" / "mncs-store"):
            if defer_mutation and (self.path / "head").is_file():
                self._store = EmbeddedStore(self.path, read_only=True)
            else:
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
        if self._promotion_owner is not None:
            self._promotion_owner.close()

    def _ensure_writable(self) -> None:
        if not getattr(self._store, "read_only", False):
            return
        EmbeddedStore, _, _ = self._api
        owner = self._store
        # Reuse the admitted native session. Promotion runs recovery; it
        # must not eagerly re-verify every unrelated payload. Each
        # publication validates its generation and bindings, reads verify
        # selected objects, and a complete scrub stays explicit via verify().
        try:
            self._store = EmbeddedStore(self.path, session=owner.session,
                                        verify_on_open=False)
        except OSError as error:
            if error.errno == errno.EROFS:
                raise StoreUnavailable(
                    "canonical Store filesystem transport is read-only in this execution",
                    code="direct-filesystem-read-only",
                ) from error
            raise
        self._promotion_owner = owner

    # -- low-level put with CAS retry ------------------------------------

    def _put_immutable(
        self,
        *,
        schema: bytes,
        identity: bytes,
        payload: bytes,
    ):
        self._ensure_writable()
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
            except OSError as error:
                if error.errno == errno.EROFS:
                    raise StoreUnavailable(
                        "canonical Store filesystem transport is read-only in this execution",
                        code="direct-filesystem-read-only",
                    ) from error
                raise
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

    def put_event_at_generation(
        self, session_id: str, sequence: int, event: dict[str, Any], *,
        expected_generation: int,
    ):
        """Publish a sequenced event in a one-object Store generation CAS."""
        self._ensure_writable()
        from mncs_store import BoundObjectInput  # noqa: E402
        from mncs_store.errors import StoreResultCode  # noqa: E402

        identity = _event_identity(session_id, sequence)
        payload = json.dumps(event, ensure_ascii=False, sort_keys=True).encode()
        descriptor = json.dumps(
            {"schema": SCHEMA_EVENT.decode(), "identity": identity.decode()},
            sort_keys=True,
        ).encode()
        result = self._store.put_bound_objects(
            [BoundObjectInput(SCHEMA_EVENT, identity, descriptor, payload)],
            expected_generation=expected_generation,
        )
        if result.code == StoreResultCode.STALE_GENERATION:
            raise _StaleGeneration(expected_generation, int(result.observed_generation))
        if result.code == StoreResultCode.IDENTITY_CONFLICT:
            raise _SequenceTaken(session_id, sequence)
        if not result.committed:
            from mncs_store.errors import StoreError

            raise StoreError(result.code, "fenced Environment event publication failed")
        return result

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

    def read_event_tail(
        self, session_id: str, *, limit: int,
    ) -> tuple[list[dict[str, Any]], int]:
        """Read only the newest event payloads while counting all event bindings."""
        page = self.read_event_page(session_id, limit=limit)
        return page["events"], page["event_count"]

    def read_event_page(
        self, session_id: str, *, limit: int, before_sequence: int | None = None,
    ) -> dict[str, Any]:
        """Read a bounded event page from binding metadata and selected payloads."""
        if type(limit) is not int or not 1 <= limit <= 256:
            raise ValueError("recent event limit must be between 1 and 256")
        if (type(before_sequence) not in (int, type(None))
                or (before_sequence is not None and before_sequence < 1)):
            raise ValueError("event page cursor must be a positive sequence")
        prefix = f"{session_id}:evt:".encode()
        generation = self._store.current_generation
        selected: list[tuple[int, bytes]] = []
        event_count = 0
        minimum_sequence: int | None = None
        for schema, identity in self._store.domain_bindings_at(generation):
            if schema != SCHEMA_EVENT or not identity.startswith(prefix):
                continue
            suffix = identity[len(prefix):]
            if len(suffix) < 10 or not suffix.isdigit():
                continue
            try:
                sequence = int(suffix)
            except ValueError:
                continue
            if sequence < 1 or suffix != f"{sequence:010d}".encode():
                continue
            event_count += 1
            minimum_sequence = (
                sequence if minimum_sequence is None
                else min(minimum_sequence, sequence)
            )
            if before_sequence is not None and sequence >= before_sequence:
                continue
            item = (sequence, identity)
            if len(selected) < limit:
                heapq.heappush(selected, item)
            elif sequence > selected[0][0]:
                heapq.heapreplace(selected, item)

        events = []
        for sequence, identity in sorted(selected):
            item = self._store.get_bound_object(SCHEMA_EVENT, identity)
            try:
                event = json.loads(item.payload.decode("utf-8"))
            except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if (isinstance(event, dict)
                    and event.get("session_id") == session_id
                    and type(event.get("sequence")) is int
                    and event["sequence"] == sequence):
                events.append(event)
        has_older = bool(
            selected and minimum_sequence is not None
            and selected[0][0] > minimum_sequence
        )
        return {
            "event_count": event_count,
            "events": events,
            "truncated": has_older,
            "next_before_sequence": selected[0][0] if has_older else None,
        }

    # -- snapshots -----------------------------------------------------------

    def put_snapshot(self, session_id: str, revision: int, snapshot: dict[str, Any]):
        payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()
        _, StoreError, StoreResultCode = self._api
        try:
            return self._put_immutable(
                schema=SCHEMA_SNAPSHOT,
                identity=_snapshot_identity(session_id, revision),
                payload=payload,
            )
        except StoreError as error:
            if error.code == StoreResultCode.IDENTITY_CONFLICT:
                from .session_store import SnapshotConflict
                raise SnapshotConflict(session_id, revision) from error
            raise

    def read_snapshot(self, session_id: str) -> dict[str, Any] | None:
        prefix = f"{session_id}:snap:".encode()
        # Select the latest immutable revision from verified binding metadata
        # (no payload reads), then verify and decode only that payload. Older
        # revisions are read only if a newer payload is missing or undecodable.
        # Payload integrity failures still raise; they are never skipped.
        revisions = []
        for schema, identity in self._store.domain_bindings_at(self._store.current_generation):
            if schema != SCHEMA_SNAPSHOT or not identity.startswith(prefix):
                continue
            try:
                revisions.append(int(identity[len(prefix):]))
            except ValueError:
                continue
        for revision in sorted(revisions, reverse=True):
            identity = _snapshot_identity(session_id, revision)
            item = next(
                (
                    candidate
                    for candidate in self._store.find_bound_objects(SCHEMA_SNAPSHOT, identity)
                    if candidate.domain_identity == identity
                ),
                None,
            )
            if item is None:
                continue
            try:
                return json.loads(item.payload.decode("utf-8"))
            except (ValueError, json.JSONDecodeError):
                continue
        return None

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

    def put_claim_batch(self, claims: list[dict[str, Any]], *, expected_generation: int):
        """Publish a related claim transition in one Store generation."""
        from mncs_store import BoundObjectInput  # noqa: E402
        from mncs_store.errors import StoreResultCode  # noqa: E402

        if not claims:
            raise ValueError("claim batch must not be empty")
        objects = []
        for claim in claims:
            claim_id = str(claim.get("claim_id", ""))
            version = int(claim.get("version", 0))
            identity = _claim_identity(claim_id, version)
            payload = json.dumps(claim, ensure_ascii=False, sort_keys=True).encode()
            descriptor = json.dumps(
                {"schema": SCHEMA_CLAIM.decode(), "identity": identity.decode()},
                sort_keys=True,
            ).encode()
            objects.append(BoundObjectInput(
                SCHEMA_CLAIM, identity, descriptor, payload
            ))
        self._ensure_writable()
        try:
            result = self._store.put_bound_objects(
                objects, expected_generation=expected_generation
            )
        except OSError as error:
            if error.errno == errno.EROFS:
                raise StoreUnavailable(
                    "canonical Store filesystem transport is read-only in this execution",
                    code="direct-filesystem-read-only",
                ) from error
            raise
        if result.code == StoreResultCode.STALE_GENERATION:
            from .session_store import ClaimBatchConflict
            raise ClaimBatchConflict(
                expected_generation, int(result.observed_generation)
            )
        return result

    def read_claims(self) -> list[dict[str, Any]]:
        out = []
        for item in self._store.find_bound_objects(SCHEMA_CLAIM):
            try:
                out.append(json.loads(item.payload.decode("utf-8")))
            except json.JSONDecodeError:
                continue
        out.sort(key=lambda record: (str(record.get("claim_id", "")), int(record.get("version", 0))))
        return out

    def read_latest_claims_for(self, claim_ids: list[str]) -> list[dict[str, Any]]:
        """Read only the newest claim payload for up to eight identities.

        Store binding identities are scanned as metadata, then only the
        selected newest payload per requested identity is verified and read.
        A generation change retries the selection so the returned states come
        from one stable Store head.
        """
        if (not isinstance(claim_ids, list) or not claim_ids
                or len(claim_ids) > 8
                or any(not isinstance(item, str) or not item or len(item) > 160
                       or "\x00" in item for item in claim_ids)):
            raise ValueError("targeted claim lookup needs 1..8 bounded claim IDs")
        requested = sorted(set(claim_ids))
        for _attempt in range(2):
            generation = self._store.current_generation
            prefixes = {
                f"claim:{claim_id}:".encode(): claim_id
                for claim_id in requested
            }
            latest: dict[str, tuple[int, bytes]] = {}
            for schema, identity in self._store.domain_bindings_at(generation):
                if schema != SCHEMA_CLAIM:
                    continue
                for prefix, claim_id in prefixes.items():
                    if not identity.startswith(prefix):
                        continue
                    suffix = identity[len(prefix):]
                    if len(suffix) != 10 or not suffix.isdigit():
                        continue
                    try:
                        version = int(suffix)
                    except ValueError:
                        continue
                    if version < 1 or suffix != f"{version:010d}".encode():
                        continue
                    current = latest.get(claim_id)
                    if current is None or version > current[0]:
                        latest[claim_id] = (version, identity)

            out = []
            for claim_id in requested:
                selected = latest.get(claim_id)
                if selected is None:
                    continue
                version, identity = selected
                record = self.get_record_strict(SCHEMA_CLAIM, identity)
                if (record is None or record.get("claim_id") != claim_id
                        or type(record.get("version")) is not int
                        or record["version"] != version):
                    raise StoreIntegrityFailure(
                        "latest claim payload does not match its Store identity"
                    )
                out.append(record)
            if self._store.current_generation == generation:
                out.sort(key=lambda record: str(record.get("claim_id", "")))
                return out
        raise StoreUnavailable(
            "claim history changed during bounded identity lookup",
            code="claim-state-changed",
        )

    # -- checkpoints / handoffs ---------------------------------------------------

    def put_record(self, schema: bytes, identity: bytes, record: dict[str, Any]):
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True).encode()
        return self._put_immutable(schema=schema, identity=identity, payload=payload)

    def get_record(self, schema: bytes, identity: bytes) -> dict[str, Any] | None:
        from mncs_store.errors import StoreError  # noqa: E402

        try:
            # Checkpoints and receipts use exact identities. Route them
            # through Store's deterministic binding lookup so projection
            # reads do not enumerate every binding to find one object.
            item = self._store.get_bound_object(schema, identity)
        except StoreError:
            return None
        try:
            record = json.loads(item.payload.decode("utf-8"))
        except json.JSONDecodeError:
            return None
        return record if isinstance(record, dict) else None

    def get_record_strict(self, schema: bytes, identity: bytes) -> dict[str, Any] | None:
        """Read one bound record, preserving integrity and provider failures."""
        _, StoreError, StoreResultCode = self._api
        try:
            item = self._store.get_bound_object(schema, identity)
        except StoreError as error:
            if (error.code == StoreResultCode.DENIED
                    and error.message == "Store object binding is not current"):
                return None
            if error.code == StoreResultCode.INTEGRITY_FAILURE:
                raise StoreIntegrityFailure(str(error)) from error
            raise
        try:
            record = json.loads(item.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise StoreIntegrityFailure("Store record payload is not valid UTF-8 JSON") from error
        if not isinstance(record, dict):
            raise StoreIntegrityFailure("Store record payload is not a JSON object")
        return record

    def list_checkpoints(self, session_id: str) -> list[dict[str, Any]]:
        """Read only checkpoint objects bound to one Environment session."""
        prefix = f"{session_id}:chk:".encode("utf-8")
        generation = self._store.current_generation
        identities = [identity for schema, identity in self._store.domain_bindings_at(generation)
                      if schema == SCHEMA_SNAPSHOT and identity.startswith(prefix)]
        records = []
        for identity in sorted(identities):
            record = self.get_record(SCHEMA_SNAPSHOT, identity)
            if (isinstance(record, dict) and record.get("session_id") == session_id
                    and record.get("identity")):
                records.append(record)
        return sorted(records, key=lambda record: (int(record.get("sequence", 0)),
                                                   str(record.get("identity", ""))))

    # -- shared projection rows / verification evidence --------------------

    def read_projection_row(
            self, projection_id: str) -> tuple[int, dict[str, Any]] | None:
        prefix = _projection_prefix(projection_id)
        best: tuple[int, dict[str, Any]] | None = None
        for item in self._store.find_bound_objects(SCHEMA_PROJECTION,
                                                   prefix):
            try:
                version = int(item.domain_identity[len(prefix):])
                record = json.loads(item.payload.decode("utf-8"))
            except (ValueError, json.JSONDecodeError):
                continue
            if not isinstance(record, dict):
                continue
            if best is None or version > best[0]:
                best = (version, record)
        return best

    def read_projection_versions(self) -> dict[str, int]:
        versions: dict[str, int] = {}
        for item in self._store.find_bound_objects(SCHEMA_PROJECTION):
            try:
                identity = item.domain_identity.decode("utf-8")
                projection_id, _, version = identity.rpartition(":")
                number = int(version)
            except (UnicodeDecodeError, ValueError):
                continue
            if not projection_id:
                continue
            if number > versions.get(projection_id, 0):
                versions[projection_id] = number
        return versions

    def write_projection_row(self, projection_id: str, version: int,
                             row: dict[str, Any]) -> None:
        from mncs_store.errors import StoreResultCode  # noqa: E402

        from .projection_store import ProjectionConflict  # noqa: E402

        payload = json.dumps(row, ensure_ascii=False,
                             sort_keys=True).encode()
        try:
            self._put_immutable(
                schema=SCHEMA_PROJECTION,
                identity=_projection_identity(projection_id, version),
                payload=payload)
        except Exception as error:
            if getattr(error, "code", None) != StoreResultCode.IDENTITY_CONFLICT:
                raise
            latest = self.read_projection_row(projection_id)
            raise ProjectionConflict(
                projection_id, latest[1] if latest else None) from error

    def read_evidence(self, evidence_id: str) -> dict[str, Any] | None:
        return self.get_record(SCHEMA_PROJECTION_EVIDENCE,
                               _evidence_identity(evidence_id))

    def write_evidence(self, evidence_id: str,
                       record: dict[str, Any]) -> dict[str, Any]:
        from mncs_store.errors import StoreResultCode  # noqa: E402

        from .projection_store import EvidenceConflict  # noqa: E402

        payload = json.dumps(record, ensure_ascii=False,
                             sort_keys=True).encode()
        try:
            self._put_immutable(
                schema=SCHEMA_PROJECTION_EVIDENCE,
                identity=_evidence_identity(evidence_id),
                payload=payload)
        except Exception as error:
            if getattr(error, "code", None) != StoreResultCode.IDENTITY_CONFLICT:
                raise
            existing = self.read_evidence(evidence_id)
            if (isinstance(existing, dict) and {
                    key: existing.get(key) for key in existing
                    if key != "schema_version"} == {
                    key: record.get(key) for key in record
                    if key != "schema_version"}):
                return existing
            raise EvidenceConflict(
                f"conflicting payload under {evidence_id}") from error
        return record

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

    def domain_bindings_at(self, generation: int) -> tuple[tuple[bytes, bytes], ...]:
        observe = getattr(self._store, "domain_bindings_at", None)
        if callable(observe):
            return observe(generation)
        return tuple((item.domain_schema, item.domain_identity) for item in self._store.objects_at(generation))

    def domain_bindings_since(
        self, generation: int, *, max_generations: int = 128
    ) -> tuple[tuple[int, bytes, bytes], ...]:
        observe = getattr(self._store, "domain_bindings_since", None)
        if not callable(observe):
            raise RuntimeError("selected Store provider does not expose bounded delta replay")
        return observe(generation, max_generations=max_generations)

    def generation_inventory(self, *, keep_last: int = 4096) -> dict[str, Any]:
        # Retention classification stays provider-owned; Environment only
        # forwards the request and reports the result.
        inventory = getattr(self._store, "generation_inventory", None)
        if not callable(inventory):
            raise RuntimeError("selected Store provider does not expose retention inventory")
        return dict(inventory(keep_last=keep_last))

    def reclaim_generations(self, *, keep_last: int = 4096, dry_run: bool = True) -> dict[str, Any]:
        # Reclamation stays provider-owned; Environment never interprets or
        # deletes Store generation files itself.
        reclaim = getattr(self._store, "prune_generations", None)
        if not callable(reclaim):
            raise RuntimeError("selected Store provider does not expose generation reclaim")
        return dict(reclaim(keep_last=keep_last, dry_run=dry_run))

    def list_sessions(self) -> list[str]:
        # Session identities come from verified binding metadata; snapshot
        # payloads are never read for a listing.
        found: set[str] = set()
        for schema, identity in self._store.domain_bindings_at(self._store.current_generation):
            if schema != SCHEMA_SNAPSHOT:
                continue
            try:
                text = identity.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if ":snap:" in text:
                found.add(text.split(":snap:")[0])
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


class _StaleGeneration(Exception):
    """Raised when a Store generation CAS observed newer durable state."""

    def __init__(self, expected_generation: int, observed_generation: int):
        self.expected_generation = expected_generation
        self.observed_generation = observed_generation
        super().__init__(
            f"Store generation advanced from {expected_generation} "
            f"to {observed_generation}"
        )
