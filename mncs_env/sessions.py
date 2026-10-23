"""Environment resolution and durable EnvironmentSession lifecycle.

Resolution composes workspace facts, discovered capabilities, authority
projection, and event sources into one inspectable Environment record.
Sessions persist through the session store (Store-backed canonical, file
as debug projection) as structured snapshots plus an append-only event
log, so a different process or consumer resumes from state, not prose.

Concurrency: records are immutable under content-derived identities and
publication uses generation CAS with bounded retries. Two processes
appending events converge on sequence order; a lost race retries, and an
identical re-put is idempotent.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from . import authority as authority_module
from . import campaign_selector, composition
from . import capabilities as capabilities_module
from . import claims as claims_module
from . import events as events_module
from . import invocation_artifacts as invocation_artifacts_module
from . import readiness as readiness_module
from . import rights as rights_module
from . import workspace as workspace_module
from .identity import (
    digest_hex,
    environment_id,
    handoff_id,
    new_session_id,
    resolved_environment_id,
)
from .intent import parse as parse_intent
from .session_store import (
    EventGenerationConflict,
    SequenceTaken,
    SessionStore,
    open_store,
    store_provider_from_environment,
    write_session_store_provider,
)

SESSION_SCHEMA = "mncs.environment.session/2"
ENVIRONMENT_SCHEMA = "mncs.environment.resolved/1"
CHECKPOINT_SCHEMA = "mncs.environment.checkpoint/1"
HANDOFF_SCHEMA = "mncs.environment.handoff/1"

MAX_EMIT_RETRIES = 16


def _request_operation_digest(
    session_id: str,
    request_id: str,
    request_operation: str,
    snapshot: dict[str, Any],
) -> str:
    return digest_hex({
        "operation": "mncs-environment-request/1",
        "session_id": session_id,
        "request_id": request_id,
        "request_operation": request_operation,
        "consumer_id": snapshot.get("consumer_id"),
        "authenticated_principal_id": snapshot.get("authenticated_principal_id"),
    })

CONTINUATION_MAX_CHECKPOINT_REMAINING = 8
CONTINUATION_MAX_FOREIGN_CLAIMS = 12
CONTINUATION_MAX_CLAIM_PATHS = 4
CAMPAIGN_EVIDENCE_MAX = 128
CAMPAIGN_CHARTER_EVIDENCE_MAX = 16
CONTINUATION_MAX_CAMPAIGN_EVIDENCE = 32
CAMPAIGN_DELIVERY_REPOSITORIES_MAX = 16
CAMPAIGN_STATE_INPUT_MAX_BYTES = 48 * 1024
CAMPAIGN_STATE_READ_MAX_BYTES = 56 * 1024
MAX_PRESERVED_CAMPAIGN_EVIDENCE_FILES = 32
MAX_PRESERVED_CAMPAIGN_EVIDENCE_FILE_BYTES = 1024 * 1024
MAX_PRESERVED_CAMPAIGN_EVIDENCE_TOTAL_BYTES = 8 * 1024 * 1024

_CAMPAIGN_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_CAMPAIGN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_GIT_OBJECT_ID_PATTERN = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


def _preserved_campaign_evidence(
    checkout: Path, dirty_files: list[str], *, dirty_truncated: bool,
    untracked_count: int,
) -> dict[str, Any] | None:
    """Digest a bounded set of existing campaign JSON evidence without adopting code.

    Recovery scope admission can preserve untracked campaign evidence only
    when those JSON files are the complete dirty set. The inventory is
    attached to the immutable scope grant so continuation can check that the
    same evidence was present when the owner approved the scope.
    """
    if (dirty_truncated or not dirty_files
            or len(dirty_files) > MAX_PRESERVED_CAMPAIGN_EVIDENCE_FILES
            or untracked_count != len(dirty_files)):
        return None
    try:
        root = checkout.resolve(strict=True)
        evidence_dir = root / "evidence"
        evidence_root = evidence_dir.resolve(strict=True)
        if evidence_dir.is_symlink() or evidence_root.parent != root:
            return None
    except OSError:
        return None

    entries: list[dict[str, Any]] = []
    total_bytes = 0
    for status_line in sorted(dirty_files):
        if not status_line.startswith("?? "):
            return None
        relative = status_line[3:]
        if not re.fullmatch(
            r"evidence/campaign-[A-Za-z0-9][A-Za-z0-9._-]{0,159}\.json",
            relative,
        ):
            return None
        path = root / relative
        try:
            if path.is_symlink() or path.parent.resolve(strict=True) != evidence_root:
                return None
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as handle:
                before = os.fstat(handle.fileno())
                if (not stat.S_ISREG(before.st_mode)
                        or before.st_size > MAX_PRESERVED_CAMPAIGN_EVIDENCE_FILE_BYTES):
                    return None
                raw = handle.read(MAX_PRESERVED_CAMPAIGN_EVIDENCE_FILE_BYTES + 1)
                after = os.fstat(handle.fileno())
            if (len(raw) != before.st_size or len(raw) > MAX_PRESERVED_CAMPAIGN_EVIDENCE_FILE_BYTES
                    or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                    != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
                return None
            document = json.loads(raw)
            if not isinstance(document, dict):
                return None
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        total_bytes += len(raw)
        if total_bytes > MAX_PRESERVED_CAMPAIGN_EVIDENCE_TOTAL_BYTES:
            return None
        identity = document.get("identity")
        entries.append({
            "path": relative,
            "byte_count": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "identity": identity if isinstance(identity, str) else None,
            "schema_version": document.get("schema_version"),
        })
    return {
        "schema_version": "mncs.environment.preserved-campaign-evidence/1",
        "file_count": len(entries),
        "byte_count": total_bytes,
        "files_digest": digest_hex(entries, length=64),
        "files": entries,
    }

# Allowed lifecycle transitions. Every transition records a reason; an
# illegal transition raises LifecycleError. Completion paths below match
# this table exactly (audited); complete()/fail() enforce the same sets.
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "defined": ("resolving", "abandoned"),
    "resolving": ("ready", "failed", "abandoned"),
    "ready": ("active", "failed", "abandoned"),
    "active": ("blocked", "waiting", "checkpointed", "handed_off", "completed", "failed", "abandoned"),
    "blocked": ("active", "failed", "abandoned", "completed"),
    "waiting": ("active", "failed", "abandoned", "completed"),
    "checkpointed": ("active", "failed", "abandoned", "completed"),
    "handed_off": ("active", "abandoned"),
    "completed": (),
    "failed": (),
    "abandoned": ("active",),
}


class LifecycleError(Exception):
    """Raised for illegal session transitions or corrupted session state."""


class CampaignStateConflict(LifecycleError):
    """Raised when an optimistic campaign-state checkpoint is stale."""

    def __init__(self, supplied_checkpoint: str, current_checkpoint: str):
        super().__init__(
            "campaign state changed since the supplied base checkpoint; "
            "read the capsule and retry with its current campaign state checkpoint"
        )
        self.diagnostics = {
            "code": "publication-conflict",
            "conflict_kind": "campaign-state-base-checkpoint",
            "supplied_base_checkpoint": supplied_checkpoint,
            "current_checkpoint": current_checkpoint,
            "publication_completed": False,
            "next": "read the authenticated continuation capsule and retry with its current campaign state checkpoint",
        }


class AuthorityScopeConflict(LifecycleError):
    """Raised when a repository authority grant is stale or inadmissible."""

    def __init__(self, code: str, message: str, *, next_step: str):
        super().__init__(message)
        self.diagnostics = {
            "code": code,
            "publication_completed": False,
            "next": next_step,
        }


class AuthorityDenied(Exception):
    """Raised when session authority denies a requested action."""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _repo_facts(workspace_view: dict[str, Any]) -> dict[str, dict[str, Any]]:
    facts = {}
    for repo in workspace_view.get("repositories", []):
        signals = workspace_module.foreign_work_signals(repo)
        facts[repo["name"]] = {
            "clean": not repo.get("dirty", False),
            "main_branch": repo.get("branch") in ("main", "master", None),
            "dirty": bool(repo.get("dirty", False)),
            "head": repo.get("head"),
            "branch": repo.get("branch"),
            "path": repo.get("path"),
            "foreign_signals": [signal["kind"] for signal in signals],
        }
    for selected in workspace_view.get("selected_checkouts", {}).values():
        repository = str(selected.get("repository", ""))
        if not repository:
            continue
        clean = bool(selected.get("clean", False))
        facts[repository] = {
            "clean": clean,
            "main_branch": selected.get("branch") in ("main", "master", None),
            "dirty": not clean,
            "head": selected.get("head"),
            "branch": selected.get("branch"),
            "path": selected.get("path"),
            "foreign_signals": [] if clean else ["dirty-tree"],
        }
    return facts


def _bounded_capsule_text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    if len(value) <= limit:
        return value
    return value[:limit - 1] + "…"


def _campaign_text(value: Any, field: str, limit: int) -> str:
    if (not isinstance(value, str) or not value or len(value) > limit
            or "\x00" in value or any(ord(char) < 32 or ord(char) == 127
                                      for char in value)):
        raise ValueError(f"campaign {field} must be bounded nonempty text")
    return value


def _campaign_commit(value: Any, field: str, *, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not _GIT_OBJECT_ID_PATTERN.fullmatch(value):
        raise ValueError(f"campaign {field} must be a full Git object identity")
    return value.lower()


def normalize_campaign_evidence(value: Any) -> list[dict[str, str]]:
    """Validate durable evidence pointers without treating them as verified.

    Each pointer names immutable source bytes by repository, full commit, and
    repository-relative path. Environment records who supplied the pointer;
    the owning system remains responsible for validating the referenced
    artifact itself.
    """
    if not isinstance(value, list) or len(value) > CAMPAIGN_EVIDENCE_MAX:
        raise ValueError(
            f"campaign evidence must be a list of at most {CAMPAIGN_EVIDENCE_MAX} references"
        )
    normalized: list[dict[str, str]] = []
    identities: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("campaign evidence references must be objects")
        allowed = {"identity", "repository", "commit", "path", "sha256"}
        if set(item) - allowed or not {"identity", "repository", "commit", "path"} <= set(item):
            raise ValueError(
                "campaign evidence references require identity, repository, commit, and path"
            )
        identity = _campaign_text(item["identity"], "evidence identity", 160)
        if not _CAMPAIGN_ID_PATTERN.fullmatch(identity):
            raise ValueError("campaign evidence identity contains unsupported characters")
        if identity in identities:
            raise ValueError(f"duplicate campaign evidence identity: {identity}")
        identities.add(identity)
        repository = _campaign_text(item["repository"], "evidence repository", 120)
        if not _CAMPAIGN_REPOSITORY_PATTERN.fullmatch(repository):
            raise ValueError("campaign evidence repository identity is invalid")
        commit = _campaign_commit(item["commit"], "evidence commit")
        path = _campaign_text(item["path"], "evidence path", 512)
        parts = path.split("/")
        pure_path = PurePosixPath(path)
        if ("\\" in path or pure_path.is_absolute() or path.startswith("~")
                or any(part in ("", ".", "..") for part in parts)):
            raise ValueError("campaign evidence path must be a normalized repository-relative path")
        record = {
            "identity": identity,
            "repository": repository,
            "commit": str(commit),
            "path": path,
        }
        if "sha256" in item:
            digest = item["sha256"]
            if not isinstance(digest, str) or not _SHA256_PATTERN.fullmatch(digest):
                raise ValueError("campaign evidence sha256 must contain 64 hexadecimal characters")
            record["sha256"] = digest.lower()
        normalized.append(record)
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    if len(encoded) > CAMPAIGN_STATE_INPUT_MAX_BYTES:
        raise ValueError("campaign evidence exceeds the bounded publication size")
    return normalized


def _append_campaign_evidence(
    previous_history: list[dict[str, str]],
    incoming_window: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Accept an append-only evidence window, evicting only its oldest prefix.

    Older references remain recoverable from their immutable campaign-state
    checkpoints. When a new reference arrives, the request must retain the
    newest prior suffix that fits alongside the additions.
    """
    prior_window = previous_history[-CAMPAIGN_EVIDENCE_MAX:]
    prior_by_identity = {
        item["identity"]: item for item in previous_history
    }
    retained: list[dict[str, str]] = []
    added: list[dict[str, str]] = []
    saw_addition = False
    for item in incoming_window:
        existing = prior_by_identity.get(item["identity"])
        if existing is None:
            saw_addition = True
            added.append(item)
            continue
        if item != existing:
            raise LifecycleError(
                "campaign evidence changed an existing immutable reference"
            )
        if saw_addition:
            raise LifecycleError(
                "new campaign evidence must follow the retained prior suffix"
            )
        retained.append(item)
    if not added:
        if incoming_window != prior_window:
            raise LifecycleError(
                "campaign evidence updates must retain the current evidence window"
            )
    else:
        keep_count = min(
            len(prior_window), CAMPAIGN_EVIDENCE_MAX - len(added)
        )
        expected_prior = prior_window[-keep_count:] if keep_count else []
        if retained != expected_prior or incoming_window != [*expected_prior, *added]:
            raise LifecycleError(
                "campaign evidence may evict only its oldest prefix when appending; "
                "prior immutable checkpoints retain the evicted references"
            )
    return added, [*previous_history, *added]


def normalize_campaign_delivery(value: Any) -> dict[str, Any]:
    """Validate a compact delivery report; it is a recorded claim, not proof."""
    if not isinstance(value, dict):
        raise ValueError("campaign delivery must be an object")
    allowed = {"schema_version", "status", "repositories", "reason"}
    if set(value) - allowed:
        raise ValueError("campaign delivery contains unsupported fields")
    if value.get("schema_version", "mncs.environment.campaign-delivery/1") != (
        "mncs.environment.campaign-delivery/1"
    ):
        raise ValueError("unsupported campaign delivery schema")
    overall = value.get("status")
    if overall not in ("pending", "partial", "delivered", "blocked"):
        raise ValueError("campaign delivery status must be pending, partial, delivered, or blocked")
    repositories = value.get("repositories", [])
    if (not isinstance(repositories, list)
            or len(repositories) > CAMPAIGN_DELIVERY_REPOSITORIES_MAX):
        raise ValueError(
            "campaign delivery repositories must be a bounded list of repository records"
        )
    normalized_repositories: list[dict[str, str]] = []
    seen_repositories: set[str] = set()
    allowed_repository_fields = {
        "repository", "branch", "head", "status", "remote_ref", "remote_head",
        "evidence_identity", "reason",
    }
    for item in repositories:
        if not isinstance(item, dict) or set(item) - allowed_repository_fields:
            raise ValueError("campaign delivery repository records contain unsupported fields")
        repository = _campaign_text(item.get("repository"), "delivery repository", 120)
        if not _CAMPAIGN_REPOSITORY_PATTERN.fullmatch(repository):
            raise ValueError("campaign delivery repository identity is invalid")
        if repository in seen_repositories:
            raise ValueError(f"duplicate campaign delivery repository: {repository}")
        seen_repositories.add(repository)
        status = item.get("status")
        if status not in ("pending", "committed", "merged", "pushed", "retained", "blocked"):
            raise ValueError("campaign repository delivery status is invalid")
        record: dict[str, str] = {"repository": repository, "status": status}
        branch = item.get("branch")
        if branch is not None:
            branch = _campaign_text(branch, "delivery branch", 200)
            if (branch.startswith("-") or branch.startswith("/") or branch.endswith("/")
                    or ".." in branch or "@{" in branch or "\\" in branch
                    or any(char.isspace() for char in branch)):
                raise ValueError("campaign delivery branch is not a safe Git ref name")
            record["branch"] = branch
        head = _campaign_commit(item.get("head"), "delivery head", required=False)
        if head is not None:
            record["head"] = head
        remote_ref = item.get("remote_ref")
        if remote_ref is not None:
            remote_ref = _campaign_text(remote_ref, "delivery remote ref", 256)
            if (remote_ref.startswith(("/", "~")) or "://" in remote_ref
                    or "@" in remote_ref or "\\" in remote_ref
                    or any(part in ("", ".", "..") for part in remote_ref.split("/"))
                    or any(char.isspace() for char in remote_ref)):
                raise ValueError("campaign delivery remote ref must be a repository ref, not a path or URL")
            record["remote_ref"] = remote_ref
        remote_head = _campaign_commit(
            item.get("remote_head"), "delivery remote head", required=False
        )
        if remote_head is not None:
            record["remote_head"] = remote_head
        evidence_identity = item.get("evidence_identity")
        if evidence_identity is not None:
            evidence_identity = _campaign_text(
                evidence_identity, "delivery evidence identity", 160
            )
            if not _CAMPAIGN_ID_PATTERN.fullmatch(evidence_identity):
                raise ValueError("campaign delivery evidence identity is invalid")
            record["evidence_identity"] = evidence_identity
        reason = item.get("reason")
        if reason is not None:
            record["reason"] = _campaign_text(reason, "delivery reason", 320)
        if status in ("committed", "merged", "pushed", "retained") and (
            not record.get("branch") or not record.get("head")
        ):
            raise ValueError(f"{status} campaign delivery requires a branch and full head")
        if status == "pushed" and not (
            record.get("remote_ref") and record.get("remote_head")
        ):
            raise ValueError("pushed campaign delivery requires remote_ref and remote_head")
        normalized_repositories.append(record)
    normalized_repositories.sort(key=lambda item: item["repository"])
    if overall == "delivered" and (
        not normalized_repositories
        or any(item["status"] not in ("pushed", "retained")
               for item in normalized_repositories)
    ):
        raise ValueError("delivered campaign status requires every repository to be pushed or retained")
    normalized: dict[str, Any] = {
        "schema_version": "mncs.environment.campaign-delivery/1",
        "status": overall,
        "repositories": normalized_repositories,
    }
    if "reason" in value:
        normalized["reason"] = _campaign_text(value["reason"], "delivery reason", 320)
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    if len(encoded) > CAMPAIGN_STATE_INPUT_MAX_BYTES:
        raise ValueError("campaign delivery exceeds the bounded publication size")
    return normalized


def _validated_campaign_state_history(
    checkpoints: list[dict[str, Any]], campaign_identity: str | None,
) -> tuple[dict[str, Any], list[dict[str, str]]] | None:
    """Validate the immutable checkpoint chain and reconstruct its evidence.

    Campaign-state/1 checkpoints carry a cumulative bounded list. Version 2
    checkpoints carry only the new evidence segment and a digest link to the
    preceding campaign state. Evicted references remain available from their
    earlier immutable checkpoints.
    """
    candidates = [record for record in checkpoints
                  if isinstance(record, dict) and isinstance(record.get("campaign_state"), dict)]
    if not candidates:
        return None
    candidates.sort(key=lambda record: (
        int(record.get("sequence", 0)), str(record.get("created_at", "")),
        str(record.get("identity", "")),
    ))

    # A same-sequence identical publication is a retry-equivalent duplicate;
    # divergent state at one sequence is ambiguous and cannot be extended.
    logical: list[dict[str, Any]] = []
    for record in candidates:
        state = record["campaign_state"]
        schema = state.get("schema_version")
        if (schema not in (
                "mncs.environment.campaign-state/1",
                "mncs.environment.campaign-state/2",
            )
                or state.get("campaign_identity") != campaign_identity
                or record.get("campaign_state_digest") != digest_hex(state)):
            raise LifecycleError("campaign state checkpoint failed identity or digest validation")
        try:
            delivery = normalize_campaign_delivery(state.get("delivery"))
            if schema == "mncs.environment.campaign-state/1":
                evidence = normalize_campaign_evidence(state.get("evidence"))
                if evidence != state.get("evidence"):
                    raise ValueError("campaign evidence is not canonically normalized")
            else:
                evidence_delta = normalize_campaign_evidence(state.get("evidence_delta"))
                if evidence_delta != state.get("evidence_delta"):
                    raise ValueError("campaign evidence delta is not canonically normalized")
        except ValueError as error:
            raise LifecycleError(f"campaign state checkpoint is invalid: {error}") from error
        if delivery != state.get("delivery"):
            raise LifecycleError("campaign state checkpoint is not canonically normalized")
        provenance = state.get("recorded_by")
        if (not isinstance(provenance, dict)
                or not isinstance(provenance.get("consumer_id"), str)
                or (provenance.get("principal_id") is not None
                    and not isinstance(provenance.get("principal_id"), str))):
            raise LifecycleError("campaign state checkpoint lacks Environment provenance")
        if logical and int(logical[-1].get("sequence", 0)) == int(record.get("sequence", 0)):
            previous = logical[-1]
            if (record.get("campaign_state_digest")
                    != previous.get("campaign_state_digest")
                    or state != previous.get("campaign_state")):
                raise LifecycleError(
                    "conflicting campaign state checkpoints share one sequence; reconcile before updating"
                )
            logical[-1] = record
        else:
            logical.append(record)

    latest_record: dict[str, Any] | None = None
    history: list[dict[str, str]] = []
    prior_digest: str | None = None
    prior_schema: str | None = None
    prior_evidence_digest = digest_hex([])
    prior_evidence_count = 0
    seen: dict[str, dict[str, str]] = {}
    for record in logical:
        state = record["campaign_state"]
        schema = state["schema_version"]
        if schema == "mncs.environment.campaign-state/1":
            if prior_schema == "mncs.environment.campaign-state/2":
                raise LifecycleError("campaign state schema cannot move back to version 1")
            try:
                cumulative = normalize_campaign_evidence(state["evidence"])
            except ValueError as error:
                raise LifecycleError(f"campaign state checkpoint is invalid: {error}") from error
            if any(seen.get(item["identity"]) != item for item in cumulative
                   if item["identity"] in seen):
                raise LifecycleError("campaign evidence history changed an immutable reference")
            if cumulative[:len(history)] != history:
                raise LifecycleError(
                    "campaign evidence history reordered or removed a prior reference"
                )
            history = cumulative
            seen = {item["identity"]: item for item in history}
            prior_evidence_count = len(history)
            prior_evidence_digest = digest_hex(history)
        else:
            if state.get("previous_state_digest") != prior_digest:
                raise LifecycleError("campaign evidence checkpoint chain has an invalid predecessor")
            delta = normalize_campaign_evidence(state["evidence_delta"])
            if any(item["identity"] in seen for item in delta):
                raise LifecycleError("campaign evidence history repeats an earlier identity")
            history.extend(delta)
            seen.update({item["identity"]: item for item in delta})
            evidence_count = state.get("evidence_count")
            if (not isinstance(evidence_count, int)
                    or isinstance(evidence_count, bool)
                    or evidence_count != prior_evidence_count + len(delta)):
                raise LifecycleError("campaign evidence count does not match its checkpoint chain")
            expected_evidence_digest = digest_hex({
                "previous": prior_evidence_digest,
                "delta": delta,
            })
            if state.get("evidence_history_digest") != expected_evidence_digest:
                raise LifecycleError("campaign evidence history digest failed verification")
            prior_evidence_count = evidence_count
            prior_evidence_digest = expected_evidence_digest
        delivery_evidence_ids = {
            item["identity"] for item in history
        }
        if any(item.get("evidence_identity") not in delivery_evidence_ids
               for item in state["delivery"].get("repositories", [])
               if item.get("evidence_identity") is not None):
            raise LifecycleError(
                "campaign delivery references an evidence identity absent from campaign history"
            )
        prior_digest = record.get("campaign_state_digest")
        prior_schema = schema
        latest_record = record
    if latest_record is None:
        return None
    return latest_record, history


def _latest_campaign_state_checkpoint(
    checkpoints: list[dict[str, Any]], campaign_identity: str | None,
) -> dict[str, Any] | None:
    history = _validated_campaign_state_history(checkpoints, campaign_identity)
    return history[0] if history else None


def _project_campaign_state(
    campaign: dict[str, Any], checkpoint: dict[str, Any] | None,
    evidence_history: list[dict[str, str]] | None = None,
) -> bool:
    if checkpoint is None:
        return False
    state = checkpoint["campaign_state"]
    if evidence_history is None:
        if state.get("schema_version") != "mncs.environment.campaign-state/1":
            raise LifecycleError("campaign evidence history is required to project a version 2 checkpoint")
        evidence_history = state["evidence"]
    values = {
        "evidence": evidence_history[-CAMPAIGN_EVIDENCE_MAX:],
        "evidence_count": len(evidence_history),
        "evidence_truncated": len(evidence_history) > CAMPAIGN_EVIDENCE_MAX,
        "delivery": state["delivery"],
        "state_checkpoint_id": checkpoint["identity"],
        "state_recorded_at": state.get("recorded_at"),
        "state_provenance": state["recorded_by"],
    }
    changed = any(campaign.get(key) != value for key, value in values.items())
    campaign.update(values)
    return changed


def continuation_observations(
    store: SessionStore, snapshot: dict[str, Any], session_id: str,
) -> dict[str, Any]:
    """Project current claim and checkpoint facts into a bounded capsule.

    Store remains authoritative. This read-only projection deliberately omits
    host-local checkout paths and does not interpret a durable session
    lifecycle as proof that its process is still running.
    """
    campaign = snapshot.get("campaign") or {}
    all_claims = claims_module.active_claims(store.read_claims())
    own_claim_ids = sorted(
        str(record.get("claim_id"))
        for record in all_claims.values()
        if record.get("session_id") == session_id and record.get("claim_id")
    )

    relevant_repositories: list[str] = []

    def add_repository(value: Any) -> None:
        if isinstance(value, str) and value and value not in relevant_repositories:
            relevant_repositories.append(value)

    work_intent = campaign.get("work_intent") or {}
    intent_repositories = work_intent.get("repositories", [])
    if isinstance(intent_repositories, list):
        for repository in intent_repositories:
            add_repository(repository)
    for item in campaign.get("repository_refs", []):
        if isinstance(item, dict):
            add_repository(item.get("repository"))
    for repository in (snapshot.get("selected_checkouts") or {}):
        add_repository(repository)
    for item in (snapshot.get("workspace") or {}).get("repositories", []):
        if isinstance(item, dict):
            add_repository(item.get("manifest_repository") or item.get("name"))

    foreign: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    repository_order = {name: index for index, name in enumerate(relevant_repositories)}
    for claim_id, record in all_claims.items():
        owner_session = str(record.get("session_id", ""))
        repository = str(record.get("repository", ""))
        if owner_session == session_id or not repository:
            continue
        raw_scope = record.get("scope")
        scope = raw_scope if isinstance(raw_scope, dict) else {}
        kind = str(scope.get("kind", "repository"))
        projected_scope: dict[str, Any] = {
            "kind": kind,
            "exclusive": bool(scope.get("exclusive", kind == "repository")),
        }
        branch = _bounded_capsule_text(scope.get("branch"), 160)
        if branch is not None:
            projected_scope["branch"] = branch
        if kind == "paths":
            paths = scope.get("paths")
            if not isinstance(paths, list):
                paths = []
            paths = [path for path in paths if isinstance(path, str) and path and not path.startswith("/")]
            projected_scope.update({
                "paths": [path[:160] for path in paths[:CONTINUATION_MAX_CLAIM_PATHS]],
                "path_count": len(paths),
                "paths_truncated": len(paths) > CONTINUATION_MAX_CLAIM_PATHS,
            })
        elif kind == "worktree":
            # The Store claim identity is the durable reference. Its checkout
            # path is a namespace-local observation and must not leak here.
            projected_scope["checkout_bound"] = bool(scope.get("checkout"))
        workspace_related = repository in repository_order
        version = record.get("version", 0)
        try:
            version = int(version)
        except (TypeError, ValueError):
            version = 0
        lease = claims_module.lease_diagnostic(record)
        projection = {
            "claim_id": str(record.get("claim_id", claim_id)),
            "version": version,
            "repository": repository,
            "workspace_related": workspace_related,
            "owner_session_id": _bounded_capsule_text(owner_session, 160),
            "owner_consumer_id": _bounded_capsule_text(record.get("consumer_id"), 160),
            "basis": _bounded_capsule_text(record.get("basis"), 80),
            "status": "held",
            "effective_expires_at": _bounded_capsule_text(
                lease.get("effective_expires_at"), 64
            ),
            "scope": projected_scope,
        }
        sort_key = (
            0 if projected_scope["exclusive"] else 1,
            0 if workspace_related else 1,
            repository_order.get(repository, len(repository_order)),
            repository, str(claim_id),
        )
        foreign.append((sort_key, projection))
    foreign.sort(key=lambda item: item[0])

    checkpoints = [
        record for record in store.list_checkpoints(session_id)
        if isinstance(record, dict) and isinstance(record.get("identity"), str)
    ]
    latest_checkpoint = None
    if checkpoints:
        def checkpoint_order(record: dict[str, Any]) -> tuple[int, str, str]:
            try:
                sequence = int(record.get("sequence", 0))
            except (TypeError, ValueError):
                sequence = 0
            return sequence, str(record.get("created_at", "")), str(record["identity"])

        latest = max(checkpoints, key=checkpoint_order)
        raw_remaining = latest.get("remaining")
        remaining = [value for value in raw_remaining if isinstance(value, str)] \
            if isinstance(raw_remaining, list) else []
        remaining_limit = CONTINUATION_MAX_CHECKPOINT_REMAINING
        progress = latest.get("progress", "")
        bounded_progress = _bounded_capsule_text(progress, 320) or ""
        bounded_remaining = [value[:200] for value in remaining[:remaining_limit]]
        latest_checkpoint = {
            "identity": latest["identity"],
            "sequence": checkpoint_order(latest)[0],
            "created_at": _bounded_capsule_text(latest.get("created_at"), 64),
            "progress": bounded_progress,
            "progress_truncated": isinstance(progress, str) and len(progress) > 320,
            "remaining": bounded_remaining,
            "remaining_count": len(remaining),
            "remaining_truncated": (
                len(remaining) > remaining_limit
                or any(len(value) > 200 for value in remaining[:remaining_limit])
            ),
        }

    campaign_state_history = _validated_campaign_state_history(
        checkpoints, campaign.get("identity")
    )
    campaign_state_projection = None
    if campaign_state_history:
        campaign_state_record, evidence_history = campaign_state_history
        state = campaign_state_record["campaign_state"]
        delivery = state["delivery"]
        delivery_repositories = delivery.get("repositories", [])
        campaign_state_projection = {
            "checkpoint_id": campaign_state_record["identity"],
            "recorded_at": _bounded_capsule_text(state.get("recorded_at"), 64),
            "recorded_by": {
                "consumer_id": _bounded_capsule_text(
                    state["recorded_by"].get("consumer_id"), 160
                ),
                "principal_id": _bounded_capsule_text(
                    state["recorded_by"].get("principal_id"), 160
                ),
            },
            # The entry capsule is a context projection, not the campaign's
            # evidence ledger. Keep only its newest references here; the
            # owner-authorized campaign_state() read provides bounded pages.
            "evidence": evidence_history[-CONTINUATION_MAX_CAMPAIGN_EVIDENCE:],
            "evidence_count": len(evidence_history),
            "evidence_truncated": (
                len(evidence_history) > CONTINUATION_MAX_CAMPAIGN_EVIDENCE
            ),
            "delivery": {
                **{key: value for key, value in delivery.items()
                   if key != "repositories"},
                "repositories": delivery_repositories[:CAMPAIGN_DELIVERY_REPOSITORIES_MAX],
                "repositories_count": len(delivery_repositories),
                "repositories_truncated": len(delivery_repositories)
                > CAMPAIGN_DELIVERY_REPOSITORIES_MAX,
            },
        }

    return {
        "claim_ids": own_claim_ids[-16:],
        "foreign_claims": [item[1] for item in foreign[:CONTINUATION_MAX_FOREIGN_CLAIMS]],
        "foreign_claims_count": len(foreign),
        "foreign_claims_truncated": len(foreign) > CONTINUATION_MAX_FOREIGN_CLAIMS,
        "latest_checkpoint": latest_checkpoint,
        "campaign_state": campaign_state_projection,
    }


