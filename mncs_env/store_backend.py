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
import hashlib
import heapq
import json
import os
import sys
import threading
from collections import OrderedDict
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import claims as claims_module
from . import resources as resources_module
from . import workspace as workspace_module

SCHEMA_EVENT = b"mncs.environment.session-event/1"
SCHEMA_SNAPSHOT = b"mncs.environment.session-snapshot/1"
SCHEMA_SNAPSHOT_SECTION = b"mncs.environment.session-snapshot-section/1"
SCHEMA_CLAIM = b"mncs.environment.workspace-claim/1"
SCHEMA_PROJECTION = b"mncs.environment.projection-state/1"
SCHEMA_PROJECTION_EVIDENCE = b"mncs.environment.verification-evidence/1"

_SNAPSHOT_SECTIONS_FIELD = "_mncs_snapshot_sections_v1"
_SNAPSHOT_SECTION_MIN_BYTES = 512
_INLINE_SNAPSHOT_KEYS = frozenset({
    "schema_version",
    "session_id",
    "environment_id",
    "environment_digest",
    "consumer_id",
    "consumer_kind",
    "authenticated_principal_id",
    "lifecycle",
    "lifecycle_history",
    "checkpoints",
    "handoffs",
    "accepted_handoffs",
    "revalidation_receipts",
    "rpc_read_activity_count",
    "rpc_read_activity_limit_reached",
    "rpc_read_activity_request_index",
    "completion_pending",
    "completion",
    "termination_pending",
    "snapshot_sequence",
    "created_at",
    "updated_at",
    "last_store_generation",
    _SNAPSHOT_SECTIONS_FIELD,
})
_NESTED_SNAPSHOT_SUBSECTIONS: tuple[tuple[str, str], ...] = (
    ("workspace", "repositories"),
    ("campaign", "repository_refs"),
)

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


def _snapshot_section_identity(sha256_hex: str) -> bytes:
    return f"snap-sec:{sha256_hex}".encode("utf-8")


