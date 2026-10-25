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

To focus analysis on a particular authenticated ledger segment, copy its offset
from a campaign-state page or projection source and choose a smaller page limit:

```bash
./scripts/mncs-env projections <session> \
  --interpret environmental-capability \
  --evidence-offset 51 --evidence-limit 16
```

The selected page is read from the current campaign checkpoint. Combining an
offset with `--history-pages` continues toward older pages from that point.
Smaller segments let agents inspect records omitted by the global 16-record
analysis budget without increasing that budget; page cursors and checkpoint
consistency remain visible in the result. The read-only session entry context
also advertises the same bounded command as
`actions.environmental_capability_projection.argv` so agents can discover the
analysis without running it during entry.

The default remains one page. Historical reads follow authenticated
previous-page cursors, stop if the checkpoint changes, and cap the scan at four
pages. The 16 observation-read allowance is shared across those pages. It is
allocated according to each page's unique observation-reference capacity, then
selects the newest observation references in that page's ledger append order.
The projection reports each page's capacity, budget, selected record
identities, and unread count, along with page offsets, coverage, and checkpoint
consistency. This bounded recency sample can omit older records within a page;
inspect the authenticated ledger pages when skipped evidence matters.
Recurrence describes the selected window;
`evidence_history_truncated` indicates when it does not cover all available
history.

When the selected workspace includes a committed Compiler cache-regression
record, the projection preserves its bounded per-build metrics and separately
classifies any source-recorded harness or bootstrap measurements under
`observation.outcome.measurement_summary`. Direct measurements retain their
phase-specific names; build-job counts remain observed counts; a null cgroup
peak remains explicitly unavailable in the source record. The source note is
carried as a qualification, and these process measurements are not attributed
to the Compiler agent session. Missing token, model, and session resource data
remain unavailable. This view reports measurements; it creates no resource
pressure hypothesis by itself.

When Work Intent declares a repository that is not selected, the projection may
also emit `context_pressure_candidates`. These candidates join the declared
repository gap with bounded source-visibility diagnostics for that repository.
They are labeled hypotheses, remain separate from recorded
`pressure_hypotheses`, and do not establish that work was blocked or that the
condition recurs. A candidate points to outside-selected sources but does not
read them or authorize adding the repository; use an authorized session that
selects the exact checkout when that evidence is needed.

Session activity uses a separate bounded event page. To inspect older events,
pass the `next_event_before_sequence` cursor from the projection source and
optionally lower the page size:

```bash
./scripts/mncs-env projections <session> \
  --interpret environmental-capability \
  --session-event-before-sequence 512 --session-event-limit 128
```

The cursor is exclusive. The result identifies the selected page with
`event_page_before_sequence`, reports whether still older records remain, and
returns the next cursor when they do. Historical pages are marked partial
because newer session events are outside the selected window; absence in one
page does not establish absence from the session. Canonical Store reads scan
the current generation's binding metadata but materialize only the selected
verified event payloads. Event page reads are on demand and do not copy the
session transcript.

Session activity keeps authenticated Control read observations distinct from
Environment-bound capability invocations. If valid Control reads are present
without Environment invocations, status is `rpc-only-in-session-history`; a
bounded or incomplete read page uses `partial-rpc-only-in-selected-window`.
`rpc_operation_records_omitted` counts authenticated valid records beyond the
projection limit, while invalid events are counted separately and do not imply
activity or omitted records. A reached capture limit also marks the activity
view partial. These statuses describe the available Environment evidence; they
do not claim that no other agent or tool activity occurred.

The selected page can also expose bounded `authority_denials`: action,
observed repository when resolvable, normalized reason class, and the exact
source event reference. The projection omits raw target paths and free-form
reason text. A repeated pattern is an investigation candidate scoped to this
session window; it does not establish that the denied action was appropriate,
that the environment caused it, or that the pattern recurs across sessions.
For absolute targets, repository attribution uses observed checkout paths.
Merge and publish authorization still requires a live claim covering the
resolved scope.

