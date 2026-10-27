# Environmental capability evidence links

The environmental capability projection connects existing implementation
accountability and native capability observations through the ordinary
`mncs-env projections` interface. It derives links in memory; it does not add a
second telemetry store or capability registry.

```sh
./scripts/mncs-env projections <session-id> \
  --interpret environmental-capability \
  --evidence-limit 16 \
  --session-event-limit 8
```

The existing machine-facing capability catalog is the generated
`.mncs/project-view.json`. It now exposes the ambient projection contract and
the read-only projection-evidence and campaign-state inspection interfaces,
including their effects, versions, and invocation paths. This establishes
catalog presence and discoverability through the project view; it does not
establish that an agent found or successfully used those interfaces.

The session activity view also retains bounded provider declarations for each
binding. A declaration's `provider_declared_evidence_reference` points to the
repository-relative path named by its provider and, when available, the
observed checkout HEAD. This is a source pointer only: implementation
ownership is `not-established` and content integrity is `not-verified`.
Invocation descriptor declaration, resolved callable address, and provider
addressing mode are reported separately so an implementation reference does
not imply that an agent can invoke the capability. The evidence summary counts
valid provider-declared references and addressing modes; its callable-address
count does not treat an unresolved declaration as usable. Counts cover the
scanned binding snapshot and coalesced declarations. Check
`provider_declaration_scan_complete` and `provider_declarations_omitted_count`
before interpreting them as complete. These fields do not establish that the
referenced file implements the capability or that an agent discovered it.
When the bounded binding-detail window is full, declared service probes and
exact observed use retain priority; bindings with provider-declared evidence
references are then selected before ordinary inventory rows.

Inspect `capability_accountability_links` in the result. It reports exact
repository, commit, path, and SHA-256 relationships, along with their source
references, the owner audit's declared host responsibilities, the candidate
native capability, the existing next-investigation text, and one bounded
source reference for its language contract, compiler support, and host runtime
provider where those records are available. It also includes a bounded,
session-scoped context-delivery observation when authenticated session events
provide one. The relationship view is bounded to four source links and 8 KiB.
Exact pointers provide traceability, not proof of causation or migration
suitability.

`session_context_delivery_observation` preserves the session ID, work intent,
selected event window, and response event reference beside the captured
binding summary. A complete identity digest and matching aggregate counts can
show that the response summary matches the current binding snapshot, but
partial binding IDs do not provide direct binding-ID attribution. The existing
per-binding projection may derive identity-tuple membership from a matching
digest; inspect it at
`session_activity.capability_delivery.bindings[*].context_response_inclusion`.
Even complete identifiers establish a delivered set only; agent receipt and
use remain separate observations. When the selected event window has no
context response, the projection labels that absence as window-scoped rather
than concluding that the session received no context.

For declared service probes, service accountability also exposes bounded
same-capability calls without valid owner-pass context, including paired
invocation/completion references, outcome status, and a current-binding match
when available. Locally executed `mncs-env invoke` calls now carry an explicit
request-surface marker on both invocation and completion events; the projection
preserves that marker only when the pair agrees. It identifies the CLI path,
not the actor, whether ambient work was due, or whether the call represents
avoidable intervention. Unmarked calls remain unattributed. This makes
outside-pass activity inspectable without claiming manual fallback, a missed
opportunity, service failure, or reduced intervention. Its digest participates
in the service-accountability projection identity, including when response-
budget compaction retains only its summary.

When readiness services are explicitly empty or no readiness declaration is
recorded, the session records an Environment owner-pass execution, and the
selected activity has no owner-pass-scoped calls, the same
`service_accountability.observed_capability_activity` view joins capability
calls lacking valid owner-pass context with the session objective, event-window
identity, activity-projection digest, and bounded invocation/completion
references. Mixed windows remain available in the ambient owner-pass activity
projection, avoiding duplicate summaries that could displace scheduler
evidence under response limits. The view does not infer that a call was manual,
that a service opportunity existed, or that an agent received a result;
attribution, causality, eligible opportunities, fulfillment, and intervention
burden remain unestablished or unmeasured. A complete selected window only
describes the available session event page, while paged or truncated windows
retain those limitations explicitly.

Under response pressure, `session_activity.service_accountability` retains
scheduler totals, the source projection digest, and a few exact links to
recorded results. The sibling
`session_activity.ambient_service_execution` contains the bounded per-pass
source. Each scheduled row can relate trigger kinds, raw scheduler disposition
and wave, execution identity, elapsed time, recorded outcome, and safe result
reference; skipped rows preserve the scheduler's bounded reason. The
disposition remains a code from the router's contract, not an interpreted
service verdict. These are Environment scheduling records, not an
agent-opportunity denominator or proof of fulfillment, correctness, or reduced
intervention. When the final ECI summary must drop the sibling activity
projection, the service-accountability view restores bounded unscoped-call
references from its source projection digest.

