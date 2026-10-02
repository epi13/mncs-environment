"""Generation-bound verification evidence for projections (transport, not policy).

Scope note: this module binds verdicts to projection subjects
(repository HEAD, worktree, input digest) and enforces executor
confinement. Ambient suite execution/queueing belongs to the parallel
ambient-verification track (`mncs.test.verification_coherence`); the
two converge on shared evidence identities as that track lands (see
the convergence pressure in pressures/registry.json).

A projection requiring verified inputs receives PASS only when this
module can identify valid verification evidence for the exact source
subject it is projecting. Subjects bind repository HEAD, worktree
content, and the projection input digest; evidence for any other
subject never authorizes this one.

Evidence comes from provider-owned verification executors selected by
the projection declaration. Executors that attest `verify` effects may
run on dirty branches; their writes are confined after the run to
declared ephemeral roots and git-ignored scratch, and evidence is
recorded only when the subject is identical before and after the run.
Anything unobservable, unrunnable, or moved mid-run stays UNKNOWN:
deterministic rendering alone can never create PASS.

Verdict *meaning* (PASS releases, UNKNOWN awaits, FAIL withholds)
lives in `mncs.automation.reconcile.v1`. This file observes subjects,
invokes executors, parses provider verdict channels, and records
immutable evidence. Executor result interpretation prefers an explicit
`mncs.check-result/1`-shaped envelope and otherwise honors the
provider's own exit code, always recording which channel applied.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import projection_store as store_module
from .identity import digest_hex

VERDICT_FAIL = 0
VERDICT_PASS = 1
VERDICT_UNKNOWN = 2

VERDICT_NAMES = {0: "fail", 1: "pass", 2: "unknown"}

#: Bounds for subject observation. Subjects that exceed them cannot be
#: bound to evidence and stay UNKNOWN.
MAX_DIFF_BYTES = 1024 * 1024
MAX_UNTRACKED_FILES = 512
MAX_UNTRACKED_BYTES = 1024 * 1024
MAX_CONFINEMENT_FILES = 512

#: Session-local backoff after an executor fails to produce a verdict,
#: so one wedged executor cannot stall every ambient pass.
RETRY_BACKOFF_SECONDS = 300


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _git(checkout: Path, *argv: str, timeout: int = 30) -> bytes | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(checkout), *argv],
            capture_output=True, timeout=timeout,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def observe_subject(checkout: Path, input_digest: str | None) -> dict | None:
    """Bind the exact candidate state under verification, or None."""
    raw_head = _git(checkout, "rev-parse", "HEAD")
    raw_branch = _git(checkout, "rev-parse", "--abbrev-ref", "HEAD")
    if raw_head is None:
        return None
    diff = _git(checkout, "diff", "HEAD", "--", ".")
    if diff is None or len(diff) > MAX_DIFF_BYTES:
        return None
    raw_untracked = _git(checkout, "ls-files", "--others",
                         "--exclude-standard", "-z")
    if raw_untracked is None:
        return None
    names = sorted(name for name in raw_untracked.decode(
        "utf-8", "replace").split("\0") if name)
    if len(names) > MAX_UNTRACKED_FILES:
        return None
    digest = hashlib.sha256()
    digest.update(raw_head.strip())
    digest.update(b"\0")
    digest.update(diff)
    total = 0
    for name in names:
        try:
            content = (checkout / name).read_bytes()
        except OSError:
            return None
        total += len(content)
        if total > MAX_UNTRACKED_BYTES:
            return None
        digest.update(b"\0")
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(content)
    return {
        "head": raw_head.decode().strip(),
        "branch": (raw_branch.decode().strip()
                   if raw_branch is not None else None),
        "input_digest": input_digest,
        "worktree": "sha256:" + digest.hexdigest(),
    }


def evidence_id_for(obligation: str, executor_rev: str | None,
                    subject: dict[str, Any]) -> str:
    return "vev_" + digest_hex({
        "obligation": obligation,
        "executor_rev": executor_rev,
        "subject": subject,
    })


def _executor_binding(session, capability: str) -> dict | None:
    from .projections import _session_binding

    return _session_binding(session, capability)


def _verdict_from_envelope(stdout: str) -> int | None:
    try:
        payload = json.loads(stdout)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    verdict = payload.get("verdict", payload.get("result"))
    if isinstance(verdict, str):
        normalized = verdict.strip().lower()
        if normalized in ("pass", "passed", "ok", "success"):
            return VERDICT_PASS
        if normalized in ("fail", "failed", "failure"):
            return VERDICT_FAIL
        if normalized in ("unknown", "skip", "skipped"):
            return VERDICT_UNKNOWN
    if verdict == 1:
        return VERDICT_PASS
    if verdict == 0:
        return VERDICT_FAIL
    if verdict == 2:
        return VERDICT_UNKNOWN
    return None


def _porcelain_paths(checkout: Path) -> dict[str, str] | None:
    raw = _git(checkout, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    if raw is None:
        return None
    entries: dict[str, str] = {}
    for chunk in raw.decode("utf-8", "replace").split("\0"):
        if len(chunk) < 4:
            continue
        entries[chunk[3:]] = chunk[:2]
    # Cap the confinement surface; beyond it the run is unconfineable.
    if len(entries) > MAX_CONFINEMENT_FILES:
        return None
    return entries


def _file_marks(checkout: Path, paths: list[str]) -> dict[str, tuple]:
    marks: dict[str, tuple] = {}
    for rel in sorted(paths)[:MAX_CONFINEMENT_FILES]:
        try:
            stat = (checkout / rel).stat()
        except OSError:
            marks[rel] = ("missing",)
            continue
        marks[rel] = (stat.st_mtime_ns, stat.st_size)
    return marks


def _ignored(checkout: Path, paths: list[str]) -> set[str]:
    if not paths:
        return set()
    try:
        completed = subprocess.run(
            ["git", "-C", str(checkout), "check-ignore", "--stdin", "-z"],
            input="\0".join(paths).encode(), capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return set()
    if completed.returncode not in (0, 1):
        return set()
    return {name for name in completed.stdout.decode(
        "utf-8", "replace").split("\0") if name}


def _within_roots(path: str, roots: list[str]) -> bool:
    cleaned = path.strip("/")
    for root in roots:
        base = str(root).strip("/")
        if not base:
            continue
        if cleaned == base or cleaned.startswith(base + "/"):
            return True
    return False


def _confined(checkout: Path, before_paths: dict[str, str],
              before_marks: dict[str, tuple],
              ephemeral_roots: list[str]) -> tuple[bool, str]:
    """True when the run touched only ephemeral roots and ignored scratch."""
    after_paths = _porcelain_paths(checkout)
    if after_paths is None:
        return False, "post-run-state-unreadable"
    changed = [path for path in after_paths
               if path not in before_paths
               or after_paths[path] != before_paths.get(path)]
    # Content tripwire for files already dirty before the run.
    after_marks = _file_marks(checkout, list(before_paths))
    for rel, mark in before_marks.items():
        if after_marks.get(rel) != mark and rel not in changed:
            changed.append(rel)
    if not changed:
        return True, "clean"
    ignored = _ignored(checkout, changed)
    for path in sorted(changed):
        if path in ignored:
            continue
        if _within_roots(path, ephemeral_roots):
            continue
        return False, f"unconfined-write:{path}"
    return True, "ephemeral-only"


def run_executor(session, capability: str, checkout: Path,
                 subject: dict[str, Any]) -> tuple[int, str, str]:
    """Run one executor; return (verdict, method, detail).

    Evidence is recorded by the caller only for PASS/FAIL; UNKNOWN is
    never recorded, so transient outages retry instead of wedging.
    """
    binding = _executor_binding(session, capability)
    if binding is None:
        return VERDICT_UNKNOWN, "unbound", "executor-unbound"
    if binding.get("availability", {}).get("status") != "available":
        return VERDICT_UNKNOWN, "unavailable", "executor-unavailable"
    before_paths = _porcelain_paths(checkout)
    if before_paths is None:
        return VERDICT_UNKNOWN, "unobservable", "pre-run-state-unreadable"
    before_marks = _file_marks(checkout, list(before_paths))
    timeout = binding.get("timeout_seconds")
    timeout_seconds = timeout if isinstance(timeout, int) and timeout > 0 else 120
    try:
        result = session.invoke(capability, [], timeout_seconds=timeout_seconds)
    except Exception as error:
        return VERDICT_UNKNOWN, "invoke-failed", (
            f"invoke-failed:{type(error).__name__}")
    # "failed" is a completed run (nonzero exit): the provider spoke
    # and its verdict channel applies. Anything else — timeout,
    # transport errors, pending escalation — produced no verdict.
    if not isinstance(result, dict) or result.get("status") not in (
            "ok", "failed"):
        return VERDICT_UNKNOWN, "refused", (
            f"invoke-{result.get('status', 'unknown') if isinstance(result, dict) else 'unknown'}")
    ephemeral = (binding.get("provenance", {}) or {}).get(
        "ephemeral_roots", []) or []
    confined, confinement = _confined(
        checkout, before_paths, before_marks,
        [str(root) for root in ephemeral])
    if not confined:
        return VERDICT_UNKNOWN, "unconfined", confinement
    # The subject must be identical after the run, or the verdict
    # describes a state that no longer exists.
    after = observe_subject(checkout, subject.get("input_digest"))
    if after != subject:
        return VERDICT_UNKNOWN, "moved", "subject-moved-during-verification"
    verdict = _verdict_from_envelope(result.get("stdout", ""))
    if verdict is not None:
        return verdict, "envelope", VERDICT_NAMES[verdict]
    if int(result.get("returncode", 1)) == 0:
        return VERDICT_PASS, "exit-code", "pass"
    return VERDICT_FAIL, "exit-code", "fail"


def _retry_at(session, evidence_id: str) -> float | None:
    backoff = session.snapshot.get("verification_retry_after")
    if not isinstance(backoff, dict):
        return None
    try:
        return float(backoff.get(evidence_id, 0.0))
    except (TypeError, ValueError):
        return None


def _defer_retry(session, evidence_id: str) -> None:
    backoff = session.snapshot.setdefault("verification_retry_after", {})
    if isinstance(backoff, dict):
        backoff[evidence_id] = time.time() + RETRY_BACKOFF_SECONDS


def resolve_verdict(session, declaration: dict[str, Any],
                    checkout: Path, subject: dict[str, Any] | None,
                    *, allow_run: bool) -> tuple[int, str | None, str, list]:
    """Resolve PASS/FAIL/UNKNOWN for one declaration subject.

    Returns (verdict, evidence_id, detail, per-obligation evidence).
    With allow_run False this is lookup-only; with True, missing
    evidence is produced by running bound executors. No obligation
    declared, no subject bound, or no runnable executor all stay
    UNKNOWN — rendering success is never consulted here.
    """
    spec = declaration.get("verification")
    obligations = (spec.get("obligations") if isinstance(spec, dict)
                   else None) or []
    if not obligations:
        return VERDICT_UNKNOWN, None, "verification-undeclared", []
    if subject is None:
        return VERDICT_UNKNOWN, None, "subject-unobservable", []
    if session.store is None:
        return (VERDICT_UNKNOWN, None,
                "verification-store-unavailable", [])
    if not isinstance(obligations, list) or not all(
            isinstance(item, str) and item for item in obligations):
        return VERDICT_UNKNOWN, None, "verification-malformed", []
    seen: list[dict[str, Any]] = []
    for capability in obligations:
        binding = _executor_binding(session, capability)
        executor_rev = ((binding.get("provenance", {}) or {}).get(
            "executor_identity") if binding else None)
        evidence_id = evidence_id_for(capability, executor_rev, subject)
        try:
            record = store_module.read_evidence(session.store, evidence_id)
        except store_module.SharedStoreUnavailable:
            return (VERDICT_UNKNOWN, None,
                    "verification-store-unavailable", seen)
        if isinstance(record, dict) and record.get("verdict") in (0, 1, 2):
            seen.append({"obligation": capability,
                         "evidence_id": evidence_id,
                         "verdict": int(record["verdict"]),
                         "method": str(record.get("method", "recorded"))})
            continue
        if not allow_run:
            seen.append({"obligation": capability,
                         "evidence_id": evidence_id,
                         "verdict": VERDICT_UNKNOWN,
                         "method": "lookup-miss"})
            continue
        retry_at = _retry_at(session, evidence_id)
        if retry_at is not None and time.time() < retry_at:
            seen.append({"obligation": capability,
                         "evidence_id": evidence_id,
                         "verdict": VERDICT_UNKNOWN,
                         "method": "retry-deferred"})
            continue
        verdict, method, detail = run_executor(
            session, capability, checkout, subject)
        if verdict == VERDICT_UNKNOWN:
            _defer_retry(session, evidence_id)
            seen.append({"obligation": capability,
                         "evidence_id": evidence_id,
                         "verdict": verdict, "method": method,
                         "detail": detail})
            continue
        try:
            store_module.write_evidence(session.store, {
                "evidence_id": evidence_id,
                "obligation": capability,
                "executor_rev": executor_rev,
                "subject": subject,
                "verdict": verdict,
                "verdict_name": VERDICT_NAMES[verdict],
                "method": method,
                "observed_by": session.session_id,
                "observed_at": utcnow(),
            })
        except store_module.EvidenceConflict:
            _defer_retry(session, evidence_id)
            seen.append({"obligation": capability,
                         "evidence_id": evidence_id,
                         "verdict": VERDICT_UNKNOWN,
                         "method": "evidence-conflict"})
            continue
        except store_module.SharedStoreUnavailable:
            return (VERDICT_UNKNOWN, None,
                    "verification-store-unavailable", seen)
        seen.append({"obligation": capability, "evidence_id": evidence_id,
                     "verdict": verdict, "method": method})
        session._emit("verification.completed", "environment",
                      {"obligation": capability, "evidence_id": evidence_id,
                       "verdict": VERDICT_NAMES[verdict], "method": method})
    verdicts = [item["verdict"] for item in seen]
    if any(item == VERDICT_FAIL for item in verdicts):
        combined: str | None = "vev_multi_" + digest_hex(sorted(
            item["evidence_id"] for item in seen)) if len(seen) > 1 else (
                seen[0]["evidence_id"] if seen else None)
        failing = next(item["evidence_id"] for item in seen
                       if item["verdict"] == VERDICT_FAIL)
        return VERDICT_FAIL, failing, "verification-failed", seen
    if verdicts and all(item == VERDICT_PASS for item in verdicts):
        if len(seen) == 1:
            return VERDICT_PASS, seen[0]["evidence_id"], "pass", seen
        return VERDICT_PASS, "vev_multi_" + digest_hex(sorted(
            item["evidence_id"] for item in seen)), "pass", seen
    return VERDICT_UNKNOWN, None, "verification-unknown", seen


def resolve_declared(session, declaration: dict[str, Any],
                     default_checkout: Path, input_digest: str | None,
                     *, allow_run: bool) -> tuple[int, str | None, str, list]:
    """Resolve a declaration whose obligations may live in other repos.

    Each obligation is verified against the working directory declared
    by its executor binding, falling back to the projection target.
    Otherwise identical to resolve_verdict.
    """
    spec = declaration.get("verification")
    obligations = (spec.get("obligations") if isinstance(spec, dict)
                   else None) or []
    if not obligations:
        return VERDICT_UNKNOWN, None, "verification-undeclared", []
    if session.store is None:
        return (VERDICT_UNKNOWN, None,
                "verification-store-unavailable", [])
    if not isinstance(obligations, list) or not all(
            isinstance(item, str) and item for item in obligations):
        return VERDICT_UNKNOWN, None, "verification-malformed", []
    subjects: dict[str, dict[str, Any] | None] = {}

    def subject_for(checkout: Path) -> dict[str, Any] | None:
        key = str(checkout)
        if key not in subjects:
            subjects[key] = observe_subject(checkout, input_digest)
        return subjects[key]

    seen: list[dict[str, Any]] = []
    for capability in obligations:
        binding = _executor_binding(session, capability)
        executor_rev = ((binding.get("provenance", {}) or {}).get(
            "executor_identity") if binding else None)
        working = ((binding.get("provenance", {}) or {}).get(
            "working_directory") if binding else None)
        provider_root = binding.get("provider_root") if binding else None
        if working and provider_root:
            checkout = Path(provider_root) / working
        else:
            checkout = default_checkout
        subject = subject_for(checkout)
        if subject is None:
            seen.append({"obligation": capability,
                         "evidence_id": None,
                         "verdict": VERDICT_UNKNOWN,
                         "method": "subject-unobservable"})
            continue
        bounded = dict(declaration)
        bounded["verification"] = {"obligations": [capability]}
        verdict, evidence_id, _detail, items = resolve_verdict(
            session, bounded, checkout, subject, allow_run=allow_run)
        seen.extend(items or [{"obligation": capability,
                               "evidence_id": evidence_id,
                               "verdict": verdict,
                               "method": "lookup"}])
    verdicts = [item["verdict"] for item in seen]
    if any(item == VERDICT_FAIL for item in verdicts):
        failing = next(item["evidence_id"] for item in seen
                       if item["verdict"] == VERDICT_FAIL)
        return VERDICT_FAIL, failing, "verification-failed", seen
    if verdicts and all(item == VERDICT_PASS for item in verdicts):
        if len(seen) == 1:
            return VERDICT_PASS, seen[0]["evidence_id"], "pass", seen
        return VERDICT_PASS, "vev_multi_" + digest_hex(sorted(
            str(item["evidence_id"]) for item in seen)), "pass", seen
    detail = next((str(item.get("method", "lookup-miss")) for item in seen
                   if item["verdict"] == VERDICT_UNKNOWN),
                  "verification-unknown")
    return VERDICT_UNKNOWN, None, detail, seen
