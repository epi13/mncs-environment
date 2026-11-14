# Environment response efficiency and first-contact recovery

## A. Verified causes and corrected assessment

The initial current-state samples at `7fa937b` were 2,571,710 bytes for workspace,
4,443,881 bytes for resolve, and 608 IDs / 17,120 bytes for global status.
Current session context was already bounded: 24,316 bytes, with whole inspection
1,109,480 bytes. The old multi-megabyte workspace/resolve assessment still applied;
it would be inaccurate to describe current ordinary context as a 100,000-line
response or to treat global session enumeration as project-oriented discovery.
The local MCP bridge already reduced entry responses, but after generation and
with loss of configuration, navigation and availability-attention fields.

The main amplification was response assembly and per-checkout expansion, not
multiple independent authorities: every managed checkout serialized its Git
repository's entire worktree inventory. One inventory appeared 51 times.
Capability discovery also reconstructed equal runtime-toolchain provenance for
many bindings, including long declaring-provider collections. Selected checkout
collections and foreign-signal arrays repeated further facts. Pretty printing
magnified those structures but was not their cause. Projection materialization
was not the dominant origin of these endpoint payloads.

Compatibility was real: CLI/Python callers use nested expanded records; MCP
already exposed a string capability list/count and, for an unqualified full
capability list, JSON text as a string. Those defaults remain compatible.

## B. Structural changes

Slice A shares one observed Git inventory across actually registered checkouts
with the same current Git common directory. Unrelated clones, stale registrations
replaced by clones, and unreadable metadata fall back to independent inspection.
Toolchain observations/groups and checkout provenance are shared within discovery.
No persistent cache or new authority was added.

Explicit `--compact` on resolve/workspace/inspect replaces equal known repeated
observations with in-document RFC 6901 pointers. Every original fact remains
present, including source references and revisions; the default expanded contract
is unchanged. The compact traversal never serializes a full document merely to
throw it away and avoids traversing shared objects again. Expanded historical
snapshots are matched by content without provider reconstruction.

Inspection sections are selected before unrelated response assembly or full
history reads. Global status gains opt-in count and ID pages, without hydrating
historical sessions. Exact capability/binding selection retains complete records.
Authority CLI projection uses effective authority directly instead of obtaining
it through an unrelated full inspection; its authority/claim tests remain intact.

Slice B prioritizes selected writable work and selected providers over historical
worktrees, labels recorded context, exposes truncation and full inventory access,
and suggests bounded capability pages rather than expanding every binding.
Availability reasons and substrate-versus-verification distinctions remain intact.
The necessary MCP adapter preserves existing bounded native orientation and
attention, supports these selectors, and passes explicit detail through intact.
Only the new exact-binding query returns a structured list; the legacy full-list
string representation remains. Equals-form options preserve selected-session
parsing on existing owner processes. Principal/campaign/checkout/claim admission
and the preflight execution boundary are unchanged.

The existing workspace AGENTS guide now links to Environment's first-contact
instructions. This is a navigation pointer, not a family orientation service or a
permission-policy change. It is outside the Git repositories; its applied local
change and reproducible patch are retained in this directory.

## C. Measurements

Controlled primary comparison, including the implementation worktree:

| Operation/view | Before bytes | After bytes | Cold seconds | Cold peak MiB |
| --- | ---: | ---: | ---: | ---: |
| Workspace, 296 checkouts, lossless | 2,602,332 | 310,419 | 2.687 → 1.777 | 43.23 → 33.49 |
| Resolve, 189 checkouts / 210 bindings, lossless | 4,480,675 | 1,151,391 | 9.031 → 8.205 | 385.98 → 382.44 |
| Whole inspection, lossless | 1,113,121 | 187,246 | 6.131 → 5.994 | 374.18 → 374.28 |
| Global status inventory → explicit count | 17,148 | 272 | 5.979 → 5.644 | 374.27 → 374.34 |

Workspace output decreased 88.1%, resolve 74.3%, and whole inspection 83.2%.
A workspace-only inspection was 58,266 bytes. The real selected session's complete
17-binding list was 75,010 bytes; its summary was 11,627 bytes, with every
availability state retained and exact full details addressable.

