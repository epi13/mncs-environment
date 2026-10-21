"""Rights/provenance gate for session environments.

Claim records come from the environment definition (``rights_claims``
inline, or ``rights_claims_file`` pointing at a JSON list resolved
relative to the workspace root). Evaluation delegates to the generic
host surface in ``mncs_rights_provenance.host_gate`` -- this module
holds no rights logic of its own.

When the rights package is unavailable the gate reports ``review``
for every subject with ``available: false``: unknown provenance never
passes silently, and it never blocks on a missing library either.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from pathlib import Path, PurePosixPath
from typing import Any


class RightsBlocked(Exception):
    """A workspace repository is rights-blocked for this session."""


RIGHTS_CONTEXT_SCHEMA = "mncs.rights-provenance.agent-context/1"
RIGHTS_CONTEXT_BINDING_SCHEMA = "mncs.environment.rights-context-binding/1"
MAX_RIGHTS_CONTEXT_SOURCE_BYTES = 256 * 1024
MAX_RIGHTS_CONTEXT_BYTES = 8192


class RightsContextSourceError(ValueError):
    """A bound rights-context source could not be safely read."""


def _bounded_text(value: Any, limit: int) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def _read_bound_bytes(root: Path, reference: Any) -> tuple[bytes, str, str]:
    if (
        not isinstance(reference, str) or not reference or "\x00" in reference
        or len(reference) > 512 or "\\" in reference
    ):
        raise RightsContextSourceError("source reference is missing or malformed")
    relative = PurePosixPath(reference)
    if relative.is_absolute() or any(part in {"..", "."} for part in relative.parts):
        raise RightsContextSourceError("source reference must remain inside the workspace root")
    target = root
    try:
        for part in relative.parts:
            target = target / part
            if target.is_symlink():
                raise RightsContextSourceError("source reference traverses a symbolic link")
        before = target.stat(follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise RightsContextSourceError("source reference is not a regular file")
        if before.st_size > MAX_RIGHTS_CONTEXT_SOURCE_BYTES:
            raise RightsContextSourceError("source file exceeds the bounded context input size")
        # O_NOFOLLOW protects the final component against replacement after
        # the component walk. Reads remain bounded even if the file grows.
        descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_size > MAX_RIGHTS_CONTEXT_SOURCE_BYTES:
                raise RightsContextSourceError("source changed to an unsupported file")
            chunks: list[bytes] = []
            remaining = MAX_RIGHTS_CONTEXT_SOURCE_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(remaining, 64 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
        finally:
            os.close(descriptor)
    except OSError as error:
        raise RightsContextSourceError(f"source is unavailable: {error}") from error
    if len(raw) > MAX_RIGHTS_CONTEXT_SOURCE_BYTES:
        raise RightsContextSourceError("source file exceeds the bounded context input size")
    return raw, "sha256:" + hashlib.sha256(raw).hexdigest(), reference


def _parse_bound_json(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RightsContextSourceError("source file is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise RightsContextSourceError("source file must contain a JSON object")
    return value


def _minimal_context(
    binding: dict[str, Any], diagnostics: list[dict[str, str]],
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "schema_version": RIGHTS_CONTEXT_SCHEMA,
        "context_budget_bytes": binding.get("context_budget_bytes", MAX_RIGHTS_CONTEXT_BYTES),
        "status": "review",
        "sources": {
            "rights_manifest": {"reference": binding.get("manifest"), "sha256": None},
            "governance_profile": {"reference": binding.get("governance_profile"), "sha256": None},
        },
        "rights": {
            "distribution_license": None,
            "declaration_state": "unknown",
            "copyright_status": "unresolved",
            "rights_basis": "unknown-needs-review",
            "third_party_material": "unknown",
            "sources": [],
            "license_terms": {"status": "unresolved", "compatibility": {"status": "unknown"}},
            "compatibility": {"status": "unknown"},
        },
        "provenance": {"origin_classification": "origin-uncertain", "evidence_state": "unavailable"},
        "governance": {"status": "unavailable", "profile_id": None, "rules": None},
        "diagnostics": diagnostics[:12],
    }
    encoded = json.dumps(context, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    context["identity"] = "sha256:" + hashlib.sha256(encoded).hexdigest()
    return context


def _normalize_context_binding(binding: Any) -> tuple[dict[str, Any], list[dict[str, str]]]:
    if not isinstance(binding, dict):
        binding = {}
    allowed_fields = {
        "schema_version", "manifest", "governance_profile", "profile_id",
        "authority", "external_metadata_authoritative", "context_budget_bytes",
    }
    errors: list[str] = []
    if binding.get("schema_version") != RIGHTS_CONTEXT_BINDING_SCHEMA:
        errors.append("rights-context binding schema version is missing or unsupported")
    if set(binding) != allowed_fields:
        errors.append("rights-context binding fields do not match the supported contract")
    manifest_ref = _bounded_text(binding.get("manifest"), 512)
    governance_ref = _bounded_text(binding.get("governance_profile"), 512)
    profile_id = binding.get("profile_id")
    if not manifest_ref or not governance_ref:
        errors.append("rights-context binding must name a manifest and governance profile")
    if not isinstance(profile_id, str) or not profile_id or len(profile_id) > 160:
        profile_id = None
        errors.append("rights-context binding profile_id is missing or too long")
    authority = binding.get("authority")
    scope = authority.get("scope") if isinstance(authority, dict) else None
    authority_valid = (
        isinstance(authority, dict)
        and set(authority) == {"identity", "basis", "source_ref", "scope"}
        and isinstance(authority.get("identity"), str)
        and 0 < len(authority["identity"]) <= 120
        and isinstance(authority.get("basis"), str)
        and 0 < len(authority["basis"]) <= 120
        and isinstance(authority.get("source_ref"), str)
        and 0 < len(authority["source_ref"]) <= 256
        and isinstance(scope, list)
        and 0 < len(scope) <= 16
        and all(isinstance(item, str) and 0 < len(item) <= 120 for item in scope)
    )
    if not authority_valid:
        errors.append("rights-context binding authority identity, basis, source, or scope is invalid")
    budget = binding.get("context_budget_bytes")
    budget_valid = (
        isinstance(budget, int) and not isinstance(budget, bool)
        and 4096 <= budget <= MAX_RIGHTS_CONTEXT_BYTES
    )
    if not budget_valid:
        errors.append("rights-context context_budget_bytes must be between 4096 and 8192")
        budget = MAX_RIGHTS_CONTEXT_BYTES
    external_metadata_authoritative = binding.get("external_metadata_authoritative")
    if external_metadata_authoritative is not False:
        errors.append("external metadata cannot be an Environment authority source")
    normalized = {
        "schema_version": binding.get("schema_version"),
        "manifest": manifest_ref,
        "governance_profile": governance_ref,
        "profile_id": profile_id,
        "authority": ({
            "identity": authority["identity"],
            "basis": authority["basis"],
            "source_ref": authority["source_ref"],
            "scope": list(scope),
        } if authority_valid else None),
        "external_metadata_authoritative": external_metadata_authoritative is True,
        "context_budget_bytes": budget,
        "authorized": not errors,
    }
    diagnostics = [{
        "code": "rights-context-binding-invalid",
        "severity": "review",
        "summary": reason,
        "evidence_ref": ".mncs/environment.json#/rights_context",
        "remedy": "repair-environment-rights-context-binding",
    } for reason in errors[:4]]
    return normalized, diagnostics


def load_agent_context(
    binding: dict[str, Any] | None,
    workspace_root: str | Path,
    *,
    previous_context: dict[str, Any] | None = None,
    previous_inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve an explicitly bound Rights manifest and governance profile.

    The Environment definition owns policy authority. Rights files are bounded
    JSON evidence and profile content; they cannot self-assign authority.
    """
    if binding is None:
        return {"context": None, "inputs": {"status": "unconfigured"}}
    normalized, binding_diagnostics = _normalize_context_binding(binding)
    inputs: dict[str, Any] = {
        "status": "review",
        "provider_contract": RIGHTS_CONTEXT_SCHEMA,
        "binding_contract": RIGHTS_CONTEXT_BINDING_SCHEMA,
        "binding": normalized,
    }
    diagnostics: list[dict[str, str]] = []
    manifest: dict[str, Any] = {}
    governance: dict[str, Any] | None = None
    manifest_digest = None
    governance_digest = None
    manifest_raw: bytes | None = None
    governance_raw: bytes | None = None
    root: Path | None = None
    try:
        root = Path(workspace_root).expanduser().resolve(strict=True)
        manifest_raw, manifest_digest, _ = _read_bound_bytes(root, normalized.get("manifest"))
        inputs["manifest"] = {"reference": normalized.get("manifest"), "sha256": manifest_digest}
    except (OSError, RightsContextSourceError) as error:
        diagnostics.append({
            "code": "rights-manifest-unavailable",
            "severity": "review",
            "summary": str(error)[:256],
            "evidence_ref": str(normalized.get("manifest", "RIGHTS.json"))[:256],
            "remedy": "restore-or-repair-bound-manifest",
        })
        inputs["manifest"] = {"reference": normalized.get("manifest"), "status": "unavailable"}
    try:
        root = root or Path(workspace_root).expanduser().resolve(strict=True)
        governance_raw, governance_digest, _ = _read_bound_bytes(root, normalized.get("governance_profile"))
        inputs["governance"] = {"reference": normalized.get("governance_profile"), "sha256": governance_digest}
    except (OSError, RightsContextSourceError) as error:
        diagnostics.append({
            "code": "agent-governance-unavailable",
            "severity": "review",
            "summary": str(error)[:256],
            "evidence_ref": str(normalized.get("governance_profile", "governance/agent-governance.json"))[:256],
            "remedy": "restore-or-repair-bound-governance-profile",
        })
        inputs["governance"] = {"reference": normalized.get("governance_profile"), "status": "unavailable"}

    previous_manifest = (previous_inputs or {}).get("manifest", {})
    previous_governance = (previous_inputs or {}).get("governance", {})
    if (
        previous_context is not None
        and (previous_inputs or {}).get("binding") == normalized
        and manifest_digest is not None
        and governance_digest is not None
        and previous_manifest.get("sha256") == manifest_digest
        and previous_governance.get("sha256") == governance_digest
    ):
        reused_inputs = dict(previous_inputs or {})
        reused_inputs["resolution_reused"] = True
        return {"context": previous_context, "inputs": reused_inputs}

    for raw, label in ((manifest_raw, "manifest"), (governance_raw, "governance")):
        if raw is None:
            continue
        try:
            value = _parse_bound_json(raw)
        except RightsContextSourceError as error:
            diagnostics.append({
                "code": "rights-manifest-invalid" if label == "manifest" else "agent-governance-invalid",
                "severity": "review",
                "summary": str(error),
                "evidence_ref": str(normalized.get("manifest" if label == "manifest" else "governance_profile", ""))[:256],
                "remedy": "repair-bound-json-source",
            })
            continue
        if label == "manifest":
            manifest = value
        else:
            governance = value

    try:
        from mncs_rights_provenance.agent_context import build_agent_context
    except ImportError as error:
        diagnostics.append({
            "code": "rights-provider-unavailable",
            "severity": "review",
            "summary": "MNCS Rights & Provenance context provider is unavailable: " + str(error)[:180],
            "evidence_ref": "mncs-rights-provenance",
            "remedy": "restore-rights-provider",
        })
        context = _minimal_context(normalized, diagnostics)
    else:
        context = build_agent_context(
            manifest,
            governance,
            binding={
                "authorized": normalized["authorized"],
                "profile_id": normalized["profile_id"],
                "authority": ({
                    "issuer": normalized["authority"]["identity"],
                    "basis": normalized["authority"]["basis"],
                    "source_ref": normalized["authority"]["source_ref"],
                    "scope": normalized["authority"]["scope"],
                } if normalized["authority"] else None),
                "context_budget_bytes": normalized["context_budget_bytes"],
            },
            manifest_ref=normalized.get("manifest") or "RIGHTS.json",
            governance_ref=normalized.get("governance_profile") or "governance/agent-governance.json",
            manifest_digest=manifest_digest,
            governance_digest=governance_digest,
        )
        if binding_diagnostics or diagnostics:
            context["diagnostics"] = (
                context.get("diagnostics", []) + binding_diagnostics + diagnostics
            )[:12]
            context["status"] = "review"
            context.pop("identity", None)
            encoded = json.dumps(context, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            context["identity"] = "sha256:" + hashlib.sha256(encoded).hexdigest()
    inputs["status"] = (
        "available" if manifest_digest and governance_digest and normalized["authorized"]
        and not binding_diagnostics and not diagnostics
        else "review"
    )
    inputs["context_identity"] = context.get("identity")
    return {"context": context, "inputs": inputs}


def load_claim_records(
    definition: dict[str, Any], workspace_root: str | Path,
) -> list[dict[str, Any]]:
    records = list(definition.get("rights_claims", []) or [])
    claims_file = definition.get("rights_claims_file")
    if claims_file:
        path = Path(claims_file)
        if not path.is_absolute():
            path = Path(workspace_root) / path
        records.extend(json.loads(path.read_text()))
    return records


def evaluate_rights(
    *,
    workspace_repos: list[str],
    records: list[dict[str, Any]],
    now: int | None = None,
) -> dict[str, Any]:
    moment = int(now if now is not None else time.time())
    try:
        from mncs_rights_provenance import host_gate
    except ImportError:
        return {
            "available": False,
            "overall": "review",
            "subjects": {name: "review" for name in workspace_repos},
            "detail": {name: ["rights library unavailable: unknown"] for name in workspace_repos},
            "evaluated_at": moment,
            "claim_count": len(records),
        }
    verdict = host_gate.evaluate_subjects(records, now=moment, subjects=workspace_repos)
    verdict["available"] = True
    verdict["evaluated_at"] = moment
    verdict["claim_count"] = len(records)
    return verdict


def enforced_repos(rights: dict[str, Any], workspace_repos: list[str]) -> list[str]:
    """Workspace repos whose rights verdict is ``blocked``."""
    subjects = rights.get("subjects", {})
    return [name for name in workspace_repos if subjects.get(name) == "blocked"]


def check_enter(
    workspace_repos: list[str], rights: dict[str, Any],
) -> None:
    blocked = enforced_repos(rights, workspace_repos)
    if blocked:
        raise RightsBlocked(
            "rights-blocked repositories: " + ", ".join(sorted(blocked))
        )
