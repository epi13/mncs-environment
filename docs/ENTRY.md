# Entry and readiness contract

## Arrival

From the Environment checkout: `./scripts/mncs-env enter --consumer my-agent`.
The checked-in definition composes six sibling providers and grants writable
intent only to Environment. It probes Environment's real workspace discovery
endpoint. Optional undeclared provider invocations are reported as degradation;
this does not mean that every resident MNCS service is running.

From another project, call the same executable by its path. Selection precedence:

1. Explicit `--definition` and optional `--workspace`.
2. Closest `.mncs/environment.json`, walking ancestors up to the Git root.
3. A read-only orientation definition for the nearest Git checkout, explicit
   workspace, or current directory. No sibling scan or mutation grant is inferred.

With `--workspace`, definition discovery starts at that root and never inherits
an unrelated current project's configuration. Definition-relative
`workspace_root` resolves against the definition's directory. Campaign
definitions still require explicit workspace selection.

`--state-dir` chooses persistence, defaulting to
`~/.local/share/mncs-environment`. Store is canonical; `--persistence file`
is the existing debug projection. Invalid definition/root/selection failures
occur before session creation or provider startup. Store failures identify
the provider and binding/recovery path; Environment never silently switches
persistence backends.

## Bounded workspace discovery

A repository root is a valid workspace and discovers itself, including linked
managed worktrees. An ordinary directory discovers immediate Git repositories
within existing directory and time limits. To select providers from a large
family directory without scanning unrelated work:

```json
{
  "name": "selected-development",
  "workspace_root": "../..",
  "workspace_scope": {
    "kind": "workspace",
    "repositories": ["mncs-environment", "mncs-language", "mncs-store"]
  },
  "intent": {
    "goal": "work on the selected environment",
    "repositories": ["mncs-environment"]
  }
}
```

Names select unique immediate Git checkouts, at most 64; missing paths,
symbolic links, and traversal are rejected. This list declares the requested
scope. Repository manifests, Git, and provider contracts remain authoritative
for identity, revisions, tools, and state. Selection persists into resume,
health, and reconciliation; bindings and toolchains use those exact roots.
Store routing is persisted for fresh-process continuation.
The runtime binding also selects the Language compiler and embed library used
by Store. Explicit entry upgrades older package-only bootstrap metadata after
checking the same selected checkout, preserving session identity and history.
Read-only inspection never performs that upgrade; re-enter first when older
bootstrap metadata needs recovery.

## Work reuse and continuation

Entry matches the canonical definition identity, resolved workspace root,
consumer identity, and consumer kind. One live match is resumed and revalidated.
Checkpointed work becomes active; its checkpoint and events remain intact.
Terminal and handed-off sessions are not implicitly reused. `--new-session`
explicitly creates independent work. Multiple matches return
`entry-session-ambiguous` and require `resume <session> --revalidate` or
independent entry. Changed definitions establish new work rather than silently
changing a prior session's authority.

Entry and `reconcile` use a nonblocking process lock per state directory and
backend. A concurrent operation returns `entry-busy`, with a retry action.
Locks release on process exit. Provider reconciliation must itself be
idempotent across consumers and state directories; Environment does not own
the provider's process supervisor.

## Context and actions

Entry returns `mncs.environment.entry-context/1`: identities, lifecycle,
consumer, configuration source, state directory/backend, workspace root,
bounded project and capability summaries, authority, toolchain observation,
readiness, recovery operations, and whether prior work was reused.

`actions.*.argv` is directly executable and preserves the current interpreter,
CLI path, state root, backend, and session. It works from another directory and
with paths containing spaces. `next_commands` contains the canonical next
action: reconciliation for blocking requirements, capability discovery for
usable work. It does not prescribe repeated reconciliation for unaddressed
optional provider contracts.

`status`/`context` are historical snapshots, labeled `observation: snapshot`.
`inspect` exposes complete binding, authority, continuation, and event state.
`health` checks current workspace availability, executable permissions,
bound toolchain presence, and declared provider probes, labeled
`observation: live`. Health does not participate, write session events,
start services, acquire claims, or advance cursors. Probe capabilities must
be explicitly addressed and declare only read effects.

## Readiness and reconciliation

Readiness describes the selected workspace and definition's requirements,
independently of durable session lifecycle:

| State | Meaning |
| --- | --- |
| `ready` | Discovered substrates are available and declared provider probes satisfy their contracts. |
| `degraded` | Required checks pass, but optional capabilities/services are unavailable, or no callable capabilities were found. |
| `blocked` | A required capability/service or complete workspace observation is unavailable. |

