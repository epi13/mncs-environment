# Architecture

## Purpose

`mncs-environment` is the composition boundary between a consumer and the wider MNCS system. Its job is to resolve a coherent working world, establish a persistent session, expose bound capabilities under explicit authority, and maintain enough state for work to continue across events, failures, and consumer handoffs.

It should make entering MNCS feel like entering a system rather than reading instructions about a collection of repositories.

## High-level lifecycle

```text
consumer requests entry
        |
        v
resolve Environment
  - workspace
  - relevant repositories/resources
  - authoritative state/revisions
  - available services/capabilities
  - authority constraints
  - event sources
        |
        v
create/resume EnvironmentSession
        |
        v
attach WorkIntent
        |
        v
planning/admission/execution through bound MNCS services
        |
        +-----------------------+
        |                       |
        v                       v
results/artifacts           events/triggers
        |                       |
        +-----------+-----------+
                    v
          update session state
                    |
                    v
        checkpoint / handoff / complete
```

The environment does not replace the planner, executor, compiler, memory service, rights service, or diagnostics stack. It assembles their usable interfaces around the current session.

## Primary responsibilities

### 1. Environment resolution

Resolve the smallest coherent working world needed for the requested work. Resolution may consider:

- workspace identity,
- repository/worktree state,
- active or protected work,
- relevant persisted MNCS state,
- available service endpoints or local capabilities,
- requested intent,
- authority grants and restrictions,
- known event sources,
- previous checkpoints or handoffs.

Resolution should avoid copying authoritative external state when a stable reference and observed revision are sufficient. Workspace selection is
an explicit boundary: campaign definitions must receive a campaign-scoped
root, and discovery is bounded before provider capabilities are considered.
Slow or incomplete discovery is surfaced as structured resolution
diagnostics rather than a partial environment.

### 2. Session establishment

Create or resume a durable `EnvironmentSession` with stable identity. A session is not equivalent to a process, shell, chat, or model invocation.

A session must be able to survive:

- model/context replacement,
- process restart,
- machine restart where backing services persist,
- temporary service unavailability,
- handoff from one compatible consumer to another.

### 3. Capability binding

Environment turns available external services into session-scoped `CapabilityBinding` objects.

Bindings should answer at least:

- what capability exists,
- which service owns it,
- how it is addressed,
- what revision/contract was observed,
- what authority is required,
- whether it is currently available,
- which events/results it can emit,
- how invocation provenance is attached to the session.

Bindings must not contain duplicate business semantics from the provider.

### 4. Authority projection

Rights and policy may be owned elsewhere, but Environment must project the effective authority into the session so the consumer can reason before acting.

The session should distinguish between:

- readable resources,
- writable resources,
- executable capabilities,
- protected resources/worktrees,
- actions requiring escalation or approval,
- explicitly prohibited actions.

### 5. Event connection

Environment is event-aware. A capability may be useful because the consumer can call it, because it can emit events, or both.

Events should be typed and attributable. Examples:

- repository state changed,
- verification completed,
- execution failed,
- dependency became available,
- pressure was recorded,
- protected work changed,
- new artifact was published,
- planned work became admissible.

Environment should support event replay or an equivalent durable continuation mechanism so a temporarily absent consumer does not have to rediscover changes manually.

### 6. Checkpoint and handoff

A checkpoint captures the minimum durable continuation state necessary to resume coherently. It should reference, rather than blindly duplicate, authoritative state.

A handoff additionally records consumer-facing continuation information such as:

- current intent and progress,
- unresolved decisions,
- important observations,
- artifacts produced,
- event cursor or continuation point,
- currently bound capabilities,
- authority snapshot/references,
- state that must be revalidated on resume.

Natural-language summaries may accompany a handoff, but they are not the canonical state.

### First-use context and inspection

The canonical entry operation discovers a local definition, resolves its
workspace selection, selects existing work by definition/workspace/consumer,
reconciles provider readiness, and returns a bounded context. Explicit
`--new-session` starts independent work; ambiguous matches require explicit
resume. Entry and explicit reconciliation serialize across processes within
a state directory. Session identity and continuation remain Store-owned
persistent records; the file lock is only bootstrap exclusion.

