# Spark descriptor exhaustion — October 4, 2026

## Finding and scope

The observed recurring failure is a **retained tool-output handle leak in the
installed Spark/Muse agent runtime**, not host file-table exhaustion. The
lifetime owner is `/home/epi13/.local/bin/muse-bin-1.4.2-R4684.1`, PID 320179,
parent Bash PID 320111, in a Konsole user cgroup. Its binary source is unavailable
in this workspace; no runtime fix is claimed. The exact retaining object or
missing destructor still requires the runtime owner's source-level audit.

The agent runtime session is `01a107a5-9d81-7d11-aadf-ac03faae697e`;
the associated MNCS consumer is `spark-backend-vm-20261004`, Environment session
`ses_f4f800e6cefd6628`. A durable MNCS session is not a Linux process. The
association here is supported by the runtime's committed campaign report and
native Environment claims, not inferred from a process name alone.

## Quantitative descriptor evidence

Existing `resource_usage_sampled` events cover 08:06–14:33 Alaska daylight time:

| Observation | Runtime FDs | Runtime RSS |
| --- | ---: | ---: |
| First recorded sample | 93 | 333 MiB |
| First idle sample | 106 | 347 MiB |
| Later idle sample | 566 | 555 MiB |
| Later idle sample | 918 | 738 MiB |
| Last recorded idle sample | 1,020 | 840 MiB |
| Independent procfs idle observation | 1,015 | 840 MiB |

There are 139 existing samples, including 70 with `procs_live=0` and
`unified_exec_live_sessions=0`. The runtime recorded repeated `os error 24`
(`EMFILE`) tool-spawn failures. It remained at 1,015 FDs across six independent
samples over 25 seconds with no direct children. Small 1,019/1,020/1,015
variation reflects transient non-output resources; the 947 output handles
remain. Completion does not return this workload to its earlier baseline.

The independent inspection found:

- 595 regular spool handles under the session's `tool-outputs/.spool/`;
- 352 published tool-output handles, including files and directories;
- 769 of these 947 handles are regular files; 178 are other output handles;
- 531 output targets have the kernel's `(deleted)` suffix in that inspection;
- 472 retained spool handles match call IDs with terminal effect records;
- all 947 inspected output handles have `O_CLOEXEC` in `/proc/PID/fdinfo`;
- the later native sample reports 14 sockets, six epoll handles, three eventfds,
  992 file/device handles total, and zero inotify handles/watch entries;
- no pipes or fanotify handles in the agent sample; no concurrent compiler,
  cache, Git or language-service child jobs in the idle observation.

The process soft/hard FD limits are **1,024 / 524,288**. Its parent shell also
has soft 1,024. Host `file-nr` was approximately 21,700 allocated handles;
`file-max` is 9,223,372,036,854,775,807 and `nr_open` is 1,048,576. Thus the
observed failure is per-process EMFILE, with a genuine accumulation exposed by
the low soft ceiling; it is not evidence that a reasonable steady workload
needs a higher limit. No limits were changed.

Later read-only monitoring found PID 320179 gone and a new Muse PID 447978
running `resume 01a107a5-9d81-7d11-aadf-ac03faae697e`, with 51 FDs at discovery.
No signal, restart, limit change or session command was issued to either agent
process by this investigation. This independently observed process replacement
reclaimed the old handles; it is not a runtime code fix and recurrence remains
possible. Spark's ownership remains authoritative across its continuation.

## Cache verification and RAM

The reported cache miss→hit check uses Spark's uncommitted
`mncs-compiler/tools/stage0-probe/src/main.rs` readiness cache in its claimed
`spark/backend-vm-20261004` worktree. Python suite harnesses start a probe and
run their existing double-run determinism checks. `MNCS_PROBE_CACHE_DIR`
selects `.build/probe-cache` by default in these edited harnesses.

Read-only inspection shows serial module traversal. Cache source reads use
`read_to_string`; load/store launches synchronous `gzip` with `Command::output`.
Rust's scoped file APIs and completed `Output` values do not explain hundreds
of output handles retained in the *parent agent*. The independent idle
observation had no cache-verification process left. Earlier assistant claims
that verification was still running do not establish present process liveness.

A controlled 1 MiB transport fixture repeated the same gzip file/output pattern
ten times: observer count **4 before / 4 after every run**, with no cumulative
FD growth. This verifies transport reclamation only, not the full compiler
cache or vendor runtime; the expensive compiler workload was deliberately not
re-run or built in a foreign worktree.