The same session activity view exposes up to four newest successful
`mncs-system-monitor:system-resource-sample` artifacts, one per binding. The
Environment definition selects this read-only capability, but sampling remains
explicit. Invoke it, then query the projection:

```bash
./scripts/mncs-env invoke <session> \
  mncs-system-monitor:system-resource-sample --
./scripts/mncs-env projections <session> --interpret environmental-capability
```

Each sample is read from the canonical Store through the existing
invocation-artifact reader and is correlated to the Environment invocation
events by provider request identity and Store reference. The view preserves session, campaign,
work-intent, environment, binding, event, and Store-file SHA-256 provenance.
Only aggregate CPU and memory fields from the Monitor schema are projected;
hostnames, process lists, and unknown fields are omitted. Missing samples,
failed invocations, artifacts outside the selected bounded window, and
integrity failures remain distinct states. A host snapshot describes conditions
at capture time; it is not agent-attributed consumption and a single sample does
not establish resource pressure. Each metric carries availability, and the
projection identifies Monitor-derived CPU utilization separately from sampled
host facts; it never fills missing values with zero.

`campaign-state <session>` includes this derived view when the existing
128 KiB campaign-state response budget permits, so a caller reading the durable
ledger can inspect related environmental context in the same response. When
the page leaves too little room, query the projection directly. The projection
reads committed records using the
`mncs.environment.environmental-observation/1` schema and the one bounded
Compiler evidence adapter described below. It verifies each recorded SHA-256
against the selected checkout and retains exact commit/path pointers. Evidence
in an unselected repository is named but not opened. Results are computed when
requested; no transcript copy, separate telemetry store, or permanent dashboard
is created.

The projection compares repository identities in the session Work Intent with
the selected workspace checkouts and reports declared repositories that are
not selected. It also summarizes campaign evidence pointers whose source
repository falls outside the selected session. `outside-selected-session`
means Environment did not open that source; it does not mean the source is
missing or corrupt, and it does not prove the agent was blocked. The bounded
source diagnostics retain exact identities and revisions so an agent can
decide whether to add read-only context or leave the source outside the current
environment.

Campaign evidence and current session activity have independent availability.
If the campaign ledger cannot be read but the session contains authenticated
tool interactions, Control read observations, authority denials, or verified
resource samples, the top-level projection reports `partial` and keeps the
available session evidence inspectable. It remains `unavailable` when neither
source provides usable evidence.

The same projection has bounded adapters for three exact Compiler
language-module regression report families. The effect and capability
invalidation family uses
`mncs.compiler.language-module-elaboration-cache-regression/2` under
`evidence/native-app-readiness-effect-capability-fact-invalidation-*.json`.
The interface invalidation and generic specialization families use
`mncs.compiler.language-module-elaboration-cache-regression/1` under their
`evidence/language-module-interface-application-invalidation-compiler-*.json`
and
`evidence/language-module-generic-specialization-application-invalidation-compiler-*.json`
paths. Each family requires the selected Compiler checkout, the campaign's
exact commit and SHA-256, its matching identity and path prefixes, and its
family's schema version. The projection selects stages that describe each
family's behavior and includes only recorded fact-reuse counts, execution
results, and per-build resource measurements. The v1 reports do not record the
effect and capability invalidation result; the projection leaves it explicitly
unavailable. The original Compiler artifact remains authoritative and is
returned as the observation's evidence reference. The adapter does not infer
campaign duration, model identity, token use, or resource use outside the
recorded build requests.

When another verified environmental observation links one of these Compiler
records through the exact identity, repository, commit, path, and SHA-256, the
projection can expose that record's source campaign and work intent separately
from the current Environment campaign that is reading it. Context is included
only when the source pointer is registered and content-verified in the selected
campaign evidence window. Conflicting source contexts are reported as
ambiguous; the projection does not choose one. Without a matching verified
supporting observation, source session context remains unavailable. This
preserves source-work attribution without treating the reading agent's session
as the origin of historical Compiler measurements. Other Compiler evidence
remains available through the normal campaign evidence page until a bounded
adapter is justified for its schema.

