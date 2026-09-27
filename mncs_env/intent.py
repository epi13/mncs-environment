"""WorkIntent: concrete machine-readable work specification.

Intent declares the desired outcome, constraints, protected scope, and
acceptance conditions. It never embeds an implementation plan; planning
and execution belong to the bound services. Human prose is allowed as
one field among structured fields, never as the whole contract.
"""

from __future__ import annotations

from typing import Any

from .identity import intent_id

SCHEMA = "mncs.environment.work-intent/1"

STRING_FIELDS = (
    "goal",
    "prose",
    "completion_policy",
    "escalation_policy",
)

LIST_FIELDS = (
    "outcomes",
    "requirements",
    "constraints",
    "forbidden_actions",
    "protected_repositories",
    "protected_paths",
    "repositories",
    "expected_artifacts",
    "acceptance_criteria",
    "completion_conditions",
    "dependencies",
    "commons_work",
    "capability_requirements",
    "write_scope",
    "shared_core",
    "escalation_conditions",
    "authority_requirements",
    "provenance_expectations",
    "priorities",
)


class IntentError(ValueError):
    """Raised when a WorkIntent cannot be trusted."""


def parse(raw: Any) -> dict[str, Any]:
    """Validate a raw mapping into a canonical WorkIntent (raises IntentError)."""
    errors = _validate(raw)
    if errors:
        raise IntentError("; ".join(errors))
    assert isinstance(raw, dict)
    intent: dict[str, Any] = {"schema_version": SCHEMA}
    intent["goal"] = raw["goal"]
    for field in STRING_FIELDS[1:]:
        if raw.get(field) is not None:
            intent[field] = raw[field]
    for field in LIST_FIELDS:
        intent[field] = list(raw.get(field, []))
    intent["identity"] = intent_id(intent)
    return intent


def _validate(raw: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(raw, dict):
        return ["intent must be a JSON object"]
    if raw.get("schema_version", SCHEMA) != SCHEMA:
        errors.append(f"schema_version must be {SCHEMA}")
    goal = raw.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        errors.append("goal must be a non-empty string")
    for field in STRING_FIELDS[1:]:
        if field in raw and raw[field] is not None and not isinstance(raw[field], str):
            errors.append(f"{field} must be a string")
    for field in LIST_FIELDS:
        value = raw.get(field, [])
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            errors.append(f"{field} must be a list of strings")
    return errors


def empty(goal: str) -> dict[str, Any]:
    """Build a minimal valid intent (useful for tests and bootstrapping)."""
    return parse({"goal": goal})
