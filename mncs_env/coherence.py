"""Event/observation transport and owner-pass composition.

Automation classifies affected passes. Owner passes retain every domain gate.
Repository catalogues and executable byte identities are observation inputs,
not replacements for semantic generations, verdicts or build-origin proof.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import re
import secrets
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import evidence_store, observations, resources, sources
from .identity import digest_hex
from .invocation_context import ambient_owner_pass_context
from .persist import write_json

SCHEMA = "mncs.environment.incremental-coherence/1"
INPUTS_PATH = Path(__file__).resolve().parents[1] / ".mncs/coherence-passes.json"
MAX_WAVES = 3
MAX_TRACE_OWNER_ROWS = 16
MAX_TRACE_PENDING_EVENT_ROWS = 32
MAX_OWNER_EXECUTION_HISTORY = 16
OWNER_EXECUTION_HISTORY_SCHEMA = (
    "mncs.environment.owner-execution-history/2"
)
LEGACY_OWNER_EXECUTION_HISTORY_SCHEMA = (
    "mncs.environment.owner-execution-history/1"
)
OWNER_RESULT_STATUSES = {
    "result-recorded", "runner-returned-none", "result-missing",
    "invalid-result-shape",
}
OWNER_RESULT_REFERENCE_STATUSES = {
    "store-record", "session-artifact", "trace-not-persisted",
    "not-recorded", "not-recorded-in-history", "identity-mismatch",
    "invalid-reference",
}
MAX_TRACE_TRIGGER_KINDS = 8
MAX_TRACE_TRIGGER_SUBJECT_IDENTITIES = 8
MAX_TRACE_RESULT_FIELDS = 16
OWNER_PASS_OUTCOME_SCHEMA = "mncs.environment.ambient-owner-pass-outcome/1"
OWNER_PASS_QUIET_REASONS = frozenset({
    "disabled-by-definition",
    "no-actionable-external-evidence",
    "no-declared-semantic-contracts",
    "no-diagnostic-findings",
    "no-relevant-family-work",
    "no-verification-obligations",
})
OWNER_PASS_SERVICE_AVAILABILITY_CODES = {
    ("family", "vocabulary-unavailable"): "family-vocabulary-unavailable",
}


def quiet_owner_pass_result(reason_code: str, *, disabled: bool = False) -> dict:
    """Record a quiet owner-pass outcome without creating an agent-facing block.

    The result is scheduler evidence, not evidence that an agent needed or
    received the service. ``owner_pass_response`` removes this internal record
    from the normal entry response so quiet passes remain quiet for agents.
    """
    if reason_code not in OWNER_PASS_QUIET_REASONS:
        raise ValueError("unsupported ambient owner-pass quiet reason")
    if disabled != (reason_code == "disabled-by-definition"):
        raise ValueError("disabled ambient passes require their exact reason code")
    return {
        "status": "disabled" if disabled else "quiet-no-op",
        "operation_status": "complete",
        "summary": {"response_emitted": False},
        "ambient_owner_pass_outcome": {
            "schema_version": OWNER_PASS_OUTCOME_SCHEMA,
            "disposition": "disabled" if disabled else "completed-quietly",
            "reason_code": reason_code,
            "agent_response_emitted": False,
        },
    }


def _validated_owner_pass_outcome(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    outcome = value.get("ambient_owner_pass_outcome")
    if (not isinstance(outcome, dict)
            or set(outcome) != {
                "schema_version", "disposition", "reason_code",
                "agent_response_emitted",
            }
            or outcome.get("schema_version") != OWNER_PASS_OUTCOME_SCHEMA
            or outcome.get("disposition") not in {"completed-quietly", "disabled"}
            or outcome.get("reason_code") not in OWNER_PASS_QUIET_REASONS
            or type(outcome.get("agent_response_emitted")) is not bool
            or outcome.get("agent_response_emitted") is not False
            or value.get("operation_status") != "complete"
            or value.get("status") != (
                "disabled" if outcome.get("disposition") == "disabled"
                else "quiet-no-op"
            )
            or (outcome.get("disposition") == "disabled")
            != (outcome.get("reason_code") == "disabled-by-definition")):
        return None
    return {
        "disposition": outcome["disposition"],
        "reason_code": outcome["reason_code"],
        "agent_response_emitted": False,
    }


def owner_pass_response(value):
    """Keep internal quiet-outcome evidence out of an agent-facing entry block."""
    return None if _validated_owner_pass_outcome(value) is not None else value


def project_trace(value: dict, *, session_id: str | None = None) -> dict | None:
    """Return a bounded, path-free view of an incremental owner-pass trace."""
    if not isinstance(value, dict):
        return None

    def safe_identity(raw):
        if (not isinstance(raw, str) or len(raw) > 160
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*", raw) is None):
            return None
        return raw

    def safe_text(raw):
        if not isinstance(raw, str) or len(raw) > 256:
            return None
        if any(
            ord(char) < 32 or ord(char) > 126 or char in "/\\"
            for char in raw
        ):
            return None
        return raw

    def nonnegative(raw):
        return raw if type(raw) is int and raw >= 0 else None

    def duration(raw):
        return round(float(raw), 3) if (
            type(raw) in (int, float) and 0 <= raw <= 86400
        ) else None

    safe_session_id = (
        session_id if isinstance(session_id, str)
        and re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", session_id) else None
    )

    raw_scheduled = value.get("scheduled")
    scheduled_shape_valid = isinstance(raw_scheduled, list)
    raw_scheduled = raw_scheduled if isinstance(raw_scheduled, list) else []
    try:
        declarations_value = declarations()
        pass_declarations = declarations_value.get("passes")
        fact_declarations = declarations_value.get("facts")
        vocabulary_known = (
            isinstance(pass_declarations, dict)
            and isinstance(fact_declarations, list)
            and all(isinstance(name, str) for name in pass_declarations)
            and all(isinstance(name, str) for name in fact_declarations)
        )
        declared_passes = set(pass_declarations) if vocabulary_known else set()
        declared_facts = set(fact_declarations) if vocabulary_known else set()
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        declared_passes = set()
        declared_facts = set()
        vocabulary_known = False
    scheduled = []
    invalid_scheduled_count = 0
    for item in raw_scheduled[:MAX_TRACE_OWNER_ROWS]:
        if not isinstance(item, dict):
            invalid_scheduled_count += 1
            continue
        owner = safe_identity(item.get("owner") or item.get("pass"))
        if owner is None or owner not in declared_passes:
            invalid_scheduled_count += 1
            continue
        row = {"owner": owner, "execution_status": "runner-returned"}
        disposition = nonnegative(item.get("disposition"))
        if disposition is not None:
            row["scheduler_disposition_code"] = disposition
        wave = nonnegative(item.get("wave"))
        if wave is not None:
            row["wave"] = wave
        if type(item.get("owner_reused")) is bool:
            row["result_reused"] = item["owner_reused"]
        execution_id = item.get("service_execution_id")
        if execution_id is not None:
            if (isinstance(execution_id, str)
                    and re.fullmatch(r"ase_[0-9a-f]{32}", execution_id)):
                row["service_execution_id"] = execution_id
            else:
                invalid_scheduled_count += 1
        if type(item.get("timer_or_lifecycle_due")) is bool:
            row["timer_or_lifecycle_due"] = item["timer_or_lifecycle_due"]
        elapsed = duration(item.get("run_elapsed_seconds"))
        if elapsed is not None:
            row["run_elapsed_seconds"] = elapsed
        inputs = item.get("inputs")
        if isinstance(inputs, list):
            kinds = []
            for raw in inputs:
                kind = raw if isinstance(raw, str) and raw in declared_facts else None
                if kind is not None and kind not in kinds:
                    kinds.append(kind)
                if len(kinds) == MAX_TRACE_TRIGGER_KINDS:
                    break
            row["trigger_kinds"] = kinds
        raw_subjects = item.get("subjects")
        if isinstance(raw_subjects, list):
            reported_subject_count = nonnegative(item.get("subject_count"))
            subject_count = max(
                len(raw_subjects), reported_subject_count or 0,
            )
            subject_identities = []
            invalid_subject_count = 0
            for raw_subject in raw_subjects[:64]:
                subject = safe_identity(raw_subject)
                if (subject is None or "/" in subject or "\\" in subject):
                    invalid_subject_count += 1
                    continue
                if subject not in subject_identities:
                    subject_identities.append(subject)
                if len(subject_identities) == MAX_TRACE_TRIGGER_SUBJECT_IDENTITIES:
                    break
            row["semantic_subject_identity_status"] = (
                "complete"
                if subject_count == len(subject_identities)
                and invalid_subject_count == 0
                else "partial"
            )
            row["semantic_subject_identity_count"] = subject_count
            row["semantic_subject_identity_omitted_count"] = max(
                0, subject_count - len(subject_identities),
            )
            row["semantic_subject_identities"] = subject_identities
        process_usage = resources.compact_process_usage(
            item.get("process_resource_usage"),
        )
        if process_usage is not None:
            row["process_resource_usage"] = process_usage
        scheduled.append(row)

    raw_skipped = value.get("skipped")
    skipped_shape_valid = isinstance(raw_skipped, list)
    raw_skipped = raw_skipped if isinstance(raw_skipped, list) else []
    skipped = []
    invalid_skipped_count = 0
    for item in raw_skipped[:MAX_TRACE_OWNER_ROWS]:
        if not isinstance(item, dict):
            invalid_skipped_count += 1
            continue
        owner = safe_identity(item.get("owner") or item.get("pass"))
        if owner is None or owner not in declared_passes:
            invalid_skipped_count += 1
            continue
        row = {"owner": owner}
        reason = safe_text(item.get("reason"))
        if reason is not None:
            row["reason"] = reason
        skipped.append(row)

    raw_failures = value.get("owner_failures")
    failures = []
    invalid_failure_count = 0
    omitted_failure_count = 0
    if isinstance(raw_failures, list):
        omitted_failure_count = max(
            0, len(raw_failures) - MAX_TRACE_OWNER_ROWS,
        )
        for raw in raw_failures[:MAX_TRACE_OWNER_ROWS]:
            owner = safe_identity(raw)
            if owner is None or owner not in declared_passes:
                invalid_failure_count += 1
                continue
            if owner in failures:
                invalid_failure_count += 1
                continue
            failures.append(owner)
    if isinstance(raw_failures, list):
        failure_observation_status = (
            "complete" if not invalid_failure_count and not omitted_failure_count
            else "partial"
        )
    elif "owner_failures" in value:
        failure_observation_status = "invalid"
    else:
        failure_observation_status = "not-recorded"
    for row in scheduled:
        owner = row["owner"]
        row["owner_outcome_status"] = (
            "failure-reported" if owner in failures else
            "no-failure-reported"
            if failure_observation_status == "complete" else
            "unknown"
        )
        # The scheduler observes completion envelopes, not independent
        # correctness. A returned owner pass is not verification evidence.
        row["verification_status"] = "not-established"
    decided_owners = [
        item["owner"] for item in (*scheduled, *skipped)
        if isinstance(item.get("owner"), str)
    ]
    unique_decided_owners = set(decided_owners)
    decision_coverage = (
        "unavailable" if not vocabulary_known else (
            "complete" if (len(unique_decided_owners) == len(declared_passes)
                           and unique_decided_owners == declared_passes
                           and len(decided_owners) == len(unique_decided_owners))
            else "partial"
        )
    )
    result = {
        "scope": "incremental-owner-pass-scheduler-trace",
        "mode": (
            value.get("mode") if isinstance(value.get("mode"), str)
            and value.get("mode") in {
                "recovery_scan", "bootstrap_scan", "targeted_update",
                "bounded_reconciliation",
            } else "unknown"
        ),
        "decision_record_status": (
            "recorded" if (scheduled_shape_valid and skipped_shape_valid
                           and vocabulary_known and not invalid_scheduled_count
                           and not invalid_skipped_count
                           and len(raw_scheduled) <= MAX_TRACE_OWNER_ROWS
                           and len(raw_skipped) <= MAX_TRACE_OWNER_ROWS)
            else "partial"
        ),
        "scheduled_pass_count": len(scheduled),
        "skipped_pass_count": len(skipped),
        "declared_owner_pass_count": len(declared_passes),
        "owner_pass_decision_count": len(unique_decided_owners),
        "owner_pass_decision_coverage": decision_coverage,
        "scheduled_passes": scheduled,
        "skipped_passes": skipped,
        "owner_failure_observation_status": failure_observation_status,
        "owner_failure_count": (
            len(failures)
            if failure_observation_status in {"complete", "partial"}
            else None
        ),
        "owner_failures": failures,
        "ambient_fulfillment": "not-established",
        "agent_need_opportunity_denominator": "not-recorded",
        "measurement_limits": [
            "The trace records only owner-pass decisions visible to the incremental scheduler; it does not count unrecognized service opportunities or agent need.",
            "A returned pass and its wall-clock duration do not establish correctness, agent receipt, or reduced intervention burden.",
            "A skipped pass means the scheduler judged its selected inputs current; it is not a universal freshness guarantee.",
            "Trace persistence distinguishes the current entry response from the session snapshot; snapshot retention does not establish service fulfillment or independent verification.",
        ],
    }
    raw_pending_events = value.get("pending_events")
    if isinstance(raw_pending_events, list):
        pending_rows = raw_pending_events[:MAX_TRACE_PENDING_EVENT_ROWS]
        pending_kind_counts: dict[str, int] = {}
        pending_repository_counts: dict[str, int] = {}
        unclassified_kind_count = 0
        invalid_record_count = 0
        for item in pending_rows:
            if not isinstance(item, dict):
                invalid_record_count += 1
                continue
            kind = safe_identity(item.get("kind"))
            if kind is not None and kind in declared_facts:
                pending_kind_counts[kind] = pending_kind_counts.get(kind, 0) + 1
            else:
                unclassified_kind_count += 1
            repository = safe_identity(item.get("repository"))
            if (repository is not None
                    and ("/" in repository or "\\" in repository)):
                repository = None
            if repository is not None:
                pending_repository_counts[repository] = (
                    pending_repository_counts.get(repository, 0) + 1
                )
        pending_omitted_count = max(
            0, len(raw_pending_events) - len(pending_rows),
        )
        pending_coverage = (
            "possibly-truncated-at-trace-limit"
            if len(raw_pending_events) >= MAX_TRACE_PENDING_EVENT_ROWS
            else "complete-selected-trace"
        )
        result["pending_event_observation"] = {
            "status": (
                "partial" if invalid_record_count or pending_omitted_count
                else "recorded"
            ),
            "scope": "selected-trace-pending-events",
            "causal_status": "not-established",
            "coverage_status": pending_coverage,
            "record_count": len(raw_pending_events),
            "returned_count": len(pending_rows),
            "omitted_count": pending_omitted_count,
            "invalid_record_count": invalid_record_count,
            "unclassified_event_kind_count": unclassified_kind_count,
            "kind_counts": dict(sorted(pending_kind_counts.items())[:12]),
            "kind_count_omitted": max(0, len(pending_kind_counts) - 12),
            "repository_counts": dict(
                sorted(pending_repository_counts.items())[:12]
            ),
            "repository_count_omitted": max(
                0, len(pending_repository_counts) - 12,
            ),
        }
    elif "pending_events" in value:
        result["pending_event_observation"] = {
            "status": "invalid",
            "scope": "selected-trace-pending-events",
            "causal_status": "not-established",
            "coverage_status": "invalid",
        }
    else:
        result["pending_event_observation"] = {
            "status": "not-recorded",
            "scope": "selected-trace-pending-events",
            "causal_status": "not-established",
            "coverage_status": "not-recorded",
        }
    raw_owner_results = value.get("owner_pass_results")
    if isinstance(raw_owner_results, list):
        scheduled_execution_ids = {
            (row.get("owner"), row.get("service_execution_id"))
            for row in scheduled
            if isinstance(row.get("service_execution_id"), str)
        }
        projected_owner_results = []
        invalid_owner_result_count = 0
        seen_owner_result_ids = set()
        for item in raw_owner_results[:MAX_TRACE_OWNER_ROWS]:
            if not isinstance(item, dict):
                invalid_owner_result_count += 1
                continue
            owner = safe_identity(item.get("owner"))
            execution_id = item.get("service_execution_id")
            if (owner is None or not isinstance(execution_id, str)
                    or re.fullmatch(r"ase_[0-9a-f]{32}", execution_id) is None
                    or (owner, execution_id) not in scheduled_execution_ids):
                invalid_owner_result_count += 1
                continue
            owner_result_id = (owner, execution_id)
            if owner_result_id in seen_owner_result_ids:
                invalid_owner_result_count += 1
                continue
            status = item.get("result_status")
            if status not in {
                "result-recorded", "runner-returned-none", "result-missing",
                "invalid-result-shape",
            }:
                invalid_owner_result_count += 1
                continue
            seen_owner_result_ids.add(owner_result_id)
            row = {
                "owner": owner,
                "service_execution_id": execution_id,
                "result_status": status,
                "verification_status": "not-established",
            }
            identity = item.get("result_identity")
            if isinstance(identity, str) and re.fullmatch(r"[0-9a-f]{64}", identity):
                row["result_identity"] = identity
            source_reference_status = item.get("source_reference_status")
            if source_reference_status in {
                "store-record", "session-artifact", "trace-not-persisted",
                "not-recorded", "identity-mismatch", "invalid-reference",
            }:
                row["source_reference_status"] = source_reference_status
            reference = item.get("source_reference")
            if (source_reference_status == "store-record"
                    and isinstance(reference, str)
                    and safe_session_id is not None
                    and reference.startswith(
                        f"mncs-store:{safe_session_id}:incremental-coherence:{owner}:"
                    )
                    and re.fullmatch(r"[0-9a-f]{64}", reference.rsplit(":", 1)[-1])
                    and re.fullmatch(
                        r"mncs-store:[A-Za-z0-9_.:-]{1,240}", reference,
                    )):
                row["source_reference"] = reference
            elif (source_reference_status == "session-artifact"
                  and isinstance(reference, str)
                  and safe_session_id is not None
                  and reference.startswith(f"sessions/{safe_session_id}/coherence-results/")
                  and re.fullmatch(
                      rf"sessions/{re.escape(safe_session_id)}/coherence-results/[0-9a-f]{{64}}\.json",
                      reference,
                  )):
                row["source_reference"] = reference
            verification_reference_status = item.get(
                "verification_evidence_reference_status",
            )
            verification_reference = item.get(
                "verification_evidence_reference",
            )
            if owner == "verification":
                if verification_reference_status is None:
                    if verification_reference is not None:
                        invalid_owner_result_count += 1
                        verification_reference_status = "invalid-reference"
                    else:
                        verification_reference_status = "not-recorded-on-trace"
                elif verification_reference_status in {
                    "store-record", "session-artifact",
                }:
                    checked_reference, checked_status = (
                        _safe_verification_evidence_reference(
                            safe_session_id, verification_reference,
                        )
                    )
                    if checked_status != verification_reference_status:
                        invalid_owner_result_count += 1
                        verification_reference_status = "invalid-reference"
                    else:
                        row["verification_evidence_reference"] = (
                            checked_reference
                        )
                elif verification_reference_status not in {
                    "not-recorded", "not-recorded-on-trace",
                    "not-recorded-in-history", "invalid-reference",
                } or verification_reference is not None:
                    invalid_owner_result_count += 1
                    verification_reference_status = "invalid-reference"
                row["verification_evidence_reference_status"] = (
                    verification_reference_status
                )
            elif (verification_reference_status is not None
                  or verification_reference is not None):
                invalid_owner_result_count += 1
            operation_status = item.get("operation_status")
            if (isinstance(operation_status, str)
                    and re.fullmatch(r"[a-z][a-z0-9-]{0,63}", operation_status)):
                row["operation_status"] = operation_status
            result_code = item.get("result_code")
            if (isinstance(result_code, str)
                    and re.fullmatch(r"[a-z][a-z0-9.-]{0,79}", result_code)):
                row["result_code"] = result_code
            outcome_disposition = item.get("outcome_disposition")
            outcome_reason = item.get("outcome_reason_code")
            if (outcome_disposition in {"completed-quietly", "disabled"}
                    and outcome_reason in OWNER_PASS_QUIET_REASONS
                    and (outcome_disposition == "disabled")
                    == (outcome_reason == "disabled-by-definition")
                    and item.get("agent_response_emitted") is False):
                row["outcome_disposition"] = outcome_disposition
                row["outcome_reason_code"] = outcome_reason
                row["agent_response_emitted"] = False
            reused = item.get("result_reused")
            if type(reused) is bool:
                row["result_reused"] = reused
            raw_summary = item.get("summary")
            if isinstance(raw_summary, dict):
                summary = {}
                for key in sorted(raw_summary):
                    if (not isinstance(key, str)
                            or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key) is None):
                        continue
                    summary_value = raw_summary[key]
                    if (type(summary_value) is int and summary_value >= 0
                            or type(summary_value) is bool
                            or type(summary_value) is float
                            and 0 <= summary_value <= 86400):
                        summary[key] = summary_value
                    if len(summary) >= MAX_TRACE_RESULT_FIELDS:
                        break
                if summary:
                    row["summary"] = summary
            availability_status = item.get("service_availability_status")
            availability_reason = item.get(
                "service_availability_reason_code",
            )
            if (availability_status == "unavailable"
                    and availability_reason
                    in OWNER_PASS_SERVICE_AVAILABILITY_CODES.values()
                    and OWNER_PASS_SERVICE_AVAILABILITY_CODES.get(
                        (owner, "vocabulary-unavailable"),
                    ) == availability_reason):
                row["service_availability_status"] = availability_status
                row["service_availability_reason_code"] = availability_reason
            projected_owner_results.append(row)
        result["owner_pass_results"] = {
            "status": (
                "recorded" if (len(projected_owner_results) == len(scheduled)
                               and len(seen_owner_result_ids) == len(scheduled_execution_ids)
                               and seen_owner_result_ids == scheduled_execution_ids
                               and not invalid_owner_result_count
                               and len(raw_owner_results) <= MAX_TRACE_OWNER_ROWS)
                else "partial"
            ),
            "scheduled_owner_count": len(scheduled),
            "result_record_count": len(projected_owner_results),
            "results": projected_owner_results,
            "ambient_fulfillment_status": "not-established",
            "measurement_limits": [
                "Each result is related to a scheduled owner pass by its exact execution identity and content identity; the reference is a source pointer, not independent verification.",
                "A returned or reused result does not establish service fulfillment, agent receipt, or reduced intervention burden.",
            ],
        }
        outcome_disposition_counts: dict[str, int] = {}
        for row in projected_owner_results:
            disposition = row.get("outcome_disposition")
            if disposition in {"completed-quietly", "disabled"}:
                outcome_disposition_counts[disposition] = (
                    outcome_disposition_counts.get(disposition, 0) + 1
                )
        if outcome_disposition_counts:
            result["owner_pass_results"][
                "outcome_disposition_counts"
            ] = dict(sorted(outcome_disposition_counts.items()))
        if invalid_owner_result_count:
            result["owner_pass_results"]["invalid_result_records"] = (
                invalid_owner_result_count
            )
        if len(raw_owner_results) > MAX_TRACE_OWNER_ROWS:
            result["owner_pass_results"]["result_records_omitted"] = (
                len(raw_owner_results) - MAX_TRACE_OWNER_ROWS
            )
    persistence_status = value.get("trace_persistence_status")
    if persistence_status in {"session-snapshot", "entry-response-only"}:
        result["trace_persistence_status"] = persistence_status
    publication_status = value.get("result_reference_publication_status")
    if publication_status in {"complete", "interrupted", "failed"}:
        result["result_reference_publication_status"] = publication_status
    publication_elapsed = duration(
        value.get("result_reference_publication_elapsed_seconds"),
    )
    if publication_elapsed is not None:
        result["result_reference_publication_elapsed_seconds"] = (
            publication_elapsed
        )
    publication_usage = resources.compact_process_usage(
        value.get("result_reference_publication_process_resource_usage"),
    )
    if publication_usage is not None:
        result["result_reference_publication_process_resource_usage"] = (
            publication_usage
        )
    for key in (
        "result_reference_store_reference_reuse_count",
        "result_reference_store_resolution_attempt_count",
        "result_reference_store_resolution_success_count",
    ):
        count = nonnegative(value.get(key))
        if count is not None:
            result[key] = count
    if publication_status in {"interrupted", "failed"}:
        result["measurement_limits"].append(
            "Owner passes returned, but immutable result-reference publication did not complete. This snapshot preserves scheduler and process-resource evidence where its save succeeded; owner result pointers may be missing and replay cursors remain at their prior values.",
        )
    reason = safe_text(value.get("reason"))
    if reason is not None:
        result["reason"] = reason
    event_count = nonnegative(value.get("event_count"))
    if event_count is not None:
        result["observed_event_count"] = event_count
    elapsed = duration(value.get("elapsed_seconds"))
    if elapsed is not None:
        result["scheduler_elapsed_seconds"] = elapsed
    if len(raw_scheduled) > len(scheduled):
        result["scheduled_passes_omitted"] = len(raw_scheduled) - len(scheduled)
    if len(raw_skipped) > len(skipped):
        result["skipped_passes_omitted"] = len(raw_skipped) - len(skipped)
    if invalid_scheduled_count:
        result["scheduled_pass_records_invalid"] = invalid_scheduled_count
    if invalid_skipped_count:
        result["skipped_pass_records_invalid"] = invalid_skipped_count
    if invalid_failure_count:
        result["owner_failure_records_invalid"] = invalid_failure_count
    if omitted_failure_count:
        result["owner_failure_records_omitted"] = omitted_failure_count
    return result


def project_owner_execution_history(
    value, *, session_id: str | None = None,
) -> dict:
    """Validate the bounded session history of exact owner execution rows."""
    unavailable = {
        "schema_version": OWNER_EXECUTION_HISTORY_SCHEMA,
        "status": "not-recorded",
        "coverage_status": "not-recorded",
        "limit": MAX_OWNER_EXECUTION_HISTORY,
        "record_count": 0,
        "omitted_count": 0,
        "invalid_record_count": 0,
        "trace_sequence": 0,
        "rows": [],
    }
    if value is None:
        return unavailable
    if (not isinstance(value, dict)
            or value.get("schema_version") not in {
                OWNER_EXECUTION_HISTORY_SCHEMA,
                LEGACY_OWNER_EXECUTION_HISTORY_SCHEMA,
            }):
        return {**unavailable, "status": "invalid", "invalid_record_count": 1}
    coverage_status = value.get("coverage_status")
    if coverage_status != "bounded-since-enabled":
        coverage_status = "unknown"
    limit = value.get("limit")
    if type(limit) is not int or not 1 <= limit <= MAX_OWNER_EXECUTION_HISTORY:
        limit = MAX_OWNER_EXECUTION_HISTORY
    omitted = value.get("omitted_count", 0)
    if type(omitted) is not int or omitted < 0:
        omitted = 0
        invalid_metadata = 1
    else:
        invalid_metadata = 0
    trace_sequence = value.get("trace_sequence", 0)
    if type(trace_sequence) is not int or trace_sequence < 0:
        trace_sequence = 0
        invalid_metadata += 1

    try:
        declared_passes = set(declarations().get("passes", {}))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        declared_passes = set()
    raw_rows = value.get("rows")
    if not isinstance(raw_rows, list):
        raw_rows = []
        invalid_metadata += 1
    rows = []
    seen_execution_ids = set()
    invalid_count = invalid_metadata
    for raw in raw_rows[:MAX_OWNER_EXECUTION_HISTORY]:
        if not isinstance(raw, dict):
            invalid_count += 1
            continue
        owner = raw.get("owner")
        execution_id = raw.get("service_execution_id")
        sequence = raw.get("trace_sequence")
        if (not isinstance(owner, str) or owner not in declared_passes
                or not isinstance(execution_id, str)
                or re.fullmatch(r"ase_[0-9a-f]{32}", execution_id) is None
                or type(sequence) is not int or sequence < 1
                or execution_id in seen_execution_ids):
            invalid_count += 1
            continue
        result_observation_status = raw.get(
            "owner_result_observation_status", "unknown",
        )
        if result_observation_status not in {
            "recorded", "not-recorded-on-trace",
            "not-recorded-in-results", "unknown",
        }:
            invalid_count += 1
            continue
        owner_result_status = raw.get("owner_result_status")
        if result_observation_status == "recorded":
            if owner_result_status not in OWNER_RESULT_STATUSES:
                invalid_count += 1
                continue
        elif owner_result_status is not None:
            invalid_count += 1
            continue
        owner_result_verification_status = raw.get(
            "owner_result_verification_status", "unknown",
        )
        if owner_result_verification_status not in {
            "not-established", "unknown",
        }:
            invalid_count += 1
            continue
        owner_result_identity = raw.get("owner_result_identity")
        if (owner_result_identity is not None
                and (not isinstance(owner_result_identity, str)
                     or re.fullmatch(r"[0-9a-f]{64}", owner_result_identity)
                     is None)):
            invalid_count += 1
            continue
        owner_result_reference_status = raw.get(
            "owner_result_reference_status", "unknown",
        )
        if (owner_result_reference_status != "unknown"
                and owner_result_reference_status
                not in OWNER_RESULT_REFERENCE_STATUSES):
            invalid_count += 1
            continue
        owner_result_reused = raw.get("owner_result_reused")
        if owner_result_reused is not None and type(owner_result_reused) is not bool:
            invalid_count += 1
            continue
        resource_status = raw.get("process_resource_usage_status")
        if resource_status not in {
            "measured", "partial", "unavailable", "not-recorded-on-trace",
            "invalid-trace-record",
        }:
            invalid_count += 1
            continue
        process_usage = resources.compact_process_usage(
            raw.get("process_resource_usage"),
        )
        if process_usage is not None:
            if process_usage.get("status") != resource_status:
                invalid_count += 1
                continue
        elif resource_status not in {
            "not-recorded-on-trace", "invalid-trace-record",
        }:
            invalid_count += 1
            continue
        elapsed = raw.get("run_elapsed_seconds")
        if (elapsed is not None
                and (type(elapsed) not in (int, float)
                     or not 0 <= elapsed <= 86400)):
            invalid_count += 1
            continue
        outcome = raw.get("owner_outcome_status")
        if outcome not in {
            "failure-reported", "no-failure-reported", "unknown",
        }:
            outcome = "unknown"
        verification = raw.get("verification_status")
        if verification != "not-established":
            verification = "unknown"
        row = {
            "owner": owner,
            "service_execution_id": execution_id,
            "trace_sequence": sequence,
            "owner_outcome_status": outcome,
            "verification_status": verification,
            "owner_result_observation_status": result_observation_status,
            "owner_result_verification_status": (
                owner_result_verification_status
            ),
            "owner_result_reference_status": owner_result_reference_status,
            "process_resource_usage_status": resource_status,
        }
        if owner_result_status is not None:
            row["owner_result_status"] = owner_result_status
        if owner_result_identity is not None:
            row["owner_result_identity"] = owner_result_identity
        owner_result_reference = raw.get("owner_result_reference")
        if owner_result_reference is not None:
            valid_owner_reference = False
            if (owner_result_reference_status == "store-record"
                    and isinstance(owner_result_reference, str)):
                owner_reference_pattern = (
                    rf"mncs-store:{re.escape(session_id)}:"
                    rf"incremental-coherence:{re.escape(owner)}:[0-9a-f]{{64}}"
                    if isinstance(session_id, str) else
                    rf"mncs-store:ses_[A-Za-z0-9_-]{{1,128}}:"
                    rf"incremental-coherence:{re.escape(owner)}:[0-9a-f]{{64}}"
                )
                valid_owner_reference = re.fullmatch(
                    owner_reference_pattern, owner_result_reference,
                ) is not None
            elif (owner_result_reference_status == "session-artifact"
                  and isinstance(owner_result_reference, str)):
                session_pattern = (
                    re.escape(session_id) if isinstance(session_id, str)
                    else r"ses_[A-Za-z0-9_-]{1,128}"
                )
                valid_owner_reference = re.fullmatch(
                    rf"sessions/{session_pattern}/coherence-results/"
                    r"[0-9a-f]{64}\.json",
                    owner_result_reference,
                ) is not None
            if not valid_owner_reference:
                invalid_count += 1
                continue
            row["owner_result_reference"] = owner_result_reference
        elif owner_result_reference_status in {"store-record", "session-artifact"}:
            # Legacy rows preserve the reference status but did not retain the
            # exact pointer. Keep that distinction instead of fabricating one.
            row["owner_result_reference_status"] = (
                "not-recorded-in-history"
            )
        verification_reference_status = raw.get(
            "verification_evidence_reference_status",
            "not-recorded-in-history",
        )
        if verification_reference_status not in {
            "store-record", "session-artifact", "not-recorded",
            "not-recorded-on-trace", "not-recorded-in-history",
            "invalid-reference",
        }:
            invalid_count += 1
            continue
        verification_reference = raw.get(
            "verification_evidence_reference",
        )
        if owner != "verification" and (
            verification_reference is not None
            or "verification_evidence_reference_status" in raw
        ):
            invalid_count += 1
            continue
        if verification_reference_status in {"store-record", "session-artifact"}:
            checked_reference, checked_status = (
                _safe_verification_evidence_reference(
                    session_id, verification_reference,
                )
            )
            if checked_status != verification_reference_status:
                invalid_count += 1
                continue
            row["verification_evidence_reference"] = checked_reference
        elif verification_reference is not None:
            invalid_count += 1
            continue
        if owner == "verification":
            row["verification_evidence_reference_status"] = (
                verification_reference_status
            )
        if type(owner_result_reused) is bool:
            row["owner_result_reused"] = owner_result_reused
        if type(elapsed) in (int, float):
            row["run_elapsed_seconds"] = round(float(elapsed), 3)
        if process_usage is not None:
            row["process_resource_usage"] = process_usage
        seen_execution_ids.add(execution_id)
        rows.append(row)
    invalid_count += max(0, len(raw_rows) - MAX_OWNER_EXECUTION_HISTORY)
    status = "partial" if invalid_count else "observed"
    if coverage_status == "unknown":
        status = "partial"
    return {
        "schema_version": OWNER_EXECUTION_HISTORY_SCHEMA,
        "status": status,
        "coverage_status": coverage_status,
        "limit": limit,
        "record_count": len(rows),
        "omitted_count": omitted + max(
            0, len(raw_rows) - MAX_OWNER_EXECUTION_HISTORY,
        ),
        "invalid_record_count": invalid_count,
        "trace_sequence": trace_sequence,
        "rows": rows,
    }


def _append_owner_execution_history(
    previous, trace: dict, *, trace_sequence: int, session_id: str,
) -> dict:
    """Append path-free owner evidence and retain only the newest 16 rows."""
    prior = project_owner_execution_history(
        previous, session_id=session_id,
    )
    retained = {
        row["service_execution_id"]: row for row in prior["rows"]
    }
    projected_trace = project_trace(trace, session_id=session_id)
    projected_rows = (
        projected_trace.get("scheduled_passes", [])
        if isinstance(projected_trace, dict) else []
    )
    raw_scheduled = trace.get("scheduled")
    raw_by_execution = {
        row.get("service_execution_id"): row
        for row in raw_scheduled if isinstance(row, dict)
        and isinstance(row.get("service_execution_id"), str)
    } if isinstance(raw_scheduled, list) else {}
    owner_result_projection = (
        projected_trace.get("owner_pass_results")
        if isinstance(projected_trace, dict) else None
    )
    owner_result_rows = (
        owner_result_projection.get("results", [])
        if isinstance(owner_result_projection, dict) else []
    )
    owner_result_rows = (
        owner_result_rows if isinstance(owner_result_rows, list) else []
    )
    owner_results_by_execution = {
        (row.get("owner"), row.get("service_execution_id")): row
        for row in owner_result_rows if isinstance(row, dict)
        and isinstance(row.get("owner"), str)
        and isinstance(row.get("service_execution_id"), str)
    }
    seen_current = set()
    invalid_current = 0
    for projected in projected_rows:
        execution_id = projected.get("service_execution_id")
        owner = projected.get("owner")
        raw = raw_by_execution.get(execution_id)
        if (not isinstance(execution_id, str) or not isinstance(owner, str)
                or not isinstance(raw, dict) or execution_id in seen_current):
            invalid_current += 1
            continue
        seen_current.add(execution_id)
        has_sample = "process_resource_usage" in raw
        process_usage = resources.compact_process_usage(
            raw.get("process_resource_usage"),
        )
        resource_status = (
            process_usage["status"] if process_usage is not None else
            "invalid-trace-record" if has_sample else "not-recorded-on-trace"
        )
        row = {
            "owner": owner,
            "service_execution_id": execution_id,
            "trace_sequence": trace_sequence,
            "owner_outcome_status": projected.get(
                "owner_outcome_status", "unknown",
            ),
            "verification_status": projected.get(
                "verification_status", "unknown",
            ),
            "process_resource_usage_status": resource_status,
        }
        owner_result = owner_results_by_execution.get((owner, execution_id))
        if isinstance(owner_result, dict):
            row["owner_result_observation_status"] = "recorded"
            row["owner_result_status"] = owner_result["result_status"]
            row["owner_result_verification_status"] = owner_result.get(
                "verification_status", "unknown",
            )
            row["owner_result_reference_status"] = owner_result.get(
                "source_reference_status", "not-recorded",
            )
            owner_reference = owner_result.get("source_reference")
            if (owner_result.get("source_reference_status")
                    in {"store-record", "session-artifact"}
                    and isinstance(owner_reference, str)):
                row["owner_result_reference"] = owner_reference
            result_identity = owner_result.get("result_identity")
            if isinstance(result_identity, str):
                row["owner_result_identity"] = result_identity
            result_reused = owner_result.get("result_reused")
            if type(result_reused) is bool:
                row["owner_result_reused"] = result_reused
            if owner == "verification":
                evidence_reference_status = owner_result.get(
                    "verification_evidence_reference_status",
                    "not-recorded-on-trace",
                )
                row["verification_evidence_reference_status"] = (
                    evidence_reference_status
                )
                evidence_reference = owner_result.get(
                    "verification_evidence_reference",
                )
                if isinstance(evidence_reference, str):
                    row["verification_evidence_reference"] = (
                        evidence_reference
                    )
        else:
            row["owner_result_observation_status"] = (
                "not-recorded-in-results"
                if isinstance(owner_result_projection, dict)
                else "not-recorded-on-trace"
            )
            row["owner_result_verification_status"] = "unknown"
            row["owner_result_reference_status"] = "unknown"
        elapsed = projected.get("run_elapsed_seconds")
        if type(elapsed) in (int, float):
            row["run_elapsed_seconds"] = elapsed
        if process_usage is not None:
            row["process_resource_usage"] = process_usage
        retained.pop(execution_id, None)
        retained[execution_id] = row
    all_rows = list(retained.values())
    dropped = max(0, len(all_rows) - MAX_OWNER_EXECUTION_HISTORY)
    rows = all_rows[-MAX_OWNER_EXECUTION_HISTORY:]
    omitted = min(
        10**12,
        prior["omitted_count"] + prior["invalid_record_count"] + dropped
        + invalid_current,
    )
    projected_invalid = 0
    if isinstance(projected_trace, dict):
        projected_invalid += projected_trace.get(
            "scheduled_pass_records_invalid", 0,
        )
        projected_invalid += projected_trace.get("scheduled_passes_omitted", 0)
    omitted = min(10**12, omitted + projected_invalid)
    return {
        "schema_version": OWNER_EXECUTION_HISTORY_SCHEMA,
        "coverage_status": "bounded-since-enabled",
        "limit": MAX_OWNER_EXECUTION_HISTORY,
        "trace_sequence": trace_sequence,
        "omitted_count": omitted,
        "rows": rows,
    }


def declarations() -> dict:
    result = json.loads(INPUTS_PATH.read_text())
    if result.get("schema_version") != "mncs.environment.coherence-pass-inputs/1":
        raise ValueError("invalid coherence input declaration")
    if len(result["passes"]) > 16 or len(result["facts"]) > 32 or len(set(result["facts"])) != len(result["facts"]):
        raise ValueError("coherence declaration exceeds native bounds")
    for indices in result["passes"].values():
        if len(set(indices)) != len(indices) or any(type(i) is not int or not 0 <= i < len(result["facts"]) for i in indices):
            raise ValueError("invalid coherence input subscription")
    return result


def _roots(session) -> dict[str, Path]:
    selected = session.snapshot.get("selected_checkouts") or {}
    root = Path(session.snapshot["workspace"]["root"])
    if selected:
        return {name: (root / record["path"]).resolve() for name, record in selected.items()
                if isinstance(record, dict) and record.get("path")}
    return {record["name"]: Path(record["path"]).resolve()
            for record in session.snapshot["workspace"].get("repositories", [])}


def _service_execution_id(session, owner_pass: str) -> str:
    return "ase_" + digest_hex({
        "session_id": session.session_id,
        "owner_pass": owner_pass,
        "nonce": secrets.token_hex(16),
    }, length=32)


_resident_adapters = {}


def _resident_provider(session, root, binary):
    """Load only the selected owner's declared resident transport."""
    manifest = json.loads((root / '.mncs/project.json').read_text())
    declaration = next((item for item in manifest.get('contracts', {}).get('provides', [])
                        if item.get('contract') == 'automation-coherence-routing'), {})
    adapter = declaration.get('resident_adapter')
    if not adapter:
        return None
    roots = _roots(session)
    roots['mncs-automation'] = root
    specification = json.loads((root / declaration['artifact_declaration']).read_text())
    repositories = {item['repository'] for item in specification['inputs']}
    repositories.add(adapter['runtime_repository'])
    for repository in repositories:
        if repository not in roots:
            override = os.environ.get('MNCS_' + repository.removeprefix('mncs-').replace('-', '_').upper() + '_ROOT')
            if repository == 'MNCS-Commons':
                override = os.environ.get('MNCS_COMMONS_ROOT')
            if override:
                roots[repository] = Path(override).resolve()
    if not repositories.issubset(roots):
        return None  # older selected owners retain source-call recovery; no exact receipt
    path = (root / adapter['path']).resolve()
    runtime_root = roots[adapter['runtime_repository']]
    runtime_path = (runtime_root / adapter['runtime_path']).resolve()
    if not path.is_relative_to(root) or not runtime_path.is_relative_to(runtime_root):
        raise ValueError('resident adapter escapes selected provider')
    if str(runtime_path) not in sys.path:
        sys.path.insert(0, str(runtime_path))
    # Source changes create a fresh adapter, not a stale module singleton.
    key = (str(path), observations.observe_artifact(path)['artifact_identity'].removeprefix('sha256:'))
    if key not in _resident_adapters:
        spec = importlib.util.spec_from_file_location('mncs_owner_coherence_' + key[1], path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _resident_adapters[key] = module
    return _resident_adapters[key], adapter['function'], roots


def _native(session, function: str, rows: list[list[int]]) -> tuple[list[int], dict]:
    from .family import find_mncs_binary
    selected = _roots(session)
    root = os.environ.get("MNCS_AUTOMATION_ROOT") or selected.get("mncs-automation")
    if root is None:
        raise ValueError("selected native Automation coherence provider unavailable")
    root = Path(root)
    source = root / "native/mncs/automation/coherence.mncs"
    binary = find_mncs_binary(session)
    if not binary or not source.is_file():
        raise ValueError("native Automation coherence artifact unavailable")
    args = [{"sequence": {"values": [{"sequence": {"values": [
        {"integer": {"value": int(value)}} for value in row]}} for row in rows]}}]
    command = [binary, "call", str(source), "--module", "mncs.automation.coherence.v1",
               "--function", function, "--args-json", json.dumps(args),
               "--library", str(root / "native"), "--cache-dir",
               str(session.state_dir / "provider-cache" / "automation-coherence")]
    retained = _resident_provider(session, root, binary)
    if retained is not None:
        module, entrypoint, roots = retained
        # The canonical embed ABI includes integer types; source-call CLI
        # historically accepted omitted types as its own input shorthand.
        for argument in args:
            for row in argument['sequence']['values']:
                for value in row['sequence']['values']:
                    value['integer']['type'] = {'bits': 64, 'signed': False}
        try:
            returned, execution = getattr(module, entrypoint)(roots=roots, compiler=binary,
                embed=Path(binary).parent / 'libmncs_embed.so',
                cache=session.state_dir / 'provider-cache', function=function, arguments=args)
        except RuntimeError as error:
            raise ValueError('owner artifact reconcile/execution unavailable: ' + str(error)) from error
        if returned.get('status') != 'returned':
            raise ValueError('retained coherence call did not return')
        values = returned['returned'][0]['sequence']['values']
        codes = [int(value['integer']['value']) for value in values]
        admitted = {0, 1} if function == 'scoped_events' else {0, 1, 2, 3} if function == 'route_batch' else {0, 1, 2, 10, 13}
        if len(codes) != len(rows) or any(code not in admitted for code in codes):
            raise ValueError('incomplete retained coherence batch')
        if callable(getattr(session.store, 'put_record', None)):
            for record in (execution.get('build'), execution):
                if not isinstance(record, dict):
                    continue
                address = (session.session_id + ':provider-receipt:' + record['identity']).encode()
                schema = record['schema_version'].encode()
                existing = session.store.get_record(schema, address)
                if existing is None:
                    session.store.put_record(schema, address, record)
                elif existing != record:
                    raise ValueError('immutable owner receipt payload differs')
        return codes, execution
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    document = json.loads(result.stdout)
    if result.returncode or document.get("status") != "returned":
        raise ValueError("native Automation coherence decision unavailable")
    values = document["call"]["returned"][0]["sequence"]["values"]
    codes = [int(value["integer"]["value"]) for value in values]
    admitted = {0, 1} if function == "scoped_events" else {0, 1, 2, 3} if function == "route_batch" else {0, 1, 2, 10, 13}
    if len(codes) != len(rows) or any(code not in admitted for code in codes):
        raise ValueError("incomplete native coherence batch")
    receipt = {key: document["call"].get(key) for key in
               ("artifact_identity", "artifact_sha256", "backend")}
    return codes, receipt


def _policy_identity(session, prior: dict) -> tuple[str, dict]:
    roots = _roots(session)
    automation = os.environ.get("MNCS_AUTOMATION_ROOT") or roots.get("mncs-automation")
    # Entry executes these host adapters in this package. An implementation
    # change cannot reuse results classified by the previous adapter code.
    files = [INPUTS_PATH, *sorted(Path(__file__).parent.glob("*.py"))]
    if automation:
        manifest = Path(automation) / '.mncs/project.json'
        files.append(manifest)
        if manifest.is_file():
            declarations = json.loads(manifest.read_text()).get('contracts', {}).get('provides', [])
            provider = next((item for item in declarations if item.get('contract') == 'automation-coherence-routing'), {})
            if provider.get('artifact_declaration'):
                declared = Path(automation) / provider['artifact_declaration']
                files.append(declared)
                specification = json.loads(declared.read_text())
                bound = dict(roots, **{'mncs-automation': Path(automation)})
                for repository, variable in [('MNCS-Commons', 'MNCS_COMMONS_ROOT'), ('mncs-doctor', 'MNCS_DOCTOR_ROOT'), ('mncs-forge', 'MNCS_FORGE_ROOT')]:
                    if os.environ.get(variable):
                        bound.setdefault(repository, Path(os.environ[variable]))
                files.extend(bound[item['repository']] / item['path'] for item in specification['inputs']
                             if item['repository'] in bound)
                if 'mncs-forge' in bound:
                    files.extend(bound['mncs-forge'] / 'src/mncs_forge' / name for name in
                                 ('provider_artifacts.py', 'retained_embed.py'))
        files.extend(Path(automation) / "native/mncs/automation" / name
                     for name in ("coherence.mncs", "revisit.mncs"))
    from .family import find_mncs_binary
    binary = find_mncs_binary(session)
    if binary:
        files.append(Path(binary))
    artifacts = {str(path): observations.observe_artifact(path, prior.get(str(path))) for path in files}
    return digest_hex(artifacts), artifacts


def _runtime_due(session, definition: dict) -> bool:
    # A provider without an admitted invalidation declaration still needs live
    # observation. Only the checkout-observation service is static today.
    covered = {item.get('service_identity') for item in _streams(session, definition)}
    return any(service.get("observation_inputs") != ["selected-repositories"] and service.get('identity') not in covered
               for service in definition.get("services", []))


def _event_mask(events: list[dict], spec: dict) -> int:
    mask = 0
    vocabulary = {name: 1 << index for index, name in enumerate(spec["facts"])}
    for event in events:
        mask |= vocabulary.get(event["kind"], 0)
    return mask


def _file_events(session, events: list[dict]) -> list[dict]:
    files = [event for event in events if event["kind"] == "file.changed"]
    if not files:
        return [event for event in events if event['kind'] != 'file.changed']
    from . import projection_sources, projections
    root = Path(session.snapshot['workspace']['root'])
    declarations, _ = projections.discover_selected_declarations(session, root)
    targets = {(d['checkout'], d['output']): d for d in declarations}
    rows = []
    for event in files:
        name = event["path"]
        path = Path(name)
        declared = name in ("family-semantic-contracts-v1.json", "stdlib-manifest.json",
                            "dist/stdlib-bundle.json") or name.startswith(".mncs/")
        projection = targets.get((event['checkout'], name))
        current = False
        if projection:
            shared = projections._read_shared_row(session, projection['id']) or {}
            observed = projection_sources.output_identity(Path(event['checkout']), projection)
            expected = (shared.get('source') or {}).get('owned_digest')
            if expected is None:
                expected = shared.get('rendered_digest')
            current = observed is not None and observed == expected
        rows.append([int(declared), int(path.suffix in (".md", ".txt", ".rst")),
                     int(projection is not None), int(current)])
    # Classification is native; the host supplies filesystem spelling facts.
    codes = []
    for offset in range(0, len(rows), 16):
        function = 'classify_projection_files' if targets else 'classify_files'
        batch_rows = rows[offset:offset + 16]
        if not targets:
            batch_rows = [row[:2] for row in batch_rows]
        batch, _ = _native(session, function, batch_rows)
        codes.extend(batch)
    names = {1: "declaration.changed", 2: "source.changed", 10: 'projection.changed', 13: "prose.changed"}
    return [{**event, "kind": names[code]} for event, code in zip(files, codes) if code != 0] + [
        event for event in events if event["kind"] != "file.changed"]


def _store_events(session, prior: dict, definition: dict | None = None) -> tuple[list[dict], str | None, bool]:
    if not callable(getattr(session.store, "generation", None)):
        # Explicit debug backend has no commit stream: read its bounded rows.
        versions = dict(session.store.read_projection_versions())
        # Own contributor bookkeeping is not an invalidation of this session.
        # Peers still observe that row through their respective cursors.
        versions.pop('family:contributor/' + session.session_id, None)
        material = {"claims": session.store.read_claims(), "rows": versions}
        cursor = digest_hex(material)
        changed = prior.get("file_cursor") not in (None, cursor)
        return ([{"kind": "claim.changed"}, {"kind": "family.changed"}] if changed else [], cursor, True)
    result = sources.StoreReplaySource(session.store, session.session_id + ":").observe(prior.get("store_cursor"))
    if result.status == "reset":
        # A bounded Store replay gap is recoverable only after a complete
        # owner pass. Keep the durable cursor at its acknowledged value and
        # carry the observed high-water separately as a candidate.
        return ([{"kind": "store.reconcile_required", "candidate_cursor": result.cursor,
                  "reason": result.detail}], prior.get("store_cursor"), True)
    if result.status != "ok":
        return [], prior.get("store_cursor"), False
    selected = set(_roots(session))
    publications = (definition or {}).get('coherence_publications') or []
    events = []
    for item in result.events:
        if item.kind == "claim.changed":
            repository = item.relations.get("claim")
            if repository in selected:
                events.append({"kind": item.kind, "repository": repository})
        elif item.kind == "store.changed":
            identity = item.provenance.get("domain_identity", "")
            if identity.startswith('family:contributor/' + session.session_id + ':'):
                continue
            if identity.startswith("family:"):
                events.append({"kind": "family.changed", "subject": identity})
            elif identity.startswith("entry:index/"):
                continue
            else:
                declared = next((entry for entry in publications if entry['schema'] == item.provenance.get('domain_schema')), None)
                if declared:
                    row = session.store.get_record(declared['schema'].encode(), identity.encode())
                    if not isinstance(row, dict):
                        return events, result.cursor, False
                    repository = row.get(declared.get('repository_field', 'repository'))
                    if not isinstance(repository, str):
                        return events, result.cursor, False
                    if repository in selected:
                        events.append({'kind': declared['event'], 'repository': repository,
                            'subject': row.get(declared.get('subject_field', 'subject')), 'identity': item.identity})
                    continue
                # Unknown shared writes cannot authorize reuse. Bound the
                # reconciliation rather than inventing their domain meaning.
                return events, result.cursor, False
    return events, result.cursor, True


def _streams(session, definition):
    declared = list(definition.get('coherence_streams') or [])
    for observation in session.snapshot.get('service_observations', []):
        transport = (observation.get('provider_observed') or {}).get('event_transport')
        if isinstance(transport, dict):
            declared.append(dict(transport, service_identity=observation['identity']))
    # One socket/stream even when both the definition and probe declare it.
    return list({item['identity']: item for item in declared}.values())


def _semantic_events(session, prior, definition):
    cursors = dict(prior.get('semantic_cursors') or {})
    events, known = [], True
    for declared in _streams(session, definition)[:8]:
        if declared.get('protocol') != 'mncs.workspace-event-cursor/2':
            known = False
            continue
        identity = declared['identity']
        source = sources.LanguageServiceSource(declared['socket'])
        result = source.observe(cursors.get(identity))
        if result.status != 'ok':
            known = False
            events.append({'kind': 'provider.changed', 'provider': declared.get('provider'),
                           'stream': identity, 'reason': result.detail, 'reset': result.status == 'reset'})
        else:
            for item in result.events:
                for observation in session.snapshot.get('service_observations', []):
                    if observation.get('identity') == declared.get('service_identity'):
                        observed = observation.setdefault('provider_observed', {})
                        observed.update(generation=item.generation, stream_identity=item.stream, event_cursor=item.provenance['cursor'])
                events.append({'kind': item.kind, 'identity': item.identity,
                    'provider': declared.get('provider'), 'stream': identity,
                    'subject': item.subject, 'generation': item.generation,
                    'provenance': item.provenance, **item.payload})
        # On reset acknowledge no new epoch until a bounded pass has run.
        cursors[identity] = result.cursor
    return events, cursors, known


def _pass_events(session, events, spec, definition):
    """Return the exact observed events admitted to each owner's route."""
    routed = {name: events for name in spec['passes']}
    subscriptions = definition.get('coherence_subscriptions') or {}
    for name, subscription in subscriptions.items():
        if name not in routed:
            raise ValueError('unknown coherence subscription owner')
        rows, considered = [], []
        for event in events:
            if event['kind'] != 'semantic.changed' or event.get('stream') != subscription.get('stream'):
                continue
            subjects = {item.get('identity') for item in event.get('semantic_subjects', []) if isinstance(item, dict)}
            obligations = event.get('obligations') or {}
            identities = set()
            for key in ('added', 'resolved', 'status_changed'):
                identities.update(item.get('identity') for item in obligations.get(key, []) if isinstance(item, dict))
            complete = bool(event.get('impact_complete')) and bool(obligations.get('complete')) and subscription.get('complete') is True
            rows.append([1, int(complete), int(bool(subjects & set(subscription.get('subjects', [])))),
                         int(bool(identities & set(subscription.get('obligations', []))))])
            considered.append(event)
        if rows:
            decisions = []
            for offset in range(0, len(rows), 16):
                codes, _ = _native(session, 'scoped_events', rows[offset:offset + 16])
                decisions.extend(codes)
            retained = [event for event in events if event not in considered]
            retained.extend(event for event, code in zip(considered, decisions) if code == 1)
            routed[name] = retained
    return routed


def _observe(session, prior: dict) -> tuple[dict, list[dict], bool, dict]:
    repositories, events, known = {}, [], True
    metrics = {"metadata_paths": 0, "enumerated_repositories": [], "errors": []}
    for name, path in _roots(session).items():
        try:
            current, changed, counts = observations.observe_repository(
                session, path, prior.get("repositories", {}).get(name))
            repositories[name] = current
            events.extend({**event, "repository": name} for event in changed)
            metrics["metadata_paths"] += counts["metadata_paths"]
            if counts["enumerated"]:
                metrics["enumerated_repositories"].append(name)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            known = False
            metrics["errors"].append({"repository": name, "reason": str(error)[:200]})
            # A missing/corrupt derived catalogue is recoverable, never trusted.
            current, _, counts = observations.observe_repository(session, path, None)
            repositories[name] = current
            metrics["enumerated_repositories"].append(name)
    artifacts = {}
    paths = {Path(sys.executable)}
    for binding in session.snapshot.get("bindings", []):
        for value in (binding.get("address"), binding.get("toolchain_address")):
            if isinstance(value, str):
                path = value.removeprefix("python:")
                if Path(path).is_absolute():
                    paths.add(Path(path))
        for value in binding.get("fixed_env", {}).values():
            if isinstance(value, str) and Path(value).is_absolute() and Path(value).is_file():
                paths.add(Path(value))
    toolchain = session.snapshot.get("toolchain") or {}
    if toolchain.get("binary"):
        paths.add(Path(toolchain["binary"]))
        paths.add(Path(toolchain["binary"]).parent / "libmncs_embed.so")
    for path in sorted(paths):
        key = str(path)
        artifact = observations.observe_artifact(path, prior.get("artifacts", {}).get(key))
        artifacts[key] = artifact
        if prior.get("artifacts") and artifact != prior["artifacts"].get(key):
            events.append({"kind": "provider.artifact_changed", "artifact": key})
    from .toolchain import language_library_for
    libraries = {}
    binary = toolchain.get("binary")
    library = language_library_for(binary, session) if binary else None
    if library:
        key = str(library.resolve())
        previous = prior.get("libraries", {}).get(key)
        try:
            libraries[key] = observations.observe_library(session, library, previous)
        except (OSError, ValueError, KeyError):
            known = False
            libraries[key] = observations.observe_library(session, library)
        declarations = {}
        for relative in (".mncs/project.json", "stdlib-manifest.json", "dist/stdlib-bundle.json"):
            path = library.parent / relative
            declarations[relative] = observations.observe_artifact(
                path, (previous or {}).get("provider_declarations", {}).get(relative))
        libraries[key]["provider_declarations"] = declarations
        if prior.get("libraries") and (previous is None or previous.get("content_identity") != libraries[key]["content_identity"]
                                      or previous.get("provider_declarations") != declarations):
            events.append({"kind": "provider.artifact_changed", "artifact": key})
    if prior.get("libraries") and set(prior["libraries"]) != set(libraries):
        events.append({"kind": "provider.artifact_changed", "reason": "effective library roots changed"})
    observed = {"repositories": repositories, "artifacts": artifacts, "libraries": libraries}
    return observed, events, known, metrics


def _reuse(block):
    if block is None:
        return None
    block = copy.deepcopy(block)
    block["reused"] = True
    if "revalidation" in block:
        block["revalidation"] = {"changed": [], "reprobed": 0, "removed": []}
        block["operations"] = []
    if isinstance(block.get("summary"), dict):
        block["summary"]["epoch_reused"] = True
        if "elapsed_seconds" in block["summary"]:
            block["summary"]["elapsed_seconds"] = 0.0
    if "elapsed_seconds" in block:
        block["elapsed_seconds"] = 0.0
    return block


def _domain_signature(value):
    # Transport timestamps do not change a provider's observed meaning.
    def material(item):
        if isinstance(item, dict):
            return {key: material(value) for key, value in item.items()
                    if key not in ("observed_at", "elapsed_seconds", "recorded_at", "updated_at")}
        if isinstance(item, list):
            return [material(value) for value in item]
        return item
    return digest_hex(material(value))


def _result_refs(
    session, results, *, verified_previous_references=None,
    publication_activity=None,
):
    """Resolve durable result pointers, reusing already verified Store refs.

    ``verified_previous_references`` must come from ``_load_results`` with a
    complete successful read. The content-derived reference remains safe to
    reuse because immutable Store records cannot change under that identity.
    """
    refs = {}
    verified_previous_references = (
        verified_previous_references
        if isinstance(verified_previous_references, dict) else {}
    )
    for name, block in results.items():
        identity = digest_hex(block, length=64)
        owner = f"incremental-coherence:{name}"
        if hasattr(getattr(session, "store", None), "backend"):
            record = {"block": block}
            previous = verified_previous_references.get(name)
            expected_reference = evidence_store.reference_for_record(
                session, owner, record,
            )
            if (isinstance(previous, dict)
                    and previous.get("identity") == identity
                    and previous.get("reference") == expected_reference):
                refs[name] = {
                    "identity": identity,
                    "reference": expected_reference,
                }
                if isinstance(publication_activity, dict):
                    publication_activity[
                        "result_reference_store_reference_reuse_count"
                    ] = publication_activity.get(
                        "result_reference_store_reference_reuse_count", 0,
                    ) + 1
                continue
            if isinstance(publication_activity, dict):
                publication_activity[
                    "result_reference_store_resolution_attempt_count"
                ] = publication_activity.get(
                    "result_reference_store_resolution_attempt_count", 0,
                ) + 1
            reference = evidence_store.publish(session, owner, record)
            if reference is None:
                from .store_backend import StoreUnavailable

                raise StoreUnavailable(
                    "Store-backed coherence results require immutable evidence publication",
                    code="provider-operation-unsupported",
                )
            refs[name] = {"identity": identity, "reference": reference}
            if isinstance(publication_activity, dict):
                publication_activity[
                    "result_reference_store_resolution_success_count"
                ] = publication_activity.get(
                    "result_reference_store_resolution_success_count", 0,
                ) + 1
            continue
        path = session.state_dir / "sessions" / session.session_id / "coherence-results" / (identity + ".json")
        try:
            current = json.loads(path.read_text())
        except (OSError, ValueError):
            current = None
        if current != {"block": block}:
            write_json(path, {"block": block})
        refs[name] = {"identity": identity, "artifact": str(path)}
    return refs


def _safe_result_reference(session, owner: str, identity: str, block: dict,
                           reference: dict | None) -> tuple[str | None, str]:
    """Expose a bounded source pointer without leaking local absolute paths."""
    if not isinstance(reference, dict):
        return None, "not-recorded"
    if reference.get("identity") != identity:
        return None, "identity-mismatch"
    store_reference = reference.get("reference")
    if isinstance(store_reference, str):
        prefix = f"mncs-store:{session.session_id}:incremental-coherence:{owner}:"
        suffix = store_reference.removeprefix(prefix)
        if (store_reference.startswith(prefix)
                and re.fullmatch(r"[0-9a-f]{64}", suffix)
                and suffix == digest_hex({"block": block}, length=64)):
            return store_reference, "store-record"
        return None, "invalid-reference"
    artifact = reference.get("artifact")
    if isinstance(artifact, str):
        try:
            state_root = Path(session.state_dir).resolve()
            expected = (state_root / "sessions" / session.session_id
                        / "coherence-results").resolve()
            path = Path(artifact).resolve()
            if (path.parent != expected or path.name != f"{identity}.json"
                    or not path.is_file()):
                return None, "invalid-reference"
            return path.relative_to(state_root).as_posix(), "session-artifact"
        except (OSError, ValueError, TypeError):
            return None, "invalid-reference"
    return None, "invalid-reference"


def _safe_verification_evidence_reference(
    session_id: str | None, reference: object,
) -> tuple[str | None, str]:
    """Accept only a verification evidence pointer scoped to this session."""
    if reference is None:
        return None, "not-recorded"
    if (not isinstance(session_id, str)
            or re.fullmatch(r"ses_[A-Za-z0-9_-]{1,128}", session_id) is None
            or not isinstance(reference, str)):
        return None, "invalid-reference"
    store_prefix = f"mncs-store:{session_id}:verification:"
    if reference.startswith(store_prefix):
        if re.fullmatch(r"[0-9a-f]{64}", reference[len(store_prefix):]):
            return reference, "store-record"
        return None, "invalid-reference"
    artifact_pattern = (
        rf"sessions/{re.escape(session_id)}/verification-artifacts/evidence/"
        r"verification-(?:ambient|explicit)-[0-9a-f]{12}\.json"
    )
    if re.fullmatch(artifact_pattern, reference) is not None:
        return reference, "session-artifact"
    return None, "invalid-reference"


def _owner_pass_results(session, scheduled: list[dict], results: dict,
                        result_refs: dict | None, *, persisted: bool) -> list[dict]:
    """Tie each scheduled runner return to its bounded content-addressed result."""
    rows = []
    for item in scheduled[:MAX_TRACE_OWNER_ROWS]:
        if not isinstance(item, dict):
            continue
        owner = item.get("owner") or item.get("pass")
        execution_id = item.get("service_execution_id")
        if (not isinstance(owner, str)
                or re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", owner) is None
                or not isinstance(execution_id, str)
                or re.fullmatch(r"ase_[0-9a-f]{32}", execution_id) is None):
            continue
        row = {
            "owner": owner,
            "service_execution_id": execution_id,
            "verification_status": "not-established",
        }
        if owner not in results:
            row["result_status"] = "result-missing"
            row["source_reference_status"] = (
                "not-recorded" if persisted else "trace-not-persisted"
            )
            rows.append(row)
            continue
        block = results[owner]
        if block is None:
            row["result_status"] = "runner-returned-none"
            row["source_reference_status"] = (
                "not-recorded" if persisted else "trace-not-persisted"
            )
            rows.append(row)
            continue
        if not isinstance(block, dict):
            row["result_status"] = "invalid-result-shape"
            row["source_reference_status"] = "invalid-reference"
            rows.append(row)
            continue
        identity = digest_hex(block, length=64)
        row.update({
            "result_status": "result-recorded",
            "result_identity": identity,
            "result_reused": block.get("reused") is True,
        })
        if owner == "verification":
            verification_reference, verification_reference_status = (
                _safe_verification_evidence_reference(
                    session.session_id, block.get("evidence"),
                )
            )
            row["verification_evidence_reference_status"] = (
                verification_reference_status
            )
            if verification_reference is not None:
                row["verification_evidence_reference"] = (
                    verification_reference
                )
        operation_status = block.get("operation_status")
        if (isinstance(operation_status, str)
                and re.fullmatch(r"[a-z][a-z0-9-]{0,63}", operation_status)):
            row["operation_status"] = operation_status
        result_code = block.get("status")
        if (isinstance(result_code, str)
                and re.fullmatch(r"[a-z][a-z0-9.-]{0,79}", result_code)):
            row["result_code"] = result_code
        raw_summary = block.get("summary")
        if isinstance(raw_summary, dict):
            summary = {}
            for key in sorted(raw_summary):
                if (not isinstance(key, str)
                        or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key) is None):
                    continue
                value = raw_summary[key]
                if (type(value) is int and value >= 0 or type(value) is bool
                        or type(value) is float and 0 <= value <= 86400):
                    summary[key] = value
                if len(summary) >= MAX_TRACE_RESULT_FIELDS:
                    break
            if summary:
                row["summary"] = summary
            availability_code = OWNER_PASS_SERVICE_AVAILABILITY_CODES.get(
                (owner, raw_summary.get("detail")),
            )
            if availability_code is not None:
                row["service_availability_status"] = "unavailable"
                row["service_availability_reason_code"] = availability_code
        outcome = _validated_owner_pass_outcome(block)
        if outcome is not None:
            row.update({
                "outcome_disposition": outcome["disposition"],
                "outcome_reason_code": outcome["reason_code"],
                "agent_response_emitted": outcome["agent_response_emitted"],
            })
        source_reference, source_status = _safe_result_reference(
            session, owner, identity, block,
            (result_refs or {}).get(owner)
            if isinstance(result_refs, dict) else None,
        )
        if not persisted and source_status == "not-recorded":
            source_status = "trace-not-persisted"
        row["source_reference_status"] = source_status
        if source_reference is not None:
            row["source_reference"] = source_reference
        rows.append(row)
    return rows


