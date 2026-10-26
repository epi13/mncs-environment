from __future__ import annotations

import json

from mncs_env import capability_relationships, capability_intelligence, projections


def _pointer(*, sha256: str = "b" * 64, integrity: str = "content-integrity-verified"):
    return {
        "identity": "MNCS-TEST-P-001",
        "kind": "native-userland-pressure-source",
        "repository": "mncs-test",
        "commit": "a" * 40,
        "path": "pressures/MNCS-TEST-P-001.json",
        "sha256": sha256,
        "content_integrity_status": integrity,
    }


def _accountability(pointer: dict[str, str]) -> dict:
    return {
        "schema_version": "mncs.environment.implementation-accountability/1",
        "sample_count": 0,
        "systemwide_native_adoption_ratio": {"status": "not-measured"},
        "owner_audit_evidence": {
            "audits": [{
                "identity": "ev_environment_native_userland_audit",
                "projects": [{
                    "repository": "mncs-test",
                    "owner_declared_status": "native_canonical",
                    "owner_declared_native_surface": "native test result and suite projections",
                    "owner_declared_next_native_slice": "native Commons pressure query materialization",
                    "remaining_host_responsibilities": [
                        "external process timeout and test binaries",
                    ],
                }],
                "source_audit_details": [{
                    "repository": "mncs-test",
                    "source_record_status": (
                        "content-integrity-verified-owner-declaration"
                    ),
                    "remaining_host_responsibilities": [{
                        "area": "host process timeout and external test binaries",
                        "classification": "external_interoperability_only",
                        "eliminating_capability": "bounded process sessions",
                    }],
                }],
                "supporting_source_references": [pointer],
                "evidence_reference": {
                    "identity": "ev_environment_native_userland_audit",
                    "repository": "mncs-environment",
                    "commit": "c" * 40,
                    "path": "evidence/native-userland-audit.json",
                    "sha256": "d" * 64,
                },
            }],
        },
        "native_capability_correspondences": [{
            "identity": "ev_environment_native_process_correspondence",
            "target_pressure_identity": "MNCS-TOOLING-B665F138D324",
            "native_capability_identity": "mncs.std.process.v1",
            "relationship_assessment": "candidate-correspondence",
            "next_investigation": "Measure an existing native consumer and parity before migration.",
            "pressure_source_links": {
                "links": [{
                    "identity": "MNCS-TOOLING-B665F138D324",
                    "status": "linked-through-integrity-verified-pressure-alias",
                    "source_content_integrity_status": (
                        pointer["content_integrity_status"]
                    ),
                    "source_reference": pointer,
                }],
            },
        }],
    }


def test_shared_verified_source_pointer_links_owner_context_without_claiming_causality():
    result = capability_relationships.project(_accountability(_pointer()))

    assert result["status"] == "linked-source-context"
    assert result["link_count"] == 1
    link = result["links"][0]
    assert link["target_pressure_identity"] == "MNCS-TOOLING-B665F138D324"
    assert link["native_capability_identity"] == "mncs.std.process.v1"
    assert link["shared_pressure_source_reference"]["identity"] == "MNCS-TEST-P-001"
    assert link["pressure_source_integrity_status"] == (
        "verified-in-both-linked-observations"
    )
    assert link["owner_audit_context"]["repository"] == "mncs-test"
    assert link["owner_audit_context"]["source_declared_host_responsibilities"][0][
        "eliminating_capability"
    ] == "bounded process sessions"
    assert link["responsibility_match_status"] == "not-established"
    assert link["causal_relation_status"] == "not-established"
    assert link["native_migration_readiness"] == "not-established"
    assert result["systemwide_native_adoption_ratio"]["status"] == "not-measured"


def test_pointer_identity_without_integrity_verification_remains_partial():
    pointer = _pointer(integrity="source-content-not-verified")

    result = capability_relationships.project(_accountability(pointer))

    assert result["status"] == "partial-source-context"
    assert result["exact_shared_pointer_match_count"] == 1
    assert result["unverified_shared_pointer_match_count"] == 1
    assert result["links"][0]["pressure_source_integrity_status"] == (
        "not-verified-in-both-linked-observations"
    )


