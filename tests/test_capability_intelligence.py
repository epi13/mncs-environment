from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import time
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from mncs_env import capability_intelligence, cli, projections
from mncs_env.sessions import LifecycleError


def _git(checkout: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(checkout), *args],
        check=True, capture_output=True, text=True,
        env={"PATH": os.environ.get("PATH", ""),
             "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
             "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
             "GIT_OPTIONAL_LOCKS": "0"},
    )
    return result.stdout.strip()


def _session(tmp_path: Path, reports: list[dict], *, extra_refs: list[dict] | None = None):
    root = tmp_path / "workspace"
    checkout = root / "mncs-environment"
    checkout.mkdir(parents=True)
    _git(checkout, "init", "--quiet")
    _git(checkout, "config", "user.name", "test")
    _git(checkout, "config", "user.email", "test@example.invalid")

    references = []
    for index, report in enumerate(reports):
        relative = f"evidence/environmental-capability/observation-{index}.json"
        path = checkout / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
        path.write_bytes(raw)
        _git(checkout, "add", relative)
        references.append({
            "identity": report["identity"],
            "repository": "mncs-environment",
            "commit": "",  # filled after the commit below
            "path": relative,
            "sha256": hashlib.sha256(raw).hexdigest(),
        })
    _git(checkout, "commit", "--quiet", "-m", "test observation records")
    head = _git(checkout, "rev-parse", "HEAD")
    for reference in references:
        reference["commit"] = head
    if extra_refs:
        references.extend(extra_refs)

    state = {
        "checkpoint_id": "chk_campaign_state",
        "recorded_at": "2026-10-24T00:00:00+00:00",
        "recorded_by": {"consumer_id": "recorder", "principal_id": "ctrl_test"},
        "evidence": references,
        "evidence_count": len(references),
        "evidence_truncated": False,
        "delivery": {"status": "pending"},
    }
    session = SimpleNamespace(
        session_id="ses_projection_reader",
        snapshot={
            "environment_id": "renv_projection_reader",
            "provenance": {"definition_id": "env_projection_reader"},
            "intent": {"identity": "int_projection_reader", "goal": "inspect current pressures"},
            "campaign": {"identity": "cmp_projection_reader"},
            "workspace": {"root": str(root)},
            "selected_checkouts": {"mncs-environment": {"path": str(checkout)}},
        },
        campaign_state=lambda **_kwargs: state,
    )
    return session, state


def _observation(identity: str, session_id: str, campaign_id: str) -> dict:
    return {
        "schema_version": capability_intelligence.OBSERVATION_SCHEMA,
        "identity": identity,
        "captured_at": "2026-10-24T00:00:00Z",
        "observation": {
            "kind": "project-campaign-state-read",
            "source_interface": "mncs_environment MCP",
            "request": {"operation": "campaign_state", "session_id": session_id},
            "outcome": {
                "code": "ENVIRONMENT_SESSION_SELECTION_REQUIRED",
                "selection_status": "context-conflict",
                "continuations_count": 3,
                "continuation_session_ids": [session_id, "ses_other_a", "ses_other_b"],
            },
        },
        "related_work": {
            "campaign_id": campaign_id,
            "session_id": session_id,
            "work_intent_id": "int_compiler",
            "work_intent_goal": "advance native compiler self-consumption",
        },
        "pressure_hypothesis": {
            "identity": "ph_environment_project_session_selection",
            "title": "Known campaign session cannot be selected when project continuations conflict",
            "category": "coordination-continuity",
            "subsystem": "mncs-control-mcp project continuation selection",
            "owner": "mncs-control-mcp",
            "actionable_recommendation": "Allow exact session selection after owner and scope checks.",
        },
        "supporting_sources": [
            {"kind": "direct-operation-result", "campaign_id": campaign_id,
             "session_id": session_id},
        ],
        "recorder": {
            "campaign_id": "cmp_observation_capture",
            "session_id": "ses_observation_capture",
            "consumer_id": "local-agent",
        },
    }


def test_project_correlates_authentic_observation_and_preserves_uncertainty(tmp_path: Path) -> None:
    record = _observation("ev_environment_observation_one", "ses_compiler_one", "cmp_compiler")
    control_source = {
        "identity": "ev_control_selector_source",
        "repository": "mncs-control-mcp",
        "commit": "a" * 40,
        "path": "src/mncs_control_mcp/local_environment_mcp.py",
        "sha256": "b" * 64,
    }
    session, _ = _session(tmp_path, [record], extra_refs=[control_source])

    result = capability_intelligence.project(session)

    assert result["status"] == "ready"
    assert result["subject"]["campaign_id"] == "cmp_projection_reader"
    assert result["subject"]["work_intent"]["identity"] == "int_projection_reader"
    assert result["source"]["campaign_state_checkpoint_id"] == "chk_campaign_state"
    assert result["observation_count"] == 1
    assert result["observations"][0]["related_work"]["work_intent_goal"] == "advance native compiler self-consumption"
    assert result["observations"][0]["related_work"]["campaign_id"] == "cmp_compiler"
    assert result["observations"][0]["recording_context"] == {
        "campaign_id": "cmp_observation_capture",
        "session_id": "ses_observation_capture",
        "consumer_id": "local-agent",
    }
    hypothesis = result["pressure_hypotheses"][0]
    assert hypothesis["observation_count"] == 1
    assert hypothesis["distinct_session_count"] == 1
    assert hypothesis["recurrence_status"] == "not-established"
    assert result["observations"][0]["observation"]["outcome"]["continuations_count"] == 3
    assert result["observations"][0]["supporting_sources"][0]["status"] == "captured-in-observation-record"


