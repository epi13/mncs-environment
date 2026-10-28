"""Real Git/Store transport and native routing; domain runners are fixtures."""
import json
import os
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from mncs_env import coherence, family, observations, workspace
from mncs_env.session_store import open_store

MNCS_WORKSPACE_ROOT = Path(workspace.verify_registered_checkout(
    Path(__file__).resolve().parents[1]
)["workspace_root"])
AUTOMATION = MNCS_WORKSPACE_ROOT / 'mncs-automation'
BINARY = os.environ.get('MNCS_BIN') or os.environ.get('MNCS_BINARY')


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], capture_output=True, check=True)


def repository(tmp_path):
    root = tmp_path / 'app'
    root.mkdir()
    git(root, 'init', '-q')
    (root / 'source.mncs').write_text('source one')
    (root / 'notes.md').write_text('prose one')
    (root / '.mncs').mkdir()
    (root / '.mncs/project.json').write_text(json.dumps({
        'schema_version': 'mncs.project/1',
        'repository': 'app',
    }))
    git(root, 'add', '.')
    git(root, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'bootstrap')
    return root


class Participant:
    def __init__(self, state, root, store=None):
        self.state_dir, self.session_id, self.saves = state, 'ses_incremental_fixture', 0
        self.store = store or SimpleNamespace(read_claims=lambda: {}, read_projection_versions=lambda: {})
        self.snapshot = {'workspace': {'root': str(root.parent)},
                         'selected_checkouts': {'app': {'path': str(root)}}, 'toolchain': {}}

    def _save(self):
        self.saves += 1
        self.snapshot['snapshot_sequence'] = self.saves
        if hasattr(self.store, 'save_snapshot'):
            self.store.save_snapshot(self.session_id, self.snapshot)


@pytest.fixture
def participant(tmp_path, monkeypatch):
    if not BINARY or not AUTOMATION.is_dir():
        pytest.skip('explicit native compiler and Automation checkout required')
    monkeypatch.setenv('MNCS_BIN', BINARY)
    monkeypatch.setenv('MNCS_AUTOMATION_ROOT', str(AUTOMATION))
    monkeypatch.setattr(family, 'scheduler_deadlines', lambda session: [])
    return Participant(tmp_path / 'state', repository(tmp_path))


def runners():
    counts = {name: 0 for name in coherence.declarations()['passes']}
    def run(name):
        counts[name] += 1
        return {'summary': {}, 'reused': False}
    return counts, {name: lambda name=name: run(name) for name in counts}


def test_owner_execution_history_is_bounded_and_preserves_exact_resource_rows():
    def resource_usage(counter):
        return {
            "schema_version": coherence.resources.PROCESS_RESOURCE_USAGE_SCHEMA,
            "status": "measured",
            "measurement_basis": "process-counters-bracketing-owner-runner",
            "pid": 505,
            "process_start_ticks": 42,
            "elapsed_seconds": 0.1,
            "cpu_user_ticks": counter,
            "cpu_system_ticks": 1,
            "rss_start_bytes": 4096,
            "rss_end_bytes": 4096,
            "rss_high_water_start_bytes": 4096,
            "rss_high_water_end_bytes": 4096,
            "rss_high_water_growth_bytes": 0,
            "io_bytes": {
                "status": "measured",
                "reason_code": None,
                "rchar": 2,
                "wchar": 3,
                "read_bytes": counter,
                "write_bytes": 4,
                "cancelled_write_bytes": 0,
            },
            "measurement_limits": [
                "Counters cover the scheduler process and exclude spawned child processes.",
            ],
        }

    def trace_rows(start, count):
        scheduled = [{
            "owner": "projections",
            "pass": "projections",
            "service_execution_id": f"ase_{index:032x}",
            "run_elapsed_seconds": 0.1,
            "process_resource_usage": resource_usage(index),
        } for index in range(start, start + count)]
        return {
            "mode": "targeted_update",
            "trace_persistence_status": "session-snapshot",
            "scheduled": scheduled,
            "skipped": [],
            "owner_failures": [],
            "owner_pass_results": [{
                "owner": "projections",
                "service_execution_id": row["service_execution_id"],
                "result_status": "result-recorded",
                "verification_status": "not-established",
                "result_identity": f"{index:064x}",
                "source_reference_status": "store-record",
                "source_reference": (
                    "mncs-store:ses_incremental_fixture:incremental-coherence:"
                    f"projections:{index:064x}"
                ),
                "result_reused": False,
            } for index, row in zip(
                range(start, start + count), scheduled, strict=True,
            )],
        }

    legacy_history = {
        "schema_version": coherence.LEGACY_OWNER_EXECUTION_HISTORY_SCHEMA,
        "coverage_status": "bounded-since-enabled",
        "limit": coherence.MAX_OWNER_EXECUTION_HISTORY,
        "trace_sequence": 1,
        "omitted_count": 0,
        "rows": [{
            "owner": "projections",
            "service_execution_id": "ase_" + "f" * 32,
            "trace_sequence": 1,
            "owner_outcome_status": "no-failure-reported",
            "verification_status": "not-established",
            "process_resource_usage_status": "not-recorded-on-trace",
        }],
    }
    legacy_projection = coherence.project_owner_execution_history(legacy_history)
    assert legacy_projection["rows"][0][
        "owner_result_observation_status"
    ] == "unknown"
    assert "owner_result_status" not in legacy_projection["rows"][0]

    first = coherence._append_owner_execution_history(
        None, trace_rows(0, 16), trace_sequence=1,
        session_id="ses_incremental_fixture",
    )
    first_projection = coherence.project_owner_execution_history(first)
    assert first_projection["status"] == "observed"
    assert first_projection["coverage_status"] == "bounded-since-enabled"
    assert first_projection["record_count"] == 16
    assert first_projection["omitted_count"] == 0
    assert first_projection["rows"][0]["process_resource_usage"][
        "cpu_user_ticks"
    ] == 0
    assert first_projection["rows"][0]["owner_result_observation_status"] == (
        "recorded"
    )
    assert first_projection["rows"][0]["owner_result_status"] == (
        "result-recorded"
    )
    assert first_projection["rows"][0]["owner_result_identity"] == f"{0:064x}"
    assert first_projection["rows"][0][
        "owner_result_reference_status"
    ] == "store-record"
    assert first_projection["rows"][0][
        "owner_result_verification_status"
    ] == "not-established"

    second = coherence._append_owner_execution_history(
        first, trace_rows(16, 2), trace_sequence=2,
        session_id="ses_incremental_fixture",
    )
    second_projection = coherence.project_owner_execution_history(second)
    assert second_projection["status"] == "observed"
    assert second_projection["record_count"] == 16
    assert second_projection["omitted_count"] == 2
    assert second_projection["trace_sequence"] == 2
    assert second_projection["rows"][0]["service_execution_id"] == (
        f"ase_{2:032x}"
    )
    assert second_projection["rows"][-1]["service_execution_id"] == (
        f"ase_{17:032x}"
    )

    older_trace = trace_rows(18, 1)
    older_trace.pop("owner_pass_results")
    third = coherence._append_owner_execution_history(
        second, older_trace, trace_sequence=3,
        session_id="ses_incremental_fixture",
    )
    third_projection = coherence.project_owner_execution_history(third)
    assert third_projection["rows"][-1][
        "owner_result_observation_status"
    ] == "not-recorded-on-trace"

    missing_result = trace_rows(19, 1)
    missing_result["owner_pass_results"] = []
    fourth = coherence._append_owner_execution_history(
        third, missing_result, trace_sequence=4,
        session_id="ses_incremental_fixture",
    )
    fourth_projection = coherence.project_owner_execution_history(fourth)
    assert fourth_projection["rows"][-1][
        "owner_result_observation_status"
    ] == "not-recorded-in-results"


