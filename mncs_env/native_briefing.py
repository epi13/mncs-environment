"""Native batch classification for bounded update-capsule event windows."""

from __future__ import annotations

from typing import Any

from . import native_readiness
from .briefing import _add_read_measurements

MAX_NATIVE_EVENTS = 200
MAX_NATIVE_TEXT_BYTES = 512

CATEGORIES = {
    1: "safety",
    2: "ownership",
    3: "blockers",
    4: "diagnostics",
    5: "verification",
    6: "coordination",
    7: "provider",
    8: "resource",
    9: "progress",
}

RELEVANCE = {
    1: "an authority denial names this session",
    2: "an escalation needs a decision",
    3: "an operation needs a coordination prerequisite",
    4: "claim ownership changed",
    5: "a claim request was rejected before publication",
    6: "stale claim safely recovered",
    7: "claim ownership changed",
    8: "claim ownership changed",
    9: "a capability invocation finished",
    10: "an Environment read operation was observed",
    11: "work was invoked",
    12: "a capability was bound from a provider",
    13: "a bound capability changed",
    14: "a bound capability was removed",
    15: "a capability changed availability",
    16: "a capability changed availability",
    17: "a capability has no verified invocation contract",
    18: "a checkpoint bounds prior work",
    19: "a handoff names a new consumer",
    20: "participation resumed",
    21: "lifecycle reached completed",
    22: "lifecycle reached failed",
    23: "session created",
    24: "an external adapter reported",
    25: "background reconciliation saw a delta",
    26: "a source reset; history may be incomplete",
    27: "an invocation succeeded",
}

SUBJECT_KEYS = ("capability", "operation", "repository", "claim_id", "action")
COORDINATION_KEYS = (
    "action", "coordination_status", "required_coordination", "next_step",
)
COORDINATION_FIELD_NAMES = (
    "action_valid", "coordination_status_valid", "required_coordination_valid",
    "next_step_valid",
)
NATIVE_SCHEMA = "mncs.environment.update-capsule-classification-result/1"


def _bounded_text(value: Any, *, limit: int = MAX_NATIVE_TEXT_BYTES) -> str | None:
    if type(value) not in (str, int, float, bool) and value is not None:
        return None
    try:
        text = str(value)
        if len(text.encode("utf-8")) > limit:
            return None
        return text
    except (UnicodeError, ValueError, OverflowError):
        return None


def _text(value: str) -> dict[str, str]:
    return {"bytes": value}


def _event_request(event: Any) -> dict[str, Any] | None:
    if not isinstance(event, dict):
        return None
    kind = _bounded_text(event.get("type", ""))
    if kind is None:
        return None
    payload = event.get("payload", {})
    if not isinstance(payload, dict):
        payload = {}

    status_present = "status" in payload
    status = _bounded_text(payload.get("status")) if status_present else ""
    outcome_status = _bounded_text(payload.get("outcome_status", ""))
    if status is None or outcome_status is None:
        return None

    subject_present = []
    subjects = []
    for key in SUBJECT_KEYS:
        value = payload.get(key)
        present = value is not None
        subject_present.append(present)
        if not present:
            text = ""
        else:
            text = _bounded_text(value)
            if text is None:
                return None
        subjects.append(_text(text))

    reason_present = "reason" in payload
    reason = _bounded_text(payload.get("reason")) if reason_present else ""
    summary_present = "summary" in payload
    summary = _bounded_text(payload.get("summary")) if summary_present else ""
    if reason is None or summary is None:
        return None

    coordination_values = []
    for key in COORDINATION_KEYS:
        value = payload.get(key)
        if not isinstance(value, str):
            coordination_values.append("")
        elif len(value) > 160:
            # Python rejects every overlong candidate before applying the key grammar.
            coordination_values.append("x" * 161)
        elif not value.isascii():
            # Unicode never matches the established ASCII coordination-key grammar.
            coordination_values.append("")
        else:
            coordination_values.append(value)

    return {
        "kind": _text(kind),
        "status_present": status_present,
        "status": _text(status),
        "outcome_status": _text(outcome_status),
        "subject_present": subject_present,
        "subjects": subjects,
        "reason_present": reason_present,
        "reason": _text(reason),
        "summary_present": summary_present,
        "summary": _text(summary),
        "action": _text(coordination_values[0]),
        "coordination_status": _text(coordination_values[1]),
        "required_coordination": _text(coordination_values[2]),
        "next_step": _text(coordination_values[3]),
        "persistent_authority_required_false": (
            payload.get("persistent_authority_required") is False
        ),
        "operation_executed_false": payload.get("operation_executed") is False,
    }