The projection also adapts the exact Compiler
`mncs.compiler.language-dependency-summary-cache-evidence/2` workload at
`evidence/language-verification-dependency-summary-cache-*.json` when its
campaign identity starts with
`ev_compiler_language_fact_reuse_workload_`. That source schema has no internal
identity field, so the adapter uses the authenticated campaign pointer identity
and still requires the selected Compiler checkout, exact commit, path, and
SHA-256. If a source record supplies an internal identity, it must match the
pointer. The projection keeps reported structural dependency-summary reuse
separate from reported Language verification-fact reuse, retains bounded
per-stage cache and build details, and derives output-match counts only from
recorded Stage-0 and VM outputs. The source explicitly describes dependency
summaries as process-local structural summaries rather than verification
evidence. Model identity, token use, and source capture time are unavailable;
build and provider metrics remain scoped to their individual requests.
For a supporting source with no exact pointer match in the selected campaign
evidence pages, `status` is
`not-present-in-selected-campaign-evidence-window` and
`registration_scope` is `selected-campaign-evidence-window`. This describes
only the bounded pages queried; it does not claim the pointer is absent from
the rest of that campaign's history or from a separately declared source
campaign. Content-integrity status is reported independently.
If a committed record's internal identity differs from its campaign evidence
pointer, the record remains unavailable to pressure analysis and diagnostics
report `observation-identity-mismatch` with both identities after content hash
verification. This distinguishes a pointer-provenance mismatch from other
observation contract failures without treating either identity as authoritative
over the other.

An observation relates the recorded operation to its originating campaign,
session, work intent, and affected subsystem. Its separate `recording_context`
preserves the campaign, session, and consumer that captured the observation;
it can also retain the recorder's `environment_id`, `definition_id`, and an
explicitly recorded `environment_version`. The resolved `environment_id`
identifies the environment configuration and resolution inputs bound to that
session. This prevents a collector session from being mistaken for the work
under observation and makes cross-session environment context inspectable.
`environment_identity_capture_status` and
`environment_version_capture_status` report which identity fields are present
in the record. `not-captured-in-record` means the persisted record omits that
field; it does not mean the session lacked an Environment identity. Both
relationships remain inspectable through the immutable evidence pointer, while
the recorded consumer field is metadata rather than an independent identity
attestation. Pressure summaries also retain bounded `affected_work_contexts`
and `recording_contexts`, with separate counts for work intents, affected-work
environment versions, recording Environment IDs and definitions, and recorder
sessions or campaigns. The two context lists preserve their different roles;
work contexts with the same campaign, session, and intent are deduplicated,
and missing details are merged only when there is one compatible context.
Affected-work summaries retain the campaign objective separately from the
work-intent goal when both are recorded. Conflicting or ambiguous context
remains separate. Long context text and lists
are truncated with explicit flags, and the source record remains authoritative.
Direct operation-result and CLI projection-result sources are labeled
`captured-in-observation-record`; they are not described as independently
verified beyond the containing observation record. A bounded authenticated
session-activity pointer has the same status. A pressure hypothesis remains
an interpretation: one observation does not establish recurrence, and a
change remains unproven until later observations and independent verification
provide evidence. An implementation record that restates its target pressure
is not counted as another pressure occurrence; a later observation can count
when it independently records that pressure again. Repeated candidate IDs or
resource use alone do not create an efficiency score or a causal claim.
Missing measurements remain explicit.
Observations can also report measurements without a pressure hypothesis, so a
resource sample is not forced into a healthy/unhealthy judgment. Its sampling
scope and unavailable attribution remain attached to that observation.

The projection subject also reports `campaign_charter` separately from
`work_intent`. A charter objective is exposed only when one content-addressed
`mncs.environment.campaign-charter/1` record is verified against its Git
commit, SHA-256, and current campaign identity. No recorded charter,
unavailable source, partial pointer index, and multiple valid charters remain
distinct statuses; the projection does not select among conflicting
objectives. Compact responses retain the charter status and exact evidence
reference while bounding the objective text. The generic session Work Intent
continues to describe the current agent task and is never overwritten by the
campaign objective.

