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
import re
import shutil
import subprocess
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .identity import digest_hex

SCHEMA = "mncs.environment.capability-binding/1"
DEFAULT_OUTPUT_LIMIT_BYTES = 64 * 1024
MAX_OUTPUT_LIMIT_BYTES = 2 * 1024 * 1024

#: Doctor's projection-health binding, consumed by structure conformance and
#: projection reconciliation. Single spelling; capability renames land here.
DOCTOR_PROJECTION_HEALTH = "mncs-doctor:projection-health"

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

# Proven orientation adapter retained until Atlas publishes its invocation.
# This is an explicit provider contract, never inferred from fingerprint files.
MANIFEST_BOOTSTRAP_INVOCATIONS = {
    ("mncs-atlas", "context-capsule"): {"kind": "python", "path": "registry/__main__.py"},
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
        elif resolved := shutil.which(candidate):
            return resolved
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
    fixed_argv: list[str] | None = None,
    fixed_env: dict[str, str] | None = None,
    timeout_seconds: int | None = None,
    working_directory: str | None = None,
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
        "fixed_argv": list(fixed_argv or []),
        "fixed_env": dict(fixed_env or {}),
        "timeout_seconds": timeout_seconds,
        "working_directory": working_directory,
        "effects": list(effects or ["read"]),
        "event_types": list(event_types if event_types is not None else ["unknown"]),
        "availability": {"status": "unknown", "reason": "not yet probed", "observed_at": None},
        "provenance": provenance or {},
    }


_PROTECTED_TOOLCHAIN_ENV = {
    "PATH", "HOME", "LD_PRELOAD", "LD_LIBRARY_PATH",
    "DYLD_INSERT_LIBRARIES", "PYTHONHOME", "PYTHONSTARTUP",
    "PYTHONINSPECT", "BASH_ENV", "ENV",
}


def _selected_repository_toolchain(
    toolchain: Any,
    workspace: Path,
    repository_roots: dict[str, Path] | None,
) -> str | None:
    """Resolve a repository toolchain only from the active checkout closure."""
    if not isinstance(toolchain, dict):
        return None
    repository = toolchain.get("repository")
    relative_raw = toolchain.get("path", ".")
    if not isinstance(repository, str) or not repository:
        return None
    if not isinstance(relative_raw, str) or not relative_raw:
        return None
    relative = Path(relative_raw)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    base = (repository_roots or {}).get(repository)
    if base is None:
        if repository_roots is not None:
            return None
        base = workspace / repository
    base = Path(base).resolve()
    candidate = (base / relative).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        return None
    return str(candidate) if candidate.exists() else None


