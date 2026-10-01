"""Ambient Doctor: validated health epochs, safe remediation, terse summaries.

The ambient pass answers one question cheaply -- "is the last validated
world still the current world?" -- and runs expensive revalidation only
when the answer is provably no. Every input that feeds readiness is
fingerprinted exactly (checkout revisions and dirty content, declaration
files, toolchain presence, binding availability, claims, event count);
any mismatch, any unreadable input, or any doubt invalidates the epoch
and the full path runs. An epoch hit performs no writes and emits no
events beyond the caller's own resume marker.

Ambient remediation mutates only the calling session's own snapshot,
events, and artifact directory. It never touches another session,
another consumer's claims, repositories, or worktrees. Repository
content remediation is explicit only (`remediate_repository`), requires
a live session claim on the target scope, and refuses unknown work.

Summaries are terse by construction (`repaired/reconciled/degraded/
blockers` plus escalation ids). Full evidence lives in the session
artifact directory and is retrievable on demand, never forced into the
working context.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import capabilities as capabilities_module
from . import claims as claims_module
from . import readiness as readiness_module
from . import workspace as workspace_module
from .identity import digest_hex
from .persist import read_json, write_json

DOCTOR_SCHEMA = "mncs.environment.doctor/2"
EPOCH_SCHEMA = "mncs.environment.doctor-epoch/2"
EVIDENCE_SCHEMA = "mncs.environment.doctor-evidence/1"

#: File-side epoch freshness for the no-store fast path (`status --terse`).
FILESIDE_EPOCH_TTL_SECONDS = 60

#: Bound on retained per-session doctor history entries.
MAX_HISTORY = 10

#: Bound on escalation ids carried in a terse summary.
MAX_REMAINING = 32

#: Bound on invalidation reasons reported for one validation.
MAX_REASONS = 16

#: Suffix identifying provider-published remediation capabilities.
REMEDIATION_CAPABILITY_SUFFIX = ":repository-remediation"

#: Schema published by remediation providers on stdout. Family-standard
#: contract owned by MNCS-Commons (`mncs.remediation/1`); the repository
#: domain is the only v1 domain.
REMEDIATION_ENVELOPE_SCHEMA = "mncs.remediation/1"
REMEDIATION_REPOSITORY_DOMAIN = "repository"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _session_directory(session) -> Path:
    return Path(session.store.state_dir).expanduser().resolve() / "sessions" / session.session_id


def _epoch_file(session) -> Path:
    return _session_directory(session) / "doctor-epoch.json"


def _evidence_file(session) -> Path:
    return _session_directory(session) / "doctor-evidence.json"


def _artifact_directory(session, capability: str) -> Path:
    return (
        _session_directory(session).resolve() / "artifacts" / digest_hex(capability)
    )


# -- epoch inputs --------------------------------------------------------

def _repo_paths(session) -> dict[str, str]:
    """Session-relevant checkout paths, mirroring revalidation scope.

    Selected sessions track exactly their selected checkouts; unselected
    sessions track every observed repository record (including managed
    `repo@checkout` worktrees, which carry their own paths).
    """
    selected = session.snapshot.get("selected_checkouts", {})
    if isinstance(selected, dict) and selected:
        root = session.snapshot.get("workspace", {}).get("root", "")
        paths = {}
        for name in sorted(selected):
            raw = Path(str(selected[name].get("path", "")))
            if not raw.is_absolute() and root:
                raw = Path(str(root)) / raw
            paths[str(name)] = str(raw)
        return paths
    paths = {}
    for repo in session.snapshot.get("workspace", {}).get("repositories", []):
        if repo.get("name") and repo.get("path"):
            paths[str(repo["name"])] = str(repo["path"])
    return paths


def _toolchain_facts(session) -> dict[str, Any]:
    """Recompute the toolchain presence facts revalidation would set."""
    toolchain = session.snapshot.get("toolchain")
    if not isinstance(toolchain, dict):
        return {"present": False}
    checkout = Path(str(toolchain.get("checkout", "")))
    candidates = (checkout / "target" / "release" / "mncs", checkout / "target" / "debug" / "mncs")
    binary = next((path for path in candidates if path.is_file()), None)
    language = session.snapshot.get("selected_checkouts", {}).get("mncs-language", {})
    revision = language.get("head", toolchain.get("revision")) if isinstance(language, dict) else toolchain.get("revision")
    return {"present": True, "checkout": str(checkout),
            "binary": str(binary) if binary else None,
            "status": "available" if binary else "unavailable", "revision": revision}


def _availability_vector(session) -> dict[str, str]:
    """Fresh substrate availability over the snapshot bindings (read-only)."""
    vector = {}
    for binding in session.snapshot.get("bindings", []):
        fresh = capabilities_module.probe_availability(binding)
        availability = fresh.get("availability", {})
        vector[str(binding.get("capability"))] = (
            f"{availability.get('status')}:{availability.get('code')}"
        )
    return vector


def _claims_digest(session) -> str:
    try:
        live = claims_module.active_claims(session.store.read_claims())
    except Exception:
        return "unreadable"
    return digest_hex({key: live[key] for key in sorted(live)})


def _event_count(session) -> int | str:
    try:
        return len(session._log())
    except Exception:
        return "unreadable"


def epoch_inputs(session) -> dict[str, Any]:
    """Fingerprint every input that feeds this session's readiness."""
    repos: dict[str, Any] = {}
    for name, raw_path in _repo_paths(session).items():
        path = Path(raw_path)
        facts = workspace_module.quick_repo_facts(path)
        repos[name] = {
            "path": str(path),
            "facts": facts,
            "manifests": workspace_module.manifest_content_digest(path),
            "worktrees": workspace_module.managed_worktree_names(path),
        }
    membership = None
    if not session.snapshot.get("selected_checkouts"):
        root = session.snapshot.get("workspace", {}).get("root")
        if root:
            membership = workspace_module.top_level_checkout_names(Path(str(root)))
    return {
        "definition_id": session.snapshot.get("provenance", {}).get("definition_id"),
        "workspace_root": session.snapshot.get("workspace", {}).get("root"),
        "consumer_id": session.snapshot.get("consumer_id"),
        "lifecycle": session.snapshot.get("lifecycle"),
        "repos": repos,
        "membership": membership,
        "toolchain": _toolchain_facts(session),
        "availability": _availability_vector(session),
        "claims": _claims_digest(session),
        "event_count": _event_count(session),
    }