Warm-process workspace: 0.080 → 0.101 seconds; resolve: 7.212 → 6.970 seconds;
inspection: 5.774 → 5.652 seconds. Normalization has a small cost on warm cached
workspaces, and tiny nonduplicated fixtures grow by the representation marker.
Workspace serialization was about 16 → 8 ms; resolve about 25 → 24 ms. These
are single observations, not latency percentiles or numerical admission criteria.

Cold Git inventory commands fell from 303 to 94 for workspace and 218 to 47 for
resolve. Resolve performed exactly 210 availability checks in both versions;
compact views add no provider readiness or diagnostic invocation. Warm caches
already avoid many inspections. Repeated worktree/toolchain collection copies
fell from 215 to zero for workspace and 274 to zero for resolve. References also
share other known observations; they do not suppress failure information.

Bounded supplemental scaling: at four repositories, increasing worktrees/bindings
per repository from 32/64 to 64/128 grew expanded output 945,451 → 3,364,259 bytes;
compact output grew 129,294 → 256,549 bytes. Every fixture round-tripped exactly.
`scaling.json` includes normalization/serialization allocation peaks; these are
separate from process RSS and exclude fixture creation.

MCP orientation intentionally retains more useful information: the same real
native context grew from 4,602 to 20,941 compact JSON bytes, with presentation/
serialization roughly 0.44 → 1.43 ms and incremental allocation 14 → 48 KiB.
The increase restores configuration, continuation, actions, binding IDs and
unavailable/unknown attention that previously required manual retrieval.

Live authenticated MCP checks against the existing owner succeeded: context
22,093 bytes, two five-binding pages 2,806 / 3,179 bytes, and one complete binding
3,101 bytes. The second page included available, unavailable and unknown states.
Pages/detail took about 33 seconds; initial context plus authenticated selection
60.6 seconds. This is a remaining host/RPC cost, not a serialization win.

Measurement scripts and JSON files describe cold/warm methodology. Cold means a
new process, not flushed OS caches. VmHWM measures actual process peak RSS;
warm RSS is cumulative. Real canonical workspace measurements are separated from
bounded fixtures and from owned temporary Store first-contact/profile exercises.

## D. Operational correctness and validation

A real resolved environment with all 210 bindings was compared recursively after
resolving pointers: every field matched, including authority, selected checkout
facts, provenance and unavailable/unknown data. Regression cases cover multiple
worktrees, shared dependencies, equal and changed revisions, pointer escaping,
unrelated/replacement clones, historical inventory pages, incomplete completion,
active-claim data, and early selected reads. File and real Store read tests show
no additional generation or snapshot publication. First-contact read navigation
also preserved its canonical Store generation.

No persistence schema, snapshot decomposition, transaction recovery, ledger
retention or per-pass provider memoization was changed. The projection epoch is
now deliberately uncached if the live claim digest changed between its decision
inputs and post-pass fingerprint. Otherwise a verifier admitted after a release
could produce PASS while the older foreign-claim decision remained pending, then
be replayed under the released-claim epoch. The deterministic regression fails
before the correction and converges on the next pass afterward. Stable claims
keep the existing quiet path; no extra provider call is added within a pass. Routine compact/summary
reads do not reconstruct providers, save sessions, scan historical event payloads
or add provider calls. Unqualified legacy detailed inspection remains available.
The host RPC's pre-existing read-activity accounting remains distinct from the
native read-only commands; these views add no new accounting operation.

Baseline full suite: 1,332 passed / 46 skipped. Worktree suite: 1,318 passed /
82 skipped, with extra skips caused by older sibling-relative fixture paths.
Canonical-checkout full run: 1,354 passed / 46 skipped / one failure. That
failure exposed a real mid-pass claim/epoch race, reproduced deterministically
against the prior code and corrected in `a8072da`. The complete affected projection,
verification, response and persistence suites were rerun after correction:
146 passed, zero skips/failures (`response-projection-tests.txt`). No production cache or
provider failure was hidden by relaxing a test. The final targeted response/authority tests passed (33), existing
capability/workspace compatibility tests passed (37), vertical proof passed,
and all 17 campaign proofs passed. Control MCP: 375 passed / 12 skipped, with
existing multiprocessing fork warnings. Changed Control files pass Ruff.

## E. First-contact evaluation