If the whole ECI response still exceeds its requested byte budget, a final
summary-only view keeps service totals, exact source references for recorded
owner results, the current Doctor epoch, and bounded Doctor history identifiers.
Rows without recorded results and detailed event history are counted as
omitted. Page-level reference identities and read-budget details can also be
omitted while aggregate offsets and the campaign ledger remain available. This
fallback preserves evidence links without claiming that omitted work failed or
was fulfilled; agents can query the session activity projection for the full
bounded detail.

For an unusually small 7–8 KiB response, if a verified occurrence, its
pressure hypothesis, and an improvement relation share the same identity and
one reduced path fits within the budget, the projection retains that single
observation-to-improvement path, preferring the newest complete path in
campaign append order. It keeps the observation's exact evidence pointer, the
implementation pointer, available verification and follow-up pointers, and the
relation's effectiveness status; omitted array rows are reported through the
existing counts and truncation fields. The compact session activity keeps
measured service totals, the ambient scheduler digest and one recorded result
link, while dropping repeated Doctor history and detailed readiness fields.
This path is used only when the complete linked summary fits. Otherwise normal
budget compaction applies, and the selected evidence records remain the source
of truth for details. Retaining an improvement relation does not establish
that a pressure was resolved; `not-assessed` remains unchanged until later
evidence supports an assessment.

Campaign history observation reads share the existing total read ceiling
across fetched pages. When an even allocation leaves capacity on the newest
page, one slot can move from the oldest page with an equal share to the newest
page. The total number of observation reads stays bounded; each page still
selects its newest references, and the projection reports per-page budgets and
omissions. This helps retain recent pressure
and improvement context without claiming that skipped older records contain
no relevant evidence.

## Entry and persistence observation

The bounded readback
[`session-entry-store-serialization-pressure-20261026.json`](../evidence/environmental-capability/session-entry-store-serialization-pressure-20261026.json)
records two interrupted local entry attempts. One captured Python at roughly
80% CPU and 502–515 MiB resident memory; the second traceback reached JSON
serialization of a canonical Store snapshot during the ambient pass. The
session identity and persistence outcome were not returned, and the snapshot
size and cause of the serialization cost remain unmeasured.

[`ambient-service-decision-trace-gap-20261027.json`](../evidence/environmental-capability/ambient-service-decision-trace-gap-20261027.json)
records a later entry that persisted a session but was interrupted before its
response returned. The ECI compact view preserves bounded readiness, session
activity, and `/proc` process-sample facts from this record under
`observation.ambient_service_evidence`, alongside the exact evidence pointer.
It marks ambient fulfillment and causal attribution as unestablished; the
record does not connect the observed process I/O to its capability calls or
identify the cause of the earlier Btrfs incident.

## Current evidence path

The verified live readback in
[`native-capability-evidence-links-20261026.json`](../evidence/environmental-capability/native-capability-evidence-links-20261026.json)
connects three existing records:

1. The Environment owner-audit observation identifies owner-declared native
   MNCS-Test surfaces and remaining host responsibilities.
2. An append-only correction observation targets that audit by its exact
   integrity-addressed reference and carries the MNCS-Test P-001 pressure
   source.
3. A native process capability correspondence links that same P-001 source to
   pressure `MNCS-TOOLING-B665F138D324` and candidate API
   `mncs.std.process.v1`.

The projection confirms that the correction target matches the audit reference
and that the P-001 source pointer is integrity-verified in both linked
observations. Its current correction status is
`replacement-values-unverified-or-unchanged`, and the improvement relation is
`not-recorded`. The view therefore preserves the source trail without claiming
that the original audit was changed. The linked process correspondence also
exposes integrity-addressed pointers to the MNCS process language contract,
compiler support, and runtime provider. These references make the candidate
implementation inspectable; they do not establish consumer integration or
implementation responsibility for an owner-declared host task.

The 2026-10-27 readback
[`unselected-proposal-worktree-preflight-20261027.json`](../evidence/environmental-capability/unselected-proposal-worktree-preflight-20261027.json)
connects an explicit, unselected worktree proposal to a fresh registered
checkout observation and its `authority.proposal-eligible` event. The
Environmental Capability projection reconstructs the same branch, HEAD,
checkout identity, and clean-tree evidence without a persistent claim or an
executed edit. A focused fixture covers the case where the earlier workspace
projection omitted the worktree; a later independent agent's adoption and any
reduction in coordination overhead remain unmeasured.

