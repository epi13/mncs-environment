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

from mncs_env import capability_intelligence, claims, cli, projections
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


def _add_compiler_native_application_proof_evidence(
    session, state: dict, *, failed_case: bool = False,
    invalid_effect_contract: bool = False,
) -> dict[str, str]:
    root = Path(session.snapshot["workspace"]["root"])
    sources = []
    revisions = {}
    for module, repository, relative, revision_field in (
        capability_intelligence.COMPILER_NATIVE_APPLICATION_MODULES
    ):
        checkout = root / repository
        if not checkout.exists():
            checkout.mkdir()
            _git(checkout, "init", "--quiet")
            _git(checkout, "config", "user.name", "test")
            _git(checkout, "config", "user.email", "test@example.invalid")
        path = checkout / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"module {module}\n", encoding="utf-8")
        _git(checkout, "add", relative)
        _git(checkout, "commit", "--quiet", "-m", f"add {module} source")
        revision = _git(checkout, "rev-parse", "HEAD")
        revisions[revision_field] = revision
        sources.append({
            "module": module,
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
        session.snapshot["selected_checkouts"][repository] = {
            "path": str(checkout),
        }

    artifact_identity = "sha256:" + "e" * 64
    cases = []
    for index in range(3):
        output_digest = f"{index + 1:x}" * 64
        cases.append({
            "case_index": index,
            "artifact_identity": artifact_identity,
            "application_exit": {"exit_code": 1 if failed_case and index == 0 else 0},
            "effect_operations": ["structured_read", "structured_write"],
            "effect_outcomes": ["completed", "completed"],
            "output_sha256": output_digest,
            "stage0_output_sha256": output_digest,
        })
    required_capability = (
        "mncs:0.2:capability:mncs.test.digest::digest_app::test_artifact"
    )
    contract_capability = "wrong_alias" if invalid_effect_contract else "test_artifact"
    report = {
        "schema_version": capability_intelligence.COMPILER_NATIVE_APPLICATION_PROOF_SCHEMA,
        "created_at": "2026-10-18T10:28:51.441053+00:00",
        "objective": "Execute the real MNCS test digest application through the canonical VM provider boundary",
        "producer": {
            "compiler_revision": "a" * 40,
            "language_revision": "b" * 40,
            "stdlib_revision": revisions["stdlib_revision"],
            "test_revision": revisions["test_revision"],
        },
        "application": {
            "entry": capability_intelligence.COMPILER_NATIVE_APPLICATION_ENTRY,
            "required_capability": required_capability,
            "runtime_dispatch_alias": "test_artifact",
            "effect_contract": [
                {
                    "capability": contract_capability,
                    "kind": "structured_read",
                    "target": "digest_app",
                },
                {
                    "capability": contract_capability,
                    "kind": "structured_write",
                    "target": "digest_app",
                },
            ],
            "module_sources": sources,
        },
        "build": {
            "artifact_identity": artifact_identity,
            "cache": {
                "publication": "stored",
                "verification_work": {
                    "front_end_outcome": "verified",
                    "resolved_module_count": len(sources),
                },
            },
        },
        "execution": {
            "exact_result_agreement": True,
            "capability_admission": {
                "authorized_capability_identity": (
                    required_capability
                ),
                "runtime_dispatch_alias": "test_artifact",
            },
            "canonical_vm_cases": cases,
        },
        "limitations": [
            "The file provider is a bounded test adapter for the canonical VM Provider API; it is not a production MNCS service provider.",
            "The provider harness is a separate Rust test program in the Compiler repository; it performs bounded structured file transport.",
        ],
    }
    compiler = root / "mncs-compiler"
    if not compiler.exists():
        compiler.mkdir()
        _git(compiler, "init", "--quiet")
        _git(compiler, "config", "user.name", "test")
        _git(compiler, "config", "user.email", "test@example.invalid")
    relative_proof = capability_intelligence.COMPILER_NATIVE_APPLICATION_PROOF_PATH
    proof_path = compiler / relative_proof
    proof_path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    proof_path.write_bytes(raw)
    _git(compiler, "add", relative_proof)
    _git(compiler, "commit", "--quiet", "-m", "compiler native application proof")
    reference = {
        "identity": capability_intelligence.COMPILER_NATIVE_APPLICATION_PROOF_IDENTITY,
        "repository": "mncs-compiler",
        "commit": _git(compiler, "rev-parse", "HEAD"),
        "path": relative_proof,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    session.snapshot["selected_checkouts"]["mncs-compiler"] = {
        "path": str(compiler),
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
            "source_session_context": {
                "campaign_id": "cmp_mncs_native_application_readiness_compiler_20261013",
                "session_id": "ses_164edc158d9b1637",
                "work_intent_id": "int_compiler_native_readiness",
                "work_intent_goal": work_intent_goal,
                "environment_id": "renv_compiler_campaign",
                "repository": "mncs-compiler",
            },
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


def _drop_pressure_hypothesis(record: dict) -> None:
    record.pop("pressure_hypothesis", None)
    record.pop("pressure_hypothesis_role", None)


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
        "pressure_hypothesis_role": "occurrence",
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


def test_invalid_observation_diagnostic_names_missing_context_fields(tmp_path: Path) -> None:
    report = _observation(
        "ev_environment_missing_observation_context",
        "ses_observation_context",
        "cmp_observation_context",
    )
    report["observation"].pop("source_interface")
    report["observation"].pop("request")
    report["related_work"].pop("session_id")
    session, _state = _session(tmp_path, [report])

    result = capability_intelligence.project(session)

    diagnostic = result["source"]["observation_reference_diagnostics"][0]
    assert diagnostic["verification"] == "observation-contract-invalid"
    assert diagnostic["contract_diagnostics"] == [
        "missing-observation-source-interface",
        "missing-observation-request",
        "missing-related-work-session-id",
    ]


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
    assert result["context_pressure_candidate_count"] == 1
    candidate = result["context_pressure_candidates"][0]
    assert candidate["kind"] == "declared-repository-selection-gap"
    assert candidate["interpretation"] == "hypothesis"
    assert candidate["recurrence_status"] == "not-established"
    assert candidate["affected_repository"] == "mncs-compiler"
    assert candidate["basis"]["outside_selected_reference_count"] == 1
    assert candidate["evidence_references"][0]["identity"] == (
        "ev_environment_compiler_source_outside_selection"
    )
    assert candidate["evidence_references"][0]["verification"] == (
        "outside-selected-session"
    )
    assert "ordinary read or write access" in candidate["recommendation"]

    compact = capability_intelligence.project(session, max_result_bytes=6_000)
    assert compact["subject"]["workspace_alignment"][
        "unselected_declared_repositories"
    ] == ["mncs-compiler"]
    assert compact["source"]["evidence_visibility"][
        "outside_selected_repositories"
    ] == [{"repository": "mncs-compiler", "diagnostic_count": 1}]
    assert compact["context_pressure_candidate_count"] == 1
    assert compact["context_pressure_candidates"][0]["evidence_references"][0][
        "identity"
    ] == "ev_environment_compiler_source_outside_selection"


def test_unselected_repository_candidate_links_claimless_preflight_without_success(
    tmp_path: Path,
) -> None:
    record = _observation(
        "ev_environment_unselected_repository_preflight",
        "ses_projection_reader",
        "cmp_projection_reader",
    )
    session, _state = _session(tmp_path, [record])
    session.snapshot["intent"]["repositories"] = ["mncs-compiler"]

    def eligible_event(
        identity: str,
        sequence: int,
        work_intent_id: str,
        campaign_id: str | None = None,
    ) -> dict:
        return {
            "identity": identity,
            "sequence": sequence,
            "type": "authority.integration-eligible",
            "session_id": session.session_id,
            "producer": "environment",
            "payload": {
                "action": "merge",
                "repository": "mncs-compiler",
                "scope": {
                    "kind": "worktree",
                    "repository": "mncs-compiler",
                    "branch": "campaign/native-pressure",
                    "exclusive": False,
                },
                "authorization_basis": "writable-repository-rights",
                "eligibility_basis": (
                    "exact-observed-scope-and-repository-native-concurrency"
                ),
                "persistent_claim_required": False,
                "operation_executed": False,
                "repository_preflight_state": {
                    "schema_version": (
                        "mncs.environment.integration-preflight-state/1"
                    ),
                    "observation_basis": (
                        "fresh-registered-checkout-inspection"
                    ),
                    "checkout_identity": "checkout:compiler-campaign",
                    "branch": "campaign/native-pressure",
                    "branch_status": "attached",
                    "head": "e" * 40,
                    "head_status": "measured",
                    "working_tree_status": "clean",
                },
                "campaign_id": campaign_id or session.snapshot["campaign"]["identity"],
                "work_intent_id": work_intent_id,
                "coordination_overlap_count": 0,
                "coordination_overlaps": [],
                "coordination_overlaps_truncated": False,
            },
        }

    events = [
        eligible_event(
            "evt_preflight_other_campaign",
            39,
            session.snapshot["intent"]["identity"],
            "cmp_another_campaign",
        ),
        eligible_event(
            "evt_preflight_other_work_intent",
            40,
            "int_another_objective",
        ),
        eligible_event(
            "evt_preflight_same_work_intent",
            41,
            session.snapshot["intent"]["identity"],
        ),
    ]
    session.recent_events = lambda *, limit, before_sequence=None: {
        "event_count": len(events), "events": events[-limit:],
        "truncated": False, "next_before_sequence": None,
    }

    result = capability_intelligence.project(session)

    candidate = result["context_pressure_candidates"][0]
    assert candidate["affected_repository"] == "mncs-compiler"
    assert candidate["interpretation"] == "hypothesis"
    assert candidate["integration_preflight_search"]["status"] == (
        "matching-preflight-observed-in-selected-window"
    )
    assert candidate["integration_preflight_search"]["matched_records_returned"] == 1
    assert candidate["integration_preflight_observations"] == [{
        "action": "merge",
        "persistent_claim_required": False,
        "operation_executed": False,
        "scope": {
            "kind": "worktree",
            "repository": "mncs-compiler",
            "branch": "campaign/native-pressure",
            "exclusive": False,
        },
        "repository_preflight_state": {
            "schema_version": "mncs.environment.integration-preflight-state/1",
            "observation_basis": "fresh-registered-checkout-inspection",
            "checkout_identity": "checkout:compiler-campaign",
            "branch": "campaign/native-pressure",
            "branch_status": "attached",
            "head": "e" * 40,
            "head_status": "measured",
            "working_tree_status": "clean",
        },
        "preflight_state_interpretation_limit": (
            "This is the local destination checkout preimage at preflight; it "
            "does not establish source branch, remote ref state, later "
            "execution, or integration outcome."
        ),
        "event_reference": {
            "identity": "evt_preflight_same_work_intent",
            "sequence": 41,
            "type": "authority.integration-eligible",
        },
    }]
    assert "did not execute integration" in candidate["recommendation"]
    assert any(
        "successful outcome" in item for item in candidate["not_measured"]
    )

    compact = capability_intelligence.project(session, max_result_bytes=6_000)
    compact_candidate = compact["context_pressure_candidates"][0]
    assert compact_candidate["integration_preflight_search"][
        "matched_records_returned"
    ] == 1
    assert compact_candidate["integration_preflight_observations"][0][
        "operation_executed"
    ] is False
    assert compact_candidate["integration_preflight_observations"][0][
        "event_reference"]["identity"] == "evt_preflight_same_work_intent"


def test_project_omits_diagnostics_when_work_intent_repositories_are_aligned(
    tmp_path: Path,
) -> None:
    session, _state = _session(tmp_path, [])
    session.snapshot["intent"]["repositories"] = ["mncs-environment"]

    result = capability_intelligence.project(session)

    assert "workspace_alignment" not in result["subject"]
    assert "evidence_visibility" not in result["source"]
    assert result.get("context_pressure_candidate_count", 0) == 0
    assert result.get("context_pressure_candidates", []) == []


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
        "workspace-scope-campaign-projection-readback-20261024",
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
    assert result["source"]["observation_records_verified"] == 4
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
    assert hypothesis["observation_count"] == 0
    assert hypothesis["unclassified_observation_count"] == 1
    assert hypothesis["role_classification_status"] == "incomplete"
    assert hypothesis["unclassified_evidence"][0]["observation_identity"] == (
        records[0]["identity"]
    )
    relation = next(
        item for item in result["improvement_relations"]
        if item["identity"] == "im_environment_workspace_scope_visibility_20261024"
    )
    assert relation["verification_record_status"] == (
        "record-integrity-verified-in-projection"
    )
    assert relation["effectiveness_assessment"] == "not-assessed"
    assert relation["subsequent_record_status"] == "not-recorded"


def test_resource_sample_followup_links_distinct_session_without_claiming_effectiveness(
    tmp_path: Path,
) -> None:
    evidence_dir = Path(__file__).resolve().parents[1] / (
        "evidence/environmental-capability"
    )
    names = (
        "resource-sample-live-readback-20261024",
        "resource-sample-session-linked-followup-20261024",
        "resource-sample-followup-relation-20261024",
    )
    records = [
        json.loads((evidence_dir / f"{name}.json").read_text(encoding="utf-8"))
        for name in names
    ]
    session, _state = _session(tmp_path, records)

    result = capability_intelligence.project(session)

    assert result["source"]["observation_records_verified"] == 3
    followup = next(
        item for item in result["observations"]
        if item["identity"] == "ev_environment_resource_sample_session_linked_followup_20261024"
    )
    assert followup["observation"]["outcome"]["event_correlation_status"] == (
        "matched-by-provider-request-id"
    )
    relation = next(
        item for item in result["improvement_relations"]
        if item["identity"] == "im_environment_monitor_resource_samples_followup_20261024"
    )
    assert relation["verification_record_status"] == (
        "record-integrity-verified-in-projection"
    )
    assert relation["subsequent_record_status"] == (
        "record-integrity-verified-in-projection"
    )
    assert relation["effectiveness_assessment"] == "not-assessed"


def test_append_only_correction_requires_exact_verified_observation_pointer(
    tmp_path: Path,
) -> None:
    def project_correction(root: Path, *, corrupt_target_digest: bool):
        target = _observation(
            "ev_environment_correctable_followup_relation",
            "ses_environment_followup",
            "cmp_environment_followup",
        )
        target["pressure_hypothesis"]["identity"] = "ph_environment_correctable_record"
        implementation = {
            "identity": "ev_environment_correctable_implementation",
            "repository": "mncs-environment",
            "commit": "c" * 40,
            "path": "mncs_env/capability_intelligence.py",
            "sha256": "d" * 64,
        }
        target["improvement_relation"] = {
            "identity": "im_environment_relation_original",
            "addresses_hypothesis_identity": "ph_environment_correctable_record",
            "implementation_reference": implementation,
            "verification_observation_identity": "ev_environment_correction_verification",
            "subsequent_observation_identity": "ev_environment_correction_followup",
            "basis": "Original relation with incomplete source metadata.",
            "not_measured": ["original measurement limitation"],
        }
        target["supporting_sources"] = [{
            "kind": "campaign-evidence-reference",
            "identity": "ev_environment_identity_only_source",
            "repository": "mncs-environment",
        }]
        target["not_measured"] = [
            "empirical elimination of an unrelated evidence visibility pressure",
        ]
        verification = _observation(
            "ev_environment_correction_verification",
            "ses_environment_followup",
            "cmp_environment_followup",
        )
        _drop_pressure_hypothesis(verification)
        followup = _observation(
            "ev_environment_correction_followup",
            "ses_environment_followup",
            "cmp_environment_followup",
        )
        _drop_pressure_hypothesis(followup)
        session, state = _session(root, [target, verification, followup])
        checkout = Path(session.snapshot["selected_checkouts"][
            "mncs-environment"
        ]["path"])
        implementation_path = checkout / "mncs_env/capability_intelligence.py"
        implementation_path.parent.mkdir(parents=True, exist_ok=True)
        implementation_bytes = b"verified environmental projection implementation\n"
        implementation_path.write_bytes(implementation_bytes)
        _git(checkout, "add", "mncs_env/capability_intelligence.py")
        _git(checkout, "commit", "--quiet", "-m", "implementation source")
        implementation_source = {
            "identity": "ev_environment_correction_implementation_source",
            "repository": "mncs-environment",
            "commit": _git(checkout, "rev-parse", "HEAD"),
            "path": "mncs_env/capability_intelligence.py",
            "sha256": hashlib.sha256(implementation_bytes).hexdigest(),
        }
        state["evidence"].append(implementation_source)
        state["evidence_count"] = len(state["evidence"])
        target_reference = dict(state["evidence"][0])
        if corrupt_target_digest:
            target_reference["sha256"] = "e" * 64

        correction = _observation(
            "ev_environment_corrected_followup_relation",
            "ses_environment_correction",
            "cmp_environment_followup",
        )
        _drop_pressure_hypothesis(correction)
        correction["record_correction"] = {
            "schema_version": capability_intelligence.OBSERVATION_CORRECTION_SCHEMA,
            "target_reference": target_reference,
            "corrected_field_paths": [
                "supporting_sources",
                "not_measured",
            ],
            "replacement_values": {
                "supporting_sources": [
                    {"kind": "campaign-evidence-reference", **state["evidence"][1]},
                    {"kind": "campaign-evidence-reference", **state["evidence"][2]},
                    {"kind": "campaign-evidence-reference", **state["evidence"][3]},
                ],
                "not_measured": [
                    "resource consumption attributable to this agent or child processes",
                    "CPU utilization for this sample",
                    "whether samples differ across this session or campaign",
                    "whether host conditions constrained a development operation",
                ],
            },
            "reason": "Replace incomplete source identities and a copied measurement limit.",
        }
        relative = "evidence/environmental-capability/correction.json"
        raw = json.dumps(
            correction, sort_keys=True, separators=(",", ":"),
        ).encode()
        correction_path = Path(session.snapshot["selected_checkouts"][
            "mncs-environment"
        ]["path"]) / relative
        correction_path.write_bytes(raw)
        _git(correction_path.parents[2], "add", relative)
        _git(correction_path.parents[2], "commit", "--quiet", "-m", "correction record")
        correction_reference = {
            "identity": correction["identity"],
            "repository": "mncs-environment",
            "commit": _git(correction_path.parents[2], "rev-parse", "HEAD"),
            "path": relative,
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        state["evidence"].append(correction_reference)
        state["evidence_count"] = len(state["evidence"])
        return capability_intelligence.project(session)

    verified = project_correction(tmp_path / "verified", corrupt_target_digest=False)

    assert verified["status"] == "ready"
    assert verified["source"]["record_corrections_applied_count"] == 1
    assert verified["source"]["record_corrections_unverified_count"] == 0
    corrected = next(
        item for item in verified["observations"]
        if item["identity"] == "ev_environment_corrected_followup_relation"
    )
    assert corrected["record_correction"]["status"] == (
        "applied-to-verified-target"
    )
    original = next(
        item for item in verified["observations"]
        if item["identity"] == "ev_environment_correctable_followup_relation"
    )
    assert original["record_correction"]["status"] == (
        "corrected-by-verified-record"
    )
    assert original["improvement_relation_status"] == "validated"
    assert original["measurement_limits"] == [
        "resource consumption attributable to this agent or child processes",
        "CPU utilization for this sample",
        "whether samples differ across this session or campaign",
        "whether host conditions constrained a development operation",
    ]
    assert [item["status"] for item in original["supporting_sources"]] == [
        "verified", "verified", "not-an-environmental-observation",
    ]
    assert all(
        item["content_integrity_status"]
        in capability_intelligence.SOURCE_INTEGRITY_VERIFIED
        for item in original["supporting_sources"]
    )
    assert [item["identity"] for item in verified["improvement_relations"]] == [
        "im_environment_relation_original",
    ]
    relation = verified["improvement_relations"][0]
    assert relation["verification_record_status"] == (
        "record-integrity-verified-in-projection"
    )
    assert relation["subsequent_record_status"] == (
        "record-integrity-verified-in-projection"
    )
    assert relation["effectiveness_assessment"] == "not-assessed"
    compact = capability_intelligence._compact_projection(
        verified, 100_000, 24_000,
    )
    compact_original = next(
        item for item in compact["observations"]
        if item["identity"] == "ev_environment_correctable_followup_relation"
    )
    assert compact_original["record_correction"]["replacement_values"][
        "not_measured"
    ] == original["measurement_limits"]
    assert compact_original["record_correction"][
        "correction_evidence_reference"
    ]["identity"] == "ev_environment_corrected_followup_relation"

    mismatched = project_correction(
        tmp_path / "mismatched", corrupt_target_digest=True,
    )

    assert mismatched["status"] == "partial"
    assert mismatched["source"]["record_corrections_applied_count"] == 0
    assert mismatched["source"]["record_corrections_unverified_count"] == 1
    rejected = next(
        item for item in mismatched["observations"]
        if item["identity"] == "ev_environment_corrected_followup_relation"
    )
    assert rejected["record_correction"]["status"] == "target-reference-mismatch"
    mismatched_original = next(
        item for item in mismatched["observations"]
        if item["identity"] == "ev_environment_correctable_followup_relation"
    )
    assert mismatched_original["measurement_limits"] == [
        "empirical elimination of an unrelated evidence visibility pressure",
    ]
    assert mismatched_original["supporting_sources"][0][
        "content_integrity_status"
    ] == "source-reference-invalid"
    assert [item["identity"] for item in mismatched["improvement_relations"]] == [
        "im_environment_relation_original",
    ]


def test_append_only_correction_completes_integrity_verified_invalid_observation(
    tmp_path: Path,
) -> None:
    target = _observation(
        "ev_environment_incomplete_observation_record",
        "ses_contract_completion",
        "cmp_contract_completion",
    )
    target["observation"].pop("request")
    target["observation"].pop("outcome")
    session, state = _session(tmp_path, [target])
    target_reference = dict(state["evidence"][0])

    correction = _observation(
        "ev_environment_incomplete_observation_correction",
        "ses_contract_completion",
        "cmp_contract_completion",
    )
    _drop_pressure_hypothesis(correction)
    correction["observation"]["kind"] = "environmental-observation-correction"
    correction["record_correction"] = {
        "schema_version": capability_intelligence.OBSERVATION_CORRECTION_SCHEMA,
        "target_reference": target_reference,
        "corrected_field_paths": [
            "observation.request",
            "observation.outcome",
        ],
        "replacement_values": {
            "observation.request": {
                "basis": "recorded from the original bounded CLI invocation",
                "operation": "environmental-capability projection readback",
                "projection_identity": "eci_contract_completion",
                "source_event_identities": ["evt_contract_failure", "evt_contract_recovery"],
            },
            "observation.outcome": {
                "basis": "recorded from the original CLI response",
                "original_record_status": "observation-contract-invalid",
                "original_contract_diagnostics": [
                    "missing-observation-request",
                    "missing-observation-outcome-or-projection-summary",
                ],
                "source_event_summary": {
                    "failure": {
                        "identity": "evt_contract_failure",
                        "sequence": 5,
                        "skip_reason": "missing-observation-request",
                    },
                },
                "status": "partial",
                "response_truncated": True,
            },
        },
        "reason": (
            "Complete fields omitted from the original content-addressed record; "
            "retain its original pointer and schema diagnostic."
        ),
    }
    relative = "evidence/environmental-capability/observation-correction.json"
    raw = json.dumps(correction, sort_keys=True, separators=(",", ":")).encode()
    correction_path = Path(session.snapshot["selected_checkouts"][
        "mncs-environment"
    ]["path"]) / relative
    correction_path.write_bytes(raw)
    _git(correction_path.parents[2], "add", relative)
    _git(correction_path.parents[2], "commit", "--quiet", "-m", "append-only correction")
    state["evidence"].append({
        "identity": correction["identity"],
        "repository": "mncs-environment",
        "commit": _git(correction_path.parents[2], "rev-parse", "HEAD"),
        "path": relative,
        "sha256": hashlib.sha256(raw).hexdigest(),
    })
    state["evidence_count"] = len(state["evidence"])

    result = capability_intelligence.project(session)

    assert result["status"] == "ready"
    assert result["source"]["record_corrections_applied_count"] == 1
    assert result["source"]["record_corrections_unverified_count"] == 0
    assert result["source"]["observation_records_unavailable"] == 0
    corrected = next(
        item for item in result["observations"]
        if item["identity"] == target["identity"]
    )
    assert corrected["record_correction"]["status"] == (
        "completed-by-append-only-record"
    )
    assert corrected["record_correction"]["correction_observation_identity"] == (
        correction["identity"]
    )
    assert corrected["observation"]["request"] == correction[
        "record_correction"
    ]["replacement_values"]["observation.request"]
    assert corrected["observation"]["outcome"] == correction[
        "record_correction"
    ]["replacement_values"]["observation.outcome"]
    diagnostic = next(
        item for item in result["source"]["observation_reference_diagnostics"]
        if item["identity"] == target["identity"]
    )
    assert diagnostic["verification"] == "observation-contract-invalid"
    assert diagnostic["contract_diagnostics"] == [
        "missing-observation-request",
        "missing-observation-outcome-or-projection-summary",
    ]
    assert diagnostic["correction_status"] == "completed-by-append-only-record"
    assert diagnostic["correction_observation_identity"] == correction["identity"]
    compact = capability_intelligence._compact_projection(
        result, 100_000, 24_000,
    )
    compact_corrected = next(
        item for item in compact["observations"]
        if item["identity"] == target["identity"]
    )
    assert compact_corrected["observation"]["request_summary"][
        "projection_identity"
    ] == "eci_contract_completion"
    assert compact_corrected["observation"]["request_summary"][
        "source_event_identities_count"
    ] == 2
    assert compact_corrected["observation"]["outcome_context"][
        "original_record_status"
    ] == "observation-contract-invalid"
    assert compact_corrected["observation"]["outcome_context"][
        "original_contract_diagnostics"
    ] == [
        "missing-observation-request",
        "missing-observation-outcome-or-projection-summary",
    ]
    assert compact_corrected["observation"]["outcome_context"][
        "source_event_summary"
    ]["failure"]["identity"] == "evt_contract_failure"
    assert compact_corrected["record_correction"]["replacement_values"][
        "observation.request"
    ]["key_count"] == 4


def test_observation_correction_restores_source_interface_and_verified_sources(
    tmp_path: Path,
) -> None:
    target = _observation(
        "ev_environment_missing_source_interface_and_request",
        "ses_contract_completion",
        "cmp_contract_completion",
    )
    target["observation"].pop("source_interface")
    target["observation"].pop("request")
    _drop_pressure_hypothesis(target)
    target["supporting_sources"] = [{"kind": "unverified-legacy-pointer"}]
    session, state = _session(tmp_path, [target])
    checkout = Path(session.snapshot["selected_checkouts"][
        "mncs-environment"
    ]["path"])
    source_path = checkout / "src" / "authority_contract.py"
    source_path.parent.mkdir(parents=True)
    source_bytes = b"# committed source for append-only observation repair\n"
    source_path.write_bytes(source_bytes)
    relative_source = source_path.relative_to(checkout).as_posix()
    _git(checkout, "add", relative_source)
    _git(checkout, "commit", "--quiet", "-m", "add verified correction source")
    source = {
        "identity": "mncs_environment_authority_contract_source",
        "repository": "mncs-environment",
        "commit": _git(checkout, "rev-parse", "HEAD"),
        "path": relative_source,
        "sha256": hashlib.sha256(source_bytes).hexdigest(),
        "kind": "implementation-source",
    }
    state["evidence"].append({key: source[key] for key in (
        "identity", "repository", "commit", "path", "sha256",
    )})
    state["evidence_count"] = len(state["evidence"])

    correction = _observation(
        "ev_environment_source_interface_correction",
        "ses_contract_completion",
        "cmp_contract_completion",
    )
    _drop_pressure_hypothesis(correction)
    correction["observation"]["kind"] = "environmental-observation-correction"
    correction["supporting_sources"] = [source]
    fields = [
        "observation.source_interface",
        "observation.request",
        "supporting_sources",
    ]
    replacements = {
        "observation.source_interface": (
            "MNCS Environment Session.check and capability projection"
        ),
        "observation.request": {
            "scenario": "An exact integration-eligibility observation was recorded without its request context.",
            "evidence_basis": "The integrity-verified target outcome, related-work identity, and committed authority implementation establish the operation context.",
        },
        "supporting_sources": [source],
    }
    correction["record_correction"] = {
        "schema_version": capability_intelligence.OBSERVATION_CORRECTION_SCHEMA,
        "target_reference": dict(state["evidence"][0]),
        "corrected_field_paths": fields,
        "replacement_values": replacements,
        "reason": (
            "Restore omitted interface and request context and replace the "
            "legacy unaddressed source with an integrity-verified pointer."
        ),
    }
    assert capability_intelligence._validate_observation_correction(
        correction["record_correction"],
    ) is not None
    over_limit = dict(correction["record_correction"])
    over_limit["corrected_field_paths"] = [*fields, "not_measured"]
    over_limit["replacement_values"] = {
        **replacements,
        "not_measured": ["one more correction field"],
    }
    assert capability_intelligence._validate_observation_correction(
        over_limit,
    ) is None

    relative_correction = (
        "evidence/environmental-capability/source-interface-correction.json"
    )
    raw = json.dumps(correction, sort_keys=True, separators=(",", ":")).encode()
    correction_path = checkout / relative_correction
    correction_path.parent.mkdir(parents=True, exist_ok=True)
    correction_path.write_bytes(raw)
    _git(checkout, "add", relative_correction)
    _git(checkout, "commit", "--quiet", "-m", "append source interface correction")
    state["evidence"].append({
        "identity": correction["identity"],
        "repository": "mncs-environment",
        "commit": _git(checkout, "rev-parse", "HEAD"),
        "path": relative_correction,
        "sha256": hashlib.sha256(raw).hexdigest(),
    })
    state["evidence_count"] = len(state["evidence"])

    result = capability_intelligence.project(session)

    assert result["source"]["record_corrections_applied_count"] == 1
    assert result["source"]["record_corrections_unverified_count"] == 0
    corrected = next(
        item for item in result["observations"]
        if item["identity"] == target["identity"]
    )
    assert corrected["record_correction"]["status"] == (
        "completed-by-append-only-record"
    )
    assert corrected["observation"]["source_interface"] == replacements[
        "observation.source_interface"
    ]
    assert corrected["observation"]["request"] == replacements[
        "observation.request"
    ]
    assert corrected["pressure_hypothesis"] is None
    assert corrected["supporting_sources"][0][
        "content_integrity_status"
    ] == "content-integrity-verified"
    diagnostic = next(
        item for item in result["source"]["observation_reference_diagnostics"]
        if item["identity"] == target["identity"]
    )
    assert diagnostic["correction_status"] == "completed-by-append-only-record"
    compact = capability_intelligence._compact_projection(
        result, 100_000, 50_000,
    )
    compact_target = next(
        item for item in compact["observations"]
        if item["identity"] == target["identity"]
    )
    assert compact_target["record_correction"]["replacement_values"][
        "observation.source_interface"
    ] == replacements["observation.source_interface"]


def test_observation_contract_correction_requires_complete_contract(
    tmp_path: Path,
) -> None:
    target = _observation(
        "ev_environment_still_incomplete_observation_record",
        "ses_contract_completion",
        "cmp_contract_completion",
    )
    target["observation"].pop("request")
    target["observation"].pop("outcome")
    session, state = _session(tmp_path, [target])
    correction = _observation(
        "ev_environment_partial_observation_correction",
        "ses_contract_completion",
        "cmp_contract_completion",
    )
    _drop_pressure_hypothesis(correction)
    correction["record_correction"] = {
        "schema_version": capability_intelligence.OBSERVATION_CORRECTION_SCHEMA,
        "target_reference": dict(state["evidence"][0]),
        "corrected_field_paths": ["observation.request"],
        "replacement_values": {
            "observation.request": {"operation": "partial-correction"},
        },
        "reason": "The outcome remains unavailable in this correction.",
    }
    relative = "evidence/environmental-capability/partial-observation-correction.json"
    raw = json.dumps(correction, sort_keys=True, separators=(",", ":")).encode()
    path = Path(session.snapshot["selected_checkouts"]["mncs-environment"]["path"])
    correction_path = path / relative
    correction_path.write_bytes(raw)
    _git(path, "add", relative)
    _git(path, "commit", "--quiet", "-m", "append partial observation correction")
    state["evidence"].append({
        "identity": correction["identity"],
        "repository": "mncs-environment",
        "commit": _git(path, "rev-parse", "HEAD"),
        "path": relative,
        "sha256": hashlib.sha256(raw).hexdigest(),
    })
    state["evidence_count"] = len(state["evidence"])

    result = capability_intelligence.project(session)

    correction_projection = next(
        item for item in result["observations"]
        if item["identity"] == correction["identity"]
    )
    assert result["source"]["record_corrections_applied_count"] == 0
    assert result["source"]["record_corrections_unverified_count"] == 1
    assert correction_projection["record_correction"]["status"] == (
        "replacement-does-not-complete-observation-contract"
    )
    assert all(item["identity"] != target["identity"]
               for item in result["observations"])


def test_observation_contract_correction_can_restore_pressure_recommendation(
    tmp_path: Path,
) -> None:
    target = _observation(
        "ev_environment_missing_pressure_recommendation",
        "ses_pressure_recommendation_correction",
        "cmp_pressure_recommendation_correction",
    )
    target["pressure_hypothesis"].pop("actionable_recommendation")
    session, state = _session(tmp_path, [target])
    correction = _observation(
        "ev_environment_pressure_recommendation_correction",
        "ses_pressure_recommendation_correction",
        "cmp_pressure_recommendation_correction",
    )
    _drop_pressure_hypothesis(correction)
    correction["observation"]["kind"] = "environmental-observation-correction"
    correction["record_correction"] = {
        "schema_version": capability_intelligence.OBSERVATION_CORRECTION_SCHEMA,
        "target_reference": dict(state["evidence"][0]),
        "corrected_field_paths": [
            "pressure_hypothesis.actionable_recommendation",
        ],
        "replacement_values": {
            "pressure_hypothesis.actionable_recommendation": (
                "Keep this verified session-selection improvement discoverable "
                "through the bounded coordination projection."
            ),
        },
        "reason": (
            "Restore the missing actionable recommendation without rewriting "
            "the content-addressed observation."
        ),
    }
    relative = (
        "evidence/environmental-capability/"
        "pressure-recommendation-correction.json"
    )
    correction_bytes = json.dumps(
        correction, sort_keys=True, separators=(",", ":"),
    ).encode()
    checkout = Path(session.snapshot["selected_checkouts"][
        "mncs-environment"
    ]["path"])
    correction_path = checkout / relative
    correction_path.parent.mkdir(parents=True, exist_ok=True)
    correction_path.write_bytes(correction_bytes)
    _git(checkout, "add", relative)
    _git(checkout, "commit", "--quiet", "-m", "append pressure recommendation correction")
    state["evidence"].append({
        "identity": correction["identity"],
        "repository": "mncs-environment",
        "commit": _git(checkout, "rev-parse", "HEAD"),
        "path": relative,
        "sha256": hashlib.sha256(correction_bytes).hexdigest(),
    })
    state["evidence_count"] = len(state["evidence"])

    result = capability_intelligence.project(session)
    corrected_target = next(
        item for item in result["observations"]
        if item["identity"] == target["identity"]
    )
    correction_observation = next(
        item for item in result["observations"]
        if item["identity"] == correction["identity"]
    )

    assert result["source"]["record_corrections_applied_count"] == 1
    assert result["source"]["record_corrections_unverified_count"] == 0
    assert corrected_target["pressure_hypothesis"][
        "actionable_recommendation"
    ] == correction["record_correction"]["replacement_values"][
        "pressure_hypothesis.actionable_recommendation"
    ]
    assert corrected_target["record_correction"]["status"] == (
        "completed-by-append-only-record"
    )
    assert correction_observation["record_correction"]["status"] == (
        "applied-to-integrity-verified-contract-invalid-target"
    )
    compact = capability_intelligence._compact_projection(
        result, 100_000, 24_000,
    )
    compact_target = next(
        item for item in compact["observations"]
        if item["identity"] == target["identity"]
    )
    assert compact_target["record_correction"]["replacement_values"] == {
        "pressure_hypothesis.actionable_recommendation": (
            correction["record_correction"]["replacement_values"][
                "pressure_hypothesis.actionable_recommendation"
            ]
        ),
    }


def test_observation_correction_cannot_rewrite_valid_pressure_recommendation(
    tmp_path: Path,
) -> None:
    target = _observation(
        "ev_environment_valid_pressure_recommendation",
        "ses_pressure_recommendation_immutability",
        "cmp_pressure_recommendation_immutability",
    )
    session, state = _session(tmp_path, [target])
    correction = _observation(
        "ev_environment_valid_pressure_recommendation_correction_attempt",
        "ses_pressure_recommendation_immutability",
        "cmp_pressure_recommendation_immutability",
    )
    _drop_pressure_hypothesis(correction)
    correction["observation"]["kind"] = "environmental-observation-correction"
    correction["record_correction"] = {
        "schema_version": capability_intelligence.OBSERVATION_CORRECTION_SCHEMA,
        "target_reference": dict(state["evidence"][0]),
        "corrected_field_paths": [
            "pressure_hypothesis.actionable_recommendation",
        ],
        "replacement_values": {
            "pressure_hypothesis.actionable_recommendation": (
                "Replace a valid recommendation."
            ),
        },
        "reason": "A valid pressure recommendation cannot be rewritten.",
    }
    relative = (
        "evidence/environmental-capability/"
        "valid-pressure-recommendation-correction.json"
    )
    correction_bytes = json.dumps(
        correction, sort_keys=True, separators=(",", ":"),
    ).encode()
    checkout = Path(session.snapshot["selected_checkouts"][
        "mncs-environment"
    ]["path"])
    correction_path = checkout / relative
    correction_path.parent.mkdir(parents=True, exist_ok=True)
    correction_path.write_bytes(correction_bytes)
    _git(checkout, "add", relative)
    _git(checkout, "commit", "--quiet", "-m", "reject valid recommendation rewrite")
    state["evidence"].append({
        "identity": correction["identity"],
        "repository": "mncs-environment",
        "commit": _git(checkout, "rev-parse", "HEAD"),
        "path": relative,
        "sha256": hashlib.sha256(correction_bytes).hexdigest(),
    })
    state["evidence_count"] = len(state["evidence"])

    result = capability_intelligence.project(session)
    unchanged_target = next(
        item for item in result["observations"]
        if item["identity"] == target["identity"]
    )
    correction_projection = next(
        item for item in result["observations"]
        if item["identity"] == correction["identity"]
    )

    assert result["source"]["record_corrections_applied_count"] == 0
    assert result["source"]["record_corrections_unverified_count"] == 1
    assert unchanged_target["pressure_hypothesis"][
        "actionable_recommendation"
    ] == target["pressure_hypothesis"]["actionable_recommendation"]
    assert correction_projection["record_correction"]["status"] == (
        "replacement-values-unverified-or-unchanged"
    )


def test_observation_contract_correction_cannot_rewrite_valid_observation(
    tmp_path: Path,
) -> None:
    target = _observation(
        "ev_environment_valid_observation_not_rewritten",
        "ses_contract_immutability",
        "cmp_contract_immutability",
    )
    session, state = _session(tmp_path, [target])
    correction = _observation(
        "ev_environment_valid_observation_correction_attempt",
        "ses_contract_immutability",
        "cmp_contract_immutability",
    )
    _drop_pressure_hypothesis(correction)
    correction["record_correction"] = {
        "schema_version": capability_intelligence.OBSERVATION_CORRECTION_SCHEMA,
        "target_reference": dict(state["evidence"][0]),
        "corrected_field_paths": ["observation.request"],
        "replacement_values": {
            "observation.request": {"operation": "rewrite-existing-request"},
        },
        "reason": "A valid target must remain unchanged.",
    }
    relative = "evidence/environmental-capability/valid-observation-correction.json"
    raw = json.dumps(correction, sort_keys=True, separators=(",", ":")).encode()
    path = Path(session.snapshot["selected_checkouts"]["mncs-environment"]["path"])
    correction_path = path / relative
    correction_path.write_bytes(raw)
    _git(path, "add", relative)
    _git(path, "commit", "--quiet", "-m", "reject valid observation rewrite")
    state["evidence"].append({
        "identity": correction["identity"],
        "repository": "mncs-environment",
        "commit": _git(path, "rev-parse", "HEAD"),
        "path": relative,
        "sha256": hashlib.sha256(raw).hexdigest(),
    })
    state["evidence_count"] = len(state["evidence"])

    result = capability_intelligence.project(session)

    correction_projection = next(
        item for item in result["observations"]
        if item["identity"] == correction["identity"]
    )
    unchanged_target = next(
        item for item in result["observations"]
        if item["identity"] == target["identity"]
    )
    assert result["source"]["record_corrections_applied_count"] == 0
    assert result["source"]["record_corrections_unverified_count"] == 1
    assert correction_projection["record_correction"]["status"] == (
        "replacement-values-unverified-or-unchanged"
    )
    assert unchanged_target["observation"]["request"] == target[
        "observation"
    ]["request"]


def test_append_only_pressure_role_correction_does_not_count_context_as_recurrence(
    tmp_path: Path,
) -> None:
    pressure = _observation(
        "ev_environment_single_pressure_occurrence",
        "ses_pressure_role",
        "cmp_pressure_role",
    )
    pressure["pressure_hypothesis"]["identity"] = "ph_single_pressure_occurrence"
    verification = _observation(
        "ev_environment_pressure_role_verification",
        "ses_pressure_role",
        "cmp_pressure_role",
    )
    verification["observation"]["kind"] = "environmental-improvement-verification"
    verification["pressure_hypothesis"] = dict(pressure["pressure_hypothesis"])
    session, state = _session(tmp_path, [pressure, verification])

    before = capability_intelligence.project(session)
    before_hypothesis = before["pressure_hypotheses"][0]
    assert before_hypothesis["recurrence_status"] == "repeated-within-session"
    assert before_hypothesis["observation_count"] == 2

    target_reference = dict(state["evidence"][1])
    correction = _observation(
        "ev_environment_pressure_role_correction",
        "ses_pressure_role_correction",
        "cmp_pressure_role",
    )
    _drop_pressure_hypothesis(correction)
    correction["observation"]["kind"] = "environmental-observation-correction"
    correction["record_correction"] = {
        "schema_version": capability_intelligence.OBSERVATION_CORRECTION_SCHEMA,
        "target_reference": target_reference,
        "corrected_field_paths": ["pressure_hypothesis_role"],
        "replacement_values": {
            "pressure_hypothesis_role": "supporting-context",
        },
        "reason": (
            "This verification record restates the linked hypothesis for context; "
            "it does not record a second pressure occurrence."
        ),
    }
    relative = "evidence/environmental-capability/pressure-role-correction.json"
    correction_bytes = json.dumps(
        correction, sort_keys=True, separators=(",", ":"),
    ).encode()
    checkout = Path(session.snapshot["selected_checkouts"][
        "mncs-environment"
    ]["path"])
    correction_path = checkout / relative
    correction_path.parent.mkdir(parents=True, exist_ok=True)
    correction_path.write_bytes(correction_bytes)
    _git(checkout, "add", relative)
    _git(checkout, "commit", "--quiet", "-m", "append-only pressure role correction")
    state["evidence"].append({
        "identity": correction["identity"],
        "repository": "mncs-environment",
        "commit": _git(checkout, "rev-parse", "HEAD"),
        "path": relative,
        "sha256": hashlib.sha256(correction_bytes).hexdigest(),
    })
    state["evidence_count"] = len(state["evidence"])

    after = capability_intelligence.project(session)
    hypothesis = after["pressure_hypotheses"][0]
    corrected_target = next(
        item for item in after["observations"]
        if item["identity"] == verification["identity"]
    )
    correction_observation = next(
        item for item in after["observations"]
        if item["identity"] == correction["identity"]
    )

    assert after["source"]["record_corrections_applied_count"] == 1
    assert after["source"]["record_corrections_unverified_count"] == 0
    assert hypothesis["recurrence_status"] == "not-established"
    assert hypothesis["observation_count"] == 1
    assert hypothesis["evidence"][0]["identity"] == pressure["identity"]
    assert corrected_target["pressure_hypothesis_role"] == "supporting-context"
    assert corrected_target["pressure_hypothesis_role_status"] == (
        "corrected-by-verified-record"
    )
    assert corrected_target["record_correction"]["status"] == (
        "corrected-by-verified-record"
    )
    assert correction_observation["record_correction"]["status"] == (
        "applied-to-verified-target"
    )
    compact = capability_intelligence._compact_projection(
        after, 100_000, 24_000,
    )
    compact_target = next(
        item for item in compact["observations"]
        if item["identity"] == verification["identity"]
    )
    assert compact_target["pressure_hypothesis_role"] == "supporting-context"
    assert compact_target["record_correction"]["replacement_values"] == {
        "pressure_hypothesis_role": "supporting-context",
    }


def test_unclassified_pressure_evidence_does_not_establish_recurrence(
    tmp_path: Path,
) -> None:
    occurrence = _observation(
        "ev_environment_classified_occurrence",
        "ses_pressure_classified",
        "cmp_pressure_classification",
    )
    occurrence["pressure_hypothesis"]["identity"] = "ph_role_coverage"

    legacy = _observation(
        "ev_environment_legacy_unclassified",
        "ses_pressure_legacy",
        "cmp_pressure_classification",
    )
    legacy["pressure_hypothesis"]["identity"] = "ph_role_coverage"
    legacy.pop("pressure_hypothesis_role")

    context = _observation(
        "ev_environment_explicit_supporting_context",
        "ses_pressure_context",
        "cmp_pressure_classification",
    )
    context["pressure_hypothesis"]["identity"] = "ph_role_coverage"
    context["pressure_hypothesis_role"] = "supporting-context"
    context["observation"]["kind"] = "environmental-improvement-verification"
    session, _ = _session(tmp_path, [occurrence, legacy, context])

    result = capability_intelligence.project(session)
    hypothesis = result["pressure_hypotheses"][0]
    assert hypothesis["recurrence_status"] == "not-established"
    assert hypothesis["observation_count"] == 1
    assert hypothesis["supporting_context_observation_count"] == 1
    assert hypothesis["unclassified_observation_count"] == 1
    assert hypothesis["total_evidence_count"] == 3
    assert hypothesis["role_classification_status"] == "incomplete"
    assert hypothesis["session_attribution_status"] == "complete"
    assert hypothesis["distinct_session_count"] == 3
    assert hypothesis["occurrence_distinct_session_count"] == 1
    assert hypothesis["evidence"][0]["identity"] == occurrence["identity"]
    assert hypothesis["supporting_context_evidence"][0][
        "observation_identity"
    ] == context["identity"]
    assert hypothesis["supporting_context_evidence"][0]["observation_kind"] == (
        "environmental-improvement-verification"
    )
    assert hypothesis["unclassified_evidence"][0][
        "observation_identity"
    ] == legacy["identity"]
    assert hypothesis["unclassified_evidence"][0]["session_id"] == (
        "ses_pressure_legacy"
    )
    assert hypothesis["unclassified_evidence"][0]["evidence_reference"][
        "identity"
    ] == legacy["identity"]

    legacy_projection = next(
        item for item in result["observations"]
        if item["identity"] == legacy["identity"]
    )
    assert "pressure_hypothesis_role" not in legacy_projection
    assert legacy_projection["pressure_hypothesis_role_status"] == "not-recorded"
    assert legacy_projection[
        "pressure_hypothesis_recurrence_classification"
    ] == "unclassified"

    compact = capability_intelligence._compact_projection(
        result, 100_000, 24_000,
    )
    compact_hypothesis = compact["pressure_hypotheses"][0]
    assert compact_hypothesis["observation_count"] == 1
    assert compact_hypothesis["supporting_context_observation_count"] == 1
    assert compact_hypothesis["unclassified_observation_count"] == 1
    assert compact_hypothesis["role_classification_status"] == "incomplete"
    assert compact_hypothesis["unclassified_evidence"][0][
        "evidence_reference"
    ]["identity"] == legacy["identity"]
    compact_legacy = next(
        item for item in compact["observations"]
        if item["identity"] == legacy["identity"]
    )
    assert compact_legacy["pressure_hypothesis_role_status"] == "not-recorded"
    assert compact_legacy[
        "pressure_hypothesis_recurrence_classification"
    ] == "unclassified"

    linked_legacy = dict(legacy_projection)
    linked_legacy["improvement_relation_identity"] = "im_role_coverage"
    linked_summary = capability_intelligence._recurrence([linked_legacy])[0]
    assert linked_summary["observation_count"] == 0
    assert linked_summary["unclassified_observation_count"] == 1
    assert linked_summary["supporting_context_observation_count"] == 0
    assert linked_summary["role_classification_status"] == "incomplete"


def test_recurrence_requires_complete_session_attribution_within_one_session() -> None:
    def occurrence(identity: str, session_id: str | None) -> dict:
        related_work = {"campaign_id": "cmp_session_attribution"}
        if session_id is not None:
            related_work["session_id"] = session_id
        return {
            "identity": identity,
            "pressure_hypothesis": {"identity": "ph_session_attribution"},
            "pressure_hypothesis_role": "occurrence",
            "related_work": related_work,
            "recording_context": {},
            "observation": {"kind": "project-campaign-state-read"},
            "evidence_reference": {"identity": identity},
        }

    hypothesis = capability_intelligence._recurrence([
        occurrence("ev_session_attribution_one", "ses_attribution"),
        occurrence("ev_session_attribution_missing", None),
    ])[0]

    assert hypothesis["observation_count"] == 2
    assert hypothesis["distinct_session_count"] == 1
    assert hypothesis["occurrence_distinct_session_count"] == 1
    assert hypothesis["session_attribution_status"] == "incomplete"
    assert hypothesis["recurrence_status"] == "not-established"


def test_observation_correction_accepts_integrity_verified_read_only_source(
    tmp_path: Path,
) -> None:
    target = _observation(
        "ev_environment_read_only_correction_target",
        "ses_environment_read_only_correction",
        "cmp_environment_read_only_correction",
    )
    session, state = _session(tmp_path, [target])
    checkout = Path(session.snapshot["workspace"]["root"]) / "mncs-test"
    checkout.mkdir(parents=True)
    _git(checkout, "init", "--quiet")
    _git(checkout, "config", "user.name", "test")
    _git(checkout, "config", "user.email", "test@example.invalid")
    source_path = checkout / "pressures/MNCS-TEST-P-001.json"
    source_path.parent.mkdir(parents=True)
    source_bytes = json.dumps({
        "schema_version": "mncs-test.pressure/1",
        "id": "MNCS-TEST-P-001",
        "commons_pressure_id": "MNCS-TOOLING-B665F138D324",
    }, sort_keys=True, separators=(",", ":")).encode()
    source_path.write_bytes(source_bytes)
    _git(checkout, "add", "pressures/MNCS-TEST-P-001.json")
    _git(checkout, "commit", "--quiet", "-m", "pressure source")
    source = {
        "identity": "MNCS-TEST-P-001",
        "repository": "mncs-test",
        "commit": _git(checkout, "rev-parse", "HEAD"),
        "path": "pressures/MNCS-TEST-P-001.json",
        "sha256": hashlib.sha256(source_bytes).hexdigest(),
        "kind": "repository-owner-runtime-and-tooling-pressure",
    }
    state["evidence"].append({key: source[key] for key in (
        "identity", "repository", "commit", "path", "sha256",
    )})
    state["evidence_count"] = len(state["evidence"])
    _register_workspace_source(
        session, checkout, source, repository="mncs-test",
        include_source_record=True,
    )

    correction = _observation(
        "ev_environment_read_only_correction",
        "ses_environment_read_only_correction",
        "cmp_environment_read_only_correction",
    )
    _drop_pressure_hypothesis(correction)
    correction["supporting_sources"] = [source]
    correction["record_correction"] = {
        "schema_version": capability_intelligence.OBSERVATION_CORRECTION_SCHEMA,
        "target_reference": dict(state["evidence"][0]),
        "corrected_field_paths": ["supporting_sources"],
        "replacement_values": {"supporting_sources": [source]},
        "reason": "Link the verified workspace-read-only MNCS-Test pressure source.",
    }
    relative = "evidence/environmental-capability/read-only-source-correction.json"
    raw = json.dumps(correction, sort_keys=True, separators=(",", ":")).encode()
    correction_path = Path(session.snapshot["selected_checkouts"][
        "mncs-environment"
    ]["path"]) / relative
    correction_path.parent.mkdir(parents=True, exist_ok=True)
    correction_path.write_bytes(raw)
    _git(correction_path.parents[2], "add", relative)
    _git(correction_path.parents[2], "commit", "--quiet", "-m", "correction record")
    state["evidence"].append({
        "identity": correction["identity"],
        "repository": "mncs-environment",
        "commit": _git(correction_path.parents[2], "rev-parse", "HEAD"),
        "path": relative,
        "sha256": hashlib.sha256(raw).hexdigest(),
    })
    state["evidence_count"] = len(state["evidence"])

    result = capability_intelligence.project(session)

    assert result["source"]["record_corrections_applied_count"] == 1
    assert result["source"]["record_corrections_unverified_count"] == 0
    corrected = next(
        item for item in result["observations"]
        if item["identity"] == "ev_environment_read_only_correction_target"
    )
    assert corrected["record_correction"]["status"] == (
        "corrected-by-verified-record"
    )
    assert corrected["supporting_sources"][0]["status"] == (
        "outside-selected-session"
    )
    assert corrected["supporting_sources"][0]["content_integrity_status"] == (
        "workspace-read-only-content-integrity-verified"
    )
    assert session.snapshot["authority"]["writable"] == ["mncs-environment"]
    assert _git(checkout, "status", "--porcelain") == ""


def test_observation_correction_accepts_verified_source_outside_campaign_page(
    tmp_path: Path,
) -> None:
    target = _observation(
        "ev_environment_page_local_correction_target",
        "ses_environment_page_local_correction",
        "cmp_environment_page_local_correction",
    )
    session, state = _session(tmp_path, [target])
    checkout = Path(session.snapshot["workspace"]["root"]) / "mncs-test"
    checkout.mkdir(parents=True)
    _git(checkout, "init", "--quiet")
    _git(checkout, "config", "user.name", "test")
    _git(checkout, "config", "user.email", "test@example.invalid")
    source_path = checkout / "pressures/MNCS-TEST-P-001.json"
    source_path.parent.mkdir(parents=True)
    source_bytes = json.dumps({
        "schema_version": "mncs-test.pressure/1",
        "id": "MNCS-TEST-P-001",
        "commons_pressure_id": "MNCS-TOOLING-B665F138D324",
    }, sort_keys=True, separators=(",", ":")).encode()
    source_path.write_bytes(source_bytes)
    _git(checkout, "add", "pressures/MNCS-TEST-P-001.json")
    _git(checkout, "commit", "--quiet", "-m", "pressure source")
    source = {
        "identity": "MNCS-TEST-P-001",
        "repository": "mncs-test",
        "commit": _git(checkout, "rev-parse", "HEAD"),
        "path": "pressures/MNCS-TEST-P-001.json",
        "sha256": hashlib.sha256(source_bytes).hexdigest(),
        "kind": "repository-owner-runtime-and-tooling-pressure",
    }
    _register_workspace_source(
        session, checkout, source, repository="mncs-test",
        include_source_record=True,
    )

    correction = _observation(
        "ev_environment_page_local_correction",
        "ses_environment_page_local_correction",
        "cmp_environment_page_local_correction",
    )
    _drop_pressure_hypothesis(correction)
    correction["supporting_sources"] = [source]
    correction["record_correction"] = {
        "schema_version": capability_intelligence.OBSERVATION_CORRECTION_SCHEMA,
        "target_reference": dict(state["evidence"][0]),
        "corrected_field_paths": ["supporting_sources"],
        "replacement_values": {"supporting_sources": [source]},
        "reason": (
            "Link a source whose blob is verified in a selected read-only "
            "checkout, even though this campaign page does not register it."
        ),
    }
    relative = "evidence/environmental-capability/page-local-correction.json"
    raw = json.dumps(correction, sort_keys=True, separators=(",", ":")).encode()
    correction_path = Path(session.snapshot["selected_checkouts"][
        "mncs-environment"
    ]["path"]) / relative
    correction_path.parent.mkdir(parents=True, exist_ok=True)
    correction_path.write_bytes(raw)
    _git(correction_path.parents[2], "add", relative)
    _git(correction_path.parents[2], "commit", "--quiet", "-m", "correction record")
    state["evidence"].append({
        "identity": correction["identity"],
        "repository": "mncs-environment",
        "commit": _git(correction_path.parents[2], "rev-parse", "HEAD"),
        "path": relative,
        "sha256": hashlib.sha256(raw).hexdigest(),
    })
    state["evidence_count"] = len(state["evidence"])

    result = capability_intelligence.project(session)

    assert result["source"]["record_corrections_applied_count"] == 1
    assert result["source"]["record_corrections_unverified_count"] == 0
    corrected = next(
        item for item in result["observations"]
        if item["identity"] == target["identity"]
    )
    assert corrected["record_correction"]["status"] == (
        "corrected-by-verified-record"
    )
    assert corrected["supporting_sources"][0]["status"] == (
        "not-present-in-selected-campaign-evidence-window"
    )
    assert corrected["supporting_sources"][0]["content_integrity_status"] == (
        "workspace-read-only-content-integrity-verified"
    )
    assert session.snapshot["authority"]["writable"] == ["mncs-environment"]
    assert _git(checkout, "status", "--porcelain") == ""


def test_append_only_evidence_reference_correction_recovers_exact_record(
    tmp_path: Path,
) -> None:
    def project_pointer_correction(root: Path, *, corrupt_target_digest: bool):
        target = _observation(
            "ev_environment_pointer_correctable",
            "ses_environment_pointer_correction",
            "cmp_environment_pointer_correction",
        )
        session, state = _session(root, [target])
        original_reference = dict(state["evidence"][0])
        unavailable_reference = {**original_reference, "commit": "f" * 40}
        state["evidence"][0] = unavailable_reference

        target_reference = dict(unavailable_reference)
        replacement_reference = dict(original_reference)
        if corrupt_target_digest:
            target_reference["sha256"] = "e" * 64
            replacement_reference["sha256"] = "e" * 64

        correction = _observation(
            "ev_environment_pointer_correction",
            "ses_environment_pointer_correction",
            "cmp_environment_pointer_correction",
        )
        _drop_pressure_hypothesis(correction)
        correction["observation"]["kind"] = "campaign-evidence-pointer-correction"
        correction["evidence_reference_correction"] = {
            "schema_version": (
                capability_intelligence.EVIDENCE_REFERENCE_CORRECTION_SCHEMA
            ),
            "target_reference": target_reference,
            "replacement_reference": replacement_reference,
            "reason": "Correct the recorded commit while preserving exact observation bytes.",
        }
        relative = "evidence/environmental-capability/pointer-correction.json"
        raw = json.dumps(
            correction, sort_keys=True, separators=(",", ":"),
        ).encode()
        checkout = Path(session.snapshot["selected_checkouts"][
            "mncs-environment"
        ]["path"])
        correction_path = checkout / relative
        correction_path.write_bytes(raw)
        _git(checkout, "add", relative)
        _git(checkout, "commit", "--quiet", "-m", "append-only pointer correction")
        state["evidence"].append({
            "identity": correction["identity"],
            "repository": "mncs-environment",
            "commit": _git(checkout, "rev-parse", "HEAD"),
            "path": relative,
            "sha256": hashlib.sha256(raw).hexdigest(),
        })
        state["evidence_count"] = len(state["evidence"])
        return capability_intelligence.project(session)

    recovered = project_pointer_correction(
        tmp_path / "recovered", corrupt_target_digest=False,
    )
    assert recovered["status"] == "ready"
    assert recovered["source"]["observation_records_unavailable"] == 0
    assert recovered["source"]["evidence_reference_correction_count"] == 1
    assert recovered["source"]["evidence_reference_corrections_applied_count"] == 1
    assert recovered["source"]["evidence_reference_corrections_unverified_count"] == 0
    corrected_target = next(
        item for item in recovered["observations"]
        if item["identity"] == "ev_environment_pointer_correctable"
    )
    assert corrected_target["evidence_reference"]["commit"] != "f" * 40
    assert corrected_target["evidence_reference_correction"]["status"] == (
        "recovered-from-append-only-correction"
    )
    assert corrected_target["evidence_reference_correction"][
        "recorded_reference"
    ]["commit"] == "f" * 40
    correction_view = next(
        item for item in recovered["observations"]
        if item["identity"] == "ev_environment_pointer_correction"
    )
    assert correction_view["evidence_reference_correction"]["status"] == (
        "applied-to-exact-unavailable-target"
    )
    compact = capability_intelligence._compact_projection(
        recovered, 100_000, 24_000,
    )
    compact_target = next(
        item for item in compact["observations"]
        if item["identity"] == "ev_environment_pointer_correctable"
    )
    assert compact_target["evidence_reference_correction"]["status"] == (
        "recovered-from-append-only-correction"
    )
    assert compact["source"]["evidence_reference_corrections_applied_count"] == 1

    mismatched = project_pointer_correction(
        tmp_path / "mismatched", corrupt_target_digest=True,
    )
    assert mismatched["status"] == "partial"
    assert mismatched["source"]["observation_records_unavailable"] == 1
    assert mismatched["source"]["evidence_reference_corrections_applied_count"] == 0
    assert mismatched["source"]["evidence_reference_corrections_unverified_count"] == 1
    mismatched_correction = next(
        item for item in mismatched["observations"]
        if item["identity"] == "ev_environment_pointer_correction"
    )
    assert mismatched_correction["evidence_reference_correction"]["status"] == (
        "target-reference-mismatch"
    )


def test_append_only_evidence_reference_correction_crosses_history_pages(
    tmp_path: Path,
) -> None:
    target = _observation(
        "ev_environment_cross_page_pointer_correctable",
        "ses_environment_pointer_correction",
        "cmp_environment_pointer_correction",
    )
    session, _state = _session(tmp_path, [target])
    target_reference = dict(_state["evidence"][0])
    unavailable_reference = {**target_reference, "commit": "f" * 40}

    correction = _observation(
        "ev_environment_cross_page_pointer_correction",
        "ses_environment_pointer_correction",
        "cmp_environment_pointer_correction",
    )
    _drop_pressure_hypothesis(correction)
    correction["observation"]["kind"] = "campaign-evidence-pointer-correction"
    correction["evidence_reference_correction"] = {
        "schema_version": (
            capability_intelligence.EVIDENCE_REFERENCE_CORRECTION_SCHEMA
        ),
        "target_reference": unavailable_reference,
        "replacement_reference": target_reference,
        "reason": "Correct the commit pointer while preserving exact observation bytes.",
    }
    relative = "evidence/environmental-capability/cross-page-pointer-correction.json"
    raw = json.dumps(correction, sort_keys=True, separators=(",", ":")).encode()
    checkout = Path(session.snapshot["selected_checkouts"][
        "mncs-environment"
    ]["path"])
    correction_path = checkout / relative
    correction_path.write_bytes(raw)
    _git(checkout, "add", relative)
    _git(checkout, "commit", "--quiet", "-m", "append cross-page pointer correction")
    correction_reference = {
        "identity": correction["identity"],
        "repository": "mncs-environment",
        "commit": _git(checkout, "rev-parse", "HEAD"),
        "path": relative,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }

    def campaign_state_page(*, evidence_offset: int, evidence_limit: int):
        page = {
            "checkpoint_id": "chk_cross_page_pointer_correction",
            "recorded_at": "2026-10-24T00:00:00+00:00",
            "recorded_by": {"consumer_id": "recorder", "principal_id": "ctrl_test"},
            "evidence_count": 2,
            "evidence_truncated": False,
            "delivery": {"status": "pending"},
            "evidence_offset": evidence_offset,
            "evidence_limit": evidence_limit,
        }
        if evidence_offset == 1:
            page.update({
                "evidence": [correction_reference],
                "previous_evidence_offset": 0,
                "next_evidence_offset": None,
            })
        else:
            page.update({
                "evidence": [unavailable_reference],
                "previous_evidence_offset": None,
                "next_evidence_offset": 1,
            })
        return page

    session.campaign_state = campaign_state_page
    result = capability_intelligence.project(
        session, history_pages=2, evidence_offset=1, evidence_limit=1,
    )

    assert result["source"]["evidence_history_page_offsets"] == [1, 0]
    assert result["source"]["evidence_reference_correction_count"] == 1
    assert result["source"]["evidence_reference_corrections_applied_count"] == 1
    assert result["source"]["evidence_reference_corrections_unverified_count"] == 0
    assert result["source"]["observation_records_unavailable"] == 0
    recovered = next(
        item for item in result["observations"]
        if item["identity"] == target["identity"]
    )
    assert recovered["evidence_reference"]["commit"] == target_reference["commit"]
    assert recovered["evidence_reference_correction"]["status"] == (
        "recovered-from-append-only-correction"
    )
    correction_view = next(
        item for item in result["observations"]
        if item["identity"] == correction["identity"]
    )
    assert correction_view["evidence_reference_correction"]["status"] == (
        "applied-to-exact-unavailable-target"
    )


def test_compact_projection_retains_newest_observation_tail(tmp_path: Path) -> None:
    identities = [f"ev_environment_compact_tail_{index:02d}" for index in range(10)]
    record = _observation(
        identities[0], "ses_compact_tail", "cmp_compact_tail",
    )
    session, _state = _session(tmp_path, [record])

    full = capability_intelligence.project(
        session, max_result_bytes=capability_intelligence.MAX_RESULT_BYTES,
    )
    base_observation = full["observations"][0]
    full["observations"] = [
        {
            **base_observation,
            "identity": identity,
            "evidence_reference": {
                **base_observation["evidence_reference"],
                "identity": identity,
                "path": f"evidence/environmental-capability/compact-tail-{index}.json",
            },
        }
        for index, identity in enumerate(identities)
    ]
    full["observations"][7]["resource_sample_relation"] = {
        "sample_identity": "rsm_compact_priority",
        "status": "matched-exact-resource-sample-and-session",
    }
    full["observations"][7]["observation"]["outcome"][
        "event_reference"
    ] = {
        "identity": "evt_compact_observation_source",
        "sequence": 7,
        "type": "authority.integration-eligible",
    }
    full["observation_count"] = len(identities)
    full["source"]["observation_records_considered"] = len(identities)
    full["source"]["observation_records_verified"] = len(identities)
    # Multi-page reads visit the newest page before older pages. Compacting
    # must still rank records by their absolute ledger offsets.
    full["observations"] = full["observations"][5:] + full["observations"][:5]
    full["source"]["evidence_history_observation_budgets_by_page"] = [
        {
            "offset": 5,
            "selected_observation_reference_identities": identities[5:],
        },
        {
            "offset": 0,
            "selected_observation_reference_identities": identities[:5],
        },
    ]
    compact = capability_intelligence._compact_projection(
        full, 100_000, 100_000,
    )
    compact_identities = [item["identity"] for item in compact["observations"]]

    assert compact_identities == identities[-8:]
    assert compact["source"]["observation_records_compacted_out_count"] == 2
    compact_priority_record = next(
        item for item in compact["observations"]
        if item["identity"] == identities[7]
    )
    assert compact_priority_record["observation"]["event_reference"] == {
        "identity": "evt_compact_observation_source",
        "sequence": 7,
        "type": "authority.integration-eligible",
    }

    compact_bytes = len(json.dumps(compact, ensure_ascii=False, sort_keys=True).encode())
    tight = capability_intelligence._compact_projection(
        full, 100_000, max(1, compact_bytes - 2_000),
    )
    tight_identities = [item["identity"] for item in tight["observations"]]

    assert identities[7] in tight_identities
    assert next(
        item for item in tight["observations"]
        if item["identity"] == identities[7]
    )["resource_sample_relation"]["status"] == (
        "matched-exact-resource-sample-and-session"
    )
    assert next(
        item for item in tight["observations"]
        if item["identity"] == identities[7]
    )["observation"]["event_reference"] == {
        "identity": "evt_compact_observation_source",
        "sequence": 7,
        "type": "authority.integration-eligible",
    }
    assert tight["source"]["observation_records_compacted_out_count"] == (
        len(identities) - len(tight_identities)
    )


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


def test_project_ignores_non_charter_readback_path_in_legacy_charter_index(
    tmp_path: Path,
) -> None:
    record = _observation(
        "ev_environment_campaign_charter_legacy_index",
        "ses_charter_legacy_index",
        "cmp_projection_reader",
    )
    session, state = _session(tmp_path, [record])
    objective = "Preserve one verified campaign objective."
    pointer = _add_campaign_charter(
        session, state, "ev_environment_campaign_charter_real", objective,
    )
    decoy = {
        "identity": "ev_environment_campaign_charter_projection_readback",
        "repository": "mncs-environment",
        "commit": "f" * 40,
        "path": "evidence/environmental-capability/campaign-charter-projection-readback.json",
        "sha256": "e" * 64,
    }
    state["campaign_charter_evidence"]["references"].append(decoy)
    state["campaign_charter_evidence"]["count"] = 2

    charter = capability_intelligence.project(session)["subject"]["campaign_charter"]

    assert charter["status"] == "verified"
    assert charter["identity"] == pointer["identity"]
    assert charter["objective"] == objective
    assert charter["candidate_reference_count"] == 2
    assert charter["references_examined"] == 2


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
    session.snapshot["bindings"] = [
        {
            "binding_id": "cap_compiler_build",
            "provider": "mncs-compiler",
            "capability": "mncs-compiler:build",
            "availability": {
                "status": "available", "verification": "substrate",
                "observed_at": "2026-10-24T01:00:00Z",
            },
        },
        {
            "binding_id": "cap_compiler_inspect",
            "provider": "mncs-compiler",
            "capability": "mncs-compiler:inspect",
            "availability": {
                "status": "unavailable", "verification": "substrate",
                "observed_at": "2026-10-24T01:00:00Z",
            },
        },
    ]
    request_id = "req_compiler_activity"
    events = [
        {
            "identity": "evt_capability_available_1", "sequence": 10,
            "type": "capability.available", "producer": "environment",
            "observed_at": "2026-10-24T00:59:59Z",
            "payload": {
                "capability": "mncs-compiler:build",
                "provider": "mncs-compiler", "previous": "unknown",
            },
        },
        {
            "identity": "evt_capability_invoked_1", "sequence": 11,
            "type": "capability.invoked", "producer": "local-agent",
            "observed_at": "2026-10-24T01:00:00Z",
            "payload": {
                "capability": "mncs-compiler:build",
                "provider_request_id": request_id,
                "argv": ["build", "private-source-path"],
                "effect_target": {
                    "kind": "worktree",
                    "repository": "mncs-compiler",
                    "checkout": "/private/selected-checkout",
                    "branch": "campaign/compiler",
                    "checkout_identity": "checkout:campaign",
                    "operation_lease": {
                        "identity": "op_fixture",
                        "kind": "checkout-directory-flock",
                        "scope": "one-provider-invocation",
                        "status": "held",
                        "waited_ms": 11,
                        "releases_on": "result-publication-or-process-exit",
                        "checkout_identity": "checkout:campaign",
                    },
                    "preflight_event": {
                        "identity": "evt_authority_proposal_1",
                        "sequence": 9,
                        "type": "authority.proposal-eligible",
                    },
                },
            },
        },
        {
            "identity": "evt_invocation_completed_1", "sequence": 12,
            "type": "invocation.completed", "producer": "mncs-compiler",
            "observed_at": "2026-10-24T01:00:02Z",
            "payload": {
                "capability": "mncs-compiler:build", "status": "failed",
                "returncode": 2, "provider_request_id": request_id,
                "effect_target": {
                    "operation_lease": {
                        "identity": "op_fixture",
                        "kind": "checkout-directory-flock",
                        "scope": "one-provider-invocation",
                        "status": "released",
                        "waited_ms": 11,
                        "held_ms": 201,
                        "checkout_identity": "checkout:campaign",
                    },
                },
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
        {
            "identity": "evt_effect_operation_deferred_1", "sequence": 14,
            "type": "effect.operation-deferred", "producer": "environment",
            "observed_at": "2026-10-24T01:02:00Z",
            "payload": {
                "action": "write",
                "repository": "mncs-compiler",
                "branch": "campaign/compiler",
                "checkout_identity": "checkout:campaign",
                "waited_ms": 15000,
                "operation_executed": False,
                "reason": "same-checkout-operation-in-progress",
                "next": "retry or select an isolated worktree",
            },
        },
        {
            "identity": "evt_effect_preflight_stale_1", "sequence": 15,
            "type": "effect.operation-deferred", "producer": "environment",
            "observed_at": "2026-10-24T01:03:00Z",
            "payload": {
                "action": "write",
                "repository": "mncs-compiler",
                "branch": "campaign/compiler",
                "checkout_identity": "checkout:campaign",
                "waited_ms": 23,
                "operation_executed": False,
                "reason": "checkout-state-changed-since-preflight",
                "changed_fields": ["head"],
                "initial_preflight_event": {
                    "identity": "evt_proposal_preflight_initial",
                    "sequence": 8,
                    "type": "authority.proposal-eligible",
                },
                "locked_preflight_event": {
                    "identity": "evt_proposal_preflight_locked",
                    "sequence": 13,
                    "type": "authority.proposal-eligible",
                },
                "next": "refresh the proposal against the current checkout state",
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
    assert prior["effect_target"] == {
        "status": "recorded",
        "repository": "mncs-compiler",
        "branch_status": "recorded",
        "branch": "campaign/compiler",
        "checkout_identity": "checkout:campaign",
        "scope_kind": "worktree",
        "preflight_event": {
            "identity": "evt_authority_proposal_1",
            "sequence": 9,
            "type": "authority.proposal-eligible",
        },
        "operation_lease": {
            "identity": "op_fixture",
            "kind": "checkout-directory-flock",
            "scope": "one-provider-invocation",
            "acquisition_status": "recorded",
            "release_status": "released-after-completion",
            "checkout_identity": "checkout:campaign",
            "waited_ms": 11,
            "held_ms": 201,
        },
    }
    assert prior["invocation_event"]["identity"] == "evt_capability_invoked_1"
    assert prior["completion_event"]["identity"] == "evt_invocation_completed_1"
    assert activity["operation_deferral_count"] == 2
    stale_deferral, busy_deferral = activity["operation_deferrals"]
    assert stale_deferral["reason"] == "checkout-state-changed-since-preflight"
    assert stale_deferral["changed_fields"] == ["head"]
    assert stale_deferral["initial_preflight_event"]["identity"] == (
        "evt_proposal_preflight_initial"
    )
    assert stale_deferral["locked_preflight_event"]["identity"] == (
        "evt_proposal_preflight_locked"
    )
    assert stale_deferral["operation_executed"] is False
    assert stale_deferral["event_reference"]["identity"] == (
        "evt_effect_preflight_stale_1"
    )
    assert busy_deferral["identity"].startswith("opdef_")
    assert busy_deferral["action"] == "write"
    assert busy_deferral["repository"] == "mncs-compiler"
    assert busy_deferral["reason"] == "same-checkout-operation-in-progress"
    assert busy_deferral["waited_ms"] == 15000
    assert busy_deferral["operation_executed"] is False
    assert busy_deferral["event_reference"]["identity"] == (
        "evt_effect_operation_deferred_1"
    )
    assert busy_deferral["checkout_identity"] == "checkout:campaign"
    assert activity["capabilities"] == [{
        "capability": "mncs-compiler:build",
        "invocation_count": 2,
        "completion_count": 1,
        "outcome_status_counts": {"failed": 1},
        "supporting_events": [
            {"identity": "evt_capability_invoked_2", "sequence": 13,
             "type": "capability.invoked",
             "observed_at": "2026-10-24T01:01:00Z"},
            {"identity": "evt_capability_invoked_1", "sequence": 11,
             "type": "capability.invoked",
             "observed_at": "2026-10-24T01:00:00Z"},
        ],
        "binding_identity_attribution": {
            "status": "not-recorded",
            "complete_identity_count": 0,
            "identity_unavailable_count": 2,
            "basis": (
                "provider-binding-id-contract-revision-on-capability-invoked-event"
            ),
        },
    }]
    delivery = activity["capability_delivery"]
    assert delivery["status"] == "ready"
    assert delivery["binding_count"] == 2
    assert delivery["availability_counts"] == {"available": 1, "unavailable": 1}
    assert delivery["discoverability_status"] == "not-measured"
    build = next(
        item for item in delivery["bindings"]
        if item["capability"] == "mncs-compiler:build"
    )
    assert build["availability_status"] == "available"
    assert build["use_observation_status"] == "invoked-in-selected-event-window"
    assert build["invocation_count"] == 2
    assert build["outcome_status_counts"] == {"failed": 1}
    assert build["provider_attribution"] == (
        "capability-name-match-only; provider-and-binding-identity-unavailable"
    )
    assert build["supporting_events"][0]["identity"] == "evt_capability_invoked_2"
    assert build["availability_event_references"][0]["identity"] == (
        "evt_capability_available_1"
    )
    assert delivery["availability_transition_events"][0]["event_reference"][
        "identity"
    ] == "evt_capability_available_1"
    inspect = next(
        item for item in delivery["bindings"]
        if item["capability"] == "mncs-compiler:inspect"
    )
    assert inspect["availability_status"] == "unavailable"
    assert inspect["use_observation_status"] == (
        "no-environment-invocation-record-in-session-history"
    )
    compact = capability_intelligence._compact_projection(
        result, 100_000, capability_intelligence.MAX_RESULT_BYTES,
    )
    assert compact["session_activity"]["capabilities"][0]["supporting_events"][0][
        "identity"
    ] == "evt_capability_invoked_2"
    assert compact["session_activity"]["interactions"][1]["effect_target"] == {
        "status": "recorded",
        "repository": "mncs-compiler",
        "branch_status": "recorded",
        "branch": "campaign/compiler",
        "checkout_identity": "checkout:campaign",
        "scope_kind": "worktree",
        "preflight_event": {
            "identity": "evt_authority_proposal_1",
            "sequence": 9,
            "type": "authority.proposal-eligible",
        },
        "operation_lease": {
            "identity": "op_fixture",
            "kind": "checkout-directory-flock",
            "scope": "one-provider-invocation",
            "acquisition_status": "recorded",
            "release_status": "released-after-completion",
            "checkout_identity": "checkout:campaign",
            "waited_ms": 11,
            "held_ms": 201,
        },
    }
    compact_stale_deferral = next(
        item for item in compact["session_activity"]["operation_deferrals"]
        if item["reason"] == "checkout-state-changed-since-preflight"
    )
    assert compact_stale_deferral["changed_fields"] == ["head"]
    assert "/private" not in json.dumps(
        compact["session_activity"]["operation_deferrals"],
    )
    assert compact["session_activity"]["capability_delivery"][
        "discoverability_status"
    ] == "not-measured"
    compact_build = next(
        item for item in compact["session_activity"]["capability_delivery"]["bindings"]
        if item["capability"] == "mncs-compiler:build"
    )
    assert compact_build["availability_event_references"][0]["identity"] == (
        "evt_capability_available_1"
    )
    assert "private-source-path" not in json.dumps(activity)
    assert "/private/selected-checkout" not in json.dumps(activity)
    assert any("token usage" in item for item in activity["measurement_limits"])


def test_capability_use_is_attributed_to_the_exact_historical_binding(
    tmp_path: Path,
) -> None:
    record = _observation(
        "ev_environment_exact_capability_use",
        "ses_exact_capability_use",
        "cmp_exact_capability_use",
    )
    session, _ = _session(tmp_path, [record])
    capability = "mncs-native:process-run"
    session.snapshot["bindings"] = [
        {
            "binding_id": "cap_process_provider_one",
            "provider": "mncs-process-provider-one",
            "capability": capability,
            "contract_revision": "2",
            "availability": {"status": "available"},
        },
        {
            "binding_id": "cap_process_provider_two",
            "provider": "mncs-process-provider-two",
            "capability": capability,
            "contract_revision": "3",
            "availability": {"status": "available"},
        },
    ]
    request_id = "req_exact_process_binding"
    events = [
        {
            "identity": "evt_exact_process_invoked", "sequence": 1,
            "type": "capability.invoked", "producer": "local-agent",
            "observed_at": "2026-10-25T01:00:00Z",
            "payload": {
                "capability": capability,
                "provider": "mncs-process-provider-one",
                "binding_id": "cap_process_provider_one",
                "contract_revision": "2",
                "provider_request_id": request_id,
            },
        },
        {
            "identity": "evt_exact_process_completed", "sequence": 2,
            "type": "invocation.completed", "producer": "mncs-process-provider-one",
            "observed_at": "2026-10-25T01:00:01Z",
            "payload": {
                "capability": capability,
                "provider": "mncs-process-provider-one",
                "binding_id": "cap_process_provider_one",
                "contract_revision": "2",
                "provider_request_id": request_id,
                "status": "ok",
                "returncode": 0,
            },
        },
    ]
    session.recent_events = lambda *, limit: {
        "event_count": len(events), "events": events[-limit:], "truncated": False,
    }

    activity = capability_intelligence.project(session)["session_activity"]

    interaction = activity["interactions"][0]
    assert interaction["binding_identity"] == {
        "status": "complete",
        "provider": "mncs-process-provider-one",
        "binding_id": "cap_process_provider_one",
        "contract_revision": "2",
    }
    capability_summary = activity["capabilities"][0]
    assert capability_summary["binding_identity_attribution"] == {
        "status": "recorded-for-all-invocations",
        "complete_identity_count": 1,
        "identity_unavailable_count": 0,
        "basis": "provider-binding-id-contract-revision-on-capability-invoked-event",
    }
    delivery = activity["capability_delivery"]
    provider_one = next(
        item for item in delivery["bindings"]
        if item["provider"] == "mncs-process-provider-one"
    )
    provider_two = next(
        item for item in delivery["bindings"]
        if item["provider"] == "mncs-process-provider-two"
    )
    assert provider_one["contract_revision"] == "2"
    assert provider_one["binding_attribution_status"] == (
        "exact-session-binding-observed-in-selected-window"
    )
    assert provider_one["invocation_count_basis"] == (
        "exact-session-binding-identity"
    )
    assert provider_one["invocation_count"] == 1
    assert provider_one["provider_attribution"] == (
        "exact-provider-binding-id-and-contract-revision"
    )
    assert provider_one["supporting_events"][0]["identity"] == (
        "evt_exact_process_invoked"
    )
    assert provider_two["binding_attribution_status"] == (
        "exact-invocations-do-not-match-current-binding"
    )
    assert provider_two["use_observation_status"] == (
        "capability-invoked; no-exact-invocation-for-this-binding"
    )
    assert provider_two["invocation_count"] == 0
    assert provider_two["supporting_events"] == []
    evidence_summary = delivery["binding_evidence_summary"]
    assert evidence_summary["schema_version"] == (
        "mncs.environment.capability-binding-evidence-summary/2"
    )
    assert evidence_summary["binding_count"] == 2
    assert evidence_summary["context_response_inclusion_status_counts"] == {
        "not-observed-in-session-history": 2,
    }
    assert evidence_summary["use_observation_status_counts"] == {
        "capability-invoked; no-exact-invocation-for-this-binding": 1,
        "invoked-for-exact-session-binding": 1,
    }
    assert evidence_summary["exact_binding_invoked_binding_count"] == 1
    assert evidence_summary["exact_binding_invocation_count"] == 1
    assert evidence_summary["exact_binding_completion_count"] == 1
    assert evidence_summary["discoverability_status"] == "not-measured"
    assert evidence_summary["agent_receipt_status"] == "not-established"

    compact = capability_intelligence._compact_projection(
        capability_intelligence.project(session),
        100_000,
        capability_intelligence.MAX_RESULT_BYTES,
    )
    compact_interaction = compact["session_activity"]["interactions"][0]
    assert compact_interaction["binding_identity"]["binding_id"] == (
        "cap_process_provider_one"
    )
    compact_bindings = compact["session_activity"]["capability_delivery"][
        "bindings"
    ]
    compact_provider_one = next(
        item for item in compact_bindings
        if item["provider"] == "mncs-process-provider-one"
    )
    assert compact_provider_one["binding_attribution_status"] == (
        "exact-session-binding-observed-in-selected-window"
    )
    compact_summary = compact["session_activity"]["capability_delivery"][
        "binding_evidence_summary"
    ]
    assert compact_summary["exact_binding_invocation_count"] == 1
    assert compact_summary["binding_details_truncated"] is False

    events[1]["payload"]["binding_id"] = "cap_wrong_provider"
    inconsistent = capability_intelligence.project(session)["session_activity"]
    inconsistent_interaction = inconsistent["interactions"][0]
    assert inconsistent_interaction["correlation_status"] == (
        "provider-request-id-binding-identity-mismatch"
    )
    assert inconsistent_interaction["completion_event"] is None
    assert inconsistent_interaction["outcome_status"] is None


def test_capability_binding_summary_survives_response_budget_trimming(
    tmp_path: Path,
) -> None:
    assert capability_intelligence._compact_count_map({
        "invalid": "not-a-count", "valid": 1,
    }) == {"valid": 1}
    record = _observation(
        "ev_environment_binding_summary_budget",
        "ses_binding_summary_budget",
        "cmp_binding_summary_budget",
    )
    session, _state = _session(tmp_path, [record])
    session.snapshot["bindings"] = []
    session.recent_events = lambda *, limit: {
        "event_count": 0, "events": [], "truncated": False,
    }
    projection = capability_intelligence.project(session)
    delivery = projection["session_activity"]["capability_delivery"]

    bindings = []
    for index in range(9):
        exact_use = index == 0
        references = [
            {
                "identity": f"evt_{index}_{event_index}_" + "x" * 320,
                "sequence": event_index,
                "type": "capability.invoked",
            }
            for event_index in range(3)
        ]
        bindings.append({
            "provider": f"provider-{index}",
            "capability": f"mncs-capability-{index}",
            "availability_status": "available",
            "binding_id": f"cap_binding_{index}",
            "contract_revision": "1",
            "use_observation_status": (
                "invoked-for-exact-session-binding" if exact_use else
                "no-environment-invocation-record-in-session-history"
            ),
            "binding_attribution_status": (
                "exact-session-binding-observed-in-selected-window" if exact_use else
                "no-exact-binding-use-in-session-history"
            ),
            "invocation_count_basis": (
                "exact-session-binding-identity" if exact_use else
                "no-invocation-record"
            ),
            "invocation_count": 2 if exact_use else 0,
            "completion_count": 1 if exact_use else 0,
            "outcome_status_counts": {"succeeded": 1} if exact_use else {},
            "supporting_events": references if exact_use else [],
            "context_response_inclusion": {
                "status": (
                    "exact-binding-id-in-response" if exact_use else
                    "identity-tuple-in-matched-response"
                ),
                "binding_id_attribution_status": (
                    "exact-from-response" if exact_use else
                    "binding-id-capture-incomplete"
                ),
                "response_binding_ids_status": (
                    "complete" if exact_use else "partial"
                ),
                "supporting_events": references[:1],
            },
            "context_response_use_sequence_relation": {
                "status": "invocation-observed-after-context-response"
                if exact_use else "no-exact-binding-invocation-in-session-history",
            },
        })
    delivery.update({
        "binding_count": len(bindings),
        "bindings": bindings,
        "bindings_omitted": 0,
        "availability_counts": {"available": len(bindings)},
        "binding_evidence_summary": (
            capability_intelligence._capability_binding_evidence_summary(
                bindings,
                availability_counts={"available": len(bindings)},
                complete_history=True,
                binding_details_omitted_count=0,
                binding_records_not_examined=0,
                binding_records_invalid=0,
            )
        ),
    })

    delivery_without_rows = dict(delivery)
    delivery_without_rows["bindings"] = []
    projection_without_rows = dict(projection)
    projection_without_rows["session_activity"] = dict(
        projection["session_activity"],
    )
    projection_without_rows["session_activity"]["capability_delivery"] = (
        delivery_without_rows
    )
    minimal = capability_intelligence._compact_projection(
        projection_without_rows, 100_000, 100_000,
    )
    minimum_size = len(json.dumps(
        minimal, ensure_ascii=False, sort_keys=True,
    ).encode("utf-8"))
    budget = minimum_size + 96

    compact = capability_intelligence._compact_projection(
        projection, 100_000, budget,
    )
    compact_delivery = compact["session_activity"]["capability_delivery"]
    summary = compact_delivery["binding_evidence_summary"]

    assert compact_delivery["bindings"] == []
    assert compact_delivery["bindings_truncated"] is True
    assert summary["binding_details_truncated"] is True
    assert summary["binding_details_omitted_count"] == 9
    assert summary["exact_binding_invocation_count"] == 2
    assert summary["exact_binding_completion_count"] == 1
    assert summary["context_response_inclusion_status_counts"] == {
        "exact-binding-id-in-response": 1,
        "identity-tuple-in-matched-response": 8,
    }
    assert summary[
        "context_response_binding_id_attribution_status_counts"
    ] == {
        "binding-id-capture-incomplete": 8,
        "exact-from-response": 1,
    }
    assert summary["context_response_binding_id_list_status_counts"] == {
        "complete": 1,
        "partial": 8,
    }
    assert len(summary["context_response_supporting_events"]) == (
        capability_intelligence.MAX_CAPABILITY_SUPPORTING_EVENTS
    )
    assert summary["context_response_supporting_events_omitted"] == 6
    assert summary["context_response_supporting_events"][0]["identity"].startswith(
        "evt_8_"
    )
    assert summary["exact_binding_invocation_supporting_events_omitted"] == 0
    assert [
        item["identity"][:8]
        for item in summary["exact_binding_invocation_supporting_events"]
    ] == ["evt_0_2_", "evt_0_1_", "evt_0_0_"]
    assert summary["discoverability_status"] == "not-measured"
    assert summary["agent_receipt_status"] == "not-established"
    assert len(json.dumps(
        compact, ensure_ascii=False, sort_keys=True,
    ).encode("utf-8")) <= budget


def test_session_resource_sample_is_correlated_sanitized_and_compactable(
    tmp_path: Path,
) -> None:
    record = _observation(
        "ev_environment_resource_sample_activity",
        "ses_resource_sample_activity",
        "cmp_resource_sample_activity",
    )
    record["observation"]["outcome"]["large_diagnostic"] = "private " * 1_000
    capability = "mncs-system-monitor:system-resource-sample"
    binding_id = "cap_monitor_resource_sample"
    request_id = "req_monitor_resource_sample"
    store_reference = "mncs-store:invocation-result:resource-sample"
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
    sample_identity = "rsm_" + capability_intelligence.digest_hex({
        "session_id": "ses_projection_reader",
        "provider_request_id": request_id,
        "artifact_store_ref": store_reference,
    }, length=16)
    record["related_work"].update({
        "session_id": "ses_projection_reader",
        "campaign_id": "cmp_projection_reader",
        "work_intent_id": "int_projection_reader",
    })
    record["observation"]["outcome"]["related_resource_sample"] = {
        "identity": sample_identity,
        "artifact_reference": dict(metadata),
    }
    session, _ = _session(tmp_path, [record])
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
    observed_record = next(
        item for item in result["observations"]
        if item["identity"] == record["identity"]
    )
    relation = observed_record["resource_sample_relation"]
    assert relation["status"] == "matched-exact-resource-sample-and-session"
    assert relation["declaration_path"] == (
        "observation.outcome.related_resource_sample"
    )
    assert relation["sample_identity"] == sample_identity
    assert relation["work_context_status"] == (
        "session-campaign-work-intent-matched"
    )
    assert relation["artifact_reference_status"] == "matched"
    assert relation["artifact_reference"]["sha256"] == metadata["sha256"]
    assert relation["invocation_event"]["identity"] == (
        "evt_monitor_resource_sample_invoked"
    )
    assert "not establish agent-attributed consumption" in (
        relation["measurement_interpretation_limit"]
    )
    sibling_record = {
        **record,
        "observation": {
            **record["observation"],
            "outcome": {
                key: value for key, value
                in record["observation"]["outcome"].items()
                if key != "related_resource_sample"
            },
            "related_resource_sample": record["observation"]["outcome"][
                "related_resource_sample"
            ],
        },
    }
    sibling_relation = capability_intelligence._observation_resource_sample_relation(
        sibling_record,
        result["session_activity"],
        selected_session_id=session.session_id,
    )
    assert sibling_relation["status"] == "matched-exact-resource-sample-and-session"
    assert sibling_relation["declaration_path"] == "observation.related_resource_sample"

    conflicting_record = {
        **sibling_record,
        "observation": {
            **sibling_record["observation"],
            "outcome": {
                **sibling_record["observation"]["outcome"],
                "related_resource_sample": {
                    **sibling_record["observation"]["related_resource_sample"],
                    "identity": "rsm_other_sample",
                },
            },
        },
    }
    conflicting_relation = capability_intelligence._observation_resource_sample_relation(
        conflicting_record,
        result["session_activity"],
        selected_session_id=session.session_id,
    )
    assert conflicting_relation["status"] == (
        "conflicting-resource-sample-declarations"
    )
    wrong_intent_record = {
        **record,
        "related_work": {
            **record["related_work"],
            "work_intent_id": "int_other_work",
        },
    }
    wrong_intent_relation = (
        capability_intelligence._observation_resource_sample_relation(
            wrong_intent_record,
            result["session_activity"],
            selected_session_id=session.session_id,
        )
    )
    assert wrong_intent_relation["status"] == "work-context-mismatch"
    wrong_session_record = {
        **record,
        "related_work": {
            **record["related_work"],
            "session_id": "ses_other_session",
        },
    }
    wrong_session_relation = (
        capability_intelligence._observation_resource_sample_relation(
            wrong_session_record,
            result["session_activity"],
            selected_session_id=session.session_id,
        )
    )
    assert wrong_session_relation["status"] == "source-session-not-selected"
    wrong_artifact_record = {
        **record,
        "observation": {
            **record["observation"],
            "outcome": {
                **record["observation"]["outcome"],
                "related_resource_sample": {
                    "identity": sample_identity,
                    "artifact_reference": {
                        **metadata,
                        "sha256": "0" * 64,
                    },
                },
            },
        },
    }
    wrong_artifact_relation = (
        capability_intelligence._observation_resource_sample_relation(
            wrong_artifact_record,
            result["session_activity"],
            selected_session_id=session.session_id,
        )
    )
    assert wrong_artifact_relation["status"] == "artifact-reference-mismatch"
    missing_relation = (
        capability_intelligence._observation_resource_sample_relation(
            record,
            {"resource_samples": {
                "status": "not-captured-in-session",
                "source": {"artifact_window_truncated": False},
                "observations": [],
            }},
            selected_session_id=session.session_id,
        )
    )
    assert missing_relation["status"] == "sample-not-present-in-selected-session"
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
    compact_relation_view = capability_intelligence._compact_projection(
        result, 100_000, 50_000,
    )
    compact_record = next(
        item for item in compact_relation_view["observations"]
        if item["identity"] == record["identity"]
    )
    assert compact_record["resource_sample_relation"]["sample_identity"] == (
        sample_identity
    )
    assert compact_record["resource_sample_relation"]["artifact_reference"][
        "sha256"
    ] == metadata["sha256"]


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


def test_session_service_readiness_correlates_snapshot_dependencies_safely(
    tmp_path: Path,
) -> None:
    session, _ = _session(tmp_path, [])
    language_service = "mncs-language-service:resident-workspace"
    forge_service = "mncs-forge:resident-workspace"
    language_probe = "mncs-language-service:ready"
    service_execution_id = "ase_" + "c" * 32
    owner_pass_context = {
        "schema_version": "mncs.environment.ambient-owner-pass-context/1",
        "owner_pass": "doctor",
        "service_execution_id": service_execution_id,
    }
    private_path = "/home/private/workspace/.bootstrap/secret"
    session.snapshot.update({
        "snapshot_sequence": 37,
        "last_reconciled_at": "2026-10-26T18:10:20.123Z",
        "campaign": {"identity": "cmp_service_readiness"},
        "intent": {
            "identity": "int_service_readiness",
            "goal": "make readiness evidence useful",
        },
        "requirements": {
            "services": [
                {
                    "identity": language_service,
                    "required": True,
                    "probe": {"capability": language_probe},
                    "ready_when": {"/ready": True},
                },
                {
                    "identity": forge_service,
                    "required": False,
                    "probe": {"capability": "mncs-forge:ready"},
                    "ready_when": {"/ready": True},
                    "environment_from_service": {
                        "MNCS_LANGUAGE_WORKSPACE": {
                            "service": language_service,
                            "pointer": "/selected/workspace",
                        },
                    },
                },
            ],
        },
        "service_observations": [
            {
                "identity": language_service,
                "required": True,
                "capability": language_probe,
                "status": "degraded",
                "code": "service-not-ready",
                "reason": f"provider could not open {private_path}",
                "observed_at": "2026-10-26T18:10:20.100Z",
                "provider_detail": private_path,
                "observation": {"/ready": False},
            },
            {
                "identity": forge_service,
                "required": False,
                "capability": "mncs-forge:ready",
                "status": "unavailable",
                "code": "service-binding-missing",
                "reason": f"provider probe missing at {private_path}",
                "observed_at": "2026-10-26T18:10:20.101Z",
            },
        ],
        "service_operations": [
            {
                "identity": forge_service,
                "status": "failed",
                "code": "service-start-failed",
                "reason": f"cannot start from {private_path}",
                "provider_response": {"detail": private_path},
            },
        ],
    })
    session.snapshot["bindings"] = [{
        "binding_id": "cap_language_service_ready",
        "provider": "mncs-language-service-provider",
        "capability": language_probe,
        "contract_revision": "2",
        "availability": {
            "status": "available",
            "verification": "probe-success",
            "observed_at": "2026-10-26T18:10:19.900Z",
        },
    }]
    session.snapshot["coherence"] = {"last_trace": {
        "mode": "targeted_update",
        "trace_persistence_status": "session-snapshot",
        "event_count": 1,
        "elapsed_seconds": 0.1,
        "scheduled": [{
            "pass": "doctor",
            "owner": "doctor",
            "disposition": 1,
            "wave": 0,
            "service_execution_id": service_execution_id,
            "owner_reused": False,
            "timer_or_lifecycle_due": False,
            "run_elapsed_seconds": 0.1,
            "inputs": ["source.changed"],
        }],
        "skipped": [],
        "owner_failures": [],
    }}
    request_id = "req_service_readiness_probe"
    events = [
        {
            "identity": "evt_service_readiness_invoked",
            "sequence": 1,
            "type": "capability.invoked",
            "producer": "local-agent",
            "observed_at": "2026-10-26T18:10:19.950Z",
            "payload": {
                "capability": language_probe,
                "provider": "mncs-language-service-provider",
                "binding_id": "cap_language_service_ready",
                "contract_revision": "2",
                "provider_request_id": request_id,
                "ambient_owner_pass_context": owner_pass_context,
            },
        },
        {
            "identity": "evt_service_readiness_completed",
            "sequence": 2,
            "type": "invocation.completed",
            "producer": "mncs-language-service-provider",
            "observed_at": "2026-10-26T18:10:20.000Z",
            "payload": {
                "capability": language_probe,
                "provider": "mncs-language-service-provider",
                "binding_id": "cap_language_service_ready",
                "contract_revision": "2",
                "provider_request_id": request_id,
                "status": "ok",
                "returncode": 0,
                "ambient_owner_pass_context": owner_pass_context,
            },
        },
    ]
    session.recent_events = lambda *, limit, before_sequence=None: {
        "event_count": len(events), "events": events[-limit:],
        "truncated": False, "next_before_sequence": None,
    }

    result = capability_intelligence.project(session)
    original_projection_identity = result["identity"]
    original_readiness_digest = result["source"][
        "session_service_readiness_digest"
    ]
    original_accountability_digest = result["source"][
        "session_service_accountability_digest"
    ]
    original_event_window_digest = result["session_activity"]["source"][
        "event_window_identity_digest"
    ]
    original_delivery_digest = result["source"][
        "session_capability_delivery_digest"
    ]
    session.snapshot["service_observations"][0]["status"] = "ready"
    changed_readiness_result = capability_intelligence.project(session)
    assert changed_readiness_result["identity"] != original_projection_identity
    assert changed_readiness_result["source"][
        "session_service_readiness_digest"
    ] != original_readiness_digest
    assert changed_readiness_result["source"][
        "session_service_accountability_digest"
    ] != original_accountability_digest
    assert changed_readiness_result["session_activity"]["source"][
        "event_window_identity_digest"
    ] == original_event_window_digest
    assert changed_readiness_result["source"][
        "session_capability_delivery_digest"
    ] == original_delivery_digest
    session.snapshot["service_observations"][0]["status"] = "degraded"

    activity = result["session_activity"]
    readiness = activity["service_readiness"]
    assert readiness["status"] == "observed"
    assert readiness["readiness_status"] == "blocked"
    assert readiness["source"]["kind"] == "current-session-snapshot"
    assert readiness["source"]["snapshot_sequence"] == 37
    assert readiness["source"]["last_reconciled_at"] == (
        "2026-10-26T18:10:20.123Z"
    )
    assert readiness["summary"]["investigation_candidate_count"] == 2
    assert readiness["summary"]["probe_opportunity_count"] == 2
    assert readiness["summary"]["probe_observation_record_count"] == 2
    assert readiness["summary"]["probe_observation_missing_count"] == 0
    assert readiness["summary"]["probe_observation_ambiguous_count"] == 0
    assert readiness["summary"]["probe_configuration_missing_count"] == 0
    assert readiness["summary"]["probe_observation_unmatched_count"] == 0
    assert readiness["summary"]["probe_observation_coverage_status"] == (
        "complete-for-selected-snapshot"
    )
    services = {item["identity"]: item for item in readiness["services"]}
    assert services[language_service]["status"] == "degraded"
    assert services[forge_service]["dependencies"] == [{
        "service_identity": language_service,
        "observed_status": "degraded",
        "relationship": "declared-environment-input",
        "causal_status": "not-established",
    }]
    forge_candidate = next(
        item for item in readiness["candidates"]
        if item["service_identity"] == forge_service
    )
    assert forge_candidate["status"] == "candidate-needs-investigation"
    assert forge_candidate["causal_status"] == "not-established"
    assert forge_candidate["supporting_dependency_identities"] == [
        language_service,
    ]
    assert forge_candidate["objective_context"] == {
        "campaign_id": "cmp_service_readiness",
        "work_intent_id": "int_service_readiness",
    }
    assert result["session_activity_pressure_candidate_count"] == 2
    accountability = activity["service_accountability"]
    assert accountability["source"]["service_session_id"] == (
        "ses_projection_reader"
    )
    assert accountability["source"]["environment_id"] == (
        "renv_projection_reader"
    )
    assert accountability["source"]["snapshot_sequence"] == 37
    assert accountability["source"]["campaign_id"] == "cmp_service_readiness"
    assert accountability["source"]["work_intent_id"] == "int_service_readiness"
    accountable_services = {
        item["service_identity"]: item
        for item in accountability["services"]
    }
    language_accountability = accountable_services[language_service]
    assert language_accountability["readiness_status"] == "degraded"
    assert accountable_services[forge_service]["operation_code"] == (
        "service-start-failed"
    )
    assert language_accountability["observation_code"] == "service-not-ready"
    assert language_accountability["observed_at"] == (
        "2026-10-26T18:10:20.100Z"
    )
    assert language_accountability["probe_observation_record_status"] == (
        "unique-record"
    )
    assert language_accountability["capability_relation_status"] == (
        "exact-capability-identity-match"
    )
    assert language_accountability["bindings"][0]["availability_status"] == (
        "available"
    )
    assert language_accountability["bindings"][0]["use_observation_status"] == (
        "invoked-for-exact-session-binding"
    )
    assert language_accountability["bindings"][0]["supporting_events"][0][
        "identity"
    ] == "evt_service_readiness_invoked"
    ambient_probe_activity = language_accountability[
        "ambient_owner_pass_activity"
    ]
    assert ambient_probe_activity["status"] == (
        "exact-current-probe-binding-invocation-observed"
    )
    assert ambient_probe_activity["event_window_status"] == (
        "complete-session-history"
    )
    assert ambient_probe_activity["scoped_invocation_count"] == 1
    assert ambient_probe_activity["scoped_completion_count"] == 1
    assert ambient_probe_activity["owner_pass_executions"] == [{
        "owner_pass": "doctor",
        "service_execution_id": service_execution_id,
        "latest_trace_relation": "exact-latest-trace-match",
    }]
    assert ambient_probe_activity["probe_calls"] == [{
        "owner_pass": "doctor",
        "service_execution_id": service_execution_id,
        "latest_trace_relation": "exact-latest-trace-match",
        "probe_binding_attribution_status": "exact-current-session-binding",
        "binding_identity": {
            "status": "complete",
            "provider": "mncs-language-service-provider",
            "binding_id": "cap_language_service_ready",
            "contract_revision": "2",
        },
        "invocation_count": 1,
        "completion_count": 1,
        "outcome_status_counts": {"ok": 1},
        "nonzero_returncode_count": 0,
        "supporting_events": [
            {
                "identity": "evt_service_readiness_invoked",
                "sequence": 1,
                "type": "capability.invoked",
            },
            {
                "identity": "evt_service_readiness_completed",
                "sequence": 2,
                "type": "invocation.completed",
            },
        ],
        "supporting_events_truncated": False,
    }]
    assert ambient_probe_activity["supporting_events"] == [
        {
            "identity": "evt_service_readiness_invoked",
            "sequence": 1,
            "type": "capability.invoked",
        },
        {
            "identity": "evt_service_readiness_completed",
            "sequence": 2,
            "type": "invocation.completed",
        },
    ]
    assert ambient_probe_activity["causal_status"] == "not-established"
    assert language_accountability["ambient_fulfillment_status"] == (
        "not-established-no-eligible-opportunity-recorded"
    )
    assert language_accountability["outcome_quality_status"] == "not-established"
    assert accountability["summary"][
        "agent_intervention_burden_status"
    ] == "not-measured"
    assert accountability["summary"][
        "ambient_probe_use_relation_status_counts"
    ]["exact-current-probe-binding-invocation-observed"] == 1
    events[1]["payload"]["status"] = "failed"
    events[1]["payload"]["returncode"] = 7
    failed_probe = capability_intelligence.project(session)["session_activity"][
        "service_accountability"
    ]["services"][0]["ambient_owner_pass_activity"]["probe_calls"][0]
    assert failed_probe["outcome_status_counts"] == {"failed": 1}
    assert failed_probe["nonzero_returncode_count"] == 1
    assert failed_probe["supporting_events"][-1]["identity"] == (
        "evt_service_readiness_completed"
    )
    events[1]["payload"]["status"] = "ok"
    events[1]["payload"]["returncode"] = 0
    invocation_context = events[0]["payload"].pop(
        "ambient_owner_pass_context",
    )
    completion_context = events[1]["payload"].pop(
        "ambient_owner_pass_context",
    )
    unclassified_result = capability_intelligence.project(session)
    unclassified_activity = unclassified_result["session_activity"][
        "service_accountability"
    ]["services"][0]["ambient_owner_pass_activity"]
    assert unclassified_activity["status"] == (
        "unclassified-probe-capability-invocation-observed"
    )
    assert unclassified_activity["scoped_invocation_count"] == 0
    assert unclassified_activity["unclassified_invocation_count"] == 1
    assert unclassified_activity["binding_attribution_status"] == (
        "not-correlated-to-owner-pass"
    )
    events[0]["payload"]["ambient_owner_pass_context"] = invocation_context
    events[1]["payload"]["ambient_owner_pass_context"] = completion_context
    assert accountable_services[forge_service]["capability_relation_status"] == (
        "capability-not-present-in-current-binding-snapshot"
    )
    assert accountability["summary"]["available_service_capability_binding_count"] == 1
    assert accountability["summary"]["exact_binding_use_observed_count"] == 1
    assert accountability["summary"]["capability_name_use_observed_count"] == 0
    assert accountability["summary"]["eligible_service_opportunity_count"] is None
    assert accountability["summary"]["readiness_probe_opportunity_count"] == 2
    assert accountability["summary"][
        "readiness_probe_observation_record_count"
    ] == 2
    assert accountability["summary"]["readiness_probe_coverage_status"] == (
        "complete-for-selected-snapshot"
    )
    assert accountability["ambient_fulfillment"][
        "eligible_opportunity_denominator"
    ] == "not-recorded"
    assert accountability["summary"]["agent_receipt_status"] == "not-established"
    assert accountability["summary"][
        "agent_intervention_burden_status"
    ] == "not-measured"
    assert private_path not in json.dumps(result)
    assert "provider_detail" not in json.dumps(result)
    assert "cannot start from" not in json.dumps(result)

    compact = capability_intelligence.project(session, max_result_bytes=8_000)
    compact_readiness = compact["session_activity"]["service_readiness"]
    assert compact["source"]["response_truncated"] is True
    assert compact_readiness["source"]["snapshot_sequence"] == 37
    assert compact_readiness["summary"]["probe_opportunity_count"] == 2
    assert any(
        item["service_identity"] == forge_service
        for item in compact_readiness["candidates"]
    )
    full_projection = capability_intelligence.project(
        session, max_result_bytes=capability_intelligence.MAX_RESULT_BYTES,
    )
    full_size = len(json.dumps(
        full_projection, ensure_ascii=False, sort_keys=True,
    ).encode("utf-8"))
    compact_with_accountability = capability_intelligence._compact_projection(
        full_projection, full_size, 16_000,
    )
    compact_accountability = compact_with_accountability["session_activity"][
        "service_accountability"
    ]
    assert compact_with_accountability["source"][
        "session_service_readiness_digest"
    ] == original_readiness_digest
    assert compact_with_accountability["source"][
        "session_service_accountability_digest"
    ] == original_accountability_digest
    compact_language = next(
        item for item in compact_accountability["services"]
        if item["service_identity"] == language_service
    )
    assert compact_language["bindings"][0]["supporting_events"][0][
        "identity"
    ] == "evt_service_readiness_invoked"
    assert compact_language["ambient_owner_pass_activity"]["probe_calls"] == (
        language_accountability["ambient_owner_pass_activity"]["probe_calls"]
    )
    assert compact_accountability["summary"][
        "eligible_service_opportunity_count"
    ] is None
    assert compact_accountability["summary"][
        "readiness_probe_coverage_status"
    ] == "complete-for-selected-snapshot"
    assert private_path not in json.dumps(compact)

    observations = session.snapshot["service_observations"]
    session.snapshot["service_observations"] = observations[:1]
    missing_probe_result = capability_intelligence.project(session)
    missing_readiness = missing_probe_result["session_activity"][
        "service_readiness"
    ]
    assert missing_readiness["summary"]["probe_opportunity_count"] == 2
    assert missing_readiness["summary"]["probe_observation_record_count"] == 1
    assert missing_readiness["summary"]["probe_observation_missing_count"] == 1
    assert missing_readiness["summary"]["probe_observation_coverage_status"] == (
        "partial-selected-snapshot"
    )
    missing_accountability = missing_probe_result["session_activity"][
        "service_accountability"
    ]
    assert missing_accountability["summary"][
        "readiness_probe_coverage_status"
    ] == "partial-selected-snapshot"
    assert missing_accountability["summary"][
        "eligible_service_opportunity_count"
    ] is None

    session.snapshot["service_observations"] = [
        observations[0], observations[0], observations[1],
    ]
    ambiguous_probe_result = capability_intelligence.project(session)
    ambiguous_readiness = ambiguous_probe_result["session_activity"][
        "service_readiness"
    ]
    assert ambiguous_readiness["summary"]["probe_opportunity_count"] == 2
    assert ambiguous_readiness["summary"][
        "probe_observation_record_count"
    ] == 1
    assert ambiguous_readiness["summary"][
        "probe_observation_ambiguous_count"
    ] == 1
    assert ambiguous_readiness["summary"][
        "probe_observation_coverage_status"
    ] == "partial-selected-snapshot"

    session.snapshot["service_observations"] = observations
    session.snapshot["requirements"]["services"][1].pop("probe")
    missing_probe_contract = capability_intelligence.project(session)
    contract_summary = missing_probe_contract["session_activity"][
        "service_readiness"
    ]["summary"]
    assert contract_summary["probe_opportunity_count"] == 1
    assert contract_summary["probe_configuration_missing_count"] == 1
    assert contract_summary["probe_observation_coverage_status"] == (
        "partial-selected-snapshot"
    )

    session.snapshot["requirements"]["services"][1]["probe"] = {
        "capability": "mncs-forge:ready",
    }
    session.snapshot["service_observations"] = [
        *observations,
        {"identity": "unmatched-old-service", "status": "ready"},
    ]
    unmatched_probe_observation = capability_intelligence.project(session)
    unmatched_summary = unmatched_probe_observation["session_activity"][
        "service_readiness"
    ]["summary"]
    assert unmatched_summary["probe_opportunity_count"] == 2
    assert unmatched_summary["probe_observation_record_count"] == 2
    assert unmatched_summary["probe_observation_unmatched_count"] == 1
    assert unmatched_summary["probe_observation_coverage_status"] == (
        "partial-selected-snapshot"
    )


def test_service_probe_bindings_survive_large_inventory_truncation() -> None:
    probe_capability = "mncs-doctor:compiler-vm-health"
    irrelevant_bindings = [{
        "binding_id": f"cap_unrelated_{index:02d}",
        "provider": "mncs-test-provider",
        "capability": f"aaa:unrelated-{index:02d}",
        "contract_revision": "1",
        "availability": {
            "status": "available", "verification": "substrate",
            "observed_at": "2026-10-27T00:00:00Z",
        },
    } for index in range(capability_intelligence.MAX_SESSION_ACTIVITY_BINDINGS + 8)]
    probe_binding = {
        "binding_id": "cap_compiler_vm_health",
        "provider": "mncs-doctor",
        "capability": probe_capability,
        "contract_revision": "2",
        "availability": {
            "status": "available", "verification": "substrate",
            "observed_at": "2026-10-27T00:00:00Z",
        },
    }
    snapshot = {
        "environment_id": "renv_service_probe_inventory",
        "bindings": [*irrelevant_bindings, probe_binding],
    }

    delivery = capability_intelligence._capability_delivery_view(
        snapshot, [], [], [],
        event_window_truncated=False,
        event_page_scoped=False,
        invocation_summaries_omitted=0,
        service_probe_capabilities=[probe_capability],
    )

    assert delivery["binding_count"] == (
        capability_intelligence.MAX_SESSION_ACTIVITY_BINDINGS + 9
    )
    assert delivery["bindings_omitted"] == 9
    assert delivery["binding_details_selection_basis"].startswith(
        "declared-service-probes-then-exact-invocation-bindings"
    )
    assert delivery["service_probe_binding_counts"] == {probe_capability: 1}
    assert delivery["service_probe_binding_count_scan_complete"] is True
    assert delivery["bindings"][0]["capability"] == probe_capability

    readiness = {
        "status": "observed",
        "services": [{
            "identity": "mncs-doctor:compiler-vm-coherence",
            "required": True,
            "status": "unavailable",
            "capability": probe_capability,
            "probe_observation_record_status": "unique-record",
            "operation_status": "failed",
        }],
        "candidates": [],
        "source": {
            "session_id": "ses_service_probe_inventory",
            "environment_id": "renv_service_probe_inventory",
            "snapshot_sequence": 9,
        },
        "summary": {"declared_service_count": 1},
    }
    accountability = capability_intelligence._session_service_accountability(
        readiness, delivery, snapshot,
        capability_intelligence._session_ambient_owner_pass_activity(
            [], 0, {},
        ),
    )

    service = accountability["services"][0]
    assert service["capability_relation_status"] == (
        "exact-capability-identity-match"
    )
    assert service["binding_match_count"] == 1
    assert service["bindings"][0]["capability"] == probe_capability

    invalid_snapshot = {"bindings": [{
        "binding_id": "cap_probe_identity_incomplete",
        "capability": probe_capability,
        "contract_revision": "2",
        "availability": {"status": "available"},
    }]}
    incomplete_delivery = capability_intelligence._capability_delivery_view(
        invalid_snapshot, [], [], [],
        event_window_truncated=False,
        event_page_scoped=False,
        invocation_summaries_omitted=0,
        service_probe_capabilities=[probe_capability],
    )
    assert incomplete_delivery["service_probe_binding_counts"] == {
        probe_capability: 0,
    }
    assert incomplete_delivery["service_probe_binding_count_scan_complete"] is False
    incomplete_accountability = (
        capability_intelligence._session_service_accountability(
            readiness, incomplete_delivery, invalid_snapshot,
        )
    )
    assert incomplete_accountability["services"][0][
        "capability_relation_status"
    ] == "capability-not-observed-in-incomplete-binding-snapshot"


def test_session_doctor_health_history_is_bounded_and_revision_linked(
    tmp_path: Path,
) -> None:
    record = _observation(
        "ev_environment_doctor_history", "ses_projection_reader",
        "cmp_doctor_history",
    )
    session, _ = _session(tmp_path, [record])
    private_path = "/home/private/workspace/.state/doctor-detail"
    epoch_digest = "a" * 16
    session.snapshot.update({
        "snapshot_sequence": 25,
        "environment_id": "renv_projection_reader",
        "campaign": {"identity": "cmp_doctor_history"},
        "intent": {"identity": "int_doctor_history", "goal": "inspect health history"},
        "requirements": {"services": [{
            "identity": "mncs-environment:health-probe",
            "required": True,
            "probe": {"capability": "mncs-environment:health-probe:ready"},
        }]},
        "service_observations": [{
            "identity": "mncs-environment:health-probe",
            "status": "degraded",
            "code": "service-not-ready",
            "observed_at": "2026-10-26T18:10:20.100Z",
        }],
        "doctor": {
            "schema_version": "mncs.environment.doctor/2",
            "epoch": {
                "schema_version": "mncs.environment.doctor-epoch/2",
                "digest": epoch_digest,
                "validated_at": "2026-10-26T18:10:20.123Z",
                "snapshot_sequence": 22,
                "readiness": "degraded",
                "summary": {
                    "repaired": 1, "reconciled": 2,
                    "degraded": 3, "blockers": 1,
                },
                "unavailable": {"count": 3, "digest": "b" * 16},
                "remaining": [private_path],
                "inputs": {"workspace_root": private_path},
            },
            "history": [
                {
                    "digest": f"{index:016x}",
                    "validated_at": f"2026-10-26T18:10:{index:02d}.123Z",
                    "summary": {
                        "repaired": index, "reconciled": 1,
                        "degraded": 2, "blockers": 0,
                    },
                    "unavailable": {"count": 2, "digest": "c" * 16},
                    "reused": index % 2 == 0,
                    "remaining": [private_path],
                }
                for index in range(10)
            ],
            "evidence_ref": {
                "schema": "mncs.environment.doctor-evidence/1",
                "identity": f"ses_projection_reader:doctor:{'d' * 64}",
            },
        },
    })

    projection = capability_intelligence.project(session)
    activity = projection["session_activity"]
    history = activity["doctor_health_history"]
    assert history["status"] == "partial"
    assert history["source"]["session_id"] == "ses_projection_reader"
    assert history["source"]["snapshot_sequence"] == 25
    assert history["source"]["campaign_id"] == "cmp_doctor_history"
    assert history["source"]["work_intent_id"] == "int_doctor_history"
    assert history["source"]["history_record_count"] == 10
    assert history["source"]["history_records_projected"] == 8
    assert history["source"]["history_records_omitted"] == 2
    assert history["current_epoch"]["snapshot_relation"] == (
        "earlier-snapshot-revision"
    )
    assert history["current_epoch"]["readiness"] == "degraded"
    assert history["current_epoch"]["evidence_reference"] == {
        "kind": "doctor-immutable-evidence-record",
        "schema": "mncs.environment.doctor-evidence/1",
        "identity": f"ses_projection_reader:doctor:{'d' * 64}",
    }
    assert history["service_readiness_co_observation"] == {
        "status": "same-current-session-snapshot",
        "relationship": "co-present-in-current-session-snapshot",
        "service_readiness_status": "blocked",
        "investigation_candidate_count": 1,
        "doctor_epoch_snapshot_relation": "earlier-snapshot-revision",
        "causal_status": "not-established",
    }
    assert history["history"][0]["digest"] == f"{9:016x}"
    assert history["history"][0]["ordinal_from_newest"] == 1
    assert history["history"][0]["evidence_reference_status"] == (
        "not-recorded-per-history-entry"
    )
    assert private_path not in json.dumps(projection)
    assert "inputs" not in json.dumps(history)
    assert "remaining" not in json.dumps(history)

    compact = capability_intelligence.project(session, max_result_bytes=8_000)
    compact_history = compact["session_activity"]["doctor_health_history"]
    assert compact["source"]["response_truncated"] is True
    assert compact_history["current_epoch"]["evidence_reference"]["identity"] == (
        f"ses_projection_reader:doctor:{'d' * 64}"
    )
    assert compact_history["service_readiness_co_observation"]["causal_status"] == (
        "not-established"
    )
    assert len(compact_history["history"]) == 8
    assert private_path not in json.dumps(compact)


def test_session_projection_coherence_history_is_bounded_and_linked(
    tmp_path: Path,
) -> None:
    session, _ = _session(tmp_path, [])
    assert capability_intelligence._session_projection_coherence_history(
        session, {"projection_history": []},
    )["status"] == "not-recorded-in-session"
    session.snapshot.update({
        "environment_id": "renv_projection_reader",
        "snapshot_sequence": 41,
        "campaign": {"identity": "cmp_projection_reader"},
        "intent": {
            "identity": "int_projection_reader",
            "goal": "inspect projection freshness",
        },
    })
    private_path = "/home/private/workspace/.state/projection-evidence"
    evidence_prefix = "mncs-store:ses_projection_reader:projections:"
    session.snapshot["projection_epoch"] = {
        "epoch": "uncached:ambient:" + "a" * 64,
        "at": "2026-10-27T05:10:02Z",
        "summary": {
            "current": 4,
            "pending": 1,
            "reconciled": 2,
            "blockers": 0,
            "degraded": 1,
            "epoch_reused": False,
            "elapsed_seconds": 1.25,
        },
        "evidence_ref": evidence_prefix + "b" * 64,
    }
    session.snapshot["projection_history"] = [
        {
            "at": f"2026-10-27T05:{index:02d}:02Z",
            "mode": "ambient" if index % 2 == 0 else "explicit",
            "summary": {
                "current": 3,
                "pending": 1,
                "reconciled": 2,
                "blockers": 0,
                "degraded": 0,
                "epoch_reused": False,
                "elapsed_seconds": index / 10,
            },
            "evidence_ref": (
                private_path if index == 8
                else (
                    "sessions/ses_projection_reader/projection-artifacts/evidence/"
                    "projection-explicit-0123456789ab.json"
                ) if index == 7
                else (
                    "mncs-store:ses_other:projections:" + "d" * 64
                ) if index == 6
                else evidence_prefix + f"{index:064x}"
            ),
            "raw_detail": private_path,
        }
        for index in range(10)
    ]

    projection = capability_intelligence.project(session)
    view = projection["session_activity"]["projection_coherence_history"]
    assert view["status"] == "partial"
    assert view["scope"] == "current-session-ambient-projection-pass-history"
    assert view["source"]["session_id"] == "ses_projection_reader"
    assert view["source"]["campaign_id"] == "cmp_projection_reader"
    assert view["source"]["work_intent_id"] == "int_projection_reader"
    assert view["source"]["snapshot_sequence"] == 41
    assert view["source"]["history_record_count"] == 10
    assert view["source"]["history_records_projected"] == 8
    assert view["source"]["history_records_omitted"] == 2
    assert view["source"]["mode_counts_projected"] == {
        "ambient": 4,
        "explicit": 4,
    }
    assert view["current_epoch"]["summary"]["reconciled"] == 2
    assert view["current_epoch"]["evidence_reference"] == {
        "kind": "projection-immutable-evidence-record",
        "schema": "mncs.environment.session-evidence/1",
        "identity": "ses_projection_reader:projections:" + "b" * 64,
    }
    assert view["history"][0]["ordinal_from_newest"] == 1
    assert view["history"][0]["evidence_reference_status"] == (
        "reference-identified"
    )
    assert view["history"][1]["evidence_reference_status"] == (
        "reference-invalid-or-unmatched"
    )
    assert view["history"][2]["evidence_reference"] == {
        "kind": "session-projection-evidence-artifact",
        "session_id": "ses_projection_reader",
        "relative_path": (
            "sessions/ses_projection_reader/projection-artifacts/evidence/"
            "projection-explicit-0123456789ab.json"
        ),
    }
    assert view["history"][3]["evidence_reference_status"] == (
        "reference-invalid-or-unmatched"
    )
    assert private_path not in json.dumps(projection)
    assert projection["source"]["session_projection_coherence_digest"]

    original_identity = projection["identity"]
    original_event_digest = projection["session_activity"]["source"][
        "event_window_identity_digest"
    ]
    session.snapshot["projection_history"][-1]["summary"]["reconciled"] = 3
    changed = capability_intelligence.project(session)
    assert changed["identity"] != original_identity
    assert changed["session_activity"]["source"][
        "event_window_identity_digest"
    ] == original_event_digest

    compact = capability_intelligence.project(session, max_result_bytes=8_000)
    compact_view = compact["session_activity"]["projection_coherence_history"]
    assert compact["source"]["response_truncated"] is True
    assert compact_view["source"]["history_record_count"] == 10
    assert compact_view["history"][0]["evidence_reference"]["identity"].endswith(
        "0" * 63 + "9",
    )
    assert private_path not in json.dumps(compact)


def test_ambient_service_execution_is_projected_with_session_context_and_limits(
    tmp_path: Path,
) -> None:
    session, _ = _session(tmp_path, [])
    assert capability_intelligence._session_ambient_service_execution(
        session, {},
    )["status"] == "not-recorded-in-session"
    session.snapshot.update({
        "environment_id": "renv_ambient_service_trace",
        "snapshot_sequence": 52,
        "campaign": {"identity": "cmp_ambient_service_trace"},
        "intent": {"identity": "int_ambient_service_trace"},
        "coherence": {"last_trace": {
            "mode": "targeted_update",
            "trace_persistence_status": "session-snapshot",
            "event_count": 2,
            "elapsed_seconds": 0.4,
            "scheduled": [{
                "pass": "verification",
                "owner": "verification",
                "disposition": 1,
                "wave": 0,
                "owner_reused": False,
                "timer_or_lifecycle_due": False,
                "run_elapsed_seconds": 0.2,
                "inputs": ["source.changed"],
                "checkout": "/private/project",
            }],
            "skipped": [{"pass": "doctor", "reason": "current inputs"}],
            "owner_failures": [],
        }},
    })

    projection = capability_intelligence.project(session)
    view = projection["session_activity"]["ambient_service_execution"]
    assert view["status"] == "partial"
    assert view["scope"] == "latest-current-session-incremental-owner-pass-trace"
    assert view["source"]["session_id"] == "ses_projection_reader"
    assert view["source"]["environment_id"] == "renv_ambient_service_trace"
    assert view["source"]["snapshot_sequence"] == 52
    assert view["source"]["campaign_id"] == "cmp_ambient_service_trace"
    assert view["source"]["work_intent_id"] == "int_ambient_service_trace"
    assert view["source"]["owner_pass_decision_coverage"] == "partial"
    assert view["execution"]["trace_persistence_status"] == "session-snapshot"
    assert view["execution"]["scheduled_pass_count"] == 1
    assert view["execution"]["skipped_pass_count"] == 1
    assert view["execution"]["scheduled_passes"][0]["run_elapsed_seconds"] == 0.2
    assert view["execution"]["ambient_fulfillment"] == "not-established"
    assert view["execution"]["agent_need_opportunity_denominator"] == "not-recorded"
    assert projection["source"]["session_ambient_service_execution_digest"]
    assert "/private/project" not in json.dumps(projection)

    original_identity = projection["identity"]
    original_event_digest = projection["session_activity"]["source"][
        "event_window_identity_digest"
    ]
    session.snapshot["coherence"]["last_trace"]["elapsed_seconds"] = 0.5
    changed = capability_intelligence.project(session)
    assert changed["identity"] != original_identity
    assert changed["session_activity"]["source"][
        "event_window_identity_digest"
    ] == original_event_digest
    compact = capability_intelligence.project(session, max_result_bytes=8_000)
    compact_view = compact["session_activity"]["ambient_service_execution"]
    assert compact["source"]["response_truncated"] is True
    assert compact_view["execution"]["scheduled_pass_count"] == 1


def test_ambient_owner_pass_invocations_correlate_to_latest_trace(tmp_path: Path) -> None:
    session, _ = _session(tmp_path, [])
    execution_id = "ase_" + "d" * 32
    owner_context = {
        "schema_version": "mncs.environment.ambient-owner-pass-context/1",
        "owner_pass": "verification",
        "service_execution_id": execution_id,
    }
    session.snapshot["coherence"] = {"last_trace": {
        "mode": "targeted_update",
        "trace_persistence_status": "session-snapshot",
        "event_count": 1,
        "elapsed_seconds": 0.1,
        "scheduled": [{
            "pass": "verification",
            "owner": "verification",
            "disposition": 1,
            "wave": 0,
            "service_execution_id": execution_id,
            "owner_reused": False,
            "timer_or_lifecycle_due": False,
            "run_elapsed_seconds": 0.1,
            "inputs": ["source.changed"],
        }],
        "skipped": [],
        "owner_failures": [],
    }}
    events = [
        {
            "identity": "evt_ambient_start",
            "sequence": 1,
            "type": "capability.invoked",
            "producer": "environment",
            "payload": {
                "provider_request_id": "req_ambient_1",
                "capability": "mncs-verification:run",
                "ambient_owner_pass_context": owner_context,
            },
        },
        {
            "identity": "evt_ambient_complete",
            "sequence": 2,
            "type": "invocation.completed",
            "producer": "verification",
            "payload": {
                "provider_request_id": "req_ambient_1",
                "capability": "mncs-verification:run",
                "status": "ok",
                "returncode": 0,
                "ambient_owner_pass_context": owner_context,
            },
        },
        {
            "identity": "evt_unclassified_start",
            "sequence": 3,
            "type": "capability.invoked",
            "producer": "environment",
            "payload": {
                "provider_request_id": "req_ambient_2",
                "capability": "mncs-projections:read",
            },
        },
    ]
    session.recent_events = lambda *, limit, before_sequence=None: {
        "event_count": len(events),
        "events": [event for event in events if (
            before_sequence is None or event["sequence"] < before_sequence
        )][-limit:],
        "truncated": False,
        "next_before_sequence": None,
    }

    projection = capability_intelligence.project(session)
    activity = projection["session_activity"]["ambient_owner_pass_activity"]
    assert activity["status"] == "observed"
    assert activity["invocation_event_count"] == 2
    assert activity["scoped_invocation_count"] == 1
    assert activity["unclassified_invocation_count"] == 1
    assert "manual_invocation_count" not in activity
    assert activity["execution_group_count"] == 1
    group = activity["execution_groups"][0]
    assert group["service_execution_id"] == execution_id
    assert group["owner_pass"] == "verification"
    assert group["latest_trace_relation"] == "exact-latest-trace-match"
    assert group["invocation_count"] == 1
    assert group["completion_count"] == 1
    assert group["outcome_status_counts"] == {"ok": 1}
    assert len(group["supporting_events"]) == 2
    assert "reduced intervention burden" in " ".join(
        activity["measurement_limits"],
    )

    compact = capability_intelligence.project(session, max_result_bytes=8_000)
    assert compact["source"]["response_truncated"] is True
    compact_activity = compact["session_activity"]["ambient_owner_pass_activity"]
    assert compact_activity["execution_groups"][0]["latest_trace_relation"] == (
        "exact-latest-trace-match"
    )
    assert compact["source"]["session_ambient_owner_pass_activity_digest"]


def test_ambient_owner_pass_mismatched_completion_context_is_not_joined(
    tmp_path: Path,
) -> None:
    session, _ = _session(tmp_path, [])
    context = {
        "schema_version": "mncs.environment.ambient-owner-pass-context/1",
        "owner_pass": "verification",
        "service_execution_id": "ase_" + "e" * 32,
    }
    completion_context = {
        **context,
        "service_execution_id": "ase_" + "f" * 32,
    }
    events = [
        {
            "identity": "evt_mismatch_start", "sequence": 1,
            "type": "capability.invoked", "producer": "environment",
            "payload": {
                "provider_request_id": "req_mismatch",
                "ambient_owner_pass_context": context,
            },
        },
        {
            "identity": "evt_mismatch_complete", "sequence": 2,
            "type": "invocation.completed", "producer": "verification",
            "payload": {
                "provider_request_id": "req_mismatch", "status": "ok",
                "returncode": 0,
                "ambient_owner_pass_context": completion_context,
            },
        },
    ]
    session.recent_events = lambda *, limit, before_sequence=None: {
        "event_count": len(events), "events": events[-limit:],
        "truncated": False, "next_before_sequence": None,
    }

    projection = capability_intelligence.project(session)
    activity = projection["session_activity"]["ambient_owner_pass_activity"]
    assert activity["status"] == "partial"
    assert activity["completion_context_mismatch_count"] == 1
    assert activity["context_bearing_completion_count"] == 0
    assert activity["execution_groups"][0]["completion_count"] == 0
    assert activity["execution_groups"][0][
        "completion_context_mismatch_count"
    ] == 1


def test_service_probe_observation_preserves_bounded_projection_uncertainty() -> None:
    observation = capability_intelligence._service_ambient_owner_pass_observation(
        "mncs-language-service:ready",
        [],
        {
            "status": "observed",
            "execution_groups": [],
            "execution_groups_truncated": True,
            "execution_groups_omitted_count": 1,
            "unclassified_capability_activity": [],
            "unclassified_capability_activity_omitted_count": 0,
        },
        "truncated-selected-session-event-window",
    )

    assert observation["status"] == (
        "probe-capability-activity-projection-incomplete"
    )
    assert observation["binding_attribution_status"] == (
        "not-established-activity-omitted"
    )
    assert observation["observation_completeness_status"] == (
        "partial-bounded-activity-projection"
    )
    assert observation["event_window_status"] == (
        "truncated-selected-session-event-window"
    )


def test_live_entry_trace_gap_evidence_projects_without_claiming_causality(
    tmp_path: Path,
) -> None:
    path = Path(__file__).resolve().parents[1] / (
        "evidence/environmental-capability/"
        "ambient-service-decision-trace-gap-20261027.json"
    )
    record = json.loads(path.read_text(encoding="utf-8"))
    session, _ = _session(tmp_path, [record])
    related = record["related_work"]
    session.session_id = related["session_id"]
    session.snapshot.update({
        "environment_id": related["environment_id"],
        "snapshot_sequence": record["observation"]["outcome"][
            "snapshot_sequence"
        ],
        "campaign": {"identity": related["campaign_id"]},
        "intent": {
            "identity": related["work_intent_id"],
            "goal": related["work_intent_goal"],
        },
        "coherence": {},
    })

    projection = capability_intelligence.project(session)

    assert projection["status"] == "ready"
    assert projection["observation_count"] == 1
    observation = projection["observations"][0]
    assert observation["identity"] == record["identity"]
    assert observation["pressure_hypothesis"]["identity"] == (
        "ph_environment_ambient_service_trace_visibility"
    )
    assert observation["pressure_hypothesis_role"] == "occurrence"
    assert observation["related_work"]["session_id"] == related["session_id"]
    assert observation["related_work"]["environment_id"] == (
        related["environment_id"]
    )
    assert observation["observation"]["outcome"][
        "incremental_scheduler_trace"
    ]["status"] == "not-recorded-in-session-snapshot"
    assert any(
        "Btrfs metadata allocation" in item
        for item in observation["measurement_limits"]
    )
    assert "ambient_service_execution" not in projection["session_activity"]

    compact = capability_intelligence._compact_projection(
        projection,
        capability_intelligence.MAX_RESULT_BYTES + 1,
        capability_intelligence.MAX_RESULT_BYTES,
    )
    compact_observation = compact["observations"][0]
    compact_evidence = compact_observation["observation"][
        "ambient_service_evidence"
    ]
    assert compact["source"]["response_truncated"] is True
    assert compact_observation["observation"]["details_omitted"] is True
    assert compact_observation["evidence_reference"] == observation[
        "evidence_reference"
    ]
    assert compact_evidence["entry"] == {
        "response_returned": False,
        "termination": "interrupted after bounded process-resource observation",
    }
    assert compact_evidence["readiness"] == {
        "status": "blocked",
        "blocker_count": 2,
        "unavailable_capability_count": 66,
    }
    assert compact_evidence["incremental_scheduler_trace"]["status"] == (
        "not-recorded-in-session-snapshot"
    )
    assert compact_evidence["session_activity"]["event_count"] == 57
    assert compact_evidence["session_activity"]["capability_invocations"] == 26
    assert compact_evidence["session_activity"]["invocations_by_capability"] == {
        "mncs-automation:automation-reconciliation": 2,
        "mncs-doctor:projection-health": 18,
        "mncs-doc:documentation-projection": 5,
        "mncs.test-verification-coherence/1": 1,
    }
    assert compact_evidence["entry_process_sample"]["block_write_bytes_at_sample"] == (
        834_867_200
    )
    assert compact_evidence["ambient_fulfillment"] == "not-established"
    assert compact_evidence["agent_need_opportunity_denominator"] == "not-recorded"
    assert compact_evidence["causal_attribution"] == "not-established"
    assert len(json.dumps(compact_evidence)) < 4_096


def test_compact_ambient_service_evidence_uses_allowlisted_values() -> None:
    path = Path(__file__).resolve().parents[1] / (
        "evidence/environmental-capability/"
        "ambient-service-decision-trace-gap-20261027.json"
    )
    observation = json.loads(path.read_text(encoding="utf-8"))["observation"]
    observation["outcome"]["entry_process_sample"]["private_path"] = (
        "/private/project/source.mncs"
    )
    observation["outcome"]["session_activity"]["invocations_by_capability"][
        "/private/project"
    ] = 99

    compact = capability_intelligence._compact_ambient_service_evidence(
        observation,
    )

    assert compact is not None
    assert "/private/project" not in json.dumps(compact)
    assert "private_path" not in json.dumps(compact)


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
                "repository": "mncs-environment",
                "repository_preflight_state": {
                    "schema_version": (
                        "mncs.environment.integration-preflight-state/1"
                    ),
                    "observation_basis": (
                        "fresh-registered-checkout-inspection"
                    ),
                    "checkout_identity": "checkout:main-target",
                    "branch": "main",
                    "branch_status": "attached",
                    "head": "d" * 40,
                    "head_status": "measured",
                    "working_tree_status": "clean",
                },
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
    assert activity["authority_denials"][0][
        "integration_preflight_state_status"
    ] == "recorded-and-validated"
    assert activity["authority_denials"][0]["repository_preflight_state"][
        "head"
    ] == "d" * 40
    assert "does not establish whether the denial was appropriate" in (
        activity["authority_denials"][0]["preflight_state_interpretation_limit"]
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

    compact = capability_intelligence.project(session, max_result_bytes=6_500)
    compact_activity = compact["session_activity"]
    assert compact_activity["authority_denials"][0]["event_reference"][
        "identity"
    ] == "evt_authority_denied_42"
    assert compact_activity["authority_denials"][0][
        "integration_preflight_state_status"
    ] == "recorded-and-validated"
    assert compact_activity["authority_denials"][0][
        "repository_preflight_state"]["head"] == "d" * 40
    assert compact_activity["authority_denial_candidates"][0][
        "supporting_events"
    ][0]["identity"] == "evt_authority_denied_41"
    assert alias_checkout not in json.dumps(compact)
    assert "private-reason-echo" not in json.dumps(compact)


def test_coordination_deferrals_project_without_becoming_authority_escalations(
    tmp_path: Path,
) -> None:
    record = _observation(
        "ev_environment_coordination_defer",
        "ses_environment_coordination_defer",
        "cmp_environment_coordination_defer",
    )
    session, _ = _session(tmp_path, [record])
    private_checkout = str(
        Path(session.snapshot["selected_checkouts"]["mncs-environment"]["path"])
        / ".worktrees" / "private-target"
    )
    events = [{
        "identity": "evt_coordination_deferred_42",
        "sequence": 42,
        "type": "authority.coordination-deferred",
        "payload": {
            "action": "write",
            "target": private_checkout,
            "reason": "private-checkout-detail should not be projected",
            "decision_domain": "coordination",
            "repository": "mncs-environment",
            "required_coordination": "scoped-mutation-lease",
            "coordination_status": "exact-scope-lease-required",
            "next_step": "select-or-create-an-observed-proposal-worktree",
            "persistent_authority_required": False,
            "operation_executed": False,
            "scope": {
                "kind": "worktree",
                "repository": "mncs-environment",
                "exclusive": False,
                "branch": "main",
                "checkout_identity": "checkout:private-target",
            },
            "campaign_id": "cmp_environment_coordination_defer",
            "work_intent_id": "int_environment_coordination_defer",
        },
    }]
    events.append({
        **events[0],
        "identity": "evt_coordination_deferred_41",
        "sequence": 41,
    })
    events.append({
        **events[0],
        "identity": "evt_coordination_deferred_other_scope",
        "sequence": 40,
        "payload": {
            **events[0]["payload"],
            "scope": {
                **events[0]["payload"]["scope"],
                "branch": "campaign/other-scope",
                "checkout_identity": "checkout:other-scope",
            },
        },
    })
    events.extend([
        {
            "identity": "evt_unrelated_proposal_eligible",
            "sequence": 43,
            "type": "authority.proposal-eligible",
            "session_id": session.session_id,
            "payload": {
                "action": "write",
                "repository": "mncs-environment",
                "scope": {
                    "kind": "worktree",
                    "repository": "mncs-environment",
                    "branch": "campaign/unrelated",
                    "exclusive": False,
                },
                "authorization_basis": "session-writable-scope",
                "eligibility_basis": (
                    capability_intelligence.authority_module
                    .PROPOSAL_WORKTREE_ELIGIBILITY
                ),
                "persistent_claim_required": False,
                "operation_executed": False,
                "coordination_only_claim_count": 0,
                "coordination_only_claims": [],
                "coordination_only_claims_truncated": False,
                "campaign_id": "cmp_other_campaign",
                "work_intent_id": "int_environment_coordination_defer",
            },
        },
        {
            "identity": "evt_same_objective_proposal_eligible",
            "sequence": 44,
            "type": "authority.proposal-eligible",
            "session_id": session.session_id,
            "payload": {
                "action": "write",
                "repository": "mncs-environment",
                "scope": {
                    "kind": "worktree",
                    "repository": "mncs-environment",
                    "branch": "campaign/proposal",
                    "exclusive": False,
                },
                "authorization_basis": "session-writable-scope",
                "eligibility_basis": (
                    capability_intelligence.authority_module
                    .PROPOSAL_WORKTREE_ELIGIBILITY
                ),
                "persistent_claim_required": False,
                "operation_executed": False,
                "coordination_only_claim_count": 0,
                "coordination_only_claims": [],
                "coordination_only_claims_truncated": False,
                "campaign_id": "cmp_environment_coordination_defer",
                "work_intent_id": "int_environment_coordination_defer",
            },
        },
    ])
    matching_preflight_reference = {
        "identity": "evt_same_objective_proposal_eligible",
        "sequence": 44,
        "type": "authority.proposal-eligible",
    }
    matching_effect_target = {
        "repository": "mncs-environment",
        "branch": "campaign/proposal",
        "scope_kind": "worktree",
        "checkout_identity": "checkout:proposal",
        "preflight_event": matching_preflight_reference,
    }
    events.extend([
        {
            "identity": "evt_same_objective_provider_invocation",
            "session_id": session.session_id,
            "sequence": 45,
            "type": "capability.invoked",
            "payload": {
                "capability": "mncs-environment:proposal-writer",
                "provider": "mncs-environment",
                "binding_id": "binding:proposal-writer",
                "contract_revision": "1",
                "provider_request_id": "req_coordination_proposal",
                "effect_target": matching_effect_target,
            },
        },
        {
            "identity": "evt_same_objective_provider_completion",
            "session_id": session.session_id,
            "sequence": 46,
            "type": "invocation.completed",
            "payload": {
                "capability": "mncs-environment:proposal-writer",
                "provider": "mncs-environment",
                "binding_id": "binding:proposal-writer",
                "contract_revision": "1",
                "provider_request_id": "req_coordination_proposal",
                "effect_target": matching_effect_target,
                "status": "ok",
                "returncode": 0,
            },
        },
    ])
    events.append({
        "identity": "evt_diagnostic_capture_deferred_47",
        "session_id": session.session_id,
        "sequence": 47,
        "type": "diagnostic.deferred",
        "causes": ["evt_coordination_deferred_42"],
        "payload": {
            "failure": "fixture.debug-probe::t-case-1",
            "reason": "coordination-required",
            "coordination": {
                "required_coordination": "non-overlapping-operation-scope-or-isolated-worktree",
                "coordination_status": "overlapping-scope-conflict",
                "next_step": "select-an-exact-non-overlapping-operation-scope",
                "authority_event_reference": {
                    "identity": "evt_coordination_deferred_42",
                    "sequence": 42,
                    "type": "authority.coordination-deferred",
                },
            },
        },
    })
    session.recent_events = lambda *, limit, before_sequence=None: {
        "event_count": len(events), "events": events[-limit:],
        "truncated": False, "next_before_sequence": None,
    }

    activity = capability_intelligence.project(session)["session_activity"]
    assert activity["source"]["coordination_deferral_events_considered"] == 3
    assert activity["source"]["coordination_deferral_records_invalid"] == 0
    assert activity["coordination_deferral_count"] == 3
    deferred = activity["coordination_deferrals"][0]
    assert deferred["action"] == "write"
    assert deferred["work_intent_id"] == "int_environment_coordination_defer"
    assert deferred["persistent_authority_required"] is False
    assert deferred["operation_executed"] is False
    assert deferred["event_reference"]["type"] == (
        "authority.coordination-deferred"
    )
    capture_relation = deferred["diagnostic_capture_relation"]
    assert capture_relation["status"] == (
        "causally-linked-diagnostic-deferral-observed"
    )
    assert capture_relation["count"] == 1
    assert capture_relation["observations"][0]["failure"] == (
        "fixture.debug-probe::t-case-1"
    )
    assert capture_relation["observations"][0]["event_reference"][
        "identity"
    ] == "evt_diagnostic_capture_deferred_47"
    no_capture_relation = next(
        item["diagnostic_capture_relation"]
        for item in activity["coordination_deferrals"]
        if item["event_reference"]["identity"] == "evt_coordination_deferred_41"
    )
    assert no_capture_relation["status"] == (
        "no-causally-linked-diagnostic-deferral-in-session-history"
    )
    partial_capture_relation = (
        capability_intelligence._coordination_diagnostic_relation(
            [events[0]], deferred["event_reference"],
            event_window_truncated=True,
        )
    )
    assert partial_capture_relation["status"] == (
        "not-observed-in-selected-window"
    )
    follow_through = deferred["later_eligibility_relation"]
    assert follow_through["status"] == (
        "later-same-objective-eligibility-observed"
    )
    assert follow_through["matching_preflight_count"] == 1
    assert follow_through["matches"][0]["kind"] == "proposal-eligibility"
    assert follow_through["matches"][0]["event_reference"]["identity"] == (
        "evt_same_objective_proposal_eligible"
    )
    assert follow_through["matches"][0]["operation_executed"] is False
    assert follow_through["causal_status"] == "not-established"
    invocation_observation = follow_through["matches"][0][
        "provider_invocation_observation"
    ]
    assert invocation_observation["status"] == "matched-provider-invocation"
    assert invocation_observation["outcome_status"] == "ok"
    assert invocation_observation["invocation_event"]["identity"] == (
        "evt_same_objective_provider_invocation"
    )
    assert invocation_observation["completion_event"]["identity"] == (
        "evt_same_objective_provider_completion"
    )
    assert invocation_observation["project_change_status"] == (
        "not-measured-by-invocation-events"
    )
    assert activity["authority_denial_count"] == 0
    candidate = activity["coordination_deferral_candidates"][0]
    assert candidate["status"] == "candidate-needs-investigation"
    assert candidate["occurrence_count"] == 2
    assert candidate["recurrence_basis"] == (
        "same-repository-action-prerequisite-and-requested-scope"
    )
    assert candidate["next_step"] == (
        "select-or-create-an-observed-proposal-worktree"
    )
    assert {
        reference["identity"] for reference in candidate["supporting_events"]
    } == {
        "evt_coordination_deferred_41", "evt_coordination_deferred_42",
    }
    assert candidate["causal_claim"] == "not-established"
    assert candidate["later_eligibility_summary"][
        "deferrals_with_later_preflight_count"
    ] == 2
    assert candidate["later_eligibility_summary"][
        "operation_execution_status"
    ] == "not-recorded-by-eligibility-events"
    assert candidate["later_eligibility_summary"][
        "preflight_linked_provider_invocation_count"
    ] == 1
    assert candidate["later_eligibility_summary"][
        "provider_invocation_outcome_counts"
    ] == {"ok": 1}
    assert private_checkout not in json.dumps(activity)
    assert "private-checkout-detail" not in json.dumps(activity)

    compact = capability_intelligence.project(
        session, max_result_bytes=12_000,
    )["session_activity"]
    assert any(
        item["event_reference"]["identity"] == "evt_coordination_deferred_42"
        for item in compact["coordination_deferrals"]
    )
    assert compact["coordination_deferral_candidates"][0][
        "causal_claim"
    ] == "not-established"
    assert compact["coordination_deferral_candidates"][0]["next_step"] == (
        "select-or-create-an-observed-proposal-worktree"
    )
    assert compact["coordination_deferrals"][0][
        "later_eligibility_status"
    ] == "later-same-objective-eligibility-observed"
    compact_capture_relation = next(
        item["diagnostic_capture_relation"]
        for item in compact["coordination_deferrals"]
        if item["event_reference"]["identity"] == "evt_coordination_deferred_42"
    )
    assert compact_capture_relation["observations"][0][
        "event_reference"
    ]["identity"] == "evt_diagnostic_capture_deferred_47"
    compact_summary = compact["coordination_deferral_candidates"][0][
        "later_eligibility_summary"
    ]
    compact_provider_event = compact_summary["provider_invocation_events"][0]
    assert compact_provider_event["outcome_status"] == "ok"
    assert compact_provider_event["completion_event"]["identity"] == (
        "evt_same_objective_provider_completion"
    )
    assert compact_summary["supporting_preflight_events"][0][
        "event_reference"
    ]["identity"] == (
        "evt_same_objective_proposal_eligible"
    )
    assert compact["coordination_deferral_candidates"][0][
        "later_eligibility_summary"
    ]["deferrals_with_later_preflight_count"] == 2
    assert compact["coordination_deferral_candidates"][0][
        "later_eligibility_summary"
    ]["preflight_linked_provider_invocation_count"] == 1
    assert private_checkout not in json.dumps(compact)


def test_coordination_deferral_followthrough_requires_exact_later_objective() -> None:
    deferral = {
        "repository": "mncs-environment",
        "action": "write",
        "campaign_id": "cmp_campaign",
        "work_intent_id": "int_objective",
        "event_reference": {"identity": "evt_defer", "sequence": 10},
    }

    def eligibility(identity: str, sequence: int, **changes: object) -> dict:
        return {
            "repository": "mncs-environment",
            "action": "write",
            "campaign_id": "cmp_campaign",
            "work_intent_id": "int_objective",
            "event_reference": {"identity": identity, "sequence": sequence},
            **changes,
        }

    unrelated_or_earlier = [
        eligibility("evt_before", 9),
        eligibility("evt_other_campaign", 11, campaign_id="cmp_other"),
        eligibility("evt_other_intent", 12, work_intent_id="int_other"),
        eligibility("evt_other_action", 13, action="merge"),
        eligibility("evt_other_repository", 14, repository="mncs-compiler"),
    ]
    relation = capability_intelligence._coordination_deferral_follow_through(
        deferral, integration_records=[], proposal_records=unrelated_or_earlier,
    )
    assert relation["status"] == (
        "no-later-match-in-returned-eligibility-records"
    )
    assert relation["matching_preflight_count"] == 0
    assert relation["causal_status"] == "not-established"

    later = eligibility("evt_same_objective_later", 15)
    relation = capability_intelligence._coordination_deferral_follow_through(
        deferral, integration_records=[later], proposal_records=unrelated_or_earlier,
    )
    assert relation["status"] == "later-same-objective-eligibility-observed"
    assert relation["matches"][0]["event_reference"]["identity"] == (
        "evt_same_objective_later"
    )
    without_intent = {
        **deferral,
        "work_intent_id": None,
    }
    relation = capability_intelligence._coordination_deferral_follow_through(
        without_intent, integration_records=[later], proposal_records=[],
    )
    assert relation["status"] == "unavailable-exact-objective-reference"
    assert relation["matches"] == []


def test_coordination_deferral_links_exact_blocker_session_lifecycle(
    tmp_path: Path,
) -> None:
    record = _observation(
        "ev_environment_coordination_blocker_lifecycle",
        "ses_environment_coordination_blocker_lifecycle",
        "cmp_environment_coordination_blocker_lifecycle",
    )
    session, _ = _session(tmp_path, [record])
    claim_id = "claim:mncs-environment:paths:coordination"
    owner_session_id = "ses_coordination_blocker"
    repository_scope = {
        "kind": "paths",
        "repository": "mncs-environment",
        "paths": ["mncs_env/sessions.py"],
        "exclusive": False,
    }
    event = {
        "identity": "evt_coordination_blocker_deferred",
        "sequence": 12,
        "type": "authority.coordination-deferred",
        "session_id": session.session_id,
        "payload": {
            "action": "write",
            "repository": "mncs-environment",
            "required_coordination": "non-overlapping-operation-scope",
            "coordination_status": "overlapping-scope-conflict",
            "next_step": "select-an-exact-non-overlapping-operation-scope",
            "decision_domain": "coordination",
            "persistent_authority_required": False,
            "operation_executed": False,
            "campaign_id": session.snapshot["campaign"]["identity"],
            "work_intent_id": session.snapshot["intent"]["identity"],
            "scope": repository_scope,
            "claim_conflict": {
                "schema_version": (
                    capability_intelligence.authority_module.CLAIM_CONFLICT_SCHEMA
                ),
                "repository": "mncs-environment",
                "conflict_reason": "repository-relative paths overlap",
                "requested_scope": repository_scope,
                "blocking_claim": {
                    "claim_id": claim_id,
                    "session_id": owner_session_id,
                    "scope": repository_scope,
                },
            },
        },
    }
    owner_event = {
        "identity": "evt_coordination_blocker_released",
        "sequence": 8,
        "type": "lease.released",
        "payload": {
            "repository": "mncs-environment",
            "claim_id": claim_id,
            "released": [claim_id],
        },
    }
    owner_page_requests = []
    claim_time = claims.utcnow()
    current_claim = {
        "schema_version": claims.SCHEMA,
        "claim_id": claim_id,
        "version": 2,
        "repository": "mncs-environment",
        "scope": repository_scope,
        "session_id": owner_session_id,
        "consumer_id": "consumer_coordination_projection",
        "basis": claims.BASIS_EXPLICIT,
        "reason": "projection fixture",
        "status": "released",
        "acquired_at": claim_time,
        "expires_at": claim_time,
        "released_at": claim_time,
        "identity": "clm_coordination_released_v2",
    }

    def read_owner_page(session_id, *, limit, before_sequence=None):
        owner_page_requests.append(session_id)
        assert session_id == owner_session_id
        return {
            "events": [owner_event][-limit:],
            "event_count": 1,
            "truncated": False,
            "next_before_sequence": None,
        }

    session.store = SimpleNamespace(
        read_latest_claims_for=lambda claim_ids: (
            [current_claim] if claim_id in claim_ids else []
        ),
        read_event_page=read_owner_page,
        load_snapshot=lambda session_id: {
            "session_id": session_id, "lifecycle": "active",
        },
    )
    session.recent_events = lambda *, limit, before_sequence=None: {
        "event_count": 1, "events": [event],
        "truncated": False, "next_before_sequence": None,
    }

    activity = capability_intelligence.project(session)["session_activity"]
    deferral = activity["coordination_deferrals"][0]
    correlation = activity["claim_lifecycle_correlations"][0]
    assert correlation["owner_session_id"] == owner_session_id
    assert correlation["claim_id"] == claim_id
    assert correlation["claim_lifecycle_events"][0]["event_reference"][
        "identity"
    ] == "evt_coordination_blocker_released"
    assert correlation["related_session_activity"][0]["kind"] == (
        "coordination-deferral"
    )
    assert deferral["claim_conflict"]["blocking_claim"][
        "claim_lifecycle_correlation_identity"
    ] == correlation["identity"]
    current_state = deferral["claim_conflict"]["current_claim_state"]
    assert current_state["basis"] == "current-authoritative-claim-store"
    assert current_state["record_status"] == "released"
    assert current_state["blocking_status"] == "does-not-block-at-projection-time"
    guidance = deferral["claim_conflict"]["coordination_guidance"]
    assert guidance["status"] == "claim-no-longer-blocks"
    assert guidance["next_step"] == "refresh-operation-scope-and-retry"
    assert guidance["claim_mutated"] is False
    assert activity["source"]["coordination_claim_state_lookup"][
        "lookup_count"
    ] == 1
    assert owner_page_requests == [owner_session_id]

    compact = capability_intelligence.project(
        session, max_result_bytes=12_000,
    )["session_activity"]
    compact_deferral = compact["coordination_deferrals"][0]
    compact_correlation = compact["claim_lifecycle_correlations"][0]
    assert compact_deferral["claim_conflict"]["blocking_claim"][
        "claim_lifecycle_correlation_identity"
    ] == compact_correlation["identity"]
    assert compact_deferral["claim_conflict"]["current_claim_state"][
        "blocking_status"
    ] == "does-not-block-at-projection-time"
    assert compact_deferral["claim_conflict"]["coordination_guidance"][
        "next_step"
    ] == "refresh-operation-scope-and-retry"
    assert compact_correlation["related_session_activity"][0]["kind"] == (
        "coordination-deferral"
    )


def test_claimless_proposal_worktree_preflight_is_projected_with_objective_context(
    tmp_path: Path,
) -> None:
    record = _observation(
        "ev_environment_proposal_worktree_preflight",
        "ses_proposal_preflight",
        "cmp_proposal_preflight",
    )
    session, _ = _session(tmp_path, [record])
    campaign_id = session.snapshot["campaign"]["identity"]
    work_intent_id = session.snapshot["intent"]["identity"]
    repository = "mncs-environment"
    event = {
        "identity": "evt_proposal_eligible_7",
        "sequence": 7,
        "type": "authority.proposal-eligible",
        "payload": {
            "action": "write",
            "repository": repository,
            "scope": {
                "kind": "worktree",
                "repository": repository,
                "branch": "campaign/proposal",
                "checkout_identity": "checkout:proposal",
                "git_common_directory_identity": "git-common:mncs-environment",
                "paths": ["mncs_env/authority.py"],
                "exclusive": False,
            },
            "authorization_basis": "session-writable-scope",
            "eligibility_basis": (
                capability_intelligence.authority_module.PROPOSAL_WORKTREE_ELIGIBILITY
            ),
            "persistent_claim_required": False,
            "operation_executed": False,
            "coordination_only_claim_count": 3,
            "coordination_only_claims": [{
                "session_id": "ses_repository_coordinator",
                "claim_id": "claim:mncs-environment",
                "scope": {
                    "kind": "repository",
                    "repository": repository,
                    "exclusive": True,
                },
                "disposition": "coordination-only-broad-claim",
            }, {
                "session_id": "ses_path_coordinator",
                "claim_id": "claim:mncs-environment:paths:shared",
                "scope": {
                    "kind": "paths",
                    "repository": repository,
                    "paths": ["mncs_env/authority.py"],
                    "exclusive": False,
                },
                "disposition": "coordination-only-repository-path-claim",
            }, {
                "session_id": "ses_shared_branch_editor",
                "claim_id": "claim:mncs-environment:worktree:shared",
                "scope": {
                    "kind": "worktree",
                    "repository": repository,
                    "branch": "campaign/proposal",
                    "checkout_identity": "checkout:other-proposal",
                    "git_common_directory_identity": (
                        "git-common:mncs-environment"
                    ),
                    "paths": ["mncs_env/authority.py"],
                    "exclusive": False,
                },
                "disposition": (
                    "coordination-only-shared-branch-proposal"
                ),
                "overlap_reason": (
                    "shared branch with overlapping paths across checkouts"
                ),
            }],
            "coordination_only_claims_truncated": False,
            "proposal_preflight_state": {
                "schema_version": "mncs.environment.proposal-preflight-state/1",
                "observation_basis": "fresh-registered-checkout-inspection",
                "checkout_identity": "checkout:proposal",
                "branch": "campaign/proposal",
                "branch_status": "attached",
                "head": "e" * 40,
                "head_status": "measured",
                "working_tree_status": "dirty",
            },
            "campaign_id": campaign_id,
            "work_intent_id": work_intent_id,
        },
    }
    session.recent_events = lambda *, limit, before_sequence=None: {
        "event_count": 1,
        "events": [event][-limit:],
        "truncated": False,
        "next_before_sequence": None,
    }

    result = capability_intelligence.project(session)
    activity = result["session_activity"]
    assert activity["source"]["proposal_eligibility_records_returned"] == 1
    assert activity["source"]["proposal_preflight_state_observations_invalid"] == 0
    assert activity["proposal_eligibility_count"] == 1
    eligible = activity["proposal_eligibility"][0]
    assert eligible["action"] == "write"
    assert eligible["repository"] == repository
    assert eligible["campaign_id"] == campaign_id
    assert eligible["work_intent_id"] == work_intent_id
    assert eligible["persistent_claim_required"] is False
    assert eligible["operation_executed"] is False
    assert eligible["preflight_state_status"] == "recorded-and-validated"
    assert eligible["proposal_preflight_state"]["head"] == "e" * 40
    assert eligible["proposal_preflight_state"]["checkout_identity"] == (
        "checkout:proposal"
    )
    assert eligible["coordination_only_claim_count"] == 3
    assert eligible["coordination_only_claims"][0]["claim_id"] == (
        "claim:mncs-environment"
    )
    assert eligible["coordination_only_claims"][1]["scope"]["paths"] == [
        "mncs_env/authority.py",
    ]
    assert eligible["coordination_only_claims"][1]["disposition"] == (
        "coordination-only-repository-path-claim"
    )
    assert eligible["coordination_only_claims"][2]["disposition"] == (
        "coordination-only-shared-branch-proposal"
    )
    assert eligible["coordination_only_claims"][2]["overlap_reason"] == (
        "shared branch with overlapping paths across checkouts"
    )
    assert eligible["event_reference"]["identity"] == "evt_proposal_eligible_7"
    assert eligible["scope"]["branch"] == "campaign/proposal"

    compact = capability_intelligence._compact_projection(
        result, original_size=25_000, max_result_bytes=10_000,
    )
    compact_eligible = compact["session_activity"]["proposal_eligibility"][0]
    assert compact_eligible["persistent_claim_required"] is False
    assert compact_eligible["preflight_state_status"] == "recorded-and-validated"
    assert compact_eligible["proposal_preflight_state"]["head"] == "e" * 40
    assert compact_eligible["event_reference"]["identity"] == (
        "evt_proposal_eligible_7"
    )
    assert compact_eligible["coordination_only_claims"][0]["claim_id"] == (
        "claim:mncs-environment"
    )
    assert compact_eligible["coordination_only_claims"][1]["claim_id"] == (
        "claim:mncs-environment:paths:shared"
    )
    assert compact_eligible["coordination_only_claims"][2]["overlap_reason"] == (
        "shared branch with overlapping paths across checkouts"
    )

    event_without_preflight = json.loads(json.dumps(event))
    event_without_preflight["identity"] = "evt_proposal_eligible_8"
    event_without_preflight["payload"].pop("proposal_preflight_state")
    session.recent_events = lambda *, limit, before_sequence=None: {
        "event_count": 1,
        "events": [event_without_preflight][-limit:],
        "truncated": False,
        "next_before_sequence": None,
    }
    without_preflight = capability_intelligence.project(session)[
        "session_activity"
    ]["proposal_eligibility"][0]
    assert without_preflight["preflight_state_status"] == (
        "not-recorded-on-event"
    )

    event_with_invalid_preflight = json.loads(json.dumps(event))
    event_with_invalid_preflight["identity"] = "evt_proposal_eligible_9"
    event_with_invalid_preflight["payload"]["proposal_preflight_state"][
        "schema_version"
    ] = "mncs.environment.unknown-preflight-state/1"
    session.recent_events = lambda *, limit, before_sequence=None: {
        "event_count": 1,
        "events": [event_with_invalid_preflight][-limit:],
        "truncated": False,
        "next_before_sequence": None,
    }
    invalid_preflight_projection = capability_intelligence.project(session)[
        "session_activity"
    ]
    assert invalid_preflight_projection["source"][
        "proposal_preflight_state_observations_invalid"
    ] == 1
    assert invalid_preflight_projection["proposal_eligibility"][0][
        "preflight_state_status"
    ] == "invalid-event-observation"


def test_proposal_preflight_records_local_only_scope_without_repository_rights(
    tmp_path: Path,
) -> None:
    record = _observation(
        "ev_environment_proposal_only_scope",
        "ses_proposal_only_scope",
        "cmp_proposal_only_scope",
    )
    session, _ = _session(tmp_path, [record])
    event = {
        "identity": "evt_proposal_only_scope",
        "sequence": 12,
        "type": "authority.proposal-eligible",
        "session_id": session.session_id,
        "producer": "environment",
        "payload": {
            "action": "write",
            "repository": "mncs-language-service",
            "scope": {
                "kind": "worktree",
                "repository": "mncs-language-service",
                "branch": "campaign/proposal",
                "checkout_identity": "checkout:language-proposal",
                "exclusive": False,
            },
            "authorization_basis": (
                capability_intelligence.authority_module
                .PROPOSAL_ONLY_AUTHORIZATION_BASIS
            ),
            "eligibility_basis": (
                capability_intelligence.authority_module
                .PROPOSAL_WORKTREE_ELIGIBILITY
            ),
            "writable_scope_required_for_integration": True,
            "repository_writable_scope_present": False,
            "persistent_claim_required": False,
            "operation_executed": False,
            "coordination_only_claim_count": 0,
            "coordination_only_claims": [],
            "coordination_only_claims_truncated": False,
            "campaign_id": "cmp_proposal_only_scope",
            "work_intent_id": "int_proposal_only_scope",
        },
    }
    session.recent_events = lambda *, limit, before_sequence=None: {
        "event_count": 1,
        "events": [event][-limit:],
        "truncated": False,
        "next_before_sequence": None,
    }

    activity = capability_intelligence.project(session)["session_activity"]
    assert activity["source"]["proposal_eligibility_records_invalid"] == 0
    assert activity["proposal_eligibility_count"] == 1
    proposal = activity["proposal_eligibility"][0]
    assert proposal["authorization_basis"] == "isolated-proposal-scope"
    assert proposal["writable_scope_required_for_integration"] is True
    assert proposal["repository_writable_scope_present"] is False
    assert proposal["writable_scope_observation_status"] == "recorded"
    assert proposal["operation_executed"] is False
    assert "merge and publish still require writable repository scope" in proposal[
        "interpretation"
    ]
    compact = capability_intelligence._compact_projection(
        capability_intelligence.project(session),
        original_size=25_000,
        max_result_bytes=10_000,
    )
    compact_proposal = compact["session_activity"]["proposal_eligibility"][0]
    assert compact_proposal["writable_scope_required_for_integration"] is True
    assert compact_proposal["repository_writable_scope_present"] is False


def test_proposal_preflight_links_exact_provider_invocation_without_claiming_change(
    tmp_path: Path,
) -> None:
    record = _observation(
        "ev_environment_proposal_invocation_link",
        "ses_proposal_invocation_link",
        "cmp_proposal_invocation_link",
    )
    session, _ = _session(tmp_path, [record])
    proposal_reference = {
        "identity": "evt_proposal_invocation_preflight",
        "sequence": 10,
        "type": "authority.proposal-eligible",
    }
    request_id = "req_proposal_invocation_link"
    events = [
        {
            **proposal_reference,
            "session_id": session.session_id,
            "producer": "environment",
            "payload": {
                "action": "write",
                "repository": "mncs-language-service",
                "scope": {
                    "kind": "worktree",
                    "repository": "mncs-language-service",
                    "branch": "campaign/proposal",
                    "checkout_identity": "checkout:proposal",
                    "exclusive": False,
                },
                "authorization_basis": "isolated-proposal-scope",
                "eligibility_basis": (
                    capability_intelligence.authority_module
                    .PROPOSAL_WORKTREE_ELIGIBILITY
                ),
                "writable_scope_required_for_integration": True,
                "repository_writable_scope_present": False,
                "persistent_claim_required": False,
                "operation_executed": False,
                "coordination_only_claim_count": 0,
                "coordination_only_claims": [],
                "coordination_only_claims_truncated": False,
                "campaign_id": "cmp_proposal_invocation_link",
                "work_intent_id": "int_proposal_invocation_link",
            },
        },
        {
            "identity": "evt_proposal_invocation_started",
            "sequence": 11,
            "type": "capability.invoked",
            "producer": "local-agent",
            "payload": {
                "capability": "mncs-language-service:write",
                "provider_request_id": request_id,
                "effect_target": {
                    "kind": "worktree",
                    "repository": "mncs-language-service",
                    "branch": "campaign/proposal",
                    "checkout_identity": "checkout:proposal",
                    "preflight_event": proposal_reference,
                },
            },
        },
        {
            "identity": "evt_proposal_invocation_completed",
            "sequence": 12,
            "type": "invocation.completed",
            "producer": "mncs-language-service",
            "payload": {
                "provider_request_id": request_id,
                "status": "succeeded",
                "returncode": 0,
            },
        },
    ]
    session.recent_events = lambda *, limit, before_sequence=None: {
        "event_count": len(events),
        "events": events[-limit:],
        "truncated": False,
        "next_before_sequence": None,
    }

    result = capability_intelligence.project(session)
    activity = result["session_activity"]
    proposal = activity["proposal_eligibility"][0]
    observation = proposal["invocation_observation"]
    assert observation["status"] == "matched-provider-invocation"
    assert observation["relation_basis"] == (
        "exact-preflight-event-reference-and-repository"
    )
    assert observation["identity"] == activity["interactions"][0]["identity"]
    assert observation["capability"] == "mncs-language-service:write"
    assert observation["correlation_status"] == "matched-by-provider-request-id"
    assert observation["outcome_status"] == "succeeded"
    assert observation["returncode"] == 0
    assert observation["invocation_event"]["identity"] == (
        "evt_proposal_invocation_started"
    )
    assert observation["completion_event"]["identity"] == (
        "evt_proposal_invocation_completed"
    )
    assert observation["causal_status"] == "not-established"
    assert observation["project_change_status"] == (
        "not-measured-by-invocation-events"
    )

    compact = capability_intelligence._compact_projection(
        result, original_size=25_000, max_result_bytes=10_000,
    )
    compact_observation = compact["session_activity"][
        "proposal_eligibility"
    ][0]["invocation_observation"]
    assert compact_observation["status"] == "matched-provider-invocation"
    assert compact_observation["completion_event"]["identity"] == (
        "evt_proposal_invocation_completed"
    )
    assert compact_observation["causal_status"] == "not-established"

    session.recent_events = lambda *, limit, before_sequence=None: {
        "event_count": 1,
        "events": [events[0]],
        "truncated": False,
        "next_before_sequence": None,
    }
    no_invocation_result = capability_intelligence.project(session)
    no_invocation = no_invocation_result["session_activity"][
        "proposal_eligibility"
    ][0]["invocation_observation"]
    assert no_invocation["status"] == (
        "no-matching-environment-invocation-in-selected-window"
    )
    assert no_invocation["search_completeness_status"] == (
        "complete-for-selected-session-window"
    )
    assert no_invocation["causal_status"] == "not-established"
    compact_no_invocation = capability_intelligence._compact_projection(
        no_invocation_result, original_size=25_000, max_result_bytes=10_000,
    )["session_activity"]["proposal_eligibility"][0]
    assert compact_no_invocation["invocation_observation_status"] == (
        "no-match-complete-window"
    )


def test_proposal_invocation_link_keeps_conflicts_and_partial_windows() -> None:
    reference = {
        "identity": "evt_proposal_relation_bound",
        "sequence": 20,
        "type": "authority.proposal-eligible",
    }
    mismatched_repository = capability_intelligence._proposal_invocation_observation(
        reference,
        "mncs-language-service",
        [{
            "identity": "act_wrong_repository",
            "invocation_event": {
                "identity": "evt_other_repo_invoked",
                "sequence": 21,
                "type": "capability.invoked",
            },
            "effect_target": {
                "repository": "mncs-compiler",
                "preflight_event": reference,
            },
        }],
        event_window_truncated=False,
        invocation_records_omitted=0,
    )
    assert mismatched_repository["status"] == "preflight-reference-conflict"
    assert mismatched_repository["reference_conflict_count"] == 1
    assert mismatched_repository["causal_status"] == "not-established"

    partial = capability_intelligence._proposal_invocation_observation(
        reference,
        "mncs-language-service",
        [],
        event_window_truncated=True,
        invocation_records_omitted=0,
    )
    assert partial["status"] == (
        "invocation-not-established-in-partial-window"
    )
    assert partial["search_completeness_status"] == (
        "partial-selected-session-window"
    )
    assert partial["causal_status"] == "not-established"


def test_scoped_integration_preflight_projects_without_claim_or_success(
    tmp_path: Path,
) -> None:
    record = _observation(
        "ev_environment_unclaimed_integration_preflight",
        "ses_projection_reader",
        "cmp_projection_reader",
    )
    record["related_work"]["work_intent_id"] = "int_projection_reader"
    record["observation"]["outcome"]["event_reference"] = {
        "identity": "evt_integration_eligible",
        "sequence": 41,
        "type": "authority.integration-eligible",
    }
    record["supporting_sources"].append({
        "kind": "authenticated-session-activity-projection",
        "repository": "mncs-environment",
        "campaign_id": "cmp_projection_reader",
        "session_id": "ses_projection_reader",
        "event_identities": ["evt_integration_eligible"],
    })
    session, _ = _session(tmp_path, [record])
    checkout = str(
        Path(session.snapshot["selected_checkouts"]["mncs-environment"]["path"])
        / ".worktrees" / "integration-target"
    )
    events = [{
        "identity": "evt_integration_eligible",
        "sequence": 41,
        "type": "authority.integration-eligible",
        "session_id": session.session_id,
        "producer": "environment",
        "payload": {
            "action": "merge",
            "repository": "mncs-environment",
            "scope": {
                "kind": "worktree",
                "repository": "mncs-environment",
                "branch": "campaign/example",
                "exclusive": False,
            },
            "authorization_basis": (
                "writable-repository-rights"
            ),
            "eligibility_basis": (
                "exact-observed-scope-and-repository-native-concurrency"
            ),
            "persistent_claim_required": False,
            "operation_executed": False,
            "repository_preflight_state": {
                "schema_version": (
                    "mncs.environment.integration-preflight-state/1"
                ),
                "observation_basis": (
                    "fresh-registered-checkout-inspection"
                ),
                "checkout_identity": "checkout:integration-target",
                "branch": "campaign/example",
                "branch_status": "attached",
                "head": "c" * 40,
                "head_status": "measured",
                "working_tree_status": "clean",
            },
            "campaign_id": session.snapshot["campaign"]["identity"],
            "work_intent_id": session.snapshot["intent"]["identity"],
            "coordination_overlap_count": 5,
            "coordination_overlaps": [{
                "session_id": f"ses_other_coordinator_{index}",
                "claim_id": f"claim:mncs-environment:paths:sample{index}",
                "scope": {
                    "kind": "paths",
                    "repository": "mncs-environment",
                    "paths": [f"mncs_env/source_{index}.py"],
                    "exclusive": False,
                },
                "overlap_reason": "repository-relative paths overlap",
            } for index in range(5)],
            "coordination_overlaps_truncated": False,
            "target": checkout,
        },
    }]
    events.insert(0, {
        "identity": "evt_authority_denied_same_claim",
        "sequence": 40,
        "type": "authority.denied",
        "session_id": session.session_id,
        "payload": {
            "action": "merge",
            "target": checkout,
            "reason": (
                f"{checkout} overlaps scope claimed by another session "
                "(ses_other_coordinator_0): repository-relative paths overlap"
            ),
            "campaign_id": session.snapshot["campaign"]["identity"],
            "work_intent_id": session.snapshot["intent"]["identity"],
            "claim_conflict": {
                "schema_version": (
                    capability_intelligence.authority_module.CLAIM_CONFLICT_SCHEMA
                ),
                "repository": "mncs-environment",
                "conflict_reason": "repository-relative paths overlap",
                "requested_scope": {
                    "kind": "paths",
                    "repository": "mncs-environment",
                    "paths": ["mncs_env/source_0.py"],
                },
                "blocking_claim": {
                    "claim_id": "claim:mncs-environment:paths:sample0",
                    "session_id": "ses_other_coordinator_0",
                    "scope": {
                        "kind": "paths",
                        "repository": "mncs-environment",
                        "paths": ["mncs_env/source_0.py"],
                    },
                },
            },
        },
    })
    events.append({
        "identity": "evt_proposal_eligible_same_claim",
        "sequence": 42,
        "type": "authority.proposal-eligible",
        "session_id": session.session_id,
        "payload": {
            "action": "write",
            "repository": "mncs-environment",
            "scope": {
                "kind": "worktree",
                "repository": "mncs-environment",
                "branch": "campaign/proposal",
                "checkout_identity": "checkout:proposal",
                "paths": ["mncs_env/source_0.py"],
                "exclusive": False,
            },
            "authorization_basis": "session-writable-scope",
            "eligibility_basis": (
                capability_intelligence.authority_module
                .PROPOSAL_WORKTREE_ELIGIBILITY
            ),
            "persistent_claim_required": False,
            "operation_executed": False,
            "coordination_only_claim_count": 1,
            "coordination_only_claims": [{
                "session_id": "ses_other_coordinator_0",
                "claim_id": "claim:mncs-environment:paths:sample0",
                "scope": {
                    "kind": "paths",
                    "repository": "mncs-environment",
                    "paths": ["mncs_env/source_0.py"],
                    "exclusive": False,
                },
                "disposition": "coordination-only-repository-path-claim",
            }],
            "coordination_only_claims_truncated": False,
            "campaign_id": session.snapshot["campaign"]["identity"],
            "work_intent_id": session.snapshot["intent"]["identity"],
        },
    })
    sample_claim_id = "claim:mncs-environment:paths:sample0"
    sample_claim_time = claims.utcnow()
    sample_claim = {
        "schema_version": claims.SCHEMA,
        "claim_id": sample_claim_id,
        "version": 2,
        "repository": "mncs-environment",
        "scope": {
            "kind": "paths",
            "repository": "mncs-environment",
            "paths": ["mncs_env/source_0.py"],
            "exclusive": False,
        },
        "session_id": "ses_other_coordinator_0",
        "consumer_id": "consumer_projection_sample",
        "basis": claims.BASIS_EXPLICIT,
        "reason": "projection fixture",
        "status": "released",
        "acquired_at": sample_claim_time,
        "expires_at": sample_claim_time,
        "released_at": sample_claim_time,
        "identity": "clm_projection_sample_v2",
    }
    claim_lookup_batches = []
    owner_event_page_requests = []
    owner_claim_id = "claim:mncs-environment:paths:sample0"
    owner_events = {
        "ses_other_coordinator_0": [
            {
                "identity": "evt_owner_claim_acquired",
                "sequence": 2,
                "type": "lease.acquired",
                "payload": {
                    "repository": "mncs-environment",
                    "basis": claims.BASIS_EXPLICIT,
                    "claim_identity": owner_claim_id,
                    "claim_version": 1,
                    "scope": {
                        "kind": "paths",
                        "repository": "mncs-environment",
                        "paths": ["mncs_env/source_0.py"],
                        "exclusive": False,
                    },
                    "campaign_id": "cmp_owner_campaign",
                    "work_intent_id": "int_owner_work",
                    "reason": "must remain outside the projection",
                },
            },
            {
                "identity": "evt_owner_claim_released",
                "sequence": 3,
                "type": "lease.released",
                "payload": {
                    "repository": "mncs-environment",
                    "claim_id": owner_claim_id,
                    "released": 1,
                    "campaign_id": "cmp_owner_campaign",
                    "work_intent_id": "int_owner_work",
                    "reason": "must remain outside the projection",
                },
            },
        ],
    }

    def read_owner_event_page(owner_session_id, *, limit, before_sequence=None):
        owner_event_page_requests.append(owner_session_id)
        selected = owner_events.get(owner_session_id, [])[-limit:]
        return {
            "events": selected,
            "event_count": len(owner_events.get(owner_session_id, [])),
            "truncated": False,
            "next_before_sequence": None,
        }

    def read_latest_claims_for(claim_ids):
        claim_lookup_batches.append(list(claim_ids))
        return [sample_claim] if sample_claim_id in claim_ids else []

    session.store = SimpleNamespace(
        read_latest_claims_for=read_latest_claims_for,
        read_event_page=read_owner_event_page,
        load_snapshot=lambda owner_session_id: {
            "session_id": owner_session_id, "lifecycle": "active",
        },
    )
    session.recent_events = lambda *, limit, before_sequence=None: {
        "event_count": len(events), "events": events[-limit:],
        "truncated": False, "next_before_sequence": None,
    }

    result = capability_intelligence.project(session)
    activity = result["session_activity"]
    projected_observation = next(
        item for item in result["observations"]
        if item["identity"] == record["identity"]
    )
    relation = projected_observation["session_activity_relation"]
    assert relation["status"] == "matched-exact-session-activity"
    assert relation["activity_kind"] == "integration_eligibility"
    assert relation["activity_record_identity"] == activity[
        "integration_eligibility"
    ][0]["identity"]
    assert relation["event_reference"] == {
        "identity": "evt_integration_eligible",
        "sequence": 41,
        "type": "authority.integration-eligible",
    }
    assert relation["activity_event_reference"] == relation["event_reference"]
    assert relation["work_context_status"] == (
        "campaign-and-work-intent-matched"
    )
    assert relation["interpretation_limit"].startswith(
        "The event reference links recorded evidence"
    )
    compact = capability_intelligence._compact_projection(
        result, 100_000, 50_000,
    )
    compact_observation = next(
        item for item in compact["observations"]
        if item["identity"] == record["identity"]
    )
    assert compact_observation["session_activity_relation"][
        "event_reference"
    ] == relation["event_reference"]
    assert compact_observation["session_activity_relation"][
        "activity_kind"
    ] == "integration_eligibility"

    mismatched_record = {
        **record,
        "observation": {
            **record["observation"],
            "outcome": {
                **record["observation"]["outcome"],
                "event_reference": {
                    "identity": "evt_integration_eligible",
                    "sequence": 40,
                    "type": "authority.integration-eligible",
                },
            },
        },
    }
    mismatched_relation = (
        capability_intelligence._observation_session_activity_relation(
            mismatched_record, activity,
            selected_session_id=session.session_id,
        )
    )
    assert mismatched_relation["status"] == "event-reference-mismatch"
    assert mismatched_relation["activity_event_reference"]["sequence"] == 41
    wrong_session_record = {
        **record,
        "related_work": {
            **record["related_work"],
            "session_id": "ses_another_session",
        },
    }
    assert capability_intelligence._observation_session_activity_relation(
        wrong_session_record, activity,
        selected_session_id=session.session_id,
    )["status"] == "source-session-not-selected"
    no_match_record = {
        **record,
        "observation": {
            **record["observation"],
            "outcome": {
                **record["observation"]["outcome"],
                "event_reference": {
                    "identity": "evt_not_in_selected_page",
                    "sequence": 2,
                    "type": "authority.integration-eligible",
                },
            },
        },
    }
    bounded_activity = {
        **activity,
        "source": {
            **activity["source"],
            "event_window_truncated": True,
            "event_page_scoped": False,
        },
    }
    assert capability_intelligence._observation_session_activity_relation(
        no_match_record, bounded_activity,
        selected_session_id=session.session_id,
    )["status"] == "not-observed-in-selected-session-event-window"
    assert capability_intelligence._observation_session_activity_relation(
        no_match_record, {"status": "partial", "source": {}},
        selected_session_id=session.session_id,
    )["activity_history_completeness"] == "unknown"
    eligibility = activity["integration_eligibility"][0]
    assert activity["integration_eligibility_count"] == 1
    assert eligibility["action"] == "merge"
    assert eligibility["authorization_basis"] == (
        "writable-repository-rights"
    )
    assert eligibility["eligibility_basis"] == (
        "exact-observed-scope-and-repository-native-concurrency"
    )
    assert eligibility["persistent_claim_required"] is False
    assert eligibility["operation_executed"] is False
    assert eligibility["preflight_state_status"] == "recorded-and-validated"
    assert eligibility["repository_preflight_state"]["checkout_identity"] == (
        "checkout:integration-target"
    )
    assert eligibility["repository_preflight_state"]["head"] == "c" * 40
    assert "does not establish source branch, remote ref state" in (
        eligibility["preflight_state_interpretation_limit"]
    )
    assert eligibility["campaign_id"] == session.snapshot["campaign"]["identity"]
    assert eligibility["work_intent_id"] == session.snapshot["intent"]["identity"]
    assert eligibility["work_intent_reference_status"] == "recorded-on-event"
    assert eligibility["coordination_overlap_count"] == 5
    assert eligibility["coordination_overlaps"][0]["session_id"] == (
        "ses_other_coordinator_0"
    )
    current_claim_state = eligibility["coordination_overlaps"][0][
        "current_claim_state"
    ]
    assert current_claim_state["basis"] == "current-authoritative-claim-store"
    assert current_claim_state["record_status"] == "released"
    assert current_claim_state["blocking_status"] == (
        "does-not-block-at-projection-time"
    )
    assert activity["authority_denial_count"] == 1
    denial = activity["authority_denials"][0]
    assert denial["campaign_id"] == session.snapshot["campaign"]["identity"]
    assert denial["work_intent_id"] == session.snapshot["intent"]["identity"]
    assert denial["work_intent_reference_status"] == "recorded-on-event"
    denial_followup = activity["authority_denials"][0]["claim_conflict"][
        "subsequent_integration_preflight"
    ]
    assert denial["claim_conflict"]["decision_claim_state_status"] == (
        "not-recorded-on-event"
    )
    assert "decision_claim_state" not in denial["claim_conflict"]
    assert denial_followup["event_reference"]["identity"] == (
        "evt_integration_eligible"
    )
    assert denial_followup["relation_basis"] == (
        "later-event-same-repository-action-and-claim-identity"
    )
    assert denial_followup["causal_resolution_status"] == "not-established"
    assert denial_followup["operation_executed"] is False
    integration_guidance = denial["claim_conflict"]["coordination_guidance"]
    assert integration_guidance["status"] == "integration-preflight-can-be-retried"
    assert integration_guidance["next_step"] == (
        "repeat-exact-scope-integration-preflight"
    )
    assert integration_guidance["ownership_transfer"] == (
        "not-required-by-current-integration-policy"
    )
    assert integration_guidance["claim_mutated"] is False
    assert denial_followup["selection_basis"] == (
        "earliest-matching-returned-record-in-selected-session-window"
    )
    assert denial_followup["search_completeness_status"] == (
        "complete-for-returned-records"
    )
    correlations = activity["claim_lifecycle_correlations"]
    sample_zero_correlation = correlations[0]
    assert sample_zero_correlation["claim_id"] == sample_claim_id
    assert sample_zero_correlation["owner_session_id"] == (
        "ses_other_coordinator_0"
    )
    assert sample_zero_correlation["status"] == (
        "matching-events-in-owner-history"
    )
    assert sample_zero_correlation["causal_resolution_status"] == (
        "not-established"
    )
    assert [
        item["action"] for item in
        sample_zero_correlation["claim_lifecycle_events"]
    ] == ["released", "acquired"]
    assert [
        item["kind"] for item in
        sample_zero_correlation["related_session_activity"]
    ] == [
        "authority-denial", "integration-eligibility", "proposal-eligibility",
    ]
    assert denial["claim_conflict"]["blocking_claim"][
        "claim_lifecycle_correlation_identity"
    ] == sample_zero_correlation["identity"]
    assert eligibility["coordination_overlaps"][0][
        "claim_lifecycle_correlation_identity"
    ] == sample_zero_correlation["identity"]
    proposal_eligibility = activity["proposal_eligibility"][0]
    proposal_claim = proposal_eligibility["coordination_only_claims"][0]
    assert proposal_claim["claim_lifecycle_correlation_identity"] == (
        sample_zero_correlation["identity"]
    )
    assert proposal_eligibility["event_reference"]["identity"] == (
        "evt_proposal_eligible_same_claim"
    )
    assert owner_event_page_requests.count("ses_other_coordinator_0") == 1
    assert "must remain outside" not in json.dumps(sample_zero_correlation)
    assert activity["source"]["authority_claim_state_lookup"][
        "lookup_count"
    ] == 1
    claim_lookup = activity["source"]["integration_claim_state_lookup"]
    assert claim_lookup["lookup_count"] == 3
    assert claim_lookup["lookup_limit"] == 3
    assert claim_lookup["lookup_omitted_count"] == 1
    assert claim_lookup["activity_lookup_count"] == 4
    assert claim_lookup["prior_activity_lookup_count"] == 1
    assert claim_lookup["reused_activity_lookup_count"] == 1
    assert [len(batch) for batch in claim_lookup_batches] == [1, 3]
    assert sample_claim_id not in claim_lookup_batches[1]
    assert eligibility["event_reference"]["identity"] == (
        "evt_integration_eligible"
    )
    assert checkout not in json.dumps(result)
    source = next(
        item for item in result["observations"][0]["supporting_sources"]
        if item.get("kind") == "authenticated-session-activity-projection"
    )
    assert source["event_reference_status"] == "all-referenced-events-present"
    assert source["event_reference_match_categories"] == [
        "integration_eligibility",
    ]
    assert any(
        "preflight decisions" in limit
        for limit in activity["measurement_limits"]
    )

    compact = capability_intelligence.project(session, max_result_bytes=15_000)
    compact_eligibility = compact["session_activity"][
        "integration_eligibility"
    ][0]
    assert compact_eligibility["event_reference"]["identity"] == (
        "evt_integration_eligible"
    )
    assert compact_eligibility["operation_executed"] is False
    assert compact_eligibility["preflight_state_status"] == (
        "recorded-and-validated"
    )
    assert compact_eligibility["repository_preflight_state"]["head"] == "c" * 40
    assert compact_eligibility["work_intent_id"] == session.snapshot["intent"]["identity"]
    compact_denial = compact["session_activity"]["authority_denials"][0]
    assert compact_denial["work_intent_reference_status"] == "recorded-on-event"
    compact_correlation = compact["session_activity"][
        "claim_lifecycle_correlations"
    ][0]
    assert compact["session_activity"][
        "claim_lifecycle_correlations_returned"
    ] == 1
    assert compact["session_activity"][
        "claim_lifecycle_correlations_omitted_count"
    ] == 4
    assert compact_correlation["claim_lifecycle_events"][0][
        "event_reference"]["identity"] == "evt_owner_claim_released"
    assert compact_correlation["claim_lifecycle_events_projection_truncated"] is True
    assert compact_correlation[
        "claim_lifecycle_events_projection_omitted_count"
    ] == 1
    assert compact_correlation["owner_event_page"] == {
        "status": "read", "truncated": False,
        "limit": capability_intelligence.MAX_SESSION_CLAIM_OWNER_EVENT_PAGE,
    }
    assert [item["kind"] for item in compact_correlation[
        "related_session_activity"
    ]] == ["authority-denial", "integration-eligibility", "proposal-eligibility"]
    assert compact["session_activity"]["integration_eligibility"][0][
        "coordination_overlaps"][0]["claim_lifecycle_correlation_identity"] == (
            compact_correlation["identity"]
        )
    compact_proposal = compact["session_activity"]["proposal_eligibility"][0]
    assert compact_proposal["coordination_only_claims"][0][
        "claim_lifecycle_correlation_identity"
    ] == compact_correlation["identity"]
    assert compact_eligibility["coordination_overlap_count"] == 5
    assert compact_eligibility["coordination_overlap_records_returned"] == 4
    assert compact_eligibility[
        "coordination_overlaps_projection_truncated"
    ] is True
    assert not compact_eligibility.get(
        "coordination_overlap_details_projection_truncated", False,
    )
    assert all(
        "scope" in overlap and "overlap_reason" in overlap
        for overlap in compact_eligibility["coordination_overlaps"]
    )
    assert compact_eligibility["coordination_overlaps"][0]["claim_id"] == (
        "claim:mncs-environment:paths:sample0"
    )
    compact_claim_state = compact_eligibility["coordination_overlaps"][0].get(
        "current_claim_state",
    )
    assert compact_claim_state["blocking_status"] == (
        "does-not-block-at-projection-time"
    )
    assert compact_eligibility[
        "current_claim_states_projection_truncated"
    ] is True
    assert compact_eligibility[
        "current_claim_states_projection_omitted_count"
    ] == 3
    compact_source = compact["session_activity"]["source"]
    assert compact_source["integration_claim_state_lookup"][
        "lookup_omitted_count"
    ] == 1
    assert checkout not in json.dumps(compact)
    assert len(json.dumps(compact, ensure_ascii=False).encode("utf-8")) <= 15_000


def test_repository_ref_followup_matches_exact_preflight_without_claiming_cause(
    tmp_path: Path,
) -> None:
    record = _observation(
        "ev_environment_ref_transition_followup",
        "ses_projection_reader",
        "cmp_projection_reader",
    )
    session, _ = _session(tmp_path, [record])
    repository = "mncs-environment"
    checkout_identity = "checkout:observed-target"
    branch = "campaign/observed-target"
    before_head = "c" * 40
    after_head = "d" * 40
    session.recent_events = lambda *, limit, before_sequence=None: {
        "event_count": 2,
        "events": [
            {
                "identity": "evt_ref_transition_preflight",
                "sequence": 41,
                "type": "authority.integration-eligible",
                "session_id": session.session_id,
                "payload": {
                    "action": "merge",
                    "repository": repository,
                    "scope": {
                        "kind": "worktree",
                        "repository": repository,
                        "branch": branch,
                        "exclusive": False,
                    },
                    "authorization_basis": "writable-repository-rights",
                    "eligibility_basis": (
                        "exact-observed-scope-and-repository-native-concurrency"
                    ),
                    "persistent_claim_required": False,
                    "operation_executed": False,
                    "repository_preflight_state": {
                        "schema_version": (
                            "mncs.environment.integration-preflight-state/1"
                        ),
                        "observation_basis": (
                            "fresh-registered-checkout-inspection"
                        ),
                        "checkout_identity": checkout_identity,
                        "branch": branch,
                        "branch_status": "attached",
                        "head": before_head,
                        "head_status": "measured",
                        "working_tree_status": "clean",
                    },
                    "campaign_id": session.snapshot["campaign"]["identity"],
                    "work_intent_id": session.snapshot["intent"]["identity"],
                    "coordination_overlap_count": 0,
                    "coordination_overlaps": [],
                    "coordination_overlaps_truncated": False,
                },
            },
            {
                "identity": "evt_ref_transition_observed",
                "sequence": 42,
                "type": "campaign.reconciled",
                "session_id": session.session_id,
                "payload": {
                    "campaign_id": session.snapshot["campaign"]["identity"],
                    "work_intent_id": session.snapshot["intent"]["identity"],
                    "repository_refs_changed": True,
                    "repository_ref_transition_count": 1,
                    "repository_ref_transitions_omitted": 0,
                    "repository_ref_transitions": [{
                        "schema_version": (
                            "mncs.environment.repository-ref-transition/1"
                        ),
                        "repository": repository,
                        "observed_path": (
                            "mncs-environment/.worktrees/observed-target"
                        ),
                        "change_kinds": ["head-changed"],
                        "state_change_status": "observed-delta",
                        "causal_status": "not-established",
                        "before": {
                            "repository": repository,
                            "observed_path": (
                                "mncs-environment/.worktrees/observed-target"
                            ),
                            "checkout_identity": checkout_identity,
                            "branch": branch,
                            "head": before_head,
                            "clean": True,
                            "dirty_entry_count": 0,
                            "observation": "current",
                        },
                        "after": {
                            "repository": repository,
                            "observed_path": (
                                "mncs-environment/.worktrees/observed-target"
                            ),
                            "checkout_identity": checkout_identity,
                            "branch": branch,
                            "head": after_head,
                            "clean": True,
                            "dirty_entry_count": 0,
                            "observation": "current",
                        },
                    }],
                },
            },
        ],
        "truncated": False,
        "next_before_sequence": None,
    }

    projection = capability_intelligence.project(
        session, session_event_limit=8,
    )
    activity = projection["session_activity"]
    transition = activity["repository_ref_transitions"][0]

    assert activity["integration_eligibility"][0][
        "persistent_claim_required"
    ] is False
    assert transition["campaign_id"] == session.snapshot["campaign"]["identity"]
    assert transition["work_intent_id"] == session.snapshot["intent"]["identity"]
    assert transition["before"]["head"] == before_head
    assert transition["after"]["head"] == after_head
    assert transition["causal_status"] == "not-established"
    assert transition["preflight_relation"]["status"] == (
        "exact-preimage-match-after-preflight"
    )
    assert transition["preflight_relation"]["matches"][0]["event_reference"][
        "identity"
    ] == "evt_ref_transition_preflight"
    mismatched_intent = {
        **activity["integration_eligibility"][0],
        "work_intent_id": "int_unrelated_objective",
    }
    assert capability_intelligence._repository_ref_preflight_relation(
        transition,
        integration_records=[mismatched_intent],
        proposal_records=[],
    )["status"] == "no-exact-preflight-match-in-selected-window"
    assert "does not establish that the operation caused the change" in (
        transition["interpretation"]
    )
    compact = capability_intelligence._compact_projection(
        projection, original_size=100_000, max_result_bytes=10_000,
    )
    compact_transition = compact["session_activity"][
        "repository_ref_transitions"
    ][0]
    assert compact_transition["preflight_relation"]["matches"][0][
        "event_reference"
    ]["identity"] == "evt_ref_transition_preflight"


def test_repository_ref_followup_labels_missing_before_identity_as_candidate() -> None:
    before_head = "c" * 40
    transition = {
        "state_change_status": "observed-delta",
        "repository": "mncs-environment",
        "campaign_id": "cmp_projection_reader",
        "work_intent_id": "int_projection_reader",
        "event_reference": {"identity": "evt_observed", "sequence": 42},
        "before": {
            "repository": "mncs-environment",
            "observed_path": "mncs-environment",
            "branch": "main",
            "head": before_head,
        },
        "after": {
            "repository": "mncs-environment",
            "observed_path": "mncs-environment",
            "checkout_identity": "checkout:observed-target",
            "branch": "main",
            "head": "d" * 40,
        },
    }
    preflight = {
        "action": "merge",
        "repository": "mncs-environment",
        "campaign_id": "cmp_projection_reader",
        "work_intent_id": "int_projection_reader",
        "preflight_state_status": "recorded-and-validated",
        "event_reference": {
            "identity": "evt_ref_transition_preflight",
            "sequence": 41,
        },
        "repository_preflight_state": {
            "checkout_identity": "checkout:observed-target",
            "branch": "main",
            "head_status": "measured",
            "head": before_head,
        },
    }

    relation = capability_intelligence._repository_ref_preflight_relation(
        transition,
        integration_records=[preflight],
        proposal_records=[],
    )

    assert relation["status"] == "no-exact-preflight-match-in-selected-window"
    assert relation["matching_preflight_count"] == 0
    assert relation["candidate_status"] == (
        "incomplete-preimage-checkout-identity-unavailable"
    )
    assert relation["candidate_count"] == 1
    assert relation["candidates"][0]["checkout_identity_status"] == (
        "unavailable-on-transition-before-state"
    )
    assert relation["candidates"][0]["event_reference"]["identity"] == (
        "evt_ref_transition_preflight"
    )

    projection = {
        "schema_version": capability_intelligence.SCHEMA,
        "identity": "eci_incomplete_preimage_test",
        "status": "partial",
        "subject": {"work_intent": {}},
        "source": {},
        "correlation": {},
        "observation_count": 0,
        "pressure_hypothesis_count": 0,
        "improvement_relation_count": 0,
        "observations": [],
        "pressure_hypotheses": [],
        "improvement_relations": [],
        "session_activity_pressure_candidate_count": 0,
        "session_activity": {
            "status": "partial",
            "source": {},
            "repository_ref_transition_count": 1,
            "repository_ref_transitions": [{
                "identity": "reftxn_incomplete_preimage_test",
                "repository": "mncs-environment",
                "observed_path": "mncs-environment",
                "state_change_status": "observed-delta",
                "causal_status": "not-established",
                "before": transition["before"],
                "after": transition["after"],
                "event_reference": transition["event_reference"],
                "preflight_relation": relation,
            }],
        },
    }
    compact = capability_intelligence._compact_projection(
        projection, original_size=100_000, max_result_bytes=10_000,
    )
    compact_relation = compact["session_activity"][
        "repository_ref_transitions"
    ][0]["preflight_relation"]
    assert compact_relation["candidate_status"] == (
        "incomplete-preimage-checkout-identity-unavailable"
    )
    assert compact_relation["candidates"][0]["event_reference"]["identity"] == (
        "evt_ref_transition_preflight"
    )

    transition["after"]["checkout_identity"] = "checkout:different-target"
    unrelated = capability_intelligence._repository_ref_preflight_relation(
        transition,
        integration_records=[preflight],
        proposal_records=[],
    )
    assert unrelated["candidate_count"] == 0


def test_claim_owner_history_uses_only_bounded_read_interfaces() -> None:
    def unbounded_read_events(_session_id):
        raise AssertionError("unbounded event history must not be read")

    unavailable = capability_intelligence._claim_owner_event_page(
        SimpleNamespace(
            load_snapshot=lambda session_id: {"session_id": session_id},
            read_events=unbounded_read_events,
        ),
        "ses_owner",
    )
    assert unavailable is None

    events = [
        {
            "identity": f"evt_owner_{sequence}",
            "sequence": sequence,
            "type": "lease.acquired",
            "payload": {},
        }
        for sequence in range(33)
    ]
    requests = []

    def read_tail(session_id, *, limit):
        requests.append((session_id, limit))
        return events, 40

    page = capability_intelligence._claim_owner_event_page(
        SimpleNamespace(
            load_snapshot=lambda session_id: {"session_id": session_id},
            read_event_tail=read_tail,
        ),
        "ses_owner",
    )
    assert requests == [(
        "ses_owner", capability_intelligence.MAX_SESSION_CLAIM_OWNER_EVENT_PAGE,
    )]
    assert page is not None
    assert page["events"] == events[
        -capability_intelligence.MAX_SESSION_CLAIM_OWNER_EVENT_PAGE:
    ]
    assert page["event_records_considered"] == (
        capability_intelligence.MAX_SESSION_CLAIM_OWNER_EVENT_PAGE
    )
    assert page["event_count"] == 40
    assert page["truncated"] is True


def test_preflight_followup_search_reports_truncated_overlap_evidence(
    tmp_path: Path,
) -> None:
    record = _observation(
        "ev_environment_truncated_preflight_followup",
        "ses_environment_truncated_preflight_followup",
        "cmp_environment_truncated_preflight_followup",
    )
    session, _ = _session(tmp_path, [record])
    checkout = session.snapshot["selected_checkouts"]["mncs-environment"]["path"]
    denial_scope = {
        "kind": "paths",
        "repository": "mncs-environment",
        "paths": ["mncs_env/authority.py"],
    }
    events = [
        {
            "identity": "evt_denial_followup_search",
            "sequence": 10,
            "type": "authority.denied",
            "payload": {
                "action": "merge",
                "target": checkout,
                "reason": (
                    f"{checkout} overlaps scope claimed by another session "
                    "(ses_blocker): repository-relative paths overlap"
                ),
                "claim_conflict": {
                    "schema_version": (
                        capability_intelligence.authority_module.CLAIM_CONFLICT_SCHEMA
                    ),
                    "repository": "mncs-environment",
                    "conflict_reason": "repository-relative paths overlap",
                    "requested_scope": denial_scope,
                    "blocking_claim": {
                        "claim_id": "claim:mncs-environment:paths:missing",
                        "session_id": "ses_blocker",
                        "scope": denial_scope,
                    },
                },
            },
        },
        {
            "identity": "evt_truncated_eligible_preflight",
            "sequence": 11,
            "type": "authority.integration-eligible",
            "payload": {
                "action": "merge",
                "repository": "mncs-environment",
                "scope": {
                    "kind": "worktree",
                    "repository": "mncs-environment",
                    "branch": "campaign/example",
                    "exclusive": False,
                },
                "authorization_basis": "writable-repository-rights",
                "eligibility_basis": (
                    "exact-observed-scope-and-repository-native-concurrency"
                ),
                "persistent_claim_required": False,
                "operation_executed": False,
                "coordination_overlap_count": 9,
                "coordination_overlaps": [{
                    "session_id": f"ses_overlap_{index}",
                    "claim_id": f"claim:mncs-environment:paths:other{index}",
                    "scope": {
                        "kind": "paths",
                        "repository": "mncs-environment",
                        "paths": [f"mncs_env/module_{index}.py"],
                        "exclusive": False,
                    },
                    "overlap_reason": "repository-relative paths overlap",
                } for index in range(8)],
                "coordination_overlaps_truncated": True,
                "target": checkout,
            },
        },
    ]
    session.recent_events = lambda *, limit, before_sequence=None: {
        "event_count": len(events),
        "events": events[-limit:],
        "truncated": False,
        "next_before_sequence": None,
    }

    activity = capability_intelligence.project(session)["session_activity"]
    conflict = activity["authority_denials"][0]["claim_conflict"]
    assert "subsequent_integration_preflight" not in conflict
    search = conflict["subsequent_integration_preflight_search"]
    assert search["status"] == "not-established-overlap-records-truncated"
    assert search["event_reference"]["identity"] == (
        "evt_truncated_eligible_preflight"
    )
    assert search["coordination_overlap_count"] == 9
    assert search["coordination_overlap_records_returned"] == 8
    assert search["coordination_overlap_records_omitted"] == 1
    assert checkout not in json.dumps(activity)


def test_claim_conflict_evidence_is_correlated_and_keeps_legacy_gaps_explicit(
    tmp_path: Path,
) -> None:
    record = _observation(
        "ev_environment_claim_conflict_activity",
        "ses_environment_claim_conflict_activity",
        "cmp_environment_claim_conflict_activity",
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
    conflict = {
        "schema_version": "mncs.environment.authority-claim-conflict/1",
        "repository": "mncs-environment",
        "conflict_reason": "repository-relative paths overlap",
        "requested_scope": {
            "kind": "worktree",
            "repository": "mncs-environment",
            "checkout": alias_checkout,
            "branch": "main",
            "paths": ["mncs_env/sessions.py"],
            "exclusive": False,
        },
        "blocking_claim": {
            "claim_id": "claim:mncs-environment:paths:abc123",
            "session_id": "ses_blocker",
            "scope": {
                "kind": "paths",
                "repository": "mncs-environment",
                "checkout": "/private/other-checkout",
                "paths": ["mncs_env/sessions.py"],
                "exclusive": False,
            },
        },
        "decision_basis": {
            "decision_domain": "coordination",
            "authorization_basis": "session-writable-scope",
            "authorization_status": "eligible",
            "eligibility_status": "blocked-by-overlapping-scope",
            "coordination_status": "scoped-mutation-lease-conflict",
            "persistent_authority_required": False,
        },
        "current_claim_state": {
            "lookup_status": "found",
            "basis": "active-claim-store-read-before-authority-check",
            "claim_id": "claim:mncs-environment:paths:abc123",
            "owner_session_id": "ses_blocker",
            "repository": "mncs-environment",
            "observed_at": claims.utcnow(),
            "claim_version": 3,
            "record_status": "held",
            "blocking_status": "blocks-at-decision-time",
            "recovery_verdict": "live",
            "freshness": "active",
            "owner_liveness": {
                "inspection_status": "observed",
                "freshness": "active",
                "recovery_eligibility": "live",
                "recommended_operation": "use-disjoint-scope-or-wait",
            },
            "scope_relation_status": "overlap-observed-at-authority-check",
        },
    }
    now = claims.utcnow()
    current_claim = {
        "schema_version": claims.SCHEMA,
        "claim_id": "claim:mncs-environment:paths:abc123",
        "version": 4,
        "repository": "mncs-environment",
        "scope": {
            "kind": "paths",
            "repository": "mncs-environment",
            "paths": ["mncs_env/sessions.py"],
            "exclusive": False,
        },
        "session_id": "ses_blocker",
        "consumer_id": "consumer_blocker",
        "basis": claims.BASIS_EXPLICIT,
        "reason": "projection fixture",
        "status": "held",
        "acquired_at": now,
        "expires_at": "2099-01-01T00:00:00+00:00",
        "identity": "clm_projection_blocker_v4",
    }
    session.store = SimpleNamespace(
        read_latest_claims_for=lambda claim_ids: (
            [current_claim] if current_claim["claim_id"] in claim_ids else []
        ),
        load_snapshot=lambda session_id: {
            "session_id": session_id, "lifecycle": "completed",
        },
        read_events=lambda _session_id: [],
    )
    events = [
        {
            "identity": f"evt_claim_conflict_{sequence}",
            "sequence": sequence,
            "type": "authority.denied",
            "payload": {
                "action": "write",
                "target": alias_checkout,
                "reason": (
                    f"{alias_checkout} overlaps scope claimed by another session "
                    "(ses_blocker): repository-relative paths overlap"
                ),
                **({"claim_conflict": conflict} if sequence < 43 else {}),
            },
        }
        for sequence in (41, 42, 43)
    ]
    session.recent_events = lambda *, limit, before_sequence=None: {
        "event_count": len(events), "events": events[-limit:],
        "truncated": False, "next_before_sequence": None,
    }

    result = capability_intelligence.project(session)
    activity = result["session_activity"]
    assert activity["authority_denial_count"] == 3
    by_sequence = {
        item["event_reference"]["sequence"]: item
        for item in activity["authority_denials"]
    }
    assert by_sequence[42]["claim_conflict_evidence_status"] == "recorded"
    assert by_sequence[42]["claim_conflict"]["decision_basis_status"] == (
        "recorded-and-validated"
    )
    assert by_sequence[42]["claim_conflict"]["decision_basis"][
        "authorization_status"
    ] == "eligible"
    assert by_sequence[42]["claim_conflict"]["blocking_claim"]["claim_id"] == (
        "claim:mncs-environment:paths:abc123"
    )
    current_state = by_sequence[42]["claim_conflict"]["current_claim_state"]
    assert current_state["basis"] == "current-authoritative-claim-store"
    assert current_state["claim_version"] == 4
    assert current_state["blocking_status"] == (
        "eligible-for-safe-recovery-on-acquisition"
    )
    assert current_state["recovery_verdict"] == "recoverable"
    assert current_state["freshness"] == "stale"
    assert current_state["owner_liveness_evidence_status"] == (
        "terminal-lifecycle-observed"
    )
    assert current_state["recorded_owner_relation"] == "same"
    assert isinstance(current_state["observed_at"], str)
    decision_state = by_sequence[42]["claim_conflict"]["decision_claim_state"]
    assert by_sequence[42]["claim_conflict"]["decision_claim_state_status"] == (
        "recorded-and-validated"
    )
    assert decision_state["claim_version"] == 3
    assert decision_state["recovery_verdict"] == "live"
    assert decision_state["blocking_status"] == "blocks-at-decision-time"
    assert current_state["claim_version"] == 4
    assert current_state["recovery_verdict"] == "recoverable"
    stale_guidance = by_sequence[42]["claim_conflict"][
        "coordination_guidance"
    ]
    assert stale_guidance["status"] == "stale-claim-recoverable-on-acquisition"
    assert stale_guidance["next_step"] == "retry-exact-scope-acquisition"
    assert stale_guidance["ownership_transfer"] == "not-required"
    assert stale_guidance["claim_mutated"] is False
    assert "checkout" not in by_sequence[42]["claim_conflict"]["requested_scope"]
    assert by_sequence[43]["claim_conflict_evidence_status"] == "not-recorded"
    assert "claim_conflict" not in by_sequence[43]
    assert activity["source"]["authority_claim_state_lookup"]["lookup_count"] == 1
    candidate = activity["authority_denial_candidates"][0]
    assert candidate["occurrence_count"] == 3
    assert candidate["claim_conflict_evidence_status"] == "partially-recorded"
    decision_summary = candidate["decision_claim_state_summary"]
    assert decision_summary["status"] == (
        "recorded-for-all-structured-denials"
    )
    assert decision_summary["structured_denial_count"] == 2
    assert decision_summary["validated_count"] == 2
    assert decision_summary["legacy_denial_count"] == 1
    assert decision_summary["state_counts"] == [{
        "blocking_status": "blocks-at-decision-time",
        "recovery_verdict": "live",
        "freshness": "active",
        "occurrence_count": 2,
    }]
    basis_summary = candidate["decision_basis_summary"]
    assert basis_summary["status"] == "recorded-for-all-structured-denials"
    assert basis_summary["structured_denial_count"] == 2
    assert basis_summary["validated_count"] == 2
    assert basis_summary["writable_scope_eligible_block_count"] == 2
    projected_blocker = candidate["blocking_claims"][0]
    assert {
        key: value for key, value in projected_blocker.items()
        if key != "current_claim_state"
    } == {
        "session_id": "ses_blocker",
        "occurrence_count": 2,
        "scope": {
            "kind": "paths",
            "repository": "mncs-environment",
            "exclusive": False,
            "paths": ["mncs_env/sessions.py"],
        },
        "claim_id": "claim:mncs-environment:paths:abc123",
    }
    assert projected_blocker["current_claim_state"] == current_state
    assert alias_checkout not in json.dumps(result)
    assert "/private/other-checkout" not in json.dumps(result)

    compact = capability_intelligence.project(session, max_result_bytes=10_000)
    compact_activity = compact["session_activity"]
    assert compact_activity["authority_denial_candidates"][0][
        "claim_conflict_evidence_status"
    ] == "partially-recorded"
    compact_decision_summary = compact_activity[
        "authority_denial_candidates"
    ][0]["decision_claim_state_summary"]
    assert compact_decision_summary["status"] == decision_summary["status"]
    assert compact_decision_summary["validated_count"] == 2
    assert compact_decision_summary["legacy_denial_count"] == 1
    assert compact_decision_summary["state_counts"] == [{
        "blocking_status": "blocks-at-decision-time",
        "recovery_verdict": "live",
        "occurrence_count": 2,
    }]
    compact_basis_summary = compact_activity["authority_denial_candidates"][0][
        "decision_basis_summary"
    ]
    assert compact_basis_summary["writable_scope_eligible_block_count"] == 2
    compact_candidate_blocker = compact_activity[
        "authority_denial_candidates"
    ][0]["blocking_claims"][0]
    assert compact_candidate_blocker["current_claim_state"][
        "recovery_verdict"
    ] == "recoverable"
    assert compact_activity["source"]["authority_claim_state_lookup"][
        "status"
    ] == "read"
    compact_recorded_denial = next(
        denial for denial in compact_activity["authority_denials"]
        if "claim_conflict" in denial
    )
    assert compact_recorded_denial["claim_conflict"][
        "current_claim_state"
    ]["recovery_verdict"] == "recoverable"
    assert compact_recorded_denial["claim_conflict"][
        "decision_claim_state"
    ]["claim_version"] == 3
    assert compact_recorded_denial["claim_conflict"]["decision_basis"][
        "authorization_status"
    ] == "eligible"
    compact_correlations = compact_activity["claim_lifecycle_correlations"]
    if compact_correlations:
        assert compact_recorded_denial["claim_conflict"]["blocking_claim"][
            "claim_lifecycle_correlation_identity"
        ] == compact_correlations[0]["identity"]
    else:
        assert "claim_lifecycle_correlation_identity" not in (
            compact_recorded_denial["claim_conflict"]["blocking_claim"]
        )
    assert (
        compact_activity["claim_lifecycle_correlations_returned"]
        + compact_activity["claim_lifecycle_correlations_omitted_count"]
    ) == 1
    assert alias_checkout not in json.dumps(compact)

    active_at = claims.utcnow()
    session.store.load_snapshot = lambda session_id: {
        "session_id": session_id, "lifecycle": "active",
    }
    session.store.read_events = lambda _session_id: [{
        "identity": "evt_projection_owner_active",
        "sequence": 1,
        "type": "test.ping",
        "observed_at": active_at,
        "payload": {},
    }]
    live_projection = capability_intelligence.project(session)
    live_denial = next(
        denial for denial in live_projection["session_activity"]["authority_denials"]
        if "claim_conflict" in denial
    )
    live_state = live_denial["claim_conflict"]["current_claim_state"]
    assert live_state["recovery_verdict"] == "live"
    assert live_state["blocking_status"] == "blocks-at-projection-time"
    live_guidance = live_denial["claim_conflict"]["coordination_guidance"]
    assert live_guidance["status"] == "active-shared-scope-conflict"
    assert live_guidance["next_step"] == "choose-a-nonoverlapping-scope-or-wait"
    assert live_guidance["claim_mutated"] is False


def test_shared_branch_conflict_guidance_preserves_claimless_proposal_path() -> None:
    guidance = capability_intelligence._authority_claim_conflict_guidance(
        "write",
        {
            "conflict_reason": (
                "shared branch with overlapping paths across checkouts"
            ),
        },
        {"blocking_status": "blocks-at-projection-time"},
    )

    assert guidance["status"] == (
        "shared-ref-overlap-is-proposal-coordination"
    )
    assert guidance["next_step"] == (
        "refresh-proposal-preflight-on-distinct-registered-worktree"
    )
    assert guidance["ownership_transfer"] == (
        "not-required-for-local-proposal-work"
    )
    assert guidance["claim_mutated"] is False


def test_claim_coordination_projects_bounded_objective_linked_transitions(
    tmp_path: Path,
) -> None:
    record = _observation(
        "ev_environment_claim_coordination_activity",
        "ses_environment_claim_coordination_activity",
        "cmp_environment_claim_coordination_activity",
    )
    session, _state = _session(tmp_path, [record])
    private_checkout = "/private/agent/worktrees/environment"
    events = [
        {
            "identity": "evt_claim_coordination_acquired",
            "sequence": 4,
            "type": "lease.acquired",
            "payload": {
                "repository": "mncs-environment",
                "basis": claims.BASIS_EXPLICIT,
                "claim_identity": "claim:mncs-environment:paths:abc123",
                "claim_version": 2,
                "scope": {
                    "kind": "paths",
                    "repository": "mncs-environment",
                    "checkout": private_checkout,
                    "branch": "campaign/coordination",
                    "paths": ["mncs_env/sessions.py"],
                    "exclusive": False,
                },
                "campaign_id": "cmp_coordination_campaign",
                "work_intent_id": "int_coordination_intent",
                "reason": "private reason must not enter the projection",
            },
        },
        {
            "identity": "evt_claim_coordination_release",
            "sequence": 5,
            "type": "lease.released",
            "payload": {
                "repository": "mncs-environment",
                "claim_id": "claim:mncs-environment:paths:abc123",
                "released": ["claim:mncs-environment:paths:abc123"],
                "reason": "legacy event has no objective reference",
            },
        },
        {
            "identity": "evt_untyped_event_name",
            "sequence": 6,
            "type": [],
            "payload": {},
        },
    ]
    session.recent_events = lambda *, limit, before_sequence=None: {
        "event_count": len(events), "events": events[-limit:],
        "truncated": False, "next_before_sequence": None,
    }

    activity = capability_intelligence.project(session)["session_activity"]
    assert activity["claim_coordination_count"] == 2
    assert activity["source"]["claim_coordination_events_considered"] == 2
    assert activity["source"]["claim_coordination_records_invalid"] == 0
    acquired, released = activity["claim_coordination"][::-1]
    assert acquired["event_reference"]["identity"] == (
        "evt_claim_coordination_acquired"
    )
    assert acquired["campaign_id"] == "cmp_coordination_campaign"
    assert acquired["campaign_reference_status"] == "recorded-on-event"
    assert acquired["work_intent_id"] == "int_coordination_intent"
    assert acquired["work_intent_reference_status"] == "recorded-on-event"
    assert acquired["scope"] == {
        "kind": "paths",
        "repository": "mncs-environment",
        "exclusive": False,
        "branch": "campaign/coordination",
        "paths": ["mncs_env/sessions.py"],
    }
    assert "reason" not in acquired
    assert released["work_intent_reference_status"] == "not-recorded-on-event"
    assert released["event_reference"]["identity"] == (
        "evt_claim_coordination_release"
    )
    assert private_checkout not in json.dumps(activity)
    assert "private reason" not in json.dumps(activity)
    assert all("successful" not in item["status"]
               for item in activity["claim_coordination"])

    compact = capability_intelligence.project(
        session, max_result_bytes=10_000,
    )["session_activity"]
    assert compact["claim_coordination"][0]["event_reference"]["identity"] == (
        "evt_claim_coordination_release"
    )
    assert compact["claim_coordination"][1]["work_intent_id"] == (
        "int_coordination_intent"
    )
    assert private_checkout not in json.dumps(compact)


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
    session.snapshot["rpc_read_activity_count"] = 4
    session.snapshot["bindings"] = [{
        "binding_id": "cap_" + "a" * 64,
        "provider": "mncs-test",
        "capability": "mncs-test:run",
        "contract_revision": "r1",
        "availability": {
            "status": "available", "verification": "substrate",
            "observed_at": "2026-10-24T01:00:00Z",
        },
    }]
    context_identity_digest = hashlib.sha256(json.dumps(
        [{
            "capability": "mncs-test:run",
            "provider": "mncs-test",
            "contract_revision": "r1",
        }],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    events = []
    for sequence, operation, outcome, exit_code, failure_code in (
        (21, "session.inspect", "failed", 2, "ENVIRONMENT_SESSION_MISSING"),
        (22, "session.inspect", "failed", None, "ENVIRONMENT_SESSION_MISSING"),
        (23, "campaign.state", "succeeded", 0, None),
        (24, "session.context", "succeeded", 0, None),
    ):
        payload = {
            "schema_version": "mncs.environment.rpc-read-activity/1",
            "operation": operation,
            "outcome_status": outcome,
            "exit_code": exit_code,
            "failure_code": failure_code,
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
        if operation == "session.context":
            payload["schema_version"] = (
                "mncs.environment.rpc-read-activity/2"
            )
            payload["context_response_summary"] = {
                "schema_version": "mncs.environment.context-response-summary/2",
                "capture_status": "captured",
                "capture_reason": None,
                "context_schema_version": "mncs.environment.entry-context/1",
                "available_count": 1,
                "listed_available_count": 1,
                "unavailable_count": 0,
                "unknown_count": 0,
                "capabilities_truncated": False,
                "available_identity_digest": context_identity_digest,
                "identity_digest_status": "complete",
                "available_binding_ids": ["cap_" + "a" * 64],
                "binding_ids_status": "complete",
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
        "identity": "evt_rpc_read_spoofed", "sequence": 25,
        "type": "environment.rpc.read_observed",
        "producer": "mncs-control-mcp", "payload": untrusted_payload,
    })
    events.extend([
        {
            "identity": "evt_rpc_capability_invoked_after_context",
            "sequence": 26,
            "type": "capability.invoked",
            "producer": "local-agent",
            "payload": {
                "capability": "mncs-test:run",
                "provider": "mncs-test",
                "binding_id": "cap_" + "a" * 64,
                "contract_revision": "r1",
                "provider_request_id": "req_rpc_capability_after_context",
            },
        },
        {
            "identity": "evt_rpc_capability_completed_after_context",
            "sequence": 27,
            "type": "invocation.completed",
            "producer": "mncs-test",
            "payload": {
                "capability": "mncs-test:run",
                "provider": "mncs-test",
                "binding_id": "cap_" + "a" * 64,
                "contract_revision": "r1",
                "provider_request_id": "req_rpc_capability_after_context",
                "status": "succeeded",
                "returncode": 0,
            },
        },
    ])
    session.recent_events = lambda *, limit: {
        "event_count": len(events), "events": events[-limit:], "truncated": False,
    }

    result = capability_intelligence.project(session)
    activity = result["session_activity"]

    assert activity["context"]["work_intent_goal"] == "inspect current pressures"
    assert activity["status"] == "ready"
    assert activity["interaction_count"] == 1
    assert activity["source"]["rpc_operation_capture_count"] == 4
    assert activity["source"]["rpc_operation_capture_limit"] == 256
    assert activity["source"]["rpc_operation_records_invalid"] == 1
    assert activity["source"]["rpc_operation_records_omitted"] == 0
    assert activity["source"]["rpc_operation_context_basis"] == {
        "repository_identity": "control-project-grant",
        "repository_branch_and_head": "session-selected-checkout",
        "environment_campaign_and_work_intent": "session-snapshot",
        "live_repository_probe": False,
    }
    assert activity["rpc_operation_count"] == 4
    failed_operations = {
        item["event_reference"]["identity"]: item
        for item in activity["rpc_operations"]
        if item["outcome_status"] == "failed"
    }
    assert failed_operations["evt_rpc_read_21"]["exit_code"] == 2
    assert "exit_code" not in failed_operations["evt_rpc_read_22"]
    assert failed_operations["evt_rpc_read_22"]["failure_code"] == (
        "ENVIRONMENT_SESSION_MISSING"
    )
    summary = activity["rpc_operation_summary"]
    assert summary["scope"] == "authenticated-records-in-selected-event-window"
    assert summary["record_count"] == 4
    assert summary["group_count"] == 3
    assert summary["groups_omitted"] == 0
    assert [
        (group["operation"], group["outcome_status"], group["occurrence_count"])
        for group in summary["groups"]
    ] == [
        ("campaign.state", "succeeded", 1),
        ("session.context", "succeeded", 1),
        ("session.inspect", "failed", 2),
    ]
    assert [event["identity"] for event in summary["groups"][2]["supporting_events"]] == [
        "evt_rpc_read_21", "evt_rpc_read_22",
    ]
    assert activity["rpc_operations"][0]["event_reference"]["identity"] == (
        "evt_rpc_read_24"
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
    context_activity = activity["capability_delivery"]["context_operation_activity"]
    assert context_activity["status"] == "observed"
    assert context_activity["outcome_counts"] == {"succeeded": 1}
    assert context_activity["supporting_events"][0]["identity"] == (
        "evt_rpc_read_24"
    )
    assert context_activity["response_capability_list_status"] == "captured"
    assert context_activity["response_capture_status_counts"] == {"captured": 1}
    assert context_activity["response_identity_digest_status_counts"] == {
        "complete": 1,
    }
    assert context_activity["response_summaries"][0]["event_reference"][
        "identity"
    ] == "evt_rpc_read_24"
    assert context_activity["response_summaries"][0][
        "available_identity_digest"
    ] == context_identity_digest
    assert context_activity["response_summaries"][0][
        "available_binding_ids"
    ] == ["cap_" + "a" * 64]
    comparison = context_activity["response_summaries"][0][
        "binding_snapshot_comparison"
    ]
    assert comparison["status"] == "matches-current-session-binding-snapshot"
    assert comparison["available_count_matches"] is True
    assert comparison["unavailable_count_matches"] is True
    assert comparison["unknown_count_matches"] is True
    assert comparison["available_identity_digest_matches"] is True
    binding_context_inclusion = activity["capability_delivery"]["bindings"][0][
        "context_response_inclusion"
    ]
    assert binding_context_inclusion["status"] == "exact-binding-id-in-response"
    assert binding_context_inclusion["identity_basis"] == (
        "exact-capability-binding-id"
    )
    assert binding_context_inclusion["binding_id_attribution_status"] == (
        "exact-from-response"
    )
    assert binding_context_inclusion["response_binding_ids_status"] == "complete"
    assert binding_context_inclusion["supporting_events"][0]["identity"] == (
        "evt_rpc_read_24"
    )
    binding_evidence_summary = activity["capability_delivery"][
        "binding_evidence_summary"
    ]
    assert binding_evidence_summary["schema_version"] == (
        "mncs.environment.capability-binding-evidence-summary/2"
    )
    assert binding_evidence_summary[
        "context_response_binding_id_attribution_status_counts"
    ] == {"exact-from-response": 1}
    assert binding_evidence_summary[
        "context_response_binding_id_list_status_counts"
    ] == {"complete": 1}
    assert binding_evidence_summary["context_response_supporting_events"][0][
        "identity"
    ] == "evt_rpc_read_24"
    assert binding_evidence_summary[
        "exact_binding_invocation_supporting_events"
    ][0]["identity"] == "evt_rpc_capability_invoked_after_context"
    sequence_relation = activity["capability_delivery"]["bindings"][0][
        "context_response_use_sequence_relation"
    ]
    assert sequence_relation["status"] == (
        "invocation-observed-after-context-response"
    )
    assert sequence_relation["causal_claim"] == "not-established"
    assert sequence_relation["observed_pairs"][0][
        "context_response_event"
    ]["identity"] == "evt_rpc_read_24"
    assert sequence_relation["observed_pairs"][0]["invocation_event"][
        "identity"
    ] == "evt_rpc_capability_invoked_after_context"
    assert context_activity["agent_receipt_status"] == "not-established"
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

    compact = capability_intelligence.project(session, max_result_bytes=6_000)
    compact_activity = compact["session_activity"]
    assert compact_activity["rpc_operations"][0]["event_reference"]["identity"] == (
        "evt_rpc_read_24"
    )
    assert compact_activity["source"]["rpc_operation_events_considered"] == 5
    assert compact_activity["source"]["rpc_operation_records_returned"] == 4
    assert compact_activity["source"]["rpc_operation_records_omitted"] == 0
    assert compact_activity["source"]["rpc_operation_records_invalid"] == 1
    compact_summary = compact_activity["rpc_operation_summary"]
    assert compact_summary["record_count"] == 4
    assert compact_summary["group_count"] == 3
    assert compact_summary["groups_omitted"] == 0
    assert compact_summary["groups"][2]["supporting_events"][0]["identity"] == (
        "evt_rpc_read_21"
    )
    compact_delivery = capability_intelligence._compact_projection(
        result, 20_000, capability_intelligence.MAX_RESULT_BYTES,
    )["session_activity"]["capability_delivery"]
    assert compact_delivery["context_operation_activity"]["outcome_counts"] == {
        "succeeded": 1,
    }
    compact_context_activity = compact_delivery["context_operation_activity"]
    assert compact_context_activity["response_capability_list_status"] == "captured"
    assert compact_context_activity["response_summaries"][0]["event_reference"][
        "identity"
    ] == "evt_rpc_read_24"
    assert compact_context_activity["response_summaries"][0][
        "binding_snapshot_comparison"
    ]["status"] == "matches-current-session-binding-snapshot"
    assert compact_context_activity["response_summaries"][0][
        "available_binding_ids"
    ] == ["cap_" + "a" * 64]
    compact_comparison = compact_context_activity["response_summaries"][0][
        "binding_snapshot_comparison"
    ]
    assert compact_comparison["unavailable_count_matches"] is True
    assert compact_comparison["unknown_count_matches"] is True
    compact_binding_inclusion = compact_delivery["bindings"][0][
        "context_response_inclusion"
    ]
    assert compact_binding_inclusion["status"] == "exact-binding-id-in-response"
    assert compact_binding_inclusion["response_binding_ids_status"] == "complete"
    assert compact_binding_inclusion["supporting_events"][0]["identity"] == (
        "evt_rpc_read_24"
    )
    compact_binding_evidence_summary = compact_delivery[
        "binding_evidence_summary"
    ]
    assert compact_binding_evidence_summary[
        "context_response_binding_id_attribution_status_counts"
    ] == {"exact-from-response": 1}
    assert compact_binding_evidence_summary[
        "context_response_supporting_events"
    ][0]["identity"] == "evt_rpc_read_24"
    compact_sequence_relation = compact_delivery["bindings"][0][
        "context_response_use_sequence_relation"
    ]
    assert compact_sequence_relation["status"] == (
        "invocation-observed-after-context-response"
    )
    assert compact_sequence_relation["causal_claim"] == "not-established"
    assert compact_sequence_relation["observed_pairs"][0][
        "invocation_event"
    ]["identity"] == "evt_rpc_capability_invoked_after_context"
    context_event = next(event for event in events if event["sequence"] == 24)
    current_context_summary = context_event["payload"]["context_response_summary"]
    historical_context_summary = {
        key: value for key, value in current_context_summary.items()
        if key not in {"available_binding_ids", "binding_ids_status"}
    }
    historical_context_summary["schema_version"] = (
        "mncs.environment.context-response-summary/1"
    )
    context_event["payload"]["context_response_summary"] = historical_context_summary
    historical_inclusion = capability_intelligence.project(session)[
        "session_activity"
    ]["capability_delivery"]["bindings"][0]["context_response_inclusion"]
    assert historical_inclusion["status"] == "identity-tuple-in-matched-response"
    assert historical_inclusion["binding_id_attribution_status"] == (
        "not-carried-by-historical-context-response-summary"
    )
    context_event["payload"]["context_response_summary"] = current_context_summary
    context_event["payload"]["context_response_summary"][
        "available_binding_ids"
    ] = []
    context_event["payload"]["context_response_summary"][
        "binding_ids_status"
    ] = "partial"
    partial_projection = capability_intelligence.project(session)[
        "session_activity"]["capability_delivery"]
    partial_inclusion = partial_projection["bindings"][0][
        "context_response_inclusion"
    ]
    assert partial_inclusion["status"] == "identity-tuple-in-matched-response"
    assert partial_inclusion["binding_id_attribution_status"] == (
        "binding-id-capture-incomplete"
    )
    assert partial_inclusion["response_binding_ids_status"] == "partial"
    partial_summary = partial_projection["binding_evidence_summary"]
    assert partial_summary[
        "context_response_binding_id_attribution_status_counts"
    ] == {"binding-id-capture-incomplete": 1}
    assert partial_summary[
        "context_response_binding_id_list_status_counts"
    ] == {"partial": 1}
    assert any(
        "no binding revision" in limit
        for limit in compact_context_activity["measurement_limits"]
    )
    assert "mncs-test:run" not in json.dumps(compact_context_activity)
    assert compact_summary["groups"][1]["supporting_events"][0]["identity"] == (
        "evt_rpc_read_24"
    )
    assert compact_activity["source"]["rpc_operation_context_basis"] == (
        "grant+session-snapshot; branch/head not live-probed"
    )
    assert compact_activity["rpc_failure_candidates"][0]["supporting_events"][0][
        "identity"
    ] == "evt_rpc_read_21"
    assert compact_activity["rpc_failure_candidates"][0]["distinct_session_count"] == 1

    context_event["payload"]["context_response_summary"]["unknown_count"] = 1
    unknown_mismatch = capability_intelligence.project(session)["session_activity"]
    unknown_comparison = unknown_mismatch["capability_delivery"][
        "context_operation_activity"
    ]["response_summaries"][0]["binding_snapshot_comparison"]
    assert unknown_comparison["status"] == (
        "differs-from-current-session-binding-snapshot"
    )
    assert unknown_comparison["available_count_matches"] is True
    assert unknown_comparison["unavailable_count_matches"] is True
    assert unknown_comparison["unknown_count_matches"] is False
    unknown_inclusion = unknown_mismatch["capability_delivery"]["bindings"][0][
        "context_response_inclusion"
    ]
    assert unknown_inclusion["status"] == "not-established"
    assert unknown_inclusion["reason"] == (
        "captured-response-differs-from-current-snapshot"
    )
    assert unknown_mismatch["capability_delivery"]["bindings"][0][
        "context_response_use_sequence_relation"
    ]["status"] == "not-comparable"

    context_event["payload"]["context_response_summary"]["unknown_count"] = 0
    context_event["payload"]["context_response_summary"][
        "available_identity_digest"
    ] = "f" * 64
    mismatch = capability_intelligence.project(session)["session_activity"]
    mismatch_comparison = mismatch["capability_delivery"][
        "context_operation_activity"
    ]["response_summaries"][0]["binding_snapshot_comparison"]
    assert mismatch_comparison["status"] == (
        "differs-from-current-session-binding-snapshot"
    )
    assert mismatch_comparison["available_count_matches"] is True
    assert mismatch_comparison["available_identity_digest_matches"] is False
    digest_mismatch_inclusion = mismatch["capability_delivery"]["bindings"][0][
        "context_response_inclusion"
    ]
    assert digest_mismatch_inclusion["status"] == "not-established"
    assert digest_mismatch_inclusion["reason"] == (
        "captured-response-differs-from-current-snapshot"
    )

    session.snapshot["bindings"][0]["contract_revision"] = None
    incomplete = capability_intelligence.project(session)["session_activity"]
    incomplete_comparison = incomplete["capability_delivery"][
        "context_operation_activity"
    ]["response_summaries"][0]["binding_snapshot_comparison"]
    assert incomplete_comparison["status"] == "not-comparable"
    assert incomplete_comparison["reason"] == "binding-identity-incomplete"
    incomplete_binding_inclusion = incomplete["capability_delivery"]["bindings"][0][
        "context_response_inclusion"
    ]
    assert incomplete_binding_inclusion["status"] == "not-comparable"
    assert incomplete_binding_inclusion["reason"] == "binding-identity-incomplete"

    session.snapshot["bindings"][0]["contract_revision"] = "r1"
    events.remove(context_event)
    unobserved = capability_intelligence.project(session)["session_activity"]
    unobserved_inclusion = unobserved["capability_delivery"]["bindings"][0][
        "context_response_inclusion"
    ]
    assert unobserved_inclusion["status"] == "not-observed-in-session-history"
    assert unobserved["capability_delivery"]["discoverability_status"] == (
        "not-measured"
    )
    assert unobserved["capability_delivery"]["bindings"][0][
        "context_response_use_sequence_relation"
    ]["status"] == "not-comparable"


def test_context_response_use_relation_preserves_temporal_and_missing_use_limits() -> None:
    inclusion = {
        "status": "identity-tuple-in-matched-response",
        "supporting_events": [{
            "identity": "evt_context_response",
            "sequence": 10,
            "type": "environment.rpc.read_observed",
        }],
    }
    invocation = {
        "invocation_event": {
            "identity": "evt_capability_invoked",
            "sequence": 12,
            "type": "capability.invoked",
        },
    }

    after = capability_intelligence._context_response_use_sequence_relation(
        inclusion, [invocation], complete_history=True,
    )
    assert after["status"] == "invocation-observed-after-context-response"
    assert after["causal_claim"] == "not-established"
    assert after["observed_pairs"][0]["relation"] == (
        "context-response-sequence-precedes-invocation"
    )

    before_invocation = {
        "invocation_event": {**invocation["invocation_event"], "sequence": 8},
    }
    before = capability_intelligence._context_response_use_sequence_relation(
        inclusion, [before_invocation], complete_history=True,
    )
    assert before["status"] == "invocation-observed-before-context-response"
    assert before["observed_pairs"][0]["relation"] == (
        "invocation-sequence-precedes-context-response"
    )

    no_use = capability_intelligence._context_response_use_sequence_relation(
        inclusion, [], complete_history=True,
    )
    assert no_use["status"] == (
        "no-exact-binding-invocation-record-in-session-history"
    )
    assert no_use["reason"] == "this does not account for use through external interfaces"

    partial_no_use = capability_intelligence._context_response_use_sequence_relation(
        inclusion, [], complete_history=False,
    )
    assert partial_no_use["status"] == (
        "no-exact-binding-invocation-observed-in-selected-window"
    )

    omitted_use = capability_intelligence._context_response_use_sequence_relation(
        inclusion, [], complete_history=True, invocation_summaries_omitted=1,
    )
    assert omitted_use["status"] == "not-observed-in-interaction-window"
    assert omitted_use["invocation_summaries_omitted"] == 1

    before_with_omitted_response = (
        capability_intelligence._context_response_use_sequence_relation(
            inclusion, [before_invocation], complete_history=True,
            response_summaries_omitted=1,
        )
    )
    assert before_with_omitted_response["status"] == "not-comparable"
    assert before_with_omitted_response["reason"] == (
        "context-response-summaries-omitted"
    )

    binding = {
        "availability_status": "available",
        "capability": "mncs-test:run",
        "provider": "mncs-test",
        "contract_revision": "r1",
    }
    differing_response = {
        "capture_status": "captured",
        "identity_digest_status": "complete",
        "capabilities_truncated": False,
        "binding_snapshot_comparison": {
            "status": "differs-from-current-session-binding-snapshot",
        },
        "event_reference": {
            "identity": "evt_mismatched_context_response",
            "sequence": 8,
            "type": "environment.rpc.read_observed",
        },
    }
    omitted_response_inclusion = (
        capability_intelligence._context_response_binding_inclusion(
            binding, [differing_response], [differing_response["event_reference"]],
            context_operation_count=2, complete_history=True,
            response_summaries_omitted=1,
        )
    )
    assert omitted_response_inclusion["status"] == "not-established"
    assert omitted_response_inclusion["reason"] == (
        "context-response-summaries-omitted"
    )


def test_context_response_inclusion_uses_exact_bounded_binding_ids() -> None:
    binding = {
        "availability_status": "available",
        "binding_id": "cap_" + "a" * 64,
        "capability": "mncs-test:run",
        "provider": "mncs-test",
        "contract_revision": "r1",
    }
    event = {
        "identity": "evt_context_binding_ids",
        "sequence": 12,
        "type": "environment.rpc.read_observed",
    }
    partially_listed_response = {
        "capture_status": "captured",
        "binding_ids_status": "partial",
        "available_binding_ids": [binding["binding_id"]],
        "event_reference": event,
    }
    exact = capability_intelligence._context_response_binding_inclusion(
        binding, [partially_listed_response], [event],
        context_operation_count=1, complete_history=False,
    )
    assert exact["status"] == "exact-binding-id-in-response"
    assert exact["response_binding_ids_status"] == "partial"
    assert exact["binding_id_attribution_status"] == "exact-from-response"

    complete_other_binding_response = {
        "capture_status": "captured",
        "binding_ids_status": "complete",
        "available_binding_ids": ["cap_" + "b" * 64],
        "event_reference": event,
    }
    absent = capability_intelligence._context_response_binding_inclusion(
        binding, [complete_other_binding_response], [event],
        context_operation_count=1, complete_history=True,
    )
    assert absent["status"] == (
        "binding-id-absent-from-complete-selected-responses"
    )
    assert absent["supporting_events"] == [event]

    incomplete_absence = capability_intelligence._context_response_binding_inclusion(
        binding, [{
            **complete_other_binding_response,
            "binding_ids_status": "partial",
        }], [event], context_operation_count=1, complete_history=True,
    )
    assert incomplete_absence["status"] == "not-established"


def test_session_activity_marks_truncated_authenticated_rpc_only_history_partial(
    tmp_path: Path,
) -> None:
    record = _observation(
        "ev_environment_rpc_activity_truncated_observation",
        "ses_compiler_rpc_activity_truncated",
        "cmp_compiler_rpc_activity_truncated",
    )
    session, _ = _session(tmp_path, [record])
    event_count = capability_intelligence.MAX_SESSION_ACTIVITY_RPC_OPERATIONS + 1
    session.snapshot["rpc_read_activity_count"] = event_count
    events = []
    principal_id = session.snapshot["authenticated_principal_id"]
    for sequence in range(1, event_count + 1):
        request_digest = hashlib.sha256(
            f"rpc-truncated-request-{sequence}".encode(),
        ).hexdigest()
        events.append({
            "identity": f"evt_rpc_truncated_{sequence}",
            "sequence": sequence,
            "type": "environment.rpc.read_observed",
            "producer": "mncs-control-mcp",
            "payload": {
                "schema_version": "mncs.environment.rpc-read-activity/1",
                "operation": "session.inspect",
                "outcome_status": "succeeded",
                "exit_code": 0,
                "failure_code": None,
                "observation_ordinal": sequence,
                "request_id": f"req_rpc_truncated_{sequence}",
                "request_digest": request_digest,
                "request_operation_digest": request_digest,
                "request_event_digest": hashlib.sha256(
                    f"rpc-truncated-event-{sequence}".encode(),
                ).hexdigest()[:16],
                "caller": {
                    "authenticated_principal_id": principal_id,
                    "control_grant_id": "b" * 32,
                    "process_identity": "proc_" + "c" * 32,
                },
                "context": {
                    "repository": "mncs-control-mcp",
                    "environment_id": session.snapshot["environment_id"],
                    "campaign_id": session.snapshot["campaign"]["identity"],
                    "work_intent_id": session.snapshot["intent"]["identity"],
                },
                "measurement_availability": {
                    "model_identity": "unavailable",
                    "token_usage": "unavailable",
                    "control_dispatch_elapsed_ms": "measured",
                    "resource_consumption": "unavailable",
                    "cache_accounting": "unavailable",
                },
                "measurements": {"control_dispatch_elapsed_ms": 1.0},
            },
        })
    session.recent_events = lambda *, limit: {
        "event_count": len(events), "events": events[-limit:], "truncated": False,
    }

    result = capability_intelligence.project(session)
    activity = result["session_activity"]

    assert activity["status"] == "partial-rpc-only-in-selected-window"
    assert activity["interaction_count"] == 0
    assert activity["rpc_operation_count"] == (
        capability_intelligence.MAX_SESSION_ACTIVITY_RPC_OPERATIONS
    )
    assert activity["source"]["rpc_operation_records_omitted"] == 1
    assert activity["source"]["rpc_operation_records_invalid"] == 0
    assert activity["rpc_operations"][0]["event_reference"]["sequence"] == event_count
    summary = activity["rpc_operation_summary"]
    assert summary["record_count"] == event_count
    assert summary["group_count"] == 1
    assert summary["groups"][0]["occurrence_count"] == event_count

    compact = capability_intelligence.project(session, max_result_bytes=5_000)
    compact_activity = compact["session_activity"]
    assert compact_activity["status"] == "partial-rpc-only-in-selected-window"
    assert compact_activity["source"]["rpc_operation_events_considered"] == event_count
    assert compact_activity["source"]["rpc_operation_records_returned"] == (
        capability_intelligence.MAX_SESSION_ACTIVITY_RPC_OPERATIONS
    )
    assert compact_activity["source"]["rpc_operation_records_omitted"] == 1
    assert compact_activity["source"]["rpc_operation_records_invalid"] == 0
    assert compact_activity["rpc_operation_summary"]["record_count"] == event_count
    assert compact_activity["rpc_operation_summary"]["groups"][0][
        "occurrence_count"
    ] == event_count


def test_session_activity_bounds_authenticated_rpc_operation_summary_groups(
    tmp_path: Path,
) -> None:
    record = _observation(
        "ev_environment_rpc_activity_summary_bounds",
        "ses_compiler_rpc_activity_summary_bounds",
        "cmp_compiler_rpc_activity_summary_bounds",
    )
    session, _ = _session(tmp_path, [record])
    event_count = capability_intelligence.MAX_SESSION_ACTIVITY_RPC_SUMMARY_GROUPS + 2
    session.snapshot["rpc_read_activity_count"] = event_count
    principal_id = session.snapshot["authenticated_principal_id"]
    events = []
    for sequence in range(1, event_count + 1):
        request_digest = hashlib.sha256(
            f"rpc-summary-request-{sequence}".encode(),
        ).hexdigest()
        events.append({
            "identity": f"evt_rpc_summary_{sequence}",
            "sequence": sequence,
            "type": "environment.rpc.read_observed",
            "producer": "mncs-control-mcp",
            "payload": {
                "schema_version": "mncs.environment.rpc-read-activity/1",
                "operation": f"session.read-{sequence:03d}",
                "outcome_status": "succeeded",
                "exit_code": 0,
                "failure_code": None,
                "observation_ordinal": sequence,
                "request_id": f"req_rpc_summary_{sequence}",
                "request_digest": request_digest,
                "request_operation_digest": request_digest,
                "request_event_digest": hashlib.sha256(
                    f"rpc-summary-event-{sequence}".encode(),
                ).hexdigest()[:16],
                "caller": {
                    "authenticated_principal_id": principal_id,
                    "control_grant_id": "b" * 32,
                    "process_identity": None,
                },
                "context": {
                    "repository": "mncs-environment",
                    "environment_id": session.snapshot["environment_id"],
                    "campaign_id": session.snapshot["campaign"]["identity"],
                    "work_intent_id": session.snapshot["intent"]["identity"],
                },
                "measurement_availability": {
                    "model_identity": "unavailable",
                    "token_usage": "unavailable",
                    "control_dispatch_elapsed_ms": "measured",
                    "resource_consumption": "unavailable",
                    "cache_accounting": "unavailable",
                },
                "measurements": {"control_dispatch_elapsed_ms": 1.0},
            },
        })
    session.recent_events = lambda *, limit: {
        "event_count": len(events), "events": events[-limit:], "truncated": False,
    }

    activity = capability_intelligence.project(session)["session_activity"]
    summary = activity["rpc_operation_summary"]

    assert summary["record_count"] == event_count
    assert summary["group_count"] == event_count
    assert len(summary["groups"]) == capability_intelligence.MAX_SESSION_ACTIVITY_RPC_SUMMARY_GROUPS
    assert summary["groups_omitted"] == 2
    assert summary["omitted_record_count"] == 2
    assert sum(group["occurrence_count"] for group in summary["groups"]) + (
        summary["omitted_record_count"]
    ) == summary["record_count"]

    compact = capability_intelligence.project(session, max_result_bytes=5_000)
    compact_summary = compact["session_activity"]["rpc_operation_summary"]
    assert compact_summary["record_count"] == event_count
    assert compact_summary["group_count"] == event_count
    assert len(compact_summary["groups"]) <= 16
    assert compact_summary["groups_omitted"] == event_count - len(
        compact_summary["groups"],
    )
    assert sum(group["occurrence_count"] for group in compact_summary["groups"]) + (
        compact_summary["omitted_record_count"]
    ) == compact_summary["record_count"]


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
    tmp_path: Path, monkeypatch,
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
    hash_limited_source_path = checkout / "src" / "hash-limited-source.mn"
    hash_limit = capability_intelligence.MAX_SOURCE_BYTES + 32
    hash_limited_source_bytes = b"y" * (hash_limit + 1)
    hash_limited_source_path.write_bytes(hash_limited_source_bytes)
    relative_hash_limited_source = "src/hash-limited-source.mn"
    _git(
        checkout, "add", relative_source, relative_oversized_source,
        relative_hash_limited_source,
    )
    monkeypatch.setattr(
        capability_intelligence, "MAX_SOURCE_HASH_BYTES", hash_limit,
    )
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
    record["supporting_sources"].append({
        "kind": "committed-source",
        "identity": "ev_environment_selected_source_oversized_bad_digest",
        "repository": "mncs-environment",
        "commit": source_commit,
        "path": relative_oversized_source,
        "sha256": "f" * 64,
    })
    hash_limited_source_digest = hashlib.sha256(
        hash_limited_source_bytes,
    ).hexdigest()
    record["supporting_sources"].append({
        "kind": "committed-source",
        "identity": "ev_environment_selected_source_hash_limit",
        "repository": "mncs-environment",
        "commit": source_commit,
        "path": relative_hash_limited_source,
        "sha256": hash_limited_source_digest,
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
    state["evidence"].append({
        "identity": "ev_environment_selected_source_oversized_bad_digest",
        "repository": "mncs-environment",
        "commit": source_commit,
        "path": relative_oversized_source,
        "sha256": "f" * 64,
    })
    state["evidence"].append({
        "identity": "ev_environment_selected_source_hash_limit",
        "repository": "mncs-environment",
        "commit": source_commit,
        "path": relative_hash_limited_source,
        "sha256": hash_limited_source_digest,
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
    assert oversized_source["content_integrity_status"] == (
        "content-integrity-verified"
    )
    assert result["source"]["source_pointer_count"] == 5
    assert result["source"]["source_pointer_content_integrity_verified_count"] == 2
    assert result["source"]["source_pointer_content_integrity_not_verified_count"] == 3
    streamed_digest, streamed_status = capability_intelligence._git_blob_sha256(
        checkout, source_commit, relative_oversized_source,
        deadline=time.monotonic() + 2,
    )
    assert streamed_status == "read"
    assert streamed_digest == oversized_source_digest
    limited_digest, limited_status = capability_intelligence._git_blob_sha256(
        checkout, source_commit, relative_hash_limited_source,
        deadline=time.monotonic() + 2,
        max_bytes=hash_limit,
    )
    assert limited_digest is None
    assert limited_status == "source-object-too-large"


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
    _drop_pressure_hypothesis(record)
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


def test_unselected_compiler_regression_uses_read_only_workspace_authority(
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
            },
            "fresh_process": {
                "cache_status": "hit",
                "execution": {"output_equal_to_stage0": True},
            },
        },
    }
    session, state = _session(tmp_path, [])
    reference = _add_compiler_regression_evidence(session, state, report)
    compiler = Path(session.snapshot["workspace"]["root"]) / "mncs-compiler"
    session.snapshot["selected_checkouts"] = {
        "mncs-environment": session.snapshot["selected_checkouts"][
            "mncs-environment"
        ],
    }
    _register_workspace_source(
        session, compiler, reference, include_source_record=True,
    )

    result = capability_intelligence.project(session)

    assert result["status"] == "ready"
    assert result["observation_count"] == 1
    assert result["source"]["observation_records_verified"] == 1
    observation = result["observations"][0]
    assert observation["evidence_reference"] == reference
    assert observation["observation"]["source_repository_read_basis"] == (
        "workspace-read-only-checkout"
    )
    assert observation["observation"]["source_session_context_resolution"] == (
        "no-matching-verified-supporting-observation"
    )
    assert observation["observation"]["outcome"][
        "language_effect_and_capability_dependencies_invalidated"
    ] is True
    assert session.snapshot["selected_checkouts"].keys() == {"mncs-environment"}
    assert session.snapshot["authority"]["writable"] == ["mncs-environment"]
    assert _git(compiler, "status", "--porcelain") == ""
    compact = capability_intelligence._compact_projection(
        result,
        capability_intelligence.MAX_RESULT_BYTES + 1,
        capability_intelligence.MAX_RESULT_BYTES,
    )
    compact_observation = compact["observations"][0]["observation"]
    assert compact_observation["details_omitted"] is True
    assert compact_observation["source_repository_read_basis"] == (
        "workspace-read-only-checkout"
    )


def test_unselected_declared_campaign_repository_is_read_only_evidence_source(
    tmp_path: Path,
) -> None:
    record = _observation(
        "ev_environment_declared_campaign_observation",
        "ses_declared_campaign_observation",
        "cmp_declared_campaign_observation",
    )
    session, _state = _session(tmp_path, [record])
    checkout = Path(session.snapshot["selected_checkouts"]["mncs-environment"]["path"])
    head = _git(checkout, "rev-parse", "HEAD")
    session.snapshot["selected_checkouts"] = {}
    session.snapshot["intent"]["repositories"] = ["mncs-environment"]
    session.snapshot["authority"] = {
        "readable": ["workspace"],
        "writable": [],
    }
    session.snapshot["workspace"].update({
        "scan": {"status": "complete"},
        "repositories": [{
            "name": "mncs-environment",
            "manifest_repository": "mncs-environment",
            "path": str(checkout),
            "head": head,
            "worktree_of": None,
            "git_error": None,
        }],
    })

    result = capability_intelligence.project(session)

    assert result["status"] == "ready"
    assert result["observation_count"] == 1
    assert result["source"]["observation_reference_diagnostic_count"] == 0
    observation = result["observations"][0]
    assert observation["identity"] == record["identity"]
    assert observation["evidence_reference"]["identity"] == record["identity"]
    assert observation["observation"]["source_repository_read_basis"] == (
        "workspace-read-only-checkout"
    )
    candidate = result["context_pressure_candidates"][0]
    assert candidate["basis"][
        "workspace_read_only_observation_reference_count"
    ] == 1
    assert candidate["workspace_read_only_evidence_references"] == [{
        "identity": record["identity"],
        "repository": "mncs-environment",
        "commit": observation["evidence_reference"]["commit"],
        "path": observation["evidence_reference"]["path"],
        "sha256": observation["evidence_reference"]["sha256"],
        "verification": "verified",
        "repository_read_basis": "workspace-read-only-checkout",
    }]
    assert "does not show that work is blocked" in candidate[
        "recommendation"
    ]
    compact = capability_intelligence.project(session, max_result_bytes=6_000)
    compact_candidate = compact["context_pressure_candidates"][0]
    assert compact_candidate["workspace_read_only_evidence_references"][0][
        "identity"
    ] == record["identity"]
    assert compact_candidate["workspace_read_only_evidence_references"][0][
        "repository_read_basis"
    ] == "workspace-read-only-checkout"
    assert session.snapshot["selected_checkouts"] == {}
    assert _git(checkout, "status", "--porcelain") == ""


def test_unselected_campaign_repository_without_intent_stays_unread(
    tmp_path: Path,
) -> None:
    record = _observation(
        "ev_environment_undeclared_campaign_observation",
        "ses_undeclared_campaign_observation",
        "cmp_undeclared_campaign_observation",
    )
    session, _state = _session(tmp_path, [record])
    checkout = Path(session.snapshot["selected_checkouts"]["mncs-environment"]["path"])
    head = _git(checkout, "rev-parse", "HEAD")
    session.snapshot["selected_checkouts"] = {}
    session.snapshot["intent"]["repositories"] = []
    session.snapshot["authority"] = {"readable": ["workspace"], "writable": []}
    session.snapshot["workspace"].update({
        "scan": {"status": "complete"},
        "repositories": [{
            "name": "mncs-environment",
            "manifest_repository": "mncs-environment",
            "path": str(checkout),
            "head": head,
            "worktree_of": None,
            "git_error": None,
        }],
    })

    result = capability_intelligence.project(session)

    assert result["observation_count"] == 0
    assert result["source"]["observation_reference_diagnostic_count"] == 1
    diagnostic = result["source"]["observation_reference_diagnostics"][0]
    assert diagnostic["identity"] == record["identity"]
    assert diagnostic["verification"] == "outside-selected-session"
    assert session.snapshot["selected_checkouts"] == {}


def test_observation_cannot_self_assert_read_only_workspace_basis(
    tmp_path: Path,
) -> None:
    record = _observation(
        "ev_environment_untrusted_read_basis",
        "ses_untrusted_read_basis",
        "cmp_untrusted_read_basis",
    )
    record["observation"]["source_repository_read_basis"] = (
        "workspace-read-only-checkout"
    )
    session, _state = _session(tmp_path, [record])

    result = capability_intelligence.project(session)

    assert result["observation_count"] == 1
    assert "source_repository_read_basis" not in result["observations"][0][
        "observation"
    ]


def test_compiler_regression_projects_source_verification_resource_measurements(
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
            "cold": {"cache_status": "miss"},
            "generic_specialization_cold": {"cache_status": "miss"},
            "generic_summarize_body_edit": {"cache_status": "miss"},
            "imported_body_edit": {"cache_status": "hit"},
            "fresh_process": {"cache_status": "hit"},
        },
        "verification": {
            "harness_process_measurement": {
                "wall_seconds": 11.11,
                "user_seconds": 8.23,
                "system_seconds": 3.0,
                "peak_rss_kib": 98644,
                "peak_cgroup_memory_bytes": None,
            },
            "stage0_bootstrap_measurement": {
                "cargo_build_jobs": 2,
                "cpu_percent": 185,
                "wall_seconds": 230.6,
                "user_seconds": 420.77,
                "system_seconds": 6.1,
                "peak_rss_kib": 1369204,
                "note": (
                    "one-time isolated target compilation; "
                    "excluded from this warm harness run"
                ),
            },
        },
    }
    session, state = _session(tmp_path, [])
    _add_compiler_regression_evidence(session, state, report)

    result = capability_intelligence.project(session)

    assert result["status"] == "ready"
    assert result["pressure_hypothesis_count"] == 0
    observation = result["observations"][0]
    assert observation["observation"]["measurement_availability"][
        "resource_consumption"
    ] == (
        "per-build + verification-process metrics; not agent-attributed"
    )
    measurement_summary = observation["observation"]["outcome"][
        "measurement_summary"
    ]
    assert measurement_summary["direct_measurements"] == {
        "harness_process_peak_rss_kib": 98644,
        "harness_process_system_seconds": 3.0,
        "harness_process_user_seconds": 8.23,
        "harness_process_wall_seconds": 11.11,
        "stage0_bootstrap_cpu_percent": 185,
        "stage0_bootstrap_peak_rss_kib": 1369204,
        "stage0_bootstrap_system_seconds": 6.1,
        "stage0_bootstrap_user_seconds": 420.77,
        "stage0_bootstrap_wall_seconds": 230.6,
    }
    assert measurement_summary["observed_counts"] == {
        "stage0_bootstrap_cargo_build_jobs": 2,
    }
    assert measurement_summary["measurement_availability"] == {
        "harness_process_peak_cgroup_memory_bytes": "null-in-source-record",
    }
    assert "derived_measurements" not in measurement_summary
    assert any(
        "excluded from this warm harness run" in item
        for item in measurement_summary["qualifications"]
    )
    assert any(
        "not measurements of resource use attributable to the Compiler agent session"
        in item
        for item in measurement_summary["qualifications"]
    )

    full_size = len(json.dumps(result, ensure_ascii=False, sort_keys=True).encode())
    compact = capability_intelligence.project(
        session, max_result_bytes=full_size - 1,
    )
    assert compact["source"]["response_truncated"] is True
    assert compact["observations"][0]["observation"]["measurement_summary"] == (
        measurement_summary
    )


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


def test_compiler_native_application_proof_projects_scoped_implementation_ownership(
    tmp_path: Path,
) -> None:
    session, state = _session(tmp_path, [])
    reference = _add_compiler_native_application_proof_evidence(session, state)
    binding_id = "cap_" + "a" * 64
    available_identity = {
        "capability": "test_artifact",
        "contract_revision": "r1",
        "provider": "mncs-test",
    }
    session.snapshot["bindings"] = [{
        "binding_id": binding_id,
        "provider": "mncs-test",
        "capability": "test_artifact",
        "contract_revision": "r1",
        "availability": {"status": "available"},
    }]
    events = [
        {
            "identity": "evt_test_artifact_context",
            "sequence": 1,
            "type": "environment.rpc.read_observed",
            "producer": "mncs-control-mcp",
            "payload": {
                "schema_version": "mncs.environment.rpc-read-activity/2",
                "operation": "session.context",
                "outcome_status": "succeeded",
                "exit_code": 0,
                "failure_code": None,
                "request_id": "req_test_artifact_context",
                "request_digest": "d" * 64,
                "request_operation_digest": "d" * 64,
                "request_event_digest": "e" * 16,
                "caller": {
                    "authenticated_principal_id": "ctrl_" + "a" * 32,
                    "control_grant_id": "b" * 32,
                    "process_identity": "proc_" + "c" * 32,
                },
                "context": {
                    "repository": "mncs-environment",
                    "repository_branch": "main",
                    "repository_head": "f" * 40,
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
                "measurements": {"control_dispatch_elapsed_ms": 12.5},
                "context_response_summary": {
                    "schema_version": (
                        "mncs.environment.context-response-summary/2"
                    ),
                    "capture_status": "captured",
                    "capture_reason": None,
                    "context_schema_version": "mncs.environment.entry-context/1",
                    "available_count": 1,
                    "listed_available_count": 1,
                    "unavailable_count": 0,
                    "unknown_count": 0,
                    "capabilities_truncated": False,
                    "available_identity_digest": hashlib.sha256(json.dumps(
                        [available_identity], sort_keys=True,
                        separators=(",", ":"),
                    ).encode()).hexdigest(),
                    "identity_digest_status": "complete",
                    "available_binding_ids": [binding_id],
                    "binding_ids_status": "complete",
                },
            },
        },
        {
            "identity": "evt_test_artifact_invoked",
            "sequence": 2,
            "type": "capability.invoked",
            "producer": "local-agent",
            "observed_at": "2026-10-25T00:00:00Z",
            "payload": {
                "capability": "test_artifact",
                "provider_request_id": "req_test_artifact",
                "argv": ["private-test-argument"],
            },
        },
        {
            "identity": "evt_test_artifact_completed",
            "sequence": 3,
            "type": "invocation.completed",
            "producer": "mncs-test",
            "observed_at": "2026-10-25T00:00:01Z",
            "payload": {
                "capability": "test_artifact",
                "status": "succeeded",
                "returncode": 0,
                "provider_request_id": "req_test_artifact",
            },
        },
    ]
    session.recent_events = lambda *, limit, before_sequence=None: {
        "event_count": len(events),
        "events": events[-limit:],
        "truncated": False,
        "next_before_sequence": None,
    }
    root = Path(session.snapshot["workspace"]["root"])
    session.snapshot["selected_checkouts"] = {
        "mncs-environment": session.snapshot["selected_checkouts"][
            "mncs-environment"
        ],
    }
    session.snapshot["authority"] = {"readable": ["workspace"]}
    session.snapshot["workspace"].update({
        "scan": {"status": "complete"},
        "repositories": [
            {
                "name": repository,
                "path": str(root / repository),
                "head": _git(root / repository, "rev-parse", "HEAD"),
                "worktree_of": None,
                "git_error": None,
            }
            for repository in ("mncs-compiler", "mncs-stdlib", "mncs-test")
        ],
    })

    result = capability_intelligence.project(session)

    assert result["status"] == "ready"
    assert result["observation_count"] == 1
    observation = result["observations"][0]
    assert observation["identity"] == reference["identity"]
    assert observation["observation"]["kind"] == (
        "compiler-native-application-implementation-ownership"
    )
    assert observation["related_work"] == {}
    accountability = observation["observation"]["outcome"][
        "implementation_accountability"
    ]
    assert accountability["native_semantic_ownership"] == (
        "verified-for-this-application-proof"
    )
    assert accountability["native_module_count"] == 4
    assert accountability["canonical_vm_case_success_count"] == 3
    assert accountability["native_migration_opportunity"] == "not-established"
    capability_contract = accountability["compiler_capability_contract"]
    assert capability_contract["required_identity"] == (
        "mncs:0.2:capability:mncs.test.digest::digest_app::test_artifact"
    )
    assert capability_contract["runtime_dispatch_alias"] == "test_artifact"
    assert [effect["kind"] for effect in capability_contract["effect_contract"]] == [
        "structured_read", "structured_write",
    ]
    assert {
        effect["capability"]
        for effect in capability_contract["effect_contract"]
    } == {"test_artifact"}
    assert capability_contract["proof_relationship"] == (
        "compiler-artifact-admission-matches"
    )
    assert capability_contract["environment_delivery"] == (
        "not-measured-by-this-compiler-proof"
    )
    assert "not attributed" in accountability["evidence_relationship_basis"]
    assert accountability["host_test_adapter"]["classification"] == (
        "test-only-provider-adapter"
    )
    assert observation["measurement_basis"].endswith(
        "workspace-read-only-checkout"
    )
    module_sources = [
        source for source in observation["supporting_sources"]
        if source.get("kind") == "native-language-module-source"
    ]
    assert {source["module"] for source in module_sources} == {
        module for module, _repository, _path, _revision_field
        in capability_intelligence.COMPILER_NATIVE_APPLICATION_MODULES
    }
    assert all(source["content_integrity_status"] == (
        "workspace-read-only-content-integrity-verified"
    ) for source in module_sources)

    projected_accountability = result["implementation_accountability"]
    assert projected_accountability["sample_count"] == 1
    projected_sample = projected_accountability["samples"][0]
    assert projected_sample["ownership_status"] == (
        "source-integrity-verified-for-sample"
    )
    scoped_ownership = projected_sample["scoped_native_module_ownership"]
    assert scoped_ownership["status"] == "measured"
    assert scoped_ownership["verified_native_module_count"] == 4
    assert scoped_ownership["module_denominator"] == 4
    assert scoped_ownership["host_semantic_implementation_count"]["status"] == (
        "not-measured"
    )
    assert scoped_ownership["host_test_adapter_included_in_denominator"] is False
    assert "not a functionality-weighted or systemwide adoption ratio" in (
        scoped_ownership["interpretation_limit"]
    )
    ratio = projected_accountability["systemwide_native_adoption_ratio"]
    assert ratio["status"] == "not-measured"
    assert "denominator" in ratio["reason"]

    unverified_module = module_sources[0]["module"]
    partial_observation = {
        **observation,
        "supporting_sources": [
            {
                **source,
                **({"content_integrity_status": "source-content-not-verified"}
                   if source.get("kind") == "native-language-module-source"
                   and source.get("module") == unverified_module else {}),
            }
            for source in observation["supporting_sources"]
        ],
    }
    partial_projection = (
        capability_intelligence._implementation_accountability_projection(
            [partial_observation],
            native_proof_reference_present=True,
        )
    )
    assert partial_projection is not None
    partial_scope = partial_projection["samples"][0][
        "scoped_native_module_ownership"
    ]
    assert partial_scope["status"] == "partial"
    assert partial_scope["verified_native_module_count"] == 3
    assert partial_scope["module_denominator"] == 4

    duplicate_source_module = module_sources[1]["module"]
    duplicate_observation = {
        **observation,
        "supporting_sources": [
            {
                **source,
                **({"module": duplicate_source_module}
                   if source.get("kind") == "native-language-module-source"
                   and source.get("module") == module_sources[0]["module"] else {}),
            }
            for source in observation["supporting_sources"]
        ],
    }
    duplicate_projection = (
        capability_intelligence._implementation_accountability_projection(
            [duplicate_observation],
            native_proof_reference_present=True,
        )
    )
    assert duplicate_projection is not None
    duplicate_scope = duplicate_projection["samples"][0][
        "scoped_native_module_ownership"
    ]
    assert duplicate_scope["status"] == "partial"
    assert duplicate_scope["verified_native_module_count"] == 3
    assert all(source["content_integrity_status"] in (
        capability_intelligence.SOURCE_INTEGRITY_VERIFIED
    ) for source in duplicate_observation["supporting_sources"]
               if source.get("kind") == "native-language-module-source")
    assert projected_accountability["samples"][0][
        "native_migration_opportunity"
    ] == "not-established"
    projected_contract = projected_accountability["samples"][0][
        "compiler_capability_contract"
    ]
    assert projected_contract["required_identity"] == (
        capability_contract["required_identity"]
    )
    assert projected_contract["effect_contract"] == capability_contract[
        "effect_contract"
    ]
    delivery = projected_accountability["samples"][0][
        "selected_session_delivery_observation"
    ]
    assert delivery["status"] == "runtime-alias-only-match"
    assert delivery["match_kind"] == "runtime-alias-only"
    assert delivery["session_id"] == session.session_id
    assert delivery["required_identity"] == capability_contract[
        "required_identity"
    ]
    assert delivery["runtime_alias_match_count"] == 1
    assert delivery["canonical_identity_match_count"] == 0
    assert delivery["binding_matches"][0]["availability_status"] == "available"
    assert delivery["binding_matches"][0]["binding_id"] == binding_id
    context_inclusion = delivery["binding_matches"][0][
        "context_response_inclusion"
    ]
    assert context_inclusion["status"] == "exact-binding-id-in-response"
    assert context_inclusion["binding_id_attribution_status"] == (
        "exact-from-response"
    )
    assert context_inclusion["supporting_events"][0]["identity"] == (
        "evt_test_artifact_context"
    )
    assert delivery["binding_matches"][0]["invocation_count"] == 1
    assert delivery["binding_matches"][0]["completion_count"] == 1
    assert delivery["binding_matches"][0]["supporting_events"][0]["identity"] == (
        "evt_test_artifact_invoked"
    )
    assert delivery["discoverability_status"] == "not-measured"
    assert delivery["agent_receipt_status"] == "not-established"

    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True).encode()
    compact = capability_intelligence._compact_projection(
        result, len(encoded), 4096,
    )
    compact_accountability = compact["implementation_accountability"]
    assert compact_accountability["systemwide_native_adoption_ratio"][
        "status"
    ] == "not-measured"
    assert compact_accountability["samples"][0]["native_module_count"] == 4
    assert compact_accountability["samples"][0][
        "scoped_native_module_ownership"
    ] == scoped_ownership
    assert compact_accountability["samples"][0]["compiler_proof_reference"][
        "identity"
    ] == reference["identity"]
    assert compact_accountability["samples"][0]["compiler_capability_contract"][
        "environment_delivery"
    ] == "not-measured-by-this-compiler-proof"
    compact_delivery = compact_accountability["samples"][0][
        "selected_session_delivery_observation"
    ]
    assert compact_delivery["status"] == "runtime-alias-only-match"
    assert compact_delivery["binding_matches"][0][
        "context_response_inclusion"
    ]["status"] == "exact-binding-id-in-response"
    assert compact_delivery["binding_matches"][0]["supporting_events"][0][
        "identity"
    ] == "evt_test_artifact_invoked"


def test_native_userland_owner_audit_is_actionable_without_claiming_verified_ownership(
    tmp_path: Path,
) -> None:
    source_path = (
        Path(__file__).resolve().parent.parent
        / "evidence/environmental-capability/native-userland-accountability-20261025.json"
    )
    report = json.loads(source_path.read_text(encoding="utf-8"))
    session, _state = _session(tmp_path, [report])

    result = capability_intelligence.project(session)

    accountability = result["implementation_accountability"]
    assert accountability["sample_count"] == 0
    assert accountability["systemwide_native_adoption_ratio"]["status"] == (
        "not-measured"
    )
    owner_evidence = accountability["owner_audit_evidence"]
    assert owner_evidence["observed_audit_record_count"] == 1
    assert owner_evidence["implementation_ownership_status"] == (
        "owner-declared; not independently verified"
    )
    assert owner_evidence["systemwide_native_adoption_ratio"]["status"] == (
        "not-measured"
    )

    audit = owner_evidence["audits"][0]
    assert audit["identity"] == report["identity"]
    assert audit["source_declared_audit_count"] == 2
    assert audit["systemwide_native_adoption_ratio"]["denominator"] == (
        "not-established"
    )
    assert audit["source_interpretation"].startswith("repository-owner-declared")
    assert {project["repository"] for project in audit["projects"]} == {
        "mncs-actions", "mncs-test",
    }
    assert all(project["owner_declared_status"] == "native_canonical"
               for project in audit["projects"])
    assert audit["actionable_opportunity"]["readiness"].startswith(
        "not-established"
    )
    assert "preserve the host adapter" in audit["actionable_opportunity"][
        "safe_next_step"
    ]
    assert audit["evidence_reference"]["identity"] == report["identity"]
    assert audit["supporting_source_references"][0]["identity"] == (
        "audit_mncs_actions_native_userland_20261025"
    )
    assert audit["supporting_source_references"][0][
        "content_integrity_status"
    ] != "content-integrity-verified"
    assert audit["source_audit_details_status"] == (
        "owner-audit-source-content-unavailable"
    )
    assert all(
        detail["source_record_status"] == "source-content-not-verified"
        and "remaining_host_responsibilities" not in detail
        for detail in audit["source_audit_details"]
    )

    compact = capability_intelligence._compact_implementation_accountability(
        accountability,
    )
    compact_audit = compact["owner_audit_evidence"]["audits"][0]
    assert compact["systemwide_native_adoption_ratio"]["status"] == (
        "not-measured"
    )
    assert compact_audit["evidence_reference"]["identity"] == report["identity"]
    assert compact_audit["source_interpretation"].startswith(
        "repository-owner-declared"
    )
    assert compact_audit["actionable_opportunity"]["readiness"].startswith(
        "not-established"
    )


def test_native_userland_audit_projection_retains_verified_owner_blockers(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    audit_records = {
        "mncs-actions": {
            "schema_version": "mncs.native-userland-audit/1",
            "repository": "mncs-actions",
            "status": "native_canonical",
            "canonical_mncs_surfaces": ["typed receipts and evidence records"],
            "migration_order": 2,
            "next_native_slice": "native receipt and evidence publication",
            "existing_pressures": ["MNCS-TOOLING-146FB6F4BBC6"],
            "native_entrypoint": "native/mncs/actions/family.mncs",
            "remaining_host_responsibilities": [{
                "area": "external evidence JSON decoding and receipt packaging",
                "classification": "boundary_adapter",
                "eliminating_capability": (
                    "native filesystem reads and receipt/evidence artifact publication"
                ),
            }],
        },
        "mncs-test": {
            "schema_version": "mncs.native-userland-audit/1",
            "repository": "mncs-test",
            "status": "native_canonical",
            "migration_order": 1,
            "next_native_slice": "native Commons pressure query materialization",
            "existing_pressures": [
                "MNCS-TEST-P-004", "MNCS-TEST-P-006",
                "MNCS-TOOLING-146FB6F4BBC6", "MNCS-LANG-52A5E0C72A39",
                "MNCS-TEST-P-999",
            ],
            "native_semantic_projection": {
                "test": "mncs.test.assertions.project_test",
                "suite": "mncs.test.suite.project",
                "rust_boundary_only": ["external TestResult JSON envelope"],
            },
            "verification_plan_ingestion": {
                "status": "native_structural_admission",
                "entrypoint": "mncs test --verification-plan FILE",
                "selection_authority": "native obligation-selection kernel",
                "shell_projection": False,
                "jq_required": False,
                "remaining_blocker": "Commons plan publication remains external",
            },
            "responsibilities": [{
                "area": "TestResult construction and failure classification",
                "classification": "already_naturally_expressible_in_mncs",
                "canonical_owner": "mncs.test.assertions and mncs.test.suite",
            }],
            "remaining_host_paths": [{
                "path": "tools/mncs_test.py",
                "role": "JSON/process transport and compatibility adapter",
            }],
        },
    }
    audit_references = []
    pressure_references = []
    workspace_repositories = []
    for repository, record in audit_records.items():
        checkout = root / repository
        checkout.mkdir()
        _git(checkout, "init", "--quiet")
        _git(checkout, "config", "user.name", "test")
        _git(checkout, "config", "user.email", "test@example.invalid")
        relative = "native-userland-audit.json"
        raw = json.dumps(record, sort_keys=True, separators=(",", ":")).encode()
        (checkout / relative).write_bytes(raw)
        _git(checkout, "add", relative)
        if repository == "mncs-test":
            for identity, kind, commons_pressure_id in (
                (
                    "MNCS-TEST-P-004",
                    "repository-owner-language-and-runtime-pressure",
                    "MNCS-LANG-52A5E0C72A39",
                ),
                (
                    "MNCS-TEST-P-006",
                    "repository-owner-native-result-transport-pressure",
                    "MNCS-TOOLING-146FB6F4BBC6",
                ),
            ):
                pressure_path = f"pressures/{identity}.json"
                pressure_record = {
                    "schema_version": "mncs-test.pressure/1",
                    "id": identity,
                    "commons_pressure_id": commons_pressure_id,
                }
                pressure_raw = json.dumps(
                    pressure_record, sort_keys=True, separators=(",", ":"),
                ).encode()
                target = checkout / pressure_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(pressure_raw)
                _git(checkout, "add", pressure_path)
        _git(checkout, "commit", "--quiet", "-m", "owner audit")
        commit = _git(checkout, "rev-parse", "HEAD")
        audit_references.append({
            "identity": f"audit_{repository.replace('-', '_')}_native_userland_20261025",
            "kind": "repository-owner-native-userland-audit",
            "repository": repository,
            "commit": commit,
            "path": relative,
            "sha256": hashlib.sha256(raw).hexdigest(),
        })
        if repository == "mncs-test":
            for identity, kind in (
                ("MNCS-TEST-P-004", "repository-owner-language-and-runtime-pressure"),
                ("MNCS-TEST-P-006", "repository-owner-native-result-transport-pressure"),
            ):
                pressure_path = f"pressures/{identity}.json"
                pressure_raw = (checkout / pressure_path).read_bytes()
                pressure_references.append({
                    "identity": identity,
                    "kind": kind,
                    "repository": repository,
                    "commit": commit,
                    "path": pressure_path,
                    "sha256": hashlib.sha256(pressure_raw).hexdigest(),
                })
        workspace_repositories.append({
            "name": repository,
            "path": str(checkout),
            "head": commit,
            "worktree_of": None,
            "git_error": None,
        })

    source_path = (
        Path(__file__).resolve().parent.parent
        / "evidence/environmental-capability/native-userland-accountability-20261025.json"
    )
    report = json.loads(source_path.read_text(encoding="utf-8"))
    report["supporting_sources"] = audit_references + pressure_references
    session, _state = _session(tmp_path, [report])
    session.snapshot["authority"] = {"readable": ["workspace"]}
    session.snapshot["workspace"].update({
        "scan": {"status": "complete"},
        "repositories": workspace_repositories,
    })

    result = capability_intelligence.project(session)

    assert result["status"] == "ready"
    accountability = result["implementation_accountability"]
    assert accountability["systemwide_native_adoption_ratio"]["status"] == (
        "not-measured"
    )
    audit = accountability["owner_audit_evidence"]["audits"][0]
    assert audit["source_audit_details_status"] == (
        "content-integrity-verified-owner-declarations-available"
    )
    details = {item["repository"]: item for item in audit["source_audit_details"]}
    assert set(details) == {"mncs-actions", "mncs-test"}
    actions = details["mncs-actions"]
    assert actions["source_record_status"] == (
        "content-integrity-verified-owner-declaration"
    )
    assert actions["canonical_mncs_surfaces"] == [
        "typed receipts and evidence records"
    ]
    assert actions["existing_pressures"] == ["MNCS-TOOLING-146FB6F4BBC6"]
    assert accountability["owner_audit_evidence"][
        "implementation_ownership_status"
    ] == (
        "owner-declared; not independently verified"
    )
    actions_reference = next(
        source for source in audit["supporting_source_references"]
        if source["identity"] == actions["identity"]
    )
    assert actions_reference["content_integrity_status"] == (
        "workspace-read-only-content-integrity-verified"
    )
    assert actions["remaining_host_responsibilities"][0]["classification"] == (
        "boundary_adapter"
    )
    assert "native filesystem reads" in actions[
        "remaining_host_responsibilities"
    ][0]["eliminating_capability"]
    test_audit = details["mncs-test"]
    assert test_audit["existing_pressures"] == [
        "MNCS-TEST-P-004", "MNCS-TEST-P-006",
        "MNCS-TOOLING-146FB6F4BBC6", "MNCS-LANG-52A5E0C72A39",
        "MNCS-TEST-P-999",
    ]
    pressure_links = test_audit["pressure_source_links"]
    assert pressure_links["status"] == "partially-linked"
    assert pressure_links["causal_relation_status"] == "not-established"
    links = {item["identity"]: item for item in pressure_links["links"]}
    assert links["MNCS-TEST-P-004"]["status"] == (
        "linked-to-integrity-verified-pressure-source"
    )
    assert links["MNCS-TEST-P-004"]["source_reference"]["path"] == (
        "pressures/MNCS-TEST-P-004.json"
    )
    assert links["MNCS-TEST-P-006"]["status"] == (
        "linked-to-integrity-verified-pressure-source"
    )
    assert links["MNCS-TOOLING-146FB6F4BBC6"]["status"] == (
        "linked-through-integrity-verified-pressure-alias"
    )
    assert links["MNCS-TOOLING-146FB6F4BBC6"]["relationship_basis"] == (
        "mncs-test.pressure/1.commons_pressure_id"
    )
    assert links["MNCS-TOOLING-146FB6F4BBC6"]["source_reference"][
        "identity"
    ] == "MNCS-TEST-P-006"
    assert links["MNCS-LANG-52A5E0C72A39"]["status"] == (
        "linked-through-integrity-verified-pressure-alias"
    )
    assert links["MNCS-TEST-P-999"]["status"] == (
        "no-matching-supporting-source-reference"
    )
    assert all(item["causal_relation_status"] == "not-established"
               for item in pressure_links["links"])
    assert test_audit["responsibilities"][0]["classification"] == (
        "already_naturally_expressible_in_mncs"
    )
    assert test_audit["verification_plan_ingestion"]["shell_projection"] is False
    assert test_audit["verification_plan_ingestion"]["jq_required"] is False
    assert test_audit["remaining_host_paths"][0]["path"] == "tools/mncs_test.py"

    compact = capability_intelligence._compact_implementation_accountability(
        accountability,
    )
    compact_audits = compact["owner_audit_evidence"]["audits"][0][
        "source_audit_details"
    ]
    compact_by_repository = {item["repository"]: item for item in compact_audits}
    assert compact_by_repository["mncs-actions"][
        "canonical_mncs_surfaces"
    ] == actions["canonical_mncs_surfaces"]
    assert compact_by_repository["mncs-actions"][
        "existing_pressures"
    ] == actions["existing_pressures"]
    assert compact_by_repository["mncs-actions"][
        "remaining_host_responsibilities"
    ][0]["eliminating_capability"] == actions[
        "remaining_host_responsibilities"
    ][0]["eliminating_capability"]
    compact_pressure_links = compact_by_repository["mncs-test"][
        "pressure_source_links"
    ]
    assert compact_pressure_links["links"][0]["source_reference"]["sha256"] == (
        links["MNCS-TEST-P-004"]["source_reference"]["sha256"]
    )
    assert compact_pressure_links["links"][2]["status"] == (
        "linked-through-integrity-verified-pressure-alias"
    )
    assert compact_pressure_links["links"][2]["relationship_basis"] == (
        "mncs-test.pressure/1.commons_pressure_id"
    )
    assert compact["systemwide_native_adoption_ratio"]["status"] == (
        "not-measured"
    )


def test_native_capability_correspondence_links_candidate_without_claiming_migration():
    pressure_source = {
        "identity": "MNCS-TEST-P-001",
        "kind": "mncs-test-pressure-source",
        "repository": "mncs-test",
        "commit": "a" * 40,
        "path": "pressures/MNCS-TEST-P-001.json",
        "sha256": "1" * 64,
    }
    sources = [
        pressure_source,
        {
            "identity": "process_contract",
            "kind": "native-language-capability-contract",
            "repository": "mncs-language",
            "commit": "b" * 40,
            "path": "library/std/process.mncs",
            "sha256": "2" * 64,
        },
        {
            "identity": "process_provider",
            "kind": "native-runtime-provider-implementation",
            "repository": "mncs-language",
            "commit": "b" * 40,
            "path": "crates/mncs-model/src/process_runtime.rs",
            "sha256": "3" * 64,
        },
        {
            "identity": "process_dispatch",
            "kind": "native-runtime-operation-dispatch",
            "repository": "mncs-language",
            "commit": "b" * 40,
            "path": "crates/mncs-model/src/execution.rs",
            "sha256": "4" * 64,
        },
        {
            "identity": "host_readiness_review",
            "kind": "host-boundary-readiness-document",
            "repository": "mncs-compiler",
            "commit": "c" * 40,
            "path": "docs/native-tool-conversion-readiness.md",
            "sha256": "5" * 64,
        },
    ]
    pressure_raw = json.dumps({
        "schema_version": "mncs-test.pressure/1",
        "id": "MNCS-TEST-P-001",
        "commons_pressure_id": "MNCS-TOOLING-B665F138D324",
    }, sort_keys=True, separators=(",", ":")).encode()
    source_integrity = {
        capability_intelligence._source_pointer_key(source): (
            "content-integrity-verified"
        )
        for source in sources
    }
    record = {
        "identity": "ev_environment_native_process_correspondence_20261025",
        "observation": {
            "kind": capability_intelligence.NATIVE_CAPABILITY_CORRESPONDENCE_KIND,
            "outcome": {
                "target_pressure_identity": "MNCS-TOOLING-B665F138D324",
                "native_capability_identity": "mncs.std.process.v1",
                "relationship_assessment": "candidate-correspondence",
                "implementation_boundary": {
                    "mncs_language_surface": "MNCS typed process API",
                    "host_runtime_provider": "Rust process runtime provider",
                    "consumer_boundary": "MNCS-Test host process adapter",
                },
                "verification_observation": {
                    "status": "passed",
                    "command": "cargo test -p mncs-model --lib process_runtime::tests::lifecycle_handle_is_provider_issued_and_cancel_requires_reap -- --exact --nocapture",
                    "test_identity": "process_runtime::tests::lifecycle_handle_is_provider_issued_and_cancel_requires_reap",
                    "result_summary": "1 passed; 0 failed; 255 filtered; 0.05 seconds",
                    "passed": 1,
                    "failed": 0,
                    "filtered": 255,
                    "unsupported_branch_may_return_early": True,
                    "interpretation_limit": "The provider test returns early when the platform reports Unsupported.",
                },
                "next_investigation": "Establish end-to-end parity and discoverability for a real MNCS consumer before recommending host-boundary migration.",
            },
        },
        "supporting_sources": sources,
    }
    projected = capability_intelligence._implementation_accountability_projection(
        [record],
        native_proof_reference_present=False,
        owner_audit_source_payloads={
            capability_intelligence._source_pointer_key(pressure_source): pressure_raw,
        },
        source_integrity=source_integrity,
    )

    assert projected is not None
    assert projected["sample_count"] == 0
    correspondence = projected["native_capability_correspondences"][0]
    assert correspondence["source_evidence_status"] == (
        "required-source-blobs-and-pressure-alias-verified"
    )
    assert correspondence["native_capability_identity"] == "mncs.std.process.v1"
    assert correspondence["relationship_assessment"] == "candidate-correspondence"
    assert correspondence["pressure_source_links"]["links"][0]["status"] == (
        "linked-through-integrity-verified-pressure-alias"
    )
    assert correspondence["pressure_source_links"]["links"][0][
        "source_reference"]["identity"] == "MNCS-TEST-P-001"
    assert correspondence["source_evidence_by_role"]["host_runtime_provider"][
        "status"
    ] == "content-integrity-verified"
    assert correspondence["verification_observation"][
        "unsupported_branch_may_return_early"
    ] is True
    assert correspondence["agent_usage_status"] == "not-measured"
    assert correspondence["causal_relation_status"] == "not-established"
    assert correspondence["native_migration_readiness"] == "not-established"
    assert correspondence["systemwide_native_adoption_ratio"]["status"] == (
        "not-measured"
    )

    compact = capability_intelligence._compact_implementation_accountability(
        projected,
    )
    compact_correspondence = compact[
        "native_capability_correspondences"
    ][0]
    assert compact_correspondence["source_evidence_by_role"][
        "language_contract"]["source_references"][0]["path"] == (
            "library/std/process.mncs"
        )
    assert compact_correspondence["pressure_source_links"]["links"][0][
        "source_reference"]["identity"] == "MNCS-TEST-P-001"
    assert compact_correspondence["native_migration_readiness"] == (
        "not-established"
    )


def test_owner_declared_pressure_links_keep_ambiguity_and_bounded_absence_explicit(
) -> None:
    def source(identity: str, path: str, kind: str) -> dict[str, str]:
        return {
            "identity": identity,
            "kind": kind,
            "repository": "mncs-test",
            "commit": "a" * 40,
            "path": path,
            "sha256": hashlib.sha256(path.encode()).hexdigest(),
        }

    duplicate_one = source(
        "MNCS-TEST-P-DUP", "pressures/first.json",
        "repository-owner-language-pressure",
    )
    duplicate_two = source(
        "MNCS-TEST-P-DUP", "pressures/second.json",
        "repository-owner-language-pressure",
    )
    non_pressure = source(
        "audit_mncs_test_native_userland_20261025",
        "native-userland-audit.json", "repository-owner-native-userland-audit",
    )
    same_pointer_pressure = source(
        "MNCS-TEST-P-KIND", "pressures/kind.json",
        "repository-owner-language-pressure",
    )
    same_pointer_audit = {
        **same_pointer_pressure,
        "kind": "repository-owner-native-userland-audit",
    }
    complete = capability_intelligence._owner_declared_pressure_links(
        [
            "MNCS-TEST-P-DUP", "audit_mncs_test_native_userland_20261025",
            "MNCS-TEST-P-KIND", "MISSING",
        ],
        [
            duplicate_one, duplicate_two, non_pressure,
            same_pointer_pressure, same_pointer_audit,
        ], {},
    )
    assert complete is not None
    statuses = {item["identity"]: item["status"] for item in complete["links"]}
    assert statuses["MNCS-TEST-P-DUP"] == (
        "ambiguous-supporting-source-references"
    )
    assert statuses["audit_mncs_test_native_userland_20261025"] == (
        "identity-matches-non-pressure-source"
    )
    assert statuses["MNCS-TEST-P-KIND"] == (
        "ambiguous-supporting-source-kinds"
    )
    assert statuses["MISSING"] == "no-matching-supporting-source-reference"

    bounded_sources = [
        source(f"unrelated-{index}", f"records/{index}.json", "other-source")
        for index in range(
            capability_intelligence.MAX_SOURCE_POINTER_VERIFICATIONS + 1
        )
    ]
    bounded_sources[-1] = source(
        "MNCS-TEST-P-LATE", "pressures/late.json",
        "repository-owner-language-pressure",
    )
    bounded = capability_intelligence._owner_declared_pressure_links(
        ["MNCS-TEST-P-LATE"], bounded_sources, {},
    )
    assert bounded is not None
    assert bounded["supporting_source_window_truncated"] is True
    assert bounded["links"][0]["status"] == (
        "not-found-in-bounded-supporting-source-window"
    )

    alias_sources = []
    alias_payloads = {}
    alias_integrity = {}
    for index in (1, 2):
        identity = f"MNCS-TEST-P-{index:03d}"
        pointer = source(
            identity, f"pressures/{identity}.json",
            "repository-owner-language-pressure",
        )
        raw = json.dumps({
            "schema_version": "mncs-test.pressure/1",
            "id": identity,
            "commons_pressure_id": "MNCS-LANG-ALIAS",
        }, sort_keys=True, separators=(",", ":")).encode()
        pointer["sha256"] = hashlib.sha256(raw).hexdigest()
        key = capability_intelligence._source_pointer_key(pointer)
        alias_sources.append(pointer)
        alias_payloads[key] = raw
        alias_integrity[key] = "content-integrity-verified"
    ambiguous_alias = capability_intelligence._owner_declared_pressure_links(
        ["MNCS-LANG-ALIAS"], alias_sources, alias_integrity,
        source_payloads=alias_payloads,
    )
    assert ambiguous_alias is not None
    assert ambiguous_alias["links"][0]["status"] == (
        "ambiguous-integrity-verified-pressure-aliases"
    )

    unverified_alias = capability_intelligence._owner_declared_pressure_links(
        ["MNCS-LANG-ALIAS"], alias_sources, {},
        source_payloads=alias_payloads,
    )
    assert unverified_alias is not None
    assert unverified_alias["links"][0]["status"] == (
        "no-matching-supporting-source-reference"
    )


def test_compiler_capability_delivery_requires_exact_identity_for_exact_match() -> None:
    contract = {
        "required_identity": (
            "mncs:0.2:capability:mncs.test.digest::digest_app::test_artifact"
        ),
        "runtime_dispatch_alias": "test_artifact",
        "effect_contract": [
            {
                "capability": "test_artifact",
                "kind": "structured_read",
                "target": "digest_app",
            },
            {
                "capability": "test_artifact",
                "kind": "structured_write",
                "target": "digest_app",
            },
        ],
        "proof_relationship": "compiler-artifact-admission-matches",
        "canonical_execution_relationship": (
            "declared-effects-matched-across-proof-cases"
        ),
        "environment_delivery": "not-measured-by-this-compiler-proof",
    }
    binding = {
        "provider": "mncs-test",
        "capability": contract["required_identity"],
        "availability_status": "available",
        "use_observation_status": "invoked-in-selected-event-window",
        "invocation_count": 1,
        "completion_count": 1,
        "outcome_status_counts": {"succeeded": 1},
        "supporting_events": [{
            "identity": "evt_exact_capability_invocation",
            "sequence": 8,
            "type": "capability.invoked",
        }],
    }
    delivery = {
        "status": "ready",
        "bindings_omitted": 0,
        "binding_records_not_examined": 0,
        "bindings": [binding],
    }

    result = capability_intelligence._selected_session_delivery_observation(
        contract, delivery, session_id="ses_delivery_reader",
    )

    assert result is not None
    assert result["status"] == "canonical-identity-match"
    assert result["match_kind"] == "canonical-identity"
    assert result["canonical_identity_match_count"] == 1
    assert result["runtime_alias_match_count"] == 0
    compact = capability_intelligence._compact_selected_session_delivery_observation(
        result,
    )
    assert compact is not None
    assert compact["binding_matches"][0]["supporting_events"][0]["identity"] == (
        "evt_exact_capability_invocation"
    )


def test_compiler_capability_delivery_marks_incomplete_absence_as_unobserved() -> None:
    contract = {
        "required_identity": "mncs:0.2:capability:mncs.test.digest::digest_app::test_artifact",
        "runtime_dispatch_alias": "test_artifact",
        "effect_contract": [
            {"capability": "test_artifact", "kind": "structured_read", "target": "digest_app"},
            {"capability": "test_artifact", "kind": "structured_write", "target": "digest_app"},
        ],
        "proof_relationship": "compiler-artifact-admission-matches",
        "canonical_execution_relationship": "declared-effects-matched-across-proof-cases",
        "environment_delivery": "not-measured-by-this-compiler-proof",
    }

    result = capability_intelligence._selected_session_delivery_observation(
        contract,
        {
            "status": "partial",
            "bindings_omitted": 1,
            "binding_records_not_examined": 2,
            "bindings": [],
        },
        session_id="ses_partial_delivery_reader",
    )

    assert result is not None
    assert result["status"] == (
        "not-observed-in-incomplete-session-binding-snapshot"
    )


def test_compiler_capability_delivery_distinguishes_complete_absence_and_alias_ambiguity() -> None:
    contract = {
        "required_identity": "mncs:0.2:capability:mncs.test.digest::digest_app::test_artifact",
        "runtime_dispatch_alias": "test_artifact",
        "effect_contract": [
            {"capability": "test_artifact", "kind": "structured_read", "target": "digest_app"},
            {"capability": "test_artifact", "kind": "structured_write", "target": "digest_app"},
        ],
        "proof_relationship": "compiler-artifact-admission-matches",
        "canonical_execution_relationship": "declared-effects-matched-across-proof-cases",
        "environment_delivery": "not-measured-by-this-compiler-proof",
    }
    complete_absence = capability_intelligence._selected_session_delivery_observation(
        contract,
        {
            "status": "ready",
            "bindings_omitted": 0,
            "binding_records_not_examined": 0,
            "bindings": [],
        },
        session_id="ses_complete_delivery_reader",
    )
    ambiguous_alias = capability_intelligence._selected_session_delivery_observation(
        contract,
        {
            "status": "ready",
            "bindings_omitted": 0,
            "binding_records_not_examined": 0,
            "bindings": [
                {"provider": "mncs-test", "capability": "test_artifact"},
                {"provider": "mncs-compiler", "capability": "test_artifact"},
            ],
        },
        session_id="ses_ambiguous_delivery_reader",
    )

    assert complete_absence is not None
    assert complete_absence["status"] == (
        "not-present-in-selected-session-binding-snapshot"
    )
    assert ambiguous_alias is not None
    assert ambiguous_alias["status"] == "ambiguous-runtime-alias"
    assert ambiguous_alias["runtime_alias_match_count"] == 2
    assert len(ambiguous_alias["binding_matches"]) == 2


def test_compiler_native_application_proof_rejects_failed_canonical_case(
    tmp_path: Path,
) -> None:
    session, state = _session(tmp_path, [])
    _add_compiler_native_application_proof_evidence(
        session, state, failed_case=True,
    )

    result = capability_intelligence.project(session)

    assert result["status"] == "partial"
    assert result["observation_count"] == 0
    assert result["implementation_accountability"]["sample_count"] == 0
    assert result["implementation_accountability"][
        "systemwide_native_adoption_ratio"
    ]["status"] == "not-measured"
    assert result["source"]["observation_reference_diagnostics"][0][
        "verification"
    ] == "observation-contract-invalid"


def test_compiler_native_application_proof_rejects_mismatched_effect_capability(
    tmp_path: Path,
) -> None:
    session, state = _session(tmp_path, [])
    _add_compiler_native_application_proof_evidence(
        session, state, invalid_effect_contract=True,
    )

    result = capability_intelligence.project(session)

    assert result["status"] == "partial"
    assert result["observation_count"] == 0
    assert result["implementation_accountability"]["sample_count"] == 0
    assert result["source"]["observation_reference_diagnostics"][0][
        "verification"
    ] == "observation-contract-invalid"


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


def test_environment_related_work_does_not_masquerade_as_compiler_source_context(
    tmp_path: Path,
) -> None:
    report = {
        "schema_version": capability_intelligence.COMPILER_CACHE_REGRESSION_SCHEMA_V2,
        "identity": "ev_compiler_digest_application_effect_capability_invalidation_20261024",
        "compiler_head": "a" * 40,
        "language_head": "b" * 40,
        "source_module_count": 4,
        "language_effect_and_capability_dependencies_invalidated": True,
        "stages": {"cold": {"cache_status": "miss"}},
    }
    session, state = _session(tmp_path, [])
    compiler_reference = _add_compiler_regression_evidence(session, state, report)
    reader_record = _observation(
        "ev_environment_reader_with_compiler_pointer",
        "ses_environment_reader",
        "cmp_environment_reader",
    )
    reader_record["supporting_sources"] = [{
        "kind": "compiler-regression-source-record",
        **compiler_reference,
    }]
    environment_checkout = Path(
        session.snapshot["selected_checkouts"]["mncs-environment"]["path"]
    )
    relative = "evidence/environmental-capability/compiler-reader-context.json"
    raw = json.dumps(reader_record, sort_keys=True, separators=(",", ":")).encode()
    support_path = environment_checkout / relative
    support_path.parent.mkdir(parents=True, exist_ok=True)
    support_path.write_bytes(raw)
    _git(environment_checkout, "add", relative)
    _git(environment_checkout, "commit", "--quiet", "-m", "reader context record")
    state["evidence"].append({
        "identity": reader_record["identity"],
        "repository": "mncs-environment",
        "commit": _git(environment_checkout, "rev-parse", "HEAD"),
        "path": relative,
        "sha256": hashlib.sha256(raw).hexdigest(),
    })
    state["evidence_count"] = len(state["evidence"])

    result = capability_intelligence.project(session)

    compiler_observation = next(
        item for item in result["observations"]
        if item["identity"] == report["identity"]
    )
    assert "source_session_context" not in compiler_observation["observation"]
    assert compiler_observation["observation"]["source_session_context_resolution"] == (
        "no-matching-verified-supporting-observation"
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
    assert hypothesis["observation_count"] == 0
    assert hypothesis["unclassified_observation_count"] == 1
    assert hypothesis["role_classification_status"] == "incomplete"
    assert hypothesis["unclassified_evidence"][0][
        "observation_identity"
    ] == record["identity"]
    summary = result["pressure_evidence_summary"]
    assert summary["pressure_observation_count"] == 1
    assert summary["role_record_counts"] == {
        "occurrence": 0,
        "supporting-context": 0,
        "unclassified": 1,
    }
    assert summary["role_classification_status"] == "incomplete"
    assert summary["recurrence_supported_hypothesis_count"] == 0
    assert summary["recurrence_status_counts"] == {
        "not-established": 1,
        "repeated-within-session": 0,
        "observed-across-sessions": 0,
    }
    assert summary["scope_status"] == (
        "read-complete-for-selected-window"
    )
    compact = capability_intelligence._compact_projection(
        result,
        original_size=capability_intelligence.MAX_RESULT_BYTES + 1,
        max_result_bytes=12_000,
    )
    assert compact["pressure_evidence_summary"]["role_record_counts"] == (
        summary["role_record_counts"]
    )
    assert compact["pressure_evidence_summary"][
        "recurrence_supported_hypothesis_count"
    ] == 0
    assert result["observations"][0]["recording_context"]["campaign_id"] == (
        "cmp_environmental_capability_intelligence_20261023"
    )


def test_pressure_evidence_summary_reports_selected_window_gaps() -> None:
    observations = [
        {
            "pressure_hypothesis": {"identity": "prs_occurrence"},
            "pressure_hypothesis_role": "occurrence",
        },
        {
            "pressure_hypothesis": {"identity": "prs_support"},
            "pressure_hypothesis_role": "supporting-context",
        },
        {"pressure_hypothesis": {"identity": "prs_unclassified"}},
        {"pressure_hypothesis": None},
    ]
    hypotheses = [
        {
            "recurrence_status": "observed-across-sessions",
            "session_attribution_status": "complete",
        },
        {
            "recurrence_status": "not-established",
            "session_attribution_status": "incomplete",
        },
    ]

    summary = capability_intelligence._pressure_evidence_summary(
        observations,
        hypotheses,
        {
            "evidence_page_truncated": True,
            "evidence_history_pages_requested": 3,
            "evidence_history_pages_read": 2,
            "evidence_history_truncated": True,
            "evidence_history_checkpoint_consistent": True,
            "observation_records_unavailable": 1,
            "observation_records_skipped_by_page_budget": 2,
        },
        campaign_state_available=True,
    )

    assert summary["pressure_observation_count"] == 3
    assert summary["role_record_counts"] == {
        "occurrence": 1,
        "supporting-context": 1,
        "unclassified": 1,
    }
    assert summary["role_classification_status"] == "incomplete"
    assert summary["recurrence_supported_hypothesis_count"] == 1
    assert summary["incomplete_session_attribution_hypothesis_count"] == 1
    assert summary["scope_status"] == "partial-selected-window"


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
    _drop_pressure_hypothesis(verification)
    followup = _observation("ev_environment_change_followup", "ses_compiler", "cmp_compiler")
    _drop_pressure_hypothesis(followup)
    change = _observation("ev_environment_capture_change", "ses_environment", "cmp_environment")
    _drop_pressure_hypothesis(change)
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


def test_compact_projection_preserves_improvement_path_before_owner_audit_detail(
    tmp_path: Path,
) -> None:
    pressure = _observation(
        "ev_environment_compaction_pressure", "ses_agent", "cmp_agent",
    )
    pressure["pressure_hypothesis"]["identity"] = "ph_compaction_priority"
    change = _observation(
        "ev_environment_compaction_change", "ses_environment", "cmp_environment",
    )
    _drop_pressure_hypothesis(change)
    verification = _observation(
        "ev_environment_compaction_verification",
        "ses_environment",
        "cmp_environment",
    )
    _drop_pressure_hypothesis(verification)
    followup = _observation(
        "ev_environment_compaction_followup", "ses_agent_later", "cmp_agent",
    )
    _drop_pressure_hypothesis(followup)
    implementation = {
        "identity": "ev_environment_compaction_implementation",
        "repository": "mncs-environment",
        "commit": "a" * 40,
        "path": "mncs_env/capability_intelligence.py",
        "sha256": "b" * 64,
    }
    change["improvement_relation"] = {
        "identity": "im_compaction_priority",
        "addresses_hypothesis_identity": "ph_compaction_priority",
        "implementation_reference": implementation,
        "verification_observation_identity": verification["identity"],
        "subsequent_observation_identity": followup["identity"],
        "basis": "Bounded projection retains a verified improvement path.",
        "not_measured": ["whether later pressure recurrence decreases"],
    }
    session, _ = _session(
        tmp_path,
        [pressure, change, verification, followup],
        extra_refs=[implementation],
    )
    projection = capability_intelligence.project(session)
    projection["implementation_accountability"] = {
        "schema_version": capability_intelligence.IMPLEMENTATION_ACCOUNTABILITY_SCHEMA,
        "sample_count": 0,
        "samples": [],
        "owner_audit_evidence": {
            "observed_audit_record_count": 1,
            "audits": [{
                "identity": "ev_environment_large_owner_audit",
                "evidence_reference": implementation,
                "source_audit_details": [
                    {
                        "identity": f"owner_source_detail_{record_index}",
                        "remaining_host_responsibilities": [
                            {
                                "area": "a" * 180,
                                "classification": "b" * 180,
                                "eliminating_capability": "c" * 180,
                                "canonical_owner": "d" * 180,
                            }
                            for _ in range(8)
                        ],
                    }
                    for record_index in range(4)
                ],
            }],
        },
    }

    compact = capability_intelligence._compact_projection(
        projection, 100_000, 18_000,
    )

    assert len(json.dumps(compact, ensure_ascii=False).encode()) <= 18_000
    assert compact["improvement_relation_count"] == 1
    relation = compact["improvement_relations"][0]
    assert relation["identity"] == "im_compaction_priority"
    assert relation["verification_evidence_reference"]["identity"] == (
        verification["identity"]
    )
    assert relation["subsequent_evidence_reference"]["identity"] == (
        followup["identity"]
    )
    assert [item["identity"] for item in compact["pressure_hypotheses"]] == [
        "ph_compaction_priority",
    ]
    assert any(
        item["identity"] == "ev_environment_compaction_pressure"
        for item in compact["observations"]
    )
    owner_audit = compact["implementation_accountability"][
        "owner_audit_evidence"
    ]["audits"][0]
    assert owner_audit["source_audit_details_truncated"] is True
    assert owner_audit["source_audit_details_omitted_count"] > 0


def test_improvement_relation_without_followup_preserves_unassessed_status(
    tmp_path: Path,
) -> None:
    pressure = _observation("ev_environment_pressure_occurrence", "ses_agent", "cmp_agent")
    pressure["pressure_hypothesis"]["identity"] = "ph_unobserved_followup"
    verification = _observation(
        "ev_environment_change_verification", "ses_environment", "cmp_environment",
    )
    _drop_pressure_hypothesis(verification)
    change = _observation(
        "ev_environment_change_implementation", "ses_environment", "cmp_environment",
    )
    _drop_pressure_hypothesis(change)
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


def test_campaign_state_bootstrap_pressure_projects_live_events_and_unassessed_fix(
    tmp_path: Path, monkeypatch,
) -> None:
    evidence_root = (
        Path(__file__).resolve().parents[1]
        / "evidence/environmental-capability"
    )
    identities = (
        "campaign-state-bootstrap-pressure-20261025.json",
        "campaign-state-bootstrap-verification-20261025.json",
        "campaign-state-bootstrap-improvement-20261025.json",
    )
    records = [
        json.loads((evidence_root / path).read_text(encoding="utf-8"))
        for path in identities
    ]
    session, _ = _session(tmp_path, records)
    session.session_id = "ses_c6b0d153689d7388"
    session.snapshot["campaign"]["identity"] = (
        "cmp_environment_authorization_delivery_20261025"
    )
    session.snapshot["intent"] = {
        "identity": "int_8475157f281c835f",
        "goal": "exercise canonical Environment persistence through the Control socket",
    }
    event_identities = [
        "evt_2d6c52f29a035119",
        "evt_6d42a18434bf9eb3",
        "evt_68d77c4ca086168b",
    ]
    activity = {
        "status": "ready",
        "source": {
            "event_window_truncated": False,
            "rpc_operation_records_omitted": 0,
            "rpc_operation_records_invalid": 0,
            "rpc_operation_capture_limit_reached": False,
            "rpc_operation_events_considered": len(event_identities),
            "rpc_operation_records_returned": len(event_identities),
        },
        "rpc_operations": [
            {"event_reference": {"identity": identity}}
            for identity in event_identities
        ],
    }
    monkeypatch.setattr(
        capability_intelligence,
        "_session_activity",
        lambda *_args, **_kwargs: activity,
    )

    result = capability_intelligence.project(session)
    compact = capability_intelligence.project(session, max_result_bytes=12_000)

    assert result["status"] == "ready"
    assert result["observation_count"] == 3
    assert result["pressure_hypothesis_count"] == 1
    hypothesis = result["pressure_hypotheses"][0]
    assert hypothesis["identity"] == "ph_campaign_state_bootstrap_20261025"
    assert hypothesis["recurrence_status"] == "not-established"
    pressure = result["observations"][0]
    event_source = pressure["supporting_sources"][0]
    assert event_source["content_integrity_status"] == (
        "captured-in-observation-record"
    )
    assert event_source["event_identities"] == [
        "evt_2d6c52f29a035119",
        "evt_6d42a18434bf9eb3",
        "evt_68d77c4ca086168b",
    ]
    assert event_source["event_reference_status"] == (
        "all-referenced-events-present"
    )
    assert event_source["event_reference_count"] == 3
    assert event_source["event_reference_matched_count"] == 3
    assert event_source["event_reference_match_categories"] == ["rpc_operation"]
    assert compact["observations"][0]["supporting_sources"][0][
        "event_reference_status"
    ] == "all-referenced-events-present"
    assert compact["observations"][0]["supporting_sources"][0][
        "event_reference_matched_count"
    ] == 3
    assert compact["observations"][0]["supporting_sources"][0][
        "event_reference_match_categories"
    ] == ["rpc_operation"]

    assert result["improvement_relation_count"] == 1
    relation = result["improvement_relations"][0]
    assert relation["addresses_hypothesis_identity"] == hypothesis["identity"]
    assert relation["pressure_hypothesis_status"] == "present-in-projection"
    assert relation["verification_record_status"] == (
        "record-integrity-verified-in-projection"
    )
    assert relation["verification_evidence_reference"]["identity"] == (
        "ev_environment_campaign_state_bootstrap_verification_20261025"
    )
    assert relation["subsequent_record_status"] == "not-recorded"
    assert relation["effectiveness_assessment"] == "not-assessed"


def test_bounded_event_reference_coverage_records_project_matched_counts_and_limits(
    tmp_path: Path, monkeypatch,
) -> None:
    evidence_root = (
        Path(__file__).resolve().parents[1]
        / "evidence/environmental-capability"
    )
    identities = (
        "session-event-reference-coverage-pressure-20261025.json",
        "session-event-reference-coverage-verification-20261025.json",
        "session-event-reference-coverage-improvement-20261025.json",
    )
    records = [
        json.loads((evidence_root / path).read_text(encoding="utf-8"))
        for path in identities
    ]
    session, _ = _session(tmp_path, records)
    session.session_id = "ses_c6b0d153689d7388"
    session.snapshot["campaign"]["identity"] = (
        "cmp_environment_authorization_delivery_20261025"
    )
    session.snapshot["intent"] = {
        "identity": "int_8475157f281c835f",
        "goal": "exercise canonical Environment persistence through the Control socket",
    }
    event_identities = records[0]["supporting_sources"][0]["event_identities"]
    activity = {
        "status": "partial-rpc-only-in-selected-window",
        "source": {
            "event_window_truncated": False,
            "rpc_operation_records_omitted": 7,
            "rpc_operation_records_invalid": 0,
            "rpc_operation_capture_limit_reached": False,
            "rpc_operation_events_considered": 39,
            "rpc_operation_records_returned": 32,
        },
        "rpc_operations": [
            {"event_reference": {"identity": event_identities[0]}},
            *(
                {"event_reference": {"identity": f"evt_projection_{index}"}}
                for index in range(31)
            ),
        ],
    }
    monkeypatch.setattr(
        capability_intelligence,
        "_session_activity",
        lambda *_args, **_kwargs: activity,
    )

    result = capability_intelligence.project(session)

    assert result["observation_count"] == 3
    assert result["pressure_hypothesis_count"] == 1
    hypothesis = result["pressure_hypotheses"][0]
    assert hypothesis["identity"] == "ph_session_event_reference_coverage_20261025"
    assert hypothesis["recurrence_status"] == "not-established"
    pressure = result["observations"][0]
    source = pressure["supporting_sources"][0]
    assert source["event_reference_status"] == (
        "some-present-others-unresolved-in-bounded-view"
    )
    assert source["event_reference_count"] == 3
    assert source["event_reference_matched_count"] == 1
    assert result["improvement_relation_count"] == 1
    relation = result["improvement_relations"][0]
    assert relation["addresses_hypothesis_identity"] == hypothesis["identity"]
    assert relation["verification_record_status"] == (
        "record-integrity-verified-in-projection"
    )
    assert relation["subsequent_record_status"] == "not-recorded"
    assert relation["effectiveness_assessment"] == "not-assessed"


def test_session_event_reference_status_preserves_selection_and_window_limits() -> None:
    source = {
        "kind": "authenticated-session-activity-projection",
        "repository": "mncs-environment",
        "campaign_id": "cmp_event_reference_status",
        "session_id": "ses_event_reference_status",
        "event_identities": ["evt_event_one", "evt_event_two"],
    }
    activity = {
        "source": {
            "event_window_truncated": False,
            "rpc_operation_records_omitted": 0,
            "rpc_operation_records_invalid": 0,
            "rpc_operation_capture_limit_reached": False,
            "rpc_operation_events_considered": 2,
            "rpc_operation_records_returned": 2,
        },
        "rpc_operations": [
            {"event_reference": {"identity": "evt_event_one"}},
            {"event_reference": {"identity": "evt_event_two"}},
        ],
    }

    assert capability_intelligence._authenticated_session_event_reference_resolution(
        source, selected_session_id="ses_event_reference_status", activity=activity,
    )["event_reference_status"] == "all-referenced-events-present"
    assert capability_intelligence._authenticated_session_event_reference_resolution(
        source, selected_session_id="ses_other", activity=activity,
    ) == {
        "event_reference_status": "source-session-not-selected",
        "event_reference_count": 2,
        "event_reference_matched_count": 0,
    }

    partial_activity = {
        **activity,
        "rpc_operations": [
            {"event_reference": {"identity": "evt_event_one"}},
            {"event_reference": {"identity": "evt_unreferenced"}},
        ],
    }
    assert capability_intelligence._authenticated_session_event_reference_resolution(
        source,
        selected_session_id="ses_event_reference_status",
        activity=partial_activity,
    )["event_reference_status"] == (
        "some-present-others-not-found-in-complete-view"
    )
    capped_activity = {
        "source": {
            "event_window_truncated": False,
            "rpc_operation_records_omitted": 6,
            "rpc_operation_records_invalid": 0,
            "rpc_operation_capture_limit_reached": False,
            "rpc_operation_events_considered": 38,
            "rpc_operation_records_returned": 32,
        },
        "rpc_operations": [
            {"event_reference": {"identity": "evt_event_one"}},
            *(
                {"event_reference": {"identity": f"evt_extra_{index}"}}
                for index in range(31)
            ),
        ],
    }
    capped_resolution = (
        capability_intelligence._authenticated_session_event_reference_resolution(
            source,
            selected_session_id="ses_event_reference_status",
            activity=capped_activity,
        )
    )
    assert capped_resolution == {
        "event_reference_status": "some-present-others-unresolved-in-bounded-view",
        "event_reference_count": 2,
        "event_reference_matched_count": 1,
        "event_reference_match_categories": ["rpc_operation"],
    }
    assert capability_intelligence._authenticated_session_event_reference_resolution(
        source,
        selected_session_id="ses_event_reference_status",
        activity={
            **partial_activity,
            "source": {**activity["source"], "event_window_truncated": True},
        },
    )["event_reference_status"] == (
        "some-present-others-unresolved-in-bounded-view"
    )

    bounded_activity = {
        "source": {"event_window_truncated": True},
        "rpc_operations": [],
    }
    assert capability_intelligence._authenticated_session_event_reference_resolution(
        source,
        selected_session_id="ses_event_reference_status",
        activity=bounded_activity,
    )["event_reference_status"] == "unresolved-in-bounded-session-view"

    incomplete_activity = {
        "source": {
            "event_window_truncated": False,
            "rpc_operation_records_omitted": 0,
            "rpc_operation_records_invalid": 0,
            "rpc_operation_capture_limit_reached": False,
            "rpc_operation_events_considered": 0,
            "rpc_operation_records_returned": 0,
        },
        "rpc_operations": [],
    }
    assert capability_intelligence._authenticated_session_event_reference_resolution(
        source,
        selected_session_id="ses_event_reference_status",
        activity=incomplete_activity,
    )["event_reference_status"] == (
        "no-referenced-events-in-selected-session-activity"
    )

    invalid_source = {**source, "event_identities": ["evt_event_one"] * 2}
    assert capability_intelligence._authenticated_session_event_reference_resolution(
        invalid_source,
        selected_session_id="ses_event_reference_status",
        activity=activity,
    )["event_reference_status"] == "invalid-source-reference"


def test_session_event_reference_resolution_includes_claim_recoveries() -> None:
    source = {
        "kind": "authenticated-session-activity-projection",
        "repository": "mncs-environment",
        "campaign_id": "cmp_claim_recovery_reference",
        "session_id": "ses_claim_recovery_reference",
        "event_identities": ["evt_recovery_one", "evt_recovery_two"],
    }
    activity = {
        "source": {
            "event_window_truncated": False,
            "claim_recovery_events_considered": 1,
            "claim_recovery_records_returned": 1,
            "claim_recovery_records_omitted": 0,
            "claim_recovery_records_invalid": 0,
        },
        "claim_recoveries": [{
            "event_reference": {
                "identity": "evt_recovery_one",
                "type": "lease.recovered-and-acquired",
            },
        }],
    }

    complete_match = (
        capability_intelligence._authenticated_session_event_reference_resolution(
            {**source, "event_identities": ["evt_recovery_one"]},
            selected_session_id="ses_claim_recovery_reference",
            activity=activity,
        )
    )
    assert complete_match == {
        "event_reference_status": "all-referenced-events-present",
        "event_reference_count": 1,
        "event_reference_matched_count": 1,
        "event_reference_match_categories": ["claim_recovery"],
    }

    coordination_match = (
        capability_intelligence._authenticated_session_event_reference_resolution(
            {**source, "event_identities": ["evt_coordination_one"]},
            selected_session_id="ses_claim_recovery_reference",
            activity={
                "source": {
                    "event_window_truncated": False,
                    "claim_coordination_events_considered": 1,
                    "claim_coordination_records_returned": 1,
                    "claim_coordination_records_omitted": 0,
                    "claim_coordination_records_invalid": 0,
                },
                "claim_coordination": [{
                    "event_reference": {
                        "identity": "evt_coordination_one",
                        "type": "lease.acquired",
                    },
                }],
            },
        )
    )
    assert coordination_match == {
        "event_reference_status": "all-referenced-events-present",
        "event_reference_count": 1,
        "event_reference_matched_count": 1,
        "event_reference_match_categories": ["claim_coordination"],
    }

    denial_match = (
        capability_intelligence._authenticated_session_event_reference_resolution(
            {**source, "event_identities": ["evt_denial_one"]},
            selected_session_id="ses_claim_recovery_reference",
            activity={
                "source": {"event_window_truncated": False},
                "authority_denials": [{
                    "event_reference": {
                        "identity": "evt_denial_one",
                        "type": "authority.denied",
                    },
                }],
            },
        )
    )
    assert denial_match == {
        "event_reference_status": "all-referenced-events-present",
        "event_reference_count": 1,
        "event_reference_matched_count": 1,
        "event_reference_match_categories": ["authority_denial"],
    }

    partial_match = (
        capability_intelligence._authenticated_session_event_reference_resolution(
            source,
            selected_session_id="ses_claim_recovery_reference",
            activity=activity,
        )
    )
    assert partial_match == {
        "event_reference_status": "some-present-others-unresolved-in-bounded-view",
        "event_reference_count": 2,
        "event_reference_matched_count": 1,
        "event_reference_match_categories": ["claim_recovery"],
    }


def test_operation_stale_claim_recovery_projects_causal_evidence(tmp_path: Path) -> None:
    session, _state = _session(tmp_path, [])
    decision_event = {
        "identity": "evt_operation_claim_coordination_deferral",
        "sequence": 1,
        "type": "authority.coordination-deferred",
        "payload": {
            "action": "write", "target": "mncs-environment",
            "decision_domain": "coordination",
            "persistent_authority_required": False,
            "operation_executed": False,
        },
    }
    recovery_event = {
        "identity": "evt_operation_claim_recovery",
        "sequence": 2,
        "type": "lease.recovered-for-operation",
        "payload": {
            "action": "write",
            "repository": "mncs-environment",
            "scope": {
                "kind": "worktree",
                "repository": "mncs-environment",
                "checkout": "/workspace/mncs-environment",
                "branch": "main",
                "exclusive": False,
            },
            "authority_decision_event": {
                "identity": decision_event["identity"],
                "sequence": decision_event["sequence"],
                "type": decision_event["type"],
            },
            "recovered_count": 1,
            "recovered_from": [{
                "claim_id": "claim:mncs-environment:worktree:abcdef123456",
                "version": 1,
                "superseded_version": 2,
                "owner_session": "ses_quiet_owner",
            }],
            "operation_effect_started": False,
        },
    }
    events = [decision_event, recovery_event]
    session.recent_events = lambda *, limit, before_sequence=None: {
        "event_count": len(events),
        "events": events[-limit:],
        "truncated": False,
        "next_before_sequence": None,
    }

    projection = capability_intelligence.project(session)
    activity = projection["session_activity"]

    assert activity["source"]["operation_claim_recovery_events_considered"] == 1
    assert activity["source"]["operation_claim_recovery_records_returned"] == 1
    recovery = activity["operation_claim_recoveries"][0]
    assert recovery["status"] == "stale-claims-superseded-for-one-operation"
    assert recovery["recovered_from"][0]["owner_session"] == "ses_quiet_owner"
    assert recovery["authority_decision_event"]["identity"] == decision_event["identity"]
    assert recovery["event_reference"]["identity"] == recovery_event["identity"]

    resolved = capability_intelligence._authenticated_session_event_reference_resolution(
        {
            "kind": "authenticated-session-activity-projection",
            "repository": "mncs-environment",
            "campaign_id": "cmp_projection_reader",
            "session_id": session.session_id,
            "event_identities": [recovery_event["identity"]],
        },
        selected_session_id=session.session_id,
        activity=activity,
    )
    assert resolved["event_reference_status"] == "all-referenced-events-present"
    assert resolved["event_reference_match_categories"] == [
        "operation_claim_recovery",
    ]

    compact = capability_intelligence._compact_projection(
        projection, 100_000, 24_000,
    )
    compact_recovery = compact["session_activity"][
        "operation_claim_recoveries"
    ][0]
    assert compact_recovery["event_reference"]["identity"] == (
        recovery_event["identity"]
    )
    assert compact_recovery["authority_decision_event"]["identity"] == (
        decision_event["identity"]
    )
    assert compact_recovery["recovered_from"][0]["claim_id"] == (
        recovery["recovered_from"][0]["claim_id"]
    )

    reordered = {
        **recovery_event,
        "payload": {
            **recovery_event["payload"],
            "authority_decision_event": {
                **recovery_event["payload"]["authority_decision_event"],
                "sequence": recovery_event["sequence"],
            },
        },
    }
    assert capability_intelligence._operation_claim_recovery_event_projection(
        reordered, session.session_id,
    ) is None

    # Historical recovery events remain projectable through the old field.
    legacy_recovery = {
        **recovery_event,
        "payload": {
            **{
                key: value
                for key, value in recovery_event["payload"].items()
                if key != "authority_decision_event"
            },
            "authority_denial_event": {
                "identity": "evt_legacy_authority_denial",
                "sequence": 1,
                "type": "authority.denied",
            },
        },
    }
    legacy_projection = (
        capability_intelligence._operation_claim_recovery_event_projection(
            legacy_recovery, session.session_id,
        )
    )
    assert legacy_projection is not None
    assert legacy_projection["authority_decision_event"]["identity"] == (
        "evt_legacy_authority_denial"
    )
    assert legacy_projection["authority_denial_event"]["identity"] == (
        "evt_legacy_authority_denial"
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


def test_streamed_source_hash_stops_after_its_total_deadline(monkeypatch) -> None:
    def unexpected_git(*_args, **_kwargs):
        raise AssertionError("Git should not run after the source hash deadline")

    monkeypatch.setattr(capability_intelligence.subprocess, "run", unexpected_git)

    digest, status = capability_intelligence._git_blob_sha256(
        Path("/unselected"), "a" * 40, "src/large-source.rs",
        deadline=time.monotonic() - 1,
    )

    assert digest is None
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
    assert len(hypothesis["evidence_references"]) == 16
    assert projection["source"]["evidence_history_pages_requested"] == 2
    assert projection["source"]["evidence_history_pages_read"] == 2
    assert projection["source"]["evidence_history_page_offsets"] == [9, 0]
    assert projection["source"]["evidence_history_truncated"] is False
    assert projection["source"]["evidence_history_checkpoint_consistent"] is True
    assert projection["session_activity"]["source"][
        "event_page_before_sequence"
    ] == 12
    assert "event_window_limit" not in projection["session_activity"]["source"]
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


def test_projection_cli_explicitly_supports_capability_page_limit(
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
            "--evidence-limit", str(capability_intelligence.MAX_EVIDENCE_REFS),
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
    repository: str = "mncs-compiler",
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
            "name": repository,
            "path": str(checkout),
            "manifest_repository": repository,
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
