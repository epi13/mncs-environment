# Deterministic remediation campaign — 2026-11-07

This bounded campaign covers the seven problem families requested by the owner.
It does not implement learned guidance. Evidence and receipts belong in
`outputs/deterministic-remediation-20261107/`; campaign checkpoints reference
the committed records through the existing Environment ledger.

## Entry and source authority

Environment started at `46057a9f449021b081f5a586dede4b7b1eb8acb6`, Control at
`bce6941ddacd1c5c8934538c3e0b92616af2f604`, and Store at `4358df4`.
The primary models checkout is an older scaffold with uncommitted work;
the requested audit documents were read from `890262b8a656dacb95439d50bf37b5bd1c6d6bb4`
without changing that checkout. Foreign checkouts and modifications are preserved.

Authenticated Environment entry selected `ses_81bd16d081ab29e7` in campaign
`deterministic-remediation-2026-11-07`. Its exact isolated worktree claim is
`claim:mncs-environment:worktree:38265bf5f1d6`, version 1,
`clm_7951fd3b84f1f949`. A separate local observation entry selected
`ses_c7aaf235528a31af`; it owns no source claim. Store's default project bridge
refused its family-root definition as outside the narrow execution grant;
ordinary local Environment entry is the supported alternate transport.

## Bounded investigation inventory

| Family / owner | Current evidence and symptom | Disposition / correction / remaining verification |
| --- | --- | --- |
| Preflight / Environment, Control | Historical demand evidence records registered-root, ownership and whole-worktree effect deferrals. Capability listing had no operation-specific advisory. | Resolved: `capabilities --preflight` and the typed bridge expose existing checkout/effect/claim checks. They recover/transfer no claims and authorize no execution. The controlled admission replay reduces unsuccessful invocation requests from two to zero, with one actual execution in both paths. |
| Context and evidence / Environment and source owners | Current demand capture already uses selected-checkout observations; the missing-preflight guard and schema-aware Compiler/Language adapters exist. | Existing corrections retained and covered by current demand, intelligence and source-adapter regressions. New correction: effect-target discovery prefers a unique primary or exact selected checkout and refuses ambiguous linked-worktree inventory, independent of row order. Exact lexical historical aliases no longer duplicate current refs; symlink/missing history remains unavailable. |
| Capture / Control | Explicit stream availability exists at the cited current source; Environment has a separate provider-transport capture boundary. | Already resolved: nine capture cases verify available empty/content, incremental truncation, restart, expiry, legacy unknown and refusal. No raw output retention was added. Environment's own transport capture remains distinct from an inner terminal job's capture availability. |
| Test correlation / Test, Forge, Environment | Native `verification.execute_suite` attaches demand/request identities and `_finish` publishes immutable evidence before events. | Native relationship already works and is verified by a real declared Test obligation with durable Store restart/readback. Generic pytest remains blocked at the missing admitted producer contract; pressure `prs_86eca87538c8bbd1` records the exact Test/Forge/Control/Actions boundary. An Actions process-outcome carrier is not native Test verification. |
| Observation cost / Store, Environment | Five observed calls profile at 1.70 s, including about 1.60 s of small native SHA work (205 hashes, 580 retained calls). | Resolved measured redundancies: one-call native short/window hashing, reuse of identical single-chunk content identity, direct existing bulk-buffer digest without temporary fsync/reread. Final repeated full-path measurements and recovery checks below retain every event and generation. |
| Doctor reuse / Environment composition | All four named failures reproduce (13 other EpochTests passed). Fresh seed refs lacked the richer fields added by continuity reconciliation; its event invalidated the epoch without drift. | Resolved: initialize the same current checkout projection used on continuation, with exact lexical alias normalization. All 17 original epoch tests and two additional selected/history regressions pass; no test was skipped or weakened. Seven controlled reentries go from five snapshot saves each to zero. |
| Ambient delivery / Environment and providers | Existing scheduler, owner traces, bounded availability views and unknown fulfillment denominators remain authoritative. Capability inventory unnecessarily called full session inspection. | Resolved: capability listing reads its binding snapshot directly and performs zero event-history reads, with equal returned bindings. The existing capability action advertises operation advice. Campaign proofs now use the existing Git-aware family resolver instead of incorrectly skipping available Language/Memory providers from linked checkouts. No new scheduler or service was added. |

