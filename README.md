# mncs-environment

Canonical machine-native development environment for assembling work intent, repository state, services, capabilities, triggers, memory, authority, provenance, and execution context into a coherent entry point for agents and other MNCS consumers.

## Why this exists

MNCS increasingly has the machinery needed to compile, plan, admit, execute, observe, verify, diagnose, automate, persist, and reason about work. What has been missing is the coherent boundary through which a consumer enters that machinery.

`mncs-environment` owns that boundary.

An agent should not need a giant prompt explaining which repositories exist, what is running, what state the workspace is in, which services are authoritative, what it may modify, what work is already active, or how to receive events. It should enter an environment and receive a machine-readable working world.

The long-term interaction should be conceptually simple:

```text
enter environment
  -> receive environment/session context
  -> submit or inherit work intent
  -> use bound MNCS capabilities
  -> observe events and triggers
  -> checkpoint, hand off, or complete
```

## Architectural role

Environment **composes** MNCS. It does not absorb or reimplement the services it exposes.

```text
consumer
   |
   v
+-----------------------+
|   mncs-environment    |
|-----------------------|
| Environment           |
| EnvironmentSession    |
| WorkIntent            |
| WorkspaceView         |
| CapabilityBindings    |
| AuthorityContext      |
| EventSubscriptions    |
| Checkpoint / Handoff  |
+-----------+-----------+
            |
            v
  existing MNCS services
```

Expected consumers include:

- coding agents such as Codex or Claude,
- future MNCS-native models and model stacks,
- specialized workers and automation,
- CI and deterministic controllers,
- human-facing dashboards,
- other MNCS services.

The contract must remain consumer-agnostic. No model vendor, chat protocol, IDE, or human UI owns the environment model.

## What this repository owns

- Resolving an environment from declarative intent and available system state.
- Constructing a coherent `EnvironmentSession` for a consumer.
- Presenting relevant workspace and repository state.
- Binding discoverable MNCS capabilities into a session without duplicating their implementation.
- Carrying authority and constraints into the session.
- Connecting session work to events, triggers, checkpoints, provenance, and handoff.
- Giving consumers stable machine-native handles to the surrounding MNCS system.
- Defining the entry and continuation contract for long-running work.

## What this repository does not own

- Language or compiler semantics.
- Planning semantics that belong to RAVEL.
- Execution/orchestration semantics that belong to Control, Actions, Automation, Forge, or other execution services.
- Persistent-memory semantics that belong to the appropriate memory/store service.
- Rights or provenance policy that belongs to rights/provenance services.
- Debugging or health policy owned by Debug/Doctor.
- Atlas's ecosystem knowledge model or human dashboard presentation.
- Agent-specific prompting, model internals, or an IDE.

Environment may expose all of the above. It must not become their second implementation.

## Core concepts

### Environment

A resolved description of the working world: relevant workspace, state, services, capabilities, authority, event sources, and durable references required to begin work coherently.

### EnvironmentSession

A persistent, scoped interaction between a consumer and an Environment. A session records identity, bound capabilities, active intent, observations, produced artifacts, decisions, events, checkpoints, and handoff state.

### WorkIntent

A machine-readable declaration of desired outcomes, constraints, priorities, acceptance conditions, and protected scope. It is richer than a TODO list but does not itself become the planner or executor.

### CapabilityBinding

A stable binding from a session to a capability provided by another MNCS service. Bindings identify what is available, how it is addressed, what authority is required, and how results/events relate back to the session.

### AuthorityContext

The explicit limits under which the session operates: readable and writable resources, executable capabilities, protected work, required escalation, and provenance expectations.

### Event / Trigger

A typed observation that can update or resume a session: verification completed, repository changed, pressure discovered, dependency satisfied, work became admissible, execution failed, and similar state transitions.

### Checkpoint / Handoff

A durable continuation boundary. Another compatible consumer should be able to resume from a checkpoint without reconstructing the world from prose or rediscovering completed work.

## Design principles

1. **One evolving canonical design.** Do not create artificial parallel `v1`, `v2`, legacy, or compatibility implementations while MNCS remains free to evolve its consumers together.
2. **Composition over duplication.** Environment binds services; it does not clone their semantics.
3. **Machine-native first.** Human-readable output is useful, but contracts must be structured, addressable, and deterministic where possible.
4. **Persistent work, replaceable consumers.** Sessions outlive any one model invocation or process.
5. **Explicit authority.** A consumer must know what it may do before doing it.
6. **Event-aware operation.** Services are not only tools to call; they may produce events that change what work is possible.
7. **Minimal prompt dependence.** System state belongs in system state, not repeatedly reconstructed in natural-language prompts.
8. **Provenance by construction.** Decisions, artifacts, capability calls, and handoffs should be attributable and traceable.
9. **No hidden god-service.** Environment is the composition boundary, not the owner of every policy in MNCS.

## Repository layout

```text
.
├── AGENTS.md
├── README.md
├── docs/
│   ├── ARCHITECTURE.md
│   ├── DEVELOPMENT.md
│   ├── INTEGRATIONS.md
│   ├── MODEL.md
│   └── ROADMAP.md
├── examples/
│   └── work-intent.md
├── rfcs/
│   └── 0001-environment-session-model.md
└── src/
    └── README.md
```

The repository is intentionally documentation-first at inception. Runtime and schema choices should follow the architectural contracts rather than accidentally define them.

## Initial entry contract

A useful environment entry should eventually be able to answer, structurally rather than conversationally:

- Where am I?
- What am I here to accomplish?
- What state already exists?
- Which repositories/resources are relevant?
- Which work is protected or already active?
- Which capabilities are available and how do I address them?
- What authority do I have?
- Which events can affect this work?
- What counts as completion?
- What must be persisted so another consumer can continue?

See [ARCHITECTURE.md](docs/ARCHITECTURE.md), [MODEL.md](docs/MODEL.md), and [RFC 0001](rfcs/0001-environment-session-model.md) for the initial contract.

## Status

Foundational architecture and repository scaffold. Implementation should proceed by establishing the canonical data/contracts first, then binding real MNCS services and proving resumable end-to-end sessions.

Licensed under Apache-2.0.
