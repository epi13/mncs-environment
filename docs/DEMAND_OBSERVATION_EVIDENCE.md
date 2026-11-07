# Observation loop evidence — 2026-11-07

This phase implements evidence capture and deterministic correlation. It does
not demonstrate learned-model superiority, useful neural guidance, or verified
agent-burden reduction. `mncs-models` is unchanged; its previous negative results
and 71-test foundation remain the starting point.

## Authority and implementation

Reconnaissance used current Environment `159618f8ed04d193b986041a8fca38ad9c3bf1df`
and Control `f5915e679e9af969770aa23f4947225525422175`, which matched the historical
audit heads in this workspace. The capture ambiguity still existed: restoration
created empty buffers and a legacy read could report empty output without loss.
Control commit `bce6941ddacd1c5c8934538c3e0b92616af2f604` fixes that authoritative
boundary. See Control's `docs/OUTPUT-CAPTURE.md`, schema and source-bound receipt.
No terminal contents were reconstructed or added to durable retention.
A subsequent read-only live `terminal_jobs` check exposed the new contract on
all 100 retained rows (`unknown` legacy capture). `terminal_output` for
`term-2406e128ff82350dc8277363` returned empty strings, zero next offsets and
`output_truncated=false`, **alongside explicit unavailable/unknown capture with
null byte ranges**. That directly resolves the earlier interface ambiguity.
`live-capture.json` preserves the bounded metadata observation, not commands or
terminal contents. The service's exact build identity was not independently attested.

Environment extends its canonical Store evidence and event log. An actual
non-ambient bound request now references an immutable, deduplicated decision-time
context. Existing invocation/completion events carry the exact demand reference
and provider request identity. Native MNCS checks bounded identity, chronology,
ambiguity and outcome joins. Existing admission and verification remain in charge.
No new observation database, daemon, model runtime or execution authority exists.

During verification, two deterministic corrections proved necessary:

- The session-activity projection crashed when an effect target had no preflight
  event. Missing/malformed references now remain absent, with a regression test.
- Family discovery includes many other worktrees and may omit a selected linked
  checkout. Context now uses the existing selected-checkout observations directly,
  with exact paths and identity where recorded. It counts other inventory entries
  outside that scope. Truncation of relevant selected records still refuses a
  complete join. Earlier incomplete observations are preserved unchanged.

The Python adapter captures facts, validates existing owner evidence and transports
structured data. Authoritative join semantics execute in
`native/mncs/environment/demand_correlation_v1.mncs`. No host semantic fallback is
used if the selected native application cannot run. Control's existing Python
process boundary remains its legitimate implementation authority.

## Authentic bounded workflow

The evidence folder `evidence/demand-observation-20261107/workflow/` preserves
responses, selected historical projections, the JUnit result and a hash manifest.
This is this campaign's actual development-validation workflow, not a population
sample or an artificial productivity benchmark.

1. The task was verification of the published capture regression cases through
   the existing `mncs-control-mcp:test/control-mcp-tests` capability.
2. An isolated-family entry could advertise that capability, but the invocation
   refused an absolute checkout outside its registered primary family root.
   No provider ran. Normal entry with the real Projects root and project-local
   `working_checkout: current` profile resolved the addressing requirement.
3. Session `ses_ceab707839e80ef4` then recorded demand
   `evt_edeff6993630e50b` (sequence 48). Admission deferred because this campaign's
   earlier session held the main-checkout claim. The existing explicit handoff
   transferred only our claim.
4. Demand `evt_af7567705cf81f99` (52) still deferred: a six-path claim did not cover
   the test capability's declared whole-worktree effects. Authority event
   `evt_30594228903dfad2` (53) gave that reason and the normal remediation. Extending
   our own scope through the claim interface allowed execution; no guard was bypassed.
5. The first execution completed at event `evt_4e1e2b0834cfaf13` (61). Its older
   context captured a truncated family inventory. Native correlation correctly
   retained incomplete-context status rather than borrowing later observations.
6. Regression verification after the context corrections recorded demand
   `evt_2b6ae327112ed979` (74), invocation (75), and completion
   `evt_7c5ca99b9097e982` (76), provider request
   `req_9152cfa8c85ee83174012e02f8151e4db595e9346b943392`.
   The archived invocation is authoritative for that request identity if this
   prose becomes stale. The native focused projection matches demand, binding,
   provider request and chronology, with four selected checkout observations,
   an integrity-checked 11,565-byte context and no omitted relevant records.
7. The independent pytest runner reported nine passing capture tests and wrote
   the requested JUnit file. This Python test capability does not publish an
   admitted MNCS Test verification record. Consequently `verification_linked` is
   false, agent receipt/usefulness remain unestablished, and no avoided work is claimed.

Repeated executions during this campaign checked implementation corrections;
they are not independent utility trials. The first deferral finish events predate
the new direct admission-reference field. Their returned responses preserve the
existing authority references; history was not backfilled. An earlier CLI parse
failure and direct local shell work are not Session.invoke observations. No
private prompts, reasoning, or broad Control audit logs were inspected or captured.

The observations establish an actual service/admission workflow and expose
specific context and projection defects. They do not establish that the original
verification, retries, or investigation were redundant. Available, admitted,
response prepared, independently verified, useful, and burden reduced remain
separate facts. Current state is never used as historical capability evidence.

## Mechanical proof and costs

