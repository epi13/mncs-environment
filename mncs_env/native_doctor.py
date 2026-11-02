"""Native classification of bounded Environment Doctor binding outcomes."""

from __future__ import annotations

from typing import Any

from . import native_readiness

MAX_BINDINGS = 256
MAX_STATUS_BYTES = 32
MAX_CODE_BYTES = 128
NATIVE_SCHEMA = "mncs.environment.doctor-unavailable-classification/1"
NATIVE_REPAIR_SCHEMA = "mncs.environment.doctor-repair-delta/1"
MAX_REPAIR_ROWS = 64


def classify_unavailable_native(bindings: list[dict[str, Any]], session) -> list[int] | None:
    """Return per-binding classes, or ``None`` for the Python compatibility path.

    Class 0 is available, 1 is transient substrate loss, and 2 is a provider
    declaration gap. The host retains provider-owned evidence fields.
    """
    if not isinstance(bindings, list) or not 0 < len(bindings) <= MAX_BINDINGS:
        return None
    encoded_bindings = []
    for binding in bindings:
        if not isinstance(binding, dict):
            return None
        availability = binding.get("availability", {})
        if not isinstance(availability, dict):
            return None
        status = availability.get("status")
        code = availability.get("code")
        status = status if isinstance(status, str) else ""
        code = code if isinstance(code, str) else ""
        try:
            status_bytes = status.encode("utf-8")
            code_bytes = code.encode("utf-8")
        except UnicodeEncodeError:
            return None
        if len(status_bytes) > MAX_STATUS_BYTES or len(code_bytes) > MAX_CODE_BYTES:
            return None
        encoded_bindings.append({"status": status, "code": code})

    paths = native_readiness._toolchain_paths(session)
    if paths is None:
        return None
    binary, stdlib = paths
    result = native_readiness._run_native_application(
        session=session,
        binary=binary,
        stdlib=stdlib,
        descriptor_name="doctor-unavailable-classification.json",
        request={
            "schema_version": "mncs.environment.doctor-unavailable-classification-request/1",
            "bindings": encoded_bindings,
        },
        expected_schema=NATIVE_SCHEMA,
        timeout_seconds=15.0,
    )
    if result.get("valid") is not True:
        raise native_readiness.NativeApplicationError(
            "native Doctor classifier rejected a bounded request"
        )
    count = result.get("binding_count")
    classes = result.get("classes")
    if type(count) is not int or count != len(bindings):
        raise native_readiness.NativeApplicationError(
            "native Doctor classifier returned an incomplete binding set"
        )
    if not isinstance(classes, list) or len(classes) != len(bindings):
        raise native_readiness.NativeApplicationError(
            "native Doctor classifier returned malformed classes"
        )
    if any(type(value) is not int or value not in (0, 1, 2) for value in classes):
        raise native_readiness.NativeApplicationError(
            "native Doctor classifier returned an invalid class"
        )
    return classes


def project_repair_delta_native(
    previous: list[dict[str, Any]],
    current: list[dict[str, Any]],
    previous_services: list[dict[str, Any]],
    current_services: list[dict[str, Any]],
    session,
) -> dict[str, list[bool]] | None:
    """Identify newly available bindings and ready services in MNCS.

    The host materializes the resulting repair records. Unsupported values or
    collections outside the bounded window remain on the Python reference path.
    """
    collections = (
        ("previous_bindings", previous, True),
        ("current_bindings", current, True),
        ("previous_services", previous_services, False),
        ("current_services", current_services, False),
    )
    encoded: dict[str, list[dict[str, Any]]] = {}
    for field, values, is_binding in collections:
        if not isinstance(values, list) or len(values) > MAX_REPAIR_ROWS:
            return None
        records: list[dict[str, Any]] = []
        encoded[field] = records
        for item in values:
            if not isinstance(item, dict):
                return None
            if is_binding:
                key = item.get("capability")
                observation = item.get("availability", {})
                if not isinstance(observation, dict):
                    return None
                status = observation.get("status")
            else:
                key = item.get("identity")
                status = item.get("status")
            if key is not None and not isinstance(key, str):
                return None
            status_text = status if isinstance(status, str) else ""
            try:
                key_bytes = key.encode("utf-8") if isinstance(key, str) else b""
                status_bytes = status_text.encode("utf-8")
            except UnicodeEncodeError:
                return None
            if len(key_bytes) > 256 or len(status_bytes) > MAX_STATUS_BYTES:
                return None
            records.append({
                "has_identity": key is not None,
                "identity": key if key is not None else "",
                "status": status_text,
            })

    paths = native_readiness._toolchain_paths(session)
    if paths is None:
        return None
    binary, stdlib = paths
    result = native_readiness._run_native_application(
        session=session,
        binary=binary,
        stdlib=stdlib,
        descriptor_name="doctor-repair-delta.json",
        request={
            "schema_version": "mncs.environment.doctor-repair-delta-request/1",
            **encoded,
        },
        expected_schema=NATIVE_REPAIR_SCHEMA,
        timeout_seconds=15.0,
    )
    if result.get("valid") is not True:
        raise native_readiness.NativeApplicationError(
            "native Doctor repair delta rejected a bounded request"
        )
    output_fields = (
        ("binding_count", "binding_recovered", len(current)),
        ("service_count", "service_recovered", len(current_services)),
    )
    output: dict[str, list[bool]] = {}
    for count_field, values_field, expected_count in output_fields:
        if type(result.get(count_field)) is not int or result[count_field] != expected_count:
            raise native_readiness.NativeApplicationError(
                "native Doctor repair delta returned an incomplete result"
            )
        flags = result.get(values_field)
        if (not isinstance(flags, list) or len(flags) != expected_count
                or any(type(flag) is not bool for flag in flags)):
            raise native_readiness.NativeApplicationError(
                "native Doctor repair delta returned malformed transition flags"
            )
        output[values_field] = flags
    return output
