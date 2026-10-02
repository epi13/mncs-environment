# Ambient diagnostic coherence

Diagnostic coherence is the environment's failure-explanation layer.
The target experience is:

```text
enter environment
  -> structured failures resolve to stable diagnostic identities
  -> already-sufficient witnesses are reused without recapture
  -> stale failures get one bounded minimal capture automatically
  -> test verdicts never change; explanations become current
  -> genuine gaps surface as tiny actionable deltas
  -> agent works from explained failures
```

Diagnostics are reactive: a healthy world carries no diagnostic block
at all. Environment does not learn debugger semantics. It observes
structured verification FAILs, measures the bound failure material,
asks the native diagnostic policy which failures are current and which
need capture, runs queued captures through the bound debugger
capability, and records identity-bound witnesses.

## What runs automatically

On every entry, re-entry, and explicit `diagnostic` pass, Environment
runs `mncs_env/diagnostics.py` after verification:

1. **Observe failures.** Recorded verification rows with FAIL verdicts
   resolve to failures. UNKNOWN rows are not product failures and
   stay out. Each failure binds obligation, test case, subject
   content digest, request digest, and toolchain identity.
2. **Check the epoch.** Failure bindings, provider availability, and
   depth form a diagnostic epoch. An unchanged epoch reuses the last
   validated summary with zero debugger work. A healthy world skips
   the native policy call entirely.
3. **Ask native policy.** `mncs.debug.diagnostic_coherence` decides
   per failure: current (reuse), capture (bounded witness),
   extend (one deeper step), deferred (budget overflow),
   unsupported (not debuggable), or escalate (missing evidence).
   Host code never reinterprets these decisions.
4. **Capture queued witnesses.** Each queued failure runs one
   `import-test` capture through `mncs.debugger/1` under the
   `verify` effect: foreign ownership conflicts deny, writes stay
   in session scratch, and the subject must be identical before and
   after or the witness is rejected. Library roots combine the
   obligation's declared roots with the test provider's
   adapter-contributed roots from its invocation descriptor.
5. **Record and quiet down.** Bound witnesses persist in session
   state; full traces stay in artifacts. Normal entry shows nothing
   when healthy and a tiny capsule when not.

## Terse summaries and evidence

Healthy entry has no `diagnostic` key. A failure entry carries:

```text
diagnostic:
  failures: 1
  captured: 1
  capsule_ids:
    - mncs-test.<obligation>::<test-case-id>
  evidence: sessions/<ses>/diagnostic-artifacts/evidence/<ref>
```

Full evidence lives behind `diagnostic <session> --evidence` and the
session evidence JSONL stream: per-failure status, reason, admitted
operation, witness reference, witness class, source path and span,
and capture tags. Witness documents (traces, values, provenance)
stay in capture directories, never in working context.

Useful commands:

```text
mncs-env diagnostic <session>                        # ambient pass
mncs-env diagnostic <session> --evidence             # full trail
mncs-env diagnostic <session> --depth standard|deep  # deeper capture
mncs-env diagnostic <session> --only <failure-key>   # one failure
```

Ambient passes always capture at minimal depth. Standard and deep
depths are explicit: they extend the recorded witness by one
admitted step (trace, then replay) without losing earlier evidence.

## What the ambient pass refuses

- Changing or reinterpreting a test verdict. FAIL stays FAIL.
- Capturing without structured source and request. Missing
  evidence escalates with the exact gap; the debugger never
  reconstructs executions from prose or filenames.
- Debugging infrastructure, compile, timeout, unsupported, or
  invalid failures as product failures. They surface as
  unsupported with their class intact.
- Recording a broken capture as knowledge. Harness breakage is
  retained in artifacts but never cached; the next pass re-observes.
- Capturing under a foreign claim on the subject. Denial names the
  holding session; release makes capture eligible immediately.
- Mutating repositories, snapshots, goldens, or sources. Capture
  confinement is verified after every run.
- Speculative debugging of healthy executions.

## Failure classes

Verification failure vocabulary projects onto the native diagnostic
classes: `test_failure` and `runtime_failure` are debuggable;
`compile_failure`, `infrastructure_failure`, `timeout`,
`unsupported`, and `invalid_request` are not. The host normalizes
literals from `mncs.test-result/1` (`failure_kind`,
`classification`, `failure_class`); only the native policy decides
which classes the debugger may capture.

## For providers: exposing diagnostics

`mncs-debug` publishes two capabilities:

- `mncs.debugger/1` with `verify` effects, invoked as
  `mncs-debug import-test <result> --test-id <id> ...` with explicit
  `--library` roots and `--cwd` inside session scratch.
- `mncs.debug-diagnostic-coherence/1`, the native policy app.

Test providers whose adapter contributes MNCS library roots beyond
obligation declarations must say so in their invocation descriptor
via `adapter_library_paths` (repo-relative directories). The debug
handoff resolves these against the provider checkout so re-execution
sees the same roots the test run saw. The long-term fix is
emitting effective roots in test-result provenance; that needs
toolchain work and is recorded as pressure, not implemented here.

## Concurrency

Diagnostic state, captures, and epochs are session-private. Two
sessions over the same failure capture independently without
corrupting each other; no shared witness reuse exists yet because
no unified cross-session evidence identity exists yet. Claims on
the subject deny foreign capture; claims on anything else are
untouched. No global locks, no session-wide serialization.

## Composition

- Verification produces FAILs; diagnostics explains them. Neither
  calls the other; both read recorded state.
- Doctor owns provider readiness. A broken debugger is a Doctor
  concern, never a reason to reinterpret the code under test.
- Projections render evidence; diagnostics never rewrites it.
- Language Service source bindings are consumed where present
  (test spans today); richer binding is that campaign's work.
- Actions and Forge own CI and orchestration; local witnesses feed
  them through receipts, not the reverse.