def descriptor_invocation(
    entry: dict[str, Any],
    repo: Path,
    workspace: Path,
    repository_roots: dict[str, Path] | None = None,
) -> dict[str, Any]:
    """Resolve a provider-declared ``invocation`` block, if usable.

    Returns ``{"address", "toolchain_address", "toolchain_env",
    "fixed_argv", "adapter_library_paths",
    "addressing", "detail"}``. ``addressing`` is ``"descriptor"`` when the
    entry carries a usable declaration, else ``"none"`` with ``detail``
    naming the failure so availability can distinguish a missing
    declaration from an unresolvable one. Paths in ``path``
    are repo-relative; ``toolchain`` can be a workspace-relative file path
    or ``{"repository": id, "path": relative_path}`` to bind a file or
    checkout directory from the selected repository closure. It is exported
    under ``toolchain_env`` (default ``MNCS``) at invoke time. Anything
    unparsable or missing resolves to ``"none"`` -- never a guess.

    The optional ``adapter_library_paths`` field names repo-relative
    directories the provider adapter itself contributes to MNCS library
    resolution beyond obligation-declared roots (for example the test
    runner's private native root). Entries that are not contained
    directories are dropped; a malformed field never breaks addressing.
    """
    empty = {"address": None, "toolchain_address": None,
             "toolchain_env": None, "fixed_argv": [],
             "adapter_library_paths": [], "addressing": "none",
             "detail": None}

    def unusable(detail):
        return dict(empty, detail=detail)

    spec = entry.get("invocation")
    if not isinstance(spec, dict):
        return empty
    fixed_argv = spec.get("fixed_argv", [])
    if not isinstance(fixed_argv, list) or any(
        not isinstance(argument, str) for argument in fixed_argv
    ):
        return unusable("invocation fixed_argv malformed")
    kind = spec.get("kind")
    address: str | None = None
    relative = Path(str(spec.get("path", "")))
    if kind in ("executable", "python") and (
        relative.is_absolute() or ".." in relative.parts
        or not (repo / relative).resolve().is_relative_to(repo.resolve())
    ):
        return unusable("invocation path escapes provider checkout")
    if kind == "executable":
        candidate = repo / relative
        if candidate.is_file():
            address = str(candidate)
    elif kind == "python":
        candidate = repo / relative
        if candidate.is_file() and candidate.suffix == ".py":
            address = "python:" + str(candidate)
    elif kind == "binary":
        address = shutil.which(str(spec.get("name", "")))
    if address is None:
        return unusable("invocation target not present: %s"
                        % (spec.get("path") if kind != "binary"
                           else spec.get("name", "")))
    toolchain_address: str | None = None
    toolchain_env = str(spec.get("toolchain_env", "MNCS"))
    toolchain = spec.get("toolchain")
    if isinstance(toolchain, dict):
        toolchain_address = _selected_repository_toolchain(
            toolchain, workspace, repository_roots
        )
    elif isinstance(toolchain, str) and toolchain:
        raw = Path(toolchain)
        candidate = raw if raw.is_absolute() else workspace / raw
        if candidate.is_file():
            toolchain_address = str(candidate)
    if ((isinstance(toolchain, dict) or isinstance(toolchain, str) and toolchain)
            and toolchain_address is None):
        return unusable("toolchain unresolvable in selected closure: %r"
                        % (toolchain,))
    adapter_library_paths: list[str] = []
    raw_roots = spec.get("adapter_library_paths", [])
    if isinstance(raw_roots, list):
        base = repo.resolve()
        for root in raw_roots:
            if not isinstance(root, str) or not root:
                continue
            relative_root = Path(root)
            if relative_root.is_absolute() or ".." in relative_root.parts:
                continue
            try:
                candidate_root = (repo / relative_root).resolve()
                candidate_root.relative_to(base)
            except (OSError, ValueError):
                continue
            if candidate_root.is_dir():
                adapter_library_paths.append(str(candidate_root))
    return {"address": address, "toolchain_address": toolchain_address,
            "toolchain_env": toolchain_env, "fixed_argv": list(fixed_argv),
            "adapter_library_paths": adapter_library_paths,
            "addressing": "descriptor"}


