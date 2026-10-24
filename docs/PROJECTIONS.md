# Ambient projection coherence

Projection coherence is the environment's derived-information layer. The
target experience is:

```text
enter environment
  -> stale safe projections converge automatically
  -> occupied targets defer without interference
  -> agent works from current information
```

Environment does not learn document, media, journal, or Atlas semantics.
It observes declared projections, asks the native automation planner
whether each may regenerate, invokes provider-owned rendering, applies
only admitted effects under narrow path claims, and records evidence.

## What runs automatically

On every entry, re-entry, and explicit `projections` pass, Environment
runs `mncs_env/projections.py`:

1. **Discover declarations.** Manifests in explicitly selected
   checkouts may declare `projections` entries with stable `id`,
   authoritative `inputs`, `output`, provider capability, render argv,
   output kind, policy, and verification obligations. Unselected
   directories are never inspected.
2. **Fingerprint the world.** Inputs, declarations, repository branch/head
   and dirty content, live claims, shared projection rows, verification
   verdicts, provider availability, and lifecycle form a projection
   epoch. An unchanged epoch reuses the last validated summary instead
   of re-rendering; the stored epoch describes the post-pass world.
3. **Resolve real verification.** Each stale projection's obligations
   resolve to PASS/FAIL/UNKNOWN from generation-bound evidence covering
   the exact subject (repository HEAD, worktree, input digest).
   Deterministic rendering alone never yields PASS; missing executors
   run lazily once per subject, and unconfined or moved runs yield no
   evidence.
4. **Ask native policy.** `mncs.automation.projection.v1::plan_tick`
   returns proceed/defer/escalate for repo, branch, claim, target,
   region, output, authorization, publication, and deferral facts,
   consuming the resolved verdict (UNKNOWN awaits, FAIL withholds).
5. **Render outside repositories.** Providers render deterministic bytes
   to session artifacts. Cache misses render twice and refuse
   nondeterministic output.
6. **Apply only admitted outputs.** Ambient writes require native
   `execute`, a live path claim over the exact output, two-phase
   revalidation under that claim (inputs, output preimage, and
   verification must all match plan time), atomic replacement, and
   post-write validation.
7. **Adopt through shared state.** Convergence publishes the observed
   generation, source identity, and evidence references to the shared
   `store.projection.v1` row via compare-and-swap; concurrent losers
   re-plan instead of overwriting. Later passes over the same world do
   no repository work.

## Terse summaries and evidence

Normal operation emits counts, not file lists:

```text
projection coherence:
  current: 2
  pending: 0
  reconciled: 1
  blockers: 0
```

Full per-projection facts, native plans, render/cache behavior, claim and
authority checks, and application results live in session evidence,
retrievable with:

```bash
./scripts/mncs-env projections <session> --evidence
```

Each pass also appends `mncs.session-evidence/1` for future journal and
media consumers.

Related surfaces:

- `mncs-env enter` returns a `projection` block with the pass summary.
- `mncs-env projections <session>` runs the ambient pass and prints the
  terse summary (exit 5 when blockers remain).
- `mncs-env projections <session> --apply <id>` explicitly reconciles one
  projection and authorizes region splicing under a live claim.
- `mncs-env projections <session> --watch <seconds>` loops epoch-gated
  passes so long-running sessions notice commits, claim releases, and
  verification resolutions without re-entry. Quiet ticks reuse the
  epoch; the entry lock is held per tick, never across the sleep.

## Environmental capability interpretation

The same projection interface can derive an on-demand view of real work
pressures from the newest page of authenticated campaign evidence:

```bash
./scripts/mncs-env projections <session> \
  --interpret environmental-capability
```

For bounded recurrence analysis across earlier campaign evidence pages, request
an explicit page window:

```bash
./scripts/mncs-env projections <session> \
  --interpret environmental-capability --history-pages 4
```

The default remains one page. Historical reads follow authenticated
previous-page cursors, stop if the checkpoint changes, and cap the scan at four
pages. The 16 observation-read allowance is shared across those pages; the
projection reports page offsets and limits, coverage, checkpoint consistency,
per-page observation capacity and allocated read budgets, and records left
unread by that budget. Recurrence describes the selected window;
`evidence_history_truncated` indicates when it does not cover all available
history.

`campaign-state <session>` includes this derived view when the existing
128 KiB campaign-state response budget permits, so a caller reading the durable
ledger can inspect related environmental context in the same response. When
the page leaves too little room, query the projection directly. The projection
reads only committed records using the
`mncs.environment.environmental-observation/1` schema, verifies each recorded
SHA-256 against the selected checkout, and retains exact commit/path pointers.
Evidence in an unselected repository is named but not opened. Results are
computed when requested; no transcript copy, separate telemetry store, or
permanent dashboard is created.

