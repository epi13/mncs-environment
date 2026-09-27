"""Authority projection: explicit, machine-readable, default-deny.

AuthorityContext evaluation is PURE: given an intent, workspace facts,
active leases, and a requested action, it returns allow / deny / escalate
with a reason. Session code enforces the verdict; nothing here spawns
processes or mutates state. Absence of a grant never becomes permission.
"""

from __future__ import annotations

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
    "invoke",
    "subscribe",
    "delegate",
    "handoff",
)

# Capability effects mapped to the minimum session action required.
EFFECT_ACTIONS = {
    "read": "read",
    "write": "write",
    "execute": "execute",
    "publish": "publish",
}


MUTATING_ACTIONS = ("write", "mutate", "execute")


def build_context(
    *,
    subject: str,
    intent: dict[str, Any],
    protected_repos: list[str],
    claim_holders: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Project effective authority for a consumer from intent + workspace facts."""
    forbidden = set(intent.get("forbidden_actions", []))
    escalate_actions = {"publish", "merge", "delete", "delegate"}
    context = {
        "schema_version": SCHEMA,
        "subject": subject,
        "readable": ["workspace"],
        "writable": [repo for repo in intent.get("repositories", [])],
        "invocable": ["*"],
        "protected_repositories": sorted(set(protected_repos)),
        "claim_holders": dict(claim_holders or {}),
        "escalation_required": sorted(action for action in escalate_actions if action not in forbidden),
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
            normalized.append({
                "session_id": str(item.get("session_id", "")),
                "scope": scope,
            })
    return normalized


def _requested_scope(repo: str, target: str,
                     scope: dict[str, Any] | None) -> dict[str, Any]:
    if scope:
        merged = {"kind": "paths" if scope.get("paths") else "repository",
                  "repository": repo}
        merged.update(scope)
        return claims_module.normalize_scope(merged, repo)
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
) -> dict[str, str]:
    """Return {verdict: allow|deny|escalate, reason} for one requested action.

    Ownership rule for mutation: naming a repository in intent is not
    ownership. A mutating action is allowed only when the requested scope
    is covered by this session's live claim, or when the repository is
    clean, on its main branch, shows no foreign-work signals, sits in
    declared writable scope, and no other session holds an overlapping
    scope. Anything else escalates; protected, forbidden, or
    foreign-claimed scope denies. Escalate and deny never authorize
    execution.
    """
    if action not in ACTIONS:
        return {"verdict": "deny", "reason": f"unknown action {action!r}"}
    if action in context.get("denied", []):
        return {"verdict": "deny", "reason": f"action {action} is forbidden by intent"}
    repo = target.split("/")[0] if "/" in target else target
    holders = _holder_list(
        claims if claims is not None else context.get("claim_holders", {}), repo)
    others = [h for h in holders if h["session_id"] != session_id]
    own = [h for h in holders if h["session_id"] == session_id]
    mutating = action in ("write", "mutate", "merge", "delete", "execute")
    requested = _requested_scope(repo, target, scope) if mutating else None
    for other in others:
        if not mutating:
            continue
        conflict = claims_module.scopes_conflict(
            requested or {}, other.get("scope") or {"kind": "repository"})
        if conflict is not None:
            return {
                "verdict": "deny",
                "reason": (f"{target} overlaps scope claimed by another session "
                           f"({other['session_id']}): {conflict}"),
            }
    if repo in context.get("protected_repositories", []) and action in (
        "write",
        "mutate",
        "merge",
        "delete",
        "execute",
        "publish",
    ):
        return {
            "verdict": "deny",
            "reason": f"{repo} is protected scope for this session",
        }
    if action in ("publish", "merge"):
        if any((h.get("scope") or {}).get("kind") == "repository"
               and (h.get("scope") or {}).get("repository", repo) == repo
               for h in own):
            return {"verdict": "allow",
                    "reason": f"{action} by repository claim on {repo}"}
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
                        "reason": f"{repo} has no observed workspace facts; claim it first"}
            return {"verdict": "escalate",
                    "reason": f"{repo} is not owned scope (dirty/foreign/off-branch); claim it first"}
        return {"verdict": "allow",
                "reason": f"{target} mutation by claim" if owned else f"{target} is owned clean scope"}
    if action in context.get("escalation_required", []):
        return {"verdict": "escalate", "reason": f"{action} requires escalation"}
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
    order = ["read", "execute", "write", "publish"]
    strongest = "read"
    for effect in effects:
        action = EFFECT_ACTIONS.get(effect, "execute")
        if order.index(action) > order.index(strongest):
            strongest = action
    return strongest