def _workspace_change_facts(
    workspace_view: dict[str, Any],
    selected_checkouts: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Return bounded checkout-state facts used by workspace change events."""
    repositories = workspace_view.get("repositories", [])

    def facts_for(repo: dict[str, Any] | None, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
        fallback = fallback or {}
        if repo is None:
            return {
                "branch": fallback.get("branch"),
                "clean": False,
                "missing": True,
                "dirty_path_count": None,
                "dirty_paths_digest": None,
                "dirty_truncated": False,
            }
        dirty_paths = repo.get("dirty_files", [])
        if not isinstance(dirty_paths, list):
            dirty_paths = []
        truncated = bool(repo.get("dirty_truncated", False))
        return {
            "branch": repo.get("branch"),
            "clean": not bool(repo.get("dirty", False)),
            "missing": False,
            "dirty_path_count": len(dirty_paths),
            "dirty_paths_digest": digest_hex({"paths": dirty_paths, "truncated": truncated}),
            "dirty_truncated": truncated,
        }

    if selected_checkouts:
        result: dict[str, dict[str, Any]] = {}
        for name, selected in selected_checkouts.items():
            selected_path = Path(str(selected.get("path", "")))
            if not selected_path.is_absolute():
                selected_path = Path(str(workspace_view.get("root", "."))) / selected_path
            repo = next(
                (item for item in repositories
                 if Path(str(item.get("path", ""))).resolve() == selected_path.resolve()),
                None,
            )
            state = facts_for(repo, selected)
            state["head"] = repo.get("head") if repo is not None else selected.get("head")
            result[name] = state
        return result

    return {
        str(repo.get("name")): {**facts_for(repo), "head": repo.get("head")}
        for repo in repositories
        if repo.get("name")
    }


def _repository_checkout_identity(
    workspace_root: str | Path,
    repository: str,
    checkout: str | Path,
) -> dict[str, str]:
    """Derive the stable checkout identity used by repository scope grants."""
    return workspace_module.repository_checkout_identity(
        workspace_root, repository, checkout
    )


def _verified_scope_checkout(
    workspace_root: str | Path,
    repository: str,
    selected: dict[str, Any] | None = None,
    *,
    authorized_checkout: dict[str, Any] | None = None,
    owner_authorized_checkout: dict[str, Any] | None = None,
) -> tuple[Path, dict[str, str]]:
    """Resolve the selected registered checkout, including nested worktrees.

    Repository grants may target the currently selected linked worktree. When
    no checkout has been selected for the repository, the workspace checkout
    remains the compatibility default. In both cases Git's worktree registry,
    common directory, repository manifest, and authorized workspace establish
    identity; directory depth is not part of acceptance.
    """
    raw_root = Path(workspace_root)
    root = raw_root.resolve(strict=True)
    if Path(os.path.abspath(raw_root)) != root or not root.is_dir():
        raise ValueError("authorized Environment workspace root is not canonical")
    if owner_authorized_checkout is not None:
        # Cross-repository campaign scopes are admitted by the authenticated
        # Control owner grant. Control supplies the configured repository root
        # and one exact registered checkout; Environment independently checks
        # Git registration, manifest identity, branch, and the identity facts
        # that Control observed. This keeps a repository grant worktree-scoped
        # without requiring that every authorized checkout live inside the
        # campaign's primary repository workspace.
        repository_root_value = owner_authorized_checkout.get("repository_root")
        checkout_value = owner_authorized_checkout.get("path")
        if (not isinstance(repository_root_value, str) or not repository_root_value
                or not isinstance(checkout_value, str) or not checkout_value):
            raise ValueError("owner-authorized checkout identity is incomplete")
        repository_root = Path(repository_root_value)
        checkout_path = Path(checkout_value)
        if not repository_root.is_absolute() or not checkout_path.is_absolute():
            raise ValueError("owner-authorized checkout paths must be absolute")
        try:
            absolute_repository_root = Path(os.path.abspath(repository_root))
            repository_root = repository_root.resolve(strict=True)
            absolute_checkout = Path(os.path.abspath(checkout_path))
            checkout_path = checkout_path.resolve(strict=True)
        except OSError as error:
            raise ValueError("owner-authorized checkout is unavailable") from error
        if absolute_repository_root != repository_root:
            raise ValueError("owner-authorized repository root traverses a symbolic link")
        if absolute_checkout != checkout_path:
            raise ValueError("owner-authorized checkout path traverses a symbolic link")
        if repository_root.name != repository:
            raise ValueError("owner-authorized repository root names a different repository")
        if checkout_path == repository_root:
            raise ValueError(
                "cross-repository scope must identify one selected worktree"
            )
        try:
            checkout_path.relative_to(repository_root)
            checkout_is_repository_scoped = True
        except ValueError:
            checkout_is_repository_scoped = False
        if not checkout_is_repository_scoped:
            try:
                workspace_relative = checkout_path.relative_to(root)
            except ValueError:
                raise ValueError(
                    "owner-authorized checkout is outside its repository "
                    "and campaign workspace"
                ) from None
            cursor = root
            for part in workspace_relative.parts:
                cursor = cursor / part
                if cursor.is_symlink():
                    raise ValueError(
                        "owner-authorized checkout path traverses a symbolic link"
                    )
        expected_branch = owner_authorized_checkout.get("branch")
        observed = workspace_module.verify_registered_checkout(
            checkout_path,
            repository=repository,
            workspace_root=repository_root.parent,
            expected_repository_root=repository_root,
            expected_branch=expected_branch if isinstance(expected_branch, str) else None,
        )
        for field in (
            "checkout_identity", "git_common_directory_identity", "observed_path",
            "branch", "head", "repository_root", "workspace_root",
        ):
            recorded = owner_authorized_checkout.get(field)
            if isinstance(recorded, str) and observed.get(field) != recorded:
                raise ValueError(f"owner-authorized checkout {field} changed")
        if selected is not None:
            for field in (
                "checkout_identity", "git_common_directory_identity", "observed_path",
            ):
                recorded = selected.get(field)
                if isinstance(recorded, str) and observed.get(field) != recorded:
                    raise ValueError(f"selected checkout {field} changed")
        return Path(observed["path"]), observed

    expected_repository_root = root / repository
    identity_workspace_root = root
    root_matches_repository = (
        root.name == repository
        and isinstance(workspace_module._manifest(root), dict)
        and workspace_module._manifest(root).get("repository") == repository
    )
    root_checkout_facts = None
    if root_matches_repository:
        # A selected linked worktree can itself be the Environment workspace.
        # Its manifest matches the repository name, but its Git common
        # directory still points at the actual repository root. Resolve that
        # identity through the shared checkout verifier before treating this
        # path as the common repository root.
        try:
            root_checkout_facts = workspace_module.verify_registered_checkout(
                root, repository=repository,
            )
        except workspace_module.WorkspaceResolutionError:
            pass
    repository_root_is_workspace = (
        root_checkout_facts is not None
        and Path(root_checkout_facts["repository_root"]) == root
    )
    if repository_root_is_workspace:
        # Some existing project definitions use the repository root itself
        # as their authorized workspace. Verify Git's common directory from
        # its canonical parent, then confine the accepted checkout to this
        # exact repository root.
        expected_repository_root = root
        identity_workspace_root = root.parent
    elif root_checkout_facts is not None:
        # The Environment is rooted at a registered linked worktree. Keep its
        # checkout as the default target and use the Git-verified repository
        # identity for registration checks. Alternate linked checkouts are
        # accepted only through the authorized-checkout anchor below.
        expected_repository_root = Path(root_checkout_facts["repository_root"])
        identity_workspace_root = Path(root_checkout_facts["workspace_root"])
    if selected is None:
        requested_path = root if root_checkout_facts is not None else expected_repository_root
        expected_branch = None
    else:
        if selected.get("repository", repository) != repository:
            raise ValueError("selected checkout names a different repository")
        raw_path = selected.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise ValueError("selected checkout has no canonical path")
        requested_path = Path(raw_path)
        if not requested_path.is_absolute():
            requested_path = root / requested_path
        expected_branch = selected.get("branch")
        if expected_branch is not None and not isinstance(expected_branch, str):
            raise ValueError("selected checkout branch is malformed")

    if authorized_checkout is not None and not repository_root_is_workspace:
        # A campaign workspace can contain one registered worktree while the
        # repository's main checkout lives elsewhere. Permit that exact
        # destination only after anchoring it to the selected checkout's Git
        # common directory and confirming the anchor is inside this session's
        # workspace. The repository name in the intent is not, by itself,
        # authority to follow an arbitrary path outside that workspace.
        anchor_value = authorized_checkout.get("path")
        if not isinstance(anchor_value, str) or not anchor_value:
            raise ValueError("authorized checkout has no canonical path")
        anchor_path = Path(anchor_value)
        if not anchor_path.is_absolute():
            anchor_path = root / anchor_path
        try:
            absolute_anchor = Path(os.path.abspath(anchor_path))
            anchor_path = absolute_anchor.resolve(strict=True)
            if absolute_anchor != anchor_path:
                raise ValueError("authorized checkout path traverses a symbolic link")
            anchor_relative = anchor_path.relative_to(root)
        except (OSError, ValueError) as error:
            if isinstance(error, ValueError) and "symbolic link" in str(error):
                raise
            raise ValueError("authorized checkout escapes its Environment workspace") from None
        cursor = root
        for part in anchor_relative.parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise ValueError("authorized checkout path traverses a symbolic link")
        anchor_facts = workspace_module.verify_registered_checkout(
            anchor_path,
            repository=repository,
            expected_branch=authorized_checkout.get("branch"),
        )
        for field in (
            "checkout_identity", "git_common_directory_identity", "observed_path",
        ):
            recorded = authorized_checkout.get(field)
            if isinstance(recorded, str) and anchor_facts.get(field) != recorded:
                raise ValueError(f"authorized checkout {field} changed")
        anchor_repository_root = Path(anchor_facts["repository_root"])
        observed = workspace_module.verify_registered_checkout(
            requested_path,
            repository=repository,
            workspace_root=anchor_repository_root.parent,
            expected_repository_root=anchor_repository_root,
            expected_branch=expected_branch,
        )
        if observed.get("git_common_directory_identity") != anchor_facts.get(
            "git_common_directory_identity"
        ):
            raise ValueError(
                "claim checkout is not registered under the authorized checkout's Git common directory"
            )
    else:
        if root_checkout_facts is not None and not repository_root_is_workspace:
            try:
                if requested_path.resolve(strict=True) != root:
                    raise ValueError(
                        "claim checkout is outside the selected linked-worktree workspace"
                    )
            except OSError as error:
                raise ValueError("selected linked-worktree checkout is unavailable") from error
        observed = workspace_module.verify_registered_checkout(
            requested_path,
            repository=repository,
            workspace_root=identity_workspace_root,
            expected_repository_root=expected_repository_root,
            expected_branch=expected_branch,
        )
    if repository_root_is_workspace:
        try:
            Path(observed["path"]).resolve(strict=True).relative_to(root)
        except (OSError, ValueError):
            raise ValueError("selected worktree escapes its repository-root workspace") from None
    if selected is not None:
        for field in (
            "checkout_identity", "git_common_directory_identity", "observed_path",
        ):
            recorded = selected.get(field)
            if isinstance(recorded, str) and observed.get(field) != recorded:
                raise ValueError(f"selected checkout {field} changed")
    return Path(observed["path"]), observed


def _verified_declared_provider_checkout(
    snapshot: dict[str, Any], repository: str, checkout: str | Path,
    *, expected_branch: str | None = None,
) -> dict[str, str] | None:
    """Verify an exact nested checkout named by a bound provider.

    Some application providers are declared by a campaign checkout but execute
    from a separate repository root nested beneath it. Such a checkout is not
    necessarily a linked worktree of the repository's workspace root. It is
    still an admissible narrow claim target when the durable capability
    binding names that exact provider root, Git verifies its repository and
    manifest identities, and the path stays inside the session workspace.

    This does not make arbitrary nested Git directories claimable: the request
    must match a provider root already present in the authenticated session.
    """
    candidate_value = Path(checkout).expanduser()
    try:
        absolute_candidate = Path(os.path.abspath(candidate_value))
        candidate = absolute_candidate.resolve(strict=True)
    except OSError as error:
        raise ValueError("declared provider checkout is unavailable") from error
    if absolute_candidate != candidate:
        raise workspace_module.WorkspaceResolutionError(
            "declared provider checkout traverses a symbolic link",
            diagnostics={
                "code": "working-checkout-unavailable",
                "repository": repository,
                "path": str(candidate_value),
                "reason": "symbolic-link-escape",
            },
        )

    declared = []
    bindings = snapshot.get("bindings", [])
    if isinstance(bindings, list):
        for binding in bindings:
            if (not isinstance(binding, dict)
                    or binding.get("provider") != repository):
                continue
            raw_root = binding.get("provider_root")
            if not isinstance(raw_root, str) or not raw_root:
                continue
            try:
                absolute_root = Path(os.path.abspath(Path(raw_root).expanduser()))
            except OSError:
                continue
            if absolute_root == candidate:
                declared.append(binding)
    if not declared:
        return None

    workspace_value = (snapshot.get("workspace") or {}).get("root")
    if not isinstance(workspace_value, str) or not workspace_value:
        raise ValueError("session has no authorized workspace for its provider checkout")
    try:
        absolute_workspace = Path(os.path.abspath(Path(workspace_value).expanduser()))
        authorized_workspace = absolute_workspace.resolve(strict=True)
        relative = candidate.relative_to(authorized_workspace)
    except (OSError, ValueError) as error:
        raise ValueError("declared provider checkout escapes the session workspace") from error
    if absolute_workspace != authorized_workspace:
        raise ValueError("session workspace traverses a symbolic link")
    cursor = authorized_workspace
    for component in relative.parts:
        cursor = cursor / component
        if cursor.is_symlink():
            raise ValueError("declared provider checkout traverses a symbolic link")

    common = workspace_module._git(
        candidate, "rev-parse", "--path-format=absolute", "--git-common-dir",
    )
    if common is None or common.returncode != 0:
        raise ValueError("declared provider checkout has no verifiable Git common directory")
    try:
        common_directory = Path(os.path.abspath(common.stdout.strip())).resolve(strict=True)
    except OSError as error:
        raise ValueError("declared provider Git common directory is unavailable") from error
    repository_root = common_directory.parent
    observed = workspace_module.verify_registered_checkout(
        candidate,
        repository=repository,
        workspace_root=repository_root.parent,
        expected_repository_root=repository_root,
        expected_branch=expected_branch,
    )
    for binding in declared:
        provenance = binding.get("provenance")
        recorded = provenance.get("checkout") if isinstance(provenance, dict) else None
        if not isinstance(recorded, dict):
            continue
        for field in (
            "path", "repository_root", "git_common_directory_identity",
            "checkout_identity", "branch", "head",
        ):
            expected = recorded.get(field)
            if isinstance(expected, str) and observed.get(field) != expected:
                raise ValueError(f"declared provider checkout {field} changed")
    return observed


def _resolve_declared_runtime_toolchain_checkouts(
    workspace_root: str | Path,
    provider_roots: dict[str, Path],
    provider_facts: dict[str, dict[str, Any]],
) -> tuple[dict[str, Path], dict[str, dict[str, Any]], dict[str, dict[str, str]]]:
    """Resolve exact manifest-declared toolchains without granting write scope.

    External dependencies are limited to their direct workspace checkout,
    which must have a matching repository identity and a clean Git tree.
    Already-selected checkouts keep their existing claim semantics. Runtime
    dependency facts are recorded in capability provenance, never in
    ``selected_checkouts`` where they could be mistaken for an effect target.
    """
    root = Path(workspace_root).resolve()
    references = capabilities_module.declared_repository_toolchain_references(
        provider_roots
    )
    roots: dict[str, Path] = {}
    facts: dict[str, dict[str, Any]] = {}
    diagnostics: dict[str, dict[str, str]] = {}
    for repository, declared_by in sorted(references.items()):
        selected_root = provider_roots.get(repository)
        read_only = selected_root is None
        candidate = Path(selected_root) if selected_root is not None else root / repository
        try:
            if candidate.is_symlink():
                raise ValueError("toolchain checkout is a symbolic link")
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
            if not resolved.is_dir():
                raise ValueError("toolchain checkout is not a directory")
        except (OSError, ValueError) as error:
            diagnostics[repository] = {
                "code": "runtime-toolchain-checkout-unavailable",
                "reason": str(error)[:240],
            }
            continue

        observed = workspace_module.inspect_repo(resolved, _refresh=True)
        if (observed is None or observed.git_error or not observed.head
                or observed.manifest_repository != repository):
            diagnostics[repository] = {
                "code": "runtime-toolchain-identity-unavailable",
                "reason": "checkout Git head or manifest repository identity is unavailable",
            }
            continue
        if read_only and observed.dirty:
            diagnostics[repository] = {
                "code": "runtime-toolchain-checkout-dirty",
                "reason": "unclaimed runtime toolchain checkout is not clean",
            }
            continue

        identity = _repository_checkout_identity(root, repository, resolved)
        source_ref = (
            "selected-checkout" if not read_only
            else "provider-declared-read-only-toolchain"
        )
        roots[repository] = resolved
        facts[repository] = {
            "repository": repository,
            "path": str(resolved),
            "branch": observed.branch,
            "head": observed.head,
            "clean": not bool(observed.dirty),
            "source_ref": source_ref,
            "authoritative_head": observed.head,
            "read_only": read_only,
            "declared_by": declared_by,
            **identity,
        }
    return roots, facts, diagnostics


def _provider_managed_checkouts(
    definition: dict[str, Any],
    workspace_root: str | Path,
    workspace_view: dict[str, Any],
) -> tuple[dict[str, Path], dict[str, dict[str, Any]]]:
    """Ask the declared workspace provider to select exact campaign checkouts.

    Environment only composes the provider capability and validates its
    bounded result shape. Git revision resolution and worktree mutations
    stay inside the provider.
    """
    selection = definition.get("managed_checkouts")
    provider = definition.get("workspace_provider")
    if selection is None and provider is None:
        return {}, {}
    if not isinstance(selection, list) or not selection:
        raise ValueError("workspace_provider requires a non-empty managed_checkouts list")
    if not isinstance(provider, dict):
        raise ValueError("managed_checkouts requires a declared workspace_provider")

    root = Path(workspace_root).resolve()
    repository = str(provider.get("repository", ""))
    checkout = str(provider.get("checkout", ""))
    capability = str(provider.get("capability", ""))
    expected_head = provider.get("revision")
    if not repository or not checkout or not capability or not isinstance(expected_head, str):
        raise ValueError("workspace_provider needs repository, checkout, capability, and pinned revision")
    relative = Path(checkout)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("workspace_provider.checkout must be repository-relative")
    provider_root = (root / repository / relative).resolve()
    try:
        provider_root.relative_to(root)
    except ValueError as error:
        raise ValueError("workspace provider checkout escapes workspace root") from error
    if (root / repository / relative).is_symlink() or not provider_root.is_dir():
        raise ValueError("workspace provider checkout is missing or a symbolic link")
    provider_record = next(
        (item for item in workspace_view.get("repositories", [])
         if Path(str(item.get("path", ""))).resolve() == provider_root),
        None,
    )
    if provider_record is None:
        raise ValueError("workspace provider checkout was not observed by the workspace provider")
    if provider_record.get("dirty"):
        raise ValueError("workspace provider checkout is dirty; refusing to bind it")
    if expected_head and provider_record.get("head") != expected_head:
        raise ValueError(
            f"workspace provider revision mismatch: expected {expected_head}, "
            f"observed {provider_record.get('head')}"
        )

    provider_bindings = capabilities_module.discover_capabilities(
        root,
        repository_roots={repository: provider_root},
        checkout_facts={repository: {
            "path": str(provider_root), "branch": provider_record.get("branch"),
            "head": provider_record.get("head"), "clean": True,
            "source_ref": "session-pinned-provider",
            "authoritative_head": provider_record.get("head"),
        }},
    )
    binding = next((item for item in provider_bindings if item.get("capability") == capability), None)
    if binding is None:
        raise ValueError(f"workspace provider does not declare capability {capability}")
    binding = capabilities_module.probe_availability(binding)
    if binding.get("availability", {}).get("status") != "available":
        raise ValueError(f"workspace provider capability is unavailable: {capability}")

    requests: list[dict[str, str]] = []
    expected: dict[str, dict[str, str]] = {}
    for item in selection:
        if not isinstance(item, dict):
            raise ValueError("managed_checkouts entries must be objects")
        name = str(item.get("repository", ""))
        slug = str(item.get("name", ""))
        branch = str(item.get("branch", ""))
        source_ref = str(item.get("source_ref", "origin/main"))
        if not name or not slug or not branch:
            raise ValueError("managed_checkouts entries need repository, name, and branch")
        if name in expected:
            raise ValueError(f"duplicate managed checkout request for {name}")
        requests.append({"repository": name, "name": slug, "branch": branch,
                         "source_ref": source_ref})
        expected[name] = {"path": f"{name}/.worktrees/{slug}",
                          "branch": branch, "source_ref": source_ref}

    invocation = capabilities_module.invoke(
        binding,
        ["prepare", "--workspace-root", str(root), "--requests-json",
         json.dumps(requests, separators=(",", ":"))],
        cwd=provider_root,
        timeout_seconds=180,
        output_limit_bytes=256 * 1024,
    )
    if invocation.get("status") != "ok":
        raise ValueError(
            f"workspace provider failed ({invocation.get('status')}): "
            f"{str(invocation.get('stderr', ''))[:1000]}"
        )
    try:
        result = json.loads(str(invocation.get("stdout", "")))
    except json.JSONDecodeError as error:
        raise ValueError("workspace provider returned invalid JSON") from error
    rows = result.get("selected_checkouts") if isinstance(result, dict) else None
    if not isinstance(rows, list) or {str(row.get("repository")) for row in rows if isinstance(row, dict)} != set(expected):
        raise ValueError("workspace provider returned an incomplete checkout closure")

    roots: dict[str, Path] = {}
    facts: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = str(row["repository"])
        requirement = expected[name]
        if row.get("path") != requirement["path"]:
            raise ValueError(f"workspace provider selected an unexpected path for {name}")
        if row.get("branch") != requirement["branch"] or row.get("source_ref") != requirement["source_ref"]:
            raise ValueError(f"workspace provider selected an unexpected branch or ref for {name}")
        if row.get("clean") is not True or row.get("head") != row.get("authoritative_head"):
            raise ValueError(f"workspace provider did not select a clean authoritative checkout for {name}")
        selected_root = (root / requirement["path"]).resolve()
        try:
            selected_root.relative_to(root)
        except ValueError as error:
            raise ValueError(f"workspace provider path escapes root for {name}") from error
        if (root / requirement["path"]).is_symlink() or not selected_root.is_dir():
            raise ValueError(f"workspace provider returned missing or symbolic checkout for {name}")
        roots[name] = selected_root
        facts[name] = dict(row)

    pinned = definition.get("pinned_checkouts", [])
    if not isinstance(pinned, list):
        raise ValueError("pinned_checkouts must be a list")
    for item in pinned:
        if not isinstance(item, dict):
            raise ValueError("pinned_checkouts entries must be objects")
        name = str(item.get("repository", ""))
        checkout_path = str(item.get("checkout", ""))
        revision = item.get("revision")
        relative_checkout = Path(checkout_path)
        if not name or not checkout_path or not isinstance(revision, str):
            raise ValueError("pinned_checkouts entries need repository, checkout, and revision")
        if relative_checkout.is_absolute() or ".." in relative_checkout.parts:
            raise ValueError("pinned checkout paths must be repository-relative")
        selected_root = (root / name / relative_checkout).resolve()
        if (root / name / relative_checkout).is_symlink() or not selected_root.is_dir():
            raise ValueError(f"pinned checkout is missing or a symbolic link: {name}")
        record = next(
            (candidate for candidate in workspace_view.get("repositories", [])
             if Path(str(candidate.get("path", ""))).resolve() == selected_root),
            None,
        )
        if record is None or record.get("dirty") or record.get("head") != revision:
            raise ValueError(f"pinned checkout is not clean at its declared revision: {name}")
        if name in roots:
            raise ValueError(f"repository appears more than once in selected checkout closure: {name}")
        roots[name] = selected_root
        facts[name] = {
            "repository": name,
            "path": str(selected_root),
            "branch": record.get("branch"),
            "head": record.get("head"),
            "clean": True,
            "source_ref": "session-pinned-checkout",
            "authoritative_head": revision,
        }

    session_checkout = definition.get("session_checkout")
    if session_checkout is not None:
        if not isinstance(session_checkout, dict):
            raise ValueError("session_checkout must be an object")
        name = str(session_checkout.get("repository", ""))
        checkout_path = str(session_checkout.get("checkout", ""))
        relative_checkout = Path(checkout_path)
        if not name or not checkout_path or relative_checkout.is_absolute() or ".." in relative_checkout.parts:
            raise ValueError("session_checkout needs a repository-relative checkout")
        selected_root = (root / name / relative_checkout).resolve()
        if (root / name / relative_checkout).is_symlink() or not selected_root.is_dir():
            raise ValueError("session checkout is missing or a symbolic link")
        record = next(
            (candidate for candidate in workspace_view.get("repositories", [])
             if Path(str(candidate.get("path", ""))).resolve() == selected_root),
            None,
        )
        if record is None or record.get("dirty"):
            raise ValueError("session checkout is not an observed clean checkout")
        if name in roots:
            raise ValueError(f"repository appears more than once in selected checkout closure: {name}")
        roots[name] = selected_root
        facts[name] = {
            "repository": name,
            "path": str(selected_root),
            "branch": record.get("branch"),
            "head": record.get("head"),
            "clean": True,
            "source_ref": "running-session-checkout",
            "authoritative_head": record.get("head"),
        }

    provider_fact = {
        "repository": repository,
        "path": str(provider_root),
        "branch": provider_record.get("branch"),
        "head": provider_record.get("head"),
        "clean": True,
        "source_ref": "session-pinned-provider",
        "authoritative_head": provider_record.get("head"),
        "capability": capability,
    }
    roots[repository] = provider_root
    facts[repository] = provider_fact
    return roots, facts


def resolve_environment(
    *,
    definition: dict[str, Any],
    workspace_root: str | Path,
    state_dir: str | Path,
    consumer_id: str,
    working_checkout: dict[str, str] | None = None,
    store: SessionStore | None = None,
    backend: str = "store",
    verify_on_open: bool = True,
) -> dict[str, Any]:
    """Resolve a declarative environment definition into an inspectable world."""
    resolved_root = workspace_module.validate_workspace_root(
        workspace_root, definition=definition
    )
    requirements = readiness_module.validate_requirements(definition)
    execution_roles = composition.validate(definition.get("execution_roles", {}))
    execution_compatibility_service = composition.validate_compatibility_service(
        definition.get("execution_compatibility_service"), requirements.get("services", []))
    repository_selection = workspace_module.repository_selection(definition)
    workspace_view = workspace_module.discover_workspace(resolved_root, repositories=repository_selection)
    scan = workspace_view.get("scan", {})
    if workspace_view.get("error") or scan.get("status") != "complete":
        message = workspace_view.get("error") or scan.get(
            "message", "workspace scan did not complete"
        )
        raise workspace_module.WorkspaceResolutionError(
            f"workspace resolution stopped safely: {message}",
            diagnostics={
                "code": "workspace-scan-incomplete",
                "root": str(resolved_root),
                "scan": scan,
            },
        )
    selected_roots, selected_facts = _provider_managed_checkouts(
        definition, resolved_root, workspace_view
    )
    provisioned = bool(selected_roots)
    if repository_selection is not None:
        for name in repository_selection:
            path = resolved_root / name
            record = next(repo for repo in workspace_view["repositories"] if Path(repo["path"]) == path)
            selected_roots[name] = path
            selected_facts[name] = {"repository": name, "path": str(path), "head": record.get("head"),
                                    "branch": record.get("branch"), "clean": not record.get("dirty"),
                                    "source_ref": "explicit-workspace-selection", "authoritative_head": record.get("head")}
    if working_checkout is not None:
        repository = str(working_checkout.get("repository", ""))
        if (repository not in (repository_selection or [])
                or repository in {
                    str(item.get("repository", ""))
                    for item in definition.get("managed_checkouts", [])
                    if isinstance(item, dict)
                }):
            raise workspace_module.WorkspaceResolutionError(
                "current checkout is outside the declared workspace selection",
                diagnostics={"code": "working-checkout-unavailable",
                             "repository": repository},
            )
        if working_checkout.get("workspace_root") != str(resolved_root):
            raise workspace_module.WorkspaceResolutionError(
                "current checkout identity belongs to a different workspace root",
                diagnostics={"code": "working-checkout-unavailable",
                             "repository": repository,
                             "path": str(working_checkout.get("path", "")),
                             "workspace_root": str(working_checkout.get("workspace_root", "")),
                             "expected_workspace_root": str(resolved_root)},
            )
        verified_checkout = workspace_module.verify_registered_checkout(
            str(working_checkout.get("path", "")),
            repository=repository,
            workspace_root=resolved_root,
            expected_repository_root=resolved_root / repository,
            expected_branch=working_checkout.get("branch"),
            expected_head=working_checkout.get("head"),
        )
        checkout = Path(verified_checkout["path"])
        observed = workspace_module.inspect_repo(checkout, _refresh=True)
        if (observed is None or observed.git_error or not observed.head
                or observed.manifest_repository != repository):
            raise workspace_module.WorkspaceResolutionError(
                "current checkout does not match its MNCS repository identity",
                diagnostics={"code": "working-checkout-unavailable",
                             "repository": repository, "path": str(checkout)},
            )
        selected_roots[repository] = checkout
        selected_facts[repository] = {
            "repository": repository,
            "path": str(checkout),
            "branch": verified_checkout["branch"] or None,
            "head": verified_checkout["head"],
            "clean": not bool(observed.dirty),
            "source_ref": "current-working-checkout",
            "authoritative_head": verified_checkout["head"],
            **{
                key: verified_checkout[key]
                for key in (
                    "checkout_identity", "git_common_directory_identity",
                    "observed_path",
                )
            },
        }
    runtime_toolchain_roots: dict[str, Path] = {}
    runtime_toolchain_facts: dict[str, dict[str, Any]] = {}
    runtime_toolchain_diagnostics: dict[str, dict[str, str]] = {}
    toolchain = None
    if selected_roots:
        # Reconcile the workspace view after provider provisioning so the
        # resolved session includes the exact checkouts it selected.
        if provisioned:
            workspace_view = workspace_module.discover_workspace(resolved_root)
        scan = workspace_view.get("scan", {})
        if workspace_view.get("error") or scan.get("status") != "complete":
            message = workspace_view.get("error") or scan.get(
                "message", "workspace scan did not complete"
            )
            raise workspace_module.WorkspaceResolutionError(
                f"workspace resolution stopped safely after provider selection: {message}",
                diagnostics={
                    "code": "workspace-scan-incomplete",
                    "root": str(resolved_root),
                    "scan": scan,
                },
            )
        workspace_view["selected_checkouts"] = selected_facts
        (runtime_toolchain_roots, runtime_toolchain_facts,
         runtime_toolchain_diagnostics) = _resolve_declared_runtime_toolchain_checkouts(
            resolved_root, selected_roots, selected_facts
        )
        workspace_view["runtime_toolchain_checkouts"] = [
            runtime_toolchain_facts[name]
            for name in sorted(runtime_toolchain_facts)
        ]
        workspace_view["runtime_toolchain_diagnostics"] = runtime_toolchain_diagnostics
        language_root = selected_roots.get("mncs-language")
        language_binary: Path | str | None = None
        if language_root is not None:
            candidates = (
                language_root / "target" / "release" / "mncs",
                language_root / "target" / "debug" / "mncs",
            )
            language_binary = next((path for path in candidates if path.is_file()), candidates[-1])
            language_facts = selected_facts["mncs-language"]
            toolchain = {
                "repository": "mncs-language",
                "checkout": str(language_root),
                "revision": language_facts.get("head"),
                "binary": str(language_binary) if language_binary.is_file() else None,
                "status": "available" if language_binary.is_file() else "unavailable",
            }
        discovered = capabilities_module.discover_capabilities(
            resolved_root,
            repository_roots=selected_roots,
            checkout_facts=selected_facts,
            runtime_toolchain_roots=runtime_toolchain_roots,
            runtime_toolchain_facts=runtime_toolchain_facts,
            language_binary=language_binary,
        )
    else:
        discovered = capabilities_module.discover_capabilities(resolved_root)
    toolchain = None
    language_root = selected_roots.get("mncs-language")
    language_record = selected_facts.get("mncs-language")
    if not selected_roots:
        language_record = next((repo for repo in workspace_view.get("repositories", [])
                                if (repo.get("manifest_repository") or repo.get("name")) == "mncs-language"), None)
        if language_record:
            language_root = Path(language_record["path"])
    if language_root is not None:
        candidates = (
            language_root / "target" / "release" / "mncs",
            language_root / "target" / "debug" / "mncs",
        )
        selected_binary = next((path for path in candidates if path.is_file()), candidates[-1])
        toolchain = {
            "repository": "mncs-language",
            "checkout": str(language_root),
            "revision": language_record.get("head"),
            "binary": str(selected_binary) if selected_binary.is_file() else None,
            "status": "available" if selected_binary.is_file() else "unavailable",
        }
    bindings = [capabilities_module.probe_availability(binding) for binding in discovered]
    unavailable = [
        {"provider": binding["provider"], "capability": binding["capability"],
         "reason": binding["availability"]["reason"]}
        for binding in bindings
        if binding["availability"]["status"] == "unavailable"
    ]
    unknown = [
        {"provider": binding["provider"], "capability": binding["capability"],
         "reason": binding["availability"]["reason"]}
        for binding in bindings
        if binding["availability"]["status"] == "unknown"
    ]
    owns_store = store is None
    backend_store = store
    if backend_store is None:
        store_provider = (
            store_provider_from_environment({
                "workspace": {"root": str(resolved_root)},
                "selected_checkouts": selected_facts,
                "toolchain": toolchain,
            })
            if backend == "store"
            else None
        )
        backend_store = open_store(
            state_dir,
            backend,
            verify_on_open=verify_on_open,
            defer_mutation=True,
            store_package_dir=(
                store_provider.get("python_package") if store_provider is not None else None
            ),
            store_runtime=(
                store_provider.get("runtime_environment")
                if store_provider is not None else None
            ),
        )
    intent = parse_intent(definition.get("intent", {"goal": definition.get("goal", "unspecified")}))
    protected = sorted(
        set(intent.get("protected_repositories", []))
        | set(definition.get("protected_repositories", []))
    )
    try:
        live_claims = claims_module.active_claims(backend_store.read_claims())
    finally:
        if owns_store:
            close = getattr(backend_store, "close", None)
            if callable(close):
                close()
    claim_holders: dict[str, list[dict[str, Any]]] = {}
    for record in live_claims.values():
        claim_holders.setdefault(str(record.get("repository", "")), []).append(
            {
                "claim_id": str(record.get("claim_id", "")),
                "session_id": str(record.get("session_id", "")),
                "consumer_id": str(record.get("consumer_id", "")),
                "basis": str(record.get("basis", "")),
                "scope": record.get("scope", {"kind": "repository"}),
            }
        )
    repo_names = [repo["name"] for repo in workspace_view.get("repositories", [])]
    rights = rights_module.evaluate_rights(
        workspace_repos=repo_names,
        records=rights_module.load_claim_records(definition, resolved_root),
    )
    rights_context_binding = definition.get("rights_context")
    rights_context_result = rights_module.load_agent_context(
        rights_context_binding if isinstance(rights_context_binding, dict) else None,
        resolved_root,
    )
    authority_context = authority_module.build_context(
        subject=consumer_id,
        intent=intent,
        protected_repos=protected,
        claim_holders=claim_holders,
    )
    inputs = {
        "workspace_root": str(resolved_root),
        "repository_heads": {
            (name if selected_roots else repo["name"]): (
                selected_facts[name].get("head") if selected_roots else repo.get("head")
            )
            for name, repo in (
                selected_roots.items() if selected_roots
                else ((item["name"], item) for item in workspace_view.get("repositories", []))
            )
        },
        "runtime_toolchain_checkouts": [
            {
                key: facts.get(key)
                for key in (
                    "repository", "observed_path", "branch", "head", "clean",
                    "checkout_identity", "git_common_directory_identity",
                    "source_ref", "read_only", "declared_by",
                )
            }
            for _, facts in sorted(runtime_toolchain_facts.items())
        ],
        "runtime_toolchain_diagnostics": runtime_toolchain_diagnostics,
        "binding_ids": sorted(binding["binding_id"] for binding in bindings),
        "intent": intent["identity"],
        "claim_holders": claim_holders,
    }
    if rights_context_result.get("context") is not None:
        inputs["rights_context"] = rights_context_result.get("inputs", {})
    definition_id = environment_id(definition)
    environment = {
        "schema_version": ENVIRONMENT_SCHEMA,
        "identity": resolved_environment_id(definition_id, inputs),
        "definition_id": definition_id,
        "configuration": {"name": definition.get("name"), "definition_id": definition_id},
        "requirements": requirements,
        "resolved_at": utcnow(),
        "consumer_id": consumer_id,
        "workspace": workspace_view,
        "selected_checkouts": selected_facts,
        "toolchain": toolchain,
        "execution_roles": execution_roles,
        "execution_compatibility_service": execution_compatibility_service,
        "execution_stack": composition.resolve(
            execution_roles, bindings, toolchain,
            compatibility_service=execution_compatibility_service,
        ),
        "repo_facts": _repo_facts(workspace_view),
        "bindings": bindings,
        "unavailable_capabilities": unavailable,
        "unknown_capabilities": unknown,
        "intent": intent,
        "protected_repositories": protected,
        "authority": authority_context,
        "claim_holders": claim_holders,
        "rights": rights,
        **({
            "rights_context": rights_context_result["context"],
            "rights_context_binding": dict(rights_context_binding),
            "rights_context_inputs": rights_context_result.get("inputs", {}),
        } if rights_context_result.get("context") is not None and isinstance(rights_context_binding, dict) else {}),
        "event_sources": [
            {"kind": "session-log", "replay": True},
            {"kind": "adapter:git-poll", "replay": False,
             "note": "polling adapter; canonical provider events are a pressure"},
        ],
        "resolution_inputs_digest": digest_hex(inputs),
    }
    return environment


def _apply_checkout_drift(readiness: dict[str, Any], execution_stack: dict[str, Any],
                          drift: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Add live checkout coherence to a read-only health composition."""
    summary = dict(readiness)
    stack = dict(execution_stack)
    if not drift:
        stack["selection_status"] = "current"
        return summary, stack
    unproven = any(
        reason in {"provider-checkout-unselected", "selected-checkout-path-unrecorded",
                   "selected-revision-unrecorded", "checkout-unreadable"}
        for item in drift for reason in item.get("reasons", [])
    )
    stack["selection_status"] = "unproven" if unproven else "stale"
    stack["selection_drift"] = drift
    compatibility = dict(stack.get("compatibility") or {})
    compatibility.update(
        state="unproven",
        evidence_stale=True,
        selection_drift=drift,
        reason=("a selected provider checkout could not be verified"
                if unproven else
                "a selected provider checkout no longer matches the bound revision, branch, or cleanliness"),
    )
    stack["compatibility"] = compatibility
    blockers = list(summary.get("blocking", []))
    prefix = "selected-checkout-unproven" if unproven else "selected-checkout-drift"
    blockers.extend(f"{prefix}:{item['provider']}" for item in drift)
    summary["blocking"] = sorted(set(blockers))
    summary["selection_drift"] = drift
    summary["status"] = "blocked"
    return summary, stack


class Session:
    """A durable session bound to a session store (snapshot + event log)."""

    def __init__(self, store: SessionStore, session_id: str):
        # Sessions always open through create/open/resume below.
        self.store = store
        self.session_id = session_id
        snapshot = store.load_snapshot(session_id)
        if not isinstance(snapshot, dict) or snapshot.get("session_id") != session_id:
            raise LifecycleError(f"session {session_id} has no readable snapshot")
        self.snapshot = snapshot
        self._next_seq_hint: int | None = None
        self._log_cache: list[dict[str, Any]] | None = None
        self._log_cache_key: Any = None
        # Store-generation baseline for observe_store; None means this
        # handle has not adopted yet (adopts silently on first observe).
        self._feed_generation: int | None = None

    def close(self) -> None:
        close = getattr(self.store, "close", None)
        if callable(close):
            close()

    def bind_task_identity(
        self,
        task_identity: str,
        *,
        campaign_id: str,
        authenticated_principal_id: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Bind an authenticated stable task key to this exact live session.

        The binding is a navigation index only. It does not resume the session,
        transfer claims, or authorize any operation on its own; normal session,
        claim, lifecycle, and rights admission still applies after lookup.
        """
        snapshot = self.snapshot
        campaign = snapshot.get("campaign")
        campaign = campaign if isinstance(campaign, dict) else {}
        if (not isinstance(authenticated_principal_id, str)
                or not authenticated_principal_id
                or snapshot.get("authenticated_principal_id")
                != authenticated_principal_id
                or campaign.get("principal_id") != authenticated_principal_id):
            raise AuthorityDenied(
                "task selector binding requires the session's authenticated principal"
            )
        if campaign.get("identity") != campaign_id:
            raise AuthorityDenied(
                "task selector campaign does not match this exact Environment session"
            )
        if snapshot.get("lifecycle") not in {
            "active", "blocked", "waiting", "checkpointed", "abandoned",
        }:
            raise LifecycleError(
                "task selector can bind only a continuable Environment session"
            )
        provenance = snapshot.get("provenance")
        provenance = provenance if isinstance(provenance, dict) else {}
        workspace = snapshot.get("workspace")
        workspace = workspace if isinstance(workspace, dict) else {}
        definition_id = provenance.get("definition_id")
        workspace_root = workspace.get("root")
        selector = campaign_selector.selector_identity(
            task_identity,
            authenticated_principal_id=authenticated_principal_id,
            definition_id=definition_id,
            workspace_root=workspace_root,
        )
        return campaign_selector.bind(
            self.store,
            selector=selector,
            authenticated_principal_id=authenticated_principal_id,
            definition_id=definition_id,
            workspace_root=workspace_root,
            campaign_id=campaign_id,
            session_id=self.session_id,
            request_id=request_id,
            admitted_by="mncs-environment.project-continuation",
        )


    @property
    def state_dir(self) -> Path:
        return getattr(self.store, "state_dir", Path("."))

    # -- construction ----------------------------------------------------

    @classmethod
    def create(
        cls,
        *,
        state_dir: Path | str,
        environment: dict[str, Any],
        consumer_id: str,
        consumer_kind: str = "agent",
        campaign_id: str | None = None,
        authenticated_principal_id: str | None = None,
        backend: str = "store",
        verify_on_open: bool = True,
        store: SessionStore | None = None,
    ) -> "Session":
        rights_module.check_enter(
            [repo["name"] for repo in environment.get("workspace", {}).get("repositories", [])],
            environment.get("rights", {}),
        )
        session_id = new_session_id(environment["identity"], consumer_id)
        store_provider = (
            store_provider_from_environment(environment)
            if backend == "store"
            else None
        )
        if store is not None and store_provider is not None:
            selected_package = Path(str(store_provider["python_package"])).resolve()
            actual_package = getattr(
                getattr(store, "backend", None), "store_package_dir", None
            )
            if actual_package is None or Path(str(actual_package)).resolve() != selected_package:
                raise ValueError(
                    "provided Store handle is not bound to the session-selected mncs-store checkout"
                )
            selected_runtime = store_provider.get("runtime_environment")
            actual_runtime = getattr(
                getattr(store, "backend", None), "store_runtime", None
            )
            if selected_runtime is not None and actual_runtime != selected_runtime:
                raise ValueError(
                    "provided Store handle is not bound to the session-selected MNCS toolchain"
                )
        store = store or open_store(
            state_dir,
            backend,
            verify_on_open=verify_on_open,
            defer_mutation=True,
            store_package_dir=(
                store_provider.get("python_package") if store_provider is not None else None
            ),
            store_runtime=(
                store_provider.get("runtime_environment")
                if store_provider is not None else None
            ),
        )
        if store_provider is not None:
            write_session_store_provider(state_dir, session_id, store_provider)
        selected_checkouts = environment.get("selected_checkouts", {})
        workspace_heads = (
            {name: selected.get("head") for name, selected in selected_checkouts.items()}
            if selected_checkouts
            else {
                repo["name"]: repo.get("head")
                for repo in environment.get("workspace", {}).get("repositories", [])
            }
        )
        snapshot = {
            "schema_version": SESSION_SCHEMA,
            "session_id": session_id,
            "environment_id": environment["identity"],
            "environment_digest": digest_hex(environment),
            "consumer_id": consumer_id,
            "consumer_kind": consumer_kind,
            "authenticated_principal_id": authenticated_principal_id,
            "campaign": {
                "identity": campaign_id,
                "work_intent_id": (environment.get("intent") or {}).get("identity"),
                "work_intent": {
                    "goal": (environment.get("intent") or {}).get("goal", ""),
                    "repositories": list((environment.get("intent") or {}).get("repositories", [])),
                    "protected_repositories": list(
                        (environment.get("intent") or {}).get("protected_repositories", [])
                    ),
                },
                "principal_id": authenticated_principal_id,
                "current_consumer_id": consumer_id,
                "authorized_consumers": [consumer_id],
                "session_ids": [session_id],
                "claim_ids": [],
                "repository_refs": [
                    {
                        "repository": str(name),
                        "observed_path": selected.get("path"),
                        "branch": selected.get("branch"),
                        "head": selected.get("head"),
                        "clean": selected.get("clean"),
                    }
                    for name, selected in sorted(
                        environment.get("selected_checkouts", {}).items()
                    )
                ],
                "evidence": [],
                "delivery": {"status": "pending"},
                "unresolved_pressures": [],
            },
            "lifecycle": "defined",
            "lifecycle_history": [{"state": "defined", "reason": "session created", "at": utcnow()}],
            "intent": environment.get("intent"),
            "authority": environment.get("authority"),
            "rights": environment.get("rights", {}),
            **({
                "rights_context": environment["rights_context"],
                "rights_context_binding": environment.get("rights_context_binding"),
                "rights_context_inputs": environment.get("rights_context_inputs", {}),
            } if isinstance(environment.get("rights_context"), dict) else {}),
            "workspace": environment.get("workspace", {}),
            "selected_checkouts": environment.get("selected_checkouts", {}),
            "toolchain": environment.get("toolchain"),
            "execution_roles": environment.get("execution_roles", {}),
            "execution_compatibility_service": environment.get("execution_compatibility_service"),
            "execution_stack": environment.get("execution_stack"),
            "configuration": environment.get("configuration", {}),
            "requirements": environment.get("requirements", {}),
            "service_observations": [],
            "repo_facts": environment.get("repo_facts", {}),
            "claim_holders": environment.get("claim_holders", {}),
            "bindings": environment.get("bindings", []),
            "workspace_heads": workspace_heads,
            "workspace_facts": _workspace_change_facts(
                environment.get("workspace", {}), selected_checkouts),
            "subscriptions": [],
            "artifacts": [],
            "decisions": [],
            "pressures": [],
            "checkpoints": [],
            "handoffs": [],
            "pending_handoff_id": None,
            "accepted_handoffs": [],
            "completion": None,
            "created_at": utcnow(),
            "updated_at": utcnow(),
            "snapshot_sequence": 0,
            "provenance": {
                "created_by": consumer_id,
                "definition_id": environment.get("definition_id"),
                "resolution_inputs_digest": environment.get("resolution_inputs_digest"),
            },
        }
        store.save_snapshot(session_id, snapshot)
        instance = cls(store, session_id)
        instance._emit("session.created", "environment", {"consumer_id": consumer_id})
        instance._save()
        return instance

    def continue_as(self, consumer_id: str, consumer_kind: str = "agent") -> None:
        """Record an authenticated campaign continuation under a new label.

        The caller must already have matched this session through its
        authenticated principal and campaign identity. A consumer label by
        itself never authorizes this transition.
        """
        previous = str(self.snapshot.get("consumer_id", ""))
        if previous == consumer_id and self.snapshot.get("consumer_kind") == consumer_kind:
            return
        campaign = dict(self.snapshot.get("campaign") or {})
        consumers = list(campaign.get("authorized_consumers", []))
        for value in (previous, consumer_id):
            if value and value not in consumers:
                consumers.append(value)
        campaign["authorized_consumers"] = consumers[-32:]
        campaign["current_consumer_id"] = consumer_id
        campaign["claim_ids"] = sorted(
            str(record.get("claim_id"))
            for record in claims_module.active_claims(self.store.read_claims()).values()
            if record.get("session_id") == self.session_id
        )
        self.snapshot["campaign"] = campaign
        self.snapshot["consumer_id"] = consumer_id
        self.snapshot["consumer_kind"] = consumer_kind
        self._emit("campaign.continued", "environment", {
            "campaign_id": campaign.get("identity"),
            "previous_consumer": previous,
            "consumer_id": consumer_id,
            "principal_id": self.snapshot.get("authenticated_principal_id"),
        })
        self._save()

    def reconcile_campaign_continuity(self) -> dict[str, Any]:
        """Rebuild the small campaign projection from immutable session facts."""
        campaign = dict(self.snapshot.get("campaign") or {})
        checkpoints = self.store.list_checkpoints(self.session_id)
        persisted_ids = [str(record["identity"]) for record in checkpoints
                         if isinstance(record.get("identity"), str)]
        live_claims = claims_module.active_claims(self.store.read_claims())
        own_claims = {
            claim_id: record for claim_id, record in live_claims.items()
            if record.get("session_id") == self.session_id
        }
        claims = sorted(str(record.get("claim_id")) for record in own_claims.values())
        prior_checkpoints = list(self.snapshot.get("checkpoints", []))
        prior_claims = list(campaign.get("claim_ids", []))
        prior_repository_refs = campaign.get("repository_refs", [])
        checkpoint_ids = list(dict.fromkeys([*prior_checkpoints, *persisted_ids]))
        missing = sorted(set(prior_checkpoints) - set(persisted_ids))
        campaign_state_history = _validated_campaign_state_history(
            checkpoints, campaign.get("identity")
        )
        campaign_state_record = (
            campaign_state_history[0] if campaign_state_history else None
        )
        evidence_history = (
            campaign_state_history[1] if campaign_state_history else None
        )
        campaign_state_changed = _project_campaign_state(
            campaign, campaign_state_record, evidence_history
        )
        self.snapshot["checkpoints"] = checkpoint_ids
        campaign["claim_ids"] = claims
        campaign["work_intent"] = campaign.get("work_intent") or {
            "goal": (self.snapshot.get("intent") or {}).get("goal", ""),
            "repositories": list((self.snapshot.get("intent") or {}).get("repositories", [])),
            "protected_repositories": list(
                (self.snapshot.get("intent") or {}).get("protected_repositories", [])
            ),
        }
        repository_refs = self._campaign_repository_refs(own_claims)
        refs_changed = repository_refs != prior_repository_refs
        campaign["repository_refs"] = repository_refs
        if missing:
            pressures = list(campaign.get("unresolved_pressures", []))
            known = {item.get("identity") for item in pressures if isinstance(item, dict)}
            for identity in missing:
                if identity not in known:
                    pressures.append({"type": "checkpoint-object-unavailable",
                                      "identity": identity})
            campaign["unresolved_pressures"] = pressures[-32:]
        self.snapshot["campaign"] = campaign
        recovered = sorted(set(checkpoint_ids) - set(prior_checkpoints))
        events = self._log()
        resumed_handoffs = {
            event.get("payload", {}).get("handoff_id")
            for event in events if event.get("type") == "session.resumed"
        }
        repaired_acceptance = False
        for accepted in self.snapshot.get("accepted_handoffs", []):
            if not isinstance(accepted, dict):
                continue
            handoff_identity = accepted.get("handoff_id")
            if not isinstance(handoff_identity, str) or handoff_identity in resumed_handoffs:
                continue
            result = accepted.get("result") or {}
            self._emit("session.resumed", str(accepted.get("consumer_id", "unknown")), {
                "previous_consumer": accepted.get("previous_consumer"),
                "handoff_id": handoff_identity,
                "authenticated_principal_id": accepted.get("authenticated_principal_id"),
                "source_authenticated_principal_id": accepted.get(
                    "source_authenticated_principal_id"
                ),
                "revalidation": result.get("divergence", {}),
            })
            resumed_handoffs.add(handoff_identity)
            repaired_acceptance = True
        if (recovered or prior_claims != claims or missing or refs_changed
                or repaired_acceptance or campaign_state_changed):
            self._emit("campaign.reconciled", "environment", {
                "campaign_id": campaign.get("identity"),
                "recovered_checkpoints": recovered,
                "unavailable_checkpoints": missing,
                "claim_ids": claims,
                "repository_refs_changed": refs_changed,
                "campaign_state_checkpoint_id": (
                    campaign_state_record.get("identity")
                    if campaign_state_record else None
                ),
            })
            self._save()
        return {"recovered_checkpoints": recovered,
                "unavailable_checkpoints": missing,
                "claim_ids": claims,
                "campaign_state_checkpoint_id": (
                    campaign_state_record.get("identity")
                    if campaign_state_record else None
                ),
                "campaign_state_changed": campaign_state_changed,
                "changed": bool(recovered or prior_claims != claims or missing
                                or refs_changed or campaign_state_changed)}

    @classmethod
    def open(
        cls, *, state_dir: Path | str, session_id: str, backend: str = "store",
        verify_on_open: bool = True, store: SessionStore | None = None,
    ) -> "Session":
        """Read-only open: loads state without appending any event."""
        store = store or open_store(
            state_dir,
            backend,
            verify_on_open=verify_on_open,
            defer_mutation=True,
            session_id=session_id,
        )
        return cls(store, session_id)

    @classmethod
    def resume(
        cls, *, state_dir: Path | str, session_id: str, backend: str = "store",
        verify_on_open: bool = True, store: SessionStore | None = None,
        request_id: str | None = None, request_operation: str = "resume",
    ) -> "Session":
        """Resume active participation: transitions abandoned sessions, logs resume."""
        instance = cls.open(
            state_dir=state_dir, session_id=session_id, backend=backend,
            verify_on_open=verify_on_open, store=store)
        operation_digest = None
        if request_id is not None:
            if (not isinstance(request_id, str) or not request_id
                    or len(request_id) > 160 or "\x00" in request_id):
                raise ValueError("resume request identity must be bounded nonempty text")
            if (not isinstance(request_operation, str) or not request_operation
                    or len(request_operation) > 256 or "\x00" in request_operation):
                raise ValueError("resume request operation must be bounded nonempty text")
            operation_digest = _request_operation_digest(
                session_id, request_id, request_operation, instance.snapshot
            )
            prior = [
                event for event in instance._log()
                if event.get("type") == "session.resumed"
                and event.get("payload", {}).get("request_id") == request_id
            ]
            if prior:
                if any(event.get("payload", {}).get("request_operation_digest")
                       != operation_digest for event in prior):
                    raise LifecycleError(
                        "request identity was already used for a different Environment operation"
                    )
                return instance
        if instance.snapshot.get("lifecycle") in ("completed", "failed"):
            raise LifecycleError(f"session {session_id} is terminal and cannot resume")
        if instance.snapshot.get("lifecycle") == "abandoned":
            instance.transition("active", "resumed from abandoned")
        instance._emit(
            "session.resumed", "environment",
            {"consumer_id": instance.snapshot.get("consumer_id")},
            request_id=request_id,
            operation_digest=operation_digest,
        )
        instance._save()
        return instance

    # -- persistence helpers ----------------------------------------------

    def _save(self) -> None:
        # Monotonic save sequence: every save is a new immutable Store
        # object. Concurrent participants can still race on the same revision;
        # persistence rejects differing contents rather than overwriting them.
        self.snapshot["snapshot_sequence"] = int(self.snapshot.get("snapshot_sequence", 0)) + 1
        self.snapshot["updated_at"] = utcnow()
        self.store.save_snapshot(self.session_id, self.snapshot)
        # Own write: re-baseline the store-feed observer so observe_store
        # only ever reports activity from outside this session.
        self._refresh_feed_baseline()

    def _reload_snapshot(self) -> None:
        """Adopt a same-session snapshot published by a bound provider."""
        latest = self.store.load_snapshot(self.session_id)
        if (not isinstance(latest, dict)
                or latest.get("session_id") != self.session_id):
            raise LifecycleError(
                f"session {self.session_id} became unreadable during capability invocation"
            )
        self.snapshot = latest
        self._next_seq_hint = None
        self._invalidate_log()
        self._refresh_feed_baseline()

    def _cache_key(self) -> Any:
        generation = getattr(self.store, "generation", None)
        if callable(generation):
            try:
                return ("store", generation())
            except Exception:
                return None
        return None

    def _log(self) -> list[dict[str, Any]]:
        key = self._cache_key()
        if key is not None and key == self._log_cache_key and self._log_cache is not None:
            return self._log_cache
        log = self.store.read_events(self.session_id)
        if key is not None:
            self._log_cache = log
            self._log_cache_key = key
        return log

    def _invalidate_log(self) -> None:
        self._log_cache = None
        self._log_cache_key = None

    def _emit(
        self,
        event_type: str,
        producer: str,
        payload: dict[str, Any] | None = None,
        causes: list[str] | None = None,
        *,
        request_id: str | None = None,
        operation_digest: str | None = None,
        expected_generation: int | None = None,
    ) -> dict[str, Any]:
        event_payload = dict(payload or {})
        if request_id is not None:
            if (not isinstance(request_id, str) or not request_id
                    or len(request_id) > 160 or "\x00" in request_id):
                raise ValueError("event request identity must be bounded nonempty text")
            if (not isinstance(operation_digest, str) or not operation_digest
                    or len(operation_digest) > 160 or "\x00" in operation_digest):
                raise ValueError("event operation digest is invalid")
            event_digest = digest_hex({
                "event_type": event_type,
                "producer": producer,
                "payload": event_payload,
                "causes": causes,
            })
            for existing in self._log():
                prior = existing.get("payload", {})
                if (existing.get("type") == event_type
                        and isinstance(prior, dict)
                        and prior.get("request_id") == request_id):
                    if prior.get("request_operation_digest") != operation_digest:
                        raise LifecycleError(
                            "request identity was already used for a different Environment operation"
                        )
                    if prior.get("request_event_digest") == event_digest:
                        return existing
            event_payload.update({
                "request_id": request_id,
                "request_operation_digest": operation_digest,
                "request_event_digest": event_digest,
            })
        last_error: Exception | None = None
        attempts = 1 if expected_generation is not None else MAX_EMIT_RETRIES
        for _ in range(attempts):
            if expected_generation is not None or self._next_seq_hint is None:
                sequences = self.store.existing_sequences(self.session_id)
                sequence_hint = (max(sequences) + 1) if sequences else 1
                if expected_generation is None:
                    self._next_seq_hint = sequence_hint
            else:
                sequence_hint = self._next_seq_hint
            sequence = sequence_hint
            event = events_module.make(
                session_id=self.session_id,
                sequence=sequence,
                event_type=event_type,
                producer=producer,
                payload=event_payload,
                causes=causes,
            )
            try:
                if expected_generation is None:
                    self.store.put_event(self.session_id, sequence, event)
                else:
                    self.store.put_event_at_generation(
                        self.session_id, sequence, event,
                        expected_generation=expected_generation,
                    )
                self._invalidate_log()
                self._next_seq_hint = sequence + 1
                return event
            except EventGenerationConflict:
                self._next_seq_hint = None
                raise
            except SequenceTaken as error:
                last_error = error
                self._next_seq_hint = None
                if expected_generation is not None:
                    raise EventGenerationConflict(
                        expected_generation, self.store.generation()
                    ) from error
                continue
        raise LifecycleError(f"event log unwritable after retries: {last_error}")

    # -- lifecycle ---------------------------------------------------------

    def transition(self, to_state: str, reason: str) -> dict[str, Any]:
        current = self.snapshot.get("lifecycle")
        allowed = TRANSITIONS.get(current, ())
        if to_state not in allowed:
            raise LifecycleError(f"illegal transition {current} -> {to_state}: {reason}")
        self.snapshot["lifecycle"] = to_state
        self.snapshot["lifecycle_history"].append({"state": to_state, "reason": reason, "at": utcnow()})
        self._save()
        return {"from": current, "to": to_state, "reason": reason}

    # -- intent / authority / capabilities ----------------------------------

    def attach_intent(self, raw_intent: dict[str, Any]) -> dict[str, Any]:
        intent = parse_intent(raw_intent)
        self.snapshot["intent"] = intent
        self._emit("intent.attached", self.snapshot.get("consumer_id", "unknown"),
                   {"intent_id": intent["identity"]})
        self._save()
        return intent

    def authority_scope_grants(self) -> list[dict[str, Any]]:
        """Project the current grant for each repository from immutable events.

        A grant refresh is a new immutable event linked to the grant it
        supersedes. Older records remain in the Store log for provenance, but
        only the latest correctly linked record contributes current authority.
        Grants created before refresh links were introduced retain their
        original append-order projection.
        """
        grants: dict[str, dict[str, Any]] = {}
        for event in self._log():
            if event.get("type") != "authority.scope-granted":
                continue
            payload = event.get("payload")
            grant = payload.get("scope_grant") if isinstance(payload, dict) else None
            if (not isinstance(grant, dict)
                    or grant.get("schema_version") != "mncs.environment.authority-scope-grant/1"
                    or grant.get("session_id") != self.session_id
                    or grant.get("campaign_id") != (self.snapshot.get("campaign") or {}).get("identity")):
                continue
            repository = grant.get("repository")
            if not isinstance(repository, str) or not repository:
                continue
            prior = grants.get(repository)
            refresh_of = grant.get("refresh_of")
            if refresh_of is not None:
                if (prior is None
                        or not isinstance(refresh_of, str)
                        or refresh_of != prior.get("identity")
                        or not isinstance(grant.get("identity"), str)
                        or grant.get("identity") == refresh_of):
                    continue
            grants[repository] = dict(grant)
        return [grants[key] for key in sorted(grants)]

    def effective_authority(self) -> dict[str, Any]:
        """Return the durable authority projection, including admitted grants."""
        authority = dict(self.snapshot.get("authority") or {})
        denied = set(authority.get("denied", []))
        # Older durable sessions stored merge and publish as unconditional
        # escalation actions. Reproject these stable policy categories at
        # read time so resumed sessions expose the same rules as new entries.
        authority["escalation_required"] = sorted(
            action for action in authority_module.ESCALATION_ACTIONS
            if action not in denied
        )
        authority["claim_required_actions"] = sorted(
            action for action in authority_module.CLAIM_REQUIRED_ACTIONS
            if action not in denied
        )
        grants = self.authority_scope_grants()
        protected = set(authority.get("protected_repositories", []))
        claim_fences = authority.get("claim_fences", [])
        fenced = {
            str(item.get("repository"))
            for item in claim_fences
            if isinstance(item, dict) and isinstance(item.get("repository"), str)
        }
        granted = {
            str(item["repository"])
            for item in grants
            if isinstance(item.get("repository"), str)
        }
        authority["writable"] = sorted(
            (set(authority.get("writable", [])) | granted) - protected - fenced
        )
        authority["scope_grants"] = grants
        authority["scope_grant_repositories"] = sorted(granted - protected - fenced)
        authority["claim_fences"] = sorted(
            (dict(item) for item in claim_fences if isinstance(item, dict)),
            key=lambda item: (str(item.get("repository", "")),
                              str(item.get("claim_id", ""))),
        )
        authority["revision"] = self.authority_revision(authority)
        return authority

    @staticmethod
    def authority_revision(authority: dict[str, Any]) -> str:
        """Hash only authority law, excluding changing claim-holder observations."""
        grants = authority.get("scope_grants", [])
        payload = {
            "subject": authority.get("subject"),
            "writable": sorted(set(authority.get("writable", []))),
            "protected_repositories": sorted(set(authority.get("protected_repositories", []))),
            "denied": sorted(set(authority.get("denied", []))),
            "grants": authority.get("grants", []),
            "scope_grants": [
                {key: item.get(key) for key in (
                    "identity", "campaign_id", "repository", "scope",
                    "authorized_by_principal_id", "approval_reference",
                    "authority_revision_before",
                )}
                for item in grants if isinstance(item, dict)
            ],
        }
        fences = authority.get("claim_fences", [])
        if fences:
            payload["claim_fences"] = [
                {key: item.get(key) for key in (
                    "repository", "claim_id", "superseded_version",
                    "fencing_generation", "recovery_authorization_id",
                )}
                for item in fences if isinstance(item, dict)
            ]
        return digest_hex(payload, length=64)

    def grant_repository_scope(
        self,
        *,
        repository: str,
        campaign_id: str,
        expected_authority_revision: str,
        approval_reference: str,
        owner_authorized_checkout: dict[str, Any] | None = None,
        authenticated_principal_id: str | None,
        request_id: str | None,
    ) -> dict[str, Any]:
        """Record one owner-admitted repository write scope for this campaign.

        The authenticated Control transport allowlists the campaign, repository,
        and approval reference before calling this Environment-owned operation.
        Environment independently checks the session owner, campaign, authority
        revision, workspace boundary, clean Git identity, and competing claims.
        The immutable event is the source of truth; retries reconcile by request
        identity, even if the process stopped after Store publication.
        """
        if not isinstance(repository, str) or not _CAMPAIGN_REPOSITORY_PATTERN.fullmatch(repository):
            raise AuthorityScopeConflict(
                "authority-scope-invalid",
                "repository scope must name one safe workspace checkout",
                next_step="select one immediate repository under the campaign workspace",
            )
        if not isinstance(campaign_id, str) or not campaign_id or len(campaign_id) > 160:
            raise AuthorityScopeConflict(
                "authority-scope-invalid", "campaign identity is invalid",
                next_step="read the authenticated campaign context and retry",
            )
        if (not isinstance(expected_authority_revision, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected_authority_revision)):
            raise AuthorityScopeConflict(
                "authority-scope-invalid", "expected authority revision is invalid",
                next_step="read the current Environment authority revision and retry",
            )
        if (not isinstance(approval_reference, str) or not approval_reference.strip()
                or len(approval_reference) > 256 or "\x00" in approval_reference):
            raise AuthorityScopeConflict(
                "authority-scope-invalid", "approval reference must be bounded nonempty text",
                next_step="supply the owner-recorded approval reference for this repository scope",
            )
        if (not isinstance(request_id, str)
                or not re.fullmatch(r"req_[A-Za-z0-9_-]{1,76}", request_id)):
            raise AuthorityScopeConflict(
                "authority-scope-invalid", "stable scope-grant request identity is required",
                next_step="retry through the authenticated Control Environment transport",
            )
        if not isinstance(authenticated_principal_id, str) or not authenticated_principal_id:
            raise AuthorityDenied("repository scope grants require a Control-authenticated principal")
        if authenticated_principal_id != self.snapshot.get("authenticated_principal_id"):
            raise AuthorityDenied("repository scope grant principal does not own this Environment session")
        if (self.snapshot.get("campaign") or {}).get("identity") != campaign_id:
            raise AuthorityDenied("repository scope grant campaign does not match the selected session")
        if self.snapshot.get("lifecycle") in ("completed", "failed"):
            raise AuthorityDenied("terminal Environment sessions cannot admit new repository scope")
        operation_digest = digest_hex({
            "operation": "environment-authority-scope-grant/1",
            "session_id": self.session_id,
            "campaign_id": campaign_id,
            "repository": repository,
            "expected_authority_revision": expected_authority_revision,
            "approval_reference": approval_reference,
            "owner_authorized_checkout": owner_authorized_checkout,
            "authenticated_principal_id": authenticated_principal_id,
        }, length=64)
        for event in self._log():
            payload = event.get("payload")
            if (event.get("type") != "authority.scope-granted"
                    or not isinstance(payload, dict)
                    or payload.get("request_id") != request_id):
                continue
            if payload.get("request_operation_digest") != operation_digest:
                raise AuthorityScopeConflict(
                    "authority-scope-request-conflict",
                    "scope-grant request identity is already bound to another operation",
                    next_step="read the campaign authority events before issuing a new request identity",
                )
            prior = payload.get("scope_grant")
            if isinstance(prior, dict):
                return dict(prior)

        generation_reader = getattr(self.store, "generation", None)
        if not callable(generation_reader):
            raise AuthorityScopeConflict(
                "authority-scope-cas-unavailable",
                "selected Environment persistence provider cannot fence scope admission",
                next_step="use a canonical Store provider with generation compare-and-swap support",
            )
        # The authority read and immutable grant publication form one
        # Store-generation transaction: another publisher advancing the
        # generation makes the grant CAS fail rather than accepting a stale
        # authority projection.
        expected_generation = int(generation_reader())
        self._invalidate_log()
        current = self.effective_authority()
        current_revision = str(current.get("revision", ""))
        if current_revision != expected_authority_revision:
            raise AuthorityScopeConflict(
                "authority-scope-conflict",
                "Environment authority changed since the scope grant was requested",
                next_step="read the current authority revision and retry with explicit owner approval",
            )
        if repository in current.get("protected_repositories", []):
            raise AuthorityDenied(f"repository scope is protected for this campaign: {repository}")
        existing_grants = [
            item for item in self.authority_scope_grants()
            if item.get("repository") == repository
        ]
        prior_grant = existing_grants[0] if len(existing_grants) == 1 else None
        if len(existing_grants) > 1:
            raise AuthorityScopeConflict(
                "authority-scope-conflict",
                f"repository has multiple current scope grants: {repository}",
                next_step="reconcile the immutable authority grant history before refreshing scope",
            )
        if repository in current.get("writable", []) and prior_grant is None:
            raise AuthorityScopeConflict(
                "authority-scope-already-granted",
                f"repository already appears in effective writable scope: {repository}",
                next_step="inspect the existing authority grant and claim before continuing",
            )
        if prior_grant is not None and (
            prior_grant.get("authorized_by_principal_id") != authenticated_principal_id
            or prior_grant.get("approval_reference") != approval_reference
            or prior_grant.get("scope") != {
                "kind": "repository", "repository": repository,
            }
        ):
            raise AuthorityDenied(
                "repository scope refresh must retain the original authenticated owner and approval"
            )

        workspace_root_value = (self.snapshot.get("workspace") or {}).get("root")
        if not isinstance(workspace_root_value, str) or not workspace_root_value:
            raise AuthorityDenied("Environment session has no owner-mapped workspace root")
        workspace_root = Path(workspace_root_value).resolve(strict=True)
        selected_checkouts = self.snapshot.get("selected_checkouts")
        selected = (
            selected_checkouts.get(repository)
            if isinstance(selected_checkouts, dict) else None
        )
        prior_identity = (
            prior_grant.get("repository_identity")
            if isinstance(prior_grant, dict) else None
        )
        if selected is None and isinstance(prior_identity, dict):
            # The durable grant is the checkout selection for a repository
            # that was not in the original campaign closure. Keep refresh
            # anchored to that exact path and stable Git identity.
            selected = dict(prior_identity)
        try:
            resolved_checkout, checkout_facts = _verified_scope_checkout(
                workspace_root, repository,
                selected if isinstance(selected, dict) else None,
                owner_authorized_checkout=owner_authorized_checkout,
            )
        except (OSError, ValueError, workspace_module.WorkspaceResolutionError) as error:
            raise AuthorityDenied(
                f"repository scope requires one registered checkout inside the authorized workspace: {repository}"
            ) from error
        observed = workspace_module.inspect_repo(resolved_checkout, _refresh=True)
        if (observed is None or observed.git_error or not observed.head
                or observed.head != checkout_facts.get("head")
                or (observed.branch or "") != checkout_facts.get("branch")):
            raise AuthorityDenied("repository scope requires a fresh Git identity observation")
        rights = self.snapshot.get("rights") or {}
        right_subjects = rights.get("subjects") if isinstance(rights, dict) else None
        if isinstance(right_subjects, dict) and right_subjects.get(repository) == "blocked":
            raise AuthorityDenied(f"repository scope is blocked by Rights/Provenance: {repository}")

        claims = claims_module.active_claims(self.store.read_claims())
        competing = [
            item for item in claims.values()
            if item.get("repository") == repository
        ]
        requested_scope = claims_module.normalize_scope({
            "kind": "worktree",
            "checkout": str(resolved_checkout),
            "branch": observed.branch,
        }, repository)
        conflicting = []
        for item in competing:
            try:
                existing_scope = claims_module.normalize_scope(
                    item.get("scope") if isinstance(item.get("scope"), dict) else None,
                    repository,
                )
            except (TypeError, ValueError):
                # Malformed live ownership is conflicting by default.
                conflicting.append(item)
                continue
            if claims_module.scopes_conflict(requested_scope, existing_scope):
                conflicting.append(item)
        same_session_recovery = [
            item for item in conflicting
            if self._is_owner_recovered_claim(item, repository, campaign_id)
        ]
        preserved_evidence = None
        if prior_grant is None:
            if conflicting and (
                len(conflicting) != 1 or len(same_session_recovery) != 1
            ):
                raise AuthorityScopeConflict(
                    "authority-scope-conflict",
                    f"selected worktree scope has an existing live claim: {repository}",
                    next_step="obtain the recorded owner's handoff or wait for a valid recovery condition",
                )
        elif conflicting:
            # Refresh ownership is checked below against a single exact
            # worktree claim after the selected checkout identity is known.
            # In particular, an existing grant never makes a foreign claim
            # eligible for transfer.
            pass
        if observed.dirty and prior_grant is None:
            if len(same_session_recovery) != 1:
                raise AuthorityDenied(
                    f"repository scope admission refuses unproven dirty work: {repository}"
                )
            preserved_evidence = _preserved_campaign_evidence(
                resolved_checkout, observed.dirty_files,
                dirty_truncated=observed.dirty_truncated,
                untracked_count=observed.untracked_count,
            )
            if preserved_evidence is None:
                raise AuthorityDenied(
                    "recovered repository scope may preserve only a bounded set of "
                    "untracked campaign evidence JSON files"
                )

        repository_identity: dict[str, Any] = {
            **_repository_checkout_identity(
                (
                    Path(owner_authorized_checkout["repository_root"]).parent
                    if owner_authorized_checkout is not None else workspace_root
                ),
                repository,
                resolved_checkout,
            ),
            **(
                {
                    field: checkout_facts[field]
                    for field in (
                        "repository_root", "workspace_root", "git_common_directory",
                        "branch", "head",
                    )
                    if field in checkout_facts
                }
                if owner_authorized_checkout is not None else {}
            ),
            "path": str(resolved_checkout),
            "branch": observed.branch,
            "head": observed.head,
            "clean": not bool(observed.dirty),
        }

        refresh_of = None
        if prior_grant is not None:
            if not isinstance(prior_identity, dict):
                raise AuthorityScopeConflict(
                    "authority-scope-conflict",
                    f"existing repository scope grant has no checkout identity: {repository}",
                    next_step="reconcile the durable grant before refreshing it",
                )
            stable_identity_fields = (
                "path", "branch", "repository_root", "workspace_root",
                "git_common_directory", "manifest_identity", "checkout_identity",
                "git_common_directory_identity", "observed_path",
            )
            changed_identity = [
                field for field in stable_identity_fields
                if (field in prior_identity
                    and prior_identity.get(field) != repository_identity.get(field))
            ]
            if changed_identity:
                raise AuthorityScopeConflict(
                    "authority-scope-checkout-conflict",
                    f"repository scope refresh selected a different registered checkout: {repository}",
                    next_step="continue in the exact worktree recorded by the existing owner grant",
                )

            live_claims = claims_module.active_claims(self.store.read_claims())
            overlapping: list[dict[str, Any]] = []
            malformed = False
            for item in live_claims.values():
                if item.get("repository") != repository:
                    continue
                try:
                    existing_scope = claims_module.normalize_scope(
                        item.get("scope") if isinstance(item.get("scope"), dict) else None,
                        repository,
                    )
                except (TypeError, ValueError):
                    malformed = True
                    continue
                if claims_module.scopes_conflict(requested_scope, existing_scope):
                    overlapping.append(item)
            exact_claims = [
                item for item in overlapping
                if item.get("session_id") == self.session_id
                and item.get("consumer_id") == self.snapshot.get("consumer_id")
                and item.get("scope") == requested_scope
            ]
            if malformed or len(overlapping) != 1 or len(exact_claims) != 1:
                raise AuthorityScopeConflict(
                    "authority-scope-conflict",
                    f"repository scope refresh requires this session's single live claim on the exact worktree: {repository}",
                    next_step="acquire or renew the exact registered worktree claim, then retry the owner-approved refresh",
                )
            refresh_of = str(prior_grant.get("identity", "")) or None
            if refresh_of is None:
                raise AuthorityScopeConflict(
                    "authority-scope-conflict",
                    f"existing repository scope grant has no immutable identity: {repository}",
                    next_step="reconcile the durable grant before refreshing it",
                )

        if preserved_evidence is not None:
            repository_identity["preserved_untracked_evidence"] = preserved_evidence
        scope_grant: dict[str, Any] = {
            "schema_version": "mncs.environment.authority-scope-grant/1",
            "identity": "scope_" + digest_hex({
                "session_id": self.session_id,
                "campaign_id": campaign_id,
                "repository": repository,
                "request_id": request_id,
            }),
            "session_id": self.session_id,
            "campaign_id": campaign_id,
            "repository": repository,
            "scope": {"kind": "repository", "repository": repository},
            "authorized_by_principal_id": authenticated_principal_id,
            "requested_by_consumer_id": str(self.snapshot.get("consumer_id", "unknown")),
            "approval_reference": approval_reference,
            "authority_revision_before": current_revision,
            "admitted_by": "mncs-environment",
            "admitted_at": utcnow(),
            "repository_identity": repository_identity,
        }
        if refresh_of is not None:
            scope_grant["refresh_of"] = refresh_of
        after = dict(current)
        after["writable"] = sorted(set(current.get("writable", [])) | {repository})
        after["scope_grants"] = [*current.get("scope_grants", []), scope_grant]
        scope_grant["authority_revision_after"] = self.authority_revision(after)
        try:
            self._emit(
                "authority.scope-granted", "environment",
                {"scope_grant": scope_grant},
                request_id=request_id,
                operation_digest=operation_digest,
                expected_generation=expected_generation,
            )
        except EventGenerationConflict as error:
            raise AuthorityScopeConflict(
                "authority-scope-conflict",
                "canonical Store advanced while the repository scope grant was being admitted",
                next_step="read the current campaign authority and retry with a stable request identity",
            ) from error
        except NotImplementedError as error:
            raise AuthorityScopeConflict(
                "authority-scope-cas-unavailable",
                "selected Environment persistence provider cannot fence scope admission",
                next_step="use a canonical Store provider with generation compare-and-swap support",
            ) from error
        return scope_grant

    def _is_owner_recovered_claim(
        self, record: dict[str, Any], repository: str, campaign_id: str,
    ) -> bool:
        """Allow scope admission through this session's exact recovered lease.

        Claim recovery and repository scope admission are separate authority
        steps. The recovered lease is the one live claim that may accompany
        the second step; ordinary, transferred, foreign, or malformed claims
        still block scope admission.
        """
        raw_scope = record.get("scope")
        if not isinstance(raw_scope, dict):
            return False
        try:
            scope = claims_module.normalize_scope(raw_scope, repository)
        except (TypeError, ValueError):
            return False
        if raw_scope != scope:
            return False
        provenance = record.get("provenance")
        provenance = provenance if isinstance(provenance, dict) else {}
        recovery = provenance.get("owner_recovery")
        if not isinstance(recovery, dict):
            return False
        owner_scope = recovery.get("owner_scope")
        target_scope = recovery.get("target_scope")
        if not isinstance(owner_scope, dict) or not isinstance(target_scope, dict):
            return False
        try:
            normalized_owner_scope = claims_module.normalize_scope(
                owner_scope, repository,
            )
            normalized_target_scope = claims_module.normalize_scope(
                target_scope, repository,
            )
        except (TypeError, ValueError):
            return False
        expected_version = recovery.get("expected_claim_version")
        if (
            record.get("schema_version") != claims_module.SCHEMA
            or record.get("claim_id") != claims_module.claim_identity(repository, scope)
            or record.get("repository") != repository
            or record.get("scope") != scope
            or normalized_owner_scope != owner_scope
            or normalized_target_scope != target_scope
            or normalized_target_scope != scope
            or claims_module.scopes_conflict(
                normalized_owner_scope, normalized_target_scope,
            ) is None
            or record.get("status") != "held"
            or record.get("basis") != claims_module.BASIS_RECOVERY
            or record.get("session_id") != self.session_id
            or record.get("consumer_id") != self.snapshot.get("consumer_id")
            or type(expected_version) is not int
            or expected_version < 1
            or record.get("version") != expected_version + 2
            or recovery.get("schema_version") != (
                "mncs.environment.control-owner-claim-recovery/2"
            )
            or recovery.get("campaign_id") != campaign_id
            or recovery.get("claim_id") != record.get("claim_id")
            or recovery.get("repository") != repository
            or recovery.get("owner_session_id") == self.session_id
            or recovery.get("requester_session_id") != self.session_id
            or recovery.get("requester_principal_id")
            != self.snapshot.get("authenticated_principal_id")
        ):
            return False
        stable_fields = (
            "schema_version", "campaign_id", "requester_session_id",
            "requester_principal_id", "recovery_basis",
            "operator_approval_reference", "owner_scope", "target_scope",
            "claim_id", "repository", "expected_claim_version",
            "owner_session_id", "owner_consumer_id",
        )
        if any(field not in recovery for field in stable_fields):
            return False
        if recovery.get("authorization_id") != digest_hex(
            {field: recovery[field] for field in stable_fields}, length=64,
        ):
            return False
        recovered_from = provenance.get("recovered_from")
        if not isinstance(recovered_from, list) or not any(
            isinstance(item, dict)
            and item.get("claim_id") == record.get("claim_id")
            and item.get("owner_session") == recovery.get("owner_session_id")
            and item.get("version") == expected_version
            and item.get("superseded_version") == expected_version + 1
            for item in recovered_from
        ):
            return False
        try:
            expected_record = claims_module._with_identity(record)
        except (KeyError, TypeError, ValueError):
            return False
        return record.get("identity") == expected_record.get("identity")

    def _compose_authorized_scope_checkouts(self) -> dict[str, Any]:
        """Add only explicitly granted repositories with this session's live claim.

        Scope grants and claims are the authority inputs. Workspace selection is
        a projection of those records: an identical consumer label, a sibling
        directory, or a historical claim is never enough to add a provider to
        this session's capability closure.
        """
        selected = self.snapshot.get("selected_checkouts", {})
        if not isinstance(selected, dict):
            selected = {}
        grants = self.authority_scope_grants()
        report: dict[str, Any] = {
            "status": "unchanged",
            "composed": [],
            "retained": [],
            "removed": [],
            "skipped": [],
        }
        if not grants:
            return report

        workspace_root_value = (self.snapshot.get("workspace") or {}).get("root")
        if not isinstance(workspace_root_value, str) or not workspace_root_value:
            report["status"] = "unknown"
            report["skipped"] = [
                {"repository": item.get("repository"),
                 "reason": "workspace-root-unavailable"}
                for item in grants
            ]
            return report

        try:
            workspace_root = Path(workspace_root_value).resolve(strict=True)
        except OSError:
            report["status"] = "unknown"
            report["skipped"] = [
                {"repository": item.get("repository"),
                 "reason": "workspace-root-unavailable"}
                for item in grants
            ]
            return report

        campaign_id = (self.snapshot.get("campaign") or {}).get("identity")
        active = claims_module.active_claims(self.store.read_claims())
        observed_claims = list(active.values())
        additions: dict[str, dict[str, Any]] = {}
        eligible_repositories: set[str] = set()
        for grant in grants:
            repository = grant.get("repository")
            if (not isinstance(repository, str)
                    or not _CAMPAIGN_REPOSITORY_PATTERN.fullmatch(repository)
                    or grant.get("session_id") != self.session_id
                    or grant.get("campaign_id") != campaign_id):
                report["skipped"].append({
                    "repository": repository,
                    "reason": "scope-grant-identity-invalid",
                })
                continue

            expected_identity = grant.get("repository_identity")
            if not isinstance(expected_identity, dict):
                report["skipped"].append({
                    "repository": repository,
                    "reason": "scope-grant-checkout-identity-missing",
                })
                continue

            selected_for_grant = None
            if isinstance(expected_identity.get("path"), str):
                selected_for_grant = {
                    "path": expected_identity["path"],
                    "branch": expected_identity.get("branch"),
                }
            try:
                resolved_checkout, checkout_facts = _verified_scope_checkout(
                    workspace_root, repository,
                    selected_for_grant,
                )
                observed = workspace_module.inspect_repo(resolved_checkout, _refresh=True)
                if (observed is None or observed.git_error or not observed.head
                        or observed.head != checkout_facts.get("head")
                        or (observed.branch or "") != checkout_facts.get("branch")):
                    raise ValueError("checkout Git identity is unavailable")
                current_identity = _repository_checkout_identity(
                    workspace_root, repository, resolved_checkout
                )
            except (OSError, RuntimeError, ValueError):
                report["skipped"].append({
                    "repository": repository,
                    "reason": "checkout-observation-unavailable",
                })
                continue

            rights = self.snapshot.get("rights") or {}
            right_subjects = rights.get("subjects") if isinstance(rights, dict) else None
            if isinstance(right_subjects, dict) and right_subjects.get(repository) == "blocked":
                report["skipped"].append({
                    "repository": repository,
                    "reason": "rights-provenance-blocked",
                })
                continue

            target_scope = claims_module.normalize_scope({
                "kind": "worktree",
                "checkout": str(resolved_checkout),
                "branch": observed.branch,
            }, repository)
            conflicting_claims = []
            malformed_scope = False
            for item in observed_claims:
                if item.get("repository") != repository:
                    continue
                raw_scope = item.get("scope")
                try:
                    current_scope = claims_module.normalize_scope(
                        raw_scope if isinstance(raw_scope, dict) else None,
                        repository,
                    )
                except (TypeError, ValueError):
                    malformed_scope = True
                    continue
                if claims_module.scopes_conflict(target_scope, current_scope):
                    conflicting_claims.append(item)
            if malformed_scope or len(conflicting_claims) != 1:
                report["skipped"].append({
                    "repository": repository,
                    "reason": (
                        "malformed-live-claim-scope" if malformed_scope else
                        "no-live-checkout-claim" if not conflicting_claims else
                        "multiple-live-checkout-claims"
                    ),
                })
                continue
            claim = conflicting_claims[0]
            claim_scope = claim.get("scope")
            same_worktree_claim = (
                isinstance(claim_scope, dict)
                and claim_scope.get("kind") == "worktree"
                and claim_scope.get("checkout") == str(resolved_checkout)
                and claim_scope.get("repository") == repository
                and (claim_scope.get("branch") or None)
                == (observed.branch or None)
            )
            legacy_repository_claim = (
                isinstance(claim_scope, dict)
                and claim_scope.get("kind") == "repository"
                and claim_scope.get("repository") == repository
                and claim_scope.get("exclusive") is True
            )
            if (claim.get("session_id") != self.session_id
                    or not (same_worktree_claim or legacy_repository_claim)):
                report["skipped"].append({
                    "repository": repository,
                    "reason": "claim-not-owned-matching-checkout-scope",
                })
                continue

            expected_evidence = expected_identity.get(
                "preserved_untracked_evidence"
            )
            observed_evidence = (
                _preserved_campaign_evidence(
                    resolved_checkout, observed.dirty_files,
                    dirty_truncated=observed.dirty_truncated,
                    untracked_count=observed.untracked_count,
                )
                if observed.dirty else None
            )
            if expected_evidence != observed_evidence:
                report["skipped"].append({
                    "repository": repository,
                    "reason": "preserved-campaign-evidence-identity-mismatch",
                })
                continue

            if any(expected_identity.get(field) != current_identity[field]
                   for field in current_identity):
                report["skipped"].append({
                    "repository": repository,
                    "reason": "checkout-identity-mismatch",
                })
                continue

            prior = selected.get(repository)
            if isinstance(prior, dict):
                prior_path = Path(str(prior.get("path", "")))
                if not prior_path.is_absolute():
                    prior_path = workspace_root / prior_path
                try:
                    same_path = prior_path.resolve() == resolved_checkout
                except OSError:
                    same_path = False
                prior_identity = prior.get("checkout_identity")
                if (not same_path
                        or (isinstance(prior_identity, str)
                            and prior_identity != current_identity["checkout_identity"])):
                    report["skipped"].append({
                        "repository": repository,
                        "reason": "selected-checkout-identity-conflict",
                    })
                    continue

            facts = {
                "repository": repository,
                "path": str(resolved_checkout),
                "branch": observed.branch,
                "head": observed.head,
                "clean": not bool(observed.dirty),
                "source_ref": "authenticated-scope-grant",
                "authoritative_head": observed.head,
                **current_identity,
            }
            composition = {
                "session_id": self.session_id,
                "campaign_id": campaign_id,
                "scope_grant_identity": grant.get("identity"),
                "claim_id": claim.get("claim_id"),
                "claim_version": claim.get("version"),
                "repository": repository,
                "checkout_identity": current_identity["checkout_identity"],
                "git_common_directory_identity": current_identity[
                    "git_common_directory_identity"
                ],
                "branch": observed.branch,
                "head": observed.head,
                "clean": not bool(observed.dirty),
            }
            request_id = "req_scope_checkout_" + digest_hex(composition, length=40)
            operation_digest = digest_hex({
                "operation": "environment-scope-checkout-compose/1",
                **composition,
            }, length=64)
            self._emit(
                "workspace.checkout-composed", "mncs-environment",
                {"composition": composition},
                request_id=request_id,
                operation_digest=operation_digest,
            )
            eligible_repositories.add(repository)
            if not isinstance(prior, dict):
                additions[repository] = facts
                report["composed"].append(repository)
            elif prior.get("source_ref") == "authenticated-scope-grant":
                additions[repository] = facts
                report["retained"].append(repository)
            else:
                report["retained"].append(repository)

        # A prior projection must stop contributing provider roots as soon as
        # its exact grant, exclusive claim, or checkout identity is no longer
        # verifiable. Explicit workspace selections remain untouched.
        removed = sorted(
            repository for repository, record in selected.items()
            if isinstance(record, dict)
            and record.get("source_ref") == "authenticated-scope-grant"
            and repository not in eligible_repositories
        )
        updated_selected = dict(selected)
        for repository in removed:
            updated_selected.pop(repository, None)
        updated_selected.update(additions)

        if additions or removed:
            workspace = dict(self.snapshot.get("workspace") or {})
            selection = workspace.get("selection")
            if not isinstance(selection, list):
                selection = list(selected)
            selected_names = {
                str(item) for item in selection if isinstance(item, str)
            }
            workspace["selection"] = sorted(
                (selected_names - set(removed)) | set(additions)
            )
            workspace["selected_checkouts"] = updated_selected
            self.snapshot["selected_checkouts"] = updated_selected
            self.snapshot["workspace"] = workspace
        report["removed"] = removed

        if report["skipped"]:
            report["status"] = "unknown"
        elif report["composed"] or report["retained"] or report["removed"]:
            report["status"] = "ready"
        return report

    def provision_checkouts(
        self,
        provider_capability: str,
        requests: list[dict[str, str]],
        *,
        timeout_seconds: int = 180,
    ) -> dict[str, Any]:
        """Extend this session's exact checkout closure through its provider.

        Environment composes the provider contract and validates the selected
        facts. The provider remains responsible for resolving Git revisions
        and creating or selecting worktrees. New checkouts become part of this
        session's authority and capability-binding closure only after the
        provider result agrees with a fresh workspace observation.
        """
        if self.snapshot.get("lifecycle") != "active":
            raise LifecycleError("checkout provisioning requires an active session")
        if not requests:
            raise ValueError("checkout provisioning requires at least one request")
        workspace_root_value = self.snapshot.get("workspace", {}).get("root")
        if not isinstance(workspace_root_value, str) or not workspace_root_value:
            raise ValueError("session has no resolved workspace root")
        workspace_root = Path(workspace_root_value).resolve()

        binding = self._binding(provider_capability)
        provider_repository = str(binding.get("provider", ""))
        selected_provider = self.snapshot.get("selected_checkouts", {}).get(provider_repository)
        if not provider_repository or not isinstance(selected_provider, dict):
            raise AuthorityDenied("checkout provider is not bound to a selected session checkout")
        provider_path = Path(str(selected_provider.get("path", "")))
        if not provider_path.is_absolute():
            provider_path = workspace_root / provider_path
        provider_path = provider_path.resolve()
        if provider_path != Path(str(binding.get("provider_root", ""))).resolve():
            raise AuthorityDenied("checkout provider binding does not match its selected checkout")
        try:
            provider_path.relative_to(workspace_root)
        except ValueError as error:
            raise AuthorityDenied("checkout provider escapes the session workspace") from error

        expected: dict[str, dict[str, str]] = {}
        normalized_requests: list[dict[str, str]] = []
        protected = set(self.snapshot.get("intent", {}).get("protected_repositories", []))
        selected = self.snapshot.get("selected_checkouts", {})
        for request in requests:
            if not isinstance(request, dict):
                raise ValueError("checkout requests must be objects")
            repository = str(request.get("repository", ""))
            name = str(request.get("name", ""))
            branch = str(request.get("branch", ""))
            source_ref = str(request.get("source_ref", "origin/main"))
            if not repository or not name or not branch:
                raise ValueError("checkout requests need repository, name, and branch")
            if repository in protected:
                raise AuthorityDenied(f"{repository} is protected by this session intent")
            if repository in selected:
                raise ValueError(f"{repository} is already selected in this session")
            if repository in expected:
                raise ValueError(f"duplicate checkout request for {repository}")
            if Path(name).is_absolute() or ".." in Path(name).parts or "/" in name or "\\" in name:
                raise ValueError("checkout name must be a single safe path component")
            relative_path = f"{repository}/.worktrees/{name}"
            expected[repository] = {
                "path": relative_path,
                "branch": branch,
                "source_ref": source_ref,
            }
            normalized_requests.append({
                "repository": repository,
                "name": name,
                "branch": branch,
                "source_ref": source_ref,
            })

        self._refresh_holders()
        self.snapshot.setdefault("authority", {})["claim_holders"] = dict(
            self.snapshot.get("claim_holders", {})
        )
        invocation = self.invoke(
            provider_capability,
            ["prepare", "--workspace-root", str(workspace_root), "--requests-json",
             json.dumps(normalized_requests, separators=(",", ":"))],
            cwd=provider_path,
            timeout_seconds=timeout_seconds,
            output_limit_bytes=256 * 1024,
        )
        if invocation.get("status") != "ok" or invocation.get("returncode") != 0:
            raise ValueError(
                f"checkout provider failed ({invocation.get('status')}): "
                f"{str(invocation.get('stderr', ''))[:1000]}"
            )
        try:
            result = json.loads(str(invocation.get("stdout", "")))
        except json.JSONDecodeError as error:
            raise ValueError("checkout provider returned invalid JSON") from error
        rows = result.get("selected_checkouts") if isinstance(result, dict) else None
        if (not isinstance(rows, list)
                or {str(row.get("repository")) for row in rows if isinstance(row, dict)}
                != set(expected)):
            raise ValueError("checkout provider returned an incomplete checkout selection")

        workspace = workspace_module.discover_workspace(workspace_root)
        if workspace.get("error") or workspace.get("scan", {}).get("status") != "complete":
            raise workspace_module.WorkspaceResolutionError(
                "workspace revalidation stopped safely; keeping the previous complete observation",
                diagnostics={"code": "workspace-scan-incomplete", "root": str(workspace_root),
                             "scan": workspace.get("scan", {}), "next": "check workspace availability, then reconcile the session"})
        repositories = workspace.get("repositories", [])
        selected_facts: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("checkout provider returned a malformed selection")
            repository = str(row.get("repository", ""))
            requirement = expected[repository]
            if row.get("path") != requirement["path"]:
                raise ValueError(f"checkout provider selected an unexpected path for {repository}")
            if (row.get("branch") != requirement["branch"]
                    or row.get("source_ref") != requirement["source_ref"]):
                raise ValueError(f"checkout provider selected an unexpected branch or ref for {repository}")
            if (row.get("clean") is not True or not row.get("head")
                    or row.get("head") != row.get("authoritative_head")):
                raise ValueError(
                    f"checkout provider did not select a clean authoritative checkout for {repository}"
                )
            selected_path = (workspace_root / requirement["path"]).resolve()
            try:
                selected_path.relative_to(workspace_root)
            except ValueError as error:
                raise ValueError(f"selected checkout escapes workspace for {repository}") from error
            if (workspace_root / requirement["path"]).is_symlink():
                raise ValueError(f"selected checkout is a symbolic link for {repository}")
            observed = next(
                (item for item in repositories
                 if Path(str(item.get("path", ""))).resolve() == selected_path),
                None,
            )
            if observed is None or observed.get("dirty") or observed.get("head") != row.get("head"):
                raise ValueError(
                    f"fresh workspace observation disagrees with provider selection for {repository}"
                )
            selected_facts[repository] = {
                **row,
                "path": requirement["path"],
                "missing": False,
            }

        raw_intent = {
            key: value for key, value in self.snapshot.get("intent", {}).items()
            if key not in ("identity", "schema_version")
        }
        raw_intent["repositories"] = sorted(
            set(raw_intent.get("repositories", [])) | set(selected_facts)
        )
        updated_intent = parse_intent(raw_intent)
        merged = {**selected, **selected_facts}
        workspace["selected_checkouts"] = merged
        self.snapshot["intent"] = updated_intent
        self.snapshot["selected_checkouts"] = merged
        self.snapshot["workspace"] = workspace
        self.snapshot["repo_facts"] = _repo_facts(workspace)
        self.snapshot["authority"] = authority_module.build_context(
            subject=self.snapshot.get("consumer_id", "unknown"),
            intent=updated_intent,
            protected_repos=self.snapshot.get("protected_repositories", []),
            claim_holders=self.snapshot.get("claim_holders", {}),
        )
        self.snapshot["workspace_heads"] = {
            **self.snapshot.get("workspace_heads", {}),
            **{name: facts.get("head") for name, facts in selected_facts.items()},
        }
        self.snapshot["workspace_facts"] = _workspace_change_facts(workspace, merged)

        language_root = None
        language_facts = merged.get("mncs-language")
        if isinstance(language_facts, dict):
            language_root = Path(str(language_facts.get("path", "")))
            if not language_root.is_absolute():
                language_root = workspace_root / language_root
        if language_root is not None:
            candidates = (
                language_root / "target" / "release" / "mncs",
                language_root / "target" / "debug" / "mncs",
            )
            selected_binary = next((path for path in candidates if path.is_file()), candidates[-1])
            self.snapshot["toolchain"] = {
                "repository": "mncs-language",
                "checkout": str(language_root.resolve()),
                "revision": language_facts.get("head"),
                "binary": str(selected_binary) if selected_binary.is_file() else None,
                "status": "available" if selected_binary.is_file() else "unavailable",
            }
        self.revalidate()
        self._emit(
            "workspace.closure.extended", "environment",
            {"provider": provider_capability,
             "repositories": sorted(selected_facts),
             "checkouts": [
                 {"repository": name, "path": facts["path"], "head": facts["head"],
                  "branch": facts["branch"]}
                 for name, facts in sorted(selected_facts.items())
             ]},
        )
        self._save()
        return {"selected_checkouts": list(selected_facts.values()), "bindings": self.snapshot.get("bindings", [])}

    def _canonical_scope(
        self, repository: str, scope: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if not scope or scope.get("kind") != "worktree":
            return scope
        checkout = scope.get("checkout")
        if not isinstance(checkout, str) or not checkout:
            return scope
        path = Path(checkout)
        workspace_root = self.snapshot.get("workspace", {}).get("root")
        if not path.is_absolute() and isinstance(workspace_root, str) and workspace_root:
            path = Path(workspace_root) / path
        normalized = dict(scope)
        # Keep symlink spelling intact until Git validates the full canonical
        # path at the mutation boundary; resolving here would hide an escape.
        normalized["checkout"] = str(Path(os.path.abspath(path)))
        return normalized

    def check(
        self,
        *,
        action: str,
        target: str,
        repo_facts: dict[str, dict[str, Any]] | None = None,
        scope: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        repository = target.split("/")[0] if "/" in target else target
        normalized_scope = self._canonical_scope(repository, scope)
        if action in ("write", "mutate", "execute", "publish", "merge", "delete", "retire"):
            # Current entry observations are not a mutation lease. Re-read
            # claims and the exact selected target at the authority boundary;
            # broad discovery is unnecessary on a quiet observational entry.
            self._refresh_holders()
            selected = self.snapshot.get("selected_checkouts", {}).get(repository)
            needs_selected_scope = (
                action in ("publish", "merge")
                and normalized_scope is None
                and target == repository
            )
            if (
                (repo_facts is None or needs_selected_scope)
                and isinstance(selected, dict)
                and selected.get("path")
            ):
                root = Path(self.snapshot.get("workspace", {}).get("root", "."))
                selected_path = Path(str(selected["path"]))
                if not selected_path.is_absolute():
                    selected_path = root / selected_path
                selected_path = selected_path.resolve()
                inspected = workspace_module.inspect_repo(selected_path, _refresh=True)
                if inspected is None or inspected.git_error:
                    raise LifecycleError("selected checkout observation unavailable at mutation boundary")
                record = inspected.record()
                record["name"] = repository
                if needs_selected_scope:
                    normalized_scope = self._canonical_scope(repository, {
                        "kind": "worktree",
                        "checkout": record["path"],
                        "branch": record.get("branch"),
                    })
                if repo_facts is None:
                    repo_facts = {**self.snapshot.get("repo_facts", {}),
                                  **_repo_facts({"repositories": [record]})}
        verdict = authority_module.evaluate(
            self.effective_authority(),
            action=action,
            target=target,
            session_id=self.session_id,
            claims=self.snapshot.get("claim_holders", {}),
            repo_facts=repo_facts if repo_facts is not None else self.snapshot.get("repo_facts", {}),
            scope=normalized_scope,
        )
        if verdict["verdict"] == "deny":
            self._emit("authority.denied", "environment",
                       {"action": action, "target": target, "reason": verdict["reason"]})
        elif verdict["verdict"] == "escalate":
            self._emit("authority.escalated", "environment",
                       {"action": action, "target": target, "reason": verdict["reason"]})
        return verdict

    def last_activity_at(self) -> str | None:
        """Newest event observation time, or None for a virgin session."""
        log = self._log()
        if not log:
            return None
        return str(log[-1].get("observed_at"))

    def _reconciler_log(self, limit: int = 100) -> list[dict[str, Any]]:
        """Bounded tail of the reconciler session log for cross-session items."""
        from . import reconciler as reconciler_module
        try:
            session_ids = self.store.list_sessions()
        except Exception:
            return []
        for session_id in session_ids:
            if session_id == self.session_id:
                continue
            try:
                other = Session(self.store, session_id)
            except Exception:
                continue
            if other.snapshot.get("consumer_id") == reconciler_module.RECONCILER_CONSUMER:
                return other._log()[-limit:]
        return []

    def brief(self, *, commons_work: dict[str, Any] | None = None) -> dict[str, Any]:
        """Compact update capsule: relevant delta + progress, cursor untouched."""
        from . import briefing as briefing_module
        return briefing_module.build_capsule(
            self, self.store, commons_work=commons_work,
            reconciler_log=self._reconciler_log())

    def catchup(self) -> dict[str, Any]:
        """Resume-time capsule: what changed while this consumer was away."""
        return self.brief()

    def updates(self, since: int | None = None) -> dict[str, Any]:
        """Classified event delta since an index (defaults to brief cursor)."""
        from . import briefing as briefing_module
        log = self._log()
        total = len(log)
        cursor = self.snapshot.get("brief_cursor", {})
        index = int(cursor.get("index", 0)) if isinstance(cursor, dict) else 0
        if since is not None:
            index = max(0, min(int(since), total))
        items = []
        for event in log[index:]:
            item = briefing_module.classify_event(event)
            if item is not None:
                items.append(item)
        return {"session_id": self.session_id, "index": index, "total": total,
                "items": items[:briefing_module.MAX_BRIEF_ITEMS],
                "truncated": len(items) > briefing_module.MAX_BRIEF_ITEMS}

    def ack(self, index: int) -> dict[str, Any]:
        """Acknowledge the brief cursor at an event index."""
        from . import briefing as briefing_module
        return briefing_module.acknowledge(
            self, index, str(self.snapshot.get("consumer_id", "unknown")))

    def _binding(self, capability: str) -> dict[str, Any]:
        for binding in self.snapshot.get("bindings", []):
            if binding.get("capability") == capability or binding.get("binding_id") == capability:
                return binding
        raise AuthorityDenied(f"no binding for capability {capability!r} in this session")

    def _selected_runtime_environment(self, binding: dict[str, Any]) -> dict[str, str]:
        selected_checkouts = self.snapshot.get("selected_checkouts", {})
        if not isinstance(selected_checkouts, dict) or not all(
            selected_checkouts.get(repository) for repository in ("mncs-store", "mncs-language")
        ):
            return {}
        provider = store_provider_from_environment({
            "workspace": self.snapshot.get("workspace", {}),
            "selected_checkouts": selected_checkouts,
            "toolchain": self.snapshot.get("toolchain"),
            "execution_stack": self.snapshot.get("execution_stack"),
        })
        if provider is None:
            return {}
        runtime = provider.get("runtime_environment")
        if not isinstance(runtime, dict):
            return {}
        contextual = {
            **runtime,
            "MNCS_STORE_PYTHON": provider["python_package"],
            "MNCS_STORE_ARTIFACT_CACHE": str(self.store.state_dir.resolve() / "provider-cache" / "mncs-store"),
            "MNCS_LANGUAGE_CHECKOUT": runtime["MNCS_LANGUAGE_ROOT"],
        }
        declared = set((binding.get("fixed_env") or {}).keys())
        declared_toolchain_env = binding.get("toolchain_env")
        if isinstance(declared_toolchain_env, str) and declared_toolchain_env:
            declared.add(declared_toolchain_env)
        return {key: value for key, value in contextual.items() if key not in declared}

    def _executor_identity(self, binding: dict[str, Any]) -> dict[str, Any]:
        """Attributable execution identity: provider plus its checkout."""
        provenance = binding.get("provenance", {})
        checkout = provenance.get("checkout", {}) if isinstance(provenance, dict) else {}
        path = (checkout.get("path") if isinstance(checkout, dict) else None)
        path = path or binding.get("provider_root")
        if isinstance(path, str) and path:
            workspace_root = self.snapshot.get("workspace", {}).get("root")
            raw = Path(path)
            if not raw.is_absolute() and workspace_root:
                raw = Path(str(workspace_root)) / raw
            try:
                path = str(raw.resolve())
            except OSError:
                path = str(raw)
        else:
            path = None
        return {"provider": str(binding.get("provider", "")),
                "capability": str(binding.get("capability", "")),
                "checkout": path}

    def _admit_effect_target(self, capability: str,
                             target: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        """Validate an explicit effect target against the session closure.

        The target repository must be session-observed (selected, else
        workspace-discovered). A supplied checkout must be exactly that
        checkout; a repository-only target resolves to that same observed
        checkout. Effect writes stay confined to claimed session scope, never
        to an arbitrary path the caller names. Anything else is denied,
        never escalated.
        """
        if not isinstance(target, dict):
            raise AuthorityDenied(
                f"capability {capability!r} names a malformed effect target")
        repository = target.get("repository")
        checkout = target.get("checkout")
        if not isinstance(repository, str) or not repository:
            raise AuthorityDenied(
                f"capability {capability!r} names an effect target without a repository")
        workspace_root = self.snapshot.get("workspace", {}).get("root")
        if not workspace_root:
            raise AuthorityDenied("session has no resolved workspace root")
        root = Path(str(workspace_root)).resolve()
        expected, branch = self._effect_checkout(repository, root)
        if expected is None:
            raise AuthorityDenied(
                f"capability {capability!r} effect target {repository!r} "
                "is not session-observed scope")
        if checkout is None:
            # Provider service contracts may name a repository selection;
            # Environment resolves it to the one checkout observed by this
            # session instead of accepting a provider-supplied ambient path.
            resolved = expected
        else:
            if not isinstance(checkout, str) or not checkout:
                raise AuthorityDenied(
                    f"capability {capability!r} names a malformed effect checkout")
            raw = Path(checkout)
            if not raw.is_absolute():
                raw = root / raw
            try:
                resolved = raw.resolve()
                resolved.relative_to(root)
            except (OSError, ValueError):
                raise AuthorityDenied(
                    f"capability {capability!r} effect target escapes the workspace root"
                ) from None
            if expected != resolved:
                raise AuthorityDenied(
                    f"capability {capability!r} effect target checkout does not match "
                    f"the session-observed checkout for {repository!r}")
        scope: dict[str, Any] = {"kind": "worktree", "checkout": str(resolved)}
        wanted = target.get("branch") or branch
        if isinstance(wanted, str) and wanted:
            scope["branch"] = wanted
        return repository, scope

    def _effect_checkout(self, repository: str, root: Path) -> tuple[Path | None, Any]:
        """Resolve a repository to its session-observed checkout and branch.

        Selected checkouts win; otherwise fall back to the workspace
        repositories discovered at entry (mirroring Doctor's target
        resolution). Anything else is outside the session closure.
        """
        selected = self.snapshot.get("selected_checkouts", {}).get(repository)
        if isinstance(selected, dict) and selected.get("path"):
            expected = Path(str(selected["path"]))
            if not expected.is_absolute():
                expected = root / expected
            try:
                return expected.resolve(), selected.get("branch")
            except OSError:
                return None, None
        read_only_dependencies = (
            self.snapshot.get("workspace", {}).get("runtime_toolchain_checkouts", [])
        )
        if any(
            isinstance(item, dict)
            and item.get("repository") == repository
            and item.get("read_only") is True
            for item in read_only_dependencies
        ):
            return None, None
        for repo in self.snapshot.get("workspace", {}).get("repositories", []):
            if not isinstance(repo, dict):
                continue
            if repo.get("name") != repository and repo.get("manifest_repository") != repository:
                continue
            try:
                return Path(str(repo["path"])).resolve(), repo.get("branch")
            except OSError:
                return None, None
        return None, None

    def _read_provider_input_artifacts(
        self, artifacts: list[dict[str, Any]] | None,
    ) -> list[dict[str, Any]]:
        if artifacts is None:
            return []
        if (not isinstance(artifacts, list)
                or len(artifacts) > invocation_artifacts_module.MAX_INPUT_ARTIFACTS):
            raise invocation_artifacts_module.InvocationArtifactError(
                "provider input artifact list exceeds its bound"
            )
        names: set[str] = set()
        total = 0
        prepared: list[dict[str, Any]] = []
        bindings = self.snapshot.get("bindings", [])
        for item in artifacts:
            if not isinstance(item, dict) or set(item) != {
                "name", "capability", "binding_id", "path",
            }:
                raise invocation_artifacts_module.InvocationArtifactError(
                    "provider input artifact request has unsupported fields"
                )
            name = item.get("name")
            source_capability = item.get("capability")
            source_binding_id = item.get("binding_id")
            if (not isinstance(name, str)
                    or not re.fullmatch(r"[A-Za-z][A-Za-z0-9._-]{0,79}", name)
                    or name in names):
                raise invocation_artifacts_module.InvocationArtifactError(
                    "provider input artifact names must be unique bounded identifiers"
                )
            if (not isinstance(source_capability, str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}",
                                        source_capability)
                    or not isinstance(source_binding_id, str)
                    or not re.fullmatch(r"cap_[A-Za-z0-9_-]{1,120}",
                                        source_binding_id)):
                raise invocation_artifacts_module.InvocationArtifactError(
                    "provider input artifact source identity is invalid"
                )
            matches = [
                binding for binding in bindings
                if isinstance(binding, dict)
                and binding.get("capability") == source_capability
                and binding.get("binding_id") == source_binding_id
            ] if isinstance(bindings, list) else []
            if len(matches) != 1:
                raise LifecycleError(
                    "provider input artifact requires one exact current source binding"
                )
            source_binding = matches[0]
            shareable_artifacts = source_binding.get("shareable_artifacts", [])
            if (source_binding.get("effects") != ["read"]
                    and (not isinstance(shareable_artifacts, list)
                         or item.get("path") not in shareable_artifacts)):
                raise AuthorityDenied(
                    "provider input artifact source must be read-only or declare this exact shareable artifact"
                )
            verdict = self.check(action="invoke", target=source_capability)
            if verdict.get("verdict") != "allow":
                raise AuthorityDenied(
                    "Environment did not authorize this source capability artifact"
                )
            metadata, content = invocation_artifacts_module.read_latest_invocation_file(
                self, source_capability, source_binding_id, item.get("path"),
                max_bytes=invocation_artifacts_module.MAX_INPUT_ARTIFACT_BYTES,
            )
            total += len(content)
            if total > invocation_artifacts_module.MAX_INPUT_TOTAL_BYTES:
                raise invocation_artifacts_module.InvocationArtifactError(
                    "provider input artifacts exceed their total size bound"
                )
            names.add(name)
            prepared.append({"name": name, **metadata, "content": content})
        return prepared

    def invoke(
        self,
        capability: str,
        argv: list[str],
        *,
        cwd: str | Path | None = None,
        timeout_seconds: int | None = None,
        output_limit_bytes: int | None = None,
        env: dict[str, str] | None = None,
        effect_target: dict[str, Any] | None = None,
        input_artifacts: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Invoke a bound capability. Deny and escalate NEVER spawn a process.

        Execution authority (running the provider) stays separate from
        effect authority (mutating state). By default the declared effects
        are checked against the provider's own checkout; pass
        ``effect_target`` (``{"repository", "checkout"?, "branch"?}``) when
        the provider legitimately mutates a different, explicitly claimed
        target checkout. Environment resolves a repository-only target to
        this session's exact observed checkout. Invocation records carry both
        executor and effect target.
        """
        binding = self._binding(capability)
        if output_limit_bytes is None:
            declared_limit = binding.get("output_limit_bytes")
            output_limit_bytes = (
                declared_limit if declared_limit is not None
                else capabilities_module.DEFAULT_OUTPUT_LIMIT_BYTES
            )
        output_limit_bytes = capabilities_module.validate_output_limit_bytes(
            output_limit_bytes
        )
        binding = capabilities_module.bind_session_context(
            binding, argv, session_id=self.session_id,
        )
        if binding.get("availability", {}).get("status") != "available":
            raise AuthorityDenied(
                f"capability {capability!r} is not available: "
                f"{binding.get('availability', {}).get('reason')}"
            )
        verdict = self.check(action="invoke", target=capability)
        if verdict["verdict"] == "deny":
            raise AuthorityDenied(verdict["reason"])
        if verdict["verdict"] == "escalate":
            return self._pending(capability, argv, "invoke", verdict["reason"])
        required = authority_module.required_action_for_effects(binding.get("effects", ["read"]))
        executor = self._executor_identity(binding)
        admitted_effect_target: dict[str, Any] | None = None
        if required != "read":
            if effect_target is not None:
                effect_repository, effect_scope = self._admit_effect_target(
                    capability, effect_target)
                admitted_effect_target = {"repository": effect_repository,
                                          **effect_scope}
                effect_verdict = self.check(
                    action=required, target=effect_repository, scope=effect_scope)
            else:
                provider = str(binding.get("provider", capability))
                checkout = binding.get("provenance", {}).get("checkout", {})
                checkout_path = checkout.get("path") or binding.get("provider_root")
                effect_scope = None
                if isinstance(checkout_path, str) and checkout_path:
                    selected_checkout = None
                    workspace_root = self.snapshot.get("workspace", {}).get("root")
                    raw_selected_path = Path(checkout_path)
                    if not raw_selected_path.is_absolute() and workspace_root:
                        raw_selected_path = Path(str(workspace_root)) / raw_selected_path
                    selected_path = raw_selected_path.resolve()
                    for repository, record in self.snapshot.get("selected_checkouts", {}).items():
                        raw_selected = Path(str(record.get("path", "")))
                        if not raw_selected.is_absolute() and workspace_root:
                            raw_selected = Path(str(workspace_root)) / raw_selected
                        if raw_selected.resolve() == selected_path:
                            provider = str(repository)
                            selected_checkout = record
                            break
                    effect_scope = {"kind": "worktree", "checkout": str(selected_path)}
                    branch = checkout.get("branch") or (
                        selected_checkout.get("branch") if selected_checkout else None
                    )
                    try:
                        declared_provider = _verified_declared_provider_checkout(
                            self.snapshot, provider, selected_path,
                            expected_branch=(
                                branch if isinstance(branch, str) and branch else None
                            ),
                        )
                    except (OSError, RuntimeError, ValueError,
                            workspace_module.WorkspaceResolutionError) as error:
                        raise AuthorityDenied(
                            f"capability {capability!r} provider checkout identity "
                            f"is unavailable: {error}"
                        ) from None
                    if declared_provider is not None:
                        branch = declared_provider.get("branch") or branch
                        for identity_key in (
                            "git_common_directory_identity", "checkout_identity",
                            "manifest_identity",
                        ):
                            identity_value = declared_provider.get(identity_key)
                            if isinstance(identity_value, str) and identity_value:
                                effect_scope[identity_key] = identity_value
                    if isinstance(branch, str) and branch:
                        effect_scope["branch"] = branch
                effect_verdict = self.check(
                    action=required, target=provider, scope=effect_scope)
            if effect_verdict["verdict"] == "deny":
                raise AuthorityDenied(effect_verdict["reason"])
            if effect_verdict["verdict"] == "escalate":
                return self._pending(capability, argv, required, effect_verdict["reason"])
        prepared_inputs = self._read_provider_input_artifacts(input_artifacts)
        input_provenance = [
            {key: value for key, value in item.items() if key != "content"}
            for item in prepared_inputs
        ]
        with invocation_artifacts_module.InvocationArtifactWorkspace(
            self, capability, binding.get("binding_id")
        ) as artifact_workspace:
            with invocation_artifacts_module.ProviderInputArtifactWorkspace(
                self.session_id, prepared_inputs,
            ) as input_workspace:
                invocation_env = dict(env or {})
                invocation_env.update(self._selected_runtime_environment(binding))
                invocation_env["MNCS_ENV_SESSION_ARTIFACT_DIR"] = str(artifact_workspace.path)
                invocation_env.update(input_workspace.environment())
                parent_request_id = os.environ.get("MNCS_ENV_RPC_REQUEST_ID")
                if (not isinstance(parent_request_id, str)
                        or not re.fullmatch(r"req_[A-Za-z0-9_-]{1,76}", parent_request_id)):
                    parent_request_id = None
                child_request = {
                    "kind": "provider-invocation",
                    "session_id": self.session_id,
                    "capability": capability,
                    "binding_id": binding.get("binding_id"),
                    "argv": list(argv),
                    "input_artifacts": input_provenance,
                }
                if parent_request_id is not None:
                    child_request["parent_request_id"] = parent_request_id
                else:
                    child_request["nonce"] = secrets.token_hex(20)
                provider_request_id = "req_" + digest_hex(child_request, length=48)
                invocation_env["MNCS_ENV_RPC_REQUEST_ID"] = provider_request_id
                self._emit("capability.invoked", self.snapshot.get("consumer_id", "unknown"),
                           {"capability": capability, "argv": argv, "executor": executor,
                            "effect_target": admitted_effect_target,
                            "input_artifacts": input_provenance,
                            "parent_request_id": parent_request_id,
                            "provider_request_id": provider_request_id})
                self._save()
                result = capabilities_module.invoke(
                    binding, argv, cwd=cwd, timeout_seconds=timeout_seconds,
                    output_limit_bytes=output_limit_bytes, env=invocation_env)
            artifact_fields = artifact_workspace.publish()
        # A declared provider may itself resume this Environment session and
        # publish domain state. Rebase the invocation result over that newer
        # snapshot instead of reusing our pre-execution CAS.
        self._reload_snapshot()
        result.update({
            key: artifact_fields[key]
            for key in ("artifact_store_ref", "artifact_file_count", "artifact_byte_count")
        })
        result["input_artifacts"] = input_provenance
        self.snapshot.setdefault("artifacts", []).append(
            {"kind": "invocation-result", "capability": capability,
             "binding_id": binding.get("binding_id"),
             "status": result["status"], "executor": executor,
             "effect_target": admitted_effect_target,
             "input_artifacts": input_provenance,
             "parent_request_id": parent_request_id,
             "provider_request_id": provider_request_id,
             **artifact_fields, "at": utcnow()}
        )
        self._emit("invocation.completed", binding.get("provider", "unknown"),
                   {"capability": capability, "status": result["status"],
                    "returncode": result["returncode"],
                    "input_artifacts": input_provenance,
                    "parent_request_id": parent_request_id,
                    "provider_request_id": provider_request_id,
                    "artifact_store_ref": artifact_fields.get("artifact_store_ref")})
        self._save()
        return result

    def _pending(self, capability: str, argv: list[str], action: str, reason: str) -> dict[str, Any]:
        self._emit("authority.escalated", "environment",
                   {"action": action, "target": capability, "reason": reason, "argv": argv})
        self._save()
        return {
            "binding_id": None,
            "capability": capability,
            "status": "pending-escalation",
            "returncode": None,
            "stdout": "",
            "stderr": f"escalation required, nothing executed: {reason}",
            "truncated": False,
        }

    # -- claims ---------------------------------------------------------------

    def _refresh_holders(self) -> None:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for record in claims_module.active_claims(self.store.read_claims()).values():
            grouped.setdefault(str(record.get("repository", "")), []).append(
                {
                    "claim_id": str(record.get("claim_id", "")),
                    "session_id": str(record.get("session_id", "")),
                    "consumer_id": str(record.get("consumer_id", "")),
                    "basis": str(record.get("basis", "")),
                    "scope": record.get("scope", {"kind": "repository"}),
                }
            )
        self.snapshot["claim_holders"] = grouped
        self.snapshot.setdefault("authority", {})["claim_holders"] = dict(grouped)

    def _campaign_repository_refs(
        self, live_claims: dict[str, dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Rebuild compact checkout refs from selected paths, claims, and Git."""
        root_value = (self.snapshot.get("workspace") or {}).get("root")
        if not isinstance(root_value, str) or not root_value:
            return list((self.snapshot.get("campaign") or {}).get("repository_refs", []))
        root = Path(root_value).resolve()
        candidates: dict[tuple[str, str], Path] = {}

        def add(repository: Any, value: Any) -> None:
            if (not isinstance(repository, str) or not repository
                    or not isinstance(value, str) or not value):
                return
            path = Path(value)
            if not path.is_absolute():
                path = root / path
            try:
                resolved = path.resolve()
                relative = resolved.relative_to(root).as_posix()
            except (OSError, ValueError):
                return
            if path.is_symlink() or not relative or relative == ".":
                return
            candidates[(repository, relative)] = resolved

        for repository, selected in (self.snapshot.get("selected_checkouts") or {}).items():
            if isinstance(selected, dict):
                add(repository, selected.get("path"))
        for item in (self.snapshot.get("workspace") or {}).get("repositories", []):
            if isinstance(item, dict):
                add(item.get("manifest_repository") or item.get("name"), item.get("path"))
        for claim in live_claims.values():
            if claim.get("session_id") != self.session_id:
                continue
            repository = str(claim.get("repository", ""))
            scope = claim.get("scope", {})
            checkout = scope.get("checkout") if isinstance(scope, dict) else None
            if isinstance(checkout, str) and checkout:
                add(repository, checkout)
                continue
            selected = (self.snapshot.get("selected_checkouts") or {}).get(repository, {})
            selected_path = selected.get("path") if isinstance(selected, dict) else None
            if selected_path:
                add(repository, selected_path)
            elif (Path(repository).name == repository and repository not in (".", "..")
                  and "/" not in repository and "\\" not in repository):
                add(repository, repository)

        observed: list[dict[str, Any]] = []
        for (repository, relative), checkout in sorted(candidates.items()):
            if not checkout.is_dir():
                continue
            facts = workspace_module.inspect_repo(checkout, _refresh=True)
            if facts is None or facts.head is None or facts.git_error:
                continue
            common = workspace_module._git(
                checkout, "rev-parse", "--path-format=absolute", "--git-common-dir"
            )
            remote = workspace_module._git(checkout, "config", "--get", "remote.origin.url")
            common_relative = None
            common_material: dict[str, Any] = {"repository": repository}
            if common is not None and common.returncode == 0:
                common_path = Path(common.stdout.strip()).resolve()
                try:
                    common_relative = common_path.relative_to(root).as_posix()
                except ValueError:
                    pass
                if common_relative is not None:
                    common_material["common_directory"] = common_relative
            if remote is not None and remote.returncode == 0 and remote.stdout.strip():
                # Hash the URL before persistence so remote credentials cannot
                # leak into the campaign capsule.
                common_material["remote_identity"] = digest_hex(remote.stdout.strip())
            common_identity = "git-common:" + digest_hex(common_material)
            observed.append({
                "repository": repository,
                "observed_path": relative,
                "checkout_identity": "checkout:" + digest_hex({
                    "repository": repository,
                    "path": relative,
                    "git_common_directory_identity": common_identity,
                }),
                "git_common_directory_relative": common_relative,
                "git_common_directory_identity": common_identity,
                "branch": facts.branch,
                "head": facts.head,
                "clean": not facts.dirty,
                "dirty_entry_count": len(facts.dirty_files),
                "observation": "current",
            })

        known = {(item.get("repository"), item.get("observed_path")) for item in observed}
        for item in (self.snapshot.get("campaign") or {}).get("repository_refs", []):
            if (isinstance(item, dict)
                    and (item.get("repository"), item.get("observed_path")) not in known):
                observed.append({**item, "observation": "unavailable_current_namespace"})
        return sorted(observed, key=lambda item: (
            str(item.get("repository", "")), str(item.get("observed_path", ""))))[:64]

    def acquire_claim(
        self, repository: str, *, basis: str = claims_module.BASIS_EXPLICIT,
        reason: str = "", ttl_hours: int = 24,
        scope: dict[str, Any] | None = None,
        checkout_facts: dict[str, Any] | None = None,
        workspace_root: str | None = None,
        request_id: str | None = None,
        expected_store_generation: int | None = None,
        _owner_recovery_authorization: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # A claim is an Environment-owned lease request. The caller must
        # already have effective write intent for its repository; merely
        # knowing a repository alias must not let a session reserve another
        # campaign's scope. Preserve idempotent readback after an ambiguous
        # publication even if the session's authority has since changed.
        prior_request = (
            any(
                record.get("provenance", {}).get("operation_request_id") == request_id
                for record in self.store.read_claims()
            )
            if request_id is not None
            else False
        )
        if not prior_request:
            authority = self.effective_authority()
            writable = authority.get("writable", [])
            protected = authority.get("protected_repositories", [])
            if repository in protected:
                raise AuthorityDenied(
                    f"claim denied: {repository} is protected in this Environment session"
                )
            if (_owner_recovery_authorization is None
                    and (not isinstance(writable, list) or repository not in writable)):
                raise AuthorityDenied(
                    f"claim denied: {repository} is outside this Environment session's writable authority"
                )
        selected_checkouts = self.snapshot.get("selected_checkouts")
        selected = (
            selected_checkouts.get(repository)
            if isinstance(selected_checkouts, dict) else None
        )
        owner_authorized_selection = False
        if selected is None:
            owner_grants = [
                grant for grant in self.authority_scope_grants()
                if grant.get("repository") == repository
            ]
            owner_identity = (
                owner_grants[0].get("repository_identity")
                if len(owner_grants) == 1 else None
            )
            if (isinstance(owner_identity, dict)
                    and isinstance(owner_identity.get("repository_root"), str)
                    and isinstance(owner_identity.get("path"), str)):
                try:
                    _resolved, verified_owner_identity = _verified_scope_checkout(
                        workspace_root or self.snapshot.get("workspace", {}).get("root") or ".",
                        repository,
                        owner_authorized_checkout=owner_identity,
                    )
                except (OSError, ValueError, workspace_module.WorkspaceResolutionError):
                    # The grant is durable authorization, not a promise that
                    # the selected worktree still exists. Refuse claim
                    # acquisition if its exact identity can no longer be
                    # established.
                    raise AuthorityDenied(
                        f"owner-authorized repository checkout is no longer valid: {repository}"
                    ) from None
                selected = {
                    **verified_owner_identity,
                    "repository": repository,
                    "path": verified_owner_identity["path"],
                    "clean": bool(owner_identity.get("clean", False)),
                }
                owner_authorized_selection = True
        explicit_scope = scope is not None
        if workspace_root is None:
            workspace_root = self.snapshot.get("workspace", {}).get("root")
        authorized_workspace = None
        if isinstance(workspace_root, str) and workspace_root:
            try:
                authorized_workspace = Path(workspace_root).resolve(strict=True)
            except OSError:
                # The canonical scope verifier below reports an unavailable
                # workspace. Do not let historical claims select an anchor
                # when the current boundary cannot be established.
                authorized_workspace = None
        if selected is None and explicit_scope:
            # A campaign can retain its exact checkout binding after the
            # time-limited mutation claim expires. That record remains
            # historical identity evidence, but never grants mutation rights:
            # every new claim below still passes normal live-conflict and
            # Store-publication checks. Keep only each claim's latest record,
            # and consider held worktree claims from this exact session.
            latest_owner_claims: dict[str, dict[str, Any]] = {}
            for record in self.store.read_claims():
                claim_id = record.get("claim_id")
                if (record.get("repository") != repository
                        or record.get("session_id") != self.session_id
                        or not isinstance(claim_id, str)):
                    continue
                previous = latest_owner_claims.get(claim_id)
                try:
                    version = int(record.get("version", 0))
                    previous_version = int(previous.get("version", 0)) if previous else -1
                except (TypeError, ValueError):
                    continue
                if previous is None or version > previous_version:
                    latest_owner_claims[claim_id] = record

            verified_anchors: list[dict[str, str]] = []
            for record in latest_owner_claims.values():
                if record.get("status") != "held":
                    continue
                owned_scope = record.get("scope")
                if (not isinstance(owned_scope, dict)
                        or owned_scope.get("kind") != "worktree"
                        or not isinstance(owned_scope.get("checkout"), str)
                        or not isinstance(owned_scope.get("branch"), str)):
                    continue
                try:
                    anchor_facts = workspace_module.verify_registered_checkout(
                        owned_scope["checkout"],
                        repository=repository,
                        expected_branch=owned_scope["branch"],
                    )
                except workspace_module.WorkspaceResolutionError:
                    # Removed, replaced, or branch-mismatched worktrees are
                    # not usable provenance. Other exact current claims may
                    # still provide a unique repository identity.
                    continue
                if any(
                    isinstance(owned_scope.get(field), str)
                    and owned_scope.get(field) != anchor_facts.get(field)
                    for field in (
                        "git_common_directory_identity", "checkout_identity",
                        "manifest_identity",
                    )
                ):
                    # A replacement Git repository at the same path and
                    # branch cannot inherit a claim with stronger identity
                    # provenance.
                    continue
                if authorized_workspace is None:
                    continue
                try:
                    Path(anchor_facts["path"]).resolve(strict=True).relative_to(
                        authorized_workspace
                    )
                except (OSError, ValueError):
                    # A retained claim outside this session's current
                    # workspace is historical identity only. It cannot steer
                    # selection of the checkout authorized for this claim.
                    continue
                verified_anchors.append(anchor_facts)

            common_directories = {
                facts["git_common_directory"]
                for facts in verified_anchors
            }
            if len(common_directories) > 1:
                conflicts = sorted({
                    (
                        facts["git_common_directory"],
                        facts["git_common_directory_identity"],
                        facts["path"],
                    )
                    for facts in verified_anchors
                })
                raise ValueError(
                    "session-owned worktree claims resolve to multiple Git common directories: "
                    + ", ".join(
                        f"{common_directory} ({identity}) at {path}"
                        for common_directory, identity, path in conflicts
                    )
                )
            if verified_anchors:
                # Multiple exact claims in separate worktrees of one common
                # repository establish the same repository identity. Choose a
                # stable in-workspace anchor; the requested checkout is still
                # verified independently against this common directory.
                anchor_facts = min(
                    verified_anchors,
                    key=lambda item: (item["path"], item["branch"] or ""),
                )
                selected = {
                    **anchor_facts,
                    "repository": repository,
                    "path": anchor_facts["path"],
                    "branch": anchor_facts["branch"],
                    "head": anchor_facts["head"],
                }
        claim_workspace_root = (
            selected.get("workspace_root")
            if owner_authorized_selection and isinstance(selected, dict)
            else workspace_root
        )
        if selected is not None and scope is None:
            # The ordinary claim follows the checkout already selected for
            # this session. A caller may name a different exact worktree
            # below, but that checkout must independently pass the same
            # workspace, Git registry, common-directory, manifest, and branch
            # identity checks as any selected checkout.
            scope = {
                "kind": "worktree",
                "checkout": str(selected.get("path", "")),
                "branch": selected.get("branch"),
                "exclusive": False,
            }
        if (scope and scope.get("kind") == "worktree"
                and selected is not None and not explicit_scope):
            # The selected checkout has already passed entry/resume
            # verification. Preserve this default for callers that omit a
            # scope; explicit destinations below receive a fresh Git identity
            # check before the lease can be published.
            scope = self._canonical_scope(repository, scope)
            observed = Path(str(selected.get("path", "")))
            if not observed.is_absolute() and workspace_root:
                observed = Path(workspace_root) / observed
            scope = {
                **scope,
                "checkout": str(observed.resolve()),
                "branch": selected.get("branch"),
                "exclusive": False,
            }
            if checkout_facts is None:
                clean = bool(selected.get("clean", False))
                checkout_facts = {
                    "head": selected.get("head"),
                    "branch": selected.get("branch"),
                    "dirty": not clean,
                    "foreign_signals": [] if clean else ["dirty-tree"],
                }
        elif scope and scope.get("kind") == "worktree":
            scope = self._canonical_scope(repository, scope)
            requested_path = str(scope.get("checkout", ""))
            requested_checkout = Path(requested_path)
            if not requested_checkout.is_absolute() and workspace_root:
                requested_checkout = Path(workspace_root) / requested_checkout
            declared_provider: dict[str, str] | None = None
            if owner_authorized_selection:
                if requested_checkout.resolve() != Path(str(selected.get("path"))).resolve():
                    raise ValueError(
                        "explicit claim checkout differs from the owner-authorized scope checkout"
                    )
                resolved_checkout, checkout_identity = _verified_scope_checkout(
                    workspace_root or ".",
                    repository,
                    owner_authorized_checkout=selected,
                )
            else:
                declared_provider = _verified_declared_provider_checkout(
                    self.snapshot, repository, requested_checkout,
                    expected_branch=(
                        scope.get("branch")
                        if isinstance(scope.get("branch"), str) else None
                    ),
                )
                if declared_provider is not None:
                    resolved_checkout = Path(declared_provider["path"])
                    checkout_identity = declared_provider
                else:
                    resolved_checkout, checkout_identity = _verified_scope_checkout(
                        workspace_root or ".",
                        repository,
                        {
                            "path": requested_path,
                            "branch": scope.get("branch"),
                        },
                        authorized_checkout=selected,
                    )
            claim_workspace_root = checkout_identity.get("workspace_root") or workspace_root
            inspected = workspace_module.inspect_repo(
                resolved_checkout, _refresh=True
            )
            if (inspected is None or inspected.git_error or not inspected.head
                    or inspected.head != checkout_identity.get("head")
                    or (inspected.branch or "") != checkout_identity.get("branch")):
                raise ValueError(
                    f"claim checkout identity changed while resolving {repository}"
                )
            scope = {
                **scope,
                "checkout": str(resolved_checkout),
                "branch": inspected.branch,
                "exclusive": False,
            }
            if declared_provider is not None:
                for identity_key in (
                    "git_common_directory_identity", "checkout_identity",
                    "manifest_identity",
                ):
                    identity_value = declared_provider.get(identity_key)
                    if isinstance(identity_value, str) and identity_value:
                        scope[identity_key] = identity_value
            if checkout_facts is None:
                checkout_facts = {
                    "head": inspected.head,
                    "branch": inspected.branch,
                    "dirty": bool(inspected.dirty),
                    "foreign_signals": ["dirty-tree"] if inspected.dirty else [],
                    "checkout_identity": checkout_identity.get("checkout_identity"),
                    "git_common_directory_identity": checkout_identity.get(
                        "git_common_directory_identity"
                    ),
                    "manifest_identity": checkout_identity.get("manifest_identity"),
                }
        elif checkout_facts is None and selected is not None:
            clean = bool(selected.get("clean", False))
            checkout_facts = {
                "head": selected.get("head"),
                "branch": selected.get("branch"),
                "dirty": not clean,
                "foreign_signals": [] if clean else ["dirty-tree"],
            }
        record = claims_module.acquire(
            self.store, repository=repository, session_id=self.session_id,
            consumer_id=self.snapshot.get("consumer_id", "unknown"),
            basis=basis, reason=reason, ttl_hours=ttl_hours,
            scope=scope, checkout_facts=checkout_facts,
            workspace_root=claim_workspace_root, request_id=request_id,
            expected_store_generation=expected_store_generation,
            owner_recovery_authorization=_owner_recovery_authorization,
        )
        campaign = dict(self.snapshot.get("campaign") or {})
        claim_ids = list(campaign.get("claim_ids", []))
        if record.get("claim_id") not in claim_ids:
            claim_ids.append(record.get("claim_id"))
        campaign["claim_ids"] = claim_ids[-64:]
        campaign["repository_refs"] = self._campaign_repository_refs(
            claims_module.active_claims(self.store.read_claims())
        )
        self.snapshot["campaign"] = campaign
        self._refresh_holders()
        event_type = (
            "lease.owner-recovered"
            if _owner_recovery_authorization is not None
            else "lease.acquired"
        )
        event_payload = {
            "repository": repository,
            "basis": basis,
            "claim_id": record["identity"],
            "claim_identity": record.get("claim_id"),
            "claim_version": record.get("version"),
            "scope": record.get("scope", {}),
        }
        if _owner_recovery_authorization is not None:
            event_payload.update({
                "owner_session_id": _owner_recovery_authorization["owner_session_id"],
                "owner_claim_version": _owner_recovery_authorization["expected_claim_version"],
                "recovery_authorization_id": _owner_recovery_authorization["authorization_id"],
            })
        self._emit(event_type, self.snapshot.get("consumer_id", "unknown"),
                   event_payload)
        self._save()
        return record

    def recover_claim_controlled(
        self,
        *,
        claim_id: str,
        repository: str,
        owner_session_id: str,
        expected_version: int,
        authorization: dict[str, Any],
        authenticated_principal_id: str,
        request_id: str,
        reason: str,
        ttl_hours: int = 24,
    ) -> dict[str, Any]:
        """Recover one exact claim scope after Control's host-side fence.

        This is an owner operation called only by Control's authenticated
        Environment service. Ordinary claim acquisition keeps its liveness
        classification unchanged and cannot supply this authorization.
        """
        if (not isinstance(request_id, str)
                or not re.fullmatch(r"req_[A-Za-z0-9_-]{1,76}", request_id)):
            raise AuthorityDenied("owner claim recovery requires the authenticated Control request identity")
        try:
            claims_module._validate_owner_recovery_authorization(
                authorization,
                repository=repository,
                claim_id=claim_id,
                owner_session_id=owner_session_id,
                requester_session_id=self.session_id,
                target_scope=authorization.get("target_scope"),
            )
        except (TypeError, ValueError) as error:
            raise AuthorityDenied(str(error)) from error
        if (not isinstance(authenticated_principal_id, str)
                or authenticated_principal_id != self.snapshot.get("authenticated_principal_id")
                or authenticated_principal_id != authorization.get("requester_principal_id")):
            raise AuthorityDenied("owner claim recovery principal does not own the selected Environment session")
        campaign_id = str((self.snapshot.get("campaign") or {}).get("identity", ""))
        if campaign_id != authorization.get("campaign_id"):
            raise AuthorityDenied("owner claim recovery campaign does not match the exact approval")
        if (authorization.get("requester_session_id") != self.session_id
                or authorization.get("claim_id") != claim_id
                or authorization.get("repository") != repository
                or authorization.get("owner_session_id") != owner_session_id
                or authorization.get("expected_claim_version") != expected_version):
            raise AuthorityDenied("owner claim recovery request does not match the exact authorized transition")
        if self.snapshot.get("lifecycle") in ("completed", "failed"):
            raise AuthorityDenied("terminal Environment sessions cannot recover repository ownership")
        authority = self.effective_authority()
        if repository in authority.get("protected_repositories", []):
            raise AuthorityDenied(f"repository scope is protected for this campaign: {repository}")

        owner_scope = claims_module.normalize_scope(
            authorization.get("owner_scope"), repository,
        )
        target_scope = claims_module.normalize_scope(
            authorization.get("target_scope"), repository,
        )
        target_claim_id = claims_module.claim_identity(repository, target_scope)
        live = claims_module.active_claims(self.store.read_claims())
        recovered_target = live.get(target_claim_id)
        if (recovered_target is not None
                and recovered_target.get("session_id") == self.session_id
                and recovered_target.get("repository") == repository
                and recovered_target.get("provenance", {}).get("owner_recovery", {}).get(
                    "authorization_id") == authorization.get("authorization_id")):
            record = recovered_target
        else:
            source = live.get(claim_id)
            if (source is None or source.get("session_id") != owner_session_id
                    or int(source.get("version", 0)) != expected_version
                    or source.get("consumer_id") != authorization.get("owner_consumer_id")
                    or source.get("repository") != repository
                    or source.get("scope") != owner_scope):
                raise claims_module.ClaimConflict(
                    "owner recovery no longer matches the exact active source claim")
            owner_snapshot = self.store.load_snapshot(owner_session_id)
            if (authorization.get("recovery_basis") == "same-principal-host-liveness"
                    and (not isinstance(owner_snapshot, dict)
                         or owner_snapshot.get("authenticated_principal_id")
                         != authenticated_principal_id)):
                raise AuthorityDenied(
                    "automatic claim recovery is limited to the same authenticated operator"
                )
            record = self.acquire_claim(
                repository,
                basis=claims_module.BASIS_RECOVERY,
                reason=reason,
                ttl_hours=ttl_hours,
                scope=target_scope,
                request_id=request_id,
                _owner_recovery_authorization=authorization,
            )

        operation_digest = digest_hex({
            "operation": "environment-owner-claim-recovery/2",
            "claim_id": claim_id,
            "target_claim_id": target_claim_id,
            "repository": repository,
            "old_session_id": owner_session_id,
            "old_version": expected_version,
            "new_session_id": self.session_id,
            "new_version": int(record.get("version", 0)),
            "authorization_id": authorization.get("authorization_id"),
        }, length=64)
        recovery_payload = {
            "claim_id": claim_id,
            "target_claim_id": target_claim_id,
            "repository": repository,
            "owner_session_id": owner_session_id,
            "superseded_version": expected_version,
            "new_session_id": self.session_id,
            "fencing_generation": expected_version + 1,
            "new_claim_version": int(record.get("version", 0)),
            "recovery_authorization_id": authorization.get("authorization_id"),
            "recovery_basis": authorization.get("recovery_basis"),
        }
        if authorization.get("operator_approval_reference") is not None:
            recovery_payload["operator_approval_reference"] = authorization[
                "operator_approval_reference"
            ]
        old_session = Session(self.store, owner_session_id)
        old_authority = dict(old_session.snapshot.get("authority") or {})
        fences = [
            dict(item) for item in old_authority.get("claim_fences", [])
            if isinstance(item, dict)
            and not (item.get("claim_id") == claim_id
                     and item.get("recovery_authorization_id")
                     == authorization.get("authorization_id"))
        ]
        fences.append({
            "repository": repository,
            "claim_id": claim_id,
            "superseded_version": expected_version,
            "target_claim_id": target_claim_id,
            "fencing_generation": expected_version + 1,
            "recovery_authorization_id": authorization.get("authorization_id"),
        })
        old_authority["claim_fences"] = fences[-64:]
        old_session.snapshot["authority"] = old_authority
        old_campaign = dict(old_session.snapshot.get("campaign") or {})
        old_campaign["claim_ids"] = [
            item for item in old_campaign.get("claim_ids", [])
            if str(item) != claim_id
        ]
        old_campaign["repository_refs"] = old_session._campaign_repository_refs(
            claims_module.active_claims(self.store.read_claims())
        )
        old_session.snapshot["campaign"] = old_campaign
        old_session._refresh_holders()
        already_fenced = any(
            event.get("type") == "lease.recovered-away"
            and event.get("payload", {}).get("recovery_authorization_id")
            == authorization.get("authorization_id")
            for event in old_session._log()
        )
        if not already_fenced:
            old_session._emit(
                "lease.recovered-away", "mncs-control-mcp", recovery_payload,
                request_id=request_id, operation_digest=operation_digest,
            )
        old_session._save()

        already_reconciled = any(
            event.get("type") == "lease.owner-recovery-reconciled"
            and event.get("payload", {}).get("recovery_authorization_id")
            == authorization.get("authorization_id")
            for event in self._log()
        )
        if not already_reconciled:
            self._emit(
                "lease.owner-recovery-reconciled", "mncs-control-mcp",
                recovery_payload, request_id=request_id,
                operation_digest=operation_digest,
            )
        self._save()
        return {
            "status": "recovered",
            "claim": record,
            "source_claim_id": claim_id,
            "target_claim_id": target_claim_id,
            "old_session_id": owner_session_id,
            "new_session_id": self.session_id,
            "fencing_generation": expected_version + 1,
            "recovery_authorization_id": authorization.get("authorization_id"),
            "continuation_state": "recoverable",
            "reconciled_sessions": [owner_session_id, self.session_id],
        }

    def release_claim(self, repository: str, reason: str = "",
                      claim_id: str | None = None,
                      request_id: str | None = None) -> bool:
        normalized_repository = repository or None
        request_digest = (
            claims_module._release_request_digest(
                session_id=self.session_id, reason=reason, claim_id=claim_id,
                repository=normalized_repository,
            )
            if request_id is not None else None
        )
        if request_id is not None:
            prior_noop = next((event for event in self._log()
                               if (event.get("type") == "lease.release_noop"
                                   or event.get("provider_type") == "lease.release_noop")
                               and event.get("payload", {}).get("request_id")
                               == request_id), None)
            if prior_noop is not None:
                if prior_noop.get("payload", {}).get("request_digest") != request_digest:
                    raise claims_module.ClaimConflict(
                        "claim release request identity is already bound to another payload")
                return False
        released = claims_module.release(
            self.store, session_id=self.session_id, reason=reason,
            claim_id=claim_id, repository=normalized_repository,
            request_id=request_id)
        if not released:
            if request_id is not None:
                self._emit(
                    "lease.release_noop",
                    self.snapshot.get("consumer_id", "unknown"),
                    {
                        "repository": normalized_repository,
                        "claim_id": claim_id,
                        "request_id": request_id,
                        "request_digest": request_digest,
                    },
                )
                self._save()
            return False
        self._refresh_holders()
        campaign = dict(self.snapshot.get("campaign") or {})
        still_held = {
            str(record.get("claim_id"))
            for record in claims_module.active_claims(self.store.read_claims()).values()
            if record.get("session_id") == self.session_id
        }
        campaign["claim_ids"] = [
            item for item in campaign.get("claim_ids", [])
            if str(item) in still_held
        ]
        campaign["repository_refs"] = self._campaign_repository_refs(
            claims_module.active_claims(self.store.read_claims())
        )
        self.snapshot["campaign"] = campaign
        release_event_exists = (
            request_id is not None
            and any(
                event.get("type") == "lease.released"
                and event.get("payload", {}).get("request_id") == request_id
                for event in self._log()
            )
        )
        if not release_event_exists:
            payload = {
                "repository": repository,
                "claim_id": claim_id,
                "released": len(released),
            }
            if request_id is not None:
                payload["request_id"] = request_id
                payload["request_digest"] = request_digest
            self._emit(
                "lease.released", self.snapshot.get("consumer_id", "unknown"),
                payload,
            )
        self._save()
        return True

    def transfer_claim(self, claim_id: str, to_session: str,
                       to_consumer: str, reason: str = "",
                       request_id: str | None = None) -> dict[str, Any]:
        record = claims_module.transfer(
            self.store, claim_id=claim_id, from_session=self.session_id,
            to_session=to_session, to_consumer=to_consumer, reason=reason,
            request_id=request_id)
        provenance = record.get("provenance")
        transfer_request_id = (
            provenance.get("transfer_request_id")
            if isinstance(provenance, dict) else None
        )
        self._refresh_holders()
        campaign = dict(self.snapshot.get("campaign") or {})
        campaign["claim_ids"] = [
            item for item in campaign.get("claim_ids", [])
            if str(item) != claim_id
        ]
        campaign["repository_refs"] = self._campaign_repository_refs(
            claims_module.active_claims(self.store.read_claims())
        )
        self.snapshot["campaign"] = campaign
        transfer_event_exists = False
        if isinstance(transfer_request_id, str):
            for event in self._log():
                payload = event.get("payload")
                if not isinstance(payload, dict):
                    continue
                if ((event.get("type") == "lease.transferred"
                     or event.get("provider_type") == "lease.transferred")
                        and payload.get("claim_id") == claim_id
                        and payload.get("to_session") == to_session
                        and payload.get("transfer_request_id") == transfer_request_id):
                    transfer_event_exists = True
                    break
        if not transfer_event_exists:
            self._emit(
                "lease.transferred", self.snapshot.get("consumer_id", "unknown"),
                {
                    "claim_id": claim_id,
                    "to_session": to_session,
                    "transfer_request_id": transfer_request_id,
                },
            )
        self._save()
        return record

    # -- events / subscriptions ---------------------------------------------

    def subscribe(self, event_types: list[str], source_filter: str | None = None) -> dict[str, Any]:
        subscription = events_module.subscribe(
            subscription_id=f"sub_{digest_hex([self.session_id, event_types, source_filter])[:12]}",
            session_id=self.session_id,
            event_types=event_types,
            source_filter=source_filter,
            cursor=len(self._log()),
        )
        subscriptions = [sub for sub in self.snapshot.get("subscriptions", [])
                         if sub["subscription_id"] != subscription["subscription_id"]]
        subscriptions.append(subscription)
        self.snapshot["subscriptions"] = subscriptions
        self._save()
        return subscription

    def poll(self, subscription_id: str) -> list[dict[str, Any]]:
        for subscription in self.snapshot.get("subscriptions", []):
            if subscription["subscription_id"] == subscription_id:
                due, advanced = events_module.deliverable(subscription, self._log())
                subscription["cursor"] = advanced["cursor"]
                self._save()
                return due
        raise LifecycleError(f"unknown subscription {subscription_id}")

    def record_observation(
        self,
        event_type: str,
        producer: str,
        payload: dict[str, Any] | None = None,
        *,
        request_id: str | None = None,
        operation_digest: str | None = None,
    ) -> dict[str, Any]:
        """Persist one observation, optionally bound to a retry identity."""
        return self._emit(
            event_type, producer, payload,
            request_id=request_id,
            operation_digest=operation_digest,
        )

    def request_event(
        self,
        event_type: str,
        request_id: str,
        operation_digest: str,
    ) -> dict[str, Any] | None:
        """Read a durable request event and reject request identity reuse."""
        if (not isinstance(event_type, str) or not event_type
                or not isinstance(request_id, str) or not request_id
                or len(request_id) > 160 or "\x00" in request_id
                or not isinstance(operation_digest, str) or not operation_digest
                or len(operation_digest) > 160 or "\x00" in operation_digest):
            raise ValueError("request event lookup identity is invalid")
        matches: list[dict[str, Any]] = []
        for event in self._log():
            payload = event.get("payload", {})
            if (event.get("type") == event_type
                    and isinstance(payload, dict)
                    and payload.get("request_id") == request_id):
                if payload.get("request_operation_digest") != operation_digest:
                    raise LifecycleError(
                        "request identity was already used for a different Environment operation"
                    )
                matches.append(event)
        if not matches:
            return None
        event_digests = [
            event.get("payload", {}).get("request_event_digest")
            for event in matches
        ]
        if (len(event_digests) != 1
                or not all(isinstance(value, str) and value for value in event_digests)
                or len(set(event_digests)) != 1):
            raise LifecycleError(
                "request identity has malformed or conflicting durable Environment events"
            )
        return matches[0]

    def observe_workspace(
        self,
        workspace_root: str | Path,
        *,
        request_id: str | None = None,
        operation_digest: str | None = None,
    ) -> list[dict[str, Any]]:
        """Adapter poll: selected checkout revision and state changes become events."""
        workspace = workspace_module.discover_workspace(
            workspace_root, repositories=self.snapshot.get("workspace", {}).get("selection"))
        if workspace.get("error") or workspace.get("scan", {}).get("status") != "complete":
            raise workspace_module.WorkspaceResolutionError(
                "workspace revalidation stopped safely; keeping the previous complete observation",
                diagnostics={"code": "workspace-scan-incomplete", "root": str(workspace_root),
                             "scan": workspace.get("scan", {}), "next": "check workspace availability, then reconcile the session"})
        if self.snapshot.get("selected_checkouts"):
            current_checkouts: dict[str, dict[str, Any]] = {}
            for name, previous_checkout in self.snapshot["selected_checkouts"].items():
                selected_path = Path(str(previous_checkout.get("path", "")))
                if not selected_path.is_absolute():
                    selected_path = Path(workspace_root) / selected_path
                match = next(
                    (item for item in workspace.get("repositories", [])
                     if Path(str(item.get("path", ""))).resolve() == selected_path.resolve()),
                    None,
                )
                if match is None:
                    current_checkouts[name] = {
                        **previous_checkout, "head": None, "clean": False,
                        "missing": True,
                    }
                else:
                    current_checkouts[name] = {
                        **previous_checkout,
                        "head": match.get("head"),
                        "branch": match.get("branch"),
                        "clean": not bool(match.get("dirty")),
                        "missing": False,
                    }
            current = {name: item.get("head") for name, item in current_checkouts.items()}
            self.snapshot["selected_checkouts"] = current_checkouts
        else:
            current = {
                repo["name"]: repo.get("head")
                for repo in workspace.get("repositories", [])
            }
        previous = self.snapshot.get("workspace_heads", {})
        previous_states = self.snapshot.get("workspace_facts")
        if not isinstance(previous_states, dict):
            previous_states = _workspace_change_facts(
                self.snapshot.get("workspace", {}),
                self.snapshot.get("selected_checkouts", {}),
            )
        current_states = _workspace_change_facts(
            workspace, current_checkouts if self.snapshot.get("selected_checkouts") else None)
        events, _ = events_module.git_poll_events(
            session_id=self.session_id,
            sequence_start=len(self._log()) + 1,
            previous_heads=previous,
            current_heads=current,
            previous_states=previous_states,
            current_states=current_states,
        )
        for event in events:
            self._emit(
                event["type"], event["producer"], event["payload"],
                request_id=request_id,
                operation_digest=operation_digest,
            )
        self.snapshot["workspace_heads"] = current
        self.snapshot["workspace_facts"] = current_states
        if self.snapshot.get("selected_checkouts"):
            workspace["selected_checkouts"] = self.snapshot["selected_checkouts"]
        self.snapshot["workspace"] = workspace
        self.snapshot["repo_facts"] = _repo_facts(workspace)
        campaign = dict(self.snapshot.get("campaign") or {})
        campaign["repository_refs"] = self._campaign_repository_refs(
            claims_module.active_claims(self.store.read_claims())
        )
        self.snapshot["campaign"] = campaign
        self._save()
        return events

    def _feed_generation_now(self) -> int | None:
        generation = getattr(self.store, "generation", None)
        if not callable(generation):
            return None
        try:
            return int(generation())
        except Exception:
            return None

    def _refresh_feed_baseline(self) -> None:
        current = self._feed_generation_now()
        if current is None:
            return
        self._feed_generation = current
        self.snapshot["last_store_generation"] = current

    def observe_store(self) -> dict[str, Any] | None:
        """Read the Store generation: an external advance becomes an event.

        Every session write re-baselines through ``_save``, so a delta
        observed here is Store activity from outside this session by
        construction. A fresh handle adopts the current generation
        silently: resume already re-reads snapshot, log, and claims, so
        nothing is lost by not reporting the downtime delta as an event.
        """
        current = self._feed_generation_now()
        if current is None:
            return None
        if self._feed_generation is None:
            self._feed_generation = current
            self.snapshot["last_store_generation"] = current
            self._save()
            return None
        if current == self._feed_generation:
            return None
        previous = self._feed_generation
        event = self._emit("adapter.observed", "adapter:store-feed",
                           {"generation": current, "previous": previous,
                            "note": "store generation advanced outside this session"})
        self._save()
        return event

    def revalidate(
        self,
        *,
        request_id: str | None = None,
        request_operation: str = "resume/revalidate",
        workspace_root: str | Path | None = None,
    ) -> dict[str, Any]:
        """Refresh selected providers and recover an interrupted revalidation."""
        operation_digest = None
        if request_id is not None:
            if (not isinstance(request_id, str) or not request_id
                    or len(request_id) > 160 or "\x00" in request_id):
                raise ValueError("revalidation request identity must be bounded nonempty text")
            if (not isinstance(request_operation, str) or not request_operation
                    or len(request_operation) > 256 or "\x00" in request_operation):
                raise ValueError("revalidation request operation must be bounded nonempty text")
            operation_digest = _request_operation_digest(
                self.session_id, request_id, request_operation, self.snapshot
            )

            receipts = self.snapshot.get("revalidation_receipts", [])
            for receipt in receipts if isinstance(receipts, list) else []:
                if not isinstance(receipt, dict) or receipt.get("request_id") != request_id:
                    continue
                if receipt.get("operation_digest") != operation_digest:
                    raise LifecycleError(
                        "request identity was already used for a different Environment operation"
                    )
                report = receipt.get("report", {})
                report = report if isinstance(report, dict) else {}
                self._emit(
                    "session.revalidated", "environment",
                    {"workspace_root": receipt.get("workspace_root"),
                     "report": report},
                    request_id=request_id,
                    operation_digest=operation_digest,
                )
                return dict(report)

            prior = [
                event for event in self._log()
                if event.get("type") == "session.revalidated"
                and event.get("payload", {}).get("request_id") == request_id
            ]
            if prior:
                if any(event.get("payload", {}).get("request_operation_digest")
                       != operation_digest for event in prior):
                    raise LifecycleError(
                        "request identity was already used for a different Environment operation"
                    )
                report = prior[-1].get("payload", {}).get("report", {})
                return dict(report) if isinstance(report, dict) else {}

        scope_composition = self._compose_authorized_scope_checkouts()
        selected_workspace = (
            workspace_root or self.snapshot.get("workspace", {}).get("root")
        )
        workspace_root = selected_workspace
        workspace_root_value = str(selected_workspace) if selected_workspace else None
        if workspace_root_value:
            self.observe_workspace(
                workspace_root_value,
                request_id=request_id,
                operation_digest=operation_digest,
            )

        old_bindings = list(self.snapshot.get("bindings", []))
        selected = self.snapshot.get("selected_checkouts", {})
        discovered = old_bindings
        if workspace_root and selected:
            roots: dict[str, Path] = {}
            facts: dict[str, dict[str, Any]] = {}
            for repository, record in sorted(selected.items()):
                raw_path = Path(str(record.get("path", "")))
                path = raw_path if raw_path.is_absolute() else Path(workspace_root) / raw_path
                path = path.resolve()
                try:
                    path.relative_to(Path(workspace_root).resolve())
                except ValueError:
                    continue
                if record.get("missing") or path.is_symlink() or not path.is_dir():
                    continue
                roots[str(repository)] = path
                facts[str(repository)] = dict(record, path=str(path))
            (runtime_toolchain_roots, runtime_toolchain_facts,
             runtime_toolchain_diagnostics) = _resolve_declared_runtime_toolchain_checkouts(
                workspace_root, roots, facts
            )
            workspace_snapshot = dict(self.snapshot.get("workspace", {}))
            workspace_snapshot["runtime_toolchain_checkouts"] = [
                runtime_toolchain_facts[name]
                for name in sorted(runtime_toolchain_facts)
            ]
            workspace_snapshot["runtime_toolchain_diagnostics"] = runtime_toolchain_diagnostics
            self.snapshot["workspace"] = workspace_snapshot
            language_binary: Path | str | None = None
            if "mncs-language" in roots:
                candidates = (
                    roots["mncs-language"] / "target" / "release" / "mncs",
                    roots["mncs-language"] / "target" / "debug" / "mncs",
                )
                language_binary = next(
                    (item for item in candidates if item.is_file()), candidates[-1]
                )
            discovered = capabilities_module.discover_capabilities(
                workspace_root,
                repository_roots=roots,
                checkout_facts=facts,
                runtime_toolchain_roots=runtime_toolchain_roots,
                runtime_toolchain_facts=runtime_toolchain_facts,
                language_binary=language_binary,
            )
        elif workspace_root:
            discovered = capabilities_module.discover_capabilities(workspace_root)

        toolchain = self.snapshot.get("toolchain")
        if isinstance(toolchain, dict):
            checkout = Path(str(toolchain.get("checkout", "")))
            candidates = (checkout / "target" / "release" / "mncs", checkout / "target" / "debug" / "mncs")
            binary = next((path for path in candidates if path.is_file()), None)
            language = self.snapshot.get("selected_checkouts", {}).get("mncs-language", {})
            self.snapshot["toolchain"] = {**toolchain, "binary": str(binary) if binary else None,
                                          "status": "available" if binary else "unavailable",
                                          "revision": language.get("head", toolchain.get("revision"))}

        prior_by_key = {(item.get("provider"), item.get("capability")): item
                        for item in old_bindings}
        current_by_key = {(item.get("provider"), item.get("capability")): item
                          for item in discovered}
        binding_fields = (
                "contract_revision", "entrypoint", "address", "effects", "toolchain_address",
                "toolchain_env", "fixed_argv", "fixed_env", "timeout_seconds", "working_directory", "provenance",
            "provider_root", "event_types", "output_limit_bytes",
        )
        report: dict[str, Any] = {
            "reprobed": 0, "reused": 0, "changed": [], "bound": [], "unbound": [],
            "scope_composition": scope_composition,
        }
        for key, binding in current_by_key.items():
            previous = prior_by_key.get(key)
            if previous is None:
                report["bound"].append(binding.get("capability"))
                self._emit("capability.bound", "environment",
                           {"capability": binding.get("capability"),
                            "provider": binding.get("provider"),
                            "provider_root": binding.get("provider_root")},
                           request_id=request_id,
                           operation_digest=operation_digest)
            elif any(previous.get(field) != binding.get(field) for field in binding_fields):
                self._emit("capability.changed", "environment",
                           {"capability": binding.get("capability"),
                            "provider": binding.get("provider"),
                            "previous_revision": previous.get("contract_revision"),
                            "current_revision": binding.get("contract_revision"),
                            "provider_root": binding.get("provider_root")},
                           request_id=request_id,
                           operation_digest=operation_digest)
        for key, previous in prior_by_key.items():
            if key not in current_by_key:
                report["unbound"].append(previous.get("capability"))
                self._emit("capability.unbound", "environment",
                           {"capability": previous.get("capability"),
                            "provider": previous.get("provider"),
                            "provider_root": previous.get("provider_root")},
                           request_id=request_id,
                           operation_digest=operation_digest)

        new_bindings = []
        for binding in discovered:
            before = prior_by_key.get((binding.get("provider"), binding.get("capability")), {})
            before_status = before.get("availability", {}).get("status")
            prior_availability = before.get("availability", {})
            same_contract = before and all(
                before.get(field) == binding.get(field) for field in binding_fields
            )
            same_substrate = (
                isinstance(prior_availability, dict)
                and prior_availability.get("substrate_key")
                == capabilities_module.availability_substrate_key(binding)
            )
            if same_contract and same_substrate:
                fresh = dict(binding)
                fresh["availability"] = dict(prior_availability)
                report["reused"] += 1
            else:
                fresh = capabilities_module.probe_availability(binding)
                report["reprobed"] += 1
            new_bindings.append(fresh)
            if fresh["availability"]["status"] != before_status:
                report["changed"].append(binding["capability"])
                status = fresh["availability"]["status"]
                event_type = {
                    "available": "capability.available",
                    "unavailable": "capability.unavailable",
                    "unknown": "capability.unknown",
                }.get(status, "capability.unknown")
                self._emit(
                    event_type,
                    "environment",
                    {"capability": binding["capability"], "previous": before_status},
                    request_id=request_id,
                    operation_digest=operation_digest,
                )
        self.snapshot["bindings"] = new_bindings
        rights_context_binding = self.snapshot.get("rights_context_binding")
        if isinstance(rights_context_binding, dict):
            refreshed_rights_context = rights_module.load_agent_context(
                rights_context_binding,
                workspace_root or self.snapshot.get("workspace", {}).get("root", "."),
                previous_context=self.snapshot.get("rights_context"),
                previous_inputs=self.snapshot.get("rights_context_inputs"),
            )
            next_context = refreshed_rights_context.get("context")
            prior_context = self.snapshot.get("rights_context")
            prior_identity = (
                prior_context.get("identity") if isinstance(prior_context, dict) else None
            )
            next_identity = (
                next_context.get("identity") if isinstance(next_context, dict) else None
            )
            self.snapshot["rights_context_inputs"] = refreshed_rights_context.get("inputs", {})
            if isinstance(next_context, dict):
                self.snapshot["rights_context"] = next_context
            changed = prior_identity != next_identity
            report["rights_context_changed"] = changed
            if changed:
                self._emit(
                    "rights.context.changed",
                    "mncs-rights-provenance",
                    {
                        "previous_identity": prior_identity,
                        "current_identity": next_identity,
                        "status": next_context.get("status") if isinstance(next_context, dict) else "unavailable",
                    },
                    request_id=request_id,
                    operation_digest=operation_digest,
                )
        selectors = self.snapshot.get("execution_roles", {})
        self.snapshot["execution_stack"] = composition.resolve(
            selectors, new_bindings, self.snapshot.get("toolchain"), self.snapshot.get("execution_stack"),
            compatibility_service=self.snapshot.get("execution_compatibility_service"),
            service_observations=self.snapshot.get("service_observations", []),
        )
        holders = claims_module.holders(self.store.read_claims())
        if holders != self.snapshot.get("claim_holders", {}):
            self._emit("adapter.observed", "environment",
                       {"note": "claim holders changed", "holders": holders},
                       request_id=request_id,
                       operation_digest=operation_digest)
        self.snapshot["claim_holders_detailed"] = holders
        self.snapshot["claim_holders"] = holders
        self.snapshot.setdefault("authority", {})["claim_holders"] = dict(holders)
        if request_id is not None:
            receipts = self.snapshot.get("revalidation_receipts", [])
            receipts = list(receipts) if isinstance(receipts, list) else []
            receipts = [item for item in receipts
                        if not isinstance(item, dict)
                        or item.get("request_id") != request_id]
            receipts.append({
                "request_id": request_id,
                "operation_digest": operation_digest,
                "workspace_root": workspace_root_value,
                "report": report,
                "recorded_at": utcnow(),
            })
            self.snapshot["revalidation_receipts"] = receipts[-64:]
        self._save()
        if request_id is not None:
            self._emit(
                "session.revalidated", "environment",
                {"workspace_root": workspace_root_value, "report": report},
                request_id=request_id,
                operation_digest=operation_digest,
            )
        return report

    # -- checkpoint / handoff / completion ------------------------------------

    def checkpoint(self, *, progress: str = "", remaining: list[str] | None = None,
                   request_id: str | None = None,
                   campaign_evidence: list[dict[str, Any]] | None = None,
                   campaign_evidence_delta: list[dict[str, Any]] | None = None,
                   campaign_delivery: dict[str, Any] | None = None,
                   campaign_base_checkpoint: str | None = None) -> dict[str, Any]:
        if request_id is not None and (
            not isinstance(request_id, str) or not request_id
            or len(request_id) > 160 or "\x00" in request_id
        ):
            raise ValueError(
                "checkpoint request identity must be bounded nonempty text"
            )
        if not isinstance(progress, str) or len(progress) > 4096 or "\x00" in progress:
            raise ValueError("checkpoint progress must be bounded text")
        if (not isinstance(remaining, (list, type(None)))
                or (remaining is not None and (len(remaining) > 32
                    or any(not isinstance(item, str) or len(item) > 1024
                           or "\x00" in item for item in remaining)))):
            raise ValueError("checkpoint remaining items must be a bounded text list")
        remaining_values = list(remaining or [])
        if campaign_evidence is not None and campaign_evidence_delta is not None:
            raise ValueError(
                "campaign evidence window and append delta are mutually exclusive"
            )
        campaign_update_requested = (
            campaign_evidence is not None
            or campaign_evidence_delta is not None
            or campaign_delivery is not None
        )
        if campaign_update_requested and campaign_base_checkpoint is None:
            raise ValueError(
                "campaign metadata updates require --campaign-base-checkpoint from the current capsule (use 'none' initially)"
            )
        if campaign_base_checkpoint is not None and (
            not isinstance(campaign_base_checkpoint, str)
            or not campaign_base_checkpoint
            or len(campaign_base_checkpoint) > 160
            or "\x00" in campaign_base_checkpoint
        ):
            raise ValueError("campaign base checkpoint identity is invalid")
        normalized_evidence = (
            normalize_campaign_evidence(campaign_evidence)
            if campaign_evidence is not None else None
        )
        normalized_evidence_delta = (
            normalize_campaign_evidence(campaign_evidence_delta)
            if campaign_evidence_delta is not None else None
        )
        if normalized_evidence_delta is not None and not normalized_evidence_delta:
            raise ValueError("campaign evidence append delta must contain at least one reference")
        normalized_delivery = (
            normalize_campaign_delivery(campaign_delivery)
            if campaign_delivery is not None else None
        )
        campaign_update = None
        if campaign_update_requested:
            campaign_update = {
                "base_checkpoint": campaign_base_checkpoint,
                "evidence": normalized_evidence,
                "delivery": normalized_delivery,
            }
            if normalized_evidence_delta is not None:
                campaign_update["evidence_delta"] = normalized_evidence_delta
        state_digest = digest_hex(
            {
                "intent": self.snapshot.get("intent"),
                "artifacts": self.snapshot.get("artifacts"),
                "decisions": self.snapshot.get("decisions"),
                "heads": self.snapshot.get("workspace_heads"),
            }
        )
        operation_digest = digest_hex({
            "session_id": self.session_id,
            "progress": progress,
            "remaining": remaining_values,
            **({"campaign_update": campaign_update}
               if campaign_update_requested else {}),
        })
        request_digest = digest_hex({
            "session_id": self.session_id,
            "state_digest": state_digest,
            "progress": progress,
            "remaining": remaining_values,
            **({"campaign_update": campaign_update}
               if campaign_update_requested else {}),
        })
        # Transport request identities survive service restarts. A retry
        # reads the original immutable checkpoint even if the session acquired
        # new workspace facts after publication.
        identity = "chk_" + digest_hex(
            {"kind": "checkpoint-request", "session": self.session_id,
             "request_id": request_id}
            if request_id is not None else
            {"kind": "checkpoint-content", "session": self.session_id,
             "request_digest": request_digest}
        )
        checkpoint_records = self.store.list_checkpoints(self.session_id)

        def checkpoint_sequence(item: dict[str, Any]) -> int:
            try:
                return int(item.get("sequence", 0))
            except (TypeError, ValueError):
                return 0

        sequence = max((checkpoint_sequence(item) for item in checkpoint_records),
                       default=0) + 1
        matching_requests = (
            [item for item in checkpoint_records
             if item.get("request_id") == request_id]
            if request_id is not None else []
        )
        if len(matching_requests) > 1:
            raise LifecycleError(
                "checkpoint request identity has multiple durable publications"
            )
        existing_request = matching_requests[0] if matching_requests else None
        if existing_request is not None:
            existing_operation_digest = existing_request.get("operation_digest")
            if existing_operation_digest is None:
                existing_operation_digest = digest_hex({
                    "session_id": self.session_id,
                    "progress": existing_request.get("progress", ""),
                    "remaining": list(existing_request.get("remaining", [])),
                })
            if existing_request.get("session_id") != self.session_id or (
                existing_operation_digest != operation_digest
            ):
                raise LifecycleError(
                    "checkpoint request identity is already bound to "
                    "conflicting operation"
                )
            record = existing_request
        else:
            record = self.store.load_checkpoint(self.session_id, identity)
        if record is not None and campaign_update_requested:
            if not isinstance(record.get("campaign_state"), dict):
                raise LifecycleError(
                    "checkpoint request identity has no durable campaign state"
                )
        campaign_identity = None
        campaign_evidence_value: list[dict[str, str]] | None = None
        campaign_evidence_delta_value: list[dict[str, str]] | None = None
        campaign_evidence_history_value: list[dict[str, str]] | None = None
        campaign_delivery_value: dict[str, Any] | None = None
        previous_campaign_state_digest: str | None = None
        if record is None and campaign_update_requested:
            campaign = dict(self.snapshot.get("campaign") or {})
            campaign_identity = campaign.get("identity")
            if (not isinstance(campaign_identity, str)
                    or not _CAMPAIGN_ID_PATTERN.fullmatch(campaign_identity)):
                raise LifecycleError(
                    "durable campaign metadata requires an Environment campaign identity"
                )
            campaign_state_history = _validated_campaign_state_history(
                checkpoint_records, campaign_identity
            )
            latest_campaign_record = (
                campaign_state_history[0] if campaign_state_history else None
            )
            latest_campaign_id = (
                latest_campaign_record.get("identity")
                if latest_campaign_record is not None else "none"
            )
            if campaign_base_checkpoint != latest_campaign_id:
                raise CampaignStateConflict(
                    str(campaign_base_checkpoint), str(latest_campaign_id)
                )
            if normalized_evidence_delta is not None and latest_campaign_record is None:
                raise ValueError(
                    "campaign evidence append delta requires an initialized campaign state; "
                    "initialize it with --campaign-evidence-json first"
                )
            if latest_campaign_record is not None:
                campaign_evidence_history_value = campaign_state_history[1]
                previous_campaign_state_digest = latest_campaign_record.get(
                    "campaign_state_digest"
                )
                previous_evidence_history_digest = (
                    latest_campaign_record["campaign_state"].get(
                        "evidence_history_digest"
                    )
                    if latest_campaign_record["campaign_state"].get("schema_version")
                    == "mncs.environment.campaign-state/2"
                    else digest_hex(campaign_evidence_history_value)
                )
                campaign_evidence_value = campaign_evidence_history_value[
                    -CAMPAIGN_EVIDENCE_MAX:
                ]
                if normalized_evidence_delta is not None:
                    prior_window = campaign_evidence_history_value[
                        -CAMPAIGN_EVIDENCE_MAX:
                    ]
                    keep_count = min(
                        len(prior_window),
                        CAMPAIGN_EVIDENCE_MAX - len(normalized_evidence_delta),
                    )
                    retained_prior = prior_window[-keep_count:] if keep_count else []
                    normalized_evidence = [
                        *retained_prior,
                        *normalized_evidence_delta,
                    ]
                prior_state = latest_campaign_record["campaign_state"]
                campaign_delivery_value = prior_state["delivery"]
            else:
                try:
                    projected_evidence = normalize_campaign_evidence(
                        campaign.get("evidence", [])
                    )
                    campaign_delivery_value = normalize_campaign_delivery(
                        campaign.get("delivery", {"status": "pending"})
                    )
                except ValueError as error:
                    raise LifecycleError(
                        f"existing campaign projection is invalid: {error}"
                    ) from error
                # No persisted campaign-state record anchors the projection,
                # so the first segment contains its retained evidence in full.
                previous_evidence_history_digest = digest_hex([])
            if normalized_evidence is not None:
                if latest_campaign_record is not None:
                    campaign_evidence_delta_value, campaign_evidence_history_value = (
                        _append_campaign_evidence(
                            campaign_evidence_history_value, normalized_evidence
                        )
                    )
                else:
                    projected_by_identity = {
                        item["identity"]: item for item in projected_evidence
                    }
                    incoming_by_identity = {
                        item["identity"]: item for item in normalized_evidence
                    }
                    if any(incoming_by_identity.get(key) != item
                           for key, item in projected_by_identity.items()):
                        raise LifecycleError(
                            "initial campaign state must retain every projected immutable reference"
                        )
                    campaign_evidence_delta_value = normalized_evidence
                    campaign_evidence_history_value = list(normalized_evidence)
            elif latest_campaign_record is None:
                campaign_evidence_delta_value = projected_evidence
                campaign_evidence_history_value = list(projected_evidence)
            else:
                campaign_evidence_delta_value = []
            campaign_evidence_value = campaign_evidence_history_value[
                -CAMPAIGN_EVIDENCE_MAX:
            ]
            if normalized_delivery is not None:
                campaign_delivery_value = normalized_delivery
            evidence_ids = {item["identity"] for item in campaign_evidence_history_value}
            if any(item.get("evidence_identity") not in evidence_ids
                   for item in campaign_delivery_value.get("repositories", [])
                   if item.get("evidence_identity") is not None):
                raise ValueError(
                    "campaign delivery references an evidence identity absent from campaign history"
                )
        if record is None:
            created_at = utcnow()
            record = {
                "schema_version": CHECKPOINT_SCHEMA,
                "identity": identity,
                "session_id": self.session_id,
                "sequence": sequence,
                "progress": progress,
                "remaining": remaining_values,
                "intent_id": (self.snapshot.get("intent") or {}).get("identity"),
                "campaign_identity": (self.snapshot.get("campaign") or {}).get("identity"),
                "environment_id": self.snapshot.get("environment_id"),
                "event_cursor": len(self._log()),
                "artifacts": list(self.snapshot.get("artifacts", [])),
                "unresolved": list(self.snapshot.get("pressures", [])),
                "revalidate_on_resume": [
                    "bindings", "workspace-heads", "claims", "authority"
                ],
                "created_at": created_at,
                "created_by": self.snapshot.get("consumer_id"),
                "created_by_principal_id": self.snapshot.get(
                    "authenticated_principal_id"
                ),
                "request_id": request_id,
                "operation_digest": (
                    operation_digest if request_id is not None else None
                ),
                "request_digest": request_digest,
            }
            if campaign_update_requested:
                campaign_state = {
                    "schema_version": "mncs.environment.campaign-state/2",
                    "campaign_identity": campaign_identity,
                    "previous_state_digest": previous_campaign_state_digest,
                    "evidence_delta": campaign_evidence_delta_value,
                    "evidence_count": len(campaign_evidence_history_value),
                    "evidence_history_digest": digest_hex(
                        {
                            "previous": previous_evidence_history_digest,
                            "delta": campaign_evidence_delta_value,
                        }
                    ),
                    "delivery": campaign_delivery_value,
                    "recorded_at": created_at,
                    "recorded_by": {
                        "consumer_id": self.snapshot.get("consumer_id"),
                        "principal_id": self.snapshot.get(
                            "authenticated_principal_id"
                        ),
                    },
                }
                record["campaign_state"] = campaign_state
                record["campaign_state_digest"] = digest_hex(campaign_state)
            self.store.save_checkpoint(self.session_id, record)
        elif (record.get("session_id") != self.session_id
              or record.get("request_id") != request_id
              or (request_id is None
                  and record.get("request_digest") != request_digest)):
            raise LifecycleError(
                "checkpoint request identity is bound to conflicting state"
            )
        self.snapshot.setdefault("checkpoints", []).append(record["identity"])
        self.snapshot["checkpoints"] = list(dict.fromkeys(self.snapshot["checkpoints"]))
        if isinstance(record.get("campaign_state"), dict):
            campaign = dict(self.snapshot.get("campaign") or {})
            current_campaign_history = _validated_campaign_state_history(
                self.store.list_checkpoints(self.session_id),
                (self.snapshot.get("campaign") or {}).get("identity"),
            )
            if current_campaign_history:
                latest_campaign_record, evidence_history = current_campaign_history
                _project_campaign_state(
                    campaign, latest_campaign_record, evidence_history
                )
            self.snapshot["campaign"] = campaign
        if self.snapshot.get("lifecycle") == "active":
            self.transition("checkpointed", f"checkpoint {sequence}")
        events = self._log()
        if not any(event.get("payload", {}).get("checkpoint_id") == record["identity"]
                   for event in events):
            self._emit("session.checkpointed", self.snapshot.get("consumer_id", "unknown"),
                       {"checkpoint_id": record["identity"],
                        "progress": record.get("progress", progress),
                        "campaign_state_checkpoint_id": (
                            record["identity"]
                            if isinstance(record.get("campaign_state"), dict)
                            else None
                        ),
                        "request_id": request_id})
        if self.snapshot.get("lifecycle") == "checkpointed":
            self.transition("active", "resumed after checkpoint")
        self._save()
        return record

    def campaign_state(
        self, *, evidence_offset: int | None = None,
        evidence_limit: int = CAMPAIGN_EVIDENCE_MAX,
    ) -> dict[str, Any]:
        """Read a bounded page from the authenticated campaign evidence history.

        Version 2 checkpoints store append-only evidence segments. The default
        page contains the newest references that fit one checkpoint update;
        older pages remain addressable by offset.
        """
        campaign = self.snapshot.get("campaign")
        campaign = campaign if isinstance(campaign, dict) else {}
        campaign_identity = campaign.get("identity")
        if (not isinstance(campaign_identity, str)
                or not _CAMPAIGN_ID_PATTERN.fullmatch(campaign_identity)):
            raise LifecycleError("session has no durable campaign identity")
        if (not isinstance(evidence_limit, int) or isinstance(evidence_limit, bool)
                or evidence_limit < 1 or evidence_limit > CAMPAIGN_EVIDENCE_MAX):
            raise LifecycleError(
                f"campaign evidence page limit must be between 1 and {CAMPAIGN_EVIDENCE_MAX}"
            )
        if (evidence_offset is not None
                and (not isinstance(evidence_offset, int)
                     or isinstance(evidence_offset, bool) or evidence_offset < 0)):
            raise LifecycleError("campaign evidence page offset must be a nonnegative integer")
        history = _validated_campaign_state_history(
            self.store.list_checkpoints(self.session_id), campaign_identity
        )
        if not history:
            raise LifecycleError("campaign has no persisted state checkpoint")
        checkpoint, all_evidence = history
        state = checkpoint["campaign_state"]
        charter_references = [
            item for item in all_evidence
            if "campaign_charter" in item.get("identity", "")
        ]
        charter_reference_index = {
            "references": charter_references[:CAMPAIGN_CHARTER_EVIDENCE_MAX],
            "count": len(charter_references),
            "truncated": len(charter_references) > CAMPAIGN_CHARTER_EVIDENCE_MAX,
        }
        total = len(all_evidence)
        limit = evidence_limit
        while True:
            offset = (
                max(0, total - limit)
                if evidence_offset is None else evidence_offset
            )
            if offset > total:
                raise LifecycleError("campaign evidence page offset exceeds evidence count")
            page = all_evidence[offset:offset + limit]
            end = offset + len(page)
            result = {
                "schema_version": "mncs.environment.campaign-state-read/1",
                "session_id": self.session_id,
                "campaign_identity": campaign_identity,
                "checkpoint_id": checkpoint["identity"],
                "recorded_at": state.get("recorded_at"),
                "recorded_by": state["recorded_by"],
                "evidence": page,
                "evidence_count": total,
                "evidence_offset": offset,
                "evidence_limit": limit,
                "previous_evidence_offset": (
                    max(0, offset - limit) if offset > 0 else None
                ),
                "next_evidence_offset": end if end < total else None,
                "evidence_truncated": offset > 0 or end < total,
                "delivery": state["delivery"],
                # This bounded identity index spans the complete validated
                # evidence history so consumers can locate immutable campaign
                # charter pointers after the ordinary page window moves on.
                "campaign_charter_evidence": charter_reference_index,
            }
            encoded = json.dumps(
                result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
            if len(encoded) <= CAMPAIGN_STATE_READ_MAX_BYTES:
                return result
            if limit == 1:
                raise LifecycleError(
                    "persisted campaign state exceeds the bounded read response"
                )
            limit = max(1, limit // 2)

    def handoff(
        self,
        *,
        to_consumer: str,
        notes: list[str] | None = None,
        blockers: list[str] | None = None,
        next_actions: list[str] | None = None,
        to_authenticated_principal_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if (not isinstance(to_consumer, str) or not to_consumer.strip()
                or len(to_consumer) > 256 or "\x00" in to_consumer):
            raise ValueError("handoff consumer identity must be bounded nonempty text")
        if to_authenticated_principal_id is not None and (
            not isinstance(to_authenticated_principal_id, str)
            or not to_authenticated_principal_id.strip()
            or len(to_authenticated_principal_id) > 256
            or "\x00" in to_authenticated_principal_id
        ):
            raise ValueError("handoff principal identity must be bounded nonempty text")
        if request_id is not None and (
            not isinstance(request_id, str) or not request_id
            or len(request_id) > 160 or "\x00" in request_id
        ):
            raise ValueError("handoff request identity must be bounded nonempty text")
        notes = list(notes or [])
        blockers = list(blockers or [])
        next_actions = list(next_actions or [])

        def request_digest(checkpoint_identity: str, source_principal: str | None) -> str:
            return digest_hex({
                "session_id": self.session_id,
                "checkpoint_id": checkpoint_identity,
                "from_consumer": self.snapshot.get("consumer_id", ""),
                "from_authenticated_principal_id": source_principal,
                "to_consumer": to_consumer,
                "to_authenticated_principal_id": to_authenticated_principal_id,
                "notes": notes,
                "blockers": blockers,
                "next_actions": next_actions,
            })

        if self.snapshot.get("lifecycle") == "handed_off":
            pending_id = self.snapshot.get("pending_handoff_id")
            existing = (
                self.store.load_handoff(self.session_id, pending_id)
                if isinstance(pending_id, str) else None
            )
            if isinstance(existing, dict):
                expected_digest = request_digest(
                    str(existing.get("checkpoint_id", "")),
                    self.snapshot.get("authenticated_principal_id"),
                )
                legacy_match = (
                    request_id is None
                    and "request_digest" not in existing
                    and existing.get("to_consumer") == to_consumer
                    and existing.get("to_authenticated_principal_id") == to_authenticated_principal_id
                    and existing.get("notes", []) == notes
                    and existing.get("blockers", []) == blockers
                    and existing.get("next_actions", []) == next_actions
                )
                if (
                    existing.get("request_digest") == expected_digest
                    and existing.get("request_id") == request_id
                ) or legacy_match:
                    events = self._log()
                    if not any(event.get("type") == "handoff.created"
                               and event.get("payload", {}).get("handoff_id") == pending_id
                               for event in events):
                        self._emit("handoff.created", self.snapshot.get("consumer_id", "unknown"), {
                            "handoff_id": pending_id, "to_consumer": to_consumer,
                        })
                        self._save()
                    return existing
            raise LifecycleError("a different owner-issued handoff is already pending")
        if self.snapshot.get("lifecycle") != "active":
            raise LifecycleError("only an active session can create a handoff")

        checkpoint = self.checkpoint(progress="handoff", remaining=next_actions,
                                     request_id=request_id)
        source_principal = self.snapshot.get("authenticated_principal_id")
        handoff_history = self.snapshot.get("handoffs", [])
        if not isinstance(handoff_history, list):
            raise LifecycleError("session handoff history is malformed")
        identity_sequence = len(handoff_history) + 1
        record_identity = handoff_id(
            checkpoint["identity"], self.snapshot.get("consumer_id", ""),
            to_consumer, to_authenticated_principal_id, identity_sequence,
        )
        record_digest = request_digest(checkpoint["identity"], source_principal)
        record = {
            "schema_version": HANDOFF_SCHEMA,
            "identity": record_identity,
            "checkpoint_id": checkpoint["identity"],
            "session_id": self.session_id,
            "from_consumer": self.snapshot.get("consumer_id"),
            "to_consumer": to_consumer,
            "to_consumer_kind": "agent",
            "from_authenticated_principal_id": source_principal,
            "to_authenticated_principal_id": to_authenticated_principal_id,
            "notes": notes,
            "blockers": blockers,
            "next_actions": next_actions,
            "request_id": request_id,
            "request_digest": record_digest,
            "created_at": utcnow(),
        }
        existing = self.store.load_handoff(self.session_id, record_identity)
        if existing is not None:
            if (existing.get("request_id") != request_id
                    or existing.get("request_digest") != record_digest):
                raise LifecycleError("handoff identity is already bound to conflicting request state")
            record = existing
        else:
            self.store.save_handoff(self.session_id, record)
        handoff_history = self.snapshot.setdefault("handoffs", [])
        if record["identity"] not in handoff_history:
            handoff_history.append(record["identity"])
        self.snapshot["pending_handoff_id"] = record["identity"]
        if self.snapshot.get("lifecycle") == "active":
            self.transition("handed_off", f"handoff to {to_consumer}")
        events = self._log()
        if not any(event.get("type") == "handoff.created"
                   and event.get("payload", {}).get("handoff_id") == record["identity"]
                   for event in events):
            self._emit("handoff.created", self.snapshot.get("consumer_id", "unknown"),
                       {"handoff_id": record["identity"], "to_consumer": to_consumer})
            self._save()
        return record

    def accept_handoff(self, handoff_id: str, *, consumer_id: str,
                       consumer_kind: str = "agent",
                       authenticated_principal_id: str | None = None,
                       request_id: str | None = None) -> dict[str, Any]:
        """Accept a handoff as the intended recipient, revalidating everything.

        Rejects unknown handoffs, wrong recipients, and stale assumptions:
        workspace facts, claims, capability availability, and authority are
        recomputed and divergence is recorded before continuation.
        """
        record = self.store.load_handoff(self.session_id, handoff_id)
        if record is None:
            raise LifecycleError(f"unknown handoff {handoff_id} for session {self.session_id}")
        if record.get("session_id") != self.session_id:
            raise LifecycleError("handoff belongs to another session")
        if record.get("to_consumer") != consumer_id:
            raise LifecycleError(
                f"handoff {handoff_id} is addressed to {record.get('to_consumer')}, "
                f"not {consumer_id}"
            )
        recipient_principal = record.get("to_authenticated_principal_id")
        if recipient_principal != authenticated_principal_id:
            raise LifecycleError("handoff recipient principal does not match authenticated continuation")
        source_principal = record.get("from_authenticated_principal_id")

        request_digest = digest_hex({
            "session_id": self.session_id,
            "handoff_id": handoff_id,
            "consumer_id": consumer_id,
            "consumer_kind": consumer_kind,
            "authenticated_principal_id": authenticated_principal_id,
        })
        request_id = request_id or "hacc_" + request_digest
        if (not isinstance(request_id, str) or not request_id
                or len(request_id) > 160 or "\x00" in request_id):
            raise ValueError("handoff acceptance request identity must be bounded nonempty text")
        accepted_handoffs = list(self.snapshot.get("accepted_handoffs", []))
        accepted = next((item for item in accepted_handoffs
                         if isinstance(item, dict) and item.get("handoff_id") == handoff_id), None)
        if accepted is not None:
            if (accepted.get("request_id") != request_id
                    or accepted.get("request_digest") != request_digest
                    or accepted.get("consumer_id") != consumer_id
                    or accepted.get("authenticated_principal_id") != authenticated_principal_id
                    or accepted.get("source_authenticated_principal_id") != source_principal):
                raise LifecycleError(f"handoff {handoff_id} was already accepted by another continuation")
            result = dict(accepted.get("result") or {})
            events = self._log()
            if not any(event.get("type") == "session.resumed"
                       and event.get("payload", {}).get("handoff_id") == handoff_id
                       for event in events):
                self._emit("session.resumed", consumer_id, {
                    "previous_consumer": accepted.get("previous_consumer"),
                    "handoff_id": handoff_id,
                    "revalidation": result.get("divergence", {}),
                })
                self._save()
            return result
        if (source_principal is not None
                and self.snapshot.get("authenticated_principal_id") != source_principal):
            raise LifecycleError("handoff source principal no longer matches the session owner")
        if self.snapshot.get("lifecycle") != "handed_off":
            raise LifecycleError("handoff is not pending in the session lifecycle")
        pending_handoff_id = self.snapshot.get("pending_handoff_id")
        handoff_history = self.snapshot.get("handoffs")
        legacy_pending = (
            "pending_handoff_id" not in self.snapshot
            and isinstance(handoff_history, list)
            and handoff_history[-1:] == [handoff_id]
        )
        if pending_handoff_id != handoff_id and not legacy_pending:
            raise LifecycleError("handoff is not the session's current pending transfer")

        previous = self.snapshot.get("consumer_id")
        # Revalidation occurs while the sender still owns the session. The
        # recipient and fencing record are committed together in the next
        # immutable snapshot revision.
        divergence = self.revalidate()
        self.snapshot["consumer_id"] = consumer_id
        self.snapshot["consumer_kind"] = consumer_kind
        if authenticated_principal_id is not None:
            self.snapshot["authenticated_principal_id"] = authenticated_principal_id
            campaign = dict(self.snapshot.get("campaign") or {})
            campaign["principal_id"] = authenticated_principal_id
            self.snapshot["campaign"] = campaign
        self.snapshot["authority"] = dict(self.snapshot.get("authority", {}))
        self.snapshot["authority"]["subject"] = consumer_id
        campaign = dict(self.snapshot.get("campaign") or {})
        consumers = list(campaign.get("authorized_consumers", []))
        for value in (previous, consumer_id):
            if value and value not in consumers:
                consumers.append(value)
        campaign["authorized_consumers"] = consumers[-32:]
        campaign["current_consumer_id"] = consumer_id
        self.snapshot["campaign"] = campaign
        result = {"previous_consumer": previous, "consumer_id": consumer_id,
                  "handoff_id": handoff_id, "divergence": divergence}
        accepted_handoffs.append({
            "handoff_id": handoff_id,
            "request_id": request_id,
            "request_digest": request_digest,
            "consumer_id": consumer_id,
            "authenticated_principal_id": authenticated_principal_id,
            "source_authenticated_principal_id": source_principal,
            "previous_consumer": previous,
            "result": result,
            "accepted_at": utcnow(),
        })
        self.snapshot["accepted_handoffs"] = accepted_handoffs[-64:]
        self.snapshot["pending_handoff_id"] = None
        self.transition("active", f"handoff {handoff_id} accepted by {consumer_id} (was {previous})")
        self._emit("session.resumed", consumer_id,
                   {"previous_consumer": previous, "handoff_id": handoff_id,
                    "revalidation": divergence})
        self._save()
        return result

    def _release_own_claims(
        self, reason: str, *, request_id: str | None = None,
    ) -> dict[str, Any]:
        """Release every live claim held by this session.

        Terminal sessions must not pin scopes until TTL expiry: a
        finished agent's claims would otherwise block the next agent
        for up to 24h. Release failures never block the terminal
        transition itself (TTL expiry remains the backstop); they are
        recorded on the completion payload instead. When ``request_id`` is
        supplied, each repository release is independently idempotent so a
        retry can recover after any subset of claim publications committed.
        """
        released: list[str] = []
        error: str | None = None
        try:
            records = self.store.read_claims()
            live = claims_module.active_claims(records)
            repositories = {
                str(record.get("repository", ""))
                for record in live.values()
                if record.get("session_id") == self.session_id
                and record.get("repository")
            }
            if request_id is not None:
                repositories.update(
                    str(record.get("repository", ""))
                    for record in records
                    if record.get("session_id") == self.session_id
                    and record.get("repository")
                    and record.get("provenance", {}).get("release_request_id")
                    == "completion-release-" + digest_hex({
                        "session_id": self.session_id,
                        "request_id": request_id,
                        "repository": str(record.get("repository", "")),
                    })
                )
            for repository in sorted(repositories):
                release_request_id = (
                    "completion-release-" + digest_hex({
                        "session_id": self.session_id,
                        "request_id": request_id,
                        "repository": repository,
                    })
                    if request_id is not None else None
                )
                repository_releases = claims_module.release(
                    self.store, session_id=self.session_id,
                    reason=reason, repository=repository,
                    request_id=release_request_id,
                )
                released.extend(repository_releases)
                if request_id is not None and repository_releases:
                    events = self._log()
                    already_emitted = any(
                        event.get("type") == "lease.released"
                        and event.get("payload", {}).get("release_request_id")
                        == release_request_id
                        for event in events
                    )
                    if not already_emitted:
                        self._emit(
                            "lease.released",
                            self.snapshot.get("consumer_id", "unknown"),
                            {
                                "released": sorted({
                                    str(record.get("claim_id", ""))
                                    for record in repository_releases
                                }),
                                "reason": reason,
                                "request_id": request_id,
                                "release_request_id": release_request_id,
                                "repository": repository,
                            },
                        )
        except Exception as exc:  # noqa: BLE001 - recorded, never raised
            error = f"{type(exc).__name__}: {exc}"
        if released:
            self._refresh_holders()
            if request_id is None:
                self._emit(
                    "lease.released", self.snapshot.get("consumer_id", "unknown"),
                    {
                        "released": sorted({
                            str(record.get("claim_id", "")) for record in released
                        }),
                        "reason": reason,
                    },
                )
        return {"released": sorted({
            str(record.get("claim_id", "")) for record in released
        }), "error": error}

    def complete(
        self,
        *,
        outcome: str | None = None,
        summary: str | None = None,
        request_id: str | None = None,
        authenticated_principal_id: str | None = None,
        consumer_id: str | None = None,
        retry_pending: bool = False,
    ) -> dict[str, Any]:
        """Complete a session with a durable, replayable request identity.

        The intent is saved before the terminal transition. A retry after a
        process or service interruption can therefore finish claim release
        and the completion record without creating a second terminal result.
        """
        if authenticated_principal_id is not None and (
            not isinstance(authenticated_principal_id, str)
            or not authenticated_principal_id.strip()
            or len(authenticated_principal_id) > 256
            or "\x00" in authenticated_principal_id
        ):
            raise ValueError("completion principal identity is invalid")
        session_principal = self.snapshot.get("authenticated_principal_id")
        if (authenticated_principal_id is not None
                and session_principal != authenticated_principal_id):
            raise LifecycleError(
                "completion principal does not match the authenticated session owner"
            )

        if retry_pending:
            source = self.snapshot.get("completion_pending")
            if not isinstance(source, dict):
                source = self.snapshot.get("completion")
            if not isinstance(source, dict):
                raise LifecycleError("there is no interrupted completion to reconcile")
            requested_by = source.get("requested_by")
            if not isinstance(requested_by, dict):
                raise LifecycleError("stored completion requester provenance is invalid")
            if (authenticated_principal_id is not None
                    and requested_by.get("principal_id")
                    != authenticated_principal_id):
                raise LifecycleError(
                    "completion recovery principal does not match the original request"
                )
            outcome = source.get("outcome")
            summary = source.get("summary", "")
            request_id = source.get("request_id")
            request_digest = source.get("request_digest")
            requested_consumer = requested_by.get("consumer_id")
            if (not isinstance(outcome, str) or not outcome.strip()
                    or len(outcome) > 256 or "\x00" in outcome):
                raise LifecycleError("stored completion outcome is invalid")
            if (not isinstance(summary, str) or len(summary) > 4096
                    or "\x00" in summary):
                raise LifecycleError("stored completion summary is invalid")
            if request_id is not None and (
                not isinstance(request_id, str) or not request_id
                or len(request_id) > 160 or "\x00" in request_id
            ):
                raise LifecycleError("stored completion request identity is invalid")
            if (requested_by.get("principal_id") != session_principal
                    or not isinstance(requested_consumer, str)
                    or not requested_consumer.strip()
                    or len(requested_consumer) > 256
                    or "\x00" in requested_consumer):
                raise LifecycleError("stored completion requester provenance is invalid")
            expected_digest = digest_hex({
                "session_id": self.session_id,
                "request_id": request_id,
                "outcome": outcome,
                "summary": summary,
                "principal_id": session_principal,
            })
            if request_digest != expected_digest:
                raise LifecycleError(
                    "stored completion request identity failed integrity validation"
                )
        else:
            if (not isinstance(outcome, str) or not outcome.strip()
                    or len(outcome) > 256 or "\x00" in outcome):
                raise ValueError("completion outcome must be bounded nonempty text")
            if (summary is None):
                summary = ""
            if (not isinstance(summary, str) or len(summary) > 4096
                    or "\x00" in summary):
                raise ValueError("completion summary must be bounded text")
            if request_id is not None and (
                not isinstance(request_id, str) or not request_id
                or len(request_id) > 160 or "\x00" in request_id
            ):
                raise ValueError("completion request identity must be bounded nonempty text")
            requested_consumer = (
                consumer_id if consumer_id is not None
                else self.snapshot.get("consumer_id", "unknown")
            )
            if (not isinstance(requested_consumer, str)
                    or not requested_consumer.strip()
                    or len(requested_consumer) > 256
                    or "\x00" in requested_consumer):
                raise ValueError("completion consumer identity is invalid")
            requested_by = {
                "principal_id": session_principal,
                "consumer_id": requested_consumer,
            }
            request_digest = digest_hex({
                "session_id": self.session_id,
                "request_id": request_id,
                "outcome": outcome,
                "summary": summary,
                "principal_id": session_principal,
            })

        assert isinstance(outcome, str)
        assert isinstance(summary, str)
        assert isinstance(request_digest, str)
        completion_request_identity = request_id or "complete_" + request_digest
        completion_checkpoint_request_id = "req_completion_checkpoint_" + digest_hex({
            "session_id": self.session_id,
            "completion_request_identity": completion_request_identity,
            "request_digest": request_digest,
        }, length=32)

        def publish_completion_checkpoint() -> str:
            checkpoint = self.checkpoint(
                progress=summary or f"Session completed: {outcome}",
                remaining=[],
                request_id=completion_checkpoint_request_id,
            )
            identity = checkpoint.get("identity")
            if not isinstance(identity, str) or not identity.startswith("chk_"):
                raise LifecycleError(
                    "completion checkpoint publication returned no durable identity"
                )
            return identity

        existing_completion = self.snapshot.get("completion")
        lifecycle = self.snapshot.get("lifecycle")
        if lifecycle == "completed" and isinstance(existing_completion, dict):
            if existing_completion.get("request_digest") != request_digest:
                raise LifecycleError(
                    "session was completed by a different request identity"
                )
            checkpoint_id = existing_completion.get("checkpoint_id")
            checkpoint_record = (
                self.store.load_checkpoint(self.session_id, checkpoint_id)
                if isinstance(checkpoint_id, str)
                and checkpoint_id.startswith("chk_") else None
            )
            if (not isinstance(checkpoint_record, dict)
                    or checkpoint_record.get("request_id")
                    != completion_checkpoint_request_id):
                # Upgrade a completion written by an earlier Environment
                # revision, or reconcile a crash after the terminal
                # transition but before its checkpoint link was saved.
                checkpoint_id = publish_completion_checkpoint()
                existing_completion["checkpoint_id"] = checkpoint_id
                self.snapshot["completion"] = existing_completion
                self._save()
            if existing_completion.get("claims_release_error"):
                claims_report = self._release_own_claims(
                    f"session completed: {outcome}",
                    request_id=completion_request_identity,
                )
                combined_released = sorted(set(
                    existing_completion.get("claims_released", [])
                ) | set(claims_report["released"]))
                if (combined_released != existing_completion.get("claims_released", [])
                        or claims_report["error"]
                        != existing_completion.get("claims_release_error")):
                    existing_completion["claims_released"] = combined_released
                    existing_completion["claims_release_error"] = claims_report["error"]
                    existing_completion["claims_reconciled_at"] = utcnow()
                    self.snapshot["completion"] = existing_completion
                    self._save()
            return dict(existing_completion)

        live_lifecycles = ("active", "checkpointed", "waiting", "blocked")
        pending = self.snapshot.get("completion_pending")
        if isinstance(pending, dict):
            if pending.get("request_digest") != request_digest:
                raise LifecycleError(
                    "a different completion request is pending; retry it with --retry-pending"
                )
            requested_by = pending.get("requested_by", requested_by)
            request_id = pending.get("request_id")
        else:
            if lifecycle not in live_lifecycles:
                raise LifecycleError("only a live session can complete")
            pending = {
                "request_id": request_id,
                "request_digest": request_digest,
                "outcome": outcome,
                "summary": summary,
                "requested_by": requested_by,
                "requested_at": utcnow(),
            }
            self.snapshot["completion_pending"] = pending
            self._save()

        # Publish an idempotent checkpoint before making the session terminal
        # or releasing any owned claims. If the process stops at any later
        # boundary, retry_pending repeats this exact checkpoint request and
        # then resumes claim settlement without creating duplicate history.
        checkpoint_id = publish_completion_checkpoint()
        pending = dict(pending)
        pending["checkpoint_id"] = checkpoint_id
        pending["checkpoint_request_id"] = completion_checkpoint_request_id
        self.snapshot["completion_pending"] = pending
        self._save()

        if self.snapshot.get("lifecycle") in live_lifecycles:
            self.transition("completed", outcome)
        elif self.snapshot.get("lifecycle") != "completed":
            raise LifecycleError("session cannot recover completion from its current lifecycle")

        claims_report = self._release_own_claims(
            f"session completed: {outcome}",
            request_id=completion_request_identity,
        )
        completion = {
            "outcome": outcome,
            "summary": summary,
            "at": pending.get("requested_at", utcnow()),
            "request_id": request_id,
            "request_digest": request_digest,
            "checkpoint_id": checkpoint_id,
            "requested_by": requested_by,
            "claims_released": claims_report["released"],
            "claims_release_error": claims_report["error"],
        }
        self.snapshot["completion"] = completion
        self.snapshot.pop("completion_pending", None)
        events = self._log()
        if not any(
            event.get("type") == "session.completed"
            and event.get("payload", {}).get("request_digest") == request_digest
            for event in events
        ):
            self._emit(
                "session.completed", str(requested_by.get("consumer_id", "unknown")),
                {
                    "outcome": outcome,
                    "checkpoint_id": checkpoint_id,
                    "claims_released": claims_report["released"],
                    "request_id": request_id,
                    "request_digest": request_digest,
                    "authenticated_principal_id": requested_by.get("principal_id"),
                    "requested_by_consumer_id": requested_by.get("consumer_id"),
                },
            )
        self._save()
        return completion

    def fail(self, *, reason: str) -> dict[str, Any]:
        if self.snapshot.get("lifecycle") not in (
            "active", "resolving", "ready", "blocked", "waiting", "checkpointed"
        ):
            raise LifecycleError("session cannot fail from its current state")
        self.transition("failed", reason)
        claims_report = self._release_own_claims(f"session failed: {reason}")
        self._emit("session.failed", self.snapshot.get("consumer_id", "unknown"),
                   {"reason": reason, "claims_released": claims_report["released"]})
        self._save()
        return {"reason": reason, "claims_released": claims_report["released"],
                "claims_release_error": claims_report["error"]}

    # -- inspection ------------------------------------------------------------

    def context(self) -> dict[str, Any]:
        """Return the bounded first-use context for this session.

        The context intentionally projects only the facts a consumer needs to
        orient itself. ``inspect`` remains the detailed state/binding path.
        This method is read-only and does not advance cursors or append events.
        """
        intent = self.snapshot.get("intent") or {}
        authority = self.effective_authority()
        protected = sorted(set(authority.get("protected_repositories", [])))
        writable = sorted(
            set(authority.get("writable", [])) - set(protected)
        )
        available: list[dict[str, Any]] = []
        unavailable = 0
        unknown = 0
        for binding in self.snapshot.get("bindings", []):
            availability = binding.get("availability") or {}
            if availability.get("status") == "available":
                available.append({
                    "capability": binding.get("capability"),
                    "provider": binding.get("provider"),
                    "contract_revision": binding.get("contract_revision"),
                })
            elif availability.get("status") == "unknown":
                unknown += 1
            else:
                unavailable += 1

        max_capabilities = 20
        session_id = self.session_id
        campaign = self.snapshot.get("campaign") or {}
        observations = continuation_observations(
            self.store, self.snapshot, session_id
        )
        authorized_consumers = list(campaign.get("authorized_consumers", []))
        previous_consumers = [
            value for value in authorized_consumers
            if value != campaign.get("current_consumer_id")
        ]
        persisted_campaign_state = observations.get("campaign_state")
        campaign_evidence = (
            persisted_campaign_state.get("evidence", [])
            if isinstance(persisted_campaign_state, dict)
            else list(campaign.get("evidence", []))[-8:]
        )
        campaign_delivery = (
            persisted_campaign_state.get("delivery")
            if isinstance(persisted_campaign_state, dict)
            else campaign.get("delivery", {"status": "pending"})
        )
        continuation = {
            "identity": campaign.get("identity"),
            "session_ids": list(campaign.get("session_ids", []))[-8:],
            "claim_ids": observations["claim_ids"],
            "repositories": [
                {key: item.get(key) for key in
                 ("repository", "observed_path", "checkout_identity",
                  "git_common_directory_identity", "branch", "head", "clean",
                  "observation")}
                for item in campaign.get("repository_refs", [])[:16]
                if isinstance(item, dict)
            ],
            "checkpoint_ids": list(self.snapshot.get("checkpoints", []))[-4:],
            "work_intent": campaign.get("work_intent", {
                "goal": intent.get("goal", ""),
            }),
            "evidence": campaign_evidence,
            "delivery": campaign_delivery,
            "unresolved_pressures": list(
                campaign.get("unresolved_pressures", [])
            )[:12],
        }
        pending_completion = self.snapshot.get("completion_pending")
        completion = self.snapshot.get("completion")
        recovery_record = (
            pending_completion if isinstance(pending_completion, dict) else None
        )
        recovery_state = "pending"
        if recovery_record is None and (
            isinstance(completion, dict)
            and completion.get("claims_release_error")
        ):
            recovery_record = completion
            recovery_state = "claim-release-pending"
        if recovery_record is not None:
            requested_by = recovery_record.get("requested_by")
            requested_by = requested_by if isinstance(requested_by, dict) else {}
            continuation["completion_recovery"] = {
                "state": recovery_state,
                "request_id": recovery_record.get("request_id"),
                "requested_by": {
                    "principal_id": requested_by.get("principal_id"),
                    "consumer_id": requested_by.get("consumer_id"),
                },
                "claim_release_error": (
                    str(recovery_record.get("claims_release_error", ""))[:256]
                    if recovery_state == "claim-release-pending" else None
                ),
                "next": (
                    f"mncs-env complete {session_id} --retry-pending"
                ),
            }
        if isinstance(persisted_campaign_state, dict):
            continuation["campaign_state_checkpoint_id"] = (
                persisted_campaign_state.get("checkpoint_id")
            )
            continuation["campaign_state_recorded_at"] = (
                persisted_campaign_state.get("recorded_at")
            )
            continuation["campaign_state_provenance"] = (
                persisted_campaign_state.get("recorded_by")
            )
            continuation["evidence_count"] = persisted_campaign_state.get(
                "evidence_count", len(campaign_evidence)
            )
            continuation["evidence_truncated"] = persisted_campaign_state.get(
                "evidence_truncated", False
            )
        if observations["foreign_claims_count"]:
            continuation.update({
                "foreign_claims": observations["foreign_claims"],
                "foreign_claims_count": observations["foreign_claims_count"],
                "foreign_claims_truncated": observations["foreign_claims_truncated"],
            })
        if observations["latest_checkpoint"] is not None:
            continuation["latest_checkpoint"] = observations["latest_checkpoint"]
        if previous_consumers:
            continuation["previous_consumers"] = previous_consumers[-7:]
            continuation["previous_consumers_truncated"] = len(previous_consumers) > 7
        return {
            "schema_version": "mncs.environment.entry-context/1",
            "session_id": session_id,
            "lifecycle": self.snapshot.get("lifecycle"),
            "environment_id": self.snapshot.get("environment_id"),
            "consumer_id": self.snapshot.get("consumer_id"),
            "configuration": self.snapshot.get("configuration", {}),
            "state_dir": str(self.state_dir.expanduser().resolve()),
            "persistence": "store" if hasattr(self.store, "backend") else "file",
            "readiness": readiness_module.summarize(self.snapshot),
            "service_operations": self.snapshot.get("service_operations", []),
            "doctor": self.doctor_summary(),
            **({"rights_context": self.snapshot["rights_context"]}
               if isinstance(self.snapshot.get("rights_context"), dict) else {}),
            "projects": [{"repository": repo.get("manifest_repository") or repo.get("name"),
                          "path": repo.get("path"), "branch": repo.get("branch"),
                          "dirty": repo.get("dirty")}
                         for repo in self.snapshot.get("workspace", {}).get("repositories", [])][:20],
            "project_count": self.snapshot.get("workspace", {}).get("repository_count", 0),
            "toolchain": self.snapshot.get("toolchain"),
            "execution_stack": self.snapshot.get("execution_stack"),
            "workspace_root": (self.snapshot.get("workspace") or {}).get("root"),
            "work_intent": {
                "identity": intent.get("identity"),
                "goal": intent.get("goal", ""),
            },
            "continuation": continuation,
            "writable_repositories": writable,
            "protected_repositories": protected,
            "authority": {
                "subject": authority.get("subject"),
                "readable": list(authority.get("readable", [])),
                "writable": writable,
                "invocable": list(authority.get("invocable", [])),
                "protected_repositories": protected,
                "escalation_required": list(authority.get("escalation_required", [])),
                "claim_required_actions": list(authority.get("claim_required_actions", [])),
                "scope_grants": list(authority.get("scope_grants", [])),
                "revision": authority.get("revision"),
            },
            "capabilities": {
                "available": available[:max_capabilities],
                "available_count": len(available),
                "unavailable_count": unavailable,
                "unknown_count": unknown,
                "truncated": len(available) > max_capabilities,
            },
            "actions": self.actions(),
            "next_commands": [shlex.join(self.actions()[
                "reconcile" if readiness_module.summarize(self.snapshot)["blocking"] else "capabilities"
            ]["argv"])],
        }

    def actions(self) -> dict[str, Any]:
        """Executable argv addressing, preserving state/backend across cwd changes."""
        prefix = [sys.executable, str(Path(__file__).resolve().parents[1] / "scripts" / "mncs-env"),
                  "--state-dir", str(self.state_dir.expanduser().resolve()),
                  "--persistence", "store" if hasattr(self.store, "backend") else "file"]
        actions = {name: {"argv": [*prefix, name, self.session_id]} for name in
                   ("health", "reconcile", "inspect", "capabilities")}
        actions["resources"] = {"argv": [*prefix, "resources"]}
        return actions

    def health(self, *, live: bool = False) -> dict[str, Any]:
        """Read-only inspection; no participation event or cursor advancement.

        By default a valid doctor epoch answers from the validated snapshot
        (labeled `observation: epoch`) without rescanning or rebinding, but
        declared services are always probed live: provider runtime state is
        observable only by probing. Any doubt falls through to the live
        path. `live=True` forces full live probes.
        """
        selected_checkout_drift = composition.selected_checkout_drift(
            self.snapshot.get("execution_roles", {}),
            self.snapshot.get("bindings", []),
            self.snapshot.get("selected_checkouts", {}),
            self.snapshot.get("workspace", {}).get("root") or ".",
        )
        if not live:
            from . import doctor as doctor_module
            epoch = self.snapshot.get("doctor", {}).get("epoch") if isinstance(
                self.snapshot.get("doctor"), dict) else None
            if isinstance(epoch, dict):
                valid, _ = doctor_module.validate_epoch(self, epoch)
                if valid:
                    match, services = doctor_module.live_services_match(self)
                    if match:
                        fresh = [capabilities_module.probe_availability(binding)
                                 for binding in self.snapshot.get("bindings", [])]
                        execution_stack = composition.resolve(
                            self.snapshot.get("execution_roles", {}), fresh,
                            self.snapshot.get("toolchain"), self.snapshot.get("execution_stack"),
                            compatibility_service=self.snapshot.get("execution_compatibility_service"),
                            service_observations=services,
                        )
                        summary = readiness_module.summarize(
                            self.snapshot, bindings=fresh, services=services, live=False)
                        summary, execution_stack = _apply_checkout_drift(
                            summary, execution_stack, selected_checkout_drift)
                        summary["observation"] = "epoch"
                        summary["epoch"] = epoch.get("digest")
                        return {"session_id": self.session_id,
                                "environment_id": self.snapshot.get("environment_id"),
                                "readiness": summary,
                                "execution_stack": execution_stack,
                                "unavailable_capabilities": [
                                    {"capability": item["capability"], **item["availability"]}
                                    for item in fresh if item["availability"]["status"] == "unavailable"],
                                "unknown_capabilities": [
                                    {"capability": item["capability"], **item["availability"]}
                                    for item in fresh if item["availability"]["status"] == "unknown"],
                                "doctor": doctor_module.terse(self),
                                "actions": self.actions()}
        fresh = [capabilities_module.probe_availability(binding) for binding in self.snapshot.get("bindings", [])]
        selectors = self.snapshot.get("execution_roles", {})
        prior_stack = self.snapshot.get("execution_stack")
        selected_service = self.snapshot.get("execution_compatibility_service")
        current_stack = composition.resolve(
            selectors, fresh, self.snapshot.get("toolchain"), prior_stack,
            compatibility_service=selected_service,
        )
        services = readiness_module.probe_services(
            self, bindings=fresh, composition_identity=current_stack.get("identity"))
        execution_stack = composition.resolve(
            selectors, fresh, self.snapshot.get("toolchain"), prior_stack,
            compatibility_service=selected_service,
            service_observations=services,
        )
        snapshot = dict(self.snapshot)
        snapshot["workspace"] = workspace_module.discover_workspace(
            self.snapshot.get("workspace", {}).get("root", "."),
            repositories=self.snapshot.get("workspace", {}).get("selection"))
        summary = readiness_module.summarize(snapshot, bindings=fresh, services=services, live=True)
        summary, execution_stack = _apply_checkout_drift(
            summary, execution_stack, selected_checkout_drift)
        return {"session_id": self.session_id, "environment_id": self.snapshot.get("environment_id"),
                "readiness": summary,
                "execution_stack": execution_stack,
                "unavailable_capabilities": [{"capability": item["capability"], **item["availability"]}
                                             for item in fresh if item["availability"]["status"] == "unavailable"],
                "unknown_capabilities": [{"capability": item["capability"], **item["availability"]}
                                         for item in fresh if item["availability"]["status"] == "unknown"],
                "actions": self.actions()}

    def reconcile(self) -> dict[str, Any]:
        """Refresh discovery and delegate declared service recovery, then verify."""
        from . import doctor as doctor_module
        previous_bindings = [dict(binding) for binding in self.snapshot.get("bindings", [])]
        previous_services = [dict(item) for item in self.snapshot.get("service_observations", [])]
        revalidation = self.revalidate()
        result = readiness_module.reconcile_services(self, force_recovery=True)
        repairs = doctor_module.repair_delta(previous_bindings, self.snapshot.get("bindings", []),
                                              previous_services,
                                              self.snapshot.get("service_observations", []))
        report = doctor_module.record_epoch(
            self, repairs=repairs,
            reconciliations=[{"id": "reconcile:explicit", "class": "bounded_reconciliation",
                              "provider": "mncs-environment",
                              "detail": "explicit reconcile: full revalidation and service recovery",
                              "validated": True}],
            operations=result["operations"], revalidation=revalidation)
        return {**self.context(), "reconciliation": {"revalidation": revalidation, **result},
                "doctor": report}

    def status(self) -> dict[str, Any]:
        """Read-only alias for the compact session context."""
        return self.context()

    def doctor_summary(self) -> dict[str, Any] | None:
        """Last terse doctor state, or None before the first ambient pass."""
        from . import doctor as doctor_module
        if not isinstance(self.snapshot.get("doctor"), dict):
            return None
        return doctor_module.terse(self)

    def inspect(self) -> dict[str, Any]:
        log = self._log()
        unavailable = [
            {
                "provider": binding.get("provider"),
                "capability": binding.get("capability"),
                "reason": (binding.get("availability") or {}).get("reason"),
            }
            for binding in self.snapshot.get("bindings", [])
            if (binding.get("availability") or {}).get("status") == "unavailable"
        ]
        unknown = [
            {
                "provider": binding.get("provider"),
                "capability": binding.get("capability"),
                "reason": (binding.get("availability") or {}).get("reason"),
            }
            for binding in self.snapshot.get("bindings", [])
            if (binding.get("availability") or {}).get("status") == "unknown"
        ]
        return {
            "session_id": self.session_id,
            "lifecycle": self.snapshot.get("lifecycle"),
            "consumer_id": self.snapshot.get("consumer_id"),
            "consumer_kind": self.snapshot.get("consumer_kind"),
            "environment_id": self.snapshot.get("environment_id"),
            "configuration": self.snapshot.get("configuration", {}),
            "requirements": self.snapshot.get("requirements", {}),
            "readiness": readiness_module.summarize(self.snapshot),
            "intent": self.snapshot.get("intent"),
            "authority": self.effective_authority(),
            "rights": self.snapshot.get("rights", {}),
            **({
                "rights_context": self.snapshot["rights_context"],
                "rights_context_inputs": self.snapshot.get("rights_context_inputs", {}),
            } if isinstance(self.snapshot.get("rights_context"), dict) else {}),
            "workspace": self.snapshot.get("workspace", {}),
            "selected_checkouts": self.snapshot.get("selected_checkouts", {}),
            "repo_facts": self.snapshot.get("repo_facts", {}),
            "toolchain": self.snapshot.get("toolchain"),
            "execution_stack": self.snapshot.get("execution_stack"),
            "claim_holders": self.snapshot.get("claim_holders", {}),
            "unavailable_capabilities": unavailable,
            "unknown_capabilities": unknown,
            "bindings": [dict(binding) for binding in self.snapshot.get("bindings", [])],
            "binding_toolchains": [
                {
                    "provider": binding.get("provider"),
                    "capability": binding.get("capability"),
                    "address": binding.get("address"),
                    "toolchain_address": binding.get("toolchain_address"),
                    "toolchain_env": binding.get("toolchain_env"),
                }
                for binding in self.snapshot.get("bindings", [])
                if binding.get("toolchain_address")
            ],
            "event_count": len(log),
            "latest_events": log[-5:],
            "checkpoints": self.snapshot.get("checkpoints", []),
            "handoffs": self.snapshot.get("handoffs", []),
            "artifacts": self.snapshot.get("artifacts", []),
            "coherence": {key: value for key, value in self.snapshot.get("coherence", {}).items()
                          if key in ("schema_version", "stable", "store_cursor", "policy_identity",
                                     "policy_receipt", "last_trace", "deadlines", "result_refs",
                                     "repositories", "artifacts", "libraries")},
            "completion": self.snapshot.get("completion"),
            "lifecycle_history": self.snapshot.get("lifecycle_history", []),
        }
