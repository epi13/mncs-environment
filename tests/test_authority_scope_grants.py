"""Campaign-scoped repository grants admitted by Environment and persisted in Store."""

from __future__ import annotations

import hashlib
import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mncs_env import authority, claims, intent, sessions
from mncs_env import workspace as workspace_module
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
    manifest = root / ".mncs" / "project.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({"repository": name}) + "\n", encoding="utf-8")
    _git(root, "add", "README.md", ".mncs/project.json")
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
           request_id: str = "req_fixture_control_scope_grant",
           owner_authorized_checkout: dict[str, str] | None = None) -> dict:
    return session.grant_repository_scope(
        repository="mncs-control-mcp",
        campaign_id=CAMPAIGN,
        expected_authority_revision=(
            revision or session.effective_authority()["revision"]
        ),
        approval_reference=APPROVAL,
        owner_authorized_checkout=owner_authorized_checkout,
        authenticated_principal_id=principal,
        request_id=request_id,
    )


def _nested_target_checkout(
    session: sessions.Session,
    workspace: Path,
    slug: str,
    branch: str | None,
) -> tuple[Path, dict[str, str | None]]:
    """Create a registered linked checkout inside the repository's worktrees."""
    repository_root = workspace / "mncs-control-mcp"
    manifest = repository_root / ".mncs" / "project.json"
    if not manifest.exists():
        manifest.parent.mkdir(parents=True)
        manifest.write_text(
            json.dumps({"repository": "mncs-control-mcp"}) + "\n",
            encoding="utf-8",
        )
        _git(repository_root, "add", ".mncs/project.json")
        _git(repository_root, "commit", "-m", "add repository identity")
    common = Path(_git(repository_root, "rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = repository_root / common
    exclude = common / "info" / "exclude"
    current_excludes = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if ".worktrees/" not in current_excludes:
        exclude.write_text(current_excludes + "\n.worktrees/\n", encoding="utf-8")
    checkout = repository_root / ".worktrees" / slug / "mncs-control-mcp"
    checkout.parent.mkdir(parents=True, exist_ok=True)
    if branch is None:
        _git(repository_root, "worktree", "add", "--detach", str(checkout), "HEAD")
    else:
        _git(repository_root, "worktree", "add", "-b", branch, str(checkout), "HEAD")
    observed = workspace_module.verify_registered_checkout(
        checkout,
        repository="mncs-control-mcp",
        workspace_root=workspace,
        expected_repository_root=repository_root,
    )
    session.snapshot.setdefault("selected_checkouts", {})[
        "mncs-control-mcp"
    ] = {
        **observed,
        "repository": "mncs-control-mcp",
        "path": str(checkout.resolve()),
        "branch": branch,
        "head": observed["head"],
        "clean": True,
    }
    return checkout.resolve(), observed


def test_detached_registered_checkouts_do_not_block_scope_admission(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = _session(tmp_path / "state", workspace)
    try:
        target_checkout, target_facts = _nested_target_checkout(
            session, workspace, "detached-target", None,
        )
        repository_root = workspace / "mncs-control-mcp"
        other_checkout = (
            repository_root / ".worktrees" / "detached-other" / "mncs-control-mcp"
        )
        other_checkout.parent.mkdir(parents=True, exist_ok=True)
        _git(repository_root, "worktree", "add", "--detach", str(other_checkout), "HEAD")
        other_facts = workspace_module.verify_registered_checkout(
            other_checkout,
            repository="mncs-control-mcp",
            workspace_root=workspace,
            expected_repository_root=repository_root,
            expected_branch=None,
        )
        assert target_facts["branch"] in (None, "")
        assert other_facts["branch"] in (None, "")
        other_claim = claims.acquire(
            session.store,
            repository="mncs-control-mcp",
            session_id="ses_independent_detached_owner",
            consumer_id="independent-detached-owner",
            basis=claims.BASIS_EXPLICIT,
            reason="independent detached registered worktree",
            scope={
                "kind": "worktree",
                "checkout": str(other_checkout.resolve()),
                "branch": None,
            },
            checkout_facts={"dirty": False, "foreign_signals": []},
            request_id="req_independent_detached_worktree_claim",
        )
        assert other_claim["scope"]["branch"] is None

        grant = _grant(session, request_id="req_detached_scope_grant")
        assert grant["repository_identity"]["path"] == str(target_checkout)
        assert grant["repository_identity"]["branch"] is None
        claim = session.acquire_claim(
            "mncs-control-mcp",
            reason="exact detached worktree claim",
            request_id="req_detached_scope_claim",
        )
        assert claim["scope"]["checkout"] == str(target_checkout)
        assert claim["scope"]["branch"] is None
        assert len(claims.holders(session.store.read_claims())["mncs-control-mcp"]) == 2
    finally:
        session.close()


def _recovered_claim(
    session: sessions.Session, repository: str, *,
    claim_session: str | None = None,
    requester_session: str | None = None,
    bad_authorization_id: bool = False,
    basis: str = claims.BASIS_RECOVERY,
    scope: dict | None = None,
) -> dict:
    """Publish a fixture claim with the provenance shape Control recovery creates."""
    scope = claims.normalize_scope(scope, repository)
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


def test_owner_grant_admits_exact_registered_worktree_outside_campaign_workspace(
    tmp_path: Path,
) -> None:
    campaign_workspace = tmp_path / "campaign-workspace"
    campaign_workspace.mkdir()
    configured_workspace = tmp_path / "configured-projects"
    configured_workspace.mkdir()
    session = _session(tmp_path / "state", campaign_workspace)
    try:
        repository = _checkout(configured_workspace, "mncs-control-mcp")
        repository_root = configured_workspace / repository["name"]
        checkout = repository_root / ".worktrees" / "compiler-scope" / repository["name"]
        checkout.parent.mkdir(parents=True)
        _git(
            repository_root, "worktree", "add", "-b",
            "campaign/compiler-scope", str(checkout), "HEAD",
        )
        checkout_facts = workspace_module.verify_registered_checkout(
            checkout,
            repository=repository["name"],
            workspace_root=configured_workspace,
            expected_repository_root=repository_root,
            expected_branch="campaign/compiler-scope",
        )

        grant = _grant(
            session,
            owner_authorized_checkout=checkout_facts,
            request_id="req_external_owner_scope_grant",
        )

        identity = grant["repository_identity"]
        assert identity["path"] == str(checkout.resolve())
        assert identity["repository_root"] == str(repository_root.resolve())
        assert identity["checkout_identity"] == checkout_facts["checkout_identity"]
        claim = session.acquire_claim(
            "mncs-control-mcp",
            reason="claim the exact owner-approved registered worktree",
            request_id="req_external_owner_scope_claim",
        )
        assert claim["scope"]["checkout"] == str(checkout.resolve())
        assert claim["scope"]["branch"] == "campaign/compiler-scope"
        assert claim["scope"]["exclusive"] is False
        session_id = session.session_id
        assert session.release_claim(
            "mncs-control-mcp",
            claim_id=claim["claim_id"],
            reason="exercise exact checkout continuation after restart",
            request_id="req_external_owner_scope_release",
        ) is True
        session.close()
        session = None

        reopened_store = open_store(
            tmp_path / "state", "store", verify_on_open=False,
        )
        resumed = sessions.Session.open(
            state_dir=tmp_path / "state",
            session_id=session_id,
            backend="store",
            verify_on_open=False,
            store=reopened_store,
        )
        try:
            resumed_claim = resumed.acquire_claim(
                "mncs-control-mcp",
                reason="resume the exact owner-approved worktree claim",
                request_id="req_external_owner_scope_reclaim",
            )
            assert resumed_claim["scope"]["checkout"] == str(checkout.resolve())
            assert resumed_claim["scope"]["branch"] == "campaign/compiler-scope"
        finally:
            resumed.close()
    finally:
        if session is not None:
            session.close()


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


def test_nested_registered_scope_uses_one_worktree_and_coexists_with_other_branch(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = _session(tmp_path / "state", workspace)
    try:
        first_checkout, first_facts = _nested_target_checkout(
            session, workspace, "first", "campaign/scope-first",
        )
        first_selection = session.snapshot["selected_checkouts"]["mncs-control-mcp"]

        # A live lease in a different registered worktree on a different
        # branch must not block this exact checkout's authenticated grant.
        second_checkout = workspace / "mncs-control-mcp" / ".worktrees" / "second" / "mncs-control-mcp"
        second_checkout.parent.mkdir(parents=True, exist_ok=True)
        _git(
            workspace / "mncs-control-mcp", "worktree", "add", "-b",
            "campaign/scope-second", str(second_checkout), "HEAD",
        )
        second_facts = workspace_module.verify_registered_checkout(
            second_checkout,
            repository="mncs-control-mcp",
            workspace_root=workspace,
            expected_repository_root=workspace / "mncs-control-mcp",
        )
        other_claim = claims.acquire(
            session.store,
            repository="mncs-control-mcp",
            session_id="ses_independent_worktree_owner",
            consumer_id="independent-worktree-owner",
            basis=claims.BASIS_EXPLICIT,
            reason="independent registered worktree",
            scope={
                "kind": "worktree",
                "checkout": str(second_checkout.resolve()),
                "branch": "campaign/scope-second",
            },
            checkout_facts={"dirty": False, "foreign_signals": []},
            request_id="req_independent_worktree_claim",
        )
        target_scope = claims.normalize_scope({
            "kind": "worktree",
            "checkout": str(first_checkout),
            "branch": "campaign/scope-first",
        }, "mncs-control-mcp")
        other_scope = claims.normalize_scope(other_claim["scope"], "mncs-control-mcp")
        assert claims.scopes_conflict(target_scope, other_scope) is None

        grant = _grant(session)
        assert grant["repository_identity"]["path"] == str(first_checkout)
        assert grant["repository_identity"]["checkout_identity"] == first_facts[
            "checkout_identity"
        ]
        assert first_selection["path"] == str(first_checkout)

        claim = session.acquire_claim(
            "mncs-control-mcp",
            reason="exact nested worktree claim",
            request_id="req_nested_worktree_scope_claim",
        )
        assert claim["scope"]["kind"] == "worktree"
        assert claim["scope"]["checkout"] == str(first_checkout)
        report = session.revalidate()
        assert report["scope_composition"]["status"] == "ready"
        assert report["scope_composition"]["retained"] == ["mncs-control-mcp"]
        selected = session.snapshot["selected_checkouts"]["mncs-control-mcp"]
        assert selected["path"] == str(first_checkout)
        assert selected["checkout_identity"] == first_facts["checkout_identity"]
        assert second_facts["checkout_identity"] != first_facts["checkout_identity"]
        session_id = session.session_id
        session.close()
        reopened_store = open_store(tmp_path / "state", "store", verify_on_open=False)
        resumed = sessions.Session.open(
            state_dir=tmp_path / "state",
            session_id=session_id,
            backend="store",
            verify_on_open=False,
            store=reopened_store,
        )
        try:
            resumed_report = resumed.revalidate()
            assert resumed_report["scope_composition"]["status"] == "ready"
            resumed_selected = resumed.snapshot["selected_checkouts"]["mncs-control-mcp"]
            assert resumed_selected["path"] == str(first_checkout)
            assert resumed_selected["checkout_identity"] == first_facts[
                "checkout_identity"
            ]
        finally:
            resumed.close()
        session = None
    finally:
        if session is not None:
            session.close()


def test_nested_worktree_root_resolves_its_claim_against_git_common_identity(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = _session(tmp_path / "state", workspace)
    try:
        checkout, facts = _nested_target_checkout(
            session, workspace, "workspace-root", "campaign/worktree-workspace-root",
        )
        _grant(session, request_id="req_nested_workspace_root_scope_grant")

        claim = session.acquire_claim(
            "mncs-control-mcp",
            reason="claim from registered linked-worktree Environment root",
            scope={
                "kind": "worktree",
                "checkout": str(checkout),
                "branch": "campaign/worktree-workspace-root",
            },
            workspace_root=str(checkout),
            request_id="req_nested_workspace_root_claim",
        )

        assert claim["scope"]["checkout"] == str(checkout)
        assert claim["scope"]["branch"] == "campaign/worktree-workspace-root"
        resolved, observed = sessions._verified_scope_checkout(
            checkout,
            "mncs-control-mcp",
            session.snapshot["selected_checkouts"]["mncs-control-mcp"],
            authorized_checkout=session.snapshot["selected_checkouts"]["mncs-control-mcp"],
        )
        assert resolved == checkout
        assert observed["checkout_identity"] == facts["checkout_identity"]
        assert observed["git_common_directory_identity"] == facts[
            "git_common_directory_identity"
        ]
    finally:
        session.close()


def test_exact_declared_nested_provider_checkout_is_claimable_and_authorized(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = _session(tmp_path / "state", workspace)
    try:
        _checkout(workspace, "mncs-forge")
        compiler_checkout = Path(_checkout(workspace, "mncs-compiler")["path"])
        compiler_root = workspace / compiler_checkout
        provider_root = (
            compiler_root / ".worktrees" / "native-application" / "mncs-forge"
        )
        provider_root.mkdir(parents=True)
        subprocess.run(
            ["git", "init", "-b", "main", str(provider_root)],
            check=True, capture_output=True, text=True,
        )
        _git(provider_root, "config", "user.name", "Environment Scope Test")
        _git(provider_root, "config", "user.email", "scope-test@example.invalid")
        (provider_root / "README.md").write_text("declared provider\n", encoding="utf-8")
        manifest = provider_root / ".mncs" / "project.json"
        manifest.parent.mkdir()
        manifest.write_text(json.dumps({"repository": "mncs-forge"}) + "\n")
        _git(provider_root, "add", "README.md", ".mncs/project.json")
        _git(provider_root, "commit", "-m", "declared provider checkout")

        session.snapshot["authority"]["writable"] = [
            "mncs-environment", "mncs-forge",
        ]
        session.snapshot["bindings"] = [{
            "provider": "mncs-forge",
            "capability": "mncs-forge:native-application-execution",
            "effects": ["execute"],
            "provider_root": str(provider_root),
        }]
        session._save()

        claim = session.acquire_claim(
            "mncs-forge",
            reason="native application provider writes its exact declared checkout",
            scope={
                "kind": "worktree",
                "checkout": str(provider_root),
                "branch": "main",
            },
            request_id="req_declared_nested_provider_claim",
        )
        assert claim["scope"]["kind"] == "worktree"
        assert claim["scope"]["repository"] == "mncs-forge"
        assert claim["scope"]["checkout"] == str(provider_root)
        assert claim["scope"]["branch"] == "main"
        assert claim["scope"]["exclusive"] is False
        assert claim["scope"]["checkout_identity"]
        assert claim["scope"]["git_common_directory_identity"]
        assert claim["scope"]["manifest_identity"]

        verdict = session.check(
            action="execute",
            target="mncs-forge",
            scope={
                "kind": "worktree",
                "checkout": str(provider_root),
                "branch": "main",
                "git_common_directory_identity": claim["scope"][
                    "git_common_directory_identity"
                ],
                "checkout_identity": claim["scope"]["checkout_identity"],
                "manifest_identity": claim["scope"]["manifest_identity"],
            },
        )
        assert verdict == {
            "verdict": "allow",
            "reason": "mncs-forge mutation by claim",
        }
        mismatched_identity = session.check(
            action="execute",
            target="mncs-forge",
            scope={
                "kind": "worktree",
                "checkout": str(provider_root),
                "branch": "main",
                "git_common_directory_identity": "git-common:replacement",
                "checkout_identity": "checkout:replacement",
                "manifest_identity": claim["scope"]["manifest_identity"],
            },
        )
        assert mismatched_identity["verdict"] == "escalate"

        lookalike = (
            compiler_root / ".worktrees" / "unbound-lookalike" / "mncs-forge"
        )
        lookalike.mkdir(parents=True)
        subprocess.run(
            ["git", "init", "-b", "main", str(lookalike)],
            check=True, capture_output=True, text=True,
        )
        _git(lookalike, "config", "user.name", "Environment Scope Test")
        _git(lookalike, "config", "user.email", "scope-test@example.invalid")
        (lookalike / "README.md").write_text("lookalike\n", encoding="utf-8")
        lookalike_manifest = lookalike / ".mncs" / "project.json"
        lookalike_manifest.parent.mkdir()
        lookalike_manifest.write_text(json.dumps({"repository": "mncs-forge"}) + "\n")
        _git(lookalike, "add", "README.md", ".mncs/project.json")
        _git(lookalike, "commit", "-m", "unbound lookalike")

        with pytest.raises(
            workspace_module.WorkspaceResolutionError,
            match="Git common repository is not an immediate repository in the authorized workspace",
        ):
            session.acquire_claim(
                "mncs-forge",
                reason="an unrelated matching manifest is not a provider checkout",
                scope={
                    "kind": "worktree",
                    "checkout": str(lookalike),
                    "branch": "main",
                },
                request_id="req_declared_provider_lookalike_rejected",
            )
    finally:
        session.close()


def test_exact_session_owned_claim_anchors_scope_when_selection_projection_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = _session(tmp_path / "state", workspace)
    try:
        linked_checkout, linked_facts = _nested_target_checkout(
            session, workspace, "retained-anchor", "campaign/retained-anchor",
        )
        _grant(session, request_id="req_missing_selection_scope_grant")
        linked_claim = session.acquire_claim(
            "mncs-control-mcp",
            reason="retain the campaign's registered linked checkout",
            scope={
                "kind": "worktree",
                "checkout": str(linked_checkout),
                "branch": "campaign/retained-anchor",
            },
            ttl_hours=1,
            request_id="req_missing_selection_anchor_claim",
        )
        assert linked_claim["scope"]["checkout"] == str(linked_checkout)
        repository_root = workspace / "mncs-control-mcp"
        second_checkout = (
            repository_root / ".worktrees" / "retained-anchor-two" / "mncs-control-mcp"
        )
        second_checkout.parent.mkdir(parents=True)
        _git(
            repository_root, "worktree", "add", "-b",
            "campaign/retained-anchor-two", str(second_checkout), "HEAD",
        )
        second_claim = session.acquire_claim(
            "mncs-control-mcp",
            reason="retain a second worktree of the same canonical repository",
            scope={
                "kind": "worktree",
                "checkout": str(second_checkout),
                "branch": "campaign/retained-anchor-two",
            },
            ttl_hours=1,
            request_id="req_missing_selection_second_anchor_claim",
        )
        session.snapshot["selected_checkouts"] = {}
        session._save()

        class ExpiredClaimClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz) + timedelta(hours=2)

        monkeypatch.setattr(claims, "datetime", ExpiredClaimClock)
        active = claims.active_claims(session.store.read_claims())
        assert linked_claim["claim_id"] not in active
        assert second_claim["claim_id"] not in active

        main_claim = session.acquire_claim(
            "mncs-control-mcp",
            reason="claim the exact repository root using its retained checkout anchor",
            scope={
                "kind": "worktree",
                "checkout": str(repository_root),
                "branch": "main",
            },
            request_id="req_missing_selection_main_claim",
        )

        assert main_claim["scope"]["checkout"] == str(repository_root)
        assert main_claim["scope"]["branch"] == "main"
        assert main_claim["session_id"] == session.session_id
        observed = workspace_module.verify_registered_checkout(
            main_claim["scope"]["checkout"],
            repository="mncs-control-mcp",
            expected_repository_root=repository_root,
            expected_branch="main",
        )
        assert observed["git_common_directory_identity"] == linked_facts[
            "git_common_directory_identity"
        ]
    finally:
        session.close()


def test_out_of_workspace_expired_claim_does_not_steer_nested_scope_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = _session(tmp_path / "state", workspace)
    try:
        nested_checkout, nested_facts = _nested_target_checkout(
            session, workspace, "continuation-anchor", "campaign/continuation-anchor",
        )
        _grant(session, request_id="req_workspace_anchor_filter_grant")

        repository_root = workspace / "mncs-control-mcp"
        main_claim = session.acquire_claim(
            "mncs-control-mcp",
            reason="retain a historical main checkout identity",
            scope={
                "kind": "worktree",
                "checkout": str(repository_root),
                "branch": "main",
            },
            ttl_hours=1,
            request_id="req_workspace_anchor_filter_main_claim",
        )
        nested_claim = session.acquire_claim(
            "mncs-control-mcp",
            reason="retain the campaign's nested registered checkout identity",
            scope={
                "kind": "worktree",
                "checkout": str(nested_checkout),
                "branch": "campaign/continuation-anchor",
            },
            ttl_hours=1,
            request_id="req_workspace_anchor_filter_nested_claim",
        )

        # Model a resumed campaign whose authorized root is now the nested
        # workspace. The old main checkout remains registered, but it must not
        # become the identity anchor merely because its path sorts first.
        authorized_root = nested_checkout.parent
        session.snapshot["workspace"]["root"] = str(authorized_root)
        session.snapshot["selected_checkouts"] = {}
        session._save()

        class ExpiredClaimClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.now(tz) + timedelta(hours=2)

        monkeypatch.setattr(claims, "datetime", ExpiredClaimClock)
        active = claims.active_claims(session.store.read_claims())
        assert main_claim["claim_id"] not in active
        assert nested_claim["claim_id"] not in active

        target_checkout = authorized_root / "mncs-control-mcp-target"
        _git(
            repository_root, "worktree", "add", "-b",
            "campaign/continuation-target", str(target_checkout), "HEAD",
        )
        target_claim = session.acquire_claim(
            "mncs-control-mcp",
            reason="claim a new nested checkout after continuation",
            scope={
                "kind": "worktree",
                "checkout": str(target_checkout),
                "branch": "campaign/continuation-target",
            },
            request_id="req_workspace_anchor_filter_target_claim",
        )

        assert target_claim["scope"]["checkout"] == str(target_checkout)
        assert target_claim["scope"]["branch"] == "campaign/continuation-target"
        observed = workspace_module.verify_registered_checkout(
            target_claim["scope"]["checkout"],
            repository="mncs-control-mcp",
            workspace_root=repository_root.parent,
            expected_repository_root=repository_root,
            expected_branch="campaign/continuation-target",
        )
        assert observed["git_common_directory_identity"] == nested_facts[
            "git_common_directory_identity"
        ]
    finally:
        session.close()


def test_session_owned_claims_from_different_git_common_directories_remain_ambiguous(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = _session(tmp_path / "state", workspace)
    try:
        linked_checkout, _ = _nested_target_checkout(
            session, workspace, "first-anchor", "campaign/first-anchor",
        )
        _grant(session, request_id="req_ambiguous_claim_scope_grant")
        session.acquire_claim(
            "mncs-control-mcp",
            reason="retain the registered checkout anchor",
            scope={
                "kind": "worktree",
                "checkout": str(linked_checkout),
                "branch": "campaign/first-anchor",
            },
            request_id="req_ambiguous_first_anchor_claim",
        )
        session.snapshot["selected_checkouts"] = {}
        session._save()

        unrelated_root = workspace / "unrelated" / "mncs-control-mcp"
        unrelated_root.parent.mkdir(parents=True)
        subprocess.run(
            ["git", "clone", "--quiet", "--shared",
             str(workspace / "mncs-control-mcp"), str(unrelated_root)],
            check=True,
            capture_output=True,
            text=True,
        )
        _recovered_claim(
            session,
            "mncs-control-mcp",
            scope={
                "kind": "worktree",
                "checkout": str(unrelated_root),
                "branch": "main",
            },
        )

        with pytest.raises(ValueError, match="multiple Git common directories") as error:
            session.acquire_claim(
                "mncs-control-mcp",
                reason="reject an ambiguous repository anchor",
                scope={
                    "kind": "worktree",
                    "checkout": str(workspace / "mncs-control-mcp"),
                    "branch": "main",
                },
                request_id="req_ambiguous_target_claim",
            )

        assert str(workspace / "mncs-control-mcp" / ".git") in str(error.value)
        assert str(unrelated_root / ".git") in str(error.value)
    finally:
        session.close()


def test_nested_registered_scope_recovers_only_exact_owner_fenced_worktree(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    session = _session(tmp_path / "state", workspace)
    try:
        checkout, facts = _nested_target_checkout(
            session, workspace, "recovered", "campaign/scope-recovered",
        )
        evidence = checkout / "evidence" / "campaign-retained-fixture.json"
        evidence.parent.mkdir()
        raw_evidence = b'{"identity":"ev_nested_retained_fixture"}\n'
        evidence.write_bytes(raw_evidence)
        recovered = _recovered_claim(
            session,
            "mncs-control-mcp",
            scope={
                "kind": "worktree",
                "checkout": str(checkout),
                "branch": "campaign/scope-recovered",
            },
        )
        assert session._is_owner_recovered_claim(
            recovered, "mncs-control-mcp", CAMPAIGN,
        )

        grant = _grant(session)
        preserved = grant["repository_identity"]["preserved_untracked_evidence"]
        assert grant["repository_identity"]["path"] == str(checkout)
        assert grant["repository_identity"]["checkout_identity"] == facts[
            "checkout_identity"
        ]
        assert preserved["file_count"] == 1
        assert preserved["files"][0]["path"] == "evidence/campaign-retained-fixture.json"
        assert preserved["files"][0]["sha256"] == hashlib.sha256(
            raw_evidence
        ).hexdigest()

        composition = session._compose_authorized_scope_checkouts()
        assert composition["status"] == "ready"
        assert composition["retained"] == ["mncs-control-mcp"]
        assert session.snapshot["selected_checkouts"]["mncs-control-mcp"][
            "checkout_identity"
        ] == facts["checkout_identity"]
    finally:
        session.close()


@pytest.mark.parametrize(
    ("foreign_claim", "expected_reason"),
    [(False, "no-live-checkout-claim"),
     (True, "claim-not-owned-matching-checkout-scope")],
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
