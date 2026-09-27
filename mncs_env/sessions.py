"""Environment resolution and durable EnvironmentSession lifecycle.

Resolution composes workspace facts, discovered capabilities, authority
projection, and event sources into one inspectable Environment record.
Sessions persist as structured snapshots plus an append-only event log,
so a different process or consumer resumes from state, not prose.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import authority as authority_module
from . import capabilities as capabilities_module
from . import events as events_module
from . import leases as leases_module
from . import workspace as workspace_module
from .identity import (
    checkpoint_id,
    digest_hex,
    environment_id,
    handoff_id,
    new_session_id,
    resolved_environment_id,
)
from .intent import parse as parse_intent
from .persist import append_jsonl, read_json, read_jsonl, write_json

SESSION_SCHEMA = "mncs.environment.session/1"
ENVIRONMENT_SCHEMA = "mncs.environment.resolved/1"
CHECKPOINT_SCHEMA = "mncs.environment.checkpoint/1"
HANDOFF_SCHEMA = "mncs.environment.handoff/1"

# Allowed lifecycle transitions. Every transition records a reason; there
# is no vague string field — an illegal transition raises LifecycleError.
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "defined": ("resolving", "abandoned"),
    "resolving": ("ready", "failed", "abandoned"),
    "ready": ("active", "failed", "abandoned"),
    "active": ("blocked", "waiting", "checkpointed", "handed_off", "completed", "failed", "abandoned"),
    "blocked": ("active", "failed", "abandoned"),
    "waiting": ("active", "failed", "abandoned"),
    "checkpointed": ("active", "failed", "abandoned"),
    "handed_off": ("active", "abandoned"),
    "completed": (),
    "failed": (),
    "abandoned": ("active",),
}


class LifecycleError(Exception):
    """Raised for illegal session transitions or corrupted session state."""


class AuthorityDenied(Exception):
    """Raised when session authority denies a requested action."""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _session_dir(state_dir: Path, session_id: str) -> Path:
    return Path(state_dir) / "sessions" / session_id


def resolve_environment(
    *,
    definition: dict[str, Any],
    workspace_root: str | Path,
    state_dir: str | Path,
    consumer_id: str,
) -> dict[str, Any]:
    """Resolve a declarative environment definition into an inspectable world."""
    state = Path(state_dir)
    workspace_view = workspace_module.discover_workspace(workspace_root)
    discovered = capabilities_module.discover_capabilities(workspace_root)
    bindings = [capabilities_module.probe_availability(binding) for binding in discovered]
    unavailable = [
        {"provider": binding["provider"], "capability": binding["capability"],
         "reason": binding["availability"]["reason"]}
        for binding in bindings
        if binding["availability"]["status"] != "available"
    ]
    intent = parse_intent(definition.get("intent", {"goal": definition.get("goal", "unspecified")}))
    protected = sorted(
        set(intent.get("protected_repositories", []))
        | set(definition.get("protected_repositories", []))
    )
    holders = leases_module.active_holders(state)
    authority_context = authority_module.build_context(
        subject=consumer_id,
        intent=intent,
        protected_repos=protected,
        lease_holders=holders,
    )
    inputs = {
        "workspace_root": str(Path(workspace_root).resolve()),
        "repository_heads": {
            repo["name"]: repo.get("head")
            for repo in workspace_view.get("repositories", [])
        },
        "binding_ids": sorted(binding["binding_id"] for binding in bindings),
        "intent": intent["identity"],
        "lease_holders": holders,
    }
    definition_id = environment_id(definition)
    environment = {
        "schema_version": ENVIRONMENT_SCHEMA,
        "identity": resolved_environment_id(definition_id, inputs),
        "definition_id": definition_id,
        "resolved_at": utcnow(),
        "consumer_id": consumer_id,
        "workspace": workspace_view,
        "bindings": bindings,
        "unavailable_capabilities": unavailable,
        "intent": intent,
        "protected_repositories": protected,
        "authority": authority_context,
        "event_sources": [
            {"kind": "session-log", "replay": True},
            {"kind": "adapter:git-poll", "replay": False,
             "note": "polling adapter; canonical provider events are a pressure"},
        ],
        "resolution_inputs_digest": digest_hex(inputs),
    }
    return environment


class Session:
    """A durable session bound to persisted snapshot + event log."""

    def __init__(self, state_dir: Path | str, session_id: str):
        self.state_dir = Path(state_dir)
        self.session_id = session_id
        self.directory = _session_dir(self.state_dir, session_id)
        snapshot = read_json(self.directory / "session.json")
        if not isinstance(snapshot, dict) or snapshot.get("session_id") != session_id:
            raise LifecycleError(f"session {session_id} has no readable snapshot")
        self.snapshot = snapshot

    # -- construction ----------------------------------------------------

    @classmethod
    def create(
        cls,
        *,
        state_dir: Path | str,
        environment: dict[str, Any],
        consumer_id: str,
        consumer_kind: str = "agent",
    ) -> "Session":
        state = Path(state_dir)
        session_id = new_session_id(environment["identity"], consumer_id)
        snapshot = {
            "schema_version": SESSION_SCHEMA,
            "session_id": session_id,
            "environment_id": environment["identity"],
            "environment_digest": digest_hex(environment),
            "consumer_id": consumer_id,
            "consumer_kind": consumer_kind,
            "lifecycle": "defined",
            "lifecycle_history": [{"state": "defined", "reason": "session created", "at": utcnow()}],
            "intent": environment.get("intent"),
            "authority": environment.get("authority"),
            "bindings": environment.get("bindings", []),
            "workspace_heads": {
                repo["name"]: repo.get("head")
                for repo in environment.get("workspace", {}).get("repositories", [])
            },
            "subscriptions": [],
            "artifacts": [],
            "decisions": [],
            "pressures": [],
            "checkpoints": [],
            "handoffs": [],
            "completion": None,
            "created_at": utcnow(),
            "updated_at": utcnow(),
            "provenance": {
                "created_by": consumer_id,
                "definition_id": environment.get("definition_id"),
                "resolution_inputs_digest": environment.get("resolution_inputs_digest"),
            },
        }
        directory = _session_dir(state, session_id)
        write_json(directory / "session.json", snapshot)
        instance = cls(state, session_id)
        instance._emit("session.created", "environment", {"consumer_id": consumer_id})
        return instance

    @classmethod
    def resume(cls, *, state_dir: Path | str, session_id: str) -> "Session":
        instance = cls(state_dir, session_id)
        if instance.snapshot.get("lifecycle") in ("completed", "failed"):
            raise LifecycleError(f"session {session_id} is terminal and cannot resume")
        instance._emit("session.resumed", "environment",
                       {"consumer_id": instance.snapshot.get("consumer_id")})
        return instance

    # -- persistence helpers ----------------------------------------------

    def _save(self) -> None:
        self.snapshot["updated_at"] = utcnow()
        write_json(self.directory / "session.json", self.snapshot)

    def _log(self) -> list[dict[str, Any]]:
        return read_jsonl(self.directory / "events.jsonl")

    def _emit(
        self,
        event_type: str,
        producer: str,
        payload: dict[str, Any] | None = None,
        causes: list[str] | None = None,
    ) -> dict[str, Any]:
        sequence = len(self._log()) + 1
        event = events_module.make(
            session_id=self.session_id,
            sequence=sequence,
            event_type=event_type,
            producer=producer,
            payload=payload,
            causes=causes,
        )
        append_jsonl(self.directory / "events.jsonl", event)
        return event

    # -- lifecycle ---------------------------------------------------------

    def transition(self, to_state: str, reason: str) -> dict[str, Any]:
        current = self.snapshot.get("lifecycle")
        allowed = TRANSITIONS.get(current, ())
        if to_state not in allowed:
            raise LifecycleError(f"illegal transition {current} -> {to_state}: {reason}")
        self.snapshot["lifecycle"] = to_state
        self.snapshot["lifecycle_history"].append({"state": to_state, "reason": reason, "at": utcnow()})
        self._save()
        return {"from": current, "to": to_state, "reason": reason}

    # -- intent / authority / capabilities ----------------------------------

    def attach_intent(self, raw_intent: dict[str, Any]) -> dict[str, Any]:
        intent = parse_intent(raw_intent)
        self.snapshot["intent"] = intent
        self._emit("intent.attached", self.snapshot.get("consumer_id", "unknown"),
                   {"intent_id": intent["identity"]})
        self._save()
        return intent

    def check(self, *, action: str, target: str) -> dict[str, str]:
        verdict = authority_module.evaluate(
            self.snapshot.get("authority", {}),
            action=action,
            target=target,
            session_id=self.session_id,
        )
        if verdict["verdict"] == "deny":
            self._emit("authority.denied", "environment",
                       {"action": action, "target": target, "reason": verdict["reason"]})
        elif verdict["verdict"] == "escalate":
            self._emit("authority.escalated", "environment",
                       {"action": action, "target": target, "reason": verdict["reason"]})
        return verdict

    def _binding(self, capability: str) -> dict[str, Any]:
        for binding in self.snapshot.get("bindings", []):
            if binding.get("capability") == capability or binding.get("binding_id") == capability:
                return binding
        raise AuthorityDenied(f"no binding for capability {capability!r} in this session")

    def invoke(
        self,
        capability: str,
        argv: list[str],
        *,
        cwd: str | Path | None = None,
        timeout_seconds: int = 120,
    ) -> dict[str, Any]:
        binding = self._binding(capability)
        if binding.get("availability", {}).get("status") != "available":
            raise AuthorityDenied(
                f"capability {capability!r} is not available: "
                f"{binding.get('availability', {}).get('reason')}"
            )
        required = authority_module.required_action_for_effects(binding.get("effects", ["read"]))
        verdict = self.check(action="invoke", target=capability)
        if verdict["verdict"] == "deny":
            raise AuthorityDenied(verdict["reason"])
        if required != "read":
            effect_verdict = self.check(action=required, target=binding.get("provider", capability))
            if effect_verdict["verdict"] == "deny":
                raise AuthorityDenied(effect_verdict["reason"])
        self._emit("capability.invoked", self.snapshot.get("consumer_id", "unknown"),
                   {"capability": capability, "argv": argv})
        result = capabilities_module.invoke(binding, argv, cwd=cwd, timeout_seconds=timeout_seconds)
        self.snapshot.setdefault("artifacts", []).append(
            {"kind": "invocation-result", "capability": capability,
             "status": result["status"], "at": utcnow()}
        )
        self._emit("invocation.completed", binding.get("provider", "unknown"),
                   {"capability": capability, "status": result["status"],
                    "returncode": result["returncode"]})
        self._save()
        return result

    # -- events / subscriptions ---------------------------------------------

    def subscribe(self, event_types: list[str], source_filter: str | None = None) -> dict[str, Any]:
        subscription = events_module.subscribe(
            subscription_id=f"sub_{digest_hex([self.session_id, event_types, source_filter])[:12]}",
            session_id=self.session_id,
            event_types=event_types,
            source_filter=source_filter,
            cursor=len(self._log()),
        )
        subscriptions = [sub for sub in self.snapshot.get("subscriptions", [])
                         if sub["subscription_id"] != subscription["subscription_id"]]
        subscriptions.append(subscription)
        self.snapshot["subscriptions"] = subscriptions
        self._save()
        return subscription

    def poll(self, subscription_id: str) -> list[dict[str, Any]]:
        for subscription in self.snapshot.get("subscriptions", []):
            if subscription["subscription_id"] == subscription_id:
                due, advanced = events_module.deliverable(subscription, self._log())
                subscription["cursor"] = advanced["cursor"]
                self._save()
                return due
        raise LifecycleError(f"unknown subscription {subscription_id}")

    def record_observation(
        self, event_type: str, producer: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return self._emit(event_type, producer, payload)

    def observe_workspace(self, workspace_root: str | Path) -> list[dict[str, Any]]:
        """Adapter poll: head changes since resolution become workspace.changed events."""
        current = {
            repo["name"]: repo.get("head")
            for repo in workspace_module.discover_workspace(workspace_root).get("repositories", [])
        }
        previous = self.snapshot.get("workspace_heads", {})
        events, _ = events_module.git_poll_events(
            session_id=self.session_id,
            sequence_start=len(self._log()) + 1,
            previous_heads=previous,
            current_heads=current,
        )
        for event in events:
            append_jsonl(self.directory / "events.jsonl", event)
        self.snapshot["workspace_heads"] = current
        self._save()
        return events

    def revalidate(self) -> dict[str, Any]:
        """Re-probe bindings on resume; divergence becomes typed events, not trust."""
        report: dict[str, Any] = {"reprobed": 0, "changed": []}
        for binding in self.snapshot.get("bindings", []):
            before = binding.get("availability", {}).get("status")
            fresh = capabilities_module.probe_availability(binding)
            binding["availability"] = fresh["availability"]
            report["reprobed"] += 1
            if fresh["availability"]["status"] != before:
                report["changed"].append(binding["capability"])
                self._emit(
                    "capability.available" if fresh["availability"]["status"] == "available"
                    else "capability.unavailable",
                    "environment",
                    {"capability": binding["capability"], "previous": before},
                )
        self._save()
        return report

    # -- checkpoint / handoff / completion ------------------------------------

    def checkpoint(self, *, progress: str = "", remaining: list[str] | None = None) -> dict[str, Any]:
        sequence = len(self.snapshot.get("checkpoints", [])) + 1
        state_digest = digest_hex(
            {"intent": self.snapshot.get("intent"), "artifacts": self.snapshot.get("artifacts"),
             "decisions": self.snapshot.get("decisions"), "heads": self.snapshot.get("workspace_heads")}
        )
        record = {
            "schema_version": CHECKPOINT_SCHEMA,
            "identity": checkpoint_id(self.session_id, sequence, state_digest),
            "session_id": self.session_id,
            "sequence": sequence,
            "progress": progress,
            "remaining": list(remaining or []),
            "intent_id": (self.snapshot.get("intent") or {}).get("identity"),
            "environment_id": self.snapshot.get("environment_id"),
            "event_cursor": len(self._log()),
            "artifacts": list(self.snapshot.get("artifacts", [])),
            "unresolved": list(self.snapshot.get("pressures", [])),
            "revalidate_on_resume": ["bindings", "workspace-heads", "leases"],
            "created_at": utcnow(),
            "created_by": self.snapshot.get("consumer_id"),
        }
        self.snapshot.setdefault("checkpoints", []).append(record["identity"])
        write_json(self.directory / "checkpoints" / f"{record['identity']}.json", record)
        if self.snapshot.get("lifecycle") == "active":
            self.transition("checkpointed", f"checkpoint {sequence}")
            self.transition("active", "resumed after checkpoint")
        self._emit("session.checkpointed", self.snapshot.get("consumer_id", "unknown"),
                   {"checkpoint_id": record["identity"], "progress": progress})
        self._save()
        return record

    def handoff(
        self,
        *,
        to_consumer: str,
        notes: list[str] | None = None,
        blockers: list[str] | None = None,
        next_actions: list[str] | None = None,
    ) -> dict[str, Any]:
        checkpoint = self.checkpoint(progress="handoff", remaining=next_actions)
        record = {
            "schema_version": HANDOFF_SCHEMA,
            "identity": handoff_id(checkpoint["identity"], self.snapshot.get("consumer_id", ""), to_consumer),
            "checkpoint_id": checkpoint["identity"],
            "session_id": self.session_id,
            "from_consumer": self.snapshot.get("consumer_id"),
            "to_consumer": to_consumer,
            "to_consumer_kind": "agent",
            "notes": list(notes or []),
            "blockers": list(blockers or []),
            "next_actions": list(next_actions or []),
            "created_at": utcnow(),
        }
        self.snapshot.setdefault("handoffs", []).append(record["identity"])
        write_json(self.directory / "handoffs" / f"{record['identity']}.json", record)
        if self.snapshot.get("lifecycle") == "active":
            self.transition("handed_off", f"handoff to {to_consumer}")
        self._emit("handoff.created", self.snapshot.get("consumer_id", "unknown"),
                   {"handoff_id": record["identity"], "to_consumer": to_consumer})
        self._save()
        return record

    def accept_handoff(self, *, consumer_id: str, consumer_kind: str = "agent") -> dict[str, Any]:
        """A different consumer takes over: identity switches, history persists."""
        previous = self.snapshot.get("consumer_id")
        self.snapshot["consumer_id"] = consumer_id
        self.snapshot["consumer_kind"] = consumer_kind
        self.snapshot["authority"] = dict(self.snapshot.get("authority", {}))
        self.snapshot["authority"]["subject"] = consumer_id
        if self.snapshot.get("lifecycle") == "handed_off":
            self.transition("active", f"accepted by {consumer_id} (was {previous})")
        self._emit("session.resumed", consumer_id, {"previous_consumer": previous})
        self._save()
        return {"previous_consumer": previous, "consumer_id": consumer_id}

    def complete(self, *, outcome: str, summary: str = "") -> dict[str, Any]:
        if self.snapshot.get("lifecycle") not in ("active", "checkpointed", "waiting", "blocked"):
            raise LifecycleError("only a live session can complete")
        self.transition("completed", outcome)
        self.snapshot["completion"] = {"outcome": outcome, "summary": summary, "at": utcnow()}
        self._emit("session.completed", self.snapshot.get("consumer_id", "unknown"),
                   {"outcome": outcome})
        self._save()
        return self.snapshot["completion"]

    def fail(self, *, reason: str) -> dict[str, Any]:
        if self.snapshot.get("lifecycle") not in ("active", "resolving", "ready", "blocked", "waiting", "checkpointed"):
            raise LifecycleError("session cannot fail from its current state")
        self.transition("failed", reason)
        self._emit("session.failed", self.snapshot.get("consumer_id", "unknown"), {"reason": reason})
        self._save()
        return {"reason": reason}

    # -- inspection ------------------------------------------------------------

    def inspect(self) -> dict[str, Any]:
        log = self._log()
        return {
            "session_id": self.session_id,
            "lifecycle": self.snapshot.get("lifecycle"),
            "consumer_id": self.snapshot.get("consumer_id"),
            "consumer_kind": self.snapshot.get("consumer_kind"),
            "environment_id": self.snapshot.get("environment_id"),
            "intent": self.snapshot.get("intent"),
            "authority": self.snapshot.get("authority"),
            "bindings": [
                {"capability": binding.get("capability"), "provider": binding.get("provider"),
                 "availability": binding.get("availability")}
                for binding in self.snapshot.get("bindings", [])
            ],
            "event_count": len(log),
            "latest_events": log[-5:],
            "checkpoints": self.snapshot.get("checkpoints", []),
            "handoffs": self.snapshot.get("handoffs", []),
            "artifacts": self.snapshot.get("artifacts", []),
            "completion": self.snapshot.get("completion"),
            "lifecycle_history": self.snapshot.get("lifecycle_history", []),
        }
