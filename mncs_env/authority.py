"""Authority projection: explicit, machine-readable, default-deny.

AuthorityContext evaluation is PURE: given an intent, workspace facts,
active coordination leases, and a requested action, it returns allow / deny /
defer / escalate with a reason. A coordination defer is not an authority
request; it means the operation needs a safer or more exact scope. Session
code enforces the verdict; nothing here spawns processes or mutates state.
Absence of a grant never becomes shared-state or integration permission. A
freshly observed isolated proposal worktree may still be eligible for local
write or mutation without repository-wide write scope.
"""

from __future__ import annotations

import os
import re
from typing import Any

from . import claims as claims_module

SCHEMA = "mncs.environment.authority-context/1"

# Actions a session can request. Kept separate from capability effects so
# the projection can require more than the provider declares.
ACTIONS = (
    "read",
    "write",
    "execute",
    "mutate",
    "publish",
    "merge",
    "delete",
    "retire",
    "invoke",
    "subscribe",
    "delegate",
    "handoff",
    "verify",
)

# Capability effects mapped to the minimum session action required.
# `delegate` (ask an external system to perform bounded work, e.g. CI)
# and `subscribe` (observe completion/status) are semantic effects, not
# local execution: delegate escalates unless the session owns the target
# repository scope, while subscribe is read-class workspace-wide.
EFFECT_ACTIONS = {
    "read": "read",
    "verify": "verify",
    "write": "write",
    "execute": "execute",
    "publish": "publish",
    "delegate": "delegate",
    "subscribe": "subscribe",
}


MUTATING_ACTIONS = ("write", "mutate", "execute", "retire")
PROPOSAL_WORKTREE_ACTIONS = frozenset({"write", "mutate", "execute", "verify"})
PROPOSAL_WORKTREE_ELIGIBILITY = "registered-non-main-worktree-no-persistent-claim"
PROPOSAL_ONLY_AUTHORIZATION_BASIS = "isolated-proposal-scope"

# These are invariant user-approval gates. Claims remain coordination and
# recovery evidence; they are not a persistent privilege required to integrate
# an explicitly scoped change into a writable repository.
ESCALATION_ACTIONS = ("delete",)
CLAIM_REQUIRED_ACTIONS = ("delegate", "retire")
CLAIM_CONFLICT_SCHEMA = "mncs.environment.authority-claim-conflict/1"
_EVIDENCE_IDENTIFIER = re.compile(r"[A-Za-z0-9:._-]{1,160}")
MAX_INTEGRATION_COORDINATION_OVERLAPS = 8


