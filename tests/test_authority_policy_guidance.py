from __future__ import annotations

from argparse import Namespace
from types import SimpleNamespace
from unittest.mock import patch

from mncs_env import authority, cli


def _context() -> dict:
    return {
        "denied": [],
        "writable": ["mncs-environment"],
        "invocable": ["*"],
        "protected_repositories": [],
        "claim_holders": {},
        "escalation_required": ["delete"],
        "claim_required_actions": ["delegate", "retire"],
    }


def _scope() -> dict:
    return {
        "kind": "worktree",
        "repository": "mncs-environment",
        "checkout": "/workspace/mncs-environment/.worktrees/main",
        "branch": "main",
        "paths": ["docs/ENTRY.md", "mncs_env/authority.py"],
        "exclusive": False,
    }


def test_scoped_delivery_uses_writable_rights_without_persistent_claim() -> None:
    context = _context()
    scope = _scope()
    repo_facts = {
        "mncs-environment@main": {
            **scope,
            "path": scope["checkout"],
            "repository": "mncs-environment",
        },
    }

    for action in ("merge", "publish"):
        decision = authority.evaluate(
            context,
            action=action,
            target="mncs-environment",
            session_id="ses_campaign",
            claims={},
            repo_facts=repo_facts,
            scope=scope,
        )

        assert decision["verdict"] == "allow"
        assert decision["persistent_claim_required"] is False
        assert decision["authorization_basis"] == (
            "writable-repository-rights"
        )
        assert decision["eligibility_basis"] == (
            "exact-observed-scope-and-repository-native-concurrency"
        )

    for action in ("retire", "write"):
        decision = authority.evaluate(
            context,
            action=action,
            target="mncs-environment",
            session_id="ses_campaign",
            claims={},
            repo_facts=repo_facts,
            scope=scope,
        )
        assert decision["verdict"] == "escalate"
        assert decision["required_authorization"] == "session-claim"
        assert decision["next_step"] == "acquire-exact-claim"

    unscoped = authority.evaluate(
        context,
        action="merge",
        target="mncs-environment",
        session_id="ses_campaign",
        claims={},
        repo_facts=repo_facts,
    )
    assert unscoped["verdict"] == "escalate"
    assert unscoped["required_authorization"] == "operation-scope"


def test_owner_approval_remains_explicit_for_delete_escalation() -> None:
    decision = authority.evaluate(
        _context(),
        action="delete",
        target="mncs-environment",
        session_id="ses_campaign",
    )

    assert decision["verdict"] == "escalate"
    assert decision["required_authorization"] == "owner-approval"
    assert decision["next_step"] == "request-owner-approval"


def test_exact_delivery_claim_allows_merge_and_publish() -> None:
    context = _context()
    scope = _scope()
    claims = {
        "mncs-environment": [{
            "session_id": "ses_campaign",
            "scope": scope,
        }],
    }

    for action in ("merge", "publish"):
        decision = authority.evaluate(
            context,
            action=action,
            target=scope["checkout"],
            session_id="ses_campaign",
            claims=claims,
            repo_facts={
                "mncs-environment@main": {
                    **scope,
                    "path": scope["checkout"],
                    "repository": "mncs-environment",
                },
            },
            scope=scope,
        )

        assert decision["verdict"] == "allow"
        assert "next_step" not in decision


def test_delivery_claim_for_another_checkout_cannot_authorize_absolute_target() -> None:
    scope = _scope()
    other_scope = {**scope, "checkout": "/workspace/mncs-environment/.worktrees/feature"}
    decision = authority.evaluate(
        _context(),
        action="merge",
        target=scope["checkout"],
        session_id="ses_campaign",
        claims={"mncs-environment": [{
            "session_id": "ses_campaign",
            "scope": other_scope,
        }]},
        repo_facts={
            "mncs-environment@main": {
                **scope,
                "path": scope["checkout"],
                "repository": "mncs-environment",
            },
        },
        scope=other_scope,
    )

    assert decision["verdict"] == "deny"
    assert "does not match" in decision["reason"]


def test_authority_cli_rechecks_with_the_exact_owned_claim_scope() -> None:
    session_id = "ses_campaign"
    claim_id = "claim:mncs-environment:worktree:exact"
    scope = _scope()
    calls = {}
    fake_session = SimpleNamespace(
        session_id=session_id,
        inspect=lambda: {"authority": {
            "claim_holders": {"mncs-environment": [{
                "claim_id": claim_id,
                "session_id": session_id,
                "scope": scope,
            }]},
        }},
        check=lambda **kwargs: calls.update(kwargs) or {
            "verdict": "allow", "reason": "merge by scoped claim",
        },
    )
    args = Namespace(
        session=session_id,
        action="merge",
        target=scope["checkout"],
        claim_id=claim_id,
    )

    with patch.object(cli, "_open", return_value=fake_session), patch.object(
        cli, "out",
    ) as output:
        assert cli.cmd_authority(args) == 0

    assert calls == {
        "action": "merge",
        "target": scope["checkout"],
        "scope": scope,
    }
    payload = output.call_args.args[0]
    assert payload["scope_claim_id"] == claim_id
    parsed = cli.build_parser().parse_args([
        "authority", session_id, "--action", "merge",
        "--target", scope["checkout"], "--claim-id", claim_id,
    ])
    assert parsed.claim_id == claim_id