def probe_availability(binding: dict[str, Any]) -> dict[str, Any]:
    """Observe whether the bound address is currently invocable (no side effects)."""
    address = binding.get("address")
    awaitable = dict(binding)
    target = (
        address[len("python:"):] if isinstance(address, str) and address.startswith("python:")
        else address
    )
    resolved = shutil.which(target) if isinstance(target, str) and "/" not in target else target
    python_script = isinstance(address, str) and address.startswith("python:")
    usable = bool(isinstance(resolved, str) and resolved and Path(resolved).is_file()
                  and os.access(resolved, os.R_OK if python_script else os.X_OK))
    toolchain = binding.get("toolchain_address")
    if toolchain and not Path(toolchain).exists():
        usable = False
        reason = f"bound toolchain is missing: {toolchain}"
        code = "toolchain-missing"
    elif not usable:
        if address is None and binding.get("entrypoint") == "undeclared":
            detail = (binding.get("provenance") or {}).get("addressing_detail")
            if detail:
                code = "provider-invocation-unresolvable"
                reason = (f"{binding.get('provider')} declares invocation for "
                          f"{binding.get('capability')} but it cannot resolve: {detail}")
            else:
                code = "provider-invocation-undeclared"
                reason = f"{binding.get('provider')} must publish an invocation descriptor for {binding.get('capability')}; source fingerprints are not commands"
        else:
            reason = f"no usable executable for entrypoint {binding.get('entrypoint')!r} at {address!r}"
            code = "executable-unavailable"
    else:
        reason = f"invocation substrate present at {address}; provider readiness not yet verified"
        code = "executable-present"
    if usable:
        awaitable["availability"] = {
            "status": "available",
            "reason": reason,
            "code": code,
            "verification": "substrate",
            "observed_at": utcnow(),
        }
    else:
        awaitable["availability"] = {
            "status": "unavailable",
            "reason": reason,
            "code": code,
            "verification": "substrate",
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
        language_root = root if root.name == "mncs-language" else root / "mncs-language"
        release_bin = language_root / "target" / "release" / "mncs"
        debug_bin = language_root / "target" / "debug" / "mncs"
        language_bin = (
            str(release_bin) if release_bin.is_file()
            else str(debug_bin) if debug_bin.is_file()
            else ""
        )
    bindings: list[dict[str, Any]] = []
    if repository_roots is None:
        try:
            repos = ([root] if (root / ".git").exists() else
                     sorted(path for path in root.iterdir() if path.is_dir() and not path.is_symlink()))
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
            *_from_verification_inventory(repo),
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


def _from_verification_inventory(repo: Path) -> list[dict[str, Any]]:
    """Bind safe, repository-owned Python integration obligations.

    The repository manifest names the inventory and each inventory entry
    names its executor. Environment exposes that declared executor as a
    capability; it does not reinterpret the test or verification semantics.
    Only a direct Python script owned by the same checkout is addressable.
    """
    manifest_path = repo / ".mncs" / "project.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    verification = manifest.get("verification") if isinstance(manifest, dict) else None
    inventory_name = (
        verification.get("obligation_inventory")
        if isinstance(verification, dict) else None
    )
    if not isinstance(inventory_name, str) or not inventory_name:
        return []
    relative_inventory = Path(inventory_name)
    if relative_inventory.is_absolute() or ".." in relative_inventory.parts:
        return []
    inventory_path = (repo / relative_inventory).resolve()
    try:
        inventory_path.relative_to(repo.resolve())
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    except (ValueError, OSError, json.JSONDecodeError):
        return []
    if not isinstance(inventory, dict) or inventory.get("repository") != manifest.get("repository"):
        return []
    obligations = inventory.get("obligations")
    if not isinstance(obligations, list):
        return []
    inventory_digest = digest_hex(inventory)
    out: list[dict[str, Any]] = []
    for obligation in obligations:
        if not isinstance(obligation, dict):
            continue
        identity = obligation.get("identity")
        executor = obligation.get("executor")
        if not isinstance(identity, str) or not identity or not isinstance(executor, dict):
            continue
        if executor.get("provider") != manifest.get("repository"):
            continue
        if executor.get("kind") != "external_integration":
            continue
        argv = executor.get("argv")
        if not isinstance(argv, list) or len(argv) < 2:
            continue
        interpreter = str(argv[0])
        suffix_by_interpreter = {"python3": ".py", "bash": ".sh"}
        suffix = suffix_by_interpreter.get(interpreter)
        if suffix is None:
            continue
        script_relative = Path(str(argv[1]))
        if (script_relative.is_absolute() or ".." in script_relative.parts
                or script_relative.suffix != suffix):
            continue
        script = (repo / script_relative).resolve()
        try:
            script.relative_to(repo.resolve())
        except ValueError:
            continue
        if not script.is_file():
            continue
        working_directory = str(executor.get("working_directory", "."))
        relative_working_directory = Path(working_directory)
        if relative_working_directory.is_absolute() or ".." in relative_working_directory.parts:
            continue
        working_root = (repo / relative_working_directory).resolve()
        try:
            working_root.relative_to(repo.resolve())
        except ValueError:
            continue
        if not working_root.is_dir():
            continue
        timeout = executor.get("timeout_seconds", 120)
        if type(timeout) is not int or timeout < 1 or timeout > 86400:
            continue
        entrypoint = str(executor.get("entrypoint", f"{interpreter} {script_relative.as_posix()}"))
        if interpreter == "python3":
            address = "python:" + str(script)
            fixed_argv = [str(item) for item in argv[2:]]
        else:
            address = shutil.which(interpreter)
            if not address:
                continue
            fixed_argv = [str(script), *[str(item) for item in argv[2:]]]
        revision = digest_hex({
            "inventory": inventory_digest,
            "obligation": obligation,
            "script": str(script_relative),
        })
        # Providers may attest verification-only effects: the executor
        # observes the checkout and writes solely to declared ephemeral
        # roots (plus session scratch). Anything else keeps write
        # effects and the owned-or-pristine bar. The attestation is
        # enforced, not trusted: verification confines post-run state.
        declared_effects = executor.get("effects")
        ephemeral = executor.get("ephemeral_roots")
        if declared_effects == ["verify"] and (
                ephemeral is None or (
                    isinstance(ephemeral, list) and all(
                        isinstance(root, str) and root
                        and not root.startswith("/")
                        and ".." not in Path(root).parts
                        for root in ephemeral))):
            effects = ["verify"]
        else:
            effects = ["write"]
            ephemeral = None
        capability = f"{manifest['repository']}:verification-executor/{identity}"
        out.append(bind(
            provider=str(manifest["repository"]),
            capability=capability,
            contract_revision=revision,
            entrypoint=entrypoint,
            address=address,
            effects=effects,
            event_types=["verification.completed"],
            provenance={
                "source": str(relative_inventory),
                "obligation_identity": identity,
                "executor_identity": digest_hex(executor),
                "inventory_revision": inventory.get("revision"),
                "working_directory": working_directory,
                "addressing": "declared-verification-inventory",
                "ephemeral_roots": sorted(set(ephemeral or [])),
            },
            provider_root=str(repo.resolve()),
            fixed_argv=fixed_argv,
            timeout_seconds=timeout,
            working_directory=str(working_root),
        ))
    return out


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
            ({name: Path(selected) for name, selected in repository_roots.items()}
             if repository_roots is not None else None),
        )
        if declared["addressing"] == "descriptor":
            address = declared["address"]
            addressing = "descriptor"
            entrypoint = entrypoint or address
        elif isinstance(entry.get("invocation"), dict):
            # A declared exact toolchain that cannot be resolved must not
            # silently fall through to the ambient PATH compiler.
            address = None
            addressing = "none"
        else:
            address = _address(entrypoint, workspace, language_bin)
            addressing = "bootstrap" if address else "none"
        effects = entry.get("effects", ["read"])
        if (not isinstance(effects, list) or not effects
                or any(effect not in {"read", "verify", "write", "execute", "publish"}
                       for effect in effects)):
            continue
        record = bind(
            provider=repository_id,
            capability=contract,
            contract_revision=str(entry.get("contract_revision", "unknown")),
            entrypoint=str(entrypoint) if entrypoint else "undeclared",
            address=address,
            toolchain_address=declared["toolchain_address"],
            toolchain_env=declared["toolchain_env"],
            fixed_argv=declared["fixed_argv"],
            effects=[str(effect) for effect in effects],
            event_types=["unknown"],
            provenance={"source": f"{repo.name}/family-semantic-contracts-v1.json",
                        "status": entry.get("status"), "addressing": addressing,
                        "adapter_library_paths": declared["adapter_library_paths"]},
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
        provides = []
    for entry in provides:
        if not isinstance(entry, dict):
            continue
        contract = entry.get("contract")
        if not isinstance(contract, str) or not contract:
            continue
        # A fingerprint names source evidence, not an invocation protocol.
        # Only provider-declared invocation blocks make a manifest callable.
        declared = descriptor_invocation(
            entry,
            repo,
            workspace,
            ({name: Path(selected) for name, selected in repository_roots.items()}
             if repository_roots is not None else None),
        )
        address: str | None = declared["address"]
        addressing = declared["addressing"]
        entrypoint = address or "undeclared"
        bootstrap = MANIFEST_BOOTSTRAP_INVOCATIONS.get((repository_id, contract))
        if not isinstance(entry.get("invocation"), dict) and bootstrap is not None:
            declared = descriptor_invocation({"invocation": bootstrap}, repo, workspace, repository_roots)
            address = declared["address"]
            addressing = "bootstrap" if address else "none"
            entrypoint = "mncs-registry-context"
        effects = entry.get("effects", ["read"])
        if (not isinstance(effects, list) or not effects
                or any(effect not in {"read", "verify", "write", "execute", "publish"}
                       for effect in effects)):
            continue
        record = bind(
            provider=repository_id,
            capability=f"{repository_id}:{contract}",
            contract_revision=str(entry.get("version", "unknown")),
            entrypoint=entrypoint,
            address=address,
            toolchain_address=declared["toolchain_address"],
            toolchain_env=declared["toolchain_env"],
            fixed_argv=declared["fixed_argv"],
            effects=list(effects),
            event_types=["unknown"],
            provenance={"source": f"{repo.name}/.mncs/project.json",
                        "kind": entry.get("kind"), "stability": entry.get("stability"),
                        "addressing": addressing, "manifest_tests": declared_tests,
                        "addressing_detail": declared.get("detail")},
            provider_root=str(repo),
        )
        out.append(record)
    out.extend(_manifest_test_bindings(
        repo, repository_id, payload, declared_tests, workspace, repository_roots
    ))
    return out


def _manifest_test_bindings(
    repo: Path,
    repository_id: str,
    manifest: dict[str, Any],
    tests: list[Any],
    workspace: Path,
    repository_roots: dict[str, Path | str] | None,
) -> list[dict[str, Any]]:
    """Bind repository-declared test commands to their selected checkout.

    Test commands are fixed argv, run from the provider checkout, and carry
    write effects because build tools may update local caches or test outputs.
    The session must therefore hold a claim scoped to this exact worktree.
    """
    manifest_revision = digest_hex(manifest)
    out: list[dict[str, Any]] = []
    for test in tests:
        if not isinstance(test, dict):
            continue
        identity = test.get("test")
        command = test.get("command")
        argv = command.get("argv") if isinstance(command, dict) else None
        if (not isinstance(identity, str) or not identity
                or not isinstance(argv, list) or not argv
                or not all(isinstance(item, str) and item for item in argv)):
            continue
        address = shutil.which(argv[0])
        if not address:
            continue
        timeout = command.get("timeout_seconds", 120)
        if type(timeout) is not int or timeout < 1 or timeout > 86400:
            continue
        fixed_env = command.get("environment", {})
        if not isinstance(fixed_env, dict) or len(fixed_env) > 32:
            continue
        protected_env = {
            "PATH", "HOME", "LD_PRELOAD", "LD_LIBRARY_PATH",
            "DYLD_INSERT_LIBRARIES", "PYTHONHOME", "PYTHONSTARTUP",
            "PYTHONINSPECT", "BASH_ENV", "ENV",
        }
        if any(
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", key)
            or key in protected_env
            or not isinstance(value, str)
            or len(value) > 4096
            or "\x00" in value
            for key, value in fixed_env.items()
        ):
            continue
        toolchain = command.get("toolchain")
        toolchain_address: str | None = None
        toolchain_env: str | None = None
        if toolchain is not None:
            declared_env = command.get("toolchain_env")
            if (
                not isinstance(toolchain, dict)
                or not isinstance(declared_env, str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", declared_env)
                or declared_env in protected_env
            ):
                continue
            roots = (
                {name: Path(selected) for name, selected in repository_roots.items()}
                if repository_roots is not None else None
            )
            toolchain_address = _selected_repository_toolchain(
                toolchain, workspace, roots
            )
            if toolchain_address is None:
                continue
            toolchain_env = declared_env
        revision = digest_hex({"manifest": manifest_revision, "test": test})
        coverage = test.get("covers", [])
        invalidation_dependencies = test.get("invalidation_dependencies", [])
        out.append(bind(
            provider=repository_id,
            capability=f"{repository_id}:test/{identity}",
            contract_revision=revision,
            entrypoint=" ".join(argv),
            address=address,
            effects=["write"],
            event_types=["verification.completed"],
            provenance={
                "source": f"{repo.name}/.mncs/project.json",
                "kind": "manifest-test-command",
                "test_identity": identity,
                "coverage": list(coverage) if isinstance(coverage, list) else [],
                "invalidation_dependencies": (
                    list(invalidation_dependencies)
                    if isinstance(invalidation_dependencies, list) else []
                ),
                "addressing": "fixed-manifest-argv",
            },
            provider_root=str(repo.resolve()),
            fixed_argv=[str(item) for item in argv[1:]],
            fixed_env={str(key): str(value) for key, value in fixed_env.items()},
            toolchain_address=toolchain_address,
            toolchain_env=toolchain_env,
            timeout_seconds=timeout,
            working_directory=str(repo.resolve()),
        ))
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
    timeout_seconds: int | None = None,
    output_limit_bytes: int = DEFAULT_OUTPUT_LIMIT_BYTES,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Invoke a bound capability (transport only; authority checked by caller).

    Returns a result envelope; provider semantics stay provider-owned.
    Manifest-declared ``fixed_env`` entries are applied first, then the
    binding-resolved toolchain address, then explicit invocation overrides.
    """
    output_limit_bytes = validate_output_limit_bytes(output_limit_bytes)
    address = binding.get("address")
    if not address:
        raise CapabilityError(f"capability {binding.get('capability')} has no bound address")
    if address.startswith("python:"):
        script = address[len("python:"):]
        if script.endswith("__main__.py") and Path(script).is_file():
            command = [sys.executable, "-m", Path(script).parent.name, *binding.get("fixed_argv", []), *argv]
        elif script.endswith(".py") and Path(script).is_file():
            command = [sys.executable, script, *binding.get("fixed_argv", []), *argv]
        else:
            module = script.replace("python -m ", "").split()[0]
            command = [sys.executable, "-m", module, *binding.get("fixed_argv", []), *argv]
    else:
        command = [address, *binding.get("fixed_argv", []), *argv]
    if cwd is None:
        cwd = binding.get("working_directory") or binding.get("provider_root")
    child_env: dict[str, str] | None = None
    toolchain_address = binding.get("toolchain_address")
    toolchain_env = binding.get("toolchain_env")
    fixed_env = binding.get("fixed_env", {})
    if fixed_env or (toolchain_address and toolchain_env) or env:
        import os
        child_env = dict(os.environ)
        if fixed_env:
            child_env.update({str(key): str(value) for key, value in fixed_env.items()})
        if toolchain_address and toolchain_env:
            child_env[str(toolchain_env)] = str(toolchain_address)
        if env:
            child_env.update(env)
    try:
        effective_timeout = timeout_seconds or binding.get("timeout_seconds") or 120
        return _run_bounded(
            binding, command, cwd=str(cwd) if cwd else None,
            timeout_seconds=effective_timeout, output_limit_bytes=output_limit_bytes,
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
    import os
    import selectors
    import signal
    import time

    head_each = output_limit_bytes // 2
    deadline = time.monotonic() + timeout_seconds
    process = subprocess.Popen(
        command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, start_new_session=(os.name == "posix"),
    )
    assert process.stdout is not None and process.stderr is not None
    os.set_blocking(process.stdout.fileno(), False)
    os.set_blocking(process.stderr.fileno(), False)
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
                try:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    continue
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
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
        process.wait()
        selector.close()
        process.stdout.close()
        process.stderr.close()
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
