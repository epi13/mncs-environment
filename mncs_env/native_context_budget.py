"""MNCS planning for bounded Environment entry-context compaction."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import native_readiness

MAX_NATIVE_BLOCKS = 7
MAX_NATIVE_INTEGER = (1 << 64) - 1
MAX_NATIVE_INPUT_BYTES = 1 << 30
NATIVE_SCHEMA = "mncs.environment.context-budget-plan-result/1"


def selected_toolchain_paths(session) -> tuple[Path, Path] | None:
    return native_readiness._toolchain_paths(session)


def project_context_budget_native(
    rows: list[dict[str, Any]], budget: dict[str, Any], session,
    *, toolchain_paths: tuple[Path, Path] | None = None,
    timeout_seconds: float = 15.0,
) -> dict[str, Any] | None:
    """Choose which bounded context blocks to compact, or return ``None``.

    The Python caller prepares measured byte counts and evidence handles. MNCS
    owns the ordered compaction decision and the resulting aggregate size.
    """
    if not isinstance(rows, list) or len(rows) > MAX_NATIVE_BLOCKS:
        return None
    budget_fields = {
        "healthy_bytes": "healthy_limit",
        "degraded_bytes": "degraded_limit",
        "total_bytes": "total_limit",
        "attention_items": "attention_limit",
    }
    request_budget: dict[str, int] = {}
    for key, output_key in budget_fields.items():
        value = budget.get(key) if isinstance(budget, dict) else None
        if type(value) is not int or not 0 <= value <= MAX_NATIVE_INTEGER:
            return None
        request_budget[output_key] = value

    request_rows = []
    for row in rows:
        if not isinstance(row, dict) or type(row.get("degraded")) is not bool:
            return None
        encoded: dict[str, Any] = {"degraded": row["degraded"]}
        for key in (
            "expanded_bytes", "full_handle_bytes", "minimal_handle_bytes",
            "attention_count",
        ):
            value = row.get(key)
            if type(value) is not int or not 0 <= value <= MAX_NATIVE_INPUT_BYTES:
                return None
            encoded[key] = value
        request_rows.append(encoded)

    toolchain_paths = toolchain_paths or selected_toolchain_paths(session)
    if toolchain_paths is None:
        return None
    binary, stdlib = toolchain_paths
    request = {
        "schema_version": "mncs.environment.context-budget-plan-request/1",
        **request_budget,
        "blocks": request_rows,
    }
    result = native_readiness._run_native_application(
        session=session,
        binary=binary,
        stdlib=stdlib,
        descriptor_name="context-budget-plan.json",
        request=request,
        expected_schema=NATIVE_SCHEMA,
        timeout_seconds=timeout_seconds,
    )
    if result.get("valid") is not True:
        raise native_readiness.NativeApplicationError(
            "native context-budget planner rejected a bounded request"
        )
    count = result.get("block_count")
    modes = result.get("modes")
    planned_bytes = result.get("planned_bytes")
    final_total = result.get("final_total_bytes")
    if type(count) is not int or count != len(rows):
        raise native_readiness.NativeApplicationError(
            "native context-budget planner returned an incomplete plan"
        )
    if not isinstance(modes, list) or len(modes) != len(rows):
        raise native_readiness.NativeApplicationError(
            "native context-budget planner returned malformed compaction decisions"
        )
    if any(type(mode) is not int or mode not in (0, 1, 2) for mode in modes):
        raise native_readiness.NativeApplicationError(
            "native context-budget planner returned an invalid compaction mode"
        )
    if not isinstance(planned_bytes, list) or len(planned_bytes) != len(rows):
        raise native_readiness.NativeApplicationError(
            "native context-budget planner returned malformed block sizes"
        )
    if any(
        type(size) is not int or not 0 <= size <= MAX_NATIVE_INTEGER
        for size in planned_bytes
    ):
        raise native_readiness.NativeApplicationError(
            "native context-budget planner returned an invalid block size"
        )
    if type(final_total) is not int or not 0 <= final_total <= MAX_NATIVE_INTEGER:
        raise native_readiness.NativeApplicationError(
            "native context-budget planner returned an invalid total size"
        )
    return {
        "modes": modes,
        "planned_bytes": planned_bytes,
        "final_total_bytes": final_total,
    }
