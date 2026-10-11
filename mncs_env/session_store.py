"""Session persistence interface with Store and file implementations.

The Store backend is canonical: immutable event/snapshot/claim objects,
generation CAS, and a commit feed. The file backend is an explicit
import/export/debug projection with identical semantics over JSON files;
it must never become a second authority.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .persist import (
    append_jsonl,
    exclusive_file_lock,
    read_json,
    read_jsonl,
    write_json,
)
from .toolchain import selected_stdlib_root


class SequenceTaken(Exception):
    """Another writer committed a different record at this sequence/revision."""


class SnapshotConflict(Exception):
    """A concurrent participant published this immutable snapshot revision."""

    def __init__(self, session_id: str, revision: int):
        self.session_id = session_id
        self.revision = revision
        super().__init__(f"session {session_id} snapshot revision {revision} already contains different state")


class ClaimVersionConflict(Exception):
    """A concurrent participant published this immutable claim version."""

    def __init__(self, claim_id: str, version: int):
        self.claim_id = claim_id
        self.version = version
        super().__init__(f"claim {claim_id} version {version} already contains different state")


class ClaimBatchConflict(Exception):
    """The claim set advanced before an atomic multi-claim publication."""

    def __init__(self, expected_generation: int, observed_generation: int):
        self.expected_generation = expected_generation
        self.observed_generation = observed_generation
        super().__init__(
            f"claim publication expected generation {expected_generation}, "
            f"observed {observed_generation}"
        )


STORE_PROVIDER_SCHEMA = "mncs.environment.session-store-provider/2"


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

    runtime_environment = None
    language = selected_checkouts.get("mncs-language") if isinstance(selected_checkouts, dict) else None
    if language is not None:
        if not isinstance(language, dict):
            raise ValueError("selected mncs-language checkout facts must be an object")
        toolchain = environment.get("toolchain")
        if not isinstance(toolchain, dict) or toolchain.get("status") != "available":
            raise ValueError(
                "selected mncs-language toolchain is unavailable; refusing an ambient Store compiler"
            )
        language_checkout_value = language.get("path")
        language_revision = language.get("head")
        binary_value = toolchain.get("binary")
        if not all(isinstance(value, str) and value for value in (
            language_checkout_value, language_revision, binary_value,
        )):
            raise ValueError("selected mncs-language toolchain binding is incomplete")
        language_checkout = Path(language_checkout_value)
        if not language_checkout.is_absolute():
            language_checkout = workspace_root / language_checkout
        language_checkout = language_checkout.resolve()
        if not language_checkout.is_relative_to(workspace_root):
            raise ValueError("selected mncs-language checkout escapes the Environment workspace")
        if toolchain.get("checkout") != str(language_checkout):
            raise ValueError("selected mncs-language toolchain checkout does not match its provider binding")
        if toolchain.get("revision") != language_revision:
            raise ValueError("selected mncs-language toolchain revision does not match its provider binding")
        binary = Path(binary_value).resolve()
        if not binary.is_relative_to(language_checkout) or not binary.is_file():
            raise ValueError("selected mncs-language compiler binary is unavailable in its checkout")
        embed_library = binary.parent / "libmncs_embed.so"
        if not embed_library.is_file():
            raise ValueError(
                f"selected mncs-language embed library is unavailable beside {binary}"
            )
        runtime_environment = {
            "MNCS_STORE_ROOT": str(checkout),
            "MNCS_LANGUAGE_ROOT": str(language_checkout),
            "MNCS_BIN": str(binary),
            "MNCS_EMBED_LIB": str(embed_library.resolve()),
        }

    stdlib = selected_checkouts.get("mncs-stdlib")
    if runtime_environment is not None and stdlib is not None:
        if not isinstance(stdlib, dict):
            raise ValueError("selected mncs-stdlib checkout facts must be an object")
        runtime_environment["MNCS_STDLIB_ROOT"] = str(selected_stdlib_root(workspace_root, stdlib))

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
        "runtime_environment": runtime_environment,
    }


def write_session_store_provider(
    state_dir: Path | str, session_id: str, binding: dict[str, Any]
) -> None:
    """Persist Store bootstrap routing under the configured Store root.

    This small bootstrap record selects the package used to open the Store;
    it lives beside Store metadata so the persistence owner can write it
    without granting access to the parent Environment state directory.
    """
    payload = {**binding, "session_id": session_id}
    root = Path(state_dir).expanduser().resolve()
    path = root / "store" / "environment" / "sessions" / session_id / "store-provider.json"
    legacy_path = root / "sessions" / session_id / "store-provider.json"
    existing = read_json(path)
    if existing is None:
        existing = read_json(legacy_path)
    if existing is not None and existing != payload:
        raise ValueError(f"session {session_id} already has a different Store provider binding")
    write_json(path, payload)


def upgrade_session_store_provider(state_dir: Path | str, snapshot: dict[str, Any]) -> bool:
    """Upgrade existing package-only bootstrap metadata during explicit entry.

    Session objects and history stay untouched. Read-only opens never migrate;
    the current Store schema remains the only runtime implementation.
    """
    session_id = snapshot["session_id"]
    root = Path(state_dir).expanduser().resolve()
    path = root / "store" / "environment" / "sessions" / session_id / "store-provider.json"
    legacy_path = root / "sessions" / session_id / "store-provider.json"
    prior = read_json(path)
    if prior is None:
        prior = read_json(legacy_path)
    if not isinstance(prior, dict) or prior.get("schema_version") != "mncs.environment.session-store-provider/1":
        return False
    selected = store_provider_from_environment(snapshot)
    if selected is None or prior.get("session_id") != session_id or any(
        prior.get(key) != selected.get(key)
        for key in ("provider", "workspace_root", "checkout", "python_package")
    ):
        raise ValueError(f"session {session_id} old Store metadata does not match its selected checkout")
    write_json(path, {**prior, "schema_version": STORE_PROVIDER_SCHEMA,
                      "runtime_environment": selected.get("runtime_environment")})
    return True


def _session_store_provider(state_dir: Path | str, session_id: str) -> dict[str, Any] | None:
    root = Path(state_dir).expanduser().resolve()
    path = root / "store" / "environment" / "sessions" / session_id / "store-provider.json"
    legacy_path = root / "sessions" / session_id / "store-provider.json"
    payload = read_json(path)
    if payload is None:
        payload = read_json(legacy_path)
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
    runtime_environment = payload.get("runtime_environment")
    if runtime_environment is not None:
        if not isinstance(runtime_environment, dict):
            raise ValueError(f"session {session_id} has an invalid Store runtime binding")
        expected_store_root = str(checkout)
        if runtime_environment.get("MNCS_STORE_ROOT") != expected_store_root:
            raise ValueError(f"session {session_id} Store root is not its selected checkout")
        language_root_value = runtime_environment.get("MNCS_LANGUAGE_ROOT")
        binary_value = runtime_environment.get("MNCS_BIN")
        embed_value = runtime_environment.get("MNCS_EMBED_LIB")
        if not all(isinstance(value, str) and value for value in (
            language_root_value, binary_value, embed_value,
        )):
            raise ValueError(f"session {session_id} has an incomplete MNCS runtime binding")
        language_root = Path(language_root_value).resolve()
        binary = Path(binary_value).resolve()
        embed_library = Path(embed_value).resolve()
        if not language_root.is_relative_to(workspace_root):
            raise ValueError(f"session {session_id} MNCS checkout escapes its workspace")
        if (not binary.is_relative_to(language_root) or not binary.is_file()
                or not embed_library.is_relative_to(language_root)
                or not embed_library.is_file()):
            raise ValueError(f"session {session_id} selected MNCS runtime is unavailable")
        stdlib_value = runtime_environment.get("MNCS_STDLIB_ROOT")
        runtime_environment = {
            "MNCS_STORE_ROOT": str(checkout),
            "MNCS_LANGUAGE_ROOT": str(language_root),
            "MNCS_BIN": str(binary),
            "MNCS_EMBED_LIB": str(embed_library),
        }
        if stdlib_value is not None:
            runtime_environment["MNCS_STDLIB_ROOT"] = str(selected_stdlib_root(
                workspace_root, {"path": stdlib_value}))
    elif payload.get("schema_version") == STORE_PROVIDER_SCHEMA:
        # A Store-only environment has no selected Language checkout. Campaigns
        # that select one must persist its exact runtime in this provider record.
        runtime_environment = None
    return {**payload, "python_package": str(package),
            "runtime_environment": runtime_environment}


def _session_store_package(state_dir: Path | str, session_id: str) -> str | None:
    provider = _session_store_provider(state_dir, session_id)
    return provider.get("python_package") if provider is not None else None


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

    def list_checkpoints(self, session_id: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    def save_handoff(self, session_id: str, record: dict[str, Any]) -> None:
        raise NotImplementedError

    def load_handoff(self, session_id: str, handoff_id: str) -> dict[str, Any] | None:
        raise NotImplementedError

    def put_claim(self, claim: dict[str, Any]) -> None:
        raise NotImplementedError

    def claim_generation(self) -> int:
        raise NotImplementedError

    def put_claim_batch(
        self, claims: list[dict[str, Any]], *, expected_generation: int
    ) -> None:
        raise NotImplementedError

    def read_claims(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    def read_projection_row(
            self, projection_id: str) -> tuple[int, dict[str, Any]] | None:
        """Latest shared (version, row); None when never recorded."""
        raise NotImplementedError

    def write_projection_row(self, projection_id: str, version: int,
                             row: dict[str, Any]) -> None:
        """Publish immutable row version; raise ProjectionConflict on race."""
        raise NotImplementedError

    def read_projection_versions(self) -> dict[str, int]:
        """Latest row version per projection identity."""
        raise NotImplementedError

    def read_evidence(self, evidence_id: str) -> dict[str, Any] | None:
        """Immutable verification evidence by identity."""
        raise NotImplementedError

    def write_evidence(self, evidence_id: str,
                       record: dict[str, Any]) -> dict[str, Any]:
        """Record immutable evidence; identical rewrites are idempotent."""
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

    def list_checkpoints(self, session_id: str) -> list[dict[str, Any]]:
        directory = self._directory(session_id) / "checkpoints"
        if not directory.is_dir():
            return []
        records = []
        for path in directory.glob("chk_*.json"):
            record = read_json(path)
            if isinstance(record, dict) and record.get("session_id") == session_id:
                records.append(record)
        return sorted(records, key=lambda record: (int(record.get("sequence", 0)),
                                                    str(record.get("identity", ""))))

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

    def claim_generation(self) -> int:
        return len(self.read_claims())

    def put_claim_batch(
        self, claims: list[dict[str, Any]], *, expected_generation: int
    ) -> None:
        """Atomically append a related claim transition in the debug backend."""
        import json
        import os
        import tempfile

        path = self.state_dir / "claims.jsonl"
        lock = self.state_dir / "claims.lock"
        with exclusive_file_lock(lock):
            existing = self.read_claims()
            observed = len(existing)
            if observed != expected_generation:
                raise ClaimBatchConflict(expected_generation, observed)
            by_version = {
                (str(row.get("claim_id", "")), int(row.get("version", 0))): row
                for row in existing
            }
            for claim in claims:
                key = (str(claim.get("claim_id", "")), int(claim.get("version", 0)))
                prior = by_version.get(key)
                if prior is not None:
                    if prior != claim:
                        raise ClaimVersionConflict(*key)
                    continue
                by_version[key] = claim
            all_records = [*existing, *claims]
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    for record in all_records:
                        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(name, path)
                directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                try:
                    os.unlink(name)
                except FileNotFoundError:
                    pass

    def read_claims(self) -> list[dict[str, Any]]:
        records = read_jsonl(self.state_dir / "claims.jsonl")
        records.sort(key=lambda record: (str(record.get("claim_id", "")), int(record.get("version", 0))))
        return records

    @staticmethod
    def _projection_path(state_dir: Path, projection_id: str) -> Path:
        safe = "".join(char if char.isalnum() or char in ("-", "_", ".")
                       else "_" for char in projection_id)
        return state_dir / "projections" / f"{safe}.json"

    def read_projection_row(
            self, projection_id: str) -> tuple[int, dict[str, Any]] | None:
        record = read_json(self._projection_path(self.state_dir,
                                                 projection_id))
        if not isinstance(record, dict):
            return None
        row = record.get("row")
        if not isinstance(row, dict):
            return None
        try:
            version = int(record.get("version", 0))
        except (TypeError, ValueError):
            return None
        return version, row

    def write_projection_row(self, projection_id: str, version: int,
                             row: dict[str, Any]) -> None:
        from .projection_store import ProjectionConflict  # noqa: E402

        path = self._projection_path(self.state_dir, projection_id)
        with exclusive_file_lock(path.with_suffix(".lock")):
            current = self.read_projection_row(projection_id)
            if current is not None and current[0] == version:
                if current[1] == row:
                    return
                raise ProjectionConflict(projection_id, current[1])
            if current is not None and current[0] > version - 1:
                raise ProjectionConflict(projection_id, current[1])
            write_json(path, {"version": int(version), "row": row})

    def read_projection_versions(self) -> dict[str, int]:
        base = self.state_dir / "projections"
        if not base.is_dir():
            return {}
        versions: dict[str, int] = {}
        for path in sorted(base.glob("*.json")):
            record = read_json(path)
            if not isinstance(record, dict):
                continue
            row = record.get("row")
            if not isinstance(row, dict) or not row.get("projection"):
                continue
            try:
                versions[str(row["projection"])] = int(
                    record.get("version", 0))
            except (TypeError, ValueError):
                continue
        return versions

    def read_evidence(self, evidence_id: str) -> dict[str, Any] | None:
        record = read_json(self.state_dir / "verification-evidence"
                           / f"{evidence_id}.json")
        return record if isinstance(record, dict) else None

    def write_evidence(self, evidence_id: str,
                       record: dict[str, Any]) -> dict[str, Any]:
        from .projection_store import EvidenceConflict  # noqa: E402

        existing = self.read_evidence(evidence_id)
        if isinstance(existing, dict):
            if all(existing.get(key) == record.get(key)
                   for key in set(existing) | set(record)
                   if key != "schema_version"):
                return existing
            raise EvidenceConflict(
                f"conflicting payload under {evidence_id}")
        write_json(self.state_dir / "verification-evidence"
                   / f"{evidence_id}.json", record)
        return record


class StoreSessionStore(SessionStore):
    """Canonical Store-backed persistence (mncs-store persistent objects)."""

    def __init__(
        self,
        state_dir: Path | str,
        *,
        verify_on_open: bool = True,
        store_package_dir: str | Path | None = None,
        store_runtime: dict[str, str] | None = None,
        defer_mutation: bool = True,
    ):
        from .store_backend import StoreBackend

        self.state_dir = Path(state_dir)
        self.backend = StoreBackend(
            self.state_dir,
            verify_on_open=verify_on_open,
            store_package_dir=store_package_dir,
            store_runtime=store_runtime,
            **({"defer_mutation": True} if defer_mutation else {}),
        )

    def close(self) -> None:
        self.backend.close()

    def get_record(self, schema: bytes, identity: bytes):
        return self.backend.get_record(schema, identity)

    def get_record_strict(self, schema: bytes, identity: bytes):
        """Read one Store binding without hiding integrity/provider failures."""
        getter = getattr(self.backend, "get_record_strict", None)
        if not callable(getter):
            from .store_backend import StoreUnavailable

            raise StoreUnavailable(
                "selected Store provider does not expose strict bound-record reads",
                code="provider-operation-unsupported",
            )
        return getter(schema, identity)

    def put_record(self, schema: bytes, identity: bytes, record: dict):
        return self.backend.put_record(schema, identity, record)

    def generation(self) -> int:
        return self.backend.generation()

    def objects_at(self, generation: int) -> list[Any] | None:
        objects_at = getattr(self.backend, "objects_at", None)
        if not callable(objects_at):
            return None
        return list(objects_at(generation))

    def domain_bindings_at(self, generation: int) -> tuple[tuple[bytes, bytes], ...]:
        return self.backend.domain_bindings_at(generation)

    def domain_bindings_since(
        self, generation: int, *, max_generations: int = 128
    ) -> tuple[tuple[int, bytes, bytes], ...]:
        """Forward Store's bounded identity delta to Environment replay."""
        return self.backend.domain_bindings_since(
            generation, max_generations=max_generations)

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

    def list_checkpoints(self, session_id: str) -> list[dict[str, Any]]:
        return self.backend.list_checkpoints(session_id)

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
        try:
            self.backend.put_claim(claim)
        except Exception as error:
            # The backend surfaces mncs-store identity conflicts as a
            # StoreError carrying an IDENTITY_CONFLICT code; translate to
            # the backend-agnostic CAS signal without importing mncs-store.
            if getattr(getattr(error, "code", None), "name", None) == "IDENTITY_CONFLICT":
                raise ClaimVersionConflict(str(claim.get("claim_id", "")),
                                           int(claim.get("version", 0))) from error
            raise

    def claim_generation(self) -> int:
        return self.backend.generation()

    def put_claim_batch(
        self, claims: list[dict[str, Any]], *, expected_generation: int
    ) -> None:
        try:
            self.backend.put_claim_batch(
                claims, expected_generation=expected_generation
            )
        except Exception as error:
            if getattr(getattr(error, "code", None), "name", None) == "IDENTITY_CONFLICT":
                first = claims[0]
                raise ClaimVersionConflict(
                    str(first.get("claim_id", "")), int(first.get("version", 0))
                ) from error
            raise

    def read_claims(self) -> list[dict[str, Any]]:
        return self.backend.read_claims()

    def read_projection_row(
            self, projection_id: str) -> tuple[int, dict[str, Any]] | None:
        return self.backend.read_projection_row(projection_id)

    def write_projection_row(self, projection_id: str, version: int,
                             row: dict[str, Any]) -> None:
        self.backend.write_projection_row(projection_id, version, row)

    def read_projection_versions(self) -> dict[str, int]:
        return self.backend.read_projection_versions()

    def read_evidence(self, evidence_id: str) -> dict[str, Any] | None:
        return self.backend.read_evidence(evidence_id)

    def write_evidence(self, evidence_id: str,
                       record: dict[str, Any]) -> dict[str, Any]:
        return self.backend.write_evidence(evidence_id, record)

    def list_sessions(self) -> list[str]:
        return self.backend.list_sessions()


def open_store(
    state_dir: Path | str,
    backend: str = "store",
    *,
    verify_on_open: bool = True,
    session_id: str | None = None,
    store_package_dir: str | Path | None = None,
    store_runtime: dict[str, str] | None = None,
    defer_mutation: bool = True,
) -> SessionStore:
    """Open the canonical Store backend or the explicit file debug projection.

    The Store backend opens read-only first and promotes to writable on the
    first mutation. Reads verify the generation, bindings, and selected
    objects without re-reading every unrelated payload; promotion runs
    recovery and each publication validates its generation and bindings.
    A complete payload scrub remains available through explicit verify.
    """
    if backend == "file":
        return FileSessionStore(state_dir)
    if backend == "store":
        if session_id is not None:
            provider = _session_store_provider(state_dir, session_id)
            if provider is not None:
                if store_package_dir is None:
                    store_package_dir = provider["python_package"]
                if store_runtime is None:
                    store_runtime = provider.get("runtime_environment")
        return StoreSessionStore(
            state_dir,
            verify_on_open=verify_on_open,
            store_package_dir=store_package_dir,
            store_runtime=store_runtime,
            **({} if defer_mutation else {"defer_mutation": False}),
        )
    raise ValueError(f"unknown session store backend {backend!r}")