The authentic `authority.integration-eligible` event in
[`unowned-scope-integration-observation-20261027.json`](../evidence/environmental-capability/unowned-scope-integration-observation-20261027.json)
is linked to its exact session resource-sample identity by the projection. The
link checks the sample identity, campaign, session, Work Intent, stored artifact
digest, and invocation/completion event references. Its append-only context
repair is recorded in
[`unowned-scope-resource-sample-correction-20261027.json`](../evidence/environmental-capability/unowned-scope-resource-sample-correction-20261027.json).
The sample is a host snapshot captured around the operation; it is not
agent-attributed resource consumption, a pressure finding, or evidence that the
preflight executed a Git operation. The readback keeps coordination overlap
visible beside eligibility without treating the overlap as a persistent
ownership requirement.
The bounded live projection result is preserved in
[`resource-sample-observation-live-readback-20261027.json`](../evidence/environmental-capability/resource-sample-observation-live-readback-20261027.json).
It shows the exact sample, event, artifact-digest, and Work Intent joins while
retaining the distinction between a preflight observation and an executed
integration.
The follow-up
[`compact-event-reference-live-readback-20261027.json`](../evidence/environmental-capability/compact-event-reference-live-readback-20261027.json)
records how the compact ECI row preserves the exact eligibility-event
reference and matches it to the selected session's activity. It keeps the
recorded authorization basis, eligibility basis, claim requirement, and
coordination overlap beside their distinct evidence, while retaining the
bounded claim-liveness and non-execution limits.

The 2026-10-27
[`coordination-aware-diagnostic-deferral-20261027.json`](../evidence/environmental-capability/coordination-aware-diagnostic-deferral-20261027.json)
records a verified Debug and Environment integration: a verification capture
that meets an overlapping coordination claim is deferred before provider
invocation, retains the required next step and exact authority event cause, and
appears through the existing coordination-deferral projection. Policy denial
remains a separate outcome. This is regression and projection evidence; live
agent recurrence and reduced coordination effort remain unmeasured.

The append-only
[`coordination-aware-diagnostic-deferral-source-correction-20261027.json`](../evidence/environmental-capability/coordination-aware-diagnostic-deferral-source-correction-20261027.json)
supplies exact commit, path, and digest references for the test and proof files
linked by that observation. It targets the original observation by its
content-addressed reference and leaves its recorded behavior and verification
unchanged.

The related
[`retirement-coordination-deferral-20261027.json`](../evidence/environmental-capability/retirement-coordination-deferral-20261027.json)
records a second verified caller of the same distinction. Branch retirement
still requires its exact scoped lease before deletion, while the CLI now reports
that missing lease as `coordination-deferred` with a concrete next step instead
of requesting owner authorization. The focused regression confirms no
retirement adapter ran; live recovery and coordination overhead remain
unmeasured.

The selected campaign evidence now records an existing MNCS Debug consumer of
`mncs.std.process.v1`, its duplicate-library-root compile pressure, the resolver
change at Debug commit `a036a0042bd0529ef8401b952eb921146e01590d`, and the exact
verification observation linked by the improvement relation. The projection
joins these records by the exact capability identity, pressure-hypothesis
identity, and verification-observation identity. The resolver unit tests (2/2)
and native-core suite (6/6) passed; the consumer integration compiles and reaches
native execution but still fails its typed-success assertion (`success=false`,
exit code 0, expected output). A separate Language runtime pressure hypothesis
records that the lifecycle reason is not exposed. The projection preserves this
as an actionable follow-up and does not attribute the runtime result to the
resolver change. Agent discovery and use, recurrence, long-term effectiveness,
and native migration readiness remain unestablished. The relationship still
does not establish that the process pressure caused any particular owner-declared
host responsibility.

When a bounded selected evidence page contains this consumer-pressure cluster
but not its older capability correspondence, `implementation_accountability`
surfaces it through `native_consumer_activities`. The projection keeps this
direct view deduplicated against activities nested under returned
correspondences and retains the same exact evidence identities and source
references.

When no exact join exists, `unmatched_source_pointer_context` shows bounded
integrity-addressed owner-audit pressure sources beside the candidate native
pressure sources. The comparison uses the same repository, commit, path, and
SHA-256 tuple as the positive link. It makes missing provenance inspectable;
it does not conclude that the sources are unrelated or causally connected.

If the owner source details and native correspondence declare the same pressure
ID but the owner side lacks its pressure source pointer,
`pressure_identity_context` exposes that identity-level match beside both
inspectable source records and the missing-pointer status. It leaves
responsibility matching, causality, and native migration readiness
`not-established`.

## Pressure recurrence and role coverage