When observations reuse one pressure identity but record different titles,
categories, subsystems, owners, or recommendations, the projection reports
`definition_status: conflicting-recorded-definitions`, lists the conflicting
fields, and omits those ambiguous summary values. It retains the supporting
observation identities and evidence references so an agent can inspect each
recorded definition. It does not choose a recommendation based on ledger order.
A single recorded value remains visible if another record leaves an optional
field such as owner unspecified.

The same projection includes a bounded view of the current session's durable
Environment capability events. It joins `capability.invoked` and
`invocation.completed` records by their provider request identity, relates the
result to the current session objective and Environment identity, and keeps
the exact source event IDs and sequences. It reports recorded outcome status
and return code, while unmatched starts and ambiguous joins remain explicit.
Invocation arguments and raw results are not copied into this view. Only the
newest 256 session events are considered and at most 32 invocation records are
projected; the selection window and omitted count are reported. The canonical
Store reader currently materializes the session event log before selecting
that tail, so this analysis is demand-driven and bounded but its underlying
event read still grows with session history. This path covers calls made
through Environment-bound capabilities; direct external tool operations may
not be present in the session event stream. These events do not provide token
usage, model identity, execution duration, or resource consumption unless
another authoritative source records those measurements. An empty truncated
window is reported as `partial-no-invocations-in-selected-window`; it does not
assert that the full session history contains no capability calls.

When response compaction is needed, each pressure summary retains bounded exact
evidence references (identity, repository, commit, path, and digest), so an
agent can inspect the records supporting the hypothesis even when their full
observation summaries are omitted. `evidence_reference_count` and
`evidence_references_truncated` disclose when the compact view contains only a
subset; use the source page cursor and campaign ledger to continue inspection.
When an observation body itself must be compacted, its digest still covers the
complete recorded observation, while `outcome_context` retains bounded
interpretation fields such as status, claim boundary, verification limits, and
source-registration status, the selected registration scope, content-integrity
status, and small scalar summaries from structured outcome sections. This
keeps evidence such as a compiler result's narrow scope or stale-input finding
visible without copying its full diagnostic payload. `measurement_summary` separately retains
classified measurements, unavailable attribution, interpretations, and
qualifications. Both are summaries of recorded evidence, not independent
attestations; the evidence pointer remains the route to the source record.
For compiler outcomes with a `representative_stages` array, the compact context
keeps at most four rows with stage name, cache status, reference-output result,
CPU and wall time, RSS, cgroup memory, and open-file-descriptor values when
present. A recorded `null` remains `null`, and omitted stage rows are disclosed
with `records_truncated`; these values are not filled or reinterpreted by the
projection.

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

Published observation records are content-addressed and are not rewritten when
a transcription or relation needs correction. A later record can use the
`mncs.environment.observation-correction/1` contract to point to the exact
target evidence reference, name corrected fields, and provide their replacement
values. The first supported fields are `supporting_sources` (which must point
to selected, integrity-verified evidence) and `not_measured`. The projection
applies a correction only when the exact target pointer and target record are
verified, the replacement values differ from the target, and every replacement
source is verified. It keeps the original record inspectable through its
evidence reference and exposes the effective values with a link to the
correction record. A target outside the selected pages remains explicitly
unresolved; an invalid or conflicting correction is not applied. Corrections
do not change an improvement relation or effectiveness assessment.

The query considers at most 32 campaign evidence references, reads at most 16
observation records of 64 KiB each within one 8 second Git-read budget, and
caps its response at 48 KiB. If a response reaches that bound, operation
payloads are summarized by digest and the exact evidence pointers remain
available for inspection. Compacted views retain the newest observation and
improvement-relation records by ledger offset, including when history pages are
read newest-page-first. `observation_records_compacted_out_count` reports how
many verified observation summaries did not fit the compact view.

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
