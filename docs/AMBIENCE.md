# MNCS tooling ambience inventory

Which subsystems the environment keeps true on the agent's behalf, and
which ones still need deliberate work. This is the map for systematic
Spark campaigns through Language Service, Actions, Memory, and RAVEL.

Ambience scale:

```text
A0 explicit
    requires deliberate invocation and intent

A1 discoverable
    environment exposes the capability automatically

A2 reactive
    automatically reacts to relevant events, then rests

A3 continuous coherence
    environment keeps derived state and evidence current

A4 adaptive ambient
    strategy changes based on validated history and context
```

Higher is not better in itself. Each subsystem should sit at the level
its ownership justifies: debugging is reactive (A2) because healthy
executions need no debugger; health is continuous (A3) because drift is
constant. The wrong level in either direction is a bug.

## Current map

### Doctor — A3, implemented

- Owns: operational health and remediation coherence.
- Trigger: entry, re-entry, dirty-state events, explicit reconcile.
- Identity: health epoch over service observations and provider state.
- Evidence: remediation envelopes, evidence artifacts outside context.
- Agent surface: terse `repaired/reconciled/degraded/blockers` counts.
- Mutation: safe automatic repair plus bounded claim-gated
  reconciliation; destructive or ambiguous work escalates.
- Explicit-only: repository mutation, destructive repair, adoption.
- Status: closed for the current phase. Do not reopen without a
  genuine regression.

### Projections — A3, implemented

- Owns: derived-information coherence (rendered docs, media, views).
- Trigger: entry, input changes, verification outcomes, `--watch`.
- Identity: projection rows bound to source subject and input digest.
- Evidence: immutable content-addressed rows, compare-and-swap
  adoption, verification-gated application.
- Agent surface: `current/pending/reconciled` counts.
- Mutation: deterministic rendering; application under claims with
  real verification gating.
- Explicit-only: region splicing under claim, new projections.
- Status: hardened (shared rows, race-safe adoption, resident watch).

### Verification (mncs-test) — A3, implemented

- Owns: test and verification-evidence coherence.
- Trigger: entry, revision/dirty/toolchain/obligation changes.
- Identity: obligation definition, subject fingerprint, executor,
  toolchain, inventory, repository generation.
- Evidence: per-session identity-bound records plus retained
  `mncs.test-result/1` documents.
- Agent surface: `current/executed/failed` counts; tiny failure deltas.
- Mutation: none. Read-only execution; snapshots and goldens never
  update ambiently.
- Explicit-only: full suite, snapshot updates, new obligations.
- Status: closed for the current phase. Known gap: cross-session
  reuse needs the unified evidence identity first
  (`prs_verify_xsession_dedup`); unsupported executor kinds belong to
  Actions/CI, compiler runners, or manual verification, not mncs-test.

### Diagnostics (mncs-debug) — A2, implemented

- Owns: failure-explanation and debugging-evidence coherence.
- Trigger: structured verification FAILs, explicit depth requests.
- Identity: failure key over obligation, test case, subject content,
  request digest, and toolchain.
- Evidence: session-private witnesses bound to the failure; full
  traces stay in artifacts.
- Agent surface: absent when healthy; tiny failure capsule otherwise.
- Mutation: none to repositories. Bounded re-execution under the
  `verify` effect with session-scratch confinement.
- Explicit-only: standard/deep depth, replay, minimization.
- Status: implemented (see `docs/DIAGNOSTICS.md`). Never changes a
  test verdict. Shared exact-witness reuse is future work behind the
  same unified evidence identity as verification.

### Store — A3 substrate, implemented

- Owns: durable session state, immutable evidence, content addressing.
- Trigger: every session read and write.
- Identity: content digests, immutable versions, session/claim keys.
- Evidence: the substrate other evidence lives on.
- Agent surface: none directly; latency visible on entry.
- Mutation: state writes through session authority only.
- Explicit-only: backend selection, recovery.
- Status: adaptive compression and environment integration merged.
  Remaining entry latency belongs to Store, not its consumers.

### Rights, claims, authority — A3 substrate, implemented

- Owns: who may do what, where, under which claim.
- Trigger: every invoke, mutation, claim, and handoff.
- Identity: sessions, consumers, claim scopes, effect declarations.
- Evidence: authority verdicts, claim records, invocation events.
- Agent surface: denials and escalations with reasons.
- Mutation: none itself; gates all mutation including `verify`-effect
  execution (foreign conflicts deny, subject dirt allowed).
- Explicit-only: grants, adoption, transfers.
- Status: stable. New capabilities declare effects; enforcement is
  shared, not per subsystem.

### Forge — A1/A2 executor, composed

- Owns: bounded execution and orchestration.
- Trigger: declared executors, assurance workflows, CI requests.
- Identity: executor revisions, obligation bindings, receipts.
- Evidence: execution receipts, assurance vectors.
- Agent surface: readiness and receipts through bindings.
- Mutation: bounded provider execution under declared effects.
- Explicit-only: workflow definition, deployment-shaped actions.
- Status: bound through manifests and contracts
  (`mncs.assurance-workflow/1`). No dedicated ambience campaign
  needed: Forge executes what others decide. Keep it that way.

### Automation — A2, composed

- Owns: adoption, reconciliation, and revisit decisions.
- Trigger: explicit adoption/reconciliation requests, revisit plans.
- Identity: automation capabilities bound per session.
- Evidence: reconciliation records.
- Agent surface: capability bindings and reconcile outcomes.
- Mutation: owned reconciliation under authority.
- Explicit-only: adoption of unknown work.
- Status: composed through bindings. No dedicated campaign planned;
  revisit in the Sol cohesion pass if resident scheduling converges.