def _decision(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or type(value.get("relevant")) is not bool:
        raise native_readiness.NativeApplicationError(
            "native capsule classifier returned a malformed decision"
        )
    category_code = value.get("category_code")
    priority = value.get("priority")
    relevance_code = value.get("relevance_code")
    summary_mode = value.get("summary_mode")
    subject_source = value.get("subject_source")
    if (type(category_code) is not int or not 0 <= category_code <= 9
            or type(priority) is not int or not 0 <= priority <= 9
            or type(relevance_code) is not int or not 0 <= relevance_code <= 27
            or type(summary_mode) is not int or summary_mode not in range(7)
            or type(subject_source) is not int or subject_source not in range(6)):
        raise native_readiness.NativeApplicationError(
            "native capsule classifier returned an invalid classification code"
        )
    for name in (
        "action_valid", "coordination_status_valid", "required_coordination_valid",
        "next_step_valid", "persistent_authority_required_false",
        "operation_executed_false",
    ):
        if type(value.get(name)) is not bool:
            raise native_readiness.NativeApplicationError(
                "native capsule classifier returned an invalid coordination projection"
            )
    if not value["relevant"]:
        if category_code != 0 or priority != 0 or relevance_code != 0:
            raise native_readiness.NativeApplicationError(
                "native capsule classifier classified an irrelevant event"
            )
        return None
    if (category_code not in CATEGORIES or priority != category_code
            or relevance_code not in RELEVANCE):
        raise native_readiness.NativeApplicationError(
            "native capsule classifier returned an unknown classification code"
        )
    return {
        "category": CATEGORIES[category_code],
        "priority": priority,
        "relevance": RELEVANCE[relevance_code],
        "summary_mode": summary_mode,
        "subject_source": subject_source,
        **{name: value[name] for name in (
            "action_valid", "coordination_status_valid",
            "required_coordination_valid", "next_step_valid",
            "persistent_authority_required_false", "operation_executed_false",
        )},
    }


def _project_item(event: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload", {})
    if not isinstance(payload, dict):
        payload = {}
    subject_source = decision["subject_source"]
    subject = ""
    if subject_source < len(SUBJECT_KEYS):
        subject = str(payload[SUBJECT_KEYS[subject_source]])

    kind = str(event.get("type", ""))
    summary_mode = decision["summary_mode"]
    if summary_mode == 0:
        summary = str(payload.get("reason"))
    elif summary_mode == 1:
        summary = str(payload.get("summary"))
    elif summary_mode == 2:
        summary = kind
    elif summary_mode == 3:
        summary = "Environment read operation succeeded"
    elif summary_mode == 4:
        summary = "Environment read operation failed"
    elif summary_mode == 5:
        summary = "Operation deferred for coordination"
    else:
        summary = (
            "Operation deferred; next step: "
            + str(payload.get("next_step"))
        )

    item = {
        "id": f"{event.get('session_id')}:{event.get('sequence')}",
        "category": decision["category"],
        "priority": decision["priority"],
        "kind": kind,
        "subject": subject,
        "summary": summary,
        "at": str(event.get("observed_at", "")),
        "relevance": decision["relevance"],
        "ref": {
            "session_id": event.get("session_id"),
            "sequence": event.get("sequence"),
        },
    }
    for key, result_key in zip(COORDINATION_KEYS, COORDINATION_FIELD_NAMES, strict=True):
        if decision[result_key]:
            item[key] = payload[key]
    if decision["persistent_authority_required_false"]:
        item["persistent_authority_required"] = False
    if decision["operation_executed_false"]:
        item["operation_executed"] = False
    if kind == "environment.rpc.read_observed":
        _add_read_measurements(item, payload)
    return item


def project_events_native(
    events: list[dict[str, Any]], session, *, timeout_seconds: float = 15.0,
) -> list[dict[str, Any] | None] | None:
    """Classify an in-bounds event batch natively, or return None for fallback."""
    if not events:
        return []
    if len(events) > MAX_NATIVE_EVENTS:
        return None
    prepared = [_event_request(event) for event in events]
    if any(event is None for event in prepared):
        return None
    toolchain_paths = native_readiness._toolchain_paths(session)
    if toolchain_paths is None:
        return None
    binary, stdlib = toolchain_paths
    request = {
        "schema_version": "mncs.environment.update-capsule-classification-request/1",
        "events": prepared,
    }
    result = native_readiness._run_native_application(
        session=session,
        binary=binary,
        stdlib=stdlib,
        descriptor_name="update-capsule-classification.json",
        request=request,
        expected_schema=NATIVE_SCHEMA,
        timeout_seconds=timeout_seconds,
    )
    if (result.get("valid") is not True
            or type(result.get("event_count")) is not int
            or result["event_count"] != len(events)):
        raise native_readiness.NativeApplicationError(
            "native capsule classifier returned an incomplete batch"
        )
    rows = result.get("decisions")
    if not isinstance(rows, list) or len(rows) != len(events):
        raise native_readiness.NativeApplicationError(
            "native capsule classifier returned a malformed batch"
        )
    items = []
    for event, row in zip(events, rows, strict=True):
        decision = _decision(row)
        items.append(
            _project_item(event, decision) if decision is not None else None
        )
    return items