There are separate cache RAM/correctness risks for Spark to address:
`Command::output` eagerly accumulates decompressed JSON, then JSON `Value`
materialization and clones duplicate large programs/artifacts; cache writes
materialize JSON plus serialized bytes and compressed output. No decoded-size
budget is visible. Cache temporary filenames are deterministic per key, with
no visible single-flight/exclusive creation across simultaneous probes. A
failed gzip-output write may leave its `.json.gz.tmp`. These are source-review
findings, not demonstrated causes of the prior machine-wide RAM event. They
remain in the foreign worktree and were not modified.

Runtime RSS grew with its session history, but only about 1.7 MiB of logical
output-file bytes were retained by the inspected handles; that alone cannot
explain roughly 500 MiB of runtime RSS growth. The previous projection OOM and
this FD leak have **no proven common cause**. Current host available memory was
about 22 GiB of 31.1 GiB. Do not infer a memory-leak verdict from peak RSS alone.

## MNCS relationship and delivered change

Environment did not own the leaking output descriptors. Required native entry
completed as consumer `sol-fd-pressure-investigation`, session
`ses_9e54aa4ccc3fbce0`; required workspace readiness was ready, overall readiness
degraded with 50 unavailable optional capability bindings. Initial ambient
verification had two blockers; this was not concealed as full provider health.

The delivered adjacent improvement is `mncs-env resources`, exposed through
`actions.resources.argv`. It directly observes bounded caller ancestry or
explicit PIDs without Store startup, provider invocation, repository traversal,
new daemons, or target-process mutation. See [RESOURCES.md](RESOURCES.md) for
limits, live/unknown labels, PID birth checks, watch-entry semantics and scope.
No Doctor health policy or provider service identity is duplicated here.

The external leak is recorded in the existing pressure registry. The correct
runtime fix is to release spool/output/directory handles on every completed,
failed and cancelled tool path, including asynchronous terminal delivery.
Keep durable paths/references or bounded content in history, not open handle
owners. The owner should test hundreds of mixed shell/patch tools and repeated
session continuation under a 64–128 FD ceiling, measuring post-operation
baselines and cancellation, before considering any limit change.

## Safety and delivery

Native claims were inspected before mutation. Spark holds live claims on
compiler, language and VM worktrees under
`/home/epi13/Documents/Projects/.worktrees/spark-backend-vm-20261004`, all on
`spark/backend-vm-20261004`. No foreign files, branches, refs, claims, processes,
limits or services were changed. Historical sessions were observed, never
retired/reclaimed merely because they were quiet. No message was injected into
Spark's binary runtime.

Only Environment's clean main checkout was claimed for this investigation.
Two visible Forge plugin processes initially had seven FDs each, Commons six,
Fabric eight and Control MCP seven: none accounts for the observed pressure.
No resident-service kill/restart or new background service was performed.

Focused validation passed ten tests, including a real child with a 64-FD
limit (27 FDs with fixture handles, three after release), pipes, deleted files,
real inotify entries and truncation. One hundred repeated observations leaked
no observer FDs. A separate memory sample stayed at four FDs and 17.6 MiB RSS
(about 96 KiB startup/caching growth across 100 observations).

Native entry was repeated three times, reusing the same session, returning
zero, preserving degraded readiness, and returning the wrapper to four FDs
after every run (53.3s / 7.6s / 5.6s). Twelve partial-run procfs samples observed
at most six FDs and 392 MiB RSS in entry processes; these are sampled maxima,
not continuous all-process peak guarantees. Vertical persistence/authority/
handoff proof passed before and after the change.

The baseline full suite reported 513 passed, 18 skipped, three failures:
concurrent claim recovery admitted two winners; cursor restart emitted unexpected Commons
records; projection watch missed a canonical change. Campaign proofs reported
13 passed, one skipped (absent language-service binary), one failed idle
observation assertion. A detailed rerun emitted two Commons records after an
initial empty pass, with the same record identities as the baseline cursor
restart failure. No changes to those owning paths were made. The post-change full suite reported **520 passed, 18 skipped, two failures**
(the same cursor restart and projection-watch tests). The claim-race test
passed on this run. There were no additional failing tests. This is not a
fully green repository; those baseline defects remain open. Final Git delivery
details are recorded in the output report.
