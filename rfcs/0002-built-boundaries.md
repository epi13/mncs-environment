# RFC 0002 — Session Persistence, Authority, and Composition Boundaries As Built

Status: accepted (implemented on `campaign/environment-foundation`).

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

## 3. File-backed session persistence owned by Environment

Each session is a directory: `session.json` snapshot, `events.jsonl`
append-only log, `checkpoints/`, `handoffs/`. Atomic writes
(write-tmp-then-rename). State root defaults to
`~/.local/share/mncs-environment`.

Store and Memory were inspected and deliberately not used: no
session/checkpoint schema exists in either, and opaque blobs would misuse
them. Recorded as pressures with desired contracts.

## 4. Authority is pure, default-deny, and event-logged

`authority.evaluate()` is a pure function over intent constraints,
protected scope, and advisory leases. Unknown actions deny; ungranted
sensitive actions escalate; every denial/escalation is a session event.
Rights/provenance binding is a recorded pressure, not a local
reimplementation.

## 5. Leases are advisory and environment-local

`mncs_env/leases.py` implements expiring session-scoped leases enforced
only inside Environment authority evaluation. Foreign work is additionally
detected heuristically (dirty trees, foreign branches, linked worktrees)
and surfaced as protection reasons. Canonical family-wide leases belong
to the control plane (recorded pressure).

## 6. Capability discovery is data-driven with an explicit seam

Bindings come from repository-owned `family-semantic-contracts-v1.json`
`provides` and `.mncs/project.json` manifests. Addressing uses a small
tested bootstrap table plus a manifest `fingerprint_sources` heuristic
(existing `.py` module becomes a `python:` address with provider root as
default cwd). Novel spellings bind as unavailable with reasons. No giant
provider switch; the seam is a recorded pressure for canonical provider
addressing.

## 7. Events are log-based with replay; polling is an adapter

Subscriptions track cursors into the session log; absent consumers replay
on return. The only cross-process observation mechanism today is the
`adapter:git-poll` head-change adapter, explicitly labeled. Canonical
provider event feeds are a recorded pressure on Forge/owning services.

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
