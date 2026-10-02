# Incremental ambient coherence — second integration pass

The entry path now resumes durable observations and routes affected owner
passes through native Automation. The campaign remains **NOT CLOSED**: provider
build/execution provenance, owner-driven artifact repair, resident convergence
and exact shared evidence admission are still incomplete.

## Entry, selection and ownership

The requested entry first encountered the sandbox's read-only home state
location. All operational proof used **Store**, with explicit writable state at
`/home/epi13/Documents/Projects/.mncs-incremental-state`; file persistence was
never substituted as the normal substrate. Initial session:
`ses_ee67c9026473ab95`; owned implementation session:
`ses_85713066d9dfad8e`; final normal selected session:
`ses_530089515f636fde` (consumer `sol-incremental-ambience`).

All thirteen requested repositories' origins, mains, branches, worktrees and
manifests were inspected. A read-only inspection of the original shared Store
observed generation 2598 and 34 nonterminal sessions. Foreign compiler, Language,
stdlib, Test, LS and Commons work was preserved. Six owned repository worktrees
received explicit claims; completed slices were delivered and their claims
released. The implementation session has a durable checkpoint listing the
remaining work. The untouched Actions baseline worktree is retained as evidence.

The selected compiler is Language's `target/release/mncs`, source checkout
revision `425de2016a1273b66fa44609ea75aa25e04f0a0f`, with foreign source changes
left untouched. Observed compiler bytes:
`sha256:4387bae352b68020a83edbbb312794522a76c9f618cc7548a8f68384c63ecb40`;
embed bytes:
`sha256:8cf2f1aa2364f6275097ead51cb1826dd6152267dcb3a7bfd19797cd04d58339`.
These are byte observations, **not** source/build/execution receipts.

Effective stdlib remains the dedicated `mncs-stdlib/library`, content identity
`sha256:17b46deea40d27ea0510f3a9c5a0ca29ea18a5aea670c5e3c1438f999a8f64ce`.
Origin main now publishes bootstrap at `d653f3c`; the selected foreign bootstrap
checkout `4f13827` has the same committed tree. Its foreign documentation changes
and Language's unfinished extraction are preserved. Store admission still uses
the effective discovered stdlib roots; normal entry has no Store SHA-256 MNE173.

The final observed Store status was generation 368 before checkpoint publication.
Replay cursor and generation are distinct; the earlier captured stable cursor
was 350. Normal default selection has no LS resident service/stream and no
relevant family capsule. The five-session family proof runs separately with its
proper provider contracts. No LS stream identity is invented for the default
entry. The required `workspace-readiness` service is **ready**, with no required
provider blockers. Broader discovery remains degraded: source-only exports lack
invocation descriptors, and verification reports `coherence-failed` plus the
pre-existing `mncs-doc.projection-check` lifecycle-unknown inventory item.

## Measurements

See [the machine-readable ledger](INCREMENTAL_COHERENCE_MEASUREMENTS.json) for
pins, byte identities and every sample. Measurements count Python subprocess
launches, high-level Store calls and logical puts, not filesystem syscalls or
physical Store writes. Baseline provider-child Git calls are not included in
parent instrumentation. Warm entry spawns no provider child, so that distinction
does not conceal warm Git work.

| Warm entry metric | This pass's initial baseline | Delivered selected entry |
| --- | ---: | ---: |
| Git launches | 129 | **0** |
| Native CLI launches | 0 | **0** |
| Other/provider launches | 2 | **0** |
| Logical Store puts | 12 | **0** |
| High-level Store reads | 18 | **9** |
| Wall time | 2.825–2.856 s | **2.146–2.267 s** |
| Compact context | 9,587 bytes | **9,566 bytes** |
| Ambient blocks | 951 bytes | **925 bytes** |
| Process descriptors after warmup | 5 | **5** |

