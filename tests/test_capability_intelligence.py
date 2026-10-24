from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
    if references:
        _git(checkout, "commit", "--quiet", "-m", "test observation records")
    else:
        _git(checkout, "commit", "--allow-empty", "--quiet", "-m", "test observation records")
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
            "authenticated_principal_id": "ctrl_" + "a" * 32,
            "provenance": {"definition_id": "env_projection_reader"},
            "intent": {"identity": "int_projection_reader", "goal": "inspect current pressures"},
            "campaign": {"identity": "cmp_projection_reader"},
            "workspace": {"root": str(root)},
            "selected_checkouts": {"mncs-environment": {"path": str(checkout)}},
        },
        campaign_state=lambda **_kwargs: state,
        recent_events=lambda *, limit, before_sequence=None: {
            "event_count": 0, "events": [], "truncated": False,
            "next_before_sequence": None,
        },
    )
    return session, state


def _add_compiler_regression_evidence(
    session, state: dict, report: dict,
) -> dict[str, str]:
    root = Path(session.snapshot["workspace"]["root"])
    checkout = root / "mncs-compiler"
    if not checkout.exists():
        checkout.mkdir()
        _git(checkout, "init", "--quiet")
        _git(checkout, "config", "user.name", "test")
        _git(checkout, "config", "user.email", "test@example.invalid")
    reference_paths = {
        identity_prefix: f"{path_prefix}20261024.json"
        for _family, identity_prefix, path_prefix, _schema, _stage_names
        in capability_intelligence.COMPILER_CACHE_REGRESSION_REFERENCE_FAMILIES
    }
    relative = next(
        (path for prefix, path in reference_paths.items()
         if report["identity"].startswith(prefix)),
        "evidence/native-app-readiness-effect-capability-fact-invalidation-20261024.json",
    )
    path = checkout / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    path.write_bytes(raw)
    _git(checkout, "add", relative)
    _git(checkout, "commit", "--quiet", "-m", "compiler regression evidence")
    reference = {
        "identity": report["identity"],
        "repository": "mncs-compiler",
        "commit": _git(checkout, "rev-parse", "HEAD"),
        "path": relative,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    session.snapshot["selected_checkouts"]["mncs-compiler"] = {
        "path": str(checkout),
    }
    state["evidence"].append(reference)
    state["evidence_count"] = len(state["evidence"])
    return reference


def _add_compiler_dependency_summary_evidence(
    session, state: dict, report: dict,
) -> dict[str, str]:
    root = Path(session.snapshot["workspace"]["root"])
    checkout = root / "mncs-compiler"
    if not checkout.exists():
        checkout.mkdir()
        _git(checkout, "init", "--quiet")
        _git(checkout, "config", "user.name", "test")
        _git(checkout, "config", "user.email", "test@example.invalid")
    relative = (
        "evidence/language-verification-dependency-summary-cache-"
        "20261023T064052.991817Z.json"
    )
    path = checkout / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    path.write_bytes(raw)
    _git(checkout, "add", relative)
    _git(checkout, "commit", "--quiet", "-m", "compiler dependency summary evidence")
    reference = {
        "identity": "ev_compiler_language_fact_reuse_workload_20261023",
        "repository": "mncs-compiler",
        "commit": _git(checkout, "rev-parse", "HEAD"),
        "path": relative,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    session.snapshot["selected_checkouts"]["mncs-compiler"] = {
        "path": str(checkout),
    }
    state["evidence"].append(reference)
    state["evidence_count"] = len(state["evidence"])
    return reference


def _add_compiler_source_context(
    session, state: dict, source_reference: dict[str, str], *,
    identity: str = "ev_environment_compiler_source_context",
    work_intent_goal: str = "Advance reliable native application development",
) -> dict[str, str]:
    checkout = Path(session.snapshot["selected_checkouts"]["mncs-environment"]["path"])
    relative = f"evidence/environmental-capability/{identity}.json"
    record = {
        "schema_version": capability_intelligence.OBSERVATION_SCHEMA,
        "identity": identity,
        "captured_at": "2026-10-24T00:00:00Z",
        "measurement_basis": "verified-compiler-campaign-supporting-record",
        "related_work": {
            "campaign_id": "cmp_mncs_native_application_readiness_compiler_20261013",
            "session_id": "ses_164edc158d9b1637",
            "work_intent_id": "int_compiler_native_readiness",
            "work_intent_goal": work_intent_goal,
            "environment_id": "renv_compiler_campaign",
        },
        "observation": {
            "kind": "compiler-source-campaign-context",
            "source_interface": "MNCS Environment supporting-source relation",
            "request": {},
            "outcome": {"source_pointer_content_integrity": "verified"},
        },
        "supporting_sources": [{
            "kind": "compiler-regression-source-record",
            **source_reference,
        }],
    }
    raw = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    path = checkout / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    _git(checkout, "add", relative)
    _git(checkout, "commit", "--quiet", "-m", "test compiler source context")
    reference = {
        "identity": record["identity"],
        "repository": "mncs-environment",
        "commit": _git(checkout, "rev-parse", "HEAD"),
        "path": relative,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    state["evidence"].append(reference)
    state["evidence_count"] = len(state["evidence"])
    return reference


def _add_campaign_charter(
    session, state: dict, identity: str, objective: str,
) -> dict[str, str]:
    checkout = Path(session.snapshot["selected_checkouts"]["mncs-environment"]["path"])
    relative = f"evidence/campaign-charters/{identity}.json"
    record = {
        "schema_version": "mncs.environment.campaign-charter/1",
        "identity": identity,
        "recorded_at": "2026-10-24T00:00:00Z",
        "campaign_identity": session.snapshot["campaign"]["identity"],
        "source": {
            "kind": "user-directed test campaign",
            "request_title": "Test campaign objective",
        },
        "objective": objective,
        "authority_boundary": "Records intent and grants no repository authority.",
    }
    raw = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
    path = checkout / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    _git(checkout, "add", relative)
    _git(checkout, "commit", "--quiet", "-m", "test campaign charter")
    reference = {
        "identity": identity,
        "repository": "mncs-environment",
        "commit": _git(checkout, "rev-parse", "HEAD"),
        "path": relative,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    index = state.setdefault("campaign_charter_evidence", {
        "count": 0, "references": [], "truncated": False,
    })
    index["references"].append(reference)
    index["count"] = len(index["references"])
    return reference


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
    record["supporting_sources"].append({
        "kind": "direct-cli-projection-result",
        "campaign_id": "cmp_compiler",
        "session_id": "ses_compiler_one",
    })
    record["recorder"].update({
        "environment_id": "renv_observation_capture",
        "definition_id": "env_observation_capture",
    })
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
        "environment_id": "renv_observation_capture",
        "definition_id": "env_observation_capture",
        "environment_identity_capture_status": "captured-in-record",
        "environment_version_capture_status": "not-captured-in-record",
    }
    hypothesis = result["pressure_hypotheses"][0]
    assert hypothesis["observation_count"] == 1
    assert hypothesis["distinct_session_count"] == 1
    assert hypothesis["recurrence_status"] == "not-established"
    assert result["observations"][0]["observation"]["outcome"]["continuations_count"] == 3
    assert result["observations"][0]["supporting_sources"][0]["status"] == "captured-in-observation-record"
    assert result["observations"][0]["supporting_sources"][1]["status"] == (
        "captured-in-observation-record"
    )
    assert result["observations"][0]["supporting_sources"][1][
        "content_integrity_status"
    ] == "captured-in-observation-record"


def test_project_links_unselected_work_intent_repository_to_evidence_visibility(
    tmp_path: Path,
) -> None:
    record = _observation(
        "ev_environment_work_scope_alignment",
        "ses_compiler_scope_alignment",
        "cmp_compiler_scope_alignment",
    )
    outside_reference = {
        "identity": "ev_environment_compiler_source_outside_selection",
        "repository": "mncs-compiler",
        "commit": "a" * 40,
        "path": "evidence/environmental-capability/compiler-observation.json",
        "sha256": "b" * 64,
    }
    session, _state = _session(
        tmp_path, [record], extra_refs=[outside_reference],
    )
    session.snapshot["intent"]["repositories"] = ["mncs-compiler"]

    result = capability_intelligence.project(session)

    assert result["status"] == "partial"
    assert result["subject"]["workspace_alignment"] == {
        "status": "declared-repositories-not-selected",
        "declared_repositories": ["mncs-compiler"],
        "selected_repositories": ["mncs-environment"],
        "unselected_declared_repositories": ["mncs-compiler"],
        "basis": "session-work-intent-and-selected-checkout-identities",
    }
    assert result["source"]["evidence_visibility"] == {
        "status": "outside-selected-session-references-observed",
        "verification_diagnostic_count": 1,
        "outside_selected_reference_diagnostic_count": 1,
        "outside_selected_repositories": [
            {"repository": "mncs-compiler", "diagnostic_count": 1},
        ],
        "diagnostics_truncated": False,
        "basis": "bounded-campaign-evidence-source-verification-diagnostics",
    }
    assert result["source"]["observation_reference_diagnostics"][0][
        "verification"
    ] == "outside-selected-session"

    compact = capability_intelligence.project(session, max_result_bytes=1_500)
    assert compact["subject"]["workspace_alignment"][
        "unselected_declared_repositories"
    ] == ["mncs-compiler"]
    assert compact["source"]["evidence_visibility"][
        "outside_selected_repositories"
    ] == [{"repository": "mncs-compiler", "diagnostic_count": 1}]


def test_project_omits_diagnostics_when_work_intent_repositories_are_aligned(
    tmp_path: Path,
) -> None:
    session, _state = _session(tmp_path, [])
    session.snapshot["intent"]["repositories"] = ["mncs-environment"]

    result = capability_intelligence.project(session)

    assert "workspace_alignment" not in result["subject"]
    assert "evidence_visibility" not in result["source"]


def test_live_compiler_scope_records_link_evidence_and_preserve_uncertainty(
    tmp_path: Path,
) -> None:
    evidence_dir = Path(__file__).resolve().parents[1] / (
        "evidence/environmental-capability"
    )
    identities = (
        "workspace-scope-visibility-pressure-20261024",
        "workspace-scope-visibility-live-readback-20261024",
        "workspace-scope-visibility-improvement-20261024",
    )
    records = [
        json.loads((evidence_dir / f"{identity}.json").read_text(encoding="utf-8"))
        for identity in identities
    ]
    outside_refs = [
        {
            "identity": "ev_compiler_language_fact_reuse_workload_20261023",
            "repository": "mncs-compiler",
            "commit": "e2924f6d602a122de1121aa273eb24f9795784bc",
            "path": "evidence/language-verification-dependency-summary-cache-20261023T064052.991817Z.json",
            "sha256": "128d4115ddb614cbe249523f162ae711bba5ca990e98dc21ac3b96cd78f1185d",
        },
        {
            "identity": "ev_compiler_digest_application_interface_invalidation_20261023",
            "repository": "mncs-compiler",
            "commit": "c9d54cdfcf8c03fce0a1a904d32e147d7f287a9d",
            "path": "evidence/language-module-interface-application-invalidation-compiler-20261023.json",
            "sha256": "ce67f0ff837bba0ff73301cb398198da5ff9125e8fe8cb75c45c5f26a1715177",
        },
        {
            "identity": "ev_compiler_digest_application_generic_specialization_invalidation_20261023",
            "repository": "mncs-compiler",
            "commit": "e874669dbd110c5a5f808249c20a086013d7e2be",
            "path": "evidence/language-module-generic-specialization-application-invalidation-compiler-20261023.json",
            "sha256": "54472426b85dd4cc4a1d2c327faf783490c96826a5d2876067344dff22324b4e",
        },
        {
            "identity": "ev_compiler_digest_application_effect_capability_invalidation_20261024",
            "repository": "mncs-compiler",
            "commit": "f4f65919f4389af4f60f37aa8d36cc1ae161962d",
            "path": "evidence/native-app-readiness-effect-capability-fact-invalidation-20261024.json",
            "sha256": "8d3c3ec91b9166a88b2a5e3c98c8631b3f0a098dfa7028c095cf3a8769dc9f12",
        },
    ]
    session, _state = _session(tmp_path, records, extra_refs=outside_refs)
    session.snapshot["intent"]["repositories"] = ["mncs-compiler"]

    result = capability_intelligence.project(session)

    assert result["status"] == "partial"
    assert result["source"]["observation_records_verified"] == 3
    assert result["subject"]["workspace_alignment"][
        "unselected_declared_repositories"
    ] == ["mncs-compiler"]
    assert result["source"]["evidence_visibility"][
        "outside_selected_reference_diagnostic_count"
    ] == 4
    hypothesis = next(
        item for item in result["pressure_hypotheses"]
        if item["identity"] == "ph_environment_workspace_scope_visibility_20261024"
    )
    assert hypothesis["observation_count"] == 1
    relation = next(
        item for item in result["improvement_relations"]
        if item["identity"] == "im_environment_workspace_scope_visibility_20261024"
    )
    assert relation["verification_record_status"] == (
        "record-integrity-verified-in-projection"
    )
    assert relation["effectiveness_assessment"] == "not-assessed"
    assert relation["subsequent_record_status"] == "not-recorded"


def test_project_exposes_verified_campaign_objective_separately_from_work_intent(
    tmp_path: Path,
) -> None:
    record = _observation(
        "ev_environment_campaign_charter_context",
        "ses_charter_context",
        "cmp_projection_reader",
    )
    session, state = _session(tmp_path, [record])
    objective = "Grow MNCS capability from real development evidence." + (
        " Expand supported work." * 40
    )
    reference = _add_campaign_charter(
        session, state,
        "ev_environment_campaign_charter_context",
        objective,
    )

    result = capability_intelligence.project(session)
    charter = result["subject"]["campaign_charter"]
    assert charter["status"] == "verified"
    assert charter["candidate_reference_count"] == 1
    assert charter["references_examined"] == 1
    assert charter["identity"] == reference["identity"]
    assert charter["objective"] == objective
    assert charter["evidence_reference"] == reference
    assert result["subject"]["work_intent"] == {
        "identity": "int_projection_reader",
        "goal": "inspect current pressures",
    }
    assert result["source"]["campaign_charter_reference_count"] == 1

    compact = capability_intelligence.project(session, max_result_bytes=1_500)
    compact_charter = compact["subject"]["campaign_charter"]
    assert compact_charter["status"] == "verified"
    assert compact_charter["objective_truncated"] is True
    assert compact_charter["evidence_reference"]["sha256"] == reference["sha256"]
    assert compact["subject"]["work_intent"]["goal"] == "inspect current pressures"


def test_project_does_not_choose_between_multiple_verified_campaign_charters(
    tmp_path: Path,
) -> None:
    record = _observation(
        "ev_environment_campaign_charter_ambiguous",
        "ses_charter_ambiguous",
        "cmp_projection_reader",
    )
    session, state = _session(tmp_path, [record])
    _add_campaign_charter(
        session, state, "ev_environment_campaign_charter_first", "First objective.",
    )
    _add_campaign_charter(
        session, state, "ev_environment_campaign_charter_second", "Second objective.",
    )

    charter = capability_intelligence.project(session)["subject"]["campaign_charter"]
    assert charter["status"] == "ambiguous"
    assert charter["valid_charter_count"] == 2
    assert "objective" not in charter


def test_project_marks_unverified_campaign_charter_pointer_partial(tmp_path: Path) -> None:
    record = _observation(
        "ev_environment_campaign_charter_bad_digest",
        "ses_charter_bad_digest",
        "cmp_projection_reader",
    )
    session, state = _session(tmp_path, [record])
    pointer = _add_campaign_charter(
        session, state, "ev_environment_campaign_charter_bad_digest", "Never trusted.",
    )
    pointer["sha256"] = "0" * 64

    charter = capability_intelligence.project(session)["subject"]["campaign_charter"]
    assert charter["status"] == "partial"
    assert charter["reason"] == "charter-candidates-not-fully-verified"
    assert "objective" not in charter


def test_project_reports_campaign_charter_as_not_recorded(tmp_path: Path) -> None:
    record = _observation(
        "ev_environment_campaign_charter_absent",
        "ses_charter_absent",
        "cmp_projection_reader",
    )
    session, state = _session(tmp_path, [record])
    state["campaign_charter_evidence"] = {
        "count": 0, "references": [], "truncated": False,
    }

    charter = capability_intelligence.project(session)["subject"]["campaign_charter"]
    assert charter == {"status": "not-recorded", "candidate_reference_count": 0}


def test_captured_session_activity_reference_is_bounded_and_inspectable(
    tmp_path: Path,
) -> None:
    event_ids = [f"evt_{index:016x}" for index in range(10)]
    record = _observation(
        "ev_environment_activity_source", "ses_compiler_activity", "cmp_compiler",
    )
    record["supporting_sources"].append({
        "kind": "authenticated-session-activity-projection",
        "repository": "mncs-compiler",
        "campaign_id": "cmp_compiler",
        "session_id": "ses_compiler_activity",
        "event_identities": event_ids,
    })
    record["observation"]["outcome"]["large_diagnostic"] = "x" * 10_000
    session, _ = _session(tmp_path, [record])

    result = capability_intelligence.project(session)

    source = result["observations"][0]["supporting_sources"][1]
    assert source["status"] == "captured-in-observation-record"
    assert source["content_integrity_status"] == "captured-in-observation-record"
    assert source["event_identities"] == event_ids

    compact = capability_intelligence.project(session, max_result_bytes=12_000)
    compact_source = next(
        source for observation in compact["observations"]
        for source in observation["supporting_sources"]
        if source.get("kind") == "authenticated-session-activity-projection"
    )
    assert compact_source["event_identities"] == event_ids[:8]
    assert compact_source["event_identities_truncated"] is True

    malformed = json.loads(json.dumps(record))
    malformed["identity"] = "ev_environment_activity_source_malformed"
    malformed["supporting_sources"][1]["event_identities"] = ["evt_bad event id"]
    session, _ = _session(tmp_path / "malformed", [malformed])

    result = capability_intelligence.project(session)

    source = result["observations"][0]["supporting_sources"][1]
    assert source["status"] == "invalid-source-reference"
    assert source["content_integrity_status"] == "source-reference-invalid"


def test_pressure_contexts_merge_sparse_identity_and_keep_recorders_distinct(
    tmp_path: Path,
) -> None:
    first = _observation(
        "ev_environment_context_sparse", "ses_compiler_work", "cmp_compiler_work",
    )
    first["related_work"].update({
        "work_intent_id": "int_compiler_work",
        "work_intent_goal": "advance verified self-consumption",
    })
    first["recorder"].update({
        "campaign_id": "cmp_environment_observer",
        "session_id": "ses_environment_observer_one",
    })

    second = _observation(
        "ev_environment_context_enriched", "ses_compiler_work", "cmp_compiler_work",
    )
    second["related_work"].update({
        "work_intent_id": "int_compiler_work",
        "work_intent_goal": "advance verified self-consumption",
        "campaign_objective": "Mature reliable multi-module native applications",
        "environment_id": "renv_compiler_work",
        "definition_id": "env_compiler_work",
        "environment_version": "compiler-env-v3",
    })
    second["recorder"].update({
        "campaign_id": "cmp_environment_observer",
        "session_id": "ses_environment_observer_two",
    })
    session, _ = _session(tmp_path, [first, second])

    result = capability_intelligence.project(session)

    pressure = result["pressure_hypotheses"][0]
    assert pressure["affected_work_context_count"] == 1
    assert pressure["affected_work_contexts"] == [{
        "campaign_id": "cmp_compiler_work",
        "session_id": "ses_compiler_work",
        "work_intent_id": "int_compiler_work",
        "work_intent_goal": "advance verified self-consumption",
        "campaign_objective": "Mature reliable multi-module native applications",
        "environment_id": "renv_compiler_work",
        "definition_id": "env_compiler_work",
        "environment_version": "compiler-env-v3",
    }]
    assert pressure["recording_context_count"] == 2
    assert pressure["distinct_recording_session_count"] == 2


def test_pressure_contexts_keep_conflicting_campaign_objectives_distinct(
    tmp_path: Path,
) -> None:
    first = _observation(
        "ev_environment_context_objective_first",
        "ses_compiler_objective",
        "cmp_compiler_objective",
    )
    second = _observation(
        "ev_environment_context_objective_second",
        "ses_compiler_objective",
        "cmp_compiler_objective",
    )
    for record in (first, second):
        record["related_work"].update({
            "work_intent_id": "int_compiler_objective",
            "work_intent_goal": "advance verified self-consumption",
        })
    first["related_work"]["campaign_objective"] = "Mature native application support"
    second["related_work"]["campaign_objective"] = "Advance compiler self-consumption"
    session, _ = _session(tmp_path, [first, second])

    result = capability_intelligence.project(session)

    contexts = result["pressure_hypotheses"][0]["affected_work_contexts"]
    assert len(contexts) == 2
    assert {context["campaign_objective"] for context in contexts} == {
        "Mature native application support",
        "Advance compiler self-consumption",
    }


def test_sparse_pressure_context_stays_separate_when_identity_is_ambiguous(
    tmp_path: Path,
) -> None:
    sparse = _observation(
        "ev_environment_context_ambiguous_sparse",
        "ses_compiler_ambiguous",
        "cmp_compiler_ambiguous",
    )
    sparse["related_work"].pop("work_intent_id")
    sparse["related_work"].pop("work_intent_goal")
    first_intent = _observation(
        "ev_environment_context_ambiguous_first",
        "ses_compiler_ambiguous",
        "cmp_compiler_ambiguous",
    )
    first_intent["related_work"].update({
        "work_intent_id": "int_compiler_first",
        "work_intent_goal": "advance the first compiler capability",
    })
    second_intent = _observation(
        "ev_environment_context_ambiguous_second",
        "ses_compiler_ambiguous",
        "cmp_compiler_ambiguous",
    )
    second_intent["related_work"].update({
        "work_intent_id": "int_compiler_second",
        "work_intent_goal": "advance the second compiler capability",
    })
    session, _ = _session(tmp_path, [sparse, first_intent, second_intent])

    result = capability_intelligence.project(session)

    contexts = result["pressure_hypotheses"][0]["affected_work_contexts"]
    assert len(contexts) == 3
    assert sum("work_intent_id" not in context for context in contexts) == 1
    assert {context.get("work_intent_id") for context in contexts} == {
        None, "int_compiler_first", "int_compiler_second",
    }


def test_project_correlates_recent_capability_events_without_exposing_arguments(
    tmp_path: Path,
) -> None:
    record = _observation(
        "ev_environment_activity_observation",
        "ses_compiler_activity",
        "cmp_compiler_activity",
    )
    session, _ = _session(tmp_path, [record])
    request_id = "req_compiler_activity"
    events = [
        {
            "identity": "evt_capability_invoked_1", "sequence": 11,
            "type": "capability.invoked", "producer": "local-agent",
            "observed_at": "2026-10-24T01:00:00Z",
            "payload": {
                "capability": "mncs-compiler:build",
                "provider_request_id": request_id,
                "argv": ["build", "private-source-path"],
            },
        },
        {
            "identity": "evt_invocation_completed_1", "sequence": 12,
            "type": "invocation.completed", "producer": "mncs-compiler",
            "observed_at": "2026-10-24T01:00:02Z",
            "payload": {
                "capability": "mncs-compiler:build", "status": "failed",
                "returncode": 2, "provider_request_id": request_id,
            },
        },
        {
            "identity": "evt_capability_invoked_2", "sequence": 13,
            "type": "capability.invoked", "producer": "local-agent",
            "observed_at": "2026-10-24T01:01:00Z",
            "payload": {
                "capability": "mncs-compiler:build",
                "provider_request_id": "req_compiler_activity_pending",
                "argv": ["build", "private-source-path"],
            },
        },
    ]
    session.recent_events = lambda *, limit: {
        "event_count": len(events), "events": events[-limit:], "truncated": False,
    }

    result = capability_intelligence.project(session)

    activity = result["session_activity"]
    assert activity["status"] == "ready"
    assert activity["context"]["campaign_id"] == "cmp_projection_reader"
    assert activity["context"]["work_intent_goal"] == "inspect current pressures"
    assert activity["context"]["environment_id"] == "renv_projection_reader"
    assert activity["source"]["invocation_events_considered"] == 2
    assert activity["interaction_count"] == 2
    latest, prior = activity["interactions"]
    assert latest["correlation_status"] == "completion-not-recorded-in-window"
    assert latest["completion_event"] is None
    assert prior["correlation_status"] == "matched-by-provider-request-id"
    assert prior["outcome_status"] == "failed"
    assert prior["returncode"] == 2
    assert prior["invocation_event"]["identity"] == "evt_capability_invoked_1"
    assert prior["completion_event"]["identity"] == "evt_invocation_completed_1"
    assert activity["capabilities"] == [{
        "capability": "mncs-compiler:build",
        "invocation_count": 2,
        "completion_count": 1,
        "outcome_status_counts": {"failed": 1},
    }]
    assert "private-source-path" not in json.dumps(activity)
    assert any("token usage" in item for item in activity["measurement_limits"])


def test_session_resource_sample_is_correlated_sanitized_and_compactable(
    tmp_path: Path,
) -> None:
    record = _observation(
        "ev_environment_resource_sample_activity",
        "ses_resource_sample_activity",
        "cmp_resource_sample_activity",
    )
    record["observation"]["outcome"]["large_diagnostic"] = "private " * 9_000
    session, _ = _session(tmp_path, [record])
    capability = "mncs-system-monitor:system-resource-sample"
    binding_id = "cap_monitor_resource_sample"
    request_id = "req_monitor_resource_sample"
    store_reference = "mncs-store:invocation-result:resource-sample"
    session.snapshot["bindings"] = [{
        "binding_id": binding_id,
        "capability": capability,
        "provider": "mncs-system-monitor",
        "contract_revision": "1",
    }]
    session.snapshot["artifacts"] = [{
        "kind": "invocation-result",
        "capability": capability,
        "binding_id": binding_id,
        "provider_request_id": request_id,
        "status": "ok",
        "artifact_store_ref": store_reference,
        "at": "2026-10-24T01:00:01Z",
    }]
    events = [
        {
            "identity": "evt_monitor_resource_sample_invoked",
            "sequence": 51,
            "type": "capability.invoked",
            "observed_at": "2026-10-24T01:00:00Z",
            "payload": {
                "capability": capability,
                "provider_request_id": request_id,
                "argv": ["private-argument-is-not-projected"],
            },
        },
        {
            "identity": "evt_monitor_resource_sample_completed",
            "sequence": 52,
            "type": "invocation.completed",
            "observed_at": "2026-10-24T01:00:01Z",
            "payload": {
                "capability": capability,
                "provider_request_id": request_id,
                "status": "ok",
                "artifact_store_ref": store_reference,
            },
        },
    ]
    session.recent_events = lambda *, limit, before_sequence=None: {
        "event_count": len(events), "events": events[-limit:],
        "truncated": False, "next_before_sequence": None,
    }
    content = json.dumps({
        "schema": capability_intelligence.SYSTEM_RESOURCE_SAMPLE_SCHEMA,
        "observed_at": 1792825200000,
        "sample_time": {
            "wall_time_unix_ms": 1792825200000,
            "monotonic_ns": 123456789,
        },
        "host": {
            "cpu_count": 8,
            "cpu_utilization": 0.25,
            "cpu_status": "observed",
            "load_average": [0.5, 0.75, 1.0],
            "memory_total_bytes": 32_000_000_000,
            "memory_available_bytes": 12_000_000_000,
            "memory_available_status": "observed",
            "memory_free_bytes": 4_000_000_000,
        },
        "hostname": "private-host-name",
        "processes": [{"command": "private-process-command"}],
    }, sort_keys=True).encode()
    metadata = {
        "store_reference": store_reference,
        "path": "system-resource-sample.json",
        "byte_count": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }

    with patch.object(
        capability_intelligence.invocation_artifacts_module,
        "read_latest_invocation_file",
        return_value=(metadata, content),
    ) as read_artifact:
        result = capability_intelligence.project(session)
        compact = capability_intelligence.project(
            session, max_result_bytes=10_000,
        )
        with patch.object(
            session, "campaign_state",
            side_effect=LifecycleError("campaign evidence unavailable"),
        ):
            activity_only = capability_intelligence.project(session)

    samples = result["session_activity"]["resource_samples"]
    assert samples["status"] == "available"
    assert samples["observation_count"] == 1
    observation = samples["observations"][0]
    assert observation["status"] == "available"
    assert observation["context"] == {
        "session_id": "ses_projection_reader",
        "campaign_id": "cmp_projection_reader",
        "work_intent_id": "int_projection_reader",
        "environment_id": "renv_projection_reader",
        "definition_id": "env_projection_reader",
    }
    assert observation["source"]["event_correlation_status"] == (
        "matched-by-provider-request-id"
    )
    assert observation["source"]["invocation_event"]["identity"] == (
        "evt_monitor_resource_sample_invoked"
    )
    assert observation["source"]["completion_event"]["identity"] == (
        "evt_monitor_resource_sample_completed"
    )
    assert observation["source"]["artifact_reference"] == {
        **metadata, "path": "system-resource-sample.json",
    }
    assert observation["sample"]["host"] == {
        "cpu_count": 8,
        "cpu_utilization": 0.25,
        "cpu_status": "observed",
        "load_average": [0.5, 0.75, 1.0],
        "memory_total_bytes": 32_000_000_000,
        "memory_available_bytes": 12_000_000_000,
        "memory_available_status": "observed",
    }
    assert observation["sample"]["measurement_basis"] == (
        "system-monitor-reported-resource-sample"
    )
    assert observation["sample"]["measurement_availability"] == {
        "host.cpu_count": "measured",
        "host.cpu_utilization": "derived",
        "host.load_average": "measured",
        "host.memory_total_bytes": "measured",
        "host.memory_available_bytes": "measured",
    }
    assert read_artifact.call_count == 3
    assert activity_only["status"] == "partial"
    assert activity_only["session_activity"]["resource_samples"][
        "observation_count"
    ] == 1
    assert "private-host-name" not in json.dumps(result)
    assert "private-process-command" not in json.dumps(result)
    assert "private-argument-is-not-projected" not in json.dumps(result)
    assert "large_diagnostic" not in json.dumps(compact)
    compact_samples = compact["session_activity"]["resource_samples"]
    assert compact["source"]["response_truncated"] is True
    assert compact_samples["observations"][0]["sample"]["host"][
        "memory_available_bytes"
    ] == 12_000_000_000
    assert compact_samples["observations"][0]["source"][
        "artifact_reference"]["sha256"] == metadata["sha256"]


def test_session_resource_sample_distinguishes_missing_and_failed_invocations(
    tmp_path: Path,
) -> None:
    record = _observation(
        "ev_environment_resource_sample_absence",
        "ses_resource_sample_absence",
        "cmp_resource_sample_absence",
    )
    session, _ = _session(tmp_path, [record])

    absent = capability_intelligence.project(session)["session_activity"][
        "resource_samples"
    ]
    assert absent["status"] == "not-captured-in-session"
    assert absent["observation_count"] == 0
    assert absent["observations"] == []

    session.snapshot["artifacts"] = [{
        "kind": "invocation-result",
        "capability": capability_intelligence.SYSTEM_RESOURCE_SAMPLE_CAPABILITY,
        "binding_id": "cap_monitor_failed_sample",
        "provider_request_id": "req_monitor_failed_sample",
        "status": "failed",
    }]
    failed = capability_intelligence.project(session)["session_activity"][
        "resource_samples"
    ]
    assert failed["status"] == "latest-invocation-unsuccessful"
    assert failed["observation_count"] == 0
    assert "sample" not in failed["observations"][0]


def test_authority_denials_are_sanitized_correlated_and_actionable(tmp_path: Path) -> None:
    record = _observation(
        "ev_environment_authority_denial_activity",
        "ses_environment_authority_denial_activity",
        "cmp_environment_authority_denial_activity",
    )
    session, _ = _session(tmp_path, [record])
    checkout = session.snapshot["selected_checkouts"]["mncs-environment"]["path"]
    alias_checkout = str(Path(checkout) / ".worktrees" / "main-target")
    session.snapshot["repo_facts"] = {"mncs-environment@main-target": {
        "path": alias_checkout, "branch": "main",
    }}
    session.snapshot["workspace"]["repositories"] = [{
        "name": "mncs-environment@main-target",
        "path": alias_checkout,
        "branch": "main",
        "manifest_repository": "mncs-environment",
        "worktree_of": "mncs-environment",
    }]
    events = [
        {
            "identity": f"evt_authority_denied_{sequence}",
            "sequence": sequence,
            "type": "authority.denied",
            "payload": {
                "action": "merge",
                "target": alias_checkout,
                "reason": (
                    "private-reason-echo is outside this session's writable authority"
                ),
            },
        }
        for sequence in (41, 42)
    ]
    session.recent_events = lambda *, limit, before_sequence=None: {
        "event_count": len(events), "events": events[-limit:],
        "truncated": False, "next_before_sequence": None,
    }

    result = capability_intelligence.project(session)
    activity = result["session_activity"]
    assert activity["source"]["authority_denial_events_considered"] == 2
    assert activity["authority_denial_count"] == 2
    assert activity["authority_denials"][0]["repository"] == "mncs-environment"
    assert activity["authority_denials"][0]["action"] == "merge"
    assert activity["authority_denials"][0]["reason_class"] == (
        "outside-writable-authority"
    )
    assert activity["authority_denials"][0]["event_reference"]["sequence"] == 42
    assert result["session_activity_pressure_candidate_count"] == 1
    candidate = activity["authority_denial_candidates"][0]
    assert candidate["status"] == "candidate-needs-investigation"
    assert candidate["occurrence_count"] == 2
    assert candidate["distinct_session_count"] == 1
    assert candidate["causal_claim"] == "not-established"
    assert candidate["repository"] == "mncs-environment"
    assert alias_checkout not in json.dumps(result)
    assert "private-reason-echo" not in json.dumps(result)

    compact = capability_intelligence.project(session, max_result_bytes=5_000)
    compact_activity = compact["session_activity"]
    assert compact_activity["authority_denials"][0]["event_reference"][
        "identity"
    ] == "evt_authority_denied_42"
    assert compact_activity["authority_denial_candidates"][0][
        "supporting_events"
    ][0]["identity"] == "evt_authority_denied_41"
    assert alias_checkout not in json.dumps(compact)
    assert "private-reason-echo" not in json.dumps(compact)


def test_session_activity_discloses_bounded_event_and_interaction_windows(
    tmp_path: Path,
) -> None:
    record = _observation(
        "ev_environment_activity_window_observation",
        "ses_compiler_activity_window",
        "cmp_compiler_activity_window",
    )
    session, _ = _session(tmp_path, [record])
    events = []
    for index in range(capability_intelligence.MAX_SESSION_ACTIVITY_INTERACTIONS + 4):
        request_id = f"req_activity_{index}"
        events.extend((
            {
                "identity": f"evt_activity_invoke_{index}",
                "sequence": index * 2 + 1, "type": "capability.invoked",
                "payload": {"capability": "mncs-test:run",
                            "provider_request_id": request_id},
            },
            {
                "identity": f"evt_activity_complete_{index}",
                "sequence": index * 2 + 2, "type": "invocation.completed",
                "payload": {"capability": "mncs-test:run", "status": "ok",
                            "returncode": 0, "provider_request_id": request_id},
            },
        ))
    requested_limits = []

    def recent_events(*, limit):
        requested_limits.append(limit)
        return {
            "event_count": len(events), "events": events[-limit:],
            "truncated": len(events) > limit,
        }

    session.recent_events = recent_events

    result = capability_intelligence.project(session)

    activity = result["session_activity"]
    assert requested_limits == [capability_intelligence.MAX_SESSION_EVENT_WINDOW]
    assert activity["status"] == "partial"
    assert activity["interaction_count"] == capability_intelligence.MAX_SESSION_ACTIVITY_INTERACTIONS
    assert activity["source"]["invocation_records_omitted"] == 4
    assert activity["interactions"][0]["invocation_event"]["sequence"] == len(events) - 1


def test_session_activity_does_not_infer_absence_from_a_truncated_event_tail(
    tmp_path: Path,
) -> None:
    record = _observation(
        "ev_environment_activity_truncated_observation",
        "ses_compiler_activity_truncated",
        "cmp_compiler_activity_truncated",
    )
    session, _ = _session(tmp_path, [record])
    session.recent_events = lambda *, limit: {
        "event_count": limit + 1,
        "events": [
            {"identity": f"evt_{index}", "sequence": index + 1,
             "type": "session.checkpointed", "payload": {}}
            for index in range(limit)
        ],
        "truncated": True,
    }

    result = capability_intelligence.project(session)

    activity = result["session_activity"]
    assert activity["status"] == "partial-no-invocations-in-selected-window"
    assert activity["source"]["event_window_truncated"] is True
    assert activity["source"]["event_window_sequence_start"] == 1
    assert activity["source"]["event_window_sequence_end"] == 256
    assert activity["source"]["event_window_identity_digest"]
    assert activity["source"]["event_type_counts"] == {"session.checkpointed": 256}


def test_session_activity_exposes_bounded_older_pages_and_next_cursor(
    tmp_path: Path,
) -> None:
    record = _observation(
        "ev_environment_activity_older_page",
        "ses_compiler_activity_older_page",
        "cmp_compiler_activity_older_page",
    )
    session, _ = _session(tmp_path, [record])
    events = [
        {"identity": f"evt_older_{sequence}", "sequence": sequence,
         "type": "session.checkpointed", "payload": {}}
        for sequence in range(1, 7)
    ]
    requests = []

    def recent_events(*, limit, before_sequence=None):
        requests.append((limit, before_sequence))
        eligible = [event for event in events
                    if before_sequence is None or event["sequence"] < before_sequence]
        selected = eligible[-limit:]
        has_older = len(eligible) > len(selected)
        return {
            "event_count": len(events), "events": selected,
            "truncated": has_older,
            "next_before_sequence": selected[0]["sequence"] if has_older else None,
        }

    session.recent_events = recent_events
    result = capability_intelligence.project(
        session, session_event_before_sequence=5, session_event_limit=2,
    )

    activity = result["session_activity"]
    assert requests == [(2, 5)]
    assert activity["status"] == "partial-no-invocations-in-selected-window"
    assert activity["source"]["event_page_scoped"] is True
    assert activity["source"]["event_page_before_sequence"] == 5
    assert activity["source"]["event_window_sequence_start"] == 3
    assert activity["source"]["event_window_sequence_end"] == 4
    assert activity["source"]["next_event_before_sequence"] == 3
    assert activity["source"]["event_page_has_older_records"] is True


def test_session_activity_correlates_authenticated_control_read_operations(
    tmp_path: Path,
) -> None:
    record = _observation(
        "ev_environment_rpc_activity_observation",
        "ses_compiler_rpc_activity",
        "cmp_compiler_rpc_activity",
    )
    record["observation"]["outcome"]["bounded_context"] = "x" * 8_000
    session, _ = _session(tmp_path, [record])
    session.snapshot["rpc_read_activity_count"] = 3
    events = []
    for sequence, outcome in ((21, "failed"), (22, "failed"), (23, "succeeded")):
        payload = {
            "schema_version": "mncs.environment.rpc-read-activity/1",
            "operation": "session.inspect" if sequence < 23 else "campaign.state",
            "outcome_status": outcome,
            "exit_code": 2 if outcome == "failed" else 0,
            "failure_code": "ENVIRONMENT_SESSION_MISSING" if outcome == "failed" else None,
            "observation_ordinal": sequence - 20,
            "request_id": f"req_rpc_read_{sequence}",
            "request_digest": hashlib.sha256(
                f"request-{sequence}".encode(),
            ).hexdigest(),
            "caller": {
                "authenticated_principal_id": "ctrl_" + "a" * 32,
                "control_grant_id": "b" * 32,
                "process_identity": "proc_" + "c" * 32,
            },
            "context": {
                "repository": "mncs-control-mcp",
                "repository_branch": "campaign/environment-rpc-operation-activity-20261024",
                "repository_head": "d" * 40,
                "environment_id": "renv_projection_reader",
                "campaign_id": "cmp_projection_reader",
                "work_intent_id": "int_projection_reader",
            },
            "measurement_availability": {
                "model_identity": "unavailable",
                "token_usage": "unavailable",
                "control_dispatch_elapsed_ms": "measured",
                "resource_consumption": "unavailable",
                "cache_accounting": "unavailable",
            },
            "measurements": {
                "control_dispatch_elapsed_ms": 12.5,
            },
            "request_operation_digest": hashlib.sha256(
                f"request-{sequence}".encode(),
            ).hexdigest(),
            "request_event_digest": hashlib.sha256(
                f"event-{sequence}".encode(),
            ).hexdigest()[:16],
            # Projection output must not echo untrusted or legacy extra fields.
            "argv": ["inspect", "private-argument"],
            "raw_response": "private-response",
        }
        events.append({
            "identity": f"evt_rpc_read_{sequence}",
            "sequence": sequence,
            "type": "environment.rpc.read_observed",
            "producer": "mncs-control-mcp",
            "payload": payload,
        })
    untrusted_payload = dict(events[-1]["payload"])
    untrusted_payload["request_id"] = "req_rpc_read_spoofed"
    untrusted_payload["caller"] = {
        **untrusted_payload["caller"],
        "authenticated_principal_id": "ctrl_" + "f" * 32,
    }
    events.append({
        "identity": "evt_rpc_read_spoofed", "sequence": 24,
        "type": "environment.rpc.read_observed",
        "producer": "mncs-control-mcp", "payload": untrusted_payload,
    })
    session.recent_events = lambda *, limit: {
        "event_count": len(events), "events": events[-limit:], "truncated": False,
    }

    result = capability_intelligence.project(session)
    activity = result["session_activity"]

    assert activity["context"]["work_intent_goal"] == "inspect current pressures"
    assert activity["source"]["rpc_operation_capture_count"] == 3
    assert activity["source"]["rpc_operation_capture_limit"] == 256
    assert activity["source"]["rpc_operation_records_invalid"] == 1
    assert activity["source"]["rpc_operation_context_basis"] == {
        "repository_identity": "control-project-grant",
        "repository_branch_and_head": "session-selected-checkout",
        "environment_campaign_and_work_intent": "session-snapshot",
        "live_repository_probe": False,
    }
    assert activity["rpc_operation_count"] == 3
    assert activity["rpc_operations"][0]["event_reference"]["identity"] == (
        "evt_rpc_read_23"
    )
    assert activity["rpc_operations"][0]["context"]["repository"] == (
        "mncs-control-mcp"
    )
    assert activity["rpc_operations"][0]["context"]["environment_id"] == (
        "renv_projection_reader"
    )
    assert activity["rpc_operations"][0]["caller"][
        "process_identity_capture_status"
    ] == "captured"
    assert activity["rpc_operations"][0]["measurement_availability"][
        "control_dispatch_elapsed_ms"
    ] == "measured"
    assert activity["rpc_operations"][0]["measurements"][
        "control_dispatch_elapsed_ms"
    ] == 12.5
    assert len(activity["rpc_failure_candidates"]) == 1
    assert result["session_activity_pressure_candidate_count"] == 1
    candidate = activity["rpc_failure_candidates"][0]
    assert candidate["status"] == "candidate-needs-investigation"
    assert candidate["causal_claim"] == "not-established"
    assert candidate["distinct_session_count"] == 1
    assert candidate["recurrence_scope"] == "same-session-selected-event-window"
    assert [item["identity"] for item in candidate["supporting_events"]] == [
        "evt_rpc_read_21", "evt_rpc_read_22",
    ]
    assert "private-argument" not in json.dumps(result)
    assert "private-response" not in json.dumps(result)

    compact = capability_intelligence.project(session, max_result_bytes=5_000)
    compact_activity = compact["session_activity"]
    assert compact_activity["rpc_operations"][0]["event_reference"]["identity"] == (
        "evt_rpc_read_23"
    )
    assert compact_activity["source"]["rpc_operation_context_basis"] == (
        "grant+session-snapshot; branch/head not live-probed"
    )
    assert compact_activity["rpc_failure_candidates"][0]["supporting_events"][0][
        "identity"
    ] == "evt_rpc_read_21"
    assert compact_activity["rpc_failure_candidates"][0]["distinct_session_count"] == 1


def test_projection_carries_source_page_cursor_through_compaction(tmp_path: Path) -> None:
    record = _observation("ev_environment_observation_page", "ses_compiler", "cmp_compiler")
    record["observation"]["outcome"]["bounded_context"] = "x" * 8_000
    session, state = _session(tmp_path, [record])
    activity_event = {
        "identity": "evt_activity_compaction", "sequence": 2,
        "type": "capability.invoked", "producer": "local-agent",
        "payload": {"capability": "mncs-test:run",
                    "provider_request_id": "req_activity_compaction"},
    }
    session.recent_events = lambda *, limit, before_sequence=None: {
        "event_count": 6, "events": [activity_event], "truncated": True,
        "next_before_sequence": 2,
    }
    state.update({
        "evidence_offset": 16,
        "evidence_limit": 8,
        "evidence_truncated": True,
        "previous_evidence_offset": 8,
        "next_evidence_offset": 24,
    })

    projection = capability_intelligence.project(
        session, session_event_before_sequence=4, session_event_limit=8,
    )
    compact = capability_intelligence.project(
        session, max_result_bytes=8_000,
        session_event_before_sequence=4, session_event_limit=8,
    )

    for result in (projection, compact):
        assert result["source"]["evidence_page_offset"] == 16
        assert result["source"]["evidence_page_limit"] == 8
        assert result["source"]["previous_evidence_offset"] == 8
        assert result["source"]["next_evidence_offset"] == 24
        assert result["source"]["evidence_page_truncated"] is True
        assert result["session_activity"]["source"][
            "event_page_before_sequence"
        ] == 4
        assert result["session_activity"]["source"][
            "next_event_before_sequence"
        ] == 2
        assert result["session_activity"]["source"]["event_window_truncated"] is True
    assert projection["session_activity"]["source"]["event_window_limit"] == 8

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
    assert compact["session_activity"]["interactions"][0]["invocation_event"]["identity"] == (
        "evt_activity_compaction"
    )


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


def test_invalid_recorder_environment_identity_is_rejected(tmp_path: Path) -> None:
    record = _observation("ev_environment_observation_bad_environment_id", "ses_compiler", "cmp_compiler")
    record["recorder"]["environment_id"] = []
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


def test_compiler_regression_evidence_is_projected_with_verified_work_context(
    tmp_path: Path,
) -> None:
    report = {
        "schema_version": capability_intelligence.COMPILER_CACHE_REGRESSION_SCHEMA_V2,
        "identity": "ev_compiler_digest_application_effect_capability_invalidation_20261024",
        "compiler_head": "a" * 40,
        "language_head": "b" * 40,
        "source_module_count": 4,
        "language_effect_and_capability_dependencies_invalidated": True,
        "stages": {
            "cold": {
                "cache_status": "miss",
                "execution": {"output_equal_to_stage0": True},
                "semantic_work": {
                    "front_end_invocations": 1,
                    "recomputed_fact_count": 30,
                    "reused_fact_count": 0,
                },
                "build_metrics": {
                    "wall_seconds": 0.764341,
                    "cpu_seconds": 0.75,
                    "peak_rss_bytes": 61272064,
                    "peak_open_file_descriptors": 4,
                    "peak_threads": 1,
                },
            },
            "effect_assumption_edit": {
                "cache_status": "miss",
                "execution": {"output_equal_to_stage0": True},
                "semantic_work": {
                    "front_end_invocations": 1,
                    "recomputed_fact_count": 2,
                    "reused_fact_count": 28,
                },
                "build_metrics": {"wall_seconds": 0.758744},
            },
            "capability_assumption_edit": {
                "cache_status": "miss",
                "execution": {"output_equal_to_stage0": True},
                "semantic_work": {
                    "front_end_invocations": 1,
                    "recomputed_fact_count": 2,
                    "reused_fact_count": 28,
                },
                "build_metrics": {"wall_seconds": 0.766849},
            },
            "fresh_process": {
                "cache_status": "hit",
                "execution": {"output_equal_to_stage0": True},
                "semantic_work": {
                    "front_end_invocations": 1,
                    "recomputed_fact_count": 0,
                    "reused_fact_count": 30,
                },
                "build_metrics": {"wall_seconds": 0.623001},
            },
        },
    }
    session, state = _session(tmp_path, [])
    reference = _add_compiler_regression_evidence(session, state, report)

    result = capability_intelligence.project(session)

    assert result["status"] == "ready"
    assert result["observation_count"] == 1
    assert result["pressure_hypothesis_count"] == 0
    observation = result["observations"][0]
    assert observation["identity"] == report["identity"]
    assert observation["related_work"]["campaign_id"] == "cmp_projection_reader"
    assert observation["related_work"]["session_id"] == "ses_projection_reader"
    assert observation["observation"]["request"]["source_schema_version"] == (
        capability_intelligence.COMPILER_CACHE_REGRESSION_SCHEMA_V2
    )
    assert observation["observation"]["outcome"][
        "language_effect_and_capability_dependencies_invalidated"
    ] is True
    stages = {
        item["name"]: item
        for item in observation["observation"]["outcome"]["stage_summaries"]
    }
    assert stages["effect_assumption_edit"]["semantic_work"] == {
        "front_end_invocations": 1,
        "recomputed_fact_count": 2,
        "reused_fact_count": 28,
    }
    assert stages["fresh_process"]["cache_status"] == "hit"
    assert stages["cold"]["build_metrics"]["peak_rss_bytes"] == 61272064
    assert observation["observation"]["measurement_availability"]["token_usage"] == (
        "not-recorded-in-source"
    )
    assert observation["evidence_reference"] == reference


def test_compiler_regression_schema_one_projects_without_inventing_effect_data(
    tmp_path: Path,
) -> None:
    report = {
        "schema_version": capability_intelligence.COMPILER_CACHE_REGRESSION_SCHEMA_V1,
        "identity": "ev_compiler_digest_application_generic_specialization_invalidation_20261023",
        "compiler_head": "c" * 40,
        "language_head": "d" * 40,
        "source_module_count": 4,
        "actual_language_modules_reused": True,
        "stages": {
            "cold": {
                "cache_status": "miss",
                "execution": {"output_equal_to_stage0": True},
                "semantic_work": {
                    "recomputed_fact_count": 30,
                    "reused_fact_count": 0,
                },
                "build_metrics": {"wall_seconds": 0.76978},
            },
            "generic_specialization_cold": {
                "cache_status": "miss",
                "semantic_work": {
                    "recomputed_fact_count": 31,
                    "reused_fact_count": 0,
                },
                "build_metrics": {"wall_seconds": 0.786621},
            },
            "imported_body_edit": {
                "cache_status": "hit",
                "execution": {"output_equal_to_stage0": True},
                "semantic_work": {
                    "recomputed_fact_count": 1,
                    "reused_fact_count": 29,
                },
                "build_metrics": {"wall_seconds": 0.508041},
            },
            "generic_summarize_body_edit": {
                "cache_status": "miss",
                "execution": {"output_equal_to_stage0": True},
                "semantic_work": {
                    "recomputed_fact_count": 2,
                    "reused_fact_count": 29,
                },
                "build_metrics": {"wall_seconds": 0.763094},
            },
            "fresh_process": {
                "cache_status": "hit",
                "execution": {"output_equal_to_stage0": True},
                "semantic_work": {
                    "recomputed_fact_count": 0,
                    "reused_fact_count": 30,
                },
                "build_metrics": {"wall_seconds": 0.628035},
            },
        },
    }
    session, state = _session(tmp_path, [])
    reference = _add_compiler_regression_evidence(session, state, report)

    result = capability_intelligence.project(session)

    assert result["status"] == "ready"
    assert result["observation_count"] == 1
    observation = result["observations"][0]
    assert observation["identity"] == report["identity"]
    assert observation["observation"]["kind"] == (
        "compiler-generic-specialization-invalidation-cache-regression"
    )
    assert observation["observation"]["request"]["source_schema_version"] == (
        capability_intelligence.COMPILER_CACHE_REGRESSION_SCHEMA_V1
    )
    outcome = observation["observation"]["outcome"]
    assert outcome["actual_language_modules_reused"] is True
    assert outcome["effect_capability_invalidation_measurement"] == (
        "not-recorded-in-source-schema/1"
    )
    assert [item["name"] for item in outcome["stage_summaries"]] == [
        "cold", "generic_specialization_cold", "generic_summarize_body_edit",
        "imported_body_edit", "fresh_process",
    ]
    assert observation["observation"]["source_session_context_resolution"] == (
        "no-matching-verified-supporting-observation"
    )
    assert observation["evidence_reference"] == reference


def test_compiler_interface_regression_projects_interface_application_stages(
    tmp_path: Path,
) -> None:
    report = {
        "schema_version": capability_intelligence.COMPILER_CACHE_REGRESSION_SCHEMA_V1,
        "identity": "ev_compiler_digest_application_interface_invalidation_20261023",
        "compiler_head": "e" * 40,
        "language_head": "f" * 40,
        "source_module_count": 4,
        "actual_language_modules_reused": True,
        "stages": {
            "cold": {"cache_status": "miss", "build_metrics": {"wall_seconds": 0.7731}},
            "imported_body_edit": {
                "cache_status": "hit",
                "semantic_work": {"recomputed_fact_count": 1, "reused_fact_count": 29},
                "build_metrics": {"wall_seconds": 0.505353},
            },
            "imported_function_result_name_edit": {
                "cache_status": "miss",
                "semantic_work": {"recomputed_fact_count": 4, "reused_fact_count": 26},
                "build_metrics": {"wall_seconds": 0.730628},
            },
            "imported_nominal_field_addition": {
                "cache_status": "miss",
                "semantic_work": {"recomputed_fact_count": 11, "reused_fact_count": 19},
                "build_metrics": {"wall_seconds": 0.794319},
            },
            "imported_signature_cold": {
                "cache_status": "miss",
                "semantic_work": {"recomputed_fact_count": 30, "reused_fact_count": 0},
                "build_metrics": {"wall_seconds": 0.771534},
            },
            "invalid_source": {
                "cache_status": "miss",
                "build_metrics": {"wall_seconds": 0.044392},
            },
            "root_comment_edit": {
                "cache_status": "hit",
                "semantic_work": {"recomputed_fact_count": 0, "reused_fact_count": 30},
                "build_metrics": {"wall_seconds": 0.468303},
            },
            "tampered_cache_digest": {
                "cache_status": "miss",
                "semantic_work": {"recomputed_fact_count": 30, "reused_fact_count": 0},
                "build_metrics": {"wall_seconds": 0.791804},
            },
            "fresh_process": {
                "cache_status": "hit",
                "semantic_work": {"recomputed_fact_count": 0, "reused_fact_count": 30},
                "build_metrics": {"wall_seconds": 0.63363},
            },
        },
    }
    session, state = _session(tmp_path, [])
    _add_compiler_regression_evidence(session, state, report)

    result = capability_intelligence.project(session)

    observation = result["observations"][0]
    assert observation["observation"]["kind"] == (
        "compiler-interface-invalidation-cache-regression"
    )
    assert [item["name"] for item in observation["observation"]["outcome"][
        "stage_summaries"
    ]] == [
        "cold", "imported_body_edit", "imported_function_result_name_edit",
        "imported_nominal_field_addition", "imported_signature_cold",
        "invalid_source", "root_comment_edit", "tampered_cache_digest", "fresh_process",
    ]
    assert observation["observation"]["outcome"][
        "effect_capability_invalidation_measurement"
    ] == "not-recorded-in-source-schema/1"


def test_compiler_language_dependency_summary_workload_is_projected_separately(
    tmp_path: Path,
) -> None:
    cache = {
        "status": "observed",
        "observation": {
            "evicted_entry_count": 1,
            "recomputed_function_subjects": ["tests.native_readiness::idle"],
            "reused_function_subjects": ["mncs.dependency_cache::imported_idle"],
            "fact_seeded_function_subjects": [],
        },
    }
    verification = {
        "front_end_outcome": "verified",
        "function_body_verification": "recomputed_with_language_fact_reuse",
        "backend_artifact": "reused",
        "artifact_decision_reason": "executable_closure_and_interfaces_match_after_full_frontend",
        "front_end_invocations": 1,
        "recomputed_semantic_facts": ["tests.native_readiness::idle"],
        "reused_semantic_facts": ["mncs.dependency_cache::imported_idle"],
        "verification_dependency_summary_cache": cache,
    }
    stages = {
        "cold": {
            "cache_status": "miss",
            "execution": {"canonical_vm_output": [5], "stage0_output": [5]},
            "build_metrics": {"wall_seconds": 0.149464, "peak_rss_bytes": 61005824},
            "verification_work": verification,
        },
        "comment_edit": {
            "cache_status": "hit",
            "execution": {"canonical_vm_output": [5], "stage0_output": [5]},
            "build_metrics": {"wall_seconds": 0.04831},
            "verification_work": verification,
        },
        "environment_provider_request_12": {
            "cache_status": "miss",
            "response_schema": "mncs.compiler.application-build/15",
            "provider_metrics": {"wall_seconds": 0.344613, "peak_process_count": 2},
        },
        "fresh_process_comment_edit": {
            "cache_status": "hit",
            "execution": {"canonical_vm_output": [8], "stage0_output": [8]},
            "readiness_metrics": {"wall_seconds": 0.135638},
            "verification_work": verification,
        },
        "invalid_source": {
            "status": "rejected",
            "artifact_published": False,
            "diagnostic_count": 1,
            "build_metrics": {"wall_seconds": 0.002402},
        },
        "reachable_imported_body_edit": {
            "cache_status": "miss",
            "execution": {"canonical_vm_output": [8], "stage0_output": [8]},
            "build_metrics": {"wall_seconds": 0.070622},
            "verification_work": verification,
        },
        "unchanged": {
            "cache_status": "hit",
            "build_metrics": {"wall_seconds": 0.001143},
            "verification_work": {"front_end_invocations": 0},
        },
        "unreachable_body_edit": {
            "cache_status": "hit",
            "execution": {"canonical_vm_output": [5], "stage0_output": [5]},
            "build_metrics": {"wall_seconds": 0.07842},
            "verification_work": verification,
        },
    }
    report = {
        "schema_version": capability_intelligence.COMPILER_LANGUAGE_DEPENDENCY_SUMMARY_SCHEMA,
        "compiler_revision_before_test": "a" * 40,
        "language_locked_revision": "b" * 40,
        "language_source_profile": "0.18",
        "language_cache_scope": "bounded process-local structural summaries; not verification evidence",
        "language_dependency_summaries_actually_reused": 13,
        "language_verified_facts_reused": 12,
        "fresh_process_verified_facts_reused": 3,
        "fresh_process_fact_seeded_dependency_summaries": 3,
        "invalid_source_reused_previous_success": False,
        "execution_matches_stage0": True,
        "request_schema": "mncs.compiler.application-build-request/12",
        "response_schema": "mncs.compiler.application-build/15",
        "verification_work_schema": "mncs.compiler.application-build-verification-work/11",
        "language_cache_schema": "mncs.language-verification-dependency-summary-cache/2",
        "language_cache_observation_schema": "mncs.language-verification-dependency-summary-cache-observation/2",
        "language_cache_policy_identity": "mncs-language-verification-dependency-extractor/1",
        "stages": stages,
    }
    session, state = _session(tmp_path, [])
    reference = _add_compiler_dependency_summary_evidence(session, state, report)

    result = capability_intelligence.project(session)

    assert result["status"] == "ready"
    assert result["observation_count"] == 1
    observation = result["observations"][0]
    assert observation["identity"] == reference["identity"]
    assert observation["related_work"]["campaign_id"] == "cmp_projection_reader"
    assert observation["observation"]["request"]["source_record_identity_basis"] == (
        "authenticated-campaign-evidence-pointer;source-record-has-no-identity-field"
    )
    outcome = observation["observation"]["outcome"]
    assert outcome["language_cache_scope"] == report["language_cache_scope"]
    assert outcome["direct_measurements"][
        "language_dependency_summaries_actually_reused"
    ] == 13
    assert outcome["direct_measurements"]["language_verified_facts_reused"] == 12
    assert outcome["direct_measurements"]["fresh_process_verified_facts_reused"] == 3
    assert outcome["measurement_summary"]["measurement_availability"][
        "token_usage"
    ] == "not-recorded-in-source"
    stage_summaries = {item["name"]: item for item in outcome["stage_summaries"]}
    assert stage_summaries["comment_edit"]["stage0_outputs_match"] is True
    assert stage_summaries["fresh_process_comment_edit"]["verification_work"][
        "dependency_summary_cache"
    ]["evicted_entry_count"] == 1
    assert stage_summaries["invalid_source"]["artifact_published"] is False
    assert observation["evidence_reference"] == reference


def test_compiler_language_dependency_summary_requires_exact_source_schema(
    tmp_path: Path,
) -> None:
    report = {
        "schema_version": "mncs.compiler.language-dependency-summary-cache-evidence/1",
        "compiler_revision_before_test": "a" * 40,
        "language_locked_revision": "b" * 40,
        "language_cache_scope": "bounded process-local structural summaries",
        "language_dependency_summaries_actually_reused": 0,
        "language_verified_facts_reused": 0,
        "fresh_process_verified_facts_reused": 0,
        "fresh_process_fact_seeded_dependency_summaries": 0,
        "invalid_source_reused_previous_success": False,
        "execution_matches_stage0": True,
        "stages": {
            name: {"cache_status": "miss"}
            for name in capability_intelligence.COMPILER_LANGUAGE_DEPENDENCY_SUMMARY_STAGES
        },
    }
    session, state = _session(tmp_path, [])
    _add_compiler_dependency_summary_evidence(session, state, report)

    result = capability_intelligence.project(session)

    assert result["status"] == "partial"
    assert result["observation_count"] == 0
    assert result["source"]["observation_reference_diagnostics"][0]["verification"] == (
        "observation-contract-invalid"
    )


def test_compiler_language_dependency_summary_rejects_internal_identity_mismatch(
    tmp_path: Path,
) -> None:
    report = {
        "schema_version": capability_intelligence.COMPILER_LANGUAGE_DEPENDENCY_SUMMARY_SCHEMA,
        "identity": "ev_compiler_language_fact_reuse_workload_wrong_source",
    }
    session, state = _session(tmp_path, [])
    _add_compiler_dependency_summary_evidence(session, state, report)

    result = capability_intelligence.project(session)

    assert result["status"] == "partial"
    assert result["observation_count"] == 0
    assert result["source"]["observation_reference_diagnostics"][0]["verification"] == (
        "observation-identity-mismatch"
    )


def test_compiler_source_context_is_correlated_only_through_verified_pointer(
    tmp_path: Path,
) -> None:
    report = {
        "schema_version": capability_intelligence.COMPILER_CACHE_REGRESSION_SCHEMA_V1,
        "identity": "ev_compiler_digest_application_generic_specialization_invalidation_20261023",
        "compiler_head": "c" * 40,
        "language_head": "d" * 40,
        "source_module_count": 4,
        "actual_language_modules_reused": True,
        "stages": {
            "cold": {
                "cache_status": "miss",
                "execution": {"output_equal_to_stage0": True},
                "semantic_work": {"recomputed_fact_count": 30, "reused_fact_count": 0},
                "build_metrics": {"wall_seconds": 0.76978},
            },
            "generic_specialization_cold": {
                "cache_status": "miss",
                "semantic_work": {"recomputed_fact_count": 31, "reused_fact_count": 0},
                "build_metrics": {"wall_seconds": 0.786621},
            },
            "generic_summarize_body_edit": {
                "cache_status": "miss",
                "semantic_work": {"recomputed_fact_count": 2, "reused_fact_count": 29},
                "build_metrics": {"wall_seconds": 0.763094},
            },
            "imported_body_edit": {
                "cache_status": "hit",
                "semantic_work": {"recomputed_fact_count": 1, "reused_fact_count": 29},
                "build_metrics": {"wall_seconds": 0.508041},
            },
            "fresh_process": {
                "cache_status": "hit",
                "semantic_work": {"recomputed_fact_count": 0, "reused_fact_count": 30},
                "build_metrics": {"wall_seconds": 0.628035},
            },
        },
    }
    session, state = _session(tmp_path, [])
    compiler_reference = _add_compiler_regression_evidence(session, state, report)
    _add_compiler_source_context(session, state, compiler_reference)

    result = capability_intelligence.project(session)

    compiler_observation = next(
        item for item in result["observations"]
        if item["identity"] == report["identity"]
    )
    assert compiler_observation["related_work"]["campaign_id"] == (
        "cmp_projection_reader"
    )
    assert compiler_observation["observation"]["source_session_context"] == {
        "campaign_id": "cmp_mncs_native_application_readiness_compiler_20261013",
        "session_id": "ses_164edc158d9b1637",
        "repository": "mncs-compiler",
        "repository_context_basis": "verified-supporting-source-pointer",
        "work_intent_id": "int_compiler_native_readiness",
        "work_intent_goal": "Advance reliable native application development",
        "environment_id": "renv_compiler_campaign",
    }
    assert compiler_observation["observation"]["source_session_context_basis"] == (
        "verified-supporting-observation"
    )
    assert compiler_observation["observation"]["source_session_context_resolution"] == (
        "verified-exact-source-pointer"
    )


def test_compiler_source_context_conflict_is_left_ambiguous(tmp_path: Path) -> None:
    report = {
        "schema_version": capability_intelligence.COMPILER_CACHE_REGRESSION_SCHEMA_V1,
        "identity": "ev_compiler_digest_application_generic_specialization_invalidation_20261023",
        "source_module_count": 4,
        "actual_language_modules_reused": True,
        "stages": {
            "cold": {"cache_status": "miss", "build_metrics": {"wall_seconds": 0.7}},
            "generic_specialization_cold": {
                "cache_status": "miss",
                "build_metrics": {"wall_seconds": 0.8},
            },
        },
    }
    session, state = _session(tmp_path, [])
    compiler_reference = _add_compiler_regression_evidence(session, state, report)
    _add_compiler_source_context(
        session, state, compiler_reference,
        identity="ev_environment_compiler_source_context_one",
    )
    _add_compiler_source_context(
        session, state, compiler_reference,
        identity="ev_environment_compiler_source_context_two",
        work_intent_goal="A conflicting source objective",
    )

    result = capability_intelligence.project(session)

    compiler_observation = next(
        item for item in result["observations"]
        if item["identity"] == report["identity"]
    )
    assert "source_session_context" not in compiler_observation["observation"]
    assert compiler_observation["observation"]["source_session_context_resolution"] == (
        "ambiguous-supporting-observations"
    )


def test_compiler_regression_family_requires_matching_source_schema(
    tmp_path: Path,
) -> None:
    report = {
        "schema_version": capability_intelligence.COMPILER_CACHE_REGRESSION_SCHEMA_V2,
        "identity": "ev_compiler_digest_application_interface_invalidation_20261023",
        "source_module_count": 4,
        "language_effect_and_capability_dependencies_invalidated": True,
        "stages": {
            "cold": {"cache_status": "miss", "build_metrics": {"wall_seconds": 0.7}},
        },
    }
    session, state = _session(tmp_path, [])
    _add_compiler_regression_evidence(session, state, report)

    result = capability_intelligence.project(session)

    assert result["status"] == "partial"
    assert result["observation_count"] == 0
    assert result["source"]["observation_records_unavailable"] == 1
    assert result["source"]["observation_reference_diagnostics"][0]["verification"] == (
        "observation-contract-invalid"
    )


def test_compiler_regression_adapter_rejects_unrecognized_source_schema(
    tmp_path: Path,
) -> None:
    report = {
        "schema_version": "mncs.compiler.unknown-regression/1",
        "identity": "ev_compiler_digest_application_effect_capability_invalidation_20261024",
        "source_module_count": 4,
        "language_effect_and_capability_dependencies_invalidated": True,
        "stages": {"cold": {"cache_status": "miss"}},
    }
    session, state = _session(tmp_path, [])
    _add_compiler_regression_evidence(session, state, report)

    result = capability_intelligence.project(session)

    assert result["status"] == "partial"
    assert result["observation_count"] == 0
    assert result["source"]["observation_records_unavailable"] == 1
    assert result["source"]["observation_reference_diagnostics"][0]["verification"] == (
        "observation-contract-invalid"
    )


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
    compact = capability_intelligence.project(session, max_result_bytes=7000)

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
    compact_observation = compact["observations"][0]["observation"]
    assert compact["source"]["response_truncated"] is True
    assert compact["observations"][0]["supporting_sources"][0][
        "registration_scope"
    ] == "selected-campaign-evidence-window"
    stage_summary = compact_observation["outcome_context"]["sections"][
        "representative_stages"
    ]
    assert len(stage_summary["records"]) == 4
    assert stage_summary["records_truncated"] is True
    first_stage = stage_summary["records"][0]["values"]
    assert first_stage["name"] == "fresh_process"
    assert first_stage["cache_status"] == "hit"
    assert first_stage["output_equal_to_stage0"] is True
    assert first_stage["cpu_seconds"] == 0.61
    assert first_stage["peak_cgroup_memory_bytes"] is None


def test_compact_projection_preserves_source_registration_scope_result(
    tmp_path: Path,
) -> None:
    path = Path(__file__).resolve().parents[1] / (
        "evidence/environmental-capability/source-registration-scope-live-readback-20261024.json"
    )
    record = json.loads(path.read_text(encoding="utf-8"))
    session, _ = _session(tmp_path, [record])

    compact = capability_intelligence.project(session, max_result_bytes=8000)

    assert compact["source"]["response_truncated"] is True
    context = compact["observations"][0]["observation"]["outcome_context"]
    assert context["projection_status"] == "partial"
    assert context["source_registration_status"] == (
        "not-present-in-selected-campaign-evidence-window"
    )
    assert context["registration_scope"] == "selected-campaign-evidence-window"
    assert context["source_content_integrity_status"] == (
        "workspace-read-only-content-integrity-verified"
    )


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


def test_improvement_relation_without_followup_preserves_unassessed_status(
    tmp_path: Path,
) -> None:
    pressure = _observation("ev_environment_pressure_occurrence", "ses_agent", "cmp_agent")
    pressure["pressure_hypothesis"]["identity"] = "ph_unobserved_followup"
    verification = _observation(
        "ev_environment_change_verification", "ses_environment", "cmp_environment",
    )
    verification.pop("pressure_hypothesis")
    change = _observation(
        "ev_environment_change_implementation", "ses_environment", "cmp_environment",
    )
    change.pop("pressure_hypothesis")
    implementation_reference = {
        "identity": "ev_environment_change_source",
        "repository": "mncs-environment",
        "commit": "a" * 40,
        "path": "mncs_env/capability_intelligence.py",
        "sha256": "b" * 64,
    }
    change["improvement_relation"] = {
        "identity": "im_unobserved_followup",
        "addresses_hypothesis_identity": "ph_unobserved_followup",
        "implementation_reference": implementation_reference,
        "verification_observation_identity": verification["identity"],
        "basis": "The implementation and verification are recorded; no subsequent independent activity is available yet.",
        "not_measured": ["whether the pressure persists after the change"],
    }
    session, _ = _session(
        tmp_path, [pressure, change, verification],
        extra_refs=[implementation_reference],
    )

    result = capability_intelligence.project(session)
    compact = capability_intelligence.project(session, max_result_bytes=5000)

    relation = result["improvement_relations"][0]
    assert result["improvement_relation_count"] == 1
    assert relation["verification_record_status"] == (
        "record-integrity-verified-in-projection"
    )
    assert relation["subsequent_observation_identity"] is None
    assert relation["subsequent_record_status"] == "not-recorded"
    assert "subsequent_evidence_reference" not in relation
    assert relation["effectiveness_assessment"] == "not-assessed"
    assert compact["improvement_relations"][0]["subsequent_record_status"] == (
        "not-recorded"
    )
    assert compact["improvement_relations"][0]["effectiveness_assessment"] == (
        "not-assessed"
    )


def test_improvement_record_does_not_count_as_another_pressure_occurrence(
    tmp_path: Path,
) -> None:
    pressure = _observation(
        "ev_environment_pressure_occurrence",
        "ses_compiler_pressure_occurrence",
        "cmp_compiler_pressure_occurrence",
    )
    pressure["pressure_hypothesis"]["identity"] = "ph_one_pressure_occurrence"
    improvement = _observation(
        "ev_environment_pressure_implementation",
        "ses_environment_pressure_implementation",
        "cmp_environment_pressure_implementation",
    )
    improvement["pressure_hypothesis"] = dict(pressure["pressure_hypothesis"])
    implementation_reference = {
        "identity": "ev_environment_pressure_implementation_source",
        "repository": "mncs-environment",
        "commit": "a" * 40,
        "path": "mncs_env/capability_intelligence.py",
        "sha256": "b" * 64,
    }
    improvement["improvement_relation"] = {
        "identity": "im_pressure_occurrence_projection",
        "addresses_hypothesis_identity": "ph_one_pressure_occurrence",
        "implementation_reference": implementation_reference,
        "verification_observation_identity": "ev_pressure_implementation_verification",
        "subsequent_observation_identity": "ev_pressure_implementation_followup",
        "basis": "The implementation record restates the pressure to identify its target.",
        "not_measured": ["whether the pressure recurs after the implementation"],
    }
    session, _ = _session(
        tmp_path, [pressure, improvement], extra_refs=[implementation_reference],
    )

    result = capability_intelligence.project(session)

    hypothesis = result["pressure_hypotheses"][0]
    assert result["observation_count"] == 2
    assert hypothesis["observation_count"] == 1
    assert hypothesis["recurrence_status"] == "not-established"
    assert [item["identity"] for item in hypothesis["evidence"]] == [
        "ev_environment_pressure_occurrence",
    ]


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
    assert result["observation_count"] == 1
    assert result["improvement_relation_count"] == 0
    assert result["source"]["observation_records_unavailable"] == 0
    assert result["source"]["improvement_relations_unverified_count"] == 1
    assert result["observations"][0]["improvement_relation_status"] == (
        "recorded-but-contract-unverified"
    )


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
    pressure = result["pressure_hypotheses"][0]
    assert pressure["distinct_work_intent_count"] == 1
    assert pressure["affected_work_context_count"] == 1
    assert pressure["affected_work_contexts"] == [{
        "campaign_id": "cmp_mncs_native_application_readiness_compiler_20261013",
        "session_id": "ses_164edc158d9b1637",
        "work_intent_id": "int_8fb697d8987722d5",
        "work_intent_goal": (
            "Mature mncs-compiler for reliable native multi-module application "
            "development while advancing verified self-consumption"
        ),
        "campaign_objective": (
            "MNCS Environmental Capability Intelligence & Continuous Improvement "
            "Campaign"
        ),
    }]
    assert pressure["recording_context_count"] == 1
    assert pressure["distinct_recording_session_count"] == 1
    assert pressure["recording_contexts"] == [{
        "campaign_id": "cmp_environmental_capability_intelligence_20261023",
        "session_id": "ses_c38b0ab35b9418ba",
        "consumer_id": "local-agent",
        "environment_identity_capture_status": "not-captured-in-record",
        "environment_version_capture_status": "not-captured-in-record",
    }]
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
    assert gap["recording_context"]["environment_identity_capture_status"] == (
        "not-captured-in-record"
    )
    assert followup["related_work"]["session_id"] == "ses_164edc158d9b1637"
    assert followup["recording_context"]["session_id"] == "ses_c38b0ab35b9418ba"


def test_pressure_context_refinement_projects_its_verified_followup_chain(
    tmp_path: Path,
) -> None:
    evidence_dir = Path(__file__).resolve().parents[1] / (
        "evidence/environmental-capability"
    )
    records = [
        json.loads((evidence_dir / filename).read_text(encoding="utf-8"))
        for filename in (
            "pressure-summary-work-context-gap-20261024.json",
            "pressure-context-and-session-source-verification-20261024.json",
            "pressure-context-and-session-source-improvement-20261024.json",
            "pressure-context-and-session-source-live-readback-20261024.json",
            "pressure-context-and-session-source-chain-readback-20261024.json",
        )
    ]
    implementation = records[2]["improvement_relation"]["implementation_reference"]
    session, _ = _session(tmp_path, records, extra_refs=[implementation])

    result = capability_intelligence.project(session)

    assert result["status"] == "ready"
    assert result["pressure_hypothesis_count"] == 1
    assert result["pressure_hypotheses"][0]["identity"] == (
        "ph_environment_pressure_summary_work_context"
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
        "ev_environment_pressure_context_and_session_source_verification_20261024"
    )
    assert relation["subsequent_evidence_reference"]["identity"] == (
        "ev_environment_pressure_context_and_session_source_live_readback_20261024"
    )
    assert "ev_environment_pressure_context_and_session_source_chain_readback_20261024" in {
        item["identity"] for item in result["observations"]
    }


def test_campaign_objective_context_gap_projects_verified_improvement(
    tmp_path: Path,
) -> None:
    evidence_dir = Path(__file__).resolve().parents[1] / (
        "evidence/environmental-capability"
    )
    records = [
        json.loads((evidence_dir / filename).read_text(encoding="utf-8"))
        for filename in (
            "campaign-objective-pressure-context-gap-20261024.json",
            "campaign-objective-pressure-context-verification-20261024.json",
            "campaign-objective-pressure-context-improvement-20261024.json",
            "campaign-objective-pressure-context-live-readback-20261024.json",
        )
    ]
    implementation = records[2]["improvement_relation"]["implementation_reference"]
    session, _ = _session(tmp_path, records, extra_refs=[implementation])

    result = capability_intelligence.project(session)

    assert result["status"] == "ready"
    assert result["pressure_hypothesis_count"] == 1
    pressure = result["pressure_hypotheses"][0]
    assert pressure["identity"] == "ph_environment_pressure_context_campaign_objective"
    assert pressure["affected_work_context_count"] == 1
    assert pressure["affected_work_contexts"][0]["campaign_objective"] == (
        "Mature mncs-compiler for reliable native multi-module application "
        "development while advancing verified self-consumption"
    )
    assert result["improvement_relation_count"] == 1
    relation = result["improvement_relations"][0]
    assert relation["pressure_hypothesis_status"] == "present-in-projection"
    assert relation["verification_record_status"] == (
        "record-integrity-verified-in-projection"
    )
    assert relation["subsequent_record_status"] == "not-recorded"
    assert relation["effectiveness_assessment"] == "not-assessed"
    assert "ev_environment_campaign_objective_pressure_context_live_readback_20261024" in {
        item["identity"] for item in result["observations"]
    }


def test_authentic_recorder_environment_identity_is_projected_separately(
    tmp_path: Path,
) -> None:
    path = Path(__file__).resolve().parents[1] / (
        "evidence/environmental-capability/recorder-environment-identity-gap-20261024.json"
    )
    record = json.loads(path.read_text(encoding="utf-8"))
    session, _ = _session(tmp_path, [record])

    result = capability_intelligence.project(session)

    observation = result["observations"][0]
    assert observation["related_work"]["campaign_id"] == (
        "cmp_mncs_native_application_readiness_compiler_20261013"
    )
    assert observation["recording_context"]["environment_id"] == (
        "renv_4844ecc725485390"
    )
    assert observation["recording_context"]["definition_id"] == (
        "env_d479727df8e9d3ef"
    )
    assert observation["recording_context"]["environment_identity_capture_status"] == (
        "captured-in-record"
    )
    pressure = result["pressure_hypotheses"][0]
    assert pressure["identity"] == "ph_environment_recorder_resolved_identity"
    assert pressure["distinct_recording_environment_id_count"] == 1
    assert pressure["distinct_recording_definition_id_count"] == 1
    assert pressure["distinct_recording_environment_version_count"] == 0


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
    record["related_work"]["work_intent_goal"] = "g" * 300
    record["recorder"].update({
        "environment_id": "renv_compact_capture",
        "definition_id": "env_compact_capture",
        "environment_version": "a" * 40,
    })
    session, _ = _session(tmp_path, [record])

    result = capability_intelligence.project(session)
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True).encode("utf-8")

    assert result["status"] == "partial"
    assert len(encoded) <= capability_intelligence.MAX_RESULT_BYTES
    assert result["observations"][0]["observation"]["details_omitted"] is True
    assert result["observations"][0]["evidence_reference"]["identity"] == record["identity"]
    assert result["observations"][0]["recording_context"]["campaign_id"] == "cmp_observation_capture"
    pressure = result["pressure_hypotheses"][0]
    assert pressure["affected_work_context_count"] == 1
    assert len(pressure["affected_work_contexts"][0]["work_intent_goal"]) <= 256
    assert pressure["affected_work_contexts"][0]["work_intent_goal_truncated"] is True
    assert pressure["recording_context_count"] == 1
    assert pressure["recording_contexts"][0]["session_id"] == (
        "ses_observation_capture"
    )
    assert pressure["recording_contexts"][0]["environment_id"] == (
        "renv_compact_capture"
    )
    assert pressure["recording_contexts"][0]["definition_id"] == (
        "env_compact_capture"
    )
    assert pressure["recording_contexts"][0]["environment_version"] == "a" * 40
    assert pressure["recording_contexts"][0]["environment_version_capture_status"] == (
        "captured-in-record"
    )
    assert pressure["distinct_recording_environment_id_count"] == 1
    assert pressure["distinct_recording_definition_id_count"] == 1
    assert pressure["distinct_recording_environment_version_count"] == 1


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
    assert hypothesis["distinct_work_intent_count"] == 1
    assert hypothesis["affected_work_context_count"] == 2
    assert hypothesis["distinct_recording_session_count"] == 1
    assert hypothesis["recurrence_status"] == "observed-across-sessions"


def test_conflicting_pressure_recommendations_are_preserved_as_ambiguous(
    tmp_path: Path,
) -> None:
    first = _observation(
        "ev_environment_selection_recommendation_first",
        "ses_compiler_selection",
        "cmp_compiler_selection",
    )
    second = _observation(
        "ev_environment_selection_recommendation_second",
        "ses_compiler_selection",
        "cmp_compiler_selection",
    )
    second["pressure_hypothesis"]["actionable_recommendation"] = (
        "Return the exact required registered worktree context after validating "
        "the caller's campaign and session."
    )
    session, _ = _session(tmp_path, [first, second])

    full = capability_intelligence.project(session)
    pressure = full["pressure_hypotheses"][0]

    assert pressure["definition_status"] == "conflicting-recorded-definitions"
    assert pressure["definition_conflict_fields"] == ["recommendation"]
    assert pressure["title"] == first["pressure_hypothesis"]["title"]
    assert "recommendation" not in pressure
    assert pressure["definition_evidence_observation_identities"] == [
        first["identity"], second["identity"],
    ]

    compact = capability_intelligence.project(session, max_result_bytes=6_000)
    compact_pressure = compact["pressure_hypotheses"][0]
    assert compact["source"]["response_truncated"] is True
    assert compact_pressure["definition_status"] == (
        "conflicting-recorded-definitions"
    )
    assert compact_pressure["definition_conflict_fields"] == ["recommendation"]
    assert "recommendation" not in compact_pressure
    assert compact_pressure["definition_evidence_observation_identities"] == [
        first["identity"], second["identity"],
    ]


def test_authentic_compiler_selection_records_surface_recommendation_conflict(
    tmp_path: Path,
) -> None:
    evidence_root = Path(__file__).resolve().parents[1] / "evidence" / "environmental-capability"
    identities = (
        "project-continuation-conflict-20261024.json",
        "compiler-project-context-selection-recurrence-20261024.json",
    )
    records = [
        json.loads((evidence_root / name).read_text(encoding="utf-8"))
        for name in identities
    ]
    session, _ = _session(tmp_path, records)

    projection = capability_intelligence.project(session)

    pressure = next(
        item for item in projection["pressure_hypotheses"]
        if item["identity"] == "ph_environment_project_session_selection"
    )
    assert pressure["definition_status"] == "conflicting-recorded-definitions"
    assert pressure["definition_conflict_fields"] == ["recommendation"]
    assert "recommendation" not in pressure
    assert pressure["definition_evidence_observation_identities"] == [
        record["identity"] for record in records
    ]


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

    requests = []

    def campaign_state(*, evidence_offset=None, evidence_limit=32):
        requests.append((evidence_offset, evidence_limit))
        return older_page if evidence_offset == 0 else latest_page

    session.campaign_state = campaign_state
    session.close = lambda: None
    monkeypatch.setattr(cli, "_open", lambda _args: session)
    output = io.StringIO()
    with redirect_stdout(output):
        status = cli.main([
            "--state-dir", str(tmp_path), "--persistence", "file",
            "projections", session.session_id, "--interpret", "environmental-capability",
            "--evidence-offset", "9", "--evidence-limit", "9",
            "--history-pages", "2",
            "--session-event-before-sequence", "12",
            "--session-event-limit", "64",
        ])

    projection = json.loads(output.getvalue())
    hypothesis = projection["pressure_hypotheses"][0]
    assert status == 0
    assert requests[:2] == [(9, 9), (0, 9)]
    assert projection["source"]["evidence_page_offset"] == 9
    assert projection["source"]["evidence_page_limit"] == 9
    assert hypothesis["recurrence_status"] == "observed-across-sessions"
    assert hypothesis["observation_count"] == 16
    assert hypothesis["distinct_session_count"] == 2
    assert len(hypothesis["evidence"]) == 16
    assert projection["source"]["evidence_history_pages_requested"] == 2
    assert projection["source"]["evidence_history_pages_read"] == 2
    assert projection["source"]["evidence_history_page_offsets"] == [9, 0]
    assert projection["source"]["evidence_history_truncated"] is False
    assert projection["source"]["evidence_history_checkpoint_consistent"] is True
    assert projection["session_activity"]["source"][
        "event_page_before_sequence"
    ] == 12
    assert projection["session_activity"]["source"]["event_window_limit"] == 64
    assert projection["source"]["evidence_history_observation_budgets_by_page"] == [
        {
            "offset": 9,
            "page_limit": 9,
            "observation_reference_capacity": 9,
            "read_budget": 8,
            "selected_observation_reference_identities": [
                f"ev_environment_pressure_history_{index:02d}"
                for index in range(10, 18)
            ],
            "observation_records_skipped_by_page_budget": 1,
        },
        {
            "offset": 0,
            "page_limit": 9,
            "observation_reference_capacity": 9,
            "read_budget": 8,
            "selected_observation_reference_identities": [
                f"ev_environment_pressure_history_{index:02d}"
                for index in range(1, 9)
            ],
            "observation_records_skipped_by_page_budget": 1,
        },
    ]
    assert projection["source"]["observation_reference_selection_policy"] == (
        "newest-in-ledger-append-order-within-each-page"
    )
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
    assert compact["source"]["observation_reference_selection_policy"] == (
        "newest-in-ledger-append-order-within-each-page"
    )
    assert compact["source"]["observation_records_skipped_by_page_budget"] == 2
    assert any(
        "older skipped references may contain additional evidence" in item
        for item in compact["measurement_limits"]
    )

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
        {
            "offset": 9,
            "page_limit": 8,
            "observation_reference_capacity": 8,
            "read_budget": 8,
            "selected_observation_reference_identities": [
                f"ev_environment_short_history_{index:02d}"
                for index in range(9, 17)
            ],
            "observation_records_skipped_by_page_budget": 0,
        },
        {
            "offset": 1,
            "page_limit": 8,
            "observation_reference_capacity": 8,
            "read_budget": 7,
            "selected_observation_reference_identities": [
                f"ev_environment_short_history_{index:02d}"
                for index in range(2, 9)
            ],
            "observation_records_skipped_by_page_budget": 1,
        },
        {
            "offset": 0,
            "page_limit": 1,
            "observation_reference_capacity": 1,
            "read_budget": 1,
            "selected_observation_reference_identities": [
                "ev_environment_short_history_00"
            ],
            "observation_records_skipped_by_page_budget": 0,
        },
    ]
    assert result["source"]["evidence_history_truncated"] is False
    assert hypothesis["recurrence_status"] == "observed-across-sessions"
    assert hypothesis["observation_count"] == 16
    assert hypothesis["distinct_session_count"] == 2


def test_single_page_budget_selects_newest_observations_and_reports_omissions(
    tmp_path: Path,
) -> None:
    reports = [
        _observation(
            f"ev_environment_single_page_history_{index:02d}",
            "ses_single_page_history",
            "cmp_single_page_history",
        )
        for index in range(20)
    ]
    session, state = _session(tmp_path, reports)
    state["evidence_offset"] = 0
    state["evidence_limit"] = 20

    result = capability_intelligence.project(session)

    assert [item["identity"] for item in result["observations"]] == [
        f"ev_environment_single_page_history_{index:02d}"
        for index in range(4, 20)
    ]
    assert result["source"]["observation_records_skipped_by_page_budget"] == 4
    assert result["source"]["observation_reference_selection_policy"] == (
        "newest-in-ledger-append-order-within-each-page"
    )
    assert result["source"]["evidence_history_observation_budgets_by_page"] == [{
        "offset": 0,
        "page_limit": 20,
        "observation_reference_capacity": 20,
        "read_budget": 16,
        "selected_observation_reference_identities": [
            f"ev_environment_single_page_history_{index:02d}"
            for index in range(4, 20)
        ],
        "observation_records_skipped_by_page_budget": 4,
    }]


def test_targeted_small_evidence_segment_reads_its_complete_observation_set(
    tmp_path: Path,
) -> None:
    reports = [
        _observation(
            f"ev_environment_targeted_segment_{index:02d}",
            "ses_targeted_segment",
            "cmp_targeted_segment",
        )
        for index in range(20)
    ]
    session, state = _session(tmp_path, reports)
    page = {
        **state,
        "evidence": state["evidence"][:8],
        "evidence_offset": 0,
        "evidence_limit": 8,
        "previous_evidence_offset": None,
        "next_evidence_offset": 8,
        "evidence_truncated": True,
    }
    requests = []

    def campaign_state(*, evidence_offset=None, evidence_limit=32):
        requests.append((evidence_offset, evidence_limit))
        return page

    session.campaign_state = campaign_state
    result = capability_intelligence.project(
        session, evidence_offset=0, evidence_limit=8,
    )

    assert requests == [(0, 8)]
    assert result["source"]["evidence_page_offset"] == 0
    assert result["source"]["evidence_page_limit"] == 8
    assert result["source"]["observation_records_verified"] == 8
    assert result["source"]["observation_records_skipped_by_page_budget"] == 0
    assert [item["identity"] for item in result["observations"]] == [
        f"ev_environment_targeted_segment_{index:02d}"
        for index in range(8)
    ]


def test_projection_cli_defaults_to_capability_page_limit(
    tmp_path: Path, monkeypatch,
) -> None:
    record = _observation(
        "ev_environment_projection_default_page_limit",
        "ses_projection_default_page_limit",
        "cmp_projection_default_page_limit",
    )
    session, state = _session(tmp_path, [record])
    state.update({
        "evidence_offset": 0,
        "evidence_limit": capability_intelligence.MAX_EVIDENCE_REFS,
        "previous_evidence_offset": None,
        "next_evidence_offset": None,
    })
    requests = []

    def campaign_state(*, evidence_offset=None, evidence_limit=32):
        requests.append((evidence_offset, evidence_limit))
        return state

    session.campaign_state = campaign_state
    session.close = lambda: None
    monkeypatch.setattr(cli, "_open", lambda _args: session)
    output = io.StringIO()
    with redirect_stdout(output):
        status = cli.main([
            "--state-dir", str(tmp_path), "--persistence", "file",
            "projections", session.session_id, "--interpret", "environmental-capability",
        ])

    projection = json.loads(output.getvalue())
    assert status == 0
    assert requests == [(None, capability_intelligence.MAX_EVIDENCE_REFS)]
    assert projection["source"]["evidence_page_limit"] == (
        capability_intelligence.MAX_EVIDENCE_REFS
    )


def test_interpreted_projection_rejects_ambient_evidence_option(
    tmp_path: Path,
    monkeypatch,
) -> None:
    opened = []
    monkeypatch.setattr(cli, "_open", lambda _args: opened.append(True))
    errors = io.StringIO()
    with redirect_stderr(errors):
        status = cli.main([
            "--state-dir", str(tmp_path), "--persistence", "file",
            "projections", "ses_projection_reader",
            "--interpret", "environmental-capability", "--evidence",
        ])

    error = json.loads(errors.getvalue())
    assert status == 2
    assert opened == []
    assert error["diagnostics"]["code"] == "incompatible-projection-options"
    assert "without --evidence" in error["diagnostics"]["next"]


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


def test_later_corrected_identity_for_same_observation_source_is_read(tmp_path: Path) -> None:
    valid_identity = "ev_environment_corrected_pointer"
    record = _observation(
        valid_identity, "ses_corrected_pointer", "cmp_corrected_pointer",
    )
    record["observation"]["outcome"]["large_diagnostic"] = "x" * 60_000
    session, state = _session(tmp_path, [record])
    corrected_reference = dict(state["evidence"][0])
    stale_reference = {**corrected_reference, "identity": "ev_environment_stale_pointer"}
    state["evidence"] = [stale_reference, corrected_reference]
    state["evidence_count"] = 2

    result = capability_intelligence.project(session)

    assert [item["identity"] for item in result["observations"]] == [valid_identity]
    assert result["observations"][0]["evidence_reference"]["identity"] == valid_identity
    assert result["source"]["observation_records_verified"] == 1
    assert result["source"]["observation_records_unavailable"] == 1
    assert result["status"] == "partial"
    assert result["source"]["observation_reference_diagnostic_count"] == 1
    assert result["source"]["observation_reference_diagnostics"] == [{
        "identity": "ev_environment_stale_pointer",
        "record_identity": valid_identity,
        "repository": "mncs-environment",
        "commit": result["observations"][0]["evidence_reference"]["commit"],
        "sha256": result["observations"][0]["evidence_reference"]["sha256"],
        "verification": "observation-identity-mismatch",
        "path": result["observations"][0]["evidence_reference"]["path"],
        "path_truncated": False,
    }]


def test_authentic_campaign_alias_mismatches_are_diagnosed_without_projection(
    tmp_path: Path,
) -> None:
    evidence_root = Path(__file__).resolve().parents[1] / "evidence" / "environmental-capability"
    names_and_aliases = (
        (
            "read-operation-live-projection-context-20261024.json",
            "ev_environment_rpc_context_basis_prechange_20261024",
        ),
        (
            "read-operation-live-projection-context-followup-20261024.json",
            "ev_environment_rpc_context_basis_followup_20261024",
        ),
    )
    records = [
        json.loads((evidence_root / name).read_text(encoding="utf-8"))
        for name, _alias in names_and_aliases
    ]
    session, state = _session(tmp_path, records)
    for reference, (_name, alias) in zip(
        state["evidence"], names_and_aliases, strict=True,
    ):
        reference["identity"] = alias

    result = capability_intelligence.project(session)

    assert result["observation_count"] == 0
    assert result["pressure_hypothesis_count"] == 0
    assert result["source"]["observation_records_unavailable"] == 2
    assert result["source"]["observation_reference_diagnostics"] == [
        {
            "identity": alias,
            "record_identity": record["identity"],
            "repository": "mncs-environment",
            "commit": state["evidence"][index]["commit"],
            "sha256": state["evidence"][index]["sha256"],
            "verification": "observation-identity-mismatch",
            "path": state["evidence"][index]["path"],
            "path_truncated": False,
        }
        for index, (record, (_name, alias)) in enumerate(
            zip(records, names_and_aliases, strict=True),
        )
    ]


def test_recorded_source_session_context_stays_distinct_from_related_work(tmp_path: Path) -> None:
    record = _observation(
        "ev_environment_nested_source_context",
        "ses_projection_capture",
        "cmp_projection_capture",
    )
    record["related_work"].pop("work_intent_id")
    record["related_work"].pop("work_intent_goal")
    record["pressure_hypothesis"].pop("owner")
    record["observation"].pop("outcome")
    record["observation"]["outcome"] = {"large_diagnostic": "x" * 8_000}
    record["observation"]["projection_summary"] = {
        "status": "partial",
        "rpc_operation_event_count": 2,
        "rpc_operation_records_returned": 2,
    }
    record["observation"]["operations"] = [{
        "event_identity": "evt_activity_source",
        "event_sequence": 4,
        "observed_at": "2026-10-24T00:00:00Z",
        "operation": "campaign.state",
        "outcome_status": "succeeded",
        "exit_code": 0,
        "control_dispatch_elapsed_ms": 250,
        "request_arguments": {"must_not_be_projected": True},
    }]
    record["observation"]["measurement_availability"] = {
        "model_identity": "unavailable",
        "token_usage": "unavailable",
        "control_dispatch_elapsed_ms": "measured",
    }
    record["observation"]["source_session_context"] = {
        "campaign_id": "cmp_activity_source",
        "session_id": "ses_activity_source",
        "work_intent_id": "int_activity_source",
        "work_intent_goal": "advance the native compiler",
        "repository_context_basis": "recorded session snapshot",
    }
    session, _state = _session(tmp_path, [record])

    result = capability_intelligence.project(session)

    projected = result["observations"][0]
    assert result["status"] == "ready"
    assert projected["related_work"]["campaign_id"] == "cmp_projection_capture"
    assert "work_intent_goal" not in projected["related_work"]
    assert projected["observation"]["source_session_context"] == {
        "campaign_id": "cmp_activity_source",
        "session_id": "ses_activity_source",
        "work_intent_id": "int_activity_source",
        "work_intent_goal": "advance the native compiler",
        "repository_context_basis": "recorded session snapshot",
    }
    assert projected["observation"]["source_session_context_basis"] == (
        "recorded-observation"
    )
    assert projected["observation"]["projection_summary"] == {
        "status": "partial",
        "rpc_operation_event_count": 2,
        "rpc_operation_records_returned": 2,
    }
    assert projected["observation"]["operations"] == [{
        "event_identity": "evt_activity_source",
        "event_sequence": 4,
        "observed_at": "2026-10-24T00:00:00Z",
        "operation": "campaign.state",
        "outcome_status": "succeeded",
        "exit_code": 0,
        "control_dispatch_elapsed_ms": 250,
    }]
    assert projected["observation"]["measurement_availability"] == {
        "model_identity": "unavailable",
        "token_usage": "unavailable",
        "control_dispatch_elapsed_ms": "measured",
    }
    assert "owner" not in projected["pressure_hypothesis"]

    compact = capability_intelligence.project(session, max_result_bytes=10_000)
    compact_observation = compact["observations"][0]["observation"]
    assert compact["source"]["response_truncated"] is True
    assert compact_observation["source_session_context"] == {
        "campaign_id": "cmp_activity_source",
        "session_id": "ses_activity_source",
        "work_intent_id": "int_activity_source",
        "work_intent_goal": "advance the native compiler",
        "repository_context_basis": "recorded session snapshot",
    }
    assert compact_observation["projection_summary"] == {
        "status": "partial",
        "rpc_operation_event_count": 2,
        "rpc_operation_records_returned": 2,
    }
    assert compact_observation["operations"] == [{
        "event_identity": "evt_activity_source",
        "event_sequence": 4,
        "observed_at": "2026-10-24T00:00:00Z",
        "operation": "campaign.state",
        "outcome_status": "succeeded",
        "exit_code": 0,
        "control_dispatch_elapsed_ms": 250,
    }]
    assert compact_observation["measurement_availability"] == {
        "model_identity": "unavailable",
        "token_usage": "unavailable",
        "control_dispatch_elapsed_ms": "measured",
    }
    assert "must_not_be_projected" not in json.dumps(compact)


def test_unverified_optional_improvement_link_does_not_discard_observation(
    tmp_path: Path,
) -> None:
    record = _observation(
        "ev_environment_partial_improvement_link",
        "ses_partial_improvement_link",
        "cmp_partial_improvement_link",
    )
    record["improvement_relation"] = {
        "identity": "im_partial_improvement_link",
        "addresses_hypothesis_identity": "ph_environment_project_session_selection",
        "implementation_status_at_capture": "not-yet-implemented",
        "effectiveness_assessment": "not-assessed",
    }
    session, _state = _session(tmp_path, [record])

    result = capability_intelligence.project(session)

    assert result["observation_count"] == 1
    assert result["observations"][0]["improvement_relation_identity"] is None
    assert result["observations"][0]["improvement_relation_status"] == (
        "recorded-but-contract-unverified"
    )
    assert result["improvement_relation_count"] == 0


def test_compact_projection_preserves_classified_measurement_summary(tmp_path: Path) -> None:
    record = _observation(
        "ev_environment_compiler_measurement_summary",
        "ses_compiler_measurement_summary",
        "cmp_compiler_measurement_summary",
    )
    record["observation"]["outcome"]["measurement_summary"] = {
        "direct_measurements": {
            "compiler_total_wall_seconds": 342.003768,
            "compiler_cold_build_peak_rss_bytes": 1_803_956_224,
        },
        "derived_measurements": {"cold_build_seconds_per_case": 15.5628596},
        "observed_counts": {"cold_body_fact_count": 2_031},
        "measurement_availability": {
            "cgroup_peak_memory_bytes": "unavailable",
        },
        "interpretations": ["compiler artifact records stale VM inputs"],
        "qualifications": ["measurements belong to the source artifact"],
    }
    record["observation"]["outcome"]["large_diagnostic"] = "private " * 6_000
    session, _state = _session(tmp_path, [record])

    result = capability_intelligence.project(session)

    assert result["source"]["response_truncated"] is True
    observation = result["observations"][0]["observation"]
    assert observation["details_omitted"] is True
    assert observation["measurement_summary"] == {
        "direct_measurements": {
            "compiler_cold_build_peak_rss_bytes": 1_803_956_224,
            "compiler_total_wall_seconds": 342.003768,
        },
        "derived_measurements": {"cold_build_seconds_per_case": 15.5628596},
        "observed_counts": {"cold_body_fact_count": 2_031},
        "measurement_availability": {
            "cgroup_peak_memory_bytes": "unavailable",
        },
        "interpretations": ["compiler artifact records stale VM inputs"],
        "qualifications": ["measurements belong to the source artifact"],
    }
    assert "private " not in json.dumps(result)


def test_compiler_artifact_observation_preserves_scope_and_measurement_uncertainty(
    tmp_path: Path,
) -> None:
    path = Path(__file__).resolve().parents[1] / (
        "evidence/environmental-capability/"
        "compiler-self-consumption-resource-provenance-20261024.json"
    )
    record = json.loads(path.read_text(encoding="utf-8"))
    record["observation"]["outcome"]["large_diagnostic"] = "x" * 60_000
    session, _state = _session(tmp_path, [record])

    result = capability_intelligence.project(session)

    observation = result["observations"][0]
    assert result["source"]["response_truncated"] is True
    assert len(json.dumps(result).encode("utf-8")) <= capability_intelligence.MAX_RESULT_BYTES
    assert observation["observation"]["details_omitted"] is True
    assert observation["related_work"]["campaign_id"] == (
        "cmp_environmental_capability_intelligence_followup_20261024"
    )
    assert observation["observation"]["source_session_context"]["campaign_id"] == (
        "cmp_mncs_native_application_readiness_compiler_20261013"
    )
    assert observation["observation"]["measurement_summary"][
        "measurement_availability"]["phase_cgroup_memory_peak_bytes"] == "unavailable"
    assert observation["observation"]["outcome_context"]["status"] == (
        "passed_language_fact_v3_project_self_consumption"
    )
    assert observation["observation"]["outcome_context"]["claim_boundary"] == (
        "selected Project function application only; whole-compiler succession remains unproven"
    )
    assert observation["observation"]["outcome_context"]["sections"][
        "vm_runtime_build_origin"]["values"] == {
            "mismatch_count": 10,
            "status": "stale-inputs",
        }
    assert observation["observation"]["outcome_context"]["sections"][
        "invalid_source"]["values"]["status"] == "rejected"
    assert result["pressure_hypotheses"][0]["recurrence_status"] == "not-established"
    assert result["source"]["source_pointer_content_integrity_not_verified_count"] == 1
    assert observation["supporting_sources"][0]["content_integrity_status"] == (
        "outside-selected-session"
    )


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


def _compiler_source_pointer(
    workspace_root: Path, *, checkout: Path | None = None,
) -> tuple[Path, dict[str, str]]:
    compiler = checkout or workspace_root / "mncs-compiler"
    compiler.mkdir(parents=True)
    _git(compiler, "init", "--quiet")
    _git(compiler, "config", "user.name", "test")
    _git(compiler, "config", "user.email", "test@example.invalid")
    relative = "src/compiler-evidence.mn"
    source = compiler / relative
    source.parent.mkdir(parents=True)
    source_bytes = b"module compiler_evidence\n"
    source.write_bytes(source_bytes)
    _git(compiler, "add", relative)
    _git(compiler, "commit", "--quiet", "-m", "test compiler evidence source")
    pointer = {
        "identity": "ev_compiler_source_read_only",
        "repository": "mncs-compiler",
        "commit": _git(compiler, "rev-parse", "HEAD"),
        "path": relative,
        "sha256": hashlib.sha256(source_bytes).hexdigest(),
    }
    return compiler, pointer


def _register_workspace_source(
    session: SimpleNamespace,
    checkout: Path,
    pointer: dict[str, str],
    *,
    readable: bool = True,
    scan_status: str = "complete",
    include_source_record: bool = False,
) -> None:
    session.snapshot["authority"] = {
        "readable": ["workspace"] if readable else [],
        "writable": ["mncs-environment"],
    }
    environment_checkout = Path(
        session.snapshot["selected_checkouts"]["mncs-environment"]["path"]
    )
    repositories = [{
        "name": "mncs-environment",
        "path": str(environment_checkout),
        "manifest_repository": "mncs-environment",
        "head": _git(environment_checkout, "rev-parse", "HEAD"),
        "git_error": None,
        "worktree_of": None,
    }]
    if include_source_record:
        repositories.append({
            "name": "mncs-compiler",
            "path": str(checkout),
            "manifest_repository": "mncs-compiler",
            "head": pointer["commit"],
            "git_error": None,
            "worktree_of": None,
        })
    session.snapshot["workspace"].update({
        "scan": {"status": scan_status},
        "selection": ["mncs-environment"],
        "repositories": repositories,
    })


def test_project_verifies_workspace_read_only_source_without_compiler_write_scope(
    tmp_path: Path, monkeypatch,
) -> None:
    workspace_root = tmp_path / "workspace"
    compiler, pointer = _compiler_source_pointer(workspace_root)
    record = _observation("ev_environment_observation_one", "ses_compiler_one", "cmp_compiler")
    record["supporting_sources"].append({
        "kind": "compiler-source",
        **pointer,
    })
    bad_pointer = dict(pointer)
    bad_pointer.update({
        "identity": "ev_compiler_source_bad_digest",
        "sha256": "0" * 64,
    })
    record["supporting_sources"].append({
        "kind": "compiler-source",
        **bad_pointer,
    })
    session, _ = _session(tmp_path, [record])
    _register_workspace_source(session, compiler, pointer)
    assert session.snapshot["selected_checkouts"].keys() == {"mncs-environment"}
    assert session.snapshot["authority"]["writable"] == ["mncs-environment"]
    assert _git(compiler, "status", "--porcelain") == ""
    discovery_calls = 0
    discover_workspace = capability_intelligence.workspace_module.discover_workspace

    def counted_discovery(*args, **kwargs):
        nonlocal discovery_calls
        discovery_calls += 1
        return discover_workspace(*args, **kwargs)

    monkeypatch.setattr(
        capability_intelligence.workspace_module, "discover_workspace", counted_discovery,
    )

    result = capability_intelligence.project(session)

    source = result["observations"][0]["supporting_sources"][1]
    bad_source = result["observations"][0]["supporting_sources"][2]
    assert source["status"] == "not-present-in-selected-campaign-evidence-window"
    assert source["registration_scope"] == "selected-campaign-evidence-window"
    assert source["content_integrity_status"] == (
        "workspace-read-only-content-integrity-verified"
    )
    assert bad_source["content_integrity_status"] == "sha256-mismatch"
    assert result["source"]["source_pointer_content_integrity_verified_count"] == 1
    assert result["source"]["source_pointer_content_integrity_not_verified_count"] == 1
    assert discovery_calls == 1
    assert session.snapshot["authority"]["writable"] == ["mncs-environment"]
    assert _git(compiler, "status", "--porcelain") == ""


def test_project_requires_workspace_read_authority_and_complete_root_scan(
    tmp_path: Path,
) -> None:
    scenarios = (
        ("no-workspace-read", False, "complete", False),
        ("incomplete-scan", True, "timed_out", False),
        ("outside-root", True, "complete", True),
    )
    for name, readable, scan_status, outside_root in scenarios:
        scenario_root = tmp_path / name
        workspace_root = scenario_root / "workspace"
        external_checkout = scenario_root / "external" / "mncs-compiler"
        compiler, pointer = _compiler_source_pointer(
            workspace_root,
            checkout=external_checkout if outside_root else None,
        )
        record = _observation(
            f"ev_environment_observation_{name}", "ses_compiler_one", "cmp_compiler",
        )
        record["supporting_sources"].append({
            "kind": "compiler-source",
            **pointer,
        })
        session, _ = _session(scenario_root, [record])
        _register_workspace_source(
            session, compiler, pointer,
            readable=readable, scan_status=scan_status,
            include_source_record=outside_root,
        )

        result = capability_intelligence.project(session)

        source = result["observations"][0]["supporting_sources"][1]
        assert source["status"] == "not-present-in-selected-campaign-evidence-window"
        assert source["registration_scope"] == "selected-campaign-evidence-window"
        assert source["content_integrity_status"] == "outside-selected-session"
