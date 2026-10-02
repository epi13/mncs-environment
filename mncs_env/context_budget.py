"""Bound ambient context without dropping provider evidence or reason meaning."""
from __future__ import annotations

from pathlib import Path

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


def apply(session, context: dict, budget: dict) -> dict:
    """Summaries retain scalar counts; complete expansion is content-addressed.

    Overflow artifacts are written once per content, outside provider trees.
    Healthy context gets no accounting block or extra tokens.
    """
    blocks = {key: context[key] for key in LAYERS if isinstance(context.get(key), dict)}
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
