# Ambient semantic coherence

The Language Service is the environment's ambient semantic-perception
layer. This document describes how Environment composes with it:
declaration, observation, durable cursors, and boundaries. The
provider-owned contracts (lifecycle, identity, capsule policy) live
in `mncs-language-service/docs/ambient-semantic-coherence.md`.

## Declaration

A definition opts workspaces into ambient semantics by declaring a
resident service per workspace root:

```json
{
  "identity": "mncs-language-service:mncs-test",
  "required": false,
  "probe": {"capability": "mncs-language-service:resident-status",
            "argv": ["--workspace", {"repository": "mncs-test", "path": "."}]},
  "reconcile": {"capability": "mncs-language-service:resident-reconcile",
                "argv": ["--workspace", {"repository": "mncs-test", "path": "."}]},
  "response_schema": "mncs.language-service.resident-status/1",
  "ready_when": {"/ready": true}
}
```

See `samples/ambient-semantic.environment.json` for a complete
definition. The `semantics.enabled` knob (default true) gates the
pass; definitions that declare no resident service carry no
semantic block at all.

## Composition

```text
enter
  -> Doctor probes resident-status, reconciles via resident-reconcile,
     persists observed/selected blocks in service observations
  -> semantics pass diffs observations vs durable per-workspace cursors
  -> change: invoke semantic-poll (bounded window) or
     semantic-capsule (first contact / stream reset)
  -> quiet: compare snapshots, spawn nothing
```

Doctor owns health and recovery. The semantics pass
(`mncs_env/semantics.py`) never starts processes, never writes
sources, and never re-invokes the probe: the probe's `observed`
block (generation, stream, cursor, diagnostic counts) is already
persisted by Doctor, so quiet re-entry is pure snapshot comparison.

Verification stays with `mncs-test`, failure explanation with
diagnostics/Debug, derived outputs with projections. The semantics
pass publishes semantic facts (current/changed/adopted/reset per
workspace, actionable counts); owners of verification, action, and
explanation strategy consume them.

## Durable cursors

`snapshot["semantics"]["workspaces"]` holds one record per
workspace root: stream identity, cursor, generation, diagnostic
fingerprint, and observation time. A cursor is never interpreted
without its stream identity: first contact and stream resets
reconcile through the bounded capsule instead of replaying a
foreign cursor. Degraded or unknown passes never cache their epoch,
so transient provider states re-validate on the next pass.

## Agent surface

- Declared and quiet: `{"current": N, ...}` with zero subprocesses.
- Changed: bounded delta counts from the resumed event window.
- Adopted/reset: bounded capsule counts plus durable baseline.
- Degraded: named reason; recovery belongs to Doctor.
- Undeclared: no block. Normal entry pays effectively zero
  semantic context.

Deep state (diagnostics, graph, obligations, capabilities, debug
bindings) stays resident behind the provider `semantic-query`
capability and the socket/MCP surfaces; entry context never inlines
source bodies, reference lists, or symbol indexes.

## Long-running observation

The background reconciler consumes the same resident event stream
through `LanguageServiceSource` when given the workspace socket:

```bash
mncs-env reconciler --workspace <root> --run \
  --language-socket <root>/.mncs/mnls-language-service.sock
```

(`MNLS_SERVICE_SOCKET` provides the default for one workspace.)
The janitor session has no provider-owned socket discovery yet;
each workspace socket needs explicit wiring (see the pressures
registry). Entry-time observation of declared workspaces needs no
daemon.

## Boundaries

- Environment never learns Cargo commands, socket paths, lease
  files, or host launch details. The provider owns operation;
  Environment owns composition.
- Ambient observation never mutates sources. Rename, quickfix,
  refactor, import insertion, formatting, and candidate application
  require explicit intent with proper claims/authority.
- Semantic impact facts inform invalidation; they never decide it.
- No RAVEL integration: adaptive strategy stays deferred until the
  ambient tooling matures.