def test_projection_carries_source_page_cursor_through_compaction(tmp_path: Path) -> None:
    record = _observation("ev_environment_observation_page", "ses_compiler", "cmp_compiler")
    record["observation"]["outcome"]["bounded_context"] = "x" * 8_000
    session, state = _session(tmp_path, [record])
    state.update({
        "evidence_offset": 16,
        "evidence_limit": 8,
        "evidence_truncated": True,
        "previous_evidence_offset": 8,
        "next_evidence_offset": 24,
    })

    projection = capability_intelligence.project(session)
    compact = capability_intelligence.project(session, max_result_bytes=5_000)

    for result in (projection, compact):
        assert result["source"]["evidence_page_offset"] == 16
        assert result["source"]["evidence_page_limit"] == 8
        assert result["source"]["previous_evidence_offset"] == 8
        assert result["source"]["next_evidence_offset"] == 24
        assert result["source"]["evidence_page_truncated"] is True

    state.update({
        "evidence_offset": 0,
        "previous_evidence_offset": None,
        "next_evidence_offset": None,
    })
    first_page = capability_intelligence.project(
        session, max_result_bytes=5_000,
    )
    assert first_page["source"]["previous_evidence_offset"] is None
    assert first_page["source"]["next_evidence_offset"] is None

    compact_hypothesis = compact["pressure_hypotheses"][0]
    expected_reference = projection["pressure_hypotheses"][0]["evidence"][0]
    assert compact_hypothesis["evidence_references"] == [expected_reference]
    assert compact_hypothesis["evidence_reference_count"] == 1
    assert compact_hypothesis["evidence_references_truncated"] is False


def test_project_does_not_read_a_supporting_repo_outside_selected_session(tmp_path: Path) -> None:
    record = _observation("ev_environment_observation_one", "ses_compiler_one", "cmp_compiler")
    control_source = {
        "identity": "ev_control_selector_source",
        "repository": "mncs-control-mcp",
        "commit": "a" * 40,
        "path": "src/mncs_control_mcp/local_environment_mcp.py",
        "sha256": "b" * 64,
    }
    record["supporting_sources"].append({
        "kind": "adapter-contract-source",
        "repository": "mncs-control-mcp",
        "commit": control_source["commit"],
        "path": control_source["path"],
        "sha256": control_source["sha256"],
    })
    session, _ = _session(tmp_path, [record], extra_refs=[control_source])

    result = capability_intelligence.project(session)

    source = result["observations"][0]["supporting_sources"][1]
    assert source["status"] == "outside-selected-session"
    assert source["content_integrity_status"] == "outside-selected-session"
    assert source["campaign_evidence_identity"] == "ev_control_selector_source"
    assert result["source"]["source_pointer_count"] == 1
    assert result["source"]["source_pointer_content_integrity_verified_count"] == 0
    assert result["source"]["source_pointer_content_integrity_not_verified_count"] == 1
    assert result["status"] == "ready"


