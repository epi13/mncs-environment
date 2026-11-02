"""Bound ambient context without dropping provider evidence or reason meaning."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .identity import canonical_bytes, digest_hex
from .persist import write_json

LAYERS = ("doctor", "projection", "verification", "diagnostic", "semantics",
          "external_evidence", "family")
DEFAULTS = {"healthy_bytes": 768, "degraded_bytes": 1536,
            "total_bytes": 4096, "attention_items": 4}


def validate(definition: dict) -> dict:
    value = definition.get("context_budget", {})
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError("context_budget contains unknown fields or is not an object")
    budget = {**DEFAULTS, **value}
    for key in ("healthy_bytes", "degraded_bytes", "total_bytes"):
        if type(budget[key]) is not int or not 512 <= budget[key] <= 16384:
            raise ValueError(f"context_budget.{key} must be an integer from 512 to 16384")
    if budget["degraded_bytes"] < budget["healthy_bytes"] or budget["total_bytes"] < 4096:
        raise ValueError("context_budget requires degraded >= healthy and total >= 4096")
    if type(budget["attention_items"]) is not int or not 0 <= budget["attention_items"] <= 8:
        raise ValueError("context_budget.attention_items must be an integer from 0 to 8")
    return budget


def _blocks(context: dict) -> dict[str, dict[str, Any]]:
    return {key: context[key] for key in LAYERS if isinstance(context.get(key), dict)}


def _compact_summaries(summary: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    priority = (
        "blockers", "status", "obligations", "failed", "pending", "current",
        "degraded", "escalated",
    )
    compact = {
        name: summary[name]
        for name in priority
        if name in summary
        and (
            type(summary[name]) in (int, bool)
            or (isinstance(summary[name], str) and len(summary[name]) <= 32)
        )
    }
    minimal = {name: compact[name] for name in ("blockers", "status") if name in compact}
    return compact, minimal


def _native_materials(session, blocks: dict[str, dict[str, Any]], budget: dict):
    """Prepare byte measurements and equivalent handles for the native planner."""
    from .native_context_budget import MAX_NATIVE_INPUT_BYTES, MAX_NATIVE_INTEGER

    if not isinstance(budget, dict):
        return None
    for key in DEFAULTS:
        value = budget.get(key)
        if type(value) is not int or not 0 <= value <= MAX_NATIVE_INTEGER:
            return None
    try:
        session_id = session.session_id
    except (AttributeError, TypeError):
        return None
    if not isinstance(session_id, str):
        return None

    materials = []
    rows = []
    total_bytes = 0
    for key, block in blocks.items():
        summary = block.get("summary", {})
        capsule = block.get("capsule", {})
        if not isinstance(summary, dict) or not isinstance(capsule, dict):
            return None
        attention = capsule.get("attention", [])
        try:
            attention_count = len(attention)
        except TypeError:
            return None
        degraded = bool(
            summary.get("blockers")
            or summary.get("escalated")
            or summary.get("degraded")
        )
        encoded_block = canonical_bytes(block)
        expanded_bytes = len(encoded_block)
        tag = hashlib.sha256(encoded_block).hexdigest()[:64]
        relative = (
            Path("sessions") / session_id / "entry-context" / f"{key}-{tag}.json"
        )
        compact_summary, minimal_summary = _compact_summaries(summary)
        full_handle = {
            "summary": compact_summary,
            "digest": tag,
            "evidence": str(relative),
            "expanded_bytes": expanded_bytes,
            "budgeted": True,
        }
        if attention:
            full_handle["attention_count"] = attention_count
        minimal_handle = {
            **full_handle,
            "summary": minimal_summary,
        }
        full_handle_bytes = len(canonical_bytes(full_handle))
        minimal_handle_bytes = len(canonical_bytes(minimal_handle))
        values = (
            expanded_bytes, full_handle_bytes, minimal_handle_bytes,
            attention_count,
        )
        if any(value > MAX_NATIVE_INPUT_BYTES for value in values):
            return None
        total_bytes += expanded_bytes
        if total_bytes > MAX_NATIVE_INPUT_BYTES * max(1, len(blocks)):
            return None
        rows.append({
            "expanded_bytes": expanded_bytes,
            "full_handle_bytes": full_handle_bytes,
            "minimal_handle_bytes": minimal_handle_bytes,
            "attention_count": attention_count,
            "degraded": degraded,
        })
        materials.append({
            "key": key,
            "block": block,
            "relative": relative,
            "expanded_bytes": expanded_bytes,
            "full_handle": full_handle,
            "minimal_handle": minimal_handle,
            "full_handle_bytes": full_handle_bytes,
            "minimal_handle_bytes": minimal_handle_bytes,
            "attention_count": attention_count,
            "limit": budget["degraded_bytes" if degraded else "healthy_bytes"],
        })
    return rows, materials


def _apply_native_plan(session, context, budget, rows, materials, plan):
    from .native_readiness import NativeApplicationError

    state_root = session.state_dir.resolve()
    total_bytes = sum(material["expanded_bytes"] for material in materials)
    for row, material, mode, planned_size in zip(
        rows, materials, plan["modes"], plan["planned_bytes"], strict=True,
    ):
        expanded_bytes = material["expanded_bytes"]
        layer_limit = material["limit"]
        must_compact = (
            expanded_bytes > layer_limit
            or row["attention_count"] > budget["attention_items"]
            or total_bytes > budget["total_bytes"]
        )
        if type(mode) is not int or mode not in (0, 1, 2):
            raise NativeApplicationError("native context-budget plan has an invalid mode")
        if mode == 0:
            if must_compact or planned_size != expanded_bytes:
                raise NativeApplicationError(
                    "native context-budget plan skipped required compaction"
                )
            continue
        if not must_compact:
            raise NativeApplicationError(
                "native context-budget plan compacted an in-budget block"
            )

        handle_key = "full_handle" if mode == 1 else "minimal_handle"
        size_key = "full_handle_bytes" if mode == 1 else "minimal_handle_bytes"
        handle = material[handle_key]
        if planned_size != material[size_key]:
            raise NativeApplicationError(
                "native context-budget plan size does not match its handle"
            )
        path = (state_root / material["relative"]).resolve()
        if not path.is_relative_to(state_root):
            raise ValueError("context evidence escapes session state")
        if not path.exists():
            write_json(path, material["block"])
        context[material["key"]] = handle
        actual_handle_bytes = len(canonical_bytes(handle))
        if actual_handle_bytes != planned_size:
            raise NativeApplicationError(
                "native context-budget plan disagrees with canonical handle size"
            )
        if actual_handle_bytes > layer_limit:
            raise ValueError("context expansion handle exceeds its declared budget")
        total_bytes -= expanded_bytes - actual_handle_bytes

    if total_bytes != plan["final_total_bytes"]:
        raise NativeApplicationError(
            "native context-budget plan disagrees with final context size"
        )
    if total_bytes > budget["total_bytes"]:
        raise ValueError("ambient expansion handles exceed total context budget")
    return context


def _apply_python(session, context: dict, budget: dict,
                  blocks: dict[str, dict[str, Any]]) -> dict:
    """Reference implementation and bounded fallback for unsupported inputs."""
    total = sum(len(canonical_bytes(block)) for block in blocks.values())
    for key, block in blocks.items():
        summary = block.get("summary", {})
        attention = block.get("capsule", {}).get("attention", [])
        degraded = bool(summary.get("blockers") or summary.get("escalated") or summary.get("degraded"))
        limit = budget["degraded_bytes" if degraded else "healthy_bytes"]
        if len(canonical_bytes(block)) <= limit and len(attention) <= budget["attention_items"] and total <= budget["total_bytes"]:
            continue
        tag = digest_hex(block, length=64)
        relative = Path("sessions") / session.session_id / "entry-context" / f"{key}-{tag}.json"
        path = (session.state_dir / relative).resolve()
        if not path.is_relative_to(session.state_dir.resolve()):
            raise ValueError("context evidence escapes session state")
        if not path.exists():
            write_json(path, block)
        priority = ("blockers", "status", "obligations", "failed", "pending", "current", "degraded", "escalated")
        compact = {name: summary[name] for name in priority if name in summary
                   and (type(summary[name]) in (int, bool) or
                        (isinstance(summary[name], str) and len(summary[name]) <= 32))}
        context[key] = {"summary": compact, "digest": tag, "evidence": str(relative),
                        "expanded_bytes": len(canonical_bytes(block)), "budgeted": True}
        if attention:
            context[key]["attention_count"] = len(attention)
        if len(canonical_bytes(context[key])) > limit:
            context[key]["summary"] = {name: compact[name] for name in ("blockers", "status") if name in compact}
        if len(canonical_bytes(context[key])) > limit:
            raise ValueError("context expansion handle exceeds its declared budget")
        total -= len(canonical_bytes(block)) - len(canonical_bytes(context[key]))
    if total > budget["total_bytes"]:
        raise ValueError("ambient expansion handles exceed total context budget")
    return context


def apply(session, context: dict, budget: dict) -> dict:
    """Summaries retain scalar counts; complete expansion is content-addressed.

    Overflow artifacts are written once per content, outside provider trees.
    Healthy context gets no accounting block or extra tokens.
    """
    blocks = _blocks(context)
    if not blocks:
        return context
    from . import native_context_budget

    toolchain_paths = native_context_budget.selected_toolchain_paths(session)
    if toolchain_paths is not None:
        prepared = _native_materials(session, blocks, budget)
        if prepared is not None:
            rows, materials = prepared
            plan = native_context_budget.project_context_budget_native(
                rows, budget, session, toolchain_paths=toolchain_paths,
            )
            if plan is not None:
                return _apply_native_plan(
                    session, context, budget, rows, materials, plan,
                )
    return _apply_python(session, context, budget, blocks)
