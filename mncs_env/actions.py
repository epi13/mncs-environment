"""Ambient external-evidence coherence: keep remote verification current.

This module composes provider-owned Actions semantics; it owns no test
semantics, no debugging, no workflow execution, and no GitHub protocol
beyond invoking the declared transport. On every environment entry it
observes external verification obligations, measures the bound subject
(repository, published revision, dirty state, workflow binding), reuses
exact prior receipts, subscribes to in-flight runs, and stays quiet
when there is nothing to route.

Policy lives in `mncs.actions.external_evidence` (current / eligible /
pending / no-route / unavailable / deferred / escalate). This file
transports facts to that policy and carries out admitted effects:
read-only remote observation and evidence recording. Dispatching a
remote run is never ambient: the dispatch capability carries the
`delegate` effect, which escalates unless the session holds a
repository claim, so an explicit grant always precedes remote work.

A dirty worktree is never remotely verifiable, an unpublished revision
has no remote runs, and a workflow success never establishes PASS by
itself: only a structurally valid receipt binding the exact obligation
and subject with ESTABLISHED claim status becomes current evidence.
Nothing here commits, pushes, branches, or mutates a repository.
"""

from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import verification as verification_module
from .identity import digest_hex

SCHEMA = "mncs.environment.actions/1"
COHERENCE_REQUEST_SCHEMA = "mncs.actions-external-evidence-request/1"
COHERENCE_RESULT_SCHEMA = "mncs.actions-external-evidence/1"
EVIDENCE_SCHEMA = "mncs.session-evidence/1"
MAX_HISTORY = 10
MAX_OBLIGATIONS = 32
MAX_IDENTITY_BYTES = 1024
MAX_CAPSULE_IDS = 8
COHERENCE_CAPABILITY = "mncs.actions-external-evidence/1"
REMOTE_CAPABILITY = "mncs.actions-remote-evidence/1"
DISPATCH_CAPABILITY = "mncs.actions-dispatch/1"
DEFAULT_MAX_DISPATCHES = 2
MAX_DISPATCH_ATTEMPTS = 3
REMOTE_TIMEOUT_SECONDS = 90
FETCH_TIMEOUT_SECONDS = 180
DISPATCH_TIMEOUT_SECONDS = 300
GIT_TIMEOUT_SECONDS = 30
#: Remote boundary failures (auth, network, missing CLI) are cached per
#: subject so one outage does not cost a probe per entry, but the cache
#: expires: recovery without a subject change becomes visible within
#: the hour instead of sticking until the next commit.
REMOTE_ERROR_TTL_SECONDS = 3600

#: GitHub run-status vocabulary projected onto the native run-state
#: vocabulary. The host normalizes these boundary literals; only the
#: native policy decides what a state means for routing and reuse.
RUN_STATUS_TO_STATE = {
    "queued": "in_progress",
    "waiting": "in_progress",
    "pending": "in_progress",
    "requested": "in_progress",
    "in_progress": "in_progress",
    "completed": "completed",
}

#: GitHub conclusion vocabulary projected onto the native conclusion
#: vocabulary. Only a completed run whose owner receipt validates can
#: carry a verdict; every other conclusion is infrastructure-grade.
RUN_CONCLUSION_TO_CONCLUSION = {
    "success": "success",
    "failure": "failure",
    "neutral": "infra",
    "cancelled": "infra",
    "timed_out": "infra",
    "action_required": "infra",
    "startup_failure": "infra",
    "stale": "infra",
    "skipped": "infra",
}

#: Transport failures that mark the remote boundary unavailable for the
#: current subject generation (quiet deferral, retried on change).
REMOTE_UNAVAILABLE = {"auth_missing", "network_error", "gh_missing", "timeout"}

#: Transport failures that invalidate the declared workflow binding for
#: the current declaration (no route, never dispatched).
BINDING_INVALID = {"workflow_missing"}

#: Staging failures that prove a completed run can never yield evidence
#: for its subject (as opposed to transient fetch faults). A terminally
#: failed run stops being pending and the obligation becomes eligible
#: for a fresh run instead of waiting on it forever.
TERMINAL_STAGE_PROBLEMS = {"receipt-invalid", "receipt-mismatch",
                            "artifact_missing", "run_not_found",
                            "receipt-unusable"}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _artifact_directory(session, *parts: str) -> Path:
    state_root = session.store.state_dir.resolve()
    path = (state_root / "sessions" / session.session_id
            / "actions-artifacts" / Path(*parts)).resolve()
    if not path.is_relative_to(state_root):
        raise ValueError("actions artifact path escapes the state root")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _workspace_root(session) -> Path | None:
    root = session.snapshot.get("workspace", {}).get("root")
    if not root:
        return None
    path = Path(root)
    return path if path.is_dir() else None


def _state_rows(session) -> dict[str, dict[str, Any]]:
    rows = session.snapshot.get("actions_state")
    return dict(rows) if isinstance(rows, dict) else {}


def _binding_of(declaration: dict[str, Any]) -> dict[str, str] | None:
    """Extract a usable external binding, or None when undeclared.

    A binding names the exact remote route: owner repository, workflow,
    artifact, and check identity. Anything less is no route; Actions
    never guesses a workflow from a provider name.
    """
    executor = declaration.get("executor")
    if not isinstance(executor, dict):
        return None
    external = executor.get("external")
    if not isinstance(external, dict):
        return None
    binding = {key: str(external.get(key, ""))
               for key in ("repository", "workflow", "artifact",
                           "check_identity")}
    if not all(binding.values()):
        return None
    binding["explicit_only"] = (
        "true" if external.get("explicit_only") is True else "false")
    return binding