def test_owner_execution_history_retains_exact_verification_source_links():
    session_id = "ses_incremental_fixture"
    execution_id = "ase_" + "1" * 32
    owner_result_reference = (
        f"mncs-store:{session_id}:incremental-coherence:verification:"
        + "2" * 64
    )
    verification_reference = (
        f"mncs-store:{session_id}:verification:" + "3" * 64
    )
    trace = {
        "mode": "targeted_update",
        "trace_persistence_status": "session-snapshot",
        "scheduled": [{
            "owner": "verification",
            "pass": "verification",
            "service_execution_id": execution_id,
            "run_elapsed_seconds": 0.25,
        }],
        "skipped": [],
        "owner_failures": [],
        "owner_pass_results": [{
            "owner": "verification",
            "service_execution_id": execution_id,
            "result_status": "result-recorded",
            "verification_status": "not-established",
            "result_identity": "4" * 64,
            "source_reference_status": "store-record",
            "source_reference": owner_result_reference,
            "verification_evidence_reference_status": "store-record",
            "verification_evidence_reference": verification_reference,
            "result_reused": False,
        }],
    }

    history = coherence._append_owner_execution_history(
        None, trace, trace_sequence=1, session_id=session_id,
    )
    projected = coherence.project_owner_execution_history(
        history, session_id=session_id,
    )

    assert history["schema_version"] == (
        "mncs.environment.owner-execution-history/2"
    )
    assert projected["status"] == "observed"
    row = projected["rows"][0]
    assert row["owner_result_reference"] == owner_result_reference
    assert row["owner_result_reference_status"] == "store-record"
    assert row["verification_evidence_reference"] == verification_reference
    assert row["verification_evidence_reference_status"] == "store-record"
    assert row["owner_result_reused"] is False

    invalid_history = {
        **history,
        "rows": [{
            **history["rows"][0],
            "verification_evidence_reference": (
                "mncs-store:ses_other_session:verification:" + "5" * 64
            ),
        }],
    }
    invalid_projection = coherence.project_owner_execution_history(
        invalid_history, session_id=session_id,
    )
    assert invalid_projection["status"] == "partial"
    assert invalid_projection["rows"] == []


def test_owner_pass_result_carries_session_scoped_verification_reference():
    session_id = "ses_incremental_fixture"
    execution_id = "ase_" + "6" * 32
    verification_reference = (
        f"mncs-store:{session_id}:verification:" + "7" * 64
    )
    block = {
        "status": "observed",
        "operation_status": "complete",
        "summary": {"executed": 1, "failed": 0},
        "evidence": verification_reference,
    }
    result_identity = coherence.digest_hex(block, length=64)
    owner_reference = (
        f"mncs-store:{session_id}:incremental-coherence:verification:"
        + coherence.digest_hex({"block": block}, length=64)
    )
    rows = coherence._owner_pass_results(
        SimpleNamespace(session_id=session_id),
        [{
            "owner": "verification",
            "service_execution_id": execution_id,
        }],
        {"verification": block},
        {"verification": {
            "identity": result_identity,
            "reference": owner_reference,
        }},
        persisted=True,
    )
    projected = coherence.project_trace({
        "mode": "targeted_update",
        "trace_persistence_status": "session-snapshot",
        "scheduled": [{
            "owner": "verification",
            "service_execution_id": execution_id,
            "disposition": 1,
        }],
        "skipped": [],
        "owner_failures": [],
        "owner_pass_results": rows,
    }, session_id=session_id)

    assert projected is not None
    result = projected["owner_pass_results"]["results"][0]
    assert result["source_reference"] == owner_reference
    assert result["verification_evidence_reference_status"] == "store-record"
    assert result["verification_evidence_reference"] == verification_reference

    foreign_block = {
        **block,
        "evidence": "mncs-store:ses_foreign:verification:" + "8" * 64,
    }
    foreign_rows = coherence._owner_pass_results(
        SimpleNamespace(session_id=session_id),
        [{
            "owner": "verification",
            "service_execution_id": execution_id,
        }],
        {"verification": foreign_block},
        None,
        persisted=False,
    )
    assert foreign_rows[0]["verification_evidence_reference_status"] == (
        "invalid-reference"
    )
    assert "verification_evidence_reference" not in foreign_rows[0]


def test_warm_tick_has_zero_processes_and_writes(participant):
    counts, owned = runners()
    _, initial_trace = coherence.tick(participant, {}, owned)
    assert initial_trace["trace_persistence_status"] == "session-snapshot"
    assert initial_trace["owner_failures"] == []
    execution_ids = [
        row["service_execution_id"] for row in initial_trace["scheduled"]
    ]
    assert all(re.fullmatch(r"ase_[0-9a-f]{32}", item) for item in execution_ids)
    assert len(set(execution_ids)) == len(execution_ids)
    assert participant.snapshot["coherence"]["last_trace"][
        "trace_persistence_status"
    ] == "session-snapshot"
    saves = participant.saves
    with patch.object(subprocess, 'Popen', side_effect=AssertionError('quiet tick spawned process')):
        _, trace = coherence.tick(participant, {}, owned)
    assert trace['scheduled'] == []
    assert len(trace['skipped']) == 7
    assert trace['elapsed_seconds'] >= 0
    assert trace["trace_persistence_status"] == "entry-response-only"
    assert set(counts.values()) == {1}
    assert participant.saves == saves


