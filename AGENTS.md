# Agent Guide

`mncs-environment` defines the coherent machine-native entry point into the MNCS working world.

## Mission

Build the boundary through which an agent or other consumer can enter MNCS and receive a resolved, persistent, authorized, event-aware development environment without reconstructing the system from prompts.

## Non-negotiable boundaries

- Compose existing MNCS services. Do not duplicate their domain semantics here.
- Keep the environment contract model/vendor agnostic.
- Do not turn this repository into an IDE, agent framework, or orchestration god-service.
- Keep authority explicit and machine-readable.
- Treat sessions as persistent work contexts that may be resumed by a different consumer.
- Treat service events and triggers as first-class inputs, not merely polling concerns.
- Preserve provenance and continuation state as part of normal operation.
- Build one evolving canonical implementation. Do not introduce parallel legacy/current or `v1`/`v2` implementations merely to avoid coordinated upgrades across MNCS.

## Before implementing

Read, in order:

1. `README.md`
2. `docs/ARCHITECTURE.md`
3. `docs/MODEL.md`
4. `docs/INTEGRATIONS.md`
5. `rfcs/0001-environment-session-model.md`
6. `rfcs/0002-built-boundaries.md`
7. `docs/DEVELOPMENT.md`

Verify behavior with `python3 -m pytest tests/ -q` and
`python3 scripts/vertical_proof.py` before changing contracts.

## Architectural test

For any proposed feature, ask:

> Is this required to assemble, expose, maintain, or resume a coherent environment/session?

If yes, it may belong here.

If it plans domain work, executes domain work, implements compiler semantics, owns persistent memory semantics, decides rights policy, diagnoses failures, or presents a human project dashboard, it probably belongs in another MNCS repository and should be exposed here through a binding.

## Implementation posture

Prefer explicit typed contracts and adapters over implicit process assumptions. A session should be reconstructible from persisted authoritative state plus referenced external state. Avoid encoding facts that another service already owns; store stable references, observed revisions, and provenance instead.

Tests should increasingly prove these properties:

- deterministic environment resolution from the same authoritative inputs,
- explicit capability and authority binding,
- inability to cross protected scope accidentally,
- event delivery or replay sufficient to resume work,
- checkpoint/handoff across consumer processes,
- no semantic duplication of bound services,
- graceful handling of unavailable or stale services.

When another MNCS repository lacks a capability this design needs, record the integration pressure clearly. Do not quietly implement the missing service behavior inside Environment.
