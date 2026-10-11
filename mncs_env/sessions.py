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

import json
import re
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from . import authority as authority_module
from . import capabilities as capabilities_module
from . import claims as claims_module
from . import composition
from . import events as events_module
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

CONTINUATION_MAX_CHECKPOINT_REMAINING = 8
CONTINUATION_MAX_FOREIGN_CLAIMS = 12
CONTINUATION_MAX_CLAIM_PATHS = 4
CAMPAIGN_EVIDENCE_MAX = 32
CAMPAIGN_DELIVERY_REPOSITORIES_MAX = 16
CAMPAIGN_STATE_INPUT_MAX_BYTES = 7168

_CAMPAIGN_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_CAMPAIGN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_GIT_OBJECT_ID_PATTERN = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")

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


def _latest_campaign_state_checkpoint(
    checkpoints: list[dict[str, Any]], campaign_identity: str | None,
) -> dict[str, Any] | None:
    candidates = [record for record in checkpoints
                  if isinstance(record, dict) and isinstance(record.get("campaign_state"), dict)]
    if not candidates:
        return None
    for record in candidates:
        state = record["campaign_state"]
        if (state.get("schema_version") != "mncs.environment.campaign-state/1"
                or state.get("campaign_identity") != campaign_identity
                or record.get("campaign_state_digest") != digest_hex(state)):
            raise LifecycleError("campaign state checkpoint failed identity or digest validation")
        try:
            evidence = normalize_campaign_evidence(state.get("evidence"))
            delivery = normalize_campaign_delivery(state.get("delivery"))
        except ValueError as error:
            raise LifecycleError(f"campaign state checkpoint is invalid: {error}") from error
        if evidence != state.get("evidence") or delivery != state.get("delivery"):
            raise LifecycleError("campaign state checkpoint is not canonically normalized")
        provenance = state.get("recorded_by")
        if (not isinstance(provenance, dict)
                or not isinstance(provenance.get("consumer_id"), str)
                or (provenance.get("principal_id") is not None
                    and not isinstance(provenance.get("principal_id"), str))):
            raise LifecycleError("campaign state checkpoint lacks Environment provenance")
    candidates.sort(key=lambda record: (
        int(record.get("sequence", 0)), str(record.get("created_at", "")),
        str(record.get("identity", "")),
    ))
    latest_sequence = int(candidates[-1].get("sequence", 0))
    same_sequence = [record for record in candidates
                     if int(record.get("sequence", 0)) == latest_sequence]
    digests = {record.get("campaign_state_digest") for record in same_sequence}
    if len(digests) > 1:
        raise LifecycleError(
            "conflicting campaign state checkpoints share one sequence; reconcile before updating"
        )
    return candidates[-1]


