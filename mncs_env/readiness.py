"""Bounded readiness observations over provider-owned capability contracts.

Executable discovery is substrate availability, not service readiness. Providers
own status and reconciliation; Environment checks declared JSON observations
and invokes their reconciliation capabilities under existing session authority.
"""

from __future__ import annotations

import json
import time
from typing import Any

from . import authority, capabilities

SCHEMA = "mncs.environment.readiness/1"
MAX_SERVICES = 16
PROBE_BUDGET_SECONDS = 15


def validate_requirements(definition: dict[str, Any]) -> dict[str, Any]:
    required = definition.get("required_capabilities", [])
    services = definition.get("services", [])
    if not isinstance(required, list) or any(not isinstance(item, str) or not item for item in required):
        raise ValueError("required_capabilities must be a list of capability identities")
    if not isinstance(services, list) or len(services) > MAX_SERVICES:
        raise ValueError(f"services must be a list of at most {MAX_SERVICES} provider bindings")
    identities = set()
    for service in services:
        if not isinstance(service, dict) or not isinstance(service.get("identity"), str) or not service["identity"]:
            raise ValueError("each service needs a stable identity")
        if service["identity"] in identities:
            raise ValueError(f"duplicate service identity: {service['identity']}")
        identities.add(service["identity"])
        if type(service.get("required", True)) is not bool:
            raise ValueError("service required must be a boolean")
        for operation in ("probe", "reconcile"):
            call = service.get(operation)
            if call is None and operation == "reconcile":
                continue
            if not isinstance(call, dict) or not isinstance(call.get("capability"), str) or not call["capability"]:
                raise ValueError(f"service {operation} needs a capability identity")
            argv = call.get("argv", [])
            if not isinstance(argv, list) or any(not isinstance(arg, str) for arg in argv):
                raise ValueError(f"service {operation}.argv must be a list of strings")
        predicates = service.get("ready_when")
        if not isinstance(predicates, dict) or not predicates or any(
            not isinstance(pointer, str) or not pointer.startswith("/") for pointer in predicates
        ):
            raise ValueError("service ready_when needs nonempty JSON-pointer/value predicates")
        schema = service.get("response_schema")
        if not isinstance(schema, str) or not schema:
            raise ValueError("service response_schema must identify the provider's JSON contract")
    return {"required_capabilities": sorted(set(required)), "services": services}