## Measured performance

`remediation_cost_proof.py` uses three fixed-seed randomized-order runs of eight
measured calls for every backend/path/handle condition (24 samples per cell),
with an untimed setup and separately recorded first call. The operation is the
same `/bin/true` capability and bounded context in every treatment. Fresh-handle
rows include open/admission in wall time; they are not OS cold-cache trials.
Native counters exclude handle construction. Every treatment reopens persisted
events and integrity-checks all contexts. No event or snapshot was suppressed.

| Full path | Baseline p50 / p90 | Final p50 / p90 |
| --- | ---: | ---: |
| Resident Store, existing path | 206.434 / 212.925 ms | 155.179 / 162.640 ms |
| Resident Store, observed path | 300.008 / 340.658 ms | 219.826 / 234.037 ms |
| Fresh Store handle, existing path | 1529.629 / 1543.652 ms | 1481.872 / 1531.502 ms |
| Fresh Store handle, observed path | 1630.349 / 1685.814 ms | 1544.530 / 1581.954 ms |
| Resident file-debug, observed path | 2.687 / 3.397 ms | 2.851 / 3.467 ms |

The resident observation increment (difference of pooled medians) falls from
93.574 to 64.647 ms, about 31%; observed-call wall time falls about 27%.
The intermediate native-composition-only and digest-reuse measurements are
retained separately, rather than attributing all improvement to one change.
All observed treatments retain three events and five Store generations per call;
controls retain two events and four generations. Per eight observed calls the
logical state grows 156,147 bytes before and after; baseline controls grow
143,344 bytes (resident condition). Native calls fall from 928 to 312.
This is lower computation, not fewer observations or reduced durability.
File-debug results show no demonstrated latency improvement.

Machine load was not isolated, and validation was running concurrently. Raw
per-run distributions, first-call conditions, native transport bytes, process CPU
samples and logical byte counts are retained in the receipts. Logical bytes are
not physical I/O; process-lifetime RSS is not per-call peak memory; native call
counts and VM steps are not wall time. No claim about physical I/O or agent
reasoning cost follows from these measurements.

Seven real-process file-debug Doctor fixtures reduce unchanged-entry median wall
time from 176.937 to 162.873 ms while avoiding five snapshot saves per reentry.
Those wall times are descriptive. The exact saved-write count and retained
service-ready outcome are stronger evidence than the small timing difference.

## Verification relationship and remaining boundaries

`scripts/remediation_verification_proof.py` runs the existing declared
`mncs-test.cross-module-suite-execution` obligation through real Test coherence
and execution providers. Its request, provider request identity, source revision
and fingerprint, selection, native result, immutable owner evidence and strict
native correlation remain in `native-verification-durable/`. The proof Store is
an ordinary durable Store artifact, not another persistence implementation.
Its session is completed and disposable filesystem compilation caches are removed;
referenced immutable Store invocation/cache bundles remain intact. Store evidence and
decision contexts were independently reopened and read strictly after completion.
The initial proof completion omitted the required outcome argument; recovery
completed the same session without another test execution. Both observations
remain in the campaign evidence.

Test's current ambient executor accepts native first-class suites. Generic
Control pytest has no matching admitted verification producer. Actions can emit
a check that an entrypoint exited successfully; its carrier does not by itself
bind this local Environment request, exact source and test selection or establish
native assertion semantics. Forge's declared provider/verifier receipts are not
an implicit generic pytest verifier. The generic join is therefore retained as a
precise producer-contract limitation, with an existing-pressure handoff, while
the supported native relationship is independently demonstrated.

Remaining resident invocation time is still dominated by native small SHA work:
the final five-call profile spends about 1.04 of 1.13 seconds there. A separate
five-reopen profile spends 6.67 of 7.92 seconds opening Store handles, including
5.66 seconds inside retained artifact/session admission and roughly 0.94 seconds
of host hashing across the profile. This identifies the Store/Language embedded
admission boundary; it is not a claim that required admission checks can be
skipped. This campaign adds no private admission cache or new resident service.

## Agent burden and the next campaign

Verified deterministic corrections and measured environmental reductions are
the repaired epoch, addressing/projection behavior, fewer fixture admission
failures, avoided discovery history reads, reduced snapshot writes and lower
native/Store invocation computation. Natural agent time, tokens, reasoning,
receipt, relevance and useful-work savings were not measured. These changes
have plausible agent benefits, but no verified agent-productivity saving or
complete demand denominator is claimed.