def build_context(
    *,
    subject: str,
    intent: dict[str, Any],
    protected_repos: list[str],
    claim_holders: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Project effective authority for a consumer from intent + workspace facts."""
    forbidden = set(intent.get("forbidden_actions", []))
    context = {
        "schema_version": SCHEMA,
        "subject": subject,
        "readable": ["workspace"],
        "writable": [repo for repo in intent.get("repositories", [])],
        "invocable": ["*"],
        "protected_repositories": sorted(set(protected_repos)),
        "claim_holders": dict(claim_holders or {}),
        "escalation_required": sorted(
            action for action in ESCALATION_ACTIONS if action not in forbidden
        ),
        "claim_required_actions": sorted(
            action for action in CLAIM_REQUIRED_ACTIONS if action not in forbidden
        ),
        "denied": sorted(forbidden),
        "grants": list(intent.get("authority_requirements", [])),
    }
    return context


def _holder_list(holders: dict[str, Any], repo: str) -> list[dict[str, Any]]:
    """Normalize repo holders to [{session_id, scope}] (accepts legacy strings)."""
    raw = (holders or {}).get(repo)
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else [raw]
    normalized = []
    for item in items:
        if isinstance(item, str):
            normalized.append({"session_id": item, "scope": {"kind": "repository",
                                                             "repository": repo}})
        elif isinstance(item, dict):
            scope = dict(item.get("scope") or {})
            scope.setdefault("kind", item.get("scope_kind", "repository"))
            scope.setdefault("repository", repo)
            holder = {
                "session_id": str(item.get("session_id", "")),
                "scope": scope,
            }
            claim_id = item.get("claim_id")
            if isinstance(claim_id, str) and claim_id:
                holder["claim_id"] = claim_id
            normalized.append(holder)
    return normalized


def _scope_evidence(scope: dict[str, Any], repository: str) -> dict[str, Any]:
    """Return bounded, repository-relative scope facts for denial provenance."""
    kind = scope.get("kind")
    if kind not in {"repository", "worktree", "paths"}:
        kind = "repository"
    result: dict[str, Any] = {
        "kind": kind,
        "repository": repository,
        "exclusive": bool(scope.get("exclusive", kind == "repository")),
    }
    branch = scope.get("branch")
    if (isinstance(branch, str) and branch and len(branch) <= 160
            and "\x00" not in branch and not branch.startswith("/")):
        result["branch"] = branch
    # Checkouts are intentionally omitted: they are absolute, machine-local
    # paths. Stable checkout identities can distinguish linked worktrees
    # without persisting those paths into session evidence.
    for key in (
        "checkout_identity", "git_common_directory_identity", "manifest_identity",
    ):
        value = scope.get(key)
        if isinstance(value, str) and _EVIDENCE_IDENTIFIER.fullmatch(value):
            result[key] = value
    paths = scope.get("paths")
    if isinstance(paths, list):
        safe_paths = [
            path for path in paths
            if isinstance(path, str) and path and len(path) <= 240
            and "\x00" not in path and "\\" not in path and not path.startswith("/")
            and not any(part in {"", ".", ".."} for part in path.split("/"))
        ]
        result["paths"] = safe_paths[:8]
        if len(safe_paths) > 8:
            result["paths_omitted"] = len(safe_paths) - 8
    return result


def repository_facts_for_snapshot(
    snapshot: dict[str, Any],
    repo_facts: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Enrich checkout facts with stable repository identity from workspace records."""
    raw = repo_facts if isinstance(repo_facts, dict) else snapshot.get("repo_facts", {})
    facts = ({
        key: dict(value)
        for key, value in raw.items()
        if isinstance(key, str) and isinstance(value, dict)
    } if isinstance(raw, dict) else {})
    workspace = snapshot.get("workspace")
    rows = workspace.get("repositories", []) if isinstance(workspace, dict) else []
    if isinstance(rows, list):
        for row in rows:
            if not isinstance(row, dict):
                continue
            alias = row.get("name")
            if not isinstance(alias, str) or not alias:
                continue
            current = dict(facts.get(alias, {}))
            repository = (
                row.get("manifest_repository")
                or row.get("repository")
                or row.get("worktree_of")
                or alias
            )
            current["repository"] = str(repository)
            for key in (
                "path", "branch", "head", "manifest_identity",
                "checkout_identity", "git_common_directory_identity",
            ):
                if isinstance(row.get(key), str):
                    current[key] = row[key]
            facts[alias] = current
    selected = snapshot.get("selected_checkouts")
    if isinstance(selected, dict):
        for alias, checkout in selected.items():
            if not isinstance(alias, str) or not isinstance(checkout, dict):
                continue
            current = dict(facts.get(alias, {}))
            current.update({
                key: value for key, value in checkout.items()
                if isinstance(key, str) and value is not None
            })
            current["repository"] = str(checkout.get("repository") or alias)
            facts[alias] = current
    workspace_roots: dict[str, str] = {}
    if isinstance(rows, list):
        for row in rows:
            if (not isinstance(row, dict)
                    or row.get("worktree_of") is not None
                    or not isinstance(row.get("name"), str)
                    or not isinstance(row.get("path"), str)):
                continue
            repository = row.get("manifest_repository") or row.get("repository")
            if isinstance(repository, str) and repository:
                workspace_roots.setdefault(repository, row["path"])
    for current in facts.values():
        repository = current.get("repository") or current.get("manifest_repository")
        root_path = workspace_roots.get(str(repository))
        if isinstance(root_path, str):
            current.setdefault("repository_root_path", root_path)
    return facts


def checkout_facts_for_target(
    target: str, repo_facts: dict[str, dict[str, Any]] | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """Return the most specific observed checkout containing an absolute target."""
    if not isinstance(target, str) or not os.path.isabs(target):
        return None
    normalized_target = os.path.normpath(target)
    candidates: list[tuple[int, str, str, dict[str, Any]]] = []
    for alias, facts in (repo_facts or {}).items():
        if not isinstance(alias, str) or not alias or not isinstance(facts, dict):
            continue
        checkout = facts.get("path")
        if not isinstance(checkout, str) or not os.path.isabs(checkout):
            continue
        normalized_checkout = os.path.normpath(checkout)
        try:
            common = os.path.commonpath((normalized_target, normalized_checkout))
        except ValueError:
            continue
        if common != normalized_checkout:
            continue
        repository = facts.get("repository") or facts.get("manifest_repository") or alias
        if not isinstance(repository, str) or not repository:
            continue
        candidates.append((len(normalized_checkout), repository,
                           normalized_checkout, facts))
    if not candidates:
        return None
    longest = max(length for length, _, _, _ in candidates)
    matches = [item for item in candidates if item[0] == longest]
    identities = {
        (repository, checkout, facts.get("branch"))
        for _, repository, checkout, facts in matches
    }
    if len(identities) != 1:
        return None
    for key in (
        "checkout_identity", "git_common_directory_identity", "manifest_identity",
    ):
        identity_values = {
            facts[key] for _, _, _, facts in matches
            if isinstance(facts.get(key), str)
        }
        if len(identity_values) > 1:
            return None
    _, repository, _, facts = max(
        matches,
        key=lambda item: sum(
            isinstance(item[3].get(key), str)
            for key in (
                "checkout_identity", "git_common_directory_identity",
                "manifest_identity",
            )
        ),
    )
    return repository, facts


def repository_for_target(
    target: str, repo_facts: dict[str, dict[str, Any]] | None = None,
) -> str | None:
    """Resolve a symbolic or observed absolute target to stable repo identity.

    Absolute paths are attributed only through observed checkout paths. This
    mapping supplies context, never authority, and does not follow symlinks.
    """
    if not isinstance(target, str) or not target:
        return None
    if not os.path.isabs(target):
        return target.split("/", 1)[0]
    match = checkout_facts_for_target(target, repo_facts)
    return match[0] if match is not None else None


def _requested_scope(repo: str, target: str,
                     scope: dict[str, Any] | None,
                     repo_facts: dict[str, dict[str, Any]] | None = None,
                     ) -> dict[str, Any]:
    if scope:
        merged = {"kind": "paths" if scope.get("paths") else "repository",
                  "repository": repo}
        merged.update(scope)
        # Operation callers may provide only an exact registered checkout and
        # branch. Attach the common-directory identity from the fresh
        # Environment observation so branch-name collisions across separate
        # clones do not become false coordination conflicts. Existing caller
        # values remain subject to the normal observation match below.
        checkout = merged.get("checkout")
        if merged.get("kind") == "worktree" and isinstance(checkout, str):
            observed = checkout_facts_for_target(checkout, repo_facts)
            if (observed is not None and observed[0] == repo
                    and os.path.normpath(str(observed[1].get("path", "")))
                    == os.path.normpath(checkout)):
                for key in (
                    "git_common_directory_identity", "checkout_identity",
                    "manifest_identity",
                ):
                    if (key not in merged
                            and isinstance(observed[1].get(key), str)):
                        merged[key] = observed[1][key]
        return claims_module.normalize_scope(merged, repo)
    if os.path.isabs(target):
        match = checkout_facts_for_target(target, repo_facts)
        facts = match[1] if match is not None and match[0] == repo else {}
        checkout = facts.get("path")
        if isinstance(checkout, str) and os.path.isabs(checkout):
            return claims_module.normalize_scope({
                "kind": "worktree",
                "checkout": os.path.normpath(checkout),
                "branch": facts.get("branch"),
                **{
                    key: facts[key]
                    for key in (
                        "git_common_directory_identity", "checkout_identity",
                        "manifest_identity",
                    )
                    if isinstance(facts.get(key), str) and facts[key]
                },
            }, repo)
        return claims_module.normalize_scope(None, repo)
    rest = target[len(repo):].lstrip("/") if target.startswith(repo) else ""
    if rest:
        return claims_module.normalize_scope(
            {"kind": "paths", "paths": [rest]}, repo)
    return claims_module.normalize_scope(None, repo)


def _worktree_scope_matches_observation(
    scope: dict[str, Any], repo: str,
    repo_facts: dict[str, dict[str, Any]] | None,
) -> bool:
    """Require an operation worktree scope to match an observed checkout."""
    checkout = scope.get("checkout")
    if not isinstance(checkout, str) or not os.path.isabs(checkout):
        return False
    match = checkout_facts_for_target(checkout, repo_facts)
    if match is None or match[0] != repo:
        return False
    observed = match[1]
    if os.path.normpath(str(observed.get("path", ""))) != os.path.normpath(checkout):
        return False
    for key in (
        "branch", "checkout_identity", "git_common_directory_identity",
        "manifest_identity",
    ):
        declared = scope.get(key)
        actual = observed.get(key)
        if isinstance(declared, str) and declared and actual != declared:
            return False
    return True


def _is_proposal_worktree(
    scope: dict[str, Any] | None,
    repo: str,
    repo_facts: dict[str, dict[str, Any]] | None,
) -> bool:
    """Whether an operation names a currently observed, non-main checkout.

    An exact worktree is an isolated proposal surface. Its fresh checkout
    observation can make local write or verification proposals eligible
    without repository-wide write scope or a persistent claim. Execution and
    integration remain subject to their own authority checks. Checkout-bound
    claims for the same checkout or attached branch are still checked
    separately as concurrency evidence.
    """
    if not isinstance(scope, dict) or scope.get("kind") != "worktree":
        return False
    branch = scope.get("branch")
    if not isinstance(branch, str) or not branch or branch in {"main", "master"}:
        return False
    if not _worktree_scope_matches_observation(scope, repo, repo_facts):
        return False
    observed = checkout_facts_for_target(
        str(scope.get("checkout", "")), repo_facts,
    )
    if observed is None or observed[0] != repo:
        return False
    observed_branch = observed[1].get("branch")
    root_checkout = observed[1].get("repository_root_path")
    return (
        isinstance(observed_branch, str)
        and bool(observed_branch)
        and observed_branch not in {"main", "master"}
        and isinstance(root_checkout, str)
        and os.path.isabs(root_checkout)
        and os.path.normpath(root_checkout)
        != os.path.normpath(str(scope.get("checkout", "")))
    )


def evaluate(
    context: dict[str, Any],
    *,
    action: str,
    target: str,
    session_id: str,
    claims: dict[str, Any] | None = None,
    repo_facts: dict[str, dict[str, Any]] | None = None,
    scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return allow, deny, defer, or escalate for one requested action.

    Writable intent and explicit operation scope govern shared-state mutations.
    ``merge`` and ``publish`` require a writable repository and an exact scope
    observed or supplied for this operation. Missing observation or scope is
    a coordination defer, not an authority escalation. The current claim view is checked
    for overlapping foreign work; claims describe coordination but never grant
    repository write eligibility or integration eligibility. Repository
    operations must still use their native concurrency checks (for example,
    fast-forward-only merges and non-forcing reference updates). ``write``,
    ``execute`` and ``retire`` require writable scope. A fresh exact observation
    of an attached non-main worktree makes local ``write`` and ``mutate``
    eligible as proposed state even when the session has no repository-wide
    write scope; it does not grant merge, publish, or general repository
    authority. Repository-wide or repository-relative path claims remain
    visible as coordination context rather than reserving every proposal
    branch. Claims on distinct worktrees sharing an attached branch are
    coordination evidence for local writes, mutations, and verification;
    operations that may update shared refs or retire a checkout retain the
    shared-ref constraint. Protected, forbidden,
    and fenced scope denies. For merge and publish, overlap remains
    coordination evidence while the repository-native update resolves actual
    conflicts. Escalate and deny never authorize execution.
    """
    if action not in ACTIONS:
        return {"verdict": "deny", "reason": f"unknown action {action!r}"}
    if action in context.get("denied", []):
        return {"verdict": "deny", "reason": f"action {action} is forbidden by intent"}
    repo = repository_for_target(target, repo_facts)
    if repo is None:
        if os.path.isabs(target) and (
            action in MUTATING_ACTIONS
            or action in ("publish", "merge", "delete", "verify")
        ):
            return {
                "verdict": "deny",
                "reason": (
                    "absolute target is outside every observed repository checkout"
                ),
            }
        repo = "" if os.path.isabs(target) else target
    target_checkout = checkout_facts_for_target(target, repo_facts)
    if (action in ("merge", "publish") and os.path.isabs(target)
            and target_checkout is not None
            and os.path.normpath(target)
            != os.path.normpath(str(target_checkout[1].get("path", "")))):
        return {
            "verdict": "deny",
            "reason": "absolute integration target must identify an observed checkout root",
        }
    if (action in MUTATING_ACTIONS or action in ("merge", "publish")) \
            and os.path.isabs(target) and scope is not None:
        scope_kind = scope.get("kind", "repository")
        if scope_kind not in {"repository", "worktree"}:
            return {
                "verdict": "deny",
                "reason": "absolute mutation or delivery target requires repository or worktree scope",
            }
        if scope_kind == "worktree":
            target_path = (
                target_checkout[1].get("path")
                if target_checkout is not None else None
            )
            if (not isinstance(target_path, str)
                    or os.path.normpath(str(scope.get("checkout", "")))
                    != os.path.normpath(target_path)):
                return {
                    "verdict": "deny",
                    "reason": "worktree scope does not match the observed absolute target",
                }
    if action in MUTATING_ACTIONS or action in ("publish", "merge", "verify"):
        fence = next((item for item in context.get("claim_fences", [])
                      if isinstance(item, dict)
                      and item.get("repository") == repo), None)
        if fence is not None:
            return {
                "verdict": "deny",
                "reason": (
                    f"{repo} authority was fenced after claim generation "
                    f"{fence.get('fencing_generation')} superseded this session"
                ),
            }
    if repo in context.get("protected_repositories", []) and action in (
        "write",
        "mutate",
        "merge",
        "delete",
        "retire",
        "execute",
        "publish",
        "verify",
    ):
        return {
            "verdict": "deny",
            "decision_domain": "protected-policy",
            "reason": f"{repo} is protected scope for this session",
        }
    holders = _holder_list(
        claims if claims is not None else context.get("claim_holders", {}), repo)
    others = [h for h in holders if h["session_id"] != session_id]
    own = [h for h in holders if h["session_id"] == session_id]
    mutating = action in (
        "write", "mutate", "merge", "delete", "execute", "retire", "publish"
    )
    # Verification executes provider code inside a checkout, so an actual
    # scope overlap defers the operation until it can use an isolated or
    # non-overlapping scope. Unlike mutation, dirt and branch are the
    # *subject* under verification, not a bar.
    conflict_checked = mutating or action == "verify"
    requested = (
        _requested_scope(repo, target, scope, repo_facts)
        if conflict_checked else None
    )
    proposal_worktree = (
        action in PROPOSAL_WORKTREE_ACTIONS
        and _is_proposal_worktree(requested, repo, repo_facts)
    )
    writable_scope_present = repo in context.get("writable", [])
    proposal_only_write = (
        action in {"write", "mutate"}
        and proposal_worktree
        and not writable_scope_present
    )
    if ((action in MUTATING_ACTIONS or action in ("publish", "merge"))
            and not writable_scope_present and not proposal_only_write):
        return {
            "verdict": "deny",
            "decision_domain": "writable-scope-policy",
            "reason": f"{repo} is outside this session's writable authority",
        }
    nonblocking_coordination_claims: list[dict[str, Any]] = []
    nonblocking_coordination_claim_count = 0
    integration_coordination_overlaps: list[dict[str, Any]] = []
    integration_coordination_overlap_count = 0
    for other in others:
        if not conflict_checked:
            continue
        other_scope = other.get("scope") or {"kind": "repository"}
        # Repository-wide and repository-relative path claims cannot identify
        # which checkout their holder is changing. They remain useful
        # coordination evidence, but cannot reserve every isolated proposal
        # worktree. A `worktree` claim carries stable checkout identity; a
        # shared attached branch remains coordination evidence for local
        # proposal effects and a constraint for ref-changing operations.
        if proposal_worktree and other_scope.get("kind") in {"repository", "paths"}:
            nonblocking_coordination_claim_count += 1
            if len(nonblocking_coordination_claims) < MAX_INTEGRATION_COORDINATION_OVERLAPS:
                observation: dict[str, Any] = {
                    "session_id": other["session_id"],
                    "scope": _scope_evidence(other_scope, repo),
                    "disposition": (
                        "coordination-only-broad-claim"
                        if other_scope.get("kind") == "repository"
                        else "coordination-only-repository-path-claim"
                    ),
                }
                claim_id = other.get("claim_id")
                if isinstance(claim_id, str) and claim_id:
                    observation["claim_id"] = claim_id
                nonblocking_coordination_claims.append(observation)
            continue
        shared_branch_overlap = (
            claims_module.shared_branch_coordination_overlap(
                requested or {}, other_scope,
            )
        )
        if shared_branch_overlap and action in ("merge", "publish"):
            integration_coordination_overlap_count += 1
            if (len(integration_coordination_overlaps)
                    < MAX_INTEGRATION_COORDINATION_OVERLAPS):
                observation = {
                    "session_id": other["session_id"],
                    "scope": _scope_evidence(other_scope, repo),
                    "overlap_reason": shared_branch_overlap,
                }
                claim_id = other.get("claim_id")
                if isinstance(claim_id, str) and claim_id:
                    observation["claim_id"] = claim_id
                integration_coordination_overlaps.append(observation)
            continue
        if (shared_branch_overlap and proposal_worktree
                and action in {"write", "mutate", "verify"}):
            nonblocking_coordination_claim_count += 1
            if (len(nonblocking_coordination_claims)
                    < MAX_INTEGRATION_COORDINATION_OVERLAPS):
                observation = {
                    "session_id": other["session_id"],
                    "scope": _scope_evidence(other_scope, repo),
                    "disposition": "coordination-only-shared-branch-proposal",
                    "overlap_reason": shared_branch_overlap,
                }
                claim_id = other.get("claim_id")
                if isinstance(claim_id, str) and claim_id:
                    observation["claim_id"] = claim_id
                nonblocking_coordination_claims.append(observation)
            continue
        conflict = shared_branch_overlap if (
            shared_branch_overlap and action in {
                "write", "mutate", "execute", "verify", "retire",
            }
        ) else claims_module.scopes_conflict(requested or {}, other_scope)
        if conflict is not None:
            blocking_claim: dict[str, Any] = {
                "session_id": other["session_id"],
                "scope": _scope_evidence(other.get("scope") or {}, repo),
            }
            claim_id = other.get("claim_id")
            if isinstance(claim_id, str) and claim_id:
                blocking_claim["claim_id"] = claim_id
            if action in ("merge", "publish"):
                integration_coordination_overlap_count += 1
                if (len(integration_coordination_overlaps)
                        < MAX_INTEGRATION_COORDINATION_OVERLAPS):
                    integration_coordination_overlaps.append({
                        **blocking_claim,
                        "overlap_reason": conflict,
                    })
                continue
            next_step = {
                "same checkout with overlapping paths": (
                    "select-or-create-an-isolated-worktree"
                ),
                "shared branch with overlapping paths across checkouts": (
                    "refresh-against-current-branch-or-select-a-distinct-branch"
                ),
            }.get(conflict, "select-an-exact-non-overlapping-operation-scope")
            return {
                "verdict": "defer",
                "decision_domain": "coordination",
                "required_coordination": (
                    "non-overlapping-operation-scope-or-isolated-worktree"
                ),
                "coordination_status": "overlapping-scope-conflict",
                "persistent_authority_required": False,
                "operation_executed": False,
                "next_step": next_step,
                "reason": (f"{target} overlaps scope claimed by another session "
                           f"({other['session_id']}): {conflict}"),
                "claim_conflict": {
                    "schema_version": CLAIM_CONFLICT_SCHEMA,
                    "repository": repo,
                    "conflict_reason": conflict,
                    "requested_scope": _scope_evidence(requested or {}, repo),
                    "blocking_claim": blocking_claim,
                    "decision_basis": {
                        "decision_domain": "coordination",
                        "authorization_basis": "session-writable-scope",
                        "authorization_status": "eligible",
                        "eligibility_status": "blocked-by-overlapping-scope",
                        "coordination_status": "scoped-mutation-lease-conflict",
                        "persistent_authority_required": False,
                    },
                },
            }
    if action in ("publish", "merge"):
        if repo not in context.get("writable", []):
            return {"verdict": "deny",
                    "reason": f"{repo} is outside this session's writable authority"}
        requested_kind = (requested or {}).get("kind")
        if requested_kind == "worktree":
            operation_scope_explicit = (
                _worktree_scope_matches_observation(
                    requested or {}, repo, repo_facts,
                )
            )
        elif os.path.isabs(target):
            operation_scope_explicit = target_checkout is not None
        elif target != repo:
            operation_scope_explicit = target.startswith(f"{repo}/")
        else:
            operation_scope_explicit = (
                scope is not None
                and requested_kind in {"repository", "paths"}
            )
        if not operation_scope_explicit:
            return {
                "verdict": "defer",
                "decision_domain": "coordination",
                "required_coordination": "operation-scope",
                "coordination_status": "exact-operation-scope-required",
                "persistent_authority_required": False,
                "next_step": "select-an-observed-checkout-or-explicit-scope",
                "reason": (
                    f"{action} requires an exact observed or explicit destination "
                    "scope; a persistent claim is optional"
                ),
            }
        decision = {
            "verdict": "allow",
            "reason": (
                f"{action} eligible within writable scope on {repo}; "
                "claim overlaps are coordination evidence"
            ),
            "authorization_basis": "writable-repository-rights",
            "eligibility_basis": (
                "exact-observed-scope-and-repository-native-concurrency"
            ),
            "persistent_claim_required": False,
            "concurrency_note": (
                "This decision does not reserve the scope; the integration "
                "operation must enforce its repository-native reference update"
            ),
        }
        if integration_coordination_overlap_count:
            decision.update({
                "coordination_overlap_count": (
                    integration_coordination_overlap_count
                ),
                "coordination_overlaps": integration_coordination_overlaps,
                "coordination_overlaps_truncated": (
                    integration_coordination_overlap_count
                    > len(integration_coordination_overlaps)
                ),
            })
        return decision
    if action == "delegate":
        # Asking an external system to verify a repository scope the
        # session owns is explicit intent, like publish. Anything else
        # defers; remote work never dispatches ambiently by default.
        if any((h.get("scope") or {}).get("kind") == "repository"
               and (h.get("scope") or {}).get("repository", repo) == repo
               for h in own):
            return {"verdict": "allow",
                    "reason": f"delegate by repository claim on {repo}"}
        return {
            "verdict": "defer",
            "decision_domain": "coordination",
            "required_coordination": "repository-scope-lease",
            "coordination_status": "repository-scope-not-established",
            "persistent_authority_required": False,
            "next_step": "select-an-observed-repository-scope",
            "reason": (
                f"delegate requires an observed repository scope on {repo}; "
                "this coordination requirement does not request owner approval"
            ),
        }
    if action == "retire":
        # Local Git retirement is narrower than general deletion: the
        # retirement adapter must first prove that the exact branch or
        # checkout is safe to remove. It still requires a current claim
        # covering that scope and declared write intent for the repository.
        if repo not in context.get("writable", []):
            return {"verdict": "deny",
                    "reason": f"{repo} is outside this session's writable authority"}
        owned = any(_covers(h.get("scope") or {}, requested or {}) for h in own)
        if owned:
            return {"verdict": "allow",
                    "decision_domain": "eligibility",
                    "authorization_basis": "session-writable-scope",
                    "coordination_status": "scoped-mutation-lease-held",
                    "persistent_authority_required": False,
                    "reason": f"{target} retirement by scoped claim"}
        return {"verdict": "defer",
                "required_coordination": "scoped-mutation-lease",
                "decision_domain": "coordination",
                "authorization_basis": "session-writable-scope",
                "coordination_status": "exact-scope-lease-required",
                "persistent_authority_required": False,
                "next_step": "acquire-exact-scope-lease",
                "reason": f"{target} retirement requires a current claim covering the requested scope"}
    if action in MUTATING_ACTIONS:
        facts = (repo_facts or {}).get(repo, {})
        owned = any(
            _covers(h.get("scope") or {}, requested or {}) for h in own
        )
        if proposal_worktree:
            return {
                "verdict": "allow",
                "decision_domain": "eligibility",
                "authorization_basis": (
                    "session-writable-scope" if writable_scope_present
                    else PROPOSAL_ONLY_AUTHORIZATION_BASIS
                ),
                "eligibility_basis": PROPOSAL_WORKTREE_ELIGIBILITY,
                "coordination_status": "isolated-proposal-worktree",
                "persistent_claim_required": False,
                "writable_scope_required_for_integration": True,
                "repository_writable_scope_present": writable_scope_present,
                "operation_executed": False,
                "coordination_only_claim_count": (
                    nonblocking_coordination_claim_count
                ),
                "coordination_only_claims": nonblocking_coordination_claims,
                "coordination_only_claims_truncated": (
                    nonblocking_coordination_claim_count
                    > len(nonblocking_coordination_claims)
                ),
                "reason": (
                    f"{target} is eligible in its observed non-main worktree; "
                    "claim overlaps remain coordination evidence"
                ),
            }
        pristine = bool(
            facts.get("clean")
            and facts.get("main_branch")
            and not facts.get("foreign_signals")
            and target in context.get("writable", [])
        )
        if not owned and not pristine:
            if not facts:
                return {"verdict": "defer",
                        "required_coordination": "fresh-workspace-observation",
                        "decision_domain": "coordination",
                        "authorization_basis": "session-writable-scope",
                        "coordination_status": "workspace-scope-not-observed",
                        "persistent_authority_required": False,
                        "next_step": "refresh-workspace-or-select-an-observed-proposal-worktree",
                        "reason": f"{repo} has no fresh workspace observation"}
            return {"verdict": "defer",
                    "required_coordination": "isolated-worktree-or-exact-scope-lease",
                    "decision_domain": "coordination",
                    "authorization_basis": "session-writable-scope",
                    "coordination_status": "shared-checkout-needs-isolation-or-lease",
                    "persistent_authority_required": False,
                    "next_step": "select-or-create-an-observed-proposal-worktree",
                    "reason": f"{repo} is not an isolated proposal worktree (dirty/foreign/off-branch)"}
        return {"verdict": "allow",
                "decision_domain": "eligibility",
                "authorization_basis": "session-writable-scope",
                "coordination_status": (
                    "scoped-mutation-lease-held" if owned
                    else "clean-main-no-lease-needed"
                ),
                "persistent_authority_required": False,
                "reason": f"{target} mutation by claim" if owned else f"{target} is owned clean scope"}
    if action == "verify":
        if proposal_worktree:
            return {
                "verdict": "allow",
                "decision_domain": "eligibility",
                "authorization_basis": (
                    "session-writable-scope" if writable_scope_present
                    else PROPOSAL_ONLY_AUTHORIZATION_BASIS
                ),
                "eligibility_basis": PROPOSAL_WORKTREE_ELIGIBILITY,
                "coordination_status": "isolated-proposal-worktree",
                "persistent_claim_required": False,
                "writable_scope_required_for_integration": True,
                "repository_writable_scope_present": writable_scope_present,
                "operation_executed": False,
                "coordination_only_claim_count": (
                    nonblocking_coordination_claim_count
                ),
                "coordination_only_claims": nonblocking_coordination_claims,
                "coordination_only_claims_truncated": (
                    nonblocking_coordination_claim_count
                    > len(nonblocking_coordination_claims)
                ),
                "reason": (
                    f"{target} verification is eligible in its observed "
                    "non-main worktree"
                ),
            }
        return {"verdict": "allow",
                "reason": f"{target} verification permitted; "
                          "repo writes must stay within declared ephemeral roots"}
    if action in context.get("escalation_required", []):
        return {
            "verdict": "escalate",
            "required_authorization": "owner-approval",
            "next_step": "request-owner-approval",
            "reason": f"{action} requires escalation",
        }
    if action == "invoke":
        return {"verdict": "allow", "reason": "invocation permitted; effects checked per call"}
    if action in ("read", "subscribe"):
        return {"verdict": "allow", "reason": "read/subscribe permitted workspace-wide"}
    if action == "handoff":
        return {"verdict": "allow", "reason": "handoff permitted; receiving consumer revalidates"}
    return {"verdict": "escalate", "reason": f"no grant covers {action} on {target}"}


def _covers(held: dict[str, Any], requested: dict[str, Any]) -> bool:
    """True when a held scope contains the requested scope."""
    if held.get("repository") != requested.get("repository"):
        return False
    if held.get("kind") == "repository":
        return True
    if (held.get("checkout") or None) != (requested.get("checkout") or None):
        return False
    held_branch = held.get("branch") or None
    wanted_branch = requested.get("branch") or None
    if held_branch and wanted_branch and held_branch != wanted_branch:
        return False
    for key in (
        "git_common_directory_identity", "checkout_identity", "manifest_identity",
    ):
        wanted_identity = requested.get(key)
        if wanted_identity is not None and held.get(key) != wanted_identity:
            return False
    held_paths = held.get("paths")
    if held_paths is None:
        return True
    wanted = requested.get("paths")
    if not wanted:
        return False
    return all(
        any(w == h or w.startswith(h + "/") for h in held_paths) for w in wanted
    )


def required_action_for_effects(effects: list[str]) -> str:
    """Strongest session action implied by capability effects."""
    order = ["read", "subscribe", "verify", "execute", "write",
             "delegate", "publish"]
    strongest = "read"
    for effect in effects:
        action = EFFECT_ACTIONS.get(effect, "execute")
        if order.index(action) > order.index(strongest):
            strongest = action
    return strongest