def test_pointer_with_same_identity_but_different_digest_does_not_link():
    accountability = _accountability(_pointer(sha256="e" * 64))
    accountability["native_capability_correspondences"][0][
        "pressure_source_links"]["links"][0]["source_reference"] = _pointer(
            sha256="f" * 64,
        )

    result = capability_relationships.project(accountability)

    assert result["status"] == "no-exact-shared-source-pointer"
    assert result["link_count"] == 0


def test_correction_observation_bridges_owner_audit_and_native_pressure_source():
    accountability = _accountability(_pointer())
    owner_reference = accountability["owner_audit_evidence"]["audits"][0][
        "evidence_reference"
    ]
    accountability["owner_audit_evidence"]["audits"][0][
        "supporting_source_references"
    ] = [_pointer(sha256="a" * 64)]
    correction = {
        "identity": "ev_native_userland_audit_correction",
        "evidence_reference": {
            "identity": "ev_native_userland_audit_correction",
            "repository": "mncs-environment",
            "commit": "e" * 40,
            "path": "evidence/native-userland-correction.json",
            "sha256": "f" * 64,
        },
        "record_correction": {
            "status": "replacement-values-unverified-or-unchanged",
            "target_reference": owner_reference,
        },
        "improvement_relation_status": "not-recorded",
        "supporting_sources": [_pointer()],
    }

    result = capability_relationships.project(
        accountability, observations=[correction],
    )

    assert result["status"] == "linked-source-context"
    assert result["exact_shared_pointer_match_count"] == 1
    link = result["links"][0]
    assert link["relationship_kind"] == (
        "audit-correction-to-native-source-pointer"
    )
    assert link["audit_correction_target_match_status"] == "exact-reference-match"
    assert link["correction_record_status"] == (
        "replacement-values-unverified-or-unchanged"
    )
    assert link["improvement_relation_status"] == "not-recorded"
    assert link["shared_pressure_source_reference"]["identity"] == "MNCS-TEST-P-001"
    assert link["causal_relation_status"] == "not-established"
    assert link["native_migration_readiness"] == "not-established"


def test_derived_relationship_output_stays_within_reserved_projection_budget():
    accountability = _accountability(_pointer())
    project = accountability["owner_audit_evidence"]["audits"][0]["projects"][0]
    project["remaining_host_responsibilities"] = ["x" * 100_000] * 50

    result = capability_relationships.project(accountability)

    assert len(json.dumps(result, separators=(",", ":")).encode()) <= (
        capability_relationships.MAX_RESULT_BYTES
    )


def test_existing_environmental_capability_route_exposes_bounded_relationships(
    monkeypatch,
):
    accountability = _accountability(_pointer())
    audit = accountability["owner_audit_evidence"]["audits"][0]
    audit["supporting_source_references"] = [_pointer(sha256="a" * 64)]
    correction = {
        "identity": "ev_native_userland_audit_correction",
        "evidence_reference": {
            "identity": "ev_native_userland_audit_correction",
            "repository": "mncs-environment",
            "commit": "e" * 40,
            "path": "evidence/native-userland-correction.json",
            "sha256": "f" * 64,
        },
        "record_correction": {
            "status": "replacement-values-unverified-or-unchanged",
            "target_reference": audit["evidence_reference"],
        },
        "improvement_relation_status": "not-recorded",
        "supporting_sources": [_pointer()],
    }
    source_projection = {
        "implementation_accountability": accountability,
        "observations": [correction],
    }
    requested_budgets = []

    def project(_session, **kwargs):
        requested_budgets.append(kwargs["max_result_bytes"])
        return source_projection.copy()

    monkeypatch.setattr(projections.capability_intelligence, "project", project)

    result = projections.interpretation(None, None, "environmental-capability")

    relation = result["capability_accountability_links"]
    assert relation["link_count"] == 1
    assert relation["links"][0]["audit_correction_target_match_status"] == (
        "exact-reference-match"
    )
    assert requested_budgets == [
        capability_intelligence.MAX_RESULT_BYTES
        - capability_relationships.MAX_RESULT_RESERVE_BYTES
    ]
    assert len(json.dumps(result, separators=(",", ":")).encode()) < (
        capability_intelligence.MAX_RESULT_BYTES
    )
