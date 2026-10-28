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
   verdicts, pending verifier retry deadlines, provider availability, and
   lifecycle form a projection epoch. An unchanged epoch reuses the last
   validated summary instead of re-rendering; the stored epoch describes the
   post-pass world.
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

When the planner cannot be called, a deferred projection record includes the
planner binding state and, when present, the exact matching
`consumed_provider_diagnostics` entry from the session workspace snapshot.
This can distinguish an unbound planner caused by a dirty read-only provider
checkout from an unavailable bound executable. The projection still defers;
the diagnostic adds no authority and does not invoke or modify the provider.
If no matching provider diagnostic was recorded, the record says so instead
of inferring a cause.

When a projection output diverges from its accepted state, `output_diagnostic`
records the relative target, owned scope, classification, current owned-content
digest, candidate render digest, and prior accepted digest when available. It
contains no file contents. The result remains deferred and records that
automatic writing was withheld, so the agent can inspect the exact conflict
while authored bytes stay protected.

Each result retains up to eight compact verification observations with the
obligation, evidence identity, verdict, method, and bounded reason. An omitted
count preserves awareness when a declaration has more observations. This lets
an agent distinguish missing evidence, a started executor failure, and a
coordination or stale-binding deferral without treating them as one generic
UNKNOWN.

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
  verification resolutions without re-entry. Each resumed pass refreshes
  selected checkout and provider identities before evaluation. If coordination
  or a stale binding prevents a verifier from starting, bounded evidence
  records the deferral and the epoch stays uncached so the next tick can
  reconsider it. Completed executor outages keep their backoff; the epoch
  becomes eligible again when that retry deadline arrives. Quiet ticks reuse
  the epoch; the entry lock is held per tick, never across the sleep.

## Projection-specific diagnosis

The read-only `why` interpretation can inspect an exact semantic projection by
its declared id. It returns that declaration, its current semantic source
identity, the last shared projection state row, the current output identity,
the bounded Doctor health diagnosis, and that projection's entry in the latest
session pass when present. The pass entry can include the exact output
divergence comparison and references the immutable evidence record. This is
useful when one projection is deferred or divergent; the query does not
regenerate, adopt, or modify its output. An absent or unreadable pass record is
reported as unavailable rather than inferred.

```bash
./scripts/mncs-env projections <session> \
  --interpret why --repository mncs-environment \
  --projection mncs-environment:overview
```

Without `--projection`, `why` retains its default machine project-view
interpretation. The selector must identify a semantic projection declared by
the selected repository.

## Environmental capability interpretation

The same projection interface can derive an on-demand view of real work
pressures from the newest page of authenticated campaign evidence:

```bash
./scripts/mncs-env projections <session> \
  --interpret environmental-capability
```

The capability-observation entry profile selects `mncs-test` as the provider
for `mncs.test-verification-coherence/1`. If an older session records a failed
ambient verification pass, the service-accountability projection reads only
that pass's exact immutable result and linked verification evidence. It exposes
the recorded reason code and evidence pointer, and keeps the source integrity
and verification status distinct from a verified service outcome. Missing or
mismatched Store evidence is reported without inferring a reason. This does not
estimate agent need, intervention burden, or ambient fulfillment.

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

When multiple pressure hypotheses or incomplete role coverage make aggregation
useful, `pressure_evidence_summary` gives compact counts for verified pressure
observations in the selected window. It separates explicit occurrence,
supporting-context, and unclassified records from hypothesis recurrence.
`scope_status` states whether analysis was complete for that window; the
projection source records carry page offsets, history consistency,
unavailable-record counts, and read-budget omissions. A
`read-complete-for-selected-window` status does not imply complete campaign
history, and the aggregate is evidence coverage rather than an agent or service
score. Under a response-size limit, the aggregate may be omitted to preserve
source evidence. The per-hypothesis summaries and observation evidence
references remain the path to inspect supporting records.

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

The same projection exposes a bounded `implementation_accountability` view
when the authenticated campaign ledger links the exact Compiler
`native-application-proof/2` digest application record. It resolves the
recorded source revisions and module paths, then checks each source blob by
repository, commit, path, and SHA-256 through selected or workspace-read-only
checkouts. The sample reports the four-module MNCS source closure, the
compiler-verified build, and successful canonical VM cases. It identifies the
Rust file-provider harness only as a test adapter because that is how the proof
describes it. This sample does not inventory production host dependencies or
establish a migration opportunity. The systemwide native-adoption ratio
therefore remains `not-measured` until a complete, ownership-verified
implementation inventory and denominator exist. Missing source campaign or
agent identity remains unavailable rather than being attributed to the
Environment projection session.

For this exact proof, `scoped_native_module_ownership` reports a bounded
module-source measure: the number of source-integrity-verified modules in its
four-module application closure. It is `measured` only when the proof blob and
all four module blobs are verified, the proof establishes native semantic
ownership and a verified compiler front end, and all three canonical VM cases
succeed. Incomplete source evidence is `partial` with the verified module
count retained. This is a source-module ownership measure for one application,
not a functionality-weighted or systemwide native-adoption ratio. The
test-only host adapter is excluded from the module denominator, while the
number of host semantic implementations remains `not-measured` because this
proof does not inventory them. The compact projection preserves this scoped
measure and its limits.

When the selected evidence window also contains a cross-project native-userland
audit, the same view exposes its owner-declared project classifications,
remaining host responsibilities, bounded source pointers, and proposed native
slice. For the bounded `mncs.native-userland-audit/1` records from
`mncs-actions` and `mncs-test`, it also reads the exact `native-userland-audit.json`
pointer under the existing selected-checkout or complete workspace-read-only
authority. The projection exposes the recorded canonical MNCS surfaces,
source-declared pressure identities, host responsibility classifications,
stated eliminating capabilities, remaining host paths, and verification-plan
boundary alongside the source pointer. These audit fields remain owner
declarations. Unverified source records do not contribute these details. Source
content integrity is reported separately from implementation ownership: a
verified audit blob does not independently establish the owner's classification,
repository coverage, or migration readiness. The projected systemwide ratio
stays `not-measured`, and the candidate opportunity retains its readiness as
`not-established` until independent implementation and verification evidence
exists.

Each verified owner-audit detail also exposes `pressure_source_links`. These
links match its source-declared pressure identities against exact supporting
source pointers carried by the same accountability observation. A match retains
the source identity, repository, commit, path, digest, source kind, and content
integrity status; the compact response preserves the same bounded references.
Ambiguous identities, non-pressure source kinds, unverified sources, and IDs not
found in the bounded source window remain distinct. The projection verifies
pointer content integrity but does not reinterpret the referenced record's
schema. A link makes an owner-declared pressure record traceable; it does not
establish that the pressure blocks a specific host responsibility or that
native migration is ready. Systemwide adoption remains `not-measured`.

When that same evidence contains an integrity-verified `mncs-test.pressure/1`
record, the projection can also match its source-declared `commons_pressure_id`
to an owner-audit pressure ID. It labels this as an alias relationship and
retains the exact MNCS-Test source pointer. Unverified records do not create
aliases, and multiple verified records for one alias remain ambiguous. This
does not establish that a shared pressure causally blocks a particular host
responsibility.

The on-demand `capability_accountability_links` view joins owner audits and
native-capability correspondences through exact source pointers. When a pointer
comes from an applied append-only correction to `supporting_sources`, the view
keeps the correction observation and exact target reference on the link, and
does not also emit a duplicate direct-pointer link for that same source. Under
the response budget, applied correction links take priority over other link
summaries. This preserves the provenance path without turning a verified
pointer into a causal or migration-readiness claim.

The same `implementation_accountability` view can expose a bounded
`native_capability_correspondences` list. Each record links a target pressure
identity to exact MNCS-language contract, runtime-provider, dispatch, test, and
host-boundary source pointers. Schema `/3` calls these
`implementation_source_role_references` so a language contract is not grouped
under a field that implies every referenced source is native implementation.
The accompanying `implementation_ownership_assessment` reports
`not-established` and keeps native-language displacement `not-established`.
The projection reports source integrity by role and can join a shared pressure
identity through a verified `commons_pressure_id` alias. Role labels are
campaign interpretations of source content; verified digests establish
traceability, not completeness, implementation ownership, or displacement of
host functionality. A recorded test result remains distinct from source
presence, consumer integration, agent usage, causal attribution, and migration
readiness. These statuses stay
`not-measured` or `not-established` until independent evidence supports them.