An earlier delivered series measured 1.969–1.997 s with eight reads, before an
additional proof session increased selection reads. The final comparable series
above includes that session and background integration tests. Store native
admission/open remains the principal latency floor; this is not a millisecond
whole-entry claim. The previous family-only 1.41–1.54 ms baseline is preserved,
not conflated with full entry. No intelligence benchmark is claimed.

The 129 baseline Git launches comprised 57 workspace HEAD/dirty/untracked
queries, 18 separate HEAD queries, 48 branch/status/worktree queries and six
projection HEAD/diff/untracked queries. Their owners were workspace transport
(121) and projection verification (8). They reconstructed unchanged facts.

A fresh durable-session bootstrap on the merged entry code, with a warmed Store
artifact cache, took 4.632 s, 153 Git launches, two native calls, two provider
launches and 17 logical puts. This is **not** an empty-machine/bootstrap compiler
benchmark. A later external control/claim drift reconciliation took 3.776 s,
106 Git launches and seven puts, then the four warm samples were quiet. Full
loss-of-state recovery and all fifteen proposed live scenarios are not claimed
as completed.

## Event/invalidation architecture

`.mncs/coherence-passes.json` declares seven passes' input masks. Automation's
native `mncs.automation.coherence.v1` classifies bounded batches as current,
affected, bounded reconciliation or bootstrap. Environment transports filesystem
facts, Store publications, deadlines and derived owner changes. It retains
separate Store generations, replay cursor, CAS versions, family generation,
LS stream/generation, input fingerprints and evidence identities.

| Event/fact | Subscribed work |
| --- | --- |
| Prose file | Projections |
| Source file | Semantics, verification, diagnostics, projections |
| Declaration | Doctor, semantics, Actions, verification, family, projections |
| Claim | Family, projections |
| Verification | Verification, diagnostics, family, projections |
| External evidence | Actions, verification |
| Semantic change | Semantics, verification, diagnostics, family |
| Provider artifact | Every potentially dependent pass |
| Git control state | Every potentially dependent pass |

Subscriptions currently operate at pass/category and selected-workspace scope,
not individual semantic subjects or obligations. A selected pass may retain its
existing broad owner scan. Unknown Store publications reconcile conservatively;
foreign snapshot publications are not yet precisely scoped. This is a remaining
source of broad wakeups.

Filesystem metadata checks established catalogues without Git enumeration.
Directory/ref/index/worktree drift re-enumerates only the indicated checkout.
Executable/library bytes are hashed only after metadata change. Restoring mtime
does not conceal an edit because ctime/inode identity also participates. Tracked
sources and untracked additions/deletions are observed; bounds are explicit.
Git uses `GIT_OPTIONAL_LOCKS=0` so observation does not itself modify the index.

Three tiers are explicit: normal targeted update; uncertain bounded
reconciliation; bootstrap/recovery discovery. Corrupt or missing digest-bound
catalogues/results are rebuilt and never supply current authority. Native
Automation absence invokes the existing recovery paths without a host policy
mirror. Submodule/nested ignored dependency catalogues and directory-symlink
closures still need stronger contracts before extending reuse to those inputs.

Owner changes produce derived provider, semantic, external-evidence,
verification and diagnostic events. At most three routing waves run, each pass
at most once. Edits during a pass remain pending. Store replay after effects
cannot silently adopt a racing peer generation. Overflow or unreadable history
requires reconciliation. Metadata-only replay uses Store-owned `(schema,
identity)` pairs and still verifies actual payloads at owner admission.

## Revisit, authority and context

Automation's existing generic revisit law is extracted and shared with
projection logic, removing the duplicate implementation. The native routing law
also composes that revisit authority. Environment transports family-owned
reconciliation/claim-expiry deadlines, provider observation leases and the
external boundary's pending deadline. Domain retry semantics remain unchanged.
A minute lease wakes only Doctor for the declared checkout-readiness service;
uncontracted volatile services retain live probing. This does **not** replace
all resident/watch loops: reconciler, LS stream polling and projection watch
still have independent entry points.

