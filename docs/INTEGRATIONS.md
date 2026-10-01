# MNCS Integration Map

This document describes expected integration direction. It does not transfer semantic ownership into `mncs-environment`.

## Principle

Environment should expose a coherent view of MNCS by binding to authoritative services and artifacts. If a required machine-readable contract does not yet exist, that is an integration pressure on the owning repository—not permission to recreate the missing subsystem here.

## Realized today

Capability discovery consumes repository-owned
`family-semantic-contracts-v1.json` and `.mncs/project.json` manifests from
selected checkouts. Explicit invocation descriptors and the small documented
bootstrap spelling table provide addressing. Source fingerprints are evidence,
never a guessed command. Unaddressed capabilities remain discoverable with a
provider-owned recovery diagnostic. Test and verification inventory commands
are bound with their declared effects and exact toolchain paths.

Definitions may compose read-only JSON status capabilities and mutating
reconciliation capabilities using the [entry contract](ENTRY.md). Environment
verifies the declared provider schema and readiness fields; the provider owns
startup, process identity, duplicate suppression, and recovery semantics.

The vertical proof invokes Atlas's real context capability. The native
integration test and agent dogfood invoke `mncs.test-result/1` through its
selected Language executable and provider-owned wrapper. Store backs durable
session and claim objects; explicit selections persist Store package routing
for fresh processes.

Forge now publishes checkout-owned resident status/reconciliation/stop/work
invocation descriptors. The required-resident sample composes them through the
same generic service model. Status preserves selected versus live startup
provenance and verifies the native supervisor's current Language stream;
reconciliation is asynchronous and provider-owned. Neither Forge commands nor
PID/socket rules are embedded in Environment. See [entry](ENTRY.md) and Forge's
resident provider contract for responsibilities and Linux platform bounds.

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

## Integration pressures recorded

The foundation campaign recorded the pressures it hit in
`pressures/registry.json` (also manageable via `mncs-env pressures`).
Each names the owning repository, evidence, desired contract, effect,
and workaround. New pressures follow the same shape.

## Integration pressures to expect (as found)

Early implementation deliberately discovered and recorded pressures such as:

- capabilities that lack stable semantic identities,
- services that are callable only through CLI/process conventions,
- results that exist only as unstructured logs,
- events that cannot be replayed after consumer downtime,
- state that lacks revision identity,
- authority that cannot be projected at sufficient granularity,
- repository/worktree ownership that cannot distinguish concurrent agents,
- artifacts that cannot be durably referenced across machines/processes.

Those findings are valuable outputs. Do not hide them with Environment-specific semantic substitutes.

## Adaptive Store consumer

Store publishes `adaptive-*` invocation descriptors and a provider-owned local
Environment definition. Select that definition explicitly for adaptive work, or
enter from the Store checkout/subdirectory. Store owns intent encoding, ranking,
codec transformation, integrity, block closure, plans and physical inventory
publication. Environment supplies exact selected runtime paths, effect authority
and session artifact transport; it contains no Store selection policy.

`python3 scripts/adaptive_store_proof.py` exercises actual Environment context,
provider inventory, and durable state as tagged regions in Store. It proves
fresh-process inspection/selective retrieval with an unrelated chunk absent,
exact reconstruction, generation-bound physical evolution, subdirectory reuse
and checkpoint/resume/handoff. See selected Store `docs/provider.md` and
`adaptive-representations` description for discoverable request fields.

Selected Store persistence also suppresses ambient `MNCS_STORE_ARTIFACT` during
retained session opening. A raw precompiled override cannot bypass selected
source/compiler artifact preparation; standalone Store callers retain their
explicit bootstrap override. The caller environment is restored after opening.

Selected Store preparation and invocations share a derived artifact cache under
`STATE_DIR/provider-cache/mncs-store`. Environment supplies writable transport
storage; Store owns source/compiler-bound keys and validates cache contents.
This avoids repeated cold compilation when a sandbox cannot write the home
cache. It introduces no registry or readiness assertion independent of Store.
