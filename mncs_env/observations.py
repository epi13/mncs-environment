"""Bounded filesystem/Git observation transport, never domain policy.

Git enumerates a checkout only at bootstrap or after external drift. Normal
observation stats that established catalogue and Git control files. Immutable
catalogues are disposable artifacts bound by a digest in the Store snapshot.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

from . import workspace as workspace_module
from .identity import digest_hex
from .persist import write_json

MAX_PATHS = 30000
CATALOGUE_SCHEMA = "mncs.environment.repository-observation/1"


def stamp(path: Path):
    try:
        item = path.stat()
        link = path.lstat()
        return [item.st_dev, item.st_ino, item.st_mode, item.st_size,
                item.st_mtime_ns, item.st_ctime_ns, link.st_ino, link.st_ctime_ns]
    except FileNotFoundError:
        return None


def git_directories(checkout: Path) -> tuple[Path, Path]:
    pointer = checkout / ".git"
    if pointer.is_dir():
        directory = pointer
    else:
        value = pointer.read_text().strip()
        if not value.startswith("gitdir: "):
            raise ValueError("invalid Git directory pointer")
        directory = (checkout / value[8:]).resolve()
    common_pointer = directory / "commondir"
    common = (directory / common_pointer.read_text().strip()).resolve() if common_pointer.is_file() else directory
    return directory, common


def control_paths(checkout: Path) -> list[Path]:
    directory, common = git_directories(checkout)
    result = [checkout / ".git", directory / "HEAD", directory / "index",
              directory / "commondir", common / "packed-refs", common / "config",
              common / "worktrees", common / "refs", common / "refs/heads",
              common / "info/exclude"]
    head = (directory / "HEAD").read_text().strip()
    if head.startswith("ref: "):
        result.append(common / head[5:])
    # Worktree registration affects authority even when selected HEAD is stable.
    if (common / "worktrees").is_dir():
        for worktree in sorted((common / "worktrees").iterdir()):
            result.extend([worktree, worktree / "gitdir", worktree / "HEAD"])
    return result


def _enumerate(checkout: Path) -> list[str]:
    completed = subprocess.run(
        [workspace_module.git_binary(), "-C", str(checkout), "ls-files",
         "--cached", "--others", "--exclude-standard", "-z"], capture_output=True, timeout=10, check=True, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    paths = sorted(set(os.fsdecode(value) for value in completed.stdout.split(b"\0") if value))
    paths = [name for name in paths if not name.startswith(".worktrees/")]
    if len(paths) > MAX_PATHS or any(Path(name).is_absolute() or ".." in Path(name).parts for name in paths):
        raise ValueError("repository observation exceeds bounded path contract")
    return paths


def _material(checkout: Path, paths: list[str]) -> dict:
    directories = {"."}
    for name in paths:
        directories.update(str(parent) for parent in Path(name).parents)
    return {"files": {name: stamp(checkout / name) for name in paths},
            "directories": {name: stamp(checkout / name) for name in sorted(directories)},
            "control": {str(path): stamp(path) for path in control_paths(checkout)}}


def _catalogue(session, payload: dict) -> dict:
    identity = digest_hex(payload, length=64)
    path = session.state_dir / "sessions" / session.session_id / "observation-catalogues" / (identity + ".json")
    try:
        current = json.loads(path.read_text())
    except (OSError, ValueError):
        current = None
    if current != payload:
        write_json(path, payload)
    return {"identity": identity, "artifact": str(path), "checkout": payload["checkout"]}


def observe_repository(session, checkout: Path, prior: dict | None) -> tuple[dict, list[dict], dict]:
    checkout = checkout.resolve()
    old = None
    if prior:
        path = Path(prior["artifact"])
        expected = session.state_dir / "sessions" / session.session_id / "observation-catalogues"
        if not path.resolve().is_relative_to(expected.resolve()):
            raise ValueError("observation catalogue escapes session artifacts")
        old = json.loads(path.read_text())
        if digest_hex(old, length=64) != prior["identity"] or old.get("checkout") != str(checkout):
            raise ValueError("corrupt or misbound repository catalogue")
    enumerated = old is None
    paths = list(old["material"]["files"]) if old else _enumerate(checkout)
    material = _material(checkout, paths)
    events = []
    if old and material != old["material"]:
        previous = old["material"]
        if material["control"] != previous["control"]:
            events.append({"kind": "repository.control_changed", "checkout": str(checkout)})
        if material["directories"] != previous["directories"] or material["control"] != previous["control"]:
            paths = _enumerate(checkout)
            enumerated = True
            material = _material(checkout, paths)
        for name in sorted(set(material["files"]) | set(previous["files"])):
            if material["files"].get(name) != previous["files"].get(name):
                events.append({"kind": "file.changed", "checkout": str(checkout), "path": name})
    payload = {"schema_version": CATALOGUE_SCHEMA, "checkout": str(checkout), "material": material}
    # Byte-identical observations produce no file writes and no Store changes.
    return (_catalogue(session, payload) if old is None or material != old["material"] else prior,
            events, {"metadata_paths": sum(len(value) for value in material.values()),
                     "enumerated": enumerated})


def observe_artifact(path: Path, prior: dict | None = None) -> dict:
    """Selected artifact bytes; build and execution receipts remain owner facts."""
    current = stamp(path)
    if prior and prior.get("stamp") == current and prior.get("address") == str(path.resolve()):
        return prior
    if current is None or not path.is_file():
        return {"address": str(path.resolve()), "stamp": current, "status": "unavailable"}
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return {"address": str(path.resolve()), "stamp": current,
            "artifact_identity": "sha256:" + digest.hexdigest(),
            "status": "observed", "build_origin": "unknown"}


def observe_library(session, root: Path, prior: dict | None = None) -> dict:
    """Content-bound library inventory with metadata-only warm observation.

    A library can be a provider bundle outside a Git checkout. Filesystem
    enumeration is bootstrap/external-directory-drift work; unchanged files
    reuse exact byte digests, never just a path or repository revision.
    """
    root = root.resolve()
    old = None
    if prior:
        path = Path(prior["artifact"])
        expected = session.state_dir / "sessions" / session.session_id / "observation-catalogues"
        if not path.resolve().is_relative_to(expected.resolve()):
            raise ValueError("library catalogue escapes session artifacts")
        old = json.loads(path.read_text())
        if digest_hex(old, length=64) != prior["identity"] or old["checkout"] != str(root):
            raise ValueError("corrupt library catalogue")
    material = old.get("material", {}) if old else {}
    directories = material.get("directories", {})
    changed = not old or any(stamp(root / name) != value for name, value in directories.items())
    if changed:
        files, directories = [], {}
        for base, children, names in os.walk(root, followlinks=False):
            children[:] = sorted(name for name in children if name not in (".git", "target", ".mncs"))
            relative = Path(base).relative_to(root)
            directories[str(relative)] = stamp(Path(base))
            files.extend(str(relative / name) for name in names)
            if len(files) + len(directories) > MAX_PATHS:
                raise ValueError("library inventory exceeds bounded path contract")
    else:
        files = list(material["files"])
    artifacts = {name: observe_artifact(root / name, material.get("files", {}).get(name)) for name in sorted(files)}
    payload = {"schema_version": "mncs.environment.library-observation/1", "checkout": str(root),
               "material": {"files": artifacts, "directories": directories}}
    ref = _catalogue(session, payload) if not old or payload != old else dict(prior)
    ref["content_identity"] = "sha256:" + digest_hex({name: value.get("artifact_identity") for name, value in artifacts.items()}, length=64)
    return ref
