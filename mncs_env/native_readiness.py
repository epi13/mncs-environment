"""Native token-span selection for bounded provider readiness responses.

Process invocation and response policy remain Environment-owned Python
responsibilities. The native application validates one bounded JSON document
and returns spans for selected JSON Pointer values, avoiding a Python object
tree for the common small readiness response.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from . import toolchain

MAX_NATIVE_DOCUMENT_BYTES = 16 * 1024
MAX_NATIVE_POINTERS = 32
MAX_NATIVE_POINTER_BYTES = 1024
MAX_NATIVE_CHUNK_BYTES = 1024
NATIVE_STEP_BUDGET = 8_000_000
_MISSING = object()


class NativeReadinessError(ValueError):
    """The selected native readiness application did not return a valid result."""


def _toolchain_paths(session) -> tuple[Path, Path] | None:
    """Resolve the selected MNCS binary and Stdlib checkout from session state."""
    selector = getattr(session, "_selected_runtime_environment", None)
    if not callable(selector):
        return None
    try:
        runtime_environment = selector({})
    except (OSError, ValueError):
        return None
    binary_value = runtime_environment.get("MNCS_BIN")
    selected = session.snapshot.get("selected_checkouts", {})
    workspace_root = (session.snapshot.get("workspace") or {}).get("root")
    checkout = selected.get("mncs-stdlib") if isinstance(selected, dict) else None
    if not isinstance(binary_value, str) or not isinstance(checkout, dict):
        return None
    if not isinstance(workspace_root, str) or not workspace_root:
        return None
    try:
        binary = Path(binary_value).resolve(strict=True)
        stdlib = toolchain.selected_stdlib_root(Path(workspace_root), checkout)
    except (OSError, ValueError):
        return None
    return binary, stdlib / "library"


def _chunks(document: bytes) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    offset = 0
    while offset < len(document):
        end = min(offset + MAX_NATIVE_CHUNK_BYTES, len(document))
        while end > offset:
            try:
                text = document[offset:end].decode("utf-8")
                break
            except UnicodeDecodeError as error:
                if error.reason != "unexpected end of data" or error.end != end - offset:
                    raise NativeReadinessError("provider response bytes are not valid UTF-8") from error
                end = offset + error.start
        if end <= offset:
            raise NativeReadinessError("provider response contains an invalid UTF-8 sequence")
        chunks.append({"bytes": text, "length": end - offset})
        offset = end
    return chunks


def _can_project(document: bytes, pointers: list[str]) -> bool:
    if not 0 < len(document) <= MAX_NATIVE_DOCUMENT_BYTES:
        return False
    if not pointers or len(pointers) > MAX_NATIVE_POINTERS:
        return False
    return all(
        isinstance(pointer, str)
        and pointer.startswith("/")
        and len(pointer.encode("utf-8")) <= MAX_NATIVE_POINTER_BYTES
        for pointer in pointers
    )


def _descriptor(repository_root: Path, stdlib: Path) -> dict[str, Any]:
    descriptor_path = repository_root / "native-applications" / "readiness-tokens.json"
    try:
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise NativeReadinessError(f"native readiness descriptor is unavailable: {error}") from error
    source = (descriptor_path.parent / descriptor.get("source", "")).resolve(strict=True)
    local_library = (descriptor_path.parent / "../native").resolve(strict=True)
    descriptor["source"] = str(source)
    descriptor["libraries"] = [str(stdlib.resolve(strict=True)), str(local_library)]
    return descriptor


def project_json_pointers(
    document_text: str,
    pointers: list[str],
    *,
    session,
    timeout_seconds: float,
) -> dict[str, Any] | None:
    """Return selected JSON values from native spans, or ``None`` if out of scope.

    Oversized documents and pointer counts beyond the native app's bounded
    window remain on the established compatibility path. Once selected, a
    native toolchain or application failure is surfaced instead of silently
    dropping back to Python semantics.
    """
    if not isinstance(document_text, str):
        return None
    document = document_text.encode("utf-8")
    unique_pointers = list(dict.fromkeys(pointers))
    if not _can_project(document, unique_pointers):
        return None
    paths = _toolchain_paths(session)
    if paths is None:
        return None
    binary, stdlib = paths
    repository_root = Path(__file__).resolve().parents[1]
    descriptor = _descriptor(repository_root, stdlib)
    request = {
        "schema_version": "mncs.environment.readiness-token-request/1",
        "chunks": _chunks(document),
        "document_length": len(document),
        "pointers": [{"bytes": pointer} for pointer in unique_pointers],
    }
    state_root = Path(session.store.state_dir).resolve()
    binary_identity = hashlib.sha256(str(binary).encode("utf-8")).hexdigest()[:16]
    cache_directory = state_root / "native-applications" / f"readiness-tokens-{binary_identity}"
    cache_directory.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="mncs-env-readiness-") as temporary:
            root = Path(temporary)
            descriptor_path = root / "native-application.json"
            descriptor_path.write_text(
                json.dumps(descriptor, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            (root / "request.json").write_text(
                json.dumps(request, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment.update(session._selected_runtime_environment({}))
            command = [
                str(binary), "run-app", str(descriptor_path),
                "--cache-dir", str(cache_directory),
                "--grant-structured", "environment_readiness",
                "--step-budget", str(NATIVE_STEP_BUDGET),
                "--", "request.json", "result.json",
            ]
            try:
                completed = subprocess.run(
                    command,
                    cwd=root,
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=max(0.1, timeout_seconds),
                )
            except subprocess.TimeoutExpired as error:
                raise NativeReadinessError("native readiness projection timed out") from error
            if completed.returncode != 0:
                detail = completed.stderr.strip() or completed.stdout.strip() or "native application failed"
                raise NativeReadinessError(detail[-1000:])
            try:
                result = json.loads((root / "result.json").read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise NativeReadinessError(f"native readiness application returned no result: {error}") from error
    except NativeReadinessError:
        raise
    except OSError as error:
        raise NativeReadinessError(f"native readiness application could not run: {error}") from error

    if not isinstance(result, dict) or result.get("schema_version") != "mncs.environment.readiness-tokens/1":
        raise NativeReadinessError("native readiness application returned an incompatible result schema")
    if result.get("query_count") != len(unique_pointers):
        raise NativeReadinessError("native readiness application returned an incomplete projection")
    if result.get("valid_json") is not True:
        # Python's established parser accepts a few non-standard constants
        # (such as NaN); leave invalid or extension-shaped input on that
        # compatibility path so provider error semantics remain unchanged.
        return None
    if result.get("root_is_object") is not True:
        return {"_valid_json": True, "_root_is_object": False}

    found = result.get("found")
    supported = result.get("supported")
    starts = result.get("starts")
    ends = result.get("ends")
    if not all(isinstance(values, list) and len(values) >= len(unique_pointers)
               for values in (found, supported, starts, ends)):
        raise NativeReadinessError("native readiness application returned malformed token spans")
    if any(supported[index] is not True for index in range(len(unique_pointers))):
        return None
    values: dict[str, Any] = {"_valid_json": True, "_root_is_object": True}
    for index, pointer in enumerate(unique_pointers):
        if found[index] is not True:
            values[pointer] = _MISSING
            continue
        start = starts[index]
        end = ends[index]
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(document):
            raise NativeReadinessError("native readiness application returned an invalid token span")
        try:
            values[pointer] = json.loads(document[start:end].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise NativeReadinessError(f"native JSON token span was not an independent JSON value: {error}") from error
    return values


def missing_value() -> Any:
    return _MISSING
