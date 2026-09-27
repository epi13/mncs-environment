# MNCS Integration Map

This document describes expected integration direction. It does not transfer semantic ownership into `mncs-environment`.

## Principle

Environment should expose a coherent view of MNCS by binding to authoritative services and artifacts. If a required machine-readable contract does not yet exist, that is an integration pressure on the owning repository—not permission to recreate the missing subsystem here.

## Expected relationships

| MNCS area | Environment needs from it | Environment must not own |
| --- | --- | --- |
| `mncs-language` / `mncs-compiler` | callable/compiler/runtime identities, artifact references, diagnostics, execution-facing contracts where appropriate | language semantics, lowering, runtime semantics |
| Commons | shared identities/contracts/types that are genuinely cross-system | a duplicate private type universe |
| Forge | build/verification capability bindings, build state/events, produced artifact references | build semantics or verification policy |
| RAVEL | plan identities, plan state, obligations, admission-relevant state/events | planning semantics or canonical plan construction |
| Actions | executable action/provider capabilities and results/events | action-family semantics or provider execution logic |
| Control | control-plane capability and lifecycle/event integration | orchestration/control policy |
| Automation | scheduled/triggered work capability and resulting events | automation scheduler semantics |
| Memory | durable memory/context references required by a session | memory storage/retrieval semantics |
| Store | durable object/state references and revisioned retrieval | generic persistence semantics |
| Rights / Provenance | authority grants, restrictions, provenance identities and records | rights decisions or provenance policy |
| Debug | structured diagnosis capabilities/results | diagnosis policy and debugging semantics |
| Doctor | health/readiness capabilities/results | system-health policy |
| Atlas | ecosystem/repository/service knowledge and discoverable topology | Atlas knowledge ownership or dashboard state |
| Test | test capability, results, executable verification references/events | testing semantics already owned by test machinery |
| Models / Learn | consumer/model capability discovery where relevant | model architecture, training, or learning semantics |

Names above describe the intended MNCS project family and may evolve. Integration code should bind through stable contracts/identities rather than assume filesystem adjacency or hard-code repository internals.

## Integration patterns

### Capability discovery

A provider should ideally expose enough structured information for Environment to construct a `CapabilityBinding` without provider-specific prompt instructions.

Preferred properties:

- stable provider identity,
- stable capability identity,
- contract/schema identity or revision,
- invocation/addressing information,
- authority requirements,
- availability/health information,
- result and event contracts,
- provenance hooks.

### Event integration

A provider that can change the admissibility or state of active work should expose typed events or a durable change feed.

Environment should avoid requiring every consumer to poll raw Git state, parse logs, or repeatedly query unrelated services to notice meaningful transitions.

### Session correlation

Where useful, capability invocations should carry a session/provenance correlation handle. This allows service-owned results and events to be associated with a session without making the provider dependent on Environment's internal implementation.

### Artifact and state references

Prefer stable, revisioned references:

```text
session -> plan reference -> RAVEL-owned plan
session -> compiler artifact reference -> compiler/runtime-owned artifact
session -> memory reference -> Memory-owned record
session -> rights grant reference -> Rights-owned authority
```

Avoid:

```text
session -> copied plan semantics
session -> copied compiler metadata with no revision identity
session -> private shadow memory
session -> local reimplementation of rights decisions
```

## Atlas versus Environment

Atlas answers ecosystem questions such as what projects/services exist, their relationships, and the broader state of MNCS.

Environment uses that knowledge to construct the particular working world required by a session.

A useful shorthand:

```text
Atlas:       What exists and how is the ecosystem shaped?
Environment: What does this consumer need to see and use right now?
```

A future Atlas dashboard may render active Environment sessions, but it should not require a second dashboard-specific session model.

## RAVEL / Control / Actions / Automation versus Environment

Environment carries intent and exposes capability. It does not decide every step of execution.

Conceptually:

```text
WorkIntent
    |
    v
EnvironmentSession
    |
    +-> planning/admission through RAVEL/control-plane machinery
    +-> execution through Actions/Forge/compiler/etc.
    +-> recurring/triggered continuation through Automation where appropriate
    |
    <- typed results/events return to session
```

The exact execution path is owned by those systems and may change without changing the fundamental Environment contract.

## Integration pressures to expect

Early implementation should deliberately discover and record pressures such as:

- capabilities that lack stable semantic identities,
- services that are callable only through CLI/process conventions,
- results that exist only as unstructured logs,
- events that cannot be replayed after consumer downtime,
- state that lacks revision identity,
- authority that cannot be projected at sufficient granularity,
- repository/worktree ownership that cannot distinguish concurrent agents,
- artifacts that cannot be durably referenced across machines/processes.

Those findings are valuable outputs. Do not hide them with Environment-specific semantic substitutes.
