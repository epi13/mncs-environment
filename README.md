# mncs-environment

Canonical machine-native development environment for assembling work intent, repository state, services, capabilities, triggers, memory, authority, provenance, and execution context into a coherent entry point for agents and other MNCS consumers.

Enter from this checkout (or a subdirectory):

```bash
./scripts/mncs-env enter --consumer my-agent
```

Entry discovers `.mncs/environment.json`, inspects its bounded selection of
MNCS providers, creates or reuses durable work, reconciles declared services,
and returns JSON context with identity, projects, toolchain, readiness, and
callable actions. Repeat the same command to continue. No PATH installation
or setup environment variables are required in the standard sibling layout.
See [the entry contract](docs/ENTRY.md) for selection, recovery, and provider
readiness declarations. To require resident Forge, use
`./scripts/mncs-env enter --consumer my-agent --definition samples/forge-resident.environment.json`.
The selected Forge must publish its resident descriptors and any unowned
worktree needs an explicit claim before reconciliation can start it.

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
├── examples/
│   ├── work-intent.md
│   ├── development-environment/environment.json
│   └── compiler-campaign/environment.json
├── mncs_env/            # canonical implementation (stdlib-only Python)
├── pressures/registry.json
├── rfcs/
├── scripts/mncs-env     # consumer entry point
├── scripts/vertical_proof.py
└── tests/
```

## Implementation status

Sessions are Store-backed persistent objects: structured snapshots plus an
append-only event log, so a different process or consumer resumes from
state, not prose. The hardening campaign added:

- versioned workspace claims replacing advisory leases (`mncs_env/claims.py`);
- ownership/acquisition authority with tri-state enforcement (`mncs_env/authority.py`);
- rights/provenance gate over `mncs-rights-provenance` (`mncs_env/rights.py`);
- provider-declared `invocation` addressing with toolchain env (`mncs_env/capabilities.py`);
- generation-based Store-feed observer: own writes re-baseline, external
  advances become events (`Session.observe_store`);
- read-only inspect plus a control-mcp tool surface (`env_enter`, `env_inspect`,
  `env_resume`, `env_claim_acquire`, `env_claim_release`, `env_claims`, `env_status`).

## Entering an environment

The checked-in development definition selects Environment, Language,
Compiler, Test, Store, and Forge from the sibling workspace without inspecting
unselected repositories. Only Environment is in its writable intent;
provider mutations still pass through claims and authority. Availability and
readiness are observations, not permission to modify a checkout.

```bash
./scripts/mncs-env enter --consumer my-agent
./scripts/mncs-env status <session>       # historical, read-only context
./scripts/mncs-env health <session>       # live read-only probes
./scripts/mncs-env reconcile <session>    # refresh, recover, verify
./scripts/mncs-env capabilities <session>
./scripts/mncs-env invoke <session> <capability> -- <args...>
./scripts/mncs-env checkpoint <session> --progress "..." --remaining ...
./scripts/mncs-env resume <session> --revalidate
./scripts/mncs-env handoff <session> --to other-consumer --next ...
./scripts/mncs-env complete <session> --outcome "..."
```

Use the returned `actions.*.argv` directly when changing directories or using
an isolated state root: each action preserves the executable, state directory,
and persistence backend. `inspect` exposes detailed state; `status` and
`context` are compact snapshots. Lifecycle `active` does not assert readiness.
Optional provider gaps yield `degraded` readiness; required gaps yield
`blocked` readiness and exit status 5 while preserving the session for recovery.

Outside a declared project, entry establishes a read-only orientation session
for the nearest Git checkout or explicit `--workspace`. It never infers a
family root from sibling directories. Explicit definitions remain supported:

```bash
./scripts/mncs-env --state-dir "$MNCS_ENV_STATE" enter \
    --definition examples/compiler-campaign/environment.json \
    --workspace "$MNCS_CAMPAIGN_ROOT" --consumer my-agent
```

Compiler campaigns deliberately require an isolated root and provider-owned
worktree selection. A broad root without an explicit bounded repository
selection is rejected before Git discovery or provider invocation.

Run the real-provider persistence/handoff proof with
`python3 scripts/vertical_proof.py`. It creates temporary scoped clones of
Atlas and Language by default; `MNCS_VERTICAL_WORKSPACE` can select an existing
isolated proof workspace. Run regressions with `python3 -m pytest tests/ -q`.
The campaign proofs are `python3 scripts/campaign_proofs.py`.

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

Implemented and proven: the vertical proof resolves a real workspace,
invokes a real provider capability, enforces protected scope,
checkpoints, resumes in a new process, hands off across consumer
identities, and completes; the campaign proofs (`python3
scripts/campaign_proofs.py`) cover two-agent conflicts, bypass
fail-closed behavior, restart missed-event recovery, live
language-service deltas, health, idle/resource bounds, brief/ack
context, session reuse, and memory protection. Sessions persist on the
canonical Store backend and a background reconciler maintains them with
durable cursors. Remaining work is broader bindings, provider push
events, and Memory/rights integration as owning repositories publish
the needed contracts (see pressures).

Licensed under Apache-2.0.
