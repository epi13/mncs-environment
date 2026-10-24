"""On-demand environmental pressure projection over durable campaign evidence.

The campaign ledger remains the evidence authority. This module resolves only
bounded, content-addressed observation records from the selected workspace,
then projects their relationship to the current session and work intent. It
does not infer missing measurements or claim that a proposed improvement
worked.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path, PurePosixPath
from typing import Any

from . import workspace as workspace_module
from .identity import digest_hex
from .sessions import LifecycleError
from .store_backend import StoreIntegrityFailure, StoreUnavailable

SCHEMA = "mncs.environment.capability-intelligence/1"
OBSERVATION_SCHEMA = "mncs.environment.environmental-observation/1"
OBSERVATION_PATH_PREFIX = "evidence/environmental-capability/"
MAX_EVIDENCE_REFS = 32
MAX_EVIDENCE_HISTORY_PAGES = 4
MAX_OBSERVATIONS = 16
MAX_OBSERVATION_BYTES = 64 * 1024
MAX_SOURCE_BYTES = 512 * 1024
MAX_OBSERVATION_READ_SECONDS = 8.0
MAX_SOURCE_POINTER_VERIFICATIONS = 32
MAX_RESULT_BYTES = 48 * 1024
MAX_SESSION_EVENT_WINDOW = 256
MAX_SESSION_ACTIVITY_INTERACTIONS = 32
MAX_SESSION_ACTIVITY_CAPABILITIES = 16
SOURCE_INTEGRITY_VERIFIED = frozenset({
    "content-integrity-verified",
    "record-integrity-verified-in-projection",
})
_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _workspace_root(session: Any) -> Path | None:
    raw = (session.snapshot.get("workspace") or {}).get("root")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return Path(raw).resolve(strict=True)
    except OSError:
        return None


def _evidence_history_pages(
    session: Any, initial_state: dict[str, Any], requested_pages: int,
) -> tuple[list[dict[str, Any]], bool, bool]:
    """Read a bounded backward page window from one immutable campaign state."""
    pages = [initial_state]
    if requested_pages == 1:
        return pages, False, True
    page_limit = initial_state.get("evidence_limit", MAX_EVIDENCE_REFS)
    if type(page_limit) is not int or page_limit < 1:
        page_limit = MAX_EVIDENCE_REFS
    if page_limit > MAX_EVIDENCE_REFS:
        return pages, True, True

    checkpoint_id = initial_state.get("checkpoint_id")
    evidence_count = initial_state.get("evidence_count")
    consistent = True
    current = initial_state
    while len(pages) < requested_pages:
        previous_offset = current.get("previous_evidence_offset")
        if previous_offset is None:
            break
        current_offset = current.get("evidence_offset")
        if (type(previous_offset) is not int or previous_offset < 0
                or type(current_offset) is not int or current_offset <= previous_offset):
            consistent = False
            break
        older_limit = current_offset - previous_offset
        if older_limit > page_limit:
            consistent = False
            break
        try:
            older = session.campaign_state(
                evidence_offset=previous_offset,
                evidence_limit=older_limit,
            )
        except (LifecycleError, StoreIntegrityFailure, StoreUnavailable):
            consistent = False
            break
        if (not isinstance(older, dict)
                or older.get("checkpoint_id") != checkpoint_id
                or older.get("evidence_count") != evidence_count
                or older.get("evidence_offset") != previous_offset
                or older.get("evidence_limit") != older_limit
                or older.get("next_evidence_offset") != current_offset):
            consistent = False
            break
        pages.append(older)
        current = older

    truncated = (
        not consistent
        or initial_state.get("next_evidence_offset") is not None
        or pages[-1].get("previous_evidence_offset") is not None
    )
    return pages, truncated, consistent


def _allocate_page_observation_budgets(
    capacities: list[int], total_budget: int,
) -> list[int]:
    """Share the observation ceiling without wasting slots on sparse pages."""
    budgets = [0 for _ in capacities]
    target = min(total_budget, sum(capacities))
    while sum(budgets) < target:
        candidates = [index for index, capacity in enumerate(capacities)
                      if budgets[index] < capacity]
        if not candidates:
            break
        selected = min(
            candidates,
            key=lambda index: (budgets[index], -capacities[index], index),
        )
        budgets[selected] += 1
    return budgets


def _checkout(session: Any, repository: str, root: Path) -> Path | None:
    selected = session.snapshot.get("selected_checkouts") or {}
    entry = selected.get(repository) if isinstance(selected, dict) else None
    if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
        return None
    raw = Path(entry["path"])
    if not raw.is_absolute():
        raw = root / raw
    try:
        path = raw.resolve(strict=True)
        path.relative_to(root)
    except (OSError, ValueError):
        return None
    if not path.is_dir() or path.is_symlink():
        return None
    return path


def _validate_reference(reference: Any) -> dict[str, str] | None:
    if not isinstance(reference, dict):
        return None
    identity = reference.get("identity")
    repository = reference.get("repository")
    commit = reference.get("commit")
    path = reference.get("path")
    sha256 = reference.get("sha256")
    if (not isinstance(identity, str) or not identity
            or len(identity) > 160
            or not isinstance(repository, str) or not repository or len(repository) > 128
            or not isinstance(commit, str) or not _COMMIT.fullmatch(commit)
            or not isinstance(path, str)
            or (sha256 is not None
                and (not isinstance(sha256, str) or not _SHA256.fullmatch(sha256)))):
        return None
    relative = PurePosixPath(path)
    try:
        path_size = len(path.encode("utf-8"))
    except UnicodeEncodeError:
        return None
    if (relative.is_absolute() or "\\" in path
            or path_size > 1024
            or any(part in ("", ".", "..") for part in path.split("/"))):
        return None
    result = {
        "identity": identity,
        "repository": repository,
        "commit": commit,
        "path": path,
    }
    if isinstance(sha256, str):
        result["sha256"] = sha256
    return result


def _validate_source_pointer(source: Any) -> dict[str, str] | None:
    """Validate a source pointer; observation sources need not define ledger identity."""
    if not isinstance(source, dict):
        return None
    candidate = source
    if "identity" not in source:
        candidate = {**source, "identity": "source-pointer"}
    pointer = _validate_reference(candidate)
    if pointer is not None and "identity" not in source:
        pointer.pop("identity", None)
    return pointer


def _short_text(value: Any, limit: int = 192) -> str | None:
    if not isinstance(value, str):
        return None
    if len(value) <= limit:
        return value
    return value[:limit - 1] + "…"


def _compact_projection(
    projection: dict[str, Any], original_size: int, max_result_bytes: int,
) -> dict[str, Any]:
    """Return a small inspectable summary when the full projection is large.

    Exact source pointers remain available, while raw operation payloads are
    represented by a digest so one large observation cannot defeat the
    projection's response bound.
    """
    subject = projection["subject"]
    intent = subject.get("work_intent") or {}
    source = projection["source"]
    compact_subject = {
        key: _short_text(subject.get(key), 160)
        for key in ("session_id", "campaign_id", "environment_id", "definition_id")
        if subject.get(key) is not None
    }
    compact_subject["work_intent"] = {
        key: _short_text(intent.get(key), 256 if key == "goal" else 160)
        for key in ("identity", "goal")
        if intent.get(key) is not None
    }
    compact_source = {
        key: source.get(key)
        for key in ("evidence_count", "evidence_page_truncated",
                    "evidence_page_offset", "evidence_page_limit",
                    "previous_evidence_offset", "next_evidence_offset",
                    "evidence_history_pages_requested",
                    "evidence_history_pages_read", "evidence_history_page_offsets",
                    "evidence_history_page_limits",
                    "evidence_history_truncated",
                    "evidence_history_checkpoint_consistent",
                    "evidence_history_observation_budgets_by_page",
                    "observation_reference_selection_policy",
                    "observation_records_skipped_by_page_budget",
                    "source_pointer_count",
                    "source_pointer_content_integrity_verified_count",
                    "source_pointer_content_integrity_not_verified_count",
                    "observation_records_considered",
                    "observation_records_verified",
                    "observation_records_unavailable")
        if key in source and (
            key in {"previous_evidence_offset", "next_evidence_offset"}
            or source.get(key) is not None
        )
    }
    for key in ("campaign_state_checkpoint_id", "recorded_at"):
        if source.get(key) is not None:
            compact_source[key] = _short_text(source[key], 160)
    compact_source["response_truncated"] = True
    compact_source["response_byte_count"] = original_size

    compact_hypotheses = []
    observations = projection["observations"]
    for hypothesis in projection["pressure_hypotheses"][:8]:
        summary = {
            key: _short_text(hypothesis.get(key), 256 if key in ("title", "recommendation") else 160)
            for key in ("identity", "title", "category", "subsystem", "owner",
                        "interpretation", "recurrence_status", "recommendation")
            if hypothesis.get(key) is not None
        }
        for key in (
            "observation_count", "distinct_session_count", "distinct_campaign_count",
            "distinct_work_intent_count", "distinct_environment_version_count",
            "affected_work_context_count", "distinct_recording_session_count",
            "distinct_recording_campaign_count", "recording_context_count",
            "distinct_recording_environment_id_count",
            "distinct_recording_definition_id_count",
            "distinct_recording_environment_version_count",
        ):
            if type(hypothesis.get(key)) is int:
                summary[key] = hypothesis[key]
        for context_key in ("affected_work_contexts", "recording_contexts"):
            raw_contexts = hypothesis.get(context_key)
            raw_contexts = raw_contexts if isinstance(raw_contexts, list) else []
            compact_contexts = []
            for context in raw_contexts[:4]:
                if not isinstance(context, dict):
                    continue
                compact_context = {}
                for key, value in context.items():
                    if isinstance(value, str):
                        limit = 256 if key in (
                            "work_intent_goal", "campaign_objective",
                        ) else 160
                        compact_context[key] = _short_text(value, limit)
                    elif isinstance(value, bool):
                        compact_context[key] = value
                compact_contexts.append(compact_context)
            summary[context_key] = compact_contexts
            summary[f"{context_key}_truncated"] = (
                len(raw_contexts) > len(compact_contexts)
            )
        raw_evidence = hypothesis.get("evidence")
        raw_evidence = raw_evidence if isinstance(raw_evidence, list) else []
        evidence_references = [
            pointer for item in raw_evidence
            if (pointer := _validate_reference(item)) is not None
        ]
        summary["evidence_reference_count"] = len(evidence_references)
        summary["evidence_references"] = evidence_references[:MAX_OBSERVATIONS]
        summary["evidence_references_truncated"] = (
            len(evidence_references) > len(summary["evidence_references"])
        )
        summary["evidence_observation_identities"] = [
            item["identity"] for item in observations
            if isinstance(item.get("pressure_hypothesis"), dict)
            and item["pressure_hypothesis"].get("identity") == hypothesis.get("identity")
        ][:8]
        compact_hypotheses.append(summary)

    compact_observations = []
    for item in observations[:8]:
        observation = item.get("observation") or {}
        observation_bytes = json.dumps(
            observation, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        work = item.get("related_work") or {}
        compact_work = {
            key: _short_text(
                work.get(key), 256 if key in ("work_intent_goal", "campaign_objective") else 160,
            )
            for key in ("campaign_id", "session_id", "work_intent_id", "work_intent_goal",
                        "campaign_objective", "environment_id", "definition_id",
                        "environment_version", "lifecycle")
            if work.get(key) is not None
        }
        checkpoint = work.get("latest_checkpoint")
        if isinstance(checkpoint, dict):
            compact_work["latest_checkpoint"] = {
                "identity": _short_text(checkpoint.get("identity"), 160),
                "sequence": checkpoint.get("sequence") if type(checkpoint.get("sequence")) is int else None,
            }
        if type(work.get("campaign_evidence_count")) is int:
            compact_work["campaign_evidence_count"] = work["campaign_evidence_count"]
        if isinstance(work.get("claim_ids"), list):
            compact_work["claim_ids"] = [
                _short_text(value, 160) for value in work["claim_ids"][:4]
                if isinstance(value, str)
            ]
        recorder = item.get("recording_context")
        compact_recorder = {
            key: _short_text(recorder.get(key), 160)
            for key in (
                "campaign_id", "session_id", "consumer_id", "environment_id",
                "definition_id", "environment_version",
                "environment_identity_capture_status",
                "environment_version_capture_status",
            )
            if isinstance(recorder, dict) and recorder.get(key) is not None
        }
        hypothesis = item.get("pressure_hypothesis")
        compact_hypothesis = None
        if isinstance(hypothesis, dict):
            compact_hypothesis = {
                key: _short_text(hypothesis.get(key), 256 if key == "title" else 160)
                for key in ("identity", "title", "category", "subsystem", "owner")
                if hypothesis.get(key) is not None
            }
        improvement_identity = item.get("improvement_relation_identity")
        measurement_limits = item.get("measurement_limits")
        compact_observation = {
            "identity": item["identity"],
            "captured_at": _short_text(item.get("captured_at"), 80),
            "measurement_basis": _short_text(item.get("measurement_basis"), 128),
            "observation": {
                "kind": _short_text(observation.get("kind"), 128),
                "source_interface": _short_text(observation.get("source_interface"), 128),
                "details_omitted": True,
                "details_sha256": hashlib.sha256(observation_bytes).hexdigest(),
            },
            "related_work": compact_work,
            "recording_context": compact_recorder or None,
            "pressure_hypothesis": compact_hypothesis,
            "measurement_limits": [
                _short_text(value, 192) for value in measurement_limits[:8]
                if isinstance(value, str)
            ] if isinstance(measurement_limits, list) else [],
            "evidence_reference": item["evidence_reference"],
        }
        compact_sources = []
        raw_sources = item.get("supporting_sources")
        raw_sources = raw_sources if isinstance(raw_sources, list) else []
        for source in raw_sources[:4]:
            if not isinstance(source, dict):
                continue
            compact_supporting_source = {
                key: _short_text(source.get(key), 128)
                for key in ("kind", "campaign_evidence_identity", "campaign_id",
                            "session_id", "status", "content_integrity_status")
                if isinstance(source.get(key), str)
            }
            pointer = _validate_source_pointer(source)
            if pointer is not None:
                compact_supporting_source.update(pointer)
            compact_sources.append(compact_supporting_source)
        if compact_sources:
            compact_observation["supporting_sources"] = compact_sources
        if len(raw_sources) > len(compact_sources):
            compact_observation["supporting_sources_truncated"] = (
                len(raw_sources) - len(compact_sources)
            )
        if isinstance(improvement_identity, str):
            compact_observation["improvement_relation_identity"] = _short_text(
                improvement_identity, 160,
            )
        compact_observations.append(compact_observation)

    compact_improvements = []
    for relation in projection["improvement_relations"][:8]:
        summary = {
            key: _short_text(relation.get(key), 256 if key == "basis" else 160)
            for key in (
                "identity", "recorded_by_observation_identity",
                "addresses_hypothesis_identity", "pressure_hypothesis_status",
                "verification_observation_identity",
                "verification_record_status", "subsequent_observation_identity",
                "subsequent_record_status", "effectiveness_assessment", "basis",
            )
            if relation.get(key) is not None
        }
        implementation = relation.get("implementation_reference")
        if isinstance(implementation, dict):
            summary["implementation_reference"] = {
                key: _short_text(value, 160)
                for key, value in implementation.items()
                if key in {
                    "identity", "repository", "commit", "path", "sha256",
                    "status", "content_integrity_status",
                }
                and value is not None
            }
        for key in (
            "verification_evidence_reference", "subsequent_evidence_reference",
        ):
            reference = relation.get(key)
            if isinstance(reference, dict):
                summary[key] = reference
        summary["evidence_reference"] = relation["evidence_reference"]
        compact_improvements.append(summary)

    activity = projection.get("session_activity")
    activity = activity if isinstance(activity, dict) else {}
    activity_source = activity.get("source")
    activity_source = activity_source if isinstance(activity_source, dict) else {}
    compact_activity_source = {
        key: activity_source[key]
        for key in (
            "event_count", "event_window_truncated",
            "event_window_sequence_start", "event_window_sequence_end",
            "invocation_events_considered", "invocation_records_omitted",
        )
        if key in activity_source
    }
    compact_activity_interactions = []
    raw_interactions = activity.get("interactions")
    raw_interactions = raw_interactions if isinstance(raw_interactions, list) else []
    for item in raw_interactions[:8]:
        if not isinstance(item, dict):
            continue
        compact_item = {
            key: _short_text(item.get(key), 160)
            for key in (
                "identity", "capability", "correlation_status",
                "outcome_status", "completion_producer",
            )
            if isinstance(item.get(key), str)
        }
        if type(item.get("returncode")) is int:
            compact_item["returncode"] = item["returncode"]
        for key in ("invocation_event", "completion_event"):
            reference = item.get(key)
            if isinstance(reference, dict):
                compact_item[key] = {
                    item_key: reference[item_key]
                    for item_key in ("identity", "sequence", "type")
                    if item_key in reference
                }
        compact_activity_interactions.append(compact_item)
    compact_activity = {
        "status": _short_text(activity.get("status"), 96),
        "source": compact_activity_source,
        "interaction_count": activity.get("interaction_count", 0),
        "completion_count": activity.get("completion_count", 0),
        "unmatched_invocation_count": activity.get("unmatched_invocation_count", 0),
        "ambiguous_correlation_count": activity.get("ambiguous_correlation_count", 0),
        "completion_without_invocation_count": activity.get(
            "completion_without_invocation_count", 0,
        ),
        "interactions": compact_activity_interactions,
        "interactions_truncated": (
            len(raw_interactions) > len(compact_activity_interactions)
        ),
    }

    result = {
        "schema_version": SCHEMA,
        "identity": projection["identity"],
        "status": "partial",
        "subject": compact_subject,
        "source": compact_source,
        "correlation": projection["correlation"],
        "observation_count": projection["observation_count"],
        "pressure_hypothesis_count": projection["pressure_hypothesis_count"],
        "improvement_relation_count": projection["improvement_relation_count"],
        "pressure_hypotheses": compact_hypotheses,
        "improvement_relations": compact_improvements,
        "observations": compact_observations,
        "session_activity": compact_activity,
        "measurement_limits": [
            "The response exceeded its byte limit; payload details are summarized by digest.",
            "Exact observation records remain addressable through their evidence references.",
            "The bounded read selects the newest observation references in each page; older skipped references may contain additional evidence.",
        ],
    }
    # The selected fields are individually bounded. This final reduction also
    # accounts for unusual but valid identifiers and reference paths.
    while len(json.dumps(result, ensure_ascii=False, sort_keys=True).encode("utf-8")) > max_result_bytes:
        if result["observations"]:
            result["observations"].pop()
        elif any(
            isinstance(hypothesis.get("evidence_references"), list)
            and len(hypothesis["evidence_references"]) > 1
            for hypothesis in result["pressure_hypotheses"]
        ):
            hypothesis = max(
                (
                    item for item in result["pressure_hypotheses"]
                    if isinstance(item.get("evidence_references"), list)
                    and len(item["evidence_references"]) > 1
                ),
                key=lambda item: len(item["evidence_references"]),
            )
            hypothesis["evidence_references"].pop()
            hypothesis["evidence_references_truncated"] = True
        elif result["pressure_hypotheses"]:
            result["pressure_hypotheses"].pop()
        elif result["improvement_relations"]:
            result["improvement_relations"].pop()
        elif result["session_activity"]["interactions"]:
            result["session_activity"]["interactions"].pop()
            result["session_activity"]["interactions_truncated"] = True
        else:
            break
    return result


def _is_observation_reference(reference: dict[str, str]) -> bool:
    return (reference["identity"].startswith("ev_environment_")
            and reference["path"].startswith(OBSERVATION_PATH_PREFIX))


def _git_blob(
    checkout: Path, commit: str, path: str, *, deadline: float,
    max_bytes: int = MAX_OBSERVATION_BYTES,
    oversized_status: str = "observation-too-large",
) -> tuple[bytes | None, str]:
    object_name = f"{commit}:{path}"
    environment = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, "read-budget-exhausted"
        size = subprocess.run(
            [workspace_module.git_binary(), "-C", str(checkout), "cat-file", "-s", object_name],
            capture_output=True, timeout=min(1.0, remaining), check=False, env=environment,
        )
        if size.returncode != 0:
            return None, "commit-path-unavailable"
        raw_size = size.stdout.strip()
        if not raw_size.isdigit():
            return None, "object-size-unavailable"
        if int(raw_size) > max_bytes:
            return None, oversized_status
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, "read-budget-exhausted"
        blob = subprocess.run(
            [workspace_module.git_binary(), "-C", str(checkout), "cat-file", "blob", object_name],
            capture_output=True, timeout=min(1.0, remaining), check=False, env=environment,
        )
    except subprocess.TimeoutExpired:
        return None, "read-budget-exhausted"
    except (OSError, subprocess.SubprocessError):
        return None, "git-read-unavailable"
    if blob.returncode != 0:
        return None, "commit-path-unavailable"
    if len(blob.stdout) > max_bytes:
        return None, oversized_status
    return blob.stdout, "read"


def _load_observation(
    session: Any, reference: dict[str, str], root: Path, *, deadline: float,
) -> tuple[dict[str, Any] | None, str]:
    checkout = _checkout(session, reference["repository"], root)
    if checkout is None:
        return None, "outside-selected-session"
    raw, status = _git_blob(
        checkout, reference["commit"], reference["path"], deadline=deadline,
    )
    if raw is None:
        return None, status
    if hashlib.sha256(raw).hexdigest() != reference["sha256"]:
        return None, "sha256-mismatch"
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "invalid-json"
    if not isinstance(record, dict):
        return None, "observation-contract-invalid"
    observation = record.get("observation")
    related_work = record.get("related_work")
    hypothesis = record.get("pressure_hypothesis")
    recorder = record.get("recorder")
    improvement_relation = record.get("improvement_relation")
    if (record.get("schema_version") != OBSERVATION_SCHEMA
            or record.get("identity") != reference["identity"]
            or not isinstance(record.get("captured_at"), str)
            or not isinstance(observation, dict)
            or not isinstance(observation.get("kind"), str)
            or not isinstance(observation.get("source_interface"), str)
            or not isinstance(observation.get("request"), dict)
            or not isinstance(observation.get("outcome"), dict)
            or not isinstance(related_work, dict)
            or any(not isinstance(related_work.get(key), str) or not related_work[key]
                   for key in ("campaign_id", "session_id", "work_intent_id", "work_intent_goal"))
            or (recorder is not None and (
                not isinstance(recorder, dict)
                or any(not isinstance(recorder.get(key), str)
                       or not recorder[key] or len(recorder[key]) > 160
                       for key in (
                           "campaign_id", "session_id", "consumer_id",
                           "environment_id", "definition_id", "environment_version",
                       )
                       if key in recorder)
            ))
            or (improvement_relation is not None
                and _validate_improvement_relation(improvement_relation) is None)
            or (hypothesis is not None and not isinstance(hypothesis, dict))):
        return None, "observation-contract-invalid"
    if isinstance(hypothesis, dict) and any(
        not isinstance(hypothesis.get(key), str) or not hypothesis[key]
        for key in ("identity", "title", "category", "subsystem", "owner",
                    "actionable_recommendation")
    ):
        return None, "observation-contract-invalid"
    return record, "verified"


def _source_status(source: Any, references: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(source, dict):
        return {"kind": "unknown", "status": "invalid-source-reference"}
    summary = {
        key: source.get(key)
        for key in ("identity", "kind", "repository", "commit", "path", "sha256",
                    "campaign_id", "session_id")
        if source.get(key) is not None
    }
    if source.get("kind") == "direct-operation-result":
        summary["status"] = "captured-in-observation-record"
        return summary
    wanted = tuple(source.get(key) for key in ("repository", "commit", "path", "sha256"))
    match = next((reference for reference in references
                  if tuple(reference.get(key) for key in
                           ("repository", "commit", "path", "sha256")) == wanted), None)
    if match is None:
        summary["status"] = "not-registered-in-campaign-ledger"
    else:
        summary["status"] = match.get("verification", "unverified")
        summary["campaign_evidence_identity"] = match.get("identity")
    return summary


def _source_pointer_key(pointer: dict[str, str]) -> tuple[str, str, str, str]:
    return (
        pointer["repository"], pointer["commit"], pointer["path"],
        pointer.get("sha256", ""),
    )


def _verify_source_pointer(
    session: Any, source: Any, references: list[dict[str, Any]], root: Path | None,
    *, deadline: float, cache: dict[tuple[str, str, str, str], str],
    attempted: set[tuple[str, str, str, str]],
) -> None:
    """Verify selected source blobs within the projection's shared read budget."""
    pointer = _validate_source_pointer(source)
    if pointer is None:
        return
    key = _source_pointer_key(pointer)
    if key in cache or key in attempted:
        return
    if root is None or _checkout(session, pointer["repository"], root) is None:
        cache[key] = "outside-selected-session"
        return
    if "sha256" not in pointer:
        cache[key] = "sha256-unavailable"
        return
    matching_reference = next((reference for reference in references
                               if _source_pointer_key(reference) == key), None)
    if matching_reference is not None and matching_reference.get("verification") == "verified":
        cache[key] = "record-integrity-verified-in-projection"
        return
    if len(attempted) >= MAX_SOURCE_POINTER_VERIFICATIONS:
        cache[key] = "source-verification-limit-exhausted"
        return
    attempted.add(key)
    checkout = _checkout(session, pointer["repository"], root)
    if checkout is None:
        cache[key] = "outside-selected-session"
        return
    raw, status = _git_blob(
        checkout, pointer["commit"], pointer["path"], deadline=deadline,
        max_bytes=MAX_SOURCE_BYTES, oversized_status="source-object-too-large",
    )
    if raw is None:
        cache[key] = {
            "read-budget-exhausted": "source-read-budget-exhausted",
            "commit-path-unavailable": "source-object-unavailable",
        }.get(status, status if status.startswith("source-") else f"source-{status}")
    elif hashlib.sha256(raw).hexdigest() != pointer["sha256"]:
        cache[key] = "sha256-mismatch"
    else:
        cache[key] = "content-integrity-verified"


def _source_integrity_status(source: Any, cache: dict[tuple[str, str, str, str], str]) -> str:
    if isinstance(source, dict) and source.get("kind") == "direct-operation-result":
        return "captured-in-observation-record"
    pointer = _validate_source_pointer(source)
    if pointer is None:
        return "source-reference-invalid"
    return cache.get(_source_pointer_key(pointer), "not-checked")


def _validate_improvement_relation(value: Any) -> dict[str, Any] | None:
    """Validate explicit links from a pressure hypothesis to change evidence."""
    if not isinstance(value, dict):
        return None
    identity = value.get("identity")
    hypothesis_identity = value.get("addresses_hypothesis_identity")
    verification_identity = value.get("verification_observation_identity")
    subsequent_identity = value.get("subsequent_observation_identity")
    implementation = _validate_reference(value.get("implementation_reference"))
    basis = value.get("basis")
    limits = value.get("not_measured", [])
    if (any(not isinstance(item, str) or not item or len(item) > 160
            for item in (identity, hypothesis_identity, verification_identity,
                         subsequent_identity))
            or implementation is None or "sha256" not in implementation
            or not isinstance(basis, str) or not basis or len(basis) > 1024
            or not isinstance(limits, list) or len(limits) > 16
            or any(not isinstance(item, str) or len(item) > 512 for item in limits)):
        return None
    return {
        "identity": identity,
        "addresses_hypothesis_identity": hypothesis_identity,
        "implementation_reference": implementation,
        "verification_observation_identity": verification_identity,
        "subsequent_observation_identity": subsequent_identity,
        "basis": basis,
        "not_measured": list(limits),
    }