Normal entry opens current sessions without a redundant resume event. Explicit
resume and lifecycle recovery retain their participation semantics. Warm Store
opens are read-only, verify selected objects and promote the same admitted native
session to writable recovery/full verification only when work actually writes.
Highest snapshot revision is selected before historical JSON decoding. Session
matching still reads candidate snapshots; a precise durable selection index and
owner-level reusable admission can reduce the remaining read/open cost.

Mutation re-reads live claims and the exact selected checkout; cached entry
facts cannot authorize a write after foreign claims or external edits. Existing
claim gates, preimages, two-phase reconciliation and post-repair renewed PASS
remain intact. Doctor still owns remediation, but the direct family edit/gate
seam has **not** been moved this pass. No automatic provider rebuild capability
or new semantic ownership-move transform is fabricated.

The seven context budgets and expansion handles remain unchanged. Cached result
blocks and catalogues are digest-bound disposable session artifacts referenced
from Store. Quiet entry adds no scheduler block or trace write. `inspect` exposes
bounded trace, result/catalogue handles, artifact/library observations and policy
receipts; it omits the expanded host-policy file catalogue. Normal model context
remains approximately 9.6 KB, with healthy empty domain blocks still omitted.

## Provider/evidence provenance and compiler boundary

Observed callable files, compiler/embed, effective stdlib content and provider
manifest/bundle declarations participate in invalidation. The native policy
receipt binds its actual compiled artifact SHA/backend. All other observations
retain `build_origin: unknown`: present bytes cannot prove current source build,
loaded host code, runtime ABI, executor identity or a producer's exact execution.

| Area | Result / remaining requirement |
| --- | --- |
| Stale artifact | Byte replacement and source/descriptor drift invalidate dependent work; source-to-build mismatch cannot yet be established from an owner receipt. |
| Owner-driven repair | Provider-owned bounded build/reconcile descriptors and exact post-build receipts remain missing; Doctor is not taught repository-specific build commands. |
| Shared provenance | Byte/library/input substrate is available; fresh inventory, environment-affecting inputs, build configuration, ABI and execution origin remain owner obligations. |
| Test cross-session reuse | Deferred; exact provenance equivalence is incomplete. Session-local and renewed post-repair proof remain strict. |
| Debug cross-session / CI folding | Deferred; exact failure/request/source/toolchain and producer receipts remain incomplete. Same-line failure is never treated as equivalence. |
| Actions shared dispatch | Deferred; exact remote request identity, shared transport and ambiguous network recovery/idempotency remain incomplete. |
| LS integration | Existing owner semantic facts/impact bridge and derived semantic events remain. Direct resident stream/cursor and subject-level scheduling are not unified here. |
| Status vocabulary | Native routing/revisit dispositions are shared; Test verdicts, Debug sufficiency, external receipts and remediation reasons retain their own meanings. |

[The proposed compiler boundary](compiler-ambient-boundary.proposed.json) names
semantic/declaration/artifact inputs, affected closure, exact compiler/library/
configuration/inventory provenance, compiled generation publication and
supersession. Compiler will own continuously materialized typed/compiled state;
LS owns semantic facts; Test/Debug own evidence. No compiler internals, RAVEL or
general Memory integration are started.

## Validation and pressure dispositions

