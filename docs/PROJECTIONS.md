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

1. **Discover declarations.** Direct-child manifests may declare
   `projections` entries with stable `id`, authoritative `inputs`,
   `output`, provider capability, render argv, output kind, and policy.
2. **Fingerprint the world.** Inputs, declarations, repository branch/head
   and dirty content, live claims, provider availability, and lifecycle
   form a projection epoch. An unchanged epoch reuses the last validated
   summary instead of re-rendering.
3. **Ask native policy.** `mncs.automation.projection.v1::plan_tick`
   returns proceed/defer/escalate for repo, branch, claim, target,
   region, output, authorization, publication, and deferral facts.
4. **Render outside repositories.** Providers render deterministic bytes
   to session artifacts. Cache misses render twice and refuse
   nondeterministic output.
5. **Apply only safe whole-file outputs.** Ambient writes require native
   `execute`, a live path claim over the exact output, revalidation under
   that claim, atomic replacement, and post-write validation.
6. **Adopt and stay quiet.** Successful application advances observed
   generation; later passes over the same world do no repository work.

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

## What the ambient pass refuses

- **Occupied checkouts.** Foreign claims, unknown dirt, foreign branches,
  diverged outputs, ambiguous markers/targets, and unknown repositories
  defer or escalate through native gate reasons. They never rewrite work.
- **Region splicing.** Region outputs need explicit apply authorization;
  ambient policy always defers them. Document admission remains provider
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
                 "{artifact}/rendered.md"],
 "policy": "ambient-safe"}
```

`{checkout}` and `{artifact}` are resolved by the orchestrator from its
session state. `ambient-safe` permits whole-file convergence; region
outputs use `explicit-only` unless the owning provider proves ambient
splicing safe.

## Concurrency

Epochs are per-session derived state. Another agent's commit, dirty file,
claim, branch change, or provider-availability change invalidates the
epoch; the next pass re-observes. Failed or deferred work remains durable
in snapshot rows and evidence, so ownership clearing makes it eligible
without manual replay.
