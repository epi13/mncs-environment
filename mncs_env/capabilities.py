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
DEFAULT_OUTPUT_LIMIT_BYTES = 64 * 1024
MAX_OUTPUT_LIMIT_BYTES = 2 * 1024 * 1024

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


def validate_output_limit_bytes(value: int) -> int:
    """Validate an explicit, bounded capability-output capture window."""
    if type(value) is not int or value < 1 or value > MAX_OUTPUT_LIMIT_BYTES:
        raise CapabilityError(
            f"output_limit_bytes must be an integer between 1 and {MAX_OUTPUT_LIMIT_BYTES}"
        )
    return value


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
    toolchain_address: str | None = None,
    toolchain_env: str | None = None,
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
        "toolchain_address": toolchain_address,
        "toolchain_env": toolchain_env,
        "effects": list(effects or ["read"]),
        "event_types": list(event_types if event_types is not None else ["unknown"]),
        "availability": {"status": "unknown", "reason": "not yet probed", "observed_at": None},
        "provenance": provenance or {},
    }


def descriptor_invocation(
    entry: dict[str, Any],
    repo: Path,
    workspace: Path,
    repository_roots: dict[str, Path] | None = None,
) -> dict[str, Any]:
    """Resolve a provider-declared ``invocation`` block, if usable.

    Returns ``{"address", "toolchain_address", "toolchain_env",
    "addressing"}``. ``addressing`` is ``"descriptor"`` when the entry
    carries a usable declaration, else ``"none"``. Paths in ``path``
    are repo-relative; ``toolchain`` can be a workspace-relative file path
    or ``{"repository": id, "path": relative_path}`` to bind a file or
    checkout directory from the selected repository closure. It is exported
    under ``toolchain_env`` (default ``MNCS``) at invoke time. Anything
    unparsable or missing resolves to ``"none"`` -- never a guess.
    """
    empty = {"address": None, "toolchain_address": None,
             "toolchain_env": None, "addressing": "none"}
    spec = entry.get("invocation")
    if not isinstance(spec, dict):
        return empty
    kind = spec.get("kind")
    address: str | None = None
    if kind == "executable":
        candidate = repo / str(spec.get("path", ""))
        if candidate.is_file():
            address = str(candidate)
    elif kind == "python":
        candidate = repo / str(spec.get("path", ""))
        if candidate.is_file() and candidate.suffix == ".py":
            address = "python:" + str(candidate)
    elif kind == "binary":
        address = shutil.which(str(spec.get("name", "")))
    if address is None:
        return empty
    toolchain_address: str | None = None
    toolchain_env = str(spec.get("toolchain_env", "MNCS"))
    toolchain = spec.get("toolchain")
    if isinstance(toolchain, dict):
        repository = str(toolchain.get("repository", ""))
        relative = Path(str(toolchain.get("path", ".")))
        base = (repository_roots or {}).get(repository)
        if base is None and repository:
            base = workspace / repository
        if base is not None and repository and not relative.is_absolute():
            base = Path(base).resolve()
            candidate = (base / relative).resolve()
            try:
                candidate.relative_to(base)
            except ValueError:
                candidate = None
            if candidate is not None and candidate.exists():
                toolchain_address = str(candidate)
    elif isinstance(toolchain, str) and toolchain:
        raw = Path(toolchain)
        candidate = raw if raw.is_absolute() else workspace / raw
        if candidate.is_file():
            toolchain_address = str(candidate)
    return {"address": address, "toolchain_address": toolchain_address,
            "toolchain_env": toolchain_env, "addressing": "descriptor"}


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


