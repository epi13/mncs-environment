"""Ambient diagnostic coherence: keep failure explanations current while agents work.

This module composes provider-owned debugger semantics; it owns no trace
semantics, no provenance, no replay, no minimization, and no source-map
semantics. On every environment entry it observes structured verification
FAILs, establishes a stable diagnostic identity per failed subject, asks
the native diagnostic policy which failures already have sufficient
witness evidence and which need a bounded capture, runs queued captures
through the bound debugger capability, records identity-bound witnesses,
and stays quiet when there is nothing to explain.

Policy lives in `mncs.debug.diagnostic_coherence` (current / capture /
extend / defer / unsupported / escalate). This file transports facts to
that policy and carries out admitted effects: provider invocation and
evidence recording. Diagnostics never changes a test verdict: a FAIL
stays FAIL however good the witness becomes, and a diagnostic UNKNOWN
never reinterprets the test result.

Capture executes provider code against the failing subject under the
`verify` effect: foreign ownership conflicts deny, dirt is the subject
rather than a bar, writes stay within session scratch, and the subject
must be identical before and after the run or the witness is rejected.
Nothing here seizes claims or touches sessions it does not own.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import evidence_store
from . import verification as verification_module
from .identity import digest_hex

SCHEMA = "mncs.environment.diagnostics/1"
COHERENCE_REQUEST_SCHEMA = "mncs.debug-diagnostic-coherence-request/1"
COHERENCE_RESULT_SCHEMA = "mncs.debug-diagnostic-coherence/1"
EVIDENCE_SCHEMA = "mncs.session-evidence/1"
MAX_HISTORY = 10
MAX_FAILURES = 32
MAX_IDENTITY_BYTES = 1024
MAX_CAPSULE_IDS = 8
COHERENCE_CAPABILITY = "mncs.debug-diagnostic-coherence/1"
DEBUG_CAPABILITY = "mncs.debugger/1"
TEST_CAPABILITY = "mncs.test-result/1"
DEFAULT_MAX_CAPTURES = 4
CAPTURE_TIMEOUT_SECONDS = 300
DEPTHS = ("minimal", "standard", "deep")

#: Transport capture bounds per depth. Which depth admits which operation
#: is native policy (`mncs.debug.diagnostic_coherence`); these numbers
#: bound the provider invocation the host performs for an admitted step.
DEPTH_BOUNDS = {
    "minimal": {"capture": "failure-only", "max_events": 64,
                "max_values": 128, "max_value_bytes": 4096},
    "standard": {"capture": "bounded", "max_events": 256,
                 "max_values": 512, "max_value_bytes": 8192},
    "deep": {"capture": "bounded", "max_events": 512,
             "max_values": 2048, "max_value_bytes": 65536},
}

#: Witness outcome classes that count as unusable captures. A witness the
#: debugger could not produce for harness reasons is not diagnostic
#: knowledge: it is retained in artifacts but never recorded as evidence,
#: and the epoch stays uncached so the next pass re-observes.
BROKEN_WITNESS_CLASSES = {"infrastructure_failure"}

#: Verification failure kinds projected onto the native diagnostic
#: vocabulary. The host normalizes literals; only the native policy
#: decides which classes the debugger may capture.
KIND_TO_CLASS = {
    "assertion": "test_failure",
    "setup": "test_failure",
    "runtime": "runtime_failure",
    "compile": "compile_failure",
    "timeout": "timeout",
    "unsupported": "unsupported",
    "infrastructure": "infrastructure_failure",
}
CLASSIFICATION_TO_CLASS = {
    "test_failure": "test_failure",
    "failed": "test_failure",
    "compile_failure": "compile_failure",
    "infrastructure_failure": "infrastructure_failure",
    "invalid_invocation": "invalid_request",
    "unsupported": "unsupported",
}
FAILURE_CLASS_TO_CLASS = {
    "none": "test_failure",
    "compile": "compile_failure",
    "infrastructure": "infrastructure_failure",
    "selection": "unsupported",
    "backend": "unsupported",
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _artifact_directory(session, *parts: str) -> Path:
    state_root = session.store.state_dir.resolve()
    path = (state_root / "sessions" / session.session_id
            / "diagnostic-artifacts" / Path(*parts)).resolve()
    if not path.is_relative_to(state_root):
        raise ValueError("diagnostic artifact path escapes the state root")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _workspace_root(session) -> Path | None:
    root = session.snapshot.get("workspace", {}).get("root")
    if not root:
        return None
    path = Path(root)
    return path if path.is_dir() else None


def _state_rows(session) -> dict[str, dict[str, Any]]:
    rows = session.snapshot.get("diagnostic_state")
    return dict(rows) if isinstance(rows, dict) else {}


def _verification_rows(session) -> dict[str, dict[str, Any]]:
    rows = session.snapshot.get("verification_state")
    return dict(rows) if isinstance(rows, dict) else {}


def _run_result(session, run_tag: str) -> dict[str, Any] | None:
    """Load a retained test-result document, or None when absent."""
    if not run_tag or "/" in run_tag or run_tag.startswith("."):
        return None
    state_root = session.store.state_dir.resolve()
    path = (state_root / "sessions" / session.session_id
            / "verification-artifacts" / "runs" / run_tag / "result.json")
    try:
        resolved = path.resolve()
        if not resolved.is_relative_to(state_root):
            return None
        document = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


def _file_sha(path: str) -> str:
    try:
        candidate = Path(path)
        if not candidate.is_file():
            return ""
        import hashlib
        return "sha256:" + hashlib.sha256(candidate.read_bytes()).hexdigest()
    except OSError:
        return ""


def _failure_class(entry: dict[str, Any], document: dict[str, Any]) -> str:
    """Project verification failure vocabulary onto diagnostic classes."""
    native = entry.get("native_result")
    if isinstance(native, dict):
        kind = str(native.get("failure_kind", "")).lower().replace("_", "")
        normalized = {"nofailure": "", "assertion": "assertion",
                      "setup": "setup", "runtime": "runtime",
                      "compile": "compile", "timeout": "timeout",
                      "unsupported": "unsupported",
                      "infrastructure": "infrastructure"}.get(kind, "")
        if normalized and normalized in KIND_TO_CLASS:
            return KIND_TO_CLASS[normalized]
    classification = str(document.get("classification", "")).lower()
    if classification in CLASSIFICATION_TO_CLASS:
        return CLASSIFICATION_TO_CLASS[classification]
    failure_class = str(document.get("failure_class", "")).lower()
    if failure_class in FAILURE_CLASS_TO_CLASS:
        return FAILURE_CLASS_TO_CLASS[failure_class]
    return "unsupported"


def _test_entry(document: dict[str, Any], test_id: str) -> dict[str, Any] | None:
    tests = document.get("tests")
    if not isinstance(tests, list):
        return None
    for item in tests:
        if not isinstance(item, dict):
            continue
        if item.get("id") == test_id or item.get("entry") == test_id:
            return item
    return None


def _subject_sha(document: dict[str, Any], entry: dict[str, Any]) -> str:
    """Bind the exact failing source content, or empty when unmeasurable."""
    source = entry.get("source")
    if not isinstance(source, str) or not source:
        return ""
    provenance = document.get("provenance")
    if isinstance(provenance, dict):
        attested = provenance.get("source")
        if (isinstance(attested, dict) and attested.get("path") == source
                and isinstance(attested.get("sha256"), str)
                and attested["sha256"]):
            return "sha256:" + attested["sha256"]
    return _file_sha(source)


def observe_failures(session, rows: dict[str, dict[str, Any]],
                     depth: str,
                     obligations: dict[str, dict[str, Any]] | None = None
                     ) -> tuple[list[dict[str, Any]], int]:
    """Build stable failure views for every recorded verification FAIL.

    Returns (failures, observed_count). Only FAIL verdicts are failures;
    UNKNOWN rows are not debuggable product failures and stay out.
    """
    toolchain = verification_module.toolchain_identity(session)
    index = obligations or {}
    failures: list[dict[str, Any]] = []
    observed = 0
    for obligation in sorted(rows):
        row = rows[obligation]
        if not isinstance(row, dict) or row.get("verdict") != "FAIL":
            continue
        if obligations is not None and obligation not in index:
            # The obligation is no longer declared (removed inventory
            # or unreadable manifest). Its lingering row is history,
            # not a live failure; the inventory signal itself owns it.
            continue
        repository = str((index.get(obligation) or {}).get("repository", ""))
        document = _run_result(session, str(row.get("run_tag", "")))
        for failed in row.get("failed_test_ids") or []:
            if not isinstance(failed, dict):
                continue
            test_id = str(failed.get("id", "?"))
            observed += 1
            entry = _test_entry(document, test_id) if document else None
            if entry is None:
                failures.append({
                    "failure_key": f"{obligation}::{test_id}"[:MAX_IDENTITY_BYTES],
                    "failure_class": "test_failure",
                    "subject_sha": "", "request_digest": "",
                    "toolchain_identity": toolchain,
                    "has_source": False, "has_request": False,
                    "has_source_binding": False,
                    "provider_available": False,
                    "requested_depth": depth,
                    "evidence_present": False,
                    "evidence": _empty_evidence(),
                    "context": {"obligation": obligation,
                                "repository": repository,
                                "test_id": test_id,
                                "result_missing": True},
                })
                continue
            sha = _subject_sha(document or {}, entry)
            request = entry.get("request")
            request_digest = (digest_hex({"request": request})
                              if isinstance(request, dict) and request else "")
            span = entry.get("source_span")
            failures.append({
                "failure_key": f"{obligation}::{test_id}"[:MAX_IDENTITY_BYTES],
                "failure_class": _failure_class(entry, document or {}),
                "subject_sha": sha,
                "request_digest": request_digest,
                "toolchain_identity": toolchain,
                "has_source": bool(sha),
                "has_request": bool(request_digest),
                "has_source_binding": isinstance(span, dict) and bool(span),
                "provider_available": False,
                "requested_depth": depth,
                "evidence_present": False,
                "evidence": _empty_evidence(),
                "context": {"obligation": obligation,
                            "repository": repository,
                            "test_id": test_id,
                            "source": entry.get("source", ""),
                            "source_span": span if isinstance(span, dict) else {},
                            "run_tag": str(row.get("run_tag", ""))},
            })
    failures.sort(key=lambda item: str(item["failure_key"]))
    return failures[:MAX_FAILURES], observed


def _empty_evidence() -> dict[str, Any]:
    return {"failure_key": "", "subject_sha": "", "request_digest": "",
            "toolchain_identity": "", "depth": "minimal",
            "witness_ref": "", "capture_complete": False}


def _coherence_request(session, failures: list[dict[str, Any]],
                       rows: dict[str, dict[str, Any]],
                       max_captures: int, provider_available: bool
                       ) -> dict[str, Any]:
    items = []
    for item in failures:
        recorded = (rows.get(str(item["failure_key"])) or {}).get("evidence")
        evidence = _empty_evidence()
        if isinstance(recorded, dict):
            for key in evidence:
                if key in recorded:
                    evidence[key] = recorded[key]
        request_item = {key: item[key] for key in
                        ("failure_key", "failure_class", "subject_sha",
                         "request_digest", "toolchain_identity", "has_source",
                         "has_request", "has_source_binding",
                         "requested_depth")}
        request_item["provider_available"] = provider_available
        request_item["evidence_present"] = isinstance(recorded, dict)
        request_item["evidence"] = evidence
        items.append(request_item)
    return {"schema_version": COHERENCE_REQUEST_SCHEMA,
            "max_captures": max_captures, "failures": items}


def request_coherence(session, request: dict[str, Any]) -> dict[str, Any] | None:
    """Invoke the native diagnostic policy through its bound capability."""
    try:
        directory = _artifact_directory(session, "coherence")
    except (OSError, ValueError):
        return None
    tag = digest_hex({"at": utcnow(), "request": request})[:12]
    workdir = directory / tag
    # run-app defaults its compiled-artifact cache beside the provider
    # descriptor; pin it into session state so ambient diagnostics never
    # write into a provider checkout.
    cache = _artifact_directory(session, "cache")
    try:
        workdir.mkdir(parents=True, exist_ok=True, mode=0o700)
        (workdir / "request.json").write_text(
            json.dumps(request, sort_keys=True), encoding="utf-8")
        result = session.invoke(
            COHERENCE_CAPABILITY, ["request.json", "result.json"],
            cwd=str(workdir), timeout_seconds=180,
            env={"MNCS_NATIVE_APPLICATION_CACHE_DIR": str(cache)})
    except Exception:
        return None
    if not isinstance(result, dict) or result.get("status") != "ok":
        return None
    try:
        verdicts = json.loads((workdir / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(verdicts, dict):
        return None
    if verdicts.get("schema_version") != COHERENCE_RESULT_SCHEMA:
        return None
    return verdicts


def _obligation_index(workspace_root: Path) -> dict[str, dict[str, Any]]:
    """Map obligation identity to its live declaration and checkout."""
    try:
        obligations, _invalid = verification_module.discover_obligations(workspace_root)
    except Exception:
        return {}
    index = {}
    for item in obligations:
        declaration = item.get("declaration") or {}
        identity = declaration.get("identity")
        if isinstance(identity, str) and identity:
            index[identity] = item
    return index


def _capture_libraries(session, obligation: dict[str, Any]) -> list[str]:
    """Library roots for re-execution: declared roots plus the test
    provider's adapter-contributed roots, all provider-owned."""
    roots: list[str] = []
    checkout = Path(str(obligation.get("checkout", "")))
    executor = (obligation.get("declaration") or {}).get("executor") or {}
    for library in executor.get("library_paths") or []:
        if not isinstance(library, str) or not library:
            continue
        candidate = (checkout / library).resolve()
        if candidate.is_dir():
            roots.append(str(candidate))
    binding = verification_module.session_binding(session, TEST_CAPABILITY)
    if binding is not None:
        for root in (binding.get("provenance") or {}).get("adapter_library_paths") or []:
            if isinstance(root, str) and Path(root).is_dir() and root not in roots:
                roots.append(root)
    return roots


