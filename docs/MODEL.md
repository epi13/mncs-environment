# Core Model

This document names the initial conceptual objects. It is intentionally implementation-language neutral.

## EnvironmentDescriptor

Describes the requested or named environment before resolution.

Suggested fields:

- stable environment identity or selector,
- workspace selector,
- requested scope,
- optional prior session/checkpoint,
- requested capability classes,
- consumer metadata,
- requested work intent reference.

Invariant: a descriptor requests a working world; it does not pretend that requested capabilities or authority already exist.

## Environment

The resolved working world presented to a consumer.

Suggested fields:

- environment identity,
- resolution timestamp/revision,
- workspace view,
- relevant resource/repository references,
- active/protected work references,
- capability bindings,
- authority context,
- event-source bindings,
- persisted-state references,
- provenance for how the environment was resolved.

Invariant: authoritative external facts should be referenced with observed revisions rather than copied without ownership.

## EnvironmentSession

A durable scoped relationship between a consumer and a resolved environment.

Suggested fields:

- session identity,
- environment identity/revision,
- consumer identity/type,
- lifecycle state,
- attached work intents,
- active capability bindings,
- authority snapshot/references,
- event cursor/subscriptions,
- produced artifact references,
- decision/observation records,
- checkpoint chain,
- handoff state,
- creation/update provenance.

A session is not a chat transcript, process ID, shell session, or model context window.

## WorkIntent

Declares what outcome is desired without embedding the implementation plan.

Suggested fields:

- intent identity,
- objective,
- desired outcomes,
- acceptance conditions,
- constraints,
- protected scope,
- priorities,
- relevant resource selectors,
- dependencies,
- completion policy/reference,
- provenance.

Intent may be decomposed or planned by other services, but the original declared intent remains attributable.

## WorkspaceView

A session-relevant view of physical and logical development state.

Suggested fields:

- workspace identity,
- roots/worktrees,
- repository identities and revisions,
- branch/head state,
- dirty-state observations,
- active locks/protections,
- artifact/cache references where relevant,
- observed-at metadata.

Environment should not become a Git implementation. Workspace providers remain responsible for repository operations.

## CapabilityBinding

Binds a provider-owned capability into the current environment/session.

Suggested fields:

- binding identity,
- capability identity/type,
- provider/service identity,
- provider contract revision,
- addressing/invocation reference,
- availability/health observation,
- authority requirements,
- supported event/result types,
- provenance requirements,
- observed-at/revalidation metadata.

Invariant: the binding describes access to semantics owned elsewhere.

## AuthorityContext

Projects effective authority into the session.

Suggested fields:

- authority subject,
- readable scopes,
- writable scopes,
- invocable capabilities,
- protected scopes,
- escalation-required actions,
- denied actions,
- grants/policy references,
- validity/revalidation metadata.

Invariant: absence of a grant must not silently become permission.

## EventSubscription

Connects the session to typed state changes from providers.

Suggested fields:

- subscription identity,
- source/provider,
- event types,
- filters/scope,
- durable cursor or replay reference,
- delivery/acknowledgment state,
- session correlation identity.

## EnvironmentEvent

A typed observation delivered into a session.

Suggested fields:

- event identity,
- event type,
- producer,
- source revision,
- session/environment correlation,
- payload/reference,
- causal/provenance references,
- timestamp/order metadata.

Events should be usable without scraping prose logs.

## Checkpoint

A durable continuation boundary.

Suggested fields:

- checkpoint identity,
- session identity,
- attached intent/progress references,
- environment revision observed,
- binding snapshot/references,
- event cursor,
- produced artifacts,
- unresolved decisions,
- revalidation requirements,
- provenance.

A checkpoint should make resume cheap, but it must not freeze external services into stale shadow copies.

## Handoff

A checkpoint intended for a potentially different consumer.

Suggested fields:

- checkpoint reference,
- outgoing consumer,
- expected/eligible incoming consumer class,
- structured continuation notes,
- unresolved blockers,
- next admissible actions if known,
- natural-language summary as optional presentation data.

## Pressure

Environment should be able to surface integration pressures without owning the external fix.

Examples:

- a service has no stable machine-addressable capability identity,
- a provider emits logs but no typed completion event,
- rights cannot yet express a required protected scope,
- workspace state cannot identify another agent's active worktree safely,
- a checkpoint cannot reference a required artifact durably.

Pressure records should identify the owning system and the missing contract rather than introducing a workaround that duplicates that system here.

## Lifecycle sketches

Environment session lifecycle:

```text
requested -> resolving -> active -> checkpointed -> active -> completed
                         |            |
                         |            +-> handed_off -> active
                         |
                         +-> degraded
                         +-> blocked
                         +-> abandoned/recoverable
```

These names are conceptual. Implementation should choose lifecycle states only once the required transitions and invariants are testable.