def test_selected_source_digest_is_verified_and_compact_pointer_is_preserved(
    tmp_path: Path,
) -> None:
    identity = "ev_environment_source_integrity"
    record = _observation(identity, "ses_compiler", "cmp_compiler")
    record["observation"]["outcome"]["large_diagnostic"] = "x" * 50_000
    session, state = _session(tmp_path, [record])
    checkout = Path(
        session.snapshot["selected_checkouts"]["mncs-environment"]["path"]
    )
    source_path = checkout / "src" / "selected-source.mn"
    source_path.parent.mkdir(parents=True)
    source_bytes = b"module selected_source\n" * 6_000
    source_path.write_bytes(source_bytes)
    relative_source = "src/selected-source.mn"
    oversized_source_path = checkout / "src" / "oversized-source.mn"
    oversized_source_bytes = b"x" * (capability_intelligence.MAX_SOURCE_BYTES + 1)
    oversized_source_path.write_bytes(oversized_source_bytes)
    relative_oversized_source = "src/oversized-source.mn"
    _git(checkout, "add", relative_source, relative_oversized_source)
    _git(checkout, "commit", "--quiet", "-m", "test selected source")
    source_commit = _git(checkout, "rev-parse", "HEAD")
    source_digest = hashlib.sha256(source_bytes).hexdigest()
    record["supporting_sources"].append({
        "kind": "committed-source",
        "identity": "ev_environment_selected_source",
        "repository": "mncs-environment",
        "commit": source_commit,
        "path": relative_source,
        "sha256": source_digest,
    })
    bad_source_digest = "0" * 64
    record["supporting_sources"].append({
        "kind": "committed-source",
        "identity": "ev_environment_selected_source_bad_digest",
        "repository": "mncs-environment",
        "commit": source_commit,
        "path": relative_source,
        "sha256": bad_source_digest,
    })
    oversized_source_digest = hashlib.sha256(oversized_source_bytes).hexdigest()
    record["supporting_sources"].append({
        "kind": "committed-source",
        "identity": "ev_environment_selected_source_oversized",
        "repository": "mncs-environment",
        "commit": source_commit,
        "path": relative_oversized_source,
        "sha256": oversized_source_digest,
    })
    observation_path = checkout / state["evidence"][0]["path"]
    raw = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    observation_path.write_bytes(raw)
    _git(checkout, "add", state["evidence"][0]["path"])
    _git(checkout, "commit", "--quiet", "-m", "link selected source")
    observation_commit = _git(checkout, "rev-parse", "HEAD")
    state["evidence"][0].update({
        "commit": observation_commit,
        "sha256": hashlib.sha256(raw).hexdigest(),
    })
    state["evidence"].append({
        "identity": "ev_environment_selected_source",
        "repository": "mncs-environment",
        "commit": source_commit,
        "path": relative_source,
        "sha256": source_digest,
    })
    state["evidence"].append({
        "identity": "ev_environment_selected_source_bad_digest",
        "repository": "mncs-environment",
        "commit": source_commit,
        "path": relative_source,
        "sha256": bad_source_digest,
    })
    state["evidence"].append({
        "identity": "ev_environment_selected_source_oversized",
        "repository": "mncs-environment",
        "commit": source_commit,
        "path": relative_oversized_source,
        "sha256": oversized_source_digest,
    })

    result = capability_intelligence.project(session)

    assert result["status"] == "partial"
    source = next(
        item for item in result["observations"][0]["supporting_sources"]
        if item.get("identity") == "ev_environment_selected_source"
    )
    assert source["status"] == "not-an-environmental-observation"
    assert source["content_integrity_status"] == "content-integrity-verified"
    assert source["commit"] == source_commit
    assert source["path"] == relative_source
    assert source["sha256"] == source_digest
    bad_source = next(
        item for item in result["observations"][0]["supporting_sources"]
        if item.get("identity") == "ev_environment_selected_source_bad_digest"
    )
    assert bad_source["content_integrity_status"] == "sha256-mismatch"
    oversized_source = next(
        item for item in result["observations"][0]["supporting_sources"]
        if item.get("identity") == "ev_environment_selected_source_oversized"
    )
    assert oversized_source["content_integrity_status"] == "source-object-too-large"
    assert result["source"]["source_pointer_count"] == 3
    assert result["source"]["source_pointer_content_integrity_verified_count"] == 1
    assert result["source"]["source_pointer_content_integrity_not_verified_count"] == 2


def test_project_requires_digest_before_exposing_a_pressure(tmp_path: Path) -> None:
    record = _observation("ev_environment_observation_one", "ses_compiler_one", "cmp_compiler")
    session, state = _session(tmp_path, [record])
    state["evidence"][0]["sha256"] = "0" * 64

    result = capability_intelligence.project(session)

    assert result["status"] == "partial"
    assert result["observation_count"] == 0
    assert result["pressure_hypothesis_count"] == 0
    assert result["source"]["observation_records_unavailable"] == 1


def test_malformed_observation_without_work_correlation_is_not_actionable(tmp_path: Path) -> None:
    record = _observation("ev_environment_observation_unrelated", "ses_compiler", "cmp_compiler")
    record.pop("related_work")
    session, _ = _session(tmp_path, [record])

    result = capability_intelligence.project(session)

    assert result["status"] == "partial"
    assert result["observation_count"] == 0
    assert result["pressure_hypothesis_count"] == 0
    assert result["source"]["observation_records_unavailable"] == 1


def test_invalid_recording_context_is_not_projected_as_provenance(tmp_path: Path) -> None:
    record = _observation("ev_environment_observation_unrelated", "ses_compiler", "cmp_compiler")
    record["recorder"]["session_id"] = []
    session, _ = _session(tmp_path, [record])

    result = capability_intelligence.project(session)

    assert result["status"] == "partial"
    assert result["observation_count"] == 0
    assert result["source"]["observation_records_unavailable"] == 1


