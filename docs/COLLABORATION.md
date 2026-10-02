# Family collaborative workspace

The family layer (`mncs_env/family.py`) keeps one shared semantic
workspace current across concurrent Environment sessions. Each worktree
stays an isolated editable projection; the shared object is the family
state itself:

```text
family generation
producer working changes (mncs.family-change/1)
contributor presence
per-consumer reconciliation rows
evidence references
```

Git remains publication and history. Agents learn what collaborators
did from shared rows and the collaboration capsule, not from sibling
`git log` archaeology.

## ChangeSets

A producer working change is a `mncs.family-change/1` record owned by
MNCS-Commons (`src/mncs_commons/family_change.py`, native law in
`mncs.commons.family.change.v1`). It is distinct from Record Spine
evidence-coordination ChangeSets; the two link via
`evidence_refs.coordination_changesets`, never by conflation.

Identity is `fc:<sha256>` over the canonical core (producer,
base head, subjects, contracts, operations, intent, flags). The full
record is immutable evidence; only the lifecycle state advances.

Lifecycle (native `lifecycle_legal` judges every transition):

```text
draft -> observed -> validated -> established -> published
draft -> abandoned            established -> superseded
any   -> invalid (explicit)
```

Drafts are visible to other sessions but never authoritative:
no repair converges toward a draft.

## Generations

`family:generation` is a shared cursor. Each `establish_change`
bumps producer and project generations idempotently (re-establishing
the same change does not advance). A change pins the generation it
established at (`established_generation`); consumers converge toward
the pin, so later producer advances never invalidate an in-flight
plan. Only supersession does.

## Presence

Every ambient pass publishes a `family:contributor/<session>` row:
consumer, workspace projects, active (non-terminal) produced changes,
live claims, observed generation. Presence is derived from machine
facts (session, claims, checkouts, ChangeSets); no transcripts.

## Observation epoch

The ambient pass is read-only observation plus bounded repair.
Observation is keyed by an epoch over:

- family generation cursor
- change identities, states, and pins
- reconciliation row versions, classes, and repair states
- owning verification verdicts for `applied_unknown` rows
- own covering claims per selected project
- contributor session set

An unchanged epoch reuses the cached summary with zero native calls
(measured 0.357s -> 0.003s live). Anything meaningful busts it: a new
or transitioned change, a row update, a landed verdict, a claim
acquired or released, a contributor joining or leaving, an expired
revisit backoff.

## Drift classification

Consumers are found structurally: a change's `contracts_changed`
match consumer manifests' `consumes` edges (`dependency_consumers`).
Each consumer is classified by native law:

```text
current  reconcilable  occupied  blocked  pending_verification
semantic_required  incompatible  unknown
```

`occupied` means another live session holds the target: the pass
records a pending row and never writes. `semantic_required` and
`incompatible` escalate with a compact attention item instead of
guessing.

## Convergence

Repair converges only inside this session's own claimed checkouts,
at most two per pass, never on first sight in ambient mode (explicit
`family --converge` may). Every operation revalidates before writing
(two-phase): change still established, claim live, preimage matches,
transform admitted by native policy, no foreign dirt. Writes that do
not match the preimage are refused as `foreign-dirt:<path>`; already
converged postimages are idempotent skips, never foreign.

Single-flight is by compare-and-swap on the reconciliation row: two
sessions that see the same drift produce one repair; the loser
observes the winner's row version advance.

Applied repairs enter `applied_unknown` until the owning verification
verdicts land: PASS adopts (`current`/`applied_pass`), FAIL
reclassifies to `semantic_required` and escalates, UNKNOWN stays
pending. `adopt_pending` runs each full pass over rows whose verdict
inputs changed.

`revisit_deferred` reconsiders occupied/blocked/pending rows with
native backoff; claim release makes them eligible again.

## Semantic impact and Doctor

`semantic_impact` queries an already-live Language Service resident
for affected subjects; it never starts residents (Doctor owns
provider lifecycle) and degrades honestly when none is bound.
`doctor.remediate_family_change` wraps convergence in an
`mncs.remediation/1` envelope; authority stays in the family layer.

## Capsule

`capsule()` is the whole agent-visible surface: generation,
contributor count, relevant changes, reconciled count, and at most 8
attention items (change, consumer, class). Measured 226 bytes live.
Full rows stay in shared state; nothing else enters context.

## CLI

```text
mncs-env family SESSION                       # observe + bounded converge
mncs-env family SESSION --publish DRAFT.json  # validate + publish draft
mncs-env family SESSION --establish CHANGE    # validate + establish
mncs-env family SESSION --transition C:STATE  # lifecycle advance
mncs-env family SESSION --converge C:CONSUMER # explicit converge
mncs-env family SESSION --converge C:CONSUMER --dry-run
```

The environment definition `family` knob (`enabled`, `converge`)
gates the ambient pass.

## Safety boundaries

- Never converges toward drafts, superseded, abandoned, or invalid
  changes.
- Never writes outside own claimed checkouts.
- Never adopts unknown work; claim acquisition with a dirty tree
  requires explicit adoption basis (existing claims machinery).
- Never commits, pushes, merges, or publishes; Git stays explicit.
- Never invents verification plans or rewrites snapshots.
- Breaking changes without deterministic operations escalate; the
  consumer tree is untouched.
- Unknown toolchain means unknown classification and deferral, never
  optimistic repair.

## Pressures (for the Sol coherence pass)

- Family manifests declare `provides` but almost no `consumes`
  edges, so structural consumer discovery finds little (the stdlib
  migration's consumers were found only by archaeology). Consumers
  should declare owned `consumes` edges, e.g. `mncs.stdlib.library`.
- `mncs call` per law function is chatty (classify + gate + admit
  per operation); a batched native pass would cut spawns.
- Reconciliation rows are re-recorded every full pass even when the
  class is unchanged; compare-before-write would calm versions.
- Cross-machine single-flight rests on Store CAS; proven here only
  on the file backend (Store entry is blocked by the MNE173 stdlib
  outage).
- The FD guard measures only the own process on Linux; shared
  executor exhaustion signalling across sessions is unproven.