Each correspondence also retains a bounded `source_work_context` from that
observation's own `related_work` record, including campaign, session, work
intent, environment, and version identities when recorded. It carries the exact
campaign evidence pointer so an agent can inspect the originating observation.
`objective_capture_status` distinguishes recorded work intent and campaign
objectives from source records that omitted them; the selected session's
campaign charter is not substituted for missing historical context.

A correspondence may also expose `consumer_evidence` when an authenticated
campaign observation links it to exact Commons family-pressure records. The
projection accepts only the bounded Commons pressure-record and
pressure-observation pointer kinds, verifies each source blob, checks its
Commons schema, and requires exact `id`/`pressureId` equality with the
correspondence target. The result preserves each consumer repository, observed
date, source-recorded validation and migration state, reproduction result,
workaround, missing-contract list, and source pointer. It also keeps the
pressure record's own affected-repository list beside the repositories in the
observations, so coverage differences remain visible. These are bounded source
records rather than an exhaustive consumer inventory; multiple records do not
by themselves establish recurring failure, agent usage, a common cause, or
host migration readiness. An actionable interpretation directs agents to
inspect the source-declared per-consumer migration state and missing contracts.

When a verified consumer observation contains an exact `language-pressure`
evidence `ref`, the projection can join that declared identity to an additional
Commons family-pressure record carried by authenticated campaign evidence. The
related record is emitted with its own verified source pointer and with the
consumer observation pointer that declared the relationship. Matching the
identity and record schema establishes traceability only; the projection keeps
causality and migration readiness `not-established`. IDs mentioned only in
free-text summaries are not treated as relationship keys.

The same correspondence can expose bounded `native_consumer_activity` when the
selected campaign evidence contains a native consumer pressure, an improvement
relation, and its linked verification record. It joins the consumer request to
the capability by exact `native_capability_identity`, joins an improvement to
the consumer pressure by exact `pressure_hypothesis.identity` and
`addresses_hypothesis_identity`, then resolves verification only by the exact
`verification_observation_identity`. Each step retains the originating
observation identity and bounded source references. For the recorded MNCS Debug
process consumer, this makes its duplicate standard-library root pressure, the
verified resolver change, and the remaining typed-success integration failure
inspectable together. Passing unit checks remain separate from the failing
integration assertion; its reported Language runtime pressure is surfaced as a
separate investigation. The projection leaves agent discovery and usage,
recurrence, causal attribution beyond the evidence's stated scope, long-term
effectiveness, and native migration readiness `not-established`; systemwide
native adoption remains `not-measured`.

The projection also exposes a bounded top-level `native_consumer_activities`
list when the selected evidence page contains that exact consumer-pressure
cluster but omits its older capability-correspondence anchor. When the same
activity is already nested under a returned correspondence, it is not repeated
in the top-level list. Each entry uses the same exact capability, pressure, and
verification identity joins and bounded source references, so page selection
does not hide a complete recent activity cluster. This remains selected-page
evidence, not an exhaustive consumer inventory; omitted evidence is not inferred.

The bounded workspace-read-only source resolver first uses an exact checkout
name. If the directory uses a case variant, it accepts the match only when the
repository manifest declares the exact requested identity; ambiguous matches
and case variants with a different or missing manifest identity fail closed.
The name-only fallback inspects at most 512 immediate directories and performs
no repository-wide Git scan.

Source pointers up to 512 KiB may be loaded when the projection needs their
bounded payload for parsing or schema checks. Larger generic source pointers
that only need digest verification are streamed through Git in 64 KiB chunks,
with a 4 MiB per-object cap under the shared 8 second read deadline; their blob
contents are not retained. Objects above that cap, or reads that exceed the
deadline, remain explicitly unverified. Streaming verifies the pointer digest
only; it does not establish the source's meaning, ownership, or suitability.

When the selected session has a capability-delivery view, the sample also
exposes `selected_session_delivery_observation`. It compares the proof's
canonical capability identity with that session's current binding snapshot and
bounded invocation events. An exact identity match is distinct from a match on
the compiler's runtime alias; an alias-only match does not establish provider
or contract identity. Absence is reported only when the binding snapshot is
complete; partial snapshots remain unobserved. This relationship describes one
selected session, and does not establish agent receipt, discoverability, or
cross-agent usability. The compiler proof's own
`environment_delivery: not-measured-by-this-compiler-proof` remains unchanged.

When Work Intent declares a repository that is not selected, the projection may
also emit `context_pressure_candidates`. These candidates join the declared
repository gap with bounded source-visibility diagnostics for that repository.
They are labeled hypotheses, remain separate from recorded
`pressure_hypotheses`, and do not establish that work was blocked or that the
condition recurs. A content-addressed environmental-observation pointer in the
selected campaign state may be read through the bounded, read-only workspace
path when its repository is named by the authenticated Work Intent and
workspace read authority is available. This verifies the referenced object;
it neither adds a selected checkout nor grants mutation authority. Other
outside-selected references remain unread and are reported as such. A
selection-gap candidate carries verified read-only observation references
separately from unresolved references, so its recommendation can distinguish
evidence access from checkout binding. This does not establish that ordinary
repository operations are available or that the objective is blocked. A
candidate may also link a validated, same-Work-Intent
`authority.integration-eligible` event for that repository. That event records
a scoped merge or publish preflight which required no persistent claim; it did
not execute integration and does not establish source or remote state, or an
integration outcome. The candidate keeps this evidence separate from the
selection gap and reports the selected session-event window and omissions, so
agents can inspect a narrow integration path without treating it as resolution
of the broader gap.

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
view partial. Compact responses retain the per-window considered, returned,
omitted, and invalid counts so this accounting remains inspectable when the
projection is size-bounded. These statuses describe the available Environment
evidence; they do not claim that no other agent or tool activity occurred.

`session_activity.rpc_operation_summary` groups every authenticated valid read
record in the selected event window by Control operation and outcome, even when
the detailed event list is capped. Each group keeps a small set of source event
references; excess groups and their record counts remain explicit. The summary
is descriptive of the selected window, not a whole-session frequency score, an
efficiency measure, or a pressure diagnosis. Invalid records are excluded and
remain visible in the separate invalid count.

