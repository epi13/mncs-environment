"""Entered-session Test routing across the canonical VM and Stage-0 lanes.

The selected provider owns test semantics and evidence. Environment chooses
the explicitly requested provider contract and exact selected checkout; it
never falls back between execution lanes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REFERENCE_CAPABILITY = "mncs.test-verify/1"
CANONICAL_VM_CAPABILITY = "mncs-test:canonical-vm-tests"
EXECUTIONS = {"canonical-vm": CANONICAL_VM_CAPABILITY,
              "stage0-reference": REFERENCE_CAPABILITY}


class TestRoutingError(Exception):
    """Raised when a test run cannot be routed (with machine diagnostics)."""

    def __init__(self, message: str, code: str, **details: Any):
        super().__init__(message)
        self.diagnostics = {"code": code, **details}


def _checkout_records(session) -> dict[str, dict[str, Any]]:
    selected = session.snapshot.get("selected_checkouts", {})
    records: dict[str, dict[str, Any]] = {}
    if isinstance(selected, dict):
        for repository, record in selected.items():
            if isinstance(record, dict):
                records[str(repository)] = dict(record)
    return records


def _resolve_record_path(session, record: dict[str, Any]) -> Path:
    path = Path(str(record.get("path", "")))
    if not path.is_absolute():
        root = session.snapshot.get("workspace", {}).get("root", "")
        path = Path(str(root)) / path
    return path.resolve()


def _has_obligation_inventory(checkout: Path) -> bool:
    try:
        manifest = json.loads((checkout / ".mncs" / "project.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(manifest, dict):
        return False
    verification = manifest.get("verification")
    name = verification.get("obligation_inventory") if isinstance(verification, dict) else None
    if not isinstance(name, str) or not name:
        return False
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        return False
    return (checkout / relative).is_file()


def inventory_candidates(session) -> list[str]:
    """Selected repositories carrying an obligation inventory (sorted)."""
    candidates: list[str] = []
    for name in sorted(_checkout_records(session)):
        path = _resolve_record_path(session, _checkout_records(session)[name])
        if path.is_dir() and _has_obligation_inventory(path):
            candidates.append(name)
    return candidates


def resolve_checkout(session, repository: str | None) -> tuple[str, str]:
    """Resolve a test target to (repository, checkout path).

    The repository is explicit: verification writes receipts and caches
    into the target checkout, so bare runs refuse with the candidate
    list instead of guessing a target (same precedent as repository
    remediation requiring --checkout).
    """
    records = _checkout_records(session)
    if repository is None:
        raise TestRoutingError(
            "testing needs --checkout <repository>",
            "test-checkout-required", candidates=inventory_candidates(session),
            next="rerun with --checkout naming one of the candidates",
        )
    record = records.get(repository)
    if record is None:
        raise TestRoutingError(
            f"repository {repository} is not selected in this session",
            "test-checkout-unknown", repository=repository,
            candidates=sorted(records),
            next="enter a session selecting the repository, then retry",
        )
    path = _resolve_record_path(session, record)
    if not path.is_dir():
        raise TestRoutingError(
            f"checkout for {repository} is missing: {path}",
            "test-checkout-missing", repository=repository,
        )
    return repository, str(path)


def provider_binding(session, execution: str = "canonical-vm") -> dict[str, Any]:
    """Return the exact requested provider binding, without backend fallback."""
    capability = EXECUTIONS.get(execution)
    if capability is None:
        raise TestRoutingError(f"unsupported test execution: {execution}",
                               "test-execution-unknown", execution=execution)
    for binding in session.snapshot.get("bindings", []):
        if not isinstance(binding, dict):
            continue
        if str(binding.get("capability", "")) != capability:
            continue
        if binding.get("availability", {}).get("status") != "available":
            raise TestRoutingError(
                f"capability {capability} is not available: "
                f"{binding.get('availability', {}).get('reason')}",
                "test-provider-unavailable", capability=capability,
                next="reconcile the session and repair the selected provider",
            )
        return binding
    raise TestRoutingError(
        f"session binds no {capability} capability",
        "test-provider-missing", capability=capability,
        next="select the repository that provides this execution contract, then reconcile",
    )


def selected_execution(session, checkout: str) -> str:
    """Use the canonical provider for its own checkout; keep other targets on Stage-0."""
    target = Path(checkout).resolve()
    for binding in session.snapshot.get("bindings", []):
        if not isinstance(binding, dict) or binding.get("capability") != CANONICAL_VM_CAPABILITY:
            continue
        provenance = binding.get("provenance", {})
        provider_checkout = provenance.get("checkout", {}).get("path") if isinstance(provenance, dict) else None
        if isinstance(provider_checkout, str) and Path(provider_checkout).resolve() == target:
            return "canonical-vm"
    return "stage0-reference"


def run_tests(
    session,
    repository: str | None,
    *,
    output_format: str = "text",
    timeout_seconds: int = 600,
    max_executions: int = 16,
    no_store: bool = False,
    execution: str | None = None,
) -> dict[str, Any]:
    """Invoke only the requested execution provider over the selected checkout."""
    name, checkout = resolve_checkout(session, repository)
    execution = execution or selected_execution(session, checkout)
    binding = provider_binding(session, execution)
    capability = EXECUTIONS[execution]
    if execution == "canonical-vm":
        bound_checkout = binding.get("provenance", {}).get("checkout", {}).get("path")
        if not isinstance(bound_checkout, str) or Path(bound_checkout).resolve() != Path(checkout).resolve():
            raise TestRoutingError("canonical-vm test contract is bound to its provider checkout",
                                   "test-canonical-target-mismatch", repository=name,
                                   provider_checkout=bound_checkout, checkout=checkout,
                                   next="use the Stage-0 reference lane for this target")
        if no_store:
            raise TestRoutingError("--no-store applies only to the Stage-0 reference lane",
                                   "test-option-not-applicable", option="no_store",
                                   execution=execution)
        argv = ["--format", output_format]
    else:
        argv = ["--format", output_format, "--max-executions", str(max_executions)]
        if no_store:
            argv.append("--no-store")
    result = session.invoke(
        capability, argv, cwd=checkout, timeout_seconds=timeout_seconds,
    )
    return {
        "repository": name,
        "checkout": checkout,
        "execution": execution,
        "capability": capability,
        "binding_id": result.get("binding_id"),
        "status": result.get("status"),
        "returncode": result.get("returncode"),
        "truncated": result.get("truncated", False),
        "report": result.get("stdout", ""),
        "stderr": result.get("stderr", ""),
        "provider": binding.get("provider"),
    }
