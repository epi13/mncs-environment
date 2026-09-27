"""Capability discovery, binding, availability, and invocation plumbing.

Discovery is data-driven: repository-owned `family-semantic-contracts-v1.json`
declarations and `.mncs/project.json` manifests become CapabilityBindings.
Only addressing (turning a declared entrypoint spelling into an executable
path) uses a small explicit bootstrap table, recorded as a pressure for
canonical provider addressing. Invocation is transport only: authority is
checked by the session before this module ever spawns a process.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .identity import digest_hex

SCHEMA = "mncs.environment.capability-binding/1"

# Bootstrap addressing for declared entrypoint spellings. Each entry maps a
# known spelling to candidate executables (checked in order). Providers
# should eventually expose canonical addressing; until then this table is
# the explicit, tested seam — not scattered per-caller shell knowledge.
ENTRYPOINT_CANDIDATES: dict[str, list[str]] = {
    "mncs test": ["{language_bin}"],
    "mncs call": ["{language_bin}"],
    "mncs-test": ["{workspace}/mncs-test/bin/mncs-test"],
    "mncs-debug": ["{workspace}/mncs-debug/bin/mncs-debug"],
    "mncs-registry-context": ["{workspace}/mncs-atlas/registry/__main__.py"],
}


class CapabilityError(ValueError):
    """Raised for unusable capability declarations or invocations."""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def resolve_executable(candidates: list[str]) -> str | None:
    for candidate in candidates:
        if "/" in candidate:
            if Path(candidate).is_file():
                return candidate
        elif shutil.which(candidate):
            return candidate
    return None


def _binding_id(provider: str, capability: str, contract_revision: str) -> str:
    return "cap_" + digest_hex(
        {"kind": "capability-binding", "provider": provider, "capability": capability,
         "revision": contract_revision}
    )


def bind(
    *,
    provider: str,
    capability: str,
    contract_revision: str,
    entrypoint: str,
    address: str | None,
    effects: list[str] | None = None,
    event_types: list[str] | None = None,
    provenance: dict[str, Any] | None = None,
    provider_root: str | None = None,
) -> dict[str, Any]:
    """Construct a binding record (availability is observed separately)."""
    return {
        "schema_version": SCHEMA,
        "binding_id": _binding_id(provider, capability, contract_revision),
        "provider": provider,
        "provider_root": provider_root,
        "capability": capability,
        "contract_revision": contract_revision,
        "entrypoint": entrypoint,
        "address": address,
        "effects": list(effects or ["read"]),
        "event_types": list(event_types if event_types is not None else ["unknown"]),
        "availability": {"status": "unknown", "reason": "not yet probed", "observed_at": None},
        "provenance": provenance or {},
    }


def probe_availability(binding: dict[str, Any]) -> dict[str, Any]:
    """Observe whether the bound address is currently invocable (no side effects)."""
    address = binding.get("address")
    awaitable = dict(binding)
    target = (
        address[len("python:"):] if isinstance(address, str) and address.startswith("python:")
        else address
    )
    if isinstance(target, str) and target and Path(target).is_file():
        awaitable["availability"] = {
            "status": "available",
            "reason": f"executable present at {address}",
            "observed_at": utcnow(),
        }
    else:
        awaitable["availability"] = {
            "status": "unavailable",
            "reason": f"no executable for entrypoint {binding.get('entrypoint')!r}",
            "observed_at": utcnow(),
        }
    return awaitable


def discover_capabilities(workspace_root: Path | str) -> list[dict[str, Any]]:
    """Build bindings from repository-owned declarations under workspace_root."""
    root = Path(workspace_root).resolve()
    release_bin = root / "mncs-language" / "target" / "release" / "mncs"
    language_bin = str(release_bin) if release_bin.is_file() else (shutil.which("mncs") or "")
    bindings: list[dict[str, Any]] = []
    try:
        repos = sorted(path for path in root.iterdir() if path.is_dir() and not path.is_symlink())
    except OSError:
        return bindings
    for repo in repos:
        bindings.extend(_from_semantic_contracts(repo, root, language_bin))
        bindings.extend(_from_manifest(repo, root, language_bin))
    bindings.sort(key=lambda item: (item["provider"], item["capability"]))
    return bindings


def _from_semantic_contracts(
    repo: Path, workspace: Path, language_bin: str
) -> list[dict[str, Any]]:
    path = repo / "family-semantic-contracts-v1.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    repository_id = str(payload.get("repository_id", repo.name))
    out = []
    provides = payload.get("provides")
    if not isinstance(provides, list):
        return []
    for entry in provides:
        if not isinstance(entry, dict):
            continue
        contract = entry.get("contract_identity")
        if not isinstance(contract, str) or not contract:
            continue
        entrypoint = entry.get("canonical_entrypoint")
        record = bind(
            provider=repository_id,
            capability=contract,
            contract_revision=str(entry.get("contract_revision", "unknown")),
            entrypoint=str(entrypoint) if entrypoint else "undeclared",
            address=_address(entrypoint, workspace, language_bin),
            effects=["read"],
            event_types=["unknown"],
            provenance={"source": f"{repo.name}/family-semantic-contracts-v1.json",
                        "status": entry.get("status")},
            provider_root=str(repo),
        )
        out.append(record)
    return out


def _from_manifest(repo: Path, workspace: Path, language_bin: str) -> list[dict[str, Any]]:
    path = repo / ".mncs" / "project.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, dict):
        return []
    repository_id = str(payload.get("repository", repo.name))
    out = []
    contracts = payload.get("contracts", {})
    provides = contracts.get("provides") if isinstance(contracts, dict) else None
    if not isinstance(provides, list):
        return []
    for entry in provides:
        if not isinstance(entry, dict):
            continue
        contract = entry.get("contract")
        if not isinstance(contract, str) or not contract:
            continue
        # Data-driven addressing: a fingerprint source that is an existing
        # executable module becomes the invocation address. No per-provider
        # switch statement; undeclared contracts stay address-less.
        address: str | None = None
        entrypoint = "undeclared"
        sources = entry.get("fingerprint_sources")
        if isinstance(sources, list):
            for source in sources:
                if not isinstance(source, str) or not source.endswith(".py"):
                    continue
                candidate = repo / source
                if candidate.is_file():
                    address = "python:" + str(candidate)
                    entrypoint = f"python:{source}"
                    break
        record = bind(
            provider=repository_id,
            capability=f"{repository_id}:{contract}",
            contract_revision=str(entry.get("version", "unknown")),
            entrypoint=entrypoint,
            address=address,
            effects=["read"],
            event_types=["unknown"],
            provenance={"source": f"{repo.name}/.mncs/project.json",
                        "kind": entry.get("kind"), "stability": entry.get("stability")},
            provider_root=str(repo),
        )
        out.append(record)
    return out


def _address(entrypoint: Any, workspace: Path, language_bin: str) -> str | None:
    if not isinstance(entrypoint, str):
        return None
    for spelling, candidates in ENTRYPOINT_CANDIDATES.items():
        if entrypoint == spelling or entrypoint.startswith(spelling + " ") or entrypoint.startswith(spelling + "/"):
            expanded = [
                candidate.format(language_bin=language_bin, workspace=str(workspace))
                for candidate in candidates
            ]
            if entrypoint == "mncs-registry-context":
                return "python:" + expanded[0]
            return resolve_executable(expanded)
    if entrypoint.startswith("python -m ") or entrypoint.startswith("python:"):
        return "python:" + entrypoint
    return resolve_executable([entrypoint.split()[0]])


def invoke(
    binding: dict[str, Any],
    argv: list[str],
    *,
    cwd: Path | str | None = None,
    timeout_seconds: int = 120,
    output_limit_bytes: int = 65536,
) -> dict[str, Any]:
    """Invoke a bound capability (transport only; authority checked by caller).

    Returns a result envelope; provider semantics stay provider-owned.
    """
    address = binding.get("address")
    if not address:
        raise CapabilityError(f"capability {binding.get('capability')} has no bound address")
    if address.startswith("python:"):
        script = address[len("python:"):]
        if script.endswith("__main__.py") and Path(script).is_file():
            command = ["python3", "-m", Path(script).parent.name, *argv]
        elif script.endswith(".py") and Path(script).is_file():
            command = ["python3", script, *argv]
        else:
            module = script.replace("python -m ", "").split()[0]
            command = ["python3", "-m", module, *argv]
    else:
        command = [address, *argv]
    if cwd is None and binding.get("provider_root"):
        cwd = binding["provider_root"]
    try:
        return _run_bounded(
            binding, command, cwd=str(cwd) if cwd else None,
            timeout_seconds=timeout_seconds, output_limit_bytes=output_limit_bytes,
        )
    except OSError as error:
        return {
            "binding_id": binding.get("binding_id"),
            "capability": binding.get("capability"),
            "status": "transport-error",
            "returncode": None,
            "stdout": "",
            "stderr": str(error),
            "truncated": False,
        }


def _run_bounded(
    binding: dict[str, Any],
    command: list[str],
    *,
    cwd: str | None,
    timeout_seconds: int,
    output_limit_bytes: int,
) -> dict[str, Any]:
    """Run a provider with streamed, capped output (never buffer unbounded).

    stdout/stderr are read incrementally; each stream keeps a head window
    and drops the middle, so a chatty provider cannot exhaust memory. Exit
    status and tail diagnostics are always preserved.
    """
    import selectors
    import time

    head_each = output_limit_bytes // 2
    deadline = time.monotonic() + timeout_seconds
    process = subprocess.Popen(
        command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert process.stdout is not None and process.stderr is not None
    buffers = {process.stdout: bytearray(), process.stderr: bytearray()}
    totals = {process.stdout: 0, process.stderr: 0}
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    selector.register(process.stderr, selectors.EVENT_READ)
    timed_out = False
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            for key, _ in selector.select(timeout=min(remaining, 0.5)):
                chunk = key.fileobj.read(65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                buffer = buffers[key.fileobj]
                totals[key.fileobj] += len(chunk)
                room = head_each - len(buffer)
                if room > 0:
                    buffer.extend(chunk[:room])
            if process.poll() is not None and not selector.get_map():
                break
    finally:
        if timed_out or process.poll() is None:
            process.kill()
        process.wait()
        selector.close()
    stdout = buffers[process.stdout].decode("utf-8", "replace")
    stderr = buffers[process.stderr].decode("utf-8", "replace")
    truncated = totals[process.stdout] > head_each or totals[process.stderr] > head_each
    if timed_out:
        return {
            "binding_id": binding.get("binding_id"),
            "capability": binding.get("capability"),
            "status": "timeout",
            "returncode": None,
            "stdout": stdout,
            "stderr": (stderr + f"\nexceeded {timeout_seconds}s").strip(),
            "truncated": truncated,
        }
    return {
        "binding_id": binding.get("binding_id"),
        "capability": binding.get("capability"),
        "status": "ok" if process.returncode == 0 else "failed",
        "returncode": process.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": truncated,
    }