def epoch_digest(inputs: dict[str, Any]) -> str:
    return digest_hex({"kind": "doctor-epoch", "inputs": inputs})


# -- epoch validation ----------------------------------------------------

def _trailing_own_resume(session, baseline: int) -> bool:
    """Whether the log grew by exactly our own resume marker since baseline.

    `Session.resume` appends one `session.resumed` event before the ambient
    pass runs. That marker is the caller's own participation evidence, not
    external change, so it must not invalidate an otherwise valid epoch.
    Anything else appended (a peer, a handoff, a second marker) invalidates.
    """
    try:
        log = session._log()
    except Exception:
        return False
    if len(log) != baseline + 1:
        return False
    last = log[-1]
    return (
        last.get("type") == "session.resumed"
        and isinstance(last.get("payload"), dict)
        and last["payload"].get("consumer_id") == session.snapshot.get("consumer_id")
        and "previous_consumer" not in last["payload"]
        and "handoff_id" not in last["payload"]
    )


def validate_epoch(session, epoch: dict[str, Any]) -> tuple[bool, list[str]]:
    """Compare live inputs against a recorded epoch.

    Returns (valid, reasons). Any unreadable input invalidates: doubt
    always runs the full path. Reasons are short stable codes naming the
    first divergences found.
    """
    reasons: list[str] = []

    def note(reason: str) -> None:
        if len(reasons) < MAX_REASONS:
            reasons.append(reason)

    if not isinstance(epoch, dict) or epoch.get("schema_version") != EPOCH_SCHEMA:
        return False, ["epoch-schema"]
    recorded = epoch.get("inputs", {})
    live = epoch_inputs(session)
    for key in ("definition_id", "workspace_root", "consumer_id", "lifecycle"):
        if recorded.get(key) != live.get(key):
            note(key)
    recorded_repos = recorded.get("repos", {})
    live_repos = live.get("repos", {})
    for name in sorted(set(recorded_repos) | set(live_repos)):
        old = recorded_repos.get(name)
        new = live_repos.get(name)
        if old is None:
            note(f"repo-added:{name}")
            continue
        if new is None:
            note(f"repo-removed:{name}")
            continue
        if old.get("path") != new.get("path"):
            note(f"repo-moved:{name}")
            continue
        if new.get("facts") is None:
            note(f"repo-unreadable:{name}")
            continue
        # Facts, declarations, and managed worktrees are independent
        # dimensions: report every divergence, not just the first.
        if old.get("facts") != new.get("facts"):
            note(f"repo-changed:{name}")
        if old.get("manifests") != new.get("manifests"):
            note(f"manifests:{name}")
        if old.get("worktrees") != new.get("worktrees"):
            note(f"worktrees:{name}")
    if recorded.get("membership") != live.get("membership"):
        note("membership")
    if recorded.get("toolchain") != live.get("toolchain"):
        note("toolchain")
    old_availability = recorded.get("availability", {})
    new_availability = live.get("availability", {})
    if old_availability != new_availability:
        flipped = sum(
            1 for key in set(old_availability) | set(new_availability)
            if old_availability.get(key) != new_availability.get(key)
        )
        note(f"availability:{flipped}")
    if recorded.get("claims", "") != live.get("claims", ""):
        note("claims")
    baseline = recorded.get("event_count")
    current = live.get("event_count")
    if baseline != current and not (
        isinstance(baseline, int) and _trailing_own_resume(session, baseline)
    ):
        note("events")
    _ = current
    return (not reasons), reasons


