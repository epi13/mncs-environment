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
