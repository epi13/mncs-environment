"""Bounded evidence links between owner audits and native API observations.

The projection joins existing Environment evidence in memory. It creates no
second registry and treats exact source pointers as traceability, not as a
causal or migration finding.
"""

from __future__ import annotations

import json
import re
from typing import Any

SCHEMA = "mncs.environment.capability-accountability-links/2"
MAX_LINKS = 4
MAX_RESULT_BYTES = 8 * 1024
MAX_RESULT_RESERVE_BYTES = MAX_RESULT_BYTES
MAX_UNMATCHED_OWNER_PRESSURE_REFERENCES = 2
MAX_UNMATCHED_PRESSURE_REFERENCES = 1
VERIFIED_SOURCE_STATUSES = frozenset({
    "content-integrity-verified",
    "workspace-read-only-content-integrity-verified",
})


def _text(value: Any, limit: int = 192) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def _pointer_key(value: Any) -> tuple[str, str, str, str] | None:
    if not isinstance(value, dict):
        return None
    repository = value.get("repository")
    commit = value.get("commit")
    path = value.get("path")
    sha256 = value.get("sha256")
    if (
        not isinstance(repository, str)
        or not repository
        or len(repository) > 120
        or not isinstance(commit, str)
        or not re.fullmatch(r"[0-9a-f]{40,64}", commit)
        or not isinstance(path, str)
        or not path
        or len(path) > 1024
        or path.startswith("/")
        or ".." in path.split("/")
        or not isinstance(sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", sha256)
    ):
        return None
    return repository, commit, path, sha256


def _reference(value: Any) -> dict[str, str] | None:
    """Return the integrity address, with bounded descriptive identity."""
    if _pointer_key(value) is None:
        return None
    assert isinstance(value, dict)
    result = {
        key: value[key][:limit]
        for key, limit in (
            ("identity", 128), ("repository", 120), ("commit", 64),
            ("path", 256), ("sha256", 64),
        )
        if isinstance(value.get(key), str)
    }
    return result


def _owner_audit_context(audit: dict[str, Any], repository: str) -> dict[str, Any]:
    projects = audit.get("projects")
    projects = projects if isinstance(projects, list) else []
    project = next((item for item in projects
                    if isinstance(item, dict)
                    and item.get("repository") == repository), None)
    result: dict[str, Any] = {"repository": repository[:120]}
    if isinstance(project, dict):
        for source_key, target_key in (
            ("owner_declared_status", "owner_declared_status"),
        ):
            value = _text(project.get(source_key), 160)
            if value is not None:
                result[target_key] = value
        responsibilities = project.get("remaining_host_responsibilities")
        if isinstance(responsibilities, list):
            result["remaining_host_responsibilities"] = [
                item[:128]
                for item in responsibilities[:3]
                if isinstance(item, str)
            ]
            result["remaining_host_responsibilities_truncated"] = (
                len(responsibilities) > 3
            )
    details = audit.get("source_audit_details")
    details = details if isinstance(details, list) else []
    detail = next((item for item in details
                   if isinstance(item, dict)
                   and item.get("repository") == repository), None)
    if isinstance(detail, dict):
        status = _text(detail.get("source_record_status"), 128)
        if status is not None:
            result["source_record_status"] = status
        responsibilities = detail.get("remaining_host_responsibilities")
        if isinstance(responsibilities, list):
            compact = []
            for item in responsibilities[:1]:
                if not isinstance(item, dict):
                    continue
                entry = {
                    key: value[:96]
                    for key, value in item.items()
                    if key in {
                        "area", "classification", "eliminating_capability",
                    }
                    and isinstance(value, str)
                }
                if entry:
                    compact.append(entry)
            result["source_declared_host_responsibilities"] = compact
            result["source_declared_host_responsibilities_truncated"] = (
                len(responsibilities) > 1
            )
    return result


def _verified(status: Any) -> bool:
    return isinstance(status, str) and status in VERIFIED_SOURCE_STATUSES


def _correspondence_links(correspondence: dict[str, Any]):
    pressure_links = correspondence.get("pressure_source_links")
    pressure_links = pressure_links if isinstance(pressure_links, dict) else {}
    raw_links = pressure_links.get("links")
    raw_links = raw_links if isinstance(raw_links, list) else []
    for pressure_link in raw_links[:4]:
        if isinstance(pressure_link, dict):
            yield pressure_link


def _native_implementation_sources(
    correspondence: dict[str, Any],
) -> list[dict[str, Any]]:
    """Expose one bounded, integrity-addressed source per native evidence role."""
    role_evidence = correspondence.get("source_evidence_by_role")
    if not isinstance(role_evidence, dict):
        return []
    result = []
    for role in ("language_contract", "compiler_support", "host_runtime_provider"):
        evidence = role_evidence.get(role)
        if not isinstance(evidence, dict):
            continue
        raw_references = evidence.get("source_references")
        raw_references = raw_references if isinstance(raw_references, list) else []
        reference = _reference(raw_references[0]) if raw_references else None
        if reference is None:
            continue
        count = evidence.get("source_reference_count")
        result.append({
            "role": role,
            "source_status": _text(evidence.get("status"), 96) or "unavailable",
            "source_reference_count": (
                count if type(count) is int and count >= 0 else len(raw_references)
            ),
            "source_reference": reference,
            "additional_references_not_included": (
                len(raw_references) > 1
                or (type(count) is int and count > 1)
            ),
        })
    return result


def _diagnostic_reference(value: Any) -> dict[str, str] | None:
    reference = _reference(value)
    if reference is None or not isinstance(value, dict):
        return None
    for key in ("kind", "status", "content_integrity_status"):
        text = _text(value.get(key), 128)
        if text is not None:
            reference[key] = text
    return reference


def _pointer_group(values: list[Any], limit: int) -> dict[str, Any]:
    references: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    reference_count = 0
    for value in values:
        key = _pointer_key(value)
        reference = _diagnostic_reference(value)
        if key is None or reference is None or key in seen:
            continue
        seen.add(key)
        reference_count += 1
        if len(references) < limit:
            references.append(reference)
    return {
        "reference_count": reference_count,
        "references": references,
        "references_truncated": reference_count > len(references),
    }


def _unmatched_source_pointer_context(
    audits: list[Any], correspondences: list[Any], observations: list[Any],
) -> dict[str, Any] | None:
    """Expose both sides of an absent exact link without implying a relation."""
    owner_pressure_sources: list[Any] = []
    for audit in audits[:4]:
        if not isinstance(audit, dict):
            continue
        sources = audit.get("supporting_source_references")
        sources = sources if isinstance(sources, list) else []
        for source in sources[:8]:
            if (isinstance(source, dict)
                    and isinstance(source.get("kind"), str)
                    and "pressure" in source["kind"].lower()):
                owner_pressure_sources.append(source)

    native_pressure_sources: list[Any] = []
    for correspondence in correspondences[:4]:
        if not isinstance(correspondence, dict):
            continue
        for pressure_link in _correspondence_links(correspondence):
            if isinstance(pressure_link, dict):
                native_pressure_sources.append(
                    pressure_link.get("source_reference"),
                )

    correction_pressure_sources: list[Any] = []
    for observation in observations[:16]:
        if not isinstance(observation, dict):
            continue
        sources = observation.get("supporting_sources")
        sources = sources if isinstance(sources, list) else []
        correction_pressure_sources.extend(sources[:8])

    native_keys = {
        key for value in native_pressure_sources
        if (key := _pointer_key(value)) is not None
    }
    correction_pressure_sources = [
        value for value in correction_pressure_sources
        if _pointer_key(value) not in native_keys
    ]
    groups = {
        "owner_audit_pressure_sources": _pointer_group(
            owner_pressure_sources, MAX_UNMATCHED_OWNER_PRESSURE_REFERENCES,
        ),
        "native_capability_pressure_sources": _pointer_group(
            native_pressure_sources, MAX_UNMATCHED_PRESSURE_REFERENCES,
        ),
        "correction_pressure_sources": _pointer_group(
            correction_pressure_sources, MAX_UNMATCHED_PRESSURE_REFERENCES,
        ),
    }
    if not any(group["reference_count"] for group in groups.values()):
        return None
    return {
        "status": "no-exact-shared-source-pointer",
        "comparison_basis": "repository+commit+path+sha256",
        "causal_relation_status": "not-established",
        **groups,
        "next_investigation": (
            "Inspect the integrity-addressed source records and establish whether they "
            "describe the same responsibility; record a provenance relation only when "
            "the source evidence supports it."
        ),
    }


def _session_context_delivery_observation(
    session_activity: Any, subject: Any,
) -> dict[str, Any] | None:
    """Expose bounded, session-level context delivery evidence.

    The context summary is evidence that a response was captured and can be
    compared with the current binding snapshot. Unless exact binding IDs are
    present, it cannot establish delivery of a particular capability, receipt
    by an agent, or subsequent use.
    """
    activity = session_activity if isinstance(session_activity, dict) else {}
    delivery = activity.get("capability_delivery")
    delivery = delivery if isinstance(delivery, dict) else {}
    context = delivery.get("context_operation_activity")
    if not isinstance(context, dict):
        return None

    observation: dict[str, Any] = {
        "scope": _text(context.get("scope"), 128) or "unavailable",
        "status": _text(context.get("status"), 96) or "unavailable",
        "agent_receipt_status": (
            _text(context.get("agent_receipt_status"), 96)
            or "not-established"
        ),
        "target_capability_attribution_status": "not-established",
        "binding_inclusion_projection_path": (
            "session_activity.capability_delivery.bindings[*].context_response_inclusion"
        ),
    }
    subject = subject if isinstance(subject, dict) else {}
    for key in ("session_id", "campaign_id", "environment_id"):
        value = _text(subject.get(key), 128)
        if value is not None:
            observation[key] = value
    work_intent = subject.get("work_intent")
    if isinstance(work_intent, dict):
        intent = {
            key: _text(work_intent.get(key), limit)
            for key, limit in (("identity", 128), ("goal", 192))
            if isinstance(work_intent.get(key), str)
        }
        if intent:
            observation["work_intent"] = intent

    source = activity.get("source")
    if isinstance(source, dict):
        window = {
            key: source[key]
            for key in (
                "event_window_sequence_start", "event_window_sequence_end",
                "event_page_before_sequence", "event_window_truncated",
            )
            if type(source.get(key)) in {int, bool}
        }
        if window:
            observation["selected_event_window"] = window

    raw_summaries = context.get("response_summaries")
    raw_summaries = raw_summaries if isinstance(raw_summaries, list) else []
    summaries: list[dict[str, Any]] = []
    for raw in raw_summaries[:1]:
        if not isinstance(raw, dict):
            continue
        summary: dict[str, Any] = {}
        for key in (
            "schema_version", "capture_status", "capture_reason",
            "context_schema_version", "identity_digest_status",
            "available_identity_digest", "binding_ids_status",
        ):
            value = _text(raw.get(key), 128)
            if value is not None:
                summary[key] = value
        for key in (
            "available_count", "listed_available_count", "unavailable_count",
            "unknown_count",
        ):
            value = raw.get(key)
            if type(value) is int and 0 <= value <= 1_000_000:
                summary[key] = value
        if type(raw.get("capabilities_truncated")) is bool:
            summary["capabilities_truncated"] = raw["capabilities_truncated"]
        binding_ids = raw.get("available_binding_ids")
        if isinstance(binding_ids, list):
            summary["available_binding_ids"] = [
                value[:80] for value in binding_ids[:20]
                if isinstance(value, str)
            ]
            if len(binding_ids) > 20:
                summary["available_binding_ids_truncated"] = True
        event_reference = raw.get("event_reference")
        if isinstance(event_reference, dict):
            reference: dict[str, Any] = {}
            for key in ("identity", "type"):
                value = _text(event_reference.get(key), 128)
                if value is not None:
                    reference[key] = value
            if type(event_reference.get("sequence")) is int:
                reference["sequence"] = event_reference["sequence"]
            if reference:
                summary["event_reference"] = reference
        comparison = raw.get("binding_snapshot_comparison")
        if isinstance(comparison, dict):
            compact_comparison = {
                key: _text(comparison.get(key), 128)
                for key in ("status", "basis", "reason")
                if isinstance(comparison.get(key), str)
            }
            for key in (
                "available_count_matches", "unavailable_count_matches",
                "unknown_count_matches", "available_identity_digest_matches",
            ):
                if type(comparison.get(key)) is bool:
                    compact_comparison[key] = comparison[key]
            if compact_comparison:
                summary["binding_snapshot_comparison"] = compact_comparison
        summaries.append(summary)
    observation["response_summaries"] = summaries
    summary_count = max(0, len(raw_summaries))
    omitted = context.get("response_summaries_omitted")
    omitted = max(0, omitted) if type(omitted) is int else 0
    omitted += max(0, summary_count - len(summaries))
    observation["response_summaries_omitted"] = omitted
    if summary_count > len(summaries):
        observation["response_summaries_truncated"] = True
    no_response_in_window = (
        not summaries
        and observation["status"] == "not-observed-in-selected-event-window"
    )
    captured_summaries = [
        item for item in summaries if item.get("capture_status") == "captured"
    ]
    summary_capture_incomplete = bool(summaries) and not captured_summaries
    partial_binding_ids = bool(captured_summaries) and any(
        item.get("binding_ids_status") != "complete"
        for item in captured_summaries
    )
    if partial_binding_ids:
        interpretation_status = "partial-binding-id-attribution"
        next_investigation = (
            "Inspect the next authentic Control session.context event for exact binding IDs "
            "and compare it with the binding-level inclusion view; keep agent receipt and use "
            "as separate observations."
        )
    elif captured_summaries:
        interpretation_status = "binding-identifiers-captured-receipt-unestablished"
        next_investigation = (
            "Correlate the captured binding-ID set with subsequent authenticated invocation "
            "events to investigate use; do not infer agent receipt from delivery alone."
        )
    elif summary_capture_incomplete:
        interpretation_status = "response-summary-unavailable-or-incomplete"
        next_investigation = (
            "Inspect the underlying authenticated event and capture-status records before "
            "assessing capability discoverability; the available summary does not establish "
            "whether a context response was captured."
        )
    elif no_response_in_window:
        interpretation_status = "no-response-summary-in-selected-window"
        next_investigation = (
            "Inspect an event window containing session.context activity before assessing "
            "capability discoverability; absence from this selected window is not evidence "
            "that no context response occurred."
        )
    else:
        interpretation_status = "response-summary-unavailable-or-incomplete"
        next_investigation = (
            "Inspect the underlying authenticated event and capture-status records before "
            "assessing capability discoverability; the available summary does not establish "
            "whether a context response was captured."
        )
    observation["interpretation"] = {
        "status": interpretation_status,
        "basis": "bounded authenticated session.context response summaries",
        "next_investigation": next_investigation,
    }
    return observation


def project(
    implementation_accountability: Any,
    observations: Any = None,
    *,
    session_activity: Any = None,
    subject: Any = None,
) -> dict[str, Any]:
    """Join owner audits and native capabilities using exact evidence pointers.

    A correction observation can bridge an audit's immutable evidence pointer
    to a pressure pointer. Its recorded status is surfaced so this projection
    does not imply that the correction was applied to the original record.
    """
    accountability = (
        implementation_accountability
        if isinstance(implementation_accountability, dict) else {}
    )
    owner_evidence = accountability.get("owner_audit_evidence")
    owner_evidence = owner_evidence if isinstance(owner_evidence, dict) else {}
    audits = owner_evidence.get("audits")
    audits = audits if isinstance(audits, list) else []
    correspondences = accountability.get("native_capability_correspondences")
    correspondences = correspondences if isinstance(correspondences, list) else []
    observations = observations if isinstance(observations, list) else []

    links: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    total_matches = 0
    unverified_matches = 0
    overflow = False

    def record_match(
        *, audit: dict[str, Any], correspondence: dict[str, Any],
        pressure_link: dict[str, Any], pointer: Any,
        observed_source_status: Any, relationship_kind: str,
        correction: dict[str, Any] | None = None,
    ) -> None:
        nonlocal total_matches, unverified_matches, overflow
        pointer_key = _pointer_key(pointer)
        if pointer_key is None:
            return
        audit_identity = _text(audit.get("identity"), 128) or "unknown-audit"
        correspondence_identity = (
            _text(correspondence.get("identity"), 128)
            or "unknown-correspondence"
        )
        correction_identity = (
            _text(correction.get("identity"), 128) if correction else None
        )
        identity_key = (
            audit_identity, correspondence_identity, correction_identity,
            pointer_key,
        )
        if identity_key in seen:
            return
        seen.add(identity_key)
        total_matches += 1

        pressure_status = pressure_link.get(
            "source_content_integrity_status",
            pointer.get("content_integrity_status")
            if isinstance(pointer, dict) else None,
        )
        integrity_verified = (
            _verified(observed_source_status) and _verified(pressure_status)
        )
        if not integrity_verified:
            unverified_matches += 1

        reference = _reference(pointer)
        if reference is None:
            return
        link: dict[str, Any] = {
            "relationship_kind": relationship_kind,
            "owner_audit_identity": audit_identity,
            "native_correspondence_identity": correspondence_identity,
            "target_pressure_identity": (
                _text(correspondence.get("target_pressure_identity"), 128)
                or "unavailable"
            ),
            "native_capability_identity": (
                _text(correspondence.get("native_capability_identity"), 128)
                or "unavailable"
            ),
            "native_capability_source_evidence_status": (
                _text(correspondence.get("source_evidence_status"), 128)
                or "unavailable"
            ),
            "native_implementation_source_references": (
                _native_implementation_sources(correspondence)
            ),
            "shared_pressure_source_reference": reference,
            "pressure_source_integrity_status": (
                "verified-in-both-linked-observations"
                if integrity_verified else "not-verified-in-both-linked-observations"
            ),
            "owner_audit_context": _owner_audit_context(
                audit, pointer_key[0],
            ),
            "responsibility_match_status": "not-established",
            "causal_relation_status": "not-established",
            "native_migration_readiness": "not-established",
            "agent_usage_status": "not-measured",
            "next_investigation": (
                _text(correspondence.get("next_investigation"), 192)
                or "unavailable"
            ),
        }
        if correction is not None:
            correction_record = correction.get("record_correction")
            correction_record = (
                correction_record if isinstance(correction_record, dict) else {}
            )
            owner_reference = _reference(audit.get("evidence_reference"))
            correction_reference = _reference(correction.get("evidence_reference"))
            owner_key = _pointer_key(audit.get("evidence_reference"))
            target_key = _pointer_key(correction_record.get("target_reference"))
            link.update({
                "owner_audit_reference": owner_reference,
                "correction_observation_reference": correction_reference,
                "audit_correction_target_match_status": (
                    "exact-reference-match"
                    if owner_reference is not None and owner_key == target_key
                    else "not-established"
                ),
                "correction_record_status": (
                    _text(correction_record.get("status"), 128)
                    or "unavailable"
                ),
                "improvement_relation_status": (
                    _text(correction.get("improvement_relation_status"), 96)
                    or "unavailable"
                ),
            })
        if len(links) < MAX_LINKS:
            links.append(link)
        else:
            overflow = True

    for audit in audits[:4]:
        if not isinstance(audit, dict):
            continue
        audit_sources = audit.get("supporting_source_references")
        audit_sources = audit_sources if isinstance(audit_sources, list) else []
        source_by_key: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for source in audit_sources[:8]:
            key = _pointer_key(source)
            if key is not None and isinstance(source, dict):
                source_by_key.setdefault(key, source)

        audit_reference_key = _pointer_key(audit.get("evidence_reference"))
        corrections = []
        for observation in observations[:16]:
            if not isinstance(observation, dict):
                continue
            correction_record = observation.get("record_correction")
            correction_record = (
                correction_record if isinstance(correction_record, dict) else {}
            )
            target_reference = correction_record.get("target_reference")
            if (
                audit_reference_key is not None
                and _pointer_key(target_reference) == audit_reference_key
            ):
                corrections.append(observation)

        for correspondence in correspondences[:4]:
            if not isinstance(correspondence, dict):
                continue
            for pressure_link in _correspondence_links(correspondence):
                source_reference = pressure_link.get("source_reference")
                pointer_key = _pointer_key(source_reference)
                if pointer_key is not None and pointer_key in source_by_key:
                    audit_source = source_by_key[pointer_key]
                    record_match(
                        audit=audit, correspondence=correspondence,
                        pressure_link=pressure_link, pointer=source_reference,
                        observed_source_status=audit_source.get(
                            "content_integrity_status",
                        ), relationship_kind="exact-shared-source-pointer",
                    )

                for correction in corrections[:4]:
                    correction_sources = correction.get("supporting_sources")
                    correction_sources = (
                        correction_sources
                        if isinstance(correction_sources, list) else []
                    )
                    for correction_source in correction_sources[:8]:
                        if _pointer_key(correction_source) != pointer_key:
                            continue
                        record_match(
                            audit=audit, correspondence=correspondence,
                            pressure_link=pressure_link,
                            pointer=source_reference,
                            observed_source_status=correction_source.get(
                                "content_integrity_status",
                            ) if isinstance(correction_source, dict) else None,
                            relationship_kind=(
                                "audit-correction-to-native-source-pointer"
                            ),
                            correction=correction,
                        )

    result: dict[str, Any] = {
        "schema_version": SCHEMA,
        "status": "source-context-unavailable",
        "relationship_kind": "integrity-addressed-source-pointer-and-correction-chain",
        "link_count": len(links),
        "exact_shared_pointer_match_count": total_matches,
        "unverified_shared_pointer_match_count": unverified_matches,
        "links_truncated": overflow or total_matches > len(links),
        "links": links,
        "unmatched_source_pointer_context": (
            _unmatched_source_pointer_context(
                audits, correspondences, observations,
            )
            if total_matches == 0 else None
        ),
        "session_context_delivery_observation": (
            _session_context_delivery_observation(session_activity, subject)
        ),
        "systemwide_native_adoption_ratio": {"status": "not-measured"},
        "recurrence_status": "not-established",
        "measurement_limits": [
            "Exact source pointers provide traceability, not causal blocking or safe replacement.",
            "Owner declarations and append-only correction values remain unverified as implementation ownership or an applied change.",
            "Session context responses are not proof of agent receipt or capability use; target-capability attribution requires exact binding evidence.",
            "Recurrence, migration readiness, and systemwide adoption are not measured.",
        ],
    }
    result["status"] = (
        "linked-source-context" if links and not unverified_matches else (
            "partial-source-context" if total_matches else (
                "no-exact-shared-source-pointer"
                if audits and correspondences else "source-context-unavailable"
            )
        )
    )
    while (
        len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode())
        > MAX_RESULT_BYTES and result["links"]
    ):
        source_roles = result["links"][0].get(
            "native_implementation_source_references",
        )
        if isinstance(source_roles, list) and len(source_roles) > 2:
            # Keep the language contract and runtime provider as the primary
            # path; omit compiler support only when the response needs room.
            compiler_index = next(
                (index for index, item in enumerate(source_roles)
                 if isinstance(item, dict)
                 and item.get("role") == "compiler_support"),
                None,
            )
            source_roles.pop(
                compiler_index if compiler_index is not None else 1,
            )
            result["links"][0]["native_implementation_sources_truncated"] = True
            continue
        result["links"].pop()
        result["link_count"] = len(result["links"])
        result["links_truncated"] = True
    return result