def discover_capabilities(
    workspace_root: Path | str,
    *,
    repository_roots: dict[str, Path | str] | None = None,
    checkout_facts: dict[str, dict[str, Any]] | None = None,
    language_binary: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Build bindings from repository declarations.

    When ``repository_roots`` is supplied, it is the complete binding set:
    no workspace-root checkout or PATH toolchain fallback is considered.
    This lets a session bind declarations to its exact provider-selected
    worktrees while retaining broad discovery for non-campaign callers.
    """
    root = Path(workspace_root).resolve()
    if language_binary is not None:
        language_bin = str(Path(language_binary).resolve()) if Path(language_binary).is_file() else ""
    else:
        release_bin = root / "mncs-language" / "target" / "release" / "mncs"
        debug_bin = root / "mncs-language" / "target" / "debug" / "mncs"
        language_bin = (
            str(release_bin) if release_bin.is_file()
            else str(debug_bin) if debug_bin.is_file()
            else (shutil.which("mncs") or "")
        )
    bindings: list[dict[str, Any]] = []
    if repository_roots is None:
        try:
            repos = sorted(path for path in root.iterdir() if path.is_dir() and not path.is_symlink())
        except OSError:
            return bindings
    else:
        repos = []
        for repository, raw_path in sorted(repository_roots.items()):
            candidate = Path(raw_path)
            if candidate.is_symlink():
                raise CapabilityError(f"selected checkout for {repository} is a symbolic link")
            path = candidate.resolve()
            try:
                path.relative_to(root)
            except ValueError as error:
                raise CapabilityError(
                    f"selected checkout for {repository} escapes workspace root"
                ) from error
            if not path.is_dir():
                raise CapabilityError(f"selected checkout for {repository} is unavailable")
            repos.append(path)
    for repo in repos:
        discovered = [
            *_from_semantic_contracts(repo, root, language_bin, repository_roots),
            *_from_manifest(repo, root, language_bin, repository_roots),
        ]
        repository = next(
            (name for name, selected in (repository_roots or {}).items()
             if Path(selected).resolve() == repo),
            repo.name,
        )
        facts = (checkout_facts or {}).get(repository)
        if facts is not None:
            for binding in discovered:
                binding.setdefault("provenance", {})["checkout"] = {
                    key: facts.get(key)
                    for key in ("path", "branch", "head", "clean", "source_ref", "authoritative_head")
                }
        bindings.extend(discovered)
    bindings.sort(key=lambda item: (item["provider"], item["capability"]))
    return bindings


def _from_semantic_contracts(
    repo: Path,
    workspace: Path,
    language_bin: str,
    repository_roots: dict[str, Path | str] | None = None,
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
        declared = descriptor_invocation(
            entry,
            repo,
            workspace,
            {name: Path(selected) for name, selected in (repository_roots or {}).items()},
        )
        if declared["addressing"] == "descriptor":
            address = declared["address"]
            addressing = "descriptor"
        else:
            address = _address(entrypoint, workspace, language_bin)
            addressing = "bootstrap" if address else "none"
        record = bind(
            provider=repository_id,
            capability=contract,
            contract_revision=str(entry.get("contract_revision", "unknown")),
            entrypoint=str(entrypoint) if entrypoint else "undeclared",
            address=address,
            toolchain_address=declared["toolchain_address"],
            toolchain_env=declared["toolchain_env"],
            effects=["read"],
            event_types=["unknown"],
            provenance={"source": f"{repo.name}/family-semantic-contracts-v1.json",
                        "status": entry.get("status"), "addressing": addressing},
            provider_root=str(repo),
        )
        out.append(record)
    return out


def _from_manifest(
    repo: Path,
    workspace: Path,
    language_bin: str,
    repository_roots: dict[str, Path | str] | None = None,
) -> list[dict[str, Any]]:
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
    declared_tests = contracts.get("tests") if isinstance(contracts, dict) else None
    if not isinstance(declared_tests, list):
        declared_tests = []
    if not isinstance(provides, list):
        return []
    for entry in provides:
        if not isinstance(entry, dict):
            continue
        contract = entry.get("contract")
        if not isinstance(contract, str) or not contract:
            continue
        # Data-driven addressing: a declared invocation block wins, then a
        # fingerprint source that is an existing executable module becomes
        # the invocation address. No per-provider switch statement;
        # undeclared contracts stay address-less.
        declared = descriptor_invocation(
            entry,
            repo,
            workspace,
            {name: Path(selected) for name, selected in (repository_roots or {}).items()},
        )
        address: str | None = declared["address"]
        addressing = declared["addressing"]
        entrypoint = "undeclared"
        sources = entry.get("fingerprint_sources")
        if address is None and isinstance(sources, list):
            for source in sources:
                if not isinstance(source, str) or not source.endswith(".py"):
                    continue
                candidate = repo / source
                if candidate.is_file():
                    address = "python:" + str(candidate)
                    entrypoint = f"python:{source}"
                    addressing = "descriptor-fingerprint"
                    break
        record = bind(
            provider=repository_id,
            capability=f"{repository_id}:{contract}",
            contract_revision=str(entry.get("version", "unknown")),
            entrypoint=entrypoint,
            address=address,
            toolchain_address=declared["toolchain_address"],
            toolchain_env=declared["toolchain_env"],
            effects=["read"],
            event_types=["unknown"],
            provenance={"source": f"{repo.name}/.mncs/project.json",
                        "kind": entry.get("kind"), "stability": entry.get("stability"),
                        "addressing": addressing, "manifest_tests": declared_tests},
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
    output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Invoke a bound capability (transport only; authority checked by caller).

    Returns a result envelope; provider semantics stay provider-owned.
    A binding-resolved ``toolchain_address`` is exported under
    ``toolchain_env``; explicit ``env`` entries win over it.
    """
    output_limit_bytes = validate_output_limit_bytes(output_limit_bytes)
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
    child_env: dict[str, str] | None = None
    toolchain_address = binding.get("toolchain_address")
    toolchain_env = binding.get("toolchain_env")
    if (toolchain_address and toolchain_env) or env:
        import os
        child_env = dict(os.environ)
        if toolchain_address and toolchain_env:
            child_env[str(toolchain_env)] = str(toolchain_address)
        if env:
            child_env.update(env)
    try:
        return _run_bounded(
            binding, command, cwd=str(cwd) if cwd else None,
            timeout_seconds=timeout_seconds, output_limit_bytes=output_limit_bytes,
            env=child_env,
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
    env: dict[str, str] | None = None,
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
        env=env,
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
