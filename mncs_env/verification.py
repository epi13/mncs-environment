"""Ambient verification coherence: keep test evidence current while agents work.

This module composes provider-owned testing semantics; it owns no test
discovery, no assertion evaluation, and no result classification. On
every environment entry it observes declared verification obligations,
measures the current world (repository state, declaration digests,
toolchain identity, recorded evidence), asks the native coherence
policy which obligations are current and which need execution, runs
queued native suites through the bound test capability, records
identity-bound evidence, and stays quiet when the world is current.

Policy lives in `mncs.test.verification_coherence` (current / queue /
defer / escalate). This file transports facts to that policy and
carries out admitted effects: filesystem reads, provider invocation,
and evidence recording. Verification is read-only: it never updates
snapshots, rewrites goldens, mutates sources, stages, or commits.
Execution artifacts land under the session state directory, never in
a provider checkout. Nothing here seizes claims or touches sessions
it does not own.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import capabilities as capabilities_module
from . import workspace as workspace_module
from .identity import digest_hex

SCHEMA = "mncs.environment.verification/1"
COHERENCE_REQUEST_SCHEMA = "mncs.test-verification-coherence-request/1"
COHERENCE_RESULT_SCHEMA = "mncs.test-verification-coherence/1"
EVIDENCE_SCHEMA = "mncs.session-evidence/1"
MAX_HISTORY = 10
MAX_OBLIGATIONS = 32
MAX_INVENTORY_IDS = 64
MAX_IDENTITY_BYTES = 1024
MAX_PATTERNS = 8
MAX_DEP_FILES = 512
MAX_DEP_BYTES = 8 * 1024 * 1024
MAX_DIRTY_FILES = 256
MAX_DIRTY_BYTES = 1024 * 1024
MAX_FAILED_IDS = 8
COHERENCE_CAPABILITY = "mncs.test-verification-coherence/1"
TEST_CAPABILITY = "mncs.test-result/1"
DEFAULT_MAX_EXECUTIONS = 8
EXECUTION_TIMEOUT_SECONDS = 300

LIFECYCLES = ("permanent", "transitional", "scheduled", "reference_only", "retired")
EXECUTOR_KINDS = ("native_first_class_test", "compile_experiment", "diagnostic_test",
                  "backend_check", "differential_oracle", "migration_parity",
                  "external_integration")
VERDICTS = ("PASS", "FAIL", "UNKNOWN")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def bytes_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _artifact_directory(session, *parts: str) -> Path:
    state_root = session.store.state_dir.resolve()
    path = (state_root / "sessions" / session.session_id
            / "verification-artifacts" / Path(*parts)).resolve()
    if not path.is_relative_to(state_root):
        raise ValueError("verification artifact path escapes the state root")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _workspace_root(session) -> Path | None:
    root = session.snapshot.get("workspace", {}).get("root")
    if not root:
        return None
    path = Path(root)
    return path if path.is_dir() else None


def _inventory_name(repo: Path) -> str | None:
    """Resolve the obligation inventory path from the repo manifest."""
    try:
        manifest = json.loads((repo / ".mncs" / "project.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict):
        return None
    verification = manifest.get("verification")
    name = verification.get("obligation_inventory") if isinstance(verification, dict) else None
    if not isinstance(name, str) or not name:
        return None
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    return name


def _validate_obligation(entry: Any) -> str | None:
    if not isinstance(entry, dict):
        return "obligation-not-an-object"
    identity = entry.get("identity")
    if not isinstance(identity, str) or not identity:
        return "identity-missing"
    if len(identity.encode("utf-8")) > MAX_IDENTITY_BYTES:
        return "identity-too-long"
    if entry.get("lifecycle") not in LIFECYCLES:
        return "lifecycle-unknown"
    executor = entry.get("executor")
    if not isinstance(executor, dict):
        return "executor-missing"
    if executor.get("kind") not in EXECUTOR_KINDS:
        return "executor-kind-unknown"
    patterns = executor.get("declaration_identities", [])
    if patterns is None:
        patterns = []
    if (not isinstance(patterns, list) or len(patterns) > MAX_PATTERNS
            or any(not isinstance(pattern, str) or not pattern
                   or len(pattern.encode("utf-8")) > MAX_IDENTITY_BYTES
                   for pattern in patterns)):
        return "patterns-invalid"
    deps = entry.get("invalidation_dependencies", [])
    if (not isinstance(deps, list)
            or any(not isinstance(dep, str) or not dep for dep in deps)):
        return "dependencies-invalid"
    sources = executor.get("source_paths", [])
    if sources is None:
        sources = []
    if (not isinstance(sources, list)
            or any(not isinstance(source, str) or not source for source in sources)):
        return "source-paths-invalid"
    return None


def discover_obligations(workspace_root: Path) -> tuple[list[dict], list[dict]]:
    """Collect verification obligations from workspace manifests.

    Only direct-child repositories carrying `.mncs/project.json` with a
    `verification.obligation_inventory` pointer are considered; anything
    else costs small JSON reads. Invalid obligations are reported,
    never executed.
    """
    obligations: list[dict] = []
    invalid: list[dict] = []
    try:
        children = sorted(path for path in workspace_root.iterdir()
                          if path.is_dir() and not path.name.startswith("."))
    except OSError:
        return [], [{"repository": "", "reason": "workspace-unreadable"}]
    for child in children:
        name = _inventory_name(child)
        if name is None:
            continue
        try:
            payload = json.loads((child / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            invalid.append({"repository": child.name, "reason": "inventory-unreadable"})
            continue
        entries = payload.get("obligations") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            invalid.append({"repository": child.name, "reason": "obligations-not-a-list"})
            continue
        for entry in entries:
            problem = _validate_obligation(entry)
            if problem is not None:
                identity = entry.get("identity") if isinstance(entry, dict) else None
                invalid.append({"repository": child.name, "reason": problem,
                                "identity": identity})
                continue
            obligations.append({"repository": child.name, "checkout": str(child),
                                "declaration": entry})
    obligations.sort(key=lambda item: str(item["declaration"].get("identity")))
    if len(obligations) > MAX_OBLIGATIONS:
        for item in obligations[MAX_OBLIGATIONS:]:
            invalid.append({"repository": item["repository"], "reason": "obligation-bound-exceeded",
                            "identity": item["declaration"].get("identity")})
        obligations = obligations[:MAX_OBLIGATIONS]
    return obligations, invalid


def _digest_files(checkout: Path, relpaths: list[str]) -> tuple[str | None, str | None]:
    """Digest dependency contents; unmeasurable entries fail closed."""
    digester = hashlib.sha256()
    count = 0
    total = 0
    for relpath in sorted(relpaths):
        relative = Path(relpath)
        if relative.is_absolute() or ".." in relative.parts:
            return None, f"dependency-escapes:{relpath}"
        target = checkout / relative
        if target.is_dir():
            try:
                members = sorted(path for path in target.rglob("*") if path.is_file())
            except OSError:
                return None, f"dependency-unreadable:{relpath}"
            nested = [str(path.relative_to(checkout)) for path in members]
            digest, problem = _digest_files(checkout, nested)
            if digest is None:
                return None, problem
            digester.update(f"{relpath}={digest}".encode())
            continue
        if not target.is_file():
            return None, f"dependency-missing:{relpath}"
        try:
            data = target.read_bytes()
        except OSError:
            return None, f"dependency-unreadable:{relpath}"
        count += 1
        total += len(data)
        if count > MAX_DEP_FILES or total > MAX_DEP_BYTES:
            return None, "dependency-bound-exceeded"
        digester.update(relpath.encode())
        digester.update(b"\0")
        digester.update(data)
        digester.update(b"\0")
    return "sha256:" + digester.hexdigest(), None


def _porcelain_name(line: str) -> str | None:
    if len(line) <= 3 or line.startswith("!!"):
        return None
    name = line[3:].strip()
    if " -> " in name:
        name = name.split(" -> ", 1)[1]
    return name.strip().strip('"') or None


def _dirty_content_digest(checkout: Path) -> str:
    """Digest current dirty/untracked content (bounded, empty when clean)."""
    try:
        state = workspace_module.inspect_repo(checkout)
    except (OSError, ValueError):
        return "repo-unreadable"
    if state is None:
        return "repo-unreadable"
    if not getattr(state, "dirty", False):
        return "clean"
    names = sorted({name for line in (getattr(state, "dirty_files", None) or [])
                    if (name := _porcelain_name(str(line))) is not None
                    and name != ".worktrees"
                    and not name.startswith(".worktrees/")})[:MAX_DIRTY_FILES]
    digester = hashlib.sha256()
    total = 0
    for name in names:
        try:
            data = (checkout / name).read_bytes() if (checkout / name).is_file() else b"<absent>"
        except OSError:
            data = b"<unreadable>"
        total += len(data)
        if total > MAX_DIRTY_BYTES:
            digester.update(b"<dirty-overflow>")
            break
        digester.update(name.encode())
        digester.update(b"\0")
        digester.update(data)
        digester.update(b"\0")
    return "sha256:" + digester.hexdigest()


def _repo_revision(checkout: Path) -> str:
    facts = workspace_module.quick_repo_facts(checkout)
    if facts is None:
        return "repo-unreadable"
    return str(facts.get("head") or "head-unknown")


def _toolchain_identity(session) -> str:
    toolchain = session.snapshot.get("toolchain")
    if not isinstance(toolchain, dict):
        return "toolchain-unknown"
    return digest_hex({key: toolchain.get(key) for key in
                       ("binary", "revision", "checkout", "repository")})


def _measured_current(session, obligation: dict) -> tuple[dict | None, str | None]:
    """Measure the bound identities for one obligation, or explain why not."""
    declaration = obligation["declaration"]
    checkout = Path(str(obligation["checkout"]))
    executor = declaration["executor"]
    if executor.get("kind") != "native_first_class_test":
        return None, "executor-not-native"
    sources = executor.get("source_paths") or []
    if not sources:
        return None, "suite-undeclared"
    suite = checkout / sources[0]
    if not suite.is_file():
        return None, "suite-missing"
    fingerprint, problem = _digest_files(
        checkout, list(declaration.get("invalidation_dependencies") or []))
    if fingerprint is None:
        return None, problem or "subject-unmeasurable"
    head = _repo_revision(checkout)
    return {
        "definition_identity": digest_hex({"declaration": declaration}),
        "subject_identity": f"{obligation['repository']}:{sources[0]}",
        "subject_fingerprint": fingerprint,
        "executor_identity": digest_hex({"executor": executor}),
        "invalidation_identity": digest_hex(
            {"dependencies": list(declaration.get("invalidation_dependencies") or [])}),
        "toolchain_identity": _toolchain_identity(session),
        "inventory_identity": "",
        "repository_revision": head,
        "repository_fingerprint": _dirty_content_digest(checkout),
    }, None


def _empty_current() -> dict[str, str]:
    return {"definition_identity": "", "subject_identity": "",
            "subject_fingerprint": "", "executor_identity": "",
            "invalidation_identity": "", "toolchain_identity": "",
            "inventory_identity": "", "repository_revision": "",
            "repository_fingerprint": ""}


def _state_rows(session) -> dict[str, dict[str, Any]]:
    rows = session.snapshot.get("verification_state")
    return dict(rows) if isinstance(rows, dict) else {}


def _session_binding(session, capability: str) -> dict[str, Any] | None:
    from .sessions import AuthorityDenied

    try:
        binding = session._binding(capability)
    except (AuthorityDenied, KeyError, ValueError, AttributeError):
        return None
    return binding if isinstance(binding, dict) else None


def _binding_available(session, capability: str) -> bool:
    binding = _session_binding(session, capability)
    if binding is None:
        return False
    return binding.get("availability", {}).get("status") == "available"


def _empty_evidence() -> dict[str, Any]:
    return {"definition_identity": "", "subject_identity": "",
            "subject_fingerprint": "", "executor_identity": "",
            "verifier_identity": "", "invalidation_identity": "",
            "toolchain_identity": "", "inventory_identity": "",
            "repository_revision": "", "repository_fingerprint": "",
            "verdict": "UNKNOWN", "evidence_id": ""}


def _coherence_request(session, obligations: list[dict],
                       measured: dict[str, dict | None],
                       rows: dict[str, dict[str, Any]],
                       max_executions: int) -> dict[str, Any]:
    provider_available = _binding_available(session, TEST_CAPABILITY)
    items = []
    for obligation in obligations:
        declaration = obligation["declaration"]
        identity = str(declaration["identity"])
        current = measured.get(identity)
        row = rows.get(identity) or {}
        recorded = row.get("evidence") if isinstance(row.get("evidence"), dict) else None
        if current is None:
            coherence_current = _empty_current()
        else:
            coherence_current = dict(current)
            coherence_current["inventory_identity"] = str(
                (recorded or {}).get("inventory_identity", ""))
        evidence = _empty_evidence()
        if recorded is not None:
            for key in evidence:
                if key in recorded:
                    evidence[key] = recorded[key]
        inventory_ids = list(row.get("inventory_test_identities") or [])
        items.append({
            "identity": identity,
            "lifecycle": declaration["lifecycle"],
            "executor_kind": declaration["executor"]["kind"],
            "runnable_native": current is not None,
            "current": coherence_current,
            "declared_patterns": list(declaration["executor"].get("declaration_identities") or []),
            "inventory_test_identities": inventory_ids[:MAX_INVENTORY_IDS],
            "inventory_truncated": bool(row.get("inventory_truncated", False)),
            "evidence_present": recorded is not None,
            "evidence": evidence,
            "evidence_conflict": False,
            "provider_available": provider_available,
        })
    return {"schema_version": COHERENCE_REQUEST_SCHEMA,
            "max_executions": max_executions, "obligations": items}


def request_coherence(session, request: dict[str, Any]) -> dict[str, Any] | None:
    """Invoke the native coherence policy through its bound capability."""
    try:
        directory = _artifact_directory(session, "coherence")
    except (OSError, ValueError):
        return None
    tag = digest_hex({"at": utcnow(), "request": request})[:12]
    workdir = directory / tag
    try:
        workdir.mkdir(parents=True, exist_ok=True, mode=0o700)
        (workdir / "request.json").write_text(
            json.dumps(request, sort_keys=True), encoding="utf-8")
        result = session.invoke(COHERENCE_CAPABILITY,
                                ["request.json", "result.json"],
                                cwd=str(workdir), timeout_seconds=180)
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


def _verdict_of(result: dict[str, Any]) -> str:
    verdict = result.get("verdict")
    if verdict in VERDICTS:
        return str(verdict)
    summary = result.get("native_suite_summary") or {}
    if isinstance(summary, dict) and summary.get("verdict") in VERDICTS:
        return str(summary.get("verdict"))
    classification = result.get("classification")
    if classification == "passed":
        return "PASS"
    if classification == "failed":
        return "FAIL"
    return "UNKNOWN"


def _failed_test_ids(result: dict[str, Any]) -> list[dict[str, str]]:
    failed = []
    tests = result.get("tests")
    if not isinstance(tests, list):
        return failed
    for entry in tests:
        if not isinstance(entry, dict):
            continue
        verdict = entry.get("verdict")
        if verdict == "PASS" or verdict == "SKIP":
            continue
        failed.append({"id": str(entry.get("id", entry.get("entry", "?"))),
                       "verdict": str(verdict)})
        if len(failed) >= MAX_FAILED_IDS:
            break
    return failed


def _inventory_of(result: dict[str, Any]) -> tuple[list[str], bool]:
    execution = result.get("execution") or {}
    ids: list[str] = []
    if isinstance(execution, dict):
        for identity in execution.get("test_case_identities") or []:
            if isinstance(identity, str) and identity:
                ids.append(identity)
    if not ids:
        for entry in result.get("tests") or []:
            if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                ids.append(entry["id"])
    seen: list[str] = []
    for identity in ids:
        if identity not in seen:
            seen.append(identity)
    return seen[:MAX_INVENTORY_IDS], len(seen) > MAX_INVENTORY_IDS


def execute_suite(session, obligation: dict, measured: dict,
                  run_tag: str) -> tuple[dict[str, Any] | None, str | None]:
    """Execute one runnable suite; return (result, applicability-problem)."""
    declaration = obligation["declaration"]
    checkout = Path(str(obligation["checkout"]))
    suite = str((checkout / declaration["executor"]["source_paths"][0]).resolve())
    argv = [suite, "--format", "json", "--result", "result.json"]
    # Declared libraries travel as environment roots, never --library
    # flags: explicit flags replace the provider adapter's own roots and
    # would silently drop the provider native library.
    libraries = []
    for library in declaration["executor"].get("library_paths") or []:
        if not isinstance(library, str) or not library:
            continue
        libraries.append(str((checkout / library).resolve()))
    inherited = os.environ.get("MNCS_LIBRARY_PATH", "")
    if inherited:
        libraries.append(inherited)
    child_env = {"MNCS_LIBRARY_PATH": ":".join(libraries)} if libraries else None
    try:
        directory = _artifact_directory(session, "runs", run_tag)
        result = session.invoke(TEST_CAPABILITY, argv, cwd=str(directory),
                                timeout_seconds=EXECUTION_TIMEOUT_SECONDS,
                                env=child_env)
    except Exception as error:
        return None, f"transport-error:{type(error).__name__}"
    status = result.get("status") if isinstance(result, dict) else None
    if status in ("timeout", "transport-error"):
        return None, f"transport-failed:{status}"
    if status not in ("ok", "failed"):
        return None, f"transport-failed:{status}"
    try:
        document = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        detail = (result.get("stderr") or result.get("stdout") or "")[:500]
        return None, f"result-unreadable:{detail or result.get('status')}"
    if not isinstance(document, dict):
        return None, "result-invalid"
    fresh_head = _repo_revision(checkout)
    fresh_dirty = _dirty_content_digest(checkout)
    if (fresh_head != measured.get("repository_revision")
            or fresh_dirty != measured.get("repository_fingerprint")):
        return None, "state-changed-during-execution"
    return document, None


def _epoch_inputs(session, obligations: list[dict],
                  measured: dict[str, dict | None]) -> dict[str, Any]:
    bindings: dict[str, str] = {}
    for capability in (COHERENCE_CAPABILITY, TEST_CAPABILITY):
        binding = _session_binding(session, capability)
        if binding is None:
            bindings[capability] = "unbound"
        else:
            bindings[capability] = str(binding.get("availability", {}).get("status"))
    return {
        "declarations": {str(item["declaration"]["identity"]): digest_hex(
            {"declaration": item["declaration"]}) for item in obligations},
        "measured": {identity: (digest_hex({"current": current})
                                 if current is not None else "unrunnable")
                     for identity, current in measured.items()},
        "bindings": bindings,
        "toolchain": _toolchain_identity(session),
        "lifecycle": session.snapshot.get("lifecycle", "active"),
    }


def ambient_pass(session, *, mode: str = "ambient",
                 max_executions: int = DEFAULT_MAX_EXECUTIONS,
                 only: str | None = None) -> dict[str, Any]:
    """Evaluate verification obligations; execute only stale runnable ones."""
    started = utcnow()
    clock_started = time.monotonic()
    workspace_root = _workspace_root(session)
    rows = _state_rows(session)
    if workspace_root is None:
        return _finish(session, started, clock_started, [], rows, [], mode,
                       {"reason": "workspace-unavailable"}, "no-workspace", None)
    obligations, invalid = discover_obligations(workspace_root)
    if only is not None:
        obligations = [item for item in obligations
                       if str(item["declaration"]["identity"]) == only]
        if not obligations:
            return _finish(session, started, clock_started, [], rows,
                           invalid, mode,
                           {"reason": f"unknown-obligation:{only}"},
                           f"unknown:{only}", None)
    measured: dict[str, dict | None] = {}
    unmeasurable: dict[str, str] = {}
    for obligation in obligations:
        identity = str(obligation["declaration"]["identity"])
        current, problem = _measured_current(session, obligation)
        measured[identity] = current
        if problem is not None:
            unmeasurable[identity] = problem
    epoch_inputs = _epoch_inputs(session, obligations, measured)
    epoch = digest_hex(epoch_inputs)
    stored = session.snapshot.get("verification_epoch") or {}
    if stored.get("epoch") == epoch and mode == "ambient":
        summary = dict(stored.get("summary") or {})
        summary["epoch_reused"] = True
        summary["elapsed_seconds"] = round(time.monotonic() - clock_started, 3)
        return {"summary": summary, "reused": True,
                "evidence": stored.get("evidence_ref")}
    if not _binding_available(session, COHERENCE_CAPABILITY):
        return _finish(session, started, clock_started, [], rows, invalid,
                       mode, {"reason": "coherence-unavailable"},
                       f"uncached:{mode}:{epoch}", None)
    request = _coherence_request(session, obligations, measured, rows, max_executions)
    coherence = request_coherence(session, request)
    if coherence is None:
        return _finish(session, started, clock_started, [], rows, invalid,
                       mode, {"reason": "coherence-failed"},
                       f"uncached:{mode}:{epoch}", None)
    by_identity = {str(item["declaration"]["identity"]): item for item in obligations}
    verdicts = {item["identity"]: item for item in coherence.get("verdicts", [])
                if isinstance(item, dict) and isinstance(item.get("identity"), str)}
    queue = [identity for identity in coherence.get("run_queue", [])
             if isinstance(identity, str) and identity in by_identity]
    results: list[dict[str, Any]] = []
    shared_runs: dict[str, tuple[dict | None, str | None, str]] = {}
    failure: dict | None = None
    for identity, item in by_identity.items():
        verdict = verdicts.get(identity) or {}
        record: dict[str, Any] = {
            "obligation": identity,
            "repository": item["repository"],
            "status": verdict.get("status", "unknown"),
            "reason": verdict.get("reason", "unknown"),
            "deferred": bool(verdict.get("deferred", False)),
            "unmeasurable": unmeasurable.get(identity),
        }
        if identity in queue and verdict.get("status") in (
                "new_execution_required", "stale"):
            current = measured.get(identity)
            if current is None:
                record["outcome"] = "not-executed"
                record["detail"] = unmeasurable.get(identity, "unrunnable")
            else:
                key = digest_hex({"suite": current.get("subject_identity"),
                                  "libraries": item["declaration"]["executor"].get(
                                      "library_paths") or [],
                                  "toolchain": current.get("toolchain_identity")})
                if key not in shared_runs:
                    run_tag = f"{digest_hex({'identity': identity})[:8]}-{digest_hex({'at': started})[:8]}"
                    shared_runs[key] = (*execute_suite(session, item, current, run_tag), run_tag)
                document, problem, run_tag = shared_runs[key]
                record["ran"] = document is not None
                if document is None:
                    record["outcome"] = "unknown"
                    record["detail"] = problem
                    failure = {"reason": f"execution-failed:{identity}",
                               "detail": problem}
                else:
                    outcome_verdict = _verdict_of(document)
                    inventory_ids, truncated = _inventory_of(document)
                    evidence_id = f"ver-{digest_hex({'identity': identity, 'at': started})[:12]}"
                    evidence = dict(current)
                    evidence["inventory_identity"] = digest_hex({"ids": inventory_ids})
                    evidence["verifier_identity"] = str(
                        item["declaration"]["executor"].get("verifier_identity", ""))
                    evidence["verdict"] = outcome_verdict
                    evidence["evidence_id"] = evidence_id
                    rows[identity] = {
                        "evidence": evidence,
                        "verdict": outcome_verdict,
                        "failure_class": str(document.get("failure_class", "none")),
                        "failed_test_ids": _failed_test_ids(document),
                        "inventory_test_identities": inventory_ids,
                        "inventory_truncated": truncated,
                        "covered_test_identities": list(
                            verdict.get("resolved_test_identities") or []),
                        "executed_at": utcnow(),
                        "run_id": str((document.get("execution") or {}).get(
                            "run_identity", "")),
                        "run_tag": run_tag,
                    }
                    record["outcome"] = ("failed" if outcome_verdict == "FAIL"
                                         else "passed" if outcome_verdict == "PASS"
                                         else "unknown")
                    record["evidence_id"] = evidence_id
                    record["failure_class"] = str(document.get("failure_class", "none"))
                    record["failed_test_ids"] = _failed_test_ids(document)
                    if record["outcome"] == "passed":
                        session._emit("verification.verified", "environment",
                                      {"obligation": identity,
                                       "evidence_id": evidence_id})
                    else:
                        session._emit("verification.failed", "environment",
                                      {"obligation": identity,
                                       "outcome": record["outcome"],
                                       "failure_class": record["failure_class"],
                                       "evidence_id": evidence_id})
        elif verdict.get("status") in ("new_execution_required", "stale"):
            record["outcome"] = "deferred"
            session._emit("verification.deferred", "environment",
                          {"obligation": identity, "reason": "budget_deferred"})
        elif verdict.get("status") == "current":
            record["outcome"] = ("failed" if verdict.get("verdict") == "FAIL"
                                 else "passed" if verdict.get("verdict") == "PASS"
                                 else "unknown")
            record["evidence_id"] = verdict.get("evidence_id", "")
        elif verdict.get("status") == "selection_unresolved":
            record["outcome"] = "unresolved"
            session._emit("verification.deferred", "environment",
                          {"obligation": identity, "reason": "selection_unresolved"})
        elif verdict.get("status") == "contradictory":
            record["outcome"] = "contradictory"
            session._emit("verification.failed", "environment",
                          {"obligation": identity, "outcome": "contradictory"})
        elif verdict.get("status") == "escalation_required":
            record["outcome"] = "escalated"
            session._emit("verification.deferred", "environment",
                          {"obligation": identity, "reason": "escalation_required"})
        elif verdict.get("status") == "not_selected":
            record["outcome"] = "excluded"
        else:
            record["outcome"] = "unknown"
        results.append(record)
    return _finish(session, started, clock_started, results, rows,
                   invalid, mode, failure, epoch, coherence)


def _finish(session, started: str, clock_started: float,
            results: list[dict], rows: dict, invalid: list[dict],
            mode: str, failure: dict | None,
            epoch: str, coherence: dict | None) -> dict[str, Any]:
    native = (coherence or {}).get("summary") or {}
    summary = {"obligations": len(results),
               "current": 0, "executed": 0, "failed": 0, "unknown": 0,
               "unsupported": int(native.get("unsupported", 0)),
               "unresolved": int(native.get("unresolved", 0)),
               "contradictory": int(native.get("contradictory", 0)),
               "excluded": int(native.get("excluded", 0)),
               "deferred": int(native.get("deferred", 0)),
               "invalid": len(invalid), "blockers": 0,
               "epoch_reused": False}
    failed_ids: list[str] = []
    for record in results:
        outcome = record.get("outcome")
        if outcome == "passed":
            summary["current"] += 1
        if record.get("ran"):
            summary["executed"] += 1
        if outcome == "failed":
            summary["failed"] += 1
            failed_ids.append(str(record.get("obligation")))
        elif outcome == "unknown":
            summary["unknown"] += 1
    summary["failed_ids"] = failed_ids[:MAX_FAILED_IDS]
    summary["blockers"] = (summary["failed"] + summary["unknown"]
                           + summary["unresolved"] + summary["contradictory"]
                           + len(invalid) + (1 if failure is not None else 0))
    evidence = {"schema_version": SCHEMA, "mode": mode,
                "started_at": started, "finished_at": utcnow(),
                "summary": summary, "results": results,
                "invalid": invalid, "failure": failure,
                "coherence_summary": dict(native)}
    evidence_ref = _write_evidence(session, evidence, mode)
    summary["elapsed_seconds"] = round(time.monotonic() - clock_started, 3)
    if failure is None:
        session.snapshot["verification_state"] = rows
    history = list(session.snapshot.get("verification_history") or [])
    history.append({"at": utcnow(), "mode": mode, "summary": dict(summary),
                    "evidence_ref": evidence_ref})
    session.snapshot["verification_history"] = history[-MAX_HISTORY:]
    # Transport/execution failures never cache their epoch: the next pass
    # must re-observe, not replay a degraded world. Recorded FAIL verdicts
    # cache normally because fail-is-current knowledge is exact.
    if mode == "ambient" and failure is None:
        stored_epoch = epoch
    else:
        stored_epoch = f"uncached:{mode}:{epoch}"
    session.snapshot["verification_epoch"] = {
        "epoch": stored_epoch,
        "summary": dict(summary), "evidence_ref": evidence_ref,
        "at": utcnow()}
    _append_session_evidence(session, summary, results, mode)
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
    directory = _artifact_directory(session, "evidence")
    ref = f"verification-{mode}-{digest_hex(evidence['finished_at'])[:12]}.json"
    path = directory / ref
    try:
        path.write_text(json.dumps(evidence, indent=2, sort_keys=True),
                        encoding="utf-8")
    except OSError:
        return "verification-evidence-unwritable"
    return f"sessions/{session.session_id}/verification-artifacts/evidence/{ref}"


def _append_session_evidence(session, summary: dict[str, Any],
                             results: list[dict], mode: str) -> None:
    entry = {
        "schema_version": EVIDENCE_SCHEMA,
        "session": session.session_id,
        "consumer": session.snapshot.get("consumer_id"),
        "mode": mode,
        "summary": {key: summary.get(key) for key in
                    ("obligations", "current", "executed", "failed",
                     "unknown", "unsupported", "unresolved", "blockers")},
        "obligations": [
            {"obligation": record.get("obligation"),
             "repository": record.get("repository"),
             "outcome": record.get("outcome"),
             "status": record.get("status"),
             "reason": record.get("reason"),
             "evidence_id": record.get("evidence_id"),
             "failure_class": record.get("failure_class")}
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
    """Compact verification status for normal agent context."""
    stored = session.snapshot.get("verification_epoch") or {}
    summary = dict(stored.get("summary") or {})
    return {"verification": {
        "current": summary.get("current", 0),
        "executed": summary.get("executed", 0),
        "failed": summary.get("failed", 0),
        "deferred": summary.get("deferred", 0),
        "blockers": summary.get("blockers", 0),
        "failed_ids": summary.get("failed_ids", []),
        "evidence": stored.get("evidence_ref")}}


def read_evidence(session) -> dict[str, Any]:
    """Full verification evidence for explicit inspection."""
    stored = session.snapshot.get("verification_epoch") or {}
    ref = stored.get("evidence_ref", "")
    if not ref or not ref.startswith("sessions/"):
        return {"evidence": None, "history": session.snapshot.get(
            "verification_history") or []}
    path = (session.store.state_dir / ref).resolve()
    try:
        if not path.is_relative_to(session.store.state_dir.resolve()):
            raise OSError("evidence escapes state root")
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        evidence = None
    return {"evidence": evidence, "history": session.snapshot.get(
        "verification_history") or []}