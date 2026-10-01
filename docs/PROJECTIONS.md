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
