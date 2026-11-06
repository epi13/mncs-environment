"""Filesystem boundary for Environment-owned disposable workspaces.

Environment allocates these paths; callers cannot register arbitrary paths
for deletion. Session snapshots own the allocation/cleanup lifecycle. This
module owns only private directories, identity checks and filesystem effects.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
from typing import Any, Iterator

SCHEMA = "mncs.environment.scratch-workspace/1"
MARKER = ".mncs-scratch-owner.json"
MAX_WORKSPACES = 32
LABEL = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
SESSION = re.compile(r"ses_[A-Za-z0-9_-]{1,128}\Z")
RESOURCE = re.compile(r"scratch_[0-9a-f]{32}\Z")


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, exist_ok=True)
    metadata = path.lstat()
    if (not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077):
        raise ValueError("scratch directory is not a private, owned real directory")


def root(state_dir: Path, session_id: str) -> Path:
    if not SESSION.fullmatch(session_id):
        raise ValueError("invalid scratch session identity")
    state = Path(state_dir).expanduser().resolve(strict=True)
    parent = state / "scratch"
    _private_directory(parent)
    selected = parent / session_id
    _private_directory(selected)
    return selected


@contextmanager
def lock(state_dir: Path, session_id: str) -> Iterator[None]:
    directory = root(state_dir, session_id)
    descriptor = os.open(directory / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def plan(session_id: str, label: str) -> dict[str, Any]:
    if not isinstance(label, str) or not LABEL.fullmatch(label):
        raise ValueError("scratch label must be 1..64 lowercase letters, digits or hyphens")
    return {"schema_version": SCHEMA, "session_id": session_id, "label": label,
            "resource_id": "scratch_" + secrets.token_hex(16),
            "token": secrets.token_hex(32), "status": "allocating"}


def path(state_dir: Path, session_id: str, record: dict[str, Any]) -> Path:
    if (record.get("schema_version") != SCHEMA or record.get("session_id") != session_id
            or not isinstance(record.get("resource_id"), str)
            or not RESOURCE.fullmatch(record["resource_id"])
            or not isinstance(record.get("token"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", record["token"])):
        raise ValueError("invalid scratch ownership record")
    return root(state_dir, session_id) / record["resource_id"]


def _marker(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record[key] for key in ("schema_version", "session_id", "resource_id", "token")}


def verify(state_dir: Path, session_id: str, record: dict[str, Any]) -> Path:
    selected = path(state_dir, session_id, record)
    metadata = selected.lstat()
    if (not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077):
        raise ValueError("scratch workspace is no longer a private, owned real directory")
    if record.get("device") is not None:
        if record.get("inode") != metadata.st_ino:
            raise ValueError("scratch workspace filesystem identity changed")
        if record.get("device") != metadata.st_dev:
            # st_dev can be namespace-relative: the same mounted filesystem
            # may receive a different device number in the Environment
            # service and in the cleanup CLI. Accept that view only when the
            # recorded inode still matches and the workspace remains on the
            # private session-root filesystem in this namespace.
            parent = selected.parent.lstat()
            if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                    or parent.st_mode & 0o077 or metadata.st_dev != parent.st_dev):
                raise ValueError("scratch workspace filesystem identity changed")
    descriptor = os.open(selected / MARKER, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor) as stream:
        if os.fstat(stream.fileno()).st_size > 1024 or json.load(stream) != _marker(record):
            raise ValueError("scratch workspace ownership marker changed")
    return selected


def allocate(state_dir: Path, session_id: str, record: dict[str, Any]) -> dict[str, Any]:
    """Finish a persisted allocation intent, including an interrupted retry."""
    selected = path(state_dir, session_id, record)
    try:
        selected.mkdir(mode=0o700)
    except FileExistsError:
        # An existing path is adopted only with the exact recorded marker.
        # An interrupted empty creation is harmless and remains visible.
        verify(state_dir, session_id, record)
    else:
        descriptor = os.open(selected / MARKER,
                             os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(_marker(record), stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
    metadata = selected.lstat()
    return {**record, "device": metadata.st_dev, "inode": metadata.st_ino, "status": "ready"}


def visible_process_users(selected: Path) -> list[int]:
    """Defer for observable processes still using the explicitly disposable path.

    Session completion is the caller's end-of-work boundary. This additional
    Linux observation is not a process fence: inaccessible processes and races
    cannot be disproved by /proc inspection. Producers must stop owned jobs
    before completing, or publish durable outputs outside scratch.
    """
    prefix = str(selected) + "/"
    exact = str(selected)
    users = []
    for process in Path("/proc").iterdir():
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        try:
            if process.stat().st_uid != os.getuid():
                continue
            links = [process / "cwd", process / "exe"]
            try:
                links.extend((process / "fd").iterdir())
            except OSError:
                pass
            used = False
            for link in links:
                try:
                    value = os.readlink(link).removesuffix(" (deleted)")
                    if value == exact or value.startswith(prefix):
                        used = True
                        break
                except OSError:
                    pass
            if not used:
                try:
                    used = prefix in (process / "maps").read_text()
                except OSError:
                    pass
            if used:
                users.append(int(process.name))
        except OSError:
            continue
    return sorted(users)


def release(state_dir: Path, session_id: str, record: dict[str, Any]) -> dict[str, Any]:
    """Delete only a matching owned workspace; preserve unexpected state."""
    try:
        selected = path(state_dir, session_id, record)
        try:
            selected.lstat()
        except FileNotFoundError:
            return {**record, "status": "released", "reason": "already-absent"}
        selected = verify(state_dir, session_id, record)
        users = visible_process_users(selected)
        if users:
            return {**record, "status": "deferred", "reason": "visible-process-users", "pids": users}
        if not shutil.rmtree.avoids_symlink_attacks:
            raise ValueError("safe directory removal is unavailable on this platform")
        # Verify again immediately before the symlink-resistant removal.
        verify(state_dir, session_id, record)
        shutil.rmtree(selected)
        return {**record, "status": "released", "reason": "removed-owned-workspace"}
    except (OSError, ValueError) as error:
        return {**record, "status": "preserved", "reason": str(error)[:256]}