The selected page can also expose bounded `authority_denials`: action,
observed repository when resolvable, normalized reason class, and the exact
source event reference. The projection omits raw target paths and free-form
reason text. Historical overlapping-claim denials retain their recorded
`claim_conflict` evidence and are marked `claim_conflict_evidence_status:
not-recorded` when that structure was absent; missing blockers and scopes are
not reconstructed. New overlapping-claim conflicts are coordination deferrals,
not authority denials. Their `session_activity.coordination_deferrals` records
carry the bounded blocker, conflict rule, repository-relative requested and
blocking scopes, and an actionable `next_step`; absolute checkout paths are
omitted. Repeated patterns remain investigation candidates scoped to the
selected session window. Neither historical denial patterns nor new deferrals
establish that a concurrency condition was unnecessary, that the environment
caused it, or that it recurs across sessions. For absolute targets, repository
attribution uses observed checkout paths. Merge and publish decisions use
writable repository authority, an exact operation scope, and the refreshed
overlap observation.
Claims describe coordination but do not grant or deny integration eligibility.
An overlap is retained as advisory coordination evidence instead of persistent
exclusive control, and a claim need not be held by the acting agent.
For historical repeated overlapping-claim denials,
`decision_claim_state_summary` counts the validated event-time blocker status,
recovery verdict, and freshness among structured denials; it separately counts
missing, invalid, and legacy state.
Compact summaries preserve blocker-status and recovery-verdict counts while
omitting freshness from each count group. The candidate's supporting event
references allow inspection of the underlying decisions. These counts describe
what the checks observed; they do not establish that each denial was necessary
or identify a cause.
Operations blocked by an overlapping claim or requiring an exact coordination
scope return `verdict: defer` and emit `authority.coordination-deferred`, rather
than reporting an authority denial or asking for owner approval.
`session_activity.coordination_deferrals`
validates these events and exposes the required coordination, next step,
sanitized repository scope, objective references, and source event. It omits
absolute checkout paths and free-form reasons. The record says
`persistent_authority_required: false` and `operation_executed: false`; it does
not establish that the proposed work is invalid or that the coordination
condition persisted. A `candidate-needs-investigation` pressure candidate
requires repeated events in one selected session window with the same
repository, action, coordination requirement and status, next step, and
requested scope/conflict class. The candidate records one-session recurrence
only and does not determine whether the concurrency constraint is unnecessary.
True policy denials and owner-approval escalations retain their separate event
types.
When a deferral records an exact blocker claim identity, the projection may
also read that claim through the existing bounded current-claim lookup. It
keeps this point-in-time status separate from the event-time decision and can
show a read-only next step when the claim no longer blocks or is eligible for
safe recovery on a normal acquisition. These lookups share the existing limit
of four claim identities across denial, deferral, and integration activity;
unread or omitted states remain explicit. Guidance does not mutate a claim or
prove an in-flight operation has stopped.
Each deferral also exposes `later_eligibility_relation` when exact campaign and
WorkIntent references are recorded. It joins only a later eligibility event
with the same repository and action, and retains that event's provenance.
Repeated-deferral candidates summarize these joins and deduplicate the
supporting preflight references. The relation establishes only that a matching
preflight was observed in the selected, returned records: eligibility records
`operation_executed: false`, so this does not establish execution, resolution,
or causation. An exact preflight reference may also link to a later provider
invocation and completion through the existing invocation interaction view.
That link establishes the recorded provider outcome and its event references,
not a repository change, verification, integration, or improvement. A missing
match is not evidence of absence beyond those bounded records. Compact
projections retain the aggregate candidate, supporting preflight references,
and bounded provider-invocation references before dropping individual
deferral detail.
When a `diagnostic.deferred` event records a coordination deferral as its
cause, the same coordination record exposes a bounded
`diagnostic_capture_relation` with the diagnostic event and failure identity.
The relation is derived only from that exact event-cause reference. A missing
match is scoped to the selected window; when the window is complete, it means
no causally linked diagnostic deferral was recorded in that session history.
It does not establish that another diagnostic could not have occurred through
an unrecorded interface.
When a current `Session.check` observes a mutation or verification conflict,
it attaches a separate `current_claim_state` with the claim version and
bounded owner liveness, and whether safe recovery is eligible on a normal
acquisition. Its
`coordination_guidance` gives the next scoped operation without mutating the
claim or changing the denial. This is derived from the same classifier used by
claim queries; providers without bounded owner-event reads report incomplete
liveness rather than scanning full session history or inferring staleness.
The `authority.denied` event preserves that observation with its claim,
owner-session, and repository identities. `decision_claim_state` in the
projection validates and retains the decision-time claim version, recovery
verdict, and bounded liveness recommendation separately from the later
`current_claim_state` lookup. The two states answer different questions: what
the check observed and what the canonical claim store reports at projection
time. Validation establishes the event observation's shape and binding; it
does not prove the denial was necessary or that either observation predicts
later state. Legacy events without the event-time snapshot report
`decision_claim_state_status: not-recorded-on-event`; malformed or mismatched
snapshots are identified as invalid rather than reconstructed.
Each allowed preflight emits a bounded `authority.integration-eligible` event.
`session_activity.integration_eligibility` exposes its action, repository,
sanitized scope, separate rights and eligibility bases, bounded overlap
evidence, event reference, and campaign/WorkIntent identities when the event
recorded them. Authority-denial rows carry the same stable objective
references. These are identifiers only: the projection does not copy goal
prose, reconstruct missing historical references, or infer that an objective
caused a denial. Older events without an objective reference report
`work_intent_reference_status: not-recorded-on-event`. The record says
`operation_executed: false`: it does not reserve the scope or assert that a
merge or publication succeeded, leaving concurrent ref updates to Git's own
checks. When the exact target checkout is available, merge and publish
preflight refreshes its registered identity and Git state even when the caller
supplies an explicit worktree scope. The event carries a bounded
`repository_preflight_state` with checkout identity, branch, HEAD, and
working-tree status. The projection validates and preserves this as the local
destination preimage; older events report `preflight_state_status:
not-recorded-on-event`. A local preimage does not establish source-branch or
remote-ref state, subsequent execution, or integration outcome. Event records
retain up to eight overlap references and the total
observed count; compact responses retain up to four and mark any projection
truncation separately from overlap records omitted at capture time.
Local `write`, `mutate`, `execute`, and `verify` preflights are also eligible
without an owned claim when they name a freshly observed attached non-main
linked worktree separate from the repository root. For local `write` and
`mutate`, that exact isolated proposal scope can be eligible without the
session having repository-wide write scope; the event records whether that
scope was present at proposal time. Repository write scope is always required
for merge or publish. `execute` remains gated by repository write scope.
Repository-wide and
repository-relative path claims are preserved as coordination evidence but do
not reserve isolated proposal worktrees. Checkout-bound worktree claims still
block overlapping writes to the same physical checkout. Distinct registered
worktrees with stable identities may propose overlapping changes on a shared
branch; local writes, mutations, and verification record that shared-ref
overlap as coordination evidence. Operations that can update the shared ref or
retire a checkout retain the conflict. Ref-changing execution and integration
still require their operation-specific eligibility checks and Git-native
concurrency handling. Missing checkout identity remains conservative.
Main-checkout mutations retain the existing clean-state or exact-claim checks,
and checkout retirement remains claim-gated.
The bounded `session_activity.proposal_eligibility` projection exposes
validated `authority.proposal-eligible` records, their objective references,
observed worktree scope, whether repository write scope was present at the
proposal preflight, and repository-wide or repository-relative path claims
treated as coordination-only. Merge and publish always require a fresh
writable-scope preflight. Eligibility records do not establish that an edit
was executed, verified, or integrated. When a later Environment invocation
names the exact proposal event as its effect preflight, the record also exposes
that invocation and its provider completion status. This relation does not
establish a repository change, independent verification, integration, or
benefit; a missing match is limited to the selected session event window. In
compact output, matched invocation evidence retains its event references while
non-matches are summarized as complete-window or partial-window observations.
For provider mutations, `session_activity.interactions[].effect_target` also
relates the exact preflight event to a bounded, operation-scoped checkout
lock. The projection reports its checkout identity, acquisition, wait, and
release evidence without exposing a local path. A completion event linked by
provider request identity establishes release after that invocation; a missing
completion remains unknown. The lock is kernel-scoped and auto-released when
the Environment process exits; it coordinates Environment-mediated provider
calls and does not fence external shell or Git activity.
Bounded `effect.operation-deferred` observations expose either a checkout wait
that expired or a selected checkout whose identity, branch, or HEAD changed
between the initial and locked preflight. Both cases
record that the provider did not start. Stale-state deferrals retain the two
preflight event references and the changed field names without local paths.
The checkout comparison ignores clean/dirty changes to allow compatible
uncommitted work to proceed; it does not detect overlapping edits within an
already-dirty tree, resolve semantic conflicts, or fence direct shell/Git
activity.
When an absolute target resolves to a registered checkout and the Environment
refreshes that checkout at the preflight boundary, the event also records a
validated `proposal_preflight_state` containing checkout identity, branch,
HEAD, and working-tree status. Older events and preflights without a freshly
inspected checkout expose `preflight_state_status:
not-recorded-on-event`; this state identifies the local checkout preimage only.
These are preflight observations: they do not prove that an operation ran,
that its changes were accepted, verified, or beneficial. The projection keeps
the source event reference so an agent can inspect the underlying evidence.
Workspace observation records bounded `repository_ref_transitions` before
advancing the campaign's stored checkout baseline; the canonical
`mncs-env reconcile` path invokes that observation and refreshes campaign
continuity. Claim acquisition, release, transfer, and controlled recovery use
the same recorder when they change the campaign's observed checkout set; an
unchanged repository ref produces no synthetic transition. Each transition
carries workspace-relative repository and checkout identity, branch and HEAD
observations, clean state, and dirty-entry count;
absolute paths and remote URLs are not copied. Initial observations and
newly available fields are marked as baseline/enrichment rather than as a
development change. `session_activity.repository_ref_transitions` validates
these records and relates them to an earlier proposal or integration preflight
only when the exact checkout identity and HEAD match the recorded before-state
in the selected session event window. The relation is a traceable sequence of
observations, not proof that the eligible operation caused the later state,
that a remote publication succeeded, or that the change was verified or
beneficial. An absent match in a bounded page may exist on an older page.
When an older before-state lacks checkout identity, the projection may expose a
separate incomplete-preimage candidate if repository, objective, branch, and
HEAD align and the later observation identifies the preflight checkout. It
retains `no-exact-preflight-match-in-selected-window`; the candidate is not an
exact relation because the earlier state could have come from another checkout
at the same path. The event-page source records which observations were
considered.
When an exact registered destination was refreshed before a merge or publish
denial, that `authority.denied` event also carries its repository identity and
the same bounded local preimage. The projection exposes it as
`integration_preflight_state_status` and retains the state in compact responses.
It does not establish that the denial was appropriate, identify the source or
remote-ref state, or record a later integration outcome; older denial events
remain `not-recorded-on-event`.
When a later returned eligible preflight in the selected event window has the
same repository, action, and overlapping claim identity as a structured
denial, the denial may expose
`claim_conflict.subsequent_integration_preflight` with the later event
reference. `search_completeness_status` identifies omitted or truncated
preflight evidence, so an observed match does not imply that it was the only
or earliest match in the full event history. When no claim match is present,
`subsequent_integration_preflight_search` distinguishes an unobserved match
from a truncated overlap list or incomplete preflight record window. These
sequence observations do not establish that the earlier denial caused a later
preflight, that the pressure was resolved, or that a merge or publication
succeeded.
Compact responses retain up to one current claim state among returned overlap
records and report other omitted states within those records with
`current_claim_states_projection_truncated` and
`current_claim_states_projection_omitted_count`. Overlap records outside the
compact four-record window remain covered by
`coordination_overlaps_projection_truncated`.