def _persist_result_publication_failure(
    session, previous: dict, report: dict, scheduled: list[dict],
    results: dict, *, status: str,
) -> None:
    """Retain completed owner observations when immutable result linking fails.

    Replay cursors are deliberately left at their prior values. The next tick
    therefore reconciles the work again instead of treating an unlinked result
    as a durable acknowledgement.
    """
    partial_trace = dict(report)
    partial_trace["trace_persistence_status"] = "session-snapshot"
    partial_trace["result_reference_publication_status"] = status
    partial_trace["owner_pass_results"] = _owner_pass_results(
        session, scheduled, results, None, persisted=True,
    )

    partial_state = dict(previous)
    partial_state["schema_version"] = SCHEMA
    partial_state["last_trace"] = partial_trace
    partial_state["stable"] = False
    prior_history = project_owner_execution_history(
        previous.get("owner_execution_history"),
        session_id=session.session_id,
    )
    partial_state["owner_execution_history"] = _append_owner_execution_history(
        previous.get("owner_execution_history"), partial_trace,
        trace_sequence=min(10**12, prior_history["trace_sequence"] + 1),
        session_id=session.session_id,
    )

    prior_coherence = session.snapshot.get("coherence")
    prior_sequence = session.snapshot.get("snapshot_sequence")
    prior_updated_at = session.snapshot.get("updated_at")
    session.snapshot["coherence"] = partial_state
    try:
        session._save()
    except Exception:
        # Preserve the publication error that caused this recovery attempt.
        # The session snapshot may already have committed despite a lost reply;
        # a later canonical read decides that using Store revision state.
        if isinstance(prior_coherence, dict):
            session.snapshot["coherence"] = prior_coherence
        else:
            session.snapshot.pop("coherence", None)
        if prior_sequence is None:
            session.snapshot.pop("snapshot_sequence", None)
        else:
            session.snapshot["snapshot_sequence"] = prior_sequence
        if prior_updated_at is None:
            session.snapshot.pop("updated_at", None)
        else:
            session.snapshot["updated_at"] = prior_updated_at


