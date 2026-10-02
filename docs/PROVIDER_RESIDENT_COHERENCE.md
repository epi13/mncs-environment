# Provider/resident ambient coherence

A build receipt and an execution receipt answer different questions. Commons
owns `family/provider-provenance-v1.json` and the native
`mncs.commons.family.provenance.v1` law. Forge owns the confined build/runtime
transport. Doctor owns targeted artifact repair admission. The provider owner
owns its source closure, effective library roots, configuration and capability.
Environment loads the selected owner's declared adapter and orchestrates it.

## First executable owner: Automation

Automation declares `.mncs/coherence-artifact.json` and
`tools/coherence_provider.py` in its family manifest. A missing/stale receipt
feeds the exact input/artifact identity to Commons, then to
`doctor.provider.v1`. An admitted repair compiles only that frozen router
artifact, confined to the Environment provider cache. There are no source edits,
release/push/deploy effects or Environment repository-specific build commands.

The receipt binds actual source/descriptor/manifest/build-adapter bytes,
compiler executable bytes, ordered effective root content, build configuration,
actual backend/interface ABI and callable inventory. The compiler's backend
artifact identity is independently checked against the retained runtime's info.
Execution adds actual embed bytes, operation, arguments/budget/grants and result
identity. Timestamps and output absolute paths are observation material only.
A source-root overlap, unsupported compiled closure/ABI, changed inputs,
corrupt content address or loaded executor replacement fails closed.

Automation's router consumes no stdlib modules: its admitted compiled closure
proves that exemption. Generic effective root digests include stdlib bytes when
an owner declares a stdlib root. Neither this exemption nor a successful build
establishes Test PASS. The selected prebuilt compiler still lacks a producer
build receipt; its exact bytes are known, its source build origin is unknown.

Forge's execution extension is `mncs:provider-provenance`. It composes with the
existing Forge execution-receipt envelope; it is not an Environment receipt
format or a new assurance verdict. Store persists immutable owner receipts on
first use. Session-local selection and operational rows remain Environment
owned, with existing Store CAS and recovery.

## Resident orchestration

`mncs-env resident SESSION [--watch --interval 1 --max-ticks N]` resumes an
existing session and calls the same owner-pass router as entry. Each boundary
iteration reopens validated Store/session state and serializes with entry.
It does not create a second janitor authority or grant repository mutation.
This first implementation pays Store open admission each iteration; retaining
that connection safely remains a performance pressure.

LS publishes its event transport in the readiness observation. Environment
consumes its stream/cursor directly, refreshes disk truth at that boundary,
and preserves semantic subjects, obligation deltas and impact completeness.
A wrong stream or reset schedules bounded reconciliation, never cursor-only
reuse. Owner-declared `coherence_subscriptions` may constrain a pass to a stream,
subjects and obligations. Native Automation admits suppression only with a
complete subscription AND complete impact/obligation delta. Otherwise broad
owner reconsideration remains mandatory. These are scheduling facts, never
repair authority or PASS evidence.

`coherence_publications` declares schema/event and repository/subject wire
fields. Verified Store metadata replay fetches only matching publication rows.
Known unrelated repositories remain quiet. Missing rows, unknown schemas and
lost history require bounded reconciliation. No global generation substitutes
for Store publication generation, LS generation, build content identity or CAS.

## Entry selection

The Store-backed selection index is an existing projection row under
`entry:index/…`. Its selector binds consumer/kind/definition/workspace. Current
Store metadata identifies the latest canonical snapshot of each session.
Changed sessions alone are decoded; selected candidates are always checked
against their actual snapshot. Missing/corrupt index data rebuilds by bounded
selection. Multiple matching live sessions still refuse implicit choice.
The index neither grants authority nor bypasses Store object admission.

## Domain seams retained

Doctor now exposes a native family remediation-plan entrypoint over Commons
family law. Environment prefers a declared selected Doctor policy and refuses
an unusable declared policy. Old owners without that contract retain explicit
Commons recovery. Exact edit transport and post-repair proof orchestration
remain in Environment; moving the edit transport requires a further owner
contract without weakening existing two-phase claim/preimage checks.

Test/Debug/CI folding and shared Actions dispatch remain deferred. Their real
producers do not yet supply the complete receipt/inventory/environment world
required by their owner admission laws. Shared provenance equality is available;
no session-local PASS or diagnostic witness is promoted by transport alone.

No RAVEL or general Memory integration is part of this substrate.
