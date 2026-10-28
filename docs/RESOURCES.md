# Live resource observations

`./scripts/mncs-env resources` observes the caller and at most seven ancestors.
This reaches an agent runtime when it launches the command directly. A proxy
or unrelated shell may have different ancestry; the command does not infer
which process belongs to a durable Environment session.

`./scripts/mncs-env resources --pid 12345 --pid 23456` selects up to eight
explicit positive PIDs. Context/status/entry expose the callable command as
`actions.resources.argv`; their historical readiness remains unchanged.

The JSON `mncs.environment.resource-observation/1` envelope labels observations
as live and includes process birth ticks, parent, cgroup, current RSS, RSS
high-water, executable path and executable-byte digest, open FD count,
soft/hard FD limits, remaining headroom, descriptor classes, deleted handle
count, and inotify watch entries. It also includes host available/total memory
and the system file-table allocation/limit. Watch entries are counted per
observed handle, so duplicated handles may count the same watches twice.
Each process row includes cumulative user/system CPU ticks and bounded
`/proc/<pid>/io` counters. `rchar` and `wchar` include pipe and socket
transfers; `read_bytes` and `write_bytes` are kernel storage-I/O accounting;
`cancelled_write_bytes` records canceled writes. These are process-lifetime
counters and exclude child processes. Missing or partial counters remain
explicit in `io_counters.status`.
The executable digest observes the image exposed by procfs; it is not a build
receipt or application-level producer attestation. This is a process sample,
not a service identity, leak verdict, RSS aggregate, or ownership grant.
Doctor/provider policies retain health interpretation.

When `/proc/<pid>/cgroup` identifies one unified cgroup v2 membership, each
process row also contains `cgroup_resource_limits` with cgroup memory current
and maximum, CPU quota and period, and PID current and maximum. `quota_cores`
is derived as `quota_us / period_us` and carries an explicit derivation basis.
Kernel `max` values are represented as `unbounded`; unavailable files and
partial sets remain explicit. Cgroup v1 membership is currently unavailable.
Each cgroup value is bounded to 4,096 bytes, and resolved paths must remain
under the configured cgroup root. The memory and PID current values describe
the entire cgroup, not only the sampled process. The record therefore says
`session_process_mapping: not-asserted`; it does not attribute a cgroup's
resource use to an Environment session or agent.

The command reads procfs directly without opening Store, scanning repositories,
spawning tools, retaining output files, starting services, or modifying target
processes. It bounds each descriptor scan at 8,192 and each watch-entry count
at 8,192. Truncation makes exact FD count/headroom null; partial details are
explicit. Unavailable procfs, exited processes and permission failures return
unknown observations. Birth ticks detect PID reuse during sampling. Counts
can change while sampled and the observer's own count includes its scan FD.

Use headroom and repeated observations before expensive work. A retained count
with no live jobs is evidence for investigating lifetime; changing limits is
not a substitute for identifying those owners. The October 4 investigation is
in [FD_PRESSURE_INVESTIGATION.md](FD_PRESSURE_INVESTIGATION.md).
