"""On-demand environmental pressure projection over durable campaign evidence.

The campaign ledger remains the evidence authority. This module resolves only
bounded, content-addressed observation records from the selected workspace,
then projects their relationship to the current session and work intent. It
does not infer missing measurements or claim that a proposed improvement
worked.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path, PurePosixPath
from typing import Any

from . import workspace as workspace_module
from .identity import digest_hex
from .sessions import LifecycleError
from .store_backend import StoreIntegrityFailure, StoreUnavailable

SCHEMA = "mncs.environment.capability-intelligence/1"
OBSERVATION_SCHEMA = "mncs.environment.environmental-observation/1"
OBSERVATION_PATH_PREFIX = "evidence/environmental-capability/"
MAX_EVIDENCE_REFS = 32
MAX_OBSERVATIONS = 16
MAX_OBSERVATION_BYTES = 64 * 1024
MAX_OBSERVATION_READ_SECONDS = 8.0
MAX_RESULT_BYTES = 48 * 1024
_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _workspace_root(session: Any) -> Path | None:
    raw = (session.snapshot.get("workspace") or {}).get("root")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return Path(raw).resolve(strict=True)
    except OSError:
        return None


def _checkout(session: Any, repository: str, root: Path) -> Path | None:
    selected = session.snapshot.get("selected_checkouts") or {}
    entry = selected.get(repository) if isinstance(selected, dict) else None
    if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
        return None
    raw = Path(entry["path"])
    if not raw.is_absolute():
        raw = root / raw
    try:
        path = raw.resolve(strict=True)
        path.relative_to(root)
    except (OSError, ValueError):
        return None
    if not path.is_dir() or path.is_symlink():
        return None
    return path


def _validate_reference(reference: Any) -> dict[str, str] | None:
    if not isinstance(reference, dict):
        return None
    identity = reference.get("identity")
    repository = reference.get("repository")
    commit = reference.get("commit")
    path = reference.get("path")
    sha256 = reference.get("sha256")
    if (not isinstance(identity, str) or not identity
            or len(identity) > 160
            or not isinstance(repository, str) or not repository or len(repository) > 128
            or not isinstance(commit, str) or not _COMMIT.fullmatch(commit)
            or not isinstance(path, str)
            or (sha256 is not None
                and (not isinstance(sha256, str) or not _SHA256.fullmatch(sha256)))):
        return None
    relative = PurePosixPath(path)
    try:
        path_size = len(path.encode("utf-8"))
    except UnicodeEncodeError:
        return None
    if (relative.is_absolute() or "\\" in path
            or path_size > 1024
            or any(part in ("", ".", "..") for part in path.split("/"))):
        return None
    result = {
        "identity": identity,
        "repository": repository,
        "commit": commit,
        "path": path,
    }
    if isinstance(sha256, str):
        result["sha256"] = sha256
    return result


def _short_text(value: Any, limit: int = 192) -> str | None:
    if not isinstance(value, str):
        return None
    if len(value) <= limit:
        return value
    return value[:limit - 1] + "…"


def _compact_projection(
    projection: dict[str, Any], original_size: int, max_result_bytes: int,
) -> dict[str, Any]:
    """Return a small inspectable summary when the full projection is large.

    Exact source pointers remain available, while raw operation payloads are
    represented by a digest so one large observation cannot defeat the
    projection's response bound.
    """
    subject = projection["subject"]
    intent = subject.get("work_intent") or {}
    source = projection["source"]
    compact_subject = {
        key: _short_text(subject.get(key), 160)
        for key in ("session_id", "campaign_id", "environment_id", "definition_id")
        if subject.get(key) is not None
    }
    compact_subject["work_intent"] = {
        key: _short_text(intent.get(key), 256 if key == "goal" else 160)
        for key in ("identity", "goal")
        if intent.get(key) is not None
    }
    compact_source = {
        key: source.get(key)
        for key in ("evidence_count", "evidence_page_truncated",
                    "observation_records_considered",
                    "observation_records_verified",
                    "observation_records_unavailable")
        if source.get(key) is not None
    }
    for key in ("campaign_state_checkpoint_id", "recorded_at"):
        if source.get(key) is not None:
            compact_source[key] = _short_text(source[key], 160)
    compact_source["response_truncated"] = True
    compact_source["response_byte_count"] = original_size

    compact_hypotheses = []
    observations = projection["observations"]
    for hypothesis in projection["pressure_hypotheses"][:8]:
        summary = {
            key: _short_text(hypothesis.get(key), 256 if key in ("title", "recommendation") else 160)
            for key in ("identity", "title", "category", "subsystem", "owner",
                        "interpretation", "recurrence_status", "recommendation")
            if hypothesis.get(key) is not None
        }
        for key in ("observation_count", "distinct_session_count", "distinct_campaign_count"):
            if type(hypothesis.get(key)) is int:
                summary[key] = hypothesis[key]
        summary["evidence_observation_identities"] = [
            item["identity"] for item in observations
            if item.get("pressure_hypothesis", {}).get("identity") == hypothesis.get("identity")
        ][:8]
        compact_hypotheses.append(summary)

    compact_observations = []
    for item in observations[:8]:
        observation = item.get("observation") or {}
        observation_bytes = json.dumps(
            observation, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        work = item.get("related_work") or {}
        compact_work = {
            key: _short_text(work.get(key), 256 if key == "work_intent_goal" else 160)
            for key in ("campaign_id", "session_id", "work_intent_id", "work_intent_goal", "lifecycle")
            if work.get(key) is not None
        }
        checkpoint = work.get("latest_checkpoint")
        if isinstance(checkpoint, dict):
            compact_work["latest_checkpoint"] = {
                "identity": _short_text(checkpoint.get("identity"), 160),
                "sequence": checkpoint.get("sequence") if type(checkpoint.get("sequence")) is int else None,
            }
        if type(work.get("campaign_evidence_count")) is int:
            compact_work["campaign_evidence_count"] = work["campaign_evidence_count"]
        if isinstance(work.get("claim_ids"), list):
            compact_work["claim_ids"] = [
                _short_text(value, 160) for value in work["claim_ids"][:4]
                if isinstance(value, str)
            ]
        hypothesis = item.get("pressure_hypothesis") or {}
        compact_hypothesis = {
            key: _short_text(hypothesis.get(key), 256 if key == "title" else 160)
            for key in ("identity", "title", "category", "subsystem", "owner")
            if hypothesis.get(key) is not None
        }
        compact_observations.append({
            "identity": item["identity"],
            "captured_at": _short_text(item.get("captured_at"), 80),
            "measurement_basis": "recorded operational response; payload summarized",
            "observation": {
                "kind": _short_text(observation.get("kind"), 128),
                "source_interface": _short_text(observation.get("source_interface"), 128),
                "details_omitted": True,
                "details_sha256": hashlib.sha256(observation_bytes).hexdigest(),
            },
            "related_work": compact_work,
            "pressure_hypothesis": compact_hypothesis,
            "evidence_reference": item["evidence_reference"],
        })

    result = {
        "schema_version": SCHEMA,
        "identity": projection["identity"],
        "status": "partial",
        "subject": compact_subject,
        "source": compact_source,
        "correlation": projection["correlation"],
        "observation_count": projection["observation_count"],
        "pressure_hypothesis_count": projection["pressure_hypothesis_count"],
        "pressure_hypotheses": compact_hypotheses,
        "observations": compact_observations,
        "measurement_limits": [
            "The response exceeded its byte limit; payload details are summarized by digest.",
            "Exact observation records remain addressable through their evidence references.",
        ],
    }
    # The selected fields are individually bounded. This final reduction also
    # accounts for unusual but valid identifiers and reference paths.
    while len(json.dumps(result, ensure_ascii=False, sort_keys=True).encode("utf-8")) > max_result_bytes:
        if len(result["observations"]) > 1:
            result["observations"].pop()
        elif result["pressure_hypotheses"]:
            result["pressure_hypotheses"].pop()
        else:
            result["observations"] = []
            break
    return result


def _is_observation_reference(reference: dict[str, str]) -> bool:
    return (reference["identity"].startswith("ev_environment_")
            and reference["path"].startswith(OBSERVATION_PATH_PREFIX))


def _git_blob(
    checkout: Path, commit: str, path: str, *, deadline: float,
) -> tuple[bytes | None, str]:
    object_name = f"{commit}:{path}"
    environment = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, "read-budget-exhausted"
        size = subprocess.run(
            [workspace_module.git_binary(), "-C", str(checkout), "cat-file", "-s", object_name],
            capture_output=True, timeout=min(1.0, remaining), check=False, env=environment,
        )
        if size.returncode != 0:
            return None, "commit-path-unavailable"
        raw_size = size.stdout.strip()
        if not raw_size.isdigit():
            return None, "object-size-unavailable"
        if int(raw_size) > MAX_OBSERVATION_BYTES:
            return None, "observation-too-large"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, "read-budget-exhausted"
        blob = subprocess.run(
            [workspace_module.git_binary(), "-C", str(checkout), "cat-file", "blob", object_name],
            capture_output=True, timeout=min(1.0, remaining), check=False, env=environment,
        )
    except subprocess.TimeoutExpired:
        return None, "read-budget-exhausted"
    except (OSError, subprocess.SubprocessError):
        return None, "git-read-unavailable"
    if blob.returncode != 0:
        return None, "commit-path-unavailable"
    if len(blob.stdout) > MAX_OBSERVATION_BYTES:
        return None, "observation-too-large"
    return blob.stdout, "read"


def _load_observation(
    session: Any, reference: dict[str, str], root: Path, *, deadline: float,
) -> tuple[dict[str, Any] | None, str]:
    checkout = _checkout(session, reference["repository"], root)
    if checkout is None:
        return None, "outside-selected-session"
    raw, status = _git_blob(
        checkout, reference["commit"], reference["path"], deadline=deadline,
    )
    if raw is None:
        return None, status
    if hashlib.sha256(raw).hexdigest() != reference["sha256"]:
        return None, "sha256-mismatch"
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "invalid-json"
    if not isinstance(record, dict):
        return None, "observation-contract-invalid"
    observation = record.get("observation")
    related_work = record.get("related_work")
    hypothesis = record.get("pressure_hypothesis")
    if (record.get("schema_version") != OBSERVATION_SCHEMA
            or record.get("identity") != reference["identity"]
            or not isinstance(record.get("captured_at"), str)
            or not isinstance(observation, dict)
            or not isinstance(observation.get("kind"), str)
            or not isinstance(observation.get("source_interface"), str)
            or not isinstance(observation.get("request"), dict)
            or not isinstance(observation.get("outcome"), dict)
            or not isinstance(related_work, dict)
            or any(not isinstance(related_work.get(key), str) or not related_work[key]
                   for key in ("campaign_id", "session_id", "work_intent_id", "work_intent_goal"))
            or not isinstance(hypothesis, dict)
            or any(not isinstance(hypothesis.get(key), str) or not hypothesis[key]
                   for key in ("identity", "title", "category", "subsystem", "owner",
                               "actionable_recommendation"))):
        return None, "observation-contract-invalid"
    return record, "verified"


def _source_status(source: Any, references: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(source, dict):
        return {"kind": "unknown", "status": "invalid-source-reference"}
    summary = {
        key: source.get(key)
        for key in ("kind", "repository", "commit", "path", "sha256",
                    "campaign_id", "session_id")
        if source.get(key) is not None
    }
    if source.get("kind") == "direct-operation-result":
        summary["status"] = "captured-in-observation-record"
        return summary
    wanted = tuple(source.get(key) for key in ("repository", "commit", "path", "sha256"))
    match = next((reference for reference in references
                  if tuple(reference.get(key) for key in
                           ("repository", "commit", "path", "sha256")) == wanted), None)
    if match is None:
        summary["status"] = "not-registered-in-campaign-ledger"
    else:
        summary["status"] = match.get("verification", "unverified")
        summary["campaign_evidence_identity"] = match.get("identity")
    return summary


def _recurrence(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for observation in observations:
        hypothesis = observation["pressure_hypothesis"]
        identity = hypothesis.get("identity")
        if isinstance(identity, str) and identity:
            grouped.setdefault(identity, []).append(observation)
    result = []
    for identity, group in sorted(grouped.items()):
        sessions = sorted({
            item["related_work"].get("session_id")
            for item in group
            if isinstance(item.get("related_work"), dict)
            and isinstance(item["related_work"].get("session_id"), str)
        })
        campaigns = sorted({
            item["related_work"].get("campaign_id")
            for item in group
            if isinstance(item.get("related_work"), dict)
            and isinstance(item["related_work"].get("campaign_id"), str)
        })
        if len(sessions) > 1:
            recurrence_status = "observed-across-sessions"
        elif len(group) > 1:
            recurrence_status = "repeated-within-session"
        else:
            recurrence_status = "not-established"
        hypothesis = group[0]["pressure_hypothesis"]
        result.append({
            "identity": identity,
            "title": hypothesis.get("title"),
            "category": hypothesis.get("category"),
            "subsystem": hypothesis.get("subsystem"),
            "owner": hypothesis.get("owner"),
            "interpretation": "hypothesis",
            "observation_count": len(group),
            "distinct_session_count": len(sessions),
            "distinct_campaign_count": len(campaigns),
            "recurrence_status": recurrence_status,
            "recommendation": hypothesis.get("actionable_recommendation"),
            "evidence": [item["evidence_reference"] for item in group],
        })
    return result


def project(
    session: Any, *, campaign_state: dict[str, Any] | None = None,
    max_result_bytes: int = MAX_RESULT_BYTES,
) -> dict[str, Any]:
    """Build a bounded read-only relationship view from campaign evidence.

    Only records in ``evidence/environmental-capability/`` are read. Their
    exact Git commit and SHA-256 must be present in the authenticated campaign
    ledger, and the repository must be selected by this Environment session.
    """
    if (type(max_result_bytes) is not int
            or max_result_bytes < 1 or max_result_bytes > MAX_RESULT_BYTES):
        raise ValueError("capability projection result limit must be within its configured bound")
    snapshot = session.snapshot
    campaign = snapshot.get("campaign") or {}
    intent = snapshot.get("intent") or {}
    if campaign_state is None:
        try:
            campaign_state = session.campaign_state(evidence_limit=MAX_EVIDENCE_REFS)
        except (LifecycleError, StoreIntegrityFailure, StoreUnavailable):
            campaign_state = None
    state = campaign_state if isinstance(campaign_state, dict) else {}
    raw_references = state.get("evidence", [])
    raw_references = raw_references if isinstance(raw_references, list) else []
    page_truncated = bool(state.get("evidence_truncated", False))
    root = _workspace_root(session)
    references: list[dict[str, Any]] = []
    verified_records: list[tuple[dict[str, Any], dict[str, Any]]] = []
    seen_observations: set[tuple[str, str, str, str]] = set()
    unavailable = 0
    read_deadline = time.monotonic() + MAX_OBSERVATION_READ_SECONDS
    for raw in raw_references[-MAX_EVIDENCE_REFS:]:
        pointer = _validate_reference(raw)
        if pointer is None:
            continue
        if not _is_observation_reference(pointer):
            verification = (
                "outside-selected-session"
                if root is None or _checkout(session, pointer["repository"], root) is None
                else "not-an-environmental-observation"
            )
            references.append({**pointer, "verification": verification})
            continue
        source_key = (
            pointer["repository"], pointer["commit"],
            pointer["path"], pointer.get("sha256", ""),
        )
        if source_key in seen_observations:
            references.append({**pointer, "verification": "duplicate-reference"})
            continue
        seen_observations.add(source_key)
        record = None
        verification = "sha256-unavailable" if "sha256" not in pointer else "workspace-unavailable"
        if root is not None and "sha256" in pointer:
            record, verification = _load_observation(
                session, pointer, root, deadline=read_deadline,
            )
        reference = {**pointer, "verification": verification}
        references.append(reference)
        if record is None:
            unavailable += 1
            continue
        verified_records.append((record, reference))
    observations: list[dict[str, Any]] = []
    for record, reference in verified_records[:MAX_OBSERVATIONS]:
        observation = record["observation"]
        work = record.get("related_work")
        work = work if isinstance(work, dict) else {}
        hypothesis = record["pressure_hypothesis"]
        sources = record.get("supporting_sources")
        sources = sources if isinstance(sources, list) else []
        observations.append({
            "identity": record["identity"],
            "captured_at": record.get("captured_at"),
            "measurement_basis": "recorded operational response",
            "observation": {
                "kind": observation.get("kind"),
                "source_interface": observation.get("source_interface"),
                "request": observation.get("request"),
                "outcome": observation.get("outcome"),
            },
            "related_work": {
                key: work.get(key)
                for key in ("campaign_id", "session_id", "work_intent_id",
                            "work_intent_goal", "lifecycle", "latest_checkpoint",
                            "campaign_evidence_count", "claim_ids")
                if work.get(key) is not None
            },
            "pressure_hypothesis": {
                key: hypothesis.get(key)
                for key in ("identity", "title", "category", "subsystem", "owner",
                            "basis", "recurrence", "actionable_recommendation",
                            "not_measured")
                if hypothesis.get(key) is not None
            },
            "supporting_sources": [
                _source_status(source, references)
                for source in sources[:16]
            ],
            "evidence_reference": {
                key: reference[key]
                for key in ("identity", "repository", "commit", "path", "sha256")
            },
        })
        if len(observations) >= MAX_OBSERVATIONS:
            break
    if campaign_state is None:
        status = "unavailable"
    elif observations and unavailable:
        status = "partial"
    elif observations:
        status = "ready"
    elif raw_references:
        status = "partial"
    else:
        status = "no-observations"
    hypotheses = _recurrence(observations)
    projection: dict[str, Any] = {
        "schema_version": SCHEMA,
        "identity": "eci_" + digest_hex({
            "session_id": session.session_id,
            "campaign_id": campaign.get("identity"),
            "evidence": [item["identity"] for item in references],
        }, length=16),
        "status": status,
        "subject": {
            "session_id": session.session_id,
            "campaign_id": campaign.get("identity"),
            "environment_id": snapshot.get("environment_id"),
            "definition_id": (snapshot.get("provenance") or {}).get("definition_id"),
            "work_intent": {
                "identity": intent.get("identity"),
                "goal": intent.get("goal"),
            },
        },
        "source": {
            "campaign_state_checkpoint_id": state.get("checkpoint_id"),
            "recorded_at": state.get("recorded_at"),
            "recorded_by": state.get("recorded_by"),
            "evidence_count": state.get("evidence_count", 0),
            "evidence_page_truncated": page_truncated,
            "observation_records_considered": len(references),
            "observation_records_verified": len(observations),
            "observation_records_unavailable": unavailable,
        },
        "correlation": {
            "kind": "campaign-evidence-to-objective-and-subsystem",
            "derived": True,
            "relationship": "each observation retains its originating campaign, session, work intent, subsystem, and immutable evidence pointer",
        },
        "observation_count": len(observations),
        "pressure_hypothesis_count": len(hypotheses),
        "pressure_hypotheses": hypotheses,
        "observations": observations,
        "measurement_limits": [
            "Only committed observation records linked from this campaign-state page are considered.",
            "An unavailable or unselected source is surfaced without reading it or treating it as verified.",
            "Repeated references to the same committed observation do not establish recurrence.",
            "Candidate continuation count is not treated as repeated pressure; recurrence requires distinct recorded observations.",
            "Effectiveness after a proposed change remains unknown until later independent observations support it.",
        ],
    }
    encoded = json.dumps(projection, ensure_ascii=False, sort_keys=True).encode("utf-8")
    if len(encoded) > max_result_bytes:
        return _compact_projection(projection, len(encoded), max_result_bytes)
    return projection
