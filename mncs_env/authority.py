"""Authority projection: explicit, machine-readable, default-deny.

AuthorityContext evaluation is PURE: given an intent, workspace facts,
active leases, and a requested action, it returns allow / deny / escalate
with a reason. Session code enforces the verdict; nothing here spawns
processes or mutates state. Absence of a grant never becomes permission.
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

# These are invariant user-approval gates. Repository-integrating actions
# instead require a live claim over the exact destination scope; a claim is
# the authenticated authorization, not a reason to ask for another approval.
ESCALATION_ACTIONS = ("delete",)
CLAIM_REQUIRED_ACTIONS = ("delegate", "merge", "publish", "retire")
CLAIM_CONFLICT_SCHEMA = "mncs.environment.authority-claim-conflict/1"
_EVIDENCE_IDENTIFIER = re.compile(r"[A-Za-z0-9:._-]{1,160}")


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
    """Return {verdict: allow|deny|escalate, reason} for one requested action.

    Ownership rule for mutation: naming a repository in intent is not
    ownership. A mutating action is allowed only when the requested scope
    is covered by this session's live claim, or when the repository is
    clean, on its main branch, shows no foreign-work signals, sits in
    declared writable scope, and no other session holds an overlapping
    scope. Anything else escalates; protected, forbidden, or
    foreign-claimed scope denies. ``merge`` and ``publish`` require a current
    claim covering the exact destination scope; a clean checkout alone is
    insufficient. Escalate and deny never authorize execution. The separate
    ``retire`` action also requires a current claim covering its exact
    repository or worktree scope; generic ``delete`` remains escalation-gated.
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
    holders = _holder_list(
        claims if claims is not None else context.get("claim_holders", {}), repo)
    others = [h for h in holders if h["session_id"] != session_id]
    own = [h for h in holders if h["session_id"] == session_id]
    mutating = action in (
        "write", "mutate", "merge", "delete", "execute", "retire", "publish"
    )
    # Verification executes provider code inside a checkout, so foreign
    # ownership conflicts deny exactly like mutation; unlike mutation,
    # dirt and branch are the *subject* under verification, not a bar.
    conflict_checked = mutating or action == "verify"
    requested = (
        _requested_scope(repo, target, scope, repo_facts)
        if conflict_checked else None
    )
    for other in others:
        if not conflict_checked:
            continue
        conflict = claims_module.scopes_conflict(
            requested or {}, other.get("scope") or {"kind": "repository"})
        if conflict is not None:
            blocking_claim: dict[str, Any] = {
                "session_id": other["session_id"],
                "scope": _scope_evidence(other.get("scope") or {}, repo),
            }
            claim_id = other.get("claim_id")
            if isinstance(claim_id, str) and claim_id:
                blocking_claim["claim_id"] = claim_id
            return {
                "verdict": "deny",
                "reason": (f"{target} overlaps scope claimed by another session "
                           f"({other['session_id']}): {conflict}"),
                "claim_conflict": {
                    "schema_version": CLAIM_CONFLICT_SCHEMA,
                    "repository": repo,
                    "conflict_reason": conflict,
                    "requested_scope": _scope_evidence(requested or {}, repo),
                    "blocking_claim": blocking_claim,
                },
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
            "reason": f"{repo} is protected scope for this session",
        }
    if action in ("publish", "merge"):
        if repo not in context.get("writable", []):
            return {"verdict": "deny",
                    "reason": f"{repo} is outside this session's writable authority"}
        owned = any(
            _covers(holder.get("scope") or {}, requested or {}) for holder in own
        )
        if not owned:
            return {
                "verdict": "escalate",
                "required_authorization": "session-claim",
                "next_step": "acquire-exact-claim",
                "reason": (
                    f"{action} requires a current session claim covering the "
                    "exact destination scope"
                ),
            }
        return {"verdict": "allow", "reason": f"{action} by scoped claim on {repo}"}
    if action == "delegate":
        # Asking an external system to verify a repository scope the
        # session owns is explicit intent, like publish. Anything else
        # escalates; remote work never dispatches ambiently by default.
        if any((h.get("scope") or {}).get("kind") == "repository"
               and (h.get("scope") or {}).get("repository", repo) == repo
               for h in own):
            return {"verdict": "allow",
                    "reason": f"delegate by repository claim on {repo}"}
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
                    "reason": f"{target} retirement by scoped claim"}
        return {"verdict": "escalate",
                "required_authorization": "session-claim",
                "next_step": "acquire-exact-claim",
                "reason": f"{target} retirement requires a current claim covering the requested scope"}
    if action in MUTATING_ACTIONS:
        facts = (repo_facts or {}).get(repo, {})
        owned = any(
            _covers(h.get("scope") or {}, requested or {}) for h in own
        )
        pristine = bool(
            facts.get("clean")
            and facts.get("main_branch")
            and not facts.get("foreign_signals")
            and target in context.get("writable", [])
        )
        if not owned and not pristine:
            if not facts:
                return {"verdict": "escalate",
                        "required_authorization": "session-claim",
                        "next_step": "acquire-exact-claim",
                        "reason": f"{repo} has no observed workspace facts; claim it first"}
            return {"verdict": "escalate",
                    "required_authorization": "session-claim",
                    "next_step": "acquire-exact-claim",
                    "reason": f"{repo} is not owned scope (dirty/foreign/off-branch); claim it first"}
        return {"verdict": "allow",
                "reason": f"{target} mutation by claim" if owned else f"{target} is owned clean scope"}
    if action == "verify":
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