An observation relates the recorded operation to its originating campaign,
session, work intent, and affected subsystem. Its separate `recording_context`
preserves the campaign, session, and consumer that captured the observation;
this prevents a collector session from being mistaken for the work under
observation. Both relationships remain inspectable through the immutable
evidence pointer, while the recorded consumer field is metadata rather than an
independent identity attestation. A pressure hypothesis remains
an interpretation: one observation does not establish recurrence, and a
change remains unproven until later observations and independent verification
provide evidence. Repeated candidate IDs or resource use alone do not create
an efficiency score or a causal claim. Missing measurements remain explicit.
Observations can also report measurements without a pressure hypothesis, so a
resource sample is not forced into a healthy/unhealthy judgment. Its sampling
scope and unavailable attribution remain attached to that observation.

When response compaction is needed, each pressure summary retains bounded exact
evidence references (identity, repository, commit, path, and digest), so an
agent can inspect the records supporting the hypothesis even when their full
observation summaries are omitted. `evidence_reference_count` and
`evidence_references_truncated` disclose when the compact view contains only a
subset; use the source page cursor and campaign ledger to continue inspection.

An observation may also carry an `improvement_relation` linking one pressure
hypothesis to an implementation source, a verification observation, and a
later operational observation. The projection reports whether the verification
and follow-up observation records are present in the current page and passed
their commit digest and schema checks, and whether the linked pressure is
present in the current view. `not-present-in-projection` means the record or
hypothesis was not available in this bounded view; it does not establish that
it is absent from campaign history. The implementation source remains an
inspectable pointer with its own selection status. These links never mark a
pressure resolved: `effectiveness_assessment` stays `not-assessed` until
independent later evidence supports an assessment. A passing test record proves
that its evidence blob was preserved intact, while its command results remain
reported evidence rather than an Environment-generated attestation.

The query considers at most 32 campaign evidence references, reads at most 16
observation records of 64 KiB each within one 8 second Git-read budget, and
caps its response at 48 KiB. If a response reaches that bound, operation
payloads are summarized by digest and the exact evidence pointers remain
available for inspection.

## What the ambient pass refuses

- **Occupied checkouts.** Foreign claims, unknown dirt, foreign branches,
  diverged outputs, ambiguous markers/targets, and unknown repositories
  defer or escalate through native gate reasons. They never rewrite work.
- **Region splicing without consent.** Each declaration's `policy`
  governs ambient mutation for both whole-file and region outputs:
  `ambient-safe` permits ambient convergence once native Doc admission
  succeeds, an exact output claim is held, and commit-time facts
  revalidate; `explicit-only` observes and records staleness ambiently
  but never mutates until `projections <session> --apply <id>` runs.
  Human prose outside the admitted region stays byte-identical, and
  ambiguous markers fail closed. Document admission remains provider
  owned through `mncs.projection-admission/1`.
- **Git authorship.** The pass never stages, commits, switches branches,
  merges, or pushes. `current` means bytes and generations agree, not that
  a commit exists.
- **Guessed providers.** Rendering and planning use bound provider
  invocations. Sibling-path command guessing is not a fallback.

## For providers: exposing a projection

Publish an invocation descriptor for rendering/planning and declare one
or more descriptors in `.mncs/project.json`:

```json
{"id": "mncs-doc:rfc-index", "template": "project-rfc-index",
 "inputs": ["docs/rfcs"], "output": "docs/rfc-index.generated.md",
 "output_kind": "whole-file",
 "provider_capability": "mncs-doc:documentation-projection",
 "render_argv": ["project-rfc-index", "--rfc-root",
                 "{checkout}/docs/rfcs", "--output",
                 "{artifact}/rendered.md", "--link-base",
                 "{checkout}/docs/rfc-index.generated.md"],
 "policy": "ambient-safe"}
```

`{checkout}` and `{artifact}` are resolved by the orchestrator from its
session state. `--link-base` names the final output path so provider
links stay repo-relative when rendering to session scratch.
`ambient-safe` permits ambient convergence for whole-file outputs
and, once native admission succeeds, for region splices; use
`explicit-only` when the owning provider has not proven ambient
mutation safe, and reconcile those via `--apply` instead.

## Concurrency

Epochs are per-session derived state. Another agent's commit, dirty file,
claim, branch change, shared-row advance, verification resolution, or
provider-availability change invalidates the epoch; the next pass
re-observes. Observed generations, defer counts, and evidence references
are durable shared `store.projection.v1` rows (every session reads and
compare-and-swaps the same row), so ownership clearing makes deferred
work eligible without manual replay, and a second session entering after
convergence sees current state instead of re-deriving it.

Verification executors are provider-owned obligations attesting
`verify` effects: they observe the checkout and write only to declared
ephemeral roots and git-ignored scratch, enforced after every run.
`verify` authority permits execution on dirty or foreign branches but
still denies foreign-claimed and protected repositories. Evidence is
immutable and content-addressed; a recorded FAIL withholds its subject
until the subject itself changes.