def test_resource_observation_can_be_projected_without_inventing_a_pressure(tmp_path: Path) -> None:
    record = _observation("ev_environment_resource_sample", "ses_environment", "cmp_environment")
    record.pop("pressure_hypothesis")
    record["measurement_basis"] = "direct-resource-observation"
    record["observation"] = {
        "kind": "resource-headroom-snapshot",
        "source_interface": "mncs-env resources",
        "request": {"command": "resources", "scope": "caller-ancestry"},
        "outcome": {
            "schema_version": "mncs.environment.resource-observation/1",
            "observed_at": "2026-10-24T07:41:17.239289+00:00",
            "system": {"memory_available_bytes": 27491688448},
            "session_process_mapping": "not-asserted",
        },
    }
    record["related_work"].update({
        "campaign_objective": "MNCS Environmental Capability Intelligence",
        "environment_version": "092460a44be8547d2472a9f163c69ff3346e80fa",
    })
    record["not_measured"] = ["mapping of sampled processes to this Environment session"]
    session, _ = _session(tmp_path, [record])

    result = capability_intelligence.project(session)

    assert result["status"] == "ready"
    assert result["observation_count"] == 1
    assert result["pressure_hypothesis_count"] == 0
    assert result["observations"][0]["measurement_basis"] == "direct-resource-observation"
    assert result["observations"][0]["related_work"]["environment_version"] == "092460a44be8547d2472a9f163c69ff3346e80fa"
    assert result["observations"][0]["pressure_hypothesis"] is None
    assert result["observations"][0]["measurement_limits"] == [
        "mapping of sampled processes to this Environment session"
    ]


def test_compiler_campaign_state_observation_preserves_target_and_recorder(tmp_path: Path) -> None:
    path = Path(__file__).resolve().parents[1] / (
        "evidence/environmental-capability/compiler-campaign-state-20261024.json"
    )
    record = json.loads(path.read_text(encoding="utf-8"))
    session, _ = _session(tmp_path, [record])

    result = capability_intelligence.project(session)

    observation = result["observations"][0]
    assert result["status"] == "ready"
    assert result["observation_count"] == 1
    assert result["pressure_hypothesis_count"] == 0
    assert observation["related_work"]["campaign_id"] == (
        "cmp_mncs_native_application_readiness_compiler_20261013"
    )
    assert observation["recording_context"]["campaign_id"] == (
        "cmp_environmental_capability_intelligence_20261023"
    )
    assert observation["observation"]["outcome"]["evidence_page"]["truncated"] is True


def test_task_selector_recovery_remains_a_single_unresolved_hypothesis(tmp_path: Path) -> None:
    path = Path(__file__).resolve().parents[1] / (
        "evidence/environmental-capability/environment-task-selector-recovery-20261024.json"
    )
    record = json.loads(path.read_text(encoding="utf-8"))
    session, _ = _session(tmp_path, [record])

    result = capability_intelligence.project(session)

    assert result["observation_count"] == 1
    assert result["pressure_hypothesis_count"] == 1
    hypothesis = result["pressure_hypotheses"][0]
    assert hypothesis["recurrence_status"] == "not-established"
    assert hypothesis["observation_count"] == 1
    assert result["observations"][0]["recording_context"]["campaign_id"] == (
        "cmp_environmental_capability_intelligence_20261023"
    )


def test_verification_observation_keeps_resource_context_without_health_scoring(
    tmp_path: Path,
) -> None:
    path = Path(__file__).resolve().parents[1] / (
        "evidence/environmental-capability/capability-intelligence-verification-20261024.json"
    )
    record = json.loads(path.read_text(encoding="utf-8"))
    session, _ = _session(tmp_path, [record])

    result = capability_intelligence.project(session)

    observation = result["observations"][0]
    assert result["status"] == "ready"
    assert result["pressure_hypothesis_count"] == 0
    assert observation["measurement_basis"] == "direct-test-and-proof-command-results"
    assert observation["observation"]["outcome"]["full_test_suite"]["passed"] == 813
    assert observation["observation"]["outcome"]["post_suite_resource_observation"][
        "measurement_basis"
    ] == "point-in-time read after tests, not resource attribution to pytest"
    assert observation["recording_context"]["campaign_id"] == (
        "cmp_environmental_capability_intelligence_20261023"
    )


def test_selector_diagnostic_hypothesis_keeps_unselected_source_explicit(tmp_path: Path) -> None:
    path = Path(__file__).resolve().parents[1] / (
        "evidence/environmental-capability/environment-task-selector-diagnostic-analysis-20261024.json"
    )
    record = json.loads(path.read_text(encoding="utf-8"))
    source = record["supporting_sources"][1]
    session, _ = _session(tmp_path, [record], extra_refs=[{
        "identity": "ev_control_task_selector_guard_source",
        "repository": source["repository"],
        "commit": source["commit"],
        "path": source["path"],
        "sha256": source["sha256"],
    }])

    result = capability_intelligence.project(session)

    assert result["pressure_hypothesis_count"] == 1
    hypothesis = result["pressure_hypotheses"][0]
    assert hypothesis["recurrence_status"] == "not-established"
    assert result["observations"][0]["supporting_sources"][1]["status"] == (
        "outside-selected-session"
    )
    assert result["observations"][0]["recording_context"]["session_id"] == (
        "ses_c38b0ab35b9418ba"
    )


