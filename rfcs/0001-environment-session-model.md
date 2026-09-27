# RFC 0001: Environment and Session Model

- Status: Accepted as foundational direction
- Scope: `mncs-environment`

## Summary

MNCS needs a canonical machine-native entry boundary through which an agent or other consumer can enter a coherent development world without reconstructing system state, service topology, authority, and prior work from natural-language instructions.

This RFC establishes two primary concepts:

1. **Environment** — the resolved working world presented to a consumer.
2. **EnvironmentSession** — the durable scoped relationship between a consumer and that environment while work proceeds.

The repository will compose existing MNCS services through bindings and references rather than reimplement their semantics.

## Motivation

MNCS already separates major concerns across dedicated systems: language/compiler/runtime, planning, orchestration, actions, automation, memory, persistence, rights/provenance, verification, debugging, health, ecosystem knowledge, and others.

That separation is valuable, but it creates an entry problem. A newly attached agent currently needs a large amount of procedural context before it can work safely:

- which repositories matter,
- which revisions/worktrees are active,
- what other agents are doing,
- what work is protected,
- which services exist,
- how those services are invoked,
- what state is authoritative,
- what previous work already happened,
- what events may change the task,
- what authority the agent has,
- what should survive when the agent exits.

Repeatedly encoding that reality into prompts is expensive, stale-prone, difficult to verify, and fundamentally not machine-native.

## Decision

`mncs-environment` will define and implement a canonical environment/session layer with the following properties.

### Environment is resolved, not assumed

An environment is produced from explicit selectors, current authoritative state, available capabilities, authority, and optional prior continuation state.

Requested state and resolved state are distinct. A consumer cannot assume a requested capability, repository, or permission is available until resolution establishes it.

### Sessions are durable

An `EnvironmentSession` is not tied to one model context, process, shell, or machine process lifetime.

The canonical session identity and continuation state must support consumer replacement and recovery.

### Work intent is first-class

A session may carry one or more explicit `WorkIntent` objects describing objectives, constraints, protected scope, priorities, dependencies, and acceptance conditions.

Intent describes the desired outcome. Planning and execution remain delegated to the systems that own those semantics.

### Capabilities are bound, not copied

External MNCS services are represented through `CapabilityBinding` objects that carry stable provider/capability identity, contract revision, addressing, authority requirements, result/event types, availability, and provenance hooks.

Environment must not duplicate provider policy in order to make the provider easier to call.

### Authority is explicit

The session exposes an `AuthorityContext` sufficient for the consumer to determine what it can read, write, execute, or must escalate before acting.

Authority decisions may originate elsewhere, but they must be projected into the environment coherently.

### Events are first-class

Providers may change the state or admissibility of work while a consumer is absent. Environment therefore treats typed events and durable continuation/replay as part of the entry model rather than an optional convenience.

### Checkpoint/handoff is canonical

A checkpoint captures structured continuation state. A handoff makes that state consumable by another compatible consumer.

Natural-language summaries may be generated for convenience but are not sufficient as the canonical continuation representation.

### Environment remains consumer-agnostic

The design must not depend on Codex, Claude, a particular LLM API, an IDE, or a human dashboard. Those are consumers/adapters over the same environment/session contract.

## Ownership boundary

Environment owns:

- environment resolution,
- session identity/lifecycle,
- work-intent attachment,
- workspace projection,
- capability binding,
- authority projection,
- event subscription/correlation,
- environment-specific observations,
- checkpoints and handoffs.

Environment does not own:

- compiler/language semantics,
- planning semantics,
- provider execution semantics,
- generic memory/storage semantics,
- rights policy,
- debugging/health policy,
- Atlas's ecosystem knowledge model,
- model internals,
- IDE behavior.

## Persistence rule

Persist Environment-owned state directly. Reference external authoritative state through stable identities and observed revisions wherever possible.

This prevents Environment from becoming a second stale database of plans, rights, compiler artifacts, memory records, repository state, or verification outcomes.

## Compatibility/versioning rule

The project will maintain one evolving canonical implementation. During the current coordinated MNCS development phase, contract improvements should update affected callers together rather than creating artificial `v1`/`v2`, legacy/current, or compatibility forks.

Formal compatibility layers should be introduced only when a real compatibility boundary exists.

## Consequences

### Positive

- New agents can enter MNCS through a coherent machine-readable boundary.
- Persistent work no longer depends on one model context window.
- Multiple consumers can share the same underlying system model.
- Protected work and authority can become explicit before mutation.
- Service-triggered continuation becomes possible.
- Human dashboards can project the same state rather than invent parallel workflow state.
- Cross-repo integration gaps become visible as concrete contract pressures.

### Costs

- Existing MNCS services may need stronger stable identities, descriptors, events, and revisioned references.
- Session durability requires careful persistence and revalidation semantics.
- Provider adapters must resist the temptation to absorb missing provider behavior.
- Concurrency/protected-work behavior must be modeled rather than left to prompt convention.

These costs are considered necessary because they expose real architectural requirements instead of hiding them in agent instructions.

## Rejected alternatives

### Put this inside Control/Automation/Actions

Rejected because those systems own execution-related concerns. The entry environment must also compose read-only state, memory, repository topology, compiler capabilities, rights, Atlas knowledge, checkpoints, and consumers that may not be executing actions at all.

### Make Atlas the environment

Rejected because Atlas describes the ecosystem and may present it to humans. A session-specific working world and durable consumer continuation are distinct responsibilities. Environment may depend heavily on Atlas knowledge without turning Atlas into an execution/session layer.

### Build an agent-specific IDE/harness

Rejected because the canonical boundary must survive changes in model vendors, agent shells, and human interfaces.

### Keep using large startup prompts

Rejected because prompts are an unsuitable source of truth for mutable system state, authority, service availability, concurrent work, and durable continuation.

## Initial proof target

The first meaningful implementation proof should demonstrate one real session that can:

1. resolve workspace and relevant repository state,
2. bind at least one real MNCS capability,
3. expose explicit authority/protected scope,
4. attach a structured WorkIntent,
5. receive a typed provider result or event,
6. checkpoint,
7. terminate the consumer,
8. resume with another consumer/process,
9. revalidate mutable bindings,
10. continue without reconstructing prior state from prose.

That proof is more valuable than broad scaffolding with no durable end-to-end semantics.
