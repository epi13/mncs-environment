"""Native token-span selection for bounded provider readiness responses.

Process invocation and response policy remain Environment-owned Python
responsibilities. The native application validates one bounded JSON document
and returns spans for selected JSON Pointer values, avoiding a Python object
tree for the common small readiness response.
"""

from __future__ import annotations

import hashlib
import errno
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
MAX_SUMMARY_ITEMS = 256
NATIVE_STEP_BUDGET = 8_000_000
_MISSING = object()


class NativeApplicationError(ValueError):
    """A selected Environment native application did not return a valid result."""


# Kept as a compatibility name for existing readiness projection callers.
NativeReadinessError = NativeApplicationError


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


def _descriptor(repository_root: Path, stdlib: Path, name: str) -> dict[str, Any]:
    descriptor_path = repository_root / "native-applications" / name
    try:
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise NativeApplicationError(f"native application descriptor is unavailable: {error}") from error
    source = (descriptor_path.parent / descriptor.get("source", "")).resolve(strict=True)
    local_library = (descriptor_path.parent / "../native").resolve(strict=True)
    descriptor["source"] = str(source)
    descriptor["libraries"] = [str(stdlib.resolve(strict=True)), str(local_library)]
    return descriptor


def _run_native_application(
    *,
    session,
    binary: Path,
    stdlib: Path,
    descriptor_name: str,
    request: dict[str, Any],
    expected_schema: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    repository_root = Path(__file__).resolve().parents[1]
    descriptor = _descriptor(repository_root, stdlib, descriptor_name)
    state_root = Path(session.store.state_dir).resolve()
    binary_identity = hashlib.sha256(str(binary).encode("utf-8")).hexdigest()[:16]
    cache_root = Path(os.environ.get(
        "MNCS_NATIVE_APPLICATION_CACHE_DIR", state_root / "native-applications",
    ))
    # Admission keys already bind source, libraries and interface. Keep one
    # bounded storage pool across descriptors and toolchain namespaces.
    cache_directory = cache_root / binary_identity
    try:
        with tempfile.TemporaryDirectory(
            prefix=f"mncs-env-{Path(descriptor_name).stem}-",
        ) as temporary:
            root = Path(temporary)
            try:
                cache_directory.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                if error.errno not in {errno.EROFS, errno.EACCES, errno.EPERM}:
                    raise
                # Compiled cache is disposable. A protected service can still
                # execute the exact selected native program using its existing
                # private request workspace; no host-semantic fallback occurs.
                cache_directory = root / "cache"
                cache_directory.mkdir()
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
            environment["MNCS_NATIVE_APPLICATION_CACHE_STORAGE_ROOT"] = str(cache_directory.parent)
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
                    stdin=subprocess.DEVNULL,
                    text=True,
                    check=False,
                    timeout=max(0.1, timeout_seconds),
                )
            except subprocess.TimeoutExpired as error:
                raise NativeApplicationError(
                    f"native application timed out ({descriptor_name}, {timeout_seconds}s)"
                ) from error
            if completed.returncode != 0:
                detail = completed.stderr.strip() or completed.stdout.strip() or "native application failed"
                raise NativeApplicationError(detail[-1000:])
            try:
                result = json.loads((root / "result.json").read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise NativeApplicationError(f"native application returned no result: {error}") from error
    except NativeReadinessError:
        raise
    except OSError as error:
        raise NativeApplicationError(f"native application could not run: {error}") from error
    if not isinstance(result, dict) or result.get("schema_version") != expected_schema:
        raise NativeApplicationError("native application returned an incompatible result schema")
    return result


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
    request = {
        "schema_version": "mncs.environment.readiness-token-request/1",
        "chunks": _chunks(document),
        "document_length": len(document),
        "pointers": [{"bytes": pointer} for pointer in unique_pointers],
    }
    result = _run_native_application(
        session=session,
        binary=binary,
        stdlib=stdlib,
        descriptor_name="readiness-tokens.json",
        request=request,
        expected_schema="mncs.environment.readiness-tokens/1",
        timeout_seconds=timeout_seconds,
    )
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


def project_readiness_summary(
    snapshot: dict[str, Any],
    bindings: list[dict[str, Any]],
    services: list[dict[str, Any]],
    *,
    session,
    timeout_seconds: float = 30,
) -> dict[str, Any] | None:
    """Classify a bounded readiness projection in MNCS, or return ``None``.

    ``None`` is limited to missing toolchain or input outside the native shape.
    Once a native application has been selected, execution and result failures
    surface instead of silently retrying the host classifier.
    """
    if not isinstance(snapshot, dict) or not isinstance(bindings, list) or not isinstance(services, list):
        return None
    requirements = snapshot.get("requirements", {})
    if not isinstance(requirements, dict):
        return None
    required = requirements.get("required_capabilities", [])
    declared_services = requirements.get("services", [])
    workspace = snapshot.get("workspace", {})
    if not isinstance(workspace, dict):
        return None
    scan = workspace.get("scan", {})
    if not isinstance(scan, dict):
        return None
    if not all(isinstance(rows, list) and len(rows) <= MAX_SUMMARY_ITEMS
               for rows in (bindings, required, services, declared_services)):
        return None

    binding_rows: list[dict[str, str]] = []
    for item in bindings:
        if not isinstance(item, dict) or not isinstance(item.get("capability"), str):
            return None
        availability = item.get("availability", {})
        if not isinstance(availability, dict):
            return None
        status = availability.get("status")
        if status is None:
            status = "unknown"
        if not isinstance(status, str):
            return None
        identity = item["capability"]
        if len(identity.encode("utf-8")) > 256 or len(status.encode("utf-8")) > 32:
            return None
        binding_rows.append({"identity": identity, "status": status})

    required_rows: list[dict[str, str]] = []
    for identity in required:
        if not isinstance(identity, str) or len(identity.encode("utf-8")) > 256:
            return None
        required_rows.append({"identity": identity})

    service_rows: list[dict[str, Any]] = []
    for item in services:
        if not isinstance(item, dict):
            return None
        identity = item.get("identity")
        status = item.get("status")
        if not isinstance(identity, str) or not isinstance(status, str):
            return None
        if len(identity.encode("utf-8")) > 256 or len(status.encode("utf-8")) > 32:
            return None
        service_rows.append({
            "identity": identity,
            "status": status,
            "required": bool(item.get("required")),
        })

    requirement_rows: list[dict[str, Any]] = []
    for item in declared_services:
        if not isinstance(item, dict):
            return None
        identity = item.get("identity")
        if not isinstance(identity, str) or len(identity.encode("utf-8")) > 256:
            return None
        requirement_rows.append({
            "identity": identity,
            "required": bool(item.get("required", True)),
        })

    scan_status = scan.get("status")
    if scan_status is None:
        scan_status = "unknown"
    if not isinstance(scan_status, str) or len(scan_status.encode("utf-8")) > 32:
        return None
    paths = _toolchain_paths(session)
    if paths is None:
        return None
    binary, stdlib = paths
    request = {
        "schema_version": "mncs.environment.readiness-summary-request/1",
        "bindings": binding_rows,
        "required_capabilities": required_rows,
        "services": service_rows,
        "required_services": requirement_rows,
        "workspace_scan_status": scan_status,
    }
    result = _run_native_application(
        session=session,
        binary=binary,
        stdlib=stdlib,
        descriptor_name="readiness-summary.json",
        request=request,
        expected_schema="mncs.environment.readiness-summary-result/1",
        timeout_seconds=timeout_seconds,
    )
    if result.get("valid") is not True:
        raise NativeReadinessError("native readiness summary rejected a bounded request")
    status = result.get("status")
    if not isinstance(status, str) or status not in {"ready", "degraded", "blocked"}:
        raise NativeReadinessError("native readiness summary returned an invalid status")

    def count(name: str, maximum: int) -> int:
        value = result.get(name)
        if type(value) is not int or not 0 <= value <= maximum:
            raise NativeReadinessError(f"native readiness summary returned an invalid {name}")
        return value

    def text_rows(name: str, row_count: int) -> list[str]:
        rows = result.get(name)
        if not isinstance(rows, list) or len(rows) < row_count:
            raise NativeReadinessError(f"native readiness summary returned incomplete {name}")
        values = []
        for row in rows[:row_count]:
            if not isinstance(row, dict) or not isinstance(row.get("bytes"), str):
                raise NativeReadinessError(f"native readiness summary returned malformed {name}")
            values.append(row["bytes"])
        return values

    unavailable_count = count("unavailable_count", len(bindings))
    unknown_count = count("unknown_count", len(bindings))
    return {
        "status": status,
        "available_count": count("available_count", len(bindings)),
        "unavailable_count": unavailable_count,
        "unknown_count": unknown_count,
        "required_unavailable": text_rows(
            "required_unavailable", count("required_unavailable_count", len(required)),
        ),
        "required_unknown": text_rows(
            "required_unknown", count("required_unknown_count", len(required)),
        ),
        "blocking": text_rows(
            "blocking", count("blocking_count", len(required) + len(services) + len(declared_services) + 1),
        ),
    }
