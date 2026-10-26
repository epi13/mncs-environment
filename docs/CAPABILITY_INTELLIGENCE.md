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