When structured denial or preflight coordination evidence includes claim IDs,
the projection performs targeted reads for at most four unique current claim
records across the activity view. It adds `current_claim_state` separately
from the immutable denial or eligibility evidence and reports lookup counts in
`source.authority_claim_state_lookup` and
`source.integration_claim_state_lookup`. This point-in-time view uses the
existing claim lifecycle explanation and includes its observation time,
current claim version/status, and available owner liveness details. Active or
unresolved claims are distinguished from a recoverable claim that ordinary
acquisition can safely supersede, and from a released or expired claim that no
longer blocks. Integration-eligibility overlaps may therefore show a
`current_claim_state` even though the claim is advisory and does not gate the
recorded eligibility decision. The read does not recover a claim, authorize a
mutation, revise the recorded decision, or establish the state that existed at
decision time; unreadable, malformed, and over-limit states stay explicit.
Older denials without recorded claim IDs remain `not-recorded`, and current
liveness does not establish whether a denial was technically necessary.

For a structured denial with a resolved current claim, `claim_conflict` also
includes bounded `coordination_guidance`. A stale non-exclusive claim eligible
for ordinary recovery points to retrying the exact scoped acquisition; the
acquisition path rechecks liveness and uses the Store compare-and-swap, so the
projection itself does not transfer or mutate the claim. Released or expired
claims point to refreshing scope and retrying. An active overlapping scope
points to a disjoint scope, an independent registered branch where that avoids
the recorded conflict, or waiting for the shared mutation to finish. An exact
merge or publish denial points back to the current scoped integration
preflight, where claims remain coordination evidence and no persistent claim is
required. Incomplete current state remains fail-closed and points to refreshing
the claim observation. These suggestions describe safe next operations from
projection-time evidence; they do not grant rights, establish that an
in-flight process stopped, or execute a change.

`session_activity.claim_recoveries` exposes up to eight
`lease.recovered-and-acquired` events from the selected page, including the
new claim identity/version, up to eight superseded claim identities, and the
event-time campaign/work-intent references when available. It is causal
provenance for an accepted claim transition, not evidence that the subsequent
implementation objective succeeded.

`session_activity.claim_coordination` exposes up to sixteen newest claim
acquisition, rejected-acquisition, release, transfer, and owner-recovery
lifecycle events in the same selected event page. A rejected acquisition
records a bounded category and diagnostic code only when the claim failed
before publication; it omits the requested reason and checkout path. Its event
does not prove the complete cause or quantify agent burden. Generation-fenced
rejections are not separately written, because doing so would advance the
Store generation after the caller's failed precondition. Each row retains the exact event reference,
event-time campaign/work-intent references when available, and validated
repository-relative scope; checkout paths and human-provided reason text are
omitted. Historical events without objective references remain explicitly
unlinked. `lease.recovered-and-acquired` remains in `claim_recoveries` to avoid
duplicating its richer recovery record. This is descriptive coordination
evidence: a claim transition records event-time activity, not the claim's
current state, an accepted code change, or a successful development outcome.
The source summary reports invalid and omitted records, and bounded pages
remain partial when older transitions are outside the view.

The entry continuation capsule also annotates its bounded page of foreign
claims with advisory owner liveness, using the same classification as the
claim query. It inspects at most four distinct owners through a provider's
bounded event interface, caches repeated owners within that projection, and
marks unavailable or over-limit observations as partial. The capsule keeps
claim presence, owner recovery eligibility, and relation to a requested scope
separate: entry has no proposed operation to compare, so
`scope_relation_status` is `not-evaluated-no-request-scope`. This read performs
no recovery or ownership transfer; an eligible non-exclusive claim still
requires a normal exact-scope acquisition to recheck current state atomically.

`session_activity.claim_lifecycle_correlations` connects structured denial
blockers, coordination-deferral blockers, integration-preflight overlaps, and
proposal-eligibility claim observations to claim transitions from the recorded
owner session. A relation requires an exact repository, owner-session, and claim
identity; related denial, deferral, preflight, and proposal rows carry the
correlation identity. The projection reads at most four foreign session event
pages, with at most 32 events per page and four lifecycle events per relation.
It uses the existing bounded page/tail interfaces and never falls back to an
unbounded event log. Owner snapshot availability, truncated tails, invalid
records, and omitted relations remain explicit. A relation establishes that
the records refer to the same claim; it does not establish why a denial or
deferral occurred, that a preflight followed because of either, the claim's
current state, or a successful integration. Current claim state remains a
separate point-in-time observation. The compact view retains the latest
lifecycle event reference and up to three related activity references in one
relation, and reports when other relations or earlier matching transitions were
omitted.

Environmental observation sources that retain authenticated session event
identities are resolved against the bounded RPC, authority-denial,
integration-eligibility, claim-recovery, and claim-coordination records in that
same session view.
A matched source reports which record categories contain its events. Finding
the event establishes that the record is present in the selected projection;
it does not establish the event's cause or downstream success. Missing
references remain unresolved when the selected window or a record collection
is bounded, including mixed views where the projection does not retain every
event identity.

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
Size-compacted projections retain the resource-sample status and observation
count even when the selected session has no samples, distinguishing
`not-captured-in-session` from fields omitted because the compact response ran
out of space.

When a campaign observation records an `outcome.related_resource_sample`
identity, the projection exposes `resource_sample_relation` beside that
observation. It joins only the exact sample identity in the selected session,
checks the recorded session/campaign/WorkIntent context, and preserves the
sample artifact digest and invocation/completion event references. Mismatched,
unavailable, and out-of-window samples remain explicit. A successful relation
establishes shared provenance, not agent-attributed consumption or causation.

When an observation records an `outcome.event_reference`, the projection also
exposes `session_activity_relation`. It searches bounded selected-session
activity records for the exact event identity, sequence, and type, and reports
the activity record and any campaign/WorkIntent context that can be compared.
Invalid references, mismatched identities, omitted records, and incomplete
event windows remain explicit. This is a provenance link: it does not establish
that an operation executed, that the event caused an outcome, or that the
change was correct. Compact projections preserve this relation and its exact
event reference when space is constrained.

`campaign-state <session>` includes this derived view when the existing
128 KiB campaign-state response budget permits, so a caller reading the durable
ledger can inspect related environmental context in the same response. When
the page leaves too little room, query the projection directly. The projection
reads committed records using the
`mncs.environment.environmental-observation/1` schema and the bounded Compiler
evidence adapters described below. It verifies each recorded SHA-256 against
the selected checkout and retains exact commit/path pointers. For the exact
allowlisted Compiler regression report families, it can also use the
workspace-read-only repository root when the Environment authority permits
workspace reads and its repository scan is complete. Other evidence in an
unselected repository is named but not opened. Results are computed when
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
paths. Each family requires the campaign's exact commit and SHA-256, its
matching identity and path prefixes, and its family's schema version. A report
is read from the selected Compiler checkout when present; otherwise, a complete
workspace scan and the Environment's existing workspace-read authority permit
a bounded read from the registered Compiler root. The projected observation
identifies whether its repository read used the selected checkout or
workspace-read-only checkout; this does not grant Compiler write access or
change checkout selection. The projection selects stages that describe each
family's behavior and includes only recorded fact-reuse counts, execution
results, and per-build resource measurements. The v1 reports do not record the
effect and capability invalidation result; the projection leaves it explicitly
unavailable. The original Compiler artifact remains authoritative and is
returned as the observation's evidence reference. The adapter does not infer
campaign duration, model identity, token use, or resource use outside the
recorded build requests.

When another verified environmental observation links one of these Compiler
records through the exact identity, repository, commit, path, and SHA-256, and
provides an explicit `observation.source_session_context`, the projection can
expose that source campaign and work intent separately from the supporting
observation's own `related_work` and recording context. A pointer-only analysis
does not attribute its reader's work to the Compiler source. Context is
included only when the source pointer is registered and content-verified in
the selected campaign evidence window. Conflicting source contexts are
reported as ambiguous; the projection does not choose one. Without a matching
verified supporting observation with explicit source context, Compiler source
session context remains unavailable. This preserves source-work attribution
without treating the reading agent's session as the origin of historical
Compiler measurements. Other Compiler evidence remains available through the
normal campaign evidence page until a bounded adapter is justified for its
schema.

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
Only records that explicitly declare the `occurrence` role count toward
recurrence. Explicit `supporting-context` records and records linked to an
improvement remain visible as supporting evidence; legacy records without a
role remain visible as unclassified evidence and do not count as occurrences.
When supporting or unclassified evidence is present, or an occurrence lacks
session attribution, the summary adds those role counts, the total evidence
count, role-coverage and session-attribution status, and bounded source
references. Omitted supporting and unclassified counts mean zero. The existing
`observation_count` and `evidence` pointer list cover explicit occurrences;
`distinct_session_count` and affected-work context describe all linked evidence,
while `occurrence_distinct_session_count` separates the occurrence sessions
when role classes are mixed. Recurrence within one session is established only
when every counted occurrence has the same recorded session identity.
Across-session recurrence requires at least two distinct recorded occurrence
session identities. Missing session attribution remains visible as incomplete
context. Missing measurements remain explicit.
Observations can also report measurements without a pressure hypothesis, so a
resource sample is not forced into a healthy/unhealthy judgment. Its sampling
scope and unavailable attribution remain attached to that observation.
When an observation records `related_resource_sample` beside `outcome` or
inside the nested outcome object, the projection joins its exact sample
identity to the selected session's resource-sample evidence. The projected
relation reports which recorded field supplied the declaration, checks the
campaign, session, Work Intent, artifact digest, and invocation/completion
correlation, and surfaces conflicting declarations. A matched host snapshot
still does not establish agent-attributed resource use, pressure, or causation.
For compacted observations, an exact `outcome.event_reference` identity,
sequence, and type are retained beside the source pointer. Agents can correlate
that reference with the session-activity projection without reconstructing it
from the details digest.
When an observation fails the record contract, its bounded
`observation_reference_diagnostics[].contract_diagnostics` names missing or
invalid fields such as `missing-observation-source-interface`,
`missing-observation-request`, or `missing-related-work-session-id`. The
record remains unavailable to pressure analysis; the field names make recorder
gaps actionable without rewriting immutable historical evidence.

The projection subject also reports `campaign_charter` separately from
`work_intent`. A charter objective is exposed only when one content-addressed
`mncs.environment.campaign-charter/1` record is verified against its Git
commit, SHA-256, and current campaign identity. No recorded charter,
unavailable source, partial pointer index, and multiple valid charters remain
distinct statuses; the projection does not select among conflicting
objectives. Compact responses retain the charter status and exact evidence
reference while bounding the objective text. The generic session Work Intent
continues to describe the current agent task and is never overwritten by the
campaign objective. The durable charter candidate index is path-scoped to the
canonical `evidence/campaign-charters/` directory, with support for the earlier
top-level `evidence/campaign-charter-*.json` path. An observation whose identity
mentions `campaign_charter` does not become a charter candidate by name alone.

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

`session_activity.service_readiness` projects the selected session's declared
services, recorded readiness observations, `environment_from_service`
dependency edges, and reconciliation outcomes from its current snapshot. Its
source identifies the session, environment, snapshot sequence, and latest
reconciliation time; `Session.inspect` exposes the same sequence so an agent
can confirm which session revision the projection used. Service rows and
investigation candidates retain that reference and, when present, the campaign
and work-intent identities. Candidates are derived from non-ready or missing
required observations, non-successful reconciliation outcomes, and declared
dependencies whose observed state is not ready. They identify work to
investigate, not established defects or causes. Probe and operation status
codes are retained, while provider prose, raw responses, and workspace paths
are omitted. The view is bounded to sixteen service records and eight
candidates; it represents current recorded state and does not reconstruct
earlier readiness history. Co-occurrence of a failed operation and a
non-ready service does not establish causation.

For each selected service declaration with a valid `probe.capability`, the
readiness view records whether there is one matching probe observation, no
observation, or an ambiguous set of observations. Its summary reports the
bounded declared-probe count, the count with one recorded result, missing,
ambiguous, unconfigured, and unmatched-record counts, and whether coverage is
complete for this snapshot. Here an opportunity means only that the session
declares a readiness probe the Environment is responsible for observing. It
does not represent an agent need or prove the underlying service fulfilled
that need; readiness outcomes remain separate from observation coverage.

`session_activity.doctor_health_history` exposes the current Doctor epoch and
the newest eight entries from the session snapshot's bounded Doctor history.
The epoch and history are linked to the session, Environment, snapshot
revision, campaign, and work intent when those identities are available. The
current epoch's snapshot sequence is compared with the inspected session
revision, and its session-bound immutable evidence record reference is
retained when its shape is valid. This view does not fetch or integrity-check
that evidence record.
When service readiness is also recorded in that exact session snapshot, the
projection exposes the shared snapshot relationship, readiness status, and
candidate count alongside the Doctor epoch revision relation; it explicitly
leaves causality unestablished.
Store-backed history rows retain their exact per-pass immutable evidence
references. Older Store records and file-backed debug sessions may have only
summary rows; the projection marks those references as not recorded rather
than reconstructing them. It omits Doctor inputs, blocker identifiers, and
workspace paths. Summaries are broader health evidence: they do not establish
a service-specific cause or improvement, and an epoch recorded at an earlier
snapshot revision does not prove that health remains unchanged.