def _load_results(session, refs):
    results, known = {}, True
    expected = session.state_dir / "sessions" / session.session_id / "coherence-results"
    for name, ref in refs.items():
        try:
            reference = ref.get("reference")
            if evidence_store.is_store_reference(reference):
                owner = f"incremental-coherence:{name}"
                block = evidence_store.read(session, owner, reference)["block"]
            else:
                path = Path(ref["artifact"])
                if not path.resolve().is_relative_to(expected.resolve()):
                    raise ValueError("coherence result escapes session artifacts")
                block = json.loads(path.read_text())["block"]
            if digest_hex(block, length=64) != ref["identity"]:
                raise ValueError("corrupt coherence result")
            results[name] = block
        except (OSError, ValueError, KeyError, TypeError):
            known = False
    return results, known


def _owner_result_failed(value) -> bool:
    """Whether an owner response says its requested work did not complete.

    Domain verdicts such as a failed test remain completed evidence. Only the
    provider/invocation envelope blocks acknowledgement of the input event.
    Legacy runners may use ``None`` for a successful quiet no-work case.
    Environment adapters now return a bounded reason-coded quiet outcome,
    which is also a completed response rather than an invocation failure.
    """
    if value is None:
        return False
    if not isinstance(value, dict):
        return True
    if value.get("ok") is False or isinstance(value.get("error"), str):
        return True
    # Owner adapters distinguish a recorded domain outcome (including FAIL,
    # deferred, or unsafe-to-repair) from work that could not be completed.
    # Only the former may acknowledge a semantic input cursor.
    operation_status = value.get("operation_status")
    if operation_status is not None and operation_status != "complete":
        return True
    status = str(value.get("status", "")).lower()
    if status in {"error", "unavailable", "not_run"}:
        return True
    invocation = value.get("invocation")
    return isinstance(invocation, dict) and str(invocation.get("status", "")).lower() in {
        "error", "unavailable", "not_run"
    }