def _pointer(document: Any, pointer: str) -> Any:
    for token in pointer[1:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if not isinstance(document, dict) or token not in document:
            raise KeyError(pointer)
        document = document[token]
    return document


def probe_services(session, *, bindings: list[dict] | None = None) -> list[dict[str, Any]]:
    """Read-only, bounded provider probes; never start services or write events."""
    by_id = {item["capability"]: item for item in (bindings if bindings is not None else session.snapshot.get("bindings", []))}
    observations = []
    deadline = time.monotonic() + PROBE_BUDGET_SECONDS
    for service in session.snapshot.get("requirements", {}).get("services", []):
        call = service["probe"]
        binding = by_id.get(call["capability"])
        record = {"identity": service["identity"], "required": service.get("required", True),
                  "capability": call["capability"], "status": "unavailable", "code": "service-binding-missing",
                  "reason": "provider probe capability was not discovered", "observed_at": capabilities.utcnow(),
                  "recovery": service.get("reconcile")}
        if binding is not None:
            try:
                if binding.get("availability", {}).get("status") != "available":
                    raise ValueError(binding.get("availability", {}).get("reason", "probe unavailable"))
                if binding.get("effects") != ["read"] or binding.get("provenance", {}).get("addressing") == "descriptor-fingerprint":
                    record["code"] = "service-probe-not-read-only"
                    raise ValueError("readiness requires an explicitly addressed read-only provider capability")
                verdict = authority.evaluate(session.snapshot.get("authority", {}), action="invoke",
                                             target=call["capability"], session_id=session.session_id)
                if verdict["verdict"] != "allow":
                    record["code"] = "service-probe-denied"
                    raise ValueError(verdict["reason"])
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    record["code"] = "service-probe-budget"
                    raise ValueError("entry probe budget exhausted; retry health for a fresh observation")
                result = capabilities.invoke(binding, call.get("argv", []), timeout_seconds=min(3.0, remaining),
                                             output_limit_bytes=16384)
                record["code"] = "service-probe-failed"
                if result["status"] != "ok":
                    raise ValueError(f"{result['status']}: {result.get('stderr', '')[-1000:]}")
                record["code"] = "service-response-incompatible"
                document = json.loads(result["stdout"])
                if not isinstance(document, dict) or document.get("schema_version") != service["response_schema"]:
                    raise ValueError(f"expected provider schema {service['response_schema']}")
                actual = {pointer: _pointer(document, pointer) for pointer in service["ready_when"]}
                ready = all(actual[key] == expected for key, expected in service["ready_when"].items())
                record.update(status="ready" if ready else "degraded", code="service-ready" if ready else "service-not-ready",
                              reason="provider readiness contract satisfied" if ready else "provider readiness predicates not satisfied",
                              observation=actual)
            except (ValueError, KeyError, OSError) as error:
                record["reason"] = str(error)
        observations.append(record)
    return observations


def summarize(snapshot: dict[str, Any], *, bindings: list[dict] | None = None,
              services: list[dict] | None = None, live: bool = False) -> dict[str, Any]:
    """Project readiness independently of the session lifecycle."""
    bindings = bindings if bindings is not None else snapshot.get("bindings", [])
    services = services if services is not None else snapshot.get("service_observations", [])
    by_id = {item["capability"]: item for item in bindings}
    required = snapshot.get("requirements", {}).get("required_capabilities", [])
    missing = [name for name in required if by_id.get(name, {}).get("availability", {}).get("status") != "available"]
    unavailable = [item["capability"] for item in bindings if item.get("availability", {}).get("status") != "available"]
    blocking = missing + [item["identity"] for item in services if item.get("required") and item["status"] != "ready"]
    observed_ids = {item["identity"] for item in services}
    blocking.extend(item["identity"] for item in snapshot.get("requirements", {}).get("services", [])
                    if item.get("required", True) and item["identity"] not in observed_ids)
    scan = snapshot.get("workspace", {}).get("scan", {})
    if scan.get("status") != "complete":
        blocking.append("workspace-scan-incomplete")
    status = "blocked" if blocking else "degraded" if unavailable or not bindings or any(item["status"] != "ready" for item in services) else "ready"
    return {"schema_version": SCHEMA, "status": status, "observation": "live" if live else "snapshot",
            "scope": "declared requirements and discovered capabilities in the selected workspace",
            "available_count": len(bindings) - len(unavailable), "unavailable_count": len(unavailable),
            "required_unavailable": missing, "blocking": blocking, "services": services,
            "verification": "provider probes for declared services; executable substrate for other capabilities",
            "last_reconciled_at": snapshot.get("last_reconciled_at")}


def reconcile_services(session) -> dict[str, Any]:
    """Probe, delegate recovery only when needed, then verify again."""
    from .sessions import AuthorityDenied, LifecycleError
    observations = probe_services(session)
    operations = []
    deadline = time.monotonic() + 20
    declared = {item["identity"]: item for item in session.snapshot.get("requirements", {}).get("services", [])}
    for observation in observations:
        call = declared[observation["identity"]].get("reconcile")
        if observation["status"] == "ready" or call is None:
            continue
        if observation["code"] == "service-response-incompatible":
            operations.append({"identity": observation["identity"], "status": "not-attempted",
                               "reason": "provider schema is incompatible; repair the declared contract before recovery"})
            continue
        try:
            if time.monotonic() >= deadline:
                operations.append({"identity": observation["identity"], "status": "deferred", "reason": "reconciliation budget exhausted"})
                continue
            result = session.invoke(call["capability"], call.get("argv", []), timeout_seconds=min(10, deadline-time.monotonic()),
                                    output_limit_bytes=16384)
            operations.append({"identity": observation["identity"], "status": result["status"],
                               "reason": result.get("stderr", "")[-1000:]})
        except (AuthorityDenied, LifecycleError, ValueError, OSError) as error:
            # Session authority/lifecycle exceptions carry actionable reasons;
            # a failed optional provider must not discard a durable entry.
            operations.append({"identity": observation["identity"], "status": "failed", "reason": str(error)})
    if operations:
        observations = probe_services(session)
    session.snapshot["service_observations"] = observations
    session.snapshot["service_operations"] = operations
    session.snapshot["last_reconciled_at"] = capabilities.utcnow()
    session._save()
    return {"operations": operations, "readiness": summarize(session.snapshot)}