`pressure_hypotheses` counts only observations that explicitly declare
`pressure_hypothesis_role: occurrence`. Explicit supporting-context records and
validated improvement-linked records remain visible with their source
references but do not add recurrence. Legacy observations without a role are
reported as unclassified evidence rather than presumed occurrences. The
projection's `observation_count` counts classified occurrences. For identities
with supporting or unclassified evidence, the summary adds
their counts, total evidence count, role-coverage status, and bounded evidence
pointers with observation kind and recorded session/campaign context. Omitted
supporting and unclassified counts mean zero. Within-session recurrence
requires complete, matching session attribution; across-session
recurrence requires at least two distinct recorded session IDs. This lets an
agent inspect authentic evidence without silently promoting missing metadata
into an occurrence or a recurrence claim.

Where multiple hypotheses or incomplete role coverage make aggregation useful,
the projection exposes `pressure_evidence_summary` for verified observations
in the selected evidence window. It separates occurrence, supporting-context,
and unclassified record counts from hypothesis recurrence statuses. The summary
reports whether its selected window is complete; existing source metadata gives
the page/history truncation, unavailable-record, and read-budget details. A
`read-complete-for-selected-window` status does not claim complete campaign
history. These counts are evidence coverage, not a service or agent score. A
response-size limit can omit this aggregate to preserve source evidence; the
per-hypothesis rows remain available.

## Measurement limits

- Systemwide native adoption remains `not-measured`; these observations are not
  a complete implementation inventory or denominator.
- The captured context response below is session-level evidence and is not
  attributed to the candidate process API. Agent receipt and successful or
  failed use of that capability remain unmeasured.
- Recurrence across sessions is not established by shared owner or pressure
  records.
- A future implementation or migration requires independent correctness and
  reliability verification. This projection does not declare success.

## Live context-delivery evidence

The older authenticated session-event page can be inspected with:

```sh
./scripts/mncs-env projections ses_9ad3efb63438cf3d \
  --interpret environmental-capability \
  --session-event-before-sequence 42 --session-event-limit 1
```

The bounded readback of this query and its separate current context view is
preserved in
[`session-context-delivery-readback-20261026.json`](../evidence/environmental-capability/session-context-delivery-readback-20261026.json).

The projection for session `ses_9ad3efb63438cf3d` records work intent
`int_8475157f281c835f` (“exercise canonical Environment persistence through the
Control socket”). Its authenticated `session.context` event
`evt_de29b1e1b27f5e61` at sequence 41 has a captured response summary for nine
available bindings. The digest and aggregate availability counts match the
current session binding snapshot, while the exact binding ID list is marked
partial and contains no IDs. The projection therefore exposes a concrete
capability-delivery measurement gap, but does not claim that any particular
capability was absent, received by the agent, or used. In this selected page,
the per-binding inclusion view reports `identity-tuple-in-matched-response`
for returned binding rows and
`binding_id_attribution_status=binding-id-capture-incomplete`. This is a
derived identity-tuple relation to the response digest and current session
snapshot, not a direct binding-ID observation; the binding list is truncated
in this page. Agent receipt and use remain unestablished.

The current `mncs-env context` view exposes all nine available binding IDs,
and Control's bounded capture code persists IDs when they are present in a
`session.context` response. That does not retroactively change event 41. The
next useful observation is a subsequent authentic Control context event, so
the projection can verify whether its summary contains exact IDs and compare
that event's binding-level view. The assessment remains session-scoped and
`target_capability_attribution_status` remains `not-established` until a
specific target capability is linked by adequate evidence.

For the broader evidence history, add `--history-pages 4` to that command.
The current expanded page has no exact shared source-pointer match: its owner
audit lists MNCS-Test pressure sources P-004 and P-006, while the candidate
process correspondence points to P-001. The projection exposes both sides in
`unmatched_source_pointer_context` with their integrity-addressed source
references and leaves causality `not-established`. Their SHA-256 values were
also rechecked against the three source files at the recorded MNCS-Test commit.
This is a provenance gap
for further inspection, not evidence that the records describe unrelated
work. The MNCS-Actions source audit also declares pressure identity
`MNCS-TOOLING-B665F138D324`, which matches the process correspondence target;
its owner pressure-link record says
`no-matching-supporting-source-reference`. The new `pressure_identity_context`
exposes this ID-level traceability beside the verified MNCS-Actions audit and
MNCS-Test P-001 source pointers without asserting that the process capability
resolves a particular host responsibility. The source audit's remaining host
responsibility mentions selected consumer/provider invocation and identifies
typed process/filesystem effects as an eliminating capability. The projection
surfaces this as `candidate-text-overlap-only` for the process API, while
`responsibility_match_status` remains `not-established`; the textual overlap is
a lead for inspection, not a migration conclusion. The readback is preserved in
[`unmatched-native-pressure-context-readback-20261026.json`](../evidence/environmental-capability/unmatched-native-pressure-context-readback-20261026.json).
The expanded identity and responsibility view is in
[`pressure-identity-context-readback-20261026.json`](../evidence/environmental-capability/pressure-identity-context-readback-20261026.json).
