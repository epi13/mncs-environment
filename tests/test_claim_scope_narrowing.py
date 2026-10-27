from __future__ import annotations

from mncs_env import claims

REPOSITORY = "mncs-environment"


def _scope(
    kind: str,
    *,
    checkout: str | None = None,
    branch: str | None = None,
    paths: list[str] | None = None,
    checkout_identity: str | None = None,
    git_common_directory_identity: str | None = None,
) -> dict:
    scope = {
        "kind": kind,
        "checkout": checkout,
        "branch": branch,
        "paths": paths,
    }
    if checkout_identity is not None:
        scope["checkout_identity"] = checkout_identity
    if git_common_directory_identity is not None:
        scope["git_common_directory_identity"] = git_common_directory_identity
    return claims.normalize_scope(
        scope,
        REPOSITORY,
    )


def test_worktree_claim_can_be_narrowed_to_repo_relative_paths() -> None:
    scope = _scope(
        "worktree",
        checkout="/workspace/mncs-environment/.worktrees/feature",
        branch="campaign/feature",
        paths=["evidence/z.json", "evidence/a.json", "evidence/z.json"],
    )

    assert scope["paths"] == ["evidence/a.json", "evidence/z.json"]
    assert claims.claim_identity(REPOSITORY, scope) != claims.claim_identity(
        REPOSITORY,
        _scope(
            "worktree",
            checkout="/workspace/mncs-environment/.worktrees/feature",
            branch="campaign/feature",
        ),
    )


def test_narrow_worktree_claim_coexists_with_disjoint_active_branch_claims() -> None:
    active_worktree = _scope(
        "worktree",
        checkout=(
            "/workspace/mncs-environment/.worktrees/"
            "environment-native-accountability"
        ),
        branch="campaign/environment-native-accountability",
    )
    active_paths = _scope(
        "paths",
        paths=["mncs_env/sessions.py", "tests/test_event_sequence_retry.py"],
    )
    campaign_paths = _scope(
        "worktree",
        checkout=(
            "/workspace/mncs-environment/.worktrees/"
            "campaign-charter-observation"
        ),
        branch="campaign/campaign-charter-observation",
        paths=["evidence/campaign-charters/objective.json"],
    )

    assert claims.scopes_conflict(campaign_paths, active_worktree) is None
    assert claims.scopes_conflict(campaign_paths, active_paths) is None


def test_repo_relative_claim_keeps_conservative_cross_checkout_conflict() -> None:
    broad_paths = _scope("paths", paths=["evidence/campaign-charters/objective.json"])
    active_worktree = _scope(
        "worktree",
        checkout="/workspace/mncs-environment/.worktrees/active",
        branch="campaign/active",
    )

    assert claims.scopes_conflict(broad_paths, active_worktree) == (
        "repository-relative paths overlap"
    )


def test_narrow_worktree_paths_conflict_on_same_checkout_or_shared_branch() -> None:
    first = _scope(
        "worktree",
        checkout="/workspace/mncs-environment/.worktrees/first",
        branch="campaign/shared",
        paths=["evidence/campaign-charters/objective.json"],
        checkout_identity="checkout:first",
    )
    same_checkout_overlap = _scope(
        "worktree",
        checkout="/workspace/mncs-environment/.worktrees/first",
        branch="campaign/shared",
        paths=["evidence/campaign-charters/objective.json/child"],
    )
    shared_branch_overlap = _scope(
        "worktree",
        checkout="/workspace/mncs-environment/.worktrees/second",
        branch="campaign/shared",
        paths=["evidence/campaign-charters/objective.json"],
        checkout_identity="checkout:second",
    )
    shared_branch_disjoint = _scope(
        "worktree",
        checkout="/workspace/mncs-environment/.worktrees/second",
        branch="campaign/shared",
        paths=["mncs_env/claims.py"],
    )
    other_branch_overlap = _scope(
        "worktree",
        checkout="/workspace/mncs-environment/.worktrees/second",
        branch="campaign/other",
        paths=["evidence/campaign-charters/objective.json"],
    )

    assert claims.scopes_conflict(first, same_checkout_overlap) == (
        "same checkout with overlapping paths"
    )
    assert claims.scopes_conflict(first, shared_branch_overlap) == (
        "shared branch with overlapping paths across checkouts"
    )
    assert claims.shared_branch_coordination_overlap(
        first, shared_branch_overlap,
    ) == "shared branch with overlapping paths across checkouts"
    assert claims.scopes_conflict(first, shared_branch_disjoint) is None
    assert claims.scopes_conflict(first, other_branch_overlap) is None


def test_same_branch_overlap_is_coordination_only_when_git_refs_are_shared():
    linked_first = _scope(
        "worktree",
        checkout="/workspace/mncs-environment/.worktrees/first",
        branch="campaign/shared",
        paths=["mncs_env/claims.py"],
        checkout_identity="checkout:first",
    )
    linked_second = _scope(
        "worktree",
        checkout="/workspace/mncs-environment/.worktrees/second",
        branch="campaign/shared",
        paths=["mncs_env/claims.py"],
        checkout_identity="checkout:second",
    )
    linked_first["git_common_directory_identity"] = "git-common:shared"
    linked_second["git_common_directory_identity"] = "git-common:shared"
    separate_clone = {
        **linked_second,
        "checkout": "/workspace/mncs-environment-copy/.worktrees/second",
        "checkout_identity": "checkout:separate",
        "git_common_directory_identity": "git-common:separate",
    }

    assert claims.scopes_conflict(linked_first, linked_second) == (
        "shared branch with overlapping paths across checkouts"
    )
    assert claims.shared_branch_coordination_overlap(
        linked_first, linked_second,
    ) == "shared branch with overlapping paths across checkouts"
    assert claims.scopes_conflict(linked_first, separate_clone) is None
    assert claims.shared_branch_coordination_overlap(
        linked_first, separate_clone,
    ) is None

    # Older claims without an observed common-directory identity remain
    # conservative.
    legacy_scope = {key: value for key, value in linked_second.items()
                    if key not in {
                        "git_common_directory_identity", "checkout_identity",
                    }}
    assert claims.scopes_conflict(linked_first, legacy_scope) == (
        "shared branch with overlapping paths across checkouts"
    )
    assert claims.shared_branch_coordination_overlap(
        linked_first, legacy_scope,
    ) is None