def _subject_authorized(session, repository: str, checkout: str) -> tuple[bool, str]:
    """Verify-effect check against the subject checkout.

    Foreign ownership conflicts deny exactly like mutation; the subject's
    own dirt and branch are the diagnostic subject, never a bar.
    """
    if not repository or not checkout:
        return False, "subject-unresolvable"
    try:
        verdict = session.check(action="verify", target=repository,
                                scope={"kind": "worktree", "checkout": checkout})
    except Exception as error:
        return False, f"authority-error:{type(error).__name__}"
    if verdict.get("verdict") != "allow":
        return False, str(verdict.get("reason", verdict.get("verdict", "denied")))
    return True, ""


def _witness_usable(witness: dict[str, Any]) -> tuple[bool, str]:
    """Decide whether a captured witness is diagnostic knowledge.

    The witness must parse with stable identities and a non-broken
    outcome. Harness breakage is retained in artifacts but never
    recorded as evidence.
    """
    if not isinstance(witness.get("witness_id"), str) or not witness["witness_id"]:
        return False, "witness-unidentified"
    if not isinstance(witness.get("execution_identity"), str) or not witness["execution_identity"]:
        return False, "witness-unidentified"
    outcome = witness.get("outcome")
    if not isinstance(outcome, dict):
        return False, "witness-outcome-missing"
    failure_class = str(outcome.get("failure_class", ""))
    if not failure_class:
        return False, "witness-outcome-missing"
    if failure_class in BROKEN_WITNESS_CLASSES:
        return False, f"witness-broken:{failure_class}"
    return True, ""


