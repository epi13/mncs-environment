# RFC 0002 — Session Persistence, Authority, and Composition Boundaries As Built

Status: accepted; updated to reflect the current implementation. Git retains the foundation decisions.

RFC 0001 defined the conceptual session model. This record fixes the
implementation choices made while building it, so future work evolves the
same design instead of relitigating it.

## 1. Python host implementation with JSON contracts

The canonical model is `mncs_env/` (Python, stdlib only) with JSON
contracts (`mncs.environment.*/1`). Host code owns discovery, transport,
serialization, Git inspection, and adapter plumbing only. No provider
semantics live here; `scripts/vertical_proof.py` proves the boundary by
invoking repository-owned code.

A native MNCS representation is recorded as a pressure, not forced onto
immature language features.

## 2. Content-hash identities, nonce-bound sessions

Environment/resolved/intent/checkpoint/handoff/event identities are
`sha256` over canonical JSON (`prefix_16hex`). Sessions bind a random
nonce so identical inputs never collide; the nonce is persisted, keeping
identities reproducible from stored records. No paths, PIDs, or ports in
identities.

## 3. Store-backed persistence with explicit bootstrap routing

Environment owns session/event/checkpoint/claim meaning. Store owns durable
objects, immutable identities, generation CAS, verification, and commit feeds.
Store is canonical; `session_store.py` retains a file debug projection.
State defaults to `~/.local/share/mncs-environment`. Selected Store package
routing is persisted outside Store so a new consumer can reopen the intended
checkout. Entry reuse reads durable session records rather than maintaining
an independent active-session registry.

## 4. Authority is pure, default-deny, and event-logged

`authority.evaluate()` projects intent constraints, protected scope, observed
workspace facts, and scoped claims into allow/deny/escalate. Denial and
escalation never spawn provider processes. Sensitive invocation effects
require the corresponding action authority. Rights/provenance evaluation
composes the owning provider's host gate; unknown rights remain explicit.

## 5. Claims preserve scoped concurrent work

`claims.py` records versioned repository/worktree/path scopes, holders,
acquisition basis, adoption, liveness, release, and transfer. Claims are
Store-backed. Foreign branch, dirty tree, and linked worktree observations
remain protection evidence. Family-wide ownership outside Environment is a
Control integration pressure; Environment does not adopt other agents' work
as an entry or recovery ritual.

## 6. Provider addressing is explicit

Bindings come from repository-owned semantic declarations, project manifests,
and test/verification inventories. Invocation blocks carry addresses, fixed
argv, effects, and exact toolchains. A small tested bootstrap table retains
known entrypoint spellings and Atlas's proven context adapter. Fingerprint
sources are evidence only: arbitrary source modules are not guessed tools.
Unaddressed contracts remain discoverable with provider-owned diagnostics.

Repository roots discover themselves. Explicit bounded selection composes
immediate providers from a large family workspace without touching unrelated
repositories. Provider-managed campaigns retain their own exact worktree
selection. Availability checks executable permissions and toolchain presence;
provider readiness is a separate structured contract.

## 7. Events and reconciliation compose provider observations

Subscriptions track session log cursors for replay. Git, Store, Commons, and
Language sources produce bounded observations; the background reconciler
persists source cursors across restart. Reconciler sessions are scoped to
their workspace. Provider push feeds remain an owning-service pressure.

Canonical foreground entry selects/reuses work, refreshes discovery, probes
provider JSON readiness, delegates declared recovery through normal authority,
and verifies again. Status is historical; live health is read-only. Entry and
foreground reconciliation serialize with a process lock; external providers
own process supervision and idempotence. Lifecycle and readiness are separate.
See [ENTRY.md](../docs/ENTRY.md) for the consumer/provider contract.

## 8. Lifecycle is an enforced machine, not a string

`defined -> resolving -> ready -> active -> blocked|waiting|checkpointed|
handed_off|completed|failed|abandoned`, with explicit allowed-transition
map and per-transition reasons in history. Illegal transitions raise.
Terminal states refuse resume; `abandoned` may return to `active`.

## 9. Handoff transfers identity, not prose

Handoff = checkpoint + transfer record; the receiving consumer accepts
explicitly, history persists, authority subject switches. Verified by the
vertical proof across consumer identities and OS processes.

## 10. `src/` removed

The scaffold's empty `src/` landing zone is superseded by `mncs_env/`.
One implementation location, per the no-parallel-paths rule.