Run `python3 scripts/demand_proof.py` using the existing native toolchain. The
receipt binds source SHA-256 values, compiler binary, native result, arguments,
call result, raw timing samples and controlled projections. Eleven native tests
pass without fallback or host subprocess semantics. A single pure native join
with ordinary-length identity handles uses **2,442 VM steps**. That excludes
context capture, evidence lookup and structured application transport.

Five sequential no-op calls per treatment measured the full existing
admission/provider/persistence path. These are mechanical fixtures. Samples were
not randomized or isolated from all machine load; medians are descriptive,
not a latency guarantee or statistical causal estimate.

| Backend / path | Median call ms | Events / 5 calls | New event JSON bytes | Logical state growth bytes |
| --- | ---: | ---: | ---: | ---: |
| File-debug existing path | 1.869 | 10 | 6,357 | 10,675 |
| File-debug with observation | 2.267 | 15 | 14,029 | 18,817 |
| Store existing path | 216.661 | 10 | 6,357 | 76,783 |
| Store with observation | 319.867 | 15 | 10,944 | 86,339 |

The unchanged 712-byte fixture context has one shared immutable Store reference
across five requests. File-debug embeds context in each event. No additional
snapshot save is introduced; the original two saves per invocation remain.

The first implementation emitted a redundant finish on normal completion: five
calls added 20 events rather than the final 15. Its initial receipt and source
revision `62235cd4c2ed0456a4dab4232fcd624171a1baec` preserve that negative finding
(Store median 383.204 ms versus its 225.471 ms baseline). Reusing the existing
completion event removes that extra append. Independent timing runs are not a
controlled estimate of the optimization's time benefit; the append-count change
is exact. Store overhead remains material and is not justified by proven savings.

Contexts are bounded to 64 KiB, 256 bindings and 32 selected checkout observations.
Each query uses existing bounded event pages, at most 16 demand rows and 32 KiB of
deduplicated context bodies, with omission counts. A wide historical page can
omit bodies; a focused page can retrieve the exact source reference. No polling,
raw output retention increase or private cache service was added. Physical disk
allocation/I/O, peak memory, agent tokens and reasoning time were not measured.
Control metadata is approximately 372 bytes for an empty capture, 367 for the
truncation fixture and 412 after restoration; existing buffers remain bounded
(default 1 MiB per stream) and persisted job metadata remains capped at 100 rows.

## Verification and limitations

The source-bound verification receipt and logs in the evidence directory separate
the full baseline from final checks. Baseline: **1,263 passed, 82 skipped, four
failed**. Final: **1,276 passed, 82 skipped, the same four failures**, in 846.72 s.
All 13 new owner tests pass; the focused run with existing verification and
project-view checks passes 35 tests. Control passes **372 tests, five skipped**
(baseline 363 passed, five skipped); the new capture module passes nine cases.
The eleven native correlation tests pass with zero fallback and no host
subprocess semantics. The baseline already failed four Doctor epoch
reuse tests; they are outside this change's ownership and remain a reported
limitation, not hidden skips. Vertical and campaign proofs exercise entry,
protection, restart, handoff, bounded resources and completion. Mechanical tests
cover missing/tampered history, duplicate/ambiguous joins, chronology, prediction
rejection, trusted outcome references, native-unavailable behavior, refusal,
privacy bounds, Store restart/context reuse and ambient-call exclusion.

Current ambient service accountability already reports fulfillment as
`not-measured`, with an unrecorded eligible-opportunity denominator, and explicitly
refuses to compute a fulfillment ratio. That correct behavior is preserved.
The new request counts supplement its evidence; they are not a replacement
eligible-need denominator or a quiet-session success score.

No complete agent-demand denominator exists. Direct Control/terminal/LSP/RAVEL
activity is not automatically linked by this Environment-bound observation path.
Decision context is the last recorded owner state, not a live admission certificate.
Agent intention, context relevance, operation usefulness, task fulfillment and
reasoning costs still require independent observations. Process exit alone supplies
none of them. Existing ambience counts therefore gain attributable request and
execution evidence, not a new success score.

## Prioritized next work

1. **Deterministic delivery — Environment/Control capability owners.** Expose the
   existing registered-root and required effect/claim scope early in capability
   preflight. Compare normal task-matched workflows and retain outcome-preserving
   verification; success is fewer scope/addressing deferrals with equal required
   checks, not merely fewer calls. The present events identify this delivery issue,
   but do not measure the saving from a future correction.
2. **Outcome join — Test/Environment and each provider owner.** A provider-owned
   verification reference must bind the existing operation request, exact source
   revision/fingerprint and test result. Generic Python test exit/JUnit data must
   not masquerade as an admitted native Test receipt. Success is a strict,
   independently checkable result join; missing links must stay unknown.
3. **Service usefulness — Environment's existing ambient accountability.** Attach
   task-obligation satisfaction or explicit consumed-delivery acknowledgements to
   existing records where naturally available. Distinguish receipt from usefulness;
   retain unknown eligible-need denominators and compare completed obligations.
4. **Cost — Environment/Store owners.** Investigate existing batched/deduplicated
   append transport under the same durability and ordering guarantees. Demonstrate
   lower full-call cost without new storage authority or lost refusal observations.
5. **Possible guidance — models/learn after a real cohort.** Only after multiple
   legitimate pre-decision alternatives and independently verified usefulness are
   recorded should a tiny guide rank capabilities. Use deterministic preflight and
   the strongest deterministic routing baseline first. No suitable learned utility
   cohort is established here. Novel design and independent verification remain
   legitimate agent work.