def test_compiler_cache_observation_keeps_resource_limits_and_source_unselected(
    tmp_path: Path,
) -> None:
    path = Path(__file__).resolve().parents[1] / (
        "evidence/environmental-capability/compiler-cache-regression-20261024.json"
    )
    record = json.loads(path.read_text(encoding="utf-8"))
    source = record["supporting_sources"][0]
    session, _ = _session(tmp_path, [record], extra_refs=[{
        "identity": "ev_compiler_cache_regression_source",
        "repository": source["repository"],
        "commit": source["commit"],
        "path": source["path"],
        "sha256": source["sha256"],
    }])

    result = capability_intelligence.project(session)

    observation = result["observations"][0]
    assert result["pressure_hypothesis_count"] == 0
    assert observation["related_work"]["campaign_id"] == (
        "cmp_mncs_native_application_readiness_compiler_20261013"
    )
    assert observation["recording_context"]["campaign_id"] == (
        "cmp_environmental_capability_intelligence_20261023"
    )
    assert observation["observation"]["outcome"]["actual_language_modules_reused"] is True
    assert observation["observation"]["outcome"]["representative_stages"][0][
        "peak_cgroup_memory_bytes"
    ] is None
    assert observation["supporting_sources"][0]["status"] == "outside-selected-session"


def test_improvement_relation_links_pressure_change_verification_and_followup(
    tmp_path: Path,
) -> None:
    pressure = _observation("ev_environment_capture_gap", "ses_compiler", "cmp_compiler")
    pressure["pressure_hypothesis"]["identity"] = "ph_capture_context"
    verification = _observation("ev_environment_change_verification", "ses_environment", "cmp_environment")
    verification.pop("pressure_hypothesis")
    followup = _observation("ev_environment_change_followup", "ses_compiler", "cmp_compiler")
    followup.pop("pressure_hypothesis")
    change = _observation("ev_environment_capture_change", "ses_environment", "cmp_environment")
    change.pop("pressure_hypothesis")
    implementation = {
        "identity": "ev_environment_capture_context_implementation",
        "repository": "mncs-environment",
        "commit": "a" * 40,
        "path": "mncs_env/capability_intelligence.py",
        "sha256": "b" * 64,
    }
    change["improvement_relation"] = {
        "identity": "im_capture_context",
        "addresses_hypothesis_identity": "ph_capture_context",
        "implementation_reference": implementation,
        "verification_observation_identity": verification["identity"],
        "subsequent_observation_identity": followup["identity"],
        "basis": "Existing projection tests verify the separate recording context; later Compiler evidence uses it.",
        "not_measured": ["whether attribution errors recur less often"],
    }
    session, _ = _session(
        tmp_path,
        [pressure, change, verification, followup],
        extra_refs=[implementation],
    )

    result = capability_intelligence.project(session)
    compact = capability_intelligence.project(session, max_result_bytes=5000)

    relation = result["improvement_relations"][0]
    assert result["improvement_relation_count"] == 1
    assert relation["addresses_hypothesis_identity"] == "ph_capture_context"
    assert relation["pressure_hypothesis_status"] == "present-in-projection"
    assert relation["verification_record_status"] == "record-integrity-verified-in-projection"
    assert relation["verification_evidence_reference"]["identity"] == verification["identity"]
    assert relation["subsequent_record_status"] == "record-integrity-verified-in-projection"
    assert relation["subsequent_evidence_reference"]["identity"] == followup["identity"]
    assert relation["effectiveness_assessment"] == "not-assessed"
    assert relation["implementation_reference"]["status"] == "not-an-environmental-observation"
    assert compact["status"] == "partial"
    assert compact["improvement_relation_count"] == 1
    assert compact["improvement_relations"][0]["effectiveness_assessment"] == "not-assessed"
    assert compact["improvement_relations"][0]["verification_evidence_reference"]["identity"] == (
        verification["identity"]
    )
    assert compact["improvement_relations"][0]["subsequent_evidence_reference"]["identity"] == (
        followup["identity"]
    )


def test_malformed_improvement_relation_is_not_projected(tmp_path: Path) -> None:
    record = _observation("ev_environment_bad_improvement", "ses_environment", "cmp_environment")
    record["improvement_relation"] = {
        "identity": "im_broken",
        "addresses_hypothesis_identity": "ph_missing",
        "verification_observation_identity": "ev_missing_verification",
        "subsequent_observation_identity": "ev_missing_followup",
        "basis": "missing implementation reference",
    }
    session, _ = _session(tmp_path, [record])

    result = capability_intelligence.project(session)

    assert result["status"] == "partial"
    assert result["observation_count"] == 0
    assert result["improvement_relation_count"] == 0
    assert result["source"]["observation_records_unavailable"] == 1


def test_improvement_relations_verification_record_preserves_skip_and_resource_limits(
    tmp_path: Path,
) -> None:
    path = Path(__file__).resolve().parents[1] / (
        "evidence/environmental-capability/improvement-relations-verification-20261024.json"
    )
    record = json.loads(path.read_text(encoding="utf-8"))
    session, _ = _session(tmp_path, [record])

    result = capability_intelligence.project(session)

    outcome = result["observations"][0]["observation"]["outcome"]
    assert result["status"] == "ready"
    assert result["pressure_hypothesis_count"] == 0
    assert outcome["full_test_suite"]["passed"] == 818
    assert outcome["full_test_suite"]["skipped"] == 81
    assert outcome["campaign_proofs"]["skip_reasons"][1] == (
        "memory.untouched: mncs-memory checkout absent"
    )
    assert outcome["post_suite_resource_observation"][
        "measurement_basis"
    ] == "point-in-time reading after tests and proofs, not resource attribution to them"


