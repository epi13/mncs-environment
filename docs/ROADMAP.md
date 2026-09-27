# Roadmap

This roadmap is organized by capabilities to establish, not product versions. The repository should remain one continuously upgraded canonical implementation.

Status ledger (foundation campaign): Foundation, Workspace awareness,
Capability discovery and binding, Authority, Events and continuation,
Checkpoint and handoff, and End-to-end environment entry are implemented
and proven (`python3 scripts/vertical_proof.py`; 25 unit/failure/session
tests). Human-facing projection, Store/Memory/rights integration, and
canonical provider addressing/events remain pressures in
`pressures/registry.json`.

## Foundation

- Define stable identities for Environment, EnvironmentSession, WorkIntent, CapabilityBinding, Checkpoint, and Handoff.
- Choose the canonical schema/type representation consistent with the surrounding MNCS architecture.
- Define lifecycle invariants and persistence ownership.
- Establish deterministic environment resolution inputs and outputs.

## Workspace awareness

- Bind repository/worktree state without becoming a Git implementation.
- Represent active and protected work explicitly.
- Detect writable/read-only/conflicting scopes before mutation.
- Distinguish disposable local artifacts/cache/debug state from protected source work where providers can express that distinction.

## Capability discovery and binding

- Discover MNCS services and capabilities through stable provider descriptors.
- Bind provider identity, contract revision, invocation reference, authority requirement, and result/event contracts.
- Revalidate bindings when provider state changes.
- Record missing provider contracts as cross-repo pressures instead of embedding provider semantics.

## Authority

- Project rights/policy results into a session-readable AuthorityContext.
- Prove protected work cannot be mutated through an otherwise valid capability.
- Carry authority/provenance correlation through provider calls.

## Events and continuation

- Subscribe to typed provider events relevant to active intent.
- Support durable cursor/replay or equivalent continuation semantics.
- Allow events to update session state and work admissibility while the consumer is absent.
- Prove a consumer can resume after downtime without reconstructing changes manually.

## Checkpoint and handoff

- Persist minimal canonical continuation state.
- Revalidate stale observations on resume.
- Resume a session with a different compatible consumer.
- Keep natural-language summaries optional and subordinate to structured state.

## End-to-end environment entry

Demonstrate a real flow in which a consumer:

1. enters a named/resolved MNCS environment,
2. receives workspace, capability, authority, and state context,
3. inherits a WorkIntent,
4. invokes real MNCS services through bindings,
5. receives service events/results,
6. checkpoints,
7. exits,
8. resumes with no manual context reconstruction,
9. completes under verifiable provenance.

## Human-facing projection

Once the machine contract is sound, expose the same environment/session data for Atlas or another human-facing project dashboard. Do not create a separate dashboard-only state model.

## Success condition

The project has succeeded when starting a fresh compatible agent no longer means teaching it the MNCS universe in a prompt. The agent receives a coherent, current, authorized working environment and can continue existing work through stable machine-native contracts.