def probe_signature(observations: list[dict[str, Any]]) -> dict[str, Any]:
    """Stable comparison key for service observations.

    Identity, status, code, predicate actuals, and a provider-diagnostics
    digest. Timestamps are excluded: they always advance and never signal
    a state change.
    """
    signature = {}
    for item in observations or []:
        diagnostics = item.get("provider_diagnostics")
        signature[str(item.get("identity"))] = {
            "status": item.get("status"),
            "code": item.get("code"),
            "observation": item.get("observation"),
            "diagnostics": digest_hex(diagnostics) if diagnostics is not None else None,
        }
    return signature


def live_services_match(session) -> tuple[bool, list[dict[str, Any]]]:
    """Re-probe declared services read-only and compare against the snapshot.

    Provider runtime state (processes, state files, sockets) is observable
    only by probing: no static fingerprint can cover it. The epoch therefore
    never stands in for service probes; a hit requires the live probes to
    agree with the validated snapshot.
    """
    fresh = readiness_module.probe_services(session)
    return probe_signature(fresh) == probe_signature(
        session.snapshot.get("service_observations", [])), fresh


# -- classification ------------------------------------------------------

def classify_unavailable(bindings: list[dict[str, Any]]) -> dict[str, Any]:
    """Split unavailable bindings into transient and provider-gap classes.

    `executable-unavailable` / `toolchain-missing` may be transient
    substrate loss (worth one targeted re-probe); anything else names a
    provider declaration gap the session cannot repair.
    """
    transient = []
    gaps = []
    for binding in bindings:
        availability = binding.get("availability", {})
        if availability.get("status") == "available":
            continue
        entry = {"capability": binding.get("capability"),
                 "code": availability.get("code"), "reason": availability.get("reason")}
        if availability.get("code") in ("executable-unavailable", "toolchain-missing"):
            transient.append(entry)
        else:
            gaps.append(entry)
    return {"transient": transient, "gaps": gaps}