`first-contact-workspace.json` records the final exercise beginning at the actual
workspace `AGENTS.md`, following its link
to the actual Environment README, and uses its documented project-definition
entry plus returned actions. Fresh processes and an unrelated cwd retrieve a
bounded capability page, exact unavailable binding provenance, compact/full
checkout detail, claims and live health; they verify unchanged generation for
reads, continue the same session and complete/clean the owned temporary Store.
All 11 steps succeeded without final manual workarounds. Native availability
remained degraded (8 available, 3 unavailable, 6 unknown), not manufactured PASS.

This is an author-defined scripted exercise, not an independent unfamiliar agent.
The small owned Store uses real repository/capability data but is not a proxy for
canonical host latency. A separate live MCP exercise verifies authenticated
selection and real pages/detail against the large canonical host environment.

Automatic context now exposes selected work, actual configuration/intent,
authority, readiness, available binding IDs, attention counts/reasons,
continuation/claims/recovery and executable next actions. Complete inventories,
full provenance, live health and operation-specific effects/claims are deliberate
detail retrieval. Owner-defined generic Work Intent text still cannot infer the
user's exact new objective; the selected source/intent is now visible rather than
silently merged with a different development definition.

Atlas is not abandoned: the real context capability returned 947 bytes in the
vertical proof and current registry/site-check/refresh consumers were inspected.
No host Atlas refresh timer was observed here; that does not establish non-use.
Atlas owns ecosystem orientation, Language Service semantic observations, Forge
retained execution and Doctor diagnostic policy. Environment composes them.

## F. Remaining bounded follow-ups

1. Large canonical Store metadata indexing dominates ordinary resolve/status/
   inspection cost and memory. A baseline cProfile resolve spent 10.01 of 15.53
   instrumented seconds in claims/bound-object/domain-index lookup, reading
   134,847 bindings. No Store migration, compaction or cache redesign was attempted.
2. Host MCP admission, continuation and existing read-activity accounting still
   impose substantial latency: about 33 seconds for a 3 KiB detail response,
   versus about 5.7 seconds for native reads. A warm pre-change MCP entry was
   58.9 seconds; a new target-project entry was 147.5 seconds. These scopes and
   Store sizes differ from the earlier 31-second assignment; there is no claim of
   a like-for-like universal entry speedup. Cold per-selector session discovery
   can hydrate historical candidates; that source path warrants a dedicated
   measurement/optimization assignment without weakening authentication.
3. Even a small owned Store has startup cost: instrumented fresh/continued entry
   was 4.22 / 2.93 seconds, with Store initialization about 1.55 seconds and Git
   inspection about 1.25 / 0.76 seconds. A live RPC read also visibly initiated a
   Store application compilation; repeated-frequency/cache-admission behavior
   was not established and should be investigated with the owning provider.
4. An existing read-activity context-summary contract expects 64-hex binding IDs,
   while current native bindings use 16. A real eight-binding context reproduced
   `binding_ids_status: partial` with an empty captured ID list. New direct
   context and exact detail preserve real IDs/provenance. The separate audit/
   observed-capability capture mismatch is recorded reproducibly in
   `context-summary-follow-up.json`, not hidden by manufacturing complete evidence.
5. Existing projection failure memoization remains scoped to a pass and resets
   for a later pass. A provider recovered after a failed observation within the
   same pass may remain unavailable until the next observation; no response-view
   cache extends that lifetime or converts failure to health. No directly
   demonstrated response regression justified changing that mechanism here.

## G. Delivery and activation

Environment Slice A: `60a693e` (already published independently).
Environment Slice B: `2816c05`; verified response-coherence correction: `a8072da`.
Control MCP integration: `94ccc45`.
The full canonical run and corrective affected-suite results are retained above;
final pushed main states are provided in the completion answer. Unrelated work and the existing Control project-view modification
are preserved. Task claims/worktrees are released/retired after publication.

Native CLI/source consumers use the new code from main. Reconnect/restart the
local Environment MCP bridge to load the updated argument metadata/presentation;
a fresh bridge was verified against the existing owner without interrupting it.
The owner parser's added split-value support loads on its next normal restart;
new bridge options already use compatible equals form. No data migration or
standing agent maintenance is required. Workspace guidance is already applied.
