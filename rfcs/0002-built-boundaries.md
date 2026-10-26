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

`merge` and `publish` are claim-required actions, not unconditional approval
gates. They require declared writable intent and a current session claim that
covers the exact destination checkout or narrower path. Selected-checkout
resolution supplies that destination when the caller names only the
repository; explicit scopes remain available for a separate merge target.
An already-writable repository may bind a different Control-verified registered
checkout without a second owner token. This changes the checkout selected for
existing repository authority, not the repository set. New repository scope
still requires an exact owner-approved grant. Claims continue to fence the
selected destination, and overlapping foreign claims deny mutation.
When an isolated campaign needs a destination outside its checkout directory,
Environment accepts only a registered worktree anchored to the selected
checkout's same Git common directory and repository manifest identity; it does
not widen authority to unrelated repositories or symlink aliases.
Overlapping foreign claims deny the operation, while independent worktrees
remain eligible to proceed. `delete` remains escalation-gated.

Local Git retirement is a separate `retire` action. It requires declared
writable intent and a current session-owned claim covering the requested
repository or worktree scope, in addition to the retirement adapter's Git
safety checks. General `delete` remains escalation-gated and is not implied by
a retirement claim.

## 5. Claims preserve scoped concurrent work

`claims.py` records versioned repository/worktree/path scopes, holders,
acquisition basis, adoption, liveness, release, and transfer. Claims are
Store-backed. Foreign branch, dirty tree, and linked worktree observations
remain protection evidence. Family-wide ownership outside Environment is a
Control integration pressure. A normal claim follows the session's exact
selected worktree and is nonexclusive; repository-wide scope is explicit for
mutations that genuinely span the repository. Disjoint worktrees can proceed
independently. Across different checkouts, overlapping paths conflict only
when both claims name the same attached branch; detached worktrees have
private `HEAD` identities and do not share a branch reference. The same
checkout and repository-wide scopes still fence overlapping writers. If a
same-operator claim blocks an ordinary selected-worktree acquisition, Control fences the
owner, checks host processes, jobs, grants, and user services, then atomically
retires the old claim and transfers its scope to the selected worktree. Store
generation CAS and stale-owner fencing remain authoritative.

Ordinary claim acquisition also applies the established claim-liveness
classifier to every overlapping claim. A terminal owner makes its scopes
recoverable; a quietly stale or unknown owner is recoverable only for a
nonexclusive scope. When every conflict is recoverable, the old claim versions
and new claim publish in one Store compare-and-swap, with `recovered_from`
provenance and a `lease.recovered-and-acquired` session event. Live, unresolved
nonterminal, or quiet exclusive claims continue to block. This automatic
transition removes a separate claim-transfer step for recoverable scopes; it
does not assert that an arbitrary host process has stopped. Control's
process/job fencing remains required for host-side operations and checkout
selection transitions.

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
