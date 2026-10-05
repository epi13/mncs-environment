# Ambient verification coherence

Verification coherence is the environment's test-evidence layer. The
target experience is:

```text
enter environment
  -> relevant verification obligations resolve from declarations
  -> already-valid evidence is reused without re-running
  -> stale runnable suites execute automatically
  -> failures surface as tiny actionable deltas
  -> agent works from trustworthy evidence
```

Environment does not learn test semantics. It observes declared
verification obligations, measures the current world, asks the native
coherence policy which obligations are current and which need
execution, runs queued native suites through the bound test
capability, and records identity-bound evidence.

## What runs automatically

On every entry, re-entry, and explicit `verify` pass, Environment runs
`mncs_env/verification.py`:

1. **Discover obligations.** Direct-child manifests may point
   `verification.obligation_inventory` at a
   `mncs-family.verification-obligation-inventory/v1` document. Invalid
   obligations are reported, never executed.
2. **Fingerprint the world.** Declaration, subject-content, executor,
   toolchain, inventory, repository revision, and dirty-content
   identities form a verification epoch. An unchanged epoch reuses the
   last validated summary instead of re-executing.
3. **Ask native policy.** `mncs.test.verification_coherence` decides
   per obligation: current (reuse), queue (bounded run list),
   deferred (budget overflow), contradictory, unresolved, unsupported,
   or excluded. Host code never reinterprets these verdicts.
4. **Execute queued suites.** Each queued native obligation runs its
   declared suite once through `mncs.test-result/1` (shared suites
   run once and share the result). A repository change during
   execution rejects the result instead of recording it.
5. **Record evidence.** Bound identities, verdict, failure
   classification, covered tests, and a run locator persist per
   session. A recorded FAIL is current knowledge, not a reason to
   re-run every entry.

## Terse summaries and evidence

Normal operation emits counts, not test lists:

```text
verification:
  current: 1
  executed: 0
  failed: 0
  blockers: 0
```

A failure adds only the actionable delta: failed obligation ids and an
evidence handle. Full per-obligation verdicts, native coherence
output, failure classes, failed test ids, and run locators live in
session evidence, retrievable with:

```bash
./scripts/mncs-env verify <session> --evidence
```

Each pass also appends `mncs.session-evidence/1` for future RAVEL and
journal consumers.

Related surfaces:

- `mncs-env enter` returns a `verification` block with the pass summary.
- `mncs-env verify <session>` runs the ambient pass and prints the
  terse summary (exit 5 when blockers remain).
- `mncs-env verify <session> --only <id>` explicitly verifies one
  obligation by identity.
- `mncs-env verify <session> --full` runs without epoch reuse and with
  the full execution budget.
- Definition knob `verification.enabled: false` opts a definition out;
  `verification.max_executions` (1-32, default 8) bounds ambient runs.

## What the ambient pass refuses

- **Snapshot/golden mutation.** Failing assertions, witnesses, and
  snapshots are evidence. Ambient verification never rewrites
  expected output, fixtures, or sources, and never stages or commits.
- **Guessed execution.** Suites run only through the bound test
  capability with declared sources and libraries. Missing suites,
  unmeasurable dependencies, and non-native executors report
  unsupported/unresolved instead of executing something adjacent.
- **Stale attribution.** Results whose subject generation changed
  mid-run are discarded. Transport and execution failures never cache
  an epoch; the next pass re-observes.
- **Cross-session contamination.** Evidence rows and run artifacts are
  per-session. Verification takes no repository claims; read-only
  execution needs none.
- **Silent scope.** Declarations outside the 32-obligation bound,
  malformed obligations, truncated inventories, and contradictory
  evidence fail closed into invalid/unresolved/blockers, never into
  quieter passes.

## Failure classes

Verdicts stay distinct: PASS, FAIL, and UNKNOWN are never conflated.
Each executed record carries the provider `classification`
(`passed`, `test_failure`, `compile_failure`, ...) alongside the
infrastructure `failure_class`, so a broken suite, a broken toolchain,
and an unrunnable environment read differently. Missing providers
surface as `coherence-unavailable` / escalation, never as PASS.

## For providers: exposing verification

Repositories declare obligations in their obligation inventory:

```json
{"identity": "example.native-suite", "lifecycle": "permanent",
 "invalidation_dependencies": ["tests/suite.mncs"],
 "executor": {"provider": "mncs-test", "kind": "native_first_class_test",
              "source_paths": ["tests/suite.mncs"],
              "library_paths": ["../mncs-language/library"],
              "declaration_identities": ["*"],
              "verifier_identity": "mncs-test-runner/0.2.1"}}
```

`mncs-test` publishes distinct execution contracts. `canonical-vm-tests`
runs the provider's VM-suitable inventory through `mncs-compiler` and
`mncs-vm`; `mncs.test-verify/1` retains the Stage-0/reference verification
and reuse lane for other selected repositories and independent comparison.
`mncs-env test` selects the canonical contract for its provider checkout and
the reference contract for other targets. `--execution` pins either lane;
an unavailable selected contract fails closed instead of switching backends.
Environment binds the exact compiler, VM, Stage-0 library, and provider state
directory from the selected checkouts.

## Concurrency

Epochs and evidence rows are per-session derived state. Another
agent's commit, dirty file, declaration change, or provider change
invalidates the epoch; the next pass re-observes. Concurrent sessions
execute independently with separate evidence; duplicate suites across
sessions are not deduplicated, only within one pass over identical
inputs. Foreign claims, worktrees, and sessions are never seized,
cleaned, or reinterpreted.

## Composition

Doctor owns system health, projections own derived files, and
verification owns test evidence. A broken provider executable is a
Doctor concern; a failing suite is a verification concern. Projection
application changes file bytes, which invalidates verification subjects
through content fingerprints; verification never rewrites projections.
Each subsystem converges on its own epoch without update loops.
