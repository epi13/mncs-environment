"""Native batch classification for bounded update-capsule event windows."""

from __future__ import annotations

from typing import Any

from . import native_readiness
from .briefing import _add_read_measurements

MAX_NATIVE_EVENTS = 200
MAX_NATIVE_OBSERVATIONS = 50
MAX_NATIVE_CONTEXT_VALUES = 32
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

OBSERVATION_RELEVANCE = {
    1: "names this session",
    2: "touches a repository in this intent",
    3: "links work this session references",
    4: "claim state changed somewhere in the family",
    5: "elevated severity is family-visible",
}

SUBJECT_KEYS = ("capability", "operation", "repository", "claim_id", "action")
COORDINATION_KEYS = (
    "action", "coordination_status", "required_coordination", "next_step",
)
COORDINATION_FIELD_NAMES = (
    "action_valid", "coordination_status_valid", "required_coordination_valid",
    "next_step_valid",
)
NATIVE_SCHEMA = "mncs.environment.update-capsule-classification-result/2"


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


def _string_set_request(value: Any) -> list[dict[str, str]] | None:
    """Encode bounded members that can match Python's string intersections."""
    if not isinstance(value, (str, list, tuple, dict, set, frozenset)):
        return None
    try:
        if len(value) > MAX_NATIVE_CONTEXT_VALUES:
            return None
        members = set(value)
    except (TypeError, ValueError):
        return None
    if len(members) > MAX_NATIVE_CONTEXT_VALUES:
        return None
    strings = sorted(member for member in members if type(member) is str)
    if any(_bounded_text(member) is None for member in strings):
        return None
    return [_text(member) for member in strings]


def _relation_repositories_request(value: Any) -> list[dict[str, str]] | None:
    """Encode the bounded iterable used by Python's repository relation test."""
    if not value:
        return []
    if type(value) is str:
        values = list(value)
    elif isinstance(value, (list, tuple)):
        values = value
    elif isinstance(value, dict):
        values = list(value)
    else:
        return None
    if len(values) > MAX_NATIVE_CONTEXT_VALUES:
        return None
    prepared = []
    for value_item in values:
        text = _bounded_text(value_item)
        if text is None:
            return None
        prepared.append(_text(text))
    return prepared


def _observation_request(event: Any) -> dict[str, Any] | None:
    if not isinstance(event, dict):
        return None
    payload = event.get("payload", {})
    if not isinstance(payload, dict):
        return None
    relations = payload.get("relations", {})
    relations_valid = isinstance(relations, dict)
    relation_session_id = ""
    session_id_present = False
    session_id_is_text = False
    subject_present = [False] * 4
    subjects = [_text("") for _ in range(4)]
    repositories: list[dict[str, str]] = []
    commons_work = ""
    if relations_valid:
        session_id_present = "session_id" in relations
        raw_session_id = relations.get("session_id")
        session_id_is_text = type(raw_session_id) is str
        if session_id_present:
            relation_session_id = _bounded_text(raw_session_id)
            if relation_session_id is None:
                return None

        for index, key in enumerate(("session_id", "claim", "repository", "uri")):
            value = relations.get(key)
            if not value:
                continue
            subject = _bounded_text(value)
            if subject is None:
                return None
            subject_present[index] = True
            subjects[index] = _text(subject)

        repositories = _relation_repositories_request(
            relations.get("repositories", []),
        )
        if repositories is None:
            return None
        commons_work = _bounded_text(relations.get("commons_work", ""))
        if commons_work is None:
            return None

    kind = _bounded_text(payload.get("kind", ""))
    severity = _bounded_text(payload.get("severity", ""))
    if kind is None or severity is None:
        return None
    return {
        "relations_valid": relations_valid,
        "session_id_present": session_id_present,
        "session_id_is_text": session_id_is_text,
        "relation_session_id": _text(relation_session_id),
        "subject_present": subject_present,
        "subjects": subjects,
        "repositories": repositories,
        "commons_work": _text(commons_work),
        "kind": _text(kind),
        "severity": _text(severity),
    }


def _observation_context(session) -> dict[str, Any] | None:
    session_id = getattr(session, "session_id", None)
    if type(session_id) is not str:
        return None
    bounded_session_id = _bounded_text(session_id)
    snapshot = getattr(session, "snapshot", None)
    if bounded_session_id is None or not isinstance(snapshot, dict):
        return None
    intent = snapshot.get("intent", {})
    if not isinstance(intent, dict):
        return None
    repositories = _string_set_request(intent.get("repositories", []))
    work_ids = _string_set_request(intent.get("commons_work", []))
    if repositories is None or work_ids is None:
        return None
    return {
        "session_id": _text(bounded_session_id),
        "intent_repositories": repositories,
        "commons_work_ids": work_ids,
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


def project_capsule_inputs_native(
    events: list[dict[str, Any]], observations: list[dict[str, Any]], session,
    *, timeout_seconds: float = 15.0,
) -> tuple[list[dict[str, Any] | None], list[str | None]] | None:
    """Classify bounded event and reconciler windows in one native batch."""
    if len(events) > MAX_NATIVE_EVENTS or len(observations) > MAX_NATIVE_OBSERVATIONS:
        return None
    prepared = [_event_request(event) for event in events]
    prepared_observations = [_observation_request(event) for event in observations]
    context = (
        _observation_context(session) if observations else {
            "session_id": _text(""),
            "intent_repositories": [],
            "commons_work_ids": [],
        }
    )
    if (any(event is None for event in prepared)
            or any(item is None for item in prepared_observations)
            or context is None):
        return None
    if not events and not observations:
        return [], []
    toolchain_paths = native_readiness._toolchain_paths(session)
    if toolchain_paths is None:
        return None
    binary, stdlib = toolchain_paths
    request = {
        "schema_version": "mncs.environment.update-capsule-classification-request/2",
        **context,
        "events": prepared,
        "observations": prepared_observations,
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
            or result["event_count"] != len(events)
            or type(result.get("observation_count")) is not int
            or result["observation_count"] != len(observations)):
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
    observation_codes = result.get("observation_relevance_codes")
    if (not isinstance(observation_codes, list)
            or len(observation_codes) != len(observations)):
        raise native_readiness.NativeApplicationError(
            "native capsule classifier returned malformed observation decisions"
        )
    observation_relevance = []
    for code in observation_codes:
        if type(code) is not int or code not in range(6):
            raise native_readiness.NativeApplicationError(
                "native capsule classifier returned an invalid observation relevance code"
            )
        observation_relevance.append(OBSERVATION_RELEVANCE.get(code))
    return items, observation_relevance


def project_events_native(
    events: list[dict[str, Any]], session, *, timeout_seconds: float = 15.0,
) -> list[dict[str, Any] | None] | None:
    """Compatibility view for event-only callers of the combined native batch."""
    result = project_capsule_inputs_native(
        events, [], session, timeout_seconds=timeout_seconds,
    )
    return None if result is None else result[0]
