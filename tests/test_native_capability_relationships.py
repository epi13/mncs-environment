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


def _native_source(identity: str, path: str, sha256: str) -> dict[str, str]:
    return {
        "identity": identity,
        "kind": "native-process-implementation-source",
        "repository": "mncs-language",
        "commit": "c" * 40,
        "path": path,
        "sha256": sha256,
        "content_integrity_status": "workspace-read-only-content-integrity-verified",
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
            "source_evidence_status": (
                "required-source-blobs-and-pressure-alias-verified"
            ),
            "source_evidence_by_role": {
                "language_contract": {
                    "status": "content-integrity-verified",
                    "source_reference_count": 1,
                    "source_references": [_native_source(
                        "mncs_language_process_contract",
                        "library/std/process.mncs", "1" * 64,
                    )],
                },
                "compiler_support": {
                    "status": "content-integrity-verified",
                    "source_reference_count": 2,
                    "source_references": [_native_source(
                        "mncs_language_process_frontend",
                        "crates/mncs-compiler/src/frontend.rs", "2" * 64,
                    ), _native_source(
                        "mncs_language_process_inventory",
                        "crates/mncs-compiler/src/inventory.rs", "3" * 64,
                    )],
                },
                "host_runtime_provider": {
                    "status": "content-integrity-verified",
                    "source_reference_count": 1,
                    "source_references": [_native_source(
                        "mncs_language_process_runtime",
                        "crates/mncs-model/src/process_runtime.rs", "4" * 64,
                    )],
                },
            },
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
    assert link["native_capability_source_evidence_status"] == (
        "required-source-blobs-and-pressure-alias-verified"
    )
    implementation_roles = link["native_implementation_source_references"]
    assert [item["role"] for item in implementation_roles] == [
        "language_contract", "compiler_support", "host_runtime_provider",
    ]
    assert implementation_roles[0]["source_reference"]["path"] == (
        "library/std/process.mncs"
    )
    assert implementation_roles[1]["additional_references_not_included"] is True
    assert implementation_roles[2]["source_reference"]["path"].endswith(
        "/process_runtime.rs"
    )
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


def test_unmatched_pressure_sources_are_exposed_without_claiming_a_relationship():
    accountability = _accountability(_pointer())
    accountability["owner_audit_evidence"]["audits"][0][
        "supporting_source_references"
    ] = [
        {
            **_pointer(sha256="4" * 64),
            "identity": "MNCS-TEST-P-004",
            "path": "pressures/MNCS-TEST-P-004.json",
            "kind": "repository-owner-language-and-runtime-pressure",
        },
        {
            **_pointer(sha256="5" * 64),
            "identity": "MNCS-TEST-P-006",
            "path": "pressures/MNCS-TEST-P-006.json",
            "kind": "repository-owner-native-result-transport-pressure",
        },
    ]

    result = capability_relationships.project(
        accountability,
        session_activity=_context_activity({
            "capture_status": "captured",
            "binding_ids_status": "partial",
            "available_binding_ids": [],
            "event_reference": {"identity": "evt_context", "sequence": 41},
        }),
    )

    assert result["status"] == "no-exact-shared-source-pointer"
    assert result["link_count"] == 0
    comparison = result["unmatched_source_pointer_context"]
    assert comparison["status"] == "no-exact-shared-source-pointer"
    assert [reference["identity"] for reference in comparison[
        "owner_audit_pressure_sources"]["references"]] == [
            "MNCS-TEST-P-004", "MNCS-TEST-P-006",
        ]
    assert [reference["identity"] for reference in comparison[
        "native_capability_pressure_sources"]["references"]] == [
            "MNCS-TEST-P-001",
        ]
    assert comparison["comparison_basis"] == (
        "repository+commit+path+sha256"
    )
    assert comparison["causal_relation_status"] == "not-established"
    assert len(json.dumps(result, separators=(",", ":")).encode()) <= (
        capability_relationships.MAX_RESULT_BYTES
    )


def test_unmatched_pointer_context_and_session_evidence_share_one_bounded_budget():
    accountability = _accountability(_pointer())
    long_pointer = {
        **_pointer(),
        "identity": "i" * 128,
        "repository": "r" * 120,
        "commit": "a" * 64,
        "path": "p" * 256,
        "sha256": "b" * 64,
        "kind": "repository-owner-language-and-runtime-pressure",
        "status": "not-present-in-selected-campaign-evidence-window",
    }
    accountability["owner_audit_evidence"]["audits"][0][
        "supporting_source_references"
    ] = [dict(long_pointer, sha256=str(index) * 64) for index in range(1, 7)]
    candidate_pointer = dict(
        long_pointer, sha256="c" * 64, kind="native-userland-pressure-source",
    )
    correction_pointer = dict(
        long_pointer,
        identity="MNCS-TEST-P-999",
        path="pressures/MNCS-TEST-P-999.json",
        sha256="f" * 64,
        kind="native-userland-pressure-source",
    )
    accountability["native_capability_correspondences"][0][
        "pressure_source_links"]["links"][0]["source_reference"] = candidate_pointer
    summary = {
        "capture_status": "captured",
        "binding_ids_status": "complete",
        "available_binding_ids": ["cap_" + "d" * 64 for _ in range(20)],
        "available_identity_digest": "e" * 64,
        "identity_digest_status": "complete",
        "available_count": 20,
        "listed_available_count": 20,
        "unavailable_count": 0,
        "unknown_count": 0,
        "capabilities_truncated": False,
        "event_reference": {"identity": "evt_long", "sequence": 7},
    }

    result = capability_relationships.project(
        accountability,
        observations=[{"supporting_sources": [correction_pointer]}],
        session_activity=_context_activity(summary),
        subject={"session_id": "s" * 128, "work_intent": {"goal": "g" * 192}},
    )

    assert result["unmatched_source_pointer_context"][
        "owner_audit_pressure_sources"
    ]["references_truncated"] is True
    assert len(json.dumps(result, separators=(",", ":")).encode()) <= (
        capability_relationships.MAX_RESULT_BYTES
    )


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


def _context_activity(summary: dict | None, *, status: str = "partial") -> dict:
    return {
        "source": {
            "event_window_sequence_start": 28,
            "event_window_sequence_end": 43,
            "event_window_truncated": True,
        },
        "capability_delivery": {
            "context_operation_activity": {
                "scope": "authenticated-Control-session.context-events-in-selected-window",
                "status": status,
                "agent_receipt_status": "not-established",
                "response_summaries": [summary] if summary else [],
                "response_summaries_omitted": 0,
            },
        },
    }


def test_context_delivery_observation_preserves_partial_session_evidence_without_target_attribution():
    summary = {
        "schema_version": "mncs.environment.context-response-summary/2",
        "capture_status": "captured",
        "context_schema_version": "mncs.environment.entry-context/1",
        "identity_digest_status": "complete",
        "available_identity_digest": "f" * 64,
        "available_count": 9,
        "listed_available_count": 9,
        "unavailable_count": 0,
        "unknown_count": 0,
        "capabilities_truncated": False,
        "available_binding_ids": [],
        "binding_ids_status": "partial",
        "event_reference": {
            "identity": "evt_de29b1e1b27f5e61",
            "sequence": 41,
            "type": "environment.rpc.read_observed",
        },
        "binding_snapshot_comparison": {
            "status": "matches-current-session-binding-snapshot",
            "basis": "current-session-binding-snapshot-at-projection-time",
            "available_count_matches": True,
            "available_identity_digest_matches": True,
        },
    }

    result = capability_relationships.project(
        {},
        session_activity=_context_activity(summary),
        subject={
            "session_id": "ses_9ad3efb63438cf3d",
            "campaign_id": "cmp_env_capability_intelligence_continuation_codex_20261025",
            "environment_id": "renv_4c126896d339c0ce",
            "work_intent": {
                "identity": "int_8475157f281c835f",
                "goal": "exercise canonical Environment persistence through the Control socket",
            },
        },
    )

    observation = result["session_context_delivery_observation"]
    assert observation["session_id"] == "ses_9ad3efb63438cf3d"
    assert observation["work_intent"]["identity"] == "int_8475157f281c835f"
    assert observation["target_capability_attribution_status"] == "not-established"
    assert observation["agent_receipt_status"] == "not-established"
    assert observation["binding_inclusion_projection_path"] == (
        "session_activity.capability_delivery.bindings[*].context_response_inclusion"
    )
    captured = observation["response_summaries"][0]
    assert captured["event_reference"]["sequence"] == 41
    assert captured["binding_ids_status"] == "partial"
    assert captured["available_binding_ids"] == []
    assert captured["binding_snapshot_comparison"]["status"] == (
        "matches-current-session-binding-snapshot"
    )
    assert observation["interpretation"]["status"] == (
        "partial-binding-id-attribution"
    )


def test_context_delivery_absence_is_scoped_to_selected_event_window():
    observation = capability_relationships.project(
        {}, session_activity=_context_activity(
            None, status="not-observed-in-selected-event-window",
        ),
    )["session_context_delivery_observation"]

    assert observation["selected_event_window"] == {
        "event_window_sequence_start": 28,
        "event_window_sequence_end": 43,
        "event_window_truncated": True,
    }
    assert observation["response_summaries"] == []
    assert observation["interpretation"]["status"] == (
        "no-response-summary-in-selected-window"
    )
    assert "not evidence" in observation["interpretation"]["next_investigation"]


def test_context_delivery_partial_status_does_not_infer_no_response():
    observation = capability_relationships.project(
        {}, session_activity=_context_activity(None, status="partial"),
    )["session_context_delivery_observation"]

    assert observation["interpretation"]["status"] == (
        "response-summary-unavailable-or-incomplete"
    )
    assert "underlying authenticated event" in observation["interpretation"][
        "next_investigation"
    ]


def test_complete_context_binding_ids_do_not_establish_agent_receipt_or_use():
    summary = {
        "capture_status": "captured",
        "binding_ids_status": "complete",
        "available_binding_ids": ["cap_" + "a" * 64],
        "event_reference": {"identity": "evt_context", "sequence": 9},
    }

    observation = capability_relationships.project(
        {}, session_activity=_context_activity(summary),
    )["session_context_delivery_observation"]

    assert observation["interpretation"]["status"] == (
        "binding-identifiers-captured-receipt-unestablished"
    )
    assert observation["agent_receipt_status"] == "not-established"
    assert observation["target_capability_attribution_status"] == "not-established"
    assert "subsequent authenticated invocation events" in observation[
        "interpretation"
    ]["next_investigation"]


def test_uncaptured_context_response_is_not_misclassified_as_partial_ids():
    summary = {
        "capture_status": "unavailable",
        "capture_reason": "response-too-large",
        "binding_ids_status": "unavailable",
        "event_reference": {"identity": "evt_context", "sequence": 9},
    }

    observation = capability_relationships.project(
        {}, session_activity=_context_activity(summary),
    )["session_context_delivery_observation"]

    assert observation["interpretation"]["status"] == (
        "response-summary-unavailable-or-incomplete"
    )


def test_context_delivery_observation_stays_bounded_at_response_summary_limits():
    summary = {
        "schema_version": "s" * 128,
        "capture_status": "captured",
        "capture_reason": "r" * 128,
        "context_schema_version": "c" * 128,
        "identity_digest_status": "complete",
        "available_identity_digest": "d" * 64,
        "binding_ids_status": "complete",
        "available_count": 20,
        "listed_available_count": 20,
        "unavailable_count": 0,
        "unknown_count": 0,
        "capabilities_truncated": False,
        "available_binding_ids": ["cap_" + "a" * 64 for _ in range(20)],
        "event_reference": {
            "identity": "i" * 128, "sequence": 42, "type": "t" * 128,
        },
        "binding_snapshot_comparison": {
            "status": "m" * 128, "basis": "b" * 128,
            "reason": "q" * 128,
            "available_count_matches": True,
            "unavailable_count_matches": True,
            "unknown_count_matches": True,
            "available_identity_digest_matches": True,
        },
    }
    activity = _context_activity(summary)
    activity["capability_delivery"]["context_operation_activity"][
        "response_summaries"
    ] = [summary, summary]

    result = capability_relationships.project(
        {}, session_activity=activity,
        subject={
            "session_id": "s" * 128,
            "campaign_id": "c" * 128,
            "environment_id": "e" * 128,
            "work_intent": {"identity": "i" * 128, "goal": "g" * 192},
        },
    )

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
        "session_activity": _context_activity({
            "capture_status": "captured",
            "binding_ids_status": "partial",
            "available_binding_ids": [],
            "event_reference": {"sequence": 41},
        }),
        "subject": {"session_id": "ses_projection-test"},
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
    assert relation["session_context_delivery_observation"]["session_id"] == (
        "ses_projection-test"
    )
    assert relation["session_context_delivery_observation"][
        "target_capability_attribution_status"
    ] == "not-established"
    assert requested_budgets == [
        capability_intelligence.MAX_RESULT_BYTES
        - capability_relationships.MAX_RESULT_RESERVE_BYTES
    ]
    assert len(json.dumps(result, separators=(",", ":")).encode()) < (
        capability_intelligence.MAX_RESULT_BYTES
    )