`session_activity.diagnostic_activity` projects the Environment diagnostic
service's newest four session history summaries, separately from Doctor's
health history. Each row retains bounded outcome counts, execution mode and
depth, and a validated session-bound immutable evidence reference when one was
recorded. The current diagnostic epoch is compared with the newest history
reference. A same-session file reference is retained only for the diagnostic
artifact path and filename shape produced by the existing writer. The
projection does not read or integrity-check diagnostic artifacts; inspect the
exact reference for witness details. Campaign, WorkIntent, Environment, and
definition identities are explicitly based on the current session snapshot,
not asserted as the context at the time of each historical diagnosis. Counts
and elapsed diagnostic time describe the diagnostic service's records; they
do not establish root cause, repair, agent effort, or burden reduction. The
projection is bounded and demand-driven with the other session activity, and
does not add a telemetry store. The field is omitted when neither a diagnostic
history nor epoch is present; absence does not imply that diagnostics were
unneeded, passed, or automatically fulfilled.

Detailed Doctor classification and bounded same-session recurrence can be
requested with
`mncs-env projections SESSION --interpret environmental-capability
--doctor-gap-limit N`. The default is zero and performs no additional Doctor
evidence read. A nonzero limit reads the epoch's exact immutable Doctor record
through the strict Store reader, verifies its content-derived identity and
binding to the session epoch, then returns bounded groups of classified
provider gaps with a limited number of capability-id examples. It also reads
at most the newest eight per-pass history references, reusing the already
verified current record when it matches the latest row. Each historical
artifact must pass content-identity and epoch-binding checks before its
classifications contribute to same-session recurrence; the scan examines at
most 512 classified gap rows in any one artifact and reports omitted rows.
The result includes
the exact verified evidence references and separates missing, duplicate,
unavailable, invalid, and mismatched records. The projection preserves the
Doctor epoch's snapshot revision and links matching unavailable-set digests
in the same session's bounded history. Recurrence is limited to distinct
verified passes in one session; it is not cross-session prevalence, causality,
current provider state, or proof of a defect. Missing or unverifiable evidence
is surfaced with an explicit status and no historical gap details; the
immutable evidence reference remains available for inspecting the original
reason text. Each gap record also retains the provider identity, declared
capability kind, and workspace-relative manifest source when Doctor recorded
them. The projection groups those owner-declared fields by provider and kind,
so an executable CLI gap can be distinguished from a storage, source, or
coordination contract without claiming that either is functional or callable.
Older Doctor records that did not retain this context remain `not-recorded`;
it is not reconstructed from today's manifests. Provider kind and source are
diagnostic context, not proof of a defect or an improvement.

The compact view also relates the current session binding snapshot to observed
capability calls. `capability_delivery.bindings` reports each bounded binding's
recorded substrate availability, verification class, observed invocations and
outcome counts, with exact invocation event references.
`capabilities_not_in_current_binding_snapshot` preserves calls whose capability
name is absent from the current snapshot; this may reflect binding changes or
provider scope and is not itself a failure. Availability transitions retain
their source event references. The projection labels capability discoverability
`not-measured`: a binding proves Environment discovery and availability state,
not that the capability was presented to or understood by the agent.
Availability probes establish substrate presence, not successful provider
behavior. New invocation start and completion records include the selected
provider, binding ID, and contract revision. When all three are present, the
projection attributes the use and outcome to that exact session binding; a
same-name binding from another provider receives no attributed invocation.
If start and completion records disagree about the binding identity, the
completion is not joined to the invocation. Older events retain their
capability-name-only counts and are explicitly marked as lacking binding
attribution. A missing invocation means no Environment invocation record in
the selected event window, unless complete session history is available; it
does not account for use through external interfaces. These relationships make
availability and recorded use inspectable without treating an absent record
as evidence of poor discoverability or usability.

`session_activity.service_accountability` connects each declared service's
probe capability to the current session binding inventory and its recorded
availability and use evidence. Exact binding-attributed invocations are
separated from older capability-name-only activity. The service readiness
snapshot remains distinct from binding availability and observed use, so a
ready binding does not imply a ready service. Source references retain the
session, Environment, and snapshot revision. The summary repeats readiness
probe coverage counts to relate this view to its declarations and records.
When `requirements.services` is explicitly present as an empty list, the
projection reports `status: empty-declared-scope`, zero declared readiness
services, and the session, Environment, snapshot, campaign, and WorkIntent
identities. If the field is absent, `service_readiness` remains absent;
`service_accountability` may still expose recorded Environment owner-pass
execution evidence.
This empty scope is limited to session readiness declarations; it does not
claim that other Environment capabilities or ambient owner passes were
unavailable, idle, or successful.
When no session readiness services are declared or recorded, a bounded
owner-pass trace makes `service_accountability` available with an
`environment_service_execution` section derived from the existing
`.mncs/coherence-passes.json` contract and `session_activity.ambient_service_execution`.
It correlates the owner-pass decision coverage, scheduled/skipped counts,
failure and result-record evidence, and exact projection digest with the
session, Environment, snapshot, campaign, and WorkIntent. This scope is
available without session-authored service declarations. The nested view links
to the bounded trace by digest, which retains per-pass trigger and result
references. When readiness services are declared, their accountability view
remains scoped to those declarations and the owner-pass trace remains available
separately. Owner-pass decision coverage is coverage of the scheduler's
declared decisions only; it does not measure agent need, capability
discoverability, service fulfillment, or intervention reduction. When no
owner-pass trace is recorded, an explicit empty readiness scope remains
limited to readiness declarations; without a readiness declaration, the
projection does not synthesize a service-accountability record.
Each row also carries the sanitized readiness observation code and time, and
the reconciliation operation code when present, so a failed or degraded
service can be investigated from its declared status without exposing provider
reason text or raw response data.
Within the bounded binding-detail list, declared service probes are selected
before other bindings, followed by exact invocation bindings and observed
capabilities. A per-probe binding count derived from the scanned inventory
distinguishes a genuinely absent binding from one omitted by the detail limit;
the count is marked incomplete if the inventory scan encountered invalid or
unexamined records. Per-service `binding_match_count` reports inventory
cardinality even when some matching details are omitted.
Those counts cover probe observations only. Ambient fulfillment, an agent-need
opportunity denominator, agent receipt, intervention burden, and outcome
quality remain explicitly unmeasured unless the environment records the
opportunities and outcomes needed to establish them; no fulfillment ratio is
inferred from readiness or invocation counts. The projection source exposes
digests for the selected session's readiness and accountability views, and
includes them in projection identity so snapshot changes remain distinguishable
even when the event tail and binding inventory are unchanged. These digests
identify derived input state; they do not verify service correctness.
When a projection exceeds its response budget, the compact view retains the
service-accountability summary and its session, Environment, snapshot, campaign,
and work-intent context. Per-service detail rows are omitted with an explicit
`services_omitted_count`; the selected-session accountability digest remains
available for correlation with the source projection. The summary preserves
unknown opportunity and burden values rather than deriving them from readiness
or invocation counts.
Each declared probe also includes `ambient_owner_pass_activity`, which relates
the exact probe capability and current binding to scoped invocation records
that ran inside a recorded ambient owner pass. It preserves bounded references
to the pass trace and invocation/completion events. Calls without valid owner
pass context are surfaced as unclassified or invalid-context use, rather than
being silently treated as ambient execution. This relationship establishes
recorded execution context only; it does not establish that the service was
needed, that an agent received the result, or that the pass fulfilled its
responsibility. The selected event-window status is included so an absent call
is not confused with complete-history evidence when the window is partial. If
bounded owner-pass or capability groups were omitted, the probe relation marks
its activity projection incomplete instead of treating the omitted rows as no
observed invocation.

For each matched ambient probe call, `probe_calls` retains the invocation and
completion counts, the completion status counts and nonzero return-code count,
exact binding attribution, and bounded source event references. The separate
`owner_pass_executions` list remains deduplicated by scheduler execution. These
are observed probe outcomes tied to an owner-pass trace; they do not establish
independent verification, service fulfillment, agent receipt, or reduced
intervention burden. The current service readiness snapshot remains separate,
so a returned probe call is not treated as proof that the service fulfilled its
responsibility.

