# Environmental capability evidence links

The environmental capability projection connects existing implementation
accountability and native capability observations through the ordinary
`mncs-env projections` interface. It derives links in memory; it does not add a
second telemetry store or capability registry.

```sh
./scripts/mncs-env projections <session-id> \
  --interpret environmental-capability \
  --evidence-limit 16 \
  --session-event-limit 8
```

Inspect `capability_accountability_links` in the result. It reports exact
repository, commit, path, and SHA-256 relationships, along with their source
references, the owner audit's declared host responsibilities, the candidate
native capability, the existing next-investigation text, and one bounded
source reference for its language contract, compiler support, and host runtime
provider where those records are available. It is bounded to four links and
5 KiB. Exact pointers provide traceability, not proof of causation or migration
suitability.

## Current evidence path

The verified live readback in
[`native-capability-evidence-links-20261026.json`](../evidence/environmental-capability/native-capability-evidence-links-20261026.json)
connects three existing records:

1. The Environment owner-audit observation identifies owner-declared native
   MNCS-Test surfaces and remaining host responsibilities.
2. An append-only correction observation targets that audit by its exact
   integrity-addressed reference and carries the MNCS-Test P-001 pressure
   source.
3. A native process capability correspondence links that same P-001 source to
   pressure `MNCS-TOOLING-B665F138D324` and candidate API
   `mncs.std.process.v1`.

The projection confirms that the correction target matches the audit reference
and that the P-001 source pointer is integrity-verified in both linked
observations. Its current correction status is
`replacement-values-unverified-or-unchanged`, and the improvement relation is
`not-recorded`. The view therefore preserves the source trail without claiming
that the original audit was changed. The linked process correspondence also
exposes integrity-addressed pointers to the MNCS process language contract,
compiler support, and runtime provider. These references make the candidate
implementation inspectable; they do not establish consumer integration or
implementation responsibility for an owner-declared host task.

For this candidate, the next investigation is to find an existing native MNCS
consumer, establish how it discovers and invokes the API, and verify parity
against a legitimate bounded host-tool operation before recommending migration.
The relationship does not establish that the process pressure causes any
particular owner-declared host responsibility.

## Measurement limits

- Systemwide native adoption remains `not-measured`; these observations are not
  a complete implementation inventory or denominator.
- Agent discovery and successful or failed use of the capability are not
  measured by this evidence path.
- Recurrence across sessions is not established by shared owner or pressure
  records.
- A future implementation or migration requires independent correctness and
  reliability verification. This projection does not declare success.
