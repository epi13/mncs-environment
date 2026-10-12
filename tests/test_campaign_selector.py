"""Authenticated task selector publication on isolated canonical Stores."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from mncs_env import campaign_selector
from mncs_env import cli
from mncs_env import sessions
from mncs_env.session_store import open_store


def _selector(*, principal: str = "principal-a", workspace: str = "/projects/a",
              definition: str = "env_fixture") -> str:
    return campaign_selector.selector_identity(
        "durable-task-fixture",
        authenticated_principal_id=principal,
        definition_id=definition,
        workspace_root=workspace,
    )


def _bind(store, selector: str, campaign_id: str, session_id: str,
          request_id: str) -> dict:
    return campaign_selector.bind(
        store,
        selector=selector,
        authenticated_principal_id="principal-a",
        definition_id="env_fixture",
        workspace_root="/projects/a",
        campaign_id=campaign_id,
        session_id=session_id,
        request_id=request_id,
    )


def test_selector_is_scoped_to_principal_definition_and_workspace() -> None:
    base = _selector()
    assert base != _selector(principal="principal-b")
    assert base != _selector(workspace="/projects/b")
    assert base != _selector(definition="env_other")


def test_publication_retry_reads_back_commit_after_response_loss(tmp_path: Path) -> None:
    store = open_store(
        tmp_path / "environment-state", "store",
        verify_on_open=False, defer_mutation=False,
    )
    selector = _selector()

    class LostResponse:
        def read_evidence(self, evidence_id: str):
            return store.read_evidence(evidence_id)

        def write_evidence(self, evidence_id: str, record: dict):
            store.write_evidence(evidence_id, record)
            raise OSError("caller lost the publication response")

    try:
        committed = _bind(
            LostResponse(), selector, "cmp_fixture", "ses_fixture",
            "req_selector_interrupted_publication",
        )
        assert committed["campaign_id"] == "cmp_fixture"
        assert campaign_selector.read_binding(store, selector) == committed
        retried = _bind(
            store, selector, "cmp_fixture", "ses_fixture",
            "req_selector_interrupted_publication",
        )
        assert retried == committed
    finally:
        store.close()


def test_competing_campaign_bindings_have_one_winner(tmp_path: Path) -> None:
    state_dir = tmp_path / "environment-state"
    seed = open_store(state_dir, "store", verify_on_open=False,
                      defer_mutation=False)
    seed.close()
    stores = [
        open_store(state_dir, "store", verify_on_open=False,
                   defer_mutation=False)
        for _ in range(2)
    ]
    barrier = threading.Barrier(2)

    class FirstReadBarrier:
        def __init__(self, store):
            self.store = store
            self.lock = threading.Lock()
            self.wait_once = True

        def read_evidence(self, evidence_id: str):
            with self.lock:
                wait = self.wait_once
                self.wait_once = False
            record = self.store.read_evidence(evidence_id)
            if wait:
                barrier.wait(timeout=15)
            return record

        def write_evidence(self, evidence_id: str, record: dict):
            return self.store.write_evidence(evidence_id, record)

    wrappers = [FirstReadBarrier(store) for store in stores]
    selector = _selector()

    def attempt(pair):
        wrapper, index = pair
        try:
            return _bind(
                wrapper, selector, f"cmp_competitor_{index}",
                f"ses_competitor_{index}", f"req_selector_competitor_{index}",
            )
        except campaign_selector.TaskSelectorError as error:
            return error

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(attempt, zip(wrappers, range(2), strict=True)))
        winners = [item for item in outcomes if isinstance(item, dict)]
        conflicts = [
            item for item in outcomes
            if isinstance(item, campaign_selector.TaskSelectorError)
        ]
        assert len(winners) == 1, outcomes
        assert len(conflicts) == 1, outcomes
        assert conflicts[0].code == "campaign-task-selector-conflict"
        reopened = open_store(
            state_dir, "store", verify_on_open=False, defer_mutation=False
        )
        try:
            durable = campaign_selector.read_binding(reopened, selector)
            assert durable == winners[0]
        finally:
            reopened.close()
    finally:
        for store in stores:
            store.close()


def test_same_selector_cannot_be_rebound_to_another_campaign(tmp_path: Path) -> None:
    store = open_store(
        tmp_path / "environment-state", "store",
        verify_on_open=False, defer_mutation=False,
    )
    selector = _selector()
    try:
        _bind(store, selector, "cmp_first", "ses_first", "req_selector_first")
        with pytest.raises(campaign_selector.TaskSelectorError) as error:
            _bind(store, selector, "cmp_other", "ses_other", "req_selector_other")
        assert error.value.code == "campaign-task-selector-conflict"
    finally:
        store.close()


def test_project_continuation_binds_exact_authenticated_session(tmp_path: Path) -> None:
    state_dir = tmp_path / "environment-state"
    store = open_store(
        state_dir, "store", verify_on_open=False, defer_mutation=False,
    )
    session_id = "ses_project_continuation_fixture"
    store.save_snapshot(session_id, {
        "schema_version": "mncs.environment.session/5",
        "session_id": session_id,
        "authenticated_principal_id": "principal-a",
        "consumer_id": "consumer-before-restart",
        "lifecycle": "active",
        "snapshot_sequence": 1,
        "provenance": {"definition_id": "env_fixture"},
        "workspace": {"root": "/projects/a"},
        "campaign": {
            "identity": "cmp_project_continuation_fixture",
            "principal_id": "principal-a",
        },
    })
    try:
        session = sessions.Session(store, session_id)
        record = session.bind_task_identity(
            "durable-project-continuation",
            campaign_id="cmp_project_continuation_fixture",
            authenticated_principal_id="principal-a",
            request_id="req_project_continuation_binding",
        )
        assert record["session_id"] == session_id
        assert record["campaign_id"] == "cmp_project_continuation_fixture"
        assert record["provenance"]["admitted_by"] == (
            "mncs-environment.project-continuation"
        )
        selector = campaign_selector.selector_identity(
            "durable-project-continuation",
            authenticated_principal_id="principal-a",
            definition_id="env_fixture",
            workspace_root="/projects/a",
        )
        assert campaign_selector.read_binding(store, selector) == record
        assert session.bind_task_identity(
            "durable-project-continuation",
            campaign_id="cmp_project_continuation_fixture",
            authenticated_principal_id="principal-a",
            request_id="req_project_continuation_binding",
        ) == record
        with pytest.raises(sessions.AuthorityDenied):
            session.bind_task_identity(
                "durable-project-continuation",
                campaign_id="cmp_project_continuation_fixture",
                authenticated_principal_id="principal-b",
                request_id="req_unauthorized_project_continuation",
            )
        with pytest.raises(sessions.AuthorityDenied):
            session.bind_task_identity(
                "durable-project-continuation",
                campaign_id="cmp_other_campaign",
                authenticated_principal_id="principal-a",
                request_id="req_wrong_campaign_project_continuation",
            )
    finally:
        store.close()


def test_continuation_and_explicit_bind_cli_arguments_are_available() -> None:
    parser = cli.build_parser()
    continuation = parser.parse_args([
        "continuation", "--task-identity", "durable-task-after-restart",
    ])
    assert continuation.command == "continuation"
    assert continuation.task_identity == "durable-task-after-restart"

    binding = parser.parse_args([
        "campaign-bind",
        "--campaign-id", "cmp_selected_campaign",
        "--session-id", "ses_selected_session",
        "--task-identity", "durable-task-after-restart",
    ])
    assert binding.command == "campaign-bind"
    assert binding.campaign_id == "cmp_selected_campaign"
    assert binding.session_id == "ses_selected_session"
    assert binding.task_identity == "durable-task-after-restart"


def test_project_continuation_binding_reconciles_lost_store_response(tmp_path: Path) -> None:
    store = open_store(
        tmp_path / "environment-state", "store",
        verify_on_open=False, defer_mutation=False,
    )
    session_id = "ses_project_continuation_lost_response"
    store.save_snapshot(session_id, {
        "schema_version": "mncs.environment.session/5",
        "session_id": session_id,
        "authenticated_principal_id": "principal-a",
        "lifecycle": "active",
        "provenance": {"definition_id": "env_fixture"},
        "workspace": {"root": "/projects/a"},
        "campaign": {
            "identity": "cmp_project_continuation_lost_response",
            "principal_id": "principal-a",
        },
    })

    class LostResponse:
        def __getattr__(self, name):
            return getattr(store, name)

        def write_evidence(self, evidence_id: str, record: dict):
            store.write_evidence(evidence_id, record)
            raise OSError("lost response after immutable Store publication")

    selector = campaign_selector.selector_identity(
        "durable-lost-response-task",
        authenticated_principal_id="principal-a",
        definition_id="env_fixture",
        workspace_root="/projects/a",
    )
    try:
        session = sessions.Session(LostResponse(), session_id)
        binding = session.bind_task_identity(
            "durable-lost-response-task",
            campaign_id="cmp_project_continuation_lost_response",
            authenticated_principal_id="principal-a",
            request_id="req_project_continuation_lost_response",
        )
        assert binding["provenance"]["admitted_by"] == (
            "mncs-environment.project-continuation"
        )
        assert campaign_selector.read_binding(store, selector) == binding
        recovered = sessions.Session(store, session_id).bind_task_identity(
            "durable-lost-response-task",
            campaign_id="cmp_project_continuation_lost_response",
            authenticated_principal_id="principal-a",
            request_id="req_project_continuation_lost_response",
        )
        assert recovered == binding
    finally:
        store.close()
