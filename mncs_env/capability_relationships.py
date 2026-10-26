"""Bounded evidence links between owner audits and native API observations.

The projection joins existing Environment evidence in memory. It creates no
second registry and treats exact source pointers as traceability, not as a
causal or migration finding.
"""

from __future__ import annotations

import json
import re
from typing import Any

SCHEMA = "mncs.environment.capability-accountability-links/1"
MAX_LINKS = 4
MAX_RESULT_BYTES = 4 * 1024
MAX_RESULT_RESERVE_BYTES = MAX_RESULT_BYTES
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
            ("owner_declared_native_surface", "owner_declared_native_surface"),
            ("owner_declared_next_native_slice", "owner_declared_next_native_slice"),
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


def project(
    implementation_accountability: Any,
    observations: Any = None,
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
        provenance: str, correction: dict[str, Any] | None = None,
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
            "relationship_assessment": (
                _text(correspondence.get("relationship_assessment"), 192)
                or "unavailable"
            ),
            "shared_pressure_source_reference": reference,
            "source_reference_provenance": provenance[:192],
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
            "interpretation_limit": (
                "Exact source pointers establish traceability only. They do not "
                "prove that the pressure causes a specific host responsibility "
                "or that the candidate native capability is a safe replacement."
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
                        provenance="owner_audit.supporting_source_references",
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
                            provenance=(
                                "correction.supporting_sources; correction target "
                                "matches owner audit evidence_reference"
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
        "systemwide_native_adoption_ratio": {"status": "not-measured"},
        "recurrence_status": "not-established",
        "measurement_limits": [
            "Links use exact repository, commit, path, and SHA-256 pointers from existing campaign observations.",
            "Owner audit classifications remain owner-declared even when referenced source bytes are integrity-verified.",
            "A correction target match preserves traceability but does not establish that replacement values were applied to the original record.",
            "A shared pressure source does not establish causal blocking, agent use, or migration readiness.",
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
        > MAX_RESULT_BYTES
        and result["links"]
    ):
        result["links"].pop()
        result["link_count"] = len(result["links"])
        result["links_truncated"] = True
    return result
