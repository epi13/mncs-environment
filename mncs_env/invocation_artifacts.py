"""Bounded, Store-backed caches for capability invocation artifacts.

Providers receive a private scratch directory for one invocation.  When the
session uses canonical Store persistence, Environment restores the previous
capability cache from an immutable Store record and publishes the resulting
tree back as a content-addressed record.  The provider never receives the
Store path or a Store file descriptor.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Self

from . import evidence_store
from .identity import digest_hex
from .store_backend import StoreIntegrityFailure, StoreUnavailable

OWNER = "capability-invocation-artifacts"
SCHEMA = "mncs.environment.capability-artifacts/1"
MAX_FILES = 256
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_RECORD_BYTES = 96 * 1024 * 1024
MAX_PATH_CHARS = 1024
MAX_TREE_DEPTH = 24
MAX_INPUT_ARTIFACTS = 8
MAX_INPUT_ARTIFACT_BYTES = 1024 * 1024
MAX_INPUT_TOTAL_BYTES = 4 * 1024 * 1024
INPUT_ARTIFACT_SCHEMA = "mncs.environment.provider-input-artifacts/1"


class InvocationArtifactError(ValueError):
    """The provider produced an unsafe or over-budget artifact tree."""


def _safe_relative(value: Any) -> PurePosixPath:
    if (not isinstance(value, str) or not value or len(value) > MAX_PATH_CHARS
            or "\\" in value or "\x00" in value):
        raise StoreIntegrityFailure("capability artifact path is invalid")
    parts = value.split("/")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in parts):
        raise StoreIntegrityFailure("capability artifact path escapes its bundle")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise StoreIntegrityFailure("capability artifact path is not valid UTF-8") from error
    return path


def _is_file_backend(session: Any) -> bool:
    # Import lazily to keep the Store adapter as the normal path and avoid a
    # module cycle during Environment startup.
    from .session_store import FileSessionStore

    return isinstance(session.store, FileSessionStore)


class InvocationArtifactWorkspace:
    """Temporary provider workspace with an immutable Store cache boundary."""

    def __init__(self, session: Any, capability: str, binding_id: str | None):
        self.session = session
        self.capability = capability
        self.binding_id = binding_id
        self.path: Path | None = None
        self.store_backed = False
        self.file_backed = False
        self.previous_reference: str | None = None
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self._root_fd: int | None = None

    def __enter__(self) -> Self:
        if _is_file_backend(self.session):
            state_root = Path(self.session.store.state_dir).expanduser().resolve()
            directory = (
                state_root / "sessions" / self.session.session_id / "artifacts"
                / digest_hex(self.capability)
            ).resolve()
            if not directory.is_relative_to(state_root):
                raise ValueError("session artifact directory escapes the Environment state root")
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.path = directory
            self.file_backed = True
            return self

        self._temporary = tempfile.TemporaryDirectory(prefix="mncs-env-artifacts-")
        self.path = Path(self._temporary.name)
        try:
            self._root_fd = os.open(
                self.path,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
        except OSError as error:
            self._temporary.cleanup()
            self._temporary = None
            self.path = None
            raise StoreUnavailable(
                f"private capability artifact workspace is unavailable: {error}",
                code="provider-operation-unavailable",
            ) from error
        methods = evidence_store._store_methods(self.session)
        self.store_backed = methods is not None
        try:
            if self.store_backed:
                self.previous_reference = self._latest_reference()
                if self.previous_reference is not None:
                    self._restore(self.previous_reference)
            elif self._has_unreadable_reference():
                raise StoreUnavailable(
                    "selected Store provider cannot restore a prior capability artifact bundle",
                    code="provider-operation-unsupported",
                )
        except Exception:
            if self._root_fd is not None:
                os.close(self._root_fd)
                self._root_fd = None
            self._temporary.cleanup()
            self._temporary = None
            self.path = None
            raise
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._root_fd is not None:
            os.close(self._root_fd)
            self._root_fd = None
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None

    def _matching_record(self, item: Any) -> bool:
        return (
            isinstance(item, dict)
            and item.get("kind") == "invocation-result"
            and item.get("capability") == self.capability
            and item.get("binding_id") == self.binding_id
        )

    def _latest_reference(self) -> str | None:
        records = self.session.snapshot.get("artifacts", [])
        if not isinstance(records, list):
            return None
        for item in reversed(records):
            if not self._matching_record(item):
                continue
            reference = item.get("artifact_store_ref")
            if isinstance(reference, str) and evidence_store.is_store_reference(reference):
                return reference
        return None

    def _has_unreadable_reference(self) -> bool:
        records = self.session.snapshot.get("artifacts", [])
        return isinstance(records, list) and any(
            self._matching_record(item)
            and isinstance(item.get("artifact_store_ref"), str)
            and evidence_store.is_store_reference(item["artifact_store_ref"])
            for item in records
        )

    def _record_identity(self, reference: str) -> dict[str, Any]:
        record = evidence_store.read(self.session, OWNER, reference)
        if (not isinstance(record, dict)
                or record.get("schema_version") != SCHEMA
                or record.get("session_id") != self.session.session_id
                or record.get("capability") != self.capability
                or record.get("binding_id") != self.binding_id):
            raise StoreIntegrityFailure(
                "capability artifact bundle does not match its session binding"
            )
        return record

    def _restore(self, reference: str) -> None:
        assert self.path is not None
        record = self._record_identity(reference)
        try:
            encoded_record = json.dumps(
                record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, UnicodeEncodeError) as error:
            raise StoreIntegrityFailure(
                "capability artifact bundle is not valid UTF-8 JSON"
            ) from error
        if len(encoded_record) > MAX_RECORD_BYTES:
            raise StoreIntegrityFailure("capability artifact bundle exceeds its size bound")
        files = record.get("files")
        if not isinstance(files, list) or len(files) > MAX_FILES:
            raise StoreIntegrityFailure("capability artifact bundle has an invalid file list")
        seen: set[str] = set()
        total = 0
        for item in files:
            if not isinstance(item, dict):
                raise StoreIntegrityFailure("capability artifact entry is not an object")
            relative = _safe_relative(item.get("path"))
            name = relative.as_posix()
            if name in seen:
                raise StoreIntegrityFailure("capability artifact bundle repeats a file path")
            seen.add(name)
            encoded = item.get("content_base64")
            if not isinstance(encoded, str):
                raise StoreIntegrityFailure("capability artifact content is missing")
            if len(encoded) > 4 * ((MAX_FILE_BYTES + 2) // 3):
                raise StoreIntegrityFailure("capability artifact encoded file exceeds its size bound")
            try:
                content = base64.b64decode(encoded.encode("ascii"), validate=True)
            except (UnicodeEncodeError, ValueError) as error:
                raise StoreIntegrityFailure("capability artifact content is not valid base64") from error
            if (len(content) > MAX_FILE_BYTES
                    or item.get("byte_count") != len(content)
                    or item.get("sha256") != hashlib.sha256(content).hexdigest()):
                raise StoreIntegrityFailure("capability artifact content failed exact verification")
            total += len(content)
            if total > MAX_TOTAL_BYTES:
                raise StoreIntegrityFailure("capability artifact bundle exceeds its size bound")
            destination = self.path.joinpath(*relative.parts)
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            flags |= getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(destination, flags, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
        if record.get("total_bytes") != total:
            raise StoreIntegrityFailure("capability artifact bundle has an invalid total size")

    def read_file(self, relative_path: str, *, max_bytes: int) -> bytes:
        """Read one restored file through the private workspace's anchored fd.

        The Store bundle has already passed full content and identity
        verification in ``_restore``. This method adds a smaller caller-facing
        size bound and never follows a path component supplied by a consumer.
        """
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("artifact read bound must be a positive integer")
        if not self.store_backed or self.path is None or self._root_fd is None:
            raise StoreUnavailable(
                "capability artifact read requires the strict Store provider",
                code="provider-operation-unsupported",
            )
        relative = _safe_relative(relative_path)
        directory_fd = os.dup(self._root_fd)
        nofollow_flag = getattr(os, "O_NOFOLLOW", 0)
        directory_flag = getattr(os, "O_DIRECTORY", 0)
        try:
            for part in relative.parts[:-1]:
                child_fd = os.open(
                    part, os.O_RDONLY | directory_flag | nofollow_flag,
                    dir_fd=directory_fd,
                )
                os.close(directory_fd)
                directory_fd = child_fd
            file_fd = os.open(
                relative.parts[-1], os.O_RDONLY | nofollow_flag,
                dir_fd=directory_fd,
            )
        except OSError as error:
            raise StoreIntegrityFailure(
                "requested capability artifact file is unavailable"
            ) from error
        finally:
            os.close(directory_fd)
        try:
            before = os.fstat(file_fd)
            if not stat.S_ISREG(before.st_mode):
                raise StoreIntegrityFailure(
                    "requested capability artifact is not a regular file"
                )
            if before.st_size > max_bytes:
                raise InvocationArtifactError(
                    "requested capability artifact exceeds the read size bound"
                )
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining:
                chunk = os.read(file_fd, min(remaining, 64 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            content = b"".join(chunks)
            after = os.fstat(file_fd)
            if (len(content) > max_bytes or len(content) != before.st_size
                    or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
                raise StoreIntegrityFailure(
                    "requested capability artifact changed during readback"
                )
            return content
        finally:
            os.close(file_fd)

    def _collect(self) -> tuple[list[dict[str, Any]], int]:
        assert self.path is not None
        assert self._root_fd is not None
        files: list[dict[str, Any]] = []
        total = 0
        directory_flag = getattr(os, "O_DIRECTORY", 0)
        nofollow_flag = getattr(os, "O_NOFOLLOW", 0)

        def visit(directory_fd: int, prefix: tuple[str, ...], depth: int) -> None:
            nonlocal total
            if depth > MAX_TREE_DEPTH:
                raise InvocationArtifactError("capability artifact tree exceeds its depth bound")
            scan_fd: int | None = None
            try:
                # Reopen the anchored directory so the iterator gets a fresh
                # directory offset and sees files created after the root fd.
                scan_fd = os.open(
                    ".", os.O_RDONLY | directory_flag | nofollow_flag,
                    dir_fd=directory_fd,
                )
                with os.scandir(scan_fd) as scan:
                    entries = sorted(
                        ((entry.name, entry.stat(follow_symlinks=False)) for entry in scan),
                        key=lambda item: item[0],
                    )
            except OSError as error:
                raise InvocationArtifactError(f"capability artifact tree is unreadable: {error}") from error
            finally:
                if scan_fd is not None:
                    os.close(scan_fd)
            for name, observed in entries:
                relative = "/".join((*prefix, name))
                _safe_relative(relative)
                if stat.S_ISLNK(observed.st_mode):
                    raise InvocationArtifactError("capability artifact tree contains a symlink")
                if stat.S_ISDIR(observed.st_mode):
                    try:
                        child_fd = os.open(
                            name,
                            os.O_RDONLY | directory_flag | nofollow_flag,
                            dir_fd=directory_fd,
                        )
                    except OSError as error:
                        raise InvocationArtifactError(
                            f"capability artifact directory changed while collected: {error}"
                        ) from error
                    try:
                        current = os.fstat(child_fd)
                        if (not stat.S_ISDIR(current.st_mode)
                                or (current.st_dev, current.st_ino)
                                != (observed.st_dev, observed.st_ino)):
                            raise InvocationArtifactError(
                                "capability artifact directory changed while collected"
                            )
                        visit(child_fd, (*prefix, name), depth + 1)
                    finally:
                        os.close(child_fd)
                    continue
                if not stat.S_ISREG(observed.st_mode):
                    raise InvocationArtifactError("capability artifact tree contains a non-regular entry")
                if observed.st_size > MAX_FILE_BYTES:
                    raise InvocationArtifactError("capability artifact file exceeds its size bound")
                try:
                    file_fd = os.open(
                        name,
                        os.O_RDONLY | nofollow_flag,
                        dir_fd=directory_fd,
                    )
                    with os.fdopen(file_fd, "rb") as handle:
                        before = os.fstat(handle.fileno())
                        if (not stat.S_ISREG(before.st_mode)
                                or (before.st_dev, before.st_ino)
                                != (observed.st_dev, observed.st_ino)
                                or before.st_size > MAX_FILE_BYTES):
                            raise InvocationArtifactError(
                                "capability artifact file changed while collected"
                            )
                        content = handle.read(MAX_FILE_BYTES + 1)
                        after = os.fstat(handle.fileno())
                except OSError as error:
                    raise InvocationArtifactError(f"capability artifact file is unreadable: {error}") from error
                identity_before = (
                    before.st_dev, before.st_ino, before.st_size,
                    before.st_mtime_ns, before.st_ctime_ns,
                )
                identity_after = (
                    after.st_dev, after.st_ino, after.st_size,
                    after.st_mtime_ns, after.st_ctime_ns,
                )
                if len(content) != observed.st_size or identity_before != identity_after:
                    raise InvocationArtifactError("capability artifact changed while it was collected")
                total += len(content)
                if total > MAX_TOTAL_BYTES:
                    raise InvocationArtifactError("capability artifact tree exceeds its total size bound")
                files.append({
                    "path": relative,
                    "byte_count": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "content_base64": base64.b64encode(content).decode("ascii"),
                })
                if len(files) > MAX_FILES:
                    raise InvocationArtifactError("capability artifact tree exceeds its file-count bound")

        if not stat.S_ISDIR(os.fstat(self._root_fd).st_mode):
            raise InvocationArtifactError("capability artifact root is not a directory")
        visit(self._root_fd, (), 0)
        return files, total

    def publish(self) -> dict[str, Any]:
        """Persist the post-invocation tree and return bounded snapshot fields."""
        assert self.path is not None
        if self.file_backed:
            return {
                "artifact_directory": str(self.path),
                "artifact_store_ref": None,
                "artifact_file_count": None,
                "artifact_byte_count": None,
            }
        files, total = self._collect()
        if not self.store_backed:
            if files:
                raise StoreUnavailable(
                    "selected Store provider cannot publish capability artifact bundles",
                    code="provider-operation-unsupported",
                )
            return {
                "artifact_directory": None,
                "artifact_store_ref": None,
                "artifact_file_count": 0,
                "artifact_byte_count": 0,
            }
        if not files and self.previous_reference is None:
            return {
                "artifact_directory": None,
                "artifact_store_ref": None,
                "artifact_file_count": 0,
                "artifact_byte_count": 0,
            }
        record = {
            "schema_version": SCHEMA,
            "session_id": self.session.session_id,
            "capability": self.capability,
            "binding_id": self.binding_id,
            "files": files,
            "total_bytes": total,
        }
        encoded_size = len(json.dumps(
            record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8"))
        if encoded_size > MAX_RECORD_BYTES:
            raise InvocationArtifactError("capability artifact Store record exceeds its size bound")
        reference = evidence_store.publish(self.session, OWNER, record)
        return {
            "artifact_directory": None,
            "artifact_store_ref": reference,
            "artifact_file_count": len(files),
            "artifact_byte_count": total,
        }


def read_latest_invocation_file(
    session: Any,
    capability: str,
    binding_id: str,
    relative_path: str,
    *,
    max_bytes: int = MAX_INPUT_ARTIFACT_BYTES,
) -> tuple[dict[str, Any], bytes]:
    """Read one file from the latest successful, Store-backed invocation.

    The immutable reference is taken from the Environment snapshot, then
    checked against the restored bundle before the file is returned.
    """
    try:
        relative = _safe_relative(relative_path)
    except StoreIntegrityFailure as error:
        raise InvocationArtifactError(
            "provider input artifact path is invalid"
        ) from error
    artifacts = session.snapshot.get("artifacts", [])
    latest = next((
        item for item in reversed(artifacts)
        if isinstance(item, dict)
        and item.get("kind") == "invocation-result"
        and item.get("capability") == capability
        and item.get("binding_id") == binding_id
    ), None) if isinstance(artifacts, list) else None
    if latest is None:
        raise InvocationArtifactError(
            "the selected capability has no recorded invocation artifact"
        )
    if latest.get("status") != "ok":
        raise InvocationArtifactError(
            "the latest capability invocation did not complete successfully"
        )
    reference = latest.get("artifact_store_ref")
    if not evidence_store.is_store_reference(reference):
        raise StoreUnavailable(
            "the latest capability invocation has no canonical Store artifact",
            code="provider-operation-unsupported",
        )

    with InvocationArtifactWorkspace(session, capability, binding_id) as workspace:
        if not workspace.store_backed:
            raise StoreUnavailable(
                "capability artifact input requires the strict Store provider",
                code="provider-operation-unsupported",
            )
        if workspace.previous_reference != reference:
            raise StoreIntegrityFailure(
                "latest invocation artifact does not match its canonical Store bundle"
            )
        content = workspace.read_file(relative.as_posix(), max_bytes=max_bytes)
    metadata = {
        "capability": capability,
        "binding_id": binding_id,
        "store_reference": reference,
        "path": relative.as_posix(),
        "byte_count": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    return metadata, content


class ProviderInputArtifactWorkspace:
    """Private, bounded copies of authorized Store artifacts for one provider.

    Providers receive only these selected files and their Store provenance in
    an environment manifest. They never receive Store paths or descriptors.
    """

    def __init__(self, session_id: str, artifacts: list[dict[str, Any]]):
        self.session_id = session_id
        self.artifacts = artifacts
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self.path: Path | None = None
        self.manifest: dict[str, Any] | None = None
        self.metadata: list[dict[str, Any]] = []

    def __enter__(self) -> Self:
        if not self.artifacts:
            return self
        if len(self.artifacts) > MAX_INPUT_ARTIFACTS:
            raise InvocationArtifactError("provider input artifact count exceeds its bound")
        self._temporary = tempfile.TemporaryDirectory(prefix="mncs-env-input-")
        self.path = Path(self._temporary.name)
        total = 0
        names: set[str] = set()
        files: list[dict[str, Any]] = []
        try:
            for index, item in enumerate(self.artifacts):
                name = item.get("name")
                content = item.get("content")
                if (not isinstance(name, str)
                        or not re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{0,79}", name)
                        or name in names):
                    raise InvocationArtifactError(
                        "provider input artifact names must be unique bounded identifiers"
                    )
                if not isinstance(content, bytes):
                    raise InvocationArtifactError("provider input artifact content must be bytes")
                if len(content) > MAX_INPUT_ARTIFACT_BYTES:
                    raise InvocationArtifactError("provider input artifact exceeds its size bound")
                total += len(content)
                if total > MAX_INPUT_TOTAL_BYTES:
                    raise InvocationArtifactError(
                        "provider input artifacts exceed their total size bound"
                    )
                names.add(name)
                metadata = {
                    key: value for key, value in item.items() if key != "content"
                }
                if (metadata.get("byte_count") != len(content)
                        or metadata.get("sha256") != hashlib.sha256(content).hexdigest()
                        or not evidence_store.is_store_reference(
                            metadata.get("store_reference")
                        )):
                    raise StoreIntegrityFailure(
                        "provider input metadata does not match its Store artifact bytes"
                    )
                local_path = f"{index:04d}.artifact"
                destination = self.path / local_path
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(destination, flags, 0o400)
                try:
                    view = memoryview(content)
                    while view:
                        written = os.write(descriptor, view)
                        if written <= 0:
                            raise OSError("short write while staging provider input artifact")
                        view = view[written:]
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                os.chmod(destination, 0o400, follow_symlinks=False)
                files.append({**metadata, "local_path": local_path})
                self.metadata.append(metadata)
            self.manifest = {
                "schema_version": INPUT_ARTIFACT_SCHEMA,
                "session_id": self.session_id,
                "artifacts": files,
            }
            return self
        except Exception:
            self._temporary.cleanup()
            self._temporary = None
            self.path = None
            raise

    def environment(self) -> dict[str, str]:
        """Return the provider-only paths and bounded provenance manifest."""
        if self.path is None or self.manifest is None:
            return {
                "MNCS_ENV_INPUT_ARTIFACT_DIR": "",
                "MNCS_ENV_INPUT_ARTIFACTS_JSON": "",
            }
        return {
            "MNCS_ENV_INPUT_ARTIFACT_DIR": str(self.path),
            "MNCS_ENV_INPUT_ARTIFACTS_JSON": json.dumps(
                self.manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        }

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._temporary is not None:
            self._temporary.cleanup()
            self._temporary = None
            self.path = None
