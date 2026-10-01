"""Recovery backoff over the family remediation retry law.

Live observation stays truthful on every run; only the repeated recovery
ACTION is withheld when the failure identity is unchanged and the native
retry law says another immediate attempt has little value. The decision
(`retry_eligible` / `retry_delay_secs`) always executes in MNCS
(`mncs.commons.family.remediation.v1`); this module only projects host
observations to the law's inputs, counts consecutive attempts, and keeps
per-session backoff state. When the toolchain is unavailable the gate
fails open to an attempt (legacy behavior), never to suppression.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .identity import canonical_bytes, digest_hex

REMEDIATION_MODULE = "mncs.commons.family.remediation.v1"
REMEDIATION_SOURCE = "mncs/commons/family/remediation/v1.mncs"
DEFAULT_BASE_DELAY_SECS = 60
DEFAULT_MAX_DELAY_SECS = 900
MAX_BACKOFF_ENTRIES = 64
NATIVE_CALL_TIMEOUT_SECS = 30


def _selected_checkout_paths(session) -> dict[str, str]:
    selected = session.snapshot.get("selected_checkouts", {})
    if not isinstance(selected, dict):
        return {}
    root = session.snapshot.get("workspace", {}).get("root", "")
    paths = {}
    for name, record in selected.items():
        if not isinstance(record, dict):
            continue
        raw = Path(str(record.get("path", "")))
        if not raw.is_absolute() and root:
            raw = Path(str(root)) / raw
        paths[str(name)] = str(raw)
    return paths


def find_mncs_binary(session=None) -> str | None:
    """Locate the `mncs` CLI: explicit env, session toolchain, PATH."""
    override = os.environ.get("MNCS_BINARY")
    if override and Path(override).is_file():
        return override
    if session is not None:
        toolchain = session.snapshot.get("toolchain")
        if isinstance(toolchain, dict) and toolchain.get("checkout"):
            checkout = Path(str(toolchain["checkout"]))
            for candidate in (checkout / "target" / "release" / "mncs",
                              checkout / "target" / "debug" / "mncs"):
                if candidate.is_file():
                    return str(candidate)
    for candidate in ("mncs",):
        resolved = _which(candidate)
        if resolved:
            return resolved
    return None


def _which(name: str) -> str | None:
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(directory) / name
        if candidate.is_file():
            return str(candidate)
    return None


def find_commons_root(session=None) -> Path | None:
    """Locate the MNCS-Commons checkout: env, session checkouts, siblings."""
    override = os.environ.get("MNCS_COMMONS_ROOT")
    if override and Path(override).is_dir():
        return Path(override)
    if session is not None:
        paths = _selected_checkout_paths(session)
        for name in ("MNCS-Commons", "mncs-commons"):
            if paths.get(name) and Path(paths[name]).is_dir():
                return Path(paths[name])
        workspace_root = session.snapshot.get("workspace", {}).get("root")
        if workspace_root:
            for name in ("MNCS-Commons", "mncs-commons"):
                candidate = Path(str(workspace_root)) / name
                if candidate.is_dir():
                    return candidate
    binary = find_mncs_binary(session)
    if binary:
        # `<language-root>/target/<profile>/mncs` sits beside family checkouts.
        sibling = Path(binary).resolve().parents[2].parent
        for name in ("MNCS-Commons", "mncs-commons"):
            if (sibling / name).is_dir():
                return sibling / name
    return None


def language_library_for(binary: str, session=None) -> Path | None:
    """Library root for `mncs call`: session language checkout, else layout."""
    if session is not None:
        paths = _selected_checkout_paths(session)
        for name in ("mncs-language",):
            if paths.get(name) and (Path(paths[name]) / "library").is_dir():
                return Path(paths[name]) / "library"
        toolchain = session.snapshot.get("toolchain")
        if isinstance(toolchain, dict) and toolchain.get("checkout"):
            candidate = Path(str(toolchain["checkout"])) / "library"
            if candidate.is_dir():
                return candidate
    root = Path(binary).resolve().parents[2]
    candidate = root / "library"
    return candidate if candidate.is_dir() else None


def load_retry_policy(session=None) -> dict[str, Any]:
    """Retry delays: family policy file when readable, else contract defaults."""
    base, cap, source = DEFAULT_BASE_DELAY_SECS, DEFAULT_MAX_DELAY_SECS, "contract-default"
    commons = find_commons_root(session)
    if commons is not None:
        policy_file = commons / "family" / "remediation-policy-v1.json"
        try:
            policy = json.loads(policy_file.read_text())
            retry = policy.get("retry", {})
            file_base, file_cap = int(retry["base_delay_secs"]), int(retry["max_delay_secs"])
            if 0 < file_base <= file_cap <= 4294967295:
                base, cap, source = file_base, file_cap, str(policy_file)
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return {"base_delay_secs": base, "max_delay_secs": cap, "source": source}


def _typed_integers(*values: int) -> str:
    return json.dumps([{"integer": {"value": int(value)}} for value in values])


def native_call(binary: str, source: Path, libraries: list[Path],
                function: str, args_json: str) -> Any | None:
    """One `mncs call` against the remediation module; None when unavailable."""
    command = [binary, "call", str(source), "--module", REMEDIATION_MODULE,
               "--function", function, "--args-json", args_json]
    for library in libraries:
        command.extend(["--library", str(library)])
    try:
        completed = subprocess.run(command, capture_output=True, text=True,
                                   timeout=NATIVE_CALL_TIMEOUT_SECS, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        document = json.loads(completed.stdout)
    except ValueError:
        return None
    if document.get("status") != "returned":
        return None
    returned = document.get("call", {}).get("returned", [])
    return returned[0] if returned else None


def native_retry_eligible(binary: str, mesh: Path, language_library: Path,
                          base: int, cap: int, attempts: int, elapsed: int) -> bool | None:
    """Native `retry_eligible`; None when the toolchain cannot answer."""
    source = mesh / REMEDIATION_SOURCE
    if not source.is_file():
        return None
    value = native_call(binary, source, [language_library, mesh], "retry_eligible",
                        _typed_integers(base, cap, attempts, elapsed))
    if not isinstance(value, dict) or "boolean" not in value:
        return None
    return bool(value["boolean"].get("value"))


def native_retry_delay(binary: str, mesh: Path, language_library: Path,
                       base: int, cap: int, attempts: int) -> int | None:
    """Native `retry_delay_secs` for inspectable suppression notes."""
    source = mesh / REMEDIATION_SOURCE
    if not source.is_file():
        return None
    value = native_call(binary, source, [language_library, mesh], "retry_delay_secs",
                        _typed_integers(base, cap, attempts))
    if not isinstance(value, dict) or "integer" not in value:
        return None
    try:
        return int(value["integer"].get("value"))
    except (TypeError, ValueError):
        return None


def default_retry_gate(session) -> Any:
    """Build the production gate: native law, fail-open to attempt."""
    policy = load_retry_policy(session)
    binary = find_mncs_binary(session)
    commons = find_commons_root(session)
    mesh = commons / "src" / "mncs_commons" / "mesh" if commons else None
    language_library = language_library_for(binary, session) if binary else None
    native_ready = bool(binary and mesh and mesh.is_dir()
                        and language_library and (mesh / REMEDIATION_SOURCE).is_file())

    def gate(*, base: int, max_delay: int, attempts: int, elapsed: int) -> tuple[bool, dict[str, Any]]:
        detail: dict[str, Any] = {"native": False, "policy": policy["source"],
                                  "base_delay_secs": base, "max_delay_secs": max_delay,
                                  "attempts": attempts, "elapsed_secs": elapsed}
        if not native_ready:
            detail["fallback"] = "toolchain-unavailable: attempt (legacy behavior)"
            return True, detail
        assert binary and mesh and language_library
        eligible = native_retry_eligible(binary, mesh, language_library, base, max_delay, attempts, elapsed)
        if eligible is None:
            detail["fallback"] = "native-call-failed: attempt (legacy behavior)"
            return True, detail
        detail["native"] = True
        if not eligible:
            delay = native_retry_delay(binary, mesh, language_library, base, max_delay, attempts)
            detail["delay_secs"] = delay
            detail["retry_in_secs"] = max(0, delay - elapsed) if delay is not None else None
        return eligible, detail

    return gate


def canonical_observation_bytes(observation: dict[str, Any]) -> bytes:
    """Stable projection: predicates and structured diagnostics only.

    Timestamps, prose reasons (stderr tails embed volatile text), and
    unbounded provider blobs are excluded so volatile bytes never rotate
    an identity; code-level changes still do.
    """
    return canonical_bytes({"observation": observation.get("observation"),
                            "diagnostics": observation.get("provider_diagnostics")})


def provider_revision(binding: dict[str, Any] | None, checkout_heads: dict[str, str]) -> str:
    """Stable provider revision: binding identity plus checkout head."""
    if not binding:
        return "binding-missing"
    head = ""
    provider = str(binding.get("provider", ""))
    if provider and provider in checkout_heads:
        head = checkout_heads[provider]
    else:
        root = str(binding.get("provider_root", ""))
        for path, commit in checkout_heads.items():
            if root and (root == path or root.startswith(path.rstrip("/") + "/")):
                head = commit
                break
    return "|".join([str(binding.get("binding_id", "")), str(binding.get("address", "")),
                     str(binding.get("contract_revision", "")), head])


def failure_identity(observation: dict[str, Any], reconcile_binding: dict[str, Any] | None,
                     checkout_heads: dict[str, str] | None = None) -> dict[str, Any]:
    """Failure identity per `mncs.remediation/1`: ordered material + digest.

    Any meaningful change (service, status, code, canonical observation,
    provider revision, recovery availability) rotates the digest and
    re-enables recovery immediately.
    """
    material = [str(observation.get("identity", "")),
                str(observation.get("status", "")),
                str(observation.get("code", "")),
                hashlib.sha256(canonical_observation_bytes(observation)).hexdigest(),
                provider_revision(reconcile_binding, checkout_heads or {}),
                str((reconcile_binding or {}).get("availability", {}).get("status", "unknown"))]
    return {"digest": "fri_" + digest_hex({"kind": "failure-identity", "material": material}),
            "service": material[0], "status": material[1], "code": material[2],
            "observation_digest": material[3], "provider_revision": material[4],
            "recovery_available": material[5]}


def checkout_heads(session) -> dict[str, str]:
    """Map checkout path and provider name to observed head commit."""
    heads: dict[str, str] = {}
    selected = session.snapshot.get("selected_checkouts", {})
    root = session.snapshot.get("workspace", {}).get("root", "")
    if isinstance(selected, dict):
        for name, record in selected.items():
            if not isinstance(record, dict):
                continue
            head = str(record.get("head", ""))
            if not head:
                continue
            heads[str(name)] = head
            raw = Path(str(record.get("path", "")))
            if not raw.is_absolute() and root:
                raw = Path(str(root)) / raw
            heads[str(raw)] = head
    return heads


def backoff_state(session) -> dict[str, Any]:
    doctor = session.snapshot.get("doctor")
    if not isinstance(doctor, dict):
        return {}
    state = doctor.get("recovery_backoff")
    return dict(state) if isinstance(state, dict) else {}


def store_backoff_state(session, state: dict[str, Any]) -> None:
    doctor = session.snapshot.get("doctor")
    if not isinstance(doctor, dict):
        doctor = {"schema_version": "mncs.environment.doctor/1", "history": []}
        session.snapshot["doctor"] = doctor
    if len(state) > MAX_BACKOFF_ENTRIES:
        ordered = sorted(state.items(), key=lambda item: str(item[1].get("last_attempt_at", "")))
        state = dict(ordered[-MAX_BACKOFF_ENTRIES:])
    doctor["recovery_backoff"] = state


def seconds_since(iso: str, now: datetime) -> int:
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return 0
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max(0, int((now - then).total_seconds()))


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
