# Incremental ambient coherence

Environment entry composes owner results through Automation's native
`mncs.automation.coherence.v1` law. Environment transports observations,
replays Store publications, obtains claims through existing session APIs and
assembles bounded context. It does not classify verdicts or authorize repairs.

## Current operation

`.mncs/coherence-passes.json` declares each ambient pass's invalidation inputs.
Automation receives up to sixteen rows in one request. A row carries established
result presence, observation-chain certainty, input and event masks, selected
scope, existing revisit wait/event codes, and a boundary deadline observation.
The native result distinguishes current, affected, bounded reconciliation and
bootstrap. Unknown chains never authorize reuse.

The seven owners retain their existing authority: Doctor, Semantics, Actions,
Test verification, Debug diagnostics, family collaboration and projections.
A changed provider observation, semantic state, external evidence or verification
state emits a derived event for another bounded routing wave. A pass runs at most
once per tick. Files changing during execution remain pending for the next tick;
after-the-fact observations cannot certify unseen inputs.

Normal entry opens the same durable session without recording a new resume
marker. Mutation authorization re-reads active claims and the exact selected
checkout at the authority boundary; cached entry facts are not a mutation lease. Explicit `resume` remains a participation operation. Lifecycle recovery
from checkpointed/abandoned state still records a transition.

## Observation tiers

* Bootstrap enumerates selected Git checkouts once with `ls-files`, then records
  filesystem metadata for those paths, their directories and Git control files.
* A warm tick stats the established catalogue. File changes invalidate subscribed
  passes; directory/control drift re-enumerates only that selected checkout.
* Missing or corrupt catalogues/results, unknown Store writes, replay overflow,
  changed policy/executor identity or moving inputs require bounded reconciliation.
* Without the native Automation provider, the existing owner discovery paths are
  explicit recovery paths. No host implementation of native policy is substituted.

Git probes use `GIT_OPTIONAL_LOCKS=0`. Read-only discovery must not itself refresh
index metadata and create another invalidation cycle. Git remains responsible for
HEAD/index/ref/worktree discovery after a control-file indication. Metadata is an
OS change indication, not a source-content identity or semantic verdict.

Catalogues and owner result blocks are immutable, digest-bound derived session
artifacts. The authoritative Store snapshot contains references and policy
receipts. Missing/corrupt artifacts are rebuilt; they cannot supply current
results. These files are disposable caches, not another collaboration database.

## Store publication and concurrency

The scheduler keeps a verified replay cursor distinct from the current Store
generation. After owner effects, it replays the intervening publications. It may
acknowledge an empty/self-only replay; it must not simply sample the latest
Store generation and discard a racing peer write. Publications arriving after
that replay remain visible on the next tick. A bounded replay overflow requires
reconciliation before adopting its new baseline.

Unchanged warm state performs no snapshot write, presence write, epoch write or
resume event. Snapshot reads select the highest immutable revision before JSON
decoding historical payloads. Warm entry opens the committed Store read-only and
verifies selected objects. Actual mutation promotes the same admitted native
session to a writable handle, retaining recovery, complete projection verification
and CAS. Replay uses Store-owned `(schema, identity)` binding observations rather
than materializing historical payloads. A notification is not payload-validity
evidence; owners verify their payloads before admission.

## Deadlines and resident boundaries

Services may declare `observation_inputs: ["selected-repositories"]` when their
response depends on selected checkout observations. The new
`workspace-readiness/1` capability returns bounded checkout readiness; the
existing `workspace-discovery/1` capability retains its complete response. Services without that
contract retain live probing. `observation_max_age_ms` publishes a bounded probe
lease; the default checkout discovery service uses sixty seconds for external
infrastructure recovery. Family publishes its existing reconciliation/claim
expiry deadlines. Pending/blocked Actions observations poll only their external
boundary once per minute when no receipt stream is available. Automation selects
work when a transported deadline matures; domain owners retain retry law.

This entry coordinator is not yet the resident reconciler's sole dispatch path.
LS probes without an admitted event/lease contract still use the existing Doctor
boundary. Full resident-loop convergence remains open.

## Artifact and evidence identity

Observation binds callable executable/script bytes and the selected compiler and
embed library by SHA-256, with metadata allowing digest reuse only while the
artifact is unchanged. The effective stdlib selection uses the existing shared
`language_library_for` rule, preserving dedicated ownership and explicit roots.
Its bounded inventory binds relative file names to exact content digests and
observes provider/compatibility/bundle declarations; warm
observation stats those files instead of rehashing the bundle. The native policy
receipt also records the compiled policy artifact identity, SHA and backend.

These observations deliberately retain `build_origin: unknown`. Observed artifact
bytes prove what is present, not that it was built from the current source or
configuration. No provider-owned build-origin receipt or automatic rebuild
capability is fabricated. Source/build/toolchain/ABI applicability must be
published by the provider owner before cross-session Test/Debug evidence reuse or
CI folding can be admitted. Existing verdicts and family post-repair renewed
verification gates are unchanged. Actions dispatch remains independently
coordinated until its exact identity and ambiguous network outcomes are handled.

## Generation laws and compiler integration

Store generation is a publication snapshot; row CAS version controls concurrency.
Family generation/cursor describes established project changes. LS workspace,
stream, cursor and generation describe semantic observation. Environment input
fingerprints classify invalidation, while evidence identity describes exact
applicability. None substitutes for another or forms a global generation.

The proposed compiler-facing boundary is in
`compiler-ambient-boundary.proposed.json`. It reuses `semantic.changed`,
`provider.artifact_changed`, the same native subscriptions and Store publication
semantics. Compiler owns affected closure, typed/compiled materialization and
proof obligations; LS owns semantic facts; Test/Debug own evidence applicability.
No compiler implementation or foreign extraction checkout is changed here.

## Trace and limits

`inspect <session>` exposes the durable `coherence.last_trace`, selected artifact
observations, library content identity, policy receipt and result handles. A trace
records the mode, invalidating events, scheduled/skipped passes, derived events,
enumerated checkouts, metadata path count and pending races. Quiet ticks do not
write trace history or add another context block.

Selectivity currently operates at declared pass/category and selected-workspace
boundaries. Semantic subject/obligation-level subscriptions, stream-reset routing,
provider-owned repair receipts and one resident scheduling path remain work for
subsequent integration. Unknown/unclassified publications use recovery rather
than silently supplying new architectural authority.
