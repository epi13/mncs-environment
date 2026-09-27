# Example Work Intent

This is an illustrative shape, not a committed serialization format.

```text
WorkIntent
  identity: improve-environment-resume

  objective:
    Prove that an MNCS development session can survive consumer replacement.

  outcomes:
    - resolve the current MNCS development workspace
    - bind required environment capabilities
    - checkpoint active work durably
    - resume from the checkpoint in a fresh compatible consumer

  constraints:
    - do not duplicate provider semantics inside Environment
    - preserve provenance across the checkpoint boundary
    - revalidate mutable repository and capability state on resume

  protected_scope:
    - repositories/worktrees actively owned by other agents
    - branches explicitly marked read-only

  acceptance:
    - second consumer resumes without reconstructing prior work from prose
    - capability bindings retain provider identity and observed revision
    - changed authoritative state is detected during resume
    - protected scope remains enforced after handoff
    - produced artifacts/results remain correlated with the original session

  dependencies:
    - durable session persistence
    - workspace-state provider
    - at least one real MNCS capability provider
    - event/result correlation
```

The important distinction is that the intent declares desired outcomes and constraints. It does not hard-code a shell script, RAVEL plan, action graph, or provider-specific procedure into the Environment layer.