Residual uncertainty concerns which independently permitted diagnostic, test or
context operation will produce useful evidence when several remain legitimate;
whether delivered context is sufficient and consumed; and how to establish
usefulness independently. Checkout identity, claim scope, supported schemas,
capture availability, unchanged epoch reuse and exact evidence correlation are
deterministic prerequisites, not learned-selection problems. No learned model
was developed, trained, integrated or optimized.

## Interpretation

Correctness, environmental resource reductions, potential agent savings, and
verified agent savings are separate conclusions. Mechanical no-op probes and
regression tests do not establish agent productivity. Independent verification
and changed-state checks remain required work.

## Final verification and delivery

| Authority / check | Outcome |
| --- | --- |
| Environment primary baseline | 1,312 passed, 46 skipped, the four named Doctor failures reproduced. |
| Environment isolated suite | 1,287 passed, 82 skipped; no failures. Fixed-depth sibling fixtures explain additional skips; primary validation below supplies the available-provider coverage. |
| Environment final primary suite | **1,324 passed, 46 skipped**, no failures. All four original Doctor cases retain their assertions. |
| Control primary baseline / final | **364 / 365 passed**, the same four dependency-related skips; no final failures. All nine capture cases pass. |
| Store current canonical native/adapter suite | **140 passed**, no skips; includes native records, adaptive representations, provider transport, immutable publication/recovery/CAS, digest boundary oracles and artifact admission/cache checks. |
| Store focused / real process | 72 focused cases pass; competing real writers produce one COMMITTED and one STALE_GENERATION; crash recovery returns a verified old committed state. |
| Architectural proofs | Vertical proof passes. Campaign proof has 14 passing core checks; three corrected Language/Memory checks pass after using the Git-aware family root, replacing the earlier two path-induced skips. |
| Supported native Test relationship | One real declared obligation executes and passes; strict request/result/verification correlation and enduring Store restart/readback pass. |

The broad historical Store matrix is **incomplete**, not a suite PASS: 189 cases
passed before deliberate interruption after 46 minutes inside
`tests/test_phase2.py::test_general_blob_boundaries_roundtrip`. The owner explicitly
separates the canonical `test_native_substrate.py` regression from scheduled
historical/scale/all-backend matrices. The completed 140-case current canonical
suite and real-process proof are this correction's verification basis. No test
was skipped or weakened to conceal a product failure; the interrupted run and
its disposition are retained in the logs.

Primary Control validation caught one obsolete test assumption: an abbreviated
fresh seed used an absolute path, while established canonical campaign refs use
workspace-relative paths. The existing Control reader already resolves both
forms. The test now resolves the declared workspace root and still asserts the
exact checkout, recorded 40-digit HEAD, changed live HEAD, and selection behavior.
The intermediate failure is retained; the corrected full primary suite passes.

The further strict native correlation query costs 88.8–92.6 ms across five
descriptive samples for two request rows with one deduplicated context. Store
generation remains unchanged; initial Store-handle admission is excluded. This
query is separate from request observation. First-call/new-context resident Store
medians are 373.705 ms before and 277.231 ms after; corresponding observed-call
user CPU medians for the measured Python process (including its embedded native
execution) fall from 292.469 to 214.819 ms per call.
These fixtures establish resource observations, not agent reasoning savings.

Implementation commits merged and pushed to `main` are Environment `d1d9df0`,
Store `af50232`, and Control `9d8f65a`. The containing evidence commit and final
pushed refs are recorded in the campaign ledger and delivery response; receipts
pin tested bytes independently of their own containing Git commit.
Control's pre-existing uncommitted `.mncs/project-view.json` is preserved.

The Store source claim belongs to local session `ses_28a77f51dbc37020`
(`claim:mncs-store:worktree:e71222ee2735`); the Control source claim belongs to
authenticated session `ses_4ff3e54fbcb10151`
(`claim:mncs-control-mcp:worktree:b47dbb0b752f`). Store's local transport work intent
and the read-only local observation context use the separate local campaign
identity recorded at entry; no principal or ownership was silently transferred.
The primary campaign ledger consolidates their exact published source/evidence
references without widening session authority.