def test_authentic_capture_context_evidence_projects_as_one_improvement_chain(
    tmp_path: Path,
) -> None:
    evidence_dir = Path(__file__).resolve().parents[1] / (
        "evidence/environmental-capability"
    )
    records = [
        json.loads((evidence_dir / filename).read_text(encoding="utf-8"))
        for filename in (
            "environment-observation-capture-context-gap-20261024.json",
            "environment-observation-capture-context-improvement-20261024.json",
            "improvement-relations-verification-20261024.json",
            "compiler-cache-regression-20261024.json",
        )
    ]
    implementation = records[1]["improvement_relation"]["implementation_reference"]
    session, _ = _session(tmp_path, records, extra_refs=[implementation])

    result = capability_intelligence.project(session)

    assert result["status"] == "ready"
    assert result["pressure_hypothesis_count"] == 1
    assert result["pressure_hypotheses"][0]["identity"] == (
        "ph_environment_observation_capture_context"
    )
    assert result["improvement_relation_count"] == 1
    relation = result["improvement_relations"][0]
    assert relation["pressure_hypothesis_status"] == "present-in-projection"
    assert relation["verification_record_status"] == (
        "record-integrity-verified-in-projection"
    )
    assert relation["subsequent_record_status"] == (
        "record-integrity-verified-in-projection"
    )
    assert relation["effectiveness_assessment"] == "not-assessed"
    assert relation["verification_evidence_reference"]["identity"] == (
        "ev_environment_improvement_relations_verification_20261024"
    )
    assert relation["subsequent_evidence_reference"]["identity"] == (
        "ev_environment_compiler_cache_regression_20261024"
    )
    observations = {item["identity"]: item for item in result["observations"]}
    gap = observations["ev_environment_observation_capture_context_gap_20261024"]
    followup = observations["ev_environment_compiler_cache_regression_20261024"]
    assert gap["recording_context"]["session_id"] == "ses_c38b0ab35b9418ba"
    assert followup["related_work"]["session_id"] == "ses_164edc158d9b1637"
    assert followup["recording_context"]["session_id"] == "ses_c38b0ab35b9418ba"


def test_read_operation_observability_hypothesis_keeps_nonrecurrence_explicit(
    tmp_path: Path,
) -> None:
    path = Path(__file__).resolve().parents[1] / (
        "evidence/environmental-capability/read-operation-activity-correlation-gap-20261024.json"
    )
    record = json.loads(path.read_text(encoding="utf-8"))
    session, _ = _session(tmp_path, [record])

    result = capability_intelligence.project(session)

    assert result["status"] == "ready"
    assert result["pressure_hypothesis_count"] == 1
    hypothesis = result["pressure_hypotheses"][0]
    assert hypothesis["identity"] == (
        "ph_environment_read_operation_activity_correlation"
    )
    assert hypothesis["owner"] == "mncs-control-mcp"
    assert hypothesis["recurrence_status"] == "not-established"
    observation = result["observations"][0]
    assert observation["observation"]["outcome"][
        "operation_identity_present_in_returned_events"
    ] is False
    assert observation["supporting_sources"][0]["status"] == (
        "captured-in-observation-record"
    )


def test_large_observation_uses_a_bounded_digest_summary(tmp_path: Path) -> None:
    record = _observation("ev_environment_observation_large", "ses_compiler", "cmp_compiler")
    record["observation"]["outcome"]["large_diagnostic"] = "x" * 60_000
    session, _ = _session(tmp_path, [record])

    result = capability_intelligence.project(session)
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True).encode("utf-8")

    assert result["status"] == "partial"
    assert len(encoded) <= capability_intelligence.MAX_RESULT_BYTES
    assert result["observations"][0]["observation"]["details_omitted"] is True
    assert result["observations"][0]["evidence_reference"]["identity"] == record["identity"]
    assert result["observations"][0]["recording_context"]["campaign_id"] == "cmp_observation_capture"


def test_observation_reader_stops_after_its_total_deadline(monkeypatch) -> None:
    def unexpected_git(*_args, **_kwargs):
        raise AssertionError("Git should not run after the projection read deadline")

    monkeypatch.setattr(capability_intelligence.subprocess, "run", unexpected_git)

    content, status = capability_intelligence._git_blob(
        Path("/unselected"), "a" * 40, "evidence/record.json",
        deadline=time.monotonic() - 1,
    )

    assert content is None
    assert status == "read-budget-exhausted"


def test_recurrence_requires_distinct_recorded_observations_and_sessions(tmp_path: Path) -> None:
    one = _observation("ev_environment_observation_one", "ses_compiler_one", "cmp_compiler")
    two = _observation("ev_environment_observation_two", "ses_compiler_two", "cmp_compiler")
    session, _ = _session(tmp_path, [one, two])

    result = capability_intelligence.project(session)

    hypothesis = result["pressure_hypotheses"][0]
    assert hypothesis["observation_count"] == 2
    assert hypothesis["distinct_session_count"] == 2
    assert hypothesis["recurrence_status"] == "observed-across-sessions"


