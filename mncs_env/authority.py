"""Authority projection: explicit, machine-readable, default-deny.

AuthorityContext evaluation is PURE: given an intent, workspace facts,
active leases, and a requested action, it returns allow / deny / escalate
with a reason. Session code enforces the verdict; nothing here spawns
processes or mutates state. Absence of a grant never becomes permission.
"""

from __future__ import annotations

from typing import Any

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
    claim_holders: dict[str, str] | None = None,
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


def evaluate(
    context: dict[str, Any],
    *,
    action: str,
    target: str,
    session_id: str,
    claims: dict[str, str] | None = None,
    repo_facts: dict[str, dict[str, Any]] | None = None,
) -> dict[str, str]:
    """Return {verdict: allow|deny|escalate, reason} for one requested action.

    Ownership rule for mutation: naming a repository in intent is not
    ownership. A mutating action is allowed only when the repository is
    clean, on its main branch, shows no foreign-work signals, and sits in
    declared writable scope — or when this session holds a live claim.
    Anything else escalates; protected, forbidden, or foreign-claimed
    scope denies. Escalate and deny never authorize execution.
    """
    if action not in ACTIONS:
        return {"verdict": "deny", "reason": f"unknown action {action!r}"}
    if action in context.get("denied", []):
        return {"verdict": "deny", "reason": f"action {action} is forbidden by intent"}
    repo = target.split("/")[0] if "/" in target else target
    holders = claims if claims is not None else context.get("claim_holders", {})
    holder = holders.get(repo)
    if holder and holder != session_id and action in (
        "write", "mutate", "merge", "delete", "execute",
    ):
        return {
            "verdict": "deny",
            "reason": f"{repo} is claimed by another session ({holder})",
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
    if action in MUTATING_ACTIONS:
        facts = (repo_facts or {}).get(repo, {})
        owned = holder == session_id
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
                "reason": f"{repo} mutation by claim" if owned else f"{target} is owned clean scope"}
    if action in context.get("escalation_required", []):
        return {"verdict": "escalate", "reason": f"{action} requires escalation"}
    if action == "invoke":
        return {"verdict": "allow", "reason": "invocation permitted; effects checked per call"}
    if action in ("read", "subscribe"):
        return {"verdict": "allow", "reason": "read/subscribe permitted workspace-wide"}
    if action == "handoff":
        return {"verdict": "allow", "reason": "handoff permitted; receiving consumer revalidates"}
    return {"verdict": "escalate", "reason": f"no grant covers {action} on {target}"}


def required_action_for_effects(effects: list[str]) -> str:
    """Strongest session action implied by capability effects."""
    order = ["read", "execute", "write", "publish"]
    strongest = "read"
    for effect in effects:
        action = EFFECT_ACTIONS.get(effect, "execute")
        if order.index(action) > order.index(strongest):
            strongest = action
    return strongest
