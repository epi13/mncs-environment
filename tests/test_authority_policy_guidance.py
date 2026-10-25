from __future__ import annotations

from mncs_env import authority


def _context() -> dict:
    return {
        "denied": [],
        "writable": ["mncs-environment"],
        "invocable": ["*"],
        "protected_repositories": [],
        "claim_holders": {},
        "escalation_required": ["delete"],
        "claim_required_actions": ["merge", "publish", "retire"],
    }


def _scope() -> dict:
    return {
        "kind": "worktree",
        "repository": "mncs-environment",
        "checkout": "/workspace/mncs-environment/.worktrees/main",
        "branch": "main",
        "paths": None,
        "exclusive": False,
    }


def test_delivery_claim_requirement_directs_to_claim_without_owner_reapproval() -> None:
    context = _context()
    scope = _scope()
    repo_facts = {
        "mncs-environment@main": {
            **scope,
            "path": scope["checkout"],
            "repository": "mncs-environment",
        },
    }

    for action in ("merge", "publish", "retire", "write"):
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
        assert "approval" not in decision["next_step"]


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
            target="mncs-environment",
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
