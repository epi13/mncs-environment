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


def build_context(
    *,
    subject: str,
    intent: dict[str, Any],
    protected_repos: list[str],
    lease_holders: dict[str, str],
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
        "lease_holders": dict(lease_holders),
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
) -> dict[str, str]:
    """Return {verdict: allow|deny|escalate, reason} for one requested action."""
    if action not in ACTIONS:
        return {"verdict": "deny", "reason": f"unknown action {action!r}"}
    if action in context.get("denied", []):
        return {"verdict": "deny", "reason": f"action {action} is forbidden by intent"}
    repo = target.split("/")[0] if "/" in target else target
    holder = context.get("lease_holders", {}).get(repo)
    if holder and holder != session_id and action in ("write", "mutate", "merge", "delete", "execute"):
        return {
            "verdict": "deny",
            "reason": f"{repo} is leased to another session ({holder})",
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
    if action in context.get("escalation_required", []):
        return {"verdict": "escalate", "reason": f"{action} requires escalation"}
    if action == "invoke":
        return {"verdict": "allow", "reason": "invocation permitted; effects checked per call"}
    if action in ("read", "subscribe"):
        return {"verdict": "allow", "reason": "read/subscribe permitted workspace-wide"}
    if action == "write" and target in context.get("writable", []):
        return {"verdict": "allow", "reason": f"{target} is in writable scope"}
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