def _subject_state(checkout: Path) -> tuple[str, str, str]:
    """Measure (state, revision, dirty) for one subject checkout.

    States are clean (published revision, verified below), dirty,
    unknown (unmeasurable), or clean_unpublished. Publication is read
    from local remote-tracking refs only; a stale ref set fails closed
    to unpublished, never to an imagined remote run.
    """
    head = verification_module.repo_revision(checkout)
    dirty = verification_module.dirty_content_digest(checkout)
    if head in ("repo-unreadable", "head-unknown", "") or dirty == "repo-unreadable":
        return "unknown", head, dirty
    if dirty != "clean":
        return "dirty", head, dirty
    try:
        completed = subprocess.run(
            ["git", "-C", str(checkout), "branch", "-r", "--contains", head],
            capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS,
            check=False)
    except (OSError, subprocess.SubprocessError):
        return "unknown", head, dirty
    if completed.returncode != 0:
        return "unknown", head, dirty
    if not completed.stdout.strip():
        return "clean_unpublished", head, dirty
    return "clean", head, dirty


def _dispatch_ref(checkout: Path, revision: str) -> str:
    """Resolve a dispatchable ref for an exact published revision.

    GitHub dispatches workflows by branch or tag, never by raw sha, so
    an exact revision is dispatchable only while some remote-tracking
    ref points at it. Returns "" when no such ref exists; observation
    of push-triggered runs still works, only fresh dispatch refuses.
    """
    try:
        completed = subprocess.run(
            ["git", "-C", str(checkout), "branch", "-r",
             "--points-at", revision, "--format=%(refname:short)"],
            capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS,
            check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    refs = sorted(line.strip() for line in completed.stdout.splitlines()
                  if line.strip() and "->" not in line)
    for ref in refs:
        if "/" in ref:
            return ref.split("/", 1)[1]
    return ""


def _subject_digest(repository: str, revision: str, binding: dict[str, str]) -> str:
    """Normative external subject identity (see the native module)."""
    return digest_hex({"repository": repository, "revision": revision,
                       "workflow": binding["workflow"],
                       "artifact": binding["artifact"],
                       "check_identity": binding["check_identity"]})


def _empty_evidence() -> dict[str, Any]:
    return {"obligation": "", "subject_digest": "", "run_identity": "",
            "verdict": "none", "claim": "none", "complete": False}


def _empty_run() -> dict[str, Any]:
    return {"run_identity": "", "subject_digest": "",
            "state": "none", "conclusion": "none"}


def _project_conclusion(conclusion: Any) -> str:
    return RUN_CONCLUSION_TO_CONCLUSION.get(str(conclusion or ""), "infra")


def _invoke_remote(session, argv: list[str], timeout: int) -> dict[str, Any] | None:
    """Invoke the read-only remote transport; None on transport failure."""
    try:
        result = session.invoke(REMOTE_CAPABILITY, argv,
                                timeout_seconds=timeout)
    except Exception:
        return None
    if not isinstance(result, dict) or result.get("status") != "ok":
        return None
    try:
        envelope = json.loads(result.get("stdout") or "")
    except ValueError:
        return None
    return envelope if isinstance(envelope, dict) else None


def _observe_runs(session, repository: str, binding: dict[str, str],
                  revision: str) -> tuple[list[dict[str, Any]] | None, str]:
    """Observe exact remote runs; (None, transport-code) on failure."""
    envelope = _invoke_remote(
        session, ["status", "--repo", binding["repository"],
                  "--workflow", binding["workflow"], "--sha", revision],
        REMOTE_TIMEOUT_SECONDS)
    if envelope is None:
        return None, "transport-failed"
    if envelope.get("transport") != "ok":
        return None, str(envelope.get("transport") or "transport_error")
    runs = envelope.get("runs")
    if not isinstance(runs, list):
        return None, "transport_error"
    return [run for run in runs if isinstance(run, dict)], ""


def _fetch_and_validate(session, binding: dict[str, str], run_id: str,
                        evidence_tag: str
                        ) -> tuple[dict[str, Any] | None, str]:
    """Fetch one artifact and validate its receipt triple.

    Returns (projected, problem). The projection carries validated field
    values only; whether the receipt satisfies an obligation is native
    policy, decided from the staged evidence on the next pass.
    """
    try:
        staged = _artifact_directory(session, "staged", evidence_tag)
    except (OSError, ValueError):
        return None, "artifact-unwritable"
    envelope = _invoke_remote(
        session, ["fetch", "--repo", binding["repository"],
                  "--run-id", str(run_id), "--artifact",
                  binding["artifact"], "--output-dir", str(staged)],
        FETCH_TIMEOUT_SECONDS)
    if envelope is None:
        return None, "transport-failed"
    if envelope.get("transport") != "ok":
        return None, str(envelope.get("transport") or "transport_error")
    envelope = _invoke_remote(
        session, ["validate", "--staged-dir", str(staged)],
        REMOTE_TIMEOUT_SECONDS)
    if envelope is None:
        return None, "transport-failed"
    if envelope.get("transport") != "ok":
        return None, str(envelope.get("transport") or "transport_error")
    if envelope.get("valid") is not True:
        return None, "receipt-invalid"
    projected = envelope.get("projected")
    if not isinstance(projected, dict):
        return None, "receipt-invalid"
    return projected, ""


def _stage_matches(projected: dict[str, Any], identity: str,
                   revision: str, binding: dict[str, str]) -> bool:
    """Check staged field values before admitting them as evidence."""
    return (str(projected.get("check_id")) == binding["check_identity"]
            and str(projected.get("revision")) == revision
            and str(projected.get("claim_status")) == "ESTABLISHED"
            and str(projected.get("verdict")) in ("PASS", "FAIL", "UNKNOWN"))


def _native_verdict(verdict: str) -> str:
    return {"PASS": "passed", "FAIL": "failed",
            "UNKNOWN": "unknown"}.get(verdict, "none")


def observe_externals(session, rows: dict[str, dict[str, Any]],
                      *, remote: bool = True,
                      ) -> tuple[list[dict[str, Any]], int]:
    """Build stable external views for every declared external obligation.

    Returns (externals, observed_count). Remote runs are observed only
    for clean published subjects with a valid binding and no usable
    staged evidence; every other obligation costs local measurement
    alone. Completed runs are never re-queried; in-flight runs are
    re-observed so completion is noticed promptly. With remote=False
    the observation is purely local so the epoch check can skip remote
    work entirely on a quiet re-entry.
    """
    workspace_root = _workspace_root(session)
    if workspace_root is None:
        return [], 0
    obligations, _invalid = verification_module.discover_obligations(
        workspace_root)
    externals: list[dict[str, Any]] = []
    observed = 0
    for item in sorted(obligations,
                       key=lambda entry: str(entry["declaration"]["identity"])):
        declaration = item["declaration"]
        if declaration["executor"].get("kind") != "external_integration":
            continue
        identity = str(declaration["identity"])
        observed += 1
        checkout = Path(str(item.get("checkout", "")))
        repository = str(item.get("repository", ""))
        recorded = rows.get(identity) if isinstance(rows.get(identity), dict) else {}
        binding = _binding_of(declaration)
        if binding is None:
            externals.append({
                "obligation": identity, "routable_kind": True,
                "has_workflow_binding": False, "explicit_only": False,
                "subject_state": "unknown", "subject_digest": "",
                "remote_available": True, "attempts_exhausted": False,
                "evidence_present": False, "evidence": _empty_evidence(),
                "run_present": False, "run": _empty_run(),
                "context": {"obligation": identity, "repository": repository,
                            "checkout": str(checkout), "binding": None,
                            "revision": ""},
            })
            continue
        state, revision, _dirty = _subject_state(checkout)
        digest = _subject_digest(repository, revision, binding)
        declaration_digest = digest_hex({"executor": declaration["executor"]})
        binding_missing = (recorded.get("binding_missing") == binding["workflow"]
                           and recorded.get("binding_declaration") == declaration_digest)
        remote_error_at = recorded.get("remote_error_at") or 0
        try:
            remote_age = time.time() - float(remote_error_at)
        except (TypeError, ValueError):
            remote_age = float("inf")
        remote_error = (recorded.get("remote_error_subject") == digest
                        and isinstance(recorded.get("remote_error"), str)
                        and recorded.get("remote_error") in REMOTE_UNAVAILABLE
                        and remote_age < REMOTE_ERROR_TTL_SECONDS)
        attempts = recorded.get("attempts") or {}
        exhausted = (isinstance(attempts, dict)
                     and int(attempts.get(digest, 0) or 0) >= MAX_DISPATCH_ATTEMPTS)
        staged = recorded.get("evidence") if isinstance(
            recorded.get("evidence"), dict) else None
        evidence_present = (
            staged is not None and staged.get("subject_digest") == digest
            and staged.get("obligation") == identity
            and staged.get("claim") == "established"
            and staged.get("complete") is True)
        evidence = dict(staged) if evidence_present and staged else _empty_evidence()
        prior_run = recorded.get("run") if isinstance(
            recorded.get("run"), dict) else None
        run_present = (prior_run is not None
                       and prior_run.get("subject_digest") == digest)
        run = dict(prior_run) if run_present and prior_run else _empty_run()
        stage_failed = recorded.get("stage_failed") \
            if isinstance(recorded.get("stage_failed"), dict) else {}
        dead_run = ""
        if (stage_failed.get("subject") == digest
                and stage_failed.get("problem") in TERMINAL_STAGE_PROBLEMS):
            dead_run = str(stage_failed.get("run_identity", ""))
        context: dict[str, Any] = {
            "obligation": identity, "repository": repository,
            "checkout": str(checkout), "binding": binding,
            "revision": revision, "subject_digest": digest,
            "declaration_digest": declaration_digest,
            "dispatch_ref": _dispatch_ref(checkout, revision)
            if state == "clean" else "",
        }
        if (remote and state == "clean" and not binding_missing
                and not evidence_present
                and not (run_present and run.get("state") == "completed"
                         and not dead_run)):
            observed_run, runs, problem = _refresh_remote(
                session, context, dead_run,
                dead_problem=str(stage_failed.get("problem", "")))
            if observed_run is not None:
                run, run_present = observed_run, True
                context.pop("stage_failed", None)
            elif dead_run and not context.get("stage_failed"):
                # The dead run is still the newest exact run: no usable
                # run exists, so the obligation reads as eligible for a
                # fresh run rather than pending on evidence that can
                # never arrive.
                run, run_present = _empty_run(), False
            context["remote_runs"] = runs
            context["remote_problem"] = problem
            if problem in BINDING_INVALID:
                binding_missing = True
            elif problem in REMOTE_UNAVAILABLE:
                context["remote_error"] = problem
        externals.append({
            "obligation": identity, "routable_kind": True,
            "has_workflow_binding": not binding_missing,
            "explicit_only": binding["explicit_only"] == "true",
            "subject_state": state, "subject_digest": digest,
            "remote_available": not remote_error,
            "attempts_exhausted": exhausted,
            "evidence_present": evidence_present, "evidence": evidence,
            "run_present": run_present, "run": run,
            "context": context,
        })
    externals.sort(key=lambda item: str(item["obligation"]))
    return externals[:MAX_OBLIGATIONS], observed


def _refresh_remote(session, context: dict[str, Any],
                    dead_run: str, dead_problem: str = ""
                    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str]:
    """Observe and stage remote state for one eligible obligation.

    Returns (run-or-None, runs-seen, problem). When the newest exact run
    completed, its artifact is fetched and validated immediately so the
    policy decides on staged evidence in the same pass; the staged
    triple is retained under the session evidence tag. A run already
    recorded as terminally failed (dead_run) is skipped in favor of any
    newer exact run; when it is still newest, None is returned so the
    obligation reads as eligible for a fresh run.
    """
    binding = context["binding"]
    runs, problem = _observe_runs(session, context["repository"], binding,
                                  context["revision"])
    if runs is None:
        return None, [], problem
    exact = [run for run in runs
             if str(run.get("head_sha")) == context["revision"]]
    if not exact:
        return None, runs, ""
    newest = sorted(exact, key=lambda run: str(run.get("created_at", "")))[-1]
    newest_id = str(newest.get("run_id", ""))
    if dead_run and newest_id == dead_run:
        context["stage_failed"] = {"subject": context["subject_digest"],
                                   "run_identity": dead_run,
                                   "problem": dead_problem or "receipt-unusable"}
        return None, runs, ""
    state = RUN_STATUS_TO_STATE.get(str(newest.get("status", "")), "none")
    conclusion = _project_conclusion(newest.get("conclusion"))
    run = {"run_identity": newest_id,
           "subject_digest": context["subject_digest"],
           "state": state, "conclusion": conclusion,
           "observed_at": utcnow()}
    if state != "completed" or not run["run_identity"]:
        return run, runs, ""
    evidence_tag = f"{digest_hex({'obligation': context['obligation']})[:8]}-{run['run_identity']}"
    projected, stage_problem = _fetch_and_validate(
        session, binding, run["run_identity"], evidence_tag)
    if projected is None:
        context["stage_problem"] = stage_problem
        if stage_problem in TERMINAL_STAGE_PROBLEMS:
            context["stage_failed"] = {"subject": context["subject_digest"],
                                       "run_identity": newest_id,
                                       "problem": stage_problem}
            return None, runs, ""
        return run, runs, ""
    if not _stage_matches(projected, context["obligation"], context["revision"], binding):
        context["stage_problem"] = "receipt-mismatch"
        context["stage_failed"] = {"subject": context["subject_digest"],
                                   "run_identity": newest_id,
                                   "problem": "receipt-mismatch"}
        return None, runs, ""
    context["staged"] = {
        "obligation": context["obligation"],
        "subject_digest": context["subject_digest"],
        "run_identity": run["run_identity"],
        "verdict": _native_verdict(str(projected.get("verdict"))),
        "claim": "established",
        "complete": True,
        "staged_tag": evidence_tag,
        "check_verdict": str(projected.get("check_verdict", "")),
    }
    return run, runs, ""


def _coherence_request(session, externals: list[dict[str, Any]],
                       rows: dict[str, dict[str, Any]],
                       max_dispatches: int) -> dict[str, Any]:
    items = []
    for item in externals:
        evidence = dict(item["evidence"])
        staged = (item.get("context") or {}).get("staged")
        evidence_present = bool(item["evidence_present"])
        if isinstance(staged, dict) and not evidence_present:
            for key in ("obligation", "subject_digest", "run_identity",
                        "verdict", "claim", "complete"):
                evidence[key] = staged[key]
            evidence_present = True
        items.append({
            "obligation": item["obligation"],
            "routable_kind": True,
            "has_workflow_binding": bool(item["has_workflow_binding"]),
            "explicit_only": bool(item["explicit_only"]),
            "subject_state": item["subject_state"],
            "subject_digest": item["subject_digest"],
            "remote_available": bool(item["remote_available"]),
            "attempts_exhausted": bool(item["attempts_exhausted"]),
            "evidence_present": evidence_present,
            "evidence": evidence,
            "run_present": bool(item["run_present"]),
            "run": {key: item["run"][key] for key in
                    ("run_identity", "subject_digest", "state", "conclusion")},
        })
    return {"schema_version": COHERENCE_REQUEST_SCHEMA,
            "max_dispatches": max_dispatches, "obligations": items}


def request_coherence(session, request: dict[str, Any]) -> dict[str, Any] | None:
    """Invoke the native external-evidence policy through its capability."""
    try:
        directory = _artifact_directory(session, "coherence")
    except (OSError, ValueError):
        return None
    tag = digest_hex({"at": utcnow(), "request": request})[:12]
    workdir = directory / tag
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


def dispatch_external(session, item: dict[str, Any]
                      ) -> tuple[dict[str, Any] | None, str | None]:
    """Dispatch one admitted remote run; return (run, problem).

    Authority is enforced by the invoke itself: the dispatch capability
    carries the `delegate` effect, which escalates without a repository
    claim. A proposed dispatch the authority layer pends is never
    executed and never recorded as an attempt.
    """
    context = item.get("context") or {}
    binding = context.get("binding")
    revision = str(context.get("revision", ""))
    ref = str(context.get("dispatch_ref", ""))
    if not isinstance(binding, dict) or not revision:
        return None, "subject-unresolvable"
    if not ref:
        # Published but no ref points at it: GitHub cannot dispatch
        # the sha, though push-triggered runs for it still observe.
        # Refusing burns no attempt and fails no pass.
        return None, "no-dispatch-ref"
    try:
        result = session.invoke(
            DISPATCH_CAPABILITY,
            ["--repo", binding["repository"], "--workflow",
             binding["workflow"], "--ref", ref, "--sha", revision,
             "--session", session.session_id],
            timeout_seconds=DISPATCH_TIMEOUT_SECONDS)
    except Exception as error:
        return None, f"transport-error:{type(error).__name__}"
    status = result.get("status") if isinstance(result, dict) else None
    if status == "pending-escalation":
        return None, "escalation-required"
    if status in ("timeout", "transport-error"):
        return None, f"transport-failed:{status}"
    if status != "ok":
        return None, f"transport-failed:{status}"
    try:
        envelope = json.loads(result.get("stdout") or "")
    except ValueError:
        return None, "dispatch-unreadable"
    if not isinstance(envelope, dict) or envelope.get("transport") != "ok":
        code = str(envelope.get("transport", "transport_error")) \
            if isinstance(envelope, dict) else "transport_error"
        return None, f"dispatch-failed:{code}"
    run = envelope.get("run")
    if not isinstance(run, dict):
        return {}, None
    if run.get("head_sha") and str(run.get("head_sha")) != revision:
        # The ref advanced between measurement and dispatch: the new
        # run tests a different subject and must never satisfy this one.
        return None, "subject-advanced-during-dispatch"
    return run, None


def _epoch_inputs(session, externals: list[dict[str, Any]]) -> dict[str, Any]:
    bindings: dict[str, str] = {}
    for capability in (COHERENCE_CAPABILITY, REMOTE_CAPABILITY,
                       DISPATCH_CAPABILITY):
        binding = verification_module.session_binding(session, capability)
        if binding is None:
            bindings[capability] = "unbound"
        else:
            bindings[capability] = str(binding.get("availability", {}).get("status"))
    rows = _state_rows(session)
    material = {}
    for item in externals:
        identity = str(item["obligation"])
        recorded = rows.get(identity) if isinstance(rows.get(identity), dict) else {}
        stage_failed = recorded.get("stage_failed") \
            if isinstance(recorded.get("stage_failed"), dict) else {}
        material[identity] = digest_hex({
            "state": item["subject_state"], "subject": item["subject_digest"],
            "binding": bool(item["has_workflow_binding"]),
            "explicit": bool(item["explicit_only"]),
            "evidence": (recorded.get("evidence") or {}).get("subject_digest", "")
            if isinstance(recorded.get("evidence"), dict) else "",
            "run": (recorded.get("run") or {}).get("run_identity", "")
            if isinstance(recorded.get("run"), dict) else "",
            "run_state": (recorded.get("run") or {}).get("state", "")
            if isinstance(recorded.get("run"), dict) else "",
            "stage_failed": digest_hex(stage_failed) if stage_failed else "",
            "remote_error": str(recorded.get("remote_error") or ""),
        })
    return {"externals": material, "bindings": bindings,
            "toolchain": verification_module.toolchain_identity(session),
            "lifecycle": session.snapshot.get("lifecycle", "active")}


def ambient_pass(session, *, mode: str = "ambient",
                 max_dispatches: int = DEFAULT_MAX_DISPATCHES,
                 only: str | None = None,
                 dispatch: bool = False) -> dict[str, Any]:
    """Keep external verification evidence current; route only stale ones.

    Ambient passes never dispatch: eligible obligations surface as
    delegate requests. Explicit dispatch passes execute the admitted
    queue through the claim-gated delegate capability.
    """
    started = utcnow()
    clock_started = time.monotonic()
    rows = _state_rows(session)
    workspace_root = _workspace_root(session)
    if workspace_root is None:
        return _finish(session, started, clock_started, [], rows, 0, mode,
                       {"reason": "workspace-unavailable"}, "no-workspace",
                       None)
    # Cheap local observation first: the epoch check must precede any
    # remote work, or a quiet re-entry would still cost GitHub calls.
    externals, observed = observe_externals(session, rows, remote=False)
    if only is not None:
        externals = [item for item in externals
                     if str(item["obligation"]) == only]
        if not externals:
            return _finish(session, started, clock_started, [], rows,
                           observed, mode, {"reason": f"unknown-obligation:{only}"},
                           f"unknown:{only}", None)
    # Ambient passes surface every eligible route; only explicit
    # dispatch passes apply the execution budget.
    policy_budget = MAX_OBLIGATIONS if not dispatch else max_dispatches
    epoch_inputs = _epoch_inputs(session, externals)
    epoch = digest_hex(epoch_inputs)
    stored = session.snapshot.get("actions_epoch") or {}
    if stored.get("epoch") == epoch and mode == "ambient" and not dispatch:
        summary = dict(stored.get("summary") or {})
        summary["epoch_reused"] = True
        summary["elapsed_seconds"] = round(time.monotonic() - clock_started, 3)
        return {"summary": summary, "reused": True,
                "evidence": stored.get("evidence_ref")}
    externals, observed = observe_externals(session, rows, remote=True)
    if only is not None:
        externals = [item for item in externals
                     if str(item["obligation"]) == only]
    if not externals:
        empty = {"summary": {"obligations": 0, "current": 0, "pending": 0,
                             "eligible": 0, "deferred": 0, "unsupported": 0,
                             "escalated": 0},
                 "decisions": [], "dispatch_queue": []}
        return _finish(session, started, clock_started, [], {}, observed,
                       mode, None, epoch, empty)
    if not verification_module.binding_available(session, COHERENCE_CAPABILITY):
        return _finish(session, started, clock_started, [], rows, observed,
                       mode, {"reason": "coherence-unavailable"},
                       f"uncached:{mode}:{epoch}", None)
    request = _coherence_request(session, externals, rows, policy_budget)
    coherence = request_coherence(session, request)
    if coherence is None:
        return _finish(session, started, clock_started, [], rows, observed,
                       mode, {"reason": "coherence-failed"},
                       f"uncached:{mode}:{epoch}", None)
    by_identity = {str(item["obligation"]): item for item in externals}
    decisions = {item["obligation"]: item
                 for item in coherence.get("decisions", [])
                 if isinstance(item, dict)
                 and isinstance(item.get("obligation"), str)}
    queue = [identity for identity in coherence.get("dispatch_queue", [])
             if isinstance(identity, str) and identity in by_identity]
    results: list[dict[str, Any]] = []
    next_rows: dict[str, dict[str, Any]] = {}
    failure: dict | None = None
    for identity, item in by_identity.items():
        verdict = decisions.get(identity) or {}
        context = item.get("context") or {}
        record: dict[str, Any] = {
            "obligation": identity,
            "repository": context.get("repository", ""),
            "status": verdict.get("status", "unknown"),
            "reason": verdict.get("reason", "unknown"),
            "operation": verdict.get("operation", "none"),
            "deferred": bool(verdict.get("deferred", False)),
        }
        status = verdict.get("status")
        if status == "current":
            record["outcome"] = "current"
            record["verdict"] = verdict.get("verdict", "none")
            staged = context.get("staged")
            if isinstance(staged, dict):
                evidence_id = f"ext-{digest_hex({'identity': identity, 'at': started})[:12]}"
                prior = rows.get(identity) if isinstance(
                    rows.get(identity), dict) else {}
                next_rows[identity] = {
                    "evidence": {key: staged[key] for key in
                                 ("obligation", "subject_digest", "run_identity",
                                  "verdict", "claim", "complete")},
                    "evidence_id": evidence_id,
                    "staged_tag": staged.get("staged_tag", ""),
                    "check_verdict": staged.get("check_verdict", ""),
                    "subject_digest": item["subject_digest"],
                    "run": dict(item["run"]) if item["run_present"] else {},
                    "attempts": dict(prior.get("attempts") or {}),
                    "admitted_at": utcnow(),
                }
                record["evidence_id"] = evidence_id
                record["run_identity"] = staged.get("run_identity", "")
                session._emit("external.admitted", "environment",
                              {"obligation": identity,
                               "evidence_id": evidence_id,
                               "verdict": staged.get("verdict", "none")})
            else:
                kept = rows.get(identity)
                if isinstance(kept, dict):
                    next_rows[identity] = kept
                    record["evidence_id"] = kept.get("evidence_id", "")
                    record["run_identity"] = (
                        kept.get("evidence") or {}).get("run_identity", "")
        elif status == "delegated_pending":
            record["outcome"] = "pending"
            record["run_identity"] = verdict.get("run_identity", "")
            kept = rows.get(identity)
            next_rows[identity] = dict(kept) if isinstance(kept, dict) else {}
            # Pending supersedes any previously staged evidence: the
            # in-flight run will produce fresh receipts, and stale
            # evidence must never satisfy a later admission.
            next_rows[identity].pop("evidence", None)
            next_rows[identity].pop("evidence_id", None)
            next_rows[identity].pop("stage_failed", None)
            if item["run_present"]:
                next_rows[identity]["run"] = dict(item["run"])
            next_rows[identity]["subject_digest"] = item["subject_digest"]
        elif status == "dispatch_eligible" and identity in queue and dispatch:
            run, problem = dispatch_external(session, item)
            digest = item["subject_digest"]
            prior = rows.get(identity) if isinstance(rows.get(identity), dict) else {}
            attempts = dict(prior.get("attempts") or {})
            if run is None:
                record["outcome"] = "not-dispatched"
                record["detail"] = problem
                if problem in ("escalation-required", "no-dispatch-ref"):
                    pass
                elif str(problem or "").startswith("dispatch-failed:claim_held"):
                    # Another live session won the dispatch race; its run
                    # will be observed via status. Not our attempt.
                    record["outcome"] = "deduped"
                else:
                    attempts[digest] = int(attempts.get(digest, 0) or 0) + 1
                    failure = {"reason": f"dispatch-failed:{identity}",
                               "detail": problem}
                    session._emit("external.failed", "environment",
                                  {"obligation": identity, "detail": problem})
                next_rows[identity] = dict(prior)
                next_rows[identity]["attempts"] = attempts
                next_rows[identity]["subject_digest"] = digest
            else:
                attempts[digest] = int(attempts.get(digest, 0) or 0) + 1
                run_identity = str(run.get("run_id", ""))
                next_rows[identity] = dict(prior)
                next_rows[identity]["attempts"] = attempts
                next_rows[identity]["subject_digest"] = digest
                if run_identity:
                    next_rows[identity]["run"] = {
                        "run_identity": run_identity,
                        "subject_digest": digest, "state": "in_progress",
                        "conclusion": "none", "observed_at": utcnow()}
                record["outcome"] = "dispatched"
                record["run_identity"] = run_identity
                session._emit("external.dispatched", "environment",
                              {"obligation": identity, "run": run_identity})
        elif status == "dispatch_eligible":
            record["outcome"] = "eligible"
            record["delegate_request"] = True
            kept = rows.get(identity)
            entry = dict(kept) if isinstance(kept, dict) else {}
            # Eligibility means no usable evidence: drop anything stale
            # so a later admission cannot rest on a previous subject.
            entry.pop("evidence", None)
            entry.pop("evidence_id", None)
            if context.get("remote_error"):
                entry["remote_error"] = context["remote_error"]
                entry["remote_error_subject"] = item["subject_digest"]
                entry["remote_error_at"] = time.time()
            if context.get("stage_failed"):
                entry["stage_failed"] = dict(context["stage_failed"])
            else:
                entry.pop("stage_failed", None)
            entry["subject_digest"] = item["subject_digest"]
            next_rows[identity] = entry
            session._emit("external.deferred", "environment",
                          {"obligation": identity,
                           "reason": "delegate_required"})
        elif status in ("no_route", "unavailable"):
            record["outcome"] = "unsupported"
            context_problem = context.get("remote_problem", "")
            if context_problem in BINDING_INVALID:
                next_rows[identity] = {
                    "binding_missing": (context.get("binding") or {}).get(
                        "workflow", ""),
                    "binding_declaration": context.get("declaration_digest", ""),
                    "subject_digest": item["subject_digest"],
                }
            elif context.get("remote_error"):
                prior = rows.get(identity) if isinstance(
                    rows.get(identity), dict) else {}
                entry = dict(prior)
                entry["remote_error"] = context["remote_error"]
                entry["remote_error_subject"] = item["subject_digest"]
                entry["remote_error_at"] = time.time()
                next_rows[identity] = entry
        elif status == "deferred":
            record["outcome"] = "deferred"
            # Deferred rows persist: attempt counts and cached remote
            # errors must survive, or the next pass would retry
            # unboundedly instead of staying quietly deferred.
            kept = rows.get(identity)
            if isinstance(kept, dict):
                entry = dict(kept)
                entry.pop("evidence", None)
                entry.pop("evidence_id", None)
                entry["subject_digest"] = item["subject_digest"]
                next_rows[identity] = entry
            session._emit("external.deferred", "environment",
                          {"obligation": identity,
                           "reason": verdict.get("reason", "deferred")})
        elif status == "escalate":
            record["outcome"] = "escalated"
            kept = rows.get(identity)
            if isinstance(kept, dict):
                entry = dict(kept)
                entry.pop("evidence", None)
                entry.pop("evidence_id", None)
                entry["subject_digest"] = item["subject_digest"]
                next_rows[identity] = entry
            session._emit("external.deferred", "environment",
                          {"obligation": identity,
                           "reason": verdict.get("reason", "escalate")})
        else:
            record["outcome"] = "unknown"
        results.append(record)
    if failure is None:
        if only is not None:
            merged = dict(rows)
            merged.update(next_rows)
            rows = merged
        else:
            rows = next_rows
    else:
        # A dispatch failure aborts full row adoption, but attempt
        # accounting must survive or the next pass would retry
        # unboundedly instead of backing off toward the cap.
        kept = dict(rows)
        for identity, entry in next_rows.items():
            if isinstance(entry, dict) and entry.get("attempts"):
                prior = dict(kept.get(identity) or {})
                prior["attempts"] = entry["attempts"]
                if entry.get("subject_digest"):
                    prior["subject_digest"] = entry["subject_digest"]
                kept[identity] = prior
        rows = kept
    return _finish(session, started, clock_started, results, rows,
                   observed, mode, failure, epoch, coherence)


def _finish(session, started: str, clock_started: float,
            results: list[dict], rows: dict, observed: int,
            mode: str, failure: dict | None,
            epoch: str, coherence: dict | None) -> dict[str, Any]:
    native = (coherence or {}).get("summary") or {}
    summary = {"obligations": len(results),
               "observed": observed,
               "current": 0, "pending": 0, "eligible": 0,
               "dispatched": 0, "deferred": 0,
               "unsupported": int(native.get("unsupported", 0)),
               "escalated": int(native.get("escalated", 0)),
               "blockers": 0,
               "epoch_reused": False}
    capsule: list[str] = []
    delegate_requests: list[str] = []
    for record in results:
        outcome = record.get("outcome")
        if outcome == "current":
            summary["current"] += 1
        if outcome == "pending":
            summary["pending"] += 1
        if outcome == "eligible":
            summary["eligible"] += 1
            delegate_requests.append(str(record.get("obligation")))
        if outcome == "dispatched":
            summary["dispatched"] += 1
        if outcome == "deferred":
            summary["deferred"] += 1
        if outcome in ("escalated", "dispatched") or record.get("verdict") == "failed":
            capsule.append(str(record.get("obligation")))
    summary["capsule_ids"] = capsule[:MAX_CAPSULE_IDS]
    summary["delegate_requests"] = delegate_requests[:MAX_CAPSULE_IDS]
    summary["blockers"] = summary["escalated"] + (
        1 if failure is not None else 0)
    evidence = {"schema_version": SCHEMA, "mode": mode,
                "started_at": started, "finished_at": utcnow(),
                "summary": summary, "results": results,
                "failure": failure,
                "coherence_summary": dict(native)}
    evidence_ref = _write_evidence(session, evidence, mode)
    summary["elapsed_seconds"] = round(time.monotonic() - clock_started, 3)
    # On the failure path `rows` carries prior rows plus attempt
    # accounting only, so persisting stays conservative.
    session.snapshot["actions_state"] = rows
    history = list(session.snapshot.get("actions_history") or [])
    history.append({"at": utcnow(), "mode": mode,
                    "summary": dict(summary), "evidence_ref": evidence_ref})
    session.snapshot["actions_history"] = history[-MAX_HISTORY:]
    if mode == "ambient" and failure is None:
        stored_epoch = epoch
    else:
        stored_epoch = f"uncached:{mode}:{epoch}"
    session.snapshot["actions_epoch"] = {
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
    ref = f"actions-{mode}-{digest_hex(evidence['finished_at'])[:12]}.json"
    path = directory / ref
    try:
        path.write_text(json.dumps(evidence, indent=2, sort_keys=True),
                        encoding="utf-8")
    except OSError:
        return "actions-evidence-unwritable"
    return f"sessions/{session.session_id}/actions-artifacts/evidence/{ref}"


def _append_session_evidence(session, summary: dict[str, Any],
                             results: list[dict], mode: str) -> None:
    entry = {
        "schema_version": EVIDENCE_SCHEMA,
        "session": session.session_id,
        "consumer": session.snapshot.get("consumer_id"),
        "mode": mode,
        "summary": {key: summary.get(key) for key in
                    ("obligations", "current", "pending", "eligible",
                     "dispatched", "deferred", "unsupported", "escalated",
                     "blockers")},
        "externals": [
            {"obligation": record.get("obligation"),
             "repository": record.get("repository"),
             "outcome": record.get("outcome"),
             "status": record.get("status"),
             "reason": record.get("reason"),
             "operation": record.get("operation"),
             "evidence_id": record.get("evidence_id"),
             "run_identity": record.get("run_identity"),
             "verdict": record.get("verdict")}
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
    """Compact external-evidence status for normal agent context."""
    stored = session.snapshot.get("actions_epoch") or {}
    summary = dict(stored.get("summary") or {})
    return {"external_evidence": {
        "current": summary.get("current", 0),
        "pending": summary.get("pending", 0),
        "eligible": summary.get("eligible", 0),
        "escalated": summary.get("escalated", 0),
        "blockers": summary.get("blockers", 0),
        "capsule_ids": summary.get("capsule_ids", []),
        "delegate_requests": summary.get("delegate_requests", []),
        "evidence": stored.get("evidence_ref")}}


def read_evidence(session) -> dict[str, Any]:
    """Full external-evidence trail for explicit inspection."""
    stored = session.snapshot.get("actions_epoch") or {}
    ref = stored.get("evidence_ref", "")
    if not ref or not ref.startswith("sessions/"):
        return {"evidence": None, "history": session.snapshot.get(
            "actions_history") or []}
    path = (session.store.state_dir / ref).resolve()
    try:
        if not path.is_relative_to(session.store.state_dir.resolve()):
            raise OSError("evidence escapes state root")
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        evidence = None
    return {"evidence": evidence, "history": session.snapshot.get(
        "actions_history") or []}


def external_admission(rows: dict[str, dict[str, Any]],
                       identity: str) -> dict[str, Any] | None:
    """Project a current actions row onto the verification admission shape.

    Returns None unless the row holds current admitted evidence. The
    verification policy re-checks the identity binding natively; this
    projection never fabricates evidence, it only restates recorded
    rows for the cross-module handoff.
    """
    row = rows.get(identity)
    if not isinstance(row, dict):
        return None
    evidence = row.get("evidence")
    if not isinstance(evidence, dict):
        return None
    if evidence.get("claim") != "established" or evidence.get("complete") is not True:
        return None
    if not evidence.get("subject_digest") or not row.get("evidence_id"):
        return None
    if row.get("subject_digest") != evidence.get("subject_digest"):
        return None
    verdict = {"passed": "PASS", "failed": "FAIL",
               "unknown": "UNKNOWN"}.get(str(evidence.get("verdict")))
    if verdict is None:
        return None
    return {"obligation": identity,
            "subject_digest": str(evidence.get("subject_digest")),
            "verdict": verdict,
            "evidence_id": str(row.get("evidence_id"))}


def external_epoch_material(session) -> dict[str, str]:
    """Identity digests of current external evidence for epoch inputs."""
    rows = _state_rows(session)
    material = {}
    for identity, row in rows.items():
        admission = external_admission(rows, str(identity))
        if admission is not None:
            material[str(identity)] = digest_hex(admission)
    return material