def test_cli_history_pages_correlate_pressure_within_bounded_read_budget(
    tmp_path: Path, monkeypatch,
) -> None:
    reports = [
        _observation(
            f"ev_environment_pressure_history_{index:02d}",
            "ses_pressure_history_old" if index < 9 else "ses_pressure_history_new",
            "cmp_pressure_history",
        )
        for index in range(18)
    ]
    session, full_state = _session(tmp_path, reports)
    references = full_state["evidence"]
    latest_page = {
        **full_state,
        "evidence": references[9:],
        "evidence_offset": 9,
        "evidence_limit": 9,
        "previous_evidence_offset": 0,
        "next_evidence_offset": None,
        "evidence_truncated": True,
    }
    older_page = {
        **full_state,
        "evidence": references[:9],
        "evidence_offset": 0,
        "evidence_limit": 9,
        "previous_evidence_offset": None,
        "next_evidence_offset": 9,
        "evidence_truncated": True,
    }

    def campaign_state(*, evidence_offset=None, evidence_limit=32):
        del evidence_limit
        return older_page if evidence_offset == 0 else latest_page

    session.campaign_state = campaign_state
    session.close = lambda: None
    monkeypatch.setattr(cli, "_open", lambda _args: session)
    output = io.StringIO()
    with redirect_stdout(output):
        status = cli.main([
            "--state-dir", str(tmp_path), "--persistence", "file",
            "projections", session.session_id, "--interpret", "environmental-capability",
            "--history-pages", "2",
        ])

    projection = json.loads(output.getvalue())
    hypothesis = projection["pressure_hypotheses"][0]
    assert status == 0
    assert hypothesis["recurrence_status"] == "observed-across-sessions"
    assert hypothesis["observation_count"] == 16
    assert hypothesis["distinct_session_count"] == 2
    assert len(hypothesis["evidence"]) == 16
    assert projection["source"]["evidence_history_pages_requested"] == 2
    assert projection["source"]["evidence_history_pages_read"] == 2
    assert projection["source"]["evidence_history_page_offsets"] == [9, 0]
    assert projection["source"]["evidence_history_truncated"] is False
    assert projection["source"]["evidence_history_checkpoint_consistent"] is True
    assert projection["source"]["evidence_history_observation_budgets_by_page"] == [
        {
            "offset": 9,
            "page_limit": 9,
            "observation_reference_capacity": 9,
            "read_budget": 8,
        },
        {
            "offset": 0,
            "page_limit": 9,
            "observation_reference_capacity": 9,
            "read_budget": 8,
        },
    ]
    assert projection["source"]["observation_records_skipped_by_page_budget"] == 2
    assert projection["source"]["observation_records_verified"] == 16
    assert projection["source"]["observation_records_unavailable"] == 0
    assert projection["status"] == "partial"

    compact = capability_intelligence.project(
        session, campaign_state=latest_page, max_result_bytes=5_000,
        history_pages=2,
    )
    assert compact["source"]["response_truncated"] is True
    assert compact["source"]["evidence_history_page_offsets"] == [9, 0]
    assert compact["source"]["evidence_history_page_limits"] == [9, 9]
    assert compact["source"]["evidence_history_truncated"] is False
    assert compact["source"]["evidence_history_checkpoint_consistent"] is True
    assert compact["source"]["evidence_history_observation_budgets_by_page"] == projection["source"][
        "evidence_history_observation_budgets_by_page"
    ]
    assert compact["source"]["observation_records_skipped_by_page_budget"] == 2

    changed_checkpoint = {**older_page, "checkpoint_id": "chk_intervening_update"}
    session.campaign_state = lambda *, evidence_offset=None, evidence_limit=32: (
        changed_checkpoint if evidence_offset == 0 else latest_page
    )
    inconsistent = capability_intelligence.project(
        session, campaign_state=latest_page, history_pages=2,
    )
    assert inconsistent["source"]["evidence_history_pages_read"] == 1
    assert inconsistent["source"]["evidence_history_checkpoint_consistent"] is False
    assert inconsistent["source"]["evidence_history_truncated"] is True
    assert inconsistent["status"] == "partial"


