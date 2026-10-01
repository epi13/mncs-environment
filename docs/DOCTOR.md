# Ambient Doctor

Doctor is the environment's autonomic remediation layer. The target
experience is:

```text
enter environment
  -> Doctor silently resolves safe known problems
  -> environment becomes ready
  -> agent works
```

The machine should not repeatedly diagnose and manually repair routine
known failure states. Doctor absorbs the loop — detect, determine whether
repair is safe, repair, validate, persist/reconcile state, stay quiet if
resolved — and surfaces only what genuinely needs intent.

## What Doctor does automatically

On every entry, re-entry, and explicit `doctor` pass, Doctor runs the
ambient pass (`mncs_env/doctor.py`):

1. **Validate the health epoch.** Every input that feeds readiness is
   fingerprinted exactly: checkout revisions, branches, and complete
   dirty/untracked content digests; declaration files (`.mncs/project.json`,
   `family-semantic-contracts-v1.json`, verification obligation
   inventories); managed-worktree membership; toolchain presence; a fresh
   binding-availability vector; the live claims digest; the event count;
   and the session lifecycle/consumer/definition identity. Any mismatch —
   or any unreadable input — invalidates the epoch. Doubt always runs the
   full path.
2. **Epoch hit: skip redundant work.** No revalidation, no rediscovery, no
   rebinding, no extra saves or events beyond the caller's own resume
   marker. A second entry over an unchanged world reports the validated
   summary instead of recomputing it.
3. **Services are always probed live.** Provider runtime state (processes,
   state files, sockets) is observable only by probing, so the epoch never
   stands in for service probes. When live probes disagree with the
   snapshot, services are reconciled without a full revalidation; when a
   service still needs recovery, recovery is re-attempted at the same
   cadence as before.
4. **Epoch miss: full revalidate, reconcile, record.** The new epoch,
   repair records, and evidence are persisted for the next pass.

Entry also opens the Store exactly once (matching, resume/creation, and
the ambient pass share the handle) and waits boundedly on a contended
entry lock instead of failing immediately. The lock is never seized or
broken: after a 15s budget the waiter reports `entry-busy` as before.

## Terse summaries and evidence

Normal operation emits counts, not diagnostics:

```text
doctor:
  repaired: 0
  reconciled: 1
  degraded: 33
  blockers: 0
```

- `repaired`: bindings restored to available, services restored to ready.
- `reconciled`: revalidations, service reconciliations, lock waits, and
  other bounded recovery work performed by the pass.
- `degraded`: currently unavailable capabilities (informational count).
- `blockers`: readiness blockers (required capabilities/services missing).
- `remaining`: blocker and unavailable-capability ids only (capped at 32,
  with a truncation flag).

Full detail — per-record repairs, invalidation reasons, revalidation
reports, service operations, the unavailable-capability classification,
and the readiness summary — lives in the session evidence artifact
(`sessions/<id>/doctor-evidence.json`) and is retrievable with
`mncs-env doctor <session> --evidence`. The last ten passes are kept in
the snapshot history.

Related surfaces:

- `mncs-env enter` returns a `doctor` block with the pass summary.
- `mncs-env doctor <session>` runs the ambient pass and prints the terse
  summary (exit 5 when blockers remain).
- `mncs-env status <session> --terse` serves the file-side epoch without
  opening the Store when it is fresh and valid (~80ms vs ~2s); otherwise
  it reports the last snapshot state and points at `doctor`.
- `mncs-env health <session>` serves the validated epoch (services still
  probed live) labeled `observation: epoch`; `--live` forces full probes.
- Session `context`/`status`/`inspect` carry the last `doctor` summary.

## What Doctor refuses

- **Repository content.** Ambient passes never touch repository files.
  Repository remediation is explicit only (see below).
- **Other sessions and claims.** The ambient pass mutates only the calling
  session's own snapshot, events, and artifact directory. It never
  deletes, seizes, or rewrites another session's state, claims, or
  worktrees, and never interprets another active agent as stale.
- **Locks.** Entry-lock contention waits boundedly; the lock is never
  broken or stolen.
- **Ambiguity.** Multiple matching sessions, incompatible provider
  schemas, and unparsable inputs escalate with actionable diagnostics;
  Doctor never guesses.
- **Provider gaps.** Capabilities whose providers never published an
  invocation descriptor (`provider-invocation-undeclared`) are classified
  as provider gaps and escalated as bare ids. Repairing another
  repository's manifest is cross-repo mutation and is never ambient.

## Explicit repository remediation

```bash
mncs-env doctor <session> --scope repository --checkout <repo> [--dry-run]
                          [--changed-path <path> ...]
```

This invokes the session-bound `*:repository-remediation` provider
capability (today: `mncs-doctor:repository-remediation`) over one
checkout. Gates, all fail-closed:

1. A bound remediation capability must exist and be available.
2. The target must resolve to a session checkout.
3. The session must hold a live whole-checkout claim (repository scope,
   or a worktree scope on exactly that checkout). Path-scoped claims do
   not cover whole-tree remediation.
4. Checkouts showing unknown work are refused unless the claim records
   explicit adoption or recovery.

The provider speaks the `mncs.doctor.remediation/1` envelope on stdout;
envelope-over-exit-codes applies (a completed run with findings is a
successful run; only a missing envelope refuses). Evidence lands in the
session artifact directory the invocation provides, and the run is
recorded in doctor history with a `doctor.repository-remediated` event.

## For providers: exposing remediation

Publish a `repository-remediation` contract in `.mncs/project.json` with
a working `invocation` descriptor (see `mncs-doctor/.mncs/project.json`
for the shape). The command must accept `--root <checkout> [--json]
[--dry-run] [--changed-path <path> ...]`, apply only repairs it can
validate, and print the `mncs.doctor.remediation/1` envelope: terse
counts on stdout, full evidence to `$MNCS_ENV_SESSION_ARTIFACT_DIR` when
set. Availability is honest: the capability binds only when the declared
address exists.

## Concurrency

- Epochs are per-session derived state; concurrent agents each validate
  their own world's fingerprint. Another agent's commit, claim, or event
  append invalidates the epoch and the full path re-observes — never a
  stale hit, never interference.
- Entry serialization is a bounded wait, so concurrent entries queue
  instead of colliding.
- The file-side epoch (`sessions/<id>/doctor-epoch.json`) is a
  regenerable cache: deleting it costs one full pass. The snapshot epoch
  and history in the Store remain authoritative for the session.

## Turning repair patterns into Doctor capabilities

When the same agent-visible repair recurs:

1. Prove the problem and the repair are both deterministic, and name the
   exact validation that follows the mutation.
2. Decide the class: safe-automatic (invisible), bounded reconciliation
   (recorded, budgeted, escalates on failure), or escalation (minimal
   evidence only).
3. Put provider-owned knowledge in the provider behind a capability
   contract; Doctor orchestrates, it does not guess shell commands.
4. Keep the terse summary terse: counts and ids on stdout, evidence in
   the artifact, history bounded.
5. Add a regression test that fails before the capability and passes
   after, including the idempotence case (second run is quiet).