def capture_witness(session, failure: dict[str, Any], obligation: dict[str, Any],
                    depth: str, capture_tag: str
                    ) -> tuple[dict[str, Any] | None, str | None]:
    """Run one admitted capture; return (witness, applicability-problem)."""
    context = failure.get("context") or {}
    checkout = Path(str(obligation.get("checkout", "")))
    run_tag = str(context.get("run_tag", ""))
    if not run_tag or not checkout.is_dir():
        return None, "subject-unresolvable"
    state_root = session.store.state_dir.resolve()
    result_path = (state_root / "sessions" / session.session_id
                   / "verification-artifacts" / "runs" / run_tag / "result.json")
    try:
        resolved_result = result_path.resolve()
        if not resolved_result.is_relative_to(state_root) or not resolved_result.is_file():
            return None, "result-unreadable"
    except OSError:
        return None, "result-unreadable"
    repository = str(context.get("repository", "") or obligation.get("repository", ""))
    allowed, reason = _subject_authorized(session, repository, str(checkout.resolve()))
    if not allowed:
        return None, f"authority-denied:{reason}"
    bounds = DEPTH_BOUNDS.get(depth, DEPTH_BOUNDS["minimal"])
    before_head = verification_module.repo_revision(checkout)
    before_dirty = verification_module.dirty_content_digest(checkout)
    try:
        directory = _artifact_directory(session, "captures", capture_tag)
        cache = _artifact_directory(session, "cache")
    except (OSError, ValueError):
        return None, "artifact-unwritable"
    argv = ["import-test", str(resolved_result),
           "--test-id", str(context.get("test_id", "")),
           "--capture", str(bounds["capture"]),
           "--max-events", str(bounds["max_events"]),
           "--max-values", str(bounds["max_values"]),
           "--max-value-bytes", str(bounds["max_value_bytes"]),
           "--cwd", str(directory),
           "--output", "witness.json"]
    for root in _capture_libraries(session, obligation):
        argv.extend(["--library", root])
    try:
        result = session.invoke(
            DEBUG_CAPABILITY, argv, cwd=str(directory),
            timeout_seconds=CAPTURE_TIMEOUT_SECONDS,
            env={"MNCS_NATIVE_APPLICATION_CACHE_DIR": str(cache)})
    except Exception as error:
        return None, f"transport-error:{type(error).__name__}"
    status = result.get("status") if isinstance(result, dict) else None
    if status in ("timeout", "transport-error", "pending-escalation"):
        return None, f"transport-failed:{status}"
    if status not in ("ok", "failed"):
        return None, f"transport-failed:{status}"
    try:
        witness = json.loads((directory / "witness.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        detail = (result.get("stderr") or result.get("stdout") or "")[:500]
        return None, f"witness-unreadable:{detail or result.get('status')}"
    if not isinstance(witness, dict):
        return None, "witness-invalid"
    usable, problem = _witness_usable(witness)
    if not usable:
        return None, problem
    fresh_head = verification_module.repo_revision(checkout)
    fresh_dirty = verification_module.dirty_content_digest(checkout)
    if (fresh_head != before_head or fresh_dirty != before_dirty):
        return None, "state-changed-during-capture"
    return witness, None


def _epoch_inputs(session, failures: list[dict[str, Any]],
                  depth: str) -> dict[str, Any]:
    bindings: dict[str, str] = {}
    for capability in (COHERENCE_CAPABILITY, DEBUG_CAPABILITY):
        binding = verification_module.session_binding(session, capability)
        if binding is None:
            bindings[capability] = "unbound"
        else:
            bindings[capability] = str(binding.get("availability", {}).get("status"))
    return {
        "failures": {str(item["failure_key"]): digest_hex(
            {"class": item["failure_class"], "subject": item["subject_sha"],
             "request": item["request_digest"],
             "toolchain": item["toolchain_identity"],
             "source": item["has_source"], "request_present": item["has_request"]})
            for item in failures},
        "bindings": bindings,
        "toolchain": verification_module.toolchain_identity(session),
        "lifecycle": session.snapshot.get("lifecycle", "active"),
        "depth": depth,
    }


def ambient_pass(session, *, mode: str = "ambient",
                 max_captures: int = DEFAULT_MAX_CAPTURES,
                 depth: str = "minimal",
                 only: str | None = None) -> dict[str, Any]:
    """Explain recorded verification FAILs; capture only stale ones."""
    started = utcnow()
    clock_started = time.monotonic()
    if depth not in DEPTHS:
        depth = "minimal"
    rows = _state_rows(session)
    verification_rows = _verification_rows(session)
    workspace_root = _workspace_root(session)
    if workspace_root is None:
        return _finish(session, started, clock_started, [], rows, 0, mode,
                       depth, {"reason": "workspace-unavailable"},
                       "no-workspace", None)
    obligations = _obligation_index(workspace_root)
    failures, observed = observe_failures(session, verification_rows, depth,
                                          obligations)
    if only is not None:
        failures = [item for item in failures
                    if str(item["failure_key"]) == only]
        if not failures:
            return _finish(session, started, clock_started, [], rows,
                           observed, mode, depth,
                           {"reason": f"unknown-failure:{only}"},
                           f"unknown:{only}", None)
    epoch_inputs = _epoch_inputs(session, failures, depth)
    epoch = digest_hex(epoch_inputs)
    stored = session.snapshot.get("diagnostic_epoch") or {}
    if (stored.get("epoch") == epoch and mode == "ambient"
            and evidence_store.cacheable(
                session, "diagnostics", stored.get("evidence_ref"))):
        summary = dict(stored.get("summary") or {})
        summary["epoch_reused"] = True
        summary["elapsed_seconds"] = round(time.monotonic() - clock_started, 3)
        return {"summary": summary, "reused": True,
                "evidence": stored.get("evidence_ref")}
    if not failures:
        # Reactive fast path: nothing fails, so there is nothing to
        # decide. The empty epoch still caches, so healthy re-entry
        # costs one digest, not one native invocation.
        empty = {"summary": {"failures": 0, "current": 0, "queued": 0,
                             "deferred": 0, "unsupported": 0, "escalated": 0},
                 "diagnostics": [], "capture_queue": []}
        return _finish(session, started, clock_started, [], {}, observed,
                       mode, depth, None, epoch, empty)
    if not verification_module.binding_available(session, COHERENCE_CAPABILITY):
        return _finish(session, started, clock_started, [], rows, observed,
                       mode, depth, {"reason": "coherence-unavailable"},
                       f"uncached:{mode}:{epoch}", None)
    provider_available = verification_module.binding_available(
        session, DEBUG_CAPABILITY)
    request = _coherence_request(session, failures, rows, max_captures,
                                 provider_available)
    coherence = request_coherence(session, request)
    if coherence is None:
        return _finish(session, started, clock_started, [], rows, observed,
                       mode, depth, {"reason": "coherence-failed"},
                       f"uncached:{mode}:{epoch}", None)
    by_key = {str(item["failure_key"]): item for item in failures}
    verdicts = {item["failure_key"]: item
                for item in coherence.get("diagnostics", [])
                if isinstance(item, dict)
                and isinstance(item.get("failure_key"), str)}
    queue = [key for key in coherence.get("capture_queue", [])
             if isinstance(key, str) and key in by_key]
    results: list[dict[str, Any]] = []
    next_rows: dict[str, dict[str, Any]] = {}
    failure: dict | None = None
    for key, item in by_key.items():
        verdict = verdicts.get(key) or {}
        context = item.get("context") or {}
        record: dict[str, Any] = {
            "failure": key,
            "obligation": context.get("obligation", ""),
            "repository": context.get("repository", ""),
            "test_id": context.get("test_id", ""),
            "failure_class": item["failure_class"],
            "status": verdict.get("status", "unknown"),
            "reason": verdict.get("reason", "unknown"),
            "operation": verdict.get("operation", "none"),
            "deferred": bool(verdict.get("deferred", False)),
        }
        if key in queue and verdict.get("status") in (
                "capture_required", "extend_required"):
            obligation = obligations.get(str(context.get("obligation", "")))
            if obligation is None or context.get("result_missing"):
                record["outcome"] = "not-captured"
                record["detail"] = "subject-unresolvable"
            else:
                # The monotonic nonce keeps tags unique even when two
                # passes start within the same second; each witness
                # keeps its own directory.
                capture_tag = (f"{digest_hex({'key': key})[:8]}-"
                               f"{digest_hex({'at': started, 'nonce': time.monotonic_ns()})[:8]}")
                witness, problem = capture_witness(
                    session, item, obligation, depth, capture_tag)
                record["captured"] = witness is not None
                record["capture_tag"] = capture_tag
                if witness is None:
                    record["outcome"] = "unknown"
                    record["detail"] = problem
                    failure = {"reason": f"capture-failed:{key}",
                               "detail": problem}
                    session._emit("diagnostic.failed", "environment",
                                  {"failure": key, "detail": problem})
                else:
                    evidence_id = f"dw-{digest_hex({'key': key, 'at': started})[:12]}"
                    evidence = {
                        "failure_key": key,
                        "subject_sha": item["subject_sha"],
                        "request_digest": item["request_digest"],
                        "toolchain_identity": item["toolchain_identity"],
                        "depth": depth,
                        "witness_ref": str(witness.get("witness_id", "")),
                        "capture_complete": True,
                    }
                    next_rows[key] = {
                        "evidence": evidence,
                        "evidence_id": evidence_id,
                        "depth": depth,
                        "witness_class": str((witness.get("outcome") or {}).get(
                            "failure_class", "")),
                        "source": str(context.get("source", "")),
                        "source_span": dict(context.get("source_span") or {}),
                        "capture_tag": capture_tag,
                        "captured_at": utcnow(),
                    }
                    record["outcome"] = "captured"
                    record["evidence_id"] = evidence_id
                    record["witness_ref"] = evidence["witness_ref"]
                    record["witness_class"] = next_rows[key]["witness_class"]
                    record["source"] = next_rows[key]["source"]
                    record["source_span"] = next_rows[key]["source_span"]
                    session._emit("diagnostic.captured", "environment",
                                  {"failure": key, "evidence_id": evidence_id,
                                   "depth": depth,
                                   "witness": evidence["witness_ref"]})
        elif verdict.get("status") in ("capture_required", "extend_required"):
            record["outcome"] = "deferred"
            session._emit("diagnostic.deferred", "environment",
                          {"failure": key, "reason": "budget_deferred"})
        elif verdict.get("status") == "current":
            record["outcome"] = "current"
            record["evidence_id"] = (rows.get(key) or {}).get("evidence_id", "")
            record["witness_ref"] = verdict.get("witness_ref", "")
            kept = rows.get(key)
            if isinstance(kept, dict):
                next_rows[key] = kept
        elif verdict.get("status") == "unsupported":
            record["outcome"] = "unsupported"
        elif verdict.get("status") == "escalate":
            record["outcome"] = "escalated"
            session._emit("diagnostic.deferred", "environment",
                          {"failure": key,
                           "reason": verdict.get("reason", "escalate")})
        else:
            record["outcome"] = "unknown"
        results.append(record)
    if failure is None:
        if only is not None:
            # A filtered explicit pass merges; only a full observation
            # may prune keys that no longer fail.
            merged = dict(rows)
            merged.update(next_rows)
            rows = merged
        else:
            rows = next_rows
    return _finish(session, started, clock_started, results, rows,
                   observed, mode, depth, failure, epoch, coherence)


def _finish(session, started: str, clock_started: float,
            results: list[dict], rows: dict, observed: int,
            mode: str, depth: str, failure: dict | None,
            epoch: str, coherence: dict | None) -> dict[str, Any]:
    native = (coherence or {}).get("summary") or {}
    summary = {"failures": len(results),
               "observed": observed,
               "current": 0, "captured": 0, "deferred": 0,
               "unsupported": int(native.get("unsupported", 0)),
               "escalated": int(native.get("escalated", 0)),
               "blockers": 0, "depth": depth,
               "epoch_reused": False}
    capsule: list[str] = []
    for record in results:
        outcome = record.get("outcome")
        if outcome == "current":
            summary["current"] += 1
        if record.get("captured"):
            summary["captured"] += 1
        if outcome == "deferred":
            summary["deferred"] += 1
        if outcome in ("escalated", "captured"):
            capsule.append(str(record.get("failure")))
    summary["capsule_ids"] = capsule[:MAX_CAPSULE_IDS]
    summary["blockers"] = summary["escalated"] + (
        1 if failure is not None else 0)
    evidence = {"schema_version": SCHEMA, "mode": mode, "depth": depth,
                "started_at": started, "finished_at": utcnow(),
                "summary": summary, "results": results,
                "failure": failure,
                "coherence_summary": dict(native)}
    evidence_ref = _write_evidence(session, evidence, mode)
    summary["elapsed_seconds"] = round(time.monotonic() - clock_started, 3)
    if failure is None:
        session.snapshot["diagnostic_state"] = rows
    history = list(session.snapshot.get("diagnostic_history") or [])
    history.append({"at": utcnow(), "mode": mode, "depth": depth,
                    "summary": dict(summary), "evidence_ref": evidence_ref})
    session.snapshot["diagnostic_history"] = history[-MAX_HISTORY:]
    # Transport/capture failures never cache their epoch: the next pass
    # must re-observe, not replay a degraded world. Recorded witnesses
    # cache normally because bound evidence is exact.
    if mode == "ambient" and failure is None:
        stored_epoch = epoch
    else:
        stored_epoch = f"uncached:{mode}:{epoch}"
    session.snapshot["diagnostic_epoch"] = {
        "epoch": stored_epoch,
        "summary": dict(summary), "evidence_ref": evidence_ref,
        "at": utcnow()}
    _append_session_evidence(session, summary, results, mode, depth)
    try:
        session._save()
    except (OSError, ValueError):
        summary["persisted"] = False
    else:
        summary["persisted"] = True
    return {"summary": summary, "reused": False,
            "evidence": evidence_ref}


def _write_evidence(session, evidence: dict[str, Any],
                    mode: str) -> str:
    stored = evidence_store.publish(session, "diagnostics", evidence)
    if stored is not None:
        return stored
    directory = _artifact_directory(session, "evidence")
    ref = f"diagnostic-{mode}-{digest_hex(evidence['finished_at'])[:12]}.json"
    path = directory / ref
    try:
        path.write_text(json.dumps(evidence, indent=2, sort_keys=True),
                        encoding="utf-8")
    except OSError:
        return "diagnostic-evidence-unwritable"
    return f"sessions/{session.session_id}/diagnostic-artifacts/evidence/{ref}"


def _append_session_evidence(session, summary: dict[str, Any],
                             results: list[dict], mode: str,
                             depth: str) -> None:
    entry = {
        "schema_version": EVIDENCE_SCHEMA,
        "session": session.session_id,
        "consumer": session.snapshot.get("consumer_id"),
        "mode": mode,
        "depth": depth,
        "summary": {key: summary.get(key) for key in
                    ("failures", "current", "captured", "deferred",
                     "unsupported", "escalated", "blockers")},
        "diagnostics": [
            {"failure": record.get("failure"),
             "obligation": record.get("obligation"),
             "repository": record.get("repository"),
             "outcome": record.get("outcome"),
             "status": record.get("status"),
             "reason": record.get("reason"),
             "operation": record.get("operation"),
             "evidence_id": record.get("evidence_id"),
             "witness_ref": record.get("witness_ref"),
             "witness_class": record.get("witness_class"),
             "source": record.get("source")}
            for record in results],
        "recorded_at": utcnow(),
    }
    try:
        directory = _artifact_directory(session)
        path = directory / "session-evidence.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
    except OSError:
        pass


def terse(session) -> dict[str, Any]:
    """Compact diagnostic status for normal agent context."""
    stored = session.snapshot.get("diagnostic_epoch") or {}
    summary = dict(stored.get("summary") or {})
    return {"diagnostic": {
        "failures": summary.get("failures", 0),
        "current": summary.get("current", 0),
        "captured": summary.get("captured", 0),
        "escalated": summary.get("escalated", 0),
        "blockers": summary.get("blockers", 0),
        "capsule_ids": summary.get("capsule_ids", []),
        "evidence": stored.get("evidence_ref")}}


def read_evidence(session) -> dict[str, Any]:
    """Full diagnostic evidence for explicit inspection."""
    stored = session.snapshot.get("diagnostic_epoch") or {}
    ref = stored.get("evidence_ref", "")
    if evidence_store.is_store_reference(ref):
        return {"evidence": evidence_store.read(session, "diagnostics", ref),
                "history": session.snapshot.get("diagnostic_history") or []}
    if not ref or not ref.startswith("sessions/"):
        return {"evidence": None, "history": session.snapshot.get(
            "diagnostic_history") or []}
    path = (session.store.state_dir / ref).resolve()
    try:
        if not path.is_relative_to(session.store.state_dir.resolve()):
            raise OSError("evidence escapes state root")
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        evidence = None
    return {"evidence": evidence, "history": session.snapshot.get(
        "diagnostic_history") or []}
