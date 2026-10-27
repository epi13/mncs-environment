from __future__ import annotations

import json
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
        "mncs-environment": {
            **scope,
            "path": scope["checkout"],
            "repository": "mncs-environment",
            "clean": False,
            "main_branch": True,
            "foreign_signals": ["dirty-tree"],
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
        assert decision["verdict"] == "defer"
        assert decision["required_coordination"] == (
            "scoped-mutation-lease" if action == "retire"
            else "isolated-worktree-or-exact-scope-lease"
        )
        assert "required_authorization" not in decision
        assert decision["persistent_authority_required"] is False

    unscoped = authority.evaluate(
        context,
        action="merge",
        target="mncs-environment",
        session_id="ses_campaign",
        claims={},
        repo_facts=repo_facts,
    )
    assert unscoped["verdict"] == "defer"
    assert unscoped["required_coordination"] == "operation-scope"
    assert "required_authorization" not in unscoped


def test_unassigned_agent_can_propose_only_in_an_observed_non_main_worktree() -> None:
    repository = "mncs-language-service"
    root = "/workspace/mncs-language-service"
    checkout = f"{root}/.worktrees/campaign/proposal"
    scope = {
        "kind": "worktree",
        "repository": repository,
        "checkout": checkout,
        "branch": "campaign/proposal",
        "checkout_identity": "checkout:proposal",
        "git_common_directory_identity": "git-common:language-service",
        "exclusive": False,
    }
    repo_facts = {
        "mncs-language-service@campaign/proposal": {
            **scope,
            "path": checkout,
            "repository_root_path": root,
        },
    }
    context = _context()
    context["writable"] = []

    for action in ("write", "mutate"):
        decision = authority.evaluate(
            context,
            action=action,
            target=checkout,
            session_id="ses_unassigned",
            claims={},
            repo_facts=repo_facts,
            scope=scope,
        )

        assert decision["verdict"] == "allow"
        assert decision["decision_domain"] == "eligibility"
        assert decision["authorization_basis"] == (
            authority.PROPOSAL_ONLY_AUTHORIZATION_BASIS
        )
        assert decision["eligibility_basis"] == (
            authority.PROPOSAL_WORKTREE_ELIGIBILITY
        )
        assert decision["persistent_claim_required"] is False
        assert decision["writable_scope_required_for_integration"] is True
        assert decision["repository_writable_scope_present"] is False
        assert decision["operation_executed"] is False

    # Proposal eligibility does not authorize execution or integration.
    for action in ("execute", "merge", "publish", "retire"):
        decision = authority.evaluate(
            context,
            action=action,
            target=checkout,
            session_id="ses_unassigned",
            claims={},
            repo_facts=repo_facts,
            scope=scope,
        )
        assert decision["verdict"] == "deny"
        assert decision["decision_domain"] == "writable-scope-policy"

    # A protected repository remains denied before proposal eligibility.
    protected_context = {**context, "protected_repositories": [repository]}
    protected = authority.evaluate(
        protected_context,
        action="write",
        target=checkout,
        session_id="ses_unassigned",
        claims={},
        repo_facts=repo_facts,
        scope=scope,
    )
    assert protected["verdict"] == "deny"
    assert protected["decision_domain"] == "protected-policy"

    # A real shared-checkout lease remains a concurrency blocker.
    conflict = authority.evaluate(
        context,
        action="write",
        target=checkout,
        session_id="ses_unassigned",
        claims={repository: [{"session_id": "ses_other", "scope": scope}]},
        repo_facts=repo_facts,
        scope=scope,
    )
    assert conflict["verdict"] == "defer"
    assert conflict["decision_domain"] == "coordination"
    assert conflict["persistent_authority_required"] is False
    assert conflict["operation_executed"] is False
    assert conflict["next_step"] == "select-or-create-an-isolated-worktree"
    assert conflict["claim_conflict"]["decision_basis"][
        "decision_domain"
    ] == "coordination"

    # A main checkout is not a proposal surface, even when observed exactly.
    main_checkout = root
    main_scope = {
        "kind": "worktree",
        "repository": repository,
        "checkout": main_checkout,
        "branch": "main",
        "checkout_identity": "checkout:main",
        "git_common_directory_identity": "git-common:language-service",
        "exclusive": False,
    }
    main_facts = {repository: {
        **main_scope,
        "path": main_checkout,
        "repository_root_path": main_checkout,
    }}
    main_decision = authority.evaluate(
        context,
        action="write",
        target=main_checkout,
        session_id="ses_unassigned",
        claims={},
        repo_facts=main_facts,
        scope=main_scope,
    )
    assert main_decision["verdict"] == "deny"
    assert main_decision["decision_domain"] == "writable-scope-policy"


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


def test_authority_cli_accepts_explicit_unowned_scope_as_an_operation_proposal() -> None:
    session_id = "ses_unassigned"
    scope = _scope()
    calls = {}
    foreign_holder = {
        "session_id": "ses_existing",
        "claim_id": "claim:mncs-environment:worktree:existing",
        "scope": scope,
    }
    repo_facts = {"mncs-environment": {
        **scope,
        "path": scope["checkout"],
        "repository_root_path": "/workspace/mncs-environment",
        "repository": "mncs-environment",
    }}

    def check(**kwargs):
        calls.update(kwargs)
        return authority.evaluate(
            _context(),
            session_id=session_id,
            claims={"mncs-environment": [foreign_holder]},
            repo_facts=repo_facts,
            **kwargs,
        )

    fake_session = SimpleNamespace(
        session_id=session_id,
        inspect=lambda: {"authority": {
            "claim_holders": {"mncs-environment": [foreign_holder]},
        }},
        check=check,
    )
    args = Namespace(
        session=session_id,
        action="merge",
        target="mncs-environment",
        claim_id=None,
        scope_json=json.dumps(scope),
    )

    with patch.object(cli, "_open", return_value=fake_session), patch.object(
        cli, "out",
    ) as output:
        assert cli.cmd_authority(args) == 0

    assert calls == {
        "action": "merge",
        "target": "mncs-environment",
        "scope": scope,
    }
    result = output.call_args.args[0]
    assert "scope_claim_id" not in result
    assert result["evaluation"]["verdict"] == "allow"
    assert result["evaluation"]["persistent_claim_required"] is False
    assert result["evaluation"]["coordination_overlap_count"] == 1
    parsed = cli.build_parser().parse_args([
        "authority", session_id, "--action", "merge",
        "--target", "mncs-environment", "--scope-json", json.dumps(scope),
    ])
    assert parsed.scope_json == json.dumps(scope)
    assert parsed.claim_id is None


def test_authority_cli_marks_explicit_worktree_write_as_non_executing_preflight() -> None:
    session_id = "ses_unassigned"
    scope = _scope()
    calls = {}

    def check(**kwargs):
        calls.update(kwargs)
        return {
            "verdict": "allow",
            "decision_domain": "eligibility",
            "eligibility_basis": authority.PROPOSAL_WORKTREE_ELIGIBILITY,
            "persistent_claim_required": False,
            "operation_executed": False,
        }

    fake_session = SimpleNamespace(
        session_id=session_id,
        inspect=lambda: {"authority": {"claim_holders": {}}},
        check=check,
    )
    args = Namespace(
        session=session_id,
        action="write",
        target="mncs-environment",
        claim_id=None,
        scope_json=json.dumps(scope),
    )

    with patch.object(cli, "_open", return_value=fake_session), patch.object(
        cli, "out",
    ) as output:
        assert cli.cmd_authority(args) == 0

    assert calls == {
        "action": "write",
        "target": "mncs-environment",
        "scope": scope,
        "proposal_preflight": True,
    }
    result = output.call_args.args[0]
    assert result["evaluation"]["verdict"] == "allow"
    assert result["evaluation"]["persistent_claim_required"] is False
    assert result["evaluation"]["operation_executed"] is False


def test_authority_cli_rejects_malformed_explicit_scope_before_opening_session() -> None:
    args = Namespace(
        session="ses_unassigned",
        action="merge",
        target="mncs-environment",
        claim_id=None,
        scope_json="[]",
    )
    with patch.object(cli, "_open") as open_session, patch.object(
        cli, "fail", return_value=2,
    ) as failure:
        assert cli.cmd_authority(args) == 2

    open_session.assert_not_called()
    failure.assert_called_once()
    assert "JSON object" in failure.call_args.args[0]
