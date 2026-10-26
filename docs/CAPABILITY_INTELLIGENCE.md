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

The existing machine-facing capability catalog is the generated
`.mncs/project-view.json`. It now exposes the ambient projection contract and
the read-only projection-evidence and campaign-state inspection interfaces,
including their effects, versions, and invocation paths. This establishes
catalog presence and discoverability through the project view; it does not
establish that an agent found or successfully used those interfaces.

Inspect `capability_accountability_links` in the result. It reports exact
repository, commit, path, and SHA-256 relationships, along with their source
references, the owner audit's declared host responsibilities, the candidate
native capability, the existing next-investigation text, and one bounded
source reference for its language contract, compiler support, and host runtime
provider where those records are available. It also includes a bounded,
session-scoped context-delivery observation when authenticated session events
provide one. The relationship view is bounded to four source links and 8 KiB.
Exact pointers provide traceability, not proof of causation or migration
suitability.

`session_context_delivery_observation` preserves the session ID, work intent,
selected event window, and response event reference beside the captured
binding summary. A complete identity digest and matching aggregate counts can
show that the response summary matches the current binding snapshot, but
partial binding IDs do not provide direct binding-ID attribution. The existing
per-binding projection may derive identity-tuple membership from a matching
digest; inspect it at
`session_activity.capability_delivery.bindings[*].context_response_inclusion`.
Even complete identifiers establish a delivered set only; agent receipt and
use remain separate observations. When the selected event window has no
context response, the projection labels that absence as window-scoped rather
than concluding that the session received no context.

## Entry and persistence observation

The bounded readback
[`session-entry-store-serialization-pressure-20261026.json`](../evidence/environmental-capability/session-entry-store-serialization-pressure-20261026.json)
records two interrupted local entry attempts. One captured Python at roughly
80% CPU and 502–515 MiB resident memory; the second traceback reached JSON
serialization of a canonical Store snapshot during the ambient pass. The
session identity and persistence outcome were not returned, and the snapshot
size and cause of the serialization cost remain unmeasured.

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

When no exact join exists, `unmatched_source_pointer_context` shows bounded
integrity-addressed owner-audit pressure sources beside the candidate native
pressure sources. The comparison uses the same repository, commit, path, and
SHA-256 tuple as the positive link. It makes missing provenance inspectable;
it does not conclude that the sources are unrelated or causally connected.

If the owner source details and native correspondence declare the same pressure
ID but the owner side lacks its pressure source pointer,
`pressure_identity_context` exposes that identity-level match beside both
inspectable source records and the missing-pointer status. It leaves
responsibility matching, causality, and native migration readiness
`not-established`.

## Measurement limits

- Systemwide native adoption remains `not-measured`; these observations are not
  a complete implementation inventory or denominator.
- The captured context response below is session-level evidence and is not
  attributed to the candidate process API. Agent receipt and successful or
  failed use of that capability remain unmeasured.
- Recurrence across sessions is not established by shared owner or pressure
  records.
- A future implementation or migration requires independent correctness and
  reliability verification. This projection does not declare success.

## Live context-delivery evidence

The older authenticated session-event page can be inspected with:

```sh
./scripts/mncs-env projections ses_9ad3efb63438cf3d \
  --interpret environmental-capability \
  --session-event-before-sequence 42 --session-event-limit 1
```

The bounded readback of this query and its separate current context view is
preserved in
[`session-context-delivery-readback-20261026.json`](../evidence/environmental-capability/session-context-delivery-readback-20261026.json).

The projection for session `ses_9ad3efb63438cf3d` records work intent
`int_8475157f281c835f` (“exercise canonical Environment persistence through the
Control socket”). Its authenticated `session.context` event
`evt_de29b1e1b27f5e61` at sequence 41 has a captured response summary for nine
available bindings. The digest and aggregate availability counts match the
current session binding snapshot, while the exact binding ID list is marked
partial and contains no IDs. The projection therefore exposes a concrete
capability-delivery measurement gap, but does not claim that any particular
capability was absent, received by the agent, or used. In this selected page,
the per-binding inclusion view reports `identity-tuple-in-matched-response`
for returned binding rows and
`binding_id_attribution_status=binding-id-capture-incomplete`. This is a
derived identity-tuple relation to the response digest and current session
snapshot, not a direct binding-ID observation; the binding list is truncated
in this page. Agent receipt and use remain unestablished.

The current `mncs-env context` view exposes all nine available binding IDs,
and Control's bounded capture code persists IDs when they are present in a
`session.context` response. That does not retroactively change event 41. The
next useful observation is a subsequent authentic Control context event, so
the projection can verify whether its summary contains exact IDs and compare
that event's binding-level view. The assessment remains session-scoped and
`target_capability_attribution_status` remains `not-established` until a
specific target capability is linked by adequate evidence.

For the broader evidence history, add `--history-pages 4` to that command.
The current expanded page has no exact shared source-pointer match: its owner
audit lists MNCS-Test pressure sources P-004 and P-006, while the candidate
process correspondence points to P-001. The projection exposes both sides in
`unmatched_source_pointer_context` with their integrity-addressed source
references and leaves causality `not-established`. Their SHA-256 values were
also rechecked against the three source files at the recorded MNCS-Test commit.
This is a provenance gap
for further inspection, not evidence that the records describe unrelated
work. The MNCS-Actions source audit also declares pressure identity
`MNCS-TOOLING-B665F138D324`, which matches the process correspondence target;
its owner pressure-link record says
`no-matching-supporting-source-reference`. The new `pressure_identity_context`
exposes this ID-level traceability beside the verified MNCS-Actions audit and
MNCS-Test P-001 source pointers without asserting that the process capability
resolves a particular host responsibility. The source audit's remaining host
responsibility mentions selected consumer/provider invocation and identifies
typed process/filesystem effects as an eliminating capability. The projection
surfaces this as `candidate-text-overlap-only` for the process API, while
`responsibility_match_status` remains `not-established`; the textual overlap is
a lead for inspection, not a migration conclusion. The readback is preserved in
[`unmatched-native-pressure-context-readback-20261026.json`](../evidence/environmental-capability/unmatched-native-pressure-context-readback-20261026.json).
The expanded identity and responsibility view is in
[`pressure-identity-context-readback-20261026.json`](../evidence/environmental-capability/pressure-identity-context-readback-20261026.json).