def _recurrence(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for observation in observations:
        hypothesis = observation["pressure_hypothesis"]
        if not isinstance(hypothesis, dict):
            continue
        identity = hypothesis.get("identity")
        if isinstance(identity, str) and identity:
            grouped.setdefault(identity, []).append(observation)
    result = []
    for identity, group in sorted(grouped.items()):
        sessions = sorted({
            item["related_work"].get("session_id")
            for item in group
            if isinstance(item.get("related_work"), dict)
            and isinstance(item["related_work"].get("session_id"), str)
        })
        campaigns = sorted({
            item["related_work"].get("campaign_id")
            for item in group
            if isinstance(item.get("related_work"), dict)
            and isinstance(item["related_work"].get("campaign_id"), str)
        })
        work_contexts = _distinct_contexts(group, "related_work", (
            "campaign_id", "session_id", "work_intent_id", "work_intent_goal",
            "environment_id", "definition_id",
            "environment_version",
        ))
        recording_contexts = _distinct_contexts(group, "recording_context", (
            "campaign_id", "session_id", "consumer_id", "environment_id",
            "definition_id", "environment_version",
        ))
        work_intents = {
            (context.get("campaign_id"), context.get("work_intent_id"))
            for context in work_contexts
            if context.get("campaign_id") and context.get("work_intent_id")
        }
        environment_versions = {
            context["environment_version"] for context in work_contexts
            if context.get("environment_version")
        }
        recording_sessions = {
            context["session_id"] for context in recording_contexts
            if context.get("session_id")
        }
        recording_campaigns = {
            context["campaign_id"] for context in recording_contexts
            if context.get("campaign_id")
        }
        recording_environment_ids = {
            context["environment_id"] for context in recording_contexts
            if context.get("environment_id")
        }
        recording_definition_ids = {
            context["definition_id"] for context in recording_contexts
            if context.get("definition_id")
        }
        recording_environment_versions = {
            context["environment_version"] for context in recording_contexts
            if context.get("environment_version")
        }
        if len(sessions) > 1:
            recurrence_status = "observed-across-sessions"
        elif len(group) > 1:
            recurrence_status = "repeated-within-session"
        else:
            recurrence_status = "not-established"
        hypothesis = group[0]["pressure_hypothesis"]
        result.append({
            "identity": identity,
            "title": hypothesis.get("title"),
            "category": hypothesis.get("category"),
            "subsystem": hypothesis.get("subsystem"),
            "owner": hypothesis.get("owner"),
            "interpretation": "hypothesis",
            "observation_count": len(group),
            "distinct_session_count": len(sessions),
            "distinct_campaign_count": len(campaigns),
            "distinct_work_intent_count": len(work_intents),
            "distinct_environment_version_count": len(environment_versions),
            "affected_work_context_count": len(work_contexts),
            "affected_work_contexts": work_contexts,
            "distinct_recording_session_count": len(recording_sessions),
            "distinct_recording_campaign_count": len(recording_campaigns),
            "distinct_recording_environment_id_count": len(recording_environment_ids),
            "distinct_recording_definition_id_count": len(recording_definition_ids),
            "distinct_recording_environment_version_count": len(
                recording_environment_versions
            ),
            "recording_context_count": len(recording_contexts),
            "recording_contexts": recording_contexts,
            "recurrence_status": recurrence_status,
            "recommendation": hypothesis.get("actionable_recommendation"),
            "evidence": [item["evidence_reference"] for item in group],
        })
    return result


def _distinct_contexts(
    observations: list[dict[str, Any]], field: str, keys: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Retain bounded, distinct target or recorder contexts for one pressure."""
    contexts: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, str], ...]] = set()
    for observation in observations:
        raw = observation.get(field)
        if not isinstance(raw, dict):
            continue
        identity = tuple(
            (key, raw[key]) for key in keys
            if isinstance(raw.get(key), str) and raw[key]
        )
        if not identity or identity in seen:
            continue
        seen.add(identity)
        context: dict[str, Any] = {}
        for key, value in identity:
            limit = 256 if key in ("work_intent_goal", "campaign_objective") else 160
            context[key] = _short_text(value, limit) or ""
            if len(value) > limit:
                context[f"{key}_truncated"] = True
        if field == "recording_context":
            context["environment_identity_capture_status"] = (
                _environment_identity_capture_status(context)
            )
            context["environment_version_capture_status"] = (
                "captured-in-record" if context.get("environment_version")
                else "not-captured-in-record"
            )
        contexts.append(context)
    return contexts


def _environment_identity_capture_status(context: dict[str, Any]) -> str:
    if context.get("environment_id") and context.get("definition_id"):
        return "captured-in-record"
    if context.get("environment_id") or context.get("definition_id"):
        return "partial-in-record"
    return "not-captured-in-record"


def _recording_context(recorder: Any) -> dict[str, str] | None:
    if not isinstance(recorder, dict):
        return None
    context = {
        key: recorder[key]
        for key in (
            "campaign_id", "session_id", "consumer_id", "environment_id",
            "definition_id", "environment_version",
        )
        if isinstance(recorder.get(key), str) and recorder[key]
    }
    if not context:
        return None
    context["environment_identity_capture_status"] = (
        _environment_identity_capture_status(context)
    )
    context["environment_version_capture_status"] = (
        "captured-in-record" if context.get("environment_version")
        else "not-captured-in-record"
    )
    return context


def _event_reference(event: dict[str, Any]) -> dict[str, Any] | None:
    identity = event.get("identity")
    sequence = event.get("sequence")
    event_type = event.get("type")
    if (not isinstance(identity, str) or not identity or len(identity) > 160
            or type(sequence) is not int or sequence < 1
            or not isinstance(event_type, str) or not event_type or len(event_type) > 96):
        return None
    reference: dict[str, Any] = {
        "identity": identity,
        "sequence": sequence,
        "type": event_type,
    }
    observed_at = event.get("observed_at")
    if isinstance(observed_at, str) and observed_at and len(observed_at) <= 96:
        reference["observed_at"] = observed_at
    return reference


def _session_activity(session: Any, snapshot: dict[str, Any]) -> dict[str, Any]:
    """Project recent Environment capability events without copying arguments."""
    reader = getattr(session, "recent_events", None)
    limits = [
        "This view covers Environment-bound capability events; direct external tool operations may not appear in the session event stream.",
        "Invocation arguments, raw results, token usage, execution duration, and resource consumption are not inferred from these events.",
        "The canonical Store reader currently materializes the session event log before selecting this bounded tail.",
    ]
    if not callable(reader):
        return {
            "status": "unavailable",
            "context": {},
            "source": {"event_window_limit": MAX_SESSION_EVENT_WINDOW},
            "interaction_count": 0,
            "interactions": [],
            "capabilities": [],
            "measurement_limits": [
                *limits,
                "The session event tail interface is unavailable.",
            ],
        }
    try:
        page = reader(limit=MAX_SESSION_EVENT_WINDOW)
    except (LifecycleError, StoreIntegrityFailure, StoreUnavailable, OSError, ValueError):
        page = None
    if (not isinstance(page, dict)
            or type(page.get("event_count")) is not int
            or page["event_count"] < 0
            or not isinstance(page.get("events"), list)
            or type(page.get("truncated")) is not bool):
        return {
            "status": "unavailable",
            "context": {},
            "source": {"event_window_limit": MAX_SESSION_EVENT_WINDOW},
            "interaction_count": 0,
            "interactions": [],
            "capabilities": [],
            "measurement_limits": [
                *limits,
                "The session event tail could not be read or did not match its declared shape.",
            ],
        }

    raw_page_events = page["events"]
    adapter_window_overflow = len(raw_page_events) > MAX_SESSION_EVENT_WINDOW
    if adapter_window_overflow:
        raw_page_events = raw_page_events[-MAX_SESSION_EVENT_WINDOW:]
    raw_events = [item for item in raw_page_events if isinstance(item, dict)]
    events = sorted(
        raw_events,
        key=lambda item: item.get("sequence") if type(item.get("sequence")) is int else -1,
    )
    invocation_events = [
        event for event in events if event.get("type") == "capability.invoked"
    ]
    completion_events = [
        event for event in events if event.get("type") == "invocation.completed"
    ]
    starts_by_request: dict[str, list[dict[str, Any]]] = {}
    completions_by_request: dict[str, list[dict[str, Any]]] = {}
    for event in invocation_events:
        payload = event.get("payload")
        request_id = payload.get("provider_request_id") if isinstance(payload, dict) else None
        if isinstance(request_id, str) and request_id and len(request_id) <= 160:
            starts_by_request.setdefault(request_id, []).append(event)
    for event in completion_events:
        payload = event.get("payload")
        request_id = payload.get("provider_request_id") if isinstance(payload, dict) else None
        if isinstance(request_id, str) and request_id and len(request_id) <= 160:
            completions_by_request.setdefault(request_id, []).append(event)

    paired_by_start: dict[int, tuple[dict[str, Any] | None, str]] = {}
    for request_id in set(starts_by_request) | set(completions_by_request):
        starts = starts_by_request.get(request_id, [])
        completions = completions_by_request.get(request_id, [])
        if len(starts) == 1 and len(completions) == 1:
            start, completion = starts[0], completions[0]
            start_sequence = start.get("sequence")
            completion_sequence = completion.get("sequence")
            if (type(start_sequence) is int and type(completion_sequence) is int
                    and completion_sequence > start_sequence):
                paired_by_start[id(start)] = (
                    completion, "matched-by-provider-request-id",
                )
            else:
                paired_by_start[id(start)] = (None, "completion-precedes-invocation")
        elif len(starts) > 1:
            for start in starts:
                paired_by_start[id(start)] = (None, "ambiguous-provider-request-id")
        elif len(starts) == 1:
            correlation = (
                "ambiguous-provider-request-id" if len(completions) > 1
                else "completion-not-recorded-in-window"
            )
            paired_by_start[id(starts[0])] = (None, correlation)

    interactions: list[dict[str, Any]] = []
    for event in reversed(invocation_events):
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        completion, correlation_status = paired_by_start.get(
            id(event), (None, "provider-request-id-unavailable"),
        )
        invocation_reference = _event_reference(event)
        if invocation_reference is None:
            continue
        completion_reference = _event_reference(completion) if completion else None
        completion_payload = completion.get("payload") if isinstance(completion, dict) else None
        completion_payload = completion_payload if isinstance(completion_payload, dict) else {}
        capability = payload.get("capability")
        interaction = {
            "identity": "act_" + digest_hex({
                "session_id": session.session_id,
                "invocation_event": invocation_reference["identity"],
                "completion_event": (
                    completion_reference.get("identity")
                    if completion_reference is not None else None
                ),
            }, length=16),
            "capability": _short_text(capability, 160) if isinstance(capability, str) else None,
            "correlation_status": correlation_status,
            "outcome_status": (
                _short_text(completion_payload.get("status"), 96)
                if isinstance(completion_payload.get("status"), str) else None
            ),
            "returncode": (
                completion_payload.get("returncode")
                if type(completion_payload.get("returncode")) is int else None
            ),
            "completion_producer": (
                _short_text(completion.get("producer"), 128)
                if isinstance(completion, dict)
                and isinstance(completion.get("producer"), str) else None
            ),
            "invocation_event": invocation_reference,
            "completion_event": completion_reference,
        }
        interactions.append(interaction)
        if len(interactions) >= MAX_SESSION_ACTIVITY_INTERACTIONS:
            break

    capability_groups: dict[str, list[dict[str, Any]]] = {}
    for interaction in interactions:
        key = interaction.get("capability")
        group_key = key if isinstance(key, str) else ""
        capability_groups.setdefault(group_key, []).append(interaction)
    capabilities = []
    for capability, group in sorted(capability_groups.items()):
        status_counts: dict[str, int] = {}
        for item in group:
            status = item.get("outcome_status")
            if isinstance(status, str):
                status_counts[status] = status_counts.get(status, 0) + 1
        capabilities.append({
            "capability": capability or None,
            "invocation_count": len(group),
            "completion_count": sum(
                item.get("completion_event") is not None for item in group
            ),
            "outcome_status_counts": status_counts,
        })
    capabilities_omitted = max(
        0, len(capabilities) - MAX_SESSION_ACTIVITY_CAPABILITIES,
    )
    capabilities = capabilities[:MAX_SESSION_ACTIVITY_CAPABILITIES]

    event_count = page["event_count"]
    event_window_truncated = (
        page["truncated"] or event_count > len(page["events"])
        or adapter_window_overflow
    )
    invocations_omitted = max(0, len(invocation_events) - len(interactions))
    partial = event_window_truncated or invocations_omitted > 0
    if partial and not invocation_events:
        status = "partial-no-invocations-in-selected-window"
    elif partial:
        status = "partial"
    elif not invocation_events:
        status = "no-capability-invocations-in-session-history"
    else:
        status = "ready"
    window_references = [
        reference for event in events
        if (reference := _event_reference(event)) is not None
    ]
    event_type_counts: dict[str, int] = {}
    for event in events:
        event_type = event.get("type")
        if isinstance(event_type, str) and event_type and len(event_type) <= 96:
            event_type_counts[event_type] = event_type_counts.get(event_type, 0) + 1
    unpaired_completion_count = 0
    for event in completion_events:
        payload = event.get("payload")
        request_id = payload.get("provider_request_id") if isinstance(payload, dict) else None
        starts = starts_by_request.get(request_id, []) if isinstance(request_id, str) else []
        if len(starts) != 1:
            unpaired_completion_count += 1
            continue
        start_sequence = starts[0].get("sequence")
        completion_sequence = event.get("sequence")
        if (type(start_sequence) is not int or type(completion_sequence) is not int
                or completion_sequence <= start_sequence):
            unpaired_completion_count += 1
    snapshot_intent = snapshot.get("intent")
    snapshot_intent = snapshot_intent if isinstance(snapshot_intent, dict) else {}
    campaign = snapshot.get("campaign")
    campaign = campaign if isinstance(campaign, dict) else {}
    provenance = snapshot.get("provenance")
    provenance = provenance if isinstance(provenance, dict) else {}
    context = {
        key: _short_text(value, 256 if key == "work_intent_goal" else 160)
        for key, value in {
            "campaign_id": campaign.get("identity"),
            "session_id": session.session_id,
            "work_intent_id": snapshot_intent.get("identity"),
            "work_intent_goal": snapshot_intent.get("goal"),
            "environment_id": snapshot.get("environment_id"),
            "definition_id": provenance.get("definition_id"),
        }.items()
        if isinstance(value, str) and value
    }
    return {
        "status": status,
        "context": context,
        "source": {
            "event_count": event_count,
            "event_window_limit": MAX_SESSION_EVENT_WINDOW,
            "events_considered": len(raw_page_events),
            "event_window_truncated": event_window_truncated,
            "event_window_identity_digest": digest_hex(window_references, length=16),
            "event_window_sequence_start": (
                window_references[0]["sequence"] if window_references else None
            ),
            "event_window_sequence_end": (
                window_references[-1]["sequence"] if window_references else None
            ),
            "event_type_counts": event_type_counts,
            "invocation_events_considered": len(invocation_events),
            "completion_events_considered": len(completion_events),
            "invocation_records_omitted": invocations_omitted,
            "capability_summaries_omitted": capabilities_omitted,
            "selection_policy": "newest-invocation-events-by-session-sequence",
        },
        "interaction_count": len(interactions),
        "completion_count": sum(
            item.get("completion_event") is not None for item in interactions
        ),
        "unmatched_invocation_count": sum(
            item.get("completion_event") is None for item in interactions
        ),
        "ambiguous_correlation_count": sum(
            item.get("correlation_status") == "ambiguous-provider-request-id"
            for item in interactions
        ),
        "completion_without_invocation_count": unpaired_completion_count,
        "interactions": interactions,
        "capabilities": capabilities,
        "measurement_limits": limits,
    }


def project(
    session: Any, *, campaign_state: dict[str, Any] | None = None,
    max_result_bytes: int = MAX_RESULT_BYTES, history_pages: int = 1,
    evidence_offset: int | None = None,
    evidence_limit: int = MAX_EVIDENCE_REFS,
) -> dict[str, Any]:
    """Build a bounded read-only relationship view from campaign evidence.

    Only records in ``evidence/environmental-capability/`` are read. Their
    exact Git commit and SHA-256 must be present in the authenticated campaign
    ledger, and the repository must be selected by this Environment session.
    """
    if (type(max_result_bytes) is not int
            or max_result_bytes < 1 or max_result_bytes > MAX_RESULT_BYTES):
        raise ValueError("capability projection result limit must be within its configured bound")
    if (type(history_pages) is not int
            or not 1 <= history_pages <= MAX_EVIDENCE_HISTORY_PAGES):
        raise ValueError(
            "capability projection history pages must be between 1 and "
            f"{MAX_EVIDENCE_HISTORY_PAGES}"
        )
    if (type(evidence_offset) not in (int, type(None))
            or (evidence_offset is not None and evidence_offset < 0)):
        raise ValueError("capability projection evidence offset must be nonnegative")
    if (type(evidence_limit) is not int
            or not 1 <= evidence_limit <= MAX_EVIDENCE_REFS):
        raise ValueError(
            "capability projection evidence limit must be between 1 and "
            f"{MAX_EVIDENCE_REFS}"
        )
    if campaign_state is not None and (
        evidence_offset is not None or evidence_limit != MAX_EVIDENCE_REFS
    ):
        raise ValueError(
            "explicit campaign state cannot be combined with evidence page arguments"
        )
    snapshot = session.snapshot
    campaign = snapshot.get("campaign") or {}
    intent = snapshot.get("intent") or {}
    if campaign_state is None:
        try:
            campaign_state = session.campaign_state(
                evidence_offset=evidence_offset,
                evidence_limit=evidence_limit,
            )
        except LifecycleError as error:
            if evidence_offset is not None:
                raise ValueError(str(error)) from error
            campaign_state = None
        except (StoreIntegrityFailure, StoreUnavailable):
            campaign_state = None
    state = campaign_state if isinstance(campaign_state, dict) else {}
    page_truncated = bool(state.get("evidence_truncated", False))
    history_states = [state]
    history_truncated = False
    history_consistent = True
    if history_pages > 1:
        if campaign_state is not None:
            history_states, history_truncated, history_consistent = _evidence_history_pages(
                session, state, history_pages,
            )
        else:
            history_truncated = True
            history_consistent = False
    page_entries = []
    page_observation_capacities = [0 for _ in history_states]
    page_observation_references: list[list[dict[str, str]]] = [
        [] for _ in history_states
    ]
    capacity_seen: set[tuple[str, str, str, str]] = set()
    for page_index, page_state in enumerate(history_states):
        page_references = page_state.get("evidence", [])
        page_references = (
            page_references[-MAX_EVIDENCE_REFS:]
            if isinstance(page_references, list) else []
        )
        for raw in page_references:
            page_entries.append((raw, page_index))
            pointer = _validate_reference(raw)
            if pointer is None or not _is_observation_reference(pointer):
                continue
            source_key = (
                pointer["repository"], pointer["commit"],
                pointer["path"], pointer.get("sha256", ""),
            )
            if source_key in capacity_seen:
                continue
            capacity_seen.add(source_key)
            page_observation_capacities[page_index] += 1
            page_observation_references[page_index].append(pointer)
    observation_read_budgets = _allocate_page_observation_budgets(
        page_observation_capacities, MAX_OBSERVATIONS,
    )
    selected_observation_keys_by_page = []
    selected_observation_identities_by_page = []
    for page_index, page_references in enumerate(page_observation_references):
        budget = observation_read_budgets[page_index]
        selected = page_references[-budget:] if budget else []
        selected_observation_keys_by_page.append({
            (
                pointer["repository"], pointer["commit"], pointer["path"],
                pointer.get("sha256", ""),
            )
            for pointer in selected
        })
        selected_observation_identities_by_page.append([
            pointer["identity"] for pointer in selected
        ])
    root = _workspace_root(session)
    references: list[dict[str, Any]] = []
    verified_records: list[tuple[dict[str, Any], dict[str, Any]]] = []
    seen_observations: set[tuple[str, str, str, str]] = set()
    unavailable = 0
    skipped_by_page_budget = 0
    read_deadline = time.monotonic() + MAX_OBSERVATION_READ_SECONDS
    for raw, page_index in page_entries:
        pointer = _validate_reference(raw)
        if pointer is None:
            continue
        if not _is_observation_reference(pointer):
            verification = (
                "outside-selected-session"
                if root is None or _checkout(session, pointer["repository"], root) is None
                else "not-an-environmental-observation"
            )
            references.append({**pointer, "verification": verification})
            continue
        source_key = (
            pointer["repository"], pointer["commit"],
            pointer["path"], pointer.get("sha256", ""),
        )
        if source_key in seen_observations:
            references.append({**pointer, "verification": "duplicate-reference"})
            continue
        seen_observations.add(source_key)
        if source_key not in selected_observation_keys_by_page[page_index]:
            references.append({
                **pointer,
                "verification": "not-read-page-observation-budget",
            })
            skipped_by_page_budget += 1
            continue
        record = None
        verification = "sha256-unavailable" if "sha256" not in pointer else "workspace-unavailable"
        if root is not None and "sha256" in pointer:
            record, verification = _load_observation(
                session, pointer, root, deadline=read_deadline,
            )
        reference = {**pointer, "verification": verification}
        references.append(reference)
        if record is None:
            unavailable += 1
            continue
        verified_records.append((record, reference))
    observations: list[dict[str, Any]] = []
    improvement_relations: list[dict[str, Any]] = []
    projected_records = verified_records[:MAX_OBSERVATIONS]
    source_integrity: dict[tuple[str, str, str, str], str] = {}
    attempted_sources: set[tuple[str, str, str, str]] = set()
    for record, _ in projected_records:
        raw_sources = record.get("supporting_sources")
        raw_sources = raw_sources if isinstance(raw_sources, list) else []
        for source in raw_sources[:16]:
            _verify_source_pointer(
                session, source, references, root, deadline=read_deadline,
                cache=source_integrity, attempted=attempted_sources,
            )
        improvement = _validate_improvement_relation(record.get("improvement_relation"))
        if improvement is not None:
            _verify_source_pointer(
                session, improvement["implementation_reference"], references, root,
                deadline=read_deadline, cache=source_integrity,
                attempted=attempted_sources,
            )
    projected_record_ids = {record["identity"] for record, _ in projected_records}
    projected_hypothesis_ids: set[str] = set()
    for record, _ in projected_records:
        hypothesis = record.get("pressure_hypothesis")
        if isinstance(hypothesis, dict) and isinstance(hypothesis.get("identity"), str):
            projected_hypothesis_ids.add(hypothesis["identity"])
    projected_references_by_identity = {
        record["identity"]: reference
        for record, reference in projected_records
    }
    for record, reference in projected_records:
        observation = record["observation"]
        work = record.get("related_work")
        work = work if isinstance(work, dict) else {}
        recorder = record.get("recorder")
        recorder = recorder if isinstance(recorder, dict) else {}
        hypothesis = record.get("pressure_hypothesis")
        hypothesis = hypothesis if isinstance(hypothesis, dict) else None
        sources = record.get("supporting_sources")
        sources = sources if isinstance(sources, list) else []
        improvement = _validate_improvement_relation(record.get("improvement_relation"))
        observations.append({
            "identity": record["identity"],
            "captured_at": record.get("captured_at"),
            "measurement_basis": record.get("measurement_basis", "recorded operational response"),
            "observation": {
                "kind": observation.get("kind"),
                "source_interface": observation.get("source_interface"),
                "request": observation.get("request"),
                "outcome": observation.get("outcome"),
            },
            "related_work": {
                key: work.get(key)
                for key in ("campaign_id", "session_id", "work_intent_id",
                            "work_intent_goal", "campaign_objective", "environment_id",
                            "definition_id", "environment_version", "lifecycle", "latest_checkpoint",
                            "campaign_evidence_count", "claim_ids")
                if work.get(key) is not None
            },
            "recording_context": _recording_context(recorder),
            "improvement_relation_identity": (
                improvement["identity"] if improvement is not None else None
            ),
            "pressure_hypothesis": ({
                key: hypothesis.get(key)
                for key in ("identity", "title", "category", "subsystem", "owner",
                            "basis", "recurrence", "actionable_recommendation",
                            "not_measured")
                if hypothesis.get(key) is not None
            } if hypothesis is not None else None),
            "supporting_sources": [
                {
                    **_source_status(source, references),
                    "content_integrity_status": _source_integrity_status(
                        source, source_integrity,
                    ),
                }
                for source in sources[:16]
            ],
            "measurement_limits": [
                value[:512] for value in record.get("not_measured", [])[:16]
                if isinstance(value, str)
            ] if isinstance(record.get("not_measured"), list) else [],
            "evidence_reference": {
                key: reference[key]
                for key in ("identity", "repository", "commit", "path", "sha256")
            },
        })
        if improvement is not None:
            implementation_status = _source_status(
                improvement["implementation_reference"], references,
            )
            implementation_status["identity"] = improvement["implementation_reference"]["identity"]
            implementation_status["content_integrity_status"] = _source_integrity_status(
                improvement["implementation_reference"], source_integrity,
            )
            relation_projection = {
                "identity": improvement["identity"],
                "recorded_by_observation_identity": record["identity"],
                "addresses_hypothesis_identity": improvement["addresses_hypothesis_identity"],
                "pressure_hypothesis_status": (
                    "present-in-projection"
                    if improvement["addresses_hypothesis_identity"] in projected_hypothesis_ids
                    else "not-present-in-projection"
                ),
                "implementation_reference": implementation_status,
                "verification_observation_identity": improvement[
                    "verification_observation_identity"
                ],
                "verification_record_status": (
                    "record-integrity-verified-in-projection"
                    if improvement["verification_observation_identity"] in projected_record_ids
                    else "not-present-in-projection"
                ),
                "subsequent_observation_identity": improvement[
                    "subsequent_observation_identity"
                ],
                "subsequent_record_status": (
                    "record-integrity-verified-in-projection"
                    if improvement["subsequent_observation_identity"] in projected_record_ids
                    else "not-present-in-projection"
                ),
                "effectiveness_assessment": "not-assessed",
                "basis": improvement["basis"],
                "measurement_limits": improvement["not_measured"],
                "evidence_reference": {
                    key: reference[key]
                    for key in ("identity", "repository", "commit", "path", "sha256")
                },
            }
            for identity_key, reference_key in (
                ("verification_observation_identity", "verification_evidence_reference"),
                ("subsequent_observation_identity", "subsequent_evidence_reference"),
            ):
                linked_reference = projected_references_by_identity.get(
                    improvement[identity_key],
                )
                if linked_reference is not None:
                    relation_projection[reference_key] = {
                        key: linked_reference[key]
                        for key in ("identity", "repository", "commit", "path", "sha256")
                    }
            improvement_relations.append(relation_projection)
        if len(observations) >= MAX_OBSERVATIONS:
            break
    if campaign_state is None:
        status = "unavailable"
    elif observations and (
        unavailable or skipped_by_page_budget or history_truncated or not history_consistent
    ):
        status = "partial"
    elif observations:
        status = "ready"
    elif page_entries:
        status = "partial"
    else:
        status = "no-observations"
    hypotheses = _recurrence(observations)
    activity = _session_activity(session, snapshot)
    projection: dict[str, Any] = {
        "schema_version": SCHEMA,
        "identity": "eci_" + digest_hex({
            "session_id": session.session_id,
            "campaign_id": campaign.get("identity"),
            "evidence": [item["identity"] for item in references],
            "session_activity_digest": (
                activity.get("source", {}).get("event_window_identity_digest")
                if isinstance(activity.get("source"), dict) else None
            ),
        }, length=16),
        "status": status,
        "subject": {
            "session_id": session.session_id,
            "campaign_id": campaign.get("identity"),
            "environment_id": snapshot.get("environment_id"),
            "definition_id": (snapshot.get("provenance") or {}).get("definition_id"),
            "work_intent": {
                "identity": intent.get("identity"),
                "goal": intent.get("goal"),
            },
        },
        "source": {
            "campaign_state_checkpoint_id": state.get("checkpoint_id"),
            "recorded_at": state.get("recorded_at"),
            "recorded_by": state.get("recorded_by"),
            "evidence_count": state.get("evidence_count", 0),
            "evidence_page_truncated": page_truncated,
            "evidence_page_offset": (
                state.get("evidence_offset")
                if type(state.get("evidence_offset")) is int
                and state["evidence_offset"] >= 0 else None
            ),
            "evidence_page_limit": (
                state.get("evidence_limit")
                if type(state.get("evidence_limit")) is int
                and state["evidence_limit"] > 0 else None
            ),
            "previous_evidence_offset": (
                state.get("previous_evidence_offset")
                if type(state.get("previous_evidence_offset")) is int
                and state["previous_evidence_offset"] >= 0 else None
            ),
            "next_evidence_offset": (
                state.get("next_evidence_offset")
                if type(state.get("next_evidence_offset")) is int
                and state["next_evidence_offset"] >= 0 else None
            ),
            "source_pointer_count": len(source_integrity),
            "source_pointer_content_integrity_verified_count": sum(
                status in SOURCE_INTEGRITY_VERIFIED
                for status in source_integrity.values()
            ),
            "source_pointer_content_integrity_not_verified_count": sum(
                status not in SOURCE_INTEGRITY_VERIFIED
                for status in source_integrity.values()
            ),
            "observation_records_considered": len(references),
            "observation_records_verified": len(observations),
            "observation_records_unavailable": unavailable,
            "observation_reference_selection_policy": (
                "newest-in-ledger-append-order-within-each-page"
            ),
            "evidence_history_observation_budgets_by_page": [
                {
                    "offset": history_states[index].get("evidence_offset"),
                    "page_limit": history_states[index].get("evidence_limit"),
                    "observation_reference_capacity": page_observation_capacities[index],
                    "read_budget": observation_read_budgets[index],
                    "selected_observation_reference_identities": (
                        selected_observation_identities_by_page[index]
                    ),
                    "observation_records_skipped_by_page_budget": (
                        page_observation_capacities[index]
                        - observation_read_budgets[index]
                    ),
                }
                for index in range(len(history_states))
            ],
            "observation_records_skipped_by_page_budget": skipped_by_page_budget,
            **({
                "evidence_history_pages_requested": history_pages,
                "evidence_history_pages_read": len(history_states),
                "evidence_history_page_offsets": [
                    page["evidence_offset"] for page in history_states
                    if type(page.get("evidence_offset")) is int
                ],
                "evidence_history_page_limits": [
                    page["evidence_limit"] for page in history_states
                    if type(page.get("evidence_limit")) is int
                ],
                "evidence_history_truncated": history_truncated,
                "evidence_history_checkpoint_consistent": history_consistent,
            } if history_pages > 1 else {}),
        },
        "correlation": {
            "kind": "campaign-evidence-and-session-events-to-objective-and-subsystem",
            "derived": True,
            "relationship": "Campaign observations retain affected work and recorder context; capability events link to the current objective and Environment; improvement relations link pressures, implementation, verification, and follow-up.",
        },
        "observation_count": len(observations),
        "pressure_hypothesis_count": len(hypotheses),
        "improvement_relation_count": len(improvement_relations),
        "pressure_hypotheses": hypotheses,
        "improvement_relations": improvement_relations,
        "observations": observations,
        "session_activity": activity,
        "measurement_limits": [
            "Only committed observation records linked from the selected bounded campaign-state page or pages are considered.",
            "The source evidence page offset and previous/next offsets identify adjacent pages when continuing this projection.",
            "Source pointer integrity counts cover unique committed pointers; sources outside selected checkouts are not read and remain not verified.",
            "An unavailable or unselected source is surfaced without reading it or treating it as verified.",
            "Repeated references to the same committed observation do not establish recurrence.",
            "Within each selected page, bounded observation reads select the newest references in ledger append order; earlier references in that page may remain unread.",
            "Candidate continuation count is not treated as repeated pressure; recurrence requires distinct recorded observations.",
            "Effectiveness after a proposed change remains unknown until later independent observations support it.",
            "A linked verification record establishes that evidence is present and integrity checked; effectiveness still requires independent follow-up.",
            "Session activity summarizes only the bounded recent event tail and exposes no raw invocation arguments or results.",
            "Session capability events do not establish duration, token usage, resource consumption, or activity performed outside Environment-bound capability invocation.",
            *([
                "Multi-page recurrence covers only the selected bounded page window; source metadata reports page offsets, checkpoint consistency, and budget omissions.",
            ] if history_pages > 1 else []),
        ],
    }
    encoded = json.dumps(projection, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(encoded) > max_result_bytes:
        return _compact_projection(projection, len(encoded), max_result_bytes)
    return projection
