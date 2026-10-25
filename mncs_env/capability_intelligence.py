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
import math
import os
import re
import subprocess
import time
from pathlib import Path, PurePosixPath
from typing import Any

from . import authority as authority_module
from . import invocation_artifacts as invocation_artifacts_module
from . import workspace as workspace_module
from .identity import digest_hex
from .sessions import MAX_RPC_READ_ACTIVITY_OBSERVATIONS, LifecycleError
from .store_backend import StoreIntegrityFailure, StoreUnavailable

SCHEMA = "mncs.environment.capability-intelligence/1"
OBSERVATION_SCHEMA = "mncs.environment.environmental-observation/1"
OBSERVATION_CORRECTION_SCHEMA = "mncs.environment.observation-correction/1"
EVIDENCE_REFERENCE_CORRECTION_SCHEMA = (
    "mncs.environment.evidence-reference-correction/1"
)
CAMPAIGN_CHARTER_SCHEMA = "mncs.environment.campaign-charter/1"
OBSERVATION_PATH_PREFIX = "evidence/environmental-capability/"
COMPILER_CACHE_REGRESSION_SCHEMA_V1 = (
    "mncs.compiler.language-module-elaboration-cache-regression/1"
)
COMPILER_CACHE_REGRESSION_SCHEMA_V2 = (
    "mncs.compiler.language-module-elaboration-cache-regression/2"
)
COMPILER_CACHE_REGRESSION_REFERENCE_FAMILIES = (
    (
        "effect-capability",
        "ev_compiler_digest_application_effect_capability_invalidation_",
        "evidence/native-app-readiness-effect-capability-fact-invalidation-",
        COMPILER_CACHE_REGRESSION_SCHEMA_V2,
        (
            "cold", "effect_assumption_edit", "capability_assumption_edit",
            "fresh_process",
        ),
    ),
    (
        "interface-invalidation",
        "ev_compiler_digest_application_interface_invalidation_",
        "evidence/language-module-interface-application-invalidation-compiler-",
        COMPILER_CACHE_REGRESSION_SCHEMA_V1,
        (
            "cold", "imported_body_edit", "imported_function_result_name_edit",
            "imported_nominal_cold", "imported_nominal_field_addition",
            "imported_signature_cold", "invalid_source", "root_comment_edit",
            "tampered_cache_digest", "fresh_process",
        ),
    ),
    (
        "generic-specialization-invalidation",
        "ev_compiler_digest_application_generic_specialization_invalidation_",
        "evidence/language-module-generic-specialization-application-invalidation-compiler-",
        COMPILER_CACHE_REGRESSION_SCHEMA_V1,
        (
            "cold", "generic_specialization_cold", "generic_summarize_body_edit",
            "imported_body_edit", "imported_function_result_name_edit",
            "fresh_process", "tampered_cache_digest",
        ),
    ),
)
COMPILER_LANGUAGE_DEPENDENCY_SUMMARY_SCHEMA = (
    "mncs.compiler.language-dependency-summary-cache-evidence/2"
)
COMPILER_LANGUAGE_DEPENDENCY_SUMMARY_IDENTITY_PREFIX = (
    "ev_compiler_language_fact_reuse_workload_"
)
COMPILER_LANGUAGE_DEPENDENCY_SUMMARY_PATH_PREFIX = (
    "evidence/language-verification-dependency-summary-cache-"
)
COMPILER_LANGUAGE_DEPENDENCY_SUMMARY_STAGES = (
    "cold", "comment_edit", "environment_provider_request_12",
    "fresh_process_comment_edit", "invalid_source",
    "reachable_imported_body_edit", "unchanged", "unreachable_body_edit",
)
MAX_EVIDENCE_REFS = 32
MAX_EVIDENCE_HISTORY_PAGES = 4
MAX_OBSERVATIONS = 16
MAX_OBSERVATION_BYTES = 64 * 1024
MAX_SOURCE_BYTES = 512 * 1024
MAX_OBSERVATION_READ_SECONDS = 8.0
MAX_SOURCE_POINTER_VERIFICATIONS = 32
MAX_EVIDENCE_REFERENCE_CORRECTIONS = 8
MAX_CAMPAIGN_CHARTER_CANDIDATES = 4
MAX_CAMPAIGN_CHARTER_OBJECTIVE_CHARS = 4096
MAX_RESULT_BYTES = 48 * 1024
CAPTURED_OPERATION_RESULT_SOURCE_KINDS = frozenset({
    "direct-operation-result", "direct-cli-projection-result",
})
MAX_SESSION_EVENT_WINDOW = 256
MAX_SESSION_RESOURCE_ARTIFACT_WINDOW = 256
MAX_SESSION_RESOURCE_SAMPLES = 4
MAX_RESOURCE_SAMPLE_BYTES = 64 * 1024
SYSTEM_RESOURCE_SAMPLE_CAPABILITY = "mncs-system-monitor:system-resource-sample"
SYSTEM_RESOURCE_SAMPLE_SCHEMA = "mncs.system-monitor.resource-sample.v1"
SYSTEM_RESOURCE_SAMPLE_PATH = "system-resource-sample.json"
SYSTEM_RESOURCE_SAMPLE_STATUSES = frozenset({
    "observed", "unknown", "unavailable", "unsupported", "permission_denied",
    "malformed", "disappeared", "stale", "counter_regression",
    "invalid_interval",
})
MAX_SESSION_ACTIVITY_INTERACTIONS = 32
MAX_SESSION_ACTIVITY_CAPABILITIES = 16
MAX_SESSION_ACTIVITY_RPC_OPERATIONS = 32
MAX_SESSION_ACTIVITY_RPC_PRESSURE_CANDIDATES = 8
MAX_SESSION_ACTIVITY_RPC_SUMMARY_GROUPS = 32
MAX_SESSION_ACTIVITY_RPC_SUMMARY_REFERENCES = 3
MAX_SESSION_ACTIVITY_AUTHORITY_DENIALS = 16
MAX_SESSION_ACTIVITY_AUTHORITY_PRESSURE_CANDIDATES = 8
SOURCE_INTEGRITY_VERIFIED = frozenset({
    "content-integrity-verified",
    "record-integrity-verified-in-projection",
    "workspace-read-only-content-integrity-verified",
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


def _workspace_read_only_checkout(
    session: Any, repository: str, root: Path,
    deadline: float,
) -> Path | None:
    """Resolve one repository only under explicit read-only workspace authority."""
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", repository)
            or repository in {".", ".."}):
        return None
    authority = session.snapshot.get("authority")
    readable = authority.get("readable") if isinstance(authority, dict) else None
    if not isinstance(readable, list) or "workspace" not in readable:
        return None
    workspace = session.snapshot.get("workspace")
    if not isinstance(workspace, dict):
        return None
    try:
        recorded_root = Path(workspace.get("root", "")).resolve(strict=True)
        trusted_root = root.resolve(strict=True)
    except (OSError, TypeError, ValueError):
        return None
    if recorded_root != trusted_root:
        return None
    scan = workspace.get("scan")
    if not isinstance(scan, dict) or scan.get("status") != "complete":
        return None
    records = workspace.get("repositories")
    if not isinstance(records, list):
        return None
    matches = [
        record for record in records
        if isinstance(record, dict)
        and record.get("name") == repository
        and record.get("worktree_of") is None
    ]
    if not matches:
        # The session registry can be intentionally limited to selected
        # checkouts. Workspace read authority permits one bounded lookup.
        remaining = deadline - time.monotonic()
        if remaining <= 0.1:
            return None
        discovery = workspace_module.discover_workspace(
            trusted_root, timeout_seconds=min(2.0, remaining), repositories=[repository],
        )
        discovery_scan = discovery.get("scan")
        if (not isinstance(discovery_scan, dict)
                or discovery_scan.get("status") != "complete"):
            return None
        discovered = discovery.get("repositories")
        if not isinstance(discovered, list):
            return None
        matches = [
            record for record in discovered
            if isinstance(record, dict)
            and record.get("name") == repository
            and record.get("worktree_of") is None
        ]
    if len(matches) != 1:
        return None
    record = matches[0]
    raw_path = record.get("path")
    if not isinstance(raw_path, str) or not raw_path:
        return None
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = trusted_root / candidate
    try:
        if candidate.is_symlink():
            return None
        checkout = candidate.resolve(strict=True)
        relative = checkout.relative_to(trusted_root)
    except (OSError, ValueError):
        return None
    if (len(relative.parts) != 1 or relative.parts[0] != repository
            or checkout.name != repository or not checkout.is_dir()
            or not (checkout / ".git").exists()
            or record.get("git_error") is not None
            or not isinstance(record.get("head"), str)
            or not record["head"]):
        return None
    manifest_repository = record.get("manifest_repository")
    if (manifest_repository is not None
            and manifest_repository != repository):
        return None
    return checkout


def _source_checkout(
    session: Any, repository: str, root: Path,
    workspace_checkouts: dict[str, Path | None],
    deadline: float,
) -> tuple[Path | None, str | None]:
    selected = _checkout(session, repository, root)
    if selected is not None:
        return selected, "selected-checkout"
    if repository not in workspace_checkouts:
        workspace_checkouts[repository] = _workspace_read_only_checkout(
            session, repository, root, deadline,
        )
    readable = workspace_checkouts[repository]
    if readable is not None:
        return readable, "workspace-read-only"
    return None, None


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


def _compact_measurement_summary(outcome: Any) -> dict[str, Any] | None:
    """Keep explicitly classified, bounded measures when raw outcome details are omitted."""
    if not isinstance(outcome, dict):
        return None
    raw = outcome.get("measurement_summary")
    if not isinstance(raw, dict):
        return None
    result: dict[str, Any] = {}
    for category in (
        "direct_measurements", "derived_measurements", "observed_counts",
    ):
        values = raw.get(category)
        if not isinstance(values, dict):
            continue
        selected = {}
        valid = []
        for key, value in values.items():
            if (isinstance(key, str) and key and len(key) <= 80
                    and ((type(value) is int and value >= 0)
                         or (category != "observed_counts"
                             and type(value) is float and math.isfinite(value)
                             and value >= 0))):
                valid.append((key, value))
        for key, value in sorted(valid)[:16]:
            selected[key] = value
        if selected:
            result[category] = selected
        if len(valid) != len(values) or len(valid) > len(selected):
            result[f"{category}_truncated"] = True
    availability = raw.get("measurement_availability")
    if isinstance(availability, dict):
        selected_availability = {
            key: _short_text(value, 64)
            for key, value in sorted(availability.items())[:16]
            if isinstance(key, str) and key and len(key) <= 80
            and isinstance(value, str) and value
        }
        if selected_availability:
            result["measurement_availability"] = selected_availability
        if len(availability) > len(selected_availability):
            result["measurement_availability_truncated"] = True
    for field in ("interpretations", "qualifications"):
        values = raw.get(field)
        if not isinstance(values, list):
            continue
        selected_values = [
            _short_text(value, 256)
            for value in values[:8]
            if isinstance(value, str) and value
        ]
        if selected_values:
            result[field] = selected_values
        if len(values) > len(selected_values):
            result[f"{field}_truncated"] = True
    return result or None


def _compact_outcome_context(outcome: Any) -> dict[str, Any] | None:
    """Retain bounded outcome meaning when compacting a large observation."""
    if not isinstance(outcome, dict):
        return None
    result: dict[str, Any] = {}
    text_fields = (
        "status", "result_status", "claim_boundary", "analysis_limit",
        "interpretation_limit", "implementation_change",
        "effectiveness_assessment", "recurrence_status",
        "verification_record_status", "subsequent_record_status",
        "projection_status", "observation_identity", "source_campaign_id",
        "source_session_id", "compiler_source_status",
        "source_registration_status", "registration_scope",
        "content_integrity_status", "source_content_integrity_status",
    )
    for key in text_fields:
        value = outcome.get(key)
        if isinstance(value, str) and value:
            result[key] = _short_text(value, 256 if key in {
                "claim_boundary", "analysis_limit", "interpretation_limit",
                "implementation_change",
            } else 128)

    compact_objects = {}
    section_hints = (
        "build", "diagnostic", "fact", "implementation", "measurement",
        "origin", "outcome", "resource", "reuse", "runtime", "source",
        "stage", "test", "verification",
    )
    field_hints = (
        "available", "byte", "cache", "case", "checkpoint", "code", "cpu",
        "count", "deselect", "diagnostic", "duration", "error", "evidence",
        "exit", "fact", "failed", "failure", "fresh", "implementation",
        "integrity", "limit", "mask", "memory", "mismatch", "observation",
        "origin", "passed", "peak", "publish", "record", "resource",
        "equal", "name", "output", "result", "return", "reuse", "rss",
        "skip", "source", "stage", "state", "status", "success", "test",
        "token", "usage", "verified", "wall",
    )
    for name, value in sorted(outcome.items()):
        if (not isinstance(name, str) or not name or len(name) > 80
                or name in text_fields
                or not any(hint in name.lower() for hint in section_hints)):
            continue
        if isinstance(value, dict):
            records = [value]
            record_array = False
        elif "stage" in name.lower() and isinstance(value, list):
            records = [item for item in value[:4] if isinstance(item, dict)]
            record_array = True
        else:
            continue
        compact_records = []
        for record in records:
            valid_fields = []
            for key, field_value in record.items():
                if (not isinstance(key, str) or not key or len(key) > 80
                        or not any(hint in key.lower() for hint in field_hints)):
                    continue
                if isinstance(field_value, str) and field_value:
                    valid_fields.append((key, _short_text(field_value, 128)))
                elif (type(field_value) is bool
                      or (type(field_value) is int and abs(field_value) <= 10**18)
                      or (type(field_value) is float and math.isfinite(field_value)
                          and abs(field_value) <= 10**18)
                      or field_value is None):
                    valid_fields.append((key, field_value))
            if record_array:
                stage_field_order = {
                    "name": 0,
                    "cache_status": 1,
                    "output_equal_to_stage0": 2,
                    "wall_seconds": 3,
                    "cpu_seconds": 4,
                    "peak_rss_bytes": 5,
                    "peak_cgroup_memory_bytes": 6,
                    "peak_open_file_descriptors": 7,
                }
                valid_fields.sort(key=lambda item: (
                    stage_field_order.get(item[0], len(stage_field_order)), item[0],
                ))
                scalar_limit = 8
            else:
                valid_fields.sort()
                scalar_limit = 6
            scalar_fields = dict(valid_fields[:scalar_limit])
            if scalar_fields:
                compact_records.append({
                    "values": scalar_fields,
                    "fields_truncated": len(valid_fields) > len(scalar_fields),
                })
        if not compact_records:
            continue
        if record_array:
            compact_objects[name] = {
                "records": compact_records,
                "records_truncated": len(value) > len(compact_records),
            }
        else:
            compact_objects[name] = compact_records[0]
    if compact_objects:
        result["sections"] = dict(sorted(compact_objects.items())[:4])
        if len(compact_objects) > len(result["sections"]):
            result["sections_truncated"] = True
    return result or None


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
    intent_repositories = intent.get("repositories")
    if isinstance(intent_repositories, list):
        compact_subject["work_intent"]["repositories"] = [
            _short_text(value, 128)
            for value in intent_repositories[:16]
            if isinstance(value, str) and value
        ]
        if len(intent_repositories) > 16:
            compact_subject["work_intent"]["repositories_truncated"] = True
    workspace_alignment = subject.get("workspace_alignment")
    if isinstance(workspace_alignment, dict):
        compact_alignment = {
            "status": _short_text(workspace_alignment.get("status"), 96),
            "basis": _short_text(workspace_alignment.get("basis"), 160),
        }
        for key in (
            "declared_repositories", "selected_repositories",
            "unselected_declared_repositories",
        ):
            value = workspace_alignment.get(key)
            if isinstance(value, list):
                compact_alignment[key] = [
                    _short_text(item, 128)
                    for item in value[:16]
                    if isinstance(item, str) and item
                ]
                if len(value) > 16:
                    compact_alignment[f"{key}_truncated"] = True
        compact_subject["workspace_alignment"] = compact_alignment
    charter = subject.get("campaign_charter")
    if isinstance(charter, dict):
        compact_charter = {
            "status": _short_text(charter.get("status"), 32),
        }
        for key in ("reason", "identity"):
            if isinstance(charter.get(key), str):
                compact_charter[key] = _short_text(charter[key], 160)
        for key in ("candidate_reference_count", "references_examined",
                    "valid_charter_count"):
            if type(charter.get(key)) is int and charter[key] >= 0:
                compact_charter[key] = charter[key]
        for key, limit in (("title", 256), ("objective", 512)):
            value = charter.get(key)
            if isinstance(value, str):
                compact_charter[key] = _short_text(value, limit)
                if len(value) > limit:
                    compact_charter[f"{key}_truncated"] = True
        pointer = _validate_source_pointer(charter.get("evidence_reference"))
        if pointer is not None:
            compact_charter["evidence_reference"] = pointer
        compact_subject["campaign_charter"] = compact_charter
    compact_source = {
        key: source.get(key)
        for key in ("campaign_charter_status", "campaign_charter_reference_count",
                    "evidence_count", "evidence_page_truncated",
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
                    "observation_records_unavailable",
                    "record_correction_count",
                    "record_corrections_applied_count",
                    "record_corrections_unverified_count",
                    "evidence_reference_correction_count",
                    "evidence_reference_corrections_applied_count",
                    "evidence_reference_corrections_unverified_count",
                    "evidence_reference_correction_target_reads")
        if key in source and (
            key in {"previous_evidence_offset", "next_evidence_offset"}
            or source.get(key) is not None
        )
    }
    evidence_visibility = source.get("evidence_visibility")
    if isinstance(evidence_visibility, dict):
        compact_visibility = {
            "status": _short_text(evidence_visibility.get("status"), 128),
            "verification_diagnostic_count": evidence_visibility.get(
                "verification_diagnostic_count"
            ),
            "outside_selected_reference_diagnostic_count": evidence_visibility.get(
                "outside_selected_reference_diagnostic_count", 0
            ),
            "diagnostics_truncated": evidence_visibility.get(
                "diagnostics_truncated"
            ),
            "basis": _short_text(evidence_visibility.get("basis"), 160),
            "outside_selected_repositories": [
                {
                    "repository": _short_text(item.get("repository"), 128),
                    "diagnostic_count": item.get("diagnostic_count", 0),
                }
                for item in evidence_visibility.get(
                    "outside_selected_repositories", []
                )[:16]
                if isinstance(item, dict)
                and isinstance(item.get("repository"), str)
            ],
        }
        compact_source["evidence_visibility"] = compact_visibility
    raw_diagnostics = source.get("observation_reference_diagnostics")
    raw_diagnostics = raw_diagnostics if isinstance(raw_diagnostics, list) else []
    for key in (
        "observation_reference_diagnostic_count",
        "improvement_relations_unverified_count",
        "record_correction_count", "record_corrections_applied_count",
        "record_corrections_unverified_count",
        "evidence_reference_correction_count",
        "evidence_reference_corrections_applied_count",
        "evidence_reference_corrections_unverified_count",
        "evidence_reference_correction_target_reads",
    ):
        if type(source.get(key)) is int and source[key] > 0:
            compact_source[key] = source[key]
    compact_diagnostics = []
    for diagnostic in raw_diagnostics[:4]:
        if not isinstance(diagnostic, dict):
            continue
        compact_diagnostics.append({
            key: _short_text(diagnostic.get(key), 192 if key == "path" else 160)
            for key in (
                "identity", "record_identity", "repository", "commit",
                "sha256", "verification", "path",
            )
            if isinstance(diagnostic.get(key), str)
        })
        if type(diagnostic.get("path_truncated")) is bool:
            compact_diagnostics[-1]["path_truncated"] = diagnostic["path_truncated"]
    if compact_diagnostics:
        compact_source["observation_reference_diagnostics"] = compact_diagnostics
        compact_source["observation_reference_diagnostics_truncated"] = (
            source.get("observation_reference_diagnostics_truncated", False)
            or len(raw_diagnostics) > len(compact_diagnostics)
        )
    for key in ("campaign_state_checkpoint_id", "recorded_at"):
        if source.get(key) is not None:
            compact_source[key] = _short_text(source[key], 160)
    compact_source["response_truncated"] = True
    compact_source["response_byte_count"] = original_size

    compact_context_candidates = []
    raw_context_candidates = projection.get("context_pressure_candidates")
    raw_context_candidates = (
        raw_context_candidates if isinstance(raw_context_candidates, list) else []
    )
    for candidate in raw_context_candidates[:8]:
        if not isinstance(candidate, dict):
            continue
        compact_candidate = {
            key: _short_text(
                candidate.get(key),
                256 if key in ("title", "recommendation") else 160,
            )
            for key in (
                "identity", "kind", "interpretation", "status", "title",
                "affected_repository", "recurrence_status", "recommendation",
            )
            if isinstance(candidate.get(key), str)
        }
        work_context = candidate.get("affected_work_context")
        if isinstance(work_context, dict):
            compact_candidate["affected_work_context"] = {
                key: _short_text(value, 160)
                for key, value in work_context.items()
                if isinstance(value, str)
            }
        basis = candidate.get("basis")
        if isinstance(basis, dict):
            compact_candidate["basis"] = {
                key: (
                    [_short_text(value, 128) for value in values[:MAX_OBSERVATIONS]
                     if isinstance(value, str)]
                    if isinstance(values, list)
                    else values
                )
                for key, values in basis.items()
                if isinstance(values, (str, int, bool, list))
            }
        references = candidate.get("evidence_references")
        references = references if isinstance(references, list) else []
        compact_references = []
        for reference in references[:MAX_OBSERVATIONS]:
            if not isinstance(reference, dict):
                continue
            compact_references.append({
                key: _short_text(reference[key], 192 if key == "path" else 160)
                for key in (
                    "identity", "repository", "commit", "path", "sha256",
                    "verification",
                )
                if isinstance(reference.get(key), str)
            })
        compact_candidate["evidence_references"] = compact_references
        compact_candidate["evidence_references_truncated"] = (
            candidate.get("evidence_references_truncated") is True
            or len(references) > len(compact_references)
        )
        not_measured = candidate.get("not_measured")
        if isinstance(not_measured, list):
            compact_candidate["not_measured"] = [
                _short_text(value, 192)
                for value in not_measured[:4]
                if isinstance(value, str)
            ]
        compact_context_candidates.append(compact_candidate)

    compact_hypotheses = []
    observations = projection["observations"]
    observation_order_by_identity: dict[str, tuple[int, int, int]] = {}
    evidence_pages = source.get("evidence_history_observation_budgets_by_page")
    evidence_pages = evidence_pages if isinstance(evidence_pages, list) else []
    for page_index, page in enumerate(evidence_pages):
        if not isinstance(page, dict):
            continue
        offset = page.get("offset")
        offset = offset if type(offset) is int and offset >= 0 else page_index
        identities = page.get("selected_observation_reference_identities")
        if not isinstance(identities, list):
            continue
        for position, identity in enumerate(identities):
            if isinstance(identity, str):
                observation_order_by_identity[identity] = (
                    offset, position, page_index,
                )

    def ordered_records(items, identity_for_item):
        return [
            item for _position, item in sorted(
                enumerate(items),
                key=lambda positioned: (
                    observation_order_by_identity.get(
                        identity_for_item(positioned[1]),
                        (0, positioned[0], 0),
                    ),
                    positioned[0],
                ),
            )
        ]

    observations = ordered_records(observations, lambda item: item.get("identity"))
    for hypothesis in projection["pressure_hypotheses"][:8]:
        summary = {
            key: _short_text(hypothesis.get(key), 256 if key in ("title", "recommendation") else 160)
            for key in ("identity", "title", "category", "subsystem", "owner",
                        "interpretation", "recurrence_status", "recommendation")
            if hypothesis.get(key) is not None
        }
        for key in ("definition_status", "definition_conflict_fields"):
            if hypothesis.get(key) is not None:
                summary[key] = hypothesis[key]
        definition_observations = hypothesis.get(
            "definition_evidence_observation_identities",
        )
        if isinstance(definition_observations, list):
            summary["definition_evidence_observation_identities"] = [
                _short_text(identity, 160)
                for identity in definition_observations[:MAX_OBSERVATIONS]
                if isinstance(identity, str)
            ]
            summary["definition_evidence_observation_identities_truncated"] = (
                len(definition_observations) > MAX_OBSERVATIONS
            )
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
        ][-8:]
        compact_hypotheses.append(summary)

    compact_observations = []
    for item in observations[-8:]:
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
        pointer_correction = item.get("evidence_reference_correction")
        if isinstance(pointer_correction, dict):
            compact_pointer_correction = {
                key: _short_text(pointer_correction.get(key), 160)
                for key in (
                    "status", "correction_observation_identity",
                    "target_identity", "reason", "replacement_verification",
                )
                if isinstance(pointer_correction.get(key), str)
            }
            for key in (
                "target_reference", "replacement_reference", "recorded_reference",
            ):
                pointer = _validate_source_pointer(pointer_correction.get(key))
                if pointer is not None:
                    compact_pointer_correction[key] = pointer
            compact_observation["evidence_reference_correction"] = (
                compact_pointer_correction
            )
        measurement_summary = _compact_measurement_summary(observation.get("outcome"))
        if measurement_summary is not None:
            compact_observation["observation"]["measurement_summary"] = (
                measurement_summary
            )
        outcome_context = _compact_outcome_context(observation.get("outcome"))
        if outcome_context is not None:
            compact_observation["observation"]["outcome_context"] = (
                outcome_context
            )
        if isinstance(observation.get("source_session_context"), dict):
            source_context = observation["source_session_context"]
            compact_observation["observation"]["source_session_context"] = {
                key: _short_text(
                    source_context.get(key),
                    256 if key == "work_intent_goal" else 160,
                )
                for key in (
                    "campaign_id", "session_id", "work_intent_id", "work_intent_goal",
                    "environment_id", "repository", "repository_branch", "repository_head",
                    "repository_context_basis",
                )
                if isinstance(source_context.get(key), str)
            }
            compact_observation["observation"]["source_session_context_basis"] = (
                _short_text(
                    observation.get(
                        "source_session_context_basis", "recorded-observation",
                    ),
                    64,
                )
            )
            compact_observation["observation"]["source_session_context_truncated"] = (
                observation.get("source_session_context_truncated", False)
            )
        source_context_resolution = observation.get(
            "source_session_context_resolution",
        )
        if isinstance(source_context_resolution, str):
            compact_observation["observation"][
                "source_session_context_resolution"
            ] = _short_text(source_context_resolution, 96)
        projection_summary = observation.get("projection_summary")
        if isinstance(projection_summary, dict):
            summary_fields = (
                "status", "session_activity_status", "activity_interpretation",
                "rpc_operation_event_count", "rpc_operation_records_returned",
                "rpc_operation_capture_count", "rpc_failure_candidate_count",
            )
            compact_observation["observation"]["projection_summary"] = {
                key: _short_text(value, 192)
                if isinstance(value, str)
                else value
                for key in summary_fields
                if (isinstance((value := projection_summary.get(key)), str) and value)
                or (type(value) is int and value >= 0)
            }
        availability = observation.get("measurement_availability")
        if isinstance(availability, dict):
            compact_observation["observation"]["measurement_availability"] = {
                key: _short_text(value, 64)
                for key, value in availability.items()
                if key in {
                    "model_identity", "token_usage", "control_dispatch_elapsed_ms",
                    "resource_consumption", "cache_accounting",
                    "repository_head_live_probe",
                }
                and isinstance(value, str)
            }
        raw_operations = observation.get("operations")
        if isinstance(raw_operations, list):
            compact_operations = []
            for operation in raw_operations[:4]:
                if not isinstance(operation, dict):
                    continue
                compact_operation = {
                    key: _short_text(operation[key], 128)
                    for key in (
                        "event_identity", "observed_at", "operation", "outcome_status",
                    )
                    if isinstance(operation.get(key), str)
                }
                for key in ("event_sequence", "exit_code"):
                    if type(operation.get(key)) is int:
                        compact_operation[key] = operation[key]
                elapsed = operation.get("control_dispatch_elapsed_ms")
                if type(elapsed) in {int, float} and 0 <= elapsed <= 600_000:
                    compact_operation["control_dispatch_elapsed_ms"] = elapsed
                if compact_operation:
                    compact_operations.append(compact_operation)
            compact_observation["observation"]["operations"] = compact_operations
            compact_observation["observation"]["operations_truncated"] = (
                observation.get("operations_truncated", False)
                or len(raw_operations) > len(compact_operations)
            )
        if isinstance(item.get("improvement_relation_status"), str):
            compact_observation["improvement_relation_status"] = _short_text(
                item["improvement_relation_status"], 96,
            )
        record_correction = item.get("record_correction")
        if isinstance(record_correction, dict):
            compact_correction = {
                key: _short_text(record_correction.get(key), 160)
                for key in (
                    "status", "correction_observation_identity", "target_identity",
                    "reason",
                )
                if isinstance(record_correction.get(key), str)
            }
            fields = record_correction.get("corrected_field_paths")
            if isinstance(fields, list):
                compact_correction["corrected_field_paths"] = [
                    _short_text(field, 160)
                    for field in fields[:8]
                    if isinstance(field, str)
                ]
                compact_correction["corrected_field_paths_truncated"] = (
                    len(fields) > 8
                )
            for source_key, output_key in (
                ("target_reference", "target_reference"),
                ("correction_evidence_reference", "correction_evidence_reference"),
            ):
                pointer = _validate_source_pointer(record_correction.get(source_key))
                if pointer is not None:
                    compact_correction[output_key] = pointer
            replacement_values = record_correction.get("replacement_values")
            if isinstance(replacement_values, dict):
                compact_replacements = {}
                replacement_sources = replacement_values.get("supporting_sources")
                if isinstance(replacement_sources, list):
                    compact_replacements["supporting_sources"] = [
                        pointer for source in replacement_sources[:16]
                        if (pointer := _validate_source_pointer(source)) is not None
                    ]
                replacement_limits = replacement_values.get("not_measured")
                if isinstance(replacement_limits, list):
                    compact_replacements["not_measured"] = [
                        _short_text(value, 512)
                        for value in replacement_limits[:16]
                        if isinstance(value, str)
                    ]
                compact_correction["replacement_values"] = compact_replacements
            compact_observation["record_correction"] = compact_correction
        compact_sources = []
        raw_sources = item.get("supporting_sources")
        raw_sources = raw_sources if isinstance(raw_sources, list) else []
        for source in raw_sources[:4]:
            if not isinstance(source, dict):
                continue
            compact_supporting_source = {
                key: _short_text(source.get(key), 128)
                for key in ("kind", "campaign_evidence_identity", "campaign_id",
                            "session_id", "status", "registration_scope",
                            "content_integrity_status", "event_reference_status")
                if isinstance(source.get(key), str)
            }
            for key in (
                "event_reference_count", "event_reference_matched_count",
            ):
                if type(source.get(key)) is int and source[key] >= 0:
                    compact_supporting_source[key] = source[key]
            event_identities = source.get("event_identities")
            if isinstance(event_identities, list):
                compact_supporting_source["event_identities"] = [
                    _short_text(identity, 160)
                    for identity in event_identities[:8]
                    if isinstance(identity, str)
                ]
                compact_supporting_source["event_identities_truncated"] = (
                    len(event_identities) > 8
                )
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
    improvement_relations = ordered_records(
        projection["improvement_relations"],
        lambda item: item.get("recorded_by_observation_identity"),
    )
    for relation in improvement_relations[-8:]:
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
            "event_page_before_sequence", "next_event_before_sequence",
            "event_window_sequence_start", "event_window_sequence_end",
            "invocation_events_considered", "invocation_records_omitted",
            "resource_sample_identity_digest",
        )
        if key in activity_source
        and (key not in {"event_page_before_sequence", "next_event_before_sequence"}
             or activity_source[key] is not None)
    }
    if activity_source.get("rpc_operation_capture_count") is not None:
        compact_activity_source.update({
            key: activity_source[key]
            for key in (
                "rpc_operation_capture_count",
                "rpc_operation_capture_limit",
                "rpc_operation_capture_limit_reached",
            )
            if key in activity_source
        })
    if activity_source.get("rpc_operation_events_considered", 0):
        compact_activity_source.update({
            key: activity_source[key]
            for key in (
                "rpc_operation_events_considered",
                "rpc_operation_records_returned",
                "rpc_operation_records_omitted",
                "rpc_operation_records_invalid",
            )
            if key in activity_source
        })
    rpc_context_basis = activity_source.get("rpc_operation_context_basis")
    if isinstance(rpc_context_basis, dict):
        compact_activity_source["rpc_operation_context_basis"] = (
            "grant+session-snapshot; branch/head not live-probed"
        )
    if activity_source.get("authority_denial_events_considered", 0):
        compact_activity_source.update({
            key: activity_source[key]
            for key in (
                "authority_denial_events_considered",
                "authority_denial_records_returned",
                "authority_denial_records_omitted",
                "authority_denial_records_invalid",
            )
            if key in activity_source
        })
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
    compact_rpc_operations = []
    raw_rpc_operations = activity.get("rpc_operations")
    raw_rpc_operations = raw_rpc_operations if isinstance(raw_rpc_operations, list) else []
    for item in raw_rpc_operations[:8]:
        if not isinstance(item, dict):
            continue
        compact_item = {
            key: _short_text(item.get(key), 160)
            for key in (
                "identity", "operation", "outcome_status", "failure_code",
            )
            if isinstance(item.get(key), str)
        }
        for key in ("exit_code", "observation_ordinal"):
            if type(item.get(key)) is int:
                compact_item[key] = item[key]
        for key in ("event_reference",):
            reference = item.get(key)
            if isinstance(reference, dict):
                compact_item[key] = {
                    item_key: reference[item_key]
                    for item_key in ("identity", "sequence", "type")
                    if item_key in reference
                }
        for key in ("caller", "context"):
            values = item.get(key)
            if isinstance(values, dict):
                compact_item[key] = {
                    subkey: _short_text(value, 160)
                    for subkey, value in values.items()
                    if isinstance(subkey, str) and isinstance(value, str)
                }
        measurements = item.get("measurement_availability")
        if isinstance(measurements, dict):
            compact_item["measurement_availability"] = dict(measurements)
        measured_values = item.get("measurements")
        if isinstance(measured_values, dict):
            compact_item["measurements"] = dict(measured_values)
        compact_rpc_operations.append(compact_item)
    raw_rpc_summary = activity.get("rpc_operation_summary")
    raw_rpc_summary = raw_rpc_summary if isinstance(raw_rpc_summary, dict) else {}
    raw_rpc_summary_groups = raw_rpc_summary.get("groups")
    raw_rpc_summary_groups = (
        raw_rpc_summary_groups if isinstance(raw_rpc_summary_groups, list) else []
    )
    compact_rpc_summary_groups = []
    for group in raw_rpc_summary_groups[:16]:
        if not isinstance(group, dict):
            continue
        compact_group = {
            key: _short_text(group.get(key), 128)
            for key in ("operation", "outcome_status")
            if isinstance(group.get(key), str)
        }
        if type(group.get("occurrence_count")) is int:
            compact_group["occurrence_count"] = group["occurrence_count"]
        references = group.get("supporting_events")
        if isinstance(references, list):
            compact_group["supporting_events"] = [
                {
                    key: reference[key]
                    for key in ("identity", "sequence", "type")
                    if key in reference
                }
                for reference in references[:2]
                if isinstance(reference, dict)
            ]
        compact_rpc_summary_groups.append(compact_group)
    compact_rpc_summary: dict[str, Any] = {}
    if raw_rpc_summary:
        raw_summary_omitted_groups = raw_rpc_summary.get("groups_omitted", 0)
        raw_summary_omitted_records = raw_rpc_summary.get("omitted_record_count", 0)
        raw_reference_limit = raw_rpc_summary.get(
            "supporting_event_limit_per_group", 2,
        )
        if type(raw_summary_omitted_groups) is not int:
            raw_summary_omitted_groups = 0
        if type(raw_summary_omitted_records) is not int:
            raw_summary_omitted_records = 0
        if type(raw_reference_limit) is not int:
            raw_reference_limit = 2
        compact_rpc_summary = {
            "scope": _short_text(raw_rpc_summary.get("scope"), 128),
            "record_count": raw_rpc_summary.get("record_count", 0),
            "group_count": raw_rpc_summary.get("group_count", 0),
            "groups_omitted": raw_summary_omitted_groups + max(
                0, len(raw_rpc_summary_groups) - len(compact_rpc_summary_groups),
            ),
            "omitted_record_count": raw_summary_omitted_records + sum(
                group.get("occurrence_count", 0)
                for group in raw_rpc_summary_groups[
                    len(compact_rpc_summary_groups):
                ]
                if isinstance(group, dict)
                and type(group.get("occurrence_count")) is int
            ),
            "supporting_event_limit_per_group": min(
                2, raw_reference_limit,
            ),
            "groups": compact_rpc_summary_groups,
        }
    compact_rpc_pressure = []
    raw_rpc_pressure = activity.get("rpc_failure_candidates")
    raw_rpc_pressure = raw_rpc_pressure if isinstance(raw_rpc_pressure, list) else []
    for item in raw_rpc_pressure[:4]:
        if not isinstance(item, dict):
            continue
        compact_item = {
            key: _short_text(item.get(key), 160)
            for key in (
                "identity", "status", "subsystem", "operation",
                "failure_code", "causal_claim", "recurrence_scope",
            )
            if isinstance(item.get(key), str)
        }
        for key in ("occurrence_count", "distinct_session_count"):
            if type(item.get(key)) is int:
                compact_item[key] = item[key]
        if isinstance(item.get("interpretation"), str):
            compact_item["interpretation"] = _short_text(
                item["interpretation"], 256,
            )
        references = item.get("supporting_events")
        if isinstance(references, list):
            compact_item["supporting_events"] = [
                {
                    key: reference[key]
                    for key in ("identity", "sequence", "type")
                    if key in reference
                }
                for reference in references[:4]
                if isinstance(reference, dict)
            ]
        compact_rpc_pressure.append(compact_item)
    compact_authority_denials = []
    raw_authority_denials = activity.get("authority_denials")
    raw_authority_denials = (
        raw_authority_denials if isinstance(raw_authority_denials, list) else []
    )
    for item in raw_authority_denials[:8]:
        if not isinstance(item, dict):
            continue
        compact_item = {
            key: _short_text(item.get(key), 160)
            for key in (
                "identity", "action", "outcome_status", "reason_class", "repository",
            )
            if isinstance(item.get(key), str)
        }
        reference = item.get("event_reference")
        if isinstance(reference, dict):
            compact_item["event_reference"] = {
                key: reference[key]
                for key in ("identity", "sequence", "type")
                if key in reference
            }
        compact_authority_denials.append(compact_item)
    compact_authority_candidates = []
    raw_authority_candidates = activity.get("authority_denial_candidates")
    raw_authority_candidates = (
        raw_authority_candidates if isinstance(raw_authority_candidates, list) else []
    )
    for item in raw_authority_candidates[:4]:
        if not isinstance(item, dict):
            continue
        compact_item = {
            key: _short_text(item.get(key), 160)
            for key in (
                "identity", "status", "subsystem", "action", "reason_class",
                "repository", "recurrence_scope", "causal_claim",
            )
            if isinstance(item.get(key), str)
        }
        for key in ("occurrence_count", "distinct_session_count"):
            if type(item.get(key)) is int:
                compact_item[key] = item[key]
        if isinstance(item.get("interpretation"), str):
            compact_item["interpretation"] = _short_text(
                item["interpretation"], 256,
            )
        references = item.get("supporting_events")
        if isinstance(references, list):
            compact_item["supporting_events"] = [
                {
                    key: reference[key]
                    for key in ("identity", "sequence", "type")
                    if key in reference
                }
                for reference in references[:4]
                if isinstance(reference, dict)
            ]
        compact_authority_candidates.append(compact_item)
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
    if (activity_source.get("rpc_operation_capture_count") is not None
            or raw_rpc_operations or raw_rpc_summary):
        compact_activity["rpc_operation_count"] = activity.get(
            "rpc_operation_count", 0,
        )
        compact_activity["rpc_operations"] = compact_rpc_operations
        if len(raw_rpc_operations) > len(compact_rpc_operations):
            compact_activity["rpc_operations_truncated"] = True
        if compact_rpc_summary:
            compact_activity["rpc_operation_summary"] = compact_rpc_summary
            if compact_rpc_summary["groups_omitted"]:
                compact_activity["rpc_operation_summary"][
                    "groups_truncated"
                ] = True
    if raw_rpc_pressure:
        compact_activity["rpc_failure_candidates"] = compact_rpc_pressure
        if len(raw_rpc_pressure) > len(compact_rpc_pressure):
            compact_activity["rpc_failure_candidates_truncated"] = True
    if raw_authority_denials:
        compact_activity["authority_denial_count"] = activity.get(
            "authority_denial_count", len(raw_authority_denials),
        )
        compact_activity["authority_denials"] = compact_authority_denials
        if len(raw_authority_denials) > len(compact_authority_denials):
            compact_activity["authority_denials_truncated"] = True
    if raw_authority_candidates:
        compact_activity["authority_denial_candidates"] = compact_authority_candidates
        if len(raw_authority_candidates) > len(compact_authority_candidates):
            compact_activity["authority_denial_candidates_truncated"] = True

    resource_samples = activity.get("resource_samples")
    resource_samples = resource_samples if isinstance(resource_samples, dict) else {}
    resource_source = resource_samples.get("source")
    resource_source = resource_source if isinstance(resource_source, dict) else {}
    raw_resource_observations = resource_samples.get("observations")
    raw_resource_observations = (
        raw_resource_observations
        if isinstance(raw_resource_observations, list) else []
    )
    compact_resource_observations = []
    for item in raw_resource_observations[:MAX_SESSION_RESOURCE_SAMPLES]:
        if not isinstance(item, dict):
            continue
        compact_item = {
            key: _short_text(item.get(key), 160)
            for key in ("identity", "status")
            if isinstance(item.get(key), str)
        }
        item_context = item.get("context")
        if isinstance(item_context, dict):
            compact_item["context"] = {
                key: _short_text(item_context[key], 160)
                for key in (
                    "session_id", "campaign_id", "work_intent_id",
                    "environment_id", "definition_id",
                )
                if isinstance(item_context.get(key), str)
            }
        item_source = item.get("source")
        if isinstance(item_source, dict):
            compact_item_source = {
                key: _short_text(item_source[key], 160)
                for key in (
                    "capability", "binding_id", "provider_request_id",
                    "event_correlation_status", "invocation_recorded_at",
                    "provider", "contract_revision",
                )
                if isinstance(item_source.get(key), str)
            }
            for key in ("invocation_event", "completion_event"):
                reference = item_source.get(key)
                if isinstance(reference, dict):
                    compact_item_source[key] = {
                        field: reference[field]
                        for field in ("identity", "sequence", "type", "observed_at")
                        if field in reference
                    }
            artifact_reference = item_source.get("artifact_reference")
            if isinstance(artifact_reference, dict):
                compact_item_source["artifact_reference"] = {
                    "store_reference": _short_text(
                        artifact_reference.get("store_reference"), 512,
                    ),
                    "path": _short_text(artifact_reference.get("path"), 160),
                    "byte_count": artifact_reference.get("byte_count"),
                    "sha256": _short_text(artifact_reference.get("sha256"), 64),
                }
            compact_item["source"] = compact_item_source
        sample = item.get("sample")
        if isinstance(sample, dict):
            compact_sample: dict[str, Any] = {
                "schema": _short_text(sample.get("schema"), 96),
            }
            if sample.get("measurement_basis") == (
                "system-monitor-reported-resource-sample"
            ):
                compact_sample["measurement_basis"] = sample["measurement_basis"]
            availability = sample.get("measurement_availability")
            if isinstance(availability, dict):
                compact_sample["measurement_availability"] = {
                    key: value
                    for key, value in availability.items()
                    if key in {
                        "host.cpu_count", "host.cpu_utilization",
                        "host.load_average", "host.memory_total_bytes",
                        "host.memory_available_bytes",
                    }
                    and value in {"measured", "derived", "unavailable"}
                }
            if type(sample.get("observed_at")) is int:
                compact_sample["observed_at"] = sample["observed_at"]
            sample_time = sample.get("sample_time")
            if isinstance(sample_time, dict):
                compact_sample["sample_time"] = {
                    key: sample_time[key]
                    for key in ("wall_time_unix_ms", "monotonic_ns")
                    if key in sample_time
                    and (type(sample_time[key]) is int or sample_time[key] is None)
                }
            host = sample.get("host")
            if isinstance(host, dict):
                compact_host = {}
                for key in (
                    "cpu_count", "memory_total_bytes", "memory_available_bytes",
                ):
                    if type(host.get(key)) is int or host.get(key) is None:
                        if key in host:
                            compact_host[key] = host[key]
                for key in ("cpu_utilization",):
                    if type(host.get(key)) in (int, float) or host.get(key) is None:
                        if key in host:
                            compact_host[key] = host[key]
                load_average = host.get("load_average")
                if isinstance(load_average, (list, dict)) or load_average is None:
                    if "load_average" in host:
                        compact_host["load_average"] = load_average
                for key in ("cpu_status", "memory_available_status"):
                    if isinstance(host.get(key), str):
                        compact_host[key] = _short_text(host[key], 64)
                compact_sample["host"] = compact_host
            compact_item["sample"] = compact_sample
        compact_resource_observations.append(compact_item)
    compact_resource_samples = {
        "status": _short_text(resource_samples.get("status"), 96),
        "observation_count": resource_samples.get("observation_count", 0),
        "records_examined": resource_samples.get("records_examined", 0),
        "source": {
            key: resource_source[key]
            for key in (
                "artifact_window_count", "artifact_window_limit",
                "artifact_window_truncated", "selection_policy",
                "records_omitted", "observation_identity_digest",
            )
            if key in resource_source
        },
        "observations": compact_resource_observations,
    }
    if len(raw_resource_observations) > len(compact_resource_observations):
        compact_resource_samples["observations_truncated"] = True
    if (raw_resource_observations
            or resource_source.get("artifact_window_truncated") is True
            or resource_source.get("records_omitted", 0) > 0):
        compact_activity["resource_samples"] = compact_resource_samples

    result = {
        "schema_version": SCHEMA,
        "identity": projection["identity"],
        "status": "partial",
        "subject": compact_subject,
        "source": compact_source,
        "correlation": projection["correlation"],
        "observation_count": projection["observation_count"],
        "pressure_hypothesis_count": projection["pressure_hypothesis_count"],
        "session_activity_pressure_candidate_count": projection.get(
            "session_activity_pressure_candidate_count", 0,
        ),
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
    if compact_context_candidates:
        result["context_pressure_candidate_count"] = projection.get(
            "context_pressure_candidate_count", len(compact_context_candidates),
        )
        result["context_pressure_candidates"] = compact_context_candidates
    # The selected fields are individually bounded. This final reduction also
    # accounts for unusual but valid identifiers and reference paths.
    while len(json.dumps(result, ensure_ascii=False, sort_keys=True).encode("utf-8")) > max_result_bytes:
        if result["observations"]:
            result["observations"].pop(0)
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
            result["improvement_relations"].pop(0)
        elif result.get("context_pressure_candidates"):
            result["context_pressure_candidates"].pop(0)
        elif result["session_activity"]["interactions"]:
            result["session_activity"]["interactions"].pop()
            result["session_activity"]["interactions_truncated"] = True
        elif result["session_activity"].get("rpc_operations"):
            result["session_activity"]["rpc_operations"].pop()
            result["session_activity"]["rpc_operations_truncated"] = True
        elif result["session_activity"].get("rpc_operation_summary", {}).get("groups"):
            summary = result["session_activity"]["rpc_operation_summary"]
            removed_group = summary["groups"].pop()
            summary["groups_omitted"] += 1
            summary["omitted_record_count"] += removed_group.get(
                "occurrence_count", 0,
            )
            summary["groups_truncated"] = True
        elif result["session_activity"].get("rpc_failure_candidates"):
            result["session_activity"]["rpc_failure_candidates"].pop()
            result["session_activity"]["rpc_failure_candidates_truncated"] = True
        elif result["session_activity"].get("authority_denial_candidates"):
            result["session_activity"]["authority_denial_candidates"].pop()
            result["session_activity"]["authority_denial_candidates_truncated"] = True
        elif result["session_activity"].get("authority_denials"):
            result["session_activity"]["authority_denials"].pop()
            result["session_activity"]["authority_denials_truncated"] = True
        elif result["session_activity"].get("resource_samples", {}).get("observations"):
            result["session_activity"]["resource_samples"]["observations"].pop()
            result["session_activity"]["resource_samples"]["observations_truncated"] = True
        else:
            break
    projection_source = projection.get("source")
    projection_source = (
        projection_source if isinstance(projection_source, dict) else {}
    )
    verified_observation_count = projection_source.get(
        "observation_records_verified",
    )
    if (type(verified_observation_count) is int
            and verified_observation_count > len(result["observations"])):
        result["source"]["observation_records_compacted_out_count"] = (
            verified_observation_count - len(result["observations"])
        )
    return result


def _compiler_regression_reference_family(
    reference: dict[str, str],
) -> tuple[str, str, tuple[str, ...]] | None:
    if reference["repository"] != "mncs-compiler":
        return None
    for family, identity_prefix, path_prefix, schema, stage_names in (
        COMPILER_CACHE_REGRESSION_REFERENCE_FAMILIES
    ):
        if (reference["identity"].startswith(identity_prefix)
                and reference["path"].startswith(path_prefix)
                and reference["path"].endswith(".json")):
            return family, schema, stage_names
    return None


def _compiler_regression_measurement_summary(
    source: dict[str, Any],
) -> dict[str, Any] | None:
    """Expose bounded verification-process measurements without agent attribution."""
    verification = source.get("verification")
    if not isinstance(verification, dict):
        return None

    direct: dict[str, int | float] = {}
    observed_counts: dict[str, int] = {}
    availability: dict[str, str] = {}
    qualifications: list[str] = []
    measurement_blocks = (
        ("harness_process_measurement", "harness_process"),
        ("stage0_bootstrap_measurement", "stage0_bootstrap"),
    )
    numeric_fields = (
        "wall_seconds", "cpu_seconds", "user_seconds", "system_seconds",
        "cpu_percent", "peak_rss_kib", "peak_rss_bytes",
    )
    for source_name, measurement_name in measurement_blocks:
        block = verification.get(source_name)
        if not isinstance(block, dict):
            continue
        for field in numeric_fields:
            value = block.get(field)
            if (type(value) is int and 0 <= value <= 10**18
                    or type(value) is float and math.isfinite(value)
                    and 0 <= value <= 10**18):
                direct[f"{measurement_name}_{field}"] = value
        jobs = block.get("cargo_build_jobs")
        if type(jobs) is int and 0 <= jobs <= 10**6:
            observed_counts[f"{measurement_name}_cargo_build_jobs"] = jobs
        if "peak_cgroup_memory_bytes" in block:
            cgroup_peak = block.get("peak_cgroup_memory_bytes")
            if type(cgroup_peak) is int and 0 <= cgroup_peak <= 10**18:
                direct[f"{measurement_name}_peak_cgroup_memory_bytes"] = cgroup_peak
            elif cgroup_peak is None:
                availability[
                    f"{measurement_name}_peak_cgroup_memory_bytes"
                ] = "null-in-source-record"
        note = block.get("note")
        if isinstance(note, str) and note:
            qualifications.append(
                f"{measurement_name}: {_short_text(note, 256)}"
            )

    measurement_notes = source.get("measurement_notes")
    if isinstance(measurement_notes, str) and measurement_notes:
        qualifications.append(_short_text(measurement_notes, 384) or "")
    if direct or observed_counts or availability:
        qualifications.append(
            "These verification-process measurements are not measurements of "
            "resource use attributable to the Compiler agent session."
        )
    if not (direct or observed_counts or availability):
        return None

    summary: dict[str, Any] = {}
    if direct:
        summary["direct_measurements"] = direct
    if observed_counts:
        summary["observed_counts"] = observed_counts
    if availability:
        summary["measurement_availability"] = availability
    if qualifications:
        summary["qualifications"] = qualifications[:8]
    return summary


def _is_compiler_regression_reference(reference: dict[str, str]) -> bool:
    return _compiler_regression_reference_family(reference) is not None


def _is_compiler_dependency_summary_reference(reference: dict[str, str]) -> bool:
    return (
        reference["repository"] == "mncs-compiler"
        and reference["identity"].startswith(
            COMPILER_LANGUAGE_DEPENDENCY_SUMMARY_IDENTITY_PREFIX,
        )
        and reference["path"].startswith(
            COMPILER_LANGUAGE_DEPENDENCY_SUMMARY_PATH_PREFIX,
        )
        and reference["path"].endswith(".json")
    )


def _is_observation_reference(reference: dict[str, str]) -> bool:
    if (reference["identity"].startswith("ev_environment_")
            and reference["path"].startswith(OBSERVATION_PATH_PREFIX)):
        return True
    return (
        _is_compiler_regression_reference(reference)
        or _is_compiler_dependency_summary_reference(reference)
    )


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


def _compiler_regression_observation(
    session: Any, reference: dict[str, str], source: dict[str, Any],
) -> dict[str, Any] | None:
    """Adapt one established Compiler regression report into bounded ECI context."""
    schema_version = source.get("schema_version")
    family = _compiler_regression_reference_family(reference)
    if family is None:
        return None
    family_name, family_schema, stage_names = family
    if schema_version != family_schema:
        return None
    invalidation = source.get("language_effect_and_capability_dependencies_invalidated")
    source_module_count = source.get("source_module_count")
    stages = source.get("stages")
    if ((invalidation is not None and type(invalidation) is not bool)
            or (family_name == "effect-capability"
                and type(invalidation) is not bool)
            or type(source_module_count) is not int
            or source_module_count < 1 or not isinstance(stages, dict)):
        return None

    stage_summaries: list[dict[str, Any]] = []
    for name in stage_names:
        stage = stages.get(name)
        if not isinstance(stage, dict):
            continue
        summary: dict[str, Any] = {"name": name}
        cache_status = stage.get("cache_status")
        if isinstance(cache_status, str) and cache_status:
            summary["cache_status"] = cache_status[:64]
        execution = stage.get("execution")
        execution = execution if isinstance(execution, dict) else {}
        output_equal = execution.get("output_equal_to_stage0")
        if type(output_equal) is bool:
            summary["output_equal_to_stage0"] = output_equal
        semantic = stage.get("semantic_work")
        if isinstance(semantic, dict):
            semantic_summary = {
                key: semantic[key]
                for key in (
                    "front_end_invocations", "recomputed_fact_count",
                    "reused_fact_count",
                )
                if type(semantic.get(key)) is int and semantic[key] >= 0
            }
            if semantic_summary:
                summary["semantic_work"] = semantic_summary
        metrics = stage.get("build_metrics")
        if isinstance(metrics, dict):
            metric_summary: dict[str, int | float] = {}
            for key in ("wall_seconds", "cpu_seconds"):
                value = metrics.get(key)
                if (type(value) in (int, float) and math.isfinite(value)
                        and value >= 0):
                    metric_summary[key] = value
            for key in (
                "peak_rss_bytes", "peak_open_file_descriptors", "peak_threads",
            ):
                value = metrics.get(key)
                if type(value) is int and value >= 0:
                    metric_summary[key] = value
            if metric_summary:
                summary["build_metrics"] = metric_summary
        if len(summary) > 1:
            stage_summaries.append(summary)
    if not stage_summaries:
        return None

    snapshot = session.snapshot
    campaign = snapshot.get("campaign")
    campaign = campaign if isinstance(campaign, dict) else {}
    intent = snapshot.get("intent")
    intent = intent if isinstance(intent, dict) else {}
    campaign_identity = campaign.get("identity")
    session_identity = getattr(session, "session_id", None)
    if (not isinstance(campaign_identity, str) or not campaign_identity
            or not isinstance(session_identity, str) or not session_identity):
        return None
    related_work: dict[str, str] = {
        "campaign_id": campaign_identity,
        "session_id": session_identity,
    }
    for key, value in (
        ("work_intent_id", intent.get("identity")),
        ("work_intent_goal", intent.get("goal")),
        ("environment_id", snapshot.get("environment_id")),
        ("definition_id", (snapshot.get("provenance") or {}).get("definition_id")
         if isinstance(snapshot.get("provenance"), dict) else None),
    ):
        if isinstance(value, str) and value:
            related_work[key] = value

    outcome: dict[str, Any] = {
        "stage_summaries": stage_summaries,
        "resource_measurement_scope": (
            "per-build and source-reported verification-process metrics; "
            "not Compiler agent session use"
        ),
        "duration_measurement_scope": (
            "individual build requests, not campaign or agent duration"
        ),
        "source_capture_time": "unavailable",
    }
    resource_measurements = _compiler_regression_measurement_summary(source)
    if resource_measurements is not None:
        outcome["measurement_summary"] = resource_measurements
    if type(invalidation) is bool:
        outcome["language_effect_and_capability_dependencies_invalidated"] = invalidation
    else:
        outcome["effect_capability_invalidation_measurement"] = (
            "not-recorded-in-source-schema/1"
        )
    modules_reused = source.get("actual_language_modules_reused")
    if type(modules_reused) is bool:
        outcome["actual_language_modules_reused"] = modules_reused

    return {
        "schema_version": OBSERVATION_SCHEMA,
        "identity": reference["identity"],
        "captured_at": "unavailable-in-source-record",
        "measurement_basis": (
            "bounded-derived-view-of-verified-compiler-regression/2"
        ),
        "observation": {
            "kind": f"compiler-{family_name}-cache-regression",
            "source_interface": "MNCS Compiler campaign evidence projection",
            "request": {
                "source_schema_version": schema_version,
                "compiler_head": source.get("compiler_head"),
                "language_head": source.get("language_head"),
                "source_module_count": source_module_count,
                "stage_names": [item["name"] for item in stage_summaries],
            },
            "outcome": outcome,
            "source_session_context_resolution": "awaiting-verified-supporting-observation",
            "measurement_availability": {
                "model_identity": "not-recorded-in-source",
                "token_usage": "not-recorded-in-source",
                "control_dispatch_elapsed_ms": "not-recorded-in-source",
                "resource_consumption": (
                    "per-build + verification-process metrics; "
                    "not agent-attributed"
                ),
                "cache_accounting": "status-and-fact-counts",
            },
        },
        "related_work": related_work,
        "not_measured": [
            "source capture timestamp",
            "model identity and token usage",
            "total compiler campaign duration",
            "resource consumption attributable to the Compiler agent session",
            "resource use outside the selected regression build requests",
            "whether the observed cache behavior recurs in other campaigns",
        ],
    }


def _compiler_dependency_summary_stage(
    name: str, stage: Any,
) -> dict[str, Any] | None:
    if not isinstance(stage, dict):
        return None
    summary: dict[str, Any] = {"name": name}
    cache_status = stage.get("cache_status")
    if isinstance(cache_status, str) and cache_status:
        summary["cache_status"] = cache_status[:64]
    status = stage.get("status")
    if isinstance(status, str) and status:
        summary["status"] = status[:64]
    artifact_published = stage.get("artifact_published")
    if type(artifact_published) is bool:
        summary["artifact_published"] = artifact_published
    diagnostic_count = stage.get("diagnostic_count")
    if type(diagnostic_count) is int and diagnostic_count >= 0:
        summary["diagnostic_count"] = diagnostic_count

    execution = stage.get("execution")
    if isinstance(execution, dict):
        canonical_output = execution.get("canonical_vm_output")
        stage0_output = execution.get("stage0_output")
        if isinstance(canonical_output, list) and isinstance(stage0_output, list):
            summary["stage0_outputs_match"] = canonical_output == stage0_output

    for metric_scope in ("build_metrics", "provider_metrics", "readiness_metrics"):
        metrics = stage.get(metric_scope)
        if not isinstance(metrics, dict):
            continue
        metric_summary: dict[str, int | float] = {}
        for key in ("wall_seconds", "cpu_seconds"):
            value = metrics.get(key)
            if (type(value) in (int, float) and math.isfinite(value)
                    and value >= 0):
                metric_summary[key] = value
        for key in (
            "peak_rss_bytes", "peak_open_file_descriptors", "peak_threads",
            "peak_child_processes", "peak_process_count",
        ):
            value = metrics.get(key)
            if type(value) is int and value >= 0:
                metric_summary[key] = value
        if metric_summary:
            summary[metric_scope] = metric_summary

    verification = stage.get("verification_work")
    if isinstance(verification, dict):
        verification_summary: dict[str, Any] = {}
        for key in (
            "front_end_outcome", "function_body_verification",
            "backend_artifact", "artifact_decision_reason",
        ):
            value = verification.get(key)
            if isinstance(value, str) and value:
                verification_summary[key] = value[:128]
        front_end_invocations = verification.get("front_end_invocations")
        if type(front_end_invocations) is int and front_end_invocations >= 0:
            verification_summary["front_end_invocations"] = front_end_invocations
        for source_key, summary_key in (
            ("recomputed_semantic_facts", "recomputed_semantic_fact_count"),
            ("reused_semantic_facts", "reused_semantic_fact_count"),
        ):
            facts = verification.get(source_key)
            if isinstance(facts, list) and all(
                isinstance(item, str) and item for item in facts[:256]
            ):
                verification_summary[summary_key] = min(len(facts), 256)

        cache = verification.get("verification_dependency_summary_cache")
        if isinstance(cache, dict):
            cache_summary: dict[str, Any] = {}
            cache_status = cache.get("status")
            if isinstance(cache_status, str) and cache_status:
                cache_summary["status"] = cache_status[:64]
            cache_observation = cache.get("observation")
            if isinstance(cache_observation, dict):
                for key, summary_key in (
                    ("recomputed_function_subjects", "recomputed_function_subject_count"),
                    ("reused_function_subjects", "reused_function_subject_count"),
                    ("fact_seeded_function_subjects", "fact_seeded_function_subject_count"),
                ):
                    items = cache_observation.get(key)
                    if isinstance(items, list) and all(
                        isinstance(item, str) and item for item in items[:256]
                    ):
                        cache_summary[summary_key] = min(len(items), 256)
                evicted = cache_observation.get("evicted_entry_count")
                if type(evicted) is int and evicted >= 0:
                    cache_summary["evicted_entry_count"] = evicted
            if cache_summary:
                verification_summary["dependency_summary_cache"] = cache_summary
        if verification_summary:
            summary["verification_work"] = verification_summary
    return summary if len(summary) > 1 else None


def _compiler_dependency_summary_observation(
    session: Any, reference: dict[str, str], source: dict[str, Any],
) -> dict[str, Any] | None:
    """Adapt one exact Compiler language-fact cache workload into ECI context."""
    if source.get("schema_version") != COMPILER_LANGUAGE_DEPENDENCY_SUMMARY_SCHEMA:
        return None
    integer_fields = (
        "language_dependency_summaries_actually_reused",
        "language_verified_facts_reused",
        "fresh_process_verified_facts_reused",
        "fresh_process_fact_seeded_dependency_summaries",
    )
    direct: dict[str, int | bool] = {}
    for key in integer_fields:
        value = source.get(key)
        if type(value) is not int or value < 0:
            return None
        direct[key] = value
    for key in ("execution_matches_stage0", "invalid_source_reused_previous_success"):
        value = source.get(key)
        if type(value) is not bool:
            return None
        direct[key] = value
    for key in ("compiler_revision_before_test", "language_locked_revision"):
        value = source.get(key)
        if not isinstance(value, str) or not _COMMIT.fullmatch(value):
            return None
    cache_scope = source.get("language_cache_scope")
    stages = source.get("stages")
    if (not isinstance(cache_scope, str) or not cache_scope
            or not isinstance(stages, dict)
            or any(name not in stages for name in COMPILER_LANGUAGE_DEPENDENCY_SUMMARY_STAGES)):
        return None

    stage_summaries = []
    stage_matches = 0
    stage_matches_observed = 0
    for name in COMPILER_LANGUAGE_DEPENDENCY_SUMMARY_STAGES:
        summary = _compiler_dependency_summary_stage(name, stages.get(name))
        if summary is None:
            return None
        stage_summaries.append(summary)
        matches = summary.get("stage0_outputs_match")
        if type(matches) is bool:
            stage_matches_observed += 1
            stage_matches += int(matches)

    derived: dict[str, int] = {
        "stage_count": len(stage_summaries),
        "stage0_output_match_count": stage_matches,
        "stage0_output_comparison_count": stage_matches_observed,
    }
    request: dict[str, Any] = {
        "source_schema_version": COMPILER_LANGUAGE_DEPENDENCY_SUMMARY_SCHEMA,
        "source_record_identity_basis": (
            "authenticated-campaign-evidence-pointer;source-record-has-no-identity-field"
        ),
        "compiler_revision_before_test": source["compiler_revision_before_test"],
        "language_locked_revision": source["language_locked_revision"],
        "language_source_profile": source.get("language_source_profile"),
        "request_schema": source.get("request_schema"),
        "response_schema": source.get("response_schema"),
        "verification_work_schema": source.get("verification_work_schema"),
        "language_cache_schema": source.get("language_cache_schema"),
        "language_cache_observation_schema": source.get(
            "language_cache_observation_schema",
        ),
        "language_cache_policy_identity": source.get("language_cache_policy_identity"),
        "stage_names": [item["name"] for item in stage_summaries],
    }
    request = {
        key: (value[:160] if isinstance(value, str) else value)
        for key, value in request.items()
        if (key == "source_schema_version"
            or key == "source_record_identity_basis"
            or key == "stage_names"
            or isinstance(value, str) and value)
    }
    measurement_summary = {
        "direct_measurements": direct,
        "derived_measurements": derived,
        "measurement_availability": {
            "model_identity": "not-recorded-in-source",
            "token_usage": "not-recorded-in-source",
            "source_capture_time": "unavailable-in-source-record",
            "cgroup_peak_memory": "not-available-in-source-environment",
            "resource_consumption": "per-build-and-provider-stage-metrics",
            "cache_accounting": "separate-language-fact-and-structural-summary-counts",
        },
    }
    snapshot = session.snapshot
    campaign = snapshot.get("campaign")
    campaign = campaign if isinstance(campaign, dict) else {}
    intent = snapshot.get("intent")
    intent = intent if isinstance(intent, dict) else {}
    campaign_identity = campaign.get("identity")
    session_identity = getattr(session, "session_id", None)
    if (not isinstance(campaign_identity, str) or not campaign_identity
            or not isinstance(session_identity, str) or not session_identity):
        return None
    related_work: dict[str, str] = {
        "campaign_id": campaign_identity,
        "session_id": session_identity,
    }
    for key, value in (
        ("work_intent_id", intent.get("identity")),
        ("work_intent_goal", intent.get("goal")),
        ("environment_id", snapshot.get("environment_id")),
        ("definition_id", (snapshot.get("provenance") or {}).get("definition_id")
         if isinstance(snapshot.get("provenance"), dict) else None),
    ):
        if isinstance(value, str) and value:
            related_work[key] = value

    return {
        "schema_version": OBSERVATION_SCHEMA,
        "identity": reference["identity"],
        "captured_at": "unavailable-in-source-record",
        "measurement_basis": (
            "bounded-derived-view-of-authenticated-compiler-language-cache-evidence/1"
        ),
        "observation": {
            "kind": "compiler-language-verification-fact-reuse-workload",
            "source_interface": "MNCS Compiler campaign evidence projection",
            "request": request,
            "outcome": {
                "direct_measurements": direct,
                "derived_measurements": derived,
                "language_cache_scope": cache_scope[:160],
                "cache_measurement_distinction": (
                    "Dependency summaries are process-local structural summaries; "
                    "Language verification facts are measured separately."
                ),
                "stage_summaries": stage_summaries,
                "measurement_summary": measurement_summary,
                "resource_measurement_scope": (
                    "per-build and provider-stage source metrics, not agent session use"
                ),
                "duration_measurement_scope": (
                    "individual build/provider requests, not campaign duration"
                ),
                "source_capture_time": "unavailable-in-source-record",
            },
            "measurement_availability": measurement_summary[
                "measurement_availability"
            ],
        },
        "related_work": related_work,
        "not_measured": [
            "source capture timestamp and model identity",
            "token and cache accounting for the Compiler agent session",
            "total campaign duration or resource use outside these requests",
            "whether this workload and cache behavior recurs in other sessions",
        ],
    }


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
    if _is_compiler_dependency_summary_reference(reference):
        source_identity = record.get("identity")
        if source_identity is not None and source_identity != reference["identity"]:
            return record, "observation-identity-mismatch"
        adapted = _compiler_dependency_summary_observation(session, reference, record)
        if adapted is None:
            return None, "observation-contract-invalid"
        return adapted, "verified"
    if record.get("identity") != reference["identity"]:
        return record, "observation-identity-mismatch"
    if _is_compiler_regression_reference(reference):
        adapted = _compiler_regression_observation(session, reference, record)
        if adapted is None:
            return None, "observation-contract-invalid"
        return adapted, "verified"
    observation = record.get("observation")
    related_work = record.get("related_work")
    source_session_context = (
        observation.get("source_session_context")
        if isinstance(observation, dict) else None
    )
    hypothesis = record.get("pressure_hypothesis")
    recorder = record.get("recorder")
    if (record.get("schema_version") != OBSERVATION_SCHEMA
            or not isinstance(record.get("captured_at"), str)
            or not isinstance(observation, dict)
            or not isinstance(observation.get("kind"), str)
            or not isinstance(observation.get("source_interface"), str)
            or not isinstance(observation.get("request"), dict)
            or (not isinstance(observation.get("outcome"), dict)
                and not isinstance(observation.get("projection_summary"), dict))
            or not isinstance(related_work, dict)
            or any(not isinstance(related_work.get(key), str) or not related_work[key]
                   for key in ("campaign_id", "session_id"))
            or any(
                related_work.get(key) is not None
                and (not isinstance(related_work.get(key), str) or not related_work[key])
                for key in ("work_intent_id", "work_intent_goal")
            )
            or (source_session_context is not None
                and not isinstance(source_session_context, dict))
            or (isinstance(source_session_context, dict)
                and any(
                    value is not None and not isinstance(value, str)
                    for value in source_session_context.values()
                ))
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
            or (hypothesis is not None and not isinstance(hypothesis, dict))):
        return None, "observation-contract-invalid"
    if isinstance(hypothesis, dict) and any(
        not isinstance(hypothesis.get(key), str) or not hypothesis[key]
        for key in ("identity", "title", "category", "subsystem",
                    "actionable_recommendation")
    ):
        return None, "observation-contract-invalid"
    if (isinstance(hypothesis, dict) and hypothesis.get("owner") is not None
            and (not isinstance(hypothesis["owner"], str) or not hypothesis["owner"])):
        return None, "observation-contract-invalid"
    return record, "verified"


def _campaign_charter_context(
    session: Any, state: dict[str, Any], root: Path | None, *,
    campaign_identity: Any, deadline: float,
) -> dict[str, Any] | None:
    """Read one unique content-addressed charter without replacing session intent."""
    index = state.get("campaign_charter_evidence")
    if not isinstance(index, dict):
        return None
    count = index.get("count")
    raw_references = index.get("references")
    if (type(count) is not int or count < 0
            or not isinstance(raw_references, list)
            or type(index.get("truncated")) is not bool):
        return {"status": "unavailable", "reason": "charter-index-invalid"}
    if count == 0:
        if raw_references or index["truncated"]:
            return {"status": "partial", "reason": "charter-index-inconsistent"}
        return {"status": "not-recorded", "candidate_reference_count": 0}

    references = raw_references[:MAX_CAMPAIGN_CHARTER_CANDIDATES]
    incomplete = (
        index["truncated"]
        or count != len(raw_references)
        or len(raw_references) > MAX_CAMPAIGN_CHARTER_CANDIDATES
    )
    valid_charters: list[dict[str, Any]] = []
    examined = 0
    for raw_reference in references:
        pointer = _validate_reference(raw_reference)
        if pointer is None or "sha256" not in pointer:
            incomplete = True
            continue
        examined += 1
        if "campaign_charter" not in pointer["identity"]:
            continue
        if root is None:
            incomplete = True
            continue
        checkout = _checkout(session, pointer["repository"], root)
        if checkout is None:
            incomplete = True
            continue
        raw, _ = _git_blob(
            checkout, pointer["commit"], pointer["path"],
            deadline=deadline,
            max_bytes=MAX_OBSERVATION_BYTES,
            oversized_status="campaign-charter-too-large",
        )
        if raw is None:
            incomplete = True
            continue
        if hashlib.sha256(raw).hexdigest() != pointer["sha256"]:
            incomplete = True
            continue
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            # The committed bytes are authentic, but they do not form a charter
            # record and therefore cannot contribute a campaign objective.
            continue
        if (not isinstance(record, dict)
                or record.get("schema_version") != CAMPAIGN_CHARTER_SCHEMA
                or record.get("identity") != pointer["identity"]
                or record.get("campaign_identity") != campaign_identity):
            continue
        objective = record.get("objective")
        source = record.get("source")
        title = source.get("request_title") if isinstance(source, dict) else None
        if (not isinstance(objective, str) or not objective.strip()
                or len(objective) > MAX_CAMPAIGN_CHARTER_OBJECTIVE_CHARS
                or "\x00" in objective
                or (title is not None
                    and (not isinstance(title, str) or len(title) > 256
                         or "\x00" in title))):
            continue
        valid_charters.append({
            "identity": pointer["identity"],
            "title": title,
            "objective": objective,
            "evidence_reference": pointer,
        })

    result: dict[str, Any] = {
        "candidate_reference_count": count,
        "references_examined": examined,
    }
    if incomplete:
        return {
            **result,
            "status": "partial",
            "reason": "charter-candidates-not-fully-verified",
        }
    if len(valid_charters) > 1:
        return {
            **result,
            "status": "ambiguous",
            "valid_charter_count": len(valid_charters),
        }
    if not valid_charters:
        return {**result, "status": "not-found"}
    return {**result, "status": "verified", **valid_charters[0]}


def _source_status(source: Any, references: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(source, dict):
        return {"kind": "unknown", "status": "invalid-source-reference"}
    summary = {
        key: source.get(key)
        for key in ("identity", "kind", "repository", "commit", "path", "sha256",
                    "campaign_id", "session_id")
        if source.get(key) is not None
    }
    if source.get("kind") in CAPTURED_OPERATION_RESULT_SOURCE_KINDS:
        summary["status"] = "captured-in-observation-record"
        return summary
    if source.get("kind") == "authenticated-session-activity-projection":
        if _validate_captured_session_activity_reference(source):
            summary["status"] = "captured-in-observation-record"
            summary["event_identities"] = list(source["event_identities"])
        else:
            summary["status"] = "invalid-source-reference"
        return summary
    wanted = tuple(source.get(key) for key in ("repository", "commit", "path", "sha256"))
    match = next((reference for reference in references
                  if tuple(reference.get(key) for key in
                           ("repository", "commit", "path", "sha256")) == wanted), None)
    summary["registration_scope"] = "selected-campaign-evidence-window"
    if match is None:
        summary["status"] = "not-present-in-selected-campaign-evidence-window"
    else:
        summary["status"] = match.get("verification", "unverified")
        summary["campaign_evidence_identity"] = match.get("identity")
    return summary


def _work_intent_workspace_alignment(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Compare declared WorkIntent repositories with selected checkout identities."""
    intent = snapshot.get("intent")
    intent = intent if isinstance(intent, dict) else {}
    raw_declared = intent.get("repositories")
    raw_selected = snapshot.get("selected_checkouts")
    if raw_declared is None:
        return {
            "status": "not-declared",
            "declared_repositories": [],
            "selected_repositories": sorted(
                key for key in raw_selected
                if isinstance(key, str) and _is_repository_identity(key)
            ) if isinstance(raw_selected, dict) else [],
            "unselected_declared_repositories": [],
            "basis": "session-work-intent-and-selected-checkout-identities",
        }
    if (not isinstance(raw_declared, list)
            or any(not _is_repository_identity(item) for item in raw_declared)):
        return {
            "status": "unavailable",
            "declared_repositories": [],
            "selected_repositories": [],
            "unselected_declared_repositories": [],
            "basis": "session-work-intent-and-selected-checkout-identities",
        }
    if not isinstance(raw_selected, dict):
        return {
            "status": "workspace-selection-unavailable",
            "declared_repositories": sorted(set(raw_declared)),
            "selected_repositories": [],
            "unselected_declared_repositories": [],
            "basis": "session-work-intent-and-selected-checkout-identities",
        }
    declared = sorted(set(raw_declared))
    selected = sorted(
        key for key in raw_selected if isinstance(key, str)
        and _is_repository_identity(key)
    )
    unselected = sorted(set(declared) - set(selected))
    if not declared:
        status = "no-repositories-declared"
    elif unselected:
        status = "declared-repositories-not-selected"
    else:
        status = "all-declared-repositories-selected"
    return {
        "status": status,
        "declared_repositories": declared,
        "selected_repositories": selected,
        "unselected_declared_repositories": unselected,
        "basis": "session-work-intent-and-selected-checkout-identities",
    }


def _is_repository_identity(value: Any) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value) is not None
    )


def _evidence_visibility_summary(
    diagnostics: Any, diagnostic_count: Any, diagnostics_truncated: Any,
) -> dict[str, Any]:
    """Summarize bounded source-scope gaps without treating them as bad content."""
    if not isinstance(diagnostics, list) or type(diagnostic_count) is not int:
        return {
            "status": "unavailable",
            "verification_diagnostic_count": None,
            "outside_selected_reference_diagnostic_count": 0,
            "outside_selected_repositories": [],
            "diagnostics_truncated": None,
            "basis": "bounded-campaign-evidence-source-verification-diagnostics",
        }
    outside = [
        item for item in diagnostics
        if isinstance(item, dict)
        and item.get("verification") == "outside-selected-session"
    ]
    counts: dict[str, int] = {}
    for item in outside:
        repository = item.get("repository")
        if _is_repository_identity(repository):
            counts[repository] = counts.get(repository, 0) + 1
    truncated = diagnostics_truncated if type(diagnostics_truncated) is bool else None
    if outside:
        status = "outside-selected-session-references-observed"
    elif diagnostic_count:
        status = "other-source-verification-gaps-observed"
    elif truncated:
        status = "diagnostics-truncated"
    else:
        status = "no-source-verification-gaps-observed"
    if truncated and outside:
        status = "outside-selected-session-references-observed-with-truncated-diagnostics"
    return {
        "status": status,
        "verification_diagnostic_count": diagnostic_count,
        "outside_selected_reference_diagnostic_count": len(outside),
        "outside_selected_repositories": [
            {"repository": repository, "diagnostic_count": counts[repository]}
            for repository in sorted(counts)[:16]
        ],
        "diagnostics_truncated": truncated,
        "basis": "bounded-campaign-evidence-source-verification-diagnostics",
    }


def _context_pressure_candidates(
    *,
    session_id: str,
    campaign_id: Any,
    work_intent_id: Any,
    workspace_alignment: dict[str, Any],
    evidence_visibility: dict[str, Any],
    observation_reference_diagnostics: Any,
    diagnostics_truncated: bool,
) -> list[dict[str, Any]]:
    """Derive bounded, explicitly hypothetical candidates from session scope."""
    unselected = workspace_alignment.get("unselected_declared_repositories")
    if not isinstance(unselected, list):
        return []
    diagnostics = (
        observation_reference_diagnostics
        if isinstance(observation_reference_diagnostics, list) else []
    )
    selected = workspace_alignment.get("selected_repositories")
    selected = selected if isinstance(selected, list) else []
    candidates: list[dict[str, Any]] = []
    for repository in unselected[:MAX_OBSERVATIONS]:
        if not _is_repository_identity(repository):
            continue
        references: list[dict[str, str]] = []
        for diagnostic in diagnostics:
            if (
                not isinstance(diagnostic, dict)
                or diagnostic.get("verification") != "outside-selected-session"
                or diagnostic.get("repository") != repository
            ):
                continue
            reference = {
                key: diagnostic[key]
                for key in ("identity", "repository", "commit", "path", "sha256")
                if isinstance(diagnostic.get(key), str)
            }
            reference["verification"] = "outside-selected-session"
            references.append(reference)
        visible_references = references[:MAX_OBSERVATIONS]
        candidate_identity = "cpc_" + digest_hex({
            "session_id": session_id,
            "campaign_id": campaign_id,
            "work_intent_id": work_intent_id,
            "repository": repository,
            "evidence_reference_identities": [
                reference.get("identity") for reference in visible_references
            ],
        }, length=16)
        candidates.append({
            "identity": candidate_identity,
            "kind": "declared-repository-selection-gap",
            "interpretation": "hypothesis",
            "status": "candidate",
            "title": "A Work Intent repository is not selected in this session",
            "affected_repository": repository,
            "affected_work_context": {
                "campaign_id": _short_text(campaign_id, 160),
                "session_id": _short_text(session_id, 160),
                "work_intent_id": _short_text(work_intent_id, 160),
            },
            "recurrence_status": "not-established",
            "basis": {
                "measurement": "session-work-intent-and-selected-checkout-identities",
                "workspace_alignment_status": _short_text(
                    workspace_alignment.get("status"), 96,
                ),
                "evidence_visibility_status": _short_text(
                    evidence_visibility.get("status"), 96,
                ),
                "selected_repositories": [
                    _short_text(value, 128) for value in selected[:MAX_OBSERVATIONS]
                    if _is_repository_identity(value)
                ],
                "selected_repositories_truncated": len(selected) > MAX_OBSERVATIONS,
                "outside_selected_reference_count": len(references),
            },
            "evidence_references": visible_references,
            "evidence_references_truncated": (
                len(references) > len(visible_references) or diagnostics_truncated
            ),
            "recommendation": (
                "Confirm whether this repository is needed for the declared objective; "
                "if so, use an authorized session that selects its exact checkout. "
                "Keep its sources outside-selected until then."
            ),
            "not_measured": [
                "whether the selection gap blocks or delays the declared objective",
                "why the repository is absent from the selected checkout set",
                "whether the same gap recurs in another session",
            ],
        })
    return candidates


def _source_pointer_key(pointer: dict[str, str]) -> tuple[str, str, str, str]:
    return (
        pointer["repository"], pointer["commit"], pointer["path"],
        pointer.get("sha256", ""),
    )


def _verify_source_pointer(
    session: Any, source: Any, references: list[dict[str, Any]], root: Path | None,
    *, deadline: float, cache: dict[tuple[str, str, str, str], str],
    attempted: set[tuple[str, str, str, str]],
    workspace_checkouts: dict[str, Path | None],
) -> None:
    """Verify accessible source blobs within the projection's shared read budget."""
    pointer = _validate_source_pointer(source)
    if pointer is None:
        return
    key = _source_pointer_key(pointer)
    if key in cache or key in attempted:
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
    if time.monotonic() >= deadline:
        cache[key] = "source-read-budget-exhausted"
        return
    checkout, verification_basis = (
        _source_checkout(
            session, pointer["repository"], root, workspace_checkouts, deadline,
        )
        if root is not None else (None, None)
    )
    if checkout is None:
        cache[key] = (
            "source-read-budget-exhausted"
            if time.monotonic() >= deadline else "outside-selected-session"
        )
        return
    attempted.add(key)
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
    elif verification_basis == "workspace-read-only":
        cache[key] = "workspace-read-only-content-integrity-verified"
    else:
        cache[key] = "content-integrity-verified"


def _source_integrity_status(source: Any, cache: dict[tuple[str, str, str, str], str]) -> str:
    if (isinstance(source, dict)
            and source.get("kind") in CAPTURED_OPERATION_RESULT_SOURCE_KINDS):
        return "captured-in-observation-record"
    if isinstance(source, dict) and source.get(
        "kind"
    ) == "authenticated-session-activity-projection":
        return (
            "captured-in-observation-record"
            if _validate_captured_session_activity_reference(source)
            else "source-reference-invalid"
        )
    pointer = _validate_source_pointer(source)
    if pointer is None:
        return "source-reference-invalid"
    return cache.get(_source_pointer_key(pointer), "not-checked")


def _validate_captured_session_activity_reference(source: Any) -> bool:
    """Validate a bounded session-event pointer stored in an observation."""
    if not isinstance(source, dict):
        return False
    repository = source.get("repository")
    campaign_id = source.get("campaign_id")
    session_id = source.get("session_id")
    event_ids = source.get("event_identities")
    return (
        isinstance(repository, str) and bool(repository) and len(repository) <= 160
        and isinstance(campaign_id, str) and bool(campaign_id) and len(campaign_id) <= 160
        and isinstance(session_id, str) and bool(session_id) and len(session_id) <= 160
        and isinstance(event_ids, list) and 0 < len(event_ids) <= 32
        and all(
            isinstance(identity, str)
            and re.fullmatch(r"evt_[A-Za-z0-9][A-Za-z0-9_-]{0,155}", identity)
            for identity in event_ids
        )
        and len(set(event_ids)) == len(event_ids)
    )


def _authenticated_session_event_reference_resolution(
    source: Any, *, selected_session_id: str, activity: dict[str, Any],
) -> dict[str, Any]:
    """Resolve a captured event pointer only against the selected bounded view."""
    if not _validate_captured_session_activity_reference(source):
        return {"event_reference_status": "invalid-source-reference"}
    expected_event_ids = set(source["event_identities"])
    if source["session_id"] != selected_session_id:
        return {
            "event_reference_status": "source-session-not-selected",
            "event_reference_count": len(expected_event_ids),
            "event_reference_matched_count": 0,
        }
    raw_operations = activity.get("rpc_operations")
    raw_operations = raw_operations if isinstance(raw_operations, list) else []
    visible_event_ids = {
        reference.get("identity")
        for operation in raw_operations
        if isinstance(operation, dict)
        and isinstance(reference := operation.get("event_reference"), dict)
        and isinstance(reference.get("identity"), str)
    }
    matched_event_ids = expected_event_ids & visible_event_ids
    activity_source = activity.get("source")
    activity_source = activity_source if isinstance(activity_source, dict) else None
    records_omitted = (
        activity_source.get("rpc_operation_records_omitted")
        if activity_source is not None else None
    )
    records_invalid = (
        activity_source.get("rpc_operation_records_invalid")
        if activity_source is not None else None
    )
    events_considered = (
        activity_source.get("rpc_operation_events_considered")
        if activity_source is not None else None
    )
    records_returned = (
        activity_source.get("rpc_operation_records_returned")
        if activity_source is not None else None
    )
    bounded = (
        activity_source is None
        or activity_source.get("event_window_truncated") is not False
        or type(records_omitted) is not int
        or records_omitted > 0
        or type(records_invalid) is not int
        or records_invalid > 0
        or activity_source.get("rpc_operation_capture_limit_reached") is not False
        or type(events_considered) is not int
        or type(records_returned) is not int
        or events_considered != records_returned
        or records_returned != len(raw_operations)
    )
    if matched_event_ids == expected_event_ids:
        status = "all-referenced-events-present"
    elif matched_event_ids and bounded:
        status = "some-present-others-unresolved-in-bounded-view"
    elif matched_event_ids:
        status = "some-present-others-not-found-in-complete-view"
    elif bounded:
        status = "unresolved-in-bounded-session-view"
    else:
        status = "no-referenced-events-in-selected-session-activity"
    return {
        "event_reference_status": status,
        "event_reference_count": len(expected_event_ids),
        "event_reference_matched_count": len(matched_event_ids),
    }


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
            for item in (identity, hypothesis_identity, verification_identity))
            or (subsequent_identity is not None
                and (not isinstance(subsequent_identity, str)
                     or not subsequent_identity or len(subsequent_identity) > 160))
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


def _validate_observation_correction(value: Any) -> dict[str, Any] | None:
    """Validate an append-only correction that targets an exact observation."""
    if not isinstance(value, dict):
        return None
    target = _validate_reference(value.get("target_reference"))
    fields = value.get("corrected_field_paths")
    replacements = value.get("replacement_values")
    reason = value.get("reason")
    if (value.get("schema_version") != OBSERVATION_CORRECTION_SCHEMA
            or target is None or "sha256" not in target
            or not _is_observation_reference(target)
            or not isinstance(fields, list) or not 1 <= len(fields) <= 2
            or any(
                not isinstance(field, str)
                or field not in {"supporting_sources", "not_measured"}
                for field in fields
            )
            or len(set(fields)) != len(fields)
            or not isinstance(replacements, dict)
            or set(replacements) != set(fields)
            or not isinstance(reason, str) or not reason or len(reason) > 512):
        return None
    if "supporting_sources" in fields:
        sources = replacements["supporting_sources"]
        if (not isinstance(sources, list) or not 1 <= len(sources) <= 16
                or any(
                    (pointer := _validate_source_pointer(source)) is None
                    or "sha256" not in pointer
                    for source in sources
                )):
            return None
    if "not_measured" in fields:
        limits = replacements["not_measured"]
        if (not isinstance(limits, list) or not 1 <= len(limits) <= 16
                or any(not isinstance(item, str) or not item or len(item) > 512
                       for item in limits)):
            return None
    return {
        "schema_version": OBSERVATION_CORRECTION_SCHEMA,
        "target_reference": target,
        "corrected_field_paths": list(fields),
        "replacement_values": replacements,
        "reason": reason,
    }


def _validate_evidence_reference_correction(
    value: Any,
) -> dict[str, Any] | None:
    """Validate an append-only correction to one exact campaign source pointer.

    A pointer correction may change only the commit. Identity, selected
    repository, path, and content digest must remain identical so the
    replacement can recover the same observation bytes from a verified commit.
    """
    if not isinstance(value, dict):
        return None
    target = _validate_reference(value.get("target_reference"))
    replacement = _validate_reference(value.get("replacement_reference"))
    reason = value.get("reason")
    if (value.get("schema_version") != EVIDENCE_REFERENCE_CORRECTION_SCHEMA
            or target is None or replacement is None
            or "sha256" not in target or "sha256" not in replacement
            or not _is_observation_reference(target)
            or not _is_observation_reference(replacement)
            or not target["identity"].startswith("ev_environment_")
            or not target["path"].startswith(OBSERVATION_PATH_PREFIX)
            or any(target.get(key) != replacement.get(key) for key in (
                "identity", "repository", "path", "sha256",
            ))
            or target["commit"] == replacement["commit"]
            or not isinstance(reason, str) or not reason or len(reason) > 512):
        return None
    return {
        "schema_version": EVIDENCE_REFERENCE_CORRECTION_SCHEMA,
        "target_reference": target,
        "replacement_reference": replacement,
        "reason": reason,
    }


def _recurrence(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for observation in observations:
        hypothesis = observation["pressure_hypothesis"]
        if (not isinstance(hypothesis, dict)
                or observation.get("improvement_relation_identity") is not None):
            # An implementation record may restate the pressure it addresses.
            # That link is context for the change, not a second occurrence.
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
            "campaign_objective", "environment_id", "definition_id",
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
        definition_values = {
            "title": "title",
            "category": "category",
            "subsystem": "subsystem",
            "owner": "owner",
            "recommendation": "actionable_recommendation",
        }
        definition_conflicts = []
        definition = {}
        for output_key, source_key in definition_values.items():
            values = sorted({
                item["pressure_hypothesis"].get(source_key)
                for item in group
                if isinstance(item["pressure_hypothesis"].get(source_key), str)
                and item["pressure_hypothesis"][source_key]
            })
            if len(values) > 1:
                definition_conflicts.append(output_key)
            elif values:
                definition[output_key] = values[0]
        definition_status = (
            "conflicting-recorded-definitions"
            if definition_conflicts else "consistent"
        )
        summary = {
            "identity": identity,
            **definition,
            "definition_status": definition_status,
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
            "evidence": [item["evidence_reference"] for item in group],
        }
        if definition_conflicts:
            summary["definition_conflict_fields"] = definition_conflicts
            summary["definition_evidence_observation_identities"] = [
                item["identity"] for item in group
            ]
        result.append(summary)
    return result


def _distinct_contexts(
    observations: list[dict[str, Any]], field: str, keys: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Deduplicate contexts by stable identity and merge non-conflicting detail."""
    contexts: list[dict[str, Any]] = []
    stable_keys = (
        ("campaign_id", "session_id", "work_intent_id")
        if field == "related_work"
        else ("campaign_id", "session_id", "consumer_id")
    )
    raw_contexts = []
    for observation in observations:
        raw = observation.get(field)
        if not isinstance(raw, dict):
            continue
        incoming = {
            key: raw[key] for key in keys
            if isinstance(raw.get(key), str) and raw[key]
        }
        if not incoming:
            continue
        raw_contexts.append(incoming)

    # Process the strongest identities first so a sparse record cannot be
    # attached to the first of several compatible contexts merely because of
    # ledger order.
    raw_contexts.sort(key=lambda context: (
        -sum(key in context for key in stable_keys),
        -len(context),
        tuple(sorted(context.items())),
    ))
    for incoming in raw_contexts:
        if incoming in contexts:
            continue

        candidates = []
        for index, existing in enumerate(contexts):
            shared_stable_identity = [
                key for key in stable_keys
                if key in incoming and key in existing
            ]
            if len(shared_stable_identity) < 2:
                continue
            if any(
                incoming[key] != existing[key]
                for key in stable_keys
                if key in incoming and key in existing
            ):
                continue
            if any(
                incoming[key] != existing[key]
                for key in keys if key in incoming and key in existing
            ):
                continue
            candidates.append(index)

        if len(candidates) == 1:
            contexts[candidates[0]].update(incoming)
            continue
        # A missing intent/consumer can match multiple stable identities. Keep
        # that sparse context separate rather than assigning it arbitrarily.
        contexts.append(incoming)

    result: list[dict[str, Any]] = []
    for identity in contexts:
        context: dict[str, Any] = {}
        for key, value in identity.items():
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
        result.append(context)
    return result


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


def _authenticated_rpc_read_event(
    event: dict[str, Any], snapshot: dict[str, Any],
) -> bool:
    """Accept only bounded Control read records bound to this session owner."""
    payload = event.get("payload")
    caller = payload.get("caller") if isinstance(payload, dict) else None
    context = payload.get("context") if isinstance(payload, dict) else None
    campaign = snapshot.get("campaign")
    campaign = campaign if isinstance(campaign, dict) else {}
    intent = snapshot.get("intent")
    intent = intent if isinstance(intent, dict) else {}
    measurements = payload.get("measurement_availability") if isinstance(
        payload, dict,
    ) else None
    values = payload.get("measurements") if isinstance(payload, dict) else None
    elapsed = values.get("control_dispatch_elapsed_ms") if isinstance(values, dict) else None
    request_digest = payload.get("request_digest") if isinstance(payload, dict) else None
    outcome = payload.get("outcome_status") if isinstance(payload, dict) else None
    exit_code = payload.get("exit_code") if isinstance(payload, dict) else None
    failure_code = payload.get("failure_code") if isinstance(payload, dict) else None
    expected_measurements = {
        "model_identity": "unavailable",
        "token_usage": "unavailable",
        "control_dispatch_elapsed_ms": "measured",
        "resource_consumption": "unavailable",
        "cache_accounting": "unavailable",
    }
    return not (
            event.get("type") != "environment.rpc.read_observed"
            or event.get("producer") != "mncs-control-mcp"
            or not isinstance(payload, dict)
            or payload.get("schema_version") != "mncs.environment.rpc-read-activity/1"
            or _event_reference(event) is None
            or not isinstance(payload.get("operation"), str)
            or not re.fullmatch(r"[a-z][a-z0-9.-]{0,95}", payload["operation"])
            or outcome not in {"succeeded", "failed"}
            or (outcome == "succeeded" and (exit_code != 0 or failure_code is not None))
            or (outcome == "failed" and (
                (exit_code is not None and (type(exit_code) is not int or exit_code == 0))
                or not isinstance(failure_code, str)
                or not re.fullmatch(r"[A-Za-z0-9._-]{1,96}", failure_code)
            ))
            or (outcome == "succeeded" and type(exit_code) is not int)
            or not isinstance(request_digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", request_digest)
            or payload.get("request_operation_digest") != request_digest
            or not isinstance(payload.get("request_event_digest"), str)
            or not re.fullmatch(r"[0-9a-f]{16}", payload["request_event_digest"])
            or not isinstance(payload.get("request_id"), str)
            or not re.fullmatch(r"req_[A-Za-z0-9_-]{1,76}", payload["request_id"])
            or not isinstance(caller, dict)
            or caller.get("authenticated_principal_id")
            != snapshot.get("authenticated_principal_id")
            or not isinstance(caller.get("authenticated_principal_id"), str)
            or not re.fullmatch(
                r"ctrl_[0-9a-f]{32}", caller["authenticated_principal_id"],
            )
            or not isinstance(caller.get("control_grant_id"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", caller["control_grant_id"])
            or (caller.get("process_identity") is not None
                and (not isinstance(caller.get("process_identity"), str)
                     or not re.fullmatch(
                         r"proc_[0-9a-f]{32}", caller["process_identity"],
                     )))
            or not isinstance(context, dict)
            or context.get("environment_id") != snapshot.get("environment_id")
            or context.get("campaign_id") != campaign.get("identity")
            or context.get("work_intent_id") != intent.get("identity")
            or not isinstance(context.get("repository"), str)
            or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", context["repository"],
            )
            or any(value is not None and (
                not isinstance(value, str) or not value or len(value) > 256
                or "\x00" in value
            ) for key, value in context.items() if key != "repository")
            or (context.get("repository_head") is not None
                and not re.fullmatch(
                    r"(?:[0-9a-f]{40}|[0-9a-f]{64})",
                    context["repository_head"],
                ))
            or measurements != expected_measurements
            or not isinstance(values, dict)
            or set(values) != {"control_dispatch_elapsed_ms"}
            or type(elapsed) not in {int, float}
            or not 0 <= elapsed <= 600_000
    )


def _authority_denial_reason_class(reason: Any) -> str:
    """Classify a denial without copying free-form target or session details."""
    if not isinstance(reason, str) or not reason:
        return "reason-unavailable"
    normalized = " ".join(reason.lower().split())
    classifications = (
        ("action-forbidden-by-intent", "forbidden by intent"),
        ("unknown-action", "unknown action"),
        ("overlapping-claim", "overlaps scope claimed by another session"),
        ("claim-fenced", "authority was fenced after claim generation"),
        ("protected-scope", "is protected scope for this session"),
        ("outside-writable-authority", "is outside this session's writable authority"),
        ("workspace-facts-unavailable", "has no observed workspace facts"),
        ("scope-not-owned", "is not owned scope"),
        ("current-claim-required", "requires a current session claim"),
        ("escalation-required", "requires escalation"),
        (
            "unobserved-absolute-target",
            "absolute target is outside every observed repository checkout",
        ),
    )
    return next((category for category, fragment in classifications
                 if fragment in normalized), "other-denial")


def _resource_sample_payload(value: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Select the privacy-minimized, bounded fields from one Monitor sample."""
    if not isinstance(value, dict) or value.get("schema") != SYSTEM_RESOURCE_SAMPLE_SCHEMA:
        return None, "resource-sample-schema-mismatch"
    observed_at = value.get("observed_at")
    if observed_at is not None and (
        type(observed_at) is not int or observed_at < 0 or observed_at > 2**63 - 1
    ):
        return None, "resource-sample-time-invalid"
    raw_time = value.get("sample_time")
    sample_time = None
    if raw_time is not None:
        if not isinstance(raw_time, dict):
            return None, "resource-sample-time-invalid"
        sample_time = {}
        for key in ("wall_time_unix_ms", "monotonic_ns"):
            field_value = raw_time.get(key)
            if field_value is None:
                if key in raw_time:
                    sample_time[key] = None
                continue
            if type(field_value) is not int or field_value < 0 or field_value > 2**64 - 1:
                return None, "resource-sample-time-invalid"
            sample_time[key] = field_value
    raw_host = value.get("host")
    if not isinstance(raw_host, dict):
        return None, "resource-sample-host-facts-unavailable"
    host: dict[str, Any] = {}
    for key in ("cpu_count", "memory_total_bytes", "memory_available_bytes"):
        if key not in raw_host:
            continue
        field_value = raw_host[key]
        if field_value is None:
            host[key] = None
        elif type(field_value) is int and 0 <= field_value <= 2**63 - 1:
            host[key] = field_value
        else:
            return None, "resource-sample-host-fact-invalid"
    if "cpu_utilization" in raw_host:
        field_value = raw_host["cpu_utilization"]
        if field_value is None:
            host["cpu_utilization"] = None
        elif (type(field_value) in (int, float) and math.isfinite(field_value)
              and abs(field_value) <= 1_000_000):
            host["cpu_utilization"] = field_value
        else:
            return None, "resource-sample-host-fact-invalid"
    if "load_average" in raw_host:
        load_average = raw_host["load_average"]
        if load_average is None:
            host["load_average"] = None
        elif isinstance(load_average, list) and len(load_average) <= 3:
            if any(
                item is not None and (
                    type(item) not in (int, float) or not math.isfinite(item)
                    or abs(item) > 1_000_000
                )
                for item in load_average
            ):
                return None, "resource-sample-host-fact-invalid"
            host["load_average"] = load_average
        else:
            return None, "resource-sample-host-fact-invalid"
    for key in ("cpu_status", "memory_available_status"):
        if key in raw_host:
            field_value = raw_host[key]
            if field_value not in SYSTEM_RESOURCE_SAMPLE_STATUSES:
                return None, "resource-sample-host-fact-invalid"
            host[key] = field_value
    sample: dict[str, Any] = {"schema": SYSTEM_RESOURCE_SAMPLE_SCHEMA, "host": host}
    sample["measurement_basis"] = "system-monitor-reported-resource-sample"
    sample["measurement_availability"] = {
        "host.cpu_count": (
            "measured" if host.get("cpu_count") is not None else "unavailable"
        ),
        "host.cpu_utilization": (
            "derived" if host.get("cpu_utilization") is not None else "unavailable"
        ),
        "host.load_average": (
            "measured" if host.get("load_average") is not None else "unavailable"
        ),
        "host.memory_total_bytes": (
            "measured" if host.get("memory_total_bytes") is not None else "unavailable"
        ),
        "host.memory_available_bytes": (
            "measured" if host.get("memory_available_bytes") is not None else "unavailable"
        ),
    }
    if observed_at is not None or "observed_at" in value:
        sample["observed_at"] = observed_at
    if sample_time is not None:
        sample["sample_time"] = sample_time
    return sample, None


def _session_resource_samples(
    session: Any, snapshot: dict[str, Any], events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Read the newest bounded Store-backed resource sample per session binding."""
    raw_artifacts = snapshot.get("artifacts")
    raw_artifacts = raw_artifacts if isinstance(raw_artifacts, list) else []
    artifact_window = raw_artifacts[-MAX_SESSION_RESOURCE_ARTIFACT_WINDOW:]
    latest_by_binding: dict[str, dict[str, Any]] = {}
    for artifact in reversed(artifact_window):
        if (not isinstance(artifact, dict)
                or artifact.get("kind") != "invocation-result"
                or artifact.get("capability") != SYSTEM_RESOURCE_SAMPLE_CAPABILITY):
            continue
        binding_id = artifact.get("binding_id")
        if (isinstance(binding_id, str) and binding_id and len(binding_id) <= 160
                and binding_id not in latest_by_binding):
            latest_by_binding[binding_id] = artifact
    candidates = list(latest_by_binding.items())
    omitted = max(0, len(candidates) - MAX_SESSION_RESOURCE_SAMPLES)
    candidates = candidates[:MAX_SESSION_RESOURCE_SAMPLES]
    source = {
        "artifact_window_count": len(artifact_window),
        "artifact_window_limit": MAX_SESSION_RESOURCE_ARTIFACT_WINDOW,
        "artifact_window_truncated": len(raw_artifacts) > len(artifact_window),
        "selection_policy": "newest-resource-sample-invocation-per-binding",
        "records_omitted": omitted,
        "observation_identity_digest": digest_hex([], length=16),
    }
    if not candidates:
        return {
            "status": (
                "not-present-in-selected-invocation-artifact-window"
                if source["artifact_window_truncated"]
                else "not-captured-in-session"
            ),
            "observation_count": 0,
            "records_examined": 0,
            "source": source,
            "observations": [],
        }

    binding_records = snapshot.get("bindings")
    binding_records = binding_records if isinstance(binding_records, list) else []
    campaign = snapshot.get("campaign")
    campaign = campaign if isinstance(campaign, dict) else {}
    intent = snapshot.get("intent")
    intent = intent if isinstance(intent, dict) else {}
    provenance = snapshot.get("provenance")
    provenance = provenance if isinstance(provenance, dict) else {}
    context = {
        key: _short_text(value, 160)
        for key, value in {
            "session_id": session.session_id,
            "campaign_id": campaign.get("identity"),
            "work_intent_id": intent.get("identity"),
            "environment_id": snapshot.get("environment_id"),
            "definition_id": provenance.get("definition_id"),
        }.items()
        if isinstance(value, str) and value
    }
    observations: list[dict[str, Any]] = []
    statuses: list[str] = []
    for binding_id, artifact in candidates:
        request_id = artifact.get("provider_request_id")
        if (not isinstance(request_id, str) or not request_id
                or len(request_id) > 160):
            request_id = None
        matching_starts: list[dict[str, Any]] = []
        matching_completions: list[dict[str, Any]] = []
        if isinstance(request_id, str) and request_id:
            for event in events:
                payload = event.get("payload")
                if not isinstance(payload, dict):
                    continue
                if (payload.get("provider_request_id") != request_id
                        or payload.get("capability") != SYSTEM_RESOURCE_SAMPLE_CAPABILITY):
                    continue
                if event.get("type") == "capability.invoked":
                    matching_starts.append(event)
                elif event.get("type") == "invocation.completed":
                    matching_completions.append(event)
        invocation_event = (
            _event_reference(matching_starts[0])
            if len(matching_starts) == 1 else None
        )
        completion_event = (
            _event_reference(matching_completions[0])
            if len(matching_completions) == 1 else None
        )
        correlation_status = "not-present-in-selected-event-window"
        if not isinstance(request_id, str) or not request_id:
            correlation_status = "provider-request-id-unavailable"
        elif len(matching_starts) > 1 or len(matching_completions) > 1:
            correlation_status = "ambiguous-provider-request-id"
        elif invocation_event is not None and completion_event is not None:
            completion_payload = matching_completions[0].get("payload")
            expected_reference = artifact.get("artifact_store_ref")
            invocation_sequence = matching_starts[0].get("sequence")
            completion_sequence = matching_completions[0].get("sequence")
            if (type(invocation_sequence) is int
                    and type(completion_sequence) is int
                    and completion_sequence <= invocation_sequence):
                correlation_status = "completion-precedes-invocation"
            elif (isinstance(expected_reference, str) and expected_reference
                    and isinstance(completion_payload, dict)
                    and completion_payload.get("artifact_store_ref") == expected_reference):
                correlation_status = "matched-by-provider-request-id"
            else:
                correlation_status = "artifact-reference-mismatch"

        binding = next((
            item for item in binding_records
            if isinstance(item, dict) and item.get("binding_id") == binding_id
        ), None)
        projection: dict[str, Any] = {
            "identity": "rsm_" + digest_hex({
                "session_id": session.session_id,
                "provider_request_id": request_id,
                "artifact_store_ref": artifact.get("artifact_store_ref"),
            }, length=16),
            "status": "unavailable",
            "context": dict(context),
            "source": {
                "capability": SYSTEM_RESOURCE_SAMPLE_CAPABILITY,
                "binding_id": binding_id,
                "provider_request_id": request_id,
                "event_correlation_status": correlation_status,
                "invocation_event": invocation_event,
                "completion_event": completion_event,
            },
        }
        if isinstance(binding, dict):
            for key in ("provider", "contract_revision"):
                if isinstance(binding.get(key), str):
                    projection["source"][key] = _short_text(binding[key], 128)
        if isinstance(artifact.get("at"), str):
            projection["source"]["invocation_recorded_at"] = _short_text(
                artifact["at"], 64,
            )
        if artifact.get("status") != "ok":
            projection["status"] = "latest-invocation-unsuccessful"
            statuses.append(projection["status"])
            observations.append(projection)
            continue
        try:
            metadata, content = invocation_artifacts_module.read_latest_invocation_file(
                session,
                SYSTEM_RESOURCE_SAMPLE_CAPABILITY,
                binding_id,
                SYSTEM_RESOURCE_SAMPLE_PATH,
                max_bytes=MAX_RESOURCE_SAMPLE_BYTES,
            )
        except invocation_artifacts_module.InvocationArtifactError:
            projection["status"] = "resource-artifact-unavailable"
            statuses.append(projection["status"])
            observations.append(projection)
            continue
        except StoreIntegrityFailure:
            projection["status"] = "resource-artifact-integrity-failure"
            statuses.append(projection["status"])
            observations.append(projection)
            continue
        except (StoreUnavailable, LifecycleError, OSError, ValueError):
            projection["status"] = "resource-artifact-unavailable"
            statuses.append(projection["status"])
            observations.append(projection)
            continue
        try:
            decoded = json.loads(content)
        except (UnicodeDecodeError, ValueError):
            decoded = None
        sample, diagnostic = _resource_sample_payload(decoded)
        projection["source"]["artifact_reference"] = {
            "store_reference": metadata["store_reference"],
            "path": metadata["path"],
            "byte_count": metadata["byte_count"],
            "sha256": metadata["sha256"],
        }
        if sample is None:
            projection["status"] = diagnostic or "resource-sample-invalid"
        else:
            projection["status"] = "available"
            projection["sample"] = sample
        statuses.append(projection["status"])
        observations.append(projection)

    available = sum(item.get("status") == "available" for item in observations)
    source["observation_identity_digest"] = digest_hex(
        [item["identity"] for item in observations], length=16,
    )
    if available == len(observations) and not omitted:
        status = "available"
    elif available:
        status = "partial"
    elif len(observations) == 1:
        status = statuses[0]
    else:
        status = "partial"
    return {
        "status": status,
        "observation_count": available,
        "records_examined": len(observations),
        "source": source,
        "observations": observations,
    }


def _session_activity(
    session: Any, snapshot: dict[str, Any], *,
    event_before_sequence: int | None = None,
    event_limit: int = MAX_SESSION_EVENT_WINDOW,
) -> dict[str, Any]:
    """Project bounded capability and authenticated Control read activity."""
    reader = getattr(session, "recent_events", None)
    limits = [
        "Capability interactions cover Environment-bound invocations; direct external tools are outside this session event view.",
        "Control read-operation events contain bounded operation and outcome fields, never raw arguments or responses.",
        (
            "Control read-operation repository branch/head fields come from the authenticated "
            "session-selected checkout; this projection does not re-probe Git at each operation."
        ),
        "Model identity, token usage, execution duration, and resource consumption are not inferred when the runtime does not expose them.",
        "The Store page reader verifies only selected event payloads; finding their bindings still scans the generation's metadata index.",
        "Resource samples are bounded aggregate host snapshots linked to Environment invocations; they do not measure agent-attributed consumption or establish pressure by themselves.",
    ]
    if not callable(reader):
        resource_samples = _session_resource_samples(session, snapshot, [])
        return {
            "status": "unavailable",
            "context": {},
            "source": {
                "event_window_limit": event_limit,
                "event_page_before_sequence": event_before_sequence,
                "resource_sample_identity_digest": resource_samples["source"][
                    "observation_identity_digest"
                ],
            },
            "interaction_count": 0,
            "interactions": [],
            "rpc_operation_count": 0,
            "rpc_operations": [],
            "rpc_failure_candidates": [],
            "capabilities": [],
            "resource_samples": resource_samples,
            "measurement_limits": [
                *limits,
                "The session event page interface is unavailable.",
            ],
        }
    try:
        if event_before_sequence is None:
            page = reader(limit=event_limit)
        else:
            page = reader(
                limit=event_limit, before_sequence=event_before_sequence,
            )
    except (LifecycleError, StoreIntegrityFailure, StoreUnavailable, OSError, ValueError):
        page = None
    if (not isinstance(page, dict)
            or type(page.get("event_count")) is not int
            or page["event_count"] < 0
            or not isinstance(page.get("events"), list)
            or type(page.get("truncated")) is not bool):
        resource_samples = _session_resource_samples(session, snapshot, [])
        return {
            "status": "unavailable",
            "context": {},
            "source": {
                "event_window_limit": event_limit,
                "event_page_before_sequence": event_before_sequence,
                "resource_sample_identity_digest": resource_samples["source"][
                    "observation_identity_digest"
                ],
            },
            "interaction_count": 0,
            "interactions": [],
            "capabilities": [],
            "resource_samples": resource_samples,
            "measurement_limits": [
                *limits,
                "The session event tail could not be read or did not match its declared shape.",
            ],
        }

    raw_page_events = page["events"]
    adapter_window_overflow = len(raw_page_events) > event_limit
    if adapter_window_overflow:
        raw_page_events = raw_page_events[-event_limit:]
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
    rpc_events = [
        event for event in events
        if event.get("type") == "environment.rpc.read_observed"
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

    rpc_operations: list[dict[str, Any]] = []
    rpc_operation_groups: dict[tuple[str, str], dict[str, Any]] = {}
    invalid_rpc_event_count = 0
    valid_rpc_event_count = 0
    for event in reversed(rpc_events):
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        reference = _event_reference(event)
        operation = payload.get("operation")
        outcome = payload.get("outcome_status")
        caller = payload.get("caller")
        context_value = payload.get("context")
        if (reference is None or not _authenticated_rpc_read_event(event, snapshot)
                or not isinstance(operation, str)
                or not isinstance(caller, dict)
                or not isinstance(context_value, dict)):
            invalid_rpc_event_count += 1
            continue
        valid_rpc_event_count += 1
        group_key = (operation, outcome)
        summary_group = rpc_operation_groups.setdefault(
            group_key,
            {
                "operation": operation,
                "outcome_status": outcome,
                "occurrence_count": 0,
                "supporting_events": [],
            },
        )
        summary_group["occurrence_count"] += 1
        if (len(summary_group["supporting_events"])
                < MAX_SESSION_ACTIVITY_RPC_SUMMARY_REFERENCES):
            summary_group["supporting_events"].append(reference)
        if len(rpc_operations) >= MAX_SESSION_ACTIVITY_RPC_OPERATIONS:
            continue
        caller_projection = {
            key: _short_text(caller.get(key), 128)
            for key in (
                "authenticated_principal_id", "control_grant_id",
                "process_identity",
            )
            if isinstance(caller.get(key), str) and caller.get(key)
        }
        caller_projection["process_identity_capture_status"] = (
            "captured" if caller_projection.get("process_identity")
            else "unavailable"
        )
        context_projection = {
            key: _short_text(context_value.get(key), 160)
            for key in (
                "repository", "repository_branch", "repository_head",
                "environment_id", "campaign_id", "work_intent_id",
            )
            if isinstance(context_value.get(key), str) and context_value.get(key)
        }
        item: dict[str, Any] = {
            "identity": "rpc_" + digest_hex({
                "session_id": session.session_id,
                "event": reference["identity"],
            }, length=16),
            "operation": operation,
            "outcome_status": outcome,
            "event_reference": reference,
            "caller": caller_projection,
            "context": context_projection,
        }
        if type(payload.get("exit_code")) is int:
            item["exit_code"] = payload["exit_code"]
        failure_code = payload.get("failure_code")
        if (outcome == "failed" and isinstance(failure_code, str)
                and re.fullmatch(r"[A-Za-z0-9._-]{1,96}", failure_code)):
            item["failure_code"] = failure_code
        if (type(payload.get("observation_ordinal")) is int
                and 1 <= payload["observation_ordinal"]
                <= MAX_RPC_READ_ACTIVITY_OBSERVATIONS):
            item["observation_ordinal"] = payload["observation_ordinal"]
        measurement_availability = payload.get("measurement_availability")
        if isinstance(measurement_availability, dict):
            item["measurement_availability"] = {
                key: value
                for key, value in measurement_availability.items()
                if key in {
                    "model_identity", "token_usage",
                    "control_dispatch_elapsed_ms", "resource_consumption",
                    "cache_accounting",
                }
                and value in {"measured", "derived", "unavailable"}
            }
        measured_values = payload.get("measurements")
        if isinstance(measured_values, dict):
            elapsed = measured_values.get("control_dispatch_elapsed_ms")
            if (type(elapsed) in {int, float} and 0 <= elapsed <= 600_000):
                item["measurements"] = {"control_dispatch_elapsed_ms": elapsed}
        rpc_operations.append(item)

    rpc_operations_omitted = max(0, valid_rpc_event_count - len(rpc_operations))
    ordered_rpc_groups = [
        {
            **group,
            "supporting_events": sorted(
                group["supporting_events"],
                key=lambda item: item["sequence"],
            ),
        }
        for _, group in sorted(rpc_operation_groups.items())
    ]
    rpc_summary_groups = ordered_rpc_groups[:MAX_SESSION_ACTIVITY_RPC_SUMMARY_GROUPS]
    rpc_summary_omitted_groups = ordered_rpc_groups[
        MAX_SESSION_ACTIVITY_RPC_SUMMARY_GROUPS:
    ]
    rpc_operation_summary = {
        "scope": "authenticated-records-in-selected-event-window",
        "record_count": valid_rpc_event_count,
        "group_count": len(ordered_rpc_groups),
        "groups_omitted": len(rpc_summary_omitted_groups),
        "omitted_record_count": sum(
            group["occurrence_count"] for group in rpc_summary_omitted_groups
        ),
        "supporting_event_limit_per_group": MAX_SESSION_ACTIVITY_RPC_SUMMARY_REFERENCES,
        "groups": rpc_summary_groups,
    }
    failures_by_identity: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in rpc_operations:
        if item.get("outcome_status") != "failed":
            continue
        key = (
            str(item.get("operation", "unknown")),
            str(item.get("failure_code", "failure-code-unavailable")),
        )
        failures_by_identity.setdefault(key, []).append(item)
    rpc_failure_candidates = []
    for (operation, failure_code), group in sorted(failures_by_identity.items()):
        if len(group) < 2:
            continue
        rpc_failure_candidates.append({
            "identity": "ph_rpc_" + digest_hex({
                "session_id": session.session_id,
                "operation": operation,
                "failure_code": failure_code,
            }, length=16),
            "status": "candidate-needs-investigation",
            "subsystem": "environment-rpc",
            "operation": operation,
            "failure_code": failure_code,
            "occurrence_count": len(group),
            "distinct_session_count": 1,
            "recurrence_scope": "same-session-selected-event-window",
            "interpretation": (
                "The same operation and failure code appeared repeatedly in this "
                "session's selected event window. Cross-session recurrence and "
                "cause are not established."
            ),
            "causal_claim": "not-established",
            "supporting_events": [
                item["event_reference"] for item in sorted(
                    group,
                    key=lambda item: item["event_reference"]["sequence"],
                )[:8]
            ],
        })
        if len(rpc_failure_candidates) >= MAX_SESSION_ACTIVITY_RPC_PRESSURE_CANDIDATES:
            break

    event_count = page["event_count"]
    event_window_truncated = (
        page["truncated"]
        or (event_before_sequence is None and event_count > len(page["events"]))
        or adapter_window_overflow
    )
    invocations_omitted = max(0, len(invocation_events) - len(interactions))
    rpc_capture_limit_reached = (
        snapshot.get("rpc_read_activity_limit_reached") is True
    )
    partial = (
        event_window_truncated or event_before_sequence is not None
        or invocations_omitted > 0 or rpc_operations_omitted > 0
        or rpc_capture_limit_reached
    )
    if partial and not invocation_events:
        status = (
            "partial-rpc-only-in-selected-window"
            if rpc_operations else "partial-no-invocations-in-selected-window"
        )
    elif partial:
        status = "partial"
    elif not invocation_events:
        status = (
            "rpc-only-in-session-history"
            if rpc_operations else "no-capability-invocations-in-session-history"
        )
    else:
        status = "ready"
    window_references = [
        reference for event in events
        if (reference := _event_reference(event)) is not None
    ]
    next_event_before_sequence = page.get("next_before_sequence")
    if (type(next_event_before_sequence) is not int
            or next_event_before_sequence < 1
            or (event_before_sequence is not None
                and next_event_before_sequence >= event_before_sequence)):
        next_event_before_sequence = (
            min(reference["sequence"] for reference in window_references)
            if event_window_truncated and window_references else None
        )
    if not event_window_truncated:
        next_event_before_sequence = None
    event_type_counts: dict[str, int] = {}
    for event in events:
        event_type = event.get("type")
        if isinstance(event_type, str) and event_type and len(event_type) <= 96:
            event_type_counts[event_type] = event_type_counts.get(event_type, 0) + 1
    denial_events = [
        event for event in events if event.get("type") == "authority.denied"
    ]
    denial_records: list[dict[str, Any]] = []
    snapshot_facts = authority_module.repository_facts_for_snapshot(snapshot)
    invalid_denial_event_count = 0
    for event in reversed(denial_events):
        reference = _event_reference(event)
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        action = payload.get("action")
        reason = payload.get("reason")
        if reference is None:
            invalid_denial_event_count += 1
            continue
        if not isinstance(action, str) or action not in authority_module.ACTIONS:
            action = "action-unavailable"
        target = payload.get("target")
        repository = (
            authority_module.repository_for_target(target, snapshot_facts)
            if isinstance(target, str) and target else None
        )
        denial_records.append({
            "identity": "authdeny_" + digest_hex({
                "session_id": session.session_id,
                "event": reference["identity"],
            }, length=16),
            "action": action,
            "outcome_status": "denied",
            "reason_class": _authority_denial_reason_class(reason),
            **({"repository": repository} if repository else {}),
            "event_reference": reference,
        })
    authority_denials = denial_records[:MAX_SESSION_ACTIVITY_AUTHORITY_DENIALS]
    authority_denials_omitted = max(0, len(denial_records) - len(authority_denials))
    denial_groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for item in denial_records:
        key = (
            str(item.get("repository") or "repository-unavailable"),
            str(item.get("action") or "action-unavailable"),
            str(item.get("reason_class") or "reason-unavailable"),
        )
        denial_groups.setdefault(key, []).append(item)
    authority_denial_candidates = []
    for (repository, action, reason_class), group in sorted(denial_groups.items()):
        if len(group) < 2:
            continue
        candidate = {
            "identity": "ph_authority_" + digest_hex({
                "session_id": session.session_id,
                "repository": repository,
                "action": action,
                "reason_class": reason_class,
            }, length=16),
            "status": "candidate-needs-investigation",
            "subsystem": "authority-and-work-scope",
            "action": action,
            "reason_class": reason_class,
            "occurrence_count": len(group),
            "distinct_session_count": 1,
            "recurrence_scope": "same-session-selected-event-window",
            "causal_claim": "not-established",
            "interpretation": (
                "This denial pattern recurred in one selected session window. "
                "The intended scope, appropriateness of each denial, and cause "
                "remain unestablished."
            ),
            "supporting_events": [
                item["event_reference"] for item in sorted(
                    group,
                    key=lambda item: item["event_reference"]["sequence"],
                )[:8]
            ],
        }
        if repository != "repository-unavailable":
            candidate["repository"] = repository
        authority_denial_candidates.append(candidate)
        if len(authority_denial_candidates) >= (
            MAX_SESSION_ACTIVITY_AUTHORITY_PRESSURE_CANDIDATES
        ):
            break
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
    resource_samples = _session_resource_samples(session, snapshot, events)
    return {
        "status": status,
        "context": context,
        "resource_samples": resource_samples,
        "source": {
            "event_count": event_count,
            "event_window_limit": event_limit,
            "event_page_before_sequence": event_before_sequence,
            "event_page_scoped": event_before_sequence is not None,
            "next_event_before_sequence": next_event_before_sequence,
            "event_page_has_older_records": event_window_truncated,
            "events_considered": len(raw_page_events),
            "event_window_truncated": event_window_truncated,
            "event_window_identity_digest": digest_hex(window_references, length=16),
            "resource_sample_identity_digest": resource_samples["source"][
                "observation_identity_digest"
            ],
            "event_window_sequence_start": (
                window_references[0]["sequence"] if window_references else None
            ),
            "event_window_sequence_end": (
                window_references[-1]["sequence"] if window_references else None
            ),
            "event_type_counts": event_type_counts,
            "authority_denial_events_considered": len(denial_events),
            "authority_denial_records_returned": len(authority_denials),
            "authority_denial_records_omitted": authority_denials_omitted,
            "authority_denial_records_invalid": invalid_denial_event_count,
            "authority_denial_selection_policy": (
                "newest-sanitized-denials-in-selected-event-window"
            ),
            "invocation_events_considered": len(invocation_events),
            "completion_events_considered": len(completion_events),
            "invocation_records_omitted": invocations_omitted,
            "capability_summaries_omitted": capabilities_omitted,
            "selection_policy": "newest-invocation-events-by-session-sequence",
            "rpc_operation_events_considered": len(rpc_events),
            "rpc_operation_records_returned": len(rpc_operations),
            "rpc_operation_records_omitted": rpc_operations_omitted,
            "rpc_operation_records_invalid": invalid_rpc_event_count,
            "rpc_operation_capture_count": (
                snapshot.get("rpc_read_activity_count")
                if type(snapshot.get("rpc_read_activity_count")) is int
                and 0 <= snapshot["rpc_read_activity_count"]
                <= MAX_RPC_READ_ACTIVITY_OBSERVATIONS else None
            ),
            "rpc_operation_capture_limit": MAX_RPC_READ_ACTIVITY_OBSERVATIONS,
            "rpc_operation_capture_limit_reached": (
                snapshot.get("rpc_read_activity_limit_reached") is True
            ),
            "rpc_operation_selection_policy": "newest-authenticated-read-operations-in-selected-event-window",
            "rpc_operation_context_basis": {
                "repository_identity": "control-project-grant",
                "repository_branch_and_head": "session-selected-checkout",
                "environment_campaign_and_work_intent": "session-snapshot",
                "live_repository_probe": False,
            },
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
        "rpc_operation_count": len(rpc_operations),
        "rpc_operations": rpc_operations,
        **({"rpc_operation_summary": rpc_operation_summary} if rpc_events else {}),
        "rpc_failure_candidates": rpc_failure_candidates,
        "authority_denial_count": len(authority_denials),
        "authority_denials": authority_denials,
        "authority_denial_candidates": authority_denial_candidates,
        "capabilities": capabilities,
        "measurement_limits": limits,
    }


def project(
    session: Any, *, campaign_state: dict[str, Any] | None = None,
    max_result_bytes: int = MAX_RESULT_BYTES, history_pages: int = 1,
    evidence_offset: int | None = None,
    evidence_limit: int = MAX_EVIDENCE_REFS,
    session_event_before_sequence: int | None = None,
    session_event_limit: int = MAX_SESSION_EVENT_WINDOW,
) -> dict[str, Any]:
    """Build a bounded read-only relationship view from campaign evidence.

    Environmental observations under ``evidence/environmental-capability/``
    and one allowlisted Compiler regression schema are read. Their exact Git
    commit and SHA-256 must be present in the authenticated campaign ledger,
    and the repository must be selected by this Environment session.
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
    if (type(session_event_before_sequence) not in (int, type(None))
            or (session_event_before_sequence is not None
                and session_event_before_sequence < 1)):
        raise ValueError("capability projection session event cursor must be positive")
    if (type(session_event_limit) is not int
            or not 1 <= session_event_limit <= MAX_SESSION_EVENT_WINDOW):
        raise ValueError(
            "capability projection session event limit must be between 1 and "
            f"{MAX_SESSION_EVENT_WINDOW}"
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
    activity = _session_activity(
        session, snapshot,
        event_before_sequence=session_event_before_sequence,
        event_limit=session_event_limit,
    )
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
    capacity_seen: set[tuple[str, str, str, str, str]] = set()
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
                pointer["identity"], pointer["repository"], pointer["commit"],
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
                pointer["identity"], pointer["repository"], pointer["commit"], pointer["path"],
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
    seen_observations: set[tuple[str, str, str, str, str]] = set()
    unavailable = 0
    skipped_by_page_budget = 0
    read_deadline = time.monotonic() + MAX_OBSERVATION_READ_SECONDS
    campaign_charter = _campaign_charter_context(
        session, state, root,
        campaign_identity=campaign.get("identity"),
        deadline=read_deadline,
    )
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
            pointer["identity"], pointer["repository"], pointer["commit"],
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
        if (verification == "observation-identity-mismatch"
                and isinstance(record, dict)
                and isinstance(record.get("identity"), str)
                and record["identity"]):
            reference["record_identity"] = record["identity"][:160]
        references.append(reference)
        if record is None or verification != "verified":
            unavailable += 1
            continue
        verified_records.append((record, reference))
    observations: list[dict[str, Any]] = []
    improvement_relations: list[dict[str, Any]] = []
    projected_records = verified_records[:MAX_OBSERVATIONS]
    pointer_correction_status_by_record: dict[str, dict[str, Any]] = {}
    pointer_correction_candidates: dict[
        str, list[tuple[dict[str, Any], dict[str, Any], int, int]]
    ] = {}
    pointer_correction_unverified_count = 0
    pointer_correction_read_count = 0
    reference_positions = {
        reference.get("identity"): position
        for position, reference in enumerate(references)
        if isinstance(reference.get("identity"), str)
    }
    references_by_identity = {
        reference["identity"]: reference
        for reference in references
        if isinstance(reference.get("identity"), str)
    }
    for correction_record, _correction_record_reference in projected_records:
        raw_pointer_correction = correction_record.get(
            "evidence_reference_correction",
        )
        if raw_pointer_correction is None:
            continue
        correction = _validate_evidence_reference_correction(
            raw_pointer_correction,
        )
        if correction is None:
            pointer_correction_status_by_record[correction_record["identity"]] = {
                "status": "recorded-but-contract-unverified",
            }
            pointer_correction_unverified_count += 1
            continue
        target = correction["target_reference"]
        target_identity = target["identity"]
        selected_target = references_by_identity.get(target_identity)
        if selected_target is None:
            status = "target-not-present-in-selected-pages"
        elif any(selected_target.get(key) != target.get(key) for key in (
            "identity", "repository", "commit", "path", "sha256",
        )):
            status = "target-reference-mismatch"
        elif selected_target.get("verification") == (
            "not-read-page-observation-budget"
        ):
            status = "target-not-read-page-budget"
        elif selected_target.get("verification") == "verified":
            status = "target-already-verified"
        else:
            status = None
        if status is not None:
            pointer_correction_status_by_record[correction_record["identity"]] = {
                "status": status,
                "target_identity": target_identity,
                "target_reference": target,
            }
            pointer_correction_unverified_count += 1
            continue
        target_position = reference_positions.get(target_identity)
        correction_position = reference_positions.get(correction_record["identity"])
        if (target_position is None or correction_position is None
                or target_position >= correction_position):
            pointer_correction_status_by_record[correction_record["identity"]] = {
                "status": "target-not-prior-to-correction",
                "target_identity": target_identity,
                "target_reference": target,
            }
            pointer_correction_unverified_count += 1
            continue
        pointer_correction_candidates.setdefault(target_identity, []).append((
            correction_record, correction, target_position, correction_position,
        ))

    recovered_records: list[tuple[dict[str, Any], dict[str, Any]]] = []
    pointer_corrections_applied_count = 0
    for target_identity, candidates in pointer_correction_candidates.items():
        if len(candidates) != 1:
            pointer_correction_unverified_count += len(candidates)
            for correction_record, _correction, _target_position, _ in candidates:
                pointer_correction_status_by_record[correction_record["identity"]] = {
                    "status": "conflicting-corrections-for-target",
                    "target_identity": target_identity,
                }
            continue
        correction_record, correction, target_position, _ = candidates[0]
        if pointer_correction_read_count >= MAX_EVIDENCE_REFERENCE_CORRECTIONS:
            pointer_correction_status_by_record[correction_record["identity"]] = {
                "status": "correction-read-limit-reached",
                "target_identity": target_identity,
            }
            pointer_correction_unverified_count += 1
            continue
        pointer_correction_read_count += 1
        replacement = correction["replacement_reference"]
        recovered_record, replacement_status = _load_observation(
            session, replacement, root, deadline=read_deadline,
        ) if root is not None else (None, "workspace-unavailable")
        if recovered_record is None or replacement_status != "verified":
            pointer_correction_status_by_record[correction_record["identity"]] = {
                "status": "replacement-reference-not-verified",
                "target_identity": target_identity,
                "target_reference": correction["target_reference"],
                "replacement_reference": replacement,
                "replacement_verification": replacement_status,
            }
            pointer_correction_unverified_count += 1
            continue
        old_reference = dict(references[target_position])
        corrected_reference = {
            **replacement,
            "verification": "verified",
            "corrected_from_reference": {
                key: old_reference[key]
                for key in ("identity", "repository", "commit", "path", "sha256")
                if old_reference.get(key) is not None
            },
            "correction_observation_identity": correction_record["identity"],
        }
        references[target_position] = corrected_reference
        references_by_identity[target_identity] = corrected_reference
        unavailable = max(0, unavailable - 1)
        recovered_records.append((recovered_record, corrected_reference))
        pointer_correction_status_by_record[correction_record["identity"]] = {
            "status": "applied-to-exact-unavailable-target",
            "target_identity": target_identity,
            "target_reference": correction["target_reference"],
            "replacement_reference": replacement,
            "reason": correction["reason"],
        }
        pointer_corrections_applied_count += 1
    projected_records = (projected_records + recovered_records)[:MAX_OBSERVATIONS]
    observation_reference_diagnostics = []
    for reference in references:
        if not _is_observation_reference(reference):
            continue
        if reference.get("verification") in {
            "verified", "duplicate-reference", "not-read-page-observation-budget",
        }:
            continue
        path = reference["path"]
        observation_reference_diagnostics.append({
            key: reference[key]
            for key in (
                "identity", "record_identity", "repository", "commit",
                "sha256", "verification",
            )
            if reference.get(key) is not None
        } | {
            "path": path[:512],
            "path_truncated": len(path) > 512,
        })
    source_integrity: dict[tuple[str, str, str, str], str] = {}
    attempted_sources: set[tuple[str, str, str, str]] = set()
    workspace_checkouts: dict[str, Path | None] = {}
    for record, _ in projected_records:
        raw_sources = record.get("supporting_sources")
        raw_sources = raw_sources if isinstance(raw_sources, list) else []
        for source in raw_sources[:16]:
            _verify_source_pointer(
                session, source, references, root, deadline=read_deadline,
                cache=source_integrity, attempted=attempted_sources,
                workspace_checkouts=workspace_checkouts,
            )
        improvement = _validate_improvement_relation(record.get("improvement_relation"))
        if improvement is not None:
            _verify_source_pointer(
                session, improvement["implementation_reference"], references, root,
                deadline=read_deadline, cache=source_integrity,
                attempted=attempted_sources,
                workspace_checkouts=workspace_checkouts,
            )
        correction = _validate_observation_correction(
            record.get("record_correction"),
        )
        if correction is not None:
            _verify_source_pointer(
                session, correction["target_reference"], references, root,
                deadline=read_deadline, cache=source_integrity,
                attempted=attempted_sources,
                workspace_checkouts=workspace_checkouts,
            )
            for source in correction["replacement_values"].get(
                "supporting_sources", [],
            ):
                _verify_source_pointer(
                    session, source, references, root,
                    deadline=read_deadline, cache=source_integrity,
                    attempted=attempted_sources,
                    workspace_checkouts=workspace_checkouts,
                )
    projected_records_by_identity = {
        record["identity"]: record for record, _ in projected_records
    }
    page_references_by_identity = {
        reference["identity"]: reference
        for reference in references
        if isinstance(reference.get("identity"), str)
    }
    page_reference_positions = {
        reference["identity"]: index
        for index, reference in enumerate(references)
        if isinstance(reference.get("identity"), str)
    }
    correction_candidates: dict[
        str, list[tuple[dict[str, Any], dict[str, Any], dict[str, str]]],
    ] = {}
    correction_status_by_record: dict[str, dict[str, Any]] = {}
    unverified_record_correction_count = 0
    for correction_record, correction_reference in projected_records:
        raw_correction = correction_record.get("record_correction")
        if raw_correction is None:
            continue
        correction = _validate_observation_correction(raw_correction)
        if correction is None:
            correction_status_by_record[correction_record["identity"]] = {
                "status": "recorded-but-contract-unverified",
            }
            unverified_record_correction_count += 1
            continue
        target_reference = correction["target_reference"]
        target_identity = target_reference["identity"]
        selected_reference = page_references_by_identity.get(target_identity)
        if selected_reference is None:
            correction_status_by_record[correction_record["identity"]] = {
                "status": "target-not-present-in-selected-pages",
                "target_identity": target_identity,
            }
            unverified_record_correction_count += 1
            continue
        pointer_fields = ("identity", "repository", "commit", "path", "sha256")
        if any(
            selected_reference.get(key) != target_reference.get(key)
            for key in pointer_fields
        ):
            correction_status_by_record[correction_record["identity"]] = {
                "status": "target-reference-mismatch",
                "target_identity": target_identity,
                "target_reference": target_reference,
            }
            unverified_record_correction_count += 1
            continue
        verification = selected_reference.get("verification")
        if verification == "not-read-page-observation-budget":
            correction_status_by_record[correction_record["identity"]] = {
                "status": "target-not-read-page-budget",
                "target_identity": target_identity,
                "target_reference": target_reference,
            }
            unverified_record_correction_count += 1
            continue
        if verification != "verified":
            correction_status_by_record[correction_record["identity"]] = {
                "status": "target-not-verified-in-projection",
                "target_identity": target_identity,
                "target_reference": target_reference,
                "target_verification": verification,
            }
            unverified_record_correction_count += 1
            continue
        correction_position = page_reference_positions.get(
            correction_record["identity"],
        )
        target_position = page_reference_positions.get(target_identity)
        if (correction_position is not None and target_position is not None
                and target_position >= correction_position):
            correction_status_by_record[correction_record["identity"]] = {
                "status": "target-not-prior-to-correction",
                "target_identity": target_identity,
                "target_reference": target_reference,
            }
            unverified_record_correction_count += 1
            continue
        target_record = projected_records_by_identity.get(target_identity)
        if target_record is None:
            correction_status_by_record[correction_record["identity"]] = {
                "status": "target-not-projected",
                "target_identity": target_identity,
                "target_reference": target_reference,
            }
            unverified_record_correction_count += 1
            continue
        target_integrity = source_integrity.get(
            _source_pointer_key(target_reference), "not-checked",
        )
        if target_integrity not in SOURCE_INTEGRITY_VERIFIED:
            correction_status_by_record[correction_record["identity"]] = {
                "status": "target-content-integrity-unverified",
                "target_identity": target_identity,
                "target_reference": target_reference,
                "target_content_integrity_status": target_integrity,
            }
            unverified_record_correction_count += 1
            continue
        replacements_verified = True
        replacement_values = correction["replacement_values"]
        for field in correction["corrected_field_paths"]:
            replacement = replacement_values[field]
            if target_record.get(field) == replacement:
                replacements_verified = False
                break
            if field != "supporting_sources":
                continue
            replacement_sources = replacement
            replacement_sources_valid = (
                isinstance(replacement_sources, list)
                and 0 < len(replacement_sources) <= 16
                and all(
                    (pointer := _validate_source_pointer(source)) is not None
                    and "sha256" in pointer
                    and _source_status(source, references).get("status") in {
                        "verified", "not-an-environmental-observation",
                    }
                    and _source_integrity_status(source, source_integrity)
                    in SOURCE_INTEGRITY_VERIFIED
                    for source in replacement_sources
                )
            )
            if not replacement_sources_valid:
                replacements_verified = False
                break
        if not replacements_verified:
            correction_status_by_record[correction_record["identity"]] = {
                "status": "replacement-values-unverified-or-unchanged",
                "target_identity": target_identity,
                "target_reference": target_reference,
            }
            unverified_record_correction_count += 1
            continue
        correction_status_by_record[correction_record["identity"]] = {
            "status": "applied-to-verified-target",
            "target_identity": target_identity,
            "target_reference": target_reference,
            "corrected_field_paths": correction["corrected_field_paths"],
            "replacement_values": replacement_values,
            "reason": correction["reason"],
        }
        correction_candidates.setdefault(target_identity, []).append((
            correction_record, correction, correction_reference,
        ))

    correction_by_target: dict[str, tuple[dict[str, Any], dict[str, Any], dict[str, str]]] = {}
    for target_identity, candidates in correction_candidates.items():
        if len(candidates) == 1:
            correction_by_target[target_identity] = candidates[0]
            continue
        unverified_record_correction_count += len(candidates)
        for correction_record, _correction, _reference in candidates:
            correction_status_by_record[correction_record["identity"]] = {
                "status": "conflicting-corrections-for-target",
                "target_identity": target_identity,
            }

    unverified_improvement_relation_count = sum(
        record.get("improvement_relation") is not None
        and _validate_improvement_relation(record.get("improvement_relation")) is None
        for record, _ in projected_records
    )
    source_context_candidates: dict[
        tuple[str, str, str, str, str], set[tuple[tuple[str, str], ...]],
    ] = {}
    for source_record, _ in projected_records:
        work = source_record.get("related_work")
        work = work if isinstance(work, dict) else {}
        supporting_sources = source_record.get("supporting_sources")
        if not isinstance(supporting_sources, list):
            continue
        campaign_id = work.get("campaign_id")
        session_id = work.get("session_id")
        if (not isinstance(campaign_id, str) or not campaign_id
                or not isinstance(session_id, str) or not session_id):
            continue
        for source in supporting_sources[:16]:
            pointer = _validate_source_pointer(source)
            if (pointer is None or pointer.get("repository") != "mncs-compiler"
                    or not pointer.get("identity")
                    or "sha256" not in pointer
                    or _source_integrity_status(source, source_integrity)
                    not in SOURCE_INTEGRITY_VERIFIED
                    or _source_status(source, references).get("status") != "verified"):
                continue
            source_key = tuple(
                pointer.get(key, "")
                for key in ("identity", "repository", "commit", "path", "sha256")
            )
            if not any(
                tuple(reference.get(key, "") for key in (
                    "identity", "repository", "commit", "path", "sha256",
                )) == source_key and reference.get("verification") == "verified"
                for reference in references
            ):
                continue
            context = {
                "campaign_id": campaign_id,
                "session_id": session_id,
                "repository": "mncs-compiler",
                "repository_context_basis": "verified-supporting-source-pointer",
            }
            for key in (
                "work_intent_id", "work_intent_goal", "environment_id",
                "definition_id",
            ):
                value = work.get(key)
                if isinstance(value, str) and value:
                    context[key] = value
            source_context_candidates.setdefault(source_key, set()).add(
                tuple(sorted(context.items()))
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
        source_session_context = observation.get("source_session_context")
        source_session_context = (
            source_session_context if isinstance(source_session_context, dict) else None
        )
        source_session_context_fields = (
            (
                "campaign_id", "session_id", "work_intent_id", "work_intent_goal",
                "environment_id", "repository", "repository_branch", "repository_head",
                "repository_context_basis",
            )
            if source_session_context is not None else ()
        )
        source_session_context_truncated = any(
            isinstance(source_session_context.get(key), str)
            and len(source_session_context[key]) > 512
            for key in source_session_context_fields
        ) if source_session_context is not None else False
        sources = record.get("supporting_sources")
        sources = sources if isinstance(sources, list) else []
        raw_improvement = _validate_improvement_relation(
            record.get("improvement_relation"),
        )
        correction_for_target = correction_by_target.get(record["identity"])
        record_correction_summary = correction_status_by_record.get(
            record["identity"],
        )
        effective_sources = sources
        effective_measurement_limits = record.get("not_measured", [])
        if correction_for_target is not None:
            correction_record, correction, correction_reference = correction_for_target
            corrected_fields = correction["corrected_field_paths"]
            replacement_values = correction["replacement_values"]
            if "supporting_sources" in corrected_fields:
                effective_sources = replacement_values["supporting_sources"]
            if "not_measured" in corrected_fields:
                effective_measurement_limits = replacement_values["not_measured"]
            if record_correction_summary is None:
                record_correction_summary = {
                    "status": "corrected-by-verified-record",
                    "correction_observation_identity": correction_record["identity"],
                    "corrected_field_paths": corrected_fields,
                    "replacement_values": replacement_values,
                    "reason": correction["reason"],
                    "correction_evidence_reference": {
                        key: correction_reference[key]
                        for key in ("identity", "repository", "commit", "path", "sha256")
                        if correction_reference.get(key) is not None
                    },
                }
        improvement = raw_improvement
        projected_observation = {
            "kind": observation.get("kind"),
            "source_interface": observation.get("source_interface"),
            "request": observation.get("request"),
        }
        source_context_resolution = observation.get(
            "source_session_context_resolution",
        )
        if isinstance(source_context_resolution, str) and source_context_resolution:
            projected_observation["source_session_context_resolution"] = (
                source_context_resolution[:96]
            )
        if _is_compiler_regression_reference(reference):
            pointer_key = tuple(
                reference.get(key, "")
                for key in ("identity", "repository", "commit", "path", "sha256")
            )
            contexts = source_context_candidates.get(pointer_key, set())
            if len(contexts) == 1:
                projected_observation["source_session_context"] = dict(
                    next(iter(contexts))
                )
                projected_observation["source_session_context_basis"] = (
                    "verified-supporting-observation"
                )
                projected_observation["source_session_context_resolution"] = (
                    "verified-exact-source-pointer"
                )
            elif len(contexts) > 1:
                projected_observation["source_session_context_resolution"] = (
                    "ambiguous-supporting-observations"
                )
            else:
                projected_observation["source_session_context_resolution"] = (
                    "no-matching-verified-supporting-observation"
                )
        if isinstance(observation.get("outcome"), dict):
            projected_observation["outcome"] = observation["outcome"]
        projection_summary = observation.get("projection_summary")
        if isinstance(projection_summary, dict):
            summary_fields = (
                "status", "session_activity_status", "activity_interpretation",
                "rpc_operation_event_count", "rpc_operation_records_returned",
                "rpc_operation_capture_count", "rpc_failure_candidate_count",
            )
            projected_summary = {}
            for key in summary_fields:
                value = projection_summary.get(key)
                if isinstance(value, str) and value:
                    projected_summary[key] = value[:512]
                elif type(value) is int and value >= 0:
                    projected_summary[key] = value
            projected_observation["projection_summary"] = projected_summary
        raw_operations = observation.get("operations")
        if isinstance(raw_operations, list):
            projected_operations = []
            for operation in raw_operations[:MAX_SESSION_ACTIVITY_RPC_OPERATIONS]:
                if not isinstance(operation, dict):
                    continue
                projected_operation = {
                    key: operation[key][:256]
                    for key in (
                        "event_identity", "observed_at", "operation", "outcome_status",
                    )
                    if isinstance(operation.get(key), str) and operation[key]
                }
                for key in ("event_sequence", "exit_code"):
                    if type(operation.get(key)) is int:
                        projected_operation[key] = operation[key]
                elapsed = operation.get("control_dispatch_elapsed_ms")
                if type(elapsed) in {int, float} and 0 <= elapsed <= 600_000:
                    projected_operation["control_dispatch_elapsed_ms"] = elapsed
                if projected_operation:
                    projected_operations.append(projected_operation)
            projected_observation["operations"] = projected_operations
            projected_observation["operations_truncated"] = (
                len(raw_operations) > MAX_SESSION_ACTIVITY_RPC_OPERATIONS
            )
        availability = observation.get("measurement_availability")
        if isinstance(availability, dict):
            projected_observation["measurement_availability"] = {
                key: value[:64]
                for key, value in availability.items()
                if key in {
                    "model_identity", "token_usage", "control_dispatch_elapsed_ms",
                    "resource_consumption", "cache_accounting",
                    "repository_head_live_probe",
                }
                and isinstance(value, str)
            }
        redaction = observation.get("redaction")
        if isinstance(redaction, str):
            projected_observation["redaction"] = redaction[:512]
            projected_observation["redaction_truncated"] = len(redaction) > 512
        observations.append({
            "identity": record["identity"],
            "captured_at": record.get("captured_at"),
            "measurement_basis": record.get("measurement_basis", "recorded operational response"),
            "observation": {
                **projected_observation,
                **({
                    "source_session_context": {
                        key: source_session_context[key][:512]
                        for key in source_session_context_fields
                        if source_session_context.get(key) is not None
                    },
                    "source_session_context_basis": observation.get(
                        "source_session_context_basis", "recorded-observation",
                    ),
                    "source_session_context_truncated": source_session_context_truncated,
                } if source_session_context is not None else {}),
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
                raw_improvement["identity"] if raw_improvement is not None else None
            ),
            "improvement_relation_status": (
                "not-recorded"
                if record.get("improvement_relation") is None
                else (
                    "validated" if improvement is not None
                    else "recorded-but-contract-unverified"
                )
            ),
            **({"record_correction": record_correction_summary}
               if record_correction_summary is not None else {}),
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
                    **(_authenticated_session_event_reference_resolution(
                        source,
                        selected_session_id=session.session_id,
                        activity=activity,
                    ) if isinstance(source, dict) and source.get("kind") == (
                        "authenticated-session-activity-projection"
                    ) else {}),
                }
                for source in effective_sources[:16]
            ],
            "measurement_limits": [
                value[:512] for value in effective_measurement_limits[:16]
                if isinstance(value, str)
            ] if isinstance(effective_measurement_limits, list) else [],
            "evidence_reference": {
                key: reference[key]
                for key in ("identity", "repository", "commit", "path", "sha256")
            },
            **({
                "evidence_reference_correction": {
                    **{
                        key: value
                        for key, value in pointer_correction_status_by_record[
                            record["identity"]
                        ].items()
                        if key in {
                            "status", "target_identity", "reason",
                            "replacement_verification",
                        }
                    },
                    **({
                        "target_reference": pointer_correction_status_by_record[
                            record["identity"]
                        ]["target_reference"]
                    } if pointer_correction_status_by_record[
                        record["identity"]
                    ].get("target_reference") is not None else {}),
                    **({
                        "replacement_reference": pointer_correction_status_by_record[
                            record["identity"]
                        ]["replacement_reference"]
                    } if pointer_correction_status_by_record[
                        record["identity"]
                    ].get("replacement_reference") is not None else {}),
                }
            } if record["identity"] in pointer_correction_status_by_record else {}),
            **({
                "evidence_reference_correction": {
                    "status": "recovered-from-append-only-correction",
                    "correction_observation_identity": reference[
                        "correction_observation_identity"
                    ],
                    "recorded_reference": reference[
                        "corrected_from_reference"
                    ],
                }
            } if isinstance(reference.get("corrected_from_reference"), dict)
                and isinstance(reference.get("correction_observation_identity"), str)
                else {}),
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
                    "not-recorded"
                    if improvement["subsequent_observation_identity"] is None
                    else (
                        "record-integrity-verified-in-projection"
                        if improvement["subsequent_observation_identity"] in projected_record_ids
                        else "not-present-in-projection"
                    )
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
                if improvement[identity_key] is None:
                    continue
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
        or unverified_improvement_relation_count
        or unverified_record_correction_count
        or pointer_correction_unverified_count
    ):
        status = "partial"
    elif observations:
        status = "ready"
    elif page_entries:
        status = "partial"
    else:
        status = "no-observations"
    hypotheses = _recurrence(observations)
    if campaign_state is None:
        activity_samples = activity.get("resource_samples")
        activity_samples = (
            activity_samples if isinstance(activity_samples, dict) else {}
        )
        activity_has_evidence = (
            activity_samples.get("observation_count", 0) > 0
            or activity.get("interaction_count", 0) > 0
            or activity.get("rpc_operation_count", 0) > 0
            or activity.get("authority_denial_count", 0) > 0
        )
        if activity_has_evidence:
            status = "partial"
    subject = {
        "session_id": session.session_id,
        "campaign_id": campaign.get("identity"),
        "environment_id": snapshot.get("environment_id"),
        "definition_id": (snapshot.get("provenance") or {}).get("definition_id"),
        "work_intent": {
            "identity": intent.get("identity"),
            "goal": intent.get("goal"),
        },
    }
    workspace_alignment = _work_intent_workspace_alignment(snapshot)
    evidence_visibility = _evidence_visibility_summary(
        observation_reference_diagnostics,
        len(observation_reference_diagnostics),
        len(observation_reference_diagnostics) > MAX_OBSERVATIONS,
    )
    context_pressure_candidates = _context_pressure_candidates(
        session_id=session.session_id,
        campaign_id=campaign.get("identity"),
        work_intent_id=intent.get("identity"),
        workspace_alignment=workspace_alignment,
        evidence_visibility=evidence_visibility,
        observation_reference_diagnostics=observation_reference_diagnostics,
        diagnostics_truncated=(
            len(observation_reference_diagnostics) > MAX_OBSERVATIONS
        ),
    )
    if workspace_alignment["status"] in {
        "declared-repositories-not-selected",
        "workspace-selection-unavailable",
        "unavailable",
    }:
        subject["workspace_alignment"] = workspace_alignment
    if isinstance(campaign_charter, dict):
        subject["campaign_charter"] = campaign_charter
    projection: dict[str, Any] = {
        "schema_version": SCHEMA,
        "identity": "eci_" + digest_hex({
            "session_id": session.session_id,
            "campaign_id": campaign.get("identity"),
            "work_intent_identity": intent.get("identity"),
            "workspace_alignment": workspace_alignment,
            "evidence": [item["identity"] for item in references],
            "campaign_charter_reference": (
                campaign_charter.get("evidence_reference")
                if isinstance(campaign_charter, dict) else None
            ),
            "campaign_charter_status": (
                campaign_charter.get("status")
                if isinstance(campaign_charter, dict) else None
            ),
            "session_activity_digest": (
                activity.get("source", {}).get("event_window_identity_digest")
                if isinstance(activity.get("source"), dict) else None
            ),
            "session_resource_sample_digest": (
                activity.get("source", {}).get("resource_sample_identity_digest")
                if isinstance(activity.get("source"), dict) else None
            ),
        }, length=16),
        "status": status,
        "subject": subject,
        "source": {
            "campaign_state_checkpoint_id": state.get("checkpoint_id"),
            **({
                "campaign_charter_status": campaign_charter.get("status"),
                "campaign_charter_reference_count": campaign_charter.get(
                    "candidate_reference_count", 0,
                ),
            } if isinstance(campaign_charter, dict) else {}),
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
            **({
                "record_correction_count": sum(
                    record.get("record_correction") is not None
                    for record, _ in projected_records
                ),
                "record_corrections_applied_count": len(correction_by_target),
                "record_corrections_unverified_count": (
                    unverified_record_correction_count
                ),
            } if any(
                record.get("record_correction") is not None
                for record, _ in projected_records
            ) else {}),
            **({
                "evidence_reference_correction_count": sum(
                    record.get("evidence_reference_correction") is not None
                    for record, _ in projected_records
                ),
                "evidence_reference_corrections_applied_count": (
                    pointer_corrections_applied_count
                ),
                "evidence_reference_corrections_unverified_count": (
                    pointer_correction_unverified_count
                ),
                "evidence_reference_correction_target_reads": (
                    pointer_correction_read_count
                ),
            } if any(
                record.get("evidence_reference_correction") is not None
                for record, _ in projected_records
            ) else {}),
            "observation_reference_diagnostics": (
                observation_reference_diagnostics[:MAX_OBSERVATIONS]
            ),
            "observation_reference_diagnostic_count": len(
                observation_reference_diagnostics
            ),
            "observation_reference_diagnostics_truncated": (
                len(observation_reference_diagnostics) > MAX_OBSERVATIONS
            ),
            **({"evidence_visibility": evidence_visibility} if (
                evidence_visibility.get(
                    "outside_selected_reference_diagnostic_count", 0
                ) > 0
            ) else {}),
            "improvement_relations_unverified_count": (
                unverified_improvement_relation_count
            ),
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
            "relationship": "A verified campaign charter and session Work Intent remain separate objectives. Campaign observations and session events link work to environmental subsystems; improvement relations link pressures, implementation, verification, and follow-up.",
        },
        "observation_count": len(observations),
        "pressure_hypothesis_count": len(hypotheses),
        "session_activity_pressure_candidate_count": len(
            activity.get("rpc_failure_candidates", [])
        ) + len(activity.get("authority_denial_candidates", [])),
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
            "A missing subsequent-observation identity is exposed as not-recorded; the projection does not infer follow-up.",
            "A linked verification record establishes that evidence is present and integrity checked; effectiveness still requires independent follow-up.",
            "Optional improvement relations that do not meet the current contract are surfaced as unverified and excluded from improvement claims.",
            *([
                "Context pressure candidates are derived hypotheses from declared repository and selected-checkout mismatch plus bounded source visibility; they do not establish impact or recurrence and do not authorize selecting an outside repository.",
            ] if context_pressure_candidates else []),
            "Session activity summarizes only the bounded recent event tail and exposes no raw invocation arguments or results.",
            "Authenticated session-event source pointers are checked against the selected session's bounded RPC activity; other sessions are marked unselected and missing events in partial views remain unresolved.",
            "Session resource samples read the newest bounded Store-backed Monitor invocation per binding and preserve session, objective, event, and artifact provenance; a host snapshot does not attribute resource consumption to an agent.",
            "Authority denial activity exposes normalized action and reason classes with event references; raw targets and free-form reasons are omitted.",
            "Authenticated Control read-operation events include a measured Control-dispatch elapsed time, which includes session-queue wait and is not total agent execution duration; model identity, token usage, resource consumption, and cache accounting remain unavailable.",
            "Direct read-operation capture stops after 128 observations per session; selected-window and capture-limit fields disclose partial coverage.",
            "Read-operation recurrence candidates cover only the selected tail of this session; cross-session recurrence is unavailable in this projection.",
            "Repeated read-operation failures are investigation candidates, not causal findings or proof of an environmental defect.",
            "Session capability events do not establish duration, token usage, resource consumption, or activity performed outside observed Environment or Control operations.",
            *(["Append-only observation corrections apply only when their exact target pointer and replacement source evidence are verified in the selected evidence pages; older record bytes remain inspectable."] if any(
                record.get("record_correction") is not None
                for record, _ in projected_records
            ) else []),
            *([
                "Multi-page recurrence covers only the selected bounded page window; source metadata reports page offsets, checkpoint consistency, and budget omissions.",
            ] if history_pages > 1 else []),
        ],
    }
    if context_pressure_candidates:
        projection["context_pressure_candidate_count"] = len(
            context_pressure_candidates
        )
        projection["context_pressure_candidates"] = context_pressure_candidates
        projection["correlation"]["relationship"] += (
            " Context pressure candidates separately relate declared repositories, "
            "selected checkouts, and bounded source-visibility diagnostics without "
            "treating a candidate as a recorded pressure or granting access."
        )
    encoded = json.dumps(projection, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(encoded) > max_result_bytes:
        return _compact_projection(projection, len(encoded), max_result_bytes)
    return projection
