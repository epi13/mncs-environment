# Slice A: payload efficiency

Baseline: `7fa937b`. Measurements were taken on 14 November 2026 in the actual
Projects workspace using its canonical Store and selected project definition.
The extra linked implementation checkout explains the difference from the first
arrival samples (2,571,710-byte workspace and 4,443,881-byte resolve).

| Operation | Expanded bytes | Selected view bytes | Cold seconds, before → after | Peak MiB, before → after |
| --- | ---: | ---: | ---: | ---: |
| Workspace, 296 checkouts | 2,602,332 | 310,419 | 2.687 → 1.777 | 43.23 → 33.49 |
| Resolve, 189 checkouts / 210 bindings | 4,480,675 | 1,151,391 | 9.031 → 8.205 | 385.98 → 382.44 |
| Own-session inspection | 1,113,121 | 187,246 | 6.131 → 5.994 | 374.18 → 374.28 |
| Global status inventory → count | 17,148 | 272 | 5.979 → 5.644 | 374.27 → 374.34 |

Workspace, resolve and whole inspection use lossless `--compact`. Status count
is explicitly selective; IDs remain accessible in the default or paged view.
The workspace-only inspection was 58,266 bytes and 5.823 seconds, avoiding
unrelated inspection assembly and full event-history reads.

`*-before.json` and `*-after.json` include warm-process measurements,
serialization costs, invocation counts and structural duplicate counts. Cold
means first request in a new Python process, **not** a flushed OS page cache.
VmHWM measures process peak RSS; output capture is included identically for both
versions. Warm RSS is cumulative for that process, not an incremental allocation.
Single samples are descriptive and do not establish a latency percentile.

Workspace inventory subprocesses fell from 303 to 94; resolve inventory calls
fell from 218 to 47. Both versions performed exactly 210 availability checks in
resolve; compact output adds no live readiness/provider calls. Warm discovery
already benefited from the existing inspection cache; compact processing has a
small cost (workspace 0.080 → 0.101 seconds). Serialization is only milliseconds;
most residual resolve/inspect/status latency and RSS is Store metadata lookup.
A baseline cProfile resolve attributed 10.01 of 15.53 instrumented seconds to
read_claims/find_bound_objects/domain-binding indexing (134,847 binding reads).
That is an owning Store indexing pressure, not JSON formatting or permission to
change Store here. No Store code or storage schema was modified.

The measured repeated-worktree/toolchain collection copies fell from 274 to zero
in resolve and 215 to zero in workspace. The view also shares checkout provenance
and selected-checkout collections. Every reference points into its own response,
including source references and observed revisions; no projection cache, manifest,
or new source of truth was added. Default CLI/Python representations remain
expanded. Git inventories are shared only for paths actually registered in the
selected repository; nested unrelated clones retain independent inventories.

`real-round-trip.json` records an exact recursive comparison of a real resolved
environment and its compact representation after pointer resolution. All binding
records, authority, claims, readiness, provenance and unknown states were retained.
The bounded tests additionally cover incomplete operations, revision differences,
escaped pointer paths, historical session inventory pagination, unregistered
clones, fresh inventory refresh and read-only file/Store generation invariants.

`scaling.json` is a supplemental bounded fixture, not a substitute for the real
measurements. At four repositories, increasing worktrees/bindings from 32/64 to
64/128 grew expanded output from 945,451 to 3,364,259 bytes; the compact view grew
from 129,294 to 256,549 bytes. Its exact round trip retains every fact. Allocation
measurements cover serialization and normalization, excluding fixture creation.
The smallest fixture grows by the explicit representation marker, as expected.

Validation before Slice A commit: 19 new response tests passed; 37 existing
capability/workspace compatibility tests passed; toolchain/projection selection
run passed (30 passed, 24 optional-provider skips, including earlier response
tests). The real Atlas vertical proof passed, including checkpoint, fresh-process
resume and handoff. Campaign proofs: 17 passed, zero skips/failures. Full-suite
results are recorded in the final completion report.
