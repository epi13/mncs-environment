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
import selectors
import subprocess
import time
from datetime import timezone
from pathlib import Path, PurePosixPath
from typing import Any

from . import authority as authority_module
from . import claims as claims_module
from . import invocation_artifacts as invocation_artifacts_module
from . import readiness as readiness_module
from . import workspace as workspace_module
from .identity import digest_hex
from .sessions import (
    MAX_RPC_READ_ACTIVITY_OBSERVATIONS,
    LifecycleError,
    is_campaign_charter_evidence_path,
    is_valid_context_response_summary,
)
from .store_backend import StoreIntegrityFailure, StoreUnavailable

SCHEMA = "mncs.environment.capability-intelligence/1"
OBSERVATION_SCHEMA = "mncs.environment.environmental-observation/1"
OBSERVATION_CORRECTION_SCHEMA = "mncs.environment.observation-correction/1"
PRESSURE_HYPOTHESIS_ROLES = frozenset({"occurrence", "supporting-context"})
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
COMPILER_NATIVE_APPLICATION_PROOF_SCHEMA = (
    "mncs.compiler.native-application-proof/2"
)
COMPILER_NATIVE_APPLICATION_PROOF_IDENTITY = (
    "ev_compiler_native_application_proof_digest_app_20261018"
)
COMPILER_NATIVE_APPLICATION_PROOF_PATH = (
    "evidence/native-app-readiness-test-digest-app.json"
)
COMPILER_NATIVE_APPLICATION_ENTRY = "mncs.test.digest::digest_app"
COMPILER_NATIVE_APPLICATION_MODULES = (
    (
        "mncs.std.application.v1", "mncs-stdlib",
        "library/std/application.mncs", "stdlib_revision",
    ),
    (
        "mncs.test.assertions", "mncs-test",
        "native/mncs/test/assertions.mncs", "test_revision",
    ),
    (
        "mncs.test.suite", "mncs-test",
        "native/mncs/test/suite.mncs", "test_revision",
    ),
    (
        "mncs.test.digest", "mncs-test",
        "native/mncs/test/digest.mncs", "test_revision",
    ),
)
IMPLEMENTATION_ACCOUNTABILITY_SCHEMA = (
    "mncs.environment.implementation-accountability/1"
)
NATIVE_USERLAND_AUDIT_SCHEMA = "mncs.native-userland-audit/1"
NATIVE_USERLAND_AUDIT_SOURCE_REPOSITORIES = frozenset({
    "mncs-actions", "mncs-test",
})
NATIVE_USERLAND_PRESSURE_SCHEMA = "mncs-test.pressure/1"
MAX_NATIVE_USERLAND_AUDIT_RECORDS = 4
MAX_NATIVE_USERLAND_PRESSURE_SOURCE_RECORDS = 8
COMMONS_PRESSURE_SOURCE_REPOSITORY = "mncs-commons"
COMMONS_PRESSURE_RECORD_SOURCE_KIND = "commons-family-pressure-record-source"
COMMONS_PRESSURE_OBSERVATION_SOURCE_KIND = (
    "commons-family-pressure-observation-source"
)
COMMONS_PRESSURE_RECORD_SCHEMA = "commons.mncs.dev/family-pressure/v1"
COMMONS_PRESSURE_OBSERVATION_SCHEMA = (
    "commons.mncs.dev/family-pressure-observation/v1"
)
# Nine pointers cover one pressure plus up to eight consumer observations. Four
# additional exact pressure records allow bounded joins for source-declared
# related language pressures.
MAX_COMMONS_PRESSURE_SOURCE_PAYLOADS = 13
MAX_COMMONS_RELATED_PRESSURE_RECORDS = 4
MAX_NATIVE_USERLAND_SOURCE_PAYLOADS = (
    MAX_NATIVE_USERLAND_AUDIT_RECORDS
    + MAX_NATIVE_USERLAND_PRESSURE_SOURCE_RECORDS
    + MAX_COMMONS_PRESSURE_SOURCE_PAYLOADS
)
MAX_NATIVE_USERLAND_AUDIT_ITEMS = 8
NATIVE_CAPABILITY_CORRESPONDENCE_KIND = (
    "native-capability-pressure-correspondence"
)
NATIVE_CAPABILITY_CONSUMER_INTEGRATION_PRESSURE_KIND = (
    "native-capability-consumer-integration-pressure"
)
NATIVE_PROCESS_CONSUMER_VERIFICATION_KIND = (
    "native-process-consumer-integration-verification"
)
ENVIRONMENTAL_IMPROVEMENT_LINK_KIND = "environmental-improvement-link-record"
NATIVE_CAPABILITY_CORRESPONDENCE_SOURCE_ROLES = {
    "native-language-capability-contract": "language_contract",
    "native-language-compiler-support": "compiler_support",
    "native-runtime-provider-implementation": "host_runtime_provider",
    "native-runtime-operation-dispatch": "host_runtime_dispatch",
    "native-runtime-provider-test-source": "runtime_provider_test_source",
    "native-language-lifecycle-test-source": "native_language_test_source",
    "mncs-test-pressure-source": "target_pressure_source",
    "native-userland-pressure-source": "target_pressure_source",
    "native-userland-audit-source": "owner_audit_source",
    "host-boundary-readiness-document": "host_boundary_readiness_document",
}
MAX_NATIVE_CAPABILITY_CORRESPONDENCES = 8
MAX_NATIVE_CAPABILITY_SOURCE_REFERENCES = 16
MAX_NATIVE_CONSUMER_ACTIVITY_RECORDS = 1
MAX_NATIVE_CONSUMER_ACTIVITY_IMPROVEMENTS = 1
MAX_NATIVE_CONSUMER_CAPABILITY_IDENTITIES = 4
MAX_NATIVE_CONSUMER_ACTIVITY_SOURCE_REFERENCES = 2
MAX_EVIDENCE_REFS = 32
MAX_EVIDENCE_HISTORY_PAGES = 4
MAX_OBSERVATIONS = 16
MAX_OBSERVATION_BYTES = 64 * 1024
MAX_SOURCE_BYTES = 512 * 1024
MAX_SOURCE_HASH_BYTES = 4 * 1024 * 1024
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
MAX_SESSION_DOCTOR_HISTORY = 8
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
MAX_SESSION_ACTIVITY_BINDINGS = 32
MAX_SESSION_ACTIVITY_BINDING_SCAN = 512
MAX_CONTEXT_RESPONSE_CAPABILITY_LIST = 20
MAX_SESSION_ACTIVITY_CAPABILITY_EVENTS = 12
MAX_CAPABILITY_SUPPORTING_EVENTS = 3
MAX_SESSION_ACTIVITY_RPC_OPERATIONS = 32
MAX_SESSION_ACTIVITY_RPC_PRESSURE_CANDIDATES = 8
MAX_SESSION_ACTIVITY_RPC_SUMMARY_GROUPS = 32
MAX_SESSION_ACTIVITY_RPC_SUMMARY_REFERENCES = 3
MAX_SESSION_ACTIVITY_AUTHORITY_DENIALS = 16
MAX_SESSION_ACTIVITY_AUTHORITY_PRESSURE_CANDIDATES = 8
MAX_SESSION_ACTIVITY_INTEGRATION_ELIGIBILITY = 16
MAX_SESSION_ACTIVITY_INTEGRATION_OVERLAPS = 8
MAX_SESSION_ACTIVITY_PROPOSAL_ELIGIBILITY = 16
MAX_SESSION_ACTIVITY_REPOSITORY_REF_TRANSITIONS = 16
MAX_SESSION_ACTIVITY_CLAIM_RECOVERIES = 8
MAX_SESSION_ACTIVITY_CLAIM_COORDINATION = 16
MAX_SESSION_ACTIVITY_CLAIM_LIFECYCLE_CORRELATIONS = 8
MAX_SESSION_ACTIVITY_OPERATION_DEFERRALS = 8
MAX_SESSION_ACTIVITY_COORDINATION_DEFERRALS = 8
MAX_SESSION_SERVICE_READINESS_RECORDS = 16
MAX_SESSION_SERVICE_READINESS_CANDIDATES = 8
MAX_COMPACT_SESSION_ACTIVITY_CLAIM_LIFECYCLE_CORRELATIONS = 1
MAX_SESSION_CLAIM_OWNER_EVENT_SESSIONS = 4
MAX_SESSION_CLAIM_OWNER_EVENT_PAGE = 32
MAX_SESSION_CLAIM_LIFECYCLE_EVENTS_PER_CORRELATION = 4
MAX_SESSION_AUTHORITY_CLAIM_LOOKUPS = 4
CLAIM_COORDINATION_EVENT_ACTIONS = {
    "lease.acquired": "acquired",
    "lease.owner-recovered": "owner-recovered",
    "lease.released": "released",
    "lease.release_noop": "release-noop",
    "lease.transferred": "transferred",
    "lease.recovered-away": "recovered-away",
    "lease.owner-recovery-reconciled": "owner-recovery-reconciled",
}
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
            and (record.get("name") == repository
                 or record.get("manifest_repository") == repository)
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
            and (record.get("name") == repository
                 or record.get("manifest_repository") == repository)
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
    manifest_repository = record.get("manifest_repository")
    if (len(relative.parts) != 1 or not checkout.is_dir()
            or not (checkout / ".git").exists()
            or record.get("git_error") is not None
            or not isinstance(record.get("head"), str)
            or not record["head"]):
        return None
    if (manifest_repository is not None
            and manifest_repository != repository):
        return None
    if (checkout.name != repository
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


def _compact_request_summary(request: Any) -> dict[str, Any] | None:
    """Keep bounded request identifiers useful for navigating an observation."""
    if not isinstance(request, dict):
        return None
    result: dict[str, Any] = {}
    for key in (
        "operation", "command", "projection_identity",
        "target_observation_identity", "target_content_sha256",
        "repository", "branch", "commit", "session_id", "work_intent_id",
    ):
        value = request.get(key)
        if isinstance(value, str) and value:
            result[key] = _short_text(value, 256 if key == "command" else 160)
    for key in ("source_event_identities", "event_identities", "corrected_field_paths"):
        values = request.get(key)
        if not isinstance(values, list):
            continue
        selected = [
            _short_text(value, 160) for value in values[:8]
            if isinstance(value, str) and value
        ]
        if selected:
            result[key] = selected
            result[f"{key}_count"] = len(values)
            result[f"{key}_truncated"] = len(values) > len(selected)
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
        "original_record_status",
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
    diagnostics = outcome.get("original_contract_diagnostics")
    if isinstance(diagnostics, list):
        selected_diagnostics = [
            _short_text(value, 128) for value in diagnostics[:8]
            if isinstance(value, str) and value
        ]
        if selected_diagnostics:
            result["original_contract_diagnostics"] = selected_diagnostics
            result["original_contract_diagnostics_truncated"] = (
                len(diagnostics) > len(selected_diagnostics)
            )
    raw_event_summary = outcome.get("source_event_summary")
    if isinstance(raw_event_summary, dict):
        event_summary = {}
        for name, event in list(raw_event_summary.items())[:4]:
            if not isinstance(name, str) or not isinstance(event, dict):
                continue
            values = {}
            for key, value in list(event.items())[:12]:
                if not isinstance(key, str) or not key or len(key) > 80:
                    continue
                if isinstance(value, str) and value:
                    values[key] = _short_text(value, 160)
                elif (type(value) in {bool, int} or value is None):
                    values[key] = value
            if values:
                event_summary[_short_text(name, 64)] = values
        if event_summary:
            result["source_event_summary"] = event_summary
            result["source_event_summary_truncated"] = (
                len(raw_event_summary) > len(event_summary)
            )
    return result or None


def _compact_native_capability_correspondences(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    compact_items = []
    for item in value[:4]:
        if not isinstance(item, dict):
            continue
        compact = {
            key: _short_text(item.get(key), 192)
            for key in (
                "identity", "target_pressure_identity",
                "native_capability_identity", "relationship_assessment",
                "recorded_pressure_status", "target_pressure_summary",
                "host_boundary_summary",
                "source_evidence_status", "causal_relation_status",
                "native_migration_readiness", "agent_usage_status",
                "classification_provenance", "next_investigation",
                "interpretation_limit",
            )
            if isinstance(item.get(key), str)
        }
        source_work_context = item.get("source_work_context")
        if isinstance(source_work_context, dict):
            compact_work_context = {
                key: _short_text(source_work_context.get(key), limit)
                for key, limit in (
                    ("status", 96), ("basis", 128),
                    ("objective_capture_status", 96),
                    ("campaign_id", 160), ("session_id", 160),
                    ("work_intent_id", 160), ("work_intent_goal", 256),
                    ("campaign_objective", 256), ("environment_id", 160),
                    ("definition_id", 160), ("environment_version", 96),
                    ("source_observation_identity", 160),
                )
                if isinstance(source_work_context.get(key), str)
            }
            for key in (
                "campaign_id_truncated", "session_id_truncated",
                "work_intent_id_truncated", "work_intent_goal_truncated",
                "campaign_objective_truncated", "environment_id_truncated",
                "definition_id_truncated", "environment_version_truncated",
            ):
                if type(source_work_context.get(key)) is bool:
                    compact_work_context[key] = source_work_context[key]
            source_reference = _validate_reference(
                source_work_context.get("source_observation_reference"),
            )
            if source_reference is not None:
                compact_work_context["source_observation_reference"] = (
                    source_reference
                )
            if compact_work_context:
                compact["source_work_context"] = compact_work_context
        role_groups = item.get("source_evidence_by_role")
        compact_groups = {}
        if isinstance(role_groups, dict):
            for role, group in list(role_groups.items())[:8]:
                if not isinstance(role, str) or not isinstance(group, dict):
                    continue
                compact_group = {
                    key: _short_text(group.get(key), 96)
                    for key in ("status",)
                    if isinstance(group.get(key), str)
                }
                for key in ("source_reference_count", "verified_reference_count"):
                    if type(group.get(key)) is int and group[key] >= 0:
                        compact_group[key] = group[key]
                references = group.get("source_references")
                references = references if isinstance(references, list) else []
                compact_group["source_references"] = [
                    {
                        **{
                            key: _short_text(source.get(key), 192 if key == "path" else 160)
                            for key in (
                                "identity", "kind", "repository", "commit", "path",
                                "sha256", "content_integrity_status",
                            )
                            if isinstance(source.get(key), str)
                        },
                    }
                    for source in references[:2]
                    if isinstance(source, dict)
                ]
                compact_group["source_references_truncated"] = (
                    group.get("source_references_truncated") is True
                    or len(references) > 2
                )
                compact_groups[role] = compact_group
        if compact_groups:
            compact["source_evidence_by_role"] = compact_groups
        pressure_links = item.get("pressure_source_links")
        if isinstance(pressure_links, dict):
            links = pressure_links.get("links")
            links = links if isinstance(links, list) else []
            compact_links = []
            for link in links[:4]:
                if not isinstance(link, dict):
                    continue
                compact_link = {
                    key: _short_text(link.get(key), 160)
                    for key in (
                        "identity", "status", "causal_relation_status",
                        "relationship_basis", "source_content_integrity_status",
                        "source_kind",
                    )
                    if isinstance(link.get(key), str)
                }
                reference = _validate_reference(link.get("source_reference"))
                if reference is not None:
                    compact_link["source_reference"] = reference
                compact_links.append(compact_link)
            compact["pressure_source_links"] = {
                key: _short_text(pressure_links.get(key), 192)
                for key in (
                    "status", "relationship_basis", "causal_relation_status",
                    "interpretation_limit",
                )
                if isinstance(pressure_links.get(key), str)
            } | {"links": compact_links}
        verification = item.get("verification_observation")
        if isinstance(verification, dict):
            compact_verification = {
                key: _short_text(verification.get(key), 256)
                for key in (
                    "status", "command", "test_identity", "result_summary",
                    "interpretation_limit",
                )
                if isinstance(verification.get(key), str)
            }
            for key in ("passed", "failed", "filtered"):
                if type(verification.get(key)) is int and verification[key] >= 0:
                    compact_verification[key] = verification[key]
            for key in ("unsupported_branch_may_return_early",):
                if type(verification.get(key)) is bool:
                    compact_verification[key] = verification[key]
            compact["verification_observation"] = compact_verification
        consumer_evidence = _compact_native_capability_consumer_evidence(
            item.get("consumer_evidence"),
        )
        if consumer_evidence is not None:
            compact["consumer_evidence"] = consumer_evidence
        native_consumer_activity = _compact_native_capability_consumer_activity(
            item.get("native_consumer_activity"),
        )
        if native_consumer_activity is not None:
            compact["native_consumer_activity"] = native_consumer_activity
        compact_items.append(compact)
    return compact_items


def _compact_selected_text_fields(
    value: Any, fields: tuple[tuple[str, int], ...],
) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {
        key: _short_text(value.get(key), limit)
        for key, limit in fields if isinstance(value.get(key), str)
    }


def _compact_native_consumer_activity_source_references(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    references = []
    for source in value[:MAX_NATIVE_CONSUMER_ACTIVITY_SOURCE_REFERENCES]:
        if not isinstance(source, dict):
            continue
        reference = _compact_selected_text_fields(source, tuple(
            (key, 192 if key == "path" else 160)
            for key in (
                "identity", "kind", "repository", "commit", "path", "sha256",
                "content_integrity_status",
            )
        ))
        if reference:
            references.append(reference)
    return references


def _compact_native_process_consumer_verification(value: Any) -> dict[str, Any]:
    result = _compact_selected_text_fields(value, (
        ("identity", 160), ("kind", 128), ("integration_status", 96),
        ("compilation_status", 256), ("assertion", 256),
        ("interpretation_limit", 384),
    ))
    if not isinstance(value, dict):
        return result
    for suite in ("resolver_unit_tests", "native_core_suite"):
        counts = value.get(suite)
        if isinstance(counts, dict):
            result[suite] = {
                key: counts[key] for key in ("passed", "failed")
                if type(counts.get(key)) is int and counts[key] >= 0
            }
    typed = value.get("observed_typed_result")
    if isinstance(typed, dict):
        result["observed_typed_result"] = {
            key: typed[key]
            for key in (
                "success", "has_exit_code", "timed_out", "exit_code",
                "subprocess_count", "stdout_truncated", "stderr_truncated",
            )
            if type(typed.get(key)) in {bool, int}
        }
    residual = value.get("reported_pressure_hypothesis")
    if isinstance(residual, dict):
        result["reported_pressure_hypothesis"] = _compact_selected_text_fields(
            residual, (
                ("identity", 160), ("title", 192), ("category", 96),
                ("subsystem", 192), ("owner", 96), ("recurrence", 128),
                ("actionable_recommendation", 320),
            ),
        )
    result["source_references"] = _compact_native_consumer_activity_source_references(
        value.get("source_references"),
    )
    if type(value.get("source_references_truncated")) is bool:
        result["source_references_truncated"] = value["source_references_truncated"]
    return result


def _compact_native_capability_consumer_activity(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result = _compact_selected_text_fields(value, (
        ("status", 96), ("native_capability_identity", 192),
        ("join_basis", 256), ("source_coverage_scope", 128),
        ("recurrence_status", 96), ("agent_usage_status", 96),
        ("causal_relation_status", 96), ("native_migration_readiness", 96),
        ("actionable_observation", 256), ("interpretation_limit", 320),
    ))
    adoption_ratio = value.get("systemwide_native_adoption_ratio")
    if isinstance(adoption_ratio, dict) and isinstance(adoption_ratio.get("status"), str):
        result["systemwide_native_adoption_ratio"] = {
            "status": _short_text(adoption_ratio["status"], 96),
        }
    raw_activities = value.get("activities")
    if not isinstance(raw_activities, list):
        return result or None
    activities = []
    for activity in raw_activities[:MAX_NATIVE_CONSUMER_ACTIVITY_RECORDS]:
        if not isinstance(activity, dict):
            continue
        compact_activity = _compact_selected_text_fields(activity, (
            ("source_observation_identity", 160), ("source_observation_kind", 128),
            ("native_capability_identity", 192), ("target_pressure_identity", 160),
            ("consumer_repository", 128), ("integration_test_identity", 160),
            ("consumer_surface", 256), ("observed_limitation", 384),
            ("environmental_pressure", 384), ("agent_impact", 160),
            ("causal_scope", 256),
        ))
        pressure = activity.get("pressure_hypothesis")
        if isinstance(pressure, dict):
            compact_activity["pressure_hypothesis"] = _compact_selected_text_fields(
                pressure, (
                    ("title", 192), ("category", 96), ("subsystem", 192),
                    ("owner", 96), ("recurrence", 128),
                    ("actionable_recommendation", 320),
                ),
            )
        work = activity.get("source_work_context")
        if isinstance(work, dict):
            compact_work = _compact_selected_text_fields(work, (
                ("status", 80), ("basis", 96),
                ("objective_capture_status", 80), ("campaign_id", 128),
                ("session_id", 128), ("work_intent_id", 128),
                ("work_intent_goal", 160), ("campaign_objective", 160),
                ("environment_id", 128), ("definition_id", 128),
                ("environment_version", 80),
                ("source_observation_identity", 128),
            ))
            for key in (
                "campaign_id_truncated", "session_id_truncated",
                "work_intent_id_truncated", "work_intent_goal_truncated",
                "campaign_objective_truncated", "environment_id_truncated",
                "definition_id_truncated", "environment_version_truncated",
            ):
                if type(work.get(key)) is bool:
                    compact_work[key] = work[key]
            reference = _validate_reference(work.get("source_observation_reference"))
            if reference is not None:
                compact_work["source_observation_reference"] = reference
            compact_activity["source_work_context"] = compact_work
        compact_activity["source_references"] = _compact_native_consumer_activity_source_references(
            activity.get("source_references"),
        )
        for key in ("source_references_truncated", "improvement_links_truncated"):
            if type(activity.get(key)) is bool:
                compact_activity[key] = activity[key]
        raw_improvements = activity.get("improvement_links")
        improvements = []
        if isinstance(raw_improvements, list):
            for improvement in raw_improvements[
                :MAX_NATIVE_CONSUMER_ACTIVITY_IMPROVEMENTS
            ]:
                if not isinstance(improvement, dict):
                    continue
                compact_improvement = _compact_selected_text_fields(improvement, (
                    ("improvement_observation_identity", 160),
                    ("improvement_relation_identity", 160),
                    ("addresses_hypothesis_identity", 160),
                    ("implementation_commit", 64), ("implementation_scope", 192),
                    ("implementation_change", 384), ("verified_change", 384),
                    ("effectiveness_assessment", 96),
                    ("subsequent_observation_identity", 160),
                    ("verification_observation_identity", 160),
                    ("improvement_record_consistency_status", 96),
                    ("verification_match_status", 96),
                    ("interpretation_limit", 320),
                ))
                compact_improvement["source_references"] = _compact_native_consumer_activity_source_references(
                    improvement.get("source_references"),
                )
                if type(improvement.get("source_references_truncated")) is bool:
                    compact_improvement["source_references_truncated"] = improvement[
                        "source_references_truncated"
                    ]
                verification = improvement.get("verification_observation")
                if isinstance(verification, dict):
                    compact_improvement["verification_observation"] = (
                        _compact_native_process_consumer_verification(verification)
                    )
                improvements.append(compact_improvement)
        compact_activity["improvement_links"] = improvements
        activities.append(compact_activity)
    result["activities"] = activities
    result["activities_truncated"] = (
        value.get("activities_truncated") is True
        or len(raw_activities) > len(activities)
    )
    return result or None


def _compact_native_capability_consumer_evidence(
    value: Any,
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result = {
        key: _short_text(value.get(key), 192)
        for key in (
            "status", "join_basis", "source_coverage_scope",
            "cross_consumer_status", "recurrence_status",
            "causal_relation_status", "native_migration_readiness",
            "actionable_observation",
        )
        if isinstance(value.get(key), str)
    }
    for key in (
        "source_observation_count", "consumer_repository_count",
        "passing_reproduction_observation_count",
        "workaround_active_observation_count", "unverified_source_count",
    ):
        if type(value.get(key)) is int and value[key] >= 0:
            result[key] = value[key]
    for key in ("recorded_affected_repositories", "consumer_repositories_observed",
                "consumer_migration_states", "related_pressure_identities"):
        values = value.get(key)
        if isinstance(values, list):
            result[key] = [
                _short_text(item, 160) for item in values[:8]
                if isinstance(item, str)
            ]
            if len(values) > 8:
                result[f"{key}_truncated"] = True
    raw_related_pressures = value.get("related_pressure_records")
    if isinstance(raw_related_pressures, list):
        related_pressures = []
        for item in raw_related_pressures[:MAX_COMMONS_RELATED_PRESSURE_RECORDS]:
            if not isinstance(item, dict):
                continue
            compact_related = {
                key: _short_text(item.get(key), 384)
                for key in (
                    "identity", "status", "recorded_status", "title", "summary",
                    "required_behavior", "relation_basis", "causal_relation_status",
                )
                if isinstance(item.get(key), str)
            }
            affected = item.get("affected_repositories")
            if isinstance(affected, list):
                compact_related["affected_repositories"] = [
                    _short_text(entry, 128) for entry in affected[:8]
                    if isinstance(entry, str)
                ]
                if len(affected) > 8:
                    compact_related["affected_repositories_truncated"] = True
            declarations = item.get("source_declaration_observations")
            if isinstance(declarations, list):
                compact_related["source_declaration_observations"] = [
                    reference
                    for entry in declarations[:4]
                    if (reference := _validate_reference(entry)) is not None
                ]
                if len(declarations) > 4:
                    compact_related["source_declaration_observations_truncated"] = True
            reference = _validate_reference(item.get("source_reference"))
            if reference is not None:
                compact_related["source_reference"] = reference
            if compact_related:
                related_pressures.append(compact_related)
        result["related_pressure_records"] = related_pressures
        result["related_pressure_records_truncated"] = (
            value.get("related_pressure_records_truncated") is True
            or len(raw_related_pressures) > len(related_pressures)
        )
    pressure_record = value.get("pressure_record")
    if isinstance(pressure_record, dict):
        compact_record = {
            key: _short_text(pressure_record.get(key), 384)
            for key in ("status", "identity", "recorded_status", "title", "summary")
            if isinstance(pressure_record.get(key), str)
        }
        reference = _validate_reference(pressure_record.get("source_reference"))
        if reference is not None:
            compact_record["source_reference"] = reference
        if compact_record:
            result["pressure_record"] = compact_record
    raw_observations = value.get("observations")
    if isinstance(raw_observations, list):
        observations = []
        for item in raw_observations[:4]:
            if not isinstance(item, dict):
                continue
            compact_item = {
                key: _short_text(item.get(key), 320 if key == "summary" else 160)
                for key in (
                    "identity", "record_kind", "consumer_repository",
                    "observed_at", "language_profile", "source_recorded_agent",
                    "current_validation", "reproduction_status",
                    "capability_state", "consumer_migration", "consumer_status",
                    "classification", "summary",
                )
                if isinstance(item.get(key), str)
            }
            if type(item.get("current_validation")) is bool:
                compact_item["current_validation"] = item["current_validation"]
            workaround = item.get("workaround")
            if isinstance(workaround, dict):
                compact_workaround = {
                    key: workaround[key]
                    for key in ("active", "removed")
                    if type(workaround.get(key)) is bool
                }
                if compact_workaround:
                    compact_item["workaround"] = compact_workaround
            for key in ("missing_contracts", "source_declared_related_pressure_identities"):
                values = item.get(key)
                if isinstance(values, list):
                    compact_item[key] = [
                        _short_text(entry, 192) for entry in values[:4]
                        if isinstance(entry, str)
                    ]
                    if len(values) > 4:
                        compact_item[f"{key}_truncated"] = True
            reference = _validate_reference(item.get("source_reference"))
            if reference is not None:
                compact_item["source_reference"] = reference
            if compact_item:
                observations.append(compact_item)
        result["observations"] = observations
        result["observations_truncated"] = (
            value.get("observations_truncated") is True
            or len(raw_observations) > len(observations)
        )
    raw_unverified = value.get("unverified_source_references")
    if isinstance(raw_unverified, list):
        result["unverified_source_references"] = [
            {
                **{
                    key: _short_text(source.get(key), 192 if key == "path" else 160)
                    for key in ("identity", "kind", "repository", "commit", "path", "sha256")
                    if isinstance(source.get(key), str)
                },
                "content_integrity_status": _short_text(
                    source.get("content_integrity_status"), 96,
                ),
            }
            for source in raw_unverified[:4]
            if isinstance(source, dict)
        ]
        if len(raw_unverified) > 4:
            result["unverified_source_references_truncated"] = True
    return result or None


def _compact_implementation_accountability(value: Any) -> dict[str, Any] | None:
    if (not isinstance(value, dict)
            or value.get("schema_version") != IMPLEMENTATION_ACCOUNTABILITY_SCHEMA):
        return None
    raw_samples = value.get("samples")
    raw_samples = raw_samples if isinstance(raw_samples, list) else []
    owner_audit_evidence = _compact_owner_audit_evidence(
        value.get("owner_audit_evidence"),
    )
    capability_correspondences = _compact_native_capability_correspondences(
        value.get("native_capability_correspondences"),
    )
    raw_consumer_activities = value.get("native_consumer_activities")
    raw_consumer_activities = (
        raw_consumer_activities
        if isinstance(raw_consumer_activities, list) else []
    )
    consumer_activities = [
        compact_activity
        for item in raw_consumer_activities[:MAX_NATIVE_CONSUMER_CAPABILITY_IDENTITIES]
        if (compact_activity := _compact_native_capability_consumer_activity(item))
        is not None
    ]
    consumer_activities_truncated = (
        value.get("native_consumer_activities_truncated") is True
        or len(raw_consumer_activities) > len(consumer_activities)
    )
    if not raw_samples:
        result = {
            "schema_version": IMPLEMENTATION_ACCOUNTABILITY_SCHEMA,
            "sample_count": 0,
            "samples": [],
            "systemwide_native_adoption_ratio": {
                "status": "not-measured",
                "reason": "A complete, ownership-verified MNCS implementation inventory is unavailable.",
            },
        }
        if owner_audit_evidence is not None:
            result["owner_audit_evidence"] = owner_audit_evidence
        if capability_correspondences:
            result["native_capability_correspondences"] = capability_correspondences
        if consumer_activities:
            result["native_consumer_activities"] = consumer_activities
        if consumer_activities_truncated:
            result["native_consumer_activities_truncated"] = True
        return result
    samples = []
    for sample in raw_samples[:4]:
        if not isinstance(sample, dict):
            continue
        compact_sample = {
            key: _short_text(sample.get(key), 192 if key == "sample_scope" else 160)
            for key in (
                "identity", "application_entry", "sample_scope",
                "native_semantic_ownership", "ownership_status",
                "artifact_identity", "compiler_front_end_outcome",
                "native_migration_opportunity", "source_campaign_context",
                "evidence_relationship_basis",
            )
            if isinstance(sample.get(key), str)
        }
        for key in (
            "native_module_count", "source_integrity_verified_module_count",
            "canonical_vm_case_count", "canonical_vm_case_success_count",
        ):
            if type(sample.get(key)) is int and sample[key] >= 0:
                compact_sample[key] = sample[key]
        compact_modules = []
        raw_modules = sample.get("native_modules")
        raw_modules = raw_modules if isinstance(raw_modules, list) else []
        for module in raw_modules[:4]:
            if not isinstance(module, dict):
                continue
            compact_modules.append({
                key: _short_text(module.get(key), 192 if key == "path" else 160)
                for key in (
                    "module", "repository", "commit", "path", "sha256",
                    "status", "content_integrity_status",
                )
                if isinstance(module.get(key), str)
            })
        compact_sample["native_modules"] = compact_modules
        proof_reference = sample.get("compiler_proof_reference")
        if isinstance(proof_reference, dict):
            compact_sample["compiler_proof_reference"] = {
                key: _short_text(proof_reference.get(key), 192 if key == "path" else 160)
                for key in (
                    "identity", "repository", "commit", "path", "sha256",
                    "status", "content_integrity_status",
                )
                if isinstance(proof_reference.get(key), str)
            }
        host_adapter = sample.get("host_test_adapter")
        if isinstance(host_adapter, dict):
            compact_sample["host_test_adapter"] = {
                key: _short_text(host_adapter.get(key), 256)
                for key in ("classification", "limitation", "harness_limitation")
                if isinstance(host_adapter.get(key), str)
            }
        scoped_ownership = sample.get("scoped_native_module_ownership")
        if isinstance(scoped_ownership, dict):
            compact_scope = {
                key: _short_text(scoped_ownership.get(key), 192)
                for key in ("status", "scope", "unit", "interpretation_limit")
                if isinstance(scoped_ownership.get(key), str)
            }
            for key in ("verified_native_module_count", "module_denominator"):
                if type(scoped_ownership.get(key)) is int:
                    compact_scope[key] = scoped_ownership[key]
            if type(scoped_ownership.get(
                "host_test_adapter_included_in_denominator",
            )) is bool:
                compact_scope["host_test_adapter_included_in_denominator"] = (
                    scoped_ownership["host_test_adapter_included_in_denominator"]
                )
            host_semantics = scoped_ownership.get(
                "host_semantic_implementation_count",
            )
            if isinstance(host_semantics, dict):
                compact_scope["host_semantic_implementation_count"] = {
                    key: _short_text(host_semantics.get(key), 192)
                    for key in ("status", "reason")
                    if isinstance(host_semantics.get(key), str)
                }
            compact_sample["scoped_native_module_ownership"] = compact_scope
        capability_contract = _compiler_capability_contract_view(
            sample.get("compiler_capability_contract"),
        )
        if capability_contract is not None:
            compact_sample["compiler_capability_contract"] = capability_contract
        delivery_observation = _compact_selected_session_delivery_observation(
            sample.get("selected_session_delivery_observation"),
        )
        if delivery_observation is not None:
            compact_sample["selected_session_delivery_observation"] = (
                delivery_observation
            )
        evidence_reference = _validate_reference(sample.get("evidence_reference"))
        if evidence_reference is not None:
            compact_sample["evidence_reference"] = evidence_reference
        samples.append(compact_sample)
    ratio = value.get("systemwide_native_adoption_ratio")
    compact_ratio = {
        "status": "not-measured",
        "reason": "A complete, ownership-verified inventory of MNCS functionality and its denominator is unavailable.",
    }
    if isinstance(ratio, dict) and isinstance(ratio.get("reason"), str):
        compact_ratio["reason"] = _short_text(ratio["reason"], 256) or compact_ratio["reason"]
    measurement_limits = value.get("measurement_limits")
    result = {
        "schema_version": IMPLEMENTATION_ACCOUNTABILITY_SCHEMA,
        "scope": _short_text(value.get("scope"), 192),
        "sample_count": value.get("sample_count", len(raw_samples)),
        "samples": samples,
        "samples_truncated": len(raw_samples) > len(samples),
        "systemwide_native_adoption_ratio": compact_ratio,
        "measurement_limits": [
            _short_text(item, 192)
            for item in measurement_limits[:4]
            if isinstance(item, str)
        ] if isinstance(measurement_limits, list) else [],
    }
    if owner_audit_evidence is not None:
        result["owner_audit_evidence"] = owner_audit_evidence
    if capability_correspondences:
        result["native_capability_correspondences"] = capability_correspondences
    if consumer_activities:
        result["native_consumer_activities"] = consumer_activities
    if consumer_activities_truncated:
        result["native_consumer_activities_truncated"] = True
    return result


def _compact_owner_audit_evidence(value: Any) -> dict[str, Any] | None:
    """Retain bounded owner declarations without upgrading them to verification."""
    if not isinstance(value, dict):
        return None
    audits = value.get("audits")
    audits = audits if isinstance(audits, list) else []
    compact_audits = []
    for audit in audits[:4]:
        if not isinstance(audit, dict):
            continue
        compact_audit = {
            key: _short_text(audit.get(key), 256 if key in {
                "source_interpretation", "interpretation_limit",
            } else 160)
            for key in (
                "identity", "source_interpretation", "interpretation_limit",
            )
            if isinstance(audit.get(key), str)
        }
        for key in (
            "source_declared_audit_count", "projected_audit_record_count",
            "source_audit_details_content_integrity_verified_count",
        ):
            if type(audit.get(key)) is int and audit[key] >= 0:
                compact_audit[key] = audit[key]
        adoption = audit.get("systemwide_native_adoption_ratio")
        if isinstance(adoption, dict):
            compact_audit["systemwide_native_adoption_ratio"] = {
                "status": "not-measured",
                **({"denominator": _short_text(adoption["denominator"], 96)}
                   if isinstance(adoption.get("denominator"), str) else {}),
                **({"reason": _short_text(adoption["reason"], 256)}
                   if isinstance(adoption.get("reason"), str) else {}),
            }
        raw_projects = audit.get("projects")
        raw_projects = raw_projects if isinstance(raw_projects, list) else []
        projects = []
        for project in raw_projects[:4]:
            if not isinstance(project, dict):
                continue
            compact_project = {
                key: _short_text(project.get(key), 192)
                for key in (
                    "repository", "owner_declared_status",
                    "owner_declared_native_surface",
                    "owner_declared_next_native_slice",
                )
                if isinstance(project.get(key), str)
            }
            responsibilities = project.get("remaining_host_responsibilities")
            if isinstance(responsibilities, list):
                compact_project["remaining_host_responsibilities"] = [
                    _short_text(item, 160)
                    for item in responsibilities[:8]
                    if isinstance(item, str)
                ]
                compact_project["remaining_host_responsibilities_truncated"] = (
                    len(responsibilities) > 8
                )
            if compact_project:
                projects.append(compact_project)
        compact_audit["projects"] = projects
        opportunity = audit.get("actionable_opportunity")
        if isinstance(opportunity, dict):
            compact_audit["actionable_opportunity"] = {
                key: _short_text(opportunity.get(key), 256)
                for key in (
                    "title", "candidate_boundary", "current_observation",
                    "readiness", "safe_next_step",
                )
                if isinstance(opportunity.get(key), str)
            }
        pressure = audit.get("pressure_hypothesis")
        if isinstance(pressure, dict):
            compact_audit["pressure_hypothesis"] = {
                key: _short_text(pressure.get(key), 192)
                for key in ("identity", "title", "category", "recurrence")
                if isinstance(pressure.get(key), str)
            }
        sources = audit.get("supporting_source_references")
        sources = sources if isinstance(sources, list) else []
        compact_audit["supporting_source_references"] = [
            {
                key: _short_text(source.get(key), 192 if key == "path" else 160)
                for key in (
                    "identity", "kind", "repository", "commit", "path", "sha256",
                    "status", "content_integrity_status",
                )
                if isinstance(source.get(key), str)
            }
            for source in sources[:8]
            if isinstance(source, dict)
        ]
        if type(audit.get("supporting_source_references_truncated")) is bool:
            compact_audit["supporting_source_references_truncated"] = audit[
                "supporting_source_references_truncated"
            ]
        raw_source_audits = audit.get("source_audit_details")
        raw_source_audits = (
            raw_source_audits if isinstance(raw_source_audits, list) else []
        )
        source_audits = []
        for source_audit in raw_source_audits[:MAX_NATIVE_USERLAND_AUDIT_RECORDS]:
            if not isinstance(source_audit, dict):
                continue
            compact_source_audit = {
                key: _short_text(source_audit.get(key), 192)
                for key in (
                    "identity", "repository", "source_record_status",
                    "native_entrypoint", "canonical_entrypoint",
                )
                if isinstance(source_audit.get(key), str)
            }
            if type(source_audit.get("migration_order")) is int:
                compact_source_audit["migration_order"] = source_audit[
                    "migration_order"
                ]
            for key in (
                "canonical_mncs_surfaces", "existing_pressures",
                "rust_boundary_only",
            ):
                raw_values = source_audit.get(key)
                if not isinstance(raw_values, list):
                    continue
                compact_source_audit[key] = [
                    _short_text(item, 160)
                    for item in raw_values[:MAX_NATIVE_USERLAND_AUDIT_ITEMS]
                    if isinstance(item, str)
                ]
                compact_source_audit[f"{key}_truncated"] = (
                    len(raw_values) > MAX_NATIVE_USERLAND_AUDIT_ITEMS
                )
            for key in ("remaining_host_responsibilities", "responsibilities"):
                raw_items = source_audit.get(key)
                if not isinstance(raw_items, list):
                    continue
                compact_items = []
                for item in raw_items[:MAX_NATIVE_USERLAND_AUDIT_ITEMS]:
                    if not isinstance(item, dict):
                        continue
                    compact_items.append({
                        field: _short_text(item.get(field), 192)
                        for field in (
                            "area", "classification", "eliminating_capability",
                            "canonical_owner",
                        )
                        if isinstance(item.get(field), str)
                    })
                compact_source_audit[key] = compact_items
                compact_source_audit[f"{key}_truncated"] = (
                    len(raw_items) > MAX_NATIVE_USERLAND_AUDIT_ITEMS
                )
            raw_paths = source_audit.get("remaining_host_paths")
            if isinstance(raw_paths, list):
                compact_source_audit["remaining_host_paths"] = [
                    {
                        field: _short_text(item.get(field), 192)
                        for field in ("path", "role")
                        if isinstance(item.get(field), str)
                    }
                    for item in raw_paths[:MAX_NATIVE_USERLAND_AUDIT_ITEMS]
                    if isinstance(item, dict)
                ]
                compact_source_audit["remaining_host_paths_truncated"] = (
                    len(raw_paths) > MAX_NATIVE_USERLAND_AUDIT_ITEMS
                )
            verification = source_audit.get("verification_plan_ingestion")
            if isinstance(verification, dict):
                compact_source_audit["verification_plan_ingestion"] = {
                    key: verification[key]
                    for key in ("shell_projection", "jq_required")
                    if type(verification.get(key)) is bool
                } | {
                    key: _short_text(verification.get(key), 256)
                    for key in (
                        "status", "entrypoint", "selection_authority",
                        "remaining_blocker",
                    )
                    if isinstance(verification.get(key), str)
                }
            projection = source_audit.get("native_semantic_projection")
            if isinstance(projection, dict):
                compact_source_audit["native_semantic_projection"] = {
                    key: _short_text(projection.get(key), 192)
                    for key in ("test", "suite", "obligation_selection")
                    if isinstance(projection.get(key), str)
                }
            pressure_links = source_audit.get("pressure_source_links")
            if isinstance(pressure_links, dict):
                raw_links = pressure_links.get("links")
                raw_links = raw_links if isinstance(raw_links, list) else []
                compact_links = []
                for link in raw_links[:MAX_NATIVE_USERLAND_AUDIT_ITEMS]:
                    if not isinstance(link, dict):
                        continue
                    compact_link = {
                        key: _short_text(link.get(key), 192)
                        for key in (
                            "identity", "status", "causal_relation_status",
                            "relationship_basis", "source_content_integrity_status",
                            "source_kind",
                        )
                        if isinstance(link.get(key), str)
                    }
                    source_reference = _validate_reference(
                        link.get("source_reference"),
                    )
                    if source_reference is not None:
                        compact_link["source_reference"] = source_reference
                    if compact_link:
                        compact_links.append(compact_link)
                compact_source_audit["pressure_source_links"] = {
                    **{
                        key: _short_text(pressure_links.get(key), 256)
                        for key in (
                            "status", "relationship_basis",
                            "causal_relation_status", "interpretation_limit",
                        )
                        if isinstance(pressure_links.get(key), str)
                    },
                    **({
                        "supporting_source_count_examined": pressure_links[
                            "supporting_source_count_examined"
                        ],
                    } if type(pressure_links.get(
                        "supporting_source_count_examined",
                    )) is int and pressure_links[
                        "supporting_source_count_examined"
                    ] >= 0 else {}),
                    **({
                        "supporting_source_window_truncated": pressure_links[
                            "supporting_source_window_truncated"
                        ],
                    } if type(pressure_links.get(
                        "supporting_source_window_truncated",
                    )) is bool else {}),
                    "links": compact_links,
                    "links_truncated": (
                        pressure_links.get("links_truncated") is True
                        or len(raw_links) > len(compact_links)
                    ),
                }
            if compact_source_audit:
                source_audits.append(compact_source_audit)
        if source_audits:
            compact_audit["source_audit_details"] = source_audits
            compact_audit["source_audit_details_truncated"] = (
                len(raw_source_audits) > len(source_audits)
            )
        if isinstance(audit.get("source_audit_details_status"), str):
            compact_audit["source_audit_details_status"] = _short_text(
                audit["source_audit_details_status"], 96,
            )
        reference = _validate_reference(audit.get("evidence_reference"))
        if reference is not None:
            compact_audit["evidence_reference"] = reference
        raw_measurement_limits = audit.get("measurement_limits")
        raw_measurement_limits = (
            raw_measurement_limits
            if isinstance(raw_measurement_limits, list) else []
        )
        compact_audit["measurement_limits"] = [
            _short_text(item, 192)
            for item in raw_measurement_limits[:8]
            if isinstance(item, str)
        ]
        compact_audits.append(compact_audit)
    if not compact_audits:
        return None
    return {
        "observed_audit_record_count": value.get(
            "observed_audit_record_count", len(audits),
        ),
        "audits_truncated": len(audits) > len(compact_audits),
        "audits": compact_audits,
        "implementation_ownership_status": "owner-declared; not independently verified",
        "systemwide_native_adoption_ratio": {
            "status": "not-measured",
            "reason": "Owner audit samples do not establish a complete implementation inventory or denominator.",
        },
    }


def _compiler_capability_contract_view(value: Any) -> dict[str, Any] | None:
    """Keep the exact Compiler capability boundary distinct from Environment delivery."""
    if not isinstance(value, dict):
        return None
    required_identity = value.get("required_identity")
    dispatch_alias = value.get("runtime_dispatch_alias")
    raw_effects = value.get("effect_contract")
    proof_relationship = value.get("proof_relationship")
    canonical_execution_relationship = value.get(
        "canonical_execution_relationship",
    )
    environment_delivery = value.get("environment_delivery")
    if (not isinstance(required_identity, str) or not required_identity
            or len(required_identity) > 256
            or not isinstance(dispatch_alias, str) or not dispatch_alias
            or len(dispatch_alias) > 128
            or not isinstance(raw_effects, list) or len(raw_effects) != 2
            or proof_relationship != "compiler-artifact-admission-matches"
            or canonical_execution_relationship
            != "declared-effects-matched-across-proof-cases"
            or environment_delivery != "not-measured-by-this-compiler-proof"):
        return None
    effects: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw_effect in raw_effects:
        if not isinstance(raw_effect, dict):
            return None
        capability = raw_effect.get("capability")
        kind = raw_effect.get("kind")
        target = raw_effect.get("target")
        if (capability != dispatch_alias
                or not isinstance(kind, str)
                or kind not in {"structured_read", "structured_write"}
                or not isinstance(target, str) or not target or len(target) > 128):
            return None
        effect_key = (capability, kind, target)
        if effect_key in seen:
            return None
        seen.add(effect_key)
        effects.append({"capability": capability, "kind": kind, "target": target})
    if {effect["kind"] for effect in effects} != {
        "structured_read", "structured_write",
    }:
        return None
    return {
        "required_identity": required_identity,
        "runtime_dispatch_alias": dispatch_alias,
        "effect_contract": effects,
        "proof_relationship": proof_relationship,
        "canonical_execution_relationship": canonical_execution_relationship,
        "environment_delivery": environment_delivery,
    }


def _compact_context_response_inclusion(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    compact = {
        key: _short_text(value[key], 160)
        for key in (
            "status", "reason", "basis", "identity_basis",
            "binding_id_attribution_status", "response_binding_ids_status",
            "comparison_time_basis",
            "scope", "event_window_status",
        )
        if isinstance(value.get(key), str)
    }
    omitted = value.get("response_summaries_omitted")
    if type(omitted) is int:
        compact["response_summaries_omitted"] = max(0, omitted)
    references = value.get("supporting_events")
    if isinstance(references, list):
        compact["supporting_events"] = [
            {
                key: reference[key]
                for key in ("identity", "sequence", "type")
                if key in reference
            }
            for reference in references[:MAX_CAPABILITY_SUPPORTING_EVENTS]
            if isinstance(reference, dict)
        ]
    return compact


def _compact_delivery_binding(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result = {
        key: _short_text(value[key], limit)
        for key, limit in (
            ("provider", 128), ("capability", 192),
            ("binding_id", 160), ("contract_revision", 160),
            ("availability_status", 48), ("use_observation_status", 96),
            ("provider_attribution", 96), ("invocation_count_basis", 96),
            ("binding_attribution_status", 96),
        )
        if isinstance(value.get(key), str)
    }
    for key in ("invocation_count", "completion_count"):
        if type(value.get(key)) is int and value[key] >= 0:
            result[key] = value[key]
    outcomes = value.get("outcome_status_counts")
    if isinstance(outcomes, dict):
        result["outcome_status_counts"] = {
            _short_text(name, 48): count
            for name, count in sorted(
                ((name, count) for name, count in outcomes.items()
                 if isinstance(name, str)), key=lambda item: item[0],
            )[:8]
            if type(count) is int and count >= 0
        }
    references = value.get("supporting_events")
    if isinstance(references, list):
        result["supporting_events"] = [
            {
                key: reference[key]
                for key in ("identity", "sequence", "type")
                if (key in reference and (
                    isinstance(reference[key], str)
                    or (key == "sequence" and type(reference[key]) is int
                        and reference[key] >= 0)
                ))
            }
            for reference in references[:3]
            if isinstance(reference, dict)
        ]
    context_inclusion = _compact_context_response_inclusion(
        value.get("context_response_inclusion"),
    )
    if context_inclusion is not None:
        result["context_response_inclusion"] = context_inclusion
    return result


def _compact_count_map(value: Any, *, key_limit: int = 64) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {
        _short_text(key, key_limit): count
        for key, count in sorted(
            (
                (key, count)
                for key, count in value.items()
                if isinstance(key, str) and type(count) is int and count >= 0
            ),
            key=lambda item: item[0],
        )[:8]
    }


def _compact_binding_evidence_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    summary = {
        key: _short_text(value[key], limit)
        for key, limit in (
            ("schema_version", 96), ("source_basis", 128),
            ("event_window_status", 64), ("discoverability_status", 64),
            ("agent_receipt_status", 64), ("interpretation_limit", 512),
        )
        if isinstance(value.get(key), str)
    }
    for key in (
        "binding_count", "exact_binding_invoked_binding_count",
        "exact_binding_invocation_count", "exact_binding_completion_count",
        "binding_details_omitted_count", "binding_records_not_examined",
        "binding_records_invalid",
    ):
        count = value.get(key)
        if type(count) is int and count >= 0:
            summary[key] = count
    if type(value.get("event_window_complete_history")) is bool:
        summary["event_window_complete_history"] = value[
            "event_window_complete_history"
        ]
    if type(value.get("binding_details_truncated")) is bool:
        summary["binding_details_truncated"] = value[
            "binding_details_truncated"
        ]
    for key in (
        "availability_status_counts",
        "context_response_inclusion_status_counts",
        "context_response_binding_id_attribution_status_counts",
        "context_response_binding_id_list_status_counts",
        "use_observation_status_counts",
        "binding_attribution_status_counts",
        "context_response_use_sequence_status_counts",
        "exact_binding_outcome_status_counts",
    ):
        counts = _compact_count_map(value.get(key))
        if counts:
            summary[key] = counts
    for key in (
        "context_response_supporting_events",
        "exact_binding_invocation_supporting_events",
    ):
        references = value.get(key)
        if isinstance(references, list):
            summary[key] = [
                {
                    event_key: reference[event_key]
                    for event_key in ("identity", "sequence", "type")
                    if event_key in reference
                }
                for reference in references[:MAX_CAPABILITY_SUPPORTING_EVENTS]
                if isinstance(reference, dict)
            ]
    for key in (
        "context_response_supporting_events_omitted",
        "exact_binding_invocation_supporting_events_omitted",
    ):
        count = value.get(key)
        if type(count) is int and count >= 0:
            summary[key] = count
    return summary


def _selected_session_delivery_observation(
    contract: Any, delivery: Any, *, session_id: str,
) -> dict[str, Any] | None:
    """Join a compiler contract to selected-session delivery without alias inference."""
    contract_view = _compiler_capability_contract_view(contract)
    if contract_view is None:
        return None
    required_identity = contract_view["required_identity"]
    dispatch_alias = contract_view["runtime_dispatch_alias"]
    delivery_view = delivery if isinstance(delivery, dict) else {}
    raw_bindings = delivery_view.get("bindings")
    raw_bindings = raw_bindings if isinstance(raw_bindings, list) else []
    exact_matches = [
        item for item in raw_bindings
        if isinstance(item, dict) and item.get("capability") == required_identity
    ]
    alias_matches = [
        item for item in raw_bindings
        if isinstance(item, dict) and item.get("capability") == dispatch_alias
    ]
    delivery_status = delivery_view.get("status")
    complete = (
        delivery_status == "ready"
        and delivery_view.get("bindings_omitted", 0) == 0
        and delivery_view.get("binding_records_not_examined", 0) == 0
    )
    if exact_matches:
        status, match_kind = "canonical-identity-match", "canonical-identity"
    elif alias_matches:
        status = "ambiguous-runtime-alias" if len(alias_matches) > 1 else "runtime-alias-only-match"
        match_kind = "runtime-alias-only"
    elif delivery_status == "unavailable" or not delivery_view:
        status, match_kind = "session-delivery-view-unavailable", "none"
    elif complete:
        status, match_kind = "not-present-in-selected-session-binding-snapshot", "none"
    else:
        status, match_kind = "not-observed-in-incomplete-session-binding-snapshot", "none"
    matches = exact_matches or alias_matches
    return {
        "status": status,
        "scope": "selected-session-current-binding-snapshot-and-event-window",
        "session_id": _short_text(session_id, 160),
        "required_identity": required_identity,
        "runtime_dispatch_alias": dispatch_alias,
        "match_kind": match_kind,
        "canonical_identity_match_count": len(exact_matches),
        "runtime_alias_match_count": len(alias_matches),
        "binding_matches": [
            compact for item in matches[:4]
            if (compact := _compact_delivery_binding(item)) is not None
        ],
        "discoverability_status": "not-measured",
        "agent_receipt_status": "not-established",
        "measurement_limits": [
            "The Compiler proof establishes its admitted artifact contract, not Environment delivery.",
            "A runtime alias match does not establish canonical identity or provider attribution.",
            "Selected-session availability and invocation do not establish agent receipt or discovery.",
            "Use observations cover only this session's bounded event window.",
        ],
    }


def _compact_selected_session_delivery_observation(
    value: Any,
) -> dict[str, Any] | None:
    statuses = {
        "canonical-identity-match", "runtime-alias-only-match",
        "ambiguous-runtime-alias",
        "not-present-in-selected-session-binding-snapshot",
        "not-observed-in-incomplete-session-binding-snapshot",
        "session-delivery-view-unavailable",
    }
    if not isinstance(value, dict) or not isinstance(value.get("status"), str):
        return None
    status = value["status"]
    if status not in statuses:
        return None
    required_identity = value.get("required_identity")
    dispatch_alias = value.get("runtime_dispatch_alias")
    session_id = value.get("session_id")
    if not all(
        isinstance(item, str) and item and len(item) <= limit
        for item, limit in (
            (required_identity, 256), (dispatch_alias, 128), (session_id, 160),
        )
    ):
        return None
    raw_matches = value.get("binding_matches")
    raw_matches = raw_matches if isinstance(raw_matches, list) else []
    limits = value.get("measurement_limits")
    return {
        "status": status,
        "scope": "selected-session-current-binding-snapshot-and-event-window",
        "session_id": session_id,
        "required_identity": required_identity,
        "runtime_dispatch_alias": dispatch_alias,
        "match_kind": value.get("match_kind")
        if value.get("match_kind") in {
            "canonical-identity", "runtime-alias-only", "none",
        } else "none",
        "canonical_identity_match_count": value.get(
            "canonical_identity_match_count", 0,
        ) if type(value.get("canonical_identity_match_count")) is int else 0,
        "runtime_alias_match_count": value.get(
            "runtime_alias_match_count", 0,
        ) if type(value.get("runtime_alias_match_count")) is int else 0,
        "binding_matches": [
            compact for item in raw_matches[:4]
            if (compact := _compact_delivery_binding(item)) is not None
        ],
        "discoverability_status": "not-measured",
        "agent_receipt_status": "not-established",
        "measurement_limits": [
            _short_text(item, 192)
            for item in limits[:4]
            if isinstance(item, str)
        ] if isinstance(limits, list) else [],
    }


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
        compact_diagnostic = {
            key: _short_text(diagnostic.get(key), 192 if key == "path" else 160)
            for key in (
                "identity", "record_identity", "repository", "commit",
                "sha256", "verification", "path", "correction_status",
                "correction_observation_identity",
            )
            if isinstance(diagnostic.get(key), str)
        }
        if type(diagnostic.get("path_truncated")) is bool:
            compact_diagnostic["path_truncated"] = diagnostic["path_truncated"]
        contract_diagnostics = diagnostic.get("contract_diagnostics")
        if isinstance(contract_diagnostics, list):
            compact_diagnostic["contract_diagnostics"] = [
                _short_text(value, 128) for value in contract_diagnostics[:8]
                if isinstance(value, str)
            ]
            if len(contract_diagnostics) > 8:
                compact_diagnostic["contract_diagnostics_truncated"] = True
        compact_diagnostics.append(compact_diagnostic)
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
        read_only_references = candidate.get(
            "workspace_read_only_evidence_references",
        )
        if isinstance(read_only_references, list) and read_only_references:
            compact_candidate["workspace_read_only_evidence_references"] = [
                {
                    key: _short_text(
                        reference[key], 192 if key == "path" else 160,
                    )
                    for key in (
                        "identity", "repository", "commit", "path", "sha256",
                        "verification", "repository_read_basis",
                    )
                    if isinstance(reference.get(key), str)
                }
                for reference in read_only_references[:4]
                if isinstance(reference, dict)
            ]
            compact_candidate[
                "workspace_read_only_evidence_references_truncated"
            ] = (
                candidate.get(
                    "workspace_read_only_evidence_references_truncated",
                ) is True
                or len(read_only_references) > 4
            )
        preflight_search = candidate.get("integration_preflight_search")
        if isinstance(preflight_search, dict):
            compact_candidate["integration_preflight_search"] = {
                key: preflight_search[key]
                for key in (
                    "status", "matched_records_returned",
                    "activity_records_omitted", "observation_records_truncated",
                    "session_event_window_truncated",
                    "event_page_before_sequence",
                )
                if key in preflight_search
            }
        raw_preflights = candidate.get("integration_preflight_observations")
        raw_preflights = raw_preflights if isinstance(raw_preflights, list) else []
        compact_preflights = []
        for preflight in raw_preflights[:4]:
            if not isinstance(preflight, dict):
                continue
            compact_preflight = {
                key: _short_text(preflight[key], 160)
                for key in ("action",)
                if isinstance(preflight.get(key), str)
            }
            for key in ("persistent_claim_required", "operation_executed"):
                if preflight.get(key) is False:
                    compact_preflight[key] = False
            scope = _authority_conflict_scope(
                preflight.get("scope"), candidate.get("affected_repository"),
            )
            if scope is not None:
                compact_preflight["scope"] = scope
            preflight_state = preflight.get("repository_preflight_state")
            if isinstance(preflight_state, dict):
                compact_preflight["repository_preflight_state"] = {
                    key: preflight_state[key]
                    for key in ("checkout_identity", "head", "working_tree_status")
                    if key in preflight_state
                }
            event_reference = preflight.get("event_reference")
            if isinstance(event_reference, dict):
                compact_preflight["event_reference"] = {
                    key: event_reference[key]
                    for key in ("identity", "sequence", "type")
                    if key in event_reference
                }
            compact_preflights.append(compact_preflight)
        if compact_preflights:
            compact_candidate["integration_preflight_observations"] = (
                compact_preflights
            )
        not_measured = candidate.get("not_measured")
        if isinstance(not_measured, list):
            compact_candidate["not_measured"] = [
                _short_text(value, 192)
                for value in not_measured[:3]
                if isinstance(value, str)
            ]
            if len(not_measured) > 3:
                compact_candidate["not_measured_truncated"] = True
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
        request_summary = _compact_request_summary(observation.get("request"))
        if request_summary is not None:
            compact_observation["observation"]["request_summary"] = (
                request_summary
            )
        pressure_hypothesis_role = item.get("pressure_hypothesis_role")
        if (isinstance(pressure_hypothesis_role, str)
                and pressure_hypothesis_role in PRESSURE_HYPOTHESIS_ROLES):
            compact_observation["pressure_hypothesis_role"] = (
                pressure_hypothesis_role
            )
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
                    "repository_context_basis", "repository_branch_capture_status",
                    "repository_head_capture_status",
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
        repository_read_basis = observation.get("source_repository_read_basis")
        if repository_read_basis in {
            "selected-checkout", "workspace-read-only-checkout",
        }:
            compact_observation["observation"]["source_repository_read_basis"] = (
                repository_read_basis
            )
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
                replacement_role = replacement_values.get(
                    "pressure_hypothesis_role",
                )
                if (isinstance(replacement_role, str)
                        and replacement_role in PRESSURE_HYPOTHESIS_ROLES):
                    compact_replacements["pressure_hypothesis_role"] = (
                        replacement_role
                    )
                recommendation = replacement_values.get(
                    "pressure_hypothesis.actionable_recommendation",
                )
                if isinstance(recommendation, str):
                    compact_replacements[
                        "pressure_hypothesis.actionable_recommendation"
                    ] = _short_text(recommendation, 512)
                for field in ("observation.request", "observation.outcome"):
                    payload = replacement_values.get(field)
                    if isinstance(payload, dict):
                        keys = [
                            _short_text(key, 64) for key in list(payload)[:12]
                            if isinstance(key, str)
                        ]
                        compact_replacements[field] = {
                            "keys": keys,
                            "key_count": len(payload),
                            "keys_truncated": len(payload) > len(keys),
                        }
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
                            "session_id", "module", "status", "registration_scope",
                            "content_integrity_status", "event_reference_status")
                if isinstance(source.get(key), str)
            }
            match_categories = source.get("event_reference_match_categories")
            if isinstance(match_categories, list):
                compact_supporting_source["event_reference_match_categories"] = [
                    _short_text(category, 48)
                    for category in match_categories[:4]
                    if isinstance(category, str)
                ]
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
            "claim_coordination_events_considered",
            "claim_coordination_records_returned",
            "claim_coordination_records_omitted",
            "claim_coordination_records_invalid",
            "claim_coordination_selection_policy",
            "proposal_eligibility_events_considered",
            "proposal_eligibility_records_returned",
            "proposal_eligibility_records_omitted",
            "proposal_eligibility_records_invalid",
            "proposal_preflight_state_observations_invalid",
            "proposal_eligibility_selection_policy",
            "coordination_deferral_events_considered",
            "coordination_deferral_records_returned",
            "coordination_deferral_records_omitted",
            "coordination_deferral_records_invalid",
            "coordination_deferral_selection_policy",
            "repository_ref_transition_events_considered",
            "repository_ref_transition_records_returned",
            "repository_ref_transition_records_omitted",
            "repository_ref_transition_records_invalid",
            "repository_ref_transition_events_invalid",
            "repository_ref_transition_selection_policy",
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
                "integration_denial_preflight_state_observations_invalid",
            )
            if key in activity_source
        })
        claim_lookup = activity_source.get("authority_claim_state_lookup")
        if isinstance(claim_lookup, dict):
            compact_activity_source["authority_claim_state_lookup"] = {
                key: claim_lookup[key]
                for key in (
                    "status", "lookup_count", "lookup_limit",
                    "lookup_omitted_count", "observed_at",
                )
                if key in claim_lookup
            }
    if activity_source.get("claim_recovery_events_considered", 0):
        compact_activity_source.update({
            key: activity_source[key]
            for key in (
                "claim_recovery_events_considered",
                "claim_recovery_records_returned",
                "claim_recovery_records_omitted",
                "claim_recovery_records_invalid",
                "claim_recovery_selection_policy",
            )
            if key in activity_source
        })
    if activity_source.get("operation_claim_recovery_events_considered", 0):
        compact_activity_source.update({
            key: activity_source[key]
            for key in (
                "operation_claim_recovery_events_considered",
                "operation_claim_recovery_records_returned",
                "operation_claim_recovery_records_omitted",
                "operation_claim_recovery_records_invalid",
                "operation_claim_recovery_selection_policy",
            )
            if key in activity_source
        })
    if activity_source.get("operation_deferral_events_considered", 0):
        compact_activity_source.update({
            key: activity_source[key]
            for key in (
                "operation_deferral_events_considered",
                "operation_deferral_records_returned",
                "operation_deferral_records_omitted",
                "operation_deferral_records_invalid",
                "operation_deferral_selection_policy",
            )
            if key in activity_source
        })
    claim_lifecycle_source = activity_source.get(
        "claim_lifecycle_correlation",
    )
    if isinstance(claim_lifecycle_source, dict):
        compact_activity_source["claim_lifecycle_correlation"] = {
            key: claim_lifecycle_source[key]
            for key in (
                "status", "correlation_records_considered",
                "correlation_records_returned", "correlation_records_omitted",
                "related_session_activity_records_considered",
                "related_session_activity_records_omitted",
                "owner_session_lookups_attempted", "owner_session_lookup_limit",
                "owner_session_lookups_omitted", "owner_event_page_limit",
                "owner_event_pages_unavailable", "owner_event_pages_truncated",
                "owner_session_states_unavailable",
                "selection_policy",
            )
            if key in claim_lifecycle_source
        }
    if activity_source.get("integration_eligibility_events_considered", 0):
        compact_activity_source.update({
            key: activity_source[key]
            for key in (
                "integration_eligibility_events_considered",
                "integration_eligibility_records_returned",
                "integration_eligibility_records_omitted",
                "integration_eligibility_records_invalid",
                "integration_preflight_state_observations_invalid",
            )
            if key in activity_source
        })
        claim_lookup = activity_source.get("integration_claim_state_lookup")
        if isinstance(claim_lookup, dict):
            compact_activity_source["integration_claim_state_lookup"] = {
                key: claim_lookup[key]
                for key in (
                    "status", "lookup_omitted_count",
                    "activity_lookup_count", "activity_lookup_limit",
                )
                if key in claim_lookup
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
        binding_identity = item.get("binding_identity")
        if isinstance(binding_identity, dict):
            compact_item["binding_identity"] = {
                key: _short_text(binding_identity[key], 160)
                for key in ("status", "provider", "binding_id", "contract_revision")
                if isinstance(binding_identity.get(key), str)
            }
        if type(item.get("returncode")) is int:
            compact_item["returncode"] = item["returncode"]
        effect_target = _invocation_effect_target(item.get("effect_target"))
        if effect_target is not None:
            compact_item["effect_target"] = effect_target
        for key in ("invocation_event", "completion_event"):
            reference = item.get(key)
            if isinstance(reference, dict):
                compact_item[key] = {
                    item_key: reference[item_key]
                    for item_key in ("identity", "sequence", "type")
                    if item_key in reference
                }
        compact_activity_interactions.append(compact_item)
    raw_activity_capabilities = activity.get("capabilities")
    raw_activity_capabilities = (
        raw_activity_capabilities
        if isinstance(raw_activity_capabilities, list) else []
    )
    compact_activity_capabilities = []
    for item in raw_activity_capabilities[:MAX_SESSION_ACTIVITY_CAPABILITIES]:
        if not isinstance(item, dict):
            continue
        compact_item = {
            key: _short_text(item.get(key), 160)
            for key in ("capability",)
            if isinstance(item.get(key), str)
        }
        for key in ("invocation_count", "completion_count"):
            if type(item.get(key)) is int and item[key] >= 0:
                compact_item[key] = item[key]
        binding_attribution = item.get("binding_identity_attribution")
        if isinstance(binding_attribution, dict):
            compact_item["binding_identity_attribution"] = {
                key: _short_text(binding_attribution[key], 128)
                if isinstance(binding_attribution.get(key), str)
                else binding_attribution[key]
                for key in (
                    "status", "complete_identity_count",
                    "identity_unavailable_count", "basis",
                )
                if (isinstance(binding_attribution.get(key), str)
                    or (type(binding_attribution.get(key)) is int
                        and binding_attribution[key] >= 0))
            }
        status_counts = item.get("outcome_status_counts")
        if isinstance(status_counts, dict):
            compact_item["outcome_status_counts"] = {
                _short_text(key, 64): value
                for key, value in sorted(status_counts.items())[:8]
                if isinstance(key, str) and type(value) is int and value >= 0
            }
        references = item.get("supporting_events")
        if isinstance(references, list):
            compact_item["supporting_events"] = [
                {
                    key: reference[key]
                    for key in ("identity", "sequence", "type")
                    if key in reference
                }
                for reference in references[:MAX_CAPABILITY_SUPPORTING_EVENTS]
                if isinstance(reference, dict)
            ]
        compact_activity_capabilities.append(compact_item)

    raw_capability_delivery = activity.get("capability_delivery")
    raw_capability_delivery = (
        raw_capability_delivery
        if isinstance(raw_capability_delivery, dict) else {}
    )
    compact_capability_delivery: dict[str, Any] = {}
    for key in (
        "status", "inventory_source", "discoverability_status",
    ):
        if isinstance(raw_capability_delivery.get(key), str):
            compact_capability_delivery[key] = _short_text(
                raw_capability_delivery[key], 96,
            )
    for key in (
        "binding_count", "binding_records_examined",
        "binding_records_not_examined", "binding_records_invalid", "bindings_omitted",
        "availability_transition_event_count",
        "availability_transition_events_omitted",
        "capabilities_not_in_current_binding_snapshot_omitted",
    ):
        if type(raw_capability_delivery.get(key)) is int:
            compact_capability_delivery[key] = raw_capability_delivery[key]
    availability_counts = raw_capability_delivery.get("availability_counts")
    if isinstance(availability_counts, dict):
        compact_capability_delivery["availability_counts"] = {
            _short_text(key, 48): value
            for key, value in sorted(availability_counts.items())[:8]
            if isinstance(key, str) and type(value) is int and value >= 0
        }
    use_event_window = raw_capability_delivery.get("use_event_window")
    if isinstance(use_event_window, dict):
        compact_capability_delivery["use_event_window"] = {
            key: use_event_window[key]
            for key in (
                "scope", "truncated", "complete_history",
                "invocation_summaries_omitted",
            )
            if key in use_event_window
        }
    compact_capability_bindings = []
    raw_capability_bindings = raw_capability_delivery.get("bindings")
    raw_capability_bindings = (
        raw_capability_bindings if isinstance(raw_capability_bindings, list) else []
    )
    for item in raw_capability_bindings[:16]:
        if not isinstance(item, dict):
            continue
        compact_item = {
            key: _short_text(item.get(key), 160)
            for key in (
                "provider", "capability", "availability_status", "binding_id",
                "contract_revision", "invocation_count_basis",
                "binding_attribution_status",
                "availability_verification", "availability_observed_at",
                "use_observation_status", "provider_attribution",
            )
            if isinstance(item.get(key), str)
        }
        for key in ("invocation_count", "completion_count"):
            if type(item.get(key)) is int and item[key] >= 0:
                compact_item[key] = item[key]
        status_counts = item.get("outcome_status_counts")
        if isinstance(status_counts, dict):
            compact_item["outcome_status_counts"] = {
                _short_text(key, 64): value
                for key, value in sorted(status_counts.items())[:8]
                if isinstance(key, str) and type(value) is int and value >= 0
            }
        references = item.get("supporting_events")
        if isinstance(references, list):
            compact_item["supporting_events"] = [
                {
                    key: reference[key]
                    for key in ("identity", "sequence", "type")
                    if key in reference
                }
                for reference in references[:MAX_CAPABILITY_SUPPORTING_EVENTS]
                if isinstance(reference, dict)
            ]
        availability_references = item.get("availability_event_references")
        if isinstance(availability_references, list):
            compact_item["availability_event_references"] = [
                {
                    key: reference[key]
                    for key in ("identity", "sequence", "type")
                    if key in reference
                }
                for reference in availability_references[:3]
                if isinstance(reference, dict)
            ]
        context_inclusion = item.get("context_response_inclusion")
        compact_context_inclusion = _compact_context_response_inclusion(
            context_inclusion,
        )
        if compact_context_inclusion is not None:
            compact_item["context_response_inclusion"] = compact_context_inclusion
        sequence_relation = item.get("context_response_use_sequence_relation")
        if isinstance(sequence_relation, dict):
            compact_sequence_relation = {
                key: _short_text(sequence_relation[key], 160)
                for key in (
                    "status", "reason", "context_response_inclusion_status",
                    "temporal_basis", "causal_claim", "event_window_status",
                    "response_comparison_time_basis",
                )
                if isinstance(sequence_relation.get(key), str)
            }
            for count_key in (
                "response_summaries_omitted", "invocation_summaries_omitted",
            ):
                if type(sequence_relation.get(count_key)) is int:
                    compact_sequence_relation[count_key] = max(
                        0, sequence_relation[count_key],
                    )
            for event_key in ("response_events",):
                references = sequence_relation.get(event_key)
                if isinstance(references, list):
                    compact_sequence_relation[event_key] = [
                        {
                            key: reference[key]
                            for key in ("identity", "sequence", "type")
                            if key in reference
                        }
                        for reference in references[:MAX_CAPABILITY_SUPPORTING_EVENTS]
                        if isinstance(reference, dict)
                    ]
            pairs = sequence_relation.get("observed_pairs")
            if isinstance(pairs, list):
                compact_sequence_relation["observed_pairs"] = [
                    {
                        key: {
                            reference_key: pair[key][reference_key]
                            for reference_key in ("identity", "sequence", "type")
                            if reference_key in pair[key]
                        }
                        for key in ("context_response_event", "invocation_event")
                        if isinstance(pair.get(key), dict)
                    } | ({"relation": _short_text(pair["relation"], 128)}
                         if isinstance(pair.get("relation"), str) else {})
                    for pair in pairs[:MAX_CAPABILITY_SUPPORTING_EVENTS]
                    if isinstance(pair, dict)
                ]
            compact_item["context_response_use_sequence_relation"] = (
                compact_sequence_relation
            )
        compact_capability_bindings.append(compact_item)
    if compact_capability_bindings:
        compact_capability_delivery["bindings"] = compact_capability_bindings
        if len(raw_capability_bindings) > len(compact_capability_bindings):
            compact_capability_delivery["bindings_truncated"] = True
    binding_evidence_summary = _compact_binding_evidence_summary(
        raw_capability_delivery.get("binding_evidence_summary"),
    )
    if binding_evidence_summary is not None:
        omitted_details = binding_evidence_summary.get(
            "binding_details_omitted_count", 0,
        )
        omitted_details = omitted_details if type(omitted_details) is int else 0
        omitted_details += max(
            0, len(raw_capability_bindings) - len(compact_capability_bindings),
        )
        binding_evidence_summary["binding_details_omitted_count"] = omitted_details
        binding_evidence_summary["binding_details_truncated"] = (
            binding_evidence_summary.get("binding_details_truncated") is True
            or omitted_details > 0
            or len(raw_capability_bindings) > len(compact_capability_bindings)
        )
        compact_capability_delivery[
            "binding_evidence_summary"
        ] = binding_evidence_summary
    transition_events = raw_capability_delivery.get("availability_transition_events")
    if isinstance(transition_events, list):
        compact_capability_delivery["availability_transition_events"] = [
            {
                **({"capability": _short_text(item["capability"], 160)}
                   if isinstance(item.get("capability"), str) else {}),
                **({"provider": _short_text(item["provider"], 128)}
                   if isinstance(item.get("provider"), str) else {}),
                **({"previous_availability_status": _short_text(
                    item["previous_availability_status"], 64,
                )} if isinstance(item.get("previous_availability_status"), str) else {}),
                **({"event_reference": {
                    key: item["event_reference"][key]
                    for key in ("identity", "sequence", "type")
                    if key in item["event_reference"]
                }} if isinstance(item.get("event_reference"), dict) else {}),
            }
            for item in transition_events[:8]
            if isinstance(item, dict)
        ]
    outside_inventory = raw_capability_delivery.get(
        "capabilities_not_in_current_binding_snapshot",
    )
    if isinstance(outside_inventory, list):
        compact_capability_delivery[
            "capabilities_not_in_current_binding_snapshot"
        ] = [
            {
                "capability": _short_text(item["capability"], 160),
                "invocation_count": item.get("invocation_count", 0),
                "outcome_status_counts": dict(item.get("outcome_status_counts", {})),
                "interpretation": _short_text(item.get("interpretation"), 128),
                "supporting_events": [
                    {
                        key: reference[key]
                        for key in ("identity", "sequence", "type")
                        if key in reference
                    }
                    for reference in item.get("supporting_events", [])[:2]
                    if isinstance(reference, dict)
                ],
            }
            for item in outside_inventory[:8]
            if isinstance(item, dict) and isinstance(item.get("capability"), str)
        ]
    context_activity = raw_capability_delivery.get("context_operation_activity")
    if isinstance(context_activity, dict):
        compact_context_activity = {
            key: _short_text(context_activity[key], 128)
            for key in (
                "status", "scope", "response_capability_list_status",
                "agent_receipt_status", "binding_snapshot_comparison_basis",
            )
            if isinstance(context_activity.get(key), str)
        }
        outcome_counts = context_activity.get("outcome_counts")
        if isinstance(outcome_counts, dict):
            compact_context_activity["outcome_counts"] = {
                _short_text(key, 48): value
                for key, value in sorted(outcome_counts.items())[:8]
                if isinstance(key, str) and type(value) is int and value >= 0
            }
        for count_key in (
            "response_capture_status_counts",
            "response_identity_digest_status_counts",
        ):
            counts = context_activity.get(count_key)
            if isinstance(counts, dict):
                compact_context_activity[count_key] = {
                    _short_text(key, 48): value
                    for key, value in sorted(counts.items())[:8]
                    if isinstance(key, str) and type(value) is int and value >= 0
                }
        response_summaries = context_activity.get("response_summaries")
        response_summaries = (
            response_summaries if isinstance(response_summaries, list) else []
        )
        compact_response_summaries = []
        for summary in response_summaries[:MAX_CAPABILITY_SUPPORTING_EVENTS]:
            if not isinstance(summary, dict):
                continue
            compact_summary = {
                key: _short_text(summary[key], 128)
                for key in (
                    "schema_version", "capture_status", "capture_reason",
                    "context_schema_version",
                    "identity_digest_status", "available_identity_digest",
                    "binding_ids_status",
                )
                if isinstance(summary.get(key), str)
            }
            binding_ids = summary.get("available_binding_ids")
            if isinstance(binding_ids, list):
                compact_summary["available_binding_ids"] = [
                    _short_text(item, 80)
                    for item in binding_ids[:MAX_CONTEXT_RESPONSE_CAPABILITY_LIST]
                    if isinstance(item, str)
                ]
            for key in (
                "available_count", "listed_available_count", "unavailable_count",
                "unknown_count",
            ):
                if type(summary.get(key)) is int:
                    compact_summary[key] = summary[key]
            for key in ("capabilities_truncated",):
                if type(summary.get(key)) is bool:
                    compact_summary[key] = summary[key]
            reference = summary.get("event_reference")
            if isinstance(reference, dict):
                compact_summary["event_reference"] = {
                    item_key: reference[item_key]
                    for item_key in ("identity", "sequence", "type")
                    if item_key in reference
                }
            comparison = summary.get("binding_snapshot_comparison")
            if isinstance(comparison, dict):
                compact_comparison = {
                    key: _short_text(comparison[key], 128)
                    for key in ("status", "basis", "reason")
                    if isinstance(comparison.get(key), str)
                }
                for key in (
                    "available_count_matches",
                    "unavailable_count_matches",
                    "unknown_count_matches",
                    "available_identity_digest_matches",
                ):
                    if type(comparison.get(key)) is bool:
                        compact_comparison[key] = comparison[key]
                compact_summary["binding_snapshot_comparison"] = (
                    compact_comparison
                )
            compact_response_summaries.append(compact_summary)
        compact_context_activity["response_summaries"] = compact_response_summaries
        if type(context_activity.get("response_summaries_omitted")) is int:
            compact_context_activity["response_summaries_omitted"] = max(
                0, context_activity["response_summaries_omitted"],
            )
        if isinstance(context_activity.get("response_summary_scope"), str):
            compact_context_activity["response_summary_scope"] = _short_text(
                context_activity["response_summary_scope"], 128,
            )
        context_limits = context_activity.get("measurement_limits")
        if isinstance(context_limits, list):
            compact_context_activity["measurement_limits"] = [
                _short_text(item, 256)
                for item in context_limits[:3]
                if isinstance(item, str)
            ]
        compact_capability_delivery["context_operation_activity"] = (
            compact_context_activity
        )
    delivery_limits = raw_capability_delivery.get("measurement_limits")
    if isinstance(delivery_limits, list):
        compact_capability_delivery["measurement_limits"] = [
            _short_text(item, 256)
            for item in delivery_limits[:4]
            if isinstance(item, str)
        ]
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
    compact_claim_recoveries = []
    raw_claim_recoveries = activity.get("claim_recoveries")
    raw_claim_recoveries = (
        raw_claim_recoveries if isinstance(raw_claim_recoveries, list) else []
    )
    for item in raw_claim_recoveries[:MAX_SESSION_ACTIVITY_CLAIM_RECOVERIES]:
        if not isinstance(item, dict):
            continue
        compact_item = {
            key: _short_text(item.get(key), 160)
            for key in (
                "identity", "status", "repository", "basis", "claim_id",
                "campaign_id", "campaign_reference_status", "work_intent_id",
                "work_intent_reference_status",
            )
            if isinstance(item.get(key), str)
        }
        for key in (
            "claim_version", "recovered_count",
            "recovered_from_omitted_count",
        ):
            if type(item.get(key)) is int:
                compact_item[key] = item[key]
        if isinstance(item.get("interpretation"), str):
            compact_item["interpretation"] = _short_text(
                item["interpretation"], 256,
            )
        if isinstance(item.get("scope"), dict):
            compact_item["scope"] = item["scope"]
        recovered_from = item.get("recovered_from")
        if isinstance(recovered_from, list):
            compact_item["recovered_from"] = [
                entry for entry in recovered_from[:4]
                if isinstance(entry, dict)
            ]
            if len(recovered_from) > 4:
                compact_item["recovered_from_truncated"] = True
        reference = item.get("event_reference")
        if isinstance(reference, dict):
            compact_item["event_reference"] = {
                key: reference[key]
                for key in ("identity", "sequence", "type")
                if key in reference
            }
        compact_claim_recoveries.append(compact_item)
    compact_operation_claim_recoveries = []
    raw_operation_claim_recoveries = activity.get("operation_claim_recoveries")
    raw_operation_claim_recoveries = (
        raw_operation_claim_recoveries
        if isinstance(raw_operation_claim_recoveries, list) else []
    )
    for item in raw_operation_claim_recoveries[
        :MAX_SESSION_ACTIVITY_CLAIM_RECOVERIES
    ]:
        if not isinstance(item, dict):
            continue
        compact_item = {
            key: _short_text(item.get(key), 160)
            for key in ("identity", "status", "action", "repository")
            if isinstance(item.get(key), str)
        }
        if isinstance(item.get("scope"), dict):
            compact_item["scope"] = item["scope"]
        if type(item.get("recovered_count")) is int:
            compact_item["recovered_count"] = item["recovered_count"]
        recovered_from = item.get("recovered_from")
        if isinstance(recovered_from, list):
            compact_item["recovered_from"] = [
                entry for entry in recovered_from[:4]
                if isinstance(entry, dict)
            ]
            if len(recovered_from) > 4:
                compact_item["recovered_from_truncated"] = True
        for key in (
            "authority_decision_event", "authority_denial_event",
            "event_reference",
        ):
            reference = item.get(key)
            if isinstance(reference, dict):
                compact_item[key] = {
                    field: reference[field]
                    for field in ("identity", "sequence", "type")
                    if field in reference
                }
        compact_operation_claim_recoveries.append(compact_item)
    compact_claim_coordination = []
    raw_claim_coordination = activity.get("claim_coordination")
    raw_claim_coordination = (
        raw_claim_coordination if isinstance(raw_claim_coordination, list) else []
    )
    for item in raw_claim_coordination[:MAX_SESSION_ACTIVITY_CLAIM_COORDINATION]:
        if not isinstance(item, dict):
            continue
        compact_item = {
            key: _short_text(item.get(key), 192)
            for key in (
                "identity", "action", "status", "repository",
                "repository_status", "scope_status", "claim_id",
                "successor_claim_id", "basis", "campaign_id",
                "campaign_reference_status", "work_intent_id",
                "work_intent_reference_status",
                "interpretation",
            )
            if isinstance(item.get(key), str)
        }
        for key in ("claim_version", "released_count"):
            if type(item.get(key)) is int and item[key] >= 0:
                compact_item[key] = item[key]
        if isinstance(item.get("scope"), dict):
            compact_item["scope"] = item["scope"]
        for key in ("owner_session_id", "new_session_id", "to_session_id"):
            if isinstance(item.get(key), str):
                compact_item[key] = _short_text(item[key], 160)
        reference = item.get("event_reference")
        if isinstance(reference, dict):
            compact_item["event_reference"] = {
                key: reference[key]
                for key in ("identity", "sequence", "type")
                if key in reference
            }
        compact_claim_coordination.append(compact_item)
    compact_claim_lifecycle_correlations = []
    raw_claim_lifecycle_correlations = activity.get(
        "claim_lifecycle_correlations",
    )
    raw_claim_lifecycle_correlations = (
        raw_claim_lifecycle_correlations
        if isinstance(raw_claim_lifecycle_correlations, list) else []
    )
    for item in raw_claim_lifecycle_correlations[
        :MAX_COMPACT_SESSION_ACTIVITY_CLAIM_LIFECYCLE_CORRELATIONS
    ]:
        if not isinstance(item, dict):
            continue
        compact_item = {
            key: _short_text(item.get(key), 192)
            for key in (
                "identity", "repository", "owner_session_id", "claim_id",
                "status", "causal_resolution_status",
            )
            if isinstance(item.get(key), str)
        }
        for key in (
            "claim_lifecycle_event_count",
            "claim_lifecycle_records_omitted",
            "related_session_activity_count",
        ):
            if type(item.get(key)) is int and item[key] >= 0:
                compact_item[key] = item[key]
        page = item.get("owner_event_page")
        if isinstance(page, dict):
            compact_item["owner_event_page"] = {
                key: page[key]
                for key in ("status", "truncated", "limit")
                if key in page
            }
        raw_related_activity = item.get("related_session_activity")
        raw_related_activity = (
            raw_related_activity if isinstance(raw_related_activity, list) else []
        )
        compact_related_activity = []
        for related in raw_related_activity[:3]:
            if not isinstance(related, dict):
                continue
            compact_related = {
                key: _short_text(related.get(key), 160)
                for key in ("kind",)
                if isinstance(related.get(key), str)
            }
            reference = related.get("event_reference")
            if isinstance(reference, dict):
                compact_related["event_reference"] = {
                    key: reference[key]
                    for key in ("identity", "sequence", "type")
                    if key in reference
                }
            if compact_related:
                compact_related_activity.append(compact_related)
        if compact_related_activity:
            compact_item["related_session_activity"] = compact_related_activity
            if len(raw_related_activity) > len(compact_related_activity):
                compact_item["related_session_activity_projection_truncated"] = True
                compact_item["related_session_activity_projection_omitted_count"] = (
                    len(raw_related_activity) - len(compact_related_activity)
                )
        lifecycle_events = item.get("claim_lifecycle_events")
        if isinstance(lifecycle_events, list):
            compact_lifecycle_events = []
            for lifecycle_event in lifecycle_events[:1]:
                if not isinstance(lifecycle_event, dict):
                    continue
                compact_event = {
                    key: _short_text(lifecycle_event[key], 192)
                    for key in ("action",)
                    if isinstance(lifecycle_event.get(key), str)
                }
                event_reference = lifecycle_event.get("event_reference")
                if isinstance(event_reference, dict):
                    compact_event["event_reference"] = {
                        key: event_reference[key]
                        for key in ("identity", "sequence", "type")
                        if key in event_reference
                    }
                compact_lifecycle_events.append(compact_event)
            compact_item["claim_lifecycle_events"] = compact_lifecycle_events
            omitted_lifecycle_events = max(
                0, len(lifecycle_events) - len(compact_lifecycle_events),
            )
            if omitted_lifecycle_events:
                compact_item[
                    "claim_lifecycle_events_projection_truncated"
                ] = True
                compact_item[
                    "claim_lifecycle_events_projection_omitted_count"
                ] = omitted_lifecycle_events
        compact_claim_lifecycle_correlations.append(compact_item)
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
                "claim_conflict_evidence_status", "campaign_id", "work_intent_id",
                "work_intent_reference_status",
                "integration_preflight_state_status",
            )
            if isinstance(item.get(key), str)
        }
        if isinstance(item.get("claim_conflict"), dict):
            raw_conflict = item["claim_conflict"]
            compact_conflict = {
                key: raw_conflict[key]
                for key in ("repository", "conflict_reason")
                if isinstance(raw_conflict.get(key), str)
            }
            requested_scope = _authority_conflict_scope(
                raw_conflict.get("requested_scope"),
                raw_conflict.get("repository"),
            )
            if requested_scope is not None:
                compact_conflict["requested_scope"] = requested_scope
            blocker = raw_conflict.get("blocking_claim")
            if isinstance(blocker, dict):
                compact_blocker = {
                    key: _short_text(blocker[key], 160)
                    for key in (
                        "session_id", "claim_id",
                        "claim_lifecycle_correlation_identity",
                    )
                    if isinstance(blocker.get(key), str)
                }
                blocker_scope = _authority_conflict_scope(
                    blocker.get("scope"), raw_conflict.get("repository"),
                )
                if blocker_scope is not None:
                    compact_blocker["scope"] = blocker_scope
                compact_conflict["blocking_claim"] = compact_blocker
            decision_basis = raw_conflict.get("decision_basis")
            if isinstance(decision_basis, dict):
                compact_conflict["decision_basis"] = {
                    key: decision_basis[key]
                    for key in (
                        "authorization_status", "eligibility_status",
                        "persistent_authority_required",
                    )
                    if key in decision_basis
                }
            decision_state = raw_conflict.get("decision_claim_state")
            if isinstance(decision_state, dict):
                compact_conflict["decision_claim_state"] = {
                    key: decision_state[key]
                    for key in (
                        "observed_at", "claim_version", "blocking_status",
                        "recovery_verdict", "freshness", "recommended_operation",
                    )
                    if key in decision_state
                }
            decision_status = raw_conflict.get(
                "decision_claim_state_status",
            )
            if isinstance(decision_status, str):
                compact_conflict["decision_claim_state_status"] = decision_status
            current_state = raw_conflict.get("current_claim_state")
            if isinstance(current_state, dict):
                compact_conflict["current_claim_state"] = {
                    key: current_state[key]
                    for key in (
                        "lookup_status", "basis", "claim_version",
                        "blocking_status", "recovery_verdict", "freshness",
                        "owner_liveness_evidence_status",
                        "recorded_owner_relation",
                    )
                    if key in current_state
                }
            guidance = raw_conflict.get("coordination_guidance")
            if isinstance(guidance, dict):
                compact_guidance = {
                    key: guidance[key]
                    for key in (
                        "status", "next_step", "claim_mutated",
                        "ownership_transfer",
                    )
                    if key in guidance
                }
                if compact_guidance:
                    compact_conflict["coordination_guidance"] = compact_guidance
            compact_item["claim_conflict"] = compact_conflict
        preflight_state = item.get("repository_preflight_state")
        if isinstance(preflight_state, dict):
            compact_item["repository_preflight_state"] = {
                key: preflight_state[key]
                for key in (
                    "schema_version", "observation_basis", "checkout_identity",
                    "branch", "branch_status", "head", "head_status",
                    "working_tree_status",
                )
                if key in preflight_state
            }
            limit = item.get("preflight_state_interpretation_limit")
            if isinstance(limit, str):
                compact_item["preflight_state_interpretation_limit"] = (
                    _short_text(limit, 320)
                )
        reference = item.get("event_reference")
        if isinstance(reference, dict):
            compact_item["event_reference"] = {
                key: reference[key]
                for key in ("identity", "sequence", "type")
                if key in reference
            }
        compact_authority_denials.append(compact_item)
    compact_coordination_deferrals = []
    raw_coordination_deferrals = activity.get("coordination_deferrals")
    raw_coordination_deferrals = (
        raw_coordination_deferrals
        if isinstance(raw_coordination_deferrals, list) else []
    )
    for item in raw_coordination_deferrals[
        :MAX_SESSION_ACTIVITY_COORDINATION_DEFERRALS
    ]:
        if not isinstance(item, dict):
            continue
        compact_item = {
            key: _short_text(item.get(key), 160)
            for key in (
                "identity", "action", "repository", "required_coordination",
                "coordination_status", "next_step", "campaign_id",
                "work_intent_id", "work_intent_reference_status",
            )
            if isinstance(item.get(key), str)
        }
        for key in ("persistent_authority_required", "operation_executed"):
            if item.get(key) is False:
                compact_item[key] = False
        if isinstance(item.get("scope"), dict):
            compact_item["scope"] = item["scope"]
        relation = item.get("later_eligibility_relation")
        if isinstance(relation, dict):
            compact_item["later_eligibility_status"] = _short_text(
                relation.get("status"), 80,
            )
            if type(relation.get("matching_preflight_count")) is int:
                compact_item["later_eligibility_match_count"] = relation[
                    "matching_preflight_count"
                ]
        reference = item.get("event_reference")
        if isinstance(reference, dict):
            compact_item["event_reference"] = {
                key: reference[key]
                for key in ("identity", "sequence", "type")
                if key in reference
            }
        compact_coordination_deferrals.append(compact_item)
    compact_coordination_deferral_candidates = []
    raw_coordination_deferral_candidates = activity.get(
        "coordination_deferral_candidates",
    )
    raw_coordination_deferral_candidates = (
        raw_coordination_deferral_candidates
        if isinstance(raw_coordination_deferral_candidates, list) else []
    )
    for item in raw_coordination_deferral_candidates[:4]:
        if not isinstance(item, dict):
            continue
        compact_item = {
            key: _short_text(item.get(key), 160)
            for key in (
                "identity", "status", "subsystem", "repository", "action",
                "required_coordination", "coordination_status", "next_step",
                "recurrence_basis", "recurrence_scope", "causal_claim",
            )
            if isinstance(item.get(key), str)
        }
        for key in ("occurrence_count", "distinct_session_count"):
            if type(item.get(key)) is int:
                compact_item[key] = item[key]
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
        follow_through = item.get("later_eligibility_summary")
        if isinstance(follow_through, dict):
            compact_follow_through = {
                key: follow_through[key]
                for key in (
                    "status", "operation_execution_status", "causal_claim",
                )
                if key in follow_through
            }
            for key in (
                "deferrals_with_later_preflight_count",
                "deferrals_without_exact_objective_reference_count",
                "matching_preflight_count",
                "supporting_preflight_events_omitted_count",
            ):
                if type(follow_through.get(key)) is int:
                    compact_follow_through[key] = follow_through[key]
            preflight_events = follow_through.get("supporting_preflight_events")
            if isinstance(preflight_events, list):
                compact_follow_through["supporting_preflight_events"] = [
                    {
                        "kind": event.get("kind"),
                        "event_reference": {
                            key: event["event_reference"][key]
                            for key in ("identity", "sequence", "type")
                            if key in event["event_reference"]
                        },
                    }
                    for event in preflight_events[:2]
                    if isinstance(event, dict)
                    and isinstance(event.get("event_reference"), dict)
                ]
            compact_item["later_eligibility_summary"] = compact_follow_through
        compact_coordination_deferral_candidates.append(compact_item)
    compact_integration_eligibility = []
    raw_integration_eligibility = activity.get("integration_eligibility")
    raw_integration_eligibility = (
        raw_integration_eligibility
        if isinstance(raw_integration_eligibility, list) else []
    )
    for item in raw_integration_eligibility[:8]:
        if not isinstance(item, dict):
            continue
        compact_item = {
            key: _short_text(item.get(key), 160)
            for key in (
                "identity", "action", "repository", "authorization_basis",
                "eligibility_basis", "campaign_id", "work_intent_id",
                "work_intent_reference_status", "preflight_state_status",
                "writable_scope_observation_status",
            )
            if isinstance(item.get(key), str)
        }
        preflight_state = item.get("repository_preflight_state")
        if isinstance(preflight_state, dict):
            compact_item["repository_preflight_state"] = {
                key: preflight_state[key]
                for key in (
                    "schema_version", "observation_basis", "checkout_identity",
                    "branch", "branch_status", "head", "head_status",
                    "working_tree_status",
                )
                if key in preflight_state
            }
            limit = item.get("preflight_state_interpretation_limit")
            if isinstance(limit, str):
                compact_item["preflight_state_interpretation_limit"] = (
                    _short_text(limit, 320)
                )
        if item.get("persistent_claim_required") is False:
            compact_item["persistent_claim_required"] = False
        if item.get("operation_executed") is False:
            compact_item["operation_executed"] = False
        if type(item.get("coordination_overlap_count")) is int:
            compact_item["coordination_overlap_count"] = item[
                "coordination_overlap_count"
            ]
        if item.get("coordination_overlaps_truncated") is True:
            compact_item["coordination_overlaps_truncated"] = True
        compact_overlaps = []
        compact_claim_state_count = 0
        omitted_compact_claim_state_count = 0
        raw_overlaps = item.get("coordination_overlaps")
        raw_overlaps = raw_overlaps if isinstance(raw_overlaps, list) else []
        for overlap in raw_overlaps[:4]:
            if not isinstance(overlap, dict):
                continue
            compact_overlap = {
                key: _short_text(overlap.get(key), 160)
                for key in (
                    "session_id", "claim_id", "overlap_reason",
                    "claim_lifecycle_correlation_identity",
                )
                if isinstance(overlap.get(key), str)
            }
            scope = _authority_conflict_scope(
                overlap.get("scope"), item.get("repository"),
            )
            if scope is not None:
                compact_overlap["scope"] = scope
            current_claim_state = overlap.get("current_claim_state")
            if isinstance(current_claim_state, dict):
                if compact_claim_state_count == 0:
                    compact_overlap["current_claim_state"] = {
                        key: current_claim_state[key]
                        for key in (
                            "lookup_status", "blocking_status",
                        )
                        if key in current_claim_state
                    }
                    compact_claim_state_count += 1
                else:
                    omitted_compact_claim_state_count += 1
            compact_overlaps.append(compact_overlap)
        if compact_overlaps:
            compact_item["coordination_overlaps"] = compact_overlaps
            compact_item["coordination_overlap_records_returned"] = len(
                compact_overlaps,
            )
            if len(compact_overlaps) < len(raw_overlaps):
                compact_item["coordination_overlaps_projection_truncated"] = True
        if omitted_compact_claim_state_count:
            compact_item["current_claim_states_projection_truncated"] = True
            compact_item["current_claim_states_projection_omitted_count"] = (
                omitted_compact_claim_state_count
            )
        scope = _authority_conflict_scope(
            item.get("scope"), item.get("repository"),
        )
        if scope is not None:
            compact_item["scope"] = scope
        reference = item.get("event_reference")
        if isinstance(reference, dict):
            compact_item["event_reference"] = {
                key: reference[key]
                for key in ("identity", "sequence", "type")
                if key in reference
            }
        compact_integration_eligibility.append(compact_item)
    raw_proposal_eligibility = activity.get("proposal_eligibility")
    raw_proposal_eligibility = (
        raw_proposal_eligibility
        if isinstance(raw_proposal_eligibility, list) else []
    )
    compact_proposal_eligibility = []
    for item in raw_proposal_eligibility[:8]:
        if not isinstance(item, dict):
            continue
        compact_item = {
            key: _short_text(item.get(key), 160)
            for key in (
                "identity", "action", "repository", "authorization_basis",
                "eligibility_basis", "campaign_id", "work_intent_id",
                "work_intent_reference_status", "preflight_state_status",
            )
            if isinstance(item.get(key), str)
        }
        preflight_state = item.get("proposal_preflight_state")
        if isinstance(preflight_state, dict):
            compact_item["proposal_preflight_state"] = {
                key: preflight_state[key]
                for key in (
                    "schema_version", "observation_basis", "checkout_identity",
                    "branch", "branch_status", "head", "head_status",
                    "working_tree_status",
                )
                if key in preflight_state
            }
            limit = item.get("preflight_state_interpretation_limit")
            if isinstance(limit, str):
                compact_item["preflight_state_interpretation_limit"] = (
                    _short_text(limit, 320)
                )
        for key in (
            "persistent_claim_required", "operation_executed",
            "writable_scope_required_for_integration",
            "repository_writable_scope_present",
            "coordination_only_claim_count", "coordination_only_claims_truncated",
        ):
            if key in item and type(item[key]) in {bool, int}:
                compact_item[key] = item[key]
        scope = _authority_conflict_scope(
            item.get("scope"), item.get("repository"),
        )
        if scope is not None:
            compact_item["scope"] = scope
        compact_claims = []
        raw_claims = item.get("coordination_only_claims")
        raw_claims = raw_claims if isinstance(raw_claims, list) else []
        for claim in raw_claims[:3]:
            if not isinstance(claim, dict):
                continue
            compact_claim = {
                key: _short_text(claim.get(key), 160)
                for key in (
                    "session_id", "claim_id", "disposition",
                    "overlap_reason",
                    "claim_lifecycle_correlation_identity",
                )
                if isinstance(claim.get(key), str)
            }
            claim_scope = _authority_conflict_scope(
                claim.get("scope"), item.get("repository"),
            )
            if claim_scope is not None:
                compact_claim["scope"] = claim_scope
            compact_claims.append(compact_claim)
        if compact_claims:
            compact_item["coordination_only_claims"] = compact_claims
            if len(raw_claims) > len(compact_claims):
                compact_item["coordination_only_claims_projection_truncated"] = True
        reference = item.get("event_reference")
        if isinstance(reference, dict):
            compact_item["event_reference"] = {
                key: reference[key]
                for key in ("identity", "sequence", "type")
                if key in reference
            }
        invocation_observation = item.get("invocation_observation")
        if isinstance(invocation_observation, dict):
            observation_status = invocation_observation.get("status")
            if observation_status == "matched-provider-invocation":
                compact_observation = {
                    key: _short_text(invocation_observation.get(key), 256)
                    for key in (
                        "status", "identity", "capability", "outcome_status",
                        "causal_status", "project_change_status",
                    )
                    if isinstance(invocation_observation.get(key), str)
                }
                for key in ("correlation_status", "returncode"):
                    value = invocation_observation.get(key)
                    if isinstance(value, str) or type(value) is int:
                        compact_observation[key] = value
                for key in ("invocation_event", "completion_event"):
                    invocation_reference = invocation_observation.get(key)
                    if isinstance(invocation_reference, dict):
                        compact_observation[key] = {
                            field: invocation_reference[field]
                            for field in ("identity", "sequence", "type")
                            if field in invocation_reference
                        }
                compact_item["invocation_observation"] = compact_observation
            else:
                compact_status = {
                    "no-matching-environment-invocation-in-selected-window": (
                        "no-match-complete-window"
                    ),
                    "invocation-not-established-in-partial-window": (
                        "unresolved-partial-window"
                    ),
                    "preflight-reference-conflict": "preflight-reference-conflict",
                    "invalid-proposal-reference": "invalid-proposal-reference",
                }.get(observation_status)
                if compact_status is not None:
                    compact_item["invocation_observation_status"] = compact_status
        compact_proposal_eligibility.append(compact_item)
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
                "claim_conflict_evidence_status",
            )
            if isinstance(item.get(key), str)
        }
        for key in ("occurrence_count", "distinct_session_count"):
            if type(item.get(key)) is int:
                compact_item[key] = item[key]
        basis_summary = item.get("decision_basis_summary")
        if isinstance(basis_summary, dict):
            compact_item["decision_basis_summary"] = {
                key: basis_summary[key]
                for key in (
                    "status", "validated_count",
                    "writable_scope_eligible_block_count",
                )
                if key in basis_summary
            }
        decision_summary = item.get("decision_claim_state_summary")
        if isinstance(decision_summary, dict):
            compact_decision_summary = {
                key: decision_summary[key]
                for key in (
                    "status", "structured_denial_count", "validated_count",
                    "invalid_count", "not_recorded_count", "legacy_denial_count",
                )
                if key in decision_summary
            }
            raw_state_counts = decision_summary.get("state_counts")
            if isinstance(raw_state_counts, list):
                compact_decision_summary["state_counts"] = [
                    {
                        key: state_count[key]
                        for key in (
                            "blocking_status", "recovery_verdict",
                            "occurrence_count",
                        )
                        if key in state_count
                    }
                    for state_count in raw_state_counts[:4]
                    if isinstance(state_count, dict)
                ]
                if len(raw_state_counts) > 4:
                    compact_decision_summary[
                        "state_counts_truncated"
                    ] = True
                    compact_decision_summary[
                        "state_counts_omitted_count"
                    ] = len(raw_state_counts) - 4
            compact_item["decision_claim_state_summary"] = (
                compact_decision_summary
            )
        conflict_reasons = item.get("conflict_reasons")
        if isinstance(conflict_reasons, list):
            compact_item["conflict_reasons"] = [
                _short_text(reason, 160)
                for reason in conflict_reasons[:4]
                if isinstance(reason, str)
            ]
        blockers = item.get("blocking_claims")
        if isinstance(blockers, list):
            compact_blockers = []
            for blocker in blockers[:4]:
                if not isinstance(blocker, dict):
                    continue
                compact_blocker = {
                    key: _short_text(blocker[key], 160)
                    for key in ("session_id", "claim_id")
                    if isinstance(blocker.get(key), str)
                }
                if type(blocker.get("occurrence_count")) is int:
                    compact_blocker["occurrence_count"] = blocker[
                        "occurrence_count"
                    ]
                scope = _authority_conflict_scope(
                    blocker.get("scope"), item.get("repository"),
                )
                if scope is not None:
                    compact_blocker["scope"] = scope
                current_state = blocker.get("current_claim_state")
                if isinstance(current_state, dict):
                    compact_blocker["current_claim_state"] = {
                        key: current_state[key]
                        for key in (
                            "basis", "claim_version", "blocking_status",
                            "recovery_verdict", "freshness",
                            "owner_liveness_evidence_status",
                            "recorded_owner_relation",
                        )
                        if key in current_state
                    }
                compact_blockers.append(compact_blocker)
            compact_item["blocking_claims"] = compact_blockers
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
    raw_operation_deferrals = activity.get("operation_deferrals")
    raw_operation_deferrals = (
        raw_operation_deferrals if isinstance(raw_operation_deferrals, list) else []
    )
    compact_operation_deferrals = []
    for item in raw_operation_deferrals[:MAX_SESSION_ACTIVITY_OPERATION_DEFERRALS]:
        if not isinstance(item, dict):
            continue
        compact_item = {
            key: _short_text(item[key], 160)
            for key in ("identity", "action", "repository", "reason", "branch",
                        "checkout_identity")
            if isinstance(item.get(key), str)
        }
        if type(item.get("waited_ms")) is int and item["waited_ms"] >= 0:
            compact_item["waited_ms"] = item["waited_ms"]
        if item.get("operation_executed") is False:
            compact_item["operation_executed"] = False
        reference = item.get("event_reference")
        if isinstance(reference, dict):
            compact_item["event_reference"] = {
                key: reference[key]
                for key in ("identity", "sequence", "type")
                if key in reference
            }
        changed_fields = item.get("changed_fields")
        if isinstance(changed_fields, list):
            compact_item["changed_fields"] = [
                field for field in changed_fields[:6]
                if isinstance(field, str)
            ]
        for key in ("initial_preflight_event", "locked_preflight_event"):
            preflight_reference = item.get(key)
            if isinstance(preflight_reference, dict):
                compact_item[key] = {
                    field: preflight_reference[field]
                    for field in ("identity", "sequence", "type")
                    if field in preflight_reference
                }
        compact_operation_deferrals.append(compact_item)
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
    raw_service_readiness = activity.get("service_readiness")
    if isinstance(raw_service_readiness, dict):
        # _session_service_readiness already returns a bounded, sanitized
        # projection, so retain that reusable record without rebuilding it.
        compact_activity["service_readiness"] = raw_service_readiness
    raw_doctor_history = activity.get("doctor_health_history")
    if isinstance(raw_doctor_history, dict):
        # Doctor's snapshot history is already bounded and sanitized here.
        compact_activity["doctor_health_history"] = raw_doctor_history
    if activity_source.get("operation_deferral_events_considered", 0):
        compact_activity["operation_deferral_count"] = activity.get(
            "operation_deferral_count", 0,
        )
        compact_activity["operation_deferrals"] = compact_operation_deferrals
    if compact_activity_capabilities:
        compact_activity["capabilities"] = compact_activity_capabilities
        if activity.get("source", {}).get("capability_summaries_omitted", 0):
            compact_activity["capabilities_omitted"] = activity["source"][
                "capability_summaries_omitted"
            ]
    if compact_capability_delivery:
        compact_activity["capability_delivery"] = compact_capability_delivery
    if raw_claim_recoveries:
        compact_activity["claim_recovery_count"] = activity.get(
            "claim_recovery_count", len(raw_claim_recoveries),
        )
        compact_activity["claim_recoveries"] = compact_claim_recoveries
        if len(raw_claim_recoveries) > len(compact_claim_recoveries):
            compact_activity["claim_recoveries_truncated"] = True
    if (raw_operation_claim_recoveries
            or activity_source.get("operation_claim_recovery_events_considered", 0)):
        compact_activity["operation_claim_recovery_count"] = activity.get(
            "operation_claim_recovery_count", len(raw_operation_claim_recoveries),
        )
        compact_activity["operation_claim_recoveries"] = (
            compact_operation_claim_recoveries
        )
        if len(raw_operation_claim_recoveries) > len(
            compact_operation_claim_recoveries,
        ):
            compact_activity["operation_claim_recoveries_truncated"] = True
    if raw_claim_coordination or activity_source.get(
        "claim_coordination_events_considered", 0,
    ):
        compact_activity["claim_coordination_count"] = activity.get(
            "claim_coordination_count", len(raw_claim_coordination),
        )
        compact_activity["claim_coordination"] = compact_claim_coordination
        if len(raw_claim_coordination) > len(compact_claim_coordination):
            compact_activity["claim_coordination_truncated"] = True
    if (raw_claim_lifecycle_correlations
            or isinstance(activity_source.get("claim_lifecycle_correlation"), dict)):
        lifecycle_source = activity_source.get("claim_lifecycle_correlation")
        lifecycle_source = lifecycle_source if isinstance(lifecycle_source, dict) else {}
        lifecycle_count = lifecycle_source.get("correlation_records_considered")
        if type(lifecycle_count) is not int:
            lifecycle_count = len(raw_claim_lifecycle_correlations)
        compact_activity["claim_lifecycle_correlation_count"] = lifecycle_count
        compact_activity["claim_lifecycle_correlations_returned"] = len(
            compact_claim_lifecycle_correlations,
        )
        compact_activity["claim_lifecycle_correlations_omitted_count"] = max(
            0, lifecycle_count - len(compact_claim_lifecycle_correlations),
        )
        compact_activity["claim_lifecycle_correlations"] = (
            compact_claim_lifecycle_correlations
        )
        if lifecycle_count > len(compact_claim_lifecycle_correlations):
            compact_activity["claim_lifecycle_correlations_truncated"] = True
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
    if (raw_coordination_deferrals
            or activity_source.get("coordination_deferral_events_considered", 0)):
        compact_activity["coordination_deferral_count"] = activity.get(
            "coordination_deferral_count", len(raw_coordination_deferrals),
        )
        compact_activity["coordination_deferrals"] = (
            compact_coordination_deferrals
        )
        compact_activity["coordination_deferral_candidates"] = (
            compact_coordination_deferral_candidates
        )
        compact_activity["coordination_deferral_candidate_count"] = activity.get(
            "coordination_deferral_candidate_count",
            len(raw_coordination_deferral_candidates),
        )
        if len(raw_coordination_deferrals) > len(compact_coordination_deferrals):
            compact_activity["coordination_deferrals_truncated"] = True
        if (len(raw_coordination_deferral_candidates)
                > len(compact_coordination_deferral_candidates)):
            compact_activity["coordination_deferral_candidates_truncated"] = True
    if raw_integration_eligibility:
        compact_activity["integration_eligibility_count"] = activity.get(
            "integration_eligibility_count", len(raw_integration_eligibility),
        )
        compact_activity["integration_eligibility"] = (
            compact_integration_eligibility
        )
        if len(raw_integration_eligibility) > len(compact_integration_eligibility):
            compact_activity["integration_eligibility_truncated"] = True
    if raw_proposal_eligibility or activity_source.get(
        "proposal_eligibility_events_considered", 0,
    ):
        compact_activity["proposal_eligibility_count"] = activity.get(
            "proposal_eligibility_count", len(raw_proposal_eligibility),
        )
        compact_activity["proposal_eligibility"] = compact_proposal_eligibility
        if len(raw_proposal_eligibility) > len(compact_proposal_eligibility):
            compact_activity["proposal_eligibility_truncated"] = True
    raw_repository_ref_transitions = activity.get("repository_ref_transitions")
    raw_repository_ref_transitions = (
        raw_repository_ref_transitions
        if isinstance(raw_repository_ref_transitions, list) else []
    )
    compact_repository_ref_transitions = []
    for item in raw_repository_ref_transitions[
        :MAX_SESSION_ACTIVITY_REPOSITORY_REF_TRANSITIONS
    ]:
        if not isinstance(item, dict):
            continue
        repository = item.get("repository")
        observed_path = item.get("observed_path")
        if not _is_repository_identity(repository):
            continue
        if (not isinstance(observed_path, str) or not observed_path
                or len(observed_path) > 240 or "\\" in observed_path
                or observed_path.startswith("/")
                or PurePosixPath(observed_path).as_posix() != observed_path
                or any(part in {"", ".", ".."}
                       for part in observed_path.split("/"))):
            continue
        compact_item = {
            key: item[key]
            for key in (
                "identity", "repository", "observed_path", "change_kinds",
                "state_change_status", "causal_status", "campaign_id",
                "work_intent_id", "work_intent_reference_status",
            )
            if key in item
        }
        for key in ("before", "after"):
            observation = _authority_repository_ref_observation(
                item.get(key), repository=repository,
                observed_path=observed_path,
            )
            if observation is not None:
                compact_item[key] = observation
        relation = item.get("preflight_relation")
        if isinstance(relation, dict):
            compact_relation = {
                key: relation[key]
                for key in ("status", "matching_preflight_count", "matches_truncated")
                if key in relation
            }
            matches = relation.get("matches")
            if isinstance(matches, list):
                compact_matches = []
                for match in matches[:2]:
                    if not isinstance(match, dict):
                        continue
                    compact_match = {
                        key: match[key]
                        for key in ("kind", "action", "match_basis")
                        if key in match
                    }
                    reference = match.get("event_reference")
                    if isinstance(reference, dict):
                        compact_match["event_reference"] = {
                            key: reference[key]
                            for key in ("identity", "sequence", "type")
                            if key in reference
                        }
                    compact_matches.append(compact_match)
                compact_relation["matches"] = compact_matches
            compact_item["preflight_relation"] = compact_relation
        reference = item.get("event_reference")
        if isinstance(reference, dict):
            compact_item["event_reference"] = {
                key: reference[key]
                for key in ("identity", "sequence", "type")
                if key in reference
            }
        compact_repository_ref_transitions.append(compact_item)
    if (raw_repository_ref_transitions or activity_source.get(
        "repository_ref_transition_events_considered", 0,
    )):
        compact_activity["repository_ref_transition_count"] = activity.get(
            "repository_ref_transition_count", len(raw_repository_ref_transitions),
        )
        compact_activity["repository_ref_transitions"] = (
            compact_repository_ref_transitions
        )
        if len(raw_repository_ref_transitions) > len(compact_repository_ref_transitions):
            compact_activity["repository_ref_transitions_truncated"] = True
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
                    if key in host and (
                        type(host.get(key)) is int or host.get(key) is None
                    ):
                        compact_host[key] = host[key]
                for key in ("cpu_utilization",):
                    if key in host and (
                        type(host.get(key)) in (int, float)
                        or host.get(key) is None
                    ):
                        compact_host[key] = host[key]
                load_average = host.get("load_average")
                if "load_average" in host and (
                    isinstance(load_average, (list, dict)) or load_average is None
                ):
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
    compact_accountability = _compact_implementation_accountability(
        projection.get("implementation_accountability"),
    )
    if compact_accountability is not None:
        result["implementation_accountability"] = compact_accountability
    if compact_context_candidates:
        result["context_pressure_candidate_count"] = projection.get(
            "context_pressure_candidate_count", len(compact_context_candidates),
        )
        result["context_pressure_candidates"] = compact_context_candidates
    raw_activity = projection.get("session_activity")
    raw_activity = raw_activity if isinstance(raw_activity, dict) else {}
    raw_correlations = raw_activity.get("claim_lifecycle_correlations")
    raw_correlations = raw_correlations if isinstance(raw_correlations, list) else []
    has_related_claim_activity_correlation = any(
        isinstance(correlation, dict)
        and any(
            isinstance(reference, dict)
            and reference.get("kind") in {
                "integration-eligibility", "proposal-eligibility",
            }
            for reference in (
                correlation.get("related_session_activity")
                if isinstance(correlation.get("related_session_activity"), list)
                else []
            )
        )
        for correlation in raw_correlations
    )

    def omit_compact_claim_lifecycle_correlation(
        removed: dict[str, Any],
    ) -> None:
        activity_result = result["session_activity"]
        returned = len(activity_result.get("claim_lifecycle_correlations", []))
        total = activity_result.get("claim_lifecycle_correlation_count", returned)
        activity_result["claim_lifecycle_correlations_returned"] = returned
        activity_result["claim_lifecycle_correlations_omitted_count"] = max(
            0, total - returned,
        )
        correlation_identity = removed.get("identity")
        if not isinstance(correlation_identity, str):
            return
        for denial in activity_result.get("authority_denials", []):
            if not isinstance(denial, dict):
                continue
            conflict = denial.get("claim_conflict")
            blocker = (
                conflict.get("blocking_claim")
                if isinstance(conflict, dict) else None
            )
            if (isinstance(blocker, dict)
                    and blocker.get("claim_lifecycle_correlation_identity")
                    == correlation_identity):
                blocker.pop("claim_lifecycle_correlation_identity", None)
        for eligibility in activity_result.get("integration_eligibility", []):
            if not isinstance(eligibility, dict):
                continue
            for overlap in eligibility.get("coordination_overlaps", []):
                if (isinstance(overlap, dict)
                        and overlap.get("claim_lifecycle_correlation_identity")
                        == correlation_identity):
                    overlap.pop("claim_lifecycle_correlation_identity", None)
        for eligibility in activity_result.get("proposal_eligibility", []):
            if not isinstance(eligibility, dict):
                continue
            for claim in eligibility.get("coordination_only_claims", []):
                if (isinstance(claim, dict)
                        and claim.get("claim_lifecycle_correlation_identity")
                        == correlation_identity):
                    claim.pop("claim_lifecycle_correlation_identity", None)
    # The selected fields are individually bounded. This final reduction also
    # accounts for unusual but valid identifiers and reference paths.
    while len(json.dumps(result, ensure_ascii=False, sort_keys=True).encode("utf-8")) > max_result_bytes:
        activity_result = result["session_activity"]
        delivery_result = activity_result.get("capability_delivery")
        if isinstance(delivery_result, dict):
            if delivery_result.get("measurement_limits"):
                delivery_result["measurement_limits"].pop()
                continue
            elif delivery_result.get("bindings"):
                delivery_result["bindings"].pop()
                delivery_result["bindings_truncated"] = True
                binding_summary = delivery_result.get(
                    "binding_evidence_summary",
                )
                if isinstance(binding_summary, dict):
                    binding_summary["binding_details_omitted_count"] = (
                        binding_summary.get("binding_details_omitted_count", 0) + 1
                        if type(binding_summary.get(
                            "binding_details_omitted_count",
                        )) is int else 1
                    )
                    binding_summary["binding_details_truncated"] = True
                continue
            elif delivery_result.get("availability_transition_events"):
                delivery_result["availability_transition_events"].pop()
                delivery_result["availability_transition_events_truncated"] = True
                continue
            elif delivery_result.get("capabilities_not_in_current_binding_snapshot"):
                delivery_result["capabilities_not_in_current_binding_snapshot"].pop()
                delivery_result[
                    "capabilities_not_in_current_binding_snapshot_truncated"
                ] = True
                continue
            context_activity = delivery_result.get("context_operation_activity")
            if isinstance(context_activity, dict):
                if context_activity.get("measurement_limits"):
                    context_activity["measurement_limits"].pop()
                    continue
                if context_activity.get("supporting_events"):
                    context_activity["supporting_events"].pop()
                    context_activity["supporting_events_truncated"] = True
                    continue
        if activity_result.get("capabilities"):
            activity_result["capabilities"].pop()
            activity_result["capabilities_truncated"] = True
            continue
        accountability = result.get("implementation_accountability")
        owner_audit = (
            accountability.get("owner_audit_evidence")
            if isinstance(accountability, dict) else None
        )
        owner_audits = (
            owner_audit.get("audits")
            if isinstance(owner_audit, dict) else None
        )
        if isinstance(owner_audits, list):
            source_audit_to_trim = next(
                (
                    audit for audit in reversed(owner_audits)
                    if isinstance(audit, dict)
                    and audit.get("source_audit_details")
                ),
                None,
            )
            if source_audit_to_trim is not None:
                source_audit_to_trim["source_audit_details"].pop()
                source_audit_to_trim[
                    "source_audit_details_truncated"
                ] = True
                source_audit_to_trim[
                    "source_audit_details_omitted_count"
                ] = source_audit_to_trim.get(
                    "source_audit_details_omitted_count", 0,
                ) + 1
                continue
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
        elif any(
            isinstance(item, dict)
            and isinstance(item.get("coordination_overlaps"), list)
            and any(
                isinstance(overlap, dict)
                and ("overlap_reason" in overlap or "scope" in overlap)
                for overlap in item["coordination_overlaps"]
            )
            for item in activity_result.get("integration_eligibility", [])
        ):
            eligibility = next(
                item for item in reversed(
                    activity_result["integration_eligibility"],
                )
                if isinstance(item, dict)
                and isinstance(item.get("coordination_overlaps"), list)
                and any(
                    isinstance(overlap, dict)
                    and ("overlap_reason" in overlap or "scope" in overlap)
                    for overlap in item["coordination_overlaps"]
                )
            )
            overlap = next(
                overlap for overlap in reversed(
                    eligibility["coordination_overlaps"],
                )
                if isinstance(overlap, dict)
                and ("overlap_reason" in overlap or "scope" in overlap)
            )
            field = (
                "overlap_reason" if "overlap_reason" in overlap else "scope"
            )
            overlap.pop(field)
            eligibility[
                "coordination_overlap_details_projection_truncated"
            ] = True
            eligibility[
                "coordination_overlap_detail_fields_omitted_count"
            ] = eligibility.get(
                "coordination_overlap_detail_fields_omitted_count", 0,
            ) + 1
        elif (has_related_claim_activity_correlation
              and result["session_activity"].get(
                  "claim_lifecycle_correlations",
              )):
            removed = result["session_activity"][
                "claim_lifecycle_correlations"
            ].pop()
            result["session_activity"][
                "claim_lifecycle_correlations_truncated"
            ] = True
            omit_compact_claim_lifecycle_correlation(removed)
        elif result["session_activity"].get("integration_eligibility"):
            result["session_activity"]["integration_eligibility"].pop()
            result["session_activity"]["integration_eligibility_truncated"] = True
        elif result["session_activity"].get("repository_ref_transitions"):
            result["session_activity"]["repository_ref_transitions"].pop()
            result["session_activity"][
                "repository_ref_transitions_truncated"
            ] = True
        elif result.get("context_pressure_candidates"):
            result["context_pressure_candidates"].pop(0)
        elif isinstance(
            result["session_activity"].get("capability_delivery"), dict,
        ):
            result["session_activity"].pop("capability_delivery")
        elif result["session_activity"].get("capabilities"):
            result["session_activity"]["capabilities"].pop()
            result["session_activity"]["capabilities_truncated"] = True
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
        elif result["session_activity"].get("claim_recoveries"):
            result["session_activity"]["claim_recoveries"].pop()
            result["session_activity"]["claim_recoveries_truncated"] = True
        elif result["session_activity"].get("operation_claim_recoveries"):
            result["session_activity"]["operation_claim_recoveries"].pop()
            result["session_activity"][
                "operation_claim_recoveries_truncated"
            ] = True
        elif result["session_activity"].get("claim_coordination"):
            result["session_activity"]["claim_coordination"].pop()
            result["session_activity"]["claim_coordination_truncated"] = True
        elif (not has_related_claim_activity_correlation
              and result["session_activity"].get(
                  "claim_lifecycle_correlations",
              )):
            removed = result["session_activity"][
                "claim_lifecycle_correlations"
            ].pop()
            result["session_activity"][
                "claim_lifecycle_correlations_truncated"
            ] = True
            omit_compact_claim_lifecycle_correlation(removed)
        elif result["session_activity"].get("authority_denial_candidates"):
            result["session_activity"]["authority_denial_candidates"].pop()
            result["session_activity"]["authority_denial_candidates_truncated"] = True
        elif result["session_activity"].get("coordination_deferrals"):
            result["session_activity"]["coordination_deferrals"].pop()
            result["session_activity"]["coordination_deferrals_truncated"] = True
        elif result["session_activity"].get(
            "coordination_deferral_candidates",
        ):
            result["session_activity"][
                "coordination_deferral_candidates"
            ].pop()
            result["session_activity"][
                "coordination_deferral_candidates_truncated"
            ] = True
        elif result["session_activity"].get("authority_denials"):
            result["session_activity"]["authority_denials"].pop()
            result["session_activity"]["authority_denials_truncated"] = True
        elif result["session_activity"].get("resource_samples", {}).get("observations"):
            result["session_activity"]["resource_samples"]["observations"].pop()
            result["session_activity"]["resource_samples"]["observations_truncated"] = True
        elif result["session_activity"].get("service_readiness", {}).get("candidates"):
            readiness = result["session_activity"]["service_readiness"]
            readiness["candidates"].pop()
            readiness["candidates_truncated"] = True
            summary = readiness.get("summary")
            if isinstance(summary, dict):
                summary["investigation_candidates_omitted"] = (
                    summary.get("investigation_candidates_omitted", 0) + 1
                )
        elif result["session_activity"].get("service_readiness", {}).get("services"):
            readiness = result["session_activity"]["service_readiness"]
            readiness["services"].pop()
            readiness["services_truncated"] = True
        elif (isinstance(result.get("implementation_accountability"), dict)
              and result["implementation_accountability"].get("samples")):
            accountability = result["implementation_accountability"]
            sample = accountability["samples"][-1]
            if sample.get("native_modules"):
                sample["native_modules"].pop()
                accountability["samples_truncated"] = True
            elif len(accountability["samples"]) > 1:
                accountability["samples"].pop()
                accountability["samples_truncated"] = True
            else:
                sample = accountability["samples"][0]
                optional_fields = (
                    "host_test_adapter", "sample_scope", "artifact_identity",
                    "compiler_front_end_outcome", "canonical_vm_case_count",
                    "canonical_vm_case_success_count", "native_semantic_ownership",
                )
                removable = next(
                    (key for key in optional_fields if key in sample), None,
                )
                if removable is None:
                    if isinstance(
                        result["session_activity"].get("capability_delivery"), dict,
                    ):
                        result["session_activity"].pop("capability_delivery")
                        continue
                    break
                sample.pop(removable)
                accountability["samples_truncated"] = True
        elif isinstance(
            result["session_activity"].get("capability_delivery"), dict,
        ):
            result["session_activity"].pop("capability_delivery")
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


def _is_compiler_native_application_proof_reference(
    reference: dict[str, str],
) -> bool:
    return (
        reference["repository"] == "mncs-compiler"
        and reference["identity"] == COMPILER_NATIVE_APPLICATION_PROOF_IDENTITY
        and reference["path"] == COMPILER_NATIVE_APPLICATION_PROOF_PATH
    )


def _is_observation_reference(reference: dict[str, str]) -> bool:
    if (reference["identity"].startswith("ev_environment_")
            and reference["path"].startswith(OBSERVATION_PATH_PREFIX)):
        return True
    return (
        _is_compiler_regression_reference(reference)
        or _is_compiler_dependency_summary_reference(reference)
        or _is_compiler_native_application_proof_reference(reference)
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


def _git_blob_sha256(
    checkout: Path, commit: str, path: str, *, deadline: float,
    max_bytes: int = MAX_SOURCE_HASH_BYTES,
    oversized_status: str = "source-object-too-large",
) -> tuple[str | None, str]:
    """Hash one bounded Git blob incrementally without retaining its contents."""
    object_name = f"{commit}:{path}"
    environment = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    process: subprocess.Popen[bytes] | None = None
    selector: selectors.BaseSelector | None = None
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
        object_size = int(raw_size)
        if object_size > max_bytes:
            return None, oversized_status

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, "read-budget-exhausted"
        process = subprocess.Popen(
            [workspace_module.git_binary(), "-C", str(checkout), "cat-file", "blob", object_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            env=environment,
        )
        if process.stdout is None:
            return None, "git-read-unavailable"
        source_fd = process.stdout.fileno()
        os.set_blocking(source_fd, False)
        selector = selectors.DefaultSelector()
        selector.register(source_fd, selectors.EVENT_READ)
        digest = hashlib.sha256()
        bytes_read = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None, "read-budget-exhausted"
            events = selector.select(min(remaining, 0.25))
            if not events:
                continue
            try:
                chunk = os.read(source_fd, 64 * 1024)
            except BlockingIOError:
                continue
            if not chunk:
                break
            bytes_read += len(chunk)
            if bytes_read > max_bytes:
                return None, oversized_status
            digest.update(chunk)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, "read-budget-exhausted"
        try:
            exit_code = process.wait(timeout=min(1.0, remaining))
        except subprocess.TimeoutExpired:
            return None, "read-budget-exhausted"
        if exit_code != 0:
            return None, "commit-path-unavailable"
        if bytes_read != object_size:
            return None, "object-size-mismatch"
        return digest.hexdigest(), "read"
    except subprocess.TimeoutExpired:
        return None, "read-budget-exhausted"
    except (OSError, subprocess.SubprocessError, ValueError):
        return None, "git-read-unavailable"
    finally:
        if selector is not None:
            selector.close()
        if process is not None:
            if process.poll() is None:
                process.kill()
                try:
                    process.wait(timeout=0.25)
                except subprocess.TimeoutExpired:
                    pass
            if process.stdout is not None:
                process.stdout.close()


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


def _compiler_native_application_proof_observation(
    reference: dict[str, str], source: dict[str, Any],
    root: Path, *, repository_read_basis: str,
) -> dict[str, Any] | None:
    """Adapt one source-backed Compiler proof without inferring a codebase ratio."""
    if source.get("schema_version") != COMPILER_NATIVE_APPLICATION_PROOF_SCHEMA:
        return None
    if source.get("identity") not in (None, reference["identity"]):
        return None
    application = source.get("application")
    producer = source.get("producer")
    build = source.get("build")
    execution = source.get("execution")
    if (not isinstance(application, dict)
            or application.get("entry") != COMPILER_NATIVE_APPLICATION_ENTRY
            or not isinstance(producer, dict)
            or not isinstance(build, dict)
            or not isinstance(execution, dict)):
        return None

    revisions: dict[str, str] = {}
    for field in (
        "compiler_revision", "language_revision", "stdlib_revision",
        "test_revision",
    ):
        value = producer.get(field)
        if not isinstance(value, str) or not _COMMIT.fullmatch(value):
            return None
        revisions[field] = value

    try:
        workspace_root = root.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    raw_modules = application.get("module_sources")
    expected_modules = {
        module: (repository, path, revision_field)
        for module, repository, path, revision_field
        in COMPILER_NATIVE_APPLICATION_MODULES
    }
    if not isinstance(raw_modules, list) or len(raw_modules) != len(expected_modules):
        return None
    source_pointers: list[dict[str, str]] = []
    seen_modules: set[str] = set()
    for raw_module in raw_modules:
        if not isinstance(raw_module, dict):
            return None
        module_name = raw_module.get("module")
        expected = expected_modules.get(module_name)
        raw_path = raw_module.get("path")
        source_digest = raw_module.get("sha256")
        if (expected is None or module_name in seen_modules
                or not isinstance(raw_path, str) or not Path(raw_path).is_absolute()
                or not isinstance(source_digest, str)
                or not _SHA256.fullmatch(source_digest)):
            return None
        repository, expected_path, revision_field = expected
        try:
            resolved_source_path = Path(raw_path).resolve(strict=False)
            relative_source_path = resolved_source_path.relative_to(workspace_root)
        except (OSError, RuntimeError, ValueError):
            return None
        if relative_source_path.as_posix() != f"{repository}/{expected_path}":
            return None
        seen_modules.add(module_name)
        source_pointers.append({
            "kind": "native-language-module-source",
            "identity": "src_" + module_name.replace(".", "_"),
            "module": module_name,
            "repository": repository,
            "commit": revisions[revision_field],
            "path": expected_path,
            "sha256": source_digest,
        })
    if seen_modules != set(expected_modules):
        return None

    artifact_identity = build.get("artifact_identity")
    if (not isinstance(artifact_identity, str)
            or not artifact_identity.startswith("sha256:")
            or not _SHA256.fullmatch(artifact_identity.removeprefix("sha256:"))):
        return None
    cache = build.get("cache")
    verification_work = cache.get("verification_work") if isinstance(cache, dict) else None
    if (not isinstance(verification_work, dict)
            or verification_work.get("front_end_outcome") != "verified"
            or verification_work.get("resolved_module_count") != len(expected_modules)):
        return None
    canonical_cases = execution.get("canonical_vm_cases")
    if (not isinstance(canonical_cases, list) or len(canonical_cases) != 3
            or execution.get("exact_result_agreement") is not True):
        return None
    capability_contract = _compiler_capability_contract_view({
        "required_identity": application.get("required_capability"),
        "runtime_dispatch_alias": application.get("runtime_dispatch_alias"),
        "effect_contract": application.get("effect_contract"),
        "proof_relationship": "compiler-artifact-admission-matches",
        "canonical_execution_relationship": (
            "declared-effects-matched-across-proof-cases"
        ),
        "environment_delivery": "not-measured-by-this-compiler-proof",
    })
    if capability_contract is None:
        return None
    expected_effect_operations = [
        effect["kind"] for effect in capability_contract["effect_contract"]
    ]
    for index, case in enumerate(canonical_cases):
        if not isinstance(case, dict):
            return None
        application_exit = case.get("application_exit")
        output_digest = case.get("output_sha256")
        if (case.get("case_index") != index
                or case.get("artifact_identity") != artifact_identity
                or not isinstance(application_exit, dict)
                or application_exit.get("exit_code") != 0
                or case.get("effect_outcomes") != [
                    "completed" for _ in expected_effect_operations
                ]
                or case.get("effect_operations") != expected_effect_operations
                or not isinstance(output_digest, str)
                or not _SHA256.fullmatch(output_digest)
                or case.get("stage0_output_sha256") != output_digest):
            return None
    capability_admission = execution.get("capability_admission")
    if (not isinstance(capability_admission, dict)
            or capability_admission.get("authorized_capability_identity")
            != capability_contract["required_identity"]
            or capability_admission.get("runtime_dispatch_alias")
            != capability_contract["runtime_dispatch_alias"]):
        return None

    raw_limitations = source.get("limitations")
    if (not isinstance(raw_limitations, list)
            or any(not isinstance(item, str) for item in raw_limitations)):
        return None
    test_adapter_limit = next((item for item in raw_limitations
                               if "bounded test adapter" in item
                               and "not a production MNCS service provider" in item), None)
    rust_harness_limit = next((item for item in raw_limitations
                               if "separate Rust test program" in item), None)
    if test_adapter_limit is None or rust_harness_limit is None:
        return None

    proof_source = {
        "kind": "compiler-native-application-proof",
        **reference,
    }
    created_at = source.get("created_at")
    return {
        "schema_version": OBSERVATION_SCHEMA,
        "identity": reference["identity"],
        "captured_at": created_at if isinstance(created_at, str)
        else "unavailable-in-source-record",
        "measurement_basis": (
            "bounded-derived-view-of-compiler-native-application-proof/2"
            f";{repository_read_basis}"
        ),
        "observation": {
            "kind": "compiler-native-application-implementation-ownership",
            "source_interface": "MNCS Environment capability projection",
            "request": {
                "source_schema_version": COMPILER_NATIVE_APPLICATION_PROOF_SCHEMA,
                "application_entry": COMPILER_NATIVE_APPLICATION_ENTRY,
                "compiler_revision": revisions["compiler_revision"],
                "language_revision": revisions["language_revision"],
            },
            "outcome": {
                "implementation_accountability": {
                    "sample_scope": "one compiler-proven MNCS application and its four-module source closure",
                    "application_entry": COMPILER_NATIVE_APPLICATION_ENTRY,
                    "native_semantic_ownership": "verified-for-this-application-proof",
                    "native_module_count": len(source_pointers),
                    "source_integrity_basis": "module commit, repository path, and SHA-256 from proof; rechecked through Environment source-pointer verification",
                    "artifact_identity": artifact_identity,
                    "compiler_front_end_outcome": "verified",
                    "canonical_vm_case_count": len(canonical_cases),
                    "canonical_vm_case_success_count": len(canonical_cases),
                    "stage0_result_agreement": "verified-in-source-proof",
                    "compiler_capability_contract": capability_contract,
                    "host_test_adapter": {
                        "classification": "test-only-provider-adapter",
                        "limitation": test_adapter_limit[:512],
                        "harness_limitation": rust_harness_limit[:512],
                    },
                    "native_migration_opportunity": "not-established",
                    "source_campaign_context": "not-recorded-in-source-proof",
                    "evidence_relationship_basis": "authenticated-campaign-ledger-pointer; source proof production is not attributed to the Environment projection session",
                    "systemwide_native_adoption_ratio": {
                        "status": "not-measured",
                        "reason": "This single application proof is not a complete, ownership-verified inventory of MNCS functionality.",
                    },
                },
                "source_repository_read_basis": repository_read_basis,
            },
            "measurement_availability": {
                "source_campaign_identity": "not-recorded-in-source-proof",
                "agent_model_identity": "not-recorded-in-source-proof",
                "agent_token_usage": "not-recorded-in-source-proof",
                "production_host_dependency_inventory": "not-established-by-this-proof",
            },
        },
        "supporting_sources": [proof_source, *source_pointers],
        "not_measured": [
            "systemwide native-versus-host functionality ratio and its denominator",
            "production host-language dependency ownership or migration readiness",
            "the Compiler campaign or agent session that produced this proof",
            "whether other agents can discover or use the same native capability reliably",
            "whether this implementation pattern recurs across other applications",
        ],
    }


def _observation_contract_diagnostics(record: dict[str, Any]) -> list[str]:
    """Return bounded, field-level reasons for rejecting an observation record."""
    diagnostics: list[str] = []

    def required_string(container: Any, field: str, label: str) -> None:
        if not isinstance(container, dict):
            diagnostics.append(f"missing-{label}")
        elif not isinstance(container.get(field), str) or not container[field]:
            state = "missing" if field not in container or container[field] is None else "invalid"
            diagnostics.append(f"{state}-{label}")

    if record.get("schema_version") != OBSERVATION_SCHEMA:
        diagnostics.append("unsupported-schema-version")
    required_string(record, "captured_at", "captured-at")
    observation = record.get("observation")
    if not isinstance(observation, dict):
        diagnostics.append("missing-observation-object")
    else:
        required_string(observation, "kind", "observation-kind")
        required_string(
            observation, "source_interface", "observation-source-interface",
        )
        if not isinstance(observation.get("request"), dict):
            state = "missing" if "request" not in observation else "invalid"
            diagnostics.append(f"{state}-observation-request")
        if (not isinstance(observation.get("outcome"), dict)
                and not isinstance(observation.get("projection_summary"), dict)):
            diagnostics.append("missing-observation-outcome-or-projection-summary")
    related_work = record.get("related_work")
    if not isinstance(related_work, dict):
        diagnostics.append("missing-related-work-object")
    else:
        required_string(related_work, "campaign_id", "related-work-campaign-id")
        required_string(related_work, "session_id", "related-work-session-id")
        for field in ("work_intent_id", "work_intent_goal"):
            value = related_work.get(field)
            if value is not None and (not isinstance(value, str) or not value):
                diagnostics.append(f"invalid-related-work-{field.replace('_', '-')}")
    source_context = (
        observation.get("source_session_context")
        if isinstance(observation, dict) else None
    )
    if source_context is not None and not isinstance(source_context, dict):
        diagnostics.append("invalid-source-session-context")
    elif isinstance(source_context, dict) and any(
        value is not None and not isinstance(value, str)
        for value in source_context.values()
    ):
        diagnostics.append("invalid-source-session-context-fields")
    recorder = record.get("recorder")
    if recorder is not None:
        if not isinstance(recorder, dict):
            diagnostics.append("invalid-recorder-object")
        elif any(
            not isinstance(recorder.get(field), str)
            or not recorder[field]
            or len(recorder[field]) > 160
            for field in (
                "campaign_id", "session_id", "consumer_id", "environment_id",
                "definition_id", "environment_version",
            ) if field in recorder
        ):
            diagnostics.append("invalid-recorder-fields")
    hypothesis = record.get("pressure_hypothesis")
    if hypothesis is not None and not isinstance(hypothesis, dict):
        diagnostics.append("invalid-pressure-hypothesis-object")
    elif isinstance(hypothesis, dict):
        for field in (
            "identity", "title", "category", "subsystem",
            "actionable_recommendation",
        ):
            required_string(
                hypothesis, field,
                f"pressure-hypothesis-{field.replace('_', '-')}",
            )
        owner = hypothesis.get("owner")
        if owner is not None and (not isinstance(owner, str) or not owner):
            diagnostics.append("invalid-pressure-hypothesis-owner")
    hypothesis_role = record.get("pressure_hypothesis_role")
    if (hypothesis_role is not None
            and (not isinstance(hypothesis_role, str)
                 or hypothesis_role not in PRESSURE_HYPOTHESIS_ROLES
                 or not isinstance(hypothesis, dict))):
        diagnostics.append("invalid-pressure-hypothesis-role")
    return diagnostics[:16]


def _load_observation(
    session: Any, reference: dict[str, str], root: Path, *, deadline: float,
) -> tuple[dict[str, Any] | None, str]:
    checkout = _checkout(session, reference["repository"], root)
    repository_read_basis = "selected-checkout"
    intent = session.snapshot.get("intent")
    declared_repositories = intent.get("repositories") if isinstance(intent, dict) else None
    declared_campaign_repository = (
        _is_observation_reference(reference)
        and isinstance(declared_repositories, list)
        and reference["repository"] in declared_repositories
    )
    if (checkout is None and (
            _is_compiler_native_application_proof_reference(reference)
            or _is_compiler_regression_reference(reference)
            or declared_campaign_repository)):
        checkout = _workspace_read_only_checkout(
            session, reference["repository"], root, deadline,
        )
        if checkout is not None:
            repository_read_basis = "workspace-read-only-checkout"
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
        if repository_read_basis == "workspace-read-only-checkout":
            adapted["_projection_repository_read_basis"] = repository_read_basis
        return adapted, "verified"
    if _is_compiler_native_application_proof_reference(reference):
        source_identity = record.get("identity")
        if source_identity is not None and source_identity != reference["identity"]:
            return record, "observation-identity-mismatch"
        adapted = _compiler_native_application_proof_observation(
            reference, record, root,
            repository_read_basis=repository_read_basis,
        )
        if adapted is None:
            return None, "observation-contract-invalid"
        if repository_read_basis == "workspace-read-only-checkout":
            adapted["_projection_repository_read_basis"] = repository_read_basis
        return adapted, "verified"
    if record.get("identity") != reference["identity"]:
        return record, "observation-identity-mismatch"
    if _is_compiler_regression_reference(reference):
        adapted = _compiler_regression_observation(session, reference, record)
        if adapted is None:
            return None, "observation-contract-invalid"
        adapted_observation = adapted.get("observation")
        if isinstance(adapted_observation, dict):
            adapted_observation["source_repository_read_basis"] = (
                repository_read_basis
            )
        if repository_read_basis == "workspace-read-only-checkout":
            adapted["_projection_repository_read_basis"] = repository_read_basis
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
        record["_projection_contract_diagnostics"] = (
            _observation_contract_diagnostics(record)
        )
        return record, "observation-contract-invalid"
    hypothesis_role = record.get("pressure_hypothesis_role")
    if (hypothesis_role is not None
            and (not isinstance(hypothesis_role, str)
                 or hypothesis_role not in PRESSURE_HYPOTHESIS_ROLES
                 or not isinstance(hypothesis, dict))):
        record["_projection_contract_diagnostics"] = (
            _observation_contract_diagnostics(record)
        )
        return record, "observation-contract-invalid"
    if isinstance(hypothesis, dict) and any(
        not isinstance(hypothesis.get(key), str) or not hypothesis[key]
        for key in ("identity", "title", "category", "subsystem",
                    "actionable_recommendation")
    ):
        record["_projection_contract_diagnostics"] = (
            _observation_contract_diagnostics(record)
        )
        return record, "observation-contract-invalid"
    if (isinstance(hypothesis, dict) and hypothesis.get("owner") is not None
            and (not isinstance(hypothesis["owner"], str) or not hypothesis["owner"])):
        record["_projection_contract_diagnostics"] = (
            _observation_contract_diagnostics(record)
        )
        return record, "observation-contract-invalid"
    if repository_read_basis == "workspace-read-only-checkout":
        record["_projection_repository_read_basis"] = repository_read_basis
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
        if not is_campaign_charter_evidence_path(pointer["path"]):
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
    module = source.get("module")
    if isinstance(module, str) and module and len(module) <= 160 and "\x00" not in module:
        summary["module"] = module
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
    workspace_read_only_observation_references: Any,
    diagnostics_truncated: bool,
    integration_eligibility: Any,
    activity_source: Any,
) -> list[dict[str, Any]]:
    """Derive bounded, explicitly hypothetical candidates from session scope."""
    unselected = workspace_alignment.get("unselected_declared_repositories")
    if not isinstance(unselected, list):
        return []
    diagnostics = (
        observation_reference_diagnostics
        if isinstance(observation_reference_diagnostics, list) else []
    )
    read_only_references = (
        workspace_read_only_observation_references
        if isinstance(workspace_read_only_observation_references, list) else []
    )
    selected = workspace_alignment.get("selected_repositories")
    selected = selected if isinstance(selected, list) else []
    eligibility_records = (
        integration_eligibility
        if isinstance(integration_eligibility, list) else []
    )
    activity_source = activity_source if isinstance(activity_source, dict) else {}
    matching_preflights_by_repository: dict[str, list[dict[str, Any]]] = {}
    matching_read_only_evidence_by_repository: dict[str, list[dict[str, str]]] = {}
    for reference in read_only_references:
        if (
            not isinstance(reference, dict)
            or reference.get("verification") != "verified"
            or reference.get("repository_read_basis")
            != "workspace-read-only-checkout"
            or not _is_repository_identity(reference.get("repository"))
            or any(not isinstance(reference.get(key), str) or not reference[key]
                   for key in ("identity", "commit", "path", "sha256"))
        ):
            continue
        matching_read_only_evidence_by_repository.setdefault(
            reference["repository"], [],
        ).append({
            key: reference[key]
            for key in (
                "identity", "repository", "commit", "path", "sha256",
                "verification", "repository_read_basis",
            )
        })
    for item in eligibility_records:
        if not isinstance(item, dict):
            continue
        repository = item.get("repository")
        event_reference = item.get("event_reference")
        raw_preflight_state = item.get("repository_preflight_state")
        preflight_state = _authority_integration_preflight_state(
            raw_preflight_state,
        )
        scope = _authority_conflict_scope(item.get("scope"), repository)
        if (
            not _is_repository_identity(repository)
            or item.get("action") not in {"merge", "publish"}
            or item.get("persistent_claim_required") is not False
            or item.get("operation_executed") is not False
            or item.get("preflight_state_status") != "recorded-and-validated"
            or preflight_state is None
            or scope is None
            or item.get("campaign_id") != campaign_id
            or item.get("work_intent_id") != work_intent_id
            or item.get("work_intent_reference_status") != "recorded-on-event"
            or not isinstance(event_reference, dict)
                or not isinstance(
                    event_reference.get("identity"), str,
                )
            or type(event_reference.get("sequence")) is not int
            or not isinstance(event_reference.get("type"), str)
        ):
            continue
        observation = {
            "action": item["action"],
            "persistent_claim_required": False,
            "operation_executed": False,
            "scope": scope,
            "repository_preflight_state": preflight_state,
            "preflight_state_interpretation_limit": _short_text(
                item.get("preflight_state_interpretation_limit"), 320,
            ),
            "event_reference": {
                key: event_reference[key]
                for key in ("identity", "sequence", "type")
                if key in event_reference
            },
        }
        matching_preflights_by_repository.setdefault(repository, []).append(
            observation,
        )
    omitted_preflight_records = activity_source.get(
        "integration_eligibility_records_omitted", 0,
    )
    if type(omitted_preflight_records) is not int or omitted_preflight_records < 0:
        omitted_preflight_records = 0
    candidates: list[dict[str, Any]] = []
    for repository in unselected[:MAX_OBSERVATIONS]:
        if not _is_repository_identity(repository):
            continue
        preflight_observations = matching_preflights_by_repository.get(
            repository, [],
        )
        visible_preflights = preflight_observations[:4]
        read_only_evidence = matching_read_only_evidence_by_repository.get(
            repository, [],
        )
        visible_read_only_evidence = read_only_evidence[:4]
        preflight_search = {
            "status": (
                "matching-preflight-observed-in-selected-window"
                if preflight_observations
                else "no-matching-preflight-observed-in-selected-window"
            ),
            "matched_records_returned": len(preflight_observations),
            "activity_records_omitted": omitted_preflight_records,
            "observation_records_truncated": (
                len(preflight_observations) > len(visible_preflights)
            ),
            "session_event_window_truncated": (
                activity_source.get("event_window_truncated") is True
            ),
        }
        page_before_sequence = activity_source.get("event_page_before_sequence")
        if type(page_before_sequence) is int and page_before_sequence >= 0:
            preflight_search["event_page_before_sequence"] = page_before_sequence
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
            "workspace_read_only_evidence_identities": [
                reference["identity"]
                for reference in visible_read_only_evidence
            ],
            "integration_preflight_event_identities": [
                item["event_reference"]["identity"]
                for item in visible_preflights
            ],
        }, length=16)
        candidate = {
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
                "Read-only campaign evidence is available, and a same-intent "
                "merge or publish preflight required no persistent claim but did "
                "not execute. Keep checkout binding operation-scoped; bind or "
                "claim only if a concrete mutation requires it."
                if read_only_evidence and preflight_observations else (
                    "Read-only campaign evidence is available; the selection gap "
                    "alone does not show that work is blocked. Bind or claim only "
                    "when a concrete operation requires this checkout."
                    if read_only_evidence else (
                        "A scoped preflight required no persistent claim but did "
                        "not execute integration. Check whether broader checkout "
                        "selection is needed."
                        if preflight_observations else
                        "Confirm broader repository access is needed. Select its "
                        "exact checkout or use a scoped integration preflight; "
                        "this does not establish ordinary read or write access."
                    )
                )
            ),
            "not_measured": [
                (
                    "whether a preflight led to executed integration or a successful "
                    "outcome"
                ),
                (
                    "whether read-only evidence access suffices for the objective "
                    "or other read or write operations need checkout selection"
                    if read_only_evidence else
                    "whether ordinary repository read or write work was available"
                ),
                "whether the same gap recurs in another session",
                "whether the selection gap blocks or delays the declared objective",
                "why the repository is absent from the selected checkout set",
            ],
        }
        if read_only_evidence:
            candidate["basis"][
                "workspace_read_only_observation_reference_count"
            ] = len(read_only_evidence)
            candidate["workspace_read_only_evidence_references"] = (
                visible_read_only_evidence
            )
            candidate["workspace_read_only_evidence_references_truncated"] = (
                len(read_only_evidence) > len(visible_read_only_evidence)
            )
        if preflight_observations or omitted_preflight_records:
            candidate["integration_preflight_search"] = preflight_search
            candidate["integration_preflight_observations"] = visible_preflights
        candidates.append(candidate)
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
    source_payloads: dict[tuple[str, str, str, str], bytes] | None = None,
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
    preserve_userland_source_payload = (
        _is_native_userland_audit_source_pointer(pointer)
        or _is_native_userland_pressure_source_pointer(pointer, source)
        or _is_commons_pressure_source_pointer(pointer, source)
    )
    wants_source_payload = (
        source_payloads is not None
        and key not in source_payloads
        and len(source_payloads) < MAX_NATIVE_USERLAND_SOURCE_PAYLOADS
        and preserve_userland_source_payload
    )
    if (matching_reference is not None
            and matching_reference.get("verification") == "verified"):
        cache[key] = "record-integrity-verified-in-projection"
        if not wants_source_payload:
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
    if raw is None and status == "source-object-too-large" and not wants_source_payload:
        streamed_digest, stream_status = _git_blob_sha256(
            checkout, pointer["commit"], pointer["path"],
            deadline=deadline,
            max_bytes=MAX_SOURCE_HASH_BYTES,
            oversized_status="source-object-too-large",
        )
        if streamed_digest is not None:
            if streamed_digest != pointer["sha256"]:
                cache[key] = "sha256-mismatch"
            elif verification_basis == "workspace-read-only":
                cache[key] = "workspace-read-only-content-integrity-verified"
            else:
                cache[key] = "content-integrity-verified"
            return
        cache[key] = {
            "read-budget-exhausted": "source-read-budget-exhausted",
            "commit-path-unavailable": "source-object-unavailable",
            "source-object-too-large": "source-object-too-large",
            "object-size-unavailable": "source-object-size-unavailable",
            "object-size-mismatch": "source-object-size-mismatch",
            "git-read-unavailable": "source-git-read-unavailable",
        }.get(stream_status, f"source-{stream_status}")
        return
    if raw is None:
        cache[key] = {
            "read-budget-exhausted": "source-read-budget-exhausted",
            "commit-path-unavailable": "source-object-unavailable",
        }.get(status, status if status.startswith("source-") else f"source-{status}")
    elif hashlib.sha256(raw).hexdigest() != pointer["sha256"]:
        cache[key] = "sha256-mismatch"
    elif verification_basis == "workspace-read-only":
        cache[key] = "workspace-read-only-content-integrity-verified"
        if wants_source_payload and source_payloads is not None:
            source_payloads[key] = raw
    else:
        cache[key] = "content-integrity-verified"
        if wants_source_payload and source_payloads is not None:
            source_payloads[key] = raw


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


def _is_native_userland_audit_source_pointer(pointer: dict[str, str]) -> bool:
    repository = pointer.get("repository")
    if repository not in NATIVE_USERLAND_AUDIT_SOURCE_REPOSITORIES:
        return False
    identity_prefix = "audit_" + repository.replace("-", "_") + "_native_userland_"
    return (
        pointer.get("identity", "").startswith(identity_prefix)
        and pointer.get("path") == "native-userland-audit.json"
        and isinstance(pointer.get("sha256"), str)
    )


def _is_native_userland_pressure_source_pointer(
    pointer: dict[str, str], source: Any,
) -> bool:
    kind = source.get("kind") if isinstance(source, dict) else None
    identity = pointer.get("identity", "")
    match = re.fullmatch(r"pressures/(MNCS-TEST-P-[0-9]{3})\.json", pointer.get("path", ""))
    return (
        pointer.get("repository") == "mncs-test"
        and isinstance(kind, str) and "pressure" in kind.lower()
        and match is not None and match.group(1) == identity
    )


def _is_commons_pressure_source_pointer(
    pointer: dict[str, str], source: Any,
) -> bool:
    if pointer.get("repository") != COMMONS_PRESSURE_SOURCE_REPOSITORY:
        return False
    kind = source.get("kind") if isinstance(source, dict) else None
    identity = pointer.get("identity", "")
    pressure_id = r"MNCS-[A-Z]+-[0-9A-F]{12}"
    if kind == COMMONS_PRESSURE_RECORD_SOURCE_KIND:
        return (
            re.fullmatch(pressure_id, identity) is not None
            and pointer.get("path") == f"pressures/records/{identity}.json"
            and isinstance(pointer.get("sha256"), str)
        )
    if kind == COMMONS_PRESSURE_OBSERVATION_SOURCE_KIND:
        match = re.fullmatch(
            rf"({pressure_id})--OBS-([0-9A-F]{{12}})", identity,
        )
        return (
            match is not None
            and pointer.get("path") == f"pressures/observations/{identity}.json"
            and isinstance(pointer.get("sha256"), str)
        )
    return False


def _bounded_source_strings(value: Any) -> tuple[list[str], bool]:
    if not isinstance(value, list):
        return [], False
    return (
        [
            _short_text(item, 192)
            for item in value[:MAX_NATIVE_USERLAND_AUDIT_ITEMS]
            if isinstance(item, str)
        ],
        len(value) > MAX_NATIVE_USERLAND_AUDIT_ITEMS,
    )


def _bounded_native_userland_responsibilities(value: Any) -> tuple[list[dict[str, str]], bool]:
    if not isinstance(value, list):
        return [], False
    records: list[dict[str, str]] = []
    for item in value[:MAX_NATIVE_USERLAND_AUDIT_ITEMS]:
        if not isinstance(item, dict):
            continue
        record = {
            key: _short_text(item.get(key), 192)
            for key in (
                "area", "classification", "eliminating_capability",
                "canonical_owner",
            )
            if isinstance(item.get(key), str)
        }
        if record:
            records.append(record)
    return records, len(value) > MAX_NATIVE_USERLAND_AUDIT_ITEMS


def _native_userland_audit_source_view(
    source: Any, source_payloads: dict[tuple[str, str, str, str], bytes],
    source_integrity: dict[tuple[str, str, str, str], str],
    supporting_sources: list[Any] | None = None,
) -> dict[str, Any] | None:
    pointer = _validate_source_pointer(source)
    if pointer is None or not _is_native_userland_audit_source_pointer(pointer):
        return None
    integrity = _source_integrity_status(source, source_integrity)
    result: dict[str, Any] = {
        "identity": pointer["identity"],
        "repository": pointer["repository"],
    }
    if integrity not in SOURCE_INTEGRITY_VERIFIED:
        result["source_record_status"] = "source-content-not-verified"
        result["source_content_integrity_status"] = integrity
        return result
    raw = source_payloads.get(_source_pointer_key(pointer))
    if raw is None:
        result["source_record_status"] = "verified-payload-unavailable"
        return result
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        result["source_record_status"] = "verified-content-not-json"
        return result
    if (not isinstance(record, dict)
            or record.get("schema_version") != NATIVE_USERLAND_AUDIT_SCHEMA
            or record.get("repository") != pointer["repository"]):
        result["source_record_status"] = "unsupported-or-mismatched-audit-record"
        return result
    result.update({
        "source_record_status": "content-integrity-verified-owner-declaration",
    })
    for source_key, output_key in (
        ("native_entrypoint", "native_entrypoint"),
        ("canonical_entrypoint", "canonical_entrypoint"),
    ):
        value = record.get(source_key)
        if isinstance(value, str):
            if (source_key == "canonical_entrypoint"
                    and value == record.get("native_entrypoint")):
                continue
            result[output_key] = _short_text(value, 256)
    migration_order = record.get("migration_order")
    if type(migration_order) is int and migration_order >= 0:
        result["migration_order"] = migration_order
    for key in ("canonical_mncs_surfaces", "existing_pressures"):
        values, truncated = _bounded_source_strings(record.get(key))
        if values or isinstance(record.get(key), list):
            result[key] = values
            result[f"{key}_truncated"] = truncated
    pressure_links = _owner_declared_pressure_links(
        result.get("existing_pressures"), supporting_sources or [],
        source_integrity, source_payloads=source_payloads,
        identities_truncated=result.get("existing_pressures_truncated") is True,
    )
    if pressure_links is not None:
        result["pressure_source_links"] = pressure_links
    for source_key in ("remaining_host_responsibilities", "responsibilities"):
        records, truncated = _bounded_native_userland_responsibilities(
            record.get(source_key),
        )
        if records or isinstance(record.get(source_key), list):
            result[source_key] = records
            result[f"{source_key}_truncated"] = truncated
    raw_paths = record.get("remaining_host_paths")
    if isinstance(raw_paths, list):
        result["remaining_host_paths"] = [
            {
                key: _short_text(item.get(key), 192)
                for key in ("path", "role")
                if isinstance(item.get(key), str)
            }
            for item in raw_paths[:MAX_NATIVE_USERLAND_AUDIT_ITEMS]
            if isinstance(item, dict)
        ]
        result["remaining_host_paths_truncated"] = (
            len(raw_paths) > MAX_NATIVE_USERLAND_AUDIT_ITEMS
        )
    semantic_projection = record.get("native_semantic_projection")
    if isinstance(semantic_projection, dict):
        result["native_semantic_projection"] = {
            key: _short_text(semantic_projection.get(key), 192)
            for key in ("test", "suite", "obligation_selection")
            if isinstance(semantic_projection.get(key), str)
        }
        boundaries, truncated = _bounded_source_strings(
            semantic_projection.get("rust_boundary_only"),
        )
        if boundaries or isinstance(semantic_projection.get("rust_boundary_only"), list):
            result["rust_boundary_only"] = boundaries
            result["rust_boundary_only_truncated"] = truncated
    verification = record.get("verification_plan_ingestion")
    if isinstance(verification, dict):
        result["verification_plan_ingestion"] = {
            key: _short_text(verification.get(key), 256)
            for key in (
                "status", "entrypoint", "selection_authority",
                "remaining_blocker",
            )
            if isinstance(verification.get(key), str)
        }
        for key in ("shell_projection", "jq_required"):
            if type(verification.get(key)) is bool:
                result["verification_plan_ingestion"][key] = verification[key]
    return result


def _native_userland_pressure_aliases(
    sources: list[Any],
    source_integrity: dict[tuple[str, str, str, str], str],
    source_payloads: dict[tuple[str, str, str, str], bytes],
) -> dict[str, list[tuple[dict[str, str], dict[str, Any]]]]:
    """Read only verified MNCS-Test pressure records for their declared Commons ID."""
    aliases: dict[str, list[tuple[dict[str, str], dict[str, Any]]]] = {}
    for source in sources:
        pointer = _validate_source_pointer(source)
        if pointer is None or not _is_native_userland_pressure_source_pointer(
            pointer, source,
        ):
            continue
        key = _source_pointer_key(pointer)
        if source_integrity.get(key) not in SOURCE_INTEGRITY_VERIFIED:
            continue
        raw = source_payloads.get(key)
        if raw is None:
            continue
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if (not isinstance(record, dict)
                or record.get("schema_version") != NATIVE_USERLAND_PRESSURE_SCHEMA
                or record.get("id") != pointer.get("identity")):
            continue
        pressure_identity = record.get("commons_pressure_id")
        if (not isinstance(pressure_identity, str) or not pressure_identity
                or len(pressure_identity) > 160 or "\x00" in pressure_identity):
            continue
        aliases.setdefault(pressure_identity, []).append((pointer, source))
    return aliases


def _owner_declared_pressure_links(
    identities: Any, supporting_sources: list[Any],
    source_integrity: dict[tuple[str, str, str, str], str], *,
    source_payloads: dict[tuple[str, str, str, str], bytes] | None = None,
    identities_truncated: bool = False,
) -> dict[str, Any] | None:
    """Join owner-declared IDs to direct pointers or verified pressure aliases."""
    if not isinstance(identities, list):
        return None
    source_window = supporting_sources[:MAX_SOURCE_POINTER_VERIFICATIONS]
    source_window_truncated = len(supporting_sources) > len(source_window)
    aliases_by_identity = _native_userland_pressure_aliases(
        source_window, source_integrity, source_payloads or {},
    )
    by_identity: dict[str, list[tuple[dict[str, str], dict[str, Any]]]] = {}
    for source in source_window:
        pointer = _validate_source_pointer(source)
        identity = source.get("identity") if isinstance(source, dict) else None
        if pointer is None or not isinstance(identity, str) or not identity:
            continue
        by_identity.setdefault(identity, []).append((pointer, source))

    links: list[dict[str, Any]] = []
    for identity in identities[:MAX_NATIVE_USERLAND_AUDIT_ITEMS]:
        if (not isinstance(identity, str) or not identity or len(identity) > 160
                or "\x00" in identity):
            links.append({
                "identity": _short_text(identity, 160),
                "status": "invalid-owner-declared-pressure-identity",
                "causal_relation_status": "not-established",
            })
            continue
        matches = by_identity.get(identity, [])
        unique_matches: dict[tuple[str, str, str, str], tuple[
            dict[str, str], list[dict[str, Any]],
        ]] = {}
        for pointer, source in matches:
            key = _source_pointer_key(pointer)
            if key not in unique_matches:
                unique_matches[key] = (pointer, [])
            unique_matches[key][1].append(source)
        if not unique_matches:
            alias_matches = aliases_by_identity.get(identity, [])
            unique_aliases: dict[tuple[str, str, str, str], tuple[
                dict[str, str], list[dict[str, Any]],
            ]] = {}
            for pointer, source in alias_matches:
                key = _source_pointer_key(pointer)
                if key not in unique_aliases:
                    unique_aliases[key] = (pointer, [])
                unique_aliases[key][1].append(source)
            if len(unique_aliases) > 1:
                links.append({
                    "identity": identity,
                    "status": "ambiguous-integrity-verified-pressure-aliases",
                    "causal_relation_status": "not-established",
                })
                continue
            if len(unique_aliases) == 1:
                pointer, alias_sources = next(iter(unique_aliases.values()))
                source_kinds = sorted({
                    source["kind"] for source in alias_sources
                    if isinstance(source.get("kind"), str)
                })
                integrity = _source_integrity_status(
                    alias_sources[0], source_integrity,
                )
                if len(source_kinds) != 1:
                    links.append({
                        "identity": identity,
                        "status": "ambiguous-supporting-source-kinds",
                        "causal_relation_status": "not-established",
                    })
                    continue
                links.append({
                    "identity": identity,
                    "status": "linked-through-integrity-verified-pressure-alias",
                    "causal_relation_status": "not-established",
                    "relationship_basis": (
                        "mncs-test.pressure/1.commons_pressure_id"
                    ),
                    "source_content_integrity_status": integrity,
                    "source_kind": _short_text(source_kinds[0], 128),
                    "source_reference": dict(pointer),
                })
                continue
            status = (
                "not-found-in-bounded-supporting-source-window"
                if source_window_truncated
                else "no-matching-supporting-source-reference"
            )
            links.append({
                "identity": identity,
                "status": status,
                "causal_relation_status": "not-established",
            })
            continue
        if len(unique_matches) != 1:
            links.append({
                "identity": identity,
                "status": "ambiguous-supporting-source-references",
                "causal_relation_status": "not-established",
            })
            continue

        pointer, matched_sources = next(iter(unique_matches.values()))
        source_kinds = sorted({
            source["kind"] for source in matched_sources
            if isinstance(source.get("kind"), str)
        })
        source_kind = source_kinds[0] if len(source_kinds) == 1 else None
        integrity = _source_integrity_status(matched_sources[0], source_integrity)
        normalized_kind = source_kind.lower() if source_kind is not None else ""
        if len(source_kinds) > 1:
            status = "ambiguous-supporting-source-kinds"
        elif "pressure" not in normalized_kind and "audit" in normalized_kind:
            status = "identity-matches-non-pressure-source"
        elif "pressure" not in normalized_kind:
            status = "source-kind-does-not-identify-pressure"
        elif integrity in SOURCE_INTEGRITY_VERIFIED:
            status = "linked-to-integrity-verified-pressure-source"
        else:
            status = "linked-to-pressure-source-content-not-verified"
        link: dict[str, Any] = {
            "identity": identity,
            "status": status,
            "causal_relation_status": "not-established",
            "relationship_basis": "direct-owner-audit-pressure-identity-match",
            "source_content_integrity_status": integrity,
        }
        if isinstance(source_kind, str):
            link["source_kind"] = _short_text(source_kind, 128)
        link["source_reference"] = dict(pointer)
        links.append(link)

    verified_link_count = sum(
        item.get("status") in {
            "linked-to-integrity-verified-pressure-source",
            "linked-through-integrity-verified-pressure-alias",
        }
        for item in links
    )
    if not identities:
        status = "no-pressure-identities-declared"
    elif verified_link_count == len(links) and not identities_truncated:
        status = "all-projected-pressure-links-integrity-verified"
    elif verified_link_count:
        status = "partially-linked"
    else:
        status = "links-unverified-or-unavailable"
    return {
        "status": status,
        "relationship_basis": (
            "owner-declared pressure identities matched directly or through an "
            "integrity-verified pressure-record alias in the same observation"
        ),
        "causal_relation_status": "not-established",
        "supporting_source_count_examined": len(source_window),
        "supporting_source_window_truncated": source_window_truncated,
        "links": links,
        "links_truncated": len(identities) > len(links),
        "interpretation_limit": (
            "A matching pointer or verified pressure-record alias makes an "
            "owner-declared pressure ID traceable; it does not establish that the "
            "pressure blocks a specific host responsibility or that native "
            "migration is ready."
        ),
    }


def _commons_consumer_observation(
    pointer: dict[str, str], source: dict[str, Any], expected_pressure_identity: str,
    source_integrity: dict[tuple[str, str, str, str], str],
    source_payloads: dict[tuple[str, str, str, str], bytes],
) -> dict[str, Any]:
    """Project one digest-verified Commons pressure or consumer record."""
    reference = {
        key: pointer[key]
        for key in ("identity", "repository", "commit", "path", "sha256")
    }
    reference["kind"] = source.get("kind")
    integrity = _source_integrity_status(source, source_integrity)
    result: dict[str, Any] = {
        "source_reference": reference,
        "content_integrity_status": integrity,
    }
    if integrity not in SOURCE_INTEGRITY_VERIFIED:
        result["status"] = "source-content-not-verified"
        return result
    raw = source_payloads.get(_source_pointer_key(pointer))
    if raw is None:
        result["status"] = "verified-payload-unavailable"
        return result
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        result["status"] = "verified-content-not-json"
        return result
    if not isinstance(record, dict):
        result["status"] = "verified-json-is-not-an-object"
        return result

    kind = source.get("kind")
    if kind == COMMONS_PRESSURE_RECORD_SOURCE_KIND:
        if (record.get("schema") != COMMONS_PRESSURE_RECORD_SCHEMA
                or record.get("id") != expected_pressure_identity
                or record.get("id") != pointer.get("identity")):
            result["status"] = "verified-pressure-record-schema-or-identity-mismatch"
            return result
        result.update({
            "status": "source-integrity-schema-and-pressure-identity-verified",
            "identity": record["id"],
            "recorded_status": _short_text(record.get("initialStatus"), 96),
            "title": _short_text(record.get("title"), 256),
            "summary": _short_text(record.get("summary"), 512),
            "required_behavior": _short_text(record.get("requiredBehavior"), 512),
        })
        affected = record.get("affectedRepositories")
        if isinstance(affected, list):
            result["affected_repositories"] = [
                _short_text(item, 128) for item in affected[:8]
                if _is_repository_identity(item)
            ]
            result["affected_repositories_truncated"] = len(affected) > 8
        return result

    if kind != COMMONS_PRESSURE_OBSERVATION_SOURCE_KIND:
        result["status"] = "unsupported-source-kind"
        return result
    consumer_repository = record.get("repository")
    if (record.get("schema") != COMMONS_PRESSURE_OBSERVATION_SCHEMA
            or record.get("pressureId") != expected_pressure_identity
            or record.get("id") != pointer.get("identity")
            or not _is_repository_identity(consumer_repository)):
        result["status"] = "verified-consumer-observation-schema-or-identity-mismatch"
        return result

    metadata = record.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    reproduction = record.get("reproduction")
    reproduction = reproduction if isinstance(reproduction, dict) else {}
    workaround = record.get("workaround")
    workaround = workaround if isinstance(workaround, dict) else {}
    projected: dict[str, Any] = {
        "status": "source-integrity-schema-and-pressure-identity-verified",
        "identity": record["id"],
        "record_kind": _short_text(record.get("kind"), 64),
        "consumer_repository": consumer_repository,
        "observed_at": _short_text(record.get("observedAt"), 96),
        "language_profile": _short_text(record.get("languageProfile"), 64),
        "source_recorded_agent": _short_text(record.get("agent"), 96),
        "reproduction_status": _short_text(reproduction.get("status"), 64),
        "capability_state": _short_text(metadata.get("capabilityState"), 96),
        "consumer_migration": _short_text(metadata.get("consumerMigration"), 96),
        "consumer_status": _short_text(metadata.get("consumer_status"), 96),
        "classification": _short_text(metadata.get("classification"), 128),
        "summary": _short_text(record.get("summary"), 768),
        "source_reference": reference,
    }
    if type(metadata.get("currentValidation")) is bool:
        projected["current_validation"] = metadata["currentValidation"]
    projected_workaround = {
        key: workaround[key]
        for key in ("active", "removed")
        if type(workaround.get(key)) is bool
    }
    if projected_workaround:
        projected["workaround"] = projected_workaround
    missing = metadata.get("missing")
    if isinstance(missing, list):
        projected["missing_contracts"] = [
            _short_text(item, 256) for item in missing[:8]
            if isinstance(item, str)
        ]
        projected["missing_contracts_truncated"] = len(missing) > 8
    related_pressure_ids: list[str] = []
    related_pressure_pattern = re.compile(r"MNCS-[A-Z]+-[0-9A-F]{12}")
    evidence = record.get("evidence")
    if isinstance(evidence, list):
        for item in evidence[:8]:
            if not isinstance(item, dict):
                continue
            if item.get("kind") != "language-pressure":
                continue
            value = item.get("ref")
            if (isinstance(value, str)
                    and related_pressure_pattern.fullmatch(value)
                    and value not in related_pressure_ids):
                related_pressure_ids.append(value)
    if related_pressure_ids:
        projected["source_declared_related_pressure_identities"] = (
            related_pressure_ids[:4]
        )
    return projected


def _native_capability_consumer_evidence_projection(
    observations: list[dict[str, Any]], target_pressure_identity: Any,
    native_capability_identity: Any,
    source_integrity: dict[tuple[str, str, str, str], str] | None,
    source_payloads: dict[tuple[str, str, str, str], bytes] | None,
) -> dict[str, Any] | None:
    """Join Commons consumer reports to one declared capability pressure ID."""
    if (not isinstance(target_pressure_identity, str)
            or not target_pressure_identity
            or not isinstance(native_capability_identity, str)
            or not native_capability_identity):
        return None
    integrity = source_integrity or {}
    payloads = source_payloads or {}
    linked_records: list[dict[str, Any]] = []
    references: dict[tuple[str, str, str, str], tuple[
        dict[str, str], dict[str, Any],
    ]] = {}
    for record in observations[:MAX_OBSERVATIONS]:
        observation = record.get("observation")
        observation = observation if isinstance(observation, dict) else {}
        if observation.get("kind") != "native-capability-consumer-evidence":
            continue
        outcome = observation.get("outcome")
        outcome = outcome if isinstance(outcome, dict) else {}
        if (outcome.get("target_pressure_identity") != target_pressure_identity
                or outcome.get("native_capability_identity")
                != native_capability_identity):
            continue
        linked_records.append(record)
        raw_sources = record.get("supporting_sources")
        raw_sources = raw_sources if isinstance(raw_sources, list) else []
        for source in raw_sources[:MAX_COMMONS_PRESSURE_SOURCE_PAYLOADS]:
            pointer = _validate_source_pointer(source)
            if pointer is None or not _is_commons_pressure_source_pointer(pointer, source):
                continue
            key = _source_pointer_key(pointer)
            references.setdefault(key, (pointer, source))

    pressure_record: dict[str, Any] | None = None
    related_pressure_candidates: dict[
        str, tuple[dict[str, str], dict[str, Any]],
    ] = {}
    consumer_observations: list[dict[str, Any]] = []
    unverified: list[dict[str, Any]] = []
    invalid_count = 0
    for pointer, source in list(references.values())[:MAX_COMMONS_PRESSURE_SOURCE_PAYLOADS]:
        kind = source.get("kind")
        if kind == COMMONS_PRESSURE_RECORD_SOURCE_KIND:
            identity = pointer.get("identity")
            if identity != target_pressure_identity and isinstance(identity, str):
                related_pressure_candidates.setdefault(identity, (pointer, source))
                continue
            expected_identity = target_pressure_identity
        elif kind == COMMONS_PRESSURE_OBSERVATION_SOURCE_KIND:
            expected_identity = target_pressure_identity
        else:
            continue
        projected = _commons_consumer_observation(
            pointer, source, expected_identity, integrity, payloads,
        )
        if projected.get("status") != (
            "source-integrity-schema-and-pressure-identity-verified"
        ):
            if projected.get("status") in {
                "source-content-not-verified", "verified-payload-unavailable",
            }:
                unverified.append(projected)
            else:
                invalid_count += 1
            continue
        if source.get("kind") == COMMONS_PRESSURE_RECORD_SOURCE_KIND:
            pressure_record = projected
        elif source.get("kind") == COMMONS_PRESSURE_OBSERVATION_SOURCE_KIND:
            consumer_observations.append(projected)

    if not linked_records and not references:
        return None
    consumer_repositories = sorted({
        item["consumer_repository"] for item in consumer_observations
        if isinstance(item.get("consumer_repository"), str)
    })
    migration_states = sorted({
        item["consumer_migration"] for item in consumer_observations
        if isinstance(item.get("consumer_migration"), str)
    })
    related_pressure_ids = sorted({
        identity
        for item in consumer_observations
        for identity in item.get("source_declared_related_pressure_identities", [])
        if isinstance(identity, str)
    })
    related_pressure_record_by_identity: dict[str, dict[str, Any]] = {}
    for related_identity in related_pressure_ids[:MAX_COMMONS_RELATED_PRESSURE_RECORDS]:
        candidate = related_pressure_candidates.get(related_identity)
        if candidate is None:
            continue
        pointer, source = candidate
        projected = _commons_consumer_observation(
            pointer, source, related_identity, integrity, payloads,
        )
        if projected.get("status") != (
            "source-integrity-schema-and-pressure-identity-verified"
        ):
            if projected.get("status") in {
                "source-content-not-verified", "verified-payload-unavailable",
            }:
                unverified.append(projected)
            else:
                invalid_count += 1
            continue
        related_pressure_record_by_identity[related_identity] = projected

    related_pressure_records: list[dict[str, Any]] = []
    for related_identity in related_pressure_ids[:MAX_COMMONS_RELATED_PRESSURE_RECORDS]:
        declaration_sources = [
            item["source_reference"]
            for item in consumer_observations
            if related_identity in item.get(
                "source_declared_related_pressure_identities", [],
            ) and isinstance(item.get("source_reference"), dict)
        ]
        related_record = related_pressure_record_by_identity.get(related_identity)
        relationship: dict[str, Any] = {
            "identity": related_identity,
            "status": (
                related_record.get("status")
                if isinstance(related_record, dict)
                else "authoritative-related-pressure-record-unavailable-or-unverified"
            ),
            "relation_basis": (
                "exact-id-from-source-declared-language-pressure-evidence-ref"
            ),
            "causal_relation_status": "not-established",
            "source_declaration_observations": declaration_sources[:4],
        }
        if isinstance(related_record, dict):
            for key in (
                "recorded_status", "title", "summary", "required_behavior",
                "affected_repositories", "source_reference",
            ):
                if key in related_record:
                    relationship[key] = related_record[key]
        related_pressure_records.append(relationship)
    recorded_affected = (
        pressure_record.get("affected_repositories", [])
        if isinstance(pressure_record, dict) else []
    )
    if len(consumer_repositories) > 1:
        cross_consumer_status = "multiple-consumer-repositories-recorded"
    elif consumer_repositories:
        cross_consumer_status = "one-consumer-repository-recorded"
    else:
        cross_consumer_status = "consumer-repositories-unavailable"
    passing_count = sum(
        isinstance(item.get("reproduction_status"), str)
        and item["reproduction_status"].upper() == "PASS"
        for item in consumer_observations
    )
    workaround_active_count = sum(
        isinstance(item.get("workaround"), dict)
        and item["workaround"].get("active") is True
        for item in consumer_observations
    )
    actionable_observation = (
        "Inspect the consumer migration states and missing contracts per repository, "
        "then follow any exact source-declared related pressure records. These "
        "relationships expose implementation conditions; they do not establish "
        "causality or authorize migration."
        if consumer_observations else (
            "Inspect the exact source references and resolve the unavailable or "
            "unverified consumer evidence before drawing a capability conclusion."
        )
    )
    return {
        "status": (
            "verified-commons-consumer-evidence-available"
            if consumer_observations else "consumer-evidence-unavailable-or-unverified"
        ),
        "join_basis": (
            "Exact target_pressure_identity and native_capability_identity in the "
            "campaign observation, followed by exact Commons pressureId checks "
            "against the capability correspondence; related pressure records are "
            "joined only when their exact identity appears in a verified consumer "
            "language-pressure evidence ref."
        ),
        "source_coverage_scope": (
            "bounded-source-references-in-authenticated-campaign-observations; "
            "not-an-exhaustive-consumer-inventory"
        ),
        "source_observation_count": len(consumer_observations),
        "consumer_repository_count": len(consumer_repositories),
        "consumer_repositories_observed": consumer_repositories[:8],
        "recorded_affected_repositories": recorded_affected[:8],
        "consumer_migration_states": migration_states[:8],
        "passing_reproduction_observation_count": passing_count,
        "workaround_active_observation_count": workaround_active_count,
        "related_pressure_identities": related_pressure_ids[:8],
        "related_pressure_records": related_pressure_records,
        "related_pressure_records_truncated": (
            len(related_pressure_ids) > len(related_pressure_records)
        ),
        "cross_consumer_status": cross_consumer_status,
        "recurrence_status": "not-established",
        "causal_relation_status": "not-established",
        "native_migration_readiness": "not-established",
        "pressure_record": pressure_record or {
            "status": "pressure-record-not-integrity-and-schema-verified",
        },
        "observations": consumer_observations[:8],
        "observations_truncated": len(consumer_observations) > 8,
        "unverified_source_count": len(unverified),
        "unverified_source_references": unverified[:4],
        "invalid_source_count": invalid_count,
        "collector_observation_identities": [
            record.get("identity") for record in linked_records[:4]
            if isinstance(record.get("identity"), str)
        ],
        "actionable_observation": actionable_observation,
        "interpretation_limit": (
            "Digest and schema checks establish traceable source-record content, not "
            "agent use, source truth, recurring failure, causality, complete consumer "
            "coverage, or readiness to migrate host functionality."
        ),
    }


def _native_capability_source_work_context(
    record: dict[str, Any],
) -> dict[str, Any]:
    """Retain the originating observation's bounded, source-declared work context."""
    raw_work = record.get("related_work")
    raw_work = raw_work if isinstance(raw_work, dict) else {}
    result: dict[str, Any] = {
        "status": "not-recorded-in-source-observation",
        "basis": "native-capability-observation.related_work",
        "objective_capture_status": (
            "objectives-not-recorded-in-source-observation"
        ),
    }
    field_limits = {
        "campaign_id": 160,
        "session_id": 160,
        "work_intent_id": 160,
        "work_intent_goal": 256,
        "campaign_objective": 256,
        "environment_id": 160,
        "definition_id": 160,
        "environment_version": 96,
    }
    captured = {}
    for key, limit in field_limits.items():
        value = raw_work.get(key)
        if isinstance(value, str) and value:
            captured[key] = _short_text(value, limit)
            if len(value) > limit:
                captured[f"{key}_truncated"] = True
    if captured:
        result.update(captured)
        result["status"] = "recorded-in-source-observation"
    objective_fields = [
        key for key in ("work_intent_goal", "campaign_objective")
        if isinstance(raw_work.get(key), str) and raw_work[key]
    ]
    if len(objective_fields) == 2:
        result["objective_capture_status"] = (
            "work-intent-and-campaign-objective-recorded"
        )
    elif objective_fields:
        result["objective_capture_status"] = "partial-objective-context-recorded"
    source_reference = _validate_reference(record.get("evidence_reference"))
    if source_reference is not None:
        result["source_observation_reference"] = source_reference
    source_identity = record.get("identity")
    if isinstance(source_identity, str) and source_identity:
        result["source_observation_identity"] = _short_text(source_identity, 160)
    return result


def _native_consumer_activity_source_references(
    record: dict[str, Any],
    source_integrity: dict[tuple[str, str, str, str], str],
) -> dict[str, Any]:
    raw_sources = record.get("supporting_sources")
    raw_sources = raw_sources if isinstance(raw_sources, list) else []
    references = []
    for source in raw_sources[:MAX_NATIVE_CONSUMER_ACTIVITY_SOURCE_REFERENCES]:
        pointer = _validate_source_pointer(source)
        if pointer is None:
            continue
        reference = dict(pointer)
        kind = source.get("kind") if isinstance(source, dict) else None
        if isinstance(kind, str):
            reference["kind"] = _short_text(kind, 128)
        reference["content_integrity_status"] = _source_integrity_status(
            source, source_integrity,
        )
        references.append(reference)
    return {
        "source_references": references,
        "source_references_truncated": (
            len(raw_sources) > MAX_NATIVE_CONSUMER_ACTIVITY_SOURCE_REFERENCES
        ),
    }


def _native_process_consumer_verification(
    record: dict[str, Any],
    source_integrity: dict[tuple[str, str, str, str], str],
) -> dict[str, Any]:
    observation = record.get("observation")
    observation = observation if isinstance(observation, dict) else {}
    outcome = observation.get("outcome")
    outcome = outcome if isinstance(outcome, dict) else {}
    integration = outcome.get("native_process_integration")
    integration = integration if isinstance(integration, dict) else {}
    typed_result = integration.get("observed_typed_result")
    typed_result = typed_result if isinstance(typed_result, dict) else {}
    result: dict[str, Any] = {
        "identity": _short_text(record.get("identity"), 160),
        "kind": NATIVE_PROCESS_CONSUMER_VERIFICATION_KIND,
        "integration_status": _short_text(integration.get("status"), 96),
        "compilation_status": _short_text(integration.get("compilation"), 256),
        "assertion": _short_text(integration.get("assertion"), 256),
        "interpretation_limit": _short_text(
            integration.get("interpretation_limit"), 512,
        ),
    }
    for name in ("resolver_unit_tests", "native_core_suite"):
        counts = outcome.get(name)
        if not isinstance(counts, dict):
            continue
        projected_counts = {
            key: counts[key]
            for key in ("passed", "failed")
            if type(counts.get(key)) is int and counts[key] >= 0
        }
        if projected_counts:
            result[name] = projected_counts
    projected_typed_result = {}
    for key in ("success", "has_exit_code", "timed_out"):
        if type(typed_result.get(key)) is bool:
            projected_typed_result[key] = typed_result[key]
    for key in ("exit_code", "subprocess_count"):
        if type(typed_result.get(key)) is int:
            projected_typed_result[key] = typed_result[key]
    for key in ("stdout_truncated", "stderr_truncated"):
        if type(typed_result.get(key)) is bool:
            projected_typed_result[key] = typed_result[key]
    if projected_typed_result:
        result["observed_typed_result"] = projected_typed_result
    pressure = record.get("pressure_hypothesis")
    if isinstance(pressure, dict):
        residual = {
            key: _short_text(pressure.get(key), limit)
            for key, limit in (
                ("identity", 160), ("title", 256), ("category", 128),
                ("subsystem", 256), ("owner", 128), ("recurrence", 192),
                ("actionable_recommendation", 512),
            )
            if isinstance(pressure.get(key), str)
        }
        if residual:
            result["reported_pressure_hypothesis"] = residual
    result.update(_native_consumer_activity_source_references(
        record, source_integrity,
    ))
    return result


def _native_capability_consumer_activity_projection(
    observations: list[dict[str, Any]], native_capability_identity: Any,
    source_integrity: dict[tuple[str, str, str, str], str],
) -> dict[str, Any] | None:
    """Join one native consumer pressure to its exact improvement and verification."""
    if (not isinstance(native_capability_identity, str)
            or not native_capability_identity
            or len(native_capability_identity) > 192):
        return None
    selected = observations[:MAX_OBSERVATIONS]
    consumer_pressure_records = []
    for record in selected:
        observation = record.get("observation")
        observation = observation if isinstance(observation, dict) else {}
        if observation.get("kind") != (
            NATIVE_CAPABILITY_CONSUMER_INTEGRATION_PRESSURE_KIND
        ):
            continue
        request = observation.get("request")
        request = request if isinstance(request, dict) else {}
        hypothesis = record.get("pressure_hypothesis")
        hypothesis = hypothesis if isinstance(hypothesis, dict) else {}
        hypothesis_identity = hypothesis.get("identity")
        if (request.get("native_capability_identity")
                != native_capability_identity
                or not isinstance(hypothesis_identity, str)
                or not hypothesis_identity or len(hypothesis_identity) > 160):
            continue
        consumer_pressure_records.append((record, request, hypothesis))
    if not consumer_pressure_records:
        return None

    improvements_by_pressure: dict[str, list[dict[str, Any]]] = {}
    for record in selected:
        observation = record.get("observation")
        observation = observation if isinstance(observation, dict) else {}
        if observation.get("kind") != ENVIRONMENTAL_IMPROVEMENT_LINK_KIND:
            continue
        request = observation.get("request")
        request = request if isinstance(request, dict) else {}
        target = request.get("addresses_hypothesis_identity")
        if not isinstance(target, str) or not target:
            continue
        improvements_by_pressure.setdefault(target, []).append(record)

    verifications_by_identity: dict[str, list[dict[str, Any]]] = {}
    for record in selected:
        observation = record.get("observation")
        observation = observation if isinstance(observation, dict) else {}
        if observation.get("kind") != NATIVE_PROCESS_CONSUMER_VERIFICATION_KIND:
            continue
        identity = record.get("identity")
        if isinstance(identity, str) and identity:
            verifications_by_identity.setdefault(identity, []).append(record)

    activities = []
    for pressure_record, request, hypothesis in consumer_pressure_records[
        :MAX_NATIVE_CONSUMER_ACTIVITY_RECORDS
    ]:
        pressure_observation = pressure_record.get("observation")
        pressure_observation = (
            pressure_observation if isinstance(pressure_observation, dict) else {}
        )
        pressure_outcome = pressure_observation.get("outcome")
        pressure_outcome = (
            pressure_outcome if isinstance(pressure_outcome, dict) else {}
        )
        pressure_identity = hypothesis["identity"]
        linked_improvements = []
        for improvement_record in improvements_by_pressure.get(
            pressure_identity, [],
        )[:MAX_NATIVE_CONSUMER_ACTIVITY_IMPROVEMENTS]:
            improvement_observation = improvement_record.get("observation")
            improvement_observation = (
                improvement_observation
                if isinstance(improvement_observation, dict) else {}
            )
            improvement_request = improvement_observation.get("request")
            improvement_request = (
                improvement_request if isinstance(improvement_request, dict) else {}
            )
            improvement_outcome = improvement_observation.get("outcome")
            improvement_outcome = (
                improvement_outcome if isinstance(improvement_outcome, dict) else {}
            )
            improvement_relation = improvement_record.get("improvement_relation")
            improvement_relation = (
                improvement_relation
                if isinstance(improvement_relation, dict) else {}
            )
            verification_identity = improvement_request.get(
                "verification_observation_identity",
            )
            relation_target = improvement_relation.get(
                "addresses_hypothesis_identity",
            )
            relation_verification_identity = improvement_relation.get(
                "verification_observation_identity",
            )
            declarations_match = (
                improvement_request.get("addresses_hypothesis_identity")
                == pressure_identity
                and relation_target == pressure_identity
                and isinstance(verification_identity, str)
                and relation_verification_identity == verification_identity
            )
            verification_matches = (
                verifications_by_identity.get(verification_identity, [])
                if declarations_match else []
            )
            improvement: dict[str, Any] = {
                "improvement_observation_identity": _short_text(
                    improvement_record.get("identity"), 160,
                ),
                "improvement_relation_identity": _short_text(
                    improvement_relation.get("identity"), 160,
                ),
                "addresses_hypothesis_identity": pressure_identity,
                "implementation_commit": _short_text(
                    improvement_request.get("implementation_commit"), 64,
                ),
                "implementation_scope": _short_text(
                    improvement_outcome.get("implementation_scope"), 256,
                ),
                "implementation_change": _short_text(
                    improvement_outcome.get("implementation_change"), 768,
                ),
                "verified_change": _short_text(
                    improvement_outcome.get("verified_change"), 768,
                ),
                "effectiveness_assessment": _short_text(
                    improvement_outcome.get("effectiveness_assessment"), 96,
                ),
                "subsequent_observation_identity": _short_text(
                    improvement_request.get("subsequent_observation_identity"),
                    160,
                ),
                "verification_observation_identity": _short_text(
                    verification_identity, 160,
                ),
                "improvement_record_consistency_status": (
                    "request-and-relation-identities-match"
                    if declarations_match else (
                        "improvement-request-and-relation-identity-mismatch"
                    )
                ),
                "verification_match_status": (
                    "improvement-request-and-relation-identity-mismatch"
                    if not declarations_match else (
                        "exact-verification-observation-identity-match"
                        if len(verification_matches) == 1 else (
                            "ambiguous-duplicate-verification-identities"
                            if len(verification_matches) > 1 else (
                                "referenced-verification-observation-unavailable"
                            )
                        )
                    )
                ),
                "interpretation_limit": _short_text(
                    improvement_outcome.get("interpretation_limit"), 512,
                ),
            }
            if len(verification_matches) == 1:
                improvement["verification_observation"] = (
                    _native_process_consumer_verification(
                        verification_matches[0], source_integrity,
                    )
                )
            improvement.update(_native_consumer_activity_source_references(
                improvement_record, source_integrity,
            ))
            linked_improvements.append(improvement)

        pressure_sources = _native_consumer_activity_source_references(
            pressure_record, source_integrity,
        )
        activity: dict[str, Any] = {
            "source_observation_identity": _short_text(
                pressure_record.get("identity"), 160,
            ),
            "source_observation_kind": (
                NATIVE_CAPABILITY_CONSUMER_INTEGRATION_PRESSURE_KIND
            ),
            "native_capability_identity": native_capability_identity,
            "target_pressure_identity": pressure_identity,
            "consumer_repository": _short_text(
                request.get("consumer_repository"), 128,
            ),
            "integration_test_identity": _short_text(
                request.get("integration_test_identity"), 192,
            ),
            "consumer_surface": _short_text(
                pressure_outcome.get("consumer_surface"), 512,
            ),
            "observed_limitation": _short_text(
                pressure_outcome.get("observed_limitation"), 768,
            ),
            "environmental_pressure": _short_text(
                pressure_outcome.get("environmental_pressure"), 768,
            ),
            "agent_impact": _short_text(
                pressure_outcome.get("agent_impact"), 256,
            ),
            "causal_scope": _short_text(
                pressure_outcome.get("causal_scope"), 512,
            ),
            "pressure_hypothesis": {
                key: _short_text(hypothesis.get(key), limit)
                for key, limit in (
                    ("title", 256), ("category", 128),
                    ("subsystem", 256), ("owner", 128),
                    ("recurrence", 192),
                    ("actionable_recommendation", 512),
                )
                if isinstance(hypothesis.get(key), str)
            },
            "source_work_context": _native_capability_source_work_context(
                pressure_record,
            ),
            "improvement_links": linked_improvements,
            "improvement_links_truncated": (
                len(improvements_by_pressure.get(pressure_identity, []))
                > len(linked_improvements)
            ),
            **pressure_sources,
        }
        activities.append(activity)

    return {
        "status": "native-consumer-pressure-evidence-available",
        "native_capability_identity": native_capability_identity,
        "join_basis": (
            "Exact native_capability_identity in the consumer pressure request, "
            "exact pressure_hypothesis.identity to improvement "
            "addresses_hypothesis_identity, and exact verification observation "
            "identity declared by that improvement."
        ),
        "source_coverage_scope": (
            "bounded-selected-campaign-observations; not-an-exhaustive-consumer-"
            "inventory"
        ),
        "activities": activities,
        "activities_truncated": (
            len(consumer_pressure_records) > len(activities)
        ),
        "recurrence_status": "not-established",
        "agent_usage_status": "not-established",
        "causal_relation_status": "not-established",
        "native_migration_readiness": "not-established",
        "systemwide_native_adoption_ratio": {"status": "not-measured"},
        "actionable_observation": (
            "Inspect the exact consumer pressure and linked verification result. "
            "Treat any remaining typed-result or runtime limitation as a separate "
            "investigation until its cause is established."
        ),
        "interpretation_limit": (
            "Exact observation identities establish traceability within the "
            "selected campaign evidence. They do not establish agent discovery, "
            "recurrence, causality beyond each observation's stated scope, "
            "long-term effectiveness, or native migration readiness."
        ),
    }


def _native_capability_correspondence_projection(
    observations: list[dict[str, Any]],
    source_integrity: dict[tuple[str, str, str, str], str] | None = None,
    source_payloads: dict[tuple[str, str, str, str], bytes] | None = None,
) -> list[dict[str, Any]]:
    """Expose bounded candidate links between host boundaries and MNCS APIs."""
    integrity = source_integrity or {}
    payloads = source_payloads or {}
    correspondences: list[dict[str, Any]] = []
    required_roles = (
        "language_contract", "host_runtime_provider", "host_runtime_dispatch",
        "target_pressure_source", "host_boundary_readiness_document",
    )
    for record in observations:
        observation = record.get("observation")
        observation = observation if isinstance(observation, dict) else {}
        if observation.get("kind") != NATIVE_CAPABILITY_CORRESPONDENCE_KIND:
            continue
        outcome = observation.get("outcome")
        outcome = outcome if isinstance(outcome, dict) else {}
        raw_sources = record.get("supporting_sources")
        raw_sources = raw_sources if isinstance(raw_sources, list) else []
        role_sources: dict[str, list[dict[str, Any]]] = {}
        all_valid_source_count = 0
        all_verified_source_count = 0
        for source in raw_sources[:MAX_NATIVE_CAPABILITY_SOURCE_REFERENCES]:
            if not isinstance(source, dict):
                continue
            pointer = _validate_source_pointer(source)
            if pointer is None:
                continue
            all_valid_source_count += 1
            source_status = _source_integrity_status(source, integrity)
            if source_status in SOURCE_INTEGRITY_VERIFIED:
                all_verified_source_count += 1
            kind = source.get("kind")
            role = NATIVE_CAPABILITY_CORRESPONDENCE_SOURCE_ROLES.get(kind)
            if role is None:
                continue
            role_sources.setdefault(role, []).append({
                **pointer,
                "kind": kind,
                "content_integrity_status": source_status,
            })

        source_evidence_by_role: dict[str, dict[str, Any]] = {}
        for role, references in role_sources.items():
            verified_count = sum(
                reference.get("content_integrity_status")
                in SOURCE_INTEGRITY_VERIFIED
                for reference in references
            )
            source_evidence_by_role[role] = {
                "status": (
                    "content-integrity-verified"
                    if verified_count == len(references) else (
                        "partially-content-integrity-verified"
                        if verified_count else "content-integrity-not-verified"
                    )
                ),
                "source_reference_count": len(references),
                "verified_reference_count": verified_count,
                "source_references": references[:4],
                "source_references_truncated": len(references) > 4,
            }
        required_roles_verified = all(
            source_evidence_by_role.get(role, {}).get("status")
            == "content-integrity-verified"
            for role in required_roles
        )
        pressure_identity = outcome.get("target_pressure_identity")
        pressure_link_projection = _owner_declared_pressure_links(
            [pressure_identity] if isinstance(pressure_identity, str) else [],
            raw_sources,
            integrity,
            source_payloads=payloads,
        )
        pressure_links = (
            pressure_link_projection.get("links", [])
            if isinstance(pressure_link_projection, dict) else []
        )
        integrity_linked_pressure = any(
            isinstance(link, dict)
            and isinstance(link.get("status"), str)
            and "integrity-verified" in link["status"]
            and link.get("source_content_integrity_status")
            in SOURCE_INTEGRITY_VERIFIED
            for link in pressure_links
        )
        source_evidence_status = (
            "required-source-blobs-and-pressure-alias-verified"
            if required_roles_verified and integrity_linked_pressure else (
                "partial-source-evidence"
                if all_verified_source_count else "source-evidence-not-verified"
            )
        )
        verification = outcome.get("verification_observation")
        verification = verification if isinstance(verification, dict) else {}
        result = {
            "identity": record.get("identity"),
            "source_work_context": _native_capability_source_work_context(record),
            "target_pressure_identity": _short_text(pressure_identity, 160),
            "native_capability_identity": _short_text(
                outcome.get("native_capability_identity"), 192,
            ),
            "relationship_assessment": _short_text(
                outcome.get("relationship_assessment"), 128,
            ),
            "recorded_pressure_status": _short_text(
                outcome.get("recorded_pressure_status"), 96,
            ),
            "target_pressure_summary": _short_text(
                outcome.get("target_pressure_summary"), 512,
            ),
            "host_boundary_summary": _short_text(
                outcome.get("host_boundary_summary"), 512,
            ),
            "source_evidence_status": source_evidence_status,
            "source_evidence_by_role": source_evidence_by_role,
            "supporting_source_reference_count": all_valid_source_count,
            "supporting_source_content_integrity_verified_count": (
                all_verified_source_count
            ),
            "supporting_sources_truncated": (
                len(raw_sources) > MAX_NATIVE_CAPABILITY_SOURCE_REFERENCES
            ),
            "pressure_source_links": pressure_link_projection or {
                "status": "target-pressure-identity-unavailable",
                "causal_relation_status": "not-established",
                "links": [],
            },
            "implementation_boundary": {
                key: _short_text(outcome.get("implementation_boundary", {}).get(key), 256)
                for key in (
                    "mncs_language_surface", "host_runtime_provider",
                    "consumer_boundary",
                )
                if isinstance(outcome.get("implementation_boundary"), dict)
                and isinstance(
                    outcome["implementation_boundary"].get(key), str,
                )
            },
            "verification_observation": {
                "status": _short_text(verification.get("status"), 96),
                "command": _short_text(verification.get("command"), 512),
                "test_identity": _short_text(verification.get("test_identity"), 192),
                "result_summary": _short_text(
                    verification.get("result_summary"), 256,
                ),
                "interpretation_limit": _short_text(
                    verification.get("interpretation_limit"), 512,
                ),
                **{
                    key: verification[key]
                    for key in ("passed", "failed", "filtered")
                    if type(verification.get(key)) is int
                    and verification[key] >= 0
                },
                **({
                    "unsupported_branch_may_return_early": verification[
                        "unsupported_branch_may_return_early"
                    ],
                } if type(verification.get(
                    "unsupported_branch_may_return_early",
                )) is bool else {}),
            },
            "agent_usage_status": "not-measured",
            "causal_relation_status": "not-established",
            "native_migration_readiness": "not-established",
            "systemwide_native_adoption_ratio": {"status": "not-measured"},
            "classification_provenance": (
                "Campaign-authored source-role interpretation; source integrity "
                "does not independently prove implementation ownership, consumer "
                "integration, or migration suitability."
            ),
            "next_investigation": _short_text(
                outcome.get("next_investigation"), 512,
            ),
            "interpretation_limit": (
                "A source correspondence identifies a candidate for investigation; "
                "it does not establish that the pressure caused the host boundary, "
                "that agents can discover or use the API, or that migration is ready."
            ),
        }
        native_consumer_activity = (
            _native_capability_consumer_activity_projection(
                observations, outcome.get("native_capability_identity"), integrity,
            )
        )
        if native_consumer_activity is not None:
            result["native_consumer_activity"] = native_consumer_activity
        consumer_evidence = _native_capability_consumer_evidence_projection(
            observations, pressure_identity,
            outcome.get("native_capability_identity"), integrity, payloads,
        )
        if consumer_evidence is not None:
            result["consumer_evidence"] = consumer_evidence
        if isinstance(outcome.get("measurement_limits"), list):
            result["measurement_limits"] = [
                _short_text(value, 384)
                for value in outcome["measurement_limits"][:8]
                if isinstance(value, str)
            ]
        correspondences.append(result)
        if len(correspondences) >= MAX_NATIVE_CAPABILITY_CORRESPONDENCES:
            break
    return correspondences


def _native_consumer_activities_projection(
    observations: list[dict[str, Any]],
    native_capability_correspondences: list[dict[str, Any]],
    source_integrity: dict[tuple[str, str, str, str], str] | None,
) -> tuple[list[dict[str, Any]], bool]:
    """Expose exact consumer evidence without requiring an older correspondence."""
    selected = observations[:MAX_OBSERVATIONS]
    already_exposed = {
        correspondence.get("native_capability_identity")
        for correspondence in native_capability_correspondences
        if isinstance(correspondence, dict)
        and isinstance(correspondence.get("native_consumer_activity"), dict)
    }
    identities: list[str] = []
    seen: set[str] = set()
    truncated = False
    for record in selected:
        observation = record.get("observation")
        observation = observation if isinstance(observation, dict) else {}
        if observation.get("kind") != (
            NATIVE_CAPABILITY_CONSUMER_INTEGRATION_PRESSURE_KIND
        ):
            continue
        request = observation.get("request")
        request = request if isinstance(request, dict) else {}
        identity = request.get("native_capability_identity")
        if (not isinstance(identity, str) or not identity or len(identity) > 192
                or identity in already_exposed or identity in seen):
            continue
        seen.add(identity)
        if len(identities) >= MAX_NATIVE_CONSUMER_CAPABILITY_IDENTITIES:
            truncated = True
            continue
        identities.append(identity)

    activities = []
    for identity in identities:
        activity = _native_capability_consumer_activity_projection(
            selected, identity, source_integrity or {},
        )
        if activity is not None:
            activities.append(activity)
    return activities, truncated


def _implementation_accountability_projection(
    observations: list[dict[str, Any]], *, native_proof_reference_present: bool,
    improvement_relations: list[dict[str, Any]] | None = None,
    selected_session_capability_delivery: Any = None,
    selected_session_id: str = "",
    owner_audit_source_payloads: dict[
        tuple[str, str, str, str], bytes,
    ] | None = None,
    source_integrity: dict[tuple[str, str, str, str], str] | None = None,
) -> dict[str, Any] | None:
    """Summarize source-verified proofs and separate owner-declared audit evidence."""
    relations_by_observation: dict[str, list[dict[str, Any]]] = {}
    for relation in improvement_relations or []:
        if not isinstance(relation, dict):
            continue
        recorded_by = relation.get("recorded_by_observation_identity")
        if not isinstance(recorded_by, str) or not recorded_by:
            continue
        relations_by_observation.setdefault(recorded_by, []).append(relation)
    relationship_observations = []
    for record in observations[:MAX_OBSERVATIONS]:
        if not isinstance(record, dict):
            continue
        if isinstance(record.get("improvement_relation"), dict):
            relationship_observations.append(record)
            continue
        identity = record.get("identity")
        matching_relations = (
            relations_by_observation.get(identity, [])
            if isinstance(identity, str) else []
        )
        if len(matching_relations) != 1:
            relationship_observations.append(record)
            continue
        relation = matching_relations[0]
        relationship_observations.append({
            **record,
            "improvement_relation": {
                key: relation[key]
                for key in (
                    "identity", "addresses_hypothesis_identity",
                    "verification_observation_identity",
                )
                if isinstance(relation.get(key), str)
            },
        })
    native_capability_correspondences = (
        _native_capability_correspondence_projection(
            relationship_observations,
            source_integrity,
            owner_audit_source_payloads,
        )
    )
    native_consumer_activities, native_consumer_activities_truncated = (
        _native_consumer_activities_projection(
            relationship_observations, native_capability_correspondences,
            source_integrity,
        )
    )
    samples: list[dict[str, Any]] = []
    owner_audits: list[dict[str, Any]] = []
    for observation_record in observations:
        observation = observation_record.get("observation")
        observation = observation if isinstance(observation, dict) else {}
        if observation.get("kind") == "cross-project-native-userland-accountability":
            outcome = observation.get("outcome")
            outcome = outcome if isinstance(outcome, dict) else {}
            raw_projects = outcome.get("projects")
            raw_projects = raw_projects if isinstance(raw_projects, list) else []
            projects = []
            for project in raw_projects[:8]:
                if not isinstance(project, dict):
                    continue
                projected_project = {
                    key: _short_text(project.get(key), 256)
                    for key in (
                        "repository", "owner_declared_status",
                        "owner_declared_native_surface",
                        "owner_declared_next_native_slice",
                    )
                    if isinstance(project.get(key), str)
                }
                responsibilities = project.get("remaining_host_responsibilities")
                if isinstance(responsibilities, list):
                    projected_project["remaining_host_responsibilities"] = [
                        _short_text(item, 192)
                        for item in responsibilities[:8]
                        if isinstance(item, str)
                    ]
                    projected_project["remaining_host_responsibilities_truncated"] = (
                        len(responsibilities) > 8
                    )
                if projected_project:
                    projects.append(projected_project)
            native_adoption = outcome.get("native_adoption")
            native_adoption = (
                native_adoption if isinstance(native_adoption, dict) else {}
            )
            declared_count = outcome.get("owner_audit_count")
            source_pointers = observation_record.get("supporting_sources")
            source_pointers = source_pointers if isinstance(source_pointers, list) else []
            owner_audit_source_details = [
                detail
                for source in source_pointers[:16]
                if (detail := _native_userland_audit_source_view(
                    source,
                    owner_audit_source_payloads or {},
                    source_integrity or {},
                    supporting_sources=source_pointers,
                )) is not None
            ]
            verified_owner_audit_details = sum(
                detail.get("source_record_status")
                == "content-integrity-verified-owner-declaration"
                for detail in owner_audit_source_details
            )
            owner_audit_detail_status = (
                "not-present-in-source-record"
                if not owner_audit_source_details else (
                    "content-integrity-verified-owner-declarations-available"
                    if verified_owner_audit_details
                    == len(owner_audit_source_details) else (
                        "partially-available-owner-declarations"
                        if verified_owner_audit_details
                        else "owner-audit-source-content-unavailable"
                    )
                )
            )
            actionable = outcome.get("actionable_opportunity")
            actionable = actionable if isinstance(actionable, dict) else {}
            pressure = observation_record.get("pressure_hypothesis")
            pressure = pressure if isinstance(pressure, dict) else {}
            evidence_reference = observation_record.get("evidence_reference")
            evidence_reference = (
                evidence_reference if isinstance(evidence_reference, dict) else None
            )
            owner_audits.append({
                "identity": observation_record.get("identity"),
                "projected_audit_record_count": 1,
                **({"source_declared_audit_count": declared_count}
                   if type(declared_count) is int and declared_count >= 0 else {}),
                "source_interpretation": _short_text(outcome.get(
                    "source_interpretation",
                    "repository-owner-declared; implementation ownership not independently verified",
                ), 512),
                "projects": projects,
                "actionable_opportunity": {
                    key: _short_text(actionable.get(key), 512)
                    for key in (
                        "title", "candidate_boundary", "current_observation",
                        "readiness", "safe_next_step",
                    )
                    if isinstance(actionable.get(key), str)
                },
                "systemwide_native_adoption_ratio": {
                    "status": "not-measured",
                    **({"denominator": _short_text(
                        native_adoption.get("denominator"), 128,
                    )} if isinstance(native_adoption.get("denominator"), str) else {}),
                    **({"reason": _short_text(
                        native_adoption.get("reason"), 512,
                    )} if isinstance(native_adoption.get("reason"), str) else {}),
                },
                "supporting_source_references": [
                    {
                        key: source[key]
                        for key in (
                            "identity", "kind", "repository", "commit", "path",
                            "sha256", "status", "content_integrity_status",
                        )
                        if isinstance(source.get(key), str)
                    }
                    for source in source_pointers[:8]
                    if isinstance(source, dict)
                ],
                "supporting_source_references_truncated": len(source_pointers) > 8,
                "source_audit_details": owner_audit_source_details,
                "source_audit_details_status": owner_audit_detail_status,
                "source_audit_details_content_integrity_verified_count": (
                    verified_owner_audit_details
                ),
                "pressure_hypothesis": {
                    key: _short_text(pressure.get(key), 256)
                    for key in ("identity", "title", "category", "recurrence")
                    if isinstance(pressure.get(key), str)
                },
                "evidence_reference": evidence_reference,
                "interpretation_limit": (
                    "Source content integrity does not independently verify repository-owner classifications, "
                    "implementation ownership, completeness, or migration readiness."
                ),
                "measurement_limits": [
                    _short_text(item, 512)
                    for item in observation_record.get("measurement_limits", [])[:16]
                    if isinstance(item, str)
                ] if isinstance(observation_record.get("measurement_limits"), list)
                else [],
            })
        if observation.get("kind") != (
            "compiler-native-application-implementation-ownership"
        ):
            continue
        outcome = observation.get("outcome")
        outcome = outcome if isinstance(outcome, dict) else {}
        accountability = outcome.get("implementation_accountability")
        if not isinstance(accountability, dict):
            continue
        sources = observation_record.get("supporting_sources")
        sources = sources if isinstance(sources, list) else []
        module_sources = [
            source for source in sources
            if isinstance(source, dict)
            and source.get("kind") == "native-language-module-source"
        ]
        expected_module_names = {
            module for module, _repository, _path, _revision_field
            in COMPILER_NATIVE_APPLICATION_MODULES
        }
        verified_module_names = {
            source.get("module")
            for source in module_sources
            if isinstance(source.get("module"), str)
            and source.get("module") in expected_module_names
            and isinstance(source.get("content_integrity_status"), str)
            and source.get("content_integrity_status") in SOURCE_INTEGRITY_VERIFIED
        }
        module_integrity_count = len(verified_module_names)
        proof_source = next((source for source in sources
                             if isinstance(source, dict)
                             and source.get("kind") == (
                                 "compiler-native-application-proof"
                             )), None)
        declared_module_count = accountability.get("native_module_count")
        if type(declared_module_count) is not int or declared_module_count < 0:
            declared_module_count = len(module_sources)
        if (len(module_sources) == declared_module_count
                and module_integrity_count == declared_module_count):
            ownership_status = "source-integrity-verified-for-sample"
        elif module_integrity_count:
            ownership_status = "partially-source-integrity-verified"
        else:
            ownership_status = "source-integrity-not-established"
        capability_contract = _compiler_capability_contract_view(
            accountability.get("compiler_capability_contract"),
        )
        sample = {
            "identity": observation_record.get("identity"),
            "application_entry": accountability.get("application_entry"),
            "sample_scope": accountability.get("sample_scope"),
            "native_semantic_ownership": accountability.get(
                "native_semantic_ownership",
            ),
            "ownership_status": ownership_status,
            "native_module_count": declared_module_count,
            "source_integrity_verified_module_count": module_integrity_count,
            "native_modules": [
                {
                    key: source[key]
                    for key in (
                        "module", "repository", "commit", "path", "sha256",
                        "status", "content_integrity_status",
                    )
                    if isinstance(source.get(key), str)
                }
                for source in module_sources[:len(COMPILER_NATIVE_APPLICATION_MODULES)]
            ],
            "compiler_proof_reference": {
                key: proof_source[key]
                for key in (
                    "identity", "repository", "commit", "path", "sha256",
                    "status", "content_integrity_status",
                )
                if isinstance(proof_source, dict)
                and isinstance(proof_source.get(key), str)
            },
            "artifact_identity": accountability.get("artifact_identity"),
            "compiler_front_end_outcome": accountability.get(
                "compiler_front_end_outcome",
            ),
            "canonical_vm_case_count": accountability.get(
                "canonical_vm_case_count",
            ),
            "canonical_vm_case_success_count": accountability.get(
                "canonical_vm_case_success_count",
            ),
            "host_test_adapter": accountability.get("host_test_adapter"),
            **({"compiler_capability_contract": capability_contract}
               if capability_contract is not None else {}),
            "native_migration_opportunity": accountability.get(
                "native_migration_opportunity", "not-established",
            ),
            "source_campaign_context": accountability.get(
                "source_campaign_context", "not-recorded-in-source-proof",
            ),
            "evidence_relationship_basis": accountability.get(
                "evidence_relationship_basis",
            ),
            "evidence_reference": observation_record.get("evidence_reference"),
        }
        expected_module_count = len(COMPILER_NATIVE_APPLICATION_MODULES)
        proof_integrity_verified = (
            isinstance(proof_source, dict)
            and proof_source.get("content_integrity_status")
            in SOURCE_INTEGRITY_VERIFIED
        )
        canonical_case_count = accountability.get("canonical_vm_case_count")
        canonical_case_success_count = accountability.get(
            "canonical_vm_case_success_count",
        )
        scoped_measurement_complete = (
            ownership_status == "source-integrity-verified-for-sample"
            and declared_module_count == expected_module_count
            and len(module_sources) == expected_module_count
            and verified_module_names == expected_module_names
            and module_integrity_count == expected_module_count
            and proof_integrity_verified
            and accountability.get("native_semantic_ownership")
            == "verified-for-this-application-proof"
            and accountability.get("compiler_front_end_outcome") == "verified"
            and canonical_case_count == 3
            and canonical_case_success_count == canonical_case_count
        )
        sample["scoped_native_module_ownership"] = {
            "status": (
                "measured" if scoped_measurement_complete else (
                    "partial" if module_sources or proof_source else "not-measured"
                )
            ),
            "scope": "one compiler-proven MNCS application source closure",
            "unit": "resolved application source module",
            "verified_native_module_count": module_integrity_count,
            "module_denominator": expected_module_count,
            "host_semantic_implementation_count": {
                "status": "not-measured",
                "reason": (
                    "The proof identifies a test-only host adapter but does not "
                    "inventory host implementations outside this application closure."
                ),
            },
            "host_test_adapter_included_in_denominator": False,
            "interpretation_limit": (
                "This source-module closure is specific to one verified application "
                "proof; it is not a functionality-weighted or systemwide adoption ratio."
            ),
        }
        delivery_observation = _selected_session_delivery_observation(
            capability_contract, selected_session_capability_delivery,
            session_id=selected_session_id,
        )
        if delivery_observation is not None:
            sample["selected_session_delivery_observation"] = delivery_observation
        samples.append(sample)
    owner_audit_evidence = None
    if owner_audits:
        owner_audit_evidence = {
            "observed_audit_record_count": len(owner_audits),
            "audits": owner_audits[:4],
            "audits_truncated": len(owner_audits) > 4,
            "implementation_ownership_status": (
                "owner-declared; not independently verified"
            ),
            "systemwide_native_adoption_ratio": {
                "status": "not-measured",
                "reason": (
                    "Owner audit samples do not establish a complete implementation "
                    "inventory or denominator."
                ),
            },
        }
    if not samples:
        if (not native_proof_reference_present and owner_audit_evidence is None
                and not native_capability_correspondences
                and not native_consumer_activities):
            return None
        result = {
            "schema_version": IMPLEMENTATION_ACCOUNTABILITY_SCHEMA,
            "sample_count": 0,
            "samples": [],
            "systemwide_native_adoption_ratio": {
                "status": "not-measured",
                "reason": "A complete, ownership-verified MNCS implementation inventory is unavailable.",
            },
        }
        if owner_audit_evidence is not None:
            result["owner_audit_evidence"] = owner_audit_evidence
        if native_capability_correspondences:
            result["native_capability_correspondences"] = (
                native_capability_correspondences
            )
        if native_consumer_activities:
            result["native_consumer_activities"] = native_consumer_activities
        if native_consumer_activities_truncated:
            result["native_consumer_activities_truncated"] = True
        return result
    result = {
        "schema_version": IMPLEMENTATION_ACCOUNTABILITY_SCHEMA,
        "scope": "source-backed implementation samples linked from the selected campaign evidence",
        "sample_count": len(samples),
        "samples": samples[:8],
        "systemwide_native_adoption_ratio": {
            "status": "not-measured",
            "reason": "A complete, ownership-verified inventory of MNCS functionality and its denominator is unavailable.",
        },
        "measurement_limits": [
            "Verified samples establish implementation ownership only for the referenced application and source closure.",
            "The Compiler proof does not inventory production host-language dependencies or establish native migration opportunities.",
            "Source campaign and agent identity are unavailable unless recorded in the source proof.",
            "Compiler proof and selected-session delivery observations are separate; neither establishes cross-agent discoverability.",
        ],
    }
    if owner_audit_evidence is not None:
        result["owner_audit_evidence"] = owner_audit_evidence
    if native_capability_correspondences:
        result["native_capability_correspondences"] = (
            native_capability_correspondences
        )
    if native_consumer_activities:
        result["native_consumer_activities"] = native_consumer_activities
    if native_consumer_activities_truncated:
        result["native_consumer_activities_truncated"] = True
    return result


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
    """Resolve a captured event pointer against selected bounded activity records."""
    if not _validate_captured_session_activity_reference(source):
        return {"event_reference_status": "invalid-source-reference"}
    expected_event_ids = set(source["event_identities"])
    if source["session_id"] != selected_session_id:
        return {
            "event_reference_status": "source-session-not-selected",
            "event_reference_count": len(expected_event_ids),
            "event_reference_matched_count": 0,
        }
    record_collections = (
        ("rpc_operation", "rpc_operations"),
        ("claim_recovery", "claim_recoveries"),
        ("operation_claim_recovery", "operation_claim_recoveries"),
        ("claim_coordination", "claim_coordination"),
        ("authority_denial", "authority_denials"),
        ("integration_eligibility", "integration_eligibility"),
    )
    matched_categories_by_event: dict[str, set[str]] = {}
    non_rpc_records_present = False
    for category, collection_key in record_collections:
        raw_records = activity.get(collection_key)
        if not isinstance(raw_records, list):
            continue
        if category != "rpc_operation" and raw_records:
            non_rpc_records_present = True
        for record in raw_records:
            if not isinstance(record, dict):
                continue
            reference = record.get("event_reference")
            identity = reference.get("identity") if isinstance(reference, dict) else None
            if isinstance(identity, str) and identity in expected_event_ids:
                matched_categories_by_event.setdefault(identity, set()).add(category)
    matched_event_ids = set(matched_categories_by_event)
    matched_categories = sorted({
        category
        for categories in matched_categories_by_event.values()
        for category in categories
    })
    activity_source = activity.get("source")
    activity_source = activity_source if isinstance(activity_source, dict) else None
    raw_operations = activity.get("rpc_operations")
    raw_operations = raw_operations if isinstance(raw_operations, list) else []
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
    claim_coordination_records = activity.get("claim_coordination")
    claim_coordination_records = (
        claim_coordination_records
        if isinstance(claim_coordination_records, list) else []
    )
    coordination_events_considered = (
        activity_source.get("claim_coordination_events_considered")
        if activity_source is not None else None
    )
    coordination_records_returned = (
        activity_source.get("claim_coordination_records_returned")
        if activity_source is not None else None
    )
    coordination_records_omitted = (
        activity_source.get("claim_coordination_records_omitted")
        if activity_source is not None else None
    )
    coordination_records_invalid = (
        activity_source.get("claim_coordination_records_invalid")
        if activity_source is not None else None
    )
    claim_coordination_bounded = (
        type(coordination_events_considered) is int
        and coordination_events_considered > 0
        and (
            type(coordination_records_returned) is not int
            or coordination_records_returned != len(claim_coordination_records)
            or type(coordination_records_omitted) is not int
            or coordination_records_omitted > 0
            or type(coordination_records_invalid) is not int
            or coordination_records_invalid > 0
            or coordination_events_considered != (
                coordination_records_returned
                + coordination_records_omitted
                + coordination_records_invalid
            )
        )
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
        or claim_coordination_bounded
        or non_rpc_records_present
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
        **({"event_reference_match_categories": matched_categories}
           if matched_categories else {}),
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
                or field not in {
                    "supporting_sources", "not_measured",
                    "pressure_hypothesis_role", "observation.request",
                    "observation.outcome",
                    "pressure_hypothesis.actionable_recommendation",
                }
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
    if ("pressure_hypothesis_role" in fields
            and (not isinstance(replacements["pressure_hypothesis_role"], str)
                 or replacements["pressure_hypothesis_role"]
                 not in PRESSURE_HYPOTHESIS_ROLES)):
        return None
    if ("pressure_hypothesis.actionable_recommendation" in fields
            and (not isinstance(
                    replacements["pressure_hypothesis.actionable_recommendation"],
                    str,
                 )
                 or not replacements[
                     "pressure_hypothesis.actionable_recommendation"
                 ]
                 or len(replacements[
                     "pressure_hypothesis.actionable_recommendation"
                 ]) > 512)):
        return None
    for field in ("observation.request", "observation.outcome"):
        if field in fields and not isinstance(replacements[field], dict):
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
                or observation.get("improvement_relation_identity") is not None
                or observation.get("pressure_hypothesis_role", "occurrence")
                != "occurrence"):
            # An implementation record may restate the pressure it addresses.
            # Its linked hypothesis is context for the change, not a second
            # occurrence. Other supporting records can state that role too.
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


def _current_context_binding_snapshot_summary(
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Return a bounded digest of currently available session bindings."""
    bindings = snapshot.get("bindings")
    if not isinstance(bindings, list):
        return {"status": "unavailable", "reason": "binding-inventory-unavailable"}
    if len(bindings) > MAX_SESSION_ACTIVITY_BINDING_SCAN:
        return {"status": "partial", "reason": "binding-scan-limit"}

    identities: list[dict[str, str]] = []
    unavailable_count = 0
    unknown_count = 0
    for binding in bindings:
        if not isinstance(binding, dict):
            return {"status": "partial", "reason": "binding-record-invalid"}
        availability = binding.get("availability") or {}
        if not isinstance(availability, dict):
            return {"status": "partial", "reason": "binding-availability-invalid"}
        availability_status = availability.get("status")
        if availability_status == "unknown":
            unknown_count += 1
            continue
        if availability_status != "available":
            unavailable_count += 1
            continue
        identity = {
            "capability": binding.get("capability"),
            "provider": binding.get("provider"),
            "contract_revision": binding.get("contract_revision"),
        }
        if any(
            not isinstance(value, str) or not value or len(value) > 256
            or "\x00" in value
            for value in identity.values()
        ):
            return {"status": "partial", "reason": "binding-identity-incomplete"}
        identities.append(identity)
        if len(identities) > MAX_CONTEXT_RESPONSE_CAPABILITY_LIST:
            return {"status": "partial", "reason": "available-list-truncated"}

    identities.sort(key=lambda item: (
        item["capability"], item["provider"], item["contract_revision"],
    ))
    digest = hashlib.sha256(json.dumps(
        identities, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return {
        "status": "ready",
        "available_count": len(identities),
        "unavailable_count": unavailable_count,
        "unknown_count": unknown_count,
        "available_identity_digest": digest,
    }


def _invocation_binding_identity(value: Any) -> dict[str, Any]:
    """Retain exact binding provenance when an invocation event carries it."""
    fields = ("provider", "binding_id", "contract_revision")
    if not isinstance(value, dict):
        return {"status": "not-recorded"}
    present = any(key in value for key in fields)
    identity = {
        key: _short_text(value[key], 160)
        for key in fields
        if isinstance(value.get(key), str)
        and 1 <= len(value[key]) <= 160
        and "\x00" not in value[key]
    }
    if not present:
        status = "not-recorded"
    elif len(identity) == len(fields):
        status = "complete"
    elif identity:
        status = "partial"
    else:
        status = "invalid"
    return {"status": status, **identity}


def _invocation_effect_target(
    value: Any, completion_value: Any = None,
) -> dict[str, Any] | None:
    """Project an explicit provider effect target without exposing checkout paths."""
    if not isinstance(value, dict):
        return None

    def bounded_identity(item: Any, limit: int) -> str | None:
        if (not isinstance(item, str) or not item or len(item) > limit
                or any(ord(character) < 32 or ord(character) == 127
                       for character in item)):
            return None
        return item

    repository = bounded_identity(value.get("repository"), 160)
    branch = bounded_identity(value.get("branch"), 160)
    checkout_identity = bounded_identity(value.get("checkout_identity"), 160)
    if repository is None:
        return {"status": "invalid"}
    result: dict[str, Any] = {
        "status": "recorded" if branch is not None else "partial",
        "repository": repository,
        "branch_status": "recorded" if branch is not None else "unavailable",
    }
    if branch is not None:
        result["branch"] = branch
    if checkout_identity is not None:
        result["checkout_identity"] = checkout_identity
    if value.get("kind", value.get("scope_kind")) == "worktree":
        result["scope_kind"] = "worktree"
    preflight_event = _event_reference(value.get("preflight_event"))
    if preflight_event is not None:
        result["preflight_event"] = preflight_event
    operation_lease = value.get("operation_lease")
    if isinstance(operation_lease, dict):
        lease_identity = bounded_identity(operation_lease.get("identity"), 160)
        lease_kind = operation_lease.get("kind")
        lease_scope = operation_lease.get("scope")
        lease_checkout = bounded_identity(
            operation_lease.get("checkout_identity"), 160,
        )
        if (lease_identity is not None
                and lease_kind == "checkout-directory-flock"
                and lease_scope == "one-provider-invocation"):
            projected_lease: dict[str, Any] = {
                "identity": lease_identity,
                "kind": lease_kind,
                "scope": lease_scope,
                "acquisition_status": operation_lease.get(
                    "acquisition_status",
                    "recorded" if operation_lease.get("status") == "held"
                    else "invalid",
                ),
                "release_status": operation_lease.get(
                    "release_status", "completion-not-recorded",
                ),
            }
            if lease_checkout is not None:
                projected_lease["checkout_identity"] = lease_checkout
            waited_ms = operation_lease.get("waited_ms")
            if type(waited_ms) is int and 0 <= waited_ms <= 3_600_000:
                projected_lease["waited_ms"] = waited_ms
            completion_lease = (
                completion_value.get("operation_lease")
                if isinstance(completion_value, dict) else None
            )
            if (isinstance(completion_lease, dict)
                    and completion_lease.get("identity") == lease_identity
                    and completion_lease.get("kind") == lease_kind
                    and completion_lease.get("scope") == lease_scope
                    and completion_lease.get("status") == "released"
                    and completion_lease.get("checkout_identity") == lease_checkout):
                projected_lease["release_status"] = "released-after-completion"
                held_ms = completion_lease.get("held_ms")
                if type(held_ms) is int and 0 <= held_ms <= 86_400_000:
                    projected_lease["held_ms"] = held_ms
            elif completion_value is not None:
                projected_lease["release_status"] = "release-not-correlated"
            for key, maximum in (("waited_ms", 3_600_000), ("held_ms", 86_400_000)):
                measured = operation_lease.get(key)
                if type(measured) is int and 0 <= measured <= maximum:
                    projected_lease[key] = measured
            if projected_lease["acquisition_status"] not in {"recorded", "invalid"}:
                projected_lease["acquisition_status"] = "invalid"
            if projected_lease["release_status"] not in {
                "released-after-completion", "completion-not-recorded",
                "release-not-correlated",
            }:
                projected_lease["release_status"] = "release-not-correlated"
            result["operation_lease"] = projected_lease
    return result


def _proposal_invocation_observation(
    proposal_reference: dict[str, Any], repository: str,
    interactions: list[dict[str, Any]], *,
    event_window_truncated: bool,
    invocation_records_omitted: int,
) -> dict[str, Any]:
    """Relate a proposal preflight to an exact later Environment invocation.

    This records invocation and provider-completion evidence only. It does not
    establish that an edit changed repository state, passed independent
    verification, was integrated, or improved the environment.
    """
    expected = {
        key: proposal_reference.get(key)
        for key in ("identity", "sequence", "type")
    }
    if (
        not isinstance(repository, str) or not repository
        or not isinstance(expected["identity"], str)
        or type(expected["sequence"]) is not int
        or not isinstance(expected["type"], str)
    ):
        return {
            "status": "invalid-proposal-reference",
            "causal_status": "not-established",
            "project_change_status": "not-measured-by-invocation-events",
        }

    same_identity: list[dict[str, Any]] = []
    matching: list[dict[str, Any]] = []
    for interaction in interactions:
        effect_target = interaction.get("effect_target")
        if not isinstance(effect_target, dict):
            continue
        reference = effect_target.get("preflight_event")
        if not isinstance(reference, dict):
            continue
        if reference.get("identity") != expected["identity"]:
            continue
        same_identity.append(interaction)
        invocation_reference = interaction.get("invocation_event")
        invocation_sequence = (
            invocation_reference.get("sequence")
            if isinstance(invocation_reference, dict) else None
        )
        if (
            all(reference.get(key) == expected[key] for key in expected)
            and effect_target.get("repository") == repository
            and type(invocation_sequence) is int
            and invocation_sequence > expected["sequence"]
        ):
            matching.append(interaction)

    if len(matching) == 1 and len(same_identity) == 1:
        interaction = matching[0]
        result: dict[str, Any] = {
            "status": "matched-provider-invocation",
            "relation_basis": (
                "exact-preflight-event-reference-and-repository"
            ),
            "matching_invocation_count": 1,
            "causal_status": "not-established",
            "project_change_status": "not-measured-by-invocation-events",
        }
        for key in (
            "identity", "capability", "correlation_status", "outcome_status",
        ):
            value = interaction.get(key)
            if isinstance(value, str):
                result[key] = value
        for key in ("invocation_event", "completion_event"):
            reference = interaction.get(key)
            if isinstance(reference, dict):
                result[key] = {
                    field: reference[field]
                    for field in ("identity", "sequence", "type")
                    if field in reference
                }
        if type(interaction.get("returncode")) is int:
            result["returncode"] = interaction["returncode"]
        return result

    if same_identity:
        return {
            "status": "preflight-reference-conflict",
            "reference_conflict_count": len(same_identity),
            "causal_status": "not-established",
            "project_change_status": "not-measured-by-invocation-events",
        }

    search_complete = (
        not event_window_truncated and invocation_records_omitted == 0
    )
    return {
        "status": (
            "no-matching-environment-invocation-in-selected-window"
            if search_complete else "invocation-not-established-in-partial-window"
        ),
        "search_completeness_status": (
            "complete-for-selected-session-window" if search_complete
            else "partial-selected-session-window"
        ),
        "causal_status": "not-established",
        "project_change_status": "not-measured-by-invocation-events",
    }


def _context_response_binding_inclusion(
    binding: dict[str, Any], response_summaries: list[dict[str, Any]],
    context_supporting_events: list[dict[str, Any]], *,
    context_operation_count: int, complete_history: bool,
    response_summaries_omitted: int = 0,
) -> dict[str, Any]:
    """Relate a current binding to a captured context response without implying receipt."""
    event_window_status = "complete" if complete_history else "partial"
    if binding.get("availability_status") != "available":
        return {
            "status": "not-applicable-not-currently-available",
            "reason": "the context response list contains available bindings only",
            "scope": "authenticated-session.context-events-in-selected-window",
            "event_window_status": event_window_status,
            "response_summaries_omitted": response_summaries_omitted,
        }

    identity_fields = ("capability", "provider", "contract_revision")
    if any(
        not isinstance(binding.get(key), str) or not binding[key]
        for key in identity_fields
    ):
        return {
            "status": "not-comparable",
            "reason": "binding-identity-incomplete",
            "scope": "authenticated-session.context-events-in-selected-window",
            "event_window_status": event_window_status,
            "response_summaries_omitted": response_summaries_omitted,
        }

    binding_id = binding.get("binding_id")
    if isinstance(binding_id, str) and binding_id:
        exact_responses = [
            summary for summary in response_summaries
            if (isinstance(summary, dict)
                and summary.get("capture_status") == "captured"
                and summary.get("binding_ids_status") in {"complete", "partial"}
                and isinstance(summary.get("available_binding_ids"), list)
                and binding_id in summary["available_binding_ids"])
        ]
        if exact_responses:
            return {
                "status": "exact-binding-id-in-response",
                "basis": "response-recorded-binding-id",
                "identity_basis": "exact-capability-binding-id",
                "binding_id_attribution_status": "exact-from-response",
                "response_binding_ids_status": (
                    "complete" if all(
                        summary.get("binding_ids_status") == "complete"
                        for summary in exact_responses
                    ) else "partial"
                ),
                "comparison_time_basis": "captured-session-context-response",
                "scope": "authenticated-session.context-events-in-selected-window",
                "event_window_status": event_window_status,
                "response_summaries_omitted": response_summaries_omitted,
                "supporting_events": [
                    summary["event_reference"]
                    for summary in exact_responses[:MAX_CAPABILITY_SUPPORTING_EVENTS]
                    if isinstance(summary.get("event_reference"), dict)
                ],
            }

        complete_id_responses = [
            summary for summary in response_summaries
            if (isinstance(summary, dict)
                and summary.get("capture_status") == "captured"
                and summary.get("binding_ids_status") == "complete")
        ]
        if (context_operation_count > 0
                and response_summaries_omitted == 0
                and len(response_summaries) == context_operation_count
                and len(complete_id_responses) == context_operation_count):
            return {
                "status": "binding-id-absent-from-complete-selected-responses",
                "basis": "complete-response-binding-id-lists",
                "identity_basis": "exact-capability-binding-id",
                "binding_id_attribution_status": "exact-from-response",
                "comparison_time_basis": "captured-session-context-response",
                "scope": "authenticated-session.context-events-in-selected-window",
                "event_window_status": event_window_status,
                "response_summaries_omitted": response_summaries_omitted,
                "supporting_events": [
                    summary["event_reference"]
                    for summary in complete_id_responses[
                        :MAX_CAPABILITY_SUPPORTING_EVENTS
                    ]
                    if isinstance(summary.get("event_reference"), dict)
                ],
            }

    matching_responses = [
        summary for summary in response_summaries
        if (isinstance(summary, dict)
            and summary.get("capture_status") == "captured"
            and summary.get("identity_digest_status") == "complete"
            and summary.get("capabilities_truncated") is False
            and (summary.get("binding_snapshot_comparison") or {}).get("status")
            == "matches-current-session-binding-snapshot")
    ]
    if matching_responses:
        binding_id_list_statuses = {
            summary.get("binding_ids_status")
            for summary in matching_responses
            if summary.get("binding_ids_status")
            in {"complete", "partial", "unavailable"}
        }
        response_binding_ids_status = (
            next(iter(binding_id_list_statuses))
            if len(binding_id_list_statuses) == 1 else (
                "mixed" if binding_id_list_statuses else "unavailable"
            )
        )
        return {
            "status": "identity-tuple-in-matched-response",
            "basis": (
                "complete-response-identity-set-matches-current-session-snapshot"
            ),
            "identity_basis": "capability-provider-contract-revision",
            "binding_id_attribution_status": (
                "not-carried-by-historical-context-response-summary"
                if all(summary.get("schema_version")
                       == "mncs.environment.context-response-summary/1"
                       for summary in matching_responses)
                else "binding-id-capture-incomplete"
            ),
            "response_binding_ids_status": response_binding_ids_status,
            "comparison_time_basis": "current-session-binding-snapshot-at-projection-time",
            "scope": "authenticated-session.context-events-in-selected-window",
            "event_window_status": event_window_status,
            "response_summaries_omitted": response_summaries_omitted,
            "supporting_events": [
                summary["event_reference"]
                for summary in matching_responses[:MAX_CAPABILITY_SUPPORTING_EVENTS]
                if isinstance(summary.get("event_reference"), dict)
            ],
        }

    if context_operation_count == 0:
        status = (
            "not-observed-in-session-history" if complete_history
            else "not-observed-in-selected-window"
        )
        reason = status
    else:
        status = "not-established"
        comparisons = [
            (summary.get("binding_snapshot_comparison") or {}).get("status")
            for summary in response_summaries
            if isinstance(summary, dict)
        ]
        if response_summaries_omitted:
            reason = "context-response-summaries-omitted"
        elif "differs-from-current-session-binding-snapshot" in comparisons:
            reason = "captured-response-differs-from-current-snapshot"
        elif "not-comparable" in comparisons:
            reason = "current-session-binding-snapshot-not-comparable"
        else:
            reason = "complete-response-identity-digest-not-captured"

    references = [
        summary["event_reference"]
        for summary in response_summaries
        if (isinstance(summary, dict)
            and isinstance(summary.get("event_reference"), dict))
    ]
    if not references:
        references = context_supporting_events
    return {
        "status": status,
        "reason": reason,
        "scope": "authenticated-session.context-events-in-selected-window",
        "event_window_status": event_window_status,
        "response_summaries_omitted": response_summaries_omitted,
        "supporting_events": references[:MAX_CAPABILITY_SUPPORTING_EVENTS],
    }


def _context_response_use_sequence_relation(
    inclusion: dict[str, Any], exact_interactions: list[dict[str, Any]], *,
    complete_history: bool, response_summaries_omitted: int = 0,
    invocation_summaries_omitted: int = 0,
) -> dict[str, Any]:
    """Describe same-session sequence order without inferring a causal effect."""
    event_window_status = "complete" if complete_history else "partial"
    if inclusion.get("status") not in {
        "identity-tuple-in-matched-response",
        "exact-binding-id-in-response",
    }:
        return {
            "status": "not-comparable",
            "reason": "context-response-inclusion-not-established",
            "context_response_inclusion_status": inclusion.get("status"),
            "temporal_basis": "same-session-event-sequence",
            "causal_claim": "not-established",
            "event_window_status": event_window_status,
            "response_summaries_omitted": response_summaries_omitted,
            "invocation_summaries_omitted": invocation_summaries_omitted,
        }

    response_references = inclusion.get("supporting_events")
    response_references = (
        response_references if isinstance(response_references, list) else []
    )
    response_events = [
        reference for reference in response_references
        if (isinstance(reference, dict)
            and type(reference.get("sequence")) is int
            and reference["sequence"] >= 0)
    ]
    invocation_events = [
        interaction["invocation_event"]
        for interaction in exact_interactions
        if (isinstance(interaction, dict)
            and isinstance(interaction.get("invocation_event"), dict)
            and type(interaction["invocation_event"].get("sequence")) is int
            and interaction["invocation_event"]["sequence"] >= 0)
    ]
    response_events.sort(key=lambda item: item["sequence"])
    invocation_events.sort(key=lambda item: item["sequence"])
    if not response_events:
        return {
            "status": "not-comparable",
            "reason": "context-response-event-sequence-unavailable",
            "temporal_basis": "same-session-event-sequence",
            "causal_claim": "not-established",
            "event_window_status": event_window_status,
            "response_summaries_omitted": response_summaries_omitted,
            "invocation_summaries_omitted": invocation_summaries_omitted,
        }

    observed_pairs: list[dict[str, Any]] = []
    for invocation in invocation_events:
        prior_responses = [
            response for response in response_events
            if response["sequence"] < invocation["sequence"]
        ]
        if prior_responses:
            observed_pairs.append({
                "context_response_event": prior_responses[-1],
                "invocation_event": invocation,
                "relation": "context-response-sequence-precedes-invocation",
            })
    if observed_pairs:
        return {
            "status": "invocation-observed-after-context-response",
            "temporal_basis": "same-session-event-sequence",
            "causal_claim": "not-established",
            "event_window_status": event_window_status,
            "response_comparison_time_basis": (
                "current-session-binding-snapshot-at-projection-time"
            ),
            "response_summaries_omitted": response_summaries_omitted,
            "invocation_summaries_omitted": invocation_summaries_omitted,
            "observed_pairs": observed_pairs[:MAX_CAPABILITY_SUPPORTING_EVENTS],
        }

    if invocation_events:
        if response_summaries_omitted:
            return {
                "status": "not-comparable",
                "reason": "context-response-summaries-omitted",
                "temporal_basis": "same-session-event-sequence",
                "causal_claim": "not-established",
                "event_window_status": event_window_status,
                "response_summaries_omitted": response_summaries_omitted,
                "invocation_summaries_omitted": invocation_summaries_omitted,
            }
        following_responses = [
            response for response in response_events
            if any(invocation["sequence"] < response["sequence"]
                   for invocation in invocation_events)
        ]
        return {
            "status": "invocation-observed-before-context-response",
            "temporal_basis": "same-session-event-sequence",
            "causal_claim": "not-established",
            "event_window_status": event_window_status,
            "response_comparison_time_basis": (
                "current-session-binding-snapshot-at-projection-time"
            ),
            "response_summaries_omitted": response_summaries_omitted,
            "invocation_summaries_omitted": invocation_summaries_omitted,
            "observed_pairs": [
                {
                    "context_response_event": response,
                    "invocation_event": max(
                        (invocation for invocation in invocation_events
                         if invocation["sequence"] < response["sequence"]),
                        key=lambda item: item["sequence"],
                    ),
                    "relation": "invocation-sequence-precedes-context-response",
                }
                for response in following_responses[:MAX_CAPABILITY_SUPPORTING_EVENTS]
            ],
        }

    if invocation_summaries_omitted:
        return {
            "status": "not-observed-in-interaction-window",
            "reason": "some-invocation-summaries-were-omitted",
            "temporal_basis": "same-session-event-sequence",
            "causal_claim": "not-established",
            "event_window_status": event_window_status,
            "response_summaries_omitted": response_summaries_omitted,
            "invocation_summaries_omitted": invocation_summaries_omitted,
        }

    return {
        "status": (
            "no-exact-binding-invocation-record-in-session-history"
            if complete_history
            else "no-exact-binding-invocation-observed-in-selected-window"
        ),
        "reason": "this does not account for use through external interfaces",
        "temporal_basis": "same-session-event-sequence",
        "causal_claim": "not-established",
        "event_window_status": event_window_status,
        "response_summaries_omitted": response_summaries_omitted,
        "invocation_summaries_omitted": invocation_summaries_omitted,
        "response_events": response_events[:MAX_CAPABILITY_SUPPORTING_EVENTS],
    }


def _capability_binding_evidence_summary(
    bindings: list[dict[str, Any]], *, availability_counts: dict[str, int],
    complete_history: bool, binding_details_omitted_count: int,
    binding_records_not_examined: int, binding_records_invalid: int,
) -> dict[str, Any]:
    def status_counts(field: str, nested_field: str | None = None) -> dict[str, int]:
        counts: dict[str, int] = {}
        for binding in bindings:
            value = binding.get(field)
            if nested_field is not None:
                value = value.get(nested_field) if isinstance(value, dict) else None
            if isinstance(value, str) and value:
                counts[value] = counts.get(value, 0) + 1
        return dict(sorted(counts.items())[:8])

    exact_bindings = [
        binding for binding in bindings
        if binding.get("use_observation_status")
        == "invoked-for-exact-session-binding"
        and binding.get("binding_attribution_status")
        == "exact-session-binding-observed-in-selected-window"
        and binding.get("invocation_count_basis")
        == "exact-session-binding-identity"
    ]
    exact_outcomes: dict[str, int] = {}
    for binding in exact_bindings:
        outcomes = binding.get("outcome_status_counts")
        if not isinstance(outcomes, dict):
            continue
        for outcome, count in outcomes.items():
            if isinstance(outcome, str) and type(count) is int and count >= 0:
                exact_outcomes[outcome] = exact_outcomes.get(outcome, 0) + count

    def bounded_event_references(
        source_bindings: list[dict[str, Any]], field: str,
    ) -> tuple[list[dict[str, Any]], int]:
        references_by_identity: dict[
            tuple[str, int | None, str], dict[str, Any]
        ] = {}
        for binding in source_bindings:
            source = binding.get(field)
            if field == "context_response_inclusion":
                source = source if isinstance(source, dict) else {}
                source = source.get("supporting_events")
            references = source if isinstance(source, list) else []
            for reference in references:
                if not isinstance(reference, dict):
                    continue
                identity = reference.get("identity")
                event_type = reference.get("type")
                sequence = reference.get("sequence")
                sequence = (
                    sequence if type(sequence) is int and sequence >= 0
                    else None
                )
                if (not isinstance(identity, str) or not identity
                        or not isinstance(event_type, str) or not event_type):
                    continue
                key = (identity, sequence, event_type)
                compact_reference = {
                    "identity": _short_text(identity, 160),
                    "type": _short_text(event_type, 96),
                }
                if sequence is not None:
                    compact_reference["sequence"] = sequence
                references_by_identity[key] = compact_reference
        ordered = sorted(
            references_by_identity.values(),
            key=lambda reference: (
                reference.get("sequence", -1),
                reference["identity"], reference["type"],
            ),
            reverse=True,
        )
        return (
            ordered[:MAX_CAPABILITY_SUPPORTING_EVENTS],
            max(0, len(ordered) - MAX_CAPABILITY_SUPPORTING_EVENTS),
        )

    context_response_events, context_response_events_omitted = (
        bounded_event_references(bindings, "context_response_inclusion")
    )
    exact_invocation_events, exact_invocation_events_omitted = (
        bounded_event_references(exact_bindings, "supporting_events")
    )

    return {
        "schema_version": "mncs.environment.capability-binding-evidence-summary/2",
        "source_basis": (
            "current-session-binding-snapshot-and-selected-session-event-window"
        ),
        "binding_count": len(bindings),
        "availability_status_counts": dict(sorted(availability_counts.items())[:8]),
        "context_response_inclusion_status_counts": status_counts(
            "context_response_inclusion", "status",
        ),
        "context_response_binding_id_attribution_status_counts": status_counts(
            "context_response_inclusion", "binding_id_attribution_status",
        ),
        "context_response_binding_id_list_status_counts": status_counts(
            "context_response_inclusion", "response_binding_ids_status",
        ),
        "context_response_supporting_events": context_response_events,
        "context_response_supporting_events_omitted": (
            context_response_events_omitted
        ),
        "use_observation_status_counts": status_counts(
            "use_observation_status",
        ),
        "binding_attribution_status_counts": status_counts(
            "binding_attribution_status",
        ),
        "context_response_use_sequence_status_counts": status_counts(
            "context_response_use_sequence_relation", "status",
        ),
        "exact_binding_invoked_binding_count": sum(
            type(binding.get("invocation_count")) is int
            and binding["invocation_count"] > 0
            for binding in exact_bindings
        ),
        "exact_binding_invocation_count": sum(
            binding.get("invocation_count", 0)
            for binding in exact_bindings
            if type(binding.get("invocation_count")) is int
            and binding["invocation_count"] >= 0
        ),
        "exact_binding_completion_count": sum(
            binding.get("completion_count", 0)
            for binding in exact_bindings
            if type(binding.get("completion_count")) is int
            and binding["completion_count"] >= 0
        ),
        "exact_binding_outcome_status_counts": dict(
            sorted(exact_outcomes.items())[:8]
        ),
        "exact_binding_invocation_supporting_events": exact_invocation_events,
        "exact_binding_invocation_supporting_events_omitted": (
            exact_invocation_events_omitted
        ),
        "event_window_status": (
            "complete-session-history" if complete_history
            else "selected-event-window"
        ),
        "event_window_complete_history": complete_history,
        "discoverability_status": "not-measured",
        "agent_receipt_status": "not-established",
        "binding_details_truncated": binding_details_omitted_count > 0,
        "binding_details_omitted_count": max(0, binding_details_omitted_count),
        "binding_records_not_examined": max(0, binding_records_not_examined),
        "binding_records_invalid": max(0, binding_records_invalid),
        "interpretation_limit": (
            "Status counts summarize the current binding snapshot and selected "
            "Environment event history. Context inclusion records response "
            "evidence, not agent receipt or discovery; invocation counts do not "
            "include direct external tool activity."
        ),
    }


def _capability_delivery_view(
    snapshot: dict[str, Any], capability_summaries: list[dict[str, Any]],
    interactions: list[dict[str, Any]], events: list[dict[str, Any]],
    *, event_window_truncated: bool, event_page_scoped: bool,
    invocation_summaries_omitted: int,
) -> dict[str, Any]:
    """Relate this session's binding snapshot to bounded, observed capability use."""
    complete_history = not event_window_truncated and not event_page_scoped
    binding_snapshot_summary = _current_context_binding_snapshot_summary(snapshot)
    context_outcome_counts: dict[str, int] = {}
    context_supporting_events = []
    response_capture_status_counts: dict[str, int] = {}
    response_identity_digest_status_counts: dict[str, int] = {}
    response_summaries: list[dict[str, Any]] = []
    context_operation_count = 0
    context_response_summary_count = 0
    for event in events:
        payload = event.get("payload")
        if (not isinstance(payload, dict)
                or payload.get("operation") != "session.context"
                or not _authenticated_rpc_read_event(event, snapshot)):
            continue
        context_operation_count += 1
        reference = _event_reference(event)
        outcome = payload.get("outcome_status")
        if isinstance(outcome, str):
            context_outcome_counts[outcome] = (
                context_outcome_counts.get(outcome, 0) + 1
            )
        if reference is not None:
            context_supporting_events.append(reference)
        response_summary = payload.get("context_response_summary")
        if not is_valid_context_response_summary(response_summary):
            response_capture_status_counts["not-captured-in-rpc-event"] = (
                response_capture_status_counts.get(
                    "not-captured-in-rpc-event", 0,
                ) + 1
            )
            continue
        context_response_summary_count += 1
        capture_status = response_summary["capture_status"]
        response_capture_status_counts[capture_status] = (
            response_capture_status_counts.get(capture_status, 0) + 1
        )
        digest_status = response_summary["identity_digest_status"]
        response_identity_digest_status_counts[digest_status] = (
            response_identity_digest_status_counts.get(digest_status, 0) + 1
        )
        if reference is not None:
            comparison = {
                "status": "not-comparable",
                "basis": "current-session-binding-snapshot-at-projection-time",
                "reason": "response-identities-incomplete-or-truncated",
            }
            if binding_snapshot_summary.get("status") != "ready":
                comparison["reason"] = binding_snapshot_summary.get(
                    "reason", "current-binding-snapshot-unavailable",
                )
            elif (response_summary["capture_status"] == "captured"
                  and response_summary["identity_digest_status"] == "complete"
                  and response_summary["capabilities_truncated"] is False):
                count_matches = (
                    response_summary["available_count"]
                    == binding_snapshot_summary["available_count"]
                )
                unavailable_count_matches = (
                    response_summary["unavailable_count"]
                    == binding_snapshot_summary["unavailable_count"]
                )
                unknown_count_matches = (
                    response_summary["unknown_count"]
                    == binding_snapshot_summary["unknown_count"]
                )
                digest_matches = (
                    response_summary["available_identity_digest"]
                    == binding_snapshot_summary["available_identity_digest"]
                )
                comparison = {
                    "status": (
                        "matches-current-session-binding-snapshot"
                        if (count_matches and unavailable_count_matches
                            and unknown_count_matches and digest_matches)
                        else "differs-from-current-session-binding-snapshot"
                    ),
                    "basis": "current-session-binding-snapshot-at-projection-time",
                    "available_count_matches": count_matches,
                    "unavailable_count_matches": unavailable_count_matches,
                    "unknown_count_matches": unknown_count_matches,
                    "available_identity_digest_matches": digest_matches,
                }
            response_summaries.append({
                "event_reference": {
                    key: reference[key]
                    for key in ("identity", "sequence", "type")
                    if key in reference
                },
                "binding_snapshot_comparison": comparison,
                **{
                    key: response_summary[key]
                    for key in (
                        "schema_version", "capture_status", "capture_reason",
                        "context_schema_version", "available_count",
                        "listed_available_count", "unavailable_count",
                        "unknown_count", "capabilities_truncated",
                        "available_identity_digest", "identity_digest_status",
                        "available_binding_ids", "binding_ids_status",
                    )
                    if key in response_summary
                },
            })
            response_summaries.sort(
                key=lambda item: (
                    item.get("event_reference", {}).get("sequence")
                    if type(item.get("event_reference", {}).get("sequence")) is int
                    else 0
                ),
                reverse=True,
            )
            del response_summaries[MAX_CAPABILITY_SUPPORTING_EVENTS:]
    context_supporting_events = sorted(
        context_supporting_events,
        key=lambda item: (
            item.get("sequence") if type(item.get("sequence")) is int else 0
        ),
        reverse=True,
    )[:MAX_CAPABILITY_SUPPORTING_EVENTS]
    response_summaries.sort(
        key=lambda item: (
            item.get("event_reference", {}).get("sequence")
            if type(item.get("event_reference", {}).get("sequence")) is int
            else 0
        ),
        reverse=True,
    )
    if context_operation_count:
        context_activity_status = (
            "partial" if event_window_truncated or event_page_scoped
            else "observed"
        )
    elif complete_history:
        context_activity_status = "no-context-operation-in-session-history"
    else:
        context_activity_status = "not-observed-in-selected-event-window"
    captured_response_count = response_capture_status_counts.get("captured", 0)
    if captured_response_count and (
        captured_response_count == context_response_summary_count
        and context_response_summary_count == context_operation_count
    ):
        response_capability_list_status = "captured"
    elif captured_response_count:
        response_capability_list_status = "partially-captured"
    elif response_capture_status_counts.get("unavailable", 0):
        response_capability_list_status = "unavailable"
    elif response_capture_status_counts.get("partial", 0):
        response_capability_list_status = "partially-available"
    elif response_capture_status_counts.get("not-captured-in-rpc-event", 0):
        response_capability_list_status = "not-captured-in-rpc-event"
    else:
        response_capability_list_status = "not-observed"
    context_operation_activity = {
        "status": context_activity_status,
        "scope": "authenticated-Control-session.context-events-in-selected-window",
        "outcome_counts": dict(sorted(context_outcome_counts.items())),
        "supporting_events": [
            {
                key: reference[key]
                for key in ("identity", "sequence", "type")
                if key in reference
            }
            for reference in context_supporting_events
        ],
        "response_capability_list_status": response_capability_list_status,
        "response_capture_status_counts": dict(
            sorted(response_capture_status_counts.items()),
        ),
        "response_identity_digest_status_counts": dict(
            sorted(response_identity_digest_status_counts.items()),
        ),
        "response_summaries": response_summaries,
        "response_summaries_omitted": max(
            0, context_response_summary_count - len(response_summaries),
        ),
        "response_summary_scope": "authenticated-session.context-events-in-selected-window",
        "binding_snapshot_comparison_basis": (
            "current-session-binding-snapshot-at-projection-time"
        ),
        "agent_receipt_status": "not-established",
        "measurement_limits": [
            "Control records only a bounded capability-tuple digest and bounded binding identifiers; it does not persist capability names or the full response body.",
            "A successful session.context operation and captured response summary do not establish the consumer's receipt, discovery, or use of a capability.",
            "Binding comparison uses the current session snapshot; the event has no binding revision, so a difference does not establish a response failure.",
        ],
    }
    raw_bindings = snapshot.get("bindings")
    if not isinstance(raw_bindings, list):
        if not capability_summaries and not context_operation_count:
            return {}
        return {
            "status": "unavailable",
            "inventory_source": "current-session-binding-snapshot",
            "binding_count": 0,
            "bindings": [],
            "context_operation_activity": context_operation_activity,
            "discoverability_status": "not-measured",
            "measurement_limits": [
                "The session binding snapshot is unavailable.",
                "Environment bindings do not establish that a capability was presented to or discovered by the agent.",
            ],
        }

    summary_by_capability = {
        item["capability"]: item
        for item in capability_summaries
        if isinstance(item.get("capability"), str)
    }
    binding_records: list[dict[str, Any]] = []
    seen_bindings: set[tuple[str, str]] = set()
    invalid_binding_count = 0
    availability_counts: dict[str, int] = {}
    bindings_by_capability: dict[str, int] = {}
    examined_bindings = raw_bindings[:MAX_SESSION_ACTIVITY_BINDING_SCAN]
    for raw in examined_bindings:
        if not isinstance(raw, dict):
            invalid_binding_count += 1
            continue
        capability = raw.get("capability")
        provider = raw.get("provider")
        if (not isinstance(capability, str) or not capability or len(capability) > 256
                or not isinstance(provider, str) or not provider or len(provider) > 160):
            invalid_binding_count += 1
            continue
        binding_key = (provider, capability)
        if binding_key in seen_bindings:
            invalid_binding_count += 1
            continue
        seen_bindings.add(binding_key)
        availability = raw.get("availability")
        availability = availability if isinstance(availability, dict) else {}
        status = availability.get("status")
        if (not isinstance(status, str)
                or status not in {"available", "unavailable", "unknown"}):
            status = "unrecorded"
        availability_counts[status] = availability_counts.get(status, 0) + 1
        bindings_by_capability[capability] = bindings_by_capability.get(capability, 0) + 1
        binding_records.append({
            "provider": provider,
            "capability": capability,
            "availability_status": status,
            **({"binding_id": _short_text(raw["binding_id"], 160)}
               if isinstance(raw.get("binding_id"), str) and raw["binding_id"] else {}),
            **({"contract_revision": _short_text(
                raw["contract_revision"], 160,
            )} if isinstance(raw.get("contract_revision"), str)
                and raw["contract_revision"] else {}),
            **({"availability_verification": _short_text(
                availability["verification"], 64,
            )} if isinstance(availability.get("verification"), str) else {}),
            **({"availability_observed_at": _short_text(
                availability["observed_at"], 96,
            )} if isinstance(availability.get("observed_at"), str) else {}),
        })

    for binding in binding_records:
        binding["context_response_inclusion"] = (
            _context_response_binding_inclusion(
                binding, response_summaries, context_supporting_events,
                context_operation_count=context_operation_count,
                complete_history=complete_history,
                response_summaries_omitted=max(
                    0, context_response_summary_count - len(response_summaries),
                ),
            )
        )

    interaction_references: dict[str, list[dict[str, Any]]] = {}
    identified_interactions_by_capability: dict[str, list[dict[str, Any]]] = {}
    identified_interactions_by_binding: dict[
        tuple[str, str, str, str], list[dict[str, Any]]
    ] = {}
    for interaction in interactions:
        capability = interaction.get("capability")
        reference = interaction.get("invocation_event")
        if isinstance(capability, str) and isinstance(reference, dict):
            refs = interaction_references.setdefault(capability, [])
            if len(refs) < MAX_CAPABILITY_SUPPORTING_EVENTS:
                refs.append({
                    key: reference[key]
                    for key in ("identity", "sequence", "type")
                    if key in reference
                })
            binding_identity = interaction.get("binding_identity")
            if (isinstance(binding_identity, dict)
                    and binding_identity.get("status") == "complete"):
                identified_interactions_by_capability.setdefault(
                    capability, [],
                ).append(interaction)
                binding_key = (
                    binding_identity["provider"], capability,
                    binding_identity["binding_id"],
                    binding_identity["contract_revision"],
                )
                identified_interactions_by_binding.setdefault(
                    binding_key, [],
                ).append(interaction)

    for binding in binding_records:
        capability = binding["capability"]
        summary = summary_by_capability.get(capability)
        binding_identity_complete = all(
            isinstance(binding.get(key), str) and binding[key]
            for key in ("provider", "binding_id", "contract_revision")
        )
        if summary is None:
            use_status = (
                "no-environment-invocation-record-in-session-history"
                if complete_history
                else "no-environment-invocation-record-in-selected-window"
            )
            binding["use_observation_status"] = use_status
            binding["invocation_count"] = 0
            binding["completion_count"] = 0
            binding["outcome_status_counts"] = {}
            binding["supporting_events"] = []
            binding["invocation_count_basis"] = "no-invocation-record"
            binding["binding_attribution_status"] = (
                "no-exact-binding-use-in-session-history"
                if complete_history and binding_identity_complete else (
                    "not-observed-in-selected-window"
                    if binding_identity_complete else "binding-identity-incomplete"
                )
            )
            continue
        exact_for_capability = identified_interactions_by_capability.get(
            capability, [],
        )
        exact_for_binding = (
            identified_interactions_by_binding.get((
                binding["provider"], capability, binding["binding_id"],
                binding["contract_revision"],
            ), [])
            if binding_identity_complete else []
        )
        if exact_for_capability:
            if exact_for_binding:
                exact_outcomes: dict[str, int] = {}
                for item in exact_for_binding:
                    outcome = item.get("outcome_status")
                    if isinstance(outcome, str):
                        exact_outcomes[outcome] = exact_outcomes.get(outcome, 0) + 1
                binding["use_observation_status"] = (
                    "invoked-for-exact-session-binding"
                )
                binding["invocation_count"] = len(exact_for_binding)
                binding["completion_count"] = sum(
                    item.get("completion_event") is not None
                    for item in exact_for_binding
                )
                binding["outcome_status_counts"] = exact_outcomes
                binding["supporting_events"] = [
                    {
                        key: item["invocation_event"][key]
                        for key in ("identity", "sequence", "type")
                        if key in item["invocation_event"]
                    }
                    for item in exact_for_binding[:MAX_CAPABILITY_SUPPORTING_EVENTS]
                    if isinstance(item.get("invocation_event"), dict)
                ]
                binding["provider_attribution"] = (
                    "exact-provider-binding-id-and-contract-revision"
                )
                binding["invocation_count_basis"] = (
                    "exact-session-binding-identity"
                )
                binding["binding_attribution_status"] = (
                    "exact-session-binding-observed-in-selected-window"
                )
            else:
                binding["use_observation_status"] = (
                    "capability-invoked; no-exact-invocation-for-this-binding"
                )
                binding["invocation_count"] = 0
                binding["completion_count"] = 0
                binding["outcome_status_counts"] = {}
                binding["supporting_events"] = []
                binding["provider_attribution"] = (
                    "exact-capability-invocation-attributed-to-another-binding"
                )
                binding["invocation_count_basis"] = (
                    "exact-session-binding-identity"
                )
                binding["binding_attribution_status"] = (
                    "exact-invocations-do-not-match-current-binding"
                )
            continue
        binding["use_observation_status"] = "invoked-in-selected-event-window"
        binding["invocation_count"] = summary["invocation_count"]
        binding["completion_count"] = summary["completion_count"]
        binding["outcome_status_counts"] = dict(summary["outcome_status_counts"])
        binding["supporting_events"] = interaction_references.get(capability, [])
        binding["invocation_count_basis"] = "capability-name-only"
        binding["binding_attribution_status"] = (
            "invocation-identity-not-recorded-on-event"
        )
        binding["provider_attribution"] = (
            "ambiguous-current-bindings"
            if bindings_by_capability.get(capability, 0) > 1
            else "capability-name-match-only; provider-and-binding-identity-unavailable"
        )

    for binding in binding_records:
        identity_complete = all(
            isinstance(binding.get(key), str) and binding[key]
            for key in ("provider", "capability", "binding_id", "contract_revision")
        )
        exact_interactions = (
            identified_interactions_by_binding.get((
                binding["provider"], binding["capability"], binding["binding_id"],
                binding["contract_revision"],
            ), [])
            if identity_complete else []
        )
        binding["context_response_use_sequence_relation"] = (
            _context_response_use_sequence_relation(
                binding.get("context_response_inclusion", {}),
                exact_interactions,
                complete_history=complete_history,
                response_summaries_omitted=max(
                    0, context_response_summary_count - len(response_summaries),
                ),
                invocation_summaries_omitted=invocation_summaries_omitted,
            )
        )

    outside_inventory = [
        {
            "capability": item["capability"],
            "invocation_count": item["invocation_count"],
            "outcome_status_counts": dict(item["outcome_status_counts"]),
            "supporting_events": interaction_references.get(item["capability"], []),
            "interpretation": "observed-invocation-not-in-current-session-binding-snapshot",
        }
        for item in capability_summaries
        if isinstance(item.get("capability"), str)
        and item["capability"] not in bindings_by_capability
    ]
    transition_events: list[dict[str, Any]] = []
    transition_count = 0
    transition_types = {
        "capability.bound", "capability.changed", "capability.unbound",
        "capability.available", "capability.unavailable", "capability.unknown",
    }
    for event in reversed(events):
        if event.get("type") not in transition_types:
            continue
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        reference = _event_reference(event)
        capability = payload.get("capability")
        if reference is None or not isinstance(capability, str) or not capability:
            continue
        transition_count += 1
        if len(transition_events) >= MAX_SESSION_ACTIVITY_CAPABILITY_EVENTS:
            continue
        provider = payload.get("provider")
        item = {
            "capability": _short_text(capability, 160),
            "event_reference": reference,
        }
        if isinstance(provider, str) and provider:
            item["provider"] = _short_text(provider, 128)
        if isinstance(payload.get("previous"), str):
            item["previous_availability_status"] = _short_text(
                payload["previous"], 64,
            )
        transition_events.append(item)

    for binding in binding_records:
        matching_events = [
            item for item in transition_events
            if item.get("capability") == binding["capability"]
            and item.get("provider") in {None, binding["provider"]}
            and item["event_reference"].get("type") in {
                "capability.available", "capability.unavailable",
                "capability.unknown", "capability.bound", "capability.changed",
            }
        ]
        if matching_events:
            binding["availability_event_references"] = [
                item["event_reference"] for item in matching_events[:3]
            ]

    binding_records.sort(key=lambda item: (item["capability"], item["provider"]))
    binding_count = len(binding_records)
    omitted_bindings = max(0, binding_count - MAX_SESSION_ACTIVITY_BINDINGS)
    binding_records_not_examined = max(
        0, len(raw_bindings) - len(examined_bindings),
    )
    binding_evidence_summary = _capability_binding_evidence_summary(
        binding_records, availability_counts=availability_counts,
        complete_history=complete_history,
        binding_details_omitted_count=omitted_bindings,
        binding_records_not_examined=binding_records_not_examined,
        binding_records_invalid=invalid_binding_count,
    )
    outside_inventory.sort(key=lambda item: item["capability"])
    return {
        "status": (
            "partial"
            if invalid_binding_count or omitted_bindings or binding_records_not_examined
            else "ready"
        ),
        "inventory_source": "current-session-binding-snapshot",
        "binding_count": binding_count,
        "binding_records_examined": len(examined_bindings),
        "binding_records_not_examined": binding_records_not_examined,
        "binding_records_invalid": invalid_binding_count,
        "availability_counts": dict(sorted(availability_counts.items())),
        "bindings_omitted": omitted_bindings,
        "bindings": binding_records[:MAX_SESSION_ACTIVITY_BINDINGS],
        "binding_evidence_summary": binding_evidence_summary,
        "availability_transition_event_count": transition_count,
        "availability_transition_events_omitted": max(
            0, transition_count - len(transition_events),
        ),
        "availability_transition_events": transition_events,
        "capabilities_not_in_current_binding_snapshot": outside_inventory[:MAX_SESSION_ACTIVITY_CAPABILITIES],
        "capabilities_not_in_current_binding_snapshot_omitted": max(
            0, len(outside_inventory) - MAX_SESSION_ACTIVITY_CAPABILITIES,
        ),
        "context_operation_activity": context_operation_activity,
        "discoverability_status": "not-measured",
        "use_event_window": {
            "scope": "selected-session-event-window",
            "truncated": event_window_truncated,
            "complete_history": complete_history,
            "invocation_summaries_omitted": invocation_summaries_omitted,
        },
        "measurement_limits": [
            "Environment bindings do not establish that a capability was presented to or discovered by the agent.",
            "Availability records a substrate probe, not verified provider behavior.",
            "Invocation events do not record provider or binding identity; capability-name matches cannot establish a contract revision and are ambiguous when multiple current bindings expose the same name.",
            "No observed invocation is scoped to the selected event window unless complete session history is explicitly available.",
        ],
    }


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
    base_payload_keys = {
        "schema_version", "operation", "outcome_status", "exit_code",
        "failure_code", "request_digest", "caller", "context",
        "measurement_availability", "measurements", "request_operation_digest",
        "request_event_digest", "request_id",
    }
    payload_schema_version = (
        payload.get("schema_version") if isinstance(payload, dict) else None
    )
    if payload_schema_version == "mncs.environment.rpc-read-activity/1":
        payload_schema_valid = (
            isinstance(payload, dict)
            and base_payload_keys.issubset(payload)
        )
    elif payload_schema_version == "mncs.environment.rpc-read-activity/2":
        payload_schema_valid = (
            isinstance(payload, dict)
            and base_payload_keys.issubset(payload)
            and "context_response_summary" in payload
            and payload.get("operation") == "session.context"
            and is_valid_context_response_summary(
                payload.get("context_response_summary"),
            )
            and (
                (payload.get("outcome_status") == "failed"
                 and payload["context_response_summary"].get("capture_status")
                 == "unavailable"
                 and payload["context_response_summary"].get("capture_reason")
                 == "operation-failed")
                or (payload.get("outcome_status") == "succeeded"
                    and payload["context_response_summary"].get("capture_reason")
                    != "operation-failed")
            )
        )
    else:
        payload_schema_valid = False
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
            or not payload_schema_valid
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


def _authority_event_objective_context(payload: dict[str, Any]) -> dict[str, str]:
    """Project stable objective references carried by one authority event."""
    result: dict[str, str] = {}
    identifier = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,159}")
    campaign_id = payload.get("campaign_id")
    if isinstance(campaign_id, str) and identifier.fullmatch(campaign_id):
        result["campaign_id"] = campaign_id
    work_intent_id = payload.get("work_intent_id")
    if isinstance(work_intent_id, str) and identifier.fullmatch(work_intent_id):
        result["work_intent_id"] = work_intent_id
        result["work_intent_reference_status"] = "recorded-on-event"
    elif work_intent_id is None:
        result["work_intent_reference_status"] = "not-recorded-on-event"
    else:
        result["work_intent_reference_status"] = "invalid-event-reference"
    return result


def _authority_conflict_scope(
    value: Any, repository: str,
) -> dict[str, Any] | None:
    """Validate a bounded, path-sanitized authority or coordination scope."""
    if not isinstance(value, dict) or value.get("repository") != repository:
        return None
    kind = value.get("kind")
    if kind not in {"repository", "worktree", "paths"}:
        return None
    result: dict[str, Any] = {
        "kind": kind,
        "repository": repository,
        "exclusive": value.get("exclusive") is True,
    }
    branch = value.get("branch")
    if (isinstance(branch, str) and branch and len(branch) <= 160
            and "\x00" not in branch and not branch.startswith("/")):
        result["branch"] = branch
    identifier = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,159}")
    for key in (
        "checkout_identity", "git_common_directory_identity", "manifest_identity",
    ):
        item = value.get(key)
        if isinstance(item, str) and identifier.fullmatch(item):
            result[key] = item
    paths = value.get("paths")
    if isinstance(paths, list):
        safe_paths = [
            item for item in paths
            if isinstance(item, str) and item and len(item) <= 240
            and "\x00" not in item and "\\" not in item and not item.startswith("/")
            and PurePosixPath(item).as_posix() == item
            and not any(part in {"", ".", ".."} for part in item.split("/"))
        ]
        result["paths"] = safe_paths[:8]
        omitted = value.get("paths_omitted", 0)
        if type(omitted) is int and 0 < omitted <= 100_000:
            result["paths_omitted"] = omitted
        if len(safe_paths) > 8:
            result["paths_omitted"] = result.get("paths_omitted", 0) + len(safe_paths) - 8
    return result


def _authority_checkout_preflight_state(
    value: Any, *, schema_version: str,
) -> dict[str, Any] | None:
    """Validate a bounded checkout preimage recorded at an effect boundary."""
    expected = {
        "schema_version", "observation_basis", "checkout_identity", "branch",
        "branch_status", "head", "head_status", "working_tree_status",
    }
    if (not isinstance(value, dict) or set(value) != expected
            or value.get("schema_version") != schema_version
            or value.get("observation_basis")
            != "fresh-registered-checkout-inspection"):
        return None
    checkout_identity = value.get("checkout_identity")
    identifier = re.compile(r"[A-Za-z0-9:._-]{1,160}")
    if (not isinstance(checkout_identity, str)
            or not identifier.fullmatch(checkout_identity)):
        return None
    branch_status = value.get("branch_status")
    branch = value.get("branch")
    if branch_status == "attached":
        if (not isinstance(branch, str) or not branch or len(branch) > 160
                or "\x00" in branch or branch.startswith("/")):
            return None
    elif branch_status in {"detached", "unavailable"}:
        if branch is not None:
            return None
    else:
        return None
    head_status = value.get("head_status")
    head = value.get("head")
    if head_status == "measured":
        if (not isinstance(head, str)
                or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", head) is None):
            return None
    elif head_status == "unavailable":
        if head is not None:
            return None
    else:
        return None
    working_tree_status = value.get("working_tree_status")
    if working_tree_status not in {"clean", "dirty", "unavailable"}:
        return None
    return {
        "schema_version": schema_version,
        "observation_basis": "fresh-registered-checkout-inspection",
        "checkout_identity": checkout_identity,
        "branch": branch,
        "branch_status": branch_status,
        "head": head,
        "head_status": head_status,
        "working_tree_status": working_tree_status,
    }


def _authority_integration_preflight_state(value: Any) -> dict[str, Any] | None:
    """Validate the bounded integration destination checkout preimage."""
    return _authority_checkout_preflight_state(
        value,
        schema_version="mncs.environment.integration-preflight-state/1",
    )


def _authority_proposal_preflight_state(value: Any) -> dict[str, Any] | None:
    """Validate the bounded proposal checkout state observed at preflight."""
    return _authority_checkout_preflight_state(
        value,
        schema_version="mncs.environment.proposal-preflight-state/1",
    )


def _authority_repository_ref_observation(
    value: Any, *, repository: str, observed_path: str,
) -> dict[str, Any] | None:
    """Validate a bounded checkout observation without absolute machine paths."""
    allowed = {
        "repository", "observed_path", "checkout_identity",
        "git_common_directory_identity", "branch", "head", "clean",
        "dirty_entry_count", "observation",
    }
    if (not isinstance(value, dict) or set(value) - allowed
            or value.get("repository") != repository
            or value.get("observed_path") != observed_path):
        return None
    result: dict[str, Any] = {
        "repository": repository,
        "observed_path": observed_path,
    }
    identifier = re.compile(r"[A-Za-z0-9:._-]{1,160}")
    for key in ("checkout_identity", "git_common_directory_identity"):
        identity = value.get(key)
        if identity is not None:
            if not isinstance(identity, str) or not identifier.fullmatch(identity):
                return None
            result[key] = identity
    branch = value.get("branch")
    if branch is not None and (
        not isinstance(branch, str) or not branch or len(branch) > 160
        or "\x00" in branch or branch.startswith("/")
    ):
        return None
    result["branch"] = branch
    head = value.get("head")
    if head is not None and (
        not isinstance(head, str)
        or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", head) is None
    ):
        return None
    result["head"] = head
    clean = value.get("clean")
    if clean is not None and type(clean) is not bool:
        return None
    result["clean"] = clean
    dirty_count = value.get("dirty_entry_count")
    if dirty_count is not None and (
        type(dirty_count) is not int or not 0 <= dirty_count <= 30_000
    ):
        return None
    result["dirty_entry_count"] = dirty_count
    observation = value.get("observation")
    if observation is not None and observation not in {
        "current", "unavailable_current_namespace",
    }:
        return None
    result["observation"] = observation
    return result


def _authority_repository_ref_transition(
    value: Any, *, event_reference: dict[str, Any], index: int,
) -> dict[str, Any] | None:
    """Sanitize one observed repository delta from a campaign event."""
    if not isinstance(value, dict):
        return None
    repository = value.get("repository")
    observed_path = value.get("observed_path")
    if (value.get("schema_version")
            != "mncs.environment.repository-ref-transition/1"
            or not _is_repository_identity(repository)
            or not isinstance(observed_path, str) or not observed_path
            or len(observed_path) > 240 or "\\" in observed_path
            or "\x00" in observed_path or observed_path.startswith("/")
            or PurePosixPath(observed_path).as_posix() != observed_path
            or any(part in {"", ".", ".."} for part in observed_path.split("/"))
            or value.get("causal_status") != "not-established"):
        return None
    allowed_kinds = {
        "first-observed", "no-longer-observed", "head-changed",
        "branch-changed", "clean-state-changed",
        "dirty-entry-count-changed", "checkout-identity-changed",
        "git-common-directory-changed", "availability-changed",
        "observation-enriched",
    }
    kinds = value.get("change_kinds")
    if (not isinstance(kinds, list) or not kinds or len(kinds) > 10
            or any(kind not in allowed_kinds for kind in kinds)
            or len(set(kinds)) != len(kinds)):
        return None
    state_change_status = value.get("state_change_status")
    if state_change_status not in {
        "observed-delta", "initial-or-enriched-observation",
    }:
        return None
    before = None
    after = None
    if "before" in value:
        before = _authority_repository_ref_observation(
            value.get("before"), repository=repository,
            observed_path=observed_path,
        )
        if before is None:
            return None
    if "after" in value:
        after = _authority_repository_ref_observation(
            value.get("after"), repository=repository,
            observed_path=observed_path,
        )
        if after is None:
            return None
    if before is None and after is None:
        return None
    if state_change_status == "observed-delta" and (before is None or after is None):
        return None
    result: dict[str, Any] = {
        "identity": "reftxn_" + digest_hex({
            "event": event_reference["identity"],
            "index": index,
        }, length=16),
        "repository": repository,
        "observed_path": observed_path,
        "change_kinds": list(kinds),
        "state_change_status": state_change_status,
        "causal_status": "not-established",
        "event_reference": event_reference,
    }
    if before is not None:
        result["before"] = before
    if after is not None:
        result["after"] = after
    return result


def _repository_ref_preflight_relation(
    transition: dict[str, Any], *,
    integration_records: list[dict[str, Any]],
    proposal_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Relate a later ref observation only to an exact recorded preimage."""
    before = transition.get("before")
    transition_event = transition.get("event_reference")
    transition_sequence = (
        transition_event.get("sequence")
        if isinstance(transition_event, dict) else None
    )
    if (transition.get("state_change_status") != "observed-delta"
            or not isinstance(before, dict)
            or type(transition_sequence) is not int):
        return {
            "status": "not-applicable-without-observed-state-delta",
            "matching_preflight_count": 0,
            "matches": [],
        }
    candidates = [
        ("integration-eligibility", integration_records, "repository_preflight_state"),
        ("proposal-eligibility", proposal_records, "proposal_preflight_state"),
    ]
    matches: list[dict[str, Any]] = []
    for relation_kind, records, state_key in candidates:
        for record in records:
            event_reference = record.get("event_reference")
            preflight_sequence = (
                event_reference.get("sequence")
                if isinstance(event_reference, dict) else None
            )
            state = record.get(state_key)
            if (
                type(preflight_sequence) is not int
                or preflight_sequence >= transition_sequence
                or record.get("repository") != transition.get("repository")
                or record.get("campaign_id") != transition.get("campaign_id")
                or record.get("work_intent_id") != transition.get("work_intent_id")
                or record.get("preflight_state_status") != "recorded-and-validated"
                or not isinstance(state, dict)
                or not isinstance(before.get("checkout_identity"), str)
                or state.get("checkout_identity") != before.get("checkout_identity")
                or state.get("head_status") != "measured"
                or state.get("head") != before.get("head")
            ):
                continue
            matches.append({
                "kind": relation_kind,
                "action": record.get("action"),
                "match_basis": "exact-checkout-identity-and-head-preimage",
                "event_reference": event_reference,
            })
    matches.sort(key=lambda item: (
        item["event_reference"]["sequence"], item["kind"],
    ))
    return {
        "status": (
            "exact-preimage-match-after-preflight" if len(matches) == 1
            else "ambiguous-exact-preflight-match" if matches
            else "no-exact-preflight-match-in-selected-window"
        ),
        "matching_preflight_count": len(matches),
        "matches": matches[:4],
        "matches_truncated": len(matches) > 4,
    }


def _coordination_deferral_follow_through(
    deferral: dict[str, Any], *,
    integration_records: list[dict[str, Any]],
    proposal_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """Relate a deferral to later eligibility for the same recorded objective."""
    deferral_reference = deferral.get("event_reference")
    deferral_sequence = (
        deferral_reference.get("sequence")
        if isinstance(deferral_reference, dict) else None
    )
    campaign_id = deferral.get("campaign_id")
    work_intent_id = deferral.get("work_intent_id")
    if type(deferral_sequence) is not int:
        status = "unavailable-deferral-event-reference"
    elif not isinstance(campaign_id, str) or not isinstance(work_intent_id, str):
        status = "unavailable-exact-objective-reference"
    else:
        status = "no-later-match-in-returned-eligibility-records"

    matches: list[dict[str, Any]] = []
    if status == "no-later-match-in-returned-eligibility-records":
        for kind, records in (
            ("integration-eligibility", integration_records),
            ("proposal-eligibility", proposal_records),
        ):
            for record in records:
                reference = record.get("event_reference")
                sequence = (
                    reference.get("sequence")
                    if isinstance(reference, dict) else None
                )
                if (
                    type(sequence) is not int
                    or sequence <= deferral_sequence
                    or record.get("repository") != deferral.get("repository")
                    or record.get("action") != deferral.get("action")
                    or record.get("campaign_id") != campaign_id
                    or record.get("work_intent_id") != work_intent_id
                ):
                    continue
                matches.append({
                    "kind": kind,
                    "action": record["action"],
                    "persistent_claim_required": False,
                    "operation_executed": False,
                    "match_basis": (
                        "later-event-same-repository-action-campaign-and-work-intent"
                    ),
                    "event_reference": reference,
                })
        matches.sort(key=lambda item: (
            item["event_reference"]["sequence"], item["kind"],
        ))
        if matches:
            status = "later-same-objective-eligibility-observed"

    return {
        "status": status,
        "evidence_scope": "selected-session-window-and-returned-eligibility-records",
        "matching_preflight_count": len(matches),
        "matches": matches[:4],
        "matches_truncated": len(matches) > 4,
        "causal_status": "not-established",
        "interpretation": (
            "A later eligibility preflight with the same recorded campaign, "
            "work intent, repository, and action was observed in this session "
            "window. Eligibility does not establish that the operation ran, "
            "that the deferral was resolved, or that the deferral caused the "
            "later preflight. A missing match does not establish that no later "
            "preflight occurred outside the returned bounded records."
        ),
    }


def _authority_decision_claim_state(
    value: Any,
    *,
    claim_id: str | None,
    owner_session_id: str,
    repository: str,
) -> tuple[dict[str, Any] | None, str]:
    """Validate the claim observation captured at the authority boundary."""
    if value is None:
        return None, "not-recorded-on-event"
    if not isinstance(value, dict):
        return None, "invalid-event-observation"
    observed = claims_module._parse_time(value.get("observed_at"))
    claim_version = value.get("claim_version")
    blocking_status = value.get("blocking_status")
    recovery = value.get("recovery_verdict")
    freshness = value.get("freshness")
    owner_liveness = value.get("owner_liveness")
    scope_relation = value.get("scope_relation_status")
    if (
        value.get("lookup_status") != "found"
        or value.get("basis") != "active-claim-store-read-before-authority-check"
        or value.get("claim_id") != claim_id
        or value.get("owner_session_id") != owner_session_id
        or value.get("repository") != repository
        or observed is None
        or type(claim_version) is not int or claim_version < 1
        or value.get("record_status") != "held"
        or not isinstance(recovery, str)
        or recovery not in {
            "live", "recoverable", "not-recoverable", "expired", "unknown",
        }
        or (freshness is not None and not isinstance(freshness, str))
        or freshness not in {None, "active", "idle", "stale"}
        or scope_relation != "overlap-observed-at-authority-check"
        or not isinstance(owner_liveness, dict)
    ):
        return None, "invalid-event-observation"
    inspection_status = owner_liveness.get("inspection_status")
    owner_recovery = owner_liveness.get("recovery_eligibility")
    owner_freshness = owner_liveness.get("freshness")
    recommended = owner_liveness.get("recommended_operation")
    if (
        not isinstance(inspection_status, str)
        or inspection_status not in {
            "observed", "owner-unknown", "owner-state-unavailable",
            "inspection-limit-reached",
        }
        or (owner_freshness is not None and not isinstance(owner_freshness, str))
        or owner_freshness not in {None, "active", "idle", "stale"}
        or not isinstance(recommended, str)
        or owner_recovery != recovery
        or owner_freshness != freshness
        or recommended not in {
            "retry-exact-scoped-acquisition",
            "use-disjoint-scope-or-wait",
            "preserve-scope-and-inspect-owner",
            "inspect-owner-state-before-acquiring",
            "refresh-scope-and-acquire",
        }
    ):
        return None, "invalid-event-observation"
    expected_blocking_status = (
        "eligible-for-safe-recovery-on-acquisition"
        if recovery == "recoverable" else (
            "blocks-at-decision-time"
            if recovery in {"live", "not-recoverable"}
            else "not-established"
        )
    )
    if blocking_status != expected_blocking_status:
        return None, "invalid-event-observation"
    return {
        "observed_at": observed.astimezone(timezone.utc).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z"),
        "claim_version": claim_version,
        "blocking_status": blocking_status,
        "recovery_verdict": recovery,
        "freshness": freshness,
        "inspection_status": inspection_status,
        "recommended_operation": recommended,
    }, "recorded-and-validated"


def _authority_claim_decision_basis(
    value: Any,
) -> tuple[dict[str, Any] | None, str]:
    """Validate the authorization/coordination split recorded at decision time."""
    if value is None:
        return None, "not-recorded-on-event"
    expected = {
        "decision_domain", "authorization_basis", "authorization_status",
        "eligibility_status", "coordination_status",
        "persistent_authority_required",
    }
    if not isinstance(value, dict) or set(value) != expected:
        return None, "invalid-event-observation"
    if (
        value.get("decision_domain") != "coordination"
        or value.get("authorization_basis") != "session-writable-scope"
        or value.get("authorization_status") != "eligible"
        or value.get("eligibility_status") != "blocked-by-overlapping-scope"
        or value.get("coordination_status") != "scoped-mutation-lease-conflict"
        or value.get("persistent_authority_required") is not False
    ):
        return None, "invalid-event-observation"
    return {
        "decision_domain": "coordination",
        "authorization_basis": "session-writable-scope",
        "authorization_status": "eligible",
        "eligibility_status": "blocked-by-overlapping-scope",
        "coordination_status": "scoped-mutation-lease-conflict",
        "persistent_authority_required": False,
    }, "recorded-and-validated"


def _authority_claim_conflict(
    value: Any, repository: str | None,
) -> dict[str, Any] | None:
    """Validate structured authority evidence without exposing checkout paths."""
    if not isinstance(value, dict):
        return None
    if value.get("schema_version") != authority_module.CLAIM_CONFLICT_SCHEMA:
        return None
    conflict_repository = value.get("repository")
    if (not isinstance(conflict_repository, str) or not conflict_repository
            or len(conflict_repository) > 160
            or (repository is not None and conflict_repository != repository)):
        return None
    reasons = {
        "repository-wide scope overlaps every checkout",
        "repository-relative paths overlap",
        "same checkout with overlapping paths",
        "shared branch with overlapping paths across checkouts",
    }
    conflict_reason = value.get("conflict_reason")
    if conflict_reason not in reasons:
        return None
    requested_scope = _authority_conflict_scope(
        value.get("requested_scope"), conflict_repository,
    )
    blocker = value.get("blocking_claim")
    if not isinstance(blocker, dict):
        return None
    blocker_session = blocker.get("session_id")
    identifier = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,159}")
    if not isinstance(blocker_session, str) or not identifier.fullmatch(blocker_session):
        return None
    blocker_scope = _authority_conflict_scope(
        blocker.get("scope"), conflict_repository,
    )
    if requested_scope is None or blocker_scope is None:
        return None
    conflict: dict[str, Any] = {
        "repository": conflict_repository,
        "conflict_reason": conflict_reason,
        "requested_scope": requested_scope,
        "blocking_claim": {
            "session_id": blocker_session,
            "scope": blocker_scope,
        },
    }
    claim_id = blocker.get("claim_id")
    if isinstance(claim_id, str) and identifier.fullmatch(claim_id):
        conflict["blocking_claim"]["claim_id"] = claim_id
    decision_state, decision_status = _authority_decision_claim_state(
        value.get("current_claim_state"),
        claim_id=conflict["blocking_claim"].get("claim_id"),
        owner_session_id=blocker_session,
        repository=conflict_repository,
    )
    conflict["decision_claim_state_status"] = decision_status
    if decision_state is not None:
        conflict["decision_claim_state"] = decision_state
    decision_basis, decision_basis_status = _authority_claim_decision_basis(
        value.get("decision_basis"),
    )
    conflict["decision_basis_status"] = decision_basis_status
    if decision_basis is not None:
        conflict["decision_basis"] = decision_basis
    return conflict


def _session_activity_current_claim_states(
    session: Any,
    activity_records: list[dict[str, Any]],
    *,
    lookup_limit: int = MAX_SESSION_AUTHORITY_CLAIM_LOOKUPS,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Read current state for a small set of claims referenced by activity.

    Event evidence remains immutable. This derived view is a separate,
    on-demand observation of the canonical claim store and owner liveness at
    projection time. Callers share a bounded activity-wide lookup allowance.
    """
    lookup_limit = max(0, min(lookup_limit, MAX_SESSION_AUTHORITY_CLAIM_LOOKUPS))
    claim_ids: list[str] = []
    recorded_owners: dict[str, str] = {}
    for activity_record in activity_records:
        conflict = activity_record.get("claim_conflict")
        blocker = (
            conflict.get("blocking_claim") if isinstance(conflict, dict) else None
        )
        references = [blocker] if isinstance(blocker, dict) else []
        overlaps = activity_record.get("coordination_overlaps")
        if isinstance(overlaps, list):
            references.extend(item for item in overlaps if isinstance(item, dict))
        for reference in references:
            claim_id = reference.get("claim_id")
            if isinstance(claim_id, str) and claim_id not in claim_ids:
                claim_ids.append(claim_id)
            if (isinstance(claim_id, str)
                    and isinstance(reference.get("session_id"), str)):
                recorded_owners.setdefault(claim_id, reference["session_id"])
    if not claim_ids:
        return {}, {
            "status": "no-recorded-claim-identities",
            "lookup_count": 0,
            "lookup_limit": lookup_limit,
        }

    selected_ids = claim_ids[:lookup_limit]
    omitted_count = len(claim_ids) - len(selected_ids)
    if not selected_ids:
        return {
            claim_id: {
                "lookup_status": "not-inspected-limit",
                "blocking_status": "not-established",
            }
            for claim_id in claim_ids
        }, {
            "status": "partial-lookup-limit",
            "lookup_count": 0,
            "lookup_limit": lookup_limit,
            "lookup_omitted_count": omitted_count,
        }
    store = getattr(session, "store", None)
    reader = getattr(store, "read_latest_claims_for", None)
    if not callable(reader):
        states = {
            claim_id: {
                "lookup_status": "targeted-claim-read-unavailable",
                "blocking_status": "not-established",
            }
            for claim_id in selected_ids
        }
        summary = {
            "status": "targeted-claim-read-unavailable",
            "lookup_count": 0,
            "lookup_limit": lookup_limit,
        }
    else:
        try:
            records = reader(selected_ids)
        except (LifecycleError, StoreIntegrityFailure, StoreUnavailable, OSError, ValueError):
            records = None
        if not isinstance(records, list):
            states = {
                claim_id: {
                    "lookup_status": "current-claim-read-failed",
                    "blocking_status": "not-established",
                }
                for claim_id in selected_ids
            }
            summary = {
                "status": "current-claim-read-failed",
                "lookup_count": len(selected_ids),
                "lookup_limit": lookup_limit,
            }
        else:
            observed_at = claims_module.utcnow()
            states = {}
            owner_states: dict[str, dict[str, Any]] = {}
            for claim_id in selected_ids:
                try:
                    record = claims_module.latest_record_for(records, claim_id)
                except (TypeError, ValueError, KeyError):
                    record = None
                if record is None:
                    states[claim_id] = {
                        "lookup_status": "claim-not-found-or-invalid-in-current-store",
                        "basis": "current-authoritative-claim-store",
                        "observed_at": observed_at,
                        "blocking_status": "not-established",
                    }
                    continue
                owner = None
                try:
                    if record.get("status") == "held":
                        owner_session = record.get("session_id")
                        if isinstance(owner_session, str) and owner_session:
                            owner = owner_states.get(owner_session)
                            if owner is None:
                                owner = claims_module.owner_state(store, owner_session)
                                owner_states[owner_session] = owner
                        else:
                            owner = None
                        classification = claims_module.classify(record, owner)
                    else:
                        classification = claims_module.classify(record, None)
                except (LifecycleError, StoreIntegrityFailure, StoreUnavailable,
                        OSError, TypeError, ValueError):
                    classification = None
                if not isinstance(classification, dict):
                    states[claim_id] = {
                        "lookup_status": "claim-classification-failed",
                        "basis": "current-authoritative-claim-store",
                        "observed_at": observed_at,
                        "blocking_status": "not-established",
                    }
                    continue
                verdict = classification.get("verdict")
                record_status = record.get("status")
                if verdict in {"live", "not-recoverable"}:
                    blocking_status = "blocks-at-projection-time"
                elif verdict == "recoverable":
                    blocking_status = "eligible-for-safe-recovery-on-acquisition"
                elif verdict in {"released", "expired", "superseded"}:
                    blocking_status = "does-not-block-at-projection-time"
                else:
                    blocking_status = "not-established"
                state: dict[str, Any] = {
                    "lookup_status": "found",
                    "basis": "current-authoritative-claim-store",
                    "observed_at": observed_at,
                    "claim_version": (
                        record["version"] if type(record.get("version")) is int
                        else None
                    ),
                    "record_status": (
                        record_status if isinstance(record_status, str)
                        else "status-unavailable"
                    ),
                    "blocking_status": blocking_status,
                }
                if isinstance(owner, dict):
                    owner_lifecycle = owner.get("lifecycle")
                    if owner_lifecycle in claims_module.TERMINAL_LIFECYCLES:
                        owner_evidence_status = "terminal-lifecycle-observed"
                    elif owner.get("error"):
                        owner_evidence_status = "incomplete"
                    elif not owner.get("known"):
                        owner_evidence_status = "owner-session-unavailable"
                    elif isinstance(owner.get("last_activity_at"), str):
                        owner_evidence_status = "activity-recorded"
                    else:
                        owner_evidence_status = "no-owner-activity-recorded"
                    state["owner_liveness_evidence_status"] = owner_evidence_status
                current_owner = record.get("session_id")
                if isinstance(current_owner, str) and re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9:._-]{0,159}", current_owner,
                ):
                    state["current_owner_session_id"] = current_owner
                    recorded_owner = recorded_owners.get(claim_id)
                    if isinstance(recorded_owner, str):
                        state["recorded_owner_relation"] = (
                            "same" if recorded_owner == current_owner else "changed"
                        )
                if verdict in {"live", "recoverable", "not-recoverable"}:
                    state["recovery_verdict"] = verdict
                freshness = classification.get("freshness")
                if isinstance(freshness, str):
                    state["freshness"] = freshness
                if type(classification.get("owner_known")) is bool:
                    state["owner_known"] = classification["owner_known"]
                for key in ("owner_lifecycle", "last_activity_at"):
                    value = classification.get(key)
                    if isinstance(value, str) and len(value) <= 96:
                        state[key] = value
                lease = classification.get("lease")
                if isinstance(lease, dict):
                    expiry = lease.get("effective_expires_at")
                    if isinstance(expiry, str) and len(expiry) <= 96:
                        state["effective_expires_at"] = expiry
                states[claim_id] = state
            summary = {
                "status": "partial-lookup-limit" if omitted_count else "read",
                "lookup_count": len(selected_ids),
                "lookup_limit": lookup_limit,
                "observed_at": observed_at,
            }

    for claim_id in claim_ids[lookup_limit:]:
        states[claim_id] = {
            "lookup_status": "not-inspected-limit",
            "blocking_status": "not-established",
        }
    if omitted_count:
        summary["lookup_omitted_count"] = omitted_count
    return states, summary


def _authority_claim_conflict_guidance(
    action: str, conflict: dict[str, Any], current_state: dict[str, Any],
) -> dict[str, Any]:
    """Suggest the least administrative next step for a recorded overlap.

    This is projection-time guidance only. It neither acquires nor releases a
    claim, and any later mutation must repeat the Environment's current
    authorization and Store-generation checks.
    """
    blocking_status = current_state.get("blocking_status")
    guidance: dict[str, Any] = {
        "basis": "current-claim-state-at-projection-time",
        "claim_mutated": False,
    }
    if action in {"merge", "publish"}:
        guidance.update({
            "status": "integration-preflight-can-be-retried",
            "next_step": "repeat-exact-scope-integration-preflight",
            "ownership_transfer": "not-required-by-current-integration-policy",
            "limit": "preflight checks eligibility; it does not integrate the change",
        })
        return guidance
    if blocking_status == "eligible-for-safe-recovery-on-acquisition":
        guidance.update({
            "status": "stale-claim-recoverable-on-acquisition",
            "next_step": "retry-exact-scope-acquisition",
            "ownership_transfer": "not-required",
            "limit": "acquisition rechecks current liveness and atomically publishes the claim",
        })
        return guidance
    if blocking_status == "does-not-block-at-projection-time":
        guidance.update({
            "status": "claim-no-longer-blocks",
            "next_step": "refresh-operation-scope-and-retry",
            "ownership_transfer": "not-required",
            "limit": "the claim may change again after this projection read",
        })
        return guidance
    if blocking_status == "blocks-at-projection-time":
        conflict_reason = conflict.get("conflict_reason")
        if (
            conflict_reason
            == "shared branch with overlapping paths across checkouts"
            and action in {"write", "mutate", "verify"}
        ):
            guidance.update({
                "status": "shared-ref-overlap-is-proposal-coordination",
                "next_step": "refresh-proposal-preflight-on-distinct-registered-worktree",
                "ownership_transfer": "not-required-for-local-proposal-work",
                "limit": (
                    "the recorded denial reflects its event-time policy; "
                    "current local proposal checks treat verified distinct "
                    "worktrees as separate file state, while ref-changing "
                    "operations retain their concurrency check"
                ),
            })
            return guidance
        if conflict_reason in {
            "same checkout with overlapping paths",
            "shared branch with overlapping paths across checkouts",
        }:
            next_step = "use-an-independent-registered-worktree-on-another-branch-or-wait"
        elif conflict_reason == "repository-relative paths overlap":
            next_step = "choose-a-nonoverlapping-scope-or-wait"
        else:
            next_step = "inspect-the-current-scope-and-wait-for-safe-release"
        guidance.update({
            "status": "active-shared-scope-conflict",
            "next_step": next_step,
            "ownership_transfer": "not-prescribed",
            "limit": "the projection does not authorize mutation or prove an in-flight operation stopped",
        })
        return guidance
    guidance.update({
        "status": "claim-state-not-established",
        "next_step": "refresh-claim-state-before-shared-mutation",
        "ownership_transfer": "not-prescribed",
        "limit": "incomplete claim or owner evidence remains fail-closed",
    })
    return guidance


def _claim_recovery_event_projection(
    event: dict[str, Any], session_id: str,
) -> dict[str, Any] | None:
    """Sanitize one automatic claim-recovery event for the activity view."""
    reference = _event_reference(event)
    payload = event.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    claim_id = payload.get("claim_identity")
    repository = payload.get("repository")
    claim_version = payload.get("claim_version")
    recovered = payload.get("recovered_from")
    omitted = payload.get("recovered_from_omitted_count", 0)
    basis = payload.get("basis")
    identifier = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,159}")
    if (
        reference is None
        or not isinstance(claim_id, str) or not identifier.fullmatch(claim_id)
        or not isinstance(repository, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", repository)
        or type(claim_version) is not int or claim_version < 1
        or not isinstance(basis, str)
        or basis not in {
            claims_module.BASIS_EXPLICIT,
            claims_module.BASIS_INTENT_SCOPE,
            claims_module.BASIS_RECOVERY,
            claims_module.BASIS_ADOPTION,
            claims_module.BASIS_TRANSFER,
        }
        or not isinstance(recovered, list) or not 1 <= len(recovered) <= 8
        or type(omitted) is not int or omitted < 0
    ):
        return None
    scope = _authority_conflict_scope(payload.get("scope"), repository)
    if scope is None:
        return None
    recovered_from = []
    for item in recovered:
        if not isinstance(item, dict):
            return None
        source_id = item.get("claim_id")
        owner = item.get("owner_session")
        source_version = item.get("version")
        superseded_version = item.get("superseded_version")
        if (
            not isinstance(source_id, str) or not identifier.fullmatch(source_id)
            or not isinstance(owner, str) or not identifier.fullmatch(owner)
            or type(source_version) is not int or source_version < 1
            or type(superseded_version) is not int
            or superseded_version != source_version + 1
        ):
            return None
        recovered_from.append({
            "claim_id": source_id,
            "version": source_version,
            "superseded_version": superseded_version,
            "owner_session": owner,
        })
    recovered_count = len(recovered_from) + omitted
    row = {
        "identity": "clmrec_" + digest_hex({
            "session_id": session_id,
            "event": reference["identity"],
        }, length=16),
        "status": "claim-transition-recorded",
        "repository": repository,
        "basis": basis,
        "scope": scope,
        "claim_id": claim_id,
        "claim_version": claim_version,
        "recovered_count": recovered_count,
        "recovered_from": recovered_from,
        "recovered_from_omitted_count": omitted,
        "event_reference": reference,
        "interpretation": (
            "The session event records a new claim published after its "
            "predecessors were classified as recoverable in the same claim "
            "transition. It does not establish downstream work success."
        ),
        **_authority_event_objective_context(payload),
    }
    campaign_id = payload.get("campaign_id")
    if (isinstance(campaign_id, str)
            and identifier.fullmatch(campaign_id)):
        row["campaign_reference_status"] = "recorded-on-event"
    else:
        row["campaign_reference_status"] = (
            "invalid-event-reference" if campaign_id is not None
            else "not-recorded-on-event"
        )
    return row


def _operation_claim_recovery_event_projection(
    event: dict[str, Any], session_id: str,
) -> dict[str, Any] | None:
    """Project stale claims superseded for one exact provider operation."""
    reference = _event_reference(event)
    payload = event.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    repository = payload.get("repository")
    action = payload.get("action")
    scope = (
        _authority_conflict_scope(payload.get("scope"), repository)
        if _is_repository_identity(repository)
        and isinstance(payload.get("scope"), dict) else None
    )
    decision = _event_reference(
        payload.get("authority_decision_event")
        or payload.get("authority_denial_event")
    )
    recovered = payload.get("recovered_from")
    recovered_count = payload.get("recovered_count")
    identifier = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,159}")
    if (
        reference is None
        or not _is_repository_identity(repository)
        or action not in {"write", "mutate", "execute", "verify"}
        or scope is None
        or scope.get("kind") != "worktree"
        or scope.get("exclusive") is not False
        or decision is None
        or decision.get("type") not in {
            "authority.denied", "authority.coordination-deferred",
        }
        or decision.get("sequence", reference["sequence"])
        >= reference["sequence"]
        or type(recovered_count) is not int
        or not 1 <= recovered_count <= 16
        or not isinstance(recovered, list)
        or len(recovered) != recovered_count
        or payload.get("operation_effect_started") is not False
    ):
        return None
    recovered_from = []
    for item in recovered:
        if not isinstance(item, dict):
            return None
        claim_id = item.get("claim_id")
        owner = item.get("owner_session")
        version = item.get("version")
        superseded = item.get("superseded_version")
        if (
            not isinstance(claim_id, str) or not identifier.fullmatch(claim_id)
            or not isinstance(owner, str) or not identifier.fullmatch(owner)
            or type(version) is not int or version < 1
            or type(superseded) is not int or superseded != version + 1
        ):
            return None
        recovered_from.append({
            "claim_id": claim_id,
            "version": version,
            "superseded_version": superseded,
            "owner_session": owner,
        })
    row = {
        "identity": "opclmrec_" + digest_hex({
            "session_id": session_id,
            "event": reference["identity"],
        }, length=16),
        "status": "stale-claims-superseded-for-one-operation",
        "action": action,
        "repository": repository,
        "scope": scope,
        "recovered_count": recovered_count,
        "recovered_from": recovered_from,
        "authority_decision_event": decision,
        "event_reference": reference,
        "interpretation": (
            "The Environment superseded only liveness-classified recoverable "
            "claims before the exact operation's fresh eligibility check. No "
            "replacement persistent claim was created, and this record alone "
            "does not establish that the operation later succeeded."
        ),
        **_authority_event_objective_context(payload),
    }
    # Preserve the former projection field for historical denial-linked rows.
    if decision.get("type") == "authority.denied":
        row["authority_denial_event"] = decision
    return row


def _claim_coordination_event_projection(
    event: dict[str, Any], session_id: str,
) -> dict[str, Any] | None:
    """Sanitize one recorded claim lifecycle transition for session activity."""
    reference = _event_reference(event)
    event_type = event.get("type")
    action = (
        CLAIM_COORDINATION_EVENT_ACTIONS.get(event_type)
        if isinstance(event_type, str) else None
    )
    payload = event.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    if reference is None or action is None:
        return None

    identifier = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,159}")
    repository = payload.get("repository")
    repository_valid = _is_repository_identity(repository)
    raw_scope = payload.get("scope")
    scope = (
        _authority_conflict_scope(raw_scope, repository)
        if repository_valid and isinstance(raw_scope, dict) else None
    )
    if isinstance(raw_scope, dict) and scope is None:
        return None

    claim_id = payload.get("claim_identity", payload.get("claim_id"))
    if claim_id is not None and (
        not isinstance(claim_id, str) or not identifier.fullmatch(claim_id)
    ):
        return None
    successor_claim_id = payload.get("target_claim_id")
    if successor_claim_id is not None and (
        not isinstance(successor_claim_id, str)
        or not identifier.fullmatch(successor_claim_id)
    ):
        return None

    claim_version = (
        payload.get("superseded_version")
        if action == "recovered-away"
        else payload.get("claim_version", payload.get("new_claim_version"))
    )
    if claim_version is not None and (
        type(claim_version) is not int or claim_version < 1
    ):
        return None
    basis = payload.get("basis")
    if basis is not None and (not isinstance(basis, str) or basis not in {
        claims_module.BASIS_EXPLICIT,
        claims_module.BASIS_INTENT_SCOPE,
        claims_module.BASIS_RECOVERY,
        claims_module.BASIS_ADOPTION,
        claims_module.BASIS_TRANSFER,
    }):
        return None

    released = payload.get("released")
    released_count = None
    if type(released) is int and 0 <= released <= 100_000:
        released_count = released
    elif isinstance(released, list) and len(released) <= 100_000:
        if any(not isinstance(item, str) or not identifier.fullmatch(item)
               for item in released):
            return None
        released_count = len(released)
    elif released is not None:
        return None

    row: dict[str, Any] = {
        "identity": "clmcoord_" + digest_hex({
            "session_id": session_id,
            "event": reference["identity"],
        }, length=16),
        "action": action,
        "status": {
            "acquired": "claim-acquired",
            "owner-recovered": "claim-acquired-by-authorized-recovery",
            "released": "claim-release-recorded",
            "release-noop": "claim-release-no-op",
            "transferred": "claim-transfer-recorded",
            "recovered-away": "claim-recovery-fenced-prior-owner",
            "owner-recovery-reconciled": "owner-recovery-reconciled",
        }[action],
        "repository_status": (
            "recorded-on-event" if repository_valid else (
                "invalid-event-value" if repository is not None
                else "not-recorded-on-event"
            )
        ),
        "scope_status": (
            "validated-from-event" if scope is not None else (
                "invalid-event-value" if raw_scope is not None
                else "not-recorded-on-event"
            )
        ),
        "event_reference": reference,
        "interpretation": (
            "This records a claim coordination transition at event time; it does "
            "not establish the claim's current state or whether the associated "
            "development work succeeded."
        ),
        **_authority_event_objective_context(payload),
    }
    campaign_id = payload.get("campaign_id")
    if (isinstance(campaign_id, str)
            and identifier.fullmatch(campaign_id)):
        row["campaign_reference_status"] = "recorded-on-event"
    else:
        row["campaign_reference_status"] = (
            "invalid-event-reference" if campaign_id is not None
            else "not-recorded-on-event"
        )
    if repository_valid:
        row["repository"] = repository
    if scope is not None:
        row["scope"] = scope
    if claim_id is not None:
        row["claim_id"] = claim_id
    if claim_version is not None:
        row["claim_version"] = claim_version
    if basis is not None:
        row["basis"] = basis
    if released_count is not None:
        row["released_count"] = released_count
    if successor_claim_id is not None:
        row["successor_claim_id"] = successor_claim_id
    for source_key, output_key in (
        ("owner_session_id", "owner_session_id"),
        ("new_session_id", "new_session_id"),
        ("to_session", "to_session_id"),
    ):
        value = payload.get(source_key)
        if value is not None:
            if not isinstance(value, str) or not identifier.fullmatch(value):
                return None
            row[output_key] = value
    return row


def _claim_event_identity_matches(
    event: dict[str, Any], *, repository: str, claim_id: str,
) -> bool:
    """Match an event to a claim only through an exact recorded identity."""
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return False
    event_repository = payload.get("repository")
    if event_repository is not None and event_repository != repository:
        return False
    identities = {
        payload.get(key)
        for key in ("claim_identity", "claim_id", "target_claim_id")
        if isinstance(payload.get(key), str)
    }
    released = payload.get("released")
    if isinstance(released, list):
        identities.update(item for item in released if isinstance(item, str))
    recovered_from = payload.get("recovered_from")
    if isinstance(recovered_from, list):
        identities.update(
            item.get("claim_id") for item in recovered_from
            if isinstance(item, dict) and isinstance(item.get("claim_id"), str)
        )
    return claim_id in identities


def _claim_owner_event_page(
    store: Any, owner_session_id: str,
) -> dict[str, Any] | None:
    """Read a bounded owner event tail; never fall back to an unbounded log."""
    snapshot_reader = getattr(store, "load_snapshot", None)
    owner_state_status = "unavailable"
    if callable(snapshot_reader):
        try:
            owner_state_status = (
                "present" if isinstance(snapshot_reader(owner_session_id), dict)
                else "not-found"
            )
        except (LifecycleError, StoreIntegrityFailure, StoreUnavailable,
                OSError, TypeError, ValueError):
            owner_state_status = "unavailable"
    page_reader = getattr(store, "read_event_page", None)
    tail_reader = getattr(store, "read_event_tail", None)
    try:
        if callable(page_reader):
            page = page_reader(
                owner_session_id, limit=MAX_SESSION_CLAIM_OWNER_EVENT_PAGE,
            )
        elif callable(tail_reader):
            events, event_count = tail_reader(
                owner_session_id, limit=MAX_SESSION_CLAIM_OWNER_EVENT_PAGE,
            )
            page = {
                "events": events,
                "event_count": event_count,
                "truncated": (
                    type(event_count) is int and isinstance(events, list)
                    and event_count > len(events)
                ),
            }
        else:
            return None
    except (LifecycleError, StoreIntegrityFailure, StoreUnavailable,
            OSError, TypeError, ValueError):
        return None
    if (
        not isinstance(page, dict)
        or not isinstance(page.get("events"), list)
        or type(page.get("event_count")) is not int
        or page["event_count"] < 0
        or type(page.get("truncated")) is not bool
        or page["event_count"] < len(page["events"])
    ):
        return None
    events = [item for item in page["events"] if isinstance(item, dict)]
    overflow = len(events) > MAX_SESSION_CLAIM_OWNER_EVENT_PAGE
    if overflow:
        events = events[-MAX_SESSION_CLAIM_OWNER_EVENT_PAGE:]
    return {
        "events": events,
        "event_count": page["event_count"],
        "truncated": (
            page["truncated"] or overflow
            or page["event_count"] > len(events)
        ),
        "event_records_considered": len(events),
        "owner_session_state_status": owner_state_status,
    }


def _session_claim_lifecycle_correlations(
    session: Any, *, selected_session_id: str,
    selected_events: list[dict[str, Any]], selected_event_count: int,
    selected_event_window_truncated: bool,
    denial_records: list[dict[str, Any]],
    integration_eligibility_records: list[dict[str, Any]],
    proposal_eligibility_records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Correlate claim history with exact denial and proposal/preflight references."""
    relations: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    identifier = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,159}")

    def add_relation(
        repository: Any, owner_session_id: Any, claim_id: Any,
        activity_kind: str, activity_record: dict[str, Any],
    ) -> None:
        if (
            not _is_repository_identity(repository)
            or not isinstance(owner_session_id, str)
            or not identifier.fullmatch(owner_session_id)
            or not isinstance(claim_id, str)
            or not identifier.fullmatch(claim_id)
        ):
            return
        key = (repository, owner_session_id, claim_id)
        references = relations.setdefault(key, [])
        event_reference = activity_record.get("event_reference")
        event_identity = (
            event_reference.get("identity")
            if isinstance(event_reference, dict) else None
        )
        if not isinstance(event_identity, str):
            return
        if any(
            item.get("event_reference", {}).get("identity") == event_identity
            for item in references
        ):
            return
        reference: dict[str, Any] = {
            "kind": activity_kind,
            "event_reference": event_reference,
        }
        for key_name in (
            "action", "campaign_id", "work_intent_id",
            "work_intent_reference_status",
        ):
            value = activity_record.get(key_name)
            if isinstance(value, str):
                reference[key_name] = value
        references.append(reference)

    for denial in denial_records:
        conflict = denial.get("claim_conflict")
        blocker = (
            conflict.get("blocking_claim")
            if isinstance(conflict, dict) else None
        )
        if isinstance(blocker, dict):
            add_relation(
                conflict.get("repository"), blocker.get("session_id"),
                blocker.get("claim_id"), "authority-denial", denial,
            )
    for eligibility in integration_eligibility_records:
        for overlap in eligibility.get("coordination_overlaps", []):
            if isinstance(overlap, dict):
                add_relation(
                    eligibility.get("repository"), overlap.get("session_id"),
                    overlap.get("claim_id"), "integration-eligibility",
                    eligibility,
                )
    for eligibility in proposal_eligibility_records:
        for claim in eligibility.get("coordination_only_claims", []):
            if isinstance(claim, dict):
                add_relation(
                    eligibility.get("repository"), claim.get("session_id"),
                    claim.get("claim_id"), "proposal-eligibility", eligibility,
                )

    ordered_keys = list(relations)
    selected_keys = ordered_keys[
        :MAX_SESSION_ACTIVITY_CLAIM_LIFECYCLE_CORRELATIONS
    ]
    owner_sessions = list(dict.fromkeys(key[1] for key in selected_keys))
    foreign_owner_sessions = [
        owner_session_id for owner_session_id in owner_sessions
        if owner_session_id != selected_session_id
    ]
    selected_foreign_owner_sessions = foreign_owner_sessions[
        :MAX_SESSION_CLAIM_OWNER_EVENT_SESSIONS
    ]
    store = getattr(session, "store", None)
    pages: dict[str, dict[str, Any] | None] = {}
    if selected_session_id in owner_sessions:
        local_events = selected_events[-MAX_SESSION_CLAIM_OWNER_EVENT_PAGE:]
        pages[selected_session_id] = {
            "events": local_events,
            "event_count": selected_event_count,
            "truncated": (
                selected_event_window_truncated
                or selected_event_count > len(local_events)
            ),
            "event_records_considered": len(local_events),
            "owner_session_state_status": "selected-session-snapshot",
        }
    for owner_session_id in selected_foreign_owner_sessions:
        pages[owner_session_id] = _claim_owner_event_page(
            store, owner_session_id,
        )

    correlations: list[dict[str, Any]] = []
    owner_pages_unavailable: set[str] = set()
    owner_pages_truncated: set[str] = set()
    owner_states_unavailable: set[str] = set()
    for repository, owner_session_id, claim_id in selected_keys:
        correlation_id = "clmlife_" + digest_hex({
            "repository": repository,
            "owner_session_id": owner_session_id,
            "claim_id": claim_id,
        }, length=16)
        owner_selected = (
            owner_session_id == selected_session_id
            or owner_session_id in selected_foreign_owner_sessions
        )
        page = pages.get(owner_session_id) if owner_selected else None
        if not owner_selected:
            page_status = "owner-session-lookup-limit"
        elif page is None:
            page_status = "owner-event-page-unavailable"
            owner_pages_unavailable.add(owner_session_id)
        else:
            page_status = "read"
            if page["truncated"]:
                owner_pages_truncated.add(owner_session_id)
            if page.get("owner_session_state_status") not in {
                "present", "selected-session-snapshot",
            }:
                owner_states_unavailable.add(owner_session_id)
        matching_records = []
        matching_invalid = 0
        if isinstance(page, dict):
            for event in page["events"]:
                event_type = event.get("type")
                if (
                    not isinstance(event_type, str)
                    or (
                        event_type not in CLAIM_COORDINATION_EVENT_ACTIONS
                        and event_type != "lease.recovered-and-acquired"
                    )
                    or not _claim_event_identity_matches(
                        event, repository=repository, claim_id=claim_id,
                    )
                ):
                    continue
                projected = (
                    _claim_recovery_event_projection(event, owner_session_id)
                    if event_type == "lease.recovered-and-acquired"
                    else _claim_coordination_event_projection(
                        event, owner_session_id,
                    )
                )
                if projected is None:
                    matching_invalid += 1
                    continue
                projected["owner_session_id"] = owner_session_id
                projected["matched_claim_id"] = claim_id
                projected["relation_basis"] = (
                    "exact-owner-session-and-claim-identity;repository-checked-if-recorded"
                )
                matching_records.append(projected)
        matching_records.sort(
            key=lambda item: item["event_reference"]["sequence"],
        )
        valid_match_count = len(matching_records)
        returned_records = list(reversed(matching_records))[
            :MAX_SESSION_CLAIM_LIFECYCLE_EVENTS_PER_CORRELATION
        ]
        if page is None:
            status = page_status
        elif page.get("owner_session_state_status") not in {
            "present", "selected-session-snapshot",
        }:
            state_status = page.get("owner_session_state_status")
            state_suffix = (
                "snapshot-not-found" if state_status == "not-found"
                else "state-unavailable"
            )
            status = f"matching-events-owner-{state_suffix}" if returned_records else (
                f"owner-{state_suffix}"
            )
        elif returned_records and page["truncated"]:
            status = "matching-events-in-truncated-owner-tail"
        elif returned_records:
            status = "matching-events-in-owner-history"
        elif matching_invalid:
            status = "matching-events-invalid-for-projection"
        elif page["truncated"]:
            status = "no-match-in-truncated-owner-tail"
        else:
            status = "no-matching-event-in-owner-history"
        row = {
            "identity": correlation_id,
            "repository": repository,
            "owner_session_id": owner_session_id,
            "claim_id": claim_id,
            "status": status,
            "relation_basis": (
                "exact repository, owner session, and claim identity; matching "
                "lifecycle event references are inspected without causal inference"
            ),
            "causal_resolution_status": "not-established",
            "related_session_activity": relations[(repository, owner_session_id, claim_id)][
                :MAX_SESSION_ACTIVITY_CLAIM_LIFECYCLE_CORRELATIONS
            ],
            "related_session_activity_count": len(
                relations[(repository, owner_session_id, claim_id)],
            ),
            "related_session_activity_omitted_count": max(
                0,
                len(relations[(repository, owner_session_id, claim_id)])
                - MAX_SESSION_ACTIVITY_CLAIM_LIFECYCLE_CORRELATIONS,
            ),
            "claim_lifecycle_event_count": valid_match_count,
            "claim_lifecycle_events": returned_records,
            "claim_lifecycle_records_omitted": max(
                0, valid_match_count - len(returned_records),
            ),
            "claim_lifecycle_records_invalid": matching_invalid,
        }
        if isinstance(page, dict):
            row["owner_event_page"] = {
                "status": "read",
                "event_count": page["event_count"],
                "event_records_considered": page["event_records_considered"],
                "truncated": page["truncated"],
                "limit": MAX_SESSION_CLAIM_OWNER_EVENT_PAGE,
                "owner_session_state_status": (
                    page["owner_session_state_status"]
                ),
            }
        else:
            row["owner_event_page"] = {
                "status": page_status,
                "limit": MAX_SESSION_CLAIM_OWNER_EVENT_PAGE,
                "owner_session_state_status": (
                    "unavailable" if page is None else
                    page.get("owner_session_state_status", "unavailable")
                ),
            }
        for activity_record in denial_records:
            conflict = activity_record.get("claim_conflict")
            blocker = (
                conflict.get("blocking_claim")
                if isinstance(conflict, dict) else None
            )
            if (
                isinstance(blocker, dict)
                and conflict.get("repository") == repository
                and blocker.get("session_id") == owner_session_id
                and blocker.get("claim_id") == claim_id
            ):
                blocker["claim_lifecycle_correlation_identity"] = correlation_id
        for eligibility in integration_eligibility_records:
            if eligibility.get("repository") != repository:
                continue
            for overlap in eligibility.get("coordination_overlaps", []):
                if (
                    isinstance(overlap, dict)
                    and overlap.get("session_id") == owner_session_id
                    and overlap.get("claim_id") == claim_id
                ):
                    overlap["claim_lifecycle_correlation_identity"] = correlation_id
        for eligibility in proposal_eligibility_records:
            if eligibility.get("repository") != repository:
                continue
            for claim in eligibility.get("coordination_only_claims", []):
                if (
                    isinstance(claim, dict)
                    and claim.get("session_id") == owner_session_id
                    and claim.get("claim_id") == claim_id
                ):
                    claim["claim_lifecycle_correlation_identity"] = correlation_id
        correlations.append(row)

    lookup_omitted = max(
        0, len(foreign_owner_sessions) - len(selected_foreign_owner_sessions),
    )
    correlation_omitted = max(0, len(ordered_keys) - len(selected_keys))
    incomplete = bool(
        lookup_omitted or correlation_omitted or owner_pages_unavailable
        or owner_pages_truncated
        or any(item.get("claim_lifecycle_records_invalid", 0) for item in correlations)
        or any(item.get("claim_lifecycle_records_omitted", 0) for item in correlations)
        or any(item.get("related_session_activity_omitted_count", 0)
               for item in correlations)
        or any(
            item.get("status") in {
                "no-match-in-truncated-owner-tail",
                "owner-state-unavailable",
                "matching-events-owner-state-unavailable",
                "owner-snapshot-not-found",
                "matching-events-owner-snapshot-not-found",
            }
            for item in correlations
        )
    )
    source = {
        "status": (
            "not-applicable" if not ordered_keys
            else "partial" if incomplete else "available"
        ),
        "correlation_records_considered": len(ordered_keys),
        "correlation_records_returned": len(correlations),
        "correlation_records_omitted": correlation_omitted,
        "related_session_activity_records_considered": sum(
            item["related_session_activity_count"] for item in correlations
        ),
        "related_session_activity_records_omitted": sum(
            item["related_session_activity_omitted_count"]
            for item in correlations
        ),
        "owner_session_lookups_attempted": len(selected_foreign_owner_sessions),
        "owner_session_lookup_limit": MAX_SESSION_CLAIM_OWNER_EVENT_SESSIONS,
        "owner_session_lookups_omitted": lookup_omitted,
        "owner_event_page_limit": MAX_SESSION_CLAIM_OWNER_EVENT_PAGE,
        "owner_event_pages_unavailable": len(owner_pages_unavailable),
        "owner_event_pages_truncated": len(owner_pages_truncated),
        "owner_session_states_unavailable": len(owner_states_unavailable),
        "selection_policy": (
            "denial-integration-and-proposal-overlap-claim-references-first"
        ),
    }
    return correlations, source


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


def _session_service_readiness(
    session: Any, snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Project bounded service readiness and dependency evidence."""
    identity_pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}$")
    code_pattern = re.compile(r"^[a-z][a-z0-9.-]{0,79}$")
    timestamp_pattern = re.compile(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$"
    )
    service_statuses = {"ready", "degraded", "unavailable", "unknown"}
    operation_statuses = {
        "ok", "failed", "suppressed", "deferred", "not-attempted",
        "pending-escalation", "unknown",
    }

    def identity(value: Any) -> str | None:
        return value if isinstance(value, str) and identity_pattern.fullmatch(value) else None

    def code(value: Any) -> str | None:
        return value if isinstance(value, str) and code_pattern.fullmatch(value) else None

    session_id = identity(getattr(session, "session_id", None))
    environment_id = identity(snapshot.get("environment_id"))
    sequence = snapshot.get("snapshot_sequence")
    sequence = sequence if type(sequence) is int and sequence >= 0 else None
    reconciled_at = snapshot.get("last_reconciled_at")
    if not isinstance(reconciled_at, str) or not timestamp_pattern.fullmatch(reconciled_at):
        reconciled_at = None

    requirements = snapshot.get("requirements")
    requirements = requirements if isinstance(requirements, dict) else {}
    declared = requirements.get("services")
    declared = declared if isinstance(declared, list) else []
    raw_observations = snapshot.get("service_observations")
    raw_observations = raw_observations if isinstance(raw_observations, list) else []
    raw_operations = snapshot.get("service_operations")
    raw_operations = raw_operations if isinstance(raw_operations, list) else []
    has_records = bool(declared or raw_observations or raw_operations)
    source = {
        "kind": "current-session-snapshot",
        "session_id": session_id,
        "environment_id": environment_id,
        "snapshot_sequence": sequence,
        "last_reconciled_at": reconciled_at,
        "declared_service_count": len(declared),
        "service_observation_count": len(raw_observations),
        "service_operation_count": len(raw_operations),
        "service_scan_limit": MAX_SESSION_SERVICE_READINESS_RECORDS,
        "declared_services_truncated": len(declared) > MAX_SESSION_SERVICE_READINESS_RECORDS,
        "observations_truncated": len(raw_observations) > MAX_SESSION_SERVICE_READINESS_RECORDS,
        "operations_truncated": len(raw_operations) > MAX_SESSION_SERVICE_READINESS_RECORDS,
        "provenance_status": (
            "identified" if session_id and environment_id and sequence is not None
            else "incomplete"
        ),
    }
    try:
        readiness = readiness_module.summarize(snapshot).get("status")
    except (KeyError, TypeError, ValueError):
        readiness = None
    readiness = (
        readiness
        if isinstance(readiness, str)
        and readiness in {"ready", "degraded", "blocked"}
        else "unknown"
    )

    observations_by_id: dict[str, list[dict[str, Any]]] = {}
    for item in raw_observations[:MAX_SESSION_SERVICE_READINESS_RECORDS]:
        if isinstance(item, dict) and (service_id := identity(item.get("identity"))):
            observations_by_id.setdefault(service_id, []).append(item)
    operations_by_id: dict[str, list[dict[str, Any]]] = {}
    for item in raw_operations[:MAX_SESSION_SERVICE_READINESS_RECORDS]:
        if isinstance(item, dict) and (service_id := identity(item.get("identity"))):
            operations_by_id.setdefault(service_id, []).append(item)

    services = []
    candidates = []
    status_counts = {status: 0 for status in (*service_statuses, "not-observed")}
    invalid_observations = invalid_operations = 0
    ambiguous_observations = ambiguous_operations = 0
    candidate_count = 0
    declarations = declared[:MAX_SESSION_SERVICE_READINESS_RECORDS]
    for declaration in declarations:
        if not isinstance(declaration, dict):
            continue
        service_id = identity(declaration.get("identity"))
        if service_id is None or any(row["identity"] == service_id for row in services):
            continue
        matching = observations_by_id.get(service_id, [])
        observation = matching[0] if len(matching) == 1 else None
        if len(matching) > 1:
            ambiguous_observations += 1
            status = "unknown"
        elif observation is None:
            status = "not-observed"
        else:
            raw_status = observation.get("status")
            status = (
                raw_status if isinstance(raw_status, str) and raw_status in service_statuses
                else "unknown"
            )
            invalid_observations += (
                not isinstance(raw_status, str)
                or raw_status not in service_statuses
            )
        status_counts[status] += 1

        matching_operations = operations_by_id.get(service_id, [])
        operation = matching_operations[0] if len(matching_operations) == 1 else None
        if len(matching_operations) > 1:
            ambiguous_operations += 1
            operation_status = "ambiguous"
        elif operation is None:
            operation_status = "not-recorded"
        else:
            raw_status = operation.get("status")
            operation_status = (
                raw_status
                if isinstance(raw_status, str) and raw_status in operation_statuses
                else "unknown"
            )
            invalid_operations += (
                not isinstance(raw_status, str)
                or raw_status not in operation_statuses
            )

        dependencies = []
        dependencies_not_ready = []
        dependency_status_by_id = {}
        references = declaration.get("environment_from_service")
        if isinstance(references, dict):
            for reference in references.values():
                dependency_id = (
                    identity(reference.get("service"))
                    if isinstance(reference, dict) else None
                )
                if dependency_id is None or dependency_id in dependencies:
                    continue
                dependencies.append(dependency_id)
                matches = observations_by_id.get(dependency_id, [])
                dependency = matches[0] if len(matches) == 1 else None
                dependency_status = (
                    dependency.get("status") if isinstance(dependency, dict) else None
                )
                if (not isinstance(dependency_status, str)
                        or dependency_status not in service_statuses):
                    dependency_status = "unknown"
                dependency_status_by_id[dependency_id] = dependency_status
                if dependency_status != "ready":
                    dependencies_not_ready.append(dependency_id)

        reference = {
            "kind": "current-session-snapshot-service",
            "session_id": session_id,
            "environment_id": environment_id,
            "snapshot_sequence": sequence,
            "service_identity": service_id,
        }
        row = {
            "identity": service_id,
            "required": (
                declaration.get("required")
                if type(declaration.get("required")) is bool else True
            ),
            "status": status,
            "operation_status": operation_status,
            "causal_status": "not-established",
            "source_reference": reference,
        }
        probe = declaration.get("probe")
        capability = identity(probe.get("capability")) if isinstance(probe, dict) else None
        if capability:
            row["capability"] = capability
        if observation is not None:
            observed_code = code(observation.get("code"))
            observed_at = observation.get("observed_at")
            if observed_code:
                row["observation_code"] = observed_code
            if isinstance(observed_at, str) and timestamp_pattern.fullmatch(observed_at):
                row["observed_at"] = observed_at
        if operation is not None and (operation_code := code(operation.get("code"))):
            row["operation_code"] = operation_code
        if dependencies:
            row["dependencies"] = [
                {
                    "service_identity": dependency_id,
                    "observed_status": dependency_status_by_id[dependency_id],
                    "relationship": "declared-environment-input",
                    "causal_status": "not-established",
                }
                for dependency_id in dependencies[:MAX_SESSION_SERVICE_READINESS_RECORDS]
            ]
        services.append(row)

        basis = []
        if status == "not-observed" and row["required"]:
            basis.append("required-service-observation-missing")
        elif status in {"degraded", "unavailable"}:
            basis.append("service-observation-not-ready")
        elif status == "unknown":
            basis.append("service-observation-incomplete")
        if operation_status == "ambiguous":
            basis.append("reconciliation-operation-ambiguous")
        elif operation_status not in {"ok", "not-recorded"}:
            basis.append("reconciliation-operation-not-successful")
        if dependencies_not_ready:
            basis.append("declared-dependency-not-ready")
        if not basis:
            continue
        candidate_count += 1
        if len(candidates) >= MAX_SESSION_SERVICE_READINESS_CANDIDATES:
            continue
        candidate = {
            "identity": "srci_" + digest_hex({
                "session_id": session_id,
                "snapshot_sequence": sequence,
                "service_identity": service_id,
                "basis": basis,
            }, length=16),
            "kind": "service-readiness-investigation-candidate",
            "status": "candidate-needs-investigation",
            "service_identity": service_id,
            "basis": basis,
            "causal_status": "not-established",
            "source_reference": reference,
        }
        if dependencies_not_ready:
            candidate["supporting_dependency_identities"] = dependencies_not_ready[:16]
        objective = {}
        campaign = snapshot.get("campaign")
        campaign = campaign if isinstance(campaign, dict) else {}
        intent = snapshot.get("intent")
        intent = intent if isinstance(intent, dict) else {}
        for key, value in (("campaign_id", campaign.get("identity")),
                           ("work_intent_id", intent.get("identity"))):
            if (objective_id := identity(value)) is not None:
                objective[key] = objective_id
        if objective:
            candidate["objective_context"] = objective
        candidates.append(candidate)

    result = {
        "status": "observed" if has_records else "not-captured-in-session",
        "readiness_status": readiness,
        "source": source,
        "summary": {
            "declared_service_count": len(declared),
            "projected_service_count": len(services),
            "status_counts": status_counts,
            "investigation_candidate_count": candidate_count,
            "investigation_candidates_omitted": max(0, candidate_count - len(candidates)),
            "invalid_observation_count": invalid_observations,
            "invalid_operation_count": invalid_operations,
            "ambiguous_observation_count": ambiguous_observations,
            "ambiguous_operation_count": ambiguous_operations,
        },
        "services": services,
        "candidates": candidates,
        "measurement_limits": [
            "Readiness and reconciliation results are co-observed in this current snapshot; causality is not established.",
            "Provider prose, raw responses, and workspace paths are omitted.",
            "A non-ready service or unsuccessful operation is an investigation candidate, not proof of an environmental defect.",
            "Earlier readiness history is not reconstructed by this view.",
        ],
    }
    if has_records and (
        source["provenance_status"] != "identified"
        or source["declared_services_truncated"]
        or source["observations_truncated"]
        or source["operations_truncated"]
        or (not declared and (raw_observations or raw_operations))
    ):
        result["status"] = "partial"
    return result


def _session_doctor_health_history(
    session: Any, snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Project bounded Doctor epochs and summaries from the session snapshot."""
    identity_pattern = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}$")
    digest_pattern = re.compile(r"^[0-9a-f]{16}$")
    evidence_digest_pattern = re.compile(r"^[0-9a-f]{64}$")
    timestamp_pattern = re.compile(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$"
    )

    def identity(value: Any) -> str | None:
        return value if isinstance(value, str) and identity_pattern.fullmatch(value) else None

    def summary(value: Any) -> dict[str, int] | None:
        if not isinstance(value, dict):
            return None
        counts = {
            key: value[key]
            for key in ("repaired", "reconciled", "degraded", "blockers")
            if type(value.get(key)) is int and value[key] >= 0
        }
        return counts if len(counts) == 4 else None

    def unavailable(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        count = value.get("count")
        digest = value.get("digest")
        result: dict[str, Any] = {}
        if type(count) is int and count >= 0:
            result["count"] = count
        if isinstance(digest, str) and digest_pattern.fullmatch(digest):
            result["digest"] = digest
        return result if len(result) == 2 else None

    def timestamp(value: Any) -> str | None:
        return value if isinstance(value, str) and timestamp_pattern.fullmatch(value) else None

    session_id = identity(getattr(session, "session_id", None))
    environment_id = identity(snapshot.get("environment_id"))
    snapshot_sequence = snapshot.get("snapshot_sequence")
    snapshot_sequence = (
        snapshot_sequence
        if type(snapshot_sequence) is int and snapshot_sequence >= 0 else None
    )
    campaign = snapshot.get("campaign")
    campaign = campaign if isinstance(campaign, dict) else {}
    intent = snapshot.get("intent")
    intent = intent if isinstance(intent, dict) else {}
    raw_doctor = snapshot.get("doctor")
    doctor = raw_doctor if isinstance(raw_doctor, dict) else {}
    raw_epoch = doctor.get("epoch")
    invalid_epoch_shape = "epoch" in doctor and not isinstance(raw_epoch, dict)
    epoch = raw_epoch if isinstance(raw_epoch, dict) else None
    raw_history_value = doctor.get("history")
    invalid_history_container = (
        "history" in doctor and not isinstance(raw_history_value, list)
    )
    raw_history = raw_history_value
    raw_history = raw_history if isinstance(raw_history, list) else []
    raw_evidence_reference = doctor.get("evidence_ref")
    invalid_evidence_reference_shape = (
        "evidence_ref" in doctor and not isinstance(raw_evidence_reference, dict)
    )
    has_records = any(key in doctor for key in ("epoch", "history", "evidence_ref"))
    invalid_doctor_schema = (
        has_records
        and doctor.get("schema_version") != "mncs.environment.doctor/2"
    )
    selected_history = raw_history[-MAX_SESSION_DOCTOR_HISTORY:]
    invalid_history_count = int(invalid_history_container)
    history = []
    for offset, item in enumerate(reversed(selected_history), start=1):
        if not isinstance(item, dict):
            invalid_history_count += 1
            continue
        digest = item.get("digest")
        validated_at = timestamp(item.get("validated_at"))
        counts = summary(item.get("summary"))
        unavailable_counts = unavailable(item.get("unavailable"))
        if (not isinstance(digest, str) or not digest_pattern.fullmatch(digest)
                or validated_at is None or counts is None):
            invalid_history_count += 1
            continue
        if (
            ("unavailable" in item and unavailable_counts is None)
            or ("reused" in item and type(item.get("reused")) is not bool)
        ):
            invalid_history_count += 1
        record = {
            "ordinal_from_newest": offset,
            "digest": digest,
            "validated_at": validated_at,
            "summary": counts,
            "evidence_reference_status": "not-recorded-per-history-entry",
        }
        if unavailable_counts is not None:
            record["unavailable"] = unavailable_counts
        if type(item.get("reused")) is bool:
            record["reused"] = item["reused"]
        history.append(record)

    current_epoch = None
    invalid_epoch = invalid_epoch_shape or bool(
        epoch is not None
        and epoch.get("schema_version") != "mncs.environment.doctor-epoch/2"
    )
    if epoch is not None:
        digest = epoch.get("digest")
        validated_at = timestamp(epoch.get("validated_at"))
        counts = summary(epoch.get("summary"))
        epoch_sequence = epoch.get("snapshot_sequence")
        if type(epoch_sequence) is not int or epoch_sequence < 0:
            epoch_sequence = None
        if (not isinstance(digest, str) or not digest_pattern.fullmatch(digest)
                or validated_at is None or counts is None):
            invalid_epoch = True
        else:
            readiness = epoch.get("readiness")
            valid_readiness = readiness in {"ready", "degraded", "blocked"}
            invalid_epoch = invalid_epoch or not valid_readiness
            current_epoch = {
                "digest": digest,
                "validated_at": validated_at,
                "summary": counts,
                "readiness": (
                    readiness if isinstance(readiness, str) and valid_readiness
                    else "unknown"
                ),
                "snapshot_sequence": epoch_sequence,
            }
            unavailable_counts = unavailable(epoch.get("unavailable"))
            if unavailable_counts is not None:
                current_epoch["unavailable"] = unavailable_counts
            else:
                invalid_epoch = True
            if epoch_sequence is None:
                invalid_epoch = True
            if epoch_sequence is None or snapshot_sequence is None:
                current_epoch["snapshot_relation"] = "snapshot-revision-unavailable"
            elif epoch_sequence == snapshot_sequence:
                current_epoch["snapshot_relation"] = "same-snapshot-revision"
            elif epoch_sequence < snapshot_sequence:
                current_epoch["snapshot_relation"] = "earlier-snapshot-revision"
            else:
                current_epoch["snapshot_relation"] = (
                    "future-or-inconsistent-snapshot-revision"
                )

    evidence_reference = None
    evidence_reference_status = "not-recorded"
    if isinstance(raw_evidence_reference, dict):
        reference_schema = raw_evidence_reference.get("schema")
        reference_identity = raw_evidence_reference.get("identity")
        expected_prefix = f"{session_id}:doctor:" if session_id else None
        if (reference_schema == "mncs.environment.doctor-evidence/1"
                and isinstance(reference_identity, str)
                and len(reference_identity) <= 256
                and expected_prefix is not None
                and reference_identity.startswith(expected_prefix)
                and evidence_digest_pattern.fullmatch(
                    reference_identity[len(expected_prefix):],
                )):
            evidence_reference = {
                "kind": "doctor-immutable-evidence-record",
                "schema": reference_schema,
                "identity": reference_identity,
            }
            evidence_reference_status = "reference-identified"
        else:
            evidence_reference_status = "reference-invalid-or-unmatched"
    elif invalid_evidence_reference_shape:
        evidence_reference_status = "reference-invalid-or-unmatched"
    if current_epoch is not None and evidence_reference is not None:
        current_epoch["evidence_reference"] = evidence_reference
    elif current_epoch is not None:
        current_epoch["evidence_reference_status"] = evidence_reference_status

    history_omitted = max(0, len(raw_history) - len(selected_history))
    provenance_status = (
        "identified" if session_id and environment_id and snapshot_sequence is not None
        else "incomplete"
    )
    source = {
        "kind": "current-session-snapshot",
        "session_id": session_id,
        "environment_id": environment_id,
        "snapshot_sequence": snapshot_sequence,
        "campaign_id": identity(campaign.get("identity")),
        "work_intent_id": identity(intent.get("identity")),
        "doctor_schema_version": (
            doctor.get("schema_version")
            if isinstance(doctor.get("schema_version"), str)
            and len(doctor["schema_version"]) <= 96 else None
        ),
        "history_record_count": len(raw_history),
        "history_records_projected": len(history),
        "history_records_omitted": history_omitted,
        "history_limit": MAX_SESSION_DOCTOR_HISTORY,
        "invalid_history_record_count": invalid_history_count,
        "evidence_reference_status": evidence_reference_status,
        "provenance_status": provenance_status,
    }
    if raw_doctor is not None and not isinstance(raw_doctor, dict):
        status = "partial"
    elif not has_records:
        status = "not-recorded-in-session"
    elif (
        provenance_status != "identified" or history_omitted
        or invalid_history_count or invalid_epoch or invalid_doctor_schema
        or evidence_reference_status == "reference-invalid-or-unmatched"
    ):
        status = "partial"
    else:
        status = "observed"
    return {
        "status": status,
        "source": source,
        "current_epoch": current_epoch,
        "history": history,
        "measurement_limits": [
            "Doctor history contains bounded health summaries; history entries do not carry individual immutable evidence references.",
            "The current epoch evidence reference identifies detailed Doctor evidence, but a health summary does not establish a service-specific cause or improvement.",
            "The current epoch evidence reference is structurally identified but is not fetched or integrity-verified by this projection.",
            "Snapshot revision relation identifies which session revision the epoch recorded; it does not prove that health remains unchanged afterward.",
        ],
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
    service_readiness = _session_service_readiness(session, snapshot)
    doctor_health_history = _session_doctor_health_history(session, snapshot)
    raw_readiness_requirements = snapshot.get("requirements")
    raw_readiness_requirements = (
        raw_readiness_requirements
        if isinstance(raw_readiness_requirements, dict) else {}
    )
    has_service_readiness = any((
        raw_readiness_requirements.get("services"),
        snapshot.get("service_observations"),
        snapshot.get("service_operations"),
    ))
    has_doctor_health_history = (
        doctor_health_history["status"] != "not-recorded-in-session"
    )
    if has_service_readiness and has_doctor_health_history:
        readiness_source = service_readiness.get("source", {})
        doctor_source = doctor_health_history.get("source", {})
        current_epoch = doctor_health_history.get("current_epoch")
        if not isinstance(readiness_source, dict):
            readiness_source = {}
        if not isinstance(doctor_source, dict):
            doctor_source = {}
        if isinstance(current_epoch, dict):
            same_snapshot_context = all(
                readiness_source.get(key) == doctor_source.get(key)
                and readiness_source.get(key) is not None
                for key in ("session_id", "environment_id", "snapshot_sequence")
            )
            service_summary = service_readiness.get("summary", {})
            service_summary = service_summary if isinstance(service_summary, dict) else {}
            doctor_health_history["service_readiness_co_observation"] = {
                "status": (
                    "same-current-session-snapshot"
                    if same_snapshot_context else "snapshot-context-incomplete-or-different"
                ),
                "relationship": "co-present-in-current-session-snapshot",
                "service_readiness_status": service_readiness.get(
                    "readiness_status", "unknown",
                ),
                "investigation_candidate_count": service_summary.get(
                    "investigation_candidate_count", 0,
                ),
                "doctor_epoch_snapshot_relation": current_epoch.get(
                    "snapshot_relation", "snapshot-revision-unavailable",
                ),
                "causal_status": "not-established",
            }
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
            **({"service_readiness": service_readiness}
               if has_service_readiness else {}),
            **({"doctor_health_history": doctor_health_history}
               if has_doctor_health_history else {}),
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
            **({"service_readiness": service_readiness}
               if has_service_readiness else {}),
            **({"doctor_health_history": doctor_health_history}
               if has_doctor_health_history else {}),
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
    operation_deferral_events = [
        event for event in events
        if event.get("type") == "effect.operation-deferred"
    ]
    operation_deferrals: list[dict[str, Any]] = []
    invalid_operation_deferral_count = 0
    valid_operation_deferral_count = 0
    for event in reversed(operation_deferral_events):
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        reference = _event_reference(event)
        repository = payload.get("repository")
        action = payload.get("action")
        reason = payload.get("reason")
        waited_ms = payload.get("waited_ms")
        changed_fields = payload.get("changed_fields")
        initial_preflight = _event_reference(
            payload.get("initial_preflight_event"),
        ) if isinstance(payload.get("initial_preflight_event"), dict) else None
        locked_preflight = _event_reference(
            payload.get("locked_preflight_event"),
        ) if isinstance(payload.get("locked_preflight_event"), dict) else None
        stale_fields = {
            "checkout_identity", "branch", "branch_status", "head",
            "head_status",
        }
        stale_preflight_valid = (
            isinstance(changed_fields, list)
            and 1 <= len(changed_fields) <= len(stale_fields)
            and all(isinstance(field, str) and field in stale_fields
                    for field in changed_fields)
            and len(set(changed_fields)) == len(changed_fields)
            and initial_preflight is not None
            and locked_preflight is not None
            and initial_preflight.get("type") in {
                "authority.proposal-eligible",
                "authority.integration-eligible",
            }
            and locked_preflight.get("type") in {
                "authority.proposal-eligible",
                "authority.integration-eligible",
            }
            and initial_preflight["sequence"] < reference["sequence"]
            and locked_preflight["sequence"] < reference["sequence"]
        ) if reference is not None else False
        if (reference is None
                or not isinstance(repository, str) or not _is_repository_identity(repository)
                or not isinstance(action, str)
                or action not in {"write", "mutate", "execute", "verify", "merge", "publish"}
                or reason not in {
                    "same-checkout-operation-in-progress",
                    "checkout-state-changed-since-preflight",
                }
                or type(waited_ms) is not int or not 0 <= waited_ms <= 300_000
                or payload.get("operation_executed") is not False):
            invalid_operation_deferral_count += 1
            continue
        if (reason == "checkout-state-changed-since-preflight"
                and not stale_preflight_valid):
            invalid_operation_deferral_count += 1
            continue
        item = {
            "identity": "opdef_" + digest_hex({
                "session_id": session.session_id,
                "event": reference["identity"],
            }, length=16),
            "action": action,
            "repository": repository,
            "reason": reason,
            "waited_ms": waited_ms,
            "operation_executed": False,
            "event_reference": reference,
        }
        if reason == "checkout-state-changed-since-preflight":
            item["changed_fields"] = changed_fields
            item["initial_preflight_event"] = initial_preflight
            item["locked_preflight_event"] = locked_preflight
        for key in ("branch", "checkout_identity"):
            value = payload.get(key)
            if isinstance(value, str) and 0 < len(value) <= 160:
                item[key] = value
        valid_operation_deferral_count += 1
        if len(operation_deferrals) < MAX_SESSION_ACTIVITY_OPERATION_DEFERRALS:
            operation_deferrals.append(item)
    operation_deferrals_omitted = max(
        0, valid_operation_deferral_count - len(operation_deferrals),
    )
    claim_recovery_events = [
        event for event in events
        if event.get("type") == "lease.recovered-and-acquired"
    ]
    claim_recoveries = []
    invalid_claim_recovery_event_count = 0
    valid_claim_recovery_count = 0
    for event in reversed(claim_recovery_events):
        item = _claim_recovery_event_projection(event, session.session_id)
        if item is None:
            invalid_claim_recovery_event_count += 1
            continue
        valid_claim_recovery_count += 1
        if len(claim_recoveries) < MAX_SESSION_ACTIVITY_CLAIM_RECOVERIES:
            claim_recoveries.append(item)
    claim_recovery_records_omitted = max(
        0, valid_claim_recovery_count - len(claim_recoveries),
    )
    operation_claim_recovery_events = [
        event for event in events
        if event.get("type") == "lease.recovered-for-operation"
    ]
    operation_claim_recoveries = []
    invalid_operation_claim_recovery_count = 0
    for event in reversed(operation_claim_recovery_events):
        item = _operation_claim_recovery_event_projection(
            event, session.session_id,
        )
        if item is None:
            invalid_operation_claim_recovery_count += 1
            continue
        if len(operation_claim_recoveries) < MAX_SESSION_ACTIVITY_CLAIM_RECOVERIES:
            operation_claim_recoveries.append(item)
    operation_claim_recoveries_omitted = max(
        0,
        len(operation_claim_recovery_events)
        - invalid_operation_claim_recovery_count
        - len(operation_claim_recoveries),
    )
    claim_coordination_events = [
        event for event in events
        if isinstance(event.get("type"), str)
        and event.get("type") in CLAIM_COORDINATION_EVENT_ACTIONS
    ]
    claim_coordination = []
    invalid_claim_coordination_event_count = 0
    valid_claim_coordination_count = 0
    for event in reversed(claim_coordination_events):
        item = _claim_coordination_event_projection(event, session.session_id)
        if item is None:
            invalid_claim_coordination_event_count += 1
            continue
        valid_claim_coordination_count += 1
        if len(claim_coordination) < MAX_SESSION_ACTIVITY_CLAIM_COORDINATION:
            claim_coordination.append(item)
    claim_coordination_records_omitted = max(
        0, valid_claim_coordination_count - len(claim_coordination),
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
                start_identity = _invocation_binding_identity(
                    start.get("payload"),
                )
                completion_identity = _invocation_binding_identity(
                    completion.get("payload"),
                )
                identity_mismatch = (
                    start_identity.get("status") == "complete"
                    and completion_identity.get("status") == "complete"
                    and any(start_identity.get(key) != completion_identity.get(key)
                            for key in (
                                "provider", "binding_id", "contract_revision",
                            ))
                )
                paired_by_start[id(start)] = (
                    (None, "provider-request-id-binding-identity-mismatch")
                    if identity_mismatch else (
                        completion, "matched-by-provider-request-id",
                    )
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
            "binding_identity": _invocation_binding_identity(payload),
        }
        effect_target = _invocation_effect_target(
            payload.get("effect_target"),
            completion_payload.get("effect_target") if completion is not None else None,
        )
        if effect_target is not None:
            interaction["effect_target"] = effect_target
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
        binding_identity_count = sum(
            isinstance(item.get("binding_identity"), dict)
            and item["binding_identity"].get("status") == "complete"
            for item in group
        )
        capabilities.append({
            "capability": capability or None,
            "invocation_count": len(group),
            "completion_count": sum(
                item.get("completion_event") is not None for item in group
            ),
            "outcome_status_counts": status_counts,
            "supporting_events": [
                item["invocation_event"]
                for item in group[:MAX_CAPABILITY_SUPPORTING_EVENTS]
                if isinstance(item.get("invocation_event"), dict)
            ],
            "binding_identity_attribution": {
                "status": (
                    "recorded-for-all-invocations"
                    if binding_identity_count == len(group) else (
                        "partially-recorded"
                        if binding_identity_count else "not-recorded"
                    )
                ),
                "complete_identity_count": binding_identity_count,
                "identity_unavailable_count": len(group) - binding_identity_count,
                "basis": (
                    "provider-binding-id-contract-revision-on-capability-invoked-event"
                ),
            },
        })
    all_capability_summaries = capabilities
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
        or rpc_capture_limit_reached or claim_coordination_records_omitted > 0
        or invalid_claim_coordination_event_count > 0
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
    capability_delivery = _capability_delivery_view(
        snapshot, all_capability_summaries, interactions, events,
        event_window_truncated=event_window_truncated,
        event_page_scoped=event_before_sequence is not None,
        invocation_summaries_omitted=capabilities_omitted,
    )
    denial_events = [
        event for event in events if event.get("type") == "authority.denied"
    ]
    coordination_deferral_events = [
        event for event in events
        if event.get("type") == "authority.coordination-deferred"
    ]
    integration_eligibility_events = [
        event for event in events
        if event.get("type") == "authority.integration-eligible"
    ]
    proposal_eligibility_events = [
        event for event in events
        if event.get("type") == "authority.proposal-eligible"
    ]
    repository_ref_transition_events = [
        event for event in events
        if event.get("type") == "campaign.reconciled"
        and isinstance(event.get("payload"), dict)
        and isinstance(event["payload"].get("repository_ref_transitions"), list)
    ]
    if integration_eligibility_events:
        limits.append(
            "Integration eligibility records are preflight decisions; they do "
            "not establish that a Git merge or publication was executed or succeeded."
        )
    if proposal_eligibility_events:
        limits.append(
            "Proposal-worktree eligibility records are preflights; an exact "
            "event reference may link one to a later Environment provider "
            "invocation. Provider completion status does not establish a "
            "repository change, independent verification, integration, or benefit."
        )
    if repository_ref_transition_events:
        limits.append(
            "Repository ref transitions are Environment observations. An exact "
            "preflight match establishes a later observation of the same checkout "
            "preimage, not that the eligible operation caused the change or that "
            "a remote publication succeeded."
        )
    if denial_events:
        limits.append(
            "Authority-denial projections compare recorded blocker identities "
            "with at most four current claim records; owner liveness is a "
            "point-in-time observation and does not prove that the original "
            "denial was appropriate."
        )
    if coordination_deferral_events:
        limits.append(
            "Coordination-deferred records describe operations withheld for a "
            "scope or concurrency condition; they are not authority denials or "
            "owner-approval requests, and do not prove the proposed work was invalid."
        )
    if claim_recovery_events:
        limits.append(
            "Claim-recovery activity records claim-state transitions, not "
            "whether later development outcomes improved."
        )
    if claim_coordination_events:
        limits.append(
            "Claim-coordination activity records bounded claim-state transitions, "
            "not exclusive work authority, accepted modifications, or development outcomes. "
            "Historical events without objective references remain unlinked."
        )
    denial_records: list[dict[str, Any]] = []
    snapshot_facts = authority_module.repository_facts_for_snapshot(snapshot)
    invalid_denial_event_count = 0
    invalid_integration_denial_preflight_state_count = 0
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
        repository = payload.get("repository")
        if not _is_repository_identity(repository):
            repository = (
                authority_module.repository_for_target(target, snapshot_facts)
                if isinstance(target, str) and target else None
            )
        reason_class = _authority_denial_reason_class(reason)
        claim_conflict = _authority_claim_conflict(
            payload.get("claim_conflict"), repository,
        )
        denial = {
            "identity": "authdeny_" + digest_hex({
                "session_id": session.session_id,
                "event": reference["identity"],
            }, length=16),
            "action": action,
            "outcome_status": "denied",
            "reason_class": reason_class,
            **_authority_event_objective_context(payload),
            "claim_conflict_evidence_status": (
                "recorded" if claim_conflict is not None else (
                    "not-recorded" if reason_class == "overlapping-claim"
                    else "not-applicable"
                )
            ),
            **({"repository": repository} if repository else {}),
            "event_reference": reference,
        }
        if action in {"merge", "publish"}:
            raw_preflight_state = payload.get("repository_preflight_state")
            preflight_state = _authority_integration_preflight_state(
                raw_preflight_state,
            )
            if raw_preflight_state is None:
                preflight_state_status = "not-recorded-on-event"
            elif preflight_state is None:
                preflight_state_status = "invalid-event-observation"
                invalid_integration_denial_preflight_state_count += 1
            else:
                preflight_state_status = "recorded-and-validated"
            denial["integration_preflight_state_status"] = (
                preflight_state_status
            )
            if preflight_state is not None:
                denial["repository_preflight_state"] = preflight_state
                denial["preflight_state_interpretation_limit"] = (
                    "This is the local destination checkout state at a denied "
                    "preflight; it does not establish whether the denial was "
                    "appropriate, source or remote-ref state, later execution, "
                    "or integration outcome."
                )
        if claim_conflict is not None:
            denial["claim_conflict"] = claim_conflict
        denial_records.append(denial)
    current_claim_states, claim_state_lookup = _session_activity_current_claim_states(
        session, denial_records,
    )
    for denial in denial_records:
        conflict = denial.get("claim_conflict")
        blocker = (
            conflict.get("blocking_claim") if isinstance(conflict, dict) else None
        )
        claim_id = blocker.get("claim_id") if isinstance(blocker, dict) else None
        current_state = (
            current_claim_states.get(claim_id) if isinstance(claim_id, str) else None
        )
        if isinstance(current_state, dict):
            conflict["current_claim_state"] = current_state
            conflict["coordination_guidance"] = (
                _authority_claim_conflict_guidance(
                    str(denial.get("action") or ""),
                    conflict,
                    current_state,
                )
            )
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
        if reason_class == "overlapping-claim":
            structured_denials = [
                (item, item["claim_conflict"])
                for item in group
                if isinstance(item.get("claim_conflict"), dict)
            ]
            recorded_conflicts = [conflict for _, conflict in structured_denials]
            candidate["claim_conflict_evidence_status"] = (
                "recorded-for-all-denials"
                if len(recorded_conflicts) == len(group) else (
                    "partially-recorded" if recorded_conflicts
                    else "not-recorded"
                )
            )
            if len(recorded_conflicts) == len(group):
                candidate["interpretation"] = (
                    "Structured requested and blocking scopes were recorded for "
                    "every denial in this session window. Any current claim state "
                    "is a separate point-in-time observation; whether the overlap "
                    "required blocking and broader cause remain unestablished."
                )
            elif recorded_conflicts:
                candidate["interpretation"] = (
                    "Structured requested and blocking scopes were recorded for "
                    "some denials in this session window; older records lack "
                    "those details. Current claim state, when available, is a "
                    "separate point-in-time observation; whether the overlap "
                    "required blocking and broader cause remain unestablished."
                )
            decision_state_counts: dict[
                tuple[str, str, str | None], int
            ] = {}
            validated_decision_state_count = 0
            invalid_decision_state_count = 0
            missing_decision_state_count = 0
            for _, conflict_record in structured_denials:
                state = conflict_record.get("decision_claim_state")
                state_status = conflict_record.get(
                    "decision_claim_state_status",
                )
                if (state_status == "recorded-and-validated"
                        and isinstance(state, dict)):
                    blocking_status = state.get("blocking_status")
                    recovery_verdict = state.get("recovery_verdict")
                    freshness = state.get("freshness")
                    if (isinstance(blocking_status, str)
                            and isinstance(recovery_verdict, str)
                            and (freshness is None or isinstance(freshness, str))):
                        key = (blocking_status, recovery_verdict, freshness)
                        decision_state_counts[key] = (
                            decision_state_counts.get(key, 0) + 1
                        )
                        validated_decision_state_count += 1
                    else:
                        invalid_decision_state_count += 1
                elif state_status == "invalid-event-observation":
                    invalid_decision_state_count += 1
                else:
                    missing_decision_state_count += 1
            if validated_decision_state_count == len(structured_denials):
                decision_state_status = (
                    "recorded-for-all-structured-denials"
                    if structured_denials else "not-recorded-on-event"
                )
            elif validated_decision_state_count:
                decision_state_status = "partially-recorded"
            elif invalid_decision_state_count:
                decision_state_status = "invalid-event-observation"
            else:
                decision_state_status = "not-recorded-on-event"
            candidate["decision_claim_state_summary"] = {
                "status": decision_state_status,
                "structured_denial_count": len(structured_denials),
                "validated_count": validated_decision_state_count,
                "invalid_count": invalid_decision_state_count,
                "not_recorded_count": missing_decision_state_count,
                "legacy_denial_count": len(group) - len(structured_denials),
                "state_counts": [
                    {
                        "blocking_status": blocking_status,
                        "recovery_verdict": recovery_verdict,
                        "freshness": freshness,
                        "occurrence_count": count,
                    }
                    for (blocking_status, recovery_verdict, freshness), count
                    in sorted(
                        decision_state_counts.items(),
                        key=lambda item: (
                            item[0][0], item[0][1], item[0][2] or "",
                        ),
                    )[:4]
                ],
            }
            decision_basis_validated = 0
            decision_basis_invalid = 0
            decision_basis_missing = 0
            writable_scope_eligible_count = 0
            for _, conflict_record in structured_denials:
                basis = conflict_record.get("decision_basis")
                basis_status = conflict_record.get("decision_basis_status")
                if (basis_status == "recorded-and-validated"
                        and isinstance(basis, dict)):
                    decision_basis_validated += 1
                    if basis.get("authorization_status") == "eligible":
                        writable_scope_eligible_count += 1
                elif basis_status == "invalid-event-observation":
                    decision_basis_invalid += 1
                else:
                    decision_basis_missing += 1
            if decision_basis_validated == len(structured_denials):
                decision_basis_summary_status = (
                    "recorded-for-all-structured-denials"
                    if structured_denials else "not-recorded-on-event"
                )
            elif decision_basis_validated:
                decision_basis_summary_status = "partially-recorded"
            elif decision_basis_invalid:
                decision_basis_summary_status = "invalid-event-observation"
            else:
                decision_basis_summary_status = "not-recorded-on-event"
            candidate["decision_basis_summary"] = {
                "status": decision_basis_summary_status,
                "structured_denial_count": len(structured_denials),
                "validated_count": decision_basis_validated,
                "invalid_count": decision_basis_invalid,
                "not_recorded_count": decision_basis_missing,
                "writable_scope_eligible_block_count": (
                    writable_scope_eligible_count
                ),
                "interpretation": (
                    "Validated decision evidence distinguishes a writable session "
                    "scope from the coordination lease that blocked mutation; it "
                    "does not establish whether the lease was necessary or what "
                    "happened after the denial."
                ),
            }
            blockers: dict[tuple[str, str], dict[str, Any]] = {}
            for conflict_record in recorded_conflicts:
                blocker = conflict_record.get("blocking_claim")
                if not isinstance(blocker, dict):
                    continue
                blocker_session = blocker.get("session_id")
                blocker_claim_id = blocker.get("claim_id")
                if not isinstance(blocker_session, str):
                    continue
                blocker_key = (
                    str(blocker_claim_id or "claim-id-unavailable"),
                    blocker_session,
                )
                summary = blockers.setdefault(blocker_key, {
                    "session_id": blocker_session,
                    "occurrence_count": 0,
                    "scope": blocker.get("scope"),
                })
                if isinstance(blocker_claim_id, str):
                    summary["claim_id"] = blocker_claim_id
                    current_state = current_claim_states.get(blocker_claim_id)
                    if isinstance(current_state, dict):
                        summary["current_claim_state"] = current_state
                summary["occurrence_count"] += 1
            if blockers:
                candidate["blocking_claims"] = list(blockers.values())[:4]
            conflict_reasons = sorted({
                item["claim_conflict"]["conflict_reason"]
                for item in recorded_conflicts
                if isinstance(item.get("claim_conflict"), dict)
                and isinstance(item["claim_conflict"].get("conflict_reason"), str)
            })
            if conflict_reasons:
                candidate["conflict_reasons"] = conflict_reasons[:4]
        if repository != "repository-unavailable":
            candidate["repository"] = repository
        authority_denial_candidates.append(candidate)
        if len(authority_denial_candidates) >= (
            MAX_SESSION_ACTIVITY_AUTHORITY_PRESSURE_CANDIDATES
        ):
            break
    coordination_deferrals: list[dict[str, Any]] = []
    invalid_coordination_deferral_event_count = 0
    valid_coordination_deferral_count = 0
    for event in reversed(coordination_deferral_events):
        reference = _event_reference(event)
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        action = payload.get("action")
        repository = payload.get("repository")
        required_coordination = payload.get("required_coordination")
        coordination_status = payload.get("coordination_status")
        next_step = payload.get("next_step")
        scope = _authority_conflict_scope(payload.get("scope"), repository)
        if (
            reference is None
            or not isinstance(action, str)
            or action not in authority_module.ACTIONS
            or not _is_repository_identity(repository)
            or not isinstance(required_coordination, str)
            or not 1 <= len(required_coordination) <= 96
            or not isinstance(coordination_status, str)
            or not 1 <= len(coordination_status) <= 96
            or not isinstance(next_step, str)
            or not 1 <= len(next_step) <= 160
            or scope is None
            or payload.get("decision_domain") != "coordination"
            or payload.get("persistent_authority_required") is not False
            or payload.get("operation_executed") is not False
        ):
            invalid_coordination_deferral_event_count += 1
            continue
        claim_conflict = _authority_claim_conflict(
            payload.get("claim_conflict"), repository,
        )
        valid_coordination_deferral_count += 1
        if len(coordination_deferrals) >= (
            MAX_SESSION_ACTIVITY_COORDINATION_DEFERRALS
        ):
            continue
        record = {
            "identity": "coorddefer_" + digest_hex({
                "session_id": session.session_id,
                "event": reference["identity"],
            }, length=16),
            "action": action,
            "repository": repository,
            **_authority_event_objective_context(payload),
            "required_coordination": required_coordination,
            "coordination_status": coordination_status,
            "next_step": next_step,
            "persistent_authority_required": False,
            "operation_executed": False,
            "scope": scope,
            "interpretation": (
                "The Environment deferred this operation for coordination. "
                "This is not an authority request or owner-approval requirement; "
                "it does not establish that the proposed work is invalid."
            ),
            "event_reference": reference,
        }
        if claim_conflict is not None:
            record["claim_conflict"] = claim_conflict
        coordination_deferrals.append(record)
    coordination_deferrals_omitted = max(
        0, valid_coordination_deferral_count - len(coordination_deferrals),
    )
    coordination_deferral_groups: dict[
        tuple[str, str, str, str], list[dict[str, Any]],
    ] = {}
    for item in coordination_deferrals:
        conflict = item.get("claim_conflict")
        recurrence_context = {
            "coordination_status": item.get("coordination_status"),
            "next_step": item.get("next_step"),
            "conflict_reason": (
                conflict.get("conflict_reason")
                if isinstance(conflict, dict) else None
            ),
            "requested_scope": (
                conflict.get("requested_scope")
                if isinstance(conflict, dict) else item.get("scope")
            ),
        }
        recurrence_context_identity = digest_hex(
            recurrence_context, length=16,
        )
        coordination_deferral_groups.setdefault((
            str(item["repository"]), str(item["action"]),
            str(item["required_coordination"]),
            recurrence_context_identity,
        ), []).append(item)
    coordination_deferral_candidates = []
    coordination_deferral_candidate_groups = {}
    for (
        repository, action, required_coordination, recurrence_context_identity,
    ), group in sorted(
        coordination_deferral_groups.items(),
    ):
        if len(group) < 2:
            continue
        candidate = {
            "identity": "ph_coordination_" + digest_hex({
                "session_id": session.session_id,
                "repository": repository,
                "action": action,
                "required_coordination": required_coordination,
                "recurrence_context_identity": recurrence_context_identity,
            }, length=16),
            "status": "candidate-needs-investigation",
            "subsystem": "coordination-and-work-scope",
            "repository": repository,
            "action": action,
            "required_coordination": required_coordination,
            "coordination_status": group[0].get("coordination_status"),
            "next_step": group[0].get("next_step"),
            "recurrence_basis": (
                "same-repository-action-prerequisite-and-requested-scope"
            ),
            "occurrence_count": len(group),
            "distinct_session_count": 1,
            "recurrence_scope": "same-session-selected-event-window",
            "causal_claim": "not-established",
            "interpretation": (
                "This scoped coordination condition recurred in one selected "
                "session window. It may protect a real shared-state invariant "
                "or reveal avoidable environmental friction; cause and "
                "appropriateness remain unestablished."
            ),
            "supporting_events": [
                item["event_reference"] for item in sorted(
                    group,
                    key=lambda row: row["event_reference"]["sequence"],
                )[:8]
            ],
        }
        coordination_deferral_candidate_groups[candidate["identity"]] = group
        coordination_deferral_candidates.append(candidate)
        if len(coordination_deferral_candidates) >= (
            MAX_SESSION_ACTIVITY_AUTHORITY_PRESSURE_CANDIDATES
        ):
            break
    integration_eligibility_records = []
    invalid_integration_eligibility_event_count = 0
    invalid_integration_preflight_state_count = 0
    valid_integration_eligibility_count = 0
    for event in reversed(integration_eligibility_events):
        reference = _event_reference(event)
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        action = payload.get("action")
        repository = payload.get("repository")
        scope = _authority_conflict_scope(payload.get("scope"), repository)
        authorization_basis = payload.get("authorization_basis")
        eligibility_basis = payload.get("eligibility_basis")
        if (
            reference is None
            or action not in {"merge", "publish"}
            or not _is_repository_identity(repository)
            or scope is None
            or authorization_basis != "writable-repository-rights"
            or eligibility_basis != (
                "exact-observed-scope-and-repository-native-concurrency"
            )
            or payload.get("persistent_claim_required") is not False
            or payload.get("operation_executed") is not False
        ):
            invalid_integration_eligibility_event_count += 1
            continue
        raw_overlap_count = payload.get("coordination_overlap_count", 0)
        raw_overlaps = payload.get("coordination_overlaps", [])
        if (
            type(raw_overlap_count) is not int
            or not 0 <= raw_overlap_count <= 100_000
            or not isinstance(raw_overlaps, list)
            or len(raw_overlaps) > MAX_SESSION_ACTIVITY_INTEGRATION_OVERLAPS
            or raw_overlap_count < len(raw_overlaps)
        ):
            invalid_integration_eligibility_event_count += 1
            continue
        coordination_overlaps = []
        invalid_overlap = False
        for overlap in raw_overlaps:
            if not isinstance(overlap, dict):
                invalid_overlap = True
                break
            overlap_scope = _authority_conflict_scope(
                overlap.get("scope"), repository,
            )
            session_id = overlap.get("session_id")
            claim_id = overlap.get("claim_id")
            overlap_reason = overlap.get("overlap_reason")
            if (
                overlap_scope is None
                or not isinstance(session_id, str) or not session_id
                or len(session_id) > 160
                or (claim_id is not None and (
                    not isinstance(claim_id, str) or not claim_id
                    or len(claim_id) > 192
                ))
                or not isinstance(overlap_reason, str)
                or not overlap_reason or len(overlap_reason) > 160
            ):
                invalid_overlap = True
                break
            coordination_overlaps.append({
                "session_id": session_id,
                **({"claim_id": claim_id} if claim_id is not None else {}),
                "scope": overlap_scope,
                "overlap_reason": overlap_reason,
            })
        truncated_overlaps = raw_overlap_count > len(coordination_overlaps)
        if (
            invalid_overlap
            or ("coordination_overlaps_truncated" in payload
                and payload.get("coordination_overlaps_truncated")
                is not truncated_overlaps)
        ):
            invalid_integration_eligibility_event_count += 1
            continue
        valid_integration_eligibility_count += 1
        if len(integration_eligibility_records) >= (
            MAX_SESSION_ACTIVITY_INTEGRATION_ELIGIBILITY
        ):
            continue
        raw_preflight_state = payload.get("repository_preflight_state")
        preflight_state = _authority_integration_preflight_state(
            raw_preflight_state,
        )
        if raw_preflight_state is None:
            preflight_state_status = "not-recorded-on-event"
        elif preflight_state is None:
            preflight_state_status = "invalid-event-observation"
            invalid_integration_preflight_state_count += 1
        else:
            preflight_state_status = "recorded-and-validated"
        eligibility_record = {
            "identity": "inteligible_" + digest_hex({
                "session_id": session.session_id,
                "event": reference["identity"],
            }, length=16),
            "action": action,
            "repository": repository,
            **_authority_event_objective_context(payload),
            "authorization_basis": authorization_basis,
            "eligibility_basis": eligibility_basis,
            "persistent_claim_required": False,
            "operation_executed": False,
            "preflight_state_status": preflight_state_status,
            "coordination_overlap_count": raw_overlap_count,
            "coordination_overlaps": coordination_overlaps,
            "coordination_overlaps_truncated": truncated_overlaps,
            "scope": scope,
            "interpretation": (
                "The Environment found this scoped merge or publish eligible "
                "at preflight time. No integration result is recorded by this event."
            ),
            "event_reference": reference,
        }
        if preflight_state is not None:
            eligibility_record["repository_preflight_state"] = preflight_state
            eligibility_record["preflight_state_interpretation_limit"] = (
                "This is the local destination checkout preimage at preflight; "
                "it does not establish source branch, remote ref state, later "
                "execution, or integration outcome."
            )
        integration_eligibility_records.append(eligibility_record)
    integration_eligibility_records_omitted = max(
        0,
        valid_integration_eligibility_count
        - len(integration_eligibility_records),
    )
    proposal_eligibility_records: list[dict[str, Any]] = []
    invalid_proposal_eligibility_event_count = 0
    invalid_proposal_preflight_state_count = 0
    valid_proposal_eligibility_count = 0
    for event in reversed(proposal_eligibility_events):
        reference = _event_reference(event)
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        action = payload.get("action")
        repository = payload.get("repository")
        scope = _authority_conflict_scope(payload.get("scope"), repository)
        authorization_basis = payload.get("authorization_basis")
        eligibility_basis = payload.get("eligibility_basis")
        writable_scope_required = payload.get(
            "writable_scope_required_for_integration",
        )
        writable_scope_present = payload.get(
            "repository_writable_scope_present",
        )
        valid_authorization_basis = authorization_basis in {
            "session-writable-scope",
            authority_module.PROPOSAL_ONLY_AUTHORIZATION_BASIS,
        }
        valid_integration_scope_requirement = (
            writable_scope_required is None
            and authorization_basis == "session-writable-scope"
        ) or writable_scope_required is True
        valid_writable_scope_observation = (
            writable_scope_present is None
            and authorization_basis == "session-writable-scope"
        ) or (
            type(writable_scope_present) is bool
            and writable_scope_present == (
                authorization_basis == "session-writable-scope"
            )
        )
        if (
            reference is None
            or action not in authority_module.PROPOSAL_WORKTREE_ACTIONS
            or not _is_repository_identity(repository)
            or scope is None
            or scope.get("kind") != "worktree"
            or scope.get("branch") in {None, "main", "master"}
            or not valid_authorization_basis
            or not valid_integration_scope_requirement
            or not valid_writable_scope_observation
            or eligibility_basis != authority_module.PROPOSAL_WORKTREE_ELIGIBILITY
            or payload.get("persistent_claim_required") is not False
            or payload.get("operation_executed") is not False
        ):
            invalid_proposal_eligibility_event_count += 1
            continue
        raw_claim_count = payload.get("coordination_only_claim_count", 0)
        raw_claims = payload.get("coordination_only_claims", [])
        raw_claims_truncated = payload.get(
            "coordination_only_claims_truncated", False,
        )
        if (
            type(raw_claim_count) is not int
            or not 0 <= raw_claim_count <= 100_000
            or not isinstance(raw_claims, list)
            or len(raw_claims) > MAX_SESSION_ACTIVITY_INTEGRATION_OVERLAPS
            or raw_claim_count < len(raw_claims)
            or type(raw_claims_truncated) is not bool
            or raw_claims_truncated != (raw_claim_count > len(raw_claims))
        ):
            invalid_proposal_eligibility_event_count += 1
            continue
        coordination_claims = []
        invalid_coordination_claim = False
        for claim in raw_claims:
            if not isinstance(claim, dict):
                invalid_coordination_claim = True
                break
            session_id = claim.get("session_id")
            claim_id = claim.get("claim_id")
            claim_scope = _authority_conflict_scope(
                claim.get("scope"), repository,
            )
            claim_kind = (
                claim_scope.get("kind") if isinstance(claim_scope, dict) else None
            )
            expected_disposition = {
                "repository": "coordination-only-broad-claim",
                "paths": "coordination-only-repository-path-claim",
            }.get(claim_kind)
            shared_checkout_identity = scope.get("checkout_identity")
            claim_checkout_identity = (
                claim_scope.get("checkout_identity")
                if isinstance(claim_scope, dict) else None
            )
            shared_git_directory = scope.get(
                "git_common_directory_identity",
            )
            claim_git_directory = (
                claim_scope.get("git_common_directory_identity")
                if isinstance(claim_scope, dict) else None
            )
            shared_branch_overlap = (
                claim_kind == "worktree"
                and isinstance(claim_scope, dict)
                and claim_scope.get("branch") == scope.get("branch")
                and isinstance(scope.get("branch"), str)
                and isinstance(shared_checkout_identity, str)
                and isinstance(claim_checkout_identity, str)
                and shared_checkout_identity != claim_checkout_identity
                and not (
                    isinstance(shared_git_directory, str)
                    and isinstance(claim_git_directory, str)
                    and shared_git_directory != claim_git_directory
                )
            )
            if shared_branch_overlap:
                expected_disposition = (
                    "coordination-only-shared-branch-proposal"
                )
            if (
                not isinstance(session_id, str) or not session_id
                or len(session_id) > 160
                or (claim_id is not None and (
                    not isinstance(claim_id, str) or not claim_id
                    or len(claim_id) > 192
                ))
                or claim_scope is None
                or expected_disposition is None
                or claim.get("disposition") != expected_disposition
                or (
                    expected_disposition
                    == "coordination-only-shared-branch-proposal"
                    and claim.get("overlap_reason")
                    != "shared branch with overlapping paths across checkouts"
                )
                or (
                    expected_disposition
                    != "coordination-only-shared-branch-proposal"
                    and "overlap_reason" in claim
                )
            ):
                invalid_coordination_claim = True
                break
            coordination_claims.append({
                "session_id": session_id,
                **({"claim_id": claim_id} if claim_id is not None else {}),
                "scope": claim_scope,
                "disposition": expected_disposition,
                **({
                    "overlap_reason": (
                        "shared branch with overlapping paths across checkouts"
                    ),
                } if expected_disposition
                    == "coordination-only-shared-branch-proposal" else {}),
            })
        if invalid_coordination_claim:
            invalid_proposal_eligibility_event_count += 1
            continue
        raw_preflight_state = payload.get("proposal_preflight_state")
        preflight_state = _authority_proposal_preflight_state(
            raw_preflight_state,
        )
        if raw_preflight_state is None:
            preflight_state_status = "not-recorded-on-event"
        elif preflight_state is None:
            preflight_state_status = "invalid-event-observation"
            invalid_proposal_preflight_state_count += 1
        else:
            preflight_state_status = "recorded-and-validated"
        valid_proposal_eligibility_count += 1
        if len(proposal_eligibility_records) >= (
            MAX_SESSION_ACTIVITY_PROPOSAL_ELIGIBILITY
        ):
            continue
        proposal_only = (
            authorization_basis
            == authority_module.PROPOSAL_ONLY_AUTHORIZATION_BASIS
        )
        proposal_eligibility_records.append({
            "identity": "proposaleligible_" + digest_hex({
                "session_id": session.session_id,
                "event": reference["identity"],
            }, length=16),
            "action": action,
            "repository": repository,
            **_authority_event_objective_context(payload),
            "authorization_basis": authorization_basis,
            "eligibility_basis": eligibility_basis,
            "writable_scope_required_for_integration": True,
            "writable_scope_observation_status": (
                "recorded" if type(writable_scope_present) is bool
                else "not-recorded-on-event"
            ),
            **({"repository_writable_scope_present": writable_scope_present}
               if type(writable_scope_present) is bool else {}),
            "persistent_claim_required": False,
            "operation_executed": False,
            "preflight_state_status": preflight_state_status,
            "coordination_only_claim_count": raw_claim_count,
            "coordination_only_claims": coordination_claims,
            "coordination_only_claims_truncated": raw_claims_truncated,
            "scope": scope,
            "interpretation": (
                "This preflight found local work eligible in an observed "
                "non-main worktree without requiring a persistent claim. "
                + (
                    "The session lacked repository write scope at this "
                    "preflight, so this was a local proposal only; merge and "
                    "publish still require writable repository scope. "
                    if proposal_only else (
                        "Repository write scope was present at this preflight, "
                        "but integration still requires a fresh scoped "
                        "preflight. "
                        if writable_scope_present is True else ""
                    )
                )
                + "It records neither an executed edit nor its outcome."
            ),
            "event_reference": reference,
        })
        proposal_eligibility_records[-1]["invocation_observation"] = (
            _proposal_invocation_observation(
                reference,
                repository,
                interactions,
                event_window_truncated=event_window_truncated,
                invocation_records_omitted=invocations_omitted,
            )
        )
        if preflight_state is not None:
            proposal_eligibility_records[-1]["proposal_preflight_state"] = (
                preflight_state
            )
            proposal_eligibility_records[-1][
                "preflight_state_interpretation_limit"
            ] = (
                "This is the local proposal checkout state at preflight; it "
                "does not establish an executed edit, verification, or outcome."
            )
    proposal_eligibility_records_omitted = max(
        0,
        valid_proposal_eligibility_count - len(proposal_eligibility_records),
    )
    for deferral in coordination_deferrals:
        deferral["later_eligibility_relation"] = (
            _coordination_deferral_follow_through(
                deferral,
                integration_records=integration_eligibility_records,
                proposal_records=proposal_eligibility_records,
            )
        )
    for candidate in coordination_deferral_candidates:
        group = coordination_deferral_candidate_groups.get(
            candidate.get("identity"), [],
        )
        relations = [
            item.get("later_eligibility_relation")
            for item in group
            if isinstance(item.get("later_eligibility_relation"), dict)
        ]
        matched_deferrals = sum(
            relation.get("status")
            == "later-same-objective-eligibility-observed"
            for relation in relations
        )
        unmatched_deferrals = sum(
            relation.get("status")
            == "no-later-match-in-returned-eligibility-records"
            for relation in relations
        )
        missing_objective_reference_deferrals = sum(
            relation.get("status") == "unavailable-exact-objective-reference"
            for relation in relations
        )
        missing_event_reference_deferrals = sum(
            relation.get("status") == "unavailable-deferral-event-reference"
            for relation in relations
        )
        supporting_preflights: dict[str, dict[str, Any]] = {}
        for relation in relations:
            matches = relation.get("matches")
            if not isinstance(matches, list):
                continue
            for match in matches:
                reference = (
                    match.get("event_reference")
                    if isinstance(match, dict) else None
                )
                if not isinstance(reference, dict):
                    continue
                identity = reference.get("identity")
                sequence = reference.get("sequence")
                if (not isinstance(identity, str)
                        or type(sequence) is not int):
                    continue
                supporting_preflights.setdefault(identity, {
                    "kind": match.get("kind"),
                    "event_reference": {
                        key: reference[key]
                        for key in ("identity", "sequence", "type")
                        if key in reference
                    },
                })
        supporting_preflight_events = sorted(
            supporting_preflights.values(),
            key=lambda item: item["event_reference"]["sequence"],
        )
        candidate["later_eligibility_summary"] = {
            "status": (
                "later-preflight-observed-for-some-deferrals"
                if matched_deferrals
                else "no-later-preflight-observed-in-returned-records"
                if unmatched_deferrals
                else "deferrals-not-correlatable-to-later-preflight"
            ),
            "deferrals_with_later_preflight_count": matched_deferrals,
            "deferrals_without_later_preflight_match_in_returned_records_count": (
                unmatched_deferrals
            ),
            "deferrals_without_exact_objective_reference_count": (
                missing_objective_reference_deferrals
            ),
            "deferrals_without_event_reference_count": (
                missing_event_reference_deferrals
            ),
            "matching_preflight_count": sum(
                relation.get("matching_preflight_count", 0)
                for relation in relations
                if type(relation.get("matching_preflight_count")) is int
            ),
            "supporting_preflight_events": supporting_preflight_events[:4],
            "supporting_preflight_events_omitted_count": max(
                0, len(supporting_preflight_events) - 4,
            ),
            "operation_execution_status": "not-recorded-by-eligibility-events",
            "causal_claim": "not-established",
            "interpretation": (
                "Counts relate only exact same-objective, repository, action, "
                "and later-sequence eligibility events in this session window. "
                "They do not establish that an operation executed, a deferral "
                "was resolved, or a coordination rule was unnecessary."
            ),
        }
    repository_ref_transition_records: list[dict[str, Any]] = []
    invalid_repository_ref_transition_event_count = 0
    invalid_repository_ref_transition_record_count = 0
    repository_ref_transitions_omitted = 0
    valid_repository_ref_transition_count = 0
    for event in reversed(repository_ref_transition_events):
        reference = _event_reference(event)
        payload = event.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        raw_count = payload.get("repository_ref_transition_count")
        raw_transitions = payload.get("repository_ref_transitions")
        raw_omitted = payload.get("repository_ref_transitions_omitted")
        if (
            reference is None
            or type(raw_count) is not int or not 0 <= raw_count <= 100_000
            or not isinstance(raw_transitions, list)
            or len(raw_transitions) > MAX_SESSION_ACTIVITY_REPOSITORY_REF_TRANSITIONS
            or type(raw_omitted) is not int or not 0 <= raw_omitted <= 100_000
            or raw_count != len(raw_transitions) + raw_omitted
        ):
            invalid_repository_ref_transition_event_count += 1
            continue
        repository_ref_transitions_omitted += raw_omitted
        for index, raw_transition in enumerate(raw_transitions):
            transition = _authority_repository_ref_transition(
                raw_transition, event_reference=reference, index=index,
            )
            if transition is None:
                invalid_repository_ref_transition_record_count += 1
                continue
            transition.update(_authority_event_objective_context(payload))
            transition["preflight_relation"] = _repository_ref_preflight_relation(
                transition,
                integration_records=integration_eligibility_records,
                proposal_records=proposal_eligibility_records,
            )
            transition["interpretation"] = (
                "Environment observed this bounded checkout state after its prior "
                "observation. An exact preflight match establishes the same "
                "checkout identity and head as a prior eligibility preimage; it "
                "does not establish that the operation caused the change."
            )
            valid_repository_ref_transition_count += 1
            if len(repository_ref_transition_records) < (
                MAX_SESSION_ACTIVITY_REPOSITORY_REF_TRANSITIONS
            ):
                repository_ref_transition_records.append(transition)
            else:
                repository_ref_transitions_omitted += 1
    if (invalid_repository_ref_transition_event_count
            or invalid_repository_ref_transition_record_count
            or repository_ref_transitions_omitted):
        partial = True
        if not status.startswith("partial"):
            status = "partial"
    if repository_ref_transition_events:
        limits.append(
            "Repository ref transition correlation uses only exact checkout "
            "identity and head preimages in the selected session event window; "
            "an absent match may be outside the bounded window."
        )
    claim_lifecycle_correlations, claim_lifecycle_correlation_source = (
        _session_claim_lifecycle_correlations(
            session,
            selected_session_id=session.session_id,
            selected_events=events,
            selected_event_count=event_count,
            selected_event_window_truncated=event_window_truncated,
            denial_records=denial_records,
            integration_eligibility_records=integration_eligibility_records,
            proposal_eligibility_records=proposal_eligibility_records,
        )
    )
    if claim_lifecycle_correlation_source["status"] != "not-applicable":
        limits.append(
            "Claim lifecycle correlations match exact repository, owner-session, "
            "and claim identities across bounded event pages; shared identity "
            "does not establish that a transition caused a denial or later preflight."
        )
    if claim_lifecycle_correlation_source["status"] == "partial":
        if not status.startswith("partial"):
            status = "partial"
        limits.append(
            "Claim lifecycle correlations use at most four bounded owner-session "
            "event pages; omitted, unreadable, or truncated pages do not establish "
            "that a matching transition is absent."
        )
    eligible_preflights_by_claim: dict[
        tuple[str, str, str], dict[str, dict[str, Any]],
    ] = {}
    eligible_preflights_by_repository_action: dict[
        tuple[str, str], dict[str, dict[str, Any]],
    ] = {}
    for eligibility in integration_eligibility_records:
        reference = eligibility.get("event_reference")
        sequence = reference.get("sequence") if isinstance(reference, dict) else None
        repository = eligibility.get("repository")
        action = eligibility.get("action")
        if (type(sequence) is not int or not isinstance(repository, str)
                or not isinstance(action, str)):
            continue
        reference_identity = str(reference.get("identity"))
        eligible_preflights_by_repository_action.setdefault(
            (repository, action), {},
        ).setdefault(reference_identity, eligibility)
        for overlap in eligibility.get("coordination_overlaps", []):
            claim_id = overlap.get("claim_id") if isinstance(overlap, dict) else None
            if not isinstance(claim_id, str):
                continue
            key = (repository, action, claim_id)
            eligible_preflights_by_claim.setdefault(key, {}).setdefault(
                reference_identity, eligibility,
            )
    for denial in denial_records:
        conflict = denial.get("claim_conflict")
        blocker = (
            conflict.get("blocking_claim") if isinstance(conflict, dict) else None
        )
        claim_id = blocker.get("claim_id") if isinstance(blocker, dict) else None
        denial_reference = denial.get("event_reference")
        denial_sequence = (
            denial_reference.get("sequence")
            if isinstance(denial_reference, dict) else None
        )
        repository = denial.get("repository")
        action = denial.get("action")
        if (not isinstance(conflict, dict) or not isinstance(claim_id, str)
                or type(denial_sequence) is not int
                or not isinstance(repository, str) or not isinstance(action, str)):
            continue
        later_preflights = [
            eligibility for eligibility in
            eligible_preflights_by_repository_action.get(
                (repository, action), {},
            ).values()
            if isinstance(eligibility.get("event_reference"), dict)
            and type(eligibility["event_reference"].get("sequence")) is int
            and eligibility["event_reference"]["sequence"] > denial_sequence
        ]
        later_preflights.sort(
            key=lambda item: item["event_reference"]["sequence"],
        )
        candidates = [
            eligibility for eligibility in eligible_preflights_by_claim.get(
                (repository, action, claim_id), {},
            ).values()
            if isinstance(eligibility.get("event_reference"), dict)
            and type(eligibility["event_reference"].get("sequence")) is int
            and eligibility["event_reference"]["sequence"] > denial_sequence
        ]
        record_window_incomplete = bool(
            integration_eligibility_records_omitted
            or invalid_integration_eligibility_event_count
        )
        record_window_limits = []
        if integration_eligibility_records_omitted:
            record_window_limits.append("valid-preflight-records-omitted")
        if invalid_integration_eligibility_event_count:
            record_window_limits.append("invalid-preflight-records-present")
        if candidates:
            followup = min(
                candidates,
                key=lambda item: item["event_reference"]["sequence"],
            )
            followup_sequence = followup["event_reference"]["sequence"]
            earlier_overlap_evidence_truncated = any(
                item.get("coordination_overlaps_truncated") is True
                and item["event_reference"]["sequence"] < followup_sequence
                for item in later_preflights
            )
            if earlier_overlap_evidence_truncated:
                record_window_limits.append("earlier-overlap-records-truncated")
            conflict["subsequent_integration_preflight"] = {
                "event_reference": followup["event_reference"],
                "eligibility_basis": followup["eligibility_basis"],
                "persistent_claim_required": False,
                "operation_executed": False,
                "relation_basis": (
                    "later-event-same-repository-action-and-claim-identity"
                ),
                "selection_basis": (
                    "earliest-matching-returned-record-in-selected-session-window"
                ),
                "search_completeness_status": (
                    "partial" if record_window_limits else "complete-for-returned-records"
                ),
                "search_completeness_limits": record_window_limits,
                "causal_resolution_status": "not-established",
            }
            continue

        truncated_later_preflights = [
            item for item in later_preflights
            if item.get("coordination_overlaps_truncated") is True
        ]
        if truncated_later_preflights:
            limited_preflight = truncated_later_preflights[0]
            overlap_count = limited_preflight.get("coordination_overlap_count", 0)
            overlap_records = limited_preflight.get("coordination_overlaps", [])
            conflict["subsequent_integration_preflight_search"] = {
                "status": "not-established-overlap-records-truncated",
                "event_reference": limited_preflight["event_reference"],
                "relation_basis": (
                    "later-event-same-repository-and-action; claim-match-unestablished"
                ),
                "coordination_overlap_count": overlap_count,
                "coordination_overlap_records_returned": len(overlap_records),
                "coordination_overlap_records_omitted": max(
                    0, overlap_count - len(overlap_records),
                ),
                "valid_preflight_records_omitted": (
                    integration_eligibility_records_omitted
                ),
                "invalid_preflight_records_present": (
                    invalid_integration_eligibility_event_count
                ),
                "causal_resolution_status": "not-established",
            }
        elif record_window_incomplete:
            conflict["subsequent_integration_preflight_search"] = {
                "status": "not-established-preflight-record-window-incomplete",
                "relation_basis": (
                    "later-event-search-limited-to-returned-valid-preflight-records"
                ),
                "valid_preflight_records_omitted": (
                    integration_eligibility_records_omitted
                ),
                "invalid_preflight_records_present": (
                    invalid_integration_eligibility_event_count
                ),
                "causal_resolution_status": "not-established",
            }
        elif later_preflights:
            conflict["subsequent_integration_preflight_search"] = {
                "status": "no-same-claim-overlap-observed-in-selected-window",
                "event_reference": later_preflights[0]["event_reference"],
                "relation_basis": (
                    "later-event-same-repository-and-action; returned-overlaps-inspected"
                ),
                "causal_resolution_status": "not-established",
            }
        else:
            conflict["subsequent_integration_preflight_search"] = {
                "status": "no-later-preflight-observed-in-selected-window",
                "relation_basis": (
                    "later-event-search-within-selected-session-window"
                ),
                "causal_resolution_status": "not-established",
            }
    integration_claim_ids = list(dict.fromkeys(
        overlap["claim_id"]
        for item in integration_eligibility_records
        for overlap in item["coordination_overlaps"]
        if isinstance(overlap.get("claim_id"), str)
    ))
    existing_integration_claim_states = {
        claim_id: current_claim_states[claim_id]
        for claim_id in integration_claim_ids
        if claim_id in current_claim_states
    }
    reused_integration_claim_lookups = sum(
        state.get("lookup_status") != "not-inspected-limit"
        for state in existing_integration_claim_states.values()
    )
    previously_uninspected_integration_claims = sum(
        state.get("lookup_status") == "not-inspected-limit"
        for state in existing_integration_claim_states.values()
    )
    integration_claim_records_for_lookup = [{
        "coordination_overlaps": [
            overlap for overlap in item["coordination_overlaps"]
            if overlap.get("claim_id") not in current_claim_states
        ],
    } for item in integration_eligibility_records]
    prior_claim_lookup_count = claim_state_lookup.get("lookup_count", 0)
    remaining_claim_lookup_limit = max(
        0, MAX_SESSION_AUTHORITY_CLAIM_LOOKUPS - prior_claim_lookup_count,
    )
    integration_claim_states, integration_claim_state_lookup = (
        _session_activity_current_claim_states(
            session,
            integration_claim_records_for_lookup,
            lookup_limit=remaining_claim_lookup_limit,
        )
    )
    current_claim_states.update(integration_claim_states)
    omitted_integration_claim_lookups = (
        integration_claim_state_lookup.get("lookup_omitted_count", 0)
        + previously_uninspected_integration_claims
    )
    if omitted_integration_claim_lookups:
        integration_claim_state_lookup["lookup_omitted_count"] = (
            omitted_integration_claim_lookups
        )
    if (integration_claim_ids
            and len(existing_integration_claim_states) == len(integration_claim_ids)
            and not integration_claim_state_lookup.get("lookup_count")):
        integration_claim_state_lookup["status"] = (
            "partial-lookup-limit"
            if previously_uninspected_integration_claims
            else "reused-prior-activity-lookups"
        )
    integration_claim_state_lookup.update({
        "claim_id_count": len(integration_claim_ids),
        "reused_activity_lookup_count": reused_integration_claim_lookups,
        "prior_activity_lookup_count": prior_claim_lookup_count,
        "activity_lookup_count": (
            prior_claim_lookup_count
            + integration_claim_state_lookup.get("lookup_count", 0)
        ),
        "activity_lookup_limit": MAX_SESSION_AUTHORITY_CLAIM_LOOKUPS,
    })
    for item in integration_eligibility_records:
        for overlap in item["coordination_overlaps"]:
            claim_id = overlap.get("claim_id")
            current_state = (
                current_claim_states.get(claim_id)
                if isinstance(claim_id, str) else None
            )
            if isinstance(current_state, dict):
                overlap["current_claim_state"] = current_state
    if ((integration_eligibility_records_omitted
         or invalid_integration_eligibility_event_count)
            and not status.startswith("partial")):
        status = "partial"
    if ((proposal_eligibility_records_omitted
         or invalid_proposal_eligibility_event_count)
            and not status.startswith("partial")):
        status = "partial"
    if ((coordination_deferrals_omitted
         or invalid_coordination_deferral_event_count)
            and not status.startswith("partial")):
        status = "partial"
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
        **({"service_readiness": service_readiness}
           if has_service_readiness else {}),
        **({"doctor_health_history": doctor_health_history}
           if has_doctor_health_history else {}),
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
            **({
                "integration_denial_preflight_state_observations_invalid": (
                    invalid_integration_denial_preflight_state_count
                ),
            } if invalid_integration_denial_preflight_state_count else {}),
            "authority_denial_selection_policy": (
                "newest-sanitized-denials-in-selected-event-window"
            ),
            **({
                "coordination_deferral_events_considered": len(
                    coordination_deferral_events,
                ),
                "coordination_deferral_records_returned": len(
                    coordination_deferrals,
                ),
                "coordination_deferral_records_omitted": (
                    coordination_deferrals_omitted
                ),
                "coordination_deferral_records_invalid": (
                    invalid_coordination_deferral_event_count
                ),
                "coordination_deferral_selection_policy": (
                    "newest-validated-coordination-deferrals-in-selected-session-window"
                ),
            } if coordination_deferral_events else {}),
            **({
                "integration_eligibility_events_considered": len(
                    integration_eligibility_events,
                ),
                "integration_eligibility_records_returned": len(
                    integration_eligibility_records,
                ),
                "integration_eligibility_records_omitted": (
                    integration_eligibility_records_omitted
                ),
                "integration_eligibility_records_invalid": (
                    invalid_integration_eligibility_event_count
                ),
                "integration_preflight_state_observations_invalid": (
                    invalid_integration_preflight_state_count
                ),
                "integration_eligibility_selection_policy": (
                    "newest-sanitized-preflight-decisions-in-selected-session-window"
                ),
            } if integration_eligibility_events else {}),
            **({
                "proposal_eligibility_events_considered": len(
                    proposal_eligibility_events,
                ),
                "proposal_eligibility_records_returned": len(
                    proposal_eligibility_records,
                ),
                "proposal_eligibility_records_omitted": (
                    proposal_eligibility_records_omitted
                ),
                "proposal_eligibility_records_invalid": (
                    invalid_proposal_eligibility_event_count
                ),
                "proposal_preflight_state_observations_invalid": (
                    invalid_proposal_preflight_state_count
                ),
                "proposal_eligibility_selection_policy": (
                    "newest-validated-proposal-preflights-in-selected-session-window"
                ),
            } if proposal_eligibility_events else {}),
            **({
                "repository_ref_transition_events_considered": len(
                    repository_ref_transition_events,
                ),
                "repository_ref_transition_records_returned": len(
                    repository_ref_transition_records,
                ),
                "repository_ref_transition_records_omitted": (
                    repository_ref_transitions_omitted
                ),
                "repository_ref_transition_records_invalid": (
                    invalid_repository_ref_transition_record_count
                ),
                "repository_ref_transition_events_invalid": (
                    invalid_repository_ref_transition_event_count
                ),
                "repository_ref_transition_selection_policy": (
                    "newest-validated-observed-ref-deltas-in-selected-session-window"
                ),
            } if repository_ref_transition_events else {}),
            **({
                "authority_claim_state_lookup": claim_state_lookup,
            } if denial_events else {}),
            **({
                "integration_claim_state_lookup": integration_claim_state_lookup,
            } if integration_eligibility_events else {}),
            **({
                "claim_recovery_events_considered": len(claim_recovery_events),
                "claim_recovery_records_returned": len(claim_recoveries),
                "claim_recovery_records_omitted": claim_recovery_records_omitted,
                "claim_recovery_records_invalid": invalid_claim_recovery_event_count,
                "claim_recovery_selection_policy": (
                    "newest-valid-recovery-events-in-selected-session-window"
                ),
            } if claim_recovery_events else {}),
            **({
                "operation_claim_recovery_events_considered": len(
                    operation_claim_recovery_events,
                ),
                "operation_claim_recovery_records_returned": len(
                    operation_claim_recoveries,
                ),
                "operation_claim_recovery_records_omitted": (
                    operation_claim_recoveries_omitted
                ),
                "operation_claim_recovery_records_invalid": (
                    invalid_operation_claim_recovery_count
                ),
                "operation_claim_recovery_selection_policy": (
                    "newest-valid-operation-recoveries-in-selected-session-window"
                ),
            } if operation_claim_recovery_events else {}),
            **({
                "claim_coordination_events_considered": len(
                    claim_coordination_events,
                ),
                "claim_coordination_records_returned": len(claim_coordination),
                "claim_coordination_records_omitted": (
                    claim_coordination_records_omitted
                ),
                "claim_coordination_records_invalid": (
                    invalid_claim_coordination_event_count
                ),
                "claim_coordination_selection_policy": (
                    "newest-sanitized-claim-transitions-in-selected-session-window"
                ),
            } if claim_coordination_events else {}),
            **({
                "claim_lifecycle_correlation": (
                    claim_lifecycle_correlation_source
                ),
            } if claim_lifecycle_correlation_source.get("status")
                != "not-applicable" else {}),
            "invocation_events_considered": len(invocation_events),
            "completion_events_considered": len(completion_events),
            **({
                "operation_deferral_events_considered": len(operation_deferral_events),
                "operation_deferral_records_returned": len(operation_deferrals),
                "operation_deferral_records_omitted": operation_deferrals_omitted,
                "operation_deferral_records_invalid": invalid_operation_deferral_count,
                "operation_deferral_selection_policy": (
                    "newest-validated-operation-deferrals-in-selected-session-window"
                ),
            } if operation_deferral_events else {}),
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
        **({
            "operation_deferral_count": valid_operation_deferral_count,
            "operation_deferrals": operation_deferrals,
        } if operation_deferral_events else {}),
        "rpc_operation_count": len(rpc_operations),
        "rpc_operations": rpc_operations,
        **({"rpc_operation_summary": rpc_operation_summary} if rpc_events else {}),
        "rpc_failure_candidates": rpc_failure_candidates,
        "authority_denial_count": len(authority_denials),
        "authority_denials": authority_denials,
        "authority_denial_candidates": authority_denial_candidates,
        **({
            "coordination_deferral_count": len(coordination_deferrals),
            "coordination_deferrals": coordination_deferrals,
            "coordination_deferral_candidate_count": len(
                coordination_deferral_candidates,
            ),
            "coordination_deferral_candidates": (
                coordination_deferral_candidates
            ),
        } if coordination_deferral_events else {}),
        **({
            "integration_eligibility_count": len(
                integration_eligibility_records,
            ),
            "integration_eligibility": integration_eligibility_records,
        } if integration_eligibility_events else {}),
        **({
            "proposal_eligibility_count": len(proposal_eligibility_records),
            "proposal_eligibility": proposal_eligibility_records,
        } if proposal_eligibility_events else {}),
        **({
            "repository_ref_transition_count": (
                valid_repository_ref_transition_count
                + repository_ref_transitions_omitted
            ),
            "repository_ref_transitions": repository_ref_transition_records,
        } if repository_ref_transition_events else {}),
        **({
            "claim_recovery_count": len(claim_recoveries),
            "claim_recoveries": claim_recoveries,
        } if claim_recovery_events else {}),
        **({
            "operation_claim_recovery_count": len(operation_claim_recoveries),
            "operation_claim_recoveries": operation_claim_recoveries,
        } if operation_claim_recovery_events else {}),
        **({
            "claim_coordination_count": len(claim_coordination),
            "claim_coordination": claim_coordination,
        } if claim_coordination_events else {}),
        **({
            "claim_lifecycle_correlations": claim_lifecycle_correlations,
        } if claim_lifecycle_correlations else {}),
        "capabilities": capabilities,
        "capability_delivery": capability_delivery,
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
    and exact allowlisted Compiler evidence families are read. Their exact Git
    commit and SHA-256 must be present in the authenticated campaign ledger.
    Exact allowlisted Compiler regression reports and the native application
    proof may also be read through the Environment's bounded
    workspace-read-only authority when Compiler is not selected.
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
        for page_entry_index, raw in enumerate(page_references):
            page_entries.append((raw, page_index, page_entry_index))
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
    if len(history_states) > 1:
        page_offsets = [page.get("evidence_offset") for page in history_states]
        if all(type(offset) is int and offset >= 0 for offset in page_offsets):
            # History pages are fetched newest-first, but append-only
            # corrections must be evaluated after their earlier targets.
            # Process the bounded selected references in ledger order while
            # retaining each page's independent read-budget selection.
            page_entries.sort(key=lambda entry: (
                page_offsets[entry[1]] + entry[2], entry[1],
            ))
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
    contract_invalid_records: dict[
        str, tuple[dict[str, Any], dict[str, Any]]
    ] = {}
    seen_observations: set[tuple[str, str, str, str, str]] = set()
    unavailable = 0
    skipped_by_page_budget = 0
    read_deadline = time.monotonic() + MAX_OBSERVATION_READ_SECONDS
    campaign_charter = _campaign_charter_context(
        session, state, root,
        campaign_identity=campaign.get("identity"),
        deadline=read_deadline,
    )
    for raw, page_index, _page_entry_index in page_entries:
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
        if (
            verification == "verified"
            and isinstance(record, dict)
            and record.get("_projection_repository_read_basis")
            == "workspace-read-only-checkout"
        ):
            reference["repository_read_basis"] = (
                "workspace-read-only-checkout"
            )
        if (verification == "observation-identity-mismatch"
                and isinstance(record, dict)
                and isinstance(record.get("identity"), str)
                and record["identity"]):
            reference["record_identity"] = record["identity"][:160]
        contract_diagnostics = (
            record.get("_projection_contract_diagnostics")
            if isinstance(record, dict) else None
        )
        if verification == "observation-contract-invalid" and isinstance(
            contract_diagnostics, list,
        ):
            reference["contract_diagnostics"] = [
                _short_text(value, 128) for value in contract_diagnostics[:8]
                if isinstance(value, str)
            ]
            reference["contract_diagnostics_truncated"] = (
                len(contract_diagnostics) > 8
            )
        references.append(reference)
        if record is not None and verification == "observation-contract-invalid":
            record_identity = record.get("identity")
            if isinstance(record_identity, str) and record_identity == pointer["identity"]:
                contract_invalid_records[record_identity] = (record, reference)
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
        diagnostic = {
            key: reference[key]
            for key in (
                "identity", "record_identity", "repository", "commit",
                "sha256", "verification",
            )
            if reference.get(key) is not None
        } | {
            "path": path[:512],
            "path_truncated": len(path) > 512,
        }
        for key in ("correction_status", "correction_observation_identity"):
            if isinstance(reference.get(key), str):
                diagnostic[key] = reference[key]
        contract_diagnostics = reference.get("contract_diagnostics")
        if isinstance(contract_diagnostics, list):
            diagnostic["contract_diagnostics"] = contract_diagnostics[:8]
            diagnostic["contract_diagnostics_truncated"] = (
                reference.get("contract_diagnostics_truncated") is True
            )
        observation_reference_diagnostics.append(diagnostic)
    source_integrity: dict[tuple[str, str, str, str], str] = {}
    source_payloads: dict[tuple[str, str, str, str], bytes] = {}
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
                source_payloads=source_payloads,
            )
        improvement = _validate_improvement_relation(record.get("improvement_relation"))
        if improvement is not None:
            _verify_source_pointer(
                session, improvement["implementation_reference"], references, root,
                deadline=read_deadline, cache=source_integrity,
                attempted=attempted_sources,
                workspace_checkouts=workspace_checkouts,
                source_payloads=source_payloads,
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
                source_payloads=source_payloads,
            )
            for source in correction["replacement_values"].get(
                "supporting_sources", [],
            ):
                _verify_source_pointer(
                    session, source, references, root,
                    deadline=read_deadline, cache=source_integrity,
                    attempted=attempted_sources,
                    workspace_checkouts=workspace_checkouts,
                    source_payloads=source_payloads,
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
        target_is_contract_invalid = (
            verification == "observation-contract-invalid"
            and target_identity in contract_invalid_records
        )
        if verification != "verified" and not target_is_contract_invalid:
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
            invalid_target = contract_invalid_records.get(target_identity)
            if invalid_target is not None:
                target_record = invalid_target[0]
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
            if field.startswith("observation."):
                observation = target_record.get("observation")
                current = (
                    observation.get(field.partition(".")[2])
                    if isinstance(observation, dict) else None
                )
                if not target_is_contract_invalid or isinstance(current, dict):
                    replacements_verified = False
                    break
            elif field.startswith("pressure_hypothesis."):
                hypothesis = target_record.get("pressure_hypothesis")
                current = (
                    hypothesis.get(field.partition(".")[2])
                    if isinstance(hypothesis, dict) else None
                )
                invalid_pressure_value = (
                    not isinstance(current, str)
                    or not current
                    or len(current) > 512
                )
                if not target_is_contract_invalid or not invalid_pressure_value:
                    replacements_verified = False
                    break
            else:
                current = target_record.get(field)
            if current == replacement:
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
                    and (
                        (source_status := _source_status(
                            source, references,
                        ).get("status")) in {
                            "verified", "not-an-environmental-observation",
                            "not-present-in-selected-campaign-evidence-window",
                        }
                        or (
                            source_status == "outside-selected-session"
                            and not _is_observation_reference(pointer)
                        )
                    )
                    and _source_integrity_status(source, source_integrity)
                    in SOURCE_INTEGRITY_VERIFIED
                    for source in replacement_sources
                )
            )
            if not replacement_sources_valid:
                replacements_verified = False
                break
        if replacements_verified and target_is_contract_invalid:
            corrected_record = {
                **target_record,
                "observation": {
                    **(target_record.get("observation")
                       if isinstance(target_record.get("observation"), dict)
                       else {}),
                    **{
                        field.partition(".")[2]: replacement_values[field]
                        for field in correction["corrected_field_paths"]
                        if field.startswith("observation.")
                    },
                },
                "pressure_hypothesis": {
                    **(target_record.get("pressure_hypothesis")
                       if isinstance(target_record.get("pressure_hypothesis"), dict)
                       else {}),
                    **{
                        field.partition(".")[2]: replacement_values[field]
                        for field in correction["corrected_field_paths"]
                        if field.startswith("pressure_hypothesis.")
                    },
                },
            }
            if _observation_contract_diagnostics(corrected_record):
                replacements_verified = False
        if not replacements_verified:
            correction_status_by_record[correction_record["identity"]] = {
                "status": (
                    "replacement-does-not-complete-observation-contract"
                    if target_is_contract_invalid
                    else "replacement-values-unverified-or-unchanged"
                ),
                "target_identity": target_identity,
                "target_reference": target_reference,
            }
            unverified_record_correction_count += 1
            continue
        correction_status_by_record[correction_record["identity"]] = {
            "status": (
                "applied-to-integrity-verified-contract-invalid-target"
                if target_is_contract_invalid
                else "applied-to-verified-target"
            ),
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

    for target_identity, (correction_record, correction, _correction_reference) in (
        list(correction_by_target.items())
    ):
        invalid_target = contract_invalid_records.get(target_identity)
        if invalid_target is None:
            continue
        target_record, target_reference = invalid_target
        corrected_record = {
            **target_record,
            "observation": {
                **(target_record.get("observation")
                   if isinstance(target_record.get("observation"), dict) else {}),
                **{
                    field.partition(".")[2]: correction["replacement_values"][field]
                    for field in correction["corrected_field_paths"]
                    if field.startswith("observation.")
                },
            },
            "pressure_hypothesis": {
                **(target_record.get("pressure_hypothesis")
                   if isinstance(target_record.get("pressure_hypothesis"), dict)
                   else {}),
                **{
                    field.partition(".")[2]: correction["replacement_values"][field]
                    for field in correction["corrected_field_paths"]
                    if field.startswith("pressure_hypothesis.")
                },
            },
        }
        corrected_record.pop("_projection_contract_diagnostics", None)
        if _observation_contract_diagnostics(corrected_record):
            continue
        if len(projected_records) >= MAX_OBSERVATIONS:
            correction_by_target.pop(target_identity, None)
            correction_status_by_record[correction_record["identity"]] = {
                "status": "corrected-target-projection-limit-reached",
                "target_identity": target_identity,
            }
            unverified_record_correction_count += 1
            continue
        projected_records.append((corrected_record, target_reference))
        projected_records_by_identity[target_identity] = corrected_record
        target_reference["correction_status"] = (
            "completed-by-append-only-record"
        )
        target_reference["correction_observation_identity"] = (
            correction_record["identity"]
        )
        for diagnostic in observation_reference_diagnostics:
            if diagnostic.get("identity") == target_identity:
                diagnostic["correction_status"] = (
                    "completed-by-append-only-record"
                )
                diagnostic["correction_observation_identity"] = (
                    correction_record["identity"]
                )
        unavailable = max(0, unavailable - 1)

    unverified_improvement_relation_count = sum(
        record.get("improvement_relation") is not None
        and _validate_improvement_relation(record.get("improvement_relation")) is None
        for record, _ in projected_records
    )
    source_context_candidates: dict[
        tuple[str, str, str, str, str], set[tuple[tuple[str, str], ...]],
    ] = {}
    for source_record, _ in projected_records:
        source_observation = source_record.get("observation")
        source_observation = (
            source_observation if isinstance(source_observation, dict) else {}
        )
        source_context = source_observation.get("source_session_context")
        source_context = source_context if isinstance(source_context, dict) else {}
        supporting_sources = source_record.get("supporting_sources")
        if not isinstance(supporting_sources, list):
            continue
        campaign_id = source_context.get("campaign_id")
        session_id = source_context.get("session_id")
        if (not isinstance(campaign_id, str) or not campaign_id
                or not isinstance(session_id, str) or not session_id
                or source_context.get("repository") != "mncs-compiler"):
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
                "definition_id", "repository_branch", "repository_head",
                "repository_branch_capture_status",
                "repository_head_capture_status",
            ):
                value = source_context.get(key)
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
        effective_pressure_hypothesis_role = record.get(
            "pressure_hypothesis_role", "occurrence",
        )
        if correction_for_target is not None:
            correction_record, correction, correction_reference = correction_for_target
            corrected_fields = correction["corrected_field_paths"]
            replacement_values = correction["replacement_values"]
            if "supporting_sources" in corrected_fields:
                effective_sources = replacement_values["supporting_sources"]
            if "not_measured" in corrected_fields:
                effective_measurement_limits = replacement_values["not_measured"]
            if "pressure_hypothesis_role" in corrected_fields:
                effective_pressure_hypothesis_role = replacement_values[
                    "pressure_hypothesis_role"
                ]
            if record_correction_summary is None:
                record_correction_summary = {
                    "status": (
                        "completed-by-append-only-record"
                        if record["identity"] in contract_invalid_records
                        else "corrected-by-verified-record"
                    ),
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
        repository_read_basis = reference.get("repository_read_basis")
        if repository_read_basis in {
            "selected-checkout", "workspace-read-only-checkout",
        }:
            projected_observation["source_repository_read_basis"] = (
                repository_read_basis
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
            **({
                "pressure_hypothesis_role": effective_pressure_hypothesis_role,
            } if hypothesis is not None else {}),
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
        workspace_read_only_observation_references=[
            {
                key: reference[key]
                for key in (
                    "identity", "repository", "commit", "path", "sha256",
                    "verification", "repository_read_basis",
                )
            }
            for reference in references
            if reference.get("verification") == "verified"
            and reference.get("repository_read_basis")
            == "workspace-read-only-checkout"
        ],
        diagnostics_truncated=(
            len(observation_reference_diagnostics) > MAX_OBSERVATIONS
        ),
        integration_eligibility=activity.get("integration_eligibility"),
        activity_source=activity.get("source"),
    )
    implementation_accountability = _implementation_accountability_projection(
        observations,
        native_proof_reference_present=any(
            _is_compiler_native_application_proof_reference(reference)
            for reference in references
        ),
        improvement_relations=improvement_relations,
        selected_session_capability_delivery=activity.get(
            "capability_delivery",
        ),
        selected_session_id=session.session_id,
        owner_audit_source_payloads=source_payloads,
        source_integrity=source_integrity,
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
            "session_capability_delivery_digest": (
                digest_hex(activity["capability_delivery"])
                if isinstance(activity.get("capability_delivery"), dict)
                else None
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
            "session_capability_delivery_digest": (
                digest_hex(activity["capability_delivery"])
                if isinstance(activity.get("capability_delivery"), dict)
                else None
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
        ) + len(activity.get("authority_denial_candidates", [])) + len(
            activity.get("coordination_deferral_candidates", [])
        ) + (
            activity.get("service_readiness", {})
            .get("summary", {})
            .get("investigation_candidate_count", 0)
            if isinstance(activity.get("service_readiness"), dict) else 0
        ),
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
    if implementation_accountability is not None:
        projection["implementation_accountability"] = implementation_accountability
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