def tick(session, definition: dict, runners: dict, *, fresh: bool = False,
         now_ms: int | None = None) -> tuple[dict, dict]:
    scheduler_started = time.monotonic()
    # Without the native scheduler provider, retain explicit owner recovery.
    # No host policy mirror and no durable "current" receipt is fabricated.
    automation = os.environ.get("MNCS_AUTOMATION_ROOT") or _roots(session).get("mncs-automation")
    if not automation or not (Path(automation) / "native/mncs/automation/coherence.mncs").is_file():
        results = {}
        scheduled = []
        for name, run in runners.items():
            resource_before = resources.process_usage_sample(os.getpid())
            owner_started = time.monotonic()
            service_execution_id = _service_execution_id(session, name)
            with ambient_owner_pass_context(
                session.session_id, name, service_execution_id,
            ):
                results[name] = run()
            owner_elapsed_seconds = round(time.monotonic() - owner_started, 3)
            resource_after = resources.process_usage_sample(os.getpid())
            scheduled.append({
                "pass": name,
                "owner": name,
                "service_execution_id": service_execution_id,
                "run_elapsed_seconds": owner_elapsed_seconds,
                "process_resource_usage": resources.process_usage_delta(
                    resource_before, resource_after,
                    elapsed_seconds=owner_elapsed_seconds,
                ),
            })
        owner_failures = sorted(
            name for name in results if _owner_result_failed(results[name])
        )
        trace = {
            "mode": "recovery_scan",
            "trace_persistence_status": "entry-response-only",
            "reason": "native coherence provider unavailable",
            "scheduled": scheduled,
            "skipped": [],
            "owner_failures": owner_failures,
            "elapsed_seconds": round(time.monotonic() - scheduler_started, 3),
        }
        trace["owner_pass_results"] = _owner_pass_results(
            session, scheduled, results, None, persisted=False,
        )
        return {
            name: owner_pass_response(value)
            for name, value in results.items()
        }, trace
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    spec = declarations()
    previous = session.snapshot.get("coherence") or {}
    if previous.get("schema_version") != SCHEMA:
        previous = {}
    acknowledged_store_cursor = previous.get("store_cursor")
    observed, events, known, metrics = _observe(session, previous)
    store_events, cursor, store_known = _store_events(session, previous, definition)
    store_reset = next((item for item in store_events
                        if item.get("kind") == "store.reconcile_required"), None)
    events.extend(item for item in store_events
                  if item.get("kind") != "store.reconcile_required")
    semantic_events, semantic_cursors, semantic_known = _semantic_events(session, previous, definition)
    prior_semantic_cursors = dict(previous.get("semantic_cursors") or {})
    reset_candidate_streams = {
        str(event.get("stream")) for event in semantic_events
        if event.get("kind") == "provider.changed" and event.get("reset")
        and event.get("stream")
        and semantic_cursors.get(str(event.get("stream"))) is not None
        and semantic_cursors.get(str(event.get("stream")))
            != prior_semantic_cursors.get(str(event.get("stream")))
    }
    events.extend(semantic_events)
    known = known and semantic_known and store_known and store_reset is None and (not previous or previous.get("stable") is True)
    policy, policy_artifacts = _policy_identity(session, previous.get("policy_artifacts", {}))
    if previous and previous.get("policy_identity") != policy:
        known = False
    deadlines = dict(previous.get("deadlines") or {})
    expired = {name for name, value in deadlines.items() if now_ms >= value}
    report = {"mode": "bootstrap_scan" if not previous else "targeted_update",
              "events": events[:32], "event_count": len(events), "scheduled": [], "skipped": [], **metrics}
    if store_reset is not None:
        report["store_replay"] = {
            "disposition": "full_owner_reconciliation_required",
            "candidate_cursor": store_reset.get("candidate_cursor"),
            "reason": store_reset.get("reason", "bounded replay reset"),
        }
    results, results_known = _load_results(session, previous.get("result_refs") or {})
    known = known and results_known
    names = list(spec["passes"])
    if set(names) != set(runners):
        raise ValueError("coherence runners must match the declared owner passes")
    rows = []
    routed_events = {name: [] for name in names}
    masks = {name: 0 for name in names}
    try:
        events = _file_events(session, events)
        routed_events = _pass_events(session, events, spec, definition)
        masks = {
            name: _event_mask(owner_events, spec)
            for name, owner_events in routed_events.items()
        }
        rows = [[int(name in results), int(known),
                 sum(1 << index for index in spec["passes"][name]), masks[name], 1, 0, 0,
                 int(name in expired or (name == "doctor" and _runtime_due(session, definition)))]
                for name in names]
        request = digest_hex({"policy": policy, "facts": rows})
        if request == previous.get("current_request") and known:
            codes, receipt = previous["current_codes"], previous.get("policy_receipt", {})
        else:
            codes, receipt = _native(session, "route_batch", rows)
        if store_reset is not None:
            # A Store cursor overflow has no safe selective interpretation.
            # Re-run all declared owners against their authoritative current
            # state before adopting the candidate high-water.
            codes = [2] * len(names)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        codes, receipt = [2] * len(names), {}
        report["mode"] = "bounded_reconciliation"
        known = False
    if any(code == 2 for code in codes):
        report["mode"] = "bounded_reconciliation"
    scheduled = set()
    derived = {"doctor": ("service_observations", "provider.changed"),
               "semantics": ("semantics", "semantic.changed"),
               "actions": ("actions_state", "external-evidence.changed"),
               "verification": ("verification_state", "verification.changed"),
               "diagnostics": ("diagnostic_state", "diagnostic.changed")}
    pending = dict(zip(names, codes))
    for wave in range(MAX_WAVES):
        emitted = []
        for name in names:
            code = pending.get(name, 0)
            if not code or name in scheduled:
                continue
            key, kind = derived.get(name, (None, None))
            before = _domain_signature(session.snapshot.get(key)) if key else None
            resource_before = resources.process_usage_sample(os.getpid())
            owner_started = time.monotonic()
            service_execution_id = _service_execution_id(session, name)
            with ambient_owner_pass_context(
                session.session_id, name, service_execution_id,
            ):
                results[name] = runners[name]()
            owner_elapsed_seconds = round(time.monotonic() - owner_started, 3)
            resource_after = resources.process_usage_sample(os.getpid())
            process_resource_usage = resources.process_usage_delta(
                resource_before, resource_after,
                elapsed_seconds=owner_elapsed_seconds,
            )
            scheduled.add(name)
            deadlines.pop(name, None)
            if name == "doctor":
                leases = [service.get("observation_max_age_ms") for service in definition.get("services", [])]
                if any(value is not None for value in leases):
                    deadlines[name] = now_ms + min(value for value in leases if value is not None)
            if name == "actions":
                summary = (results[name] or {}).get("summary", {})
                if summary.get("pending") or summary.get("blockers") or summary.get("eligible"):
                    # Poll only this external boundary when no receipt feed exists.
                    deadlines[name] = now_ms + 60_000
            if name == "family":
                from .family import scheduler_deadlines
                values = scheduler_deadlines(session)
                if values:
                    deadlines[name] = min(int(datetime.fromisoformat(value).timestamp() * 1000) for value in values)
            interest_mask = sum(
                1 << index for index in spec["passes"][name]
            )
            owner_events = routed_events.get(name, [])
            trigger_events = [
                event for event in owner_events
                if _event_mask([event], spec) & interest_mask
            ]
            trigger_kinds = list(dict.fromkeys(
                event["kind"] for event in trigger_events
            ))[:MAX_TRACE_TRIGGER_KINDS]
            trigger_subjects = sorted({
                subject.get("identity")
                for event in trigger_events
                for subject in (
                    event.get("semantic_subjects")
                    if isinstance(event.get("semantic_subjects"), list)
                    else []
                )
                if isinstance(subject, dict)
                and isinstance(subject.get("identity"), str)
            })
            report["scheduled"].append({"pass": name, "owner": name, "disposition": code, "wave": wave,
                "service_execution_id": service_execution_id,
                "owner_reused": bool((results[name] or {}).get("reused", False)),
                "run_elapsed_seconds": owner_elapsed_seconds,
                "process_resource_usage": process_resource_usage,
                "invalidated_input_mask": rows[names.index(name)][3] if rows else None,
                "timer_or_lifecycle_due": bool(rows[names.index(name)][7]) if rows else None,
                "inputs": trigger_kinds,
                "subjects": trigger_subjects[:64],
                "subject_count": len(trigger_subjects)})
            if key and before != _domain_signature(session.snapshot.get(key)):
                emitted.append({"kind": kind, "producer": name})
        if not emitted or len(scheduled) == len(names):
            break
        routed_events = _pass_events(session, emitted, spec, definition)
        masks = {
            name: _event_mask(owner_events, spec)
            for name, owner_events in routed_events.items()
        }
        rows = [[int(name in results), 1, sum(1 << i for i in spec["passes"][name]),
                 masks[name], int(name not in scheduled), 0, 0, 0] for name in names]
        next_codes, _ = _native(session, "route_batch", rows)
        pending = dict(zip(names, next_codes))
        report.setdefault("derived_events", []).extend(emitted)
    report["skipped"] = [{"pass": name, "reason": "current inputs"} for name in names if name not in scheduled]
    owner_failures = sorted(
        name for name in scheduled if _owner_result_failed(results.get(name))
    )
    # Persist explicit empty coverage too, so projections can distinguish a
    # completed pass with no classified owner failure from an older trace
    # that never recorded this outcome field.
    report["owner_failures"] = owner_failures
    if store_reset is not None and (scheduled != set(names) or owner_failures):
        after_known = False
        report["store_replay"]["disposition"] = "reconciliation_incomplete_or_store_advanced"
        if owner_failures:
            report["store_replay"]["owner_failures"] = owner_failures
    elif not store_known:
        after_known = False
    if semantic_events and owner_failures:
        # The LS cursor is a single stream acknowledgement shared by all
        # subscribed owners. Keep the prior durable cursor until every owner
        # selected for this semantic batch has completed its invocation. A
        # later tick replays the same source window and retries the failed
        # owner; successful owners are expected to be idempotent.
        semantic_cursors = dict(previous.get("semantic_cursors") or {})
        semantic_known = False
        report["owner_failures"] = owner_failures
        report["mode"] = "bounded_reconciliation"
        report["cursor_disposition"] = "held_for_owner_retry"
    elif reset_candidate_streams and scheduled == set(names):
        # The provider reported that its bounded event window no longer
        # contains the prior cursor. Only a complete successful owner pass
        # can adopt the exact high-water candidate returned with that reset.
        semantic_known = True
        report["cursor_disposition"] = "reconciled_and_advanced"
    # Observation and effects are two phases. A moving checkout cannot be
    # certified current using an after-the-fact catalogue of unseen edits.
    _after, intervening, after_known, _ = _observe(session, observed)
    # A replay, rather than sampling the latest generation, can acknowledge
    # our own effects without discarding a publication racing those effects.
    if callable(getattr(session.store, "generation", None)) and report["scheduled"]:
        raced, checked_cursor, checked_known = _store_events(session, {"store_cursor": cursor}, definition)
        raced_reset = next((item for item in raced
                            if item.get("kind") == "store.reconcile_required"), None)
        if (store_reset is not None and raced_reset is not None
                and checked_known
                and raced_reset.get("candidate_cursor") == store_reset.get("candidate_cursor")
                and scheduled == set(names) and not owner_failures):
            cursor = store_reset.get("candidate_cursor")
            report["store_replay"]["disposition"] = "full_owner_reconciliation_complete"
            report["cursor_disposition"] = "store_reconciled_and_advanced"
        elif checked_known and not raced:
            cursor = checked_cursor
        else:
            after_known = False
            intervening.extend(item for item in raced
                               if item.get("kind") != "store.reconcile_required")
            if store_reset is not None:
                report["store_replay"]["disposition"] = "reconciliation_incomplete_or_store_advanced"
    if store_events and owner_failures and store_reset is None:
        # A Store event is acknowledged only after every selected owner has
        # completed. The post-pass replay above still detects concurrent
        # publications, but the durable cursor returns to its last committed
        # value so this input is delivered again on the next tick.
        cursor = acknowledged_store_cursor
        after_known = False
        report["store_cursor_disposition"] = "held_for_owner_retry"
    stable = after_known and not intervening and not owner_failures
    if not stable:
        report["mode"] = "bounded_reconciliation"
        report["pending_events"] = intervening[:32]
    effects = any(not (results.get(name) or {}).get('reused', False) for name in scheduled)
    report["elapsed_seconds"] = round(time.monotonic() - scheduler_started, 3)
    persist_trace = (
        effects or events or store_reset is not None or not store_known or owner_failures
        or previous.get("repositories") != observed["repositories"]
        or semantic_cursors != previous.get("semantic_cursors", {})
        or previous.get('policy_identity') != policy
    )
    report["trace_persistence_status"] = (
        "session-snapshot" if persist_trace else "entry-response-only"
    )
    if persist_trace:
        publication_started = time.monotonic()
        publication_resource_before = resources.process_usage_sample(os.getpid())
        publication_activity = {
            "result_reference_store_reference_reuse_count": 0,
            "result_reference_store_resolution_attempt_count": 0,
            "result_reference_store_resolution_success_count": 0,
        }
        try:
            result_refs = _result_refs(
                session, results,
                verified_previous_references=(
                    previous.get("result_refs") if results_known else None
                ),
                publication_activity=publication_activity,
            )
        except (KeyboardInterrupt, Exception) as publication_error:
            report.update(publication_activity)
            publication_elapsed = round(
                time.monotonic() - publication_started, 3,
            )
            publication_resource_after = resources.process_usage_sample(
                os.getpid(),
            )
            report["result_reference_publication_status"] = (
                "interrupted" if isinstance(publication_error, KeyboardInterrupt)
                else "failed"
            )
            report["result_reference_publication_elapsed_seconds"] = (
                publication_elapsed
            )
            report["result_reference_publication_process_resource_usage"] = (
                resources.process_usage_delta(
                    publication_resource_before, publication_resource_after,
                    elapsed_seconds=publication_elapsed,
                    measurement_basis=(
                        "process-counters-bracketing-result-reference-publication"
                    ),
                )
            )
            _persist_result_publication_failure(
                session, previous, report, report["scheduled"], results,
                status=report["result_reference_publication_status"],
            )
            raise
        report.update(publication_activity)
        publication_elapsed = round(time.monotonic() - publication_started, 3)
        publication_resource_after = resources.process_usage_sample(os.getpid())
        report["result_reference_publication_status"] = "complete"
        report["result_reference_publication_elapsed_seconds"] = (
            publication_elapsed
        )
        report["result_reference_publication_process_resource_usage"] = (
            resources.process_usage_delta(
                publication_resource_before, publication_resource_after,
                elapsed_seconds=publication_elapsed,
                measurement_basis=(
                    "process-counters-bracketing-result-reference-publication"
                ),
            )
        )
    else:
        result_refs = None
    report["owner_pass_results"] = _owner_pass_results(
        session, report["scheduled"], results, result_refs,
        persisted=persist_trace,
    )
    if persist_trace:
        state = {"schema_version": SCHEMA, **observed, "result_refs": result_refs,
                 "policy_identity": policy, "policy_artifacts": policy_artifacts,
                 "policy_receipt": receipt, "last_trace": report, "deadlines": deadlines,
                 "stable": stable and semantic_known, "store_cursor": cursor, "semantic_cursors": semantic_cursors,
                 "file_cursor": cursor}
        prior_owner_history = project_owner_execution_history(
            previous.get("owner_execution_history"),
            session_id=session.session_id,
        )
        history_sequence = min(
            10**12, prior_owner_history["trace_sequence"] + 1,
        )
        state["owner_execution_history"] = _append_owner_execution_history(
            previous.get("owner_execution_history"), report,
            trace_sequence=history_sequence,
            session_id=session.session_id,
        )
        # Keep the replay cursor captured BEFORE effects. Own writes are
        # filtered by the feed. A concurrent peer publication must be replayed
        # on the next tick, including publications during a long owner pass.
        current_rows = [[1, int(state["stable"]), sum(1 << index for index in spec["passes"][name]),
                         0, 1, 0, 0, 0] for name in names]
        try:
            current_codes, current_receipt = _native(session, "route_batch", current_rows)
            state.update(current_codes=current_codes,
                         current_request=digest_hex({"policy": policy, "facts": current_rows}),
                         policy_receipt=current_receipt)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            state["stable"] = False
        session.snapshot["coherence"] = state
        session._save()
    report["events"] = events[:32]
    report["elapsed_seconds"] = round(time.monotonic() - scheduler_started, 3)
    blocks = {name: owner_pass_response(
        results[name] if any(item["pass"] == name for item in report["scheduled"])
        else _reuse(results.get(name))
    ) for name in names}
    return blocks, report