def test_recovery_scan_records_owner_failure_coverage_without_native_automation(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("MNCS_AUTOMATION_ROOT", str(tmp_path / "unavailable"))
    session = Participant(tmp_path / "state", repository(tmp_path))
    _, owned = runners()

    def resource_sample(counter):
        return {
            "schema_version": coherence.resources.PROCESS_RESOURCE_USAGE_SCHEMA,
            "status": "observed",
            "pid": 505,
            "process_start_ticks": 42,
            "cpu_user_ticks": counter,
            "cpu_system_ticks": counter // 2,
            "rss_bytes": 4096 + counter,
            "rss_high_water_bytes": 8192 + counter,
            "io_status": "complete",
            "io_reason_code": None,
            "io_counters": {
                "rchar": counter * 10,
                "wchar": counter * 5,
                "read_bytes": counter * 2,
                "write_bytes": counter * 3,
                "cancelled_write_bytes": counter,
            },
        }

    process_samples = iter([
        sample
        for index in range(len(owned))
        for sample in (
            resource_sample(index * 10),
            resource_sample(index * 10 + 4),
        )
    ])
    with patch.object(
        coherence.resources, "process_usage_sample",
        side_effect=lambda _pid: next(process_samples),
    ):
        _, successful_trace = coherence.tick(session, {}, owned)

    assert successful_trace["mode"] == "recovery_scan"
    assert successful_trace["owner_failures"] == []
    assert len(successful_trace["owner_pass_results"]) == 7
    for row in successful_trace["scheduled"]:
        usage = row["process_resource_usage"]
        assert usage["status"] == "measured"
        assert usage["cpu_user_ticks"] == 4
        assert re.fullmatch(r"ase_[0-9a-f]{32}", row["service_execution_id"])
    successful_projection = coherence.project_trace(successful_trace)
    assert successful_projection["owner_failure_observation_status"] == "complete"
    assert all(
        row["owner_outcome_status"] == "no-failure-reported"
        for row in successful_projection["scheduled_passes"]
    )
    verification = next(
        row for row in successful_projection["owner_pass_results"]["results"]
        if row["owner"] == "verification"
    )
    scheduled_verification = next(
        row for row in successful_projection["scheduled_passes"]
        if row["owner"] == "verification"
    )
    source_verification = next(
        row for row in successful_trace["scheduled"]
        if row["owner"] == "verification"
    )
    assert verification["service_execution_id"] == (
        scheduled_verification["service_execution_id"]
    )
    assert scheduled_verification["process_resource_usage"] == (
        source_verification["process_resource_usage"]
    )
    assert scheduled_verification["process_resource_usage"]["status"] == (
        "measured"
    )
    assert verification["result_status"] == "result-recorded"
    assert verification["source_reference_status"] == "trace-not-persisted"
    assert successful_projection["owner_pass_results"][
        "ambient_fulfillment_status"
    ] == "not-established"

    owned["verification"] = lambda: {"operation_status": "retry"}
    _, failed_trace = coherence.tick(session, {}, owned)
    assert failed_trace["owner_failures"] == ["verification"]
    failed_projection = coherence.project_trace(failed_trace)
    verification = next(
        row for row in failed_projection["scheduled_passes"]
        if row["owner"] == "verification"
    )
    assert verification["owner_outcome_status"] == "failure-reported"
    assert verification["verification_status"] == "not-established"
    result = next(
        row for row in failed_projection["owner_pass_results"]["results"]
        if row["owner"] == "verification"
    )
    assert result["operation_status"] == "retry"
    assert result["result_status"] == "result-recorded"


def test_quiet_owner_outcome_is_traceable_without_an_agent_response(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("MNCS_AUTOMATION_ROOT", str(tmp_path / "unavailable"))
    session = Participant(tmp_path / "state", repository(tmp_path))
    _, owned = runners()
    owned["diagnostics"] = lambda: coherence.quiet_owner_pass_result(
        "no-diagnostic-findings",
    )

    blocks, trace = coherence.tick(session, {}, owned)

    assert blocks["diagnostics"] is None
    assert trace["owner_failures"] == []
    projected = coherence.project_trace(trace)
    diagnostic = next(
        row for row in projected["owner_pass_results"]["results"]
        if row["owner"] == "diagnostics"
    )
    assert diagnostic["result_status"] == "result-recorded"
    assert diagnostic["result_code"] == "quiet-no-op"
    assert diagnostic["outcome_disposition"] == "completed-quietly"
    assert diagnostic["outcome_reason_code"] == "no-diagnostic-findings"
    assert diagnostic["agent_response_emitted"] is False
    assert diagnostic["summary"] == {"response_emitted": False}
    assert diagnostic["source_reference_status"] == "trace-not-persisted"
    assert projected["owner_pass_results"]["outcome_disposition_counts"] == {
        "completed-quietly": 1,
    }
    assert projected["ambient_fulfillment"] == "not-established"
    assert projected["agent_need_opportunity_denominator"] == "not-recorded"


def test_project_trace_exposes_bounded_owner_decisions_without_claiming_fulfillment():
    private_path = "/home/private/project/source.mncs"
    projected = coherence.project_trace({
        "mode": "targeted_update",
        "trace_persistence_status": "entry-response-only",
        "event_count": 2,
        "elapsed_seconds": 0.25,
        "scheduled": [{
            "pass": "verification",
            "owner": "verification",
            "disposition": 1,
            "wave": 0,
            "owner_reused": False,
            "timer_or_lifecycle_due": False,
            "service_execution_id": "ase_" + "a" * 32,
            "run_elapsed_seconds": 0.125,
            "inputs": ["source.changed", "private/source.mncs"],
            "subjects": [
                "private/project/source.mncs",
                *[f"mncs:module:{index}" for index in range(10)],
            ],
            "subject_count": 11,
            "checkout": private_path,
        }],
        "skipped": [{"pass": "doctor", "reason": "current inputs"}],
        "owner_failures": [],
        "private_detail": private_path,
    })

    assert projected["decision_record_status"] == "recorded"
    assert projected["trace_persistence_status"] == "entry-response-only"
    assert projected["scheduled_pass_count"] == 1
    assert projected["skipped_pass_count"] == 1
    assert projected["declared_owner_pass_count"] == 7
    assert projected["owner_pass_decision_coverage"] == "partial"
    assert projected["owner_failure_observation_status"] == "complete"
    assert projected["owner_failure_count"] == 0
    assert projected["scheduled_passes"][0]["run_elapsed_seconds"] == 0.125
    assert projected["scheduled_passes"][0]["owner_outcome_status"] == (
        "no-failure-reported"
    )
    assert projected["scheduled_passes"][0]["verification_status"] == (
        "not-established"
    )
    assert projected["scheduled_passes"][0]["service_execution_id"] == (
        "ase_" + "a" * 32
    )
    assert projected["scheduled_passes"][0]["trigger_kinds"] == ["source.changed"]
    assert projected["scheduled_passes"][0][
        "semantic_subject_identity_status"
    ] == "partial"
    assert projected["scheduled_passes"][0][
        "semantic_subject_identity_count"
    ] == 11
    assert projected["scheduled_passes"][0][
        "semantic_subject_identity_omitted_count"
    ] == 3
    assert projected["scheduled_passes"][0][
        "semantic_subject_identities"
    ] == [f"mncs:module:{index}" for index in range(8)]
    assert projected["skipped_passes"] == [{
        "owner": "doctor", "reason": "current inputs",
    }]
    assert projected["ambient_fulfillment"] == "not-established"
    assert projected["agent_need_opportunity_denominator"] == "not-recorded"
    assert private_path not in json.dumps(projected)
    assert "private/project/source.mncs" not in json.dumps(projected)


def test_owner_pass_results_link_scheduled_execution_to_bounded_result_reference(
    tmp_path,
):
    session = SimpleNamespace(
        session_id="ses_incremental_fixture",
        state_dir=tmp_path,
    )
    execution_id = "ase_" + "b" * 32
    block = {
        "operation_status": "complete",
        "reused": False,
        "summary": {
            "current": 3,
            "failed": 0,
            "elapsed_seconds": 0.25,
            "failed_ids": ["private-test-detail"],
            "detail": "not exposed",
        },
    }
    references = coherence._result_refs(session, {"verification": block})
    scheduled = [{
        "owner": "verification",
        "service_execution_id": execution_id,
        "owner_reused": False,
    }]
    rows = coherence._owner_pass_results(
        session, scheduled, {"verification": block}, references,
        persisted=True,
    )
    projected = coherence.project_trace({
        "mode": "targeted_update",
        "trace_persistence_status": "session-snapshot",
        "scheduled": scheduled,
        "skipped": [],
        "owner_failures": [],
        "owner_pass_results": rows,
    }, session_id=session.session_id)["owner_pass_results"]

    assert projected["status"] == "recorded"
    assert projected["scheduled_owner_count"] == 1
    assert projected["result_record_count"] == 1
    row = projected["results"][0]
    assert row["owner"] == "verification"
    assert row["service_execution_id"] == execution_id
    assert row["result_status"] == "result-recorded"
    assert row["result_identity"] == references["verification"]["identity"]
    assert row["source_reference_status"] == "session-artifact"
    assert row["source_reference"] == (
        "sessions/ses_incremental_fixture/coherence-results/"
        + references["verification"]["identity"] + ".json"
    )
    assert row["operation_status"] == "complete"
    assert row["summary"] == {
        "current": 3, "elapsed_seconds": 0.25, "failed": 0,
    }
    assert row["verification_status"] == "not-established"
    assert projected["ambient_fulfillment_status"] == "not-established"
    assert str(tmp_path) not in json.dumps(projected)
    assert "private-test-detail" not in json.dumps(projected)
    assert "not exposed" not in json.dumps(projected)


def test_family_vocabulary_availability_is_projected_as_allowlisted_reason():
    session_id = "ses_family_availability_fixture"
    execution_id = "ase_" + "d" * 32
    block = {
        "reused": False,
        "summary": {
            "relevant": 0,
            "detail": "vocabulary-unavailable",
        },
    }
    rows = coherence._owner_pass_results(
        SimpleNamespace(session_id=session_id),
        [{"owner": "family", "service_execution_id": execution_id}],
        {"family": block},
        None,
        persisted=False,
    )
    projected = coherence.project_trace({
        "mode": "targeted_update",
        "scheduled": [{
            "owner": "family",
            "service_execution_id": execution_id,
        }],
        "skipped": [],
        "owner_failures": [],
        "owner_pass_results": rows,
    }, session_id=session_id)["owner_pass_results"]["results"][0]

    assert projected["service_availability_status"] == "unavailable"
    assert projected["service_availability_reason_code"] == (
        "family-vocabulary-unavailable"
    )
    assert "detail" not in projected.get("summary", {})
    assert projected["verification_status"] == "not-established"

    block["summary"]["detail"] = "/private/workspace/source.mncs"
    unsafe_rows = coherence._owner_pass_results(
        SimpleNamespace(session_id=session_id),
        [{"owner": "family", "service_execution_id": execution_id}],
        {"family": block},
        None,
        persisted=False,
    )
    unsafe = coherence.project_trace({
        "mode": "targeted_update",
        "scheduled": [{
            "owner": "family",
            "service_execution_id": execution_id,
        }],
        "skipped": [],
        "owner_failures": [],
        "owner_pass_results": unsafe_rows,
    }, session_id=session_id)["owner_pass_results"]["results"][0]
    assert "service_availability_status" not in unsafe
    assert "service_availability_reason_code" not in unsafe
    assert "/private/workspace" not in json.dumps(unsafe)


def test_owner_pass_result_reference_mismatch_is_not_exposed(tmp_path):
    session = SimpleNamespace(
        session_id="ses_incremental_fixture",
        state_dir=tmp_path,
    )
    execution_id = "ase_" + "c" * 32
    block = {"summary": {"failed": 1}, "reused": False}
    references = coherence._result_refs(session, {"diagnostics": block})
    references["diagnostics"]["identity"] = "0" * 64
    rows = coherence._owner_pass_results(
        session,
        [{"owner": "diagnostics", "service_execution_id": execution_id}],
        {"diagnostics": block}, references, persisted=True,
    )

    projected = coherence.project_trace({
        "mode": "targeted_update",
        "scheduled": [{
            "owner": "diagnostics",
            "service_execution_id": execution_id,
        }],
        "skipped": [],
        "owner_failures": [],
        "owner_pass_results": rows,
    })["owner_pass_results"]["results"][0]
    assert projected["source_reference_status"] == "identity-mismatch"
    assert "source_reference" not in projected
    assert projected["verification_status"] == "not-established"


def test_owner_pass_result_store_reference_is_session_and_owner_bound(tmp_path):
    class Store:
        backend = "store"

        def __init__(self):
            self.records = {}

        def get_record_strict(self, schema, address):
            return self.records.get((schema, address))

        def put_record(self, schema, address, record):
            self.records[(schema, address)] = record

    session = SimpleNamespace(
        session_id="ses_incremental_fixture",
        state_dir=tmp_path,
        store=Store(),
    )
    execution_id = "ase_" + "d" * 32
    block = {"summary": {"failed": 1}, "operation_status": "complete"}
    references = coherence._result_refs(session, {"diagnostics": block})
    rows = coherence._owner_pass_results(
        session,
        [{"owner": "diagnostics", "service_execution_id": execution_id}],
        {"diagnostics": block}, references, persisted=True,
    )
    projected = coherence.project_trace({
        "mode": "targeted_update",
        "scheduled": [{
            "owner": "diagnostics",
            "service_execution_id": execution_id,
        }],
        "skipped": [],
        "owner_failures": [],
        "owner_pass_results": rows,
    }, session_id=session.session_id)["owner_pass_results"]["results"][0]

    assert projected["source_reference_status"] == "store-record"
    assert projected["source_reference"] == references["diagnostics"]["reference"]
    assert projected["source_reference"].startswith(
        "mncs-store:ses_incremental_fixture:incremental-coherence:diagnostics:"
    )


def test_project_trace_preserves_missing_and_reported_owner_outcomes():
    scheduled = [{
        "pass": "verification",
        "disposition": 1,
        "run_elapsed_seconds": 0.1,
    }]
    reported_failure = coherence.project_trace({
        "mode": "targeted_update",
        "scheduled": scheduled,
        "skipped": [],
        "owner_failures": ["verification"],
    })
    assert reported_failure["owner_failure_observation_status"] == "complete"
    assert reported_failure["owner_failure_count"] == 1
    assert reported_failure["scheduled_passes"][0]["owner_outcome_status"] == (
        "failure-reported"
    )
    assert reported_failure["scheduled_passes"][0]["verification_status"] == (
        "not-established"
    )

    missing_failure_list = coherence.project_trace({
        "mode": "targeted_update",
        "scheduled": scheduled,
        "skipped": [],
    })
    assert missing_failure_list["owner_failure_observation_status"] == (
        "not-recorded"
    )
    assert missing_failure_list["owner_failure_count"] is None
    assert missing_failure_list["scheduled_passes"][0][
        "owner_outcome_status"
    ] == "unknown"

    malformed_failure_list = coherence.project_trace({
        "mode": "targeted_update",
        "scheduled": scheduled,
        "skipped": [],
        "owner_failures": ["undeclared-owner"],
    })
    assert malformed_failure_list["owner_failure_observation_status"] == (
        "partial"
    )
    assert malformed_failure_list["owner_failure_records_invalid"] == 1
    assert malformed_failure_list["scheduled_passes"][0][
        "owner_outcome_status"
    ] == "unknown"


@pytest.mark.parametrize('path,expected', [
    ('notes.md', {'projections'}),
    ('source.mncs', {'semantics', 'verification', 'diagnostics', 'projections'}),
    ('.mncs/project.json', {'doctor', 'semantics', 'actions', 'verification', 'family', 'projections'}),
])
def test_external_edits_select_only_subscribed_passes(participant, path, expected):
    counts, owned = runners()
    coherence.tick(participant, {}, owned)
    (Path(participant.snapshot['selected_checkouts']['app']['path']) / path).write_text('changed')
    _, trace = coherence.tick(participant, {}, owned)
    assert {item['pass'] for item in trace['scheduled']} == expected
    assert all(item["run_elapsed_seconds"] >= 0 for item in trace["scheduled"])
    _, quiet = coherence.tick(participant, {}, owned)
    assert quiet['scheduled'] == []


def test_later_owner_wave_records_its_derived_trigger(tmp_path, monkeypatch):
    session = Participant(tmp_path / 'state', tmp_path)
    automation = tmp_path / 'automation'
    coherence_provider = automation / 'native/mncs/automation/coherence.mncs'
    coherence_provider.parent.mkdir(parents=True)
    coherence_provider.write_text('fixture provider')
    monkeypatch.setenv('MNCS_AUTOMATION_ROOT', str(automation))
    declarations = coherence.declarations()
    names = list(declarations['passes'])
    session.snapshot['coherence'] = {
        'schema_version': coherence.SCHEMA,
        'stable': True,
        'policy_identity': 'fixture-policy',
        'current_request': 'prior-request',
        'policy_artifacts': {},
        'result_refs': {},
        'deadlines': {},
        'repositories': {},
        'artifacts': {},
        'libraries': {},
        'store_cursor': 0,
        'semantic_cursors': {},
    }
    monkeypatch.setattr(coherence, '_roots', lambda _session: {})
    monkeypatch.setattr(coherence, '_policy_identity', lambda *_args: (
        'fixture-policy', {},
    ))
    event_sample = {'count': 0}

    def observe(_session, _prior):
        event_sample['count'] += 1
        events = (
            [
                {
                    'kind': 'source.changed',
                    'identity': 'fixture-source',
                    'semantic_subjects': [
                        {'identity': 'mncs:compiler:lexer'},
                    ],
                },
                {
                    'kind': 'projection.changed',
                    'identity': 'fixture-projection',
                    'semantic_subjects': [
                        {'identity': 'mncs:environment:projection'},
                    ],
                },
            ] if event_sample['count'] == 1 else []
        )
        return (
            {'repositories': {}, 'artifacts': {}, 'libraries': {}},
            events, True,
            {'metadata_paths': 0, 'enumerated_repositories': [], 'errors': []},
        )

    monkeypatch.setattr(coherence, '_observe', observe)
    monkeypatch.setattr(coherence, '_store_events', lambda *_args: ([], 0, True))
    monkeypatch.setattr(coherence, '_semantic_events', lambda *_args: ([], {}, True))
    monkeypatch.setattr(coherence, '_file_events', lambda _session, events: events)
    monkeypatch.setattr(coherence, '_load_results', lambda *_args: ({
        name: {'summary': {}, 'reused': True} for name in names
    }, True))
    monkeypatch.setattr(coherence, '_result_refs', lambda *_args: {})
    monkeypatch.setattr(family, 'scheduler_deadlines', lambda _session: [])

    route_call = {'count': 0}
    verification_changed = 1 << declarations['facts'].index('verification.changed')

    def native(_session, function, rows):
        assert function == 'route_batch'
        route_call['count'] += 1
        if route_call['count'] == 1:
            codes = [1 if name == 'verification' else 0 for name in names]
        elif route_call['count'] == 2:
            codes = [
                1 if name == 'family' and row[3] & verification_changed else 0
                for name, row in zip(names, rows)
            ]
        else:
            codes = [0] * len(rows)
        return codes, {}

    monkeypatch.setattr(coherence, '_native', native)

    def run(name):
        if name == 'verification':
            session.snapshot['verification_state'] = {
                'ob-1': {'verdict': 'PASS'},
            }
            return coherence.quiet_owner_pass_result(
                'no-verification-obligations',
            )
        return {'summary': {}, 'reused': False}

    blocks, trace = coherence.tick(
        session, {}, {name: lambda name=name: run(name) for name in names},
    )
    assert blocks['verification'] is None
    family_pass = next(
        item for item in trace['scheduled'] if item['pass'] == 'family'
    )
    assert family_pass['wave'] == 1
    assert family_pass['inputs'] == ['verification.changed']
    verification_pass = next(
        item for item in trace['scheduled'] if item['pass'] == 'verification'
    )
    assert verification_pass['inputs'] == ['source.changed']
    assert verification_pass['subjects'] == ['mncs:compiler:lexer']
    assert verification_pass['subject_count'] == 1
    verification_result = next(
        item for item in trace['owner_pass_results']
        if item['owner'] == 'verification'
    )
    assert verification_result['result_status'] == 'result-recorded'
    assert verification_result['outcome_disposition'] == 'completed-quietly'
    assert verification_result['outcome_reason_code'] == (
        'no-verification-obligations'
    )
    assert verification_result['agent_response_emitted'] is False


def test_same_dirty_filename_edited_twice_is_not_cached(participant):
    counts, owned = runners()
    coherence.tick(participant, {}, owned)
    path = Path(participant.snapshot['selected_checkouts']['app']['path']) / 'source.mncs'
    path.write_text('first dirty state')
    coherence.tick(participant, {}, owned)
    path.write_text('second dirty state')
    coherence.tick(participant, {}, owned)
    assert counts['verification'] == 3
    assert counts['doctor'] == 1


def test_missing_catalogue_recovers_once(participant):
    _, owned = runners()
    coherence.tick(participant, {}, owned)
    ref = participant.snapshot['coherence']['repositories']['app']
    Path(ref['artifact']).unlink()
    _, trace = coherence.tick(participant, {}, owned)
    assert trace['mode'] == 'bounded_reconciliation'
    assert len(trace['scheduled']) == 7
    _, quiet = coherence.tick(participant, {}, owned)
    assert quiet['scheduled'] == []


def test_corrupt_cached_result_cannot_authorize_reuse(participant):
    _, owned = runners()
    coherence.tick(participant, {}, owned)
    ref = participant.snapshot['coherence']['result_refs']['verification']
    Path(ref['artifact']).write_text('{"block":{"summary":{"current":999}}}')
    _, trace = coherence.tick(participant, {}, owned)
    assert len(trace['scheduled']) == 7
    _, quiet = coherence.tick(participant, {}, owned)
    assert quiet['scheduled'] == []


def test_edit_during_pass_is_pending_not_current(participant):
    _, owned = runners()
    path = Path(participant.snapshot['selected_checkouts']['app']['path']) / 'source.mncs'
    owned['verification'] = lambda: (path.write_text('moving input'), {'summary': {}})[1]
    _, trace = coherence.tick(participant, {}, owned)
    assert participant.snapshot['coherence']['stable'] is False
    assert trace['pending_events']


def test_owner_boundary_deadline_wakes_only_doctor(participant):
    _, owned = runners()
    definition = {'services': [{'observation_inputs': ['selected-repositories'], 'observation_max_age_ms': 1000}]}
    coherence.tick(participant, definition, owned, now_ms=1000)
    _, quiet = coherence.tick(participant, definition, owned, now_ms=1999)
    assert quiet['scheduled'] == []
    _, due = coherence.tick(participant, definition, owned, now_ms=2000)
    assert {item['pass'] for item in due['scheduled']} == {'doctor'}


def test_verification_change_routes_to_family_and_projection(participant):
    _, owned = runners()
    coherence.tick(participant, {}, owned)
    (Path(participant.snapshot['selected_checkouts']['app']['path']) / 'source.mncs').write_text('new obligation')
    def verify():
        participant.snapshot['verification_state'] = {'ob-1': {'verdict': 'PASS', 'input': 'new obligation'}}
        return {'summary': {'current': 1}}
    owned['verification'] = verify
    _, trace = coherence.tick(participant, {}, owned)
    assert 'family' in {item['pass'] for item in trace['scheduled']}
    assert any(item['kind'] == 'verification.changed' for item in trace['derived_events'])
    family_pass = next(
        item for item in trace['scheduled'] if item['pass'] == 'family'
    )
    assert family_pass['wave'] == 1
    assert family_pass['inputs'] == ['verification.changed']


def test_store_publication_during_pass_is_not_skipped(participant):
    store = open_store(participant.state_dir, 'store')
    participant.store = store
    _, owned = runners()
    fired = False
    def publish():
        nonlocal fired
        if not fired:
            store.write_projection_row('family:change/concurrent-fixture', 1, {'test': True})
            fired = True
        return {'summary': {}}
    owned['verification'] = publish
    try:
        coherence.tick(participant, {}, owned)
        assert participant.snapshot['coherence']['stable'] is False
        _, trace = coherence.tick(participant, {}, owned)
        assert any(event['kind'] == 'family.changed' for event in trace['events'])
        _, quiet = coherence.tick(participant, {}, owned)
        assert quiet['scheduled'] == []
    finally:
        store.close()


def test_owner_resource_trace_survives_result_publication_interruption(
    tmp_path, monkeypatch,
):
    participant = Participant(tmp_path / "state", repository(tmp_path))
    automation = tmp_path / "automation"
    (automation / "native/mncs/automation").mkdir(parents=True)
    (automation / "native/mncs/automation/coherence.mncs").write_text(
        "fixture scheduler provider presence",
    )
    monkeypatch.setenv("MNCS_AUTOMATION_ROOT", str(automation))

    def route(session, function, rows):
        if function == "route_batch":
            return [2] * len(rows), {"status": "fixture"}
        return [0] * len(rows), {"status": "fixture"}

    monkeypatch.setattr(coherence, "_native", route)
    monkeypatch.setattr(family, "scheduler_deadlines", lambda session: [])
    counts, owned = runners()
    for name in owned:
        def run(name=name):
            counts[name] += 1
            return {"summary": {"run_count": counts[name]}, "reused": False}
        owned[name] = run

    coherence.tick(participant, {}, owned)
    previous = participant.snapshot["coherence"]
    prior_store_cursor = previous.get("store_cursor")
    prior_semantic_cursors = dict(previous.get("semantic_cursors") or {})
    prior_saves = participant.saves
    (Path(participant.snapshot["selected_checkouts"]["app"]["path"])
     / "source.mncs").write_text("source changed before interrupted publication")

    with patch.object(
        coherence, "_result_refs", side_effect=KeyboardInterrupt,
    ):
        with pytest.raises(KeyboardInterrupt):
            coherence.tick(participant, {}, owned)

    state = participant.snapshot["coherence"]
    trace = state["last_trace"]
    assert participant.saves == prior_saves + 1
    assert state["stable"] is False
    assert state.get("store_cursor") == prior_store_cursor
    assert state.get("semantic_cursors") == prior_semantic_cursors
    assert trace["trace_persistence_status"] == "session-snapshot"
    assert trace["result_reference_publication_status"] == "interrupted"
    assert trace["result_reference_publication_elapsed_seconds"] >= 0
    assert trace["result_reference_publication_process_resource_usage"][
        "status"
    ] in {"measured", "partial"}
    assert trace["scheduled"]
    assert trace["owner_pass_results"]
    assert all(
        row["result_status"] == "result-recorded"
        and row["source_reference_status"] == "not-recorded"
        for row in trace["owner_pass_results"]
    )

    history = coherence.project_owner_execution_history(
        state["owner_execution_history"], session_id=participant.session_id,
    )
    latest = {row["service_execution_id"]: row for row in history["rows"]}
    for scheduled in trace["scheduled"]:
        row = latest[scheduled["service_execution_id"]]
        assert row["process_resource_usage"] == scheduled[
            "process_resource_usage"
        ]

    projected_trace = coherence.project_trace(
        trace, session_id=participant.session_id,
    )
    assert projected_trace["result_reference_publication_status"] == (
        "interrupted"
    )
    assert projected_trace["result_reference_publication_process_resource_usage"][
        "status"
    ] in {"measured", "partial"}
    assert any(
        "result-reference publication did not complete" in limit
        for limit in projected_trace["measurement_limits"]
    )


def test_store_cursor_waits_for_owner_success(participant):
    store = open_store(participant.state_dir, 'store')
    participant.store = store
    _, owned = runners()
    try:
        coherence.tick(participant, {}, owned)
        previous = participant.snapshot['coherence']['store_cursor']
        store.write_projection_row('family:change/retry-fixture', 1, {'test': True})

        owned['family'] = lambda: {
            'summary': {'blockers': 1}, 'operation_status': 'retry'}
        _, failed = coherence.tick(participant, {}, owned)
        assert failed['owner_failures'] == ['family']
        assert failed['store_cursor_disposition'] == 'held_for_owner_retry'
        assert participant.snapshot['coherence']['store_cursor'] == previous
        assert participant.snapshot['coherence']['stable'] is False

        owned['family'] = lambda: {'summary': {'checked': True}}
        _, retried = coherence.tick(participant, {}, owned)
        assert retried.get('owner_failures', []) == []
        assert any(event.get('kind') == 'family.changed'
                   for event in retried['events']), retried
        assert participant.snapshot['coherence']['store_cursor'] != previous
    finally:
        store.close()


def test_owner_retry_cannot_leave_quiet_state_stable(participant):
    _, owned = runners()
    definition = {'services': [{'observation_inputs': ['selected-repositories'],
                                'observation_max_age_ms': 1000}]}
    coherence.tick(participant, definition, owned, now_ms=1000)
    owned['doctor'] = lambda: {
        'summary': {'blockers': 1}, 'operation_status': 'retry'}
    _, failed = coherence.tick(participant, definition, owned, now_ms=2000)
    assert failed['owner_failures'] == ['doctor']
    assert participant.snapshot['coherence']['stable'] is False


def test_semantic_cursor_waits_for_owner_success_after_reset(participant):
    _, owned = runners()
    coherence.tick(participant, {}, owned)
    slot = "mncs-language-service:workspace-stream"
    old = json.dumps({"stream": "old-stream", "cursor": 8}, sort_keys=True)
    candidate = json.dumps({"stream": "new-stream", "cursor": 23}, sort_keys=True)
    participant.snapshot["coherence"]["semantic_cursors"] = {slot: old}
    reset = [{"kind": "provider.changed", "provider": "mncs-language-service",
              "stream": slot, "reason": "cursor expired", "reset": True}]
    with patch.object(coherence, "_semantic_events",
                      return_value=(reset, {slot: candidate}, False)):
        owned["verification"] = lambda: {
            "summary": {"blockers": 1}, "operation_status": "retry"}
        _, failed = coherence.tick(participant, {}, owned)
    assert failed["cursor_disposition"] == "held_for_owner_retry"
    assert failed["owner_failures"] == ["verification"]
    assert participant.snapshot["coherence"]["semantic_cursors"] == {slot: old}

    with patch.object(coherence, "_semantic_events",
                      return_value=(reset, {slot: candidate}, False)):
        owned["verification"] = lambda: {"summary": {"checked": True}}
        _, retried = coherence.tick(participant, {}, owned)
    assert retried.get("owner_failures", []) == []
    assert participant.snapshot["coherence"]["semantic_cursors"] == {slot: candidate}
    assert retried["cursor_disposition"] == "reconciled_and_advanced"
    assert participant.snapshot["coherence"]["stable"] is True


def test_semantic_cursor_acknowledges_explicit_owner_no_work(participant):
    _, owned = runners()
    coherence.tick(participant, {}, owned)
    slot = "mncs-language-service:workspace-stream"
    old = json.dumps({"stream": "stream-1", "cursor": 4}, sort_keys=True)
    candidate = json.dumps({"stream": "stream-1", "cursor": 5}, sort_keys=True)
    participant.snapshot["coherence"]["semantic_cursors"] = {slot: old}
    event = {"kind": "semantic.changed", "stream": slot,
             "identity": "event-5", "generation": "generation-5"}
    with patch.object(coherence, "_semantic_events",
                      return_value=([event], {slot: candidate}, True)):
        owned["actions"] = lambda: None
        owned["diagnostics"] = lambda: None
        owned["family"] = lambda: None
        _, trace = coherence.tick(participant, {}, owned)
    assert trace.get("owner_failures", []) == []
    assert participant.snapshot["coherence"]["semantic_cursors"] == {slot: candidate}


def test_store_replay_reset_holds_cursor_until_full_owner_reconciliation(participant):
    _, owned = runners()
    coherence.tick(participant, {}, owned)
    participant.store = SimpleNamespace(generation=lambda: 999)
    old = participant.snapshot["coherence"].get("store_cursor")
    candidate = "999"
    reset = [{"kind": "store.reconcile_required", "candidate_cursor": candidate,
              "reason": "observation bound exceeded"}]
    with patch.object(coherence, "_store_events", return_value=(reset, old, True)):
        owned["verification"] = lambda: {
            "summary": {"blockers": 1}, "operation_status": "retry"}
        _, failed = coherence.tick(participant, {}, owned)
    assert len(failed["scheduled"]) == 7
    assert failed["store_replay"]["disposition"] == "reconciliation_incomplete_or_store_advanced"
    assert participant.snapshot["coherence"]["store_cursor"] == old

    with patch.object(coherence, "_store_events", return_value=(reset, old, True)):
        owned["verification"] = lambda: {"summary": {"checked": True}}
        _, retried = coherence.tick(participant, {}, owned)
    assert len(retried["scheduled"]) == 7
    assert retried["store_replay"]["disposition"] == "full_owner_reconciliation_complete"
    assert participant.snapshot["coherence"]["store_cursor"] == candidate


def test_store_replay_report_survives_route_and_postpass_observation_failure(
    tmp_path, monkeypatch,
):
    automation = tmp_path / "automation"
    marker = automation / "native/mncs/automation/coherence.mncs"
    marker.parent.mkdir(parents=True)
    marker.write_text("fixture provider presence")
    monkeypatch.setenv("MNCS_AUTOMATION_ROOT", str(automation))
    participant = Participant(tmp_path / "state", repository(tmp_path))
    participant.store = SimpleNamespace(generation=lambda: 999)
    monkeypatch.setattr(family, "scheduler_deadlines", lambda _session: [])
    _, owned = runners()
    old = "old-cursor"
    reset = [{
        "kind": "store.reconcile_required",
        "candidate_cursor": "999",
        "reason": "observation bound exceeded",
    }]
    store_results = iter(((reset, old, True), ([], old, False)))
    monkeypatch.setattr(
        coherence, "_observe",
        lambda *_args: ({"repositories": {}}, [], True, {}),
    )
    monkeypatch.setattr(
        coherence, "_store_events", lambda *_args: next(store_results),
    )
    monkeypatch.setattr(
        coherence, "_policy_identity", lambda *_args: ("policy", {}),
    )
    monkeypatch.setattr(
        coherence, "_load_results", lambda *_args: ({}, True),
    )
    monkeypatch.setattr(
        coherence, "_owner_pass_results", lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        coherence, "_file_events",
        lambda *_args: (_ for _ in ()).throw(KeyError("route")),
    )
    _, trace = coherence.tick(participant, {}, owned)

    assert len(trace["scheduled"]) == 7
    assert trace["store_replay"]["disposition"] == (
        "reconciliation_incomplete_or_store_advanced"
    )
    assert trace["mode"] == "bounded_reconciliation"
    persisted_history = coherence.project_owner_execution_history(
        participant.snapshot["coherence"]["owner_execution_history"],
    )
    assert persisted_history["status"] == "observed"
    assert persisted_history["record_count"] == 7
    assert {
        row["service_execution_id"] for row in persisted_history["rows"]
    } == {
        row["service_execution_id"] for row in trace["scheduled"]
    }


def test_store_snapshot_conflict_replays_semantic_work_until_retry_commits(participant):
    from mncs_env.session_store import SnapshotConflict

    store = open_store(participant.state_dir, "store")
    participant.store = store
    _, owned = runners()
    try:
        coherence.tick(participant, {}, owned)
        slot = "mncs-language-service:workspace-stream"
        old = json.dumps({"stream": "stream-1", "cursor": 4}, sort_keys=True)
        candidate = json.dumps({"stream": "stream-1", "cursor": 5}, sort_keys=True)
        participant.snapshot["coherence"]["semantic_cursors"] = {slot: old}
        participant.snapshot["snapshot_sequence"] += 1
        store.save_snapshot(participant.session_id, participant.snapshot)
        participant.saves = participant.snapshot["snapshot_sequence"]
        event = {"kind": "semantic.changed", "stream": slot,
                 "identity": "event-5", "generation": "generation-5"}
        with patch.object(coherence, "_semantic_events",
                          return_value=([event], {slot: candidate}, True)):
            def race_snapshot():
                competing = store.load_snapshot(participant.session_id)
                competing["snapshot_sequence"] += 1
                competing["concurrent_owner_receipt"] = "committed-first"
                store.save_snapshot(participant.session_id, competing)
                return {"summary": {"handled": True}}

            owned["verification"] = race_snapshot
            with pytest.raises(SnapshotConflict):
                coherence.tick(participant, {}, owned)

        durable = store.load_snapshot(participant.session_id)
        assert durable["concurrent_owner_receipt"] == "committed-first"
        assert durable["coherence"]["semantic_cursors"] == {slot: old}

        participant.snapshot = durable
        participant.saves = durable["snapshot_sequence"]
        owned["verification"] = lambda: {"summary": {"handled": True}}
        with patch.object(coherence, "_semantic_events",
                          return_value=([event], {slot: candidate}, True)):
            coherence.tick(participant, {}, owned)
        retried = store.load_snapshot(participant.session_id)
        assert retried["coherence"]["semantic_cursors"] == {slot: candidate}
    finally:
        store.close()


def test_unrelated_repository_is_outside_observation(participant, tmp_path):
    _, owned = runners()
    coherence.tick(participant, {}, owned)
    (tmp_path / 'unrelated').mkdir()
    (tmp_path / 'unrelated/source.mncs').write_text('independent')
    _, trace = coherence.tick(participant, {}, owned)
    assert trace['scheduled'] == []


def test_library_content_changes_even_with_restored_mtime(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    path = root / 'module.mncs'
    path.write_text('first content')
    session = SimpleNamespace(state_dir=tmp_path / 'state', session_id='ses_library_fixture')
    first = observations.observe_library(session, root)
    original = path.stat()
    path.write_text('other content')
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    second = observations.observe_library(session, root, first)
    assert first['content_identity'] != second['content_identity']


def test_executable_identity_binds_actual_bytes(tmp_path):
    path = tmp_path / 'provider'
    path.write_bytes(b'old executable')
    first = observations.observe_artifact(path)
    path.write_bytes(b'new executable')
    second = observations.observe_artifact(path, first)
    assert first['artifact_identity'] != second['artifact_identity']
    assert second['build_origin'] == 'unknown'


def test_store_reopen_resumes_exact_observations_without_processes(participant):
    store = open_store(participant.state_dir, 'store')
    participant.store = store
    _, owned = runners()
    coherence.tick(participant, {}, owned)
    store.close()
    reopened = open_store(participant.state_dir, 'store')
    restored = Participant(participant.state_dir,
        Path(participant.snapshot['selected_checkouts']['app']['path']), reopened)
    restored.snapshot = reopened.load_snapshot(participant.session_id)
    restored.saves = restored.snapshot['snapshot_sequence']
    try:
        with patch.object(subprocess, 'Popen', side_effect=AssertionError('restart rediscovered current state')):
            _, trace = coherence.tick(restored, {}, owned)
        assert trace['scheduled'] == []
        assert restored.saves == participant.saves
    finally:
        reopened.close()


def test_external_head_advance_invalidates_git_facts(participant):
    _, owned = runners()
    coherence.tick(participant, {}, owned)
    root = Path(participant.snapshot['selected_checkouts']['app']['path'])
    (root / 'source.mncs').write_text('committed change')
    git(root, 'add', 'source.mncs')
    git(root, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'advance')
    _, trace = coherence.tick(participant, {}, owned)
    assert any(event['kind'] == 'repository.control_changed' for event in trace['events'])
    assert len(trace['scheduled']) == 7
    assert trace['enumerated_repositories'] == ['app']


def test_warm_store_entry_handle_promotes_only_when_state_changes(participant):
    store = open_store(participant.state_dir, 'store')
    participant.store = store
    _, owned = runners()
    coherence.tick(participant, {}, owned)
    store.close()
    reader = open_store(participant.state_dir, 'store', defer_mutation=True)
    participant.store = reader
    participant.snapshot = reader.load_snapshot(participant.session_id)
    assert reader.backend._store.read_only is True
    try:
        _, quiet = coherence.tick(participant, {}, owned)
        assert quiet['scheduled'] == []
        assert reader.backend._store.read_only is True
        (Path(participant.snapshot['selected_checkouts']['app']['path']) / 'source.mncs').write_text('promote for changed state')
        coherence.tick(participant, {}, owned)
        assert reader.backend._store.read_only is False
        assert reader.load_snapshot(participant.session_id)['coherence']['stable'] is True
    finally:
        reader.close()


def test_replay_identity_includes_domain_schema():
    from mncs_env import sources
    class Store:
        generation = lambda self: 2
        def domain_bindings_at(self, generation):
            return ((b'owner-a/1', b'same-id'),) if generation == 1 else (
                (b'owner-a/1', b'same-id'), (b'owner-b/1', b'same-id'))
    replay = sources.StoreReplaySource(Store(), 'ses_self:').observe('1')
    assert replay.status == 'ok'
    assert len(replay.events) == 1
    assert replay.events[0].provenance['domain_identity'] == 'same-id'


def test_store_replay_prefers_verified_bounded_generation_deltas():
    from mncs_env import sources

    class Store:
        def generation(self):
            return 3

        def domain_bindings_since(self, cursor, *, max_generations):
            assert cursor == 1
            assert max_generations == sources.MAX_REPLAY_WALK
            return (
                (2, b'claim/1', b'claim:peer'),
                (3, b'snapshot/1', b'ses_peer:snap:2'),
                (3, b'event/1', b'ses_self:evt:7'),
            )

        def domain_bindings_at(self, _generation):
            raise AssertionError('delta-aware Store replay must not rescan historical generations')

    replay = sources.StoreReplaySource(Store(), 'ses_self:').observe('1')
    assert replay.status == 'ok'
    assert [event.kind for event in replay.events] == ['claim.changed', 'session.changed']
    assert [event.generation for event in replay.events] == ['2', '3']


def test_canonical_session_store_forwards_bounded_delta_contract():
    from mncs_env.session_store import StoreSessionStore

    class Backend:
        def domain_bindings_since(self, generation, *, max_generations):
            assert generation == 10
            assert max_generations == 4096
            return ((11, b'claim/1', b'claim:peer'),)

    store = StoreSessionStore.__new__(StoreSessionStore)
    store.backend = Backend()
    assert store.domain_bindings_since(10, max_generations=4096) == (
        (11, b'claim/1', b'claim:peer'),
    )


def test_store_backend_forwards_provider_owned_reclaim():
    import pytest

    from mncs_env.store_backend import StoreBackend

    class Provider:
        def generation_inventory(self, *, keep_last):
            assert keep_last == 4096
            return {'head': 5000, 'floor': 768, 'prunable_files': 10}

        def prune_generations(self, *, keep_last, dry_run):
            assert keep_last == 4096
            assert dry_run is True
            return {'head': 5000, 'floor': 768, 'removed_files': 0}

    backend = StoreBackend.__new__(StoreBackend)
    backend._store = Provider()
    assert backend.generation_inventory()['prunable_files'] == 10
    assert backend.reclaim_generations()['removed_files'] == 0

    backend._store = object()
    with pytest.raises(RuntimeError, match='retention inventory'):
        backend.generation_inventory()
    with pytest.raises(RuntimeError, match='generation reclaim'):
        backend.reclaim_generations()


def test_store_replay_ignores_derived_products_and_resolves_claim_repository():
    from mncs_env import sources

    class Store:
        def generation(self):
            return 9

        def domain_bindings_since(self, cursor, *, max_generations):
            assert cursor == 7
            return (
                (8, b'mncs.environment.projection-state/1', b'mncs-compiler:overview:0000000031'),
                (8, b'mncs.provider-execution-provenance/1', b'projection-receipt:abc'),
                (9, b'mncs.environment.workspace-claim/2', b'claim:claim:mncs-language-service:0000000009'),
            )

    result = sources.StoreReplaySource(Store(), 'ses_self:').observe('7')
    assert result.status == 'ok'
    assert len(result.events) == 1
    assert result.events[0].kind == 'claim.changed'
    assert result.events[0].relations['claim'] == 'mncs-language-service'


def mutation_session(tmp_path, root, claims):
    from mncs_env.sessions import Session
    session = Session.__new__(Session)
    session.session_id = 'ses_mutation_fixture'
    session.store = SimpleNamespace(
        read_claims=lambda: claims,
        read_events=lambda _session_id: [],
    )
    session.snapshot = {'workspace': {'root': str(tmp_path)},
        'selected_checkouts': {'app': {'path': str(root)}},
        'claim_holders': {}, 'repo_facts': {'app': {'clean': True, 'main_branch': True}},
        'authority': {'readable': ['app'], 'writable': ['app'], 'invocable': []}}
    session._emit = lambda event_type, _producer, _payload: {
        'identity': 'evt_mutation_fixture',
        'sequence': 1,
        'type': event_type,
    }
    return session


def test_mutation_boundary_reads_foreign_claim_instead_of_cached_permission(tmp_path):
    from datetime import datetime, timedelta, timezone
    root = repository(tmp_path)
    record = {'claim_id': 'claim:app', 'repository': 'app', 'session_id': 'ses_foreign',
              'consumer_id': 'foreign', 'status': 'held', 'version': 1,
              'expires_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
              'scope': {'kind': 'repository', 'repository': 'app', 'exclusive': True}}
    session = mutation_session(tmp_path, root, [record])
    result = session.check(action='write', target='app')
    assert result['verdict'] != 'allow'
    assert session.snapshot['claim_holders']['app'][0]['session_id'] == 'ses_foreign'


def test_mutation_boundary_observes_new_dirty_target(tmp_path):
    root = repository(tmp_path)
    session = mutation_session(tmp_path, root, {})
    assert session.check(action='write', target='app')['verdict'] == 'allow'
    (root / 'source.mncs').write_text('foreign external edit')
    assert session.check(action='write', target='app')['verdict'] != 'allow'


def test_missing_mutation_target_cannot_use_cached_clean_facts(tmp_path):
    from mncs_env.sessions import LifecycleError
    session = mutation_session(tmp_path, tmp_path / 'missing-checkout', {})
    with pytest.raises(LifecycleError, match='not currently registered'):
        session.check(action='write', target='app')


def test_bounded_workspace_readiness_preserves_discovery_contract():
    from mncs_env import cli
    manifest = json.loads((Path(__file__).parents[1] / '.mncs/project.json').read_text())
    provided = {value['contract']: value for value in manifest['contracts']['provides']}
    assert '--summary' not in provided['workspace-discovery']['invocation']['fixed_argv']
    assert '--summary' in provided['workspace-readiness']['invocation']['fixed_argv']
    observed = {'schema_version': 'mncs.environment.workspace-discovery/1',
                'root': '/workspace', 'repository_count': 100, 'scan': {'complete': True},
                'repositories': [{'name': 'large-detail'}] * 100}
    parser = cli.build_parser()
    with patch.object(cli.workspace_module, 'discover_workspace', return_value=observed), patch.object(cli, 'out') as out:
        cli.cmd_workspace(parser.parse_args(['workspace', '--root', '/workspace', '--summary']))
        response = out.call_args.args[0]
        assert response['schema_version'] == 'mncs.environment.workspace-readiness/1'
        assert response['repository_count'] == 100 and 'repositories' not in response
        cli.cmd_workspace(parser.parse_args(['workspace', '--root', '/workspace']))
        assert out.call_args.args[0] == observed
