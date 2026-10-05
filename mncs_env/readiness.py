"""Bounded readiness observations over provider-owned capability contracts.

Executable discovery is substrate availability, not service readiness. Providers
own status and reconciliation; Environment checks declared JSON observations
and invokes their reconciliation capabilities under existing session authority.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import authority, capabilities

SCHEMA = "mncs.environment.readiness/1"
MAX_SERVICES = 16
PROBE_BUDGET_SECONDS = 15


def _valid_argument(value: Any) -> bool:
    if isinstance(value, str):
        return True
    # The Environment's selected root is a first-class invocation address.
    # It lets a provider bind its resident service to the exact composed
    # workspace without embedding an ambient absolute path in the definition.
    if isinstance(value, dict) and value == {"workspace_root": True}:
        return True
    if isinstance(value, dict) and value == {"selected_repository_roots_json": True}:
        return True
    if not isinstance(value, dict) or set(value) != {"repository", "path"}:
        return False
    relative = value.get("path")
    return (isinstance(value.get("repository"), str) and bool(value["repository"])
            and isinstance(relative, str) and bool(relative)
            and not Path(relative).is_absolute() and ".." not in Path(relative).parts)


def resolve_arguments(session, argv: list[Any]) -> list[str]:
    """Resolve declared path arguments against selected bindings, never cwd."""
    roots = {item["provider"]: item.get("provider_root") for item in session.snapshot.get("bindings", [])}
    result = []
    for argument in argv:
        if not _valid_argument(argument):
            raise ValueError("service argument must be a string, selected repository/path reference, or typed Environment selection reference")
        if isinstance(argument, str):
            result.append(argument)
            continue
        if argument == {"workspace_root": True}:
            selected_root = session.snapshot.get("workspace", {}).get("root")
            if not isinstance(selected_root, str) or not Path(selected_root).is_absolute():
                raise ValueError("Environment workspace root is absent or not absolute")
            result.append(str(Path(selected_root).resolve()))
            continue
        if argument == {"selected_repository_roots_json": True}:
            workspace_root = session.snapshot.get("workspace", {}).get("root")
            if not isinstance(workspace_root, str) or not Path(workspace_root).is_absolute():
                raise ValueError("Environment workspace root is absent or not absolute")
            base = Path(workspace_root).resolve()
            selected = session.snapshot.get("selected_checkouts")
            if not isinstance(selected, dict) or not selected:
                raise ValueError("Environment selected checkout set is absent")
            roots = []
            for repository, checkout in sorted(selected.items()):
                if not isinstance(checkout, dict) or not isinstance(checkout.get("path"), str):
                    raise ValueError(f"selected checkout has no path: {repository}")
                path = Path(checkout["path"])
                if not path.is_absolute():
                    path = base / path
                path = path.resolve()
                if not path.is_dir() or not path.is_relative_to(base):
                    raise ValueError(f"selected checkout escapes the Environment workspace: {repository}")
                roots.append(str(path))
            result.append(json.dumps(roots, ensure_ascii=False, separators=(",", ":")))
            continue
        selected = roots.get(argument["repository"])
        if not selected:
            raise ValueError(f"service argument repository was not selected: {argument['repository']}")
        root = Path(selected).resolve()
        path = (root / argument["path"]).resolve()
        if not path.is_relative_to(root):
            raise ValueError("service argument escapes the selected repository")
        result.append(str(path))
    return result


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
        inputs = service.get("observation_inputs")
        if inputs is not None and inputs != ["selected-repositories"]:
            raise ValueError("service observation_inputs must declare selected-repositories or be omitted for live probing")
        lease = service.get("observation_max_age_ms")
        if lease is not None and (type(lease) is not int or not 1 <= lease <= 3600000):
            raise ValueError("service observation_max_age_ms must be an integer from 1 to 3600000")
        for operation in ("probe", "reconcile"):
            call = service.get(operation)
            if call is None and operation == "reconcile":
                continue
            if not isinstance(call, dict) or not isinstance(call.get("capability"), str) or not call["capability"]:
                raise ValueError(f"service {operation} needs a capability identity")
            argv = call.get("argv", [])
            if not isinstance(argv, list) or any(not _valid_argument(arg) for arg in argv):
                raise ValueError(f"service {operation}.argv must contain strings or selected repository/path references")
        predicates = service.get("ready_when")
        if not isinstance(predicates, dict) or not predicates or any(
            not isinstance(pointer, str) or not pointer.startswith("/") for pointer in predicates
        ):
            raise ValueError("service ready_when needs nonempty JSON-pointer/value predicates")
        response_max = service.get("response_max_bytes", 16384)
        if type(response_max) is not int or not 1 <= response_max <= capabilities.MAX_OUTPUT_LIMIT_BYTES:
            raise ValueError("service response_max_bytes exceeds bounded capability output")
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


def probe_services(session, *, bindings: list[dict] | None = None,
                   composition_identity: str | None = None) -> list[dict[str, Any]]:
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
                state_root = Path(session.store.state_dir).resolve()
                artifact_directory = (state_root / "sessions" / session.session_id / "artifacts"
                                      / capabilities.digest_hex(call["capability"])).resolve()
                if not artifact_directory.is_relative_to(state_root):
                    raise ValueError("service artifact directory escapes the Environment state root")
                result = capabilities.invoke(binding, resolve_arguments(session, call.get("argv", [])), timeout_seconds=min(3.0, remaining),
                                             output_limit_bytes=service.get("response_max_bytes", 16384),
                                             env={**session._selected_runtime_environment(binding),
                                                  "GIT_OPTIONAL_LOCKS": "0",
                                                  "MNCS_ENV_SESSION_ARTIFACT_DIR": str(artifact_directory)})
                record["code"] = "service-probe-failed"
                if result["status"] != "ok":
                    if result["status"] == "timeout":
                        record["code"] = "service-probe-timeout"
                    raise ValueError(f"{result['status']}: {result.get('stderr', '')[-1000:]}")
                record["code"] = "service-response-incompatible"
                document = json.loads(result["stdout"])
                if not isinstance(document, dict) or document.get("schema_version") != service["response_schema"]:
                    raise ValueError(f"expected provider schema {service['response_schema']}")
                # Provider diagnostics remain provider-owned. Preserve bounded
                # structured evidence instead of replacing it with prose.
                diagnostics = document.get("diagnostics")
                if isinstance(diagnostics, list):
                    record["provider_diagnostics"] = diagnostics[:8]
                for field in ("selected", "observed", "recovery"):
                    if field in document:
                        record[f"provider_{field}"] = document[field]
                actual = {pointer: _pointer(document, pointer) for pointer in service["ready_when"]}
                ready = all(actual[key] == expected for key, expected in service["ready_when"].items())
                record.update(status="ready" if ready else "degraded", code="service-ready" if ready else "service-not-ready",
                              reason="provider readiness contract satisfied" if ready else "provider readiness predicates not satisfied",
                              observation=actual)
            except (ValueError, KeyError, OSError) as error:
                record["reason"] = str(error)
        if service["identity"] == session.snapshot.get("execution_compatibility_service"):
            stack = session.snapshot.get("execution_stack") or {}
            record["composition_identity"] = composition_identity or stack.get("identity")
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


def reconcile_services(session, *, force_recovery: bool = False,
                       retry_gate=None) -> dict[str, Any]:
    """Probe, delegate recovery only when needed, then verify again.

    Observation is always live; the recovery ACTION for an unchanged
    failure is withheld while the family retry law says another attempt
    has little value. `force_recovery` bypasses suppression for explicit
    recovery. `retry_gate` is an injectable
    `(base, max_delay, attempts, elapsed) -> (eligible, detail)` decision;
    production always uses the native MNCS law via `retry.default_retry_gate`.
    """
    from .sessions import AuthorityDenied, LifecycleError
    from . import retry as retry_module
    observations = probe_services(session)
    operations = []
    deadline = time.monotonic() + 20
    declared = {item["identity"]: item for item in session.snapshot.get("requirements", {}).get("services", [])}
    by_capability = {item["capability"]: item for item in session.snapshot.get("bindings", [])}
    heads = retry_module.checkout_heads(session)
    backoff = retry_module.backoff_state(session)
    policy = retry_module.load_retry_policy(session)
    gate = retry_gate or retry_module.default_retry_gate(session)
    now = datetime.now(timezone.utc)
    pre_identities: dict[str, dict[str, Any]] = {}
    attempted: set[str] = set()
    for observation in observations:
        identity = observation["identity"]
        call = declared[identity].get("reconcile")
        if observation["status"] == "ready" or call is None:
            continue
        if observation["code"] == "service-response-incompatible":
            operations.append({"identity": identity, "status": "not-attempted",
                               "reason": "provider schema is incompatible; repair the declared contract before recovery"})
            continue
        failure = retry_module.failure_identity(observation, by_capability.get(call["capability"]), heads)
        pre_identities[identity] = failure
        entry = backoff.get(failure["digest"], {})
        attempts = int(entry.get("attempts", 0) or 0)
        consulted: dict[str, Any] | None = None
        if not force_recovery and attempts > 0:
            elapsed = retry_module.seconds_since(str(entry.get("last_attempt_at", "")), now)
            eligible, detail = gate(base=policy["base_delay_secs"], max_delay=policy["max_delay_secs"],
                                    attempts=attempts, elapsed=elapsed)
            if not eligible:
                operations.append({"identity": identity, "status": "suppressed",
                                   "reason": "unchanged failure; the retry law withholds another identical "
                                             f"recovery attempt (attempts={attempts}, elapsed={elapsed}s, "
                                             f"retry_in={detail.get('retry_in_secs')}s)",
                                   "failure_identity": failure["digest"], "retry": detail})
                continue
            consulted = detail
        try:
            if time.monotonic() >= deadline:
                operations.append({"identity": identity, "status": "deferred", "reason": "reconciliation budget exhausted"})
                continue
            result = session.invoke(call["capability"], resolve_arguments(session, call.get("argv", [])), timeout_seconds=min(10, deadline-time.monotonic()),
                                    output_limit_bytes=16384)
            if result.get("status") != "pending-escalation":
                attempted.add(identity)
            operation = {"identity": identity, "status": result["status"],
                         "reason": result.get("stderr", "")[-1000:]}
            try:
                response = json.loads(result.get("stdout", ""))
                if isinstance(response, dict):
                    operation["provider_response"] = response
            except ValueError:
                pass
            if consulted is not None:
                operation["retry"] = {**consulted, "eligible": True,
                                      "note": "retry law re-admitted recovery"}
            elif attempts > 0 and force_recovery:
                operation["retry"] = {"attempts": attempts, "forced": True,
                                      "note": "explicit recovery bypassed suppression"}
            operations.append(operation)
        except (AuthorityDenied, LifecycleError, ValueError, OSError) as error:
            # Session authority/lifecycle exceptions carry actionable reasons;
            # a failed optional provider must not discard a durable entry.
            # The provider never ran, so this is not a failed recovery
            # attempt: it stays visible every run instead of backing off.
            operations.append({"identity": identity, "status": "failed", "reason": str(error)})
    if attempted:
        observations = probe_services(session)
    final_by_identity = {item["identity"]: item for item in observations}
    carried: dict[str, Any] = {}
    for service_id, failure in pre_identities.items():
        final = final_by_identity.get(service_id)
        call = declared[service_id].get("reconcile")
        if final is None or final.get("status") == "ready" or call is None:
            continue
        current = retry_module.failure_identity(final, by_capability.get(call["capability"]), heads)
        if service_id in attempted and current["digest"] == failure["digest"]:
            carried[failure["digest"]] = {"service": service_id, "attempts": attempts_for(backoff, failure) + 1,
                                          "last_attempt_at": retry_module.utcnow(), "identity": failure}
        elif current["digest"] in backoff:
            carried[current["digest"]] = backoff[current["digest"]]
    retry_module.store_backoff_state(session, carried)
    session.snapshot["service_observations"] = observations
    session.snapshot["service_operations"] = operations
    session.snapshot["last_reconciled_at"] = capabilities.utcnow()
    from . import composition
    session.snapshot["execution_stack"] = composition.resolve(
        session.snapshot.get("execution_roles", {}), session.snapshot.get("bindings", []),
        session.snapshot.get("toolchain"), session.snapshot.get("execution_stack"),
        compatibility_service=session.snapshot.get("execution_compatibility_service"),
        service_observations=observations,
    )
    session._save()
    return {"operations": operations, "readiness": summarize(session.snapshot)}


def attempts_for(backoff: dict[str, Any], failure: dict[str, Any]) -> int:
    entry = backoff.get(failure["digest"], {})
    return int(entry.get("attempts", 0) or 0)