def summarize(snapshot: dict[str, Any], *, repairs: list[dict[str, Any]],
              reconciliations: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the terse doctor summary over a readiness snapshot.

    `remaining` carries only blocking (actionable) ids. The
    non-blocking unavailable tail is stable background already visible
    in readiness; it travels as a count plus a change digest, with full
    per-capability detail in the evidence classification.
    """
    readiness = readiness_module.summarize(snapshot)
    bindings = snapshot.get("bindings", [])
    unavailable = [item for item in bindings
                   if item.get("availability", {}).get("status") != "available"]
    blocking = list(readiness.get("blocking", []))
    unavailable_ids = sorted({str(item.get("capability")) for item in unavailable
                              if item.get("capability")})
    truncated = len(blocking) > MAX_REMAINING
    return {
        "schema_version": DOCTOR_SCHEMA,
        "summary": {"repaired": len(repairs), "reconciled": len(reconciliations),
                    "degraded": len(unavailable_ids), "blockers": len(blocking)},
        "remaining": blocking[:MAX_REMAINING],
        "remaining_truncated": truncated,
        "unavailable": {"count": len(unavailable_ids),
                        "digest": digest_hex({"kind": "unavailable-capabilities",
                                              "capabilities": unavailable_ids})},
        "readiness": readiness.get("status"),
    }


# -- epoch recording -----------------------------------------------------

def repair_delta(previous: list[dict[str, Any]], current: list[dict[str, Any]],
                  previous_services: list[dict[str, Any]],
                  current_services: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Safe-automatic repairs evidenced by before/after availability."""
    repairs = []
    before = {item.get("capability"): item.get("availability", {}).get("status")
              for item in previous}
    for item in current:
        capability = item.get("capability")
        if before.get(capability) != "available" and item.get("availability", {}).get("status") == "available":
            repairs.append({"id": f"binding:available:{capability}", "class": "safe_automatic",
                            "provider": str(item.get("provider")),
                            "detail": "capability substrate recovered; re-probe confirms availability",
                            "validated": True})
    before_services = {item.get("identity"): item.get("status") for item in previous_services}
    for item in current_services:
        identity = item.get("identity")
        if before_services.get(identity) != "ready" and item.get("status") == "ready":
            repairs.append({"id": f"service:ready:{identity}", "class": "safe_automatic",
                            "provider": "mncs-environment",
                            "detail": "declared service reconciled to ready and re-probed",
                            "validated": True})
    return repairs


def record_epoch(session, *, repairs: list[dict[str, Any]],
                 reconciliations: list[dict[str, Any]],
                 operations: list[dict[str, Any]] | None = None,
                 revalidation: dict[str, Any] | None = None,
                 reused: bool = False) -> dict[str, Any]:
    """Validate the current world, persist the epoch, and return the report.

    The epoch (inputs digest + terse summary + history) is stored in the
    snapshot; full evidence goes to the session-side evidence file. Both
    are derived state: deleting them only costs one full pass.
    """
    terse = summarize(session.snapshot, repairs=repairs, reconciliations=reconciliations)
    now = utcnow()
    # The remediation marker is emitted BEFORE the inputs fingerprint is
    # taken, so the recorded event baseline already includes our own write.
    # Emitting after would invalidate the just-recorded epoch on every pass.
    session._emit("doctor.remediated", "environment",
                  {"summary": terse["summary"],
                   "remaining": terse["remaining"][:8],
                   "remaining_count": len(terse["remaining"]), "reused": reused})
    inputs = epoch_inputs(session)
    digest = epoch_digest(inputs)
    epoch = {"schema_version": EPOCH_SCHEMA, "digest": digest, "inputs": inputs,
             "summary": terse["summary"], "remaining": terse["remaining"],
             "remaining_truncated": terse["remaining_truncated"],
             "unavailable": terse["unavailable"],
             "readiness": terse["readiness"], "validated_at": now,
             "snapshot_sequence": int(session.snapshot.get("snapshot_sequence", 0)) + 1}
    doctor = session.snapshot.get("doctor")
    if not isinstance(doctor, dict):
        doctor = {"schema_version": DOCTOR_SCHEMA, "history": []}
    history = list(doctor.get("history", []))
    history.append({"validated_at": now, "digest": digest, "summary": terse["summary"],
                    "remaining": terse["remaining"], "unavailable": terse["unavailable"],
                    "reused": reused})
    doctor.update({"schema_version": DOCTOR_SCHEMA, "epoch": epoch,
                   "history": history[-MAX_HISTORY:]})
    session.snapshot["doctor"] = doctor
    session._save()
    fileside = {"schema_version": EPOCH_SCHEMA, "digest": digest,
                "session_id": session.session_id,
                "workspace_root": inputs.get("workspace_root"),
                "repos": {name: {"path": info.get("path"), "facts": info.get("facts"),
                                 "manifests": info.get("manifests"),
                                 "worktrees": info.get("worktrees")}
                          for name, info in inputs.get("repos", {}).items()},
                "membership": inputs.get("membership"),
                "summary": terse["summary"], "remaining": terse["remaining"],
                "remaining_truncated": terse["remaining_truncated"],
                "unavailable": terse["unavailable"],
                "readiness": terse["readiness"], "validated_at": now}
    write_json(_epoch_file(session), fileside)
    evidence = {"schema_version": EVIDENCE_SCHEMA, "session_id": session.session_id,
                "digest": digest, "validated_at": now, "reused": reused,
                "summary": terse["summary"], "remaining": terse["remaining"],
                "repairs": repairs, "reconciliations": reconciliations,
                "classification": classify_unavailable(session.snapshot.get("bindings", [])),
                "operations": operations or [], "revalidation": revalidation or {},
                "readiness": readiness_module.summarize(session.snapshot)}
    write_json(_evidence_file(session), evidence)
    return {"digest": digest, "validated_at": now, "summary": terse["summary"],
            "remaining": terse["remaining"],
            "remaining_truncated": terse["remaining_truncated"],
            "unavailable": terse["unavailable"],
            "readiness": terse["readiness"], "reused": reused}


def terse(session) -> dict[str, Any]:
    """Current terse doctor state from the snapshot (read-only)."""
    doctor = session.snapshot.get("doctor", {})
    epoch = doctor.get("epoch", {})
    return {"summary": epoch.get("summary",
                                 {"repaired": 0, "reconciled": 0, "degraded": 0, "blockers": 0}),
            "remaining": epoch.get("remaining", []),
            "remaining_truncated": epoch.get("remaining_truncated", False),
            "unavailable": epoch.get("unavailable", {"count": 0, "digest": None}),
            "readiness": epoch.get("readiness"),
            "epoch": epoch.get("digest"), "validated_at": epoch.get("validated_at")}


def evidence(session) -> dict[str, Any]:
    """Full evidence trail: snapshot history plus the evidence artifact."""
    doctor = session.snapshot.get("doctor", {})
    payload = read_json(_evidence_file(session))
    return {"session_id": session.session_id, "history": doctor.get("history", []),
            "epoch": doctor.get("epoch", {}).get("digest"),
            "artifact": str(_evidence_file(session)),
            "evidence": payload if isinstance(payload, dict) else None}


# -- ambient pass ----------------------------------------------------------

def ambient_pass(session, *, force_full: bool = False, fresh: bool = False,
                 upgraded_store: bool = False, lock_waited: float = 0.0) -> dict[str, Any]:
    """Run the ambient remediation pass over a resumed session.

    Epoch hit: no writes and no revalidation; declared services are still
    probed live (read-only) because provider runtime state is observable
    only by probing. When the probes disagree with the snapshot, or a
    service still needs recovery, services are reconciled without a full
    revalidation. Epoch miss: full revalidate + service reconcile, then
    the new epoch is recorded. Returns entry-shaped revalidation and
    operations plus the terse doctor report.
    """
    started = time.monotonic()
    previous_bindings = [dict(binding) for binding in session.snapshot.get("bindings", [])]
    previous_services = [dict(item) for item in session.snapshot.get("service_observations", [])]
    reconciliations: list[dict[str, Any]] = []
    if lock_waited >= 0.5:
        reconciliations.append({"id": "entry-lock:waited", "class": "bounded_reconciliation",
                                "provider": "mncs-environment",
                                "detail": f"waited {lock_waited:.1f}s for a concurrent entry to finish; lock was never seized",
                                "validated": True})
    if upgraded_store:
        reconciliations.append({"id": "store-provider:upgraded", "class": "bounded_reconciliation",
                                "provider": "mncs-environment",
                                "detail": "session Store bootstrap metadata upgraded to the current schema",
                                "validated": True})
    epoch = session.snapshot.get("doctor", {}).get("epoch") if isinstance(
        session.snapshot.get("doctor"), dict) else None
    if not force_full and not fresh and isinstance(epoch, dict):
        valid, reasons = validate_epoch(session, epoch)
        if valid:
            services_match, _ = live_services_match(session)
            all_ready = all(item.get("status") == "ready"
                            for item in session.snapshot.get("service_observations", []))
            if not (services_match and all_ready):
                result = readiness_module.reconcile_services(session)
                repairs = repair_delta(previous_bindings, session.snapshot.get("bindings", []),
                                        previous_services,
                                        session.snapshot.get("service_observations", []))
                reconciliations.append({"id": "services:reconciled",
                                        "class": "bounded_reconciliation",
                                        "provider": "mncs-environment",
                                        "detail": ("live service probes disagree with the validated "
                                                   "snapshot; reconciled services without revalidation")
                                        if not services_match else
                                        "a declared service still needs recovery; re-attempted without revalidation",
                                        "validated": True})
                report = record_epoch(session, repairs=repairs, reconciliations=reconciliations,
                                      operations=result["operations"])
                report["revalidation"] = {"reprobed": 0, "changed": [], "bound": [],
                                          "unbound": [], "epoch": epoch.get("digest"),
                                          "note": "epoch inputs valid; services reconciled"}
                report["operations"] = result["operations"]
                report["elapsed_seconds"] = round(time.monotonic() - started, 3)
                return report
            report = {"digest": epoch.get("digest"), "validated_at": epoch.get("validated_at"),
                      "summary": epoch.get("summary"), "remaining": epoch.get("remaining", []),
                      "remaining_truncated": epoch.get("remaining_truncated", False),
                      "unavailable": epoch.get("unavailable", {"count": 0, "digest": None}),
                      "readiness": epoch.get("readiness"), "reused": True,
                      "revalidation": {"reprobed": 0, "changed": [], "bound": [],
                                       "unbound": [], "epoch": epoch.get("digest")},
                      "operations": [],
                      "elapsed_seconds": round(time.monotonic() - started, 3)}
            return report
        reconciliations.append({"id": "revalidate:full", "class": "bounded_reconciliation",
                                "provider": "mncs-environment",
                                "detail": f"epoch invalid ({', '.join(reasons) or 'no epoch'}); running full revalidation",
                                "validated": False})
    elif fresh:
        reconciliations.append({"id": "revalidate:skipped-fresh", "class": "bounded_reconciliation",
                                "provider": "mncs-environment",
                                "detail": "fresh session: bindings just discovered; skipping redundant revalidation",
                                "validated": True})
    else:
        reconciliations.append({"id": "revalidate:full", "class": "bounded_reconciliation",
                                "provider": "mncs-environment",
                                "detail": "no validated epoch; running full revalidation",
                                "validated": False})
    if fresh:
        revalidation: dict[str, Any] = {"reprobed": 0, "changed": [], "bound": [],
                                        "unbound": [], "note": "fresh session"}
    else:
        revalidation = session.revalidate()
    result = readiness_module.reconcile_services(session)
    repairs = repair_delta(previous_bindings, session.snapshot.get("bindings", []),
                            previous_services, session.snapshot.get("service_observations", []))
    for entry in reconciliations:
        if entry["id"] == "revalidate:full":
            entry["validated"] = True
            entry["detail"] += (f"; reprobed {revalidation.get('reprobed', 0)}, "
                                f"{len(revalidation.get('changed', []))} availability changes")
    report = record_epoch(session, repairs=repairs, reconciliations=reconciliations,
                          operations=result["operations"], revalidation=revalidation)
    report["revalidation"] = revalidation
    report["operations"] = result["operations"]
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return report


# -- no-store fast path ----------------------------------------------------

def read_fileside_epoch(state_dir: Path | str, session_id: str) -> dict[str, Any] | None:
    payload = read_json(Path(state_dir).expanduser().resolve() / "sessions" / session_id / "doctor-epoch.json")
    if not isinstance(payload, dict) or payload.get("schema_version") != EPOCH_SCHEMA:
        return None
    if payload.get("session_id") != session_id:
        return None
    return payload


def validate_fileside_epoch(record: dict[str, Any]) -> tuple[bool, str]:
    """Validate a file-side epoch without opening the Store.

    Recomputes the git and declaration facts over the recorded paths. Any
    mismatch, unreadable input, or expired TTL invalidates. Claims and the
    event log cannot be checked without the Store, so a hit is TTL-bound
    and labeled `epoch`: honest, read-only, and at most a minute stale.
    """
    try:
        moment = datetime.fromisoformat(str(record.get("validated_at")))
    except (TypeError, ValueError):
        return False, "unparsable timestamp"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    if (datetime.now(timezone.utc) - moment).total_seconds() > FILESIDE_EPOCH_TTL_SECONDS:
        return False, "ttl expired"
    repos = record.get("repos", {})
    if not isinstance(repos, dict):
        return False, "unparsable repos"
    for name in sorted(repos):
        info = repos[name]
        path = Path(str(info.get("path", "")))
        facts = workspace_module.quick_repo_facts(path)
        if facts is None or facts != info.get("facts"):
            return False, f"repo-changed:{name}"
        if workspace_module.manifest_content_digest(path) != info.get("manifests"):
            return False, f"manifests:{name}"
        if workspace_module.managed_worktree_names(path) != info.get("worktrees"):
            return False, f"worktrees:{name}"
    root = record.get("workspace_root")
    if root and record.get("membership") is not None:
        if workspace_module.top_level_checkout_names(Path(str(root))) != record.get("membership"):
            return False, "membership"
    return True, "epoch valid"


def serve_terse_fast(state_dir: Path | str, session_id: str) -> dict[str, Any] | None:
    """Serve the terse summary without opening the Store, when valid."""
    record = read_fileside_epoch(state_dir, session_id)
    if record is None:
        return None
    valid, _ = validate_fileside_epoch(record)
    if not valid:
        return None
    return {"session_id": session_id, "observation": "epoch",
            "epoch": record.get("digest"), "validated_at": record.get("validated_at"),
            "summary": record.get("summary"), "remaining": record.get("remaining", []),
            "remaining_truncated": record.get("remaining_truncated", False),
            "unavailable": record.get("unavailable", {"count": 0, "digest": None}),
            "readiness": record.get("readiness")}


# -- explicit repository remediation -----------------------------------------

class RemediationRefused(Exception):
    """Raised when repository remediation is unsafe or unauthorized."""

    def __init__(self, message: str, code: str, **details: Any):
        super().__init__(message)
        self.diagnostics = {"code": code, **details}


def _remediation_binding(session) -> dict[str, Any]:
    for binding in session.snapshot.get("bindings", []):
        if str(binding.get("capability", "")).endswith(REMEDIATION_CAPABILITY_SUFFIX):
            if binding.get("availability", {}).get("status") != "available":
                raise RemediationRefused(
                    f"remediation provider {binding.get('capability')} is not available: "
                    f"{binding.get('availability', {}).get('reason')}",
                    "remediation-provider-unavailable",
                    capability=binding.get("capability"),
                    next="build the provider binary or revalidate the session")
            return binding
    raise RemediationRefused("no bound repository-remediation provider capability",
                             "remediation-provider-missing",
                             next="select a checkout publishing a repository-remediation contract")


def _target_checkout(session, repository: str) -> dict[str, Any]:
    selected = session.snapshot.get("selected_checkouts", {})
    if isinstance(selected, dict) and repository in selected:
        record = dict(selected[repository])
        path = Path(str(record.get("path", "")))
        if not path.is_absolute():
            root = session.snapshot.get("workspace", {}).get("root", "")
            path = Path(str(root)) / path
        record["resolved_path"] = str(path.resolve())
        return record
    for repo in session.snapshot.get("workspace", {}).get("repositories", []):
        if repo.get("name") == repository or repo.get("manifest_repository") == repository:
            return {"resolved_path": str(Path(str(repo["path"])).resolve()),
                    "branch": repo.get("branch"), "head": repo.get("head")}
    raise RemediationRefused(f"repository {repository} is not in this session",
                             "remediation-scope-unknown", repository=repository)


def _covering_claim(session, repository: str, checkout_path: str) -> dict[str, Any]:
    """Require a live whole-checkout claim held by this session."""
    live = claims_module.active_claims(session.store.read_claims())
    for record in live.values():
        if record.get("session_id") != session.session_id:
            continue
        if str(record.get("repository", "")) != repository:
            continue
        scope = record.get("scope", {})
        if not isinstance(scope, dict):
            continue
        if scope.get("kind") == "repository":
            return record
        if scope.get("kind") == "worktree":
            try:
                if Path(str(scope.get("checkout", ""))).resolve() == Path(checkout_path).resolve():
                    return record
            except OSError:
                continue
    raise RemediationRefused(
        f"session holds no whole-checkout claim on {repository}; remediation refuses unclaimed trees",
        "remediation-claim-required", repository=repository,
        next=f"acquire a claim on {repository} before remediating it")


def remediate_repository(session, repository: str, *, dry_run: bool = False,
                         changed_paths: list[str] | None = None,
                         timeout_seconds: int = 180) -> dict[str, Any]:
    """Invoke the bound remediation provider over one claimed checkout.

    Explicit only: ambient passes never touch repository content. Requires
    a live whole-checkout claim held by this session and refuses checkouts
    showing unknown work unless the claim records explicit adoption.
    """
    binding = _remediation_binding(session)
    target = _target_checkout(session, repository)
    checkout = Path(target["resolved_path"])
    if not checkout.is_dir():
        raise RemediationRefused(f"checkout for {repository} is missing: {checkout}",
                                 "remediation-checkout-missing", repository=repository)
    claim = _covering_claim(session, repository, str(checkout))
    observed = workspace_module.inspect_repo(checkout)
    if observed is None:
        raise RemediationRefused(f"checkout for {repository} is not git-readable",
                                 "remediation-checkout-unreadable", repository=repository)
    if observed.dirty and claim.get("basis") not in claims_module.ADOPTION_BASES:
        raise claims_module.ClaimAdoptionRequired(
            f"{repository} checkout shows unknown work and the session claim does not record adoption; "
            "remediation refuses to rewrite it",
            facts={"dirty": observed.dirty, "head": observed.head, "branch": observed.branch})
    argv = ["--target", str(checkout), "--json"]
    if dry_run:
        argv.append("--dry-run")
    for changed in changed_paths or []:
        if not isinstance(changed, str) or not changed or changed.startswith("-"):
            raise RemediationRefused(f"invalid changed path: {changed!r}",
                                     "remediation-changed-path-invalid", repository=repository)
        argv.extend(["--changed-path", changed])
    artifact_directory = _artifact_directory(session, str(binding.get("capability")))
    artifact_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    result = session.invoke(str(binding.get("capability")), argv, timeout_seconds=timeout_seconds,
                            output_limit_bytes=64 * 1024)
    # Envelope over exit codes: a provider that ran to completion reports
    # findings, escalations, and even verification failure inside its
    # envelope. Only a missing or unparsable envelope is a refusal.
    try:
        envelope = json.loads(str(result.get("stdout", "")))
    except json.JSONDecodeError as error:
        raise RemediationRefused(
            f"remediation provider returned no parsable envelope ({result.get('status')}): "
            f"{str(result.get('stderr', ''))[-1000:]}",
            "remediation-envelope-invalid", repository=repository) from error
    if not isinstance(envelope, dict) or envelope.get("schema_version") != REMEDIATION_ENVELOPE_SCHEMA:
        raise RemediationRefused("remediation provider returned an incompatible envelope",
                                 "remediation-envelope-incompatible", repository=repository,
                                 schema=envelope.get("schema_version") if isinstance(envelope, dict) else None,
                                 status=result.get("status"))
    scope = envelope.get("scope")
    if not isinstance(scope, dict) or scope.get("domain") != REMEDIATION_REPOSITORY_DOMAIN:
        raise RemediationRefused("remediation provider addressed an unsupported scope domain",
                                 "remediation-scope-unsupported", repository=repository,
                                 scope=scope if isinstance(scope, dict) else None)
    echoed = scope.get("target")
    if not isinstance(echoed, str) or Path(echoed).resolve() != checkout.resolve():
        raise RemediationRefused("remediation provider scope echo does not match the authorized checkout",
                                 "remediation-envelope-invalid", repository=repository,
                                 scope_target=echoed if isinstance(echoed, str) else None)
    session._emit("doctor.repository-remediated", "environment",
                  {"repository": repository, "dry_run": dry_run,
                   "changed_paths": list(changed_paths or []),
                   "summary": envelope.get("summary"), "remaining": envelope.get("remaining", [])[:8]})
    doctor = session.snapshot.get("doctor")
    if not isinstance(doctor, dict):
        doctor = {"schema_version": DOCTOR_SCHEMA, "history": []}
    history = list(doctor.get("history", []))
    history.append({"validated_at": utcnow(), "repository": repository, "dry_run": dry_run,
                    "summary": envelope.get("summary"), "remaining": envelope.get("remaining", []),
                    "evidence": envelope.get("evidence"),
                    "exit_code": envelope.get("exit_code"),
                    "provider_status": result.get("status")})
    doctor.update({"schema_version": DOCTOR_SCHEMA, "history": history[-MAX_HISTORY:]})
    session.snapshot["doctor"] = doctor
    session._save()
    return {"repository": repository, "dry_run": dry_run,
            "summary": envelope.get("summary"), "remaining": envelope.get("remaining", []),
            "evidence": envelope.get("evidence"),
            "exit_code": envelope.get("exit_code"), "exit_meaning": envelope.get("exit_meaning"),
            "provider_status": result.get("status")}
