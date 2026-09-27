#!/usr/bin/env python3
"""Seed the checked-in pressure registry (run rarely; records are curated)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mncs_env import pressures

SEEDS = [
    {
        "title": "No canonical provider event feeds",
        "description": "Services emit logs and return values but no typed completion/state-change events Environment can subscribe to. Session effects rely on a git-poll adapter and invocation-result envelopes.",
        "owner": "mncs-forge (orchestration), owning services (domain events)",
        "evidence": "mncs-environment/mncs_env/events.py git_poll_events; no event bus found in forge/actions/automation survey",
        "why_not_local": "Event semantics belong to the owning services; an environment-local bus would become a competing standard.",
        "desired_contract": "Typed, subscribable completion/state events per owning service.",
        "effect": "Service-affects-session is adapter-only; push notification is not yet reliable.",
        "workaround": "Session log + adapter:git-poll + invocation envelopes; subscriptions replay from the log.",
    },
    {
        "title": "No canonical workspace lease/ownership mechanism",
        "description": "Nothing in MNCS records which agent owns a worktree or branch. Environment keeps a local advisory lease file enforced only inside its own authority evaluation.",
        "owner": "mncs-control (ownership plane)",
        "evidence": "control/leases.py exists but is service-local; no family lease contract found",
        "why_not_local": "Cross-agent ownership must be authoritative family-wide; a local file cannot bind other harnesses.",
        "desired_contract": "Family lease record (holder, scope, expiry, release) readable without joining Environment.",
        "effect": "Foreign-work protection is heuristic plus voluntary leases.",
        "workaround": "mncs_env/leases.py advisory leases + foreign_work_signals; unknown work is never touched.",
    },
    {
        "title": "Rights/provenance granularity for session authority",
        "description": "Session authority is projected from intent constraints and protected scopes. Canonical authorization semantics live in mncs-rights-provenance, which Environment does not yet bind.",
        "owner": "mncs-rights-provenance",
        "evidence": "No machine grant-check endpoint was consumable from survey",
        "why_not_local": "Replacing rights evaluation would be security theater and a competing authorization universe.",
        "desired_contract": "Machine-checkable grant query (subject, action, scope).",
        "effect": "AuthorityContext is intent-declared; escalation paths are explicit but human-mediated.",
        "workaround": "Pure evaluate() with default-deny; every denial/escalation is an event.",
    },
    {
        "title": "Session durability lives in Environment file state, not Store/Memory",
        "description": "Sessions persist as JSON snapshots + JSONL logs under the state directory. Neither Store nor Memory currently offers a session-state contract.",
        "owner": "mncs-store (durable objects), mncs-memory (experience)",
        "evidence": "mncs_store.StoreSession is MNCS-binary-backed relation storage; no session/checkpoint schema exists there",
        "why_not_local": "Environment must own the session model; opaque blobs into Memory would misuse it.",
        "desired_contract": "Store-backed session segments or a referenceable session schema.",
        "effect": "Sessions are machine-local; cross-machine resume needs shared state-dir transport.",
        "workaround": "Atomic file writes + content identities; references (not copies) for external artifacts.",
    },
    {
        "title": "No canonical provider addressing",
        "description": "Entrypoint spellings resolve through a small bootstrap table plus manifest fingerprint heuristics, not provider-published addresses.",
        "owner": "owning service repositories (provider metadata)",
        "evidence": "family-provider-metadata-v1.json carries no invocation address",
        "why_not_local": "Addressing belongs to providers; environment-side guessing would be a shadow registry.",
        "desired_contract": "Provider-published invocation address per capability revision.",
        "effect": "Novel entrypoint spellings bind as unavailable until addressed.",
        "workaround": "ENTRYPOINT_CANDIDATES table + fingerprint_sources heuristic, both explicit and tested.",
    },
    {
        "title": "Environment concepts have no native MNCS representation yet",
        "description": "Intent, authority projection, lifecycle, and checkpoints are JSON contracts over host infrastructure.",
        "owner": "mncs-language / mncs-compiler (capability growth)",
        "evidence": "docs/MODEL.md native-MNCS section",
        "why_not_local": "Forcing immature language features would block useful infrastructure for ideological purity.",
        "desired_contract": "Stable file-effect + record-schema story for Environment projection inside MNCS.",
        "effect": "Canonical semantics live in Python with JSON contracts; migration path stays open.",
        "workaround": "JSON contracts are canonical; host code is transport/discovery only.",
    },
]


def main() -> int:
    out = [pressures.record(**seed) for seed in SEEDS]
    target = Path(__file__).resolve().parents[1] / "pressures" / "registry.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(
        json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"{len(out)} pressures seeded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