### Language Service — A0, next campaign

- Owns: semantic perception (symbols, dependencies, impacts,
  obligations, source and debug bindings).
- Trigger today: explicit MCP and client queries only.
- Identity: compiler-owned declaration, span, and operation
  identities; `debug_source_binding` already exists.
- Evidence: resident workspace events, semantic snapshots.
- Agent surface today: full client responses on demand.
- Mutation: none; read-only semantic index.
- Explicit-only: workspace subscription scope, index rebuilds.
- Status: no environment provider contract yet
  (`family-semantic-contracts-v1.json` absent), so the environment
  cannot bind it. A resident workspace event service exists but is
  not environment-integrated.
- Target: A3 semantic coherence. The dedicated campaign should make
  source rereading and impact reconstruction disappear behind
  resident snapshots the environment already trusts.
- Why next: every later campaign (Actions routing, Memory capsules,
  Debug enrichment, RAVEL planning) consumes semantic identities.
  Bind the producer before the consumers.

### Actions — A2 reactive external-evidence orchestration (landed)

- Owns: GitHub and CI execution plus evidence transport.
- Trigger: missing/stale external evidence for a published subject.
- Identity: repository, revision, workflow, artifact, check identity.
- Evidence: execution receipts, evidence manifests, check-results
  admitted per exact subject; retained under session actions artifacts.
- Agent surface: tiny `actions` block only when something needs
  attention (pending, eligible delegate requests, failures); full
  trail on demand.
- Mutation: CI-side execution only; never local sources, never
  commits/pushes/branches.
- Explicit-only: dispatch (delegate grant via repository claim),
  deployment/release/publication-shaped effects, workflow changes,
  new actions, secret handling.
- Status: reactive. `mncs.actions-external-evidence/1` (native
  policy), `mncs.actions-remote-evidence/1` (read transport), and
  `mncs.actions-dispatch/1` (delegate transport) are bound
  environment capabilities; ambient passes reuse exact receipts,
  subscribe to in-flight runs, and admit completed outcomes.
  RAVEL remains an optional plan producer, not a requirement.
- Pressure: most `external_integration` obligations still declare
  no external route (local harnesses without workflows, vacuous or
  partial entrypoints); owners must declare honest bindings or
  reclassify (see pressures registry).

### Memory — A0, after Actions

- Owns: validated continuity across sessions.
- Trigger today: none ambient; explicit corpus and tool use.
- Identity: capsule identities over validated knowledge (to be
  established; must reuse compiler and semantic identities, not
  invent parallel ones).
- Evidence: corpus, provenance and replay records.
- Agent surface today: whatever the agent explicitly asks for.
- Mutation: capsule retention under provenance, never silent
  history rewrites.
- Explicit-only: what counts as validated, retention scope.
- Status: design-heavy, integration-light. No provider contract,
  no environment binding.
- Target: A3 continuity coherence: preserve validated continuity,
  retrieve only relevant compact capsules, prevent reconstruction
  of already-established knowledge. Explicitly not a context dump.
- Why after Actions: continuity spans local and CI outcomes; the
  transport boundary should exist before Memory promises
  cross-boundary recall.

### RAVEL — A1, last

- Owns: adaptive evidence strategy and learning.
- Trigger today: explicit planning requests.
- Identity: `mncs.verification-plan/1`,
  `mncs.verification-obligation-plan/1`.
- Evidence: obligation plans consumed structurally by verification
  where available.
- Agent surface today: plans on demand.
- Mutation: none ambient; planning only.
- Explicit-only: strategy changes, learning scope, interventions.
- Status: planning provider, deliberately not ambient. The
  surrounding tools now emit the structured evidence RAVEL will
  need: validation results, remediation results, verification
  verdicts with failure classes, diagnostic witnesses with source
  bindings, health and readiness changes, and session evidence
  JSONL streams.
- Target: A4 adaptive ambient, only after Language Service,
  Actions, and Memory land. RAVEL held back is a schedule
  decision, not a gap: adaptation over weak facts would be worse
  than no adaptation.

## Campaign ordering

```text
done:
    doctor          A3 health coherence
    projections     A3 derived-information coherence
    mncs-test       A3 verification coherence
    mncs-debug      A2 diagnostic coherence
    store           A3 state substrate
    rights          A3 authority substrate

next:
    language-service    A0 -> A3 semantic coherence

then:
    actions             A1 -> A2 reactive CI orchestration

then:
    memory              A0 -> A3 continuity coherence

last:
    ravel               A1 -> A4 adaptive evidence strategy
```

Change this order only with repository evidence that another
sequence composes better. In particular, do not start RAVEL
adaptation before the fact-producing campaigns land: the ambient
layers above exist so that RAVEL reasons over structured evidence,
never over prose or reconstructed state.

## Shared primitives to reuse, not rebuild

Every ambient subsystem so far converges on the same small set:

- stable identity-bound records, never wall-clock keys;
- epochs over measured inputs with exact invalidation;
- native policy modules for reuse, selection, and classification;
- host transport that measures facts and executes admitted effects;
- session evidence JSONL for machine-readable handoff;
- terse summaries in context, full trails on demand;
- bounded budgets with honest deferred states;
- fail-closed unknowns that never become silent passes.

A future campaign that needs another cache, event, or evidence
architecture should first check whether these primitives already
solve the generic part. Generalize only demonstrated shared
invariants; the Sol cohesion pass owns cross-subsystem scheduling
if resident loops proliferate.
