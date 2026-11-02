"""Native classification of bounded Environment Doctor binding outcomes."""

from __future__ import annotations

from typing import Any

from . import native_readiness

MAX_BINDINGS = 256
MAX_STATUS_BYTES = 32
MAX_CODE_BYTES = 128
NATIVE_SCHEMA = "mncs.environment.doctor-unavailable-classification/1"


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
