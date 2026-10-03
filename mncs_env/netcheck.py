"""Layered GitHub reachability probe.

A failed ``git fetch``/``git push`` conflates several unrelated faults:
name resolution, TCP/TLS connectivity, HTTP availability, and
authentication/authorization. Agents that treat every failure as
"GitHub is down" (or as an auth problem) misroute recovery: a DNS
blip needs a retry, an auth failure needs credentials, and neither is
fixed by the other's remedy.

:func:`check` separates those layers so callers can report *which*
boundary failed instead of an opaque Git error. It performs no
mutation: DNS lookup, one TCP+TLS handshake, one HTTPS request, and
optionally one read-only ``git ls-remote``.
"""

from __future__ import annotations

import http.client
import os
import socket
import ssl
import subprocess
import threading
from typing import Any
from urllib.parse import urlparse

from . import workspace as workspace_module

DEFAULT_HOST = "github.com"
DEFAULT_TIMEOUT = 5.0


def _ok(**fields: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"status": "ok"}
    result.update(fields)
    return result


def _unavailable(reason: str, detail: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {"status": "unavailable", "reason": reason}
    if detail:
        result["detail"] = detail[:300]
    return result


def _skipped(reason: str) -> dict[str, Any]:
    return {"status": "skipped", "reason": reason}


def check_dns(host: str, *, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Resolve ``host``; distinguishes resolver failure from the rest."""
    outcome: dict[str, Any] = {}

    def resolve() -> None:
        try:
            outcome["infos"] = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except Exception as error:  # noqa: BLE001 - surfaced as detail
            outcome["error"] = error

    worker = threading.Thread(target=resolve, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        return _unavailable("dns-timeout", f"no answer within {timeout}s")
    if "error" in outcome:
        error = outcome["error"]
        if isinstance(error, socket.gaierror):
            return _unavailable("dns-resolution-failed", str(error))
        if isinstance(error, (OSError, ValueError)):
            return _unavailable("dns-error", f"{type(error).__name__}: {error}")
        return _unavailable("dns-error", repr(error)[:150])
    infos = outcome.get("infos", [])
    addresses = sorted({info[4][0] for info in infos if info[4]})
    if not addresses:
        return _unavailable("dns-no-addresses")
    return _ok(addresses=addresses)


def check_tcp_tls(host: str, *, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Complete a TCP connection plus TLS handshake against ``host``:443."""
    context = ssl.create_default_context()
    try:
        raw = socket.create_connection((host, 443), timeout=timeout)
    except socket.gaierror as error:
        return _unavailable("dns-resolution-failed", str(error))
    except (OSError, ValueError) as error:
        return _unavailable("tcp-connect-failed", f"{type(error).__name__}: {error}")
    try:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            tls.settimeout(timeout)
            peer = tls.getpeercert()
    except (ssl.SSLError, OSError, ValueError) as error:
        raw.close()
        return _unavailable("tls-handshake-failed", f"{type(error).__name__}: {error}")
    subject = ""
    if isinstance(peer, dict):
        subject = str(peer.get("subject", ""))
    return _ok(tls_subject=subject)


def check_https(host: str, *, timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Issue one HTTPS HEAD request; surfaces proxy/HTTP faults."""
    try:
        connection = http.client.HTTPSConnection(host, timeout=timeout)
        connection.request("HEAD", "/")
        response = connection.getresponse()
        status = int(response.status)
        connection.close()
    except socket.gaierror as error:
        return _unavailable("dns-resolution-failed", str(error))
    except (ssl.SSLError, OSError, ValueError) as error:
        return _unavailable("https-failed", f"{type(error).__name__}: {error}")
    if status >= 500:
        return _unavailable("https-server-error", f"HTTP {status}")
    return _ok(http_status=status)


def classify_git_stderr(stderr: str) -> str:
    """Map ``git ls-remote`` stderr to a failure boundary (no network use)."""
    lowered = stderr.lower()
    if "could not resolve host" in lowered or "could not resolve hostname" in lowered:
        return "dns-resolution-failed"
    if "temporary failure in name resolution" in lowered or "name or service not known" in lowered:
        return "dns-resolution-failed"
    if "failed to connect" in lowered or "connection refused" in lowered:
        return "tcp-connect-failed"
    if "connection timed out" in lowered or "operation timed out" in lowered:
        return "tcp-connect-failed"
    if "ssl" in lowered and ("certificate" in lowered or "handshake" in lowered):
        return "tls-handshake-failed"
    if "401" in lowered or "403" in lowered or "authentication failed" in lowered:
        return "authentication-failed"
    if "repository not found" in lowered or "404" in lowered:
        return "repository-not-found"
    if "permission denied" in lowered and "publickey" in lowered:
        return "authentication-failed"
    return "git-command-failed"


def check_git(remote: str, *, timeout: float = 25.0) -> dict[str, Any]:
    """Read-only ``git ls-remote`` against ``remote`` with classified errors."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        completed = subprocess.run(
            [workspace_module.git_binary(), "ls-remote", remote, "HEAD"],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
    except FileNotFoundError:
        return _unavailable("git-missing", "git executable not found")
    except subprocess.TimeoutExpired:
        return _unavailable("git-timeout", f"no answer within {timeout}s")
    except OSError as error:
        return _unavailable("git-error", f"{type(error).__name__}: {error}")
    if completed.returncode == 0:
        tip = completed.stdout.split()[0] if completed.stdout.split() else ""
        return _ok(tip=tip)
    stderr = (completed.stderr or completed.stdout or "").strip()
    return _unavailable(classify_git_stderr(stderr), stderr[-300:])


def check(host: str = DEFAULT_HOST, *, remote: str | None = None,
          timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Probe DNS, TCP+TLS, HTTPS, and optionally one git remote.

    Later layers report ``skipped`` when an earlier layer already
    failed, so the first non-ok layer names the failure boundary.
    When ``remote`` names an ssh-style URL, the HTTPS layer is still
    probed against ``host`` (connectivity signal) while the git layer
    carries the authoritative verdict.
    """
    if remote and not host:
        parsed = urlparse(remote)
        host = parsed.hostname or DEFAULT_HOST
    dns = check_dns(host, timeout=timeout)
    if dns["status"] != "ok":
        return {"host": host, "remote": remote, "dns": dns,
                "tcp_tls": _skipped("dns-unavailable"),
                "https": _skipped("dns-unavailable"),
                "git": _skipped("dns-unavailable") if remote else None,
                "verdict": dns["reason"]}
    tcp_tls = check_tcp_tls(host, timeout=timeout)
    if tcp_tls["status"] != "ok":
        return {"host": host, "remote": remote, "dns": dns, "tcp_tls": tcp_tls,
                "https": _skipped("tcp-tls-unavailable"),
                "git": _skipped("tcp-tls-unavailable") if remote else None,
                "verdict": tcp_tls["reason"]}
    https = check_https(host, timeout=timeout)
    report: dict[str, Any] = {"host": host, "remote": remote, "dns": dns,
                              "tcp_tls": tcp_tls, "https": https}
    if remote:
        git_result = check_git(remote)
        report["git"] = git_result
        if git_result["status"] != "ok":
            report["verdict"] = git_result["reason"]
            return report
    else:
        report["git"] = None
    if https["status"] != "ok":
        report["verdict"] = https["reason"]
        return report
    report["verdict"] = "reachable"
    return report