def _decompose_snapshot(
    snapshot: dict[str, Any],
) -> tuple[bytes, dict[bytes, bytes]]:
    """Split a session snapshot into a compact envelope and content-addressed sections.

    Large top-level fields (and large nested repository/checkout tables inside
    ``workspace`` and ``campaign``) are externalized by their canonical UTF-8
    JSON SHA-256 digest when they reach ``_SNAPSHOT_SECTION_MIN_BYTES``. Small
    snapshots without qualifying sections remain identical to the legacy
    single-object encoding.
    """
    envelope: dict[str, Any] = {}
    sections_meta: dict[str, dict[str, Any]] = {}
    section_blobs: dict[bytes, bytes] = {}
    nested_parents = {parent: subkey for parent, subkey in _NESTED_SNAPSHOT_SUBSECTIONS}

    for key, value in snapshot.items():
        if key in _INLINE_SNAPSHOT_KEYS:
            envelope[key] = value
            continue
        subkey = nested_parents.get(key)
        if subkey is not None and isinstance(value, dict) and subkey in value:
            sub_val = value[subkey]
            sub_bytes = json.dumps(
                sub_val, ensure_ascii=False, sort_keys=True
            ).encode("utf-8")
            if len(sub_bytes) >= _SNAPSHOT_SECTION_MIN_BYTES:
                sub_sha = hashlib.sha256(sub_bytes).hexdigest()
                sub_ident = _snapshot_section_identity(sub_sha)
                sections_meta[f"{key}.{subkey}"] = {
                    "sha256": sub_sha,
                    "size": len(sub_bytes),
                }
                section_blobs[sub_ident] = sub_bytes
                value = {k: v for k, v in value.items() if k != subkey}

        sec_bytes = json.dumps(
            value, ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
        if len(sec_bytes) >= _SNAPSHOT_SECTION_MIN_BYTES:
            sec_sha = hashlib.sha256(sec_bytes).hexdigest()
            sec_ident = _snapshot_section_identity(sec_sha)
            sections_meta[key] = {
                "sha256": sec_sha,
                "size": len(sec_bytes),
            }
            section_blobs[sec_ident] = sec_bytes
        else:
            envelope[key] = value

    if sections_meta:
        envelope[_SNAPSHOT_SECTIONS_FIELD] = sections_meta
    envelope_bytes = json.dumps(
        envelope, ensure_ascii=False, sort_keys=True
    ).encode("utf-8")
    return envelope_bytes, section_blobs


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


_BINDING_DIRECTORY_MAX_GENERATIONS = 2
_EVENT_DIRECT_GET_MAX = 4
_CLAIM_PAYLOAD_CACHE_MAX = 65536
_SCOPE_GRANT_SESSION_CACHE_MAX = 1024


def _decode_utf8(raw: bytes) -> str | None:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _index_snapshot_binding(
    identity: bytes,
    session_ids: set[str],
    snapshot_map: dict[str, list[tuple[int, bytes]]],
    checkpoints_map: dict[str, list[bytes]],
) -> None:
    if b":snap:" in identity:
        text = _decode_utf8(identity)
        if text and ":snap:" in text:
            session_ids.add(text.split(":snap:", 1)[0])
        prefix_bytes, _, suffix = identity.partition(b":snap:")
        sid = _decode_utf8(prefix_bytes)
        if sid is not None:
            try:
                revision = int(suffix)
            except ValueError:
                revision = None
            if revision is not None:
                snapshot_map.setdefault(sid, []).append((revision, identity))
    if b":chk:" in identity:
        prefix_bytes, _, _ = identity.partition(b":chk:")
        sid = _decode_utf8(prefix_bytes)
        if sid is not None:
            checkpoints_map.setdefault(sid, []).append(identity)


def _index_event_binding(
    identity: bytes, events_map: dict[str, list[tuple[int, bytes, bool]]],
) -> None:
    if b":evt:" not in identity:
        return
    prefix_bytes, _, suffix = identity.partition(b":evt:")
    sid = _decode_utf8(prefix_bytes)
    if sid is None:
        return
    try:
        sequence = int(suffix)
    except ValueError:
        return
    is_canonical = (
        len(suffix) >= 10
        and suffix.isdigit()
        and sequence >= 1
        and suffix == f"{sequence:010d}".encode("ascii")
    )
    events_map.setdefault(sid, []).append((sequence, identity, is_canonical))


def _index_claim_binding(
    identity: bytes,
    claims_latest: dict[str, tuple[int, bytes]],
    claims_all: set[bytes],
    claims_noncanonical: list[bytes],
) -> None:
    claims_all.add(identity)
    if not identity.startswith(b"claim:"):
        claims_noncanonical.append(identity)
        return
    claim_bytes, sep, suffix = identity[6:].rpartition(b":")
    if not sep or len(suffix) != 10 or not suffix.isdigit():
        claims_noncanonical.append(identity)
        return
    version = int(suffix)
    claim_id = _decode_utf8(claim_bytes)
    if version < 1 or claim_id is None:
        claims_noncanonical.append(identity)
        return
    current = claims_latest.get(claim_id)
    if current is None or version > current[0]:
        claims_latest[claim_id] = (version, identity)


def _index_projection_binding(
    identity: bytes,
    projections_map: dict[str, list[tuple[int, bytes]]],
    projection_versions: dict[str, int],
) -> None:
    text = _decode_utf8(identity)
    if text is None:
        return
    projection_id, _, ver_text = text.rpartition(":")
    if not projection_id:
        return
    try:
        number = int(ver_text)
    except ValueError:
        return
    projections_map.setdefault(projection_id, []).append((number, identity))
    if number > projection_versions.get(projection_id, 0):
        projection_versions[projection_id] = number


@dataclass(frozen=True)
class _BindingDirectory:
    """Derived binding metadata index for one exact Store generation."""

    generation: int
    session_ids: tuple[str, ...]
    snapshot_revisions: dict[str, tuple[tuple[int, bytes], ...]]
    checkpoints: dict[str, tuple[bytes, ...]]
    events: dict[str, tuple[tuple[int, bytes, bool], ...]]
    claims_latest: dict[str, tuple[int, bytes]]
    claims_all: tuple[bytes, ...]
    claims_noncanonical: tuple[bytes, ...]
    projections: dict[str, tuple[tuple[int, bytes], ...]]
    projection_versions: dict[str, int]


def _build_binding_directory(
    generation: int, bindings: tuple[tuple[bytes, bytes], ...] | list[tuple[bytes, bytes]],
) -> _BindingDirectory:
    """Index immutable binding identities once for a fixed Store generation."""
    session_ids: set[str] = set()
    snapshot_map: dict[str, list[tuple[int, bytes]]] = {}
    checkpoints_map: dict[str, list[bytes]] = {}
    events_map: dict[str, list[tuple[int, bytes, bool]]] = {}
    claims_latest: dict[str, tuple[int, bytes]] = {}
    claims_all: set[bytes] = set()
    claims_noncanonical: list[bytes] = []
    projections_map: dict[str, list[tuple[int, bytes]]] = {}
    projection_versions: dict[str, int] = {}

    for schema, identity in bindings:
        if schema == SCHEMA_SNAPSHOT:
            _index_snapshot_binding(identity, session_ids, snapshot_map, checkpoints_map)
        elif schema == SCHEMA_EVENT:
            _index_event_binding(identity, events_map)
        elif schema == SCHEMA_CLAIM:
            _index_claim_binding(identity, claims_latest, claims_all, claims_noncanonical)
        elif schema == SCHEMA_PROJECTION:
            _index_projection_binding(identity, projections_map, projection_versions)

    return _BindingDirectory(
        generation=generation,
        session_ids=tuple(sorted(session_ids)),
        snapshot_revisions={
            sid: tuple(sorted(
                set(revs),
                key=lambda pair: (pair[0], pair[1] == _snapshot_identity(sid, pair[0]), pair[1]),
                reverse=True,
            ))
            for sid, revs in snapshot_map.items()
        },
        checkpoints={
            sid: tuple(sorted(items))
            for sid, items in checkpoints_map.items()
        },
        events={
            sid: tuple(sorted(items, key=lambda entry: (entry[0], entry[1])))
            for sid, items in events_map.items()
        },
        claims_latest=claims_latest,
        claims_all=tuple(sorted(claims_all)),
        claims_noncanonical=tuple(sorted(set(claims_noncanonical))),
        projections={
            proj_id: tuple(sorted(items, reverse=True))
            for proj_id, items in projections_map.items()
        },
        projection_versions=projection_versions,
    )


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
        self._binding_cache_lock = threading.RLock()
        self._binding_cache_store = None
        self._binding_directories: OrderedDict[int, _BindingDirectory] = OrderedDict()
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
        self._clear_binding_directory()
        self._store.close()
        if self._promotion_owner is not None:
            self._promotion_owner.close()

    def _cache_lock(self) -> threading.RLock:
        lock = getattr(self, "_binding_cache_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._binding_cache_lock = lock
        return lock

    def _clear_binding_directory(self) -> None:
        lock = self._cache_lock()
        with lock:
            cache = getattr(self, "_binding_directories", None)
            if cache is not None:
                cache.clear()
            self._binding_cache_store = None
            claim_cache = getattr(self, "_claim_payload_cache", None)
            if claim_cache is not None:
                claim_cache.clear()
            self._claim_cache_store = None
            self._claim_cache_gen = -1
            scope_cache = getattr(self, "_scope_grant_event_cache", None)
            if scope_cache is not None:
                scope_cache.clear()
            self._scope_grant_cache_store = None
            self._scope_grant_cache_gen = -1

    def _binding_directory_for(self, generation: Any) -> _BindingDirectory | None:
        if type(generation) is not int:
            return None
        observe = getattr(self._store, "domain_bindings_at", None)
        if not callable(observe):
            return None
        # Support StoreBackend.__new__ in unit tests that bypass __init__.
        lock = getattr(self, "_binding_cache_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._binding_cache_lock = lock
        cache = getattr(self, "_binding_directories", None)
        if cache is None:
            cache = OrderedDict()
            self._binding_directories = cache
        store = self._store
        with lock:
            if getattr(self, "_binding_cache_store", None) is not store:
                cache.clear()
                self._binding_cache_store = store
            cached = cache.get(generation)
            if cached is not None:
                cache.move_to_end(generation)
                return cached
        bindings = observe(generation)
        directory = _build_binding_directory(generation, bindings)
        with lock:
            if self._store is store and getattr(self, "_binding_cache_store", None) is store:
                cache[generation] = directory
                cache.move_to_end(generation)
                while len(cache) > _BINDING_DIRECTORY_MAX_GENERATIONS:
                    cache.popitem(last=False)
        return directory

    def _is_not_current_store_error(self, error: Exception) -> bool:
        api = getattr(self, "_api", None)
        if api is not None and len(api) >= 3 and api[1] is not None and api[2] is not None:
            _, StoreError, StoreResultCode = api
            return (
                isinstance(error, StoreError)
                and error.code == StoreResultCode.DENIED
                and error.message == "Store object binding is not current"
            )
        code = getattr(error, "code", None)
        return (
            getattr(code, "name", None) == "DENIED"
            and getattr(error, "message", None) == "Store object binding is not current"
        )

    def _read_exact_or_find(self, schema: bytes, identity: bytes) -> Any:
        get_bound = getattr(self._store, "get_bound_object", None)
        if callable(get_bound):
            try:
                return get_bound(schema, identity)
            except Exception as error:
                if self._is_not_current_store_error(error):
                    return None
                raise
        return next(
            (
                candidate
                for candidate in self._store.find_bound_objects(schema, identity)
                if candidate.domain_identity == identity
            ),
            None,
        )

    def _read_bound_objects_batch(
        self,
        schema: bytes,
        identities: list[bytes] | tuple[bytes, ...],
        *,
        allow_missing: bool = False,
    ) -> list[Any]:
        if not identities:
            return []
        if len(identities) == 1:
            if allow_missing:
                return [self._read_exact_or_find(schema, identities[0])]
            return [self._store.get_bound_object(schema, identities[0])]
        batch_getter = getattr(self._store, "get_bound_objects", None)
        if callable(batch_getter):
            try:
                return list(batch_getter([(schema, identity) for identity in identities]))
            except Exception as error:
                if allow_missing and self._is_not_current_store_error(error):
                    return [self._read_exact_or_find(schema, ident) for ident in identities]
                raise
        if allow_missing:
            return [self._read_exact_or_find(schema, ident) for ident in identities]
        return [self._store.get_bound_object(schema, ident) for ident in identities]

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
        self._clear_binding_directory()

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
        generation = getattr(self._store, "current_generation", None)
        directory = self._binding_directory_for(generation)
        found: list[tuple[int, dict[str, Any]]] = []
        if directory is not None:
            entries = directory.events.get(session_id, ())
            if not entries:
                return []
            if (
                len(entries) == 1
                or len(entries) <= _EVENT_DIRECT_GET_MAX
                or callable(getattr(self._store, "get_bound_objects", None))
                or not callable(getattr(self._store, "find_bound_objects", None))
            ):
                items = self._read_bound_objects_batch(
                    SCHEMA_EVENT, [identity for _, identity, _ in entries], allow_missing=True,
                )
                items_with_seq = (
                    (entry[0], item)
                    for entry, item in zip(entries, items, strict=True)
                )
            else:
                items_with_seq = (
                    (None, item)
                    for item in self._store.find_bound_objects(SCHEMA_EVENT, prefix)
                )
        else:
            items_with_seq = (
                (None, item)
                for item in self._store.find_bound_objects(SCHEMA_EVENT, prefix)
            )
        for sequence, item in items_with_seq:
            if item is None:
                continue
            try:
                parsed_seq = (
                    int(item.domain_identity[len(prefix):])
                    if sequence is None
                    else sequence
                )
                found.append((parsed_seq, json.loads(item.payload.decode("utf-8"))))
            except (AttributeError, ValueError, json.JSONDecodeError):
                continue
        found.sort(key=lambda pair: pair[0])
        return [event for _, event in found]

    def read_scope_grant_events(self, session_id: str) -> list[dict[str, Any]]:
        """Read immutable authority.scope-granted events for one session incrementally."""
        generation = getattr(self._store, "current_generation", None)
        directory = self._binding_directory_for(generation)
        if directory is None or not callable(getattr(self._store, "get_bound_objects", None)):
            return [
                event for event in self.read_events(session_id)
                if isinstance(event, dict) and event.get("type") == "authority.scope-granted"
            ]
        entries = directory.events.get(session_id, ())
        if not entries:
            return []
        store = self._store
        lock = self._cache_lock()
        with lock:
            if (
                getattr(self, "_scope_grant_cache_store", None) is not store
                or type(generation) is not int
                or generation < getattr(self, "_scope_grant_cache_gen", -1)
            ):
                cache = getattr(self, "_scope_grant_event_cache", None)
                if cache is not None:
                    cache.clear()
                self._scope_grant_cache_store = store
                self._scope_grant_cache_gen = -1
            cache = getattr(self, "_scope_grant_event_cache", None)
            if cache is None:
                cache = OrderedDict()
                self._scope_grant_event_cache = cache
            cached = cache.get(session_id)
            if cached is not None:
                cached_entries, grant_jsons = cached
                if cached_entries == entries:
                    cache.move_to_end(session_id)
                    return [json.loads(raw) for raw in grant_jsons]
                if (
                    len(entries) > len(cached_entries)
                    and entries[:len(cached_entries)] == cached_entries
                ):
                    delta = entries[len(cached_entries):]
                    base_jsons = grant_jsons
                else:
                    delta = entries
                    base_jsons = ()
            else:
                delta = entries
                base_jsons = ()

        items = self._read_bound_objects_batch(
            SCHEMA_EVENT, [identity for _, identity, _ in delta], allow_missing=True,
        )
        new_jsons: list[str] = []
        all_present = True
        for item in items:
            if item is None:
                all_present = False
                continue
            try:
                raw_text = item.payload.decode("utf-8")
                event = json.loads(raw_text)
            except (AttributeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(event, dict) and event.get("type") == "authority.scope-granted":
                new_jsons.append(raw_text)

        combined = (*base_jsons, *new_jsons)
        if all_present and type(generation) is int:
            with lock:
                if (
                    self._store is store
                    and getattr(self, "_scope_grant_cache_store", None) is store
                    and generation >= getattr(self, "_scope_grant_cache_gen", -1)
                ):
                    self._scope_grant_cache_gen = generation
                    cache[session_id] = (entries, combined)
                    cache.move_to_end(session_id)
                    while len(cache) > _SCOPE_GRANT_SESSION_CACHE_MAX:
                        cache.popitem(last=False)
        return [json.loads(raw) for raw in combined]

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
        directory = self._binding_directory_for(generation)
        selected: list[tuple[int, bytes]] = []
        event_count = 0
        minimum_sequence: int | None = None
        if directory is not None:
            for sequence, identity, is_canonical in directory.events.get(session_id, ()):
                if not is_canonical:
                    continue
                event_count += 1
                if minimum_sequence is None or sequence < minimum_sequence:
                    minimum_sequence = sequence
                if before_sequence is not None and sequence >= before_sequence:
                    continue
                item = (sequence, identity)
                if len(selected) < limit:
                    heapq.heappush(selected, item)
                elif sequence > selected[0][0]:
                    heapq.heapreplace(selected, item)
        else:
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
        ordered_selected = sorted(selected)
        if ordered_selected:
            items = self._read_bound_objects_batch(
                SCHEMA_EVENT, [identity for _, identity in ordered_selected],
            )
            for (sequence, _identity), item in zip(ordered_selected, items, strict=True):
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

    def _has_binding_at(
        self, generation: int, schema: bytes, identity: bytes
    ) -> bool:
        committed = getattr(self, "_committed_snapshot_sections", None)
        if schema == SCHEMA_SNAPSHOT_SECTION and isinstance(committed, set):
            if (
                getattr(self, "_committed_sections_store_id", None)
                != id(self._store)
                or generation
                < getattr(self, "_committed_sections_generation", 0)
            ):
                committed.clear()
                self._committed_sections_store_id = id(self._store)
                self._committed_sections_generation = generation
            elif identity in committed:
                return True
        found = False
        entry_lookup = getattr(self._store, "_entry_by_binding", None)
        binding_id_fn = getattr(self._store, "_binding_id", None)
        if callable(entry_lookup) and callable(binding_id_fn):
            found = (
                entry_lookup(generation, binding_id_fn(schema, identity))
                is not None
            )
        else:
            observe = getattr(self._store, "domain_bindings_at", None)
            if callable(observe):
                found = (schema, identity) in set(observe(generation))
        if found and schema == SCHEMA_SNAPSHOT_SECTION:
            if not isinstance(committed, set):
                committed = set()
                self._committed_snapshot_sections = committed
            self._committed_sections_store_id = id(self._store)
            self._committed_sections_generation = generation
            committed.add(identity)
        return found

    def _read_snapshot_section_payload(self, sec_identity: bytes) -> bytes:
        _, StoreError, StoreResultCode = getattr(
            self, "_api", (None, Exception, None)
        )
        get_bound = getattr(self._store, "get_bound_object", None)
        if callable(get_bound):
            try:
                item = get_bound(SCHEMA_SNAPSHOT_SECTION, sec_identity)
            except StoreError as error:
                if (
                    StoreResultCode is not None
                    and getattr(error, "code", None)
                    in (
                        StoreResultCode.DENIED,
                        StoreResultCode.INTEGRITY_FAILURE,
                    )
                ):
                    raise StoreIntegrityFailure(
                        f"snapshot section {sec_identity.decode('utf-8', 'replace')} "
                        f"is missing or failed verification: {error}"
                    ) from error
                raise
            return bytes(item.payload)

        find_bound = getattr(self._store, "find_bound_objects", None)
        if callable(find_bound):
            item = next(
                (
                    candidate
                    for candidate in find_bound(
                        SCHEMA_SNAPSHOT_SECTION, sec_identity
                    )
                    if candidate.domain_identity == sec_identity
                ),
                None,
            )
            if item is not None:
                return bytes(item.payload)
        raise StoreIntegrityFailure(
            f"snapshot section {sec_identity.decode('utf-8', 'replace')} is missing"
        )

    def _read_snapshot_section_payloads(
        self, identities: list[bytes],
    ) -> dict[bytes, bytes]:
        if not identities:
            return {}
        unique = list(dict.fromkeys(identities))
        if len(unique) == 1 or not callable(getattr(self._store, "get_bound_objects", None)):
            return {
                sec_identity: self._read_snapshot_section_payload(sec_identity)
                for sec_identity in unique
            }
        _, StoreError, StoreResultCode = getattr(
            self, "_api", (None, Exception, None)
        )
        try:
            items = self._store.get_bound_objects(
                [(SCHEMA_SNAPSHOT_SECTION, sec_identity) for sec_identity in unique]
            )
        except StoreError as error:
            if (
                StoreResultCode is not None
                and getattr(error, "code", None)
                in (
                    StoreResultCode.DENIED,
                    StoreResultCode.INTEGRITY_FAILURE,
                )
            ):
                raise StoreIntegrityFailure(
                    f"snapshot section batch is missing or failed verification: {error}"
                ) from error
            raise
        return {
            sec_identity: bytes(item.payload)
            for sec_identity, item in zip(unique, items, strict=True)
        }

    def _reconstruct_sectioned_snapshot(
        self, record: dict[str, Any]
    ) -> dict[str, Any]:
        reconstructed = dict(record)
        sections = reconstructed.pop(_SNAPSHOT_SECTIONS_FIELD, None)
        if not isinstance(sections, dict):
            raise StoreIntegrityFailure(
                "snapshot section manifest is not a JSON object"
            )
        top_items: list[tuple[str, str, int, bytes]] = []
        nested_items: list[tuple[str, str, str, int, bytes]] = []
        section_identities: list[bytes] = []
        for sec_key, meta in sections.items():
            if not isinstance(sec_key, str) or not isinstance(meta, dict):
                raise StoreIntegrityFailure(
                    "snapshot section entry is malformed"
                )
            sha256_hex = meta.get("sha256")
            expected_size = meta.get("size")
            if (
                not isinstance(sha256_hex, str)
                or len(sha256_hex) != 64
                or type(expected_size) is not int
                or expected_size < 0
            ):
                raise StoreIntegrityFailure(
                    f"snapshot section {sec_key!r} has invalid metadata"
                )
            sec_identity = _snapshot_section_identity(sha256_hex)
            section_identities.append(sec_identity)
            if "." in sec_key:
                parent, _, subkey = sec_key.partition(".")
                nested_items.append((parent, subkey, sha256_hex, expected_size, sec_identity))
            else:
                top_items.append((sec_key, sha256_hex, expected_size, sec_identity))

        payloads = self._read_snapshot_section_payloads(section_identities)
        for sec_key, sha256_hex, expected_size, sec_identity in top_items:
            reconstructed[sec_key] = self._verify_and_parse_section(
                sec_key, payloads[sec_identity], sha256_hex, expected_size,
            )

        for parent, subkey, sha256_hex, expected_size, sec_identity in nested_items:
            parent_val = reconstructed.get(parent)
            if not isinstance(parent_val, dict):
                raise StoreIntegrityFailure(
                    f"snapshot section parent {parent!r} is not a JSON object"
                )
            updated_parent = dict(parent_val)
            updated_parent[subkey] = self._verify_and_parse_section(
                f"{parent}.{subkey}", payloads[sec_identity], sha256_hex, expected_size,
            )
            reconstructed[parent] = updated_parent
        return reconstructed

    @staticmethod
    def _verify_and_parse_section(
        sec_key: str, payload: bytes, sha256_hex: str, expected_size: int,
    ) -> Any:
        if (
            len(payload) != expected_size
            or hashlib.sha256(payload).hexdigest() != sha256_hex
        ):
            raise StoreIntegrityFailure(
                f"snapshot section {sec_key!r} failed content digest verification"
            )
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise StoreIntegrityFailure(
                f"snapshot section {sec_key!r} is not valid UTF-8 JSON"
            ) from error

    def put_snapshot(self, session_id: str, revision: int, snapshot: dict[str, Any]):
        envelope_bytes, section_blobs = _decompose_snapshot(snapshot)
        snap_identity = _snapshot_identity(session_id, revision)
        _, StoreError, StoreResultCode = self._api
        store_obj = getattr(self, "_store", None)
        if not section_blobs or store_obj is None or not hasattr(store_obj, "put_bound_objects"):
            try:
                return self._put_immutable(
                    schema=SCHEMA_SNAPSHOT,
                    identity=snap_identity,
                    payload=envelope_bytes,
                )
            except StoreError as error:
                if error.code == StoreResultCode.IDENTITY_CONFLICT:
                    from .session_store import SnapshotConflict
                    raise SnapshotConflict(session_id, revision) from error
                raise

        self._ensure_writable()
        from mncs_store import BoundObjectInput  # noqa: E402

        snap_descriptor = json.dumps(
            {
                "schema": SCHEMA_SNAPSHOT.decode(),
                "identity": snap_identity.decode(),
            },
            sort_keys=True,
        ).encode()
        last_error: Exception | None = None
        for _ in range(MAX_CAS_RETRIES):
            generation = self._store.current_generation
            missing_sections = [
                (sec_ident, sec_bytes)
                for sec_ident, sec_bytes in section_blobs.items()
                if not self._has_binding_at(
                    generation, SCHEMA_SNAPSHOT_SECTION, sec_ident
                )
            ]
            if not missing_sections or self._has_binding_at(
                generation, SCHEMA_SNAPSHOT, snap_identity
            ):
                try:
                    return self._put_immutable(
                        schema=SCHEMA_SNAPSHOT,
                        identity=snap_identity,
                        payload=envelope_bytes,
                    )
                except StoreError as error:
                    if error.code == StoreResultCode.IDENTITY_CONFLICT:
                        from .session_store import SnapshotConflict
                        raise SnapshotConflict(session_id, revision) from error
                    raise

            batch = [
                BoundObjectInput(
                    SCHEMA_SNAPSHOT_SECTION,
                    sec_ident,
                    json.dumps(
                        {
                            "schema": SCHEMA_SNAPSHOT_SECTION.decode(),
                            "identity": sec_ident.decode(),
                        },
                        sort_keys=True,
                    ).encode(),
                    sec_bytes,
                )
                for sec_ident, sec_bytes in missing_sections
            ]
            batch.append(
                BoundObjectInput(
                    SCHEMA_SNAPSHOT,
                    snap_identity,
                    snap_descriptor,
                    envelope_bytes,
                )
            )
            try:
                result = self._store.put_bound_objects(
                    batch, expected_generation=generation
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
                    from .session_store import SnapshotConflict
                    raise SnapshotConflict(session_id, revision) from error
                last_error = error
                continue
            if result.code == StoreResultCode.STALE_GENERATION:
                last_error = StoreError(
                    result.code, "generation advanced; retrying"
                )
                continue
            if result.code == StoreResultCode.IDENTITY_CONFLICT:
                from .session_store import SnapshotConflict
                raise SnapshotConflict(session_id, revision)
            if not result.committed:
                raise StoreError(
                    result.code, "session snapshot publication failed"
                )
            committed = getattr(self, "_committed_snapshot_sections", None)
            if not isinstance(committed, set):
                committed = set()
                self._committed_snapshot_sections = committed
            self._committed_sections_store_id = id(self._store)
            self._committed_sections_generation = getattr(
                result, "generation", generation
            )
            for sec_ident, _ in missing_sections:
                committed.add(sec_ident)
            return result
        raise last_error or StoreError(
            StoreResultCode.CONFLICT, "CAS retries exhausted"
        )

    def _snapshot_revisions_for(
        self, session_id: str, generation: Any,
    ) -> tuple[tuple[int, bytes], ...]:
        directory = self._binding_directory_for(generation)
        if directory is not None:
            return directory.snapshot_revisions.get(session_id, ())
        prefix = f"{session_id}:snap:".encode()
        revisions: list[tuple[int, bytes]] = []
        for schema, identity in self._store.domain_bindings_at(generation):
            if schema != SCHEMA_SNAPSHOT or not identity.startswith(prefix):
                continue
            try:
                revisions.append((int(identity[len(prefix):]), identity))
            except ValueError:
                continue
        return tuple(sorted(set(revisions), reverse=True))

    def read_snapshot(self, session_id: str) -> dict[str, Any] | None:
        # Select the latest immutable revision from verified binding metadata
        # (no payload reads), then verify and decode only that payload. Older
        # revisions are read only if a newer payload is missing or undecodable.
        # Payload integrity failures still raise; they are never skipped.
        for attempt in range(2):
            generation = getattr(self._store, "current_generation", None)
            revisions = self._snapshot_revisions_for(session_id, generation)
            for _revision, identity in revisions:
                item = self._read_exact_or_find(SCHEMA_SNAPSHOT, identity)
                if item is None:
                    continue
                try:
                    record = json.loads(item.payload.decode("utf-8"))
                except (ValueError, json.JSONDecodeError):
                    continue
                if not isinstance(record, dict):
                    continue
                if (
                    attempt == 0
                    and type(generation) is int
                    and getattr(self._store, "current_generation", None) != generation
                ):
                    break
                if _SNAPSHOT_SECTIONS_FIELD in record:
                    return self._reconstruct_sectioned_snapshot(record)
                return record
            else:
                if (
                    attempt == 0
                    and type(generation) is int
                    and getattr(self._store, "current_generation", None) != generation
                ):
                    continue
                return None
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

    def _read_claims_for_identities(
        self, identities: tuple[bytes, ...], generation: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        if not identities:
            return [], False
        store = self._store
        lock = self._cache_lock()
        with lock:
            if (
                getattr(self, "_claim_cache_store", None) is not store
                or generation < getattr(self, "_claim_cache_gen", -1)
            ):
                cache = getattr(self, "_claim_payload_cache", None)
                if cache is not None:
                    cache.clear()
                self._claim_cache_store = store
                self._claim_cache_gen = -1
            cache = getattr(self, "_claim_payload_cache", None)
            if cache is None:
                cache = OrderedDict()
                self._claim_payload_cache = cache
            missing = [ident for ident in identities if ident not in cache]

        fetched: dict[bytes, str | None] = {}
        if missing:
            items = self._read_bound_objects_batch(SCHEMA_CLAIM, missing, allow_missing=True)
            for ident, item in zip(missing, items, strict=True):
                if item is None:
                    continue
                raw_text = item.payload.decode("utf-8")
                try:
                    json.loads(raw_text)
                except json.JSONDecodeError:
                    fetched[ident] = None
                else:
                    fetched[ident] = raw_text
            with lock:
                if (
                    self._store is store
                    and getattr(self, "_claim_cache_store", None) is store
                    and generation >= getattr(self, "_claim_cache_gen", -1)
                ):
                    self._claim_cache_gen = generation
                    for ident, raw_text in fetched.items():
                        cache[ident] = raw_text
                        cache.move_to_end(ident)
                    while len(cache) > _CLAIM_PAYLOAD_CACHE_MAX:
                        cache.popitem(last=False)
        else:
            with lock:
                if (
                    self._store is store
                    and getattr(self, "_claim_cache_store", None) is store
                    and generation >= getattr(self, "_claim_cache_gen", -1)
                ):
                    self._claim_cache_gen = generation

        out: list[dict[str, Any]] = []
        had_invalid = False
        with lock:
            use_cache = (
                self._store is store
                and getattr(self, "_claim_cache_store", None) is store
            )
            for ident in identities:
                if ident in fetched:
                    raw_text = fetched[ident]
                elif use_cache and ident in cache:
                    raw_text = cache[ident]
                    cache.move_to_end(ident)
                else:
                    continue
                if raw_text is None:
                    had_invalid = True
                    continue
                out.append(json.loads(raw_text))
        out.sort(key=lambda record: (str(record.get("claim_id", "")), int(record.get("version", 0))))
        return out, had_invalid

    def read_claims(self) -> list[dict[str, Any]]:
        generation = getattr(self._store, "current_generation", None)
        directory = self._binding_directory_for(generation)
        if (
            directory is not None
            and type(generation) is int
            and callable(getattr(self._store, "get_bound_objects", None))
        ):
            out, _ = self._read_claims_for_identities(directory.claims_all, generation)
            return out
        out = []
        for item in self._store.find_bound_objects(SCHEMA_CLAIM):
            try:
                out.append(json.loads(item.payload.decode("utf-8")))
            except json.JSONDecodeError:
                continue
        out.sort(key=lambda record: (str(record.get("claim_id", "")), int(record.get("version", 0))))
        return out

    def read_latest_claims(self) -> list[dict[str, Any]]:
        """Read only the latest version of each canonical claim plus non-canonical bindings."""
        generation = getattr(self._store, "current_generation", None)
        directory = self._binding_directory_for(generation)
        if (
            directory is not None
            and type(generation) is int
            and callable(getattr(self._store, "get_bound_objects", None))
        ):
            canonical = [ident for _, ident in sorted(directory.claims_latest.values())]
            identities = tuple(dict.fromkeys([*canonical, *directory.claims_noncanonical]))
            out, had_invalid = self._read_claims_for_identities(identities, generation)
            if not had_invalid:
                return out
            # Fall back to complete claim history only if a latest payload had invalid JSON.
            return self.read_claims()

        latest_by_id: dict[str, tuple[int, dict[str, Any]]] = {}
        unkeyed: list[dict[str, Any]] = []
        for record in self.read_claims():
            if not isinstance(record, dict):
                continue
            claim_id = record.get("claim_id")
            version = record.get("version")
            if isinstance(claim_id, str) and claim_id and type(version) is int:
                current = latest_by_id.get(claim_id)
                if current is None or version > current[0]:
                    latest_by_id[claim_id] = (version, record)
            else:
                unkeyed.append(record)
        out = [pair[1] for pair in latest_by_id.values()]
        out.extend(unkeyed)
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
            directory = self._binding_directory_for(generation)
            if directory is not None:
                latest = {
                    claim_id: directory.claims_latest[claim_id]
                    for claim_id in requested
                    if claim_id in directory.claims_latest
                }
            else:
                prefixes = {
                    f"claim:{claim_id}:".encode(): claim_id
                    for claim_id in requested
                }
                latest = {}
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
            selected_entries = [
                (claim_id, *latest[claim_id])
                for claim_id in requested
                if claim_id in latest
            ]
            if (
                len(selected_entries) > 1
                and callable(getattr(self._store, "get_bound_objects", None))
            ):
                _, StoreError, StoreResultCode = getattr(self, "_api", None) or _load_store_api()
                try:
                    batch_items = self._store.get_bound_objects(
                        [(SCHEMA_CLAIM, ident) for _, _, ident in selected_entries]
                    )
                except StoreError as error:
                    if (error.code == StoreResultCode.DENIED
                            and error.message == "Store object binding is not current"):
                        raise StoreIntegrityFailure(
                            "latest claim payload does not match its Store identity"
                        ) from error
                    if error.code == StoreResultCode.INTEGRITY_FAILURE:
                        raise StoreIntegrityFailure(str(error)) from error
                    raise
                for (claim_id, version, _identity), item in zip(
                    selected_entries, batch_items, strict=True,
                ):
                    try:
                        record = json.loads(item.payload.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as error:
                        raise StoreIntegrityFailure(
                            "Store record payload is not valid UTF-8 JSON"
                        ) from error
                    if (not isinstance(record, dict)
                            or record.get("claim_id") != claim_id
                            or type(record.get("version")) is not int
                            or record["version"] != version):
                        raise StoreIntegrityFailure(
                            "latest claim payload does not match its Store identity"
                        )
                    out.append(record)
            else:
                for claim_id, version, identity in selected_entries:
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
        api = getattr(self, "_api", None)
        if api is not None:
            StoreError = api[1]
        else:
            try:
                from mncs_store.errors import StoreError  # noqa: E402
            except ImportError:
                StoreError = ()  # type: ignore[assignment]

        try:
            # Checkpoints and receipts use exact identities. Route them
            # through Store's deterministic binding lookup so projection
            # reads do not enumerate every binding to find one object.
            item = self._store.get_bound_object(schema, identity)
        except StoreError:
            return None
        except Exception as error:
            if type(error).__name__ in {"StoreError", "StoreIntegrityError"} or self._is_not_current_store_error(error):
                return None
            raise
        if item is None:
            return None
        try:
            record = json.loads(item.payload.decode("utf-8"))
        except json.JSONDecodeError:
            return None
        return record if isinstance(record, dict) else None

    def get_record_strict(self, schema: bytes, identity: bytes) -> dict[str, Any] | None:
        """Read one bound record, preserving integrity and provider failures."""
        _, StoreError, StoreResultCode = getattr(self, "_api", None) or _load_store_api()
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
        directory = self._binding_directory_for(generation)
        if directory is not None:
            identities = list(directory.checkpoints.get(session_id, ()))
        else:
            identities = sorted(
                identity for schema, identity in self._store.domain_bindings_at(generation)
                if schema == SCHEMA_SNAPSHOT and identity.startswith(prefix)
            )
        records = []
        if len(identities) > 1 and callable(getattr(self._store, "get_bound_objects", None)):
            try:
                items = self._read_bound_objects_batch(SCHEMA_SNAPSHOT, identities, allow_missing=True)
            except Exception as error:
                if type(error).__name__ in {"StoreError", "StoreIntegrityError"}:
                    items = None
                else:
                    raise
            if items is not None:
                for item in items:
                    if item is None:
                        continue
                    try:
                        record = json.loads(item.payload.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if (isinstance(record, dict) and record.get("session_id") == session_id
                            and record.get("identity")):
                        records.append(record)
                return sorted(records, key=lambda record: (int(record.get("sequence", 0)),
                                                           str(record.get("identity", ""))))
        for identity in identities:
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
        generation = getattr(self._store, "current_generation", None)
        directory = self._binding_directory_for(generation)
        if directory is not None and callable(getattr(self._store, "get_bound_object", None)):
            for version, identity in directory.projections.get(projection_id, ()):
                item = self._read_exact_or_find(SCHEMA_PROJECTION, identity)
                if item is None:
                    continue
                try:
                    record = json.loads(item.payload.decode("utf-8"))
                except (ValueError, json.JSONDecodeError):
                    continue
                if isinstance(record, dict):
                    return (version, record)
            return None
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
        generation = getattr(self._store, "current_generation", None)
        directory = self._binding_directory_for(generation)
        if directory is not None:
            return dict(directory.projection_versions)
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

    def generation_inventory(
        self,
        *,
        keep_last: int = 4096,
        protected_generations: Sequence[int] = (),
    ) -> dict[str, Any]:
        # Retention classification stays provider-owned; Environment only
        # forwards the request and reports the result.
        inventory = getattr(self._store, "generation_inventory", None)
        if not callable(inventory):
            raise RuntimeError("selected Store provider does not expose retention inventory")
        if protected_generations:
            return dict(inventory(keep_last=keep_last, protected_generations=tuple(protected_generations)))
        return dict(inventory(keep_last=keep_last))

    def reclaim_generations(
        self,
        *,
        keep_last: int = 4096,
        dry_run: bool = True,
        protected_generations: Sequence[int] = (),
        policy_id: str | None = None,
        reclaim_chunks: bool = False,
        audit_path: Path | None = None,
        mark_path: Path | None = None,
        expected_head: int | None = None,
        expected_plan_digest: str | None = None,
        max_files: int = 8192,
        max_bytes: int = 64 * 1024 * 1024,
        max_seconds: float = 30.0,
    ) -> dict[str, Any]:
        # Reclamation stays provider-owned; Environment never interprets or
        # deletes Store generation files itself.
        reclaim = getattr(self._store, "prune_generations", None)
        if not callable(reclaim):
            raise RuntimeError("selected Store provider does not expose generation reclaim")
        if (
            not protected_generations
            and policy_id is None
            and not reclaim_chunks
            and audit_path is None
            and mark_path is None
            and expected_head is None
            and expected_plan_digest is None
            and max_files == 8192
            and max_bytes == 64 * 1024 * 1024
            and max_seconds == 30.0
        ):
            return dict(reclaim(keep_last=keep_last, dry_run=dry_run))
        return dict(
            reclaim(
                keep_last=keep_last,
                dry_run=dry_run,
                protected_generations=tuple(protected_generations),
                policy_id=policy_id,
                reclaim_chunks=reclaim_chunks,
                audit_path=audit_path,
                mark_path=mark_path,
                expected_head=expected_head,
                expected_plan_digest=expected_plan_digest,
                max_files=max_files,
                max_bytes=max_bytes,
                max_seconds=max_seconds,
            )
        )

    def create_backup(
        self,
        destination: Path,
        *,
        verify_payloads: bool = False,
    ) -> dict[str, Any]:
        backup = getattr(self._store, "create_backup", None)
        if not callable(backup):
            raise RuntimeError("selected Store provider does not expose backup creation")
        return dict(backup(destination, verify_payloads=verify_payloads))

    def verify_backup(
        self,
        backup_dir: Path,
        *,
        verify_payloads: bool = True,
    ) -> dict[str, Any]:
        EmbeddedStore, _, _ = self._api
        verifier = getattr(EmbeddedStore, "verify_backup", None)
        if not callable(verifier):
            raise RuntimeError("selected Store provider does not expose backup verification")
        return dict(verifier(backup_dir, verify_payloads=verify_payloads))

    def restore_backup(
        self,
        backup_dir: Path,
        target_dir: Path,
        *,
        verify_payloads: bool = True,
    ) -> dict[str, Any]:
        EmbeddedStore, _, _ = self._api
        restorer = getattr(EmbeddedStore, "restore_backup", None)
        if not callable(restorer):
            raise RuntimeError("selected Store provider does not expose isolated backup restoration")
        return dict(restorer(backup_dir, target_dir, verify_payloads=verify_payloads))

    def _read_session_header_maps(self) -> list[dict[str, Any]]:
        generation = getattr(self._store, "current_generation", None)
        directory = self._binding_directory_for(generation)
        if (
            directory is not None
            and callable(getattr(self._store, "get_bound_objects", None))
        ):
            latest_identities = [
                revs[0][1]
                for _sid, revs in sorted(directory.snapshot_revisions.items())
                if revs
            ]
            out: list[dict[str, Any]] = []
            for start in range(0, len(latest_identities), 128):
                batch = latest_identities[start : start + 128]
                items = self._read_bound_objects_batch(SCHEMA_SNAPSHOT, batch, allow_missing=True)
                for item in items:
                    if item is None:
                        continue
                    try:
                        parsed = json.loads(item.payload.decode("utf-8"))
                    except (AttributeError, ValueError, json.JSONDecodeError):
                        parsed = None
                    if isinstance(parsed, dict):
                        out.append(parsed)
                    else:
                        out.append({"_corrupt_snapshot": True})
            return out
        return [
            self.read_snapshot(sid) or {"_corrupt_snapshot": True}
            for sid in self.list_sessions()
        ]

    def retention_pins(self) -> dict[str, Any]:
        """Collect mandatory Environment Store generation pins read-only."""
        head = self.generation()
        mandatory: set[int] = set()
        reconciler_cursors: dict[str, int] = {}
        selection_cursors: dict[str, int] = {}
        active_sessions = 0
        handed_off_sessions = 0
        discovery_complete = True
        diagnostics: list[str] = []

        for header in self._read_session_header_maps():
            if header.get("_corrupt_snapshot"):
                discovery_complete = False
                diagnostics.append("unreadable session snapshot header")
                continue
            lifecycle = header.get("lifecycle")
            if lifecycle == "handed_off":
                handed_off_sessions += 1
            elif lifecycle not in ("completed", "failed"):
                active_sessions += 1
            if (
                header.get("consumer_id") == "environment-reconciler"
                and lifecycle not in ("completed", "failed")
                and isinstance(header.get("session_id"), str)
            ):
                full_snap = self.read_snapshot(header["session_id"])
                if not isinstance(full_snap, dict):
                    discovery_complete = False
                    diagnostics.append(f"reconciler snapshot unreadable: {header['session_id']}")
                    continue
                cursor_raw = (
                    (full_snap.get("reconciler") or {}).get("cursors", {}).get("store-replay")
                )
                if cursor_raw is not None:
                    try:
                        cursor_gen = int(cursor_raw)
                        if not 0 <= cursor_gen <= head:
                            raise ValueError("out of range")
                    except (TypeError, ValueError):
                        discovery_complete = False
                        diagnostics.append(f"invalid reconciler store-replay cursor: {cursor_raw!r}")
                    else:
                        mandatory.add(cursor_gen)
                        reconciler_cursors[header["session_id"]] = cursor_gen

        all_latest_claims = self.read_latest_claims()
        active_claims = claims_module.active_claims(all_latest_claims)

        for row_id in sorted(self.read_projection_versions()):
            if not row_id.startswith("entry:index/"):
                continue
            row = self.read_projection_row(row_id)
            if not row:
                continue
            sel = row[1].get("selection")
            gen = sel.get("generation") if isinstance(sel, dict) else None
            if type(gen) is int and 0 <= gen <= head:
                selection_cursors[row_id] = gen

        return {
            "head": head,
            "mandatory_protected_generations": sorted(mandatory),
            "reconciler_replay_cursors": reconciler_cursors,
            "recoverable_selection_cursors": selection_cursors,
            "active_sessions_count": active_sessions,
            "handed_off_sessions_count": handed_off_sessions,
            "active_claims_count": len(active_claims),
            "pin_discovery_complete": discovery_complete,
            "fail_closed": not discovery_complete,
            "diagnostics": diagnostics,
            "external_consumers_note": (
                "Store consumers outside Environment are not discoverable from Environment metadata "
                "and require explicit caller pins or consumer drain before destructive maintenance."
            ),
        }

    def maintenance_readiness(
        self,
        *,
        keep_last: int | None = None,
        protected_generations: Sequence[int] = (),
        reclaim_chunks: bool = False,
        mark_path: Path | None = None,
        max_files: int = 8192,
        max_bytes: int = 64 * 1024 * 1024,
        max_seconds: float = 10.0,
        min_free_disk_bytes: int = 512 * 1024 * 1024,
        min_available_memory_bytes: int = 256 * 1024 * 1024,
    ) -> dict[str, Any]:
        """Assess Store maintenance readiness without performing destructive reclamation."""
        pins_info = self.retention_pins()
        all_pins = sorted(set(pins_info["mandatory_protected_generations"]) | set(protected_generations))
        staging_dir = self.path / "staging"
        pending_staging = staging_dir.is_dir() and any(staging_dir.iterdir())

        system_obs = resources_module.observe([os.getpid()]).get("system", {})
        mem_avail = system_obs.get("memory_available_bytes")
        try:
            fs_stat = os.statvfs(self.path)
            disk_free_bytes = fs_stat.f_bavail * fs_stat.f_frsize
        except OSError:
            disk_free_bytes = None

        blockers: list[str] = []
        if pending_staging:
            blockers.append("pending-staging-recovery")
        if pins_info["fail_closed"]:
            blockers.append("pin-discovery-incomplete")
        if pins_info["active_claims_count"] > 0:
            blockers.append("active-workspace-claims")
        if type(mem_avail) is int and mem_avail < min_available_memory_bytes:
            blockers.append("insufficient-available-memory")
        if type(disk_free_bytes) is int and disk_free_bytes < min_free_disk_bytes:
            blockers.append("insufficient-free-disk-space")

        if keep_last is None:
            inventory = self.generation_inventory(
                keep_last=(1 << 64) - 1,
                protected_generations=all_pins,
            )
            plan = None
            disposition = (
                "deferred-recovery" if pending_staging
                else "deferred-pin-uncertainty" if pins_info["fail_closed"]
                else "deferred-active-claims" if pins_info["active_claims_count"] > 0
                else "deferred-resource-pressure" if (
                    "insufficient-available-memory" in blockers
                    or "insufficient-free-disk-space" in blockers
                )
                else "policy-required"
            )
        else:
            inventory = self.generation_inventory(
                keep_last=keep_last,
                protected_generations=all_pins,
            )
            if blockers:
                plan = None
                disposition = (
                    "deferred-recovery" if pending_staging
                    else "deferred-pin-uncertainty" if pins_info["fail_closed"]
                    else "deferred-active-claims" if pins_info["active_claims_count"] > 0
                    else "deferred-resource-pressure"
                )
            else:
                plan = self.reclaim_generations(
                    keep_last=keep_last,
                    dry_run=True,
                    protected_generations=all_pins,
                    reclaim_chunks=reclaim_chunks,
                    mark_path=mark_path,
                    max_files=max_files,
                    max_bytes=max_bytes,
                    max_seconds=max_seconds,
                )
                disposition = (
                    "dry-run-planned" if plan.get("status") == "complete"
                    else "incomplete-resumable" if plan.get("mark_resumable")
                    else "unknown"
                )

        return {
            "schema_version": "mncs.environment.store-maintenance-readiness/1",
            "autonomous_deletion_enabled": False,
            "dry_run_only": True,
            "policy_required": keep_last is None,
            "keep_last": keep_last,
            "disposition": disposition,
            "blockers": blockers,
            "own_store_handle_open": True,
            "resources": {
                "memory_available_bytes": mem_avail,
                "disk_free_bytes": disk_free_bytes,
                "min_available_memory_bytes": min_available_memory_bytes,
                "min_free_disk_bytes": min_free_disk_bytes,
            },
            "pins": pins_info,
            "protected_generations": all_pins,
            "inventory": inventory,
            "dry_run_plan": plan,
            "activation_prerequisites_unmet": [
                "explicit-retention-window-and-policy-authorization",
                "offline-or-upgraded-consumer-drain",
                "verified-isolated-backup-and-restore-drill",
                *(["resumable-chunk-mark-completion"] if reclaim_chunks and (plan is None or plan.get("status") != "complete") else []),
            ],
        }

    def list_sessions(self) -> list[str]:
        # Session identities come from verified binding metadata; snapshot
        # payloads are never read for a listing.
        generation = getattr(self._store, "current_generation", None)
        directory = self._binding_directory_for(generation)
        if directory is not None:
            return list(directory.session_ids)
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