For a declared probe, `ambient_owner_pass_activity.unclassified_probe_activity`
also exposes bounded calls to the same capability that lack valid owner-pass
context. It retains paired completion status and source event references, and
reports an exact current-binding match where the event carries a complete
binding identity. This makes activity outside the recorded scheduler context
inspectable without attributing it to an agent or calling it manual fallback,
a missed opportunity, or service failure. The event-window and omitted-group
statuses remain part of the interpretation. When response compaction applies,
the service-accountability digest keeps this relation in projection identity;
bounded source references remain in the service row while response budget
allows.

`session_activity.ambient_service_execution` exposes the latest recorded
incremental owner-pass scheduler trace from the same session snapshot. Entry
also returns the current bounded trace directly, including scheduled and
skipped owner passes, coverage of declared pass decisions, and wall-clock
duration for completed passes. `trace_persistence_status` distinguishes
`session-snapshot` from `entry-response-only`: a quiet entry can return a
current trace without writing the snapshot. This makes
the environment's own service routing visible alongside capability use and
service readiness. It counts only decisions made by the scheduler; unrecognized
service opportunities and agent need are not recorded. A returned pass, a
skipped-current-input decision, or a short duration does not establish correct
service fulfillment, agent receipt, or reduced intervention burden. The
projection links the trace to the session, Environment, snapshot revision,
campaign, and work intent where available, and its digest participates in
projection identity. It does not add per-entry durable telemetry: a quiet run
whose scheduler state was not otherwise saved may be visible in the entry
response but absent from a later session projection. Recovery-scan traces are
also returned at entry and are not retained as incremental scheduler state.

For each scheduled owner pass, the trace now carries a bounded result relation
when the runner returned one. The row joins the scheduler's exact
`service_execution_id` to the content identity and safe source reference in
the existing `coherence.result_refs` map, and exposes only scalar summary
counts and operation status. Local artifact references are repository-independent
paths under the session state; absolute checkout and state paths are omitted. A missing or
mismatched result reference remains explicit. This makes the service's
recorded result inspectable alongside its scheduling decision without adding
a telemetry store. The relation does not independently verify the result,
prove service fulfillment, or measure agent receipt or intervention burden.
Entry-only traces can show a result summary but do not claim a durable result
reference.

The nested `environment_service_execution.summary` also retains a bounded
`owner_failure_details` list so response compaction does not hide a recorded
failure behind aggregate counts. Where an owner result links to an immutable
verification record, the projection resolves that exact record and exposes its
allowlisted reason code, source schema, integrity status, and references.
`owner_failure_details_status` distinguishes complete, partial, invalid, and
`not-recorded` failure coverage. This is failure evidence, not an assertion that
the service caused the failure or that any agent received the result.
For recorded verification owner results, the same exact Store relation also
surfaces bounded obligation and outcome counts, reason-code counts, and up to
eight non-passing obligation summaries under `verification_result_details`.
This makes unsupported executors and undeclared suites inspectable from the
service-accountability projection. It preserves `verification_status` as
`not-established` when the evidence has unsupported or unresolved obligations;
the summary does not infer that a capability gap is the only cause or that
future verification will pass.

When a Family ambient pass persists `family_last_summary`, ECI exposes it as
`session_activity.family_ambient_observation`, with the selected session,
Environment, snapshot sequence, an observation-epoch key when recorded, and a
digest of the bounded summary. When the pass runs under the Environment owner
scheduler, its exact execution ID is joined to the matching Family result in
the latest scheduler trace. Direct calls, older snapshots, missing results,
and ambiguous matches remain explicit; a later domain pass cannot rewrite an
earlier scheduler result. The observation carries only bounded counts, elapsed
time, and epoch reuse state. A valid execution link establishes provenance,
not verification, agent-need fulfillment, or intervention reduction. Its
digest participates in ECI projection identity and survives response-budget
compaction.

When an ambient adapter completes without emitting an entry block, it records
a compact reason-coded result such as `no-diagnostic-findings`,
`no-verification-obligations`, or `disabled-by-definition`. The normal entry
response still omits that quiet block. The persisted or entry-only trace joins
the outcome to the exact scheduled execution and keeps its source-reference
status explicit. These codes describe the adapter's completed path; they do
not establish that an agent needed the service, that it fulfilled a need, or
that agent intervention was reduced. Opportunity denominator, fulfillment,
and burden remain unmeasured.

Committed campaign evidence with kind
`ambient-owner-pass-result-trace` retains a compact view of the same
relationship for historical investigation: scheduler persistence and duration,
bounded owner results, their exact execution and content identities, and
recorded Store or session-artifact pointers with bounded session and owner
checks. The projection keeps the boundary between scheduler activity and
service fulfillment explicit. The Environment capability-observation profile
selects `mncs-automation` because the resident native coherence router is an
implementation dependency; omitting that checkout forces the router to
require an invocation-specific root override or fall back to recovery-scan
behavior.

`session_activity.ambient_owner_pass_activity` joins Environment capability
invocations that were explicitly tagged while running inside a synchronous
incremental owner pass to the latest stored scheduler trace using an opaque
execution ID and pass name. Invocation and completion events carry that same
context only while the pass is active. The projection validates both event
contexts, counts outcomes only when the paired completion repeats the exact
context, and preserves bounded event references for inspection. It also
distinguishes calls without a context from malformed context; an unclassified
call is not attributed to an agent or treated as manual work. A locally
executed `mncs-env invoke` records the explicit CLI request surface in both
invocation and completion events. The projection exposes that surface only
when the paired events agree; it does not identify the actor or establish that
ambient work was due. Other invocation paths remain unmarked. Bounded
unclassified groups retain the request surface when present, exact binding
identity when recorded, paired completion counts, outcome status,
completion-context status, and source references for the invocation and
completion. Under response pressure, the
projection trims these details before
discarding correlated RPC records and marks omitted groups when a compact
summary fits. An exact trace match establishes that the invocation was observed
within the scheduled pass, not that the service fulfilled its responsibility,
that an agent received the result, or that intervention burden fell. The event
window and latest stored trace are bounded, so an absent match may mean the
relevant trace was entry-only or has since been replaced. No opportunity
denominator or fulfillment ratio is inferred from these records.

`session_activity.projection_coherence_history` exposes the selected session's
recorded projection-pass summaries and their evidence references, together
with the latest stored epoch summary and campaign context. It reuses the existing
session snapshot; evidence references identify immutable Store records or
session-scoped file artifacts according to the persistence backend. The view
does not create a second telemetry store or fetch or integrity-verify the
referenced historical evidence. The view is bounded to
the newest eight pass records and rejects evidence references belonging to
another session or projection owner. Cache-reused ambient reads and trigger
opportunities that never resulted in a recorded pass are not counted, so the
history cannot establish an ambient-fulfillment ratio. A reconciled or
current outcome records the pass result; it does not establish lasting
freshness or reduced agent burden. Its digest participates in projection
identity, so a snapshot-only history change is detectable even when the event
tail is unchanged. Per-pass snapshot-save durability is not recorded in the
history and remains unknown.

New write-effect invocation events may include an `effect_target` with the
exact repository, checkout, and branch admitted for that operation.
`session_activity.interactions[].effect_target` exposes the recorded
repository, branch, stable checkout identity, and authority preflight event
reference, omitting the local checkout path. Its reference leads to the
immutable proposal-eligibility or integration-eligibility observation; its
absence is not evidence that the capability was read-only or that no effect
occurred.

`context_operation_activity` also summarizes authenticated `session.context`
RPC records. New `/2` records preserve a bounded summary of the returned
capability list: available, unavailable, and unknown counts; truncation state;
and a digest of available capability, provider, and contract-revision
identities when the list is complete. Capability names and the full response
body are not copied into the event. `response_summaries` retain the source
event references, and capture-status counts distinguish these records from
older `/1` events, which remain `not-captured-in-rpc-event` rather than being
backfilled. The selected window and omitted summary count remain explicit.