def _project_campaign_state(
    campaign: dict[str, Any], checkpoint: dict[str, Any] | None,
) -> bool:
    if checkpoint is None:
        return False
    state = checkpoint["campaign_state"]
    values = {
        "evidence": state["evidence"],
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

    campaign_state_record = _latest_campaign_state_checkpoint(
        checkpoints, campaign.get("identity")
    )
    campaign_state_projection = None
    if campaign_state_record is not None:
        state = campaign_state_record["campaign_state"]
        evidence = state["evidence"]
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
            "evidence": evidence[-8:],
            "evidence_count": len(evidence),
            "evidence_truncated": len(evidence) > 8,
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
        language_root = selected_roots.get("mncs-language")
        language_binary = None
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
        if binding["availability"]["status"] != "available"
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
        "binding_ids": sorted(binding["binding_id"] for binding in bindings),
        "intent": intent["identity"],
        "claim_holders": claim_holders,
    }
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
        "intent": intent,
        "protected_repositories": protected,
        "authority": authority_context,
        "claim_holders": claim_holders,
        "rights": rights,
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
        campaign_state_record = _latest_campaign_state_checkpoint(
            checkpoints, campaign.get("identity")
        )
        campaign_state_changed = _project_campaign_state(
            campaign, campaign_state_record
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
    ) -> "Session":
        """Resume active participation: transitions abandoned sessions, logs resume."""
        instance = cls.open(
            state_dir=state_dir, session_id=session_id, backend=backend,
            verify_on_open=verify_on_open, store=store)
        if instance.snapshot.get("lifecycle") in ("completed", "failed"):
            raise LifecycleError(f"session {session_id} is terminal and cannot resume")
        if instance.snapshot.get("lifecycle") == "abandoned":
            instance.transition("active", "resumed from abandoned")
        instance._emit("session.resumed", "environment",
                       {"consumer_id": instance.snapshot.get("consumer_id")})
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
    ) -> dict[str, Any]:
        last_error: Exception | None = None
        for _ in range(MAX_EMIT_RETRIES):
            if self._next_seq_hint is None:
                sequences = self.store.existing_sequences(self.session_id)
                self._next_seq_hint = (max(sequences) + 1) if sequences else 1
            sequence = self._next_seq_hint
            event = events_module.make(
                session_id=self.session_id,
                sequence=sequence,
                event_type=event_type,
                producer=producer,
                payload=payload,
                causes=causes,
            )
            try:
                self.store.put_event(self.session_id, sequence, event)
                self._invalidate_log()
                self._next_seq_hint = sequence + 1
                return event
            except SequenceTaken as error:
                last_error = error
                self._next_seq_hint = None
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
        normalized["checkout"] = str(path.resolve())
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
        if action in ("write", "mutate", "execute", "publish", "merge", "delete"):
            # Current entry observations are not a mutation lease. Re-read
            # claims and the exact selected target at the authority boundary;
            # broad discovery is unnecessary on a quiet observational entry.
            self._refresh_holders()
            selected = self.snapshot.get("selected_checkouts", {}).get(repository)
            if repo_facts is None and isinstance(selected, dict) and selected.get("path"):
                root = Path(self.snapshot.get("workspace", {}).get("root", "."))
                inspected = workspace_module.inspect_repo((root / selected["path"]).resolve(), _refresh=True)
                if inspected is None or inspected.git_error:
                    raise LifecycleError("selected checkout observation unavailable at mutation boundary")
                record = inspected.record()
                record["name"] = repository
                repo_facts = {**self.snapshot.get("repo_facts", {}),
                              **_repo_facts({"repositories": [record]})}
        verdict = authority_module.evaluate(
            self.snapshot.get("authority", {}),
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

    def invoke(
        self,
        capability: str,
        argv: list[str],
        *,
        cwd: str | Path | None = None,
        timeout_seconds: int | None = None,
        output_limit_bytes: int = capabilities_module.DEFAULT_OUTPUT_LIMIT_BYTES,
        env: dict[str, str] | None = None,
        effect_target: dict[str, Any] | None = None,
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
        output_limit_bytes = capabilities_module.validate_output_limit_bytes(
            output_limit_bytes
        )
        binding = self._binding(capability)
        state_root = self.store.state_dir.resolve()
        artifact_directory = (
            state_root / "sessions" / self.session_id / "artifacts"
            / digest_hex(capability)
        ).resolve()
        if not artifact_directory.is_relative_to(state_root):
            raise LifecycleError("session artifact directory escapes the Environment state root")
        artifact_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        invocation_env = dict(env or {})
        invocation_env.update(self._selected_runtime_environment(binding))
        invocation_env["MNCS_ENV_SESSION_ARTIFACT_DIR"] = str(artifact_directory)
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
                    if isinstance(branch, str) and branch:
                        effect_scope["branch"] = branch
                effect_verdict = self.check(
                    action=required, target=provider, scope=effect_scope)
            if effect_verdict["verdict"] == "deny":
                raise AuthorityDenied(effect_verdict["reason"])
            if effect_verdict["verdict"] == "escalate":
                return self._pending(capability, argv, required, effect_verdict["reason"])
        self._emit("capability.invoked", self.snapshot.get("consumer_id", "unknown"),
                   {"capability": capability, "argv": argv, "executor": executor,
                    "effect_target": admitted_effect_target})
        result = capabilities_module.invoke(
            binding, argv, cwd=cwd, timeout_seconds=timeout_seconds,
            output_limit_bytes=output_limit_bytes, env=invocation_env)
        self.snapshot.setdefault("artifacts", []).append(
            {"kind": "invocation-result", "capability": capability,
             "status": result["status"], "executor": executor,
             "effect_target": admitted_effect_target,
             "artifact_directory": str(artifact_directory), "at": utcnow()}
        )
        self._emit("invocation.completed", binding.get("provider", "unknown"),
                   {"capability": capability, "status": result["status"],
                    "returncode": result["returncode"]})
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
    ) -> dict[str, Any]:
        selected = self.snapshot.get("selected_checkouts", {}).get(repository)
        if selected is not None:
            if scope and scope.get("kind") == "worktree":
                scope = self._canonical_scope(repository, scope)
                requested = Path(str(scope.get("checkout", ""))).resolve()
                observed = Path(str(selected.get("path", "")))
                if not observed.is_absolute():
                    root = self.snapshot.get("workspace", {}).get("root")
                    if root:
                        observed = Path(str(root)) / observed
                observed = observed.resolve()
                if requested != observed:
                    raise ValueError(
                        f"claim checkout does not match the session-selected checkout for {repository}"
                    )
                scope = dict(scope)
                scope["checkout"] = str(observed)
                scope.setdefault("branch", selected.get("branch"))
            if checkout_facts is None:
                clean = bool(selected.get("clean", False))
                checkout_facts = {
                    "head": selected.get("head"),
                    "branch": selected.get("branch"),
                    "dirty": not clean,
                    "foreign_signals": [] if clean else ["dirty-tree"],
                }
        if workspace_root is None:
            workspace_root = self.snapshot.get("workspace", {}).get("root")
        record = claims_module.acquire(
            self.store, repository=repository, session_id=self.session_id,
            consumer_id=self.snapshot.get("consumer_id", "unknown"),
            basis=basis, reason=reason, ttl_hours=ttl_hours,
            scope=scope, checkout_facts=checkout_facts,
            workspace_root=workspace_root, request_id=request_id,
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
        self._emit("lease.acquired", self.snapshot.get("consumer_id", "unknown"),
                   {"repository": repository, "basis": basis,
                    "claim_id": record["identity"],
                    "scope": record.get("scope", {})})
        self._save()
        return record

    def release_claim(self, repository: str, reason: str = "",
                      claim_id: str | None = None,
                      request_id: str | None = None) -> bool:
        released = claims_module.release(
            self.store, session_id=self.session_id, reason=reason,
            claim_id=claim_id, repository=repository or None,
            request_id=request_id)
        if not released:
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
        self._emit("lease.released", self.snapshot.get("consumer_id", "unknown"),
                   {"repository": repository, "claim_id": claim_id,
                    "released": len(released)})
        self._save()
        return True

    def transfer_claim(self, claim_id: str, to_session: str,
                       to_consumer: str, reason: str = "",
                       request_id: str | None = None) -> dict[str, Any]:
        record = claims_module.transfer(
            self.store, claim_id=claim_id, from_session=self.session_id,
            to_session=to_session, to_consumer=to_consumer, reason=reason,
            request_id=request_id)
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
        self._emit("lease.transferred", self.snapshot.get("consumer_id", "unknown"),
                   {"claim_id": claim_id, "to_session": to_session})
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
        self, event_type: str, producer: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return self._emit(event_type, producer, payload)

    def observe_workspace(self, workspace_root: str | Path) -> list[dict[str, Any]]:
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
            self._emit(event["type"], event["producer"], event["payload"])
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

    def revalidate(self) -> dict[str, Any]:
        """Refresh selected providers, reprobe bindings, and reconcile claims."""
        workspace_root = self.snapshot.get("workspace", {}).get("root")
        if workspace_root:
            self.observe_workspace(workspace_root)

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
            language_binary: Path | str = "/nonexistent/mncs-language/mncs"
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
            "provider_root", "event_types",
        )
        report: dict[str, Any] = {
            "reprobed": 0, "reused": 0, "changed": [], "bound": [], "unbound": []
        }
        for key, binding in current_by_key.items():
            previous = prior_by_key.get(key)
            if previous is None:
                report["bound"].append(binding.get("capability"))
                self._emit("capability.bound", "environment",
                           {"capability": binding.get("capability"),
                            "provider": binding.get("provider"),
                            "provider_root": binding.get("provider_root")})
            elif any(previous.get(field) != binding.get(field) for field in binding_fields):
                self._emit("capability.changed", "environment",
                           {"capability": binding.get("capability"),
                            "provider": binding.get("provider"),
                            "previous_revision": previous.get("contract_revision"),
                            "current_revision": binding.get("contract_revision"),
                            "provider_root": binding.get("provider_root")})
        for key, previous in prior_by_key.items():
            if key not in current_by_key:
                report["unbound"].append(previous.get("capability"))
                self._emit("capability.unbound", "environment",
                           {"capability": previous.get("capability"),
                            "provider": previous.get("provider"),
                            "provider_root": previous.get("provider_root")})

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
                self._emit(
                    "capability.available" if fresh["availability"]["status"] == "available"
                    else "capability.unavailable",
                    "environment",
                    {"capability": binding["capability"], "previous": before_status},
                )
        self.snapshot["bindings"] = new_bindings
        selectors = self.snapshot.get("execution_roles", {})
        self.snapshot["execution_stack"] = composition.resolve(
            selectors, new_bindings, self.snapshot.get("toolchain"), self.snapshot.get("execution_stack"),
            compatibility_service=self.snapshot.get("execution_compatibility_service"),
            service_observations=self.snapshot.get("service_observations", []),
        )
        holders = claims_module.holders(self.store.read_claims())
        if holders != self.snapshot.get("claim_holders", {}):
            self._emit("adapter.observed", "environment",
                       {"note": "claim holders changed", "holders": holders})
        self.snapshot["claim_holders_detailed"] = holders
        self.snapshot["claim_holders"] = holders
        self.snapshot.setdefault("authority", {})["claim_holders"] = dict(holders)
        self._save()
        return report

    # -- checkpoint / handoff / completion ------------------------------------

    def checkpoint(self, *, progress: str = "", remaining: list[str] | None = None,
                   request_id: str | None = None,
                   campaign_evidence: list[dict[str, Any]] | None = None,
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
        campaign_update_requested = (
            campaign_evidence is not None or campaign_delivery is not None
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
        campaign_delivery_value: dict[str, Any] | None = None
        if record is None and campaign_update_requested:
            campaign = dict(self.snapshot.get("campaign") or {})
            campaign_identity = campaign.get("identity")
            if (not isinstance(campaign_identity, str)
                    or not _CAMPAIGN_ID_PATTERN.fullmatch(campaign_identity)):
                raise LifecycleError(
                    "durable campaign metadata requires an Environment campaign identity"
                )
            latest_campaign_record = _latest_campaign_state_checkpoint(
                checkpoint_records, campaign_identity
            )
            latest_campaign_id = (
                latest_campaign_record.get("identity")
                if latest_campaign_record is not None else "none"
            )
            if campaign_base_checkpoint != latest_campaign_id:
                raise LifecycleError(
                    "campaign state changed since the supplied base checkpoint; read the capsule and retry with its current campaign state checkpoint"
                )
            if latest_campaign_record is not None:
                prior_state = latest_campaign_record["campaign_state"]
                campaign_evidence_value = prior_state["evidence"]
                campaign_delivery_value = prior_state["delivery"]
            else:
                try:
                    campaign_evidence_value = normalize_campaign_evidence(
                        campaign.get("evidence", [])
                    )
                    campaign_delivery_value = normalize_campaign_delivery(
                        campaign.get("delivery", {"status": "pending"})
                    )
                except ValueError as error:
                    raise LifecycleError(
                        f"existing campaign projection is invalid: {error}"
                    ) from error
            if normalized_evidence is not None:
                prior_by_identity = {
                    item["identity"]: item for item in campaign_evidence_value
                }
                new_by_identity = {
                    item["identity"]: item for item in normalized_evidence
                }
                if any(new_by_identity.get(key) != item
                       for key, item in prior_by_identity.items()):
                    raise LifecycleError(
                        "campaign evidence updates must retain every prior immutable reference"
                    )
                campaign_evidence_value = normalized_evidence
            if normalized_delivery is not None:
                campaign_delivery_value = normalized_delivery
            evidence_ids = {
                item["identity"] for item in campaign_evidence_value
            }
            if any(item.get("evidence_identity") not in evidence_ids
                   for item in campaign_delivery_value.get("repositories", [])
                   if item.get("evidence_identity") is not None):
                raise ValueError(
                    "campaign delivery references an evidence identity absent from campaign evidence"
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
                    "schema_version": "mncs.environment.campaign-state/1",
                    "campaign_identity": campaign_identity,
                    "evidence": campaign_evidence_value,
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
            _project_campaign_state(campaign, record)
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

    def _release_own_claims(self, reason: str) -> dict[str, Any]:
        """Release every live claim held by this session.

        Terminal sessions must not pin scopes until TTL expiry: a
        finished agent's claims would otherwise block the next agent
        for up to 24h. Release failures never block the terminal
        transition itself (TTL expiry remains the backstop); they are
        recorded on the completion payload instead.
        """
        released: list[str] = []
        error: str | None = None
        try:
            live = claims_module.active_claims(self.store.read_claims())
            repositories = sorted({
                str(record.get("repository", ""))
                for record in live.values()
                if record.get("session_id") == self.session_id
                and record.get("repository")
            })
            for repository in repositories:
                for record in claims_module.release(
                    self.store, session_id=self.session_id,
                    reason=reason, repository=repository,
                ):
                    released.append(str(record.get("claim_id", "")))
        except Exception as exc:  # noqa: BLE001 - recorded, never raised
            error = f"{type(exc).__name__}: {exc}"
        if released:
            self._refresh_holders()
            self._emit("lease.released", self.snapshot.get("consumer_id", "unknown"),
                       {"released": released, "reason": reason})
        return {"released": released, "error": error}

    def complete(self, *, outcome: str, summary: str = "") -> dict[str, Any]:
        if self.snapshot.get("lifecycle") not in ("active", "checkpointed", "waiting", "blocked"):
            raise LifecycleError("only a live session can complete")
        self.transition("completed", outcome)
        claims_report = self._release_own_claims(f"session completed: {outcome}")
        self.snapshot["completion"] = {"outcome": outcome, "summary": summary, "at": utcnow(),
                                       "claims_released": claims_report["released"],
                                       "claims_release_error": claims_report["error"]}
        self._emit("session.completed", self.snapshot.get("consumer_id", "unknown"),
                   {"outcome": outcome, "claims_released": claims_report["released"]})
        self._save()
        return self.snapshot["completion"]

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
        authority = self.snapshot.get("authority") or {}
        protected = sorted(set(authority.get("protected_repositories", [])))
        writable = sorted(
            set(authority.get("writable", [])) - set(protected)
        )
        available: list[dict[str, Any]] = []
        unavailable = 0
        for binding in self.snapshot.get("bindings", []):
            availability = binding.get("availability") or {}
            if availability.get("status") == "available":
                available.append({
                    "capability": binding.get("capability"),
                    "provider": binding.get("provider"),
                    "contract_revision": binding.get("contract_revision"),
                })
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
            },
            "capabilities": {
                "available": available[:max_capabilities],
                "available_count": len(available),
                "unavailable_count": unavailable,
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
                                    for item in fresh if item["availability"]["status"] != "available"],
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
                                             for item in fresh if item["availability"]["status"] != "available"],
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
            if (binding.get("availability") or {}).get("status") != "available"
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
            "authority": self.snapshot.get("authority"),
            "rights": self.snapshot.get("rights", {}),
            "workspace": self.snapshot.get("workspace", {}),
            "selected_checkouts": self.snapshot.get("selected_checkouts", {}),
            "repo_facts": self.snapshot.get("repo_facts", {}),
            "toolchain": self.snapshot.get("toolchain"),
            "execution_stack": self.snapshot.get("execution_stack"),
            "claim_holders": self.snapshot.get("claim_holders", {}),
            "unavailable_capabilities": unavailable,
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
