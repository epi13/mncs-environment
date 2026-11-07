# RFC 0003 — Request-time evidence and native correlation

Status: implemented experimental observation boundary; no learned operation control.

Extend canonical Environment events and immutable session evidence to preserve
what the owner knew before an actual non-ambient capability request. Use existing
session, task, event, binding and provider request identities. Context publication
is bounded and content addressed; no new telemetry database or authority exists.

The native correlation kernel validates bounded identity, chronology, ambiguity,
missing-state and observed-outcome joins. Host code transports facts and checks
existing strict owner evidence. Current permissions, execution and verification
remain with their existing authorities. Available is not delivered, delivered is
not useful, and a completed invocation is not a verified outcome. No universal
burden score, complete need denominator or causal saving is inferred.

The normal execution/admission implementation is preserved. Explicit refusal,
nonexecution and missing capture remain visible. Ambient owner-pass work keeps
its existing trace and is excluded from extra demand observations. Native policy
unavailability leaves raw evidence available without a host policy fallback.

See the executable contract and limits in
[DEMAND_OBSERVATION.md](../docs/DEMAND_OBSERVATION.md). The direct model-facing
boundary remains `mncs-models` RFC 0005; no model runtime is copied here.