For an untruncated complete response, the projection compares its count and
identity digest with the current session binding snapshot. The comparison is
made against projection-time state because the RPC event does not carry a
binding revision. A difference is therefore evidence to investigate, not
proof that the endpoint returned an incorrect response. For each currently
available binding, `context_response_inclusion` reports when its
capability/provider/contract-revision tuple belongs to a complete response
whose full identity set matches the current session snapshot. This is a
server-observed response relationship at projection time. The response
summary does not carry the binding ID, so it does not distinguish duplicate
bindings with the same tuple. A response mismatch or missing digest leaves
inclusion unestablished; it is not evidence that the capability was omitted.
`context_response_use_sequence_relation` then compares the matched response
event with exact-binding invocation events in the same session. It reports
whether an invocation was recorded before or after that response, or whether
an exact invocation record is absent from the available history. Sequence
order does not show that the agent received the response or that it caused a
later invocation; the relation keeps its causal claim unestablished and notes
when the event window is partial or response/invocation summaries were
omitted. A missing exact invocation record also does not account for use
through external interfaces.
These records do not establish that the consumer received, discovered,
understood, or used a capability. Agent receipt and discoverability remain
unmeasured. This delivery evidence is separate from
`implementation_accountability`: an available capability does not establish
native implementation ownership or migration readiness.

If the environmental-capability response budget trims per-binding rows,
`capability_delivery.binding_evidence_summary` preserves bounded counts for
availability, context-response inclusion, observed use, binding attribution,
and response-to-use sequence relationships. It includes invocation and outcome
counts only for events attributed to the exact provider, binding ID, and
contract revision. `binding_details_truncated` and
`binding_details_omitted_count` show when the per-binding source references are
not in this response; use a narrower event or evidence window to inspect them.
The summary reports the selected event-window scope and keeps agent receipt
`not-established` and discoverability `not-measured` even when a context
response and invocation are recorded.

The `/2` binding evidence summary also retains counts for
`context_response_binding_id_attribution_status_counts` and
`context_response_binding_id_list_status_counts`. These distinguish an exact
binding-ID match from tuple-only inclusion when the Control response did not
capture a complete ID list. A per-binding `context_response_inclusion` also
reports `response_binding_ids_status` (`complete`, `partial`, `unavailable`, or
`mixed`) for its matched response evidence. The bounded
`context_response_supporting_events` references identify the source context
responses, while `exact_binding_invocation_supporting_events` identifies
invocations counted for exact bindings. Their corresponding
`*_omitted` counts disclose source references left out after deduplication and
the projection bound. These references keep event identity, sequence when
available, and type; inspect the selected source event window for full payloads.
The summary and references remain evidence of server-side recording, not proof
that an agent received or used a capability.

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
For the `ambient-service-decision-trace-gap` observation kind, compaction also
retains a bounded `ambient_service_evidence` view of allowlisted entry,
readiness, session-event, and process-sample values. This keeps real service
activity inspectable when the whole response exceeds its byte limit while
preserving the exact source pointer. The view explicitly leaves ambient
fulfillment, the agent-need denominator, and causal attribution unestablished;
it does not attribute process I/O to listed capability invocations.
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
values. One correction may name up to three supported fields, including
`supporting_sources`, `not_measured`, `related_work`,
`pressure_hypothesis_role`, and
`pressure_hypothesis.actionable_recommendation`. On an integrity-verified
contract-invalid target, a correction may supply a missing or invalid
actionable recommendation, `observation.source_interface`,
`observation.request`, `observation.outcome`, or the top-level
`schema_version`.
These fields can only complete a contract-invalid target; they cannot rewrite
a valid observation. The exception is an actionable recommendation that
conflicts with another definition of the same pressure: it may be reconciled
only when its replacement exactly matches an integrity-verified `occurrence`
in the same campaign with the same hypothesis identity, title, category,
subsystem, and owner. The projection reports
`corroborated-by-verified-occurrence` as the basis. Unsupported rewrites remain
unverified. The projection reapplies the complete record contract before it
includes a contract-invalid corrected record in pressure analysis. It retains
the exact target pointer and correction identity so the effective value remains
traceable to both the original record and its correction.
A pressure hypothesis can have role `occurrence` or
`supporting-context`. Records without an explicit role are unclassified and do
not count toward recurrence. A verification or improvement record that repeats
a hypothesis only to describe the change should use `supporting-context`; a
validated improvement relation is also treated as supporting context in the
recurrence view. Projected observation rows distinguish a recorded role, a
verified append-only role correction, and a role that was not recorded. An
append-only correction can fix a previously published role while preserving the
original record and its pointer. A correction may set the role to `null` to
remove an unsupported role; the projection reports `corrected-to-not-recorded`
and does not count that record as a pressure occurrence or supporting context.
This is useful when a record has no pressure-hypothesis object to support its
role. A correction may also restore a missing `related_work` object from the
target's recorded campaign/session context and request or source-session
context. Each restored identifier must match that recorded context; the
correction cannot invent attribution. These additions let the projection
repair incomplete immutable evidence without rewriting its source.
Each replacement source must have verified content integrity. A source may
already be registered in the selected campaign evidence window, or a
non-observation source may be read through the bounded, read-only workspace
lookup when its in-root checkout is uniquely resolved, then verified by exact
commit, path, and digest. In the latter case, the projection reports
`not-present-in-selected-campaign-evidence-window` for page-local registration
while preserving the independent content-integrity result; it does not grant
write authority or a claim on the source repository. An
environmental-observation source outside the selected campaign evidence is
not accepted as a replacement. The projection applies a correction only when
the exact target pointer and target record are integrity-verified and the
replacement values differ from the target. For a contract-invalid target, the
replacement fields must make the complete observation contract valid. It keeps
the original record inspectable through
its evidence reference and exposes the effective values with a link to the
correction record. A target outside the selected pages remains explicitly
unresolved; an invalid or conflicting correction is not applied. An
`improvement_relation` correction may repair only an existing invalid relation.
It must preserve the relation identity, hypothesis, verification and follow-up
identities, basis, measurement limits, and implementation repository, commit,
and source identity. The replacement path must be present in the original
pointer's recorded path or paths, and its content digest must verify in the
bounded source read and match an exact source pointer already recorded on the
target observation. A valid or absent relation cannot be rewritten through
this correction path. This restores inspectability without asserting that the
linked change was effective; effectiveness remains `not-assessed` until
independent later evidence supports an assessment.

The query considers at most 32 campaign evidence references, reads at most 16
observation records of 64 KiB each within one 8 second Git-read budget, and
caps its response at 72 KiB. If a response reaches that bound, operation
payloads are summarized by digest and the exact evidence pointers remain
available for inspection. Compacted observations retain bounded request
identifiers and outcome context, including original contract diagnostics and
small source-event summaries when recorded. Compacted views retain the newest observation and
improvement-relation records by ledger offset, including when history pages are
read newest-page-first. `observation_records_compacted_out_count` reports how
many verified observation summaries did not fit the compact view. The CLI
keeps environmental-capability JSON within the same byte limit; it omits
indentation when pretty formatting would exceed the bound and refuses a
compact payload that still exceeds it.

The bounded observation selection also retains the latest occurrence for
distinct pressure identities represented in the selected evidence, alongside
the newest observation tail. When response compaction is necessary, exact
observation and improvement rows take priority over duplicated native-consumer
activity details and expanded current-session histories. The native-consumer
capability summary remains, and response-budget omission counts or markers
identify details that were not returned.

Before dropping environmental observation summaries to meet this bound, the
compactor can omit nested owner-audit source-detail entries one at a time.
`source_audit_details_truncated` and
`source_audit_details_omitted_count` make that reduction explicit; the owner
audit's exact supporting references remain available for source inspection.
This gives linked pressure hypotheses and improvement relations room to remain
inspectable in a compact response. If their observation summaries are omitted,
the relation and hypothesis still carry exact evidence references when present
in the selected view.

The top-level `identity` is a correlation key for the selected source context,
including its evidence and session-state digests; it does not identify the
projection implementation or guarantee identical interpretation output. The
`content_digest` is a 128-bit fingerprint of the complete returned
interpretation, calculated with that field omitted. It therefore changes
when projection logic changes the result for the same source context, and also
distinguishes a compact response from its full response. The fixed-size digest
is included in response-budget accounting.

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