def test_history_window_uses_cursor_distance_for_short_first_page(tmp_path: Path) -> None:
    reports = [
        _observation(
            f"ev_environment_short_history_{index:02d}",
            "ses_short_history_old" if index < 9 else "ses_short_history_new",
            "cmp_short_history",
        )
        for index in range(17)
    ]
    session, full_state = _session(tmp_path, reports)
    references = full_state["evidence"]
    latest_page = {
        **full_state,
        "evidence": references[9:],
        "evidence_offset": 9,
        "evidence_limit": 8,
        "previous_evidence_offset": 1,
        "next_evidence_offset": None,
        "evidence_truncated": True,
    }
    middle_page = {
        **full_state,
        "evidence": references[1:9],
        "evidence_offset": 1,
        "evidence_limit": 8,
        "previous_evidence_offset": 0,
        "next_evidence_offset": 9,
        "evidence_truncated": True,
    }
    first_page = {
        **full_state,
        "evidence": references[:1],
        "evidence_offset": 0,
        "evidence_limit": 1,
        "previous_evidence_offset": None,
        "next_evidence_offset": 1,
        "evidence_truncated": True,
    }
    requests = []

    def campaign_state(*, evidence_offset=None, evidence_limit=32):
        requests.append((evidence_offset, evidence_limit))
        return middle_page if evidence_offset == 1 else first_page

    session.campaign_state = campaign_state
    result = capability_intelligence.project(
        session, campaign_state=latest_page, history_pages=3,
    )

    hypothesis = result["pressure_hypotheses"][0]
    assert requests == [(1, 8), (0, 1)]
    assert result["source"]["evidence_history_page_offsets"] == [9, 1, 0]
    assert result["source"]["evidence_history_page_limits"] == [8, 8, 1]
    assert result["source"]["observation_records_considered"] == 17
    assert result["source"]["observation_records_verified"] == 16
    assert result["source"]["observation_records_skipped_by_page_budget"] == 1
    assert result["source"]["evidence_history_observation_budgets_by_page"] == [
        {"offset": 9, "page_limit": 8, "observation_reference_capacity": 8, "read_budget": 8},
        {"offset": 1, "page_limit": 8, "observation_reference_capacity": 8, "read_budget": 7},
        {"offset": 0, "page_limit": 1, "observation_reference_capacity": 1, "read_budget": 1},
    ]
    assert result["source"]["evidence_history_truncated"] is False
    assert hypothesis["recurrence_status"] == "observed-across-sessions"
    assert hypothesis["observation_count"] == 16
    assert hypothesis["distinct_session_count"] == 2


def test_repeated_ledger_pointer_does_not_establish_recurrence(tmp_path: Path) -> None:
    record = _observation("ev_environment_observation_one", "ses_compiler", "cmp_compiler")
    session, state = _session(tmp_path, [record])
    state["evidence"].append(dict(state["evidence"][0]))
    state["evidence_count"] = 2

    result = capability_intelligence.project(session)

    assert result["source"]["observation_records_considered"] == 2
    assert result["observation_count"] == 1
    assert result["pressure_hypotheses"][0]["observation_count"] == 1
    assert result["pressure_hypotheses"][0]["recurrence_status"] == "not-established"


def test_missing_campaign_state_is_reported_without_inference() -> None:
    session = SimpleNamespace(
        session_id="ses_without_campaign_state",
        snapshot={"campaign": {"identity": "cmp_without_state"}, "intent": {}},
        campaign_state=lambda **_kwargs: (_ for _ in ()).throw(LifecycleError("no state")),
    )

    result = capability_intelligence.project(session)

    assert result["status"] == "unavailable"
    assert result["observation_count"] == 0
    assert result["pressure_hypothesis_count"] == 0


def test_campaign_state_and_projection_interfaces_expose_the_derived_view(
    tmp_path: Path, monkeypatch,
) -> None:
    record = _observation("ev_environment_observation_one", "ses_compiler_one", "cmp_compiler")
    session, _state = _session(tmp_path, [record])
    session.close = lambda: None
    monkeypatch.setattr(cli, "_open", lambda _args: session)

    output = io.StringIO()
    with redirect_stdout(output):
        status = cli.main([
            "--state-dir", str(tmp_path), "--persistence", "file",
            "campaign-state", session.session_id, "--evidence-limit", "32",
        ])
    campaign_state = json.loads(output.getvalue())
    projection = projections.interpretation(
        session, None, "environmental-capability",
    )

    assert status == 0
    assert campaign_state["environmental_capability"]["identity"] == projection["identity"]
    assert campaign_state["environmental_capability"]["observation_count"] == 1


def test_campaign_state_projection_respects_capability_output_budget(
    tmp_path: Path, monkeypatch,
) -> None:
    session, state = _session(
        tmp_path,
        [_observation("ev_environment_observation_seed", "ses_seed", "cmp_seed")],
    )
    state["evidence"] = [
        {
            "identity": f"ev_external_{index:03d}",
            "repository": "mncs-external",
            "commit": "a" * 40,
            "path": "p" * 512,
            "sha256": "b" * 64,
        }
        for index in range(128)
    ]
    state["evidence_count"] = len(state["evidence"])
    state["delivery"]["diagnostic"] = ""
    base_size = len(json.dumps(state, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    state["delivery"]["diagnostic"] = "x" * max(
        0, cli.CAMPAIGN_STATE_OUTPUT_LIMIT_BYTES - 6 * 1024 - base_size,
    )
    session.close = lambda: None
    monkeypatch.setattr(cli, "_open", lambda _args: session)

    output = io.StringIO()
    with redirect_stdout(output):
        status = cli.main([
            "--state-dir", str(tmp_path), "--persistence", "file",
            "campaign-state", session.session_id, "--evidence-limit", "128",
        ])

    campaign_state = json.loads(output.getvalue())
    assert status == 0
    assert "environmental_capability" in campaign_state
    assert len(output.getvalue().encode("utf-8")) <= cli.CAMPAIGN_STATE_OUTPUT_LIMIT_BYTES + 1
