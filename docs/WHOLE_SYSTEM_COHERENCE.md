# Shared family coherence

This describes the delivered system boundaries and the remaining integration
seams. The campaign is **not closed**; see [the campaign report](WHOLE_SYSTEM_REPORT.md).

## Authority map

| Owner | Meaning |
| --- | --- |
| Commons | Family change/lifecycle vocabulary, declared contract edges, pressure lifecycle |
| Store | Durable objects, immutable publication, generation CAS, commit feed |
| Environment | Selected checkouts, sessions, claims, cursors, orchestration and context |
| Language Service | Observed semantic dependency and impact facts |
| Doctor | Remediation eligibility and plans |
| Automation | Generic reconcile/adopt/revisit decisions |
| Actions | External routing, receipts and remote evidence |
| Test | Exact verification inventories, obligations and verdicts |
| Debug | Failure/request-bound witnesses and diagnostic depth |
| Forge | Bounded execution and execution receipts |
| Stdlib | Extracted library sources and their module index |
| Language | Compiler/runtime, bootstrap ABI and source inventory |

Family's current convergence path still invokes the Commons repair gate and
bounded edits directly. Doctor's structured family remediation contract exists,
but removing the remaining direct-policy seam needs a separate, proven transition.
There is no new universal provider, Memory authority or RAVEL integration.

## Durable state and generations

`family:change/<fc:sha256>` records lifecycle and the pinned establishment
generation; immutable evidence holds canonical core material. Identity does not
change with lifecycle. Producer project generations advance on changed observed
producer heads; the family cursor is a publication/observation cursor, not a
compiler semantic generation or global version of every project.

`family:recon/<change>/<consumer>/wc:<sha256>` addresses adoption by the selected
physical checkout. The suffix hashes its resolved path inside this Store
namespace. It is an addressing identity, not cross-machine semantic or evidence
equivalence. Observers selecting the same checkout share CAS and final state;
different worktrees do not inherit one another's adoption. Historical
repository-only rows remain intact and cannot authorize checkout adoption.

Row versions are local CAS revisions. Store generations name durable publication
snapshots. Language Service generations/cursors name provider observations.
Environment epoch digests name exact invalidation inputs. They are not mutually
substitutable and do not form one global counter.

Contributor rows contain session/consumer identity, selected workspace addresses,
claim handles and active changes. Changed facts publish immediately; unchanged
presence renews at most hourly against the existing four-hour liveness budget.
Rows survive process death. A changed family epoch is saved once for fresh-process
reconstruction; quiet observations do not save it again.

## Observation and scheduling

A family epoch includes established change generations, relevant reconciliation
versions, owning verification verdict/digest, relevant live claim versions and
holders, contributors, and the family cursor. Foreign claim release therefore
wakes a previously occupied consumer. Due revisit deadlines invalidate cached
observation. Repeated identical rows and presence facts do not republish.

Environment already has a reconciler with typed Git/Store/Commons/semantic source
cursors. Optional projection watches and provider residents still coexist. The
full event-to-affected-pass scheduler is not yet implemented. Automation's generic
revisit matrix and Commons' family retry-budget law have different responsibilities;
this campaign does not pretend those laws are equivalent.

## Declared and observed dependencies

Commons `repository_contracts()` qualifies bare exports using the manifest's
repository identity, retains exact consumes identities and includes explicit
validated semantic-contract declarations. Environment uses that reader instead
of its prior duplicate manifest interpretation. LS semantic impact remains an
observed fact; it cannot create architectural edges or authorize repair.

The explicit `scripts/audit_repository_dependencies.py` Commons command validates
stdlib module-index digests and scans only native runtime evidence named by
provided contracts. Comments and dev/test/example trees do not ground edges.
It reports missing declarations and exits nonzero; it never mutates manifests.
Stable runtime imports ground five new `mncs-stdlib.stdlib-source` consumes edges
in Store, Test, Debug, Forge and Actions. Environment declares its actual Store,
Commons-family and optional semantic-query relationships. General provider import
indexing and mandatory family CI admission remain future work.

## Provider lifecycle and native transport

Store uses explicit `MNCS_STDLIB_ROOT/library`, otherwise the selected Language
checkout's sibling `mncs-stdlib/library`, with bounded legacy `language/library`
fallback only when no extracted sibling exists. An explicitly invalid root fails;
an empty override disables discovery. Store admission, compilation and cache
identity use the same effective roots and source content. Selected stdlib bindings
are persisted in session Store bootstrap metadata and restored on reopen.

Environment's family and retry native transports share one library addressing
helper. Native family consumer classification carries at most 32 eight-field fact
vectors to Commons in one ordered request. Domain laws and repair gates remain
native. FD pressure uses the spawning process's effective soft limit and avoids
unnecessary family native launches when that limit approaches exhaustion.

Prebuilt compiler/embed artifacts in the real workspace are older than the
selected source: they lack Commons' required composite projection symbols. An
isolated build of committed Language main proves the current ABI works. This does
not silently replace another campaign's runtime artifacts or finish its extraction.

## Evidence and transformations

Family adoption requires PASS for **every** named obligation; one PASS cannot
mask missing or unknown obligations. Verification observes only the selected
physical checkouts. Invalid-only inventories are structural blockers, not empty
native verification requests; their diagnostic material participates in the epoch.

Verification remains session-local. Recorded toolchain path/revision and borrowed
inventory facts do not prove compiler bytes, effective library source digests,
fresh provider inventory and relevant environment equivalence. Debug's library
root provenance gap also prevents safe CI/local witness folding. Remote routing
identity is not an executor identity or proof of equivalence. Actions' local
claim-once ledger is not cross-machine exactly-once dispatch after an ambiguous
network outcome. These are explicit open provenance/coordination pressures.

Existing deterministic transforms remain `set_json_field`, `replace_span` and
`run_capability`, with declared paths, preimages, claims, native admission and
post-validation. An ownership move still needs a semantic choice; there is no
stdlib-specific repair script or unproven generic AST migration.

## Context budgets

`context_budget` optionally specifies `healthy_bytes` (default 768),
`degraded_bytes` (1536), `total_bytes` (4096) and `attention_items` (4). Byte limits
range from 512 to 16384; total must be at least 4096, degraded at least healthy;
attention ranges from zero to eight. The total governs ambient provider blocks,
not the session identity, capability/action map or all JSON entry bytes.

Over-budget blocks retain bounded counts/status and an expansion handle. Complete
content is written once under a content-addressed session `entry-context` artifact;
reasons are preserved there. Healthy blocks gain no extra accounting fields.
Healthy empty verification and projection blocks are omitted; Debug, Actions and
family keep their existing healthy omission rules. Capability availability has one
canonical nested representation, avoiding its duplicate top-level list.

Domain states remain distinct: verification PASS/FAIL/UNKNOWN, diagnostic witness
completeness, external receipt status, family ConsumerClass and pressure lifecycle
have different authority. No superficial status renaming was introduced.
