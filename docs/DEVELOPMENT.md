# Development Guidance

## Build the contract before the convenience layer

The first implementation work should establish canonical typed contracts and lifecycle behavior before adding model-specific adapters, prompts, UI, or workflow sugar.

A healthy order is:

1. environment/session identities and core data model,
2. deterministic resolution inputs/outputs,
3. workspace-state binding,
4. capability discovery/binding,
5. authority projection,
6. event subscription and durable continuation,
7. checkpoint/handoff,
8. real provider adapters and end-to-end proofs,
9. consumer-specific adapters only where needed.

This is an implementation sequence, not a set of parallel product versions.

## One implementation, continuously upgraded

Do not create `v1`, `v2`, `legacy`, `next`, compatibility forks, duplicate schemas, or frozen half-implementations simply because a contract changes during development. MNCS currently has coordinated control over its consumers; when the canonical design improves, update the implementation and affected callers together.

Introduce compatibility/versioning only when a real external compatibility requirement exists.

## Keep provider semantics out

Adapters may translate transport or representation. They must not become substitute implementations of the provider's meaning.

Acceptable:

- converting a provider descriptor into a `CapabilityBinding`,
- tracking the provider contract revision observed,
- correlating a provider event with a session,
- revalidating availability on resume.

Not acceptable:

- reproducing RAVEL planning rules locally,
- implementing compiler/runtime policy in Environment,
- recreating Rights decisions because the rights API is inconvenient,
- parsing human logs forever instead of recording pressure for typed provider output.

Temporary compatibility shims, if unavoidable, must be isolated, explicit, tested, and treated as debt with an owning integration pressure.

## Prefer identities over paths

Filesystem paths, process IDs, ports, and branch names are useful observations but weak long-lived identities.

Where MNCS exposes semantic identities, bind to them and store the transient addressing data separately.

## Make staleness visible

Any state that can become stale should carry enough metadata to decide whether it needs revalidation. Examples include:

- repository head/dirty state,
- capability availability,
- authority grants,
- service contract revisions,
- event cursors,
- artifact availability.

Do not blur persisted historical observation with current truth.

## Concurrency and protected work

Environment must eventually support multiple simultaneous consumers without treating the whole workspace as uncontended.

Implementation should model:

- which repositories/worktrees are actively owned by other work,
- read-only versus writable views,
- protected branches/worktrees,
- leases/locks if supplied by the owning control system,
- explicit conflict detection before mutation.

Do not infer that locally stored build/debug/cache data is protected merely because a repository is protected; protection should be expressible at the appropriate resource scope.

## Testing priorities

Run `python3 -m pytest tests/ -q`, `python3 scripts/vertical_proof.py`, and
`python3 scripts/campaign_proofs.py`. The vertical proof selects temporary
Atlas/Language clones by default so verification does not require a broad
family root or modify foreign working trees. Entry regressions use fresh
CLI processes, both persistence backends, read-only health, selected roots,
failure recovery, and observable provider contracts.

Prefer end-to-end architectural proofs over large amounts of isolated scaffolding.

High-value tests include:

- resolving the same authoritative inputs yields an equivalent environment,
- a session survives consumer restart,
- a different consumer resumes from checkpoint without prose reconstruction,
- protected scope prevents an otherwise valid mutation,
- unavailable capability degrades the session without corrupting it,
- provider revision changes trigger revalidation,
- missed events can be replayed or recovered deterministically,
- service results remain attributable to the originating session/intent,
- no test path depends on provider semantics duplicated inside Environment.

## Documentation discipline

When a design decision changes, update the canonical document rather than preserving obsolete architectural generations for ceremony. Git already preserves history.

Use RFCs for consequential decisions that establish durable contracts or shift repository boundaries. Keep the main docs current with the accepted design.

## Recording pressures

A pressure record should state:

- required behavior,
- owning external system,
- current limitation,
- why Environment cannot safely own the workaround,
- evidence/reproduction,
- desired contract shape if known.

This lets Environment drive cross-repo improvement without becoming the dumping ground for missing functionality.

## Real resident provider proof

After the unit/integration suite, run:

```bash
python3 scripts/forge_resident_proof.py --forge-checkout /absolute/selected/mncs-forge
```

The proof clones committed selected providers into an owned campaign, copies
existing selected runtime artifacts, and enters through the local canonical
interface. It uses real Store, Forge, Language Service, and native Test. It
checks startup/reuse/recovery, cwd independence, invalid descriptors, foreign
checkout rejection, native MNCS work, and checkpoint/resume/handoff. Failed
campaigns remain available for diagnosis. Provider-owned stop is the cleanup
boundary. Binary copying is fixture setup and makes no build-origin claim.

## Adaptive Store proof

Run `python3 scripts/adaptive_store_proof.py --family-root /absolute/family` after
Store provider changes. It uses selected real providers and real Store session
persistence, preserving its campaign/report for inspection. It does not require
Forge or modify selected source checkouts. Results include actual inspected,
materialized and coded-transfer bytes plus retained ABI counters.

The adaptive proof retries only bounded provider readiness timeouts, preserving
the same durable session and recording each observation. Other failures fail
immediately. This exposes cold admission/resource latency without adopting an
ambient provider or broadening the three-second readiness deadline.
