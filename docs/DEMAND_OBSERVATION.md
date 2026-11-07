# Request demand and decision-time evidence

Environment observes actual non-ambient `Session.invoke` requests without
inferring private intention or treating every tool call as a decision. Control
owns terminal capture; its `output_capture` contract distinguishes available
empty/content/truncated streams from restored unavailable and legacy unknown.
Environment does not reconstruct missing diagnostics from a successful exit.

The canonical request loop extends existing session events and Store evidence:

1. `demand.observed` records the requested capability, existing session/event/task
   references, bounded source interface, valid parent RPC request ID if present,
   and the snapshot sequence known before admission.
2. A bounded immutable `decision-context` record preserves the last owner-observed
   bindings, selected checkout observations, authority revision and rights-input digest.
   Repeated unchanged contexts reuse their content-derived Store reference.
   File-debug sessions embed the same bounded projection in the event log.
3. Existing `capability.invoked` and `invocation.completed` events carry the exact
   demand-event reference and provider request identity. They retain their existing
   admission, checkout-lease, failure and capture semantics.
4. Normal requests reuse that completion event as their finish reference. Only
   refusal, exception or deferred nonexecution needs a separate `demand.finished`
   observation, including an available admission-event reference. Provider completion
   and result availability never imply agent receipt.
5. Existing verification records publish their immutable evidence before emitting
   the corresponding verification event. The event points to that exact evidence
   digest and carries the selected operation/demand references. Verifier semantics,
   source freshness checks and PASS/FAIL/UNKNOWN policy remain unchanged.

This is an event-driven observation path, not another telemetry service or store.
No model is loaded, no operation is selected by a model, and observation grants no
execution permission. The original admission implementation remains authoritative.
Ambient owner-pass calls retain their existing trace and do not create duplicate
agent-demand observations. Own observation writes rebaseline the existing Store
feed so they are not mistaken for external work.

## Meaning and uncertainty

The capability set describes what Environment had recorded at request time. It
is not a live permissions certificate or a historical reconstruction from today's
bindings. Repository HEAD/branch/dirty observations and selected checkout identity
remain scoped observations, not proof of every dirty byte. Selected checkout
observations are taken from the existing selection record, including linked
worktrees absent from family discovery. Other discovered worktrees are explicitly
outside this repository scope, not silently counted as omitted selected state. Snapshot update time
is not necessarily the age of each provider observation. Freshness is explicitly
last-owner-observation, not a new TTL or live admission verdict.

Capability availability, readiness, admission, response preparation, agent receipt,
usefulness, fulfilled work and burden reduction remain different properties.
Unknown denominators remain null. A completed provider invocation cannot by itself
become verified work or usefulness. A verified outcome can still be unhelpful to
the agent. Source handles and native records do not authenticate themselves.

New observations copy no invocation arguments, commands, prompts or arbitrary
request-surface text. Existing capability argv policy is unchanged. Unmarked API
requests are unattributed; a CLI surface marker identifies a route, not a human
or agent decision. An invalid surface is refused without copying its raw text.

## Native correlation and bounded access

`native/mncs/environment/demand_correlation_v1.mncs` owns join classification.
`mncs_env/demand.py` captures facts and transports bounded rows through the existing
native application adapter. It checks exact request/binding/demand identities,
sequence chronology, missing context, duplicate observations and observed versus
prediction labels. Verification links additionally require a strict read of the
existing owner evidence and an exact matching result row. No unchecked claimed
verification flag is accepted from a provider response.

Inspect the existing session-activity projection with:

```bash
./scripts/mncs-env projections SESSION --interpret session-activity --activity-section demand
```

The report keeps demand, operation, capture and verification references alongside
explicit unknown receipt/usefulness/burden fields. Native classification requires
the session's selected native toolchain and Stdlib. When unavailable, raw evidence
remains inspectable and classification is `native-unavailable`; there is no Python
policy fallback. This does not prevent normal deterministic operation.

At most 256 bindings and 32 repository observations are captured, with a 64 KiB
context limit. Omitted/budget-refused context cannot be silently admitted as
complete. A projection reads the existing bounded event page, returns at most 16
request rows and 32 KiB of deduplicated context bodies, and preserves exact
references when details are omitted. Partial pages can contain orphan events;
they do not imply absent demand, failed delivery, or fulfilled needs. One immutable
context is read once per unique Store reference/digest within a query. File-debug
inline records are individually integrity checked. Store owns durable history and
retention; no additional polling or raw-output retention is introduced here.

## Evaluation and model-facing handoff

`python3 scripts/demand_proof.py` runs controlled file/Store cost probes and real
native tests/application execution. Its no-op fixtures are mechanical evidence,
not naturally occurring agent behavior or productivity measurements. Wall time,
event bytes and logical state-file growth remain separate units. They do not
measure physical filesystem allocation, peak memory, agent reasoning or tokens.

A future handoff to `mncs-models` RFC 0005 may map the exact demand event to a named
question, retain full owner references beside local handles, and provide only
pre-decision feature views. An independent authority must establish the currently
permitted candidate set: recorded availability does not grant permission. Missing
capture/state remains unavailable, not zero-valued evidence. Later verified outcomes
are evaluation data, never earlier input features. Existing disjoint/frozen
construction and held-out protocols and `mncs-learn` governance remain required.

No live learned guide, useful prediction cohort or verified agent-work saving is
established by this implementation. The next empirical step is ordinary request,
delivery/use and independently verified task outcomes at an explicitly named
residual boundary, compared with the strongest deterministic service. Independent
verification and legitimate retries must not be counted as redundant work.

The scoped validation profile `.mncs/demand-observation.environment.json` selects
Control and Environment plus read-only Language/Stdlib dependencies. From an
isolated linked checkout, use the registered family root explicitly:

```bash
./scripts/mncs-env enter --definition .mncs/demand-observation.environment.json \
  --workspace /absolute/path/to/Projects --consumer validation-consumer
```

Declared whole-project test effects on shared main still require their exact
claim scope. This profile does not bypass those checks or reconcile an entire
family of services. See [execution evidence](DEMAND_OBSERVATION_EVIDENCE.md) for
the authentic validation workflow, resource costs and remaining verification gap.
