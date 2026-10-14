"""Campaign-scoped repository grants admitted by Environment and persisted in Store."""

from __future__ import annotations

import hashlib
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mncs_env import authority, claims, intent, sessions
from mncs_env.session_store import open_store

PRINCIPAL = "ctrl_scope_grant_fixture"
CAMPAIGN = "cmp_scope_grant_fixture"
APPROVAL = "user-approval:scope-grant-fixture"


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _checkout(workspace: Path, name: str, *, dirty: bool = False) -> dict:
    root = workspace / name
    root.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "-b", "main", str(root)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(root, "config", "user.name", "Environment Scope Test")
    _git(root, "config", "user.email", "scope-test@example.invalid")
    (root / "README.md").write_text(f"{name}\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "fixture")
    if dirty:
        (root / "README.md").write_text("unproven change\n", encoding="utf-8")
    return {
        "name": name,
        "path": name,
        "branch": _git(root, "branch", "--show-current"),
        "head": _git(root, "rev-parse", "HEAD"),
        "dirty": dirty,
    }


def _session(state_dir: Path, workspace: Path, *, dirty_target: bool = False):
    env_repo = _checkout(workspace, "mncs-environment")
    target_repo = _checkout(
        workspace, "mncs-control-mcp", dirty=dirty_target
    )
    work_intent = intent.parse({
        "goal": "admit one explicitly approved Control repository scope",
        "repositories": ["mncs-environment"],
    })
    env = {
        "identity": "renv_scope_grant_fixture",
        "definition_id": "env_scope_grant_fixture",
        "resolution_inputs_digest": "fixture-inputs",
        "intent": work_intent,
        "authority": authority.build_context(
            subject="scope-grant-consumer",
            intent=work_intent,
            protected_repos=[],
            claim_holders={},
        ),
        "rights": {
            "available": True,
            "overall": "allowed",
            "subjects": {
                "mncs-environment": "allowed",
                "mncs-control-mcp": "allowed",
            },
        },
        "workspace": {
            "root": str(workspace),
            "repositories": [env_repo, target_repo],
            "repository_count": 2,
        },
        "selected_checkouts": {},
        "bindings": [],
    }
    store = open_store(state_dir, "store", verify_on_open=False)
    session = sessions.Session.create(
        state_dir=state_dir,
        environment=env,
        consumer_id="scope-grant-consumer",
        campaign_id=CAMPAIGN,
        authenticated_principal_id=PRINCIPAL,
        backend="store",
        store=store,
    )
    session.transition("resolving", "fixture")
    session.transition("ready", "fixture")
    session.transition("active", "fixture")
    session.acquire_claim(
        "mncs-environment",
        reason="the test session owns the Environment domain checkout",
        request_id="req_fixture_environment_claim",
    )
    return session


def _grant(session: sessions.Session, *, revision: str | None = None,
           principal: str = PRINCIPAL,
           request_id: str = "req_fixture_control_scope_grant") -> dict:
    return session.grant_repository_scope(
        repository="mncs-control-mcp",
        campaign_id=CAMPAIGN,
        expected_authority_revision=(
            revision or session.effective_authority()["revision"]
        ),
        approval_reference=APPROVAL,
        authenticated_principal_id=principal,
        request_id=request_id,
    )


def _recovered_claim(
    session: sessions.Session, repository: str, *,
    claim_session: str | None = None,
    requester_session: str | None = None,
    bad_authorization_id: bool = False,
    basis: str = claims.BASIS_RECOVERY,
) -> dict:
    """Publish a fixture claim with the provenance shape Control recovery creates."""
    scope = claims.normalize_scope(None, repository)
    claim_id = claims.claim_identity(repository, scope)
    claim_session = claim_session or session.session_id
    requester_session = requester_session or claim_session
    owner_session = "ses_fixture_former_owner"
    authorization = {
        "schema_version": "mncs.environment.control-owner-claim-recovery/2",
        "campaign_id": CAMPAIGN,
        "requester_session_id": requester_session,
        "requester_principal_id": PRINCIPAL,
        "recovery_basis": "operator-approved-config",
        "operator_approval_reference": "user-approval:fixture-recovery",
        "claim_id": claim_id,
        "repository": repository,
        "expected_claim_version": 3,
        "owner_session_id": owner_session,
        "owner_consumer_id": "former-consumer",
        "owner_scope": scope,
        "target_scope": scope,
    }
    stable_fields = (
        "schema_version", "campaign_id", "requester_session_id",
        "requester_principal_id", "recovery_basis",
        "operator_approval_reference", "owner_scope", "target_scope", "claim_id",
        "repository", "expected_claim_version", "owner_session_id",
        "owner_consumer_id",
    )
    authorization["authorization_id"] = claims.digest_hex(
        {field: authorization[field] for field in stable_fields}, length=64,
    )
    if bad_authorization_id:
        authorization["authorization_id"] = "0" * 64
    now = datetime.now(timezone.utc)
    record = {
        "schema_version": claims.SCHEMA,
        "claim_id": claim_id,
        "version": 5,
        "repository": repository,
        "scope": scope,
        "session_id": claim_session,
        "consumer_id": session.snapshot["consumer_id"],
        "basis": basis,
        "reason": "fixture owner-authorized recovery",
        "status": "held",
        "acquired_at": now.isoformat(timespec="seconds"),
        "expires_at": (now + timedelta(hours=12)).isoformat(timespec="seconds"),
        "provenance": {
            "owner_recovery": authorization,
            "recovered_from": [{
                "claim_id": claim_id,
                "version": 3,
                "superseded_version": 4,
                "owner_session": owner_session,
            }],
        },
    }
    record = claims._with_identity(record)
    session.store.put_claim(record)
    return record


def test_grant_is_store_durable_and_claimable_after_fresh_open(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    session = _session(state_dir, workspace)
    try:
        before = session.effective_authority()["revision"]
        grant = _grant(session, revision=before)
        assert grant["repository"] == "mncs-control-mcp"
        assert grant["approval_reference"] == APPROVAL
        assert grant["authorized_by_principal_id"] == PRINCIPAL
        assert grant["authority_revision_before"] == before
        assert grant["repository_identity"]["clean"] is True

        authority_after = session.effective_authority()
        assert "mncs-control-mcp" in authority_after["writable"]
        assert authority_after["revision"] == grant["authority_revision_after"]
        assert session.authority_scope_grants() == [grant]

        acquired = session.acquire_claim(
            "mncs-control-mcp",
            reason="scope-grant acceptance",
            request_id="req_fixture_control_claim_acquire",
        )
        assert acquired["repository"] == "mncs-control-mcp"
        assert acquired["session_id"] == session.session_id
        released = session.release_claim(
            "mncs-control-mcp",
            claim_id=acquired["claim_id"],
            reason="scope-grant acceptance complete",
            request_id="req_fixture_control_claim_release",
        )
        assert released is True
        session_id = session.session_id
    finally:
        session.close()

    reopened_store = open_store(state_dir, "store", verify_on_open=False)
    try:
        resumed = sessions.Session.open(
            state_dir=state_dir,
            session_id=session_id,
            backend="store",
            verify_on_open=False,
            store=reopened_store,
        )
        try:
            assert "mncs-control-mcp" in resumed.effective_authority()["writable"]
            durable = resumed.authority_scope_grants()
            assert len(durable) == 1
            assert durable[0]["identity"] == grant["identity"]
            assert durable[0]["repository_identity"]["head"]
        finally:
            resumed.close()
    except Exception:
        reopened_store.close()
        raise


def test_scope_grant_retry_is_idempotent_and_payload_fenced(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = _session(tmp_path / "state", workspace)
    try:
        before = session.effective_authority()["revision"]
        grant = _grant(session, revision=before)
        retry = _grant(
            session,
            revision=before,
            request_id="req_fixture_control_scope_grant",
        )
        assert retry == grant
        with pytest.raises(sessions.AuthorityScopeConflict) as error:
            session.grant_repository_scope(
                repository="mncs-control-mcp",
                campaign_id=CAMPAIGN,
                expected_authority_revision="0" * 64,
                approval_reference="different-approval",
                authenticated_principal_id=PRINCIPAL,
                request_id="req_fixture_control_scope_grant",
            )
        assert error.value.diagnostics["code"] == "authority-scope-request-conflict"
        assert len(session.authority_scope_grants()) == 1
    finally:
        session.close()


def test_scope_grant_retry_reconciles_after_commit_response_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    session = _session(state_dir, workspace)
    session_id = session.session_id
    revision = session.effective_authority()["revision"]
    request_id = "req_scope_grant_lost_response"
    store = session.store
    original_put = store.put_event_at_generation

    def commit_then_interrupt(*args, **kwargs):
        original_put(*args, **kwargs)
        raise OSError("simulated lost response after Store commit")

    monkeypatch.setattr(store, "put_event_at_generation", commit_then_interrupt)
    try:
        with pytest.raises(OSError, match="lost response"):
            _grant(session, revision=revision, request_id=request_id)
        assert len(session.authority_scope_grants()) == 1
    finally:
        session.close()

    resumed_store = open_store(state_dir, "store", verify_on_open=False)
    resumed = sessions.Session.open(
        state_dir=state_dir,
        session_id=session_id,
        backend="store",
        verify_on_open=False,
        store=resumed_store,
    )
    try:
        reconciled = _grant(resumed, revision=revision, request_id=request_id)
        assert reconciled["repository"] == "mncs-control-mcp"
        assert reconciled["approval_reference"] == APPROVAL
        assert len(resumed.authority_scope_grants()) == 1
    finally:
        resumed.close()


@pytest.mark.parametrize(
    ("principal", "campaign", "revision", "dirty"),
    [
        ("another-control-principal", CAMPAIGN, None, False),
        (PRINCIPAL, "cmp_other_campaign", None, False),
        (PRINCIPAL, CAMPAIGN, "0" * 64, False),
        (PRINCIPAL, CAMPAIGN, None, True),
    ],
)
def test_scope_grant_rejects_unowned_stale_or_dirty_requests(
    tmp_path: Path, principal: str, campaign: str,
    revision: str | None, dirty: bool,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = _session(tmp_path / "state", workspace, dirty_target=dirty)
    try:
        current_revision = session.effective_authority()["revision"]
        with pytest.raises((sessions.AuthorityDenied, sessions.AuthorityScopeConflict)):
            session.grant_repository_scope(
                repository="mncs-control-mcp",
                campaign_id=campaign,
                expected_authority_revision=revision or current_revision,
                approval_reference=APPROVAL,
                authenticated_principal_id=principal,
                request_id="req_fixture_rejected_scope_grant",
            )
        assert session.authority_scope_grants() == []
        assert "mncs-control-mcp" not in session.effective_authority()["writable"]
    finally:
        session.close()


def test_scope_grant_accepts_exact_recovered_claim_held_by_current_session(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = _session(tmp_path / "state", workspace)
    try:
        recovered = _recovered_claim(session, "mncs-control-mcp")
        grant = _grant(session)

        assert grant["repository"] == "mncs-control-mcp"
        assert recovered["session_id"] == session.session_id
        assert "mncs-control-mcp" in session.effective_authority()["writable"]
        assert session.authority_scope_grants() == [grant]
    finally:
        session.close()


@pytest.mark.parametrize(
    "claim_options",
    [
        {"claim_session": "ses_foreign_recovered_owner"},
        {"requester_session": "ses_other_requester"},
        {"bad_authorization_id": True},
        {"basis": claims.BASIS_EXPLICIT},
    ],
)
def test_scope_grant_rejects_foreign_or_nonrecovery_claims(
    tmp_path: Path, claim_options: dict,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = _session(tmp_path / "state", workspace)
    try:
        _recovered_claim(session, "mncs-control-mcp", **claim_options)

        with pytest.raises(sessions.AuthorityScopeConflict):
            _grant(session)
        assert session.authority_scope_grants() == []
        assert "mncs-control-mcp" not in session.effective_authority()["writable"]
    finally:
        session.close()


def test_recovered_scope_records_and_revalidates_preserved_campaign_evidence(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = _session(tmp_path / "state", workspace)
    try:
        evidence_dir = workspace / "mncs-control-mcp" / "evidence"
        evidence_dir.mkdir()
        evidence_path = evidence_dir / "campaign-retained-fixture.json"
        raw = b'{"identity":"ev_retained_fixture","schema_version":1}\n'
        evidence_path.write_bytes(raw)
        _recovered_claim(session, "mncs-control-mcp")

        grant = _grant(session)
        inventory = grant["repository_identity"]["preserved_untracked_evidence"]
        assert grant["repository_identity"]["clean"] is False
        assert inventory["file_count"] == 1
        assert inventory["files"][0]["path"] == "evidence/campaign-retained-fixture.json"
        assert inventory["files"][0]["sha256"] == hashlib.sha256(raw).hexdigest()
        assert session._compose_authorized_scope_checkouts()["composed"] == [
            "mncs-control-mcp"
        ]

        evidence_path.write_bytes(b'{"identity":"changed"}\n')
        report = session._compose_authorized_scope_checkouts()
        assert "mncs-control-mcp" in report["removed"]
        assert "mncs-control-mcp" not in session.snapshot["selected_checkouts"]
    finally:
        session.close()


@pytest.mark.parametrize("dirty_path", ["notes.py", "evidence/other-results.json"])
def test_recovered_scope_rejects_untracked_source_or_noncampaign_data(
    tmp_path: Path, dirty_path: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = _session(tmp_path / "state", workspace)
    try:
        dirty = workspace / "mncs-control-mcp" / dirty_path
        dirty.parent.mkdir(parents=True, exist_ok=True)
        dirty.write_text("unreviewed existing work\n", encoding="utf-8")
        _recovered_claim(session, "mncs-control-mcp")

        with pytest.raises(sessions.AuthorityDenied):
            _grant(session)
        assert session.authority_scope_grants() == []
    finally:
        session.close()


def test_stale_authority_revision_is_rejected_after_another_grant(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # Add a third clean checkout so the second grant has a distinct target.
    session = _session(tmp_path / "state", workspace)
    try:
        doctor = _checkout(workspace, "mncs-doctor")
        session.snapshot["workspace"]["repositories"].append(doctor)
        stale = session.effective_authority()["revision"]
        _grant(session, revision=stale)
        with pytest.raises(sessions.AuthorityScopeConflict) as error:
            session.grant_repository_scope(
                repository="mncs-doctor",
                campaign_id=CAMPAIGN,
                expected_authority_revision=stale,
                approval_reference=APPROVAL,
                authenticated_principal_id=PRINCIPAL,
                request_id="req_fixture_second_scope_grant",
            )
        assert error.value.diagnostics["code"] == "authority-scope-conflict"
    finally:
        session.close()


def test_concurrent_grants_with_one_authority_revision_have_one_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    initial = _session(state_dir, workspace)
    session_id = initial.session_id
    initial.close()

    left_store = open_store(state_dir, "store", verify_on_open=False)
    right_store = open_store(state_dir, "store", verify_on_open=False)
    left = sessions.Session.open(
        state_dir=state_dir, session_id=session_id, backend="store", store=left_store
    )
    right = sessions.Session.open(
        state_dir=state_dir, session_id=session_id, backend="store", store=right_store
    )
    barrier = threading.Barrier(2)
    for store in (left_store, right_store):
        original_put = store.put_event_at_generation

        def synchronized_put(*args, _put=original_put, **kwargs):
            barrier.wait(timeout=10)
            return _put(*args, **kwargs)

        monkeypatch.setattr(store, "put_event_at_generation", synchronized_put)

    revision = left.effective_authority()["revision"]

    def attempt(session: sessions.Session, request_id: str):
        try:
            return _grant(session, revision=revision, request_id=request_id)
        except sessions.AuthorityScopeConflict as error:
            return error

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda args: attempt(*args),
                [
                    (left, "req_concurrent_scope_grant_left"),
                    (right, "req_concurrent_scope_grant_right"),
                ],
            ))
        winners = [item for item in results if isinstance(item, dict)]
        conflicts = [item for item in results if isinstance(item, sessions.AuthorityScopeConflict)]
        assert len(winners) == 1
        assert len(conflicts) == 1
        assert conflicts[0].diagnostics["publication_completed"] is False
        assert len(left.authority_scope_grants()) == 1
        assert "mncs-control-mcp" in left.effective_authority()["writable"]
    finally:
        left.close()
        right.close()


def test_revalidation_composes_only_the_granted_checkout_with_owned_claim(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = _session(tmp_path / "state", workspace)
    try:
        grant = _grant(session)
        claim = session.acquire_claim(
            "mncs-control-mcp",
            reason="continuation composition fixture",
            request_id="req_fixture_composition_claim",
        )

        report = session.revalidate()

        composition = report["scope_composition"]
        assert composition["status"] == "ready"
        assert composition["composed"] == ["mncs-control-mcp"]
        selected = session.snapshot["selected_checkouts"]["mncs-control-mcp"]
        assert selected["source_ref"] == "authenticated-scope-grant"
        assert selected["checkout_identity"] == grant["repository_identity"][
            "checkout_identity"
        ]
        assert session.snapshot["workspace"]["selection"] == ["mncs-control-mcp"]

        events = [
            event for event in session._log()
            if event.get("type") == "workspace.checkout-composed"
        ]
        assert len(events) == 1
        record = events[0]["payload"]["composition"]
        assert record["scope_grant_identity"] == grant["identity"]
        assert record["claim_id"] == claim["claim_id"]
        assert record["claim_version"] == claim["version"]
    finally:
        session.close()


@pytest.mark.parametrize(
    ("foreign_claim", "expected_reason"),
    [(False, "no-live-exclusive-claim"),
     (True, "claim-not-owned-exclusive-repository-scope")],
)
def test_revalidation_does_not_compose_without_current_session_claim(
    tmp_path: Path, foreign_claim: bool, expected_reason: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = _session(tmp_path / "state", workspace)
    try:
        _grant(session)
        if foreign_claim:
            claims.acquire(
                session.store,
                repository="mncs-control-mcp",
                session_id="ses_foreign_scope_owner",
                # Matching the user's consumer label still does not transfer
                # the exact Environment session identity that owns the grant.
                consumer_id="scope-grant-consumer",
                basis=claims.BASIS_EXPLICIT,
                reason="fixture foreign owner",
                checkout_facts={"dirty": False, "foreign_signals": []},
                request_id="req_fixture_foreign_scope_claim",
            )

        report = session.revalidate()

        composition = report["scope_composition"]
        assert composition["status"] == "unknown"
        assert composition["skipped"] == [{
            "repository": "mncs-control-mcp",
            "reason": expected_reason,
        }]
        assert "mncs-control-mcp" not in session.snapshot["selected_checkouts"]
        assert not [
            event for event in session._log()
            if event.get("type") == "workspace.checkout-composed"
        ]
    finally:
        session.close()


def test_revalidation_preserves_dirty_work_owned_by_the_live_claim(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = _session(tmp_path / "state", workspace)
    try:
        _grant(session)
        session.acquire_claim(
            "mncs-control-mcp",
            reason="claim before making campaign changes",
            request_id="req_fixture_dirty_composition_claim",
        )
        (workspace / "mncs-control-mcp" / "README.md").write_text(
            "owned campaign change\n", encoding="utf-8"
        )

        report = session.revalidate()

        assert report["scope_composition"]["status"] == "ready"
        assert session.snapshot["selected_checkouts"]["mncs-control-mcp"][
            "clean"
        ] is False
    finally:
        session.close()


def test_revalidation_rejects_a_checkout_whose_git_identity_changed(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = _session(tmp_path / "state", workspace)
    try:
        _grant(session)
        session.acquire_claim(
            "mncs-control-mcp",
            reason="claim for checkout identity fixture",
            request_id="req_fixture_identity_composition_claim",
        )
        _git(
            workspace / "mncs-control-mcp",
            "remote", "add", "origin", "https://example.invalid/replaced.git",
        )

        report = session.revalidate()

        assert report["scope_composition"]["status"] == "unknown"
        assert report["scope_composition"]["skipped"] == [{
            "repository": "mncs-control-mcp",
            "reason": "checkout-identity-mismatch",
        }]
        assert "mncs-control-mcp" not in session.snapshot["selected_checkouts"]
    finally:
        session.close()


def test_revalidation_drops_composed_checkout_when_claim_is_released(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = _session(tmp_path / "state", workspace)
    try:
        _grant(session)
        claim = session.acquire_claim(
            "mncs-control-mcp",
            reason="claim for composition and release fixture",
            request_id="req_fixture_release_composition_claim",
        )
        session.revalidate()
        assert "mncs-control-mcp" in session.snapshot["selected_checkouts"]

        session.release_claim(
            "mncs-control-mcp",
            claim_id=claim["claim_id"],
            reason="release composition fixture",
            request_id="req_fixture_release_composition_claim_release",
        )
        report = session.revalidate()

        assert report["scope_composition"]["removed"] == ["mncs-control-mcp"]
        assert "mncs-control-mcp" not in session.snapshot["selected_checkouts"]
        assert "mncs-control-mcp" not in session.snapshot["workspace"]["selection"]
    finally:
        session.close()


def test_revalidation_reconciles_checkout_composition_after_lost_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_dir = tmp_path / "state"
    session = _session(state_dir, workspace)
    _grant(session)
    session.acquire_claim(
        "mncs-control-mcp",
        reason="claim for interrupted composition fixture",
        request_id="req_fixture_interrupted_composition_claim",
    )
    session_id = session.session_id
    request_id = "req_fixture_interrupted_scope_revalidation"
    store = session.store
    original_put = store.put_event
    interrupted = False

    def commit_then_interrupt(*args, **kwargs):
        nonlocal interrupted
        result = original_put(*args, **kwargs)
        if not interrupted:
            interrupted = True
            raise OSError("simulated lost response after Store commit")
        return result

    monkeypatch.setattr(store, "put_event", commit_then_interrupt)
    try:
        with pytest.raises(OSError, match="lost response"):
            session.revalidate(request_id=request_id)
    finally:
        session.close()

    resumed_store = open_store(state_dir, "store", verify_on_open=False)
    resumed = sessions.Session.open(
        state_dir=state_dir,
        session_id=session_id,
        backend="store",
        verify_on_open=False,
        store=resumed_store,
    )
    try:
        report = resumed.revalidate(request_id=request_id)
        assert report["scope_composition"]["status"] == "ready"
        assert "mncs-control-mcp" in resumed.snapshot["selected_checkouts"]
        assert len([
            event for event in resumed._log()
            if event.get("type") == "workspace.checkout-composed"
        ]) == 1
    finally:
        resumed.close()
