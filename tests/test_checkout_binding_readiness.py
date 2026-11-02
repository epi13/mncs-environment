"""Real Store + Git provider execution after ordinary checkout selection."""
import json

import pytest
from pathlib import Path

from mncs_env import entry, sessions
from test_authority_scope_grants import (
    _checkout, _git, _nested_target_checkout, CAMPAIGN, PRINCIPAL,
)


def test_checkout_binding_routes_execution_and_survives_release_and_restart(tmp_path):
    workspace = tmp_path / "workspace"
    root = workspace / "mncs-environment"
    _checkout(workspace, "mncs-environment")
    (root / "provider.py").write_text(
        'import pathlib\np=pathlib.Path.cwd()\n'
        '(p / "sentinel.txt").write_text("bounded operation\\n")\nprint(p)\n'
    )
    (root / "family-semantic-contracts-v1.json").write_text(json.dumps({
        "repository_id": "mncs-environment", "provides": [{
            "contract_identity": "fixture.write/1", "contract_revision": "1",
            "effects": ["write"], "invocation": {
                "kind": "python", "path": "provider.py", "fixed_argv": [],
            },
        }],
    }))
    definition = {
        "name": "bounded-development", "working_checkout": "current",
        "workspace_scope": {"kind": "workspace", "repositories": ["mncs-environment"]},
        "intent": {"goal": "exercise ordinary checkout selection", "repositories": ["mncs-environment"]},
    }
    path = root / ".mncs/environment.json"
    path.write_text(json.dumps(definition))
    _git(root, "add", ".")
    _git(root, "commit", "-m", "declare provider and development entry")
    state = tmp_path / "state"
    context = entry.enter(
        definition=definition, definition_path=path, workspace_root=str(workspace),
        state_dir=state, backend="store", consumer_id="agent", consumer_kind="agent",
        authenticated_principal_id=PRINCIPAL, campaign_id=CAMPAIGN,
    )
    session = sessions.Session.open(state_dir=state, session_id=context["session_id"])
    try:
        checkout, facts = _nested_target_checkout(
            session, workspace, "selected", "campaign/selected", repository="mncs-environment",
        )
        grant = session.grant_repository_scope(
            repository="mncs-environment", campaign_id=CAMPAIGN,
            expected_authority_revision=session.effective_authority()["revision"],
            approval_reference=f"campaign-writable-checkout:{CAMPAIGN}:mncs-environment:{session.session_id}",
            owner_authorized_checkout=facts, campaign_writable_checkout=True,
            authenticated_principal_id=PRINCIPAL, request_id="req_select_checkout",
        )
        # No private reconciliation command or temporary ownership claim should
        # be needed to make the explicitly selected provider usable.
        result = session.invoke("fixture.write/1", [])
        assert result["status"] == "ok", result
        assert Path(result["stdout"].strip()) == checkout
        assert not (root / "sentinel.txt").exists()
        assert (checkout / "sentinel.txt").is_file()
        _git(checkout, "add", "sentinel.txt")
        _git(checkout, "commit", "-m", "record bounded isolated operation")
        claim = session.acquire_claim("mncs-environment", reason="bounded operation")
        assert claim["scope"]["checkout"] == str(checkout)
        session.release_claim("mncs-environment")
        (checkout / "README.md").write_text("legitimate unfinished work\n")
        session.checkpoint(progress="dirty isolated work retained", remaining=["resume"])
        session_id = session.session_id
    finally:
        session.close()
    resumed = sessions.Session.resume(state_dir=state, session_id=session_id)
    try:
        resumed.revalidate()
        assert resumed.snapshot["selected_checkouts"]["mncs-environment"]["scope_grant_identity"] == grant["identity"]
        assert resumed.invoke("fixture.write/1", [])["status"] == "ok"
        assert (checkout / "README.md").read_text() == "legitimate unfinished work\n"
        # A temporarily unavailable provider has a normal refresh/retry path.
        provider = checkout / "provider.py"
        hidden = checkout / "provider.unavailable"
        provider.rename(hidden)
        resumed.revalidate()
        with pytest.raises(sessions.AuthorityDenied, match="not available"):
            resumed.invoke("fixture.write/1", [])
        hidden.rename(provider)
        resumed.revalidate(request_id="req_provider_recovered")
        resumed.revalidate(request_id="req_provider_recovered")
        assert resumed.invoke("fixture.write/1", [])["status"] == "ok"
        _git(checkout, "add", "README.md")
        _git(checkout, "commit", "-m", "bounded forward progress")
        resumed.revalidate()
        assert resumed.invoke("fixture.write/1", [])["status"] == "ok"
        # A separate real session can acquire this released scope. Its live
        # claim must still fence the original provider operation.
        other_context = entry.enter(
            definition=definition, definition_path=path, workspace_root=str(workspace),
            state_dir=state, backend="store", consumer_id="other", consumer_kind="agent",
            authenticated_principal_id=PRINCIPAL, campaign_id="cmp_competing_work",
        )
        other = sessions.Session.open(state_dir=state, session_id=other_context["session_id"])
        try:
            from test_authority_scope_grants import _refresh_owner_facts
            current = _refresh_owner_facts(checkout, workspace, "mncs-environment", "campaign/selected")
            other.grant_repository_scope(
                repository="mncs-environment", campaign_id="cmp_competing_work",
                expected_authority_revision=other.effective_authority()["revision"],
                approval_reference=f"campaign-writable-checkout:cmp_competing_work:mncs-environment:{other.session_id}",
                owner_authorized_checkout=current, campaign_writable_checkout=True,
                authenticated_principal_id=PRINCIPAL, request_id="req_competing_checkout",
            )
            other.acquire_claim("mncs-environment", reason="separate live work")
            assert resumed.invoke("fixture.write/1", [])["status"] == "deferred-coordination"
            other.complete(outcome="verified", summary="release controlled conflict")
        finally:
            other.close()
        assert resumed.invoke("fixture.write/1", [])["status"] == "ok"
        resumed.complete(outcome="verified", summary="bounded lifecycle")
    finally:
        resumed.close()