Substrate availability is explicitly labeled; it does not prove a compiler's
semantic correctness or that a service is resident. Unaddressed provider
contracts remain discoverable with stable diagnostic codes. Fingerprints
never authorize invocation. Reconciliation rediscovers bindings, so deleted,
restored, added, and changed provider descriptors are reflected in ordinary
sessions as well as campaigns. Incomplete scans preserve prior complete facts.

Providers can be composed declaratively:

```json
{
  "required_capabilities": ["provider:status"],
  "services": [
    {
      "identity": "provider:resident-service",
      "required": true,
      "probe": {"capability": "provider:status", "argv": []},
      "reconcile": {"capability": "provider:reconcile", "argv": []},
      "response_schema": "provider.service-status/1",
      "ready_when": {"/state": "ready", "/workspace_identity": "expected-workspace"}
    }
  ]
}
```

Referenced capabilities must come from provider declarations. Status is a
read-only JSON contract. `ready_when` contains JSON object pointer/value
comparisons; include provider/workspace identity where applicable to prevent
trusting a service for a different workspace. The response schema must match.
Environment preserves selected fields, observation times, stable diagnostic
codes, and recovery addressing; provider domain status stays provider-owned.

Entry or explicit reconciliation probes first, delegates recovery only for
nonready services with a recovery declaration, then probes again. Ready
services are reused. A schema mismatch requires contract repair before
automatic recovery. Recovery uses normal session invocation, including
provider effects, claims, authority, and result/event provenance. A denied or
pending escalation cannot start a process. Environment never kills a PID,
removes a socket, or adopts foreign work as a startup workaround.

There are at most 16 service declarations. Each read probe has a three-second
limit, within a 15-second aggregate probe budget; recovery has ten seconds per
operation within a 20-second aggregate budget. A final probe pass verifies
recovery. Providers needing longer asynchronous startup should return bounded
state and converge on subsequent reconciliation.

Entry/health/reconcile return exit 5 for blocked readiness and still emit
structured context. Degraded readiness returns 0 so optional gaps do not
prevent work. Invalid inputs, persistence failures, ambiguous work, and busy
entry return 2 with JSON diagnostics. Status remains read-only even when the
historical observation is blocked.

## Boundaries and current gaps

Environment owns selection, composition, session continuity, observation,
and invocation transport. Providers own startup semantics, service identities,
health meaning, and process lifecycle. Rights and claims still govern writes.
The filesystem lock is bootstrap infrastructure, not workspace ownership.

Forge publishes resident status/reconciliation/stop/work descriptors. To require
that provider, enter with `--definition samples/forge-resident.environment.json`.
The sample selects Language Service explicitly and binds `.mncs/forge.toml`
from the selected Environment checkout. Ordinary local entry retains its
existing six-provider definition; Forge residency is an explicit requirement.
An unavailable required descriptor blocks; optional gaps remain degradation.
An unowned provider worktree needs a claim before its execute/write effects.

Service `argv` also accepts `{"repository": "mncs-environment", "path":
".mncs/forge.toml"}` references. These resolve only through selected capability
roots, reject traversal/symlink escapes, and survive cwd changes and campaign
relocation. Missing selection is diagnostic; no sibling path is inferred.

Provider diagnostics, selected identity, observed identity, and recovery remain
structured in service observations. Reconciliation preserves the provider JSON
response; transport success is separate from subsequent verified readiness.
Forge owns live challenge, process birth/pidfd control, runtime artifact identity,
Language stream validation, and asynchronous startup. Environment owns none of
those internal rules. See Forge's `docs/RESIDENT_PROVIDER.md`.
Compiler build origin remains unproven by checkout HEAD or artifact SHA256.
The corresponding Commons pressure is `MNCS-TOOLING-9E7E149C94D9`.

Python remains the process/Git/Store/file transport substrate; native MNCS
capabilities are invoked through the same session binding interface.

An unwritable state location reports `entry-state-unwritable` with the explicit
`--state-dir` recovery action. Environment does not silently select a new state
store. Keep the chosen state directory in subsequent actions to preserve work.

Store publishes its own local Environment definition and adaptive operation
descriptors. See selected Store `docs/provider.md`; Environment only composes
those operations. `scripts/adaptive_store_proof.py` provides a real consumer
proof from entry through physical inventory evolution, selective retrieval and
durable handoff.

## Concurrent session mutations

Store-backed session snapshots are immutable revisions. Two participating
commands that loaded the same revision can race; Store refuses differing
contents instead of overwriting committed state. Environment reports
`session-snapshot-conflict` with the session and revision. For an invocation,
`capability_may_have_run` is true: inspect durable events and invocation
artifacts before retrying. Serialize mutating commands for one session;
read-only inspection can run independently. Entry's reuse lock does not
serialize every subsequent participant. Safe concurrent snapshot convergence
and durable invocation receipts remain Environment responsibilities.