| Validation | Result |
| --- | --- |
| Environment final committed suite | **327 passed, 72 skipped**; optional provider fixtures are unavailable in this default profile. |
| Explicit native routing + Git/Store mutation/selectivity | **22 passed**. |
| Routing plus complete session tests | **49 passed**, including fresh foreign-claim, dirty-target and missing-target refusals. |
| Family policy and Store collaboration suites | **33 + 4 passed** with explicitly matched proof artifacts. |
| Store embedded API/CAS/recovery/feed | **31 passed**. |
| Store broad suite | First 162 tests completed; stalled for over 25 minutes at legacy `test_general_blob_boundaries_roundtrip`, then interrupted. Remaining tests unverified; native/layout code unchanged. |
| Automation existing suite | **54 passed** with its explicit Test/native fixture. |
| Automation new native law | **6 passed**. |
| Commons pressure registry | Native validation: valid, no diagnostics; views regenerated. |
| Real vertical Environment lifecycle | PASS: invocation, denial, checkpoint, fresh-process resume, handoff and completion. |
| Actions impact-map coverage | **1 passed** after declaring the two real executable paths. |
| Actions whole suite with identical selected CLI/library configuration | Untouched main baseline **12 failed, 364 passed, 19 skipped**; delivered slice **11 failed, 365 passed, 19 skipped**. Exact failure set removes only impact-map coverage. Owner project check retains **FAIL**. |

Native routing tests exercise external prose/source/declaration edits, repeated
edits to the same dirty filename, unrelated checkout quietness, HEAD advance,
provider byte and library-content changes, restored mtime, deadlines, derived
verification, racing Store publication, corrupt cache recovery, read-only
promotion and fresh Store reopen without subprocess work. Domain runners are
fixtures in these tests; real family concurrency, worktrees, claim release,
late join and SIGKILL recovery are established by the separate four Store tests.
Actual LS restart/toolchain-switch, CI receipt arrival, automatic provider
rebuild, distributed dispatch and full host FD audit are **not** proven here.

Actions' earlier four failures were reviewed. Executable mapping is repaired.
Parser tests pass without the complete library override, but retain `CMP301`
obligations with the full selected library configuration. Debug lineage becomes
available with the dedicated stdlib root; passing-test packaging/diagnosis and
native policy fixtures still fail in the broad selected-artifact profile.
Additional baseline failures include missing `mncs.test.assertions` provider
roots in temporary native fixtures. Nothing is reclassified as PASS or patched
inside foreign Test/Debug/Language work. These results demonstrate why exact
library/provider/execution provenance must precede aggressive evidence reuse.

Commons observations `MNCS-TOOLING-CC5E9CCFA538--OBS-135AC2332B96` and
`MNCS-TOOLING-9FEE08F6756E--OBS-2E1CADC30948` mark entry discovery and artifact
identity pressures **partially resolved**, leaving their compound status open.
MNE173 Store admission repair remains resolved. Resident scheduling,
source-to-build origin, cross-session Test/Debug reuse, CI folding, remote
single-flight, LS-P-007 candidate artifacts, Debug P-013 library provenance and
owner descriptor coverage remain open. No unsupported universal evidence or
lifecycle authority is introduced. Process FD count is stable at five in the
visible test process; sandbox PID isolation prevents a whole-host leak audit.

## Delivery and next campaign

Pushed main slices: Environment `57fb5a7`, `9123bb9`; Automation `e179ebe`;
Store `24fbff7`; Commons `db2f830`; Actions `4c2a903`. The final report/ledger are
delivered in a following documentation commit. Commons main was delivered through
an isolated clone because its original local main has foreign staged changes;
that worktree and local ref remain protected. Network briefly failed and later
recovered; all listed slices reached their origin mains without force pushing.

Next: owner-published artifact build/ABI/execution and effective dependency-root
receipts with bounded owner reconcile capabilities. Feed those events into this
router, then connect resident LS/Store streams and semantic-subject subscriptions.
Only then admit exact shared evidence and remote single-flight. Store admission
and durable session-selection indexing are the remaining warm-entry cost seam.

MNCS incremental ambient coherence status: NOT CLOSED

Blocking items:
- Provider-owned build/execution provenance and targeted artifact reconciliation.
- One resident scheduling path with LS stream/subject-level invalidation.
- Exact shared Test/Debug/CI evidence admission and remote dispatch coordination.
- Selected-provider verification/Actions baselines remain degraded.
