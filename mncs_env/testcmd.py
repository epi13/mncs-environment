"""Entered-session test runs through the provider-owned verifier.

``mncs-env test`` is a thin routing command: it resolves a session checkout,
invokes the ``mncs.test-verify/1`` capability bound in the session with that
checkout as its working directory, and streams the provider's report. Test
semantics, selection, reuse, receipts, and Store admission all live in the
mncs-test provider; this module owns only checkout resolution and result
pass-through.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

TEST_CAPABILITY = "mncs.test-verify/1"


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


def provider_binding(session) -> dict[str, Any]:
    """Return the bound verify capability, or explain how to get one."""
    for binding in session.snapshot.get("bindings", []):
        if not isinstance(binding, dict):
            continue
        if str(binding.get("capability", "")) != TEST_CAPABILITY:
            continue
        if binding.get("availability", {}).get("status") != "available":
            raise TestRoutingError(
                f"capability {TEST_CAPABILITY} is not available: "
                f"{binding.get('availability', {}).get('reason')}",
                "test-provider-unavailable", capability=TEST_CAPABILITY,
                next="reconcile the session or repair the mncs-test checkout",
            )
        return binding
    raise TestRoutingError(
        f"session binds no {TEST_CAPABILITY} capability",
        "test-provider-missing", capability=TEST_CAPABILITY,
        next="select the mncs-test checkout providing mncs.test-verify/1, then reconcile",
    )


def run_tests(
    session,
    repository: str | None,
    *,
    output_format: str = "text",
    timeout_seconds: int = 600,
    max_executions: int = 16,
    no_store: bool = False,
) -> dict[str, Any]:
    """Invoke the provider verifier over the resolved checkout."""
    name, checkout = resolve_checkout(session, repository)
    binding = provider_binding(session)
    argv = ["--format", output_format, "--max-executions", str(max_executions)]
    if no_store:
        argv.append("--no-store")
    result = session.invoke(
        TEST_CAPABILITY, argv, cwd=checkout, timeout_seconds=timeout_seconds,
    )
    return {
        "repository": name,
        "checkout": checkout,
        "capability": TEST_CAPABILITY,
        "binding_id": result.get("binding_id"),
        "status": result.get("status"),
        "returncode": result.get("returncode"),
        "truncated": result.get("truncated", False),
        "report": result.get("stdout", ""),
        "stderr": result.get("stderr", ""),
        "provider": binding.get("provider"),
    }
