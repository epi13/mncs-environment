# Ambient external evidence (Actions)

The environment keeps remote verification evidence current through the
`mncs-actions` provider. Ambient behavior is reactive: steady states
cost nothing, and remote work always follows an explicit grant.

## Lifecycle

On entry (before verification, so admitted receipts feed the
verification epoch in the same pass):

```text
observe external_integration obligations
→ measure subjects (revision, dirt, publication, binding)
→ reuse exact current receipts (zero remote calls)
→ subscribe to in-flight runs (one status call per pending obligation)
→ admit completed receipts (fetch + validate + native admission)
→ surface eligible routes as delegate requests (never auto-dispatch)
→ stay quiet when steady
```

`mncs_env/actions.py` transports facts; `mncs.actions.external_evidence`
decides. The native test-coherence policy admits current external
evidence for `external_integration` obligations with the carried
verdict and external provenance intact; test semantics never claim to
have executed what ran elsewhere. Obligations without current external
evidence keep their previous verification standing.

## What surfaces

Healthy entry carries no `actions` block. Otherwise the block is tiny:

```text
actions:
  current: 1
  pending: 1
  eligible: 0
  delegate_requests: [mncs-actions.family-proof.affected-consumer]
  evidence: sessions/.../actions-artifacts/evidence/...
```

Full receipts, runs, and history stay in session evidence (`mncs-env
actions <session> --evidence`).

## Authority

- `mncs.actions-external-evidence/1`, `mncs.actions-remote-evidence/1`:
  `read` — ambient-safe observation and import.
- `mncs.actions-dispatch/1`: `delegate` — escalates unless the session
  holds a repository claim on `mncs-actions`. Dispatch verifies a
  published revision only; it never commits, pushes, branches, or
  mutates a repository.
- `mncs-env actions <session> --dispatch [--only ID]` performs explicit
  claim-gated dispatch of the admitted queue (default budget 2/pass,
  3 attempts per subject).

`subscribe` is read-class workspace-wide. Unknown effects still resolve
to `execute` (fail closed).

## Subject rules

- dirty worktree → unavailable (never dispatched, never committed for CI)
- clean but unpublished (no remote-tracking ref contains HEAD) →
  unavailable; refresh refs to become eligible
- clean and published but pointed at by no remote-tracking ref →
  observable, but explicit dispatch refuses (`no-dispatch-ref`):
  GitHub dispatches by branch/tag, never by raw sha
- a ref that advances between measurement and dispatch yields
  `subject-advanced-during-dispatch`: one attempt burns, and the new
  run never satisfies the old subject
- unmeasurable → unavailable, retried on change
- remote auth/network failure → deferred, cached per subject for one
  hour, then retried

## Evidence and reuse

Admitted receipts are identity-bound (repository, revision, workflow,
artifact, check identity) and retained under
`sessions/<id>/actions-artifacts/staged/`. Re-entry over the same
subject reuses them with zero remote calls. A completed run whose
artifacts are terminally invalid stops being pending: the obligation
becomes eligible for a fresh run instead of waiting forever. Concurrent
sessions attach to the same remote run by exact identity; the dispatch
ledger dedupes racy double-dispatch best-effort per machine. Attempt
accounting survives dispatch failure (only full row adoption aborts),
so repeated failures back off to the cap (3 attempts) and then defer
instead of retrying on every re-entry.

## Explicit surfaces

```text
mncs-env actions <session>              # ambient pass + terse summary
mncs-env actions <session> --evidence   # full trail
mncs-env actions <session> --only <id>  # reconcile one obligation
mncs-env actions <session> --dispatch   # dispatch admitted queue (claimed)
mncs-env actions <session> --full       # explicit full pass, full budget
```

## Non-goals (this phase)

No RAVEL planning dependency; no Memory integration; no deployment or
release automation (explicit-only effects refuse); no CI debug-witness
folding into local diagnostic rows (CI witness references are recorded
in actions evidence for a later pass to consume).