Context contains environment/session identities, configuration provenance,
workspace/project facts, selected toolchain, effective authority, capability
availability, readiness observations, and executable action argv. `status`
and `context` project persisted observations without advancing cursors.
`health` probes live substrate and declared provider capabilities without
changing session records. `reconcile` refreshes discovery, invokes provider
recovery under session authority when needed, then verifies readiness.

Readiness is independent of lifecycle: an active durable session can be
blocked or degraded. Executable presence verifies substrate only. Provider
JSON schemas and declared readiness predicates verify actual capabilities.
Probe and recovery budgets bound entry cost. An incomplete workspace rescan
preserves the last complete view rather than publishing missing repositories
as if they had been removed. Optional unavailable capabilities remain visible
and do not block useful work.

Workspace selection can be a Git checkout, a bounded directory of checkouts,
or an explicit list of immediate repository directories. Explicit selection
avoids unrelated discovery in a large family root. Provider-managed compiler
campaigns retain their existing isolated root and exact worktree closure;
static selection does not replace campaign provisioning.

See [ENTRY.md](ENTRY.md) for the observable contract and provider declaration.

## Dependency direction

Environment should depend on stable contracts exposed by MNCS services. Other services should not need to depend on Environment merely to function.

The exception is optional environment-aware integration: a service may accept a session/provenance handle or emit session-addressable events without making Environment its semantic owner.

```text
consumer -> Environment -> service contracts
                         -> service references
                         -> event sources

service semantics remain in service repositories
```

## Persistence model

Environment state should be split conceptually into three categories:

1. **Owned state** — session identity, intent attachment, bindings, checkpoints, handoffs, continuation cursors, and environment-specific observations.
2. **Referenced authoritative state** — repository revisions, RAVEL plans, compiler artifacts, memory records, rights grants, verification results, Atlas facts, etc.
3. **Ephemeral observations** — availability, process endpoints, transient health data, or cacheable discovery results that must be revalidated.

This distinction prevents Environment from becoming a stale shadow copy of MNCS.

## Determinism and revalidation

Given identical authoritative inputs and policy, environment resolution should be deterministic wherever practical. When nondeterminism is unavoidable, the selected result and basis should be persisted for provenance.

Resuming a session does not mean trusting every old observation. The resume path should explicitly revalidate time-sensitive or revision-sensitive bindings while preserving the durable history of what the session previously observed.

The ambient Doctor pass (`docs/DOCTOR.md`) implements this as validated
health epochs: entry fingerprints every readiness input exactly and skips
revalidation only when the fingerprint proves the world unchanged, while
declared services are always probed live. Any doubt runs the full path.

## Failure model

Environment should distinguish between:

- **resolution failure** — a coherent environment cannot be constructed,
- **binding degradation** — one or more capabilities are unavailable or stale,
- **authority denial** — requested action exceeds session authority,
- **continuation conflict** — authoritative state changed incompatibly since checkpoint,
- **consumer failure** — the attached consumer disappeared while the session remains recoverable,
- **provider failure** — a bound service failed and should surface its own typed result/diagnostic where possible.

Environment should preserve enough structured failure state that Debug/Doctor or another consumer can inspect what happened without reconstructing it from logs alone.

## Human interfaces

A human dashboard may render Environment and EnvironmentSession state, but the dashboard is a view, not the source of truth. The same session objects should be usable by agents, automation, and humans without parallel state models.

## Shared family coherence

The current cross-service authority and identity map is documented in
[WHOLE_SYSTEM_COHERENCE.md](WHOLE_SYSTEM_COHERENCE.md). Family operational rows
use the selected session Store; file persistence remains an explicit debug
projection. A family ChangeSet is shared semantic intent, while a reconciliation
is adoption by one selected physical checkout. Another worktree establishes its
own repair and proof. Repository-wide adoption must not imply checkout-wide
adoption.

Architectural consumer edges come from Commons' validated declaration reader.
Language Service supplies observed semantic impact separately. Source-import
validation is an explicit Commons audit, never a repeated Environment discovery
pass and never architectural authority by itself.

## Incremental entry composition

Canonical entry routes observed changes through Automation native subscriptions
and resumes Store-backed owner observations. See [Incremental ambient coherence](INCREMENTAL_COHERENCE.md)
for invalidation, recovery, cursor races, artifact observation and the proposed
compiler materialization boundary. Provider build origin and cross-session
evidence equivalence remain explicit unknowns.
