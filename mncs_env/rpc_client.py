"""Client for Control's authenticated Environment persistence transport."""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import struct
import sys
from pathlib import Path
from typing import Any

MAX_FRAME = 2 * 1024 * 1024


class _RequestNotSent(OSError):
    pass


def _read_exact(connection: socket.socket, count: int) -> bytes:
    result = bytearray()
    while len(result) < count:
        part = connection.recv(count - len(result))
        if not part:
            raise ConnectionError("Control closed the Environment transport")
        result.extend(part)
    return bytes(result)


def _exchange(socket_path: str, grant: str, request: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps({**request, "grant": grant}, separators=(",", ":")).encode()
    if len(payload) > MAX_FRAME:
        raise ValueError("Environment RPC request exceeds its size limit")
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(300.0)
    try:
        try:
            connection.connect(socket_path)
        except OSError as error:
            raise _RequestNotSent(str(error)) from error
        try:
            connection.sendall(struct.pack("!I", len(payload)) + payload)
        except OSError as error:
            raise ConnectionError(f"request delivery may be partial: {error}") from error
        size = struct.unpack("!I", _read_exact(connection, 4))[0]
        if size < 2 or size > MAX_FRAME:
            raise ValueError("Environment RPC response size is invalid")
        result = json.loads(_read_exact(connection, size).decode("utf-8"))
        if not isinstance(result, dict):
            raise TypeError("Environment RPC response is invalid")
        return result
    finally:
        connection.close()


def dispatch(argv: list[str]) -> int:
    """Forward one CLI invocation, retrying ambiguous delivery with one id."""
    socket_path = os.environ.get("MNCS_ENV_RPC_SOCKET", "")
    grant_path = os.environ.get("MNCS_ENV_RPC_GRANT_FILE", "")
    try:
        grant = Path(grant_path).read_text(encoding="ascii").strip()
    except OSError as error:
        return _fail("persistence-service-unavailable",
                     f"Control Environment grant is unavailable: {error}",
                     "run this command through the MNCS Control protected execution path")
    if not socket_path or not grant:
        return _fail("persistence-service-unavailable",
                     "Control Environment transport configuration is incomplete",
                     "run this command through the MNCS Control protected execution path")
    supplied_request_id = os.environ.get("MNCS_ENV_RPC_REQUEST_ID", "")
    request_id = (supplied_request_id if re.fullmatch(r"req_[A-Za-z0-9_-]{1,76}", supplied_request_id)
                  else "req_" + secrets.token_hex(20))
    request = {
        "argv": list(argv),
        "cwd": os.getcwd(),
        "request_id": request_id,
    }
    for _ in range(2):
        try:
            response = _exchange(socket_path, grant, request)
        except _RequestNotSent as error:
            return _fail("persistence-service-unavailable", str(error),
                         "check the Control Environment publication endpoint")
        except (OSError, ConnectionError, TimeoutError, ValueError, TypeError):
            continue
        if not response.get("ok"):
            error = response.get("error") or {}
            code = str(error.get("code", "ENVIRONMENT_RPC_REJECTED"))
            category = {
                "ENVIRONMENT_AUTHORITY_DENIED": "authority-denied",
                "ENVIRONMENT_PUBLICATION_CONFLICT": "publication-conflict",
                "ENVIRONMENT_STORE_CORRUPTION": "store-corruption",
                "ENVIRONMENT_DIRECT_READ_ONLY": "direct-filesystem-read-only",
                "ENVIRONMENT_PUBLICATION_AMBIGUOUS": "publication-outcome-ambiguous",
                "ENVIRONMENT_PERSISTENCE_UNAVAILABLE": "persistence-service-unavailable",
                "ENVIRONMENT_UNAVAILABLE": "persistence-service-unavailable",
            }.get(code, "publication-rejected")
            return _fail(category, str(error.get("message", code)),
                         str((error.get("details") or {}).get("next", "inspect the structured Environment diagnostic")))
        result = response.get("result") or {}
        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "")
        exit_code = int(result.get("exit_code", 1))
        if exit_code != 0:
            failed = _cli_error(stderr) or _cli_error(stdout)
            if failed is not None:
                diagnostics = failed.get("diagnostics")
                diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
                raw_code = str(diagnostics.get("code", ""))
                category = {
                    "session-snapshot-conflict": "publication-conflict",
                    "claim-conflict": "publication-conflict",
                    "claim-adoption-required": "authority-denied",
                    "store-integrity-failure": "store-corruption",
                    "store-unavailable": "persistence-service-unavailable",
                    "persistence-service-unavailable": "persistence-service-unavailable",
                    "direct-filesystem-read-only": "direct-filesystem-read-only",
                    "rights-blocked": "authority-denied",
                    "campaign-owner-conflict": "authority-denied",
                    "campaign-continuation-unverified": "authority-denied",
                }.get(raw_code, "publication-rejected")
                message = str(failed.get("error", "Environment operation was rejected"))
                _fail(category, message,
                      str(diagnostics.get("next", "inspect the structured Environment diagnostic")))
                return exit_code
        if stdout:
            print(stdout, end="" if stdout.endswith("\n") else "\n")
        if stderr:
            print(stderr, end="" if stderr.endswith("\n") else "\n", file=sys.stderr)
        return exit_code
    return _fail("publication-outcome-ambiguous",
                 "Control did not return a result after the request was sent; retry with a fresh reconciliation read",
                 f"request_id={request['request_id']}")


def _fail(category: str, message: str, next_step: str) -> int:
    print(json.dumps({
        "error": message,
        "diagnostics": {
            "code": category,
            "transport": "mncs-control-environment-rpc",
            "publication_completed": False if category != "publication-outcome-ambiguous" else None,
            "next": next_step,
        },
    }, ensure_ascii=False))
    return 3


def _cli_error(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        result = json.loads(value)
    except json.JSONDecodeError:
        return None
    return result if isinstance(result, dict) and isinstance(result.get("error"), str) else None
