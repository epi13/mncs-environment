"""Ambient projection coherence: derived information converges while agents work.

This module composes provider-owned projection semantics; it owns no
domain rendering, no document knowledge, and no media knowledge. On
every environment entry it observes declared projections, asks the
native automation planner whether each may regenerate, renders
through provider capabilities, applies only whole-file outputs (and
explicitly authorized region splices) under narrow path claims, and
stays quiet when the world is current.

Policy lives in `mncs.automation.projection.v1` (proceed / defer /
escalate). This file transports facts to that policy and carries out
admitted effects: filesystem reads, provider invocation, atomic file
replacement, claim acquisition, and evidence recording. Repository
content is never touched unless the native gate proceeds AND a live
claim covers the exact output path AND post-write validation passes.
Nothing here commits, stages, switches branches, or pushes.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import capabilities as capabilities_module
from . import claims as claims_module
from . import projection_store as store_module
from . import projection_verification as verification_module
from . import workspace as workspace_module
from .identity import digest_hex
from .persist import read_json, write_json

SCHEMA = "mncs.environment.projections/1"
PLAN_SCHEMA = "mncs.reconcile-plan/1"
EVIDENCE_SCHEMA = "mncs.session-evidence/1"
MAX_HISTORY = 10
MAX_DECLARATIONS = 256
MAX_INPUT_FILES = 512
MAX_INPUT_BYTES = 8 * 1024 * 1024
MAX_RENDER_CACHE = 20
PLANNER_CAPABILITY = "mncs-automation:automation-reconciliation"

# Status codes mirror store.projection: current/stale/unknown/blocked/failed.
STATUS_CURRENT = 0
STATUS_STALE = 1
STATUS_UNKNOWN = 2
STATUS_BLOCKED = 3
STATUS_FAILED = 4

# Fact codes shared with mncs.automation.projection (see its MODEL.md).
REPO_CLEAN = 0
REPO_DIRTY_GENERATED_ONLY = 1
REPO_DIRTY_OTHER = 2
REPO_UNKNOWN = 3
BRANCH_MAINLINE = 0
BRANCH_FOREIGN = 1
BRANCH_UNKNOWN = 2
CLAIM_NONE = 0
CLAIM_SELF = 1
CLAIM_FOREIGN = 2
CLAIM_ADOPTED = 3
TARGET_WHOLE_FILE = 0
TARGET_REGION_IN_FILE = 1
TARGET_HUMAN_ONLY = 2
TARGET_UNKNOWN = 3
REGION_MISSING = 0
REGION_INVALID = 1
REGION_VALID = 2
REGION_NOT_APPLICABLE = 3
OUTPUT_MISSING = 0
OUTPUT_MATCHES_FRESH = 1
OUTPUT_MATCHES_LAST_RENDER = 2
OUTPUT_DIVERGED = 3
OUTPUT_UNKNOWN = 4
GATE_PROCEED = 0
GATE_DEFER = 1
GATE_ESCALATE = 2
VERDICT_FAIL = 0
VERDICT_PASS = 1
VERDICT_UNKNOWN = 2


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def bytes_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _artifact_directory(session, *parts: str) -> Path:
    state_root = session.store.state_dir.resolve()
    path = (state_root / "sessions" / session.session_id
            / "projection-artifacts" / Path(*parts)).resolve()
    if not path.is_relative_to(state_root):
        raise ValueError("projection artifact path escapes the state root")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _workspace_root(session) -> Path | None:
    root = session.snapshot.get("workspace", {}).get("root")
    if not root:
        return None
    path = Path(root)
    return path if path.is_dir() else None


def _collect_from_checkout(repository: str, child: Path,
                         declarations: list[dict],
                         invalid: list[dict]) -> bool:
    """Collect one checkout's declarations; False when capped."""
    manifest_path = child / ".mncs" / "project.json"
    if not manifest_path.is_file():
        return True
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        invalid.append({"repository": repository,
                        "reason": "manifest-unreadable"})
        return True
    entries = manifest.get("projections") or []
    if not isinstance(entries, list):
        invalid.append({"repository": repository,
                        "reason": "projections-not-a-list"})
        return True
    for entry in entries:
        problem = _validate_declaration(entry)
        record = {"repository": repository, "checkout": str(child)}
        if problem is not None:
            record.update({"reason": problem,
                           "declaration": _summarize(entry)})
            invalid.append(record)
            continue
        record.update(entry)
        declarations.append(record)
        if len(declarations) >= MAX_DECLARATIONS:
            invalid.append({"repository": repository,
                            "reason": "declaration-cap-reached"})
            return False
    return True


def discover_declarations(workspace_root: Path) -> tuple[list[dict], list[dict]]:
    """Collect projection declarations from workspace manifests.

    Only direct-child repositories carrying `.mncs/project.json` with a
    non-empty `projections` section are considered; anything else costs
    one small JSON read. Invalid declarations are reported, never run.
    """
    declarations: list[dict] = []
    invalid: list[dict] = []
    try:
        children = sorted(path for path in workspace_root.iterdir()
                          if path.is_dir() and not path.name.startswith("."))
    except OSError:
        return [], [{"repository": "", "reason": "workspace-unreadable"}]
    for child in children:
        if not _collect_from_checkout(child.name, child, declarations,
                                      invalid):
            return declarations, invalid
    return declarations, invalid


def discover_selected_declarations(
        session, workspace_root: Path) -> tuple[list[dict], list[dict]]:
    """Collect declarations from explicitly selected checkouts only.

    The ambient pass must not wander the workspace looking for
    manifests: it inspects exactly the checkouts the session selected,
    so unrelated directories are never opened. Sessions without any
    selection fall back to the bounded direct-child scan.
    """
    selected = session.snapshot.get("selected_checkouts") or {}
    if not isinstance(selected, dict) or not selected:
        return discover_declarations(workspace_root)
    declarations: list[dict] = []
    invalid: list[dict] = []
    try:
        root = workspace_root.resolve()
    except OSError:
        return [], [{"repository": "", "reason": "workspace-unreadable"}]
    for name in sorted(selected):
        record = selected[name]
        path = (record or {}).get("path") if isinstance(record, dict) else None
        if not isinstance(path, str) or not path:
            invalid.append({"repository": str(name),
                            "reason": "selection-without-path"})
            continue
        child = Path(path)
        if not child.is_absolute():
            child = workspace_root / child
        if child.is_symlink():
            invalid.append({"repository": str(name),
                            "reason": "selection-is-symlink"})
            continue
        try:
            resolved = child.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError):
            invalid.append({"repository": str(name),
                            "reason": "selection-escapes-workspace"})
            continue
        if not resolved.is_dir():
            invalid.append({"repository": str(name),
                            "reason": "selection-unavailable"})
            continue
        if not _collect_from_checkout(str(name), resolved, declarations,
                                      invalid):
            return declarations, invalid
    return declarations, invalid


def _validate_declaration(entry: Any) -> str | None:
    if not isinstance(entry, dict):
        return "declaration-not-an-object"
    for key in ("id", "template", "inputs", "output",
                "provider_capability", "render_argv", "policy"):
        if key not in entry:
            return f"missing-{key}"
    if entry.get("output_kind", "whole-file") not in ("whole-file", "region"):
        return "bad-output-kind"
    if entry.get("policy") not in ("ambient-safe", "explicit-only"):
        return "bad-policy"
    verification = entry.get("verification")
    if verification is not None and not isinstance(verification, dict):
        return "bad-verification"
    if entry.get("output_kind") == "region":
        admit = entry.get("admit")
        if not isinstance(admit, dict):
            return "region-missing-admit"
        try:
            if int(admit.get("sources", 0)) <= 0:
                return "region-missing-sources"
        except (TypeError, ValueError):
            return "region-missing-sources"
    if (not isinstance(entry["inputs"], list)
            or not isinstance(entry["render_argv"], list)):
        return "bad-shapes"
    for rel in entry["inputs"] + [entry["output"]]:
        path = Path(str(rel))
        if path.is_absolute() or ".." in path.parts:
            return "path-escapes-checkout"
    return None


def _summarize(entry: Any) -> Any:
    if isinstance(entry, dict):
        return {"id": entry.get("id"), "template": entry.get("template")}
    return {"id": None}


def input_digest(checkout: Path, inputs: list[str]) -> str | None:
    """Digest the authoritative projection inputs, deterministically."""
    digest = hashlib.sha256()
    files = 0
    total = 0
    for rel in sorted(inputs):
        root = checkout / rel
        if not root.exists():
            return None
        members = sorted(root.rglob("*")) if root.is_dir() else [root]
        for member in members:
            if not member.is_file() or member.is_symlink():
                continue
            try:
                content = member.read_bytes()
            except OSError:
                return None
            files += 1
            total += len(content)
            if files > MAX_INPUT_FILES or total > MAX_INPUT_BYTES:
                return None
            digest.update(str(member.relative_to(checkout)).encode())
            digest.update(b"\0")
            digest.update(content)
    return "sha256:" + digest.hexdigest()


def _dirty_digest(state) -> str:
    files = sorted(getattr(state, "dirty_files", None) or [])
    return digest_hex({"dirty": bool(getattr(state, "dirty", False)),
                       "files": files})


def _porcelain_path(line: Any) -> str | None:
    """Extract the checkout-relative path from a porcelain line."""
    if not isinstance(line, str) or len(line) < 4:
        return None
    path = line[3:]
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    path = path.strip()
    if len(path) >= 2 and path.startswith('"') and path.endswith('"'):
        path = path[1:-1]
    return path or None


def repo_facts(session, repository: str, checkout: Path,
               machine_outputs: list[str]) -> dict[str, Any]:
    """Observe repository facts and encode them for the native gate."""
    try:
        state = workspace_module.inspect_repo(checkout)
    except (OSError, ValueError):
        return {"repo": REPO_UNKNOWN, "branch": BRANCH_UNKNOWN,
                "head": None, "dirty_files": None}
    branch = getattr(state, "branch", None)
    if branch in ("main", "master"):
        branch_code = BRANCH_MAINLINE
    elif branch:
        branch_code = BRANCH_FOREIGN
    else:
        branch_code = BRANCH_UNKNOWN
    dirty_files = [_porcelain_path(line) for line in
                   (getattr(state, "dirty_files", None) or [])]
    if not getattr(state, "dirty", False):
        repo_code = REPO_CLEAN
    elif dirty_files and all(path is not None and path in machine_outputs
                             for path in dirty_files):
        repo_code = REPO_DIRTY_GENERATED_ONLY
    else:
        repo_code = REPO_DIRTY_OTHER
    return {"repo": repo_code, "branch": branch_code,
            "branch_name": branch,
            "head": getattr(state, "head", None),
            "dirty_files": sorted(path for path in dirty_files
                                  if path is not None),
            "dirty_digest": _dirty_digest(state)}


def claim_facts(session, repository: str, output: str) -> dict[str, Any]:
    """Classify live claims over one projection output path."""
    try:
        records = session.store.read_claims()
    except (OSError, ValueError):
        return {"claim": CLAIM_FOREIGN, "detail": "claims-unreadable"}
    holders = claims_module.holders(records).get(repository, [])
    if not holders:
        return {"claim": CLAIM_NONE, "detail": "unclaimed"}
    requested = claims_module.normalize_scope(
        {"kind": "paths", "paths": [output]}, repository)
    foreign = [holder for holder in holders
               if holder.get("session_id") != session.session_id
               and claims_module.scopes_conflict(
                   holder.get("scope") or {"kind": "repository"},
                   requested)]
    if foreign:
        sessions = sorted({str(holder.get("session_id", "?"))
                           for holder in foreign})
        return {"claim": CLAIM_FOREIGN,
                "detail": f"foreign-claim:{','.join(sessions)}"}
    own = [holder for holder in holders
           if holder.get("session_id") == session.session_id]
    if any((holder.get("basis") in claims_module.ADOPTION_BASES)
           for holder in own):
        return {"claim": CLAIM_ADOPTED, "detail": "own-adopted"}
    if own:
        return {"claim": CLAIM_SELF, "detail": "own-claim"}
    return {"claim": CLAIM_NONE, "detail": "unclaimed"}


def _session_binding(session, capability: str) -> dict[str, Any] | None:
    """Return an optional binding, treating absence as unbound."""
    from .sessions import AuthorityDenied

    try:
        binding = session._binding(capability)
    except (AuthorityDenied, KeyError, ValueError, AttributeError):
        return None
    return binding if isinstance(binding, dict) else None


def planner_available(session) -> bool:
    binding = _session_binding(session, PLANNER_CAPABILITY)
    if binding is None:
        return False
    return binding.get("availability", {}).get("status") == "available"


def request_plan(session, request: dict[str, Any]) -> dict[str, Any] | None:
    """Invoke the native planner through its bound capability."""
    if not planner_available(session):
        return None
    directory = _artifact_directory(session, "plans")
    request_path = directory / "request.json"
    request_path.write_text(json.dumps(request, sort_keys=True),
                            encoding="utf-8")
    try:
        result = session.invoke(PLANNER_CAPABILITY,
                                ["--request", str(request_path)],
                                timeout_seconds=120)
    except Exception:
        return None
    if not isinstance(result, dict) or result.get("status") != "ok":
        return None
    try:
        envelope = json.loads(result.get("stdout", ""))
    except ValueError:
        return None
    if not isinstance(envelope, dict):
        return None
    if envelope.get("schema_version") != PLAN_SCHEMA:
        return None
    for key in ("gate", "gate_reason", "action", "reason",
                "new_canonical", "new_observed", "execute"):
        if key not in envelope:
            return None
    return envelope


def render_projection(session, declaration: dict[str, Any], checkout: Path,
                      tag: str) -> tuple[bytes | None, str]:
    """Render projection bytes to the session artifact directory."""
    capability = str(declaration["provider_capability"])
    binding = _session_binding(session, capability)
    if binding is None:
        return None, "provider-unbound"
    if binding.get("availability", {}).get("status") != "available":
        return None, "provider-unavailable"
    directory = _artifact_directory(session, "renders")
    rendered_path = directory / f"{digest_hex(declaration['id'])}.{tag}.bin"
    argv = [str(item).replace("{checkout}", str(checkout)).replace(
        "{artifact}", str(directory)) for item in declaration["render_argv"]]
    # The declaration names {artifact}/rendered.md; redirect per-tag so
    # double-render validation never aliases the same path.
    argv = [item.replace("rendered.md", rendered_path.name) for item in argv]
    try:
        result = session.invoke(capability, argv, timeout_seconds=120)
    except Exception as error:
        return None, f"invoke-failed:{type(error).__name__}"
    if not isinstance(result, dict) or result.get("status") != "ok":
        return None, f"invoke-{result.get('status', 'unknown')}"
    try:
        return rendered_path.read_bytes(), "ok"
    except OSError:
        return None, "render-missing"


def render_cached(session, declaration: dict[str, Any], checkout: Path,
                  digest: str) -> tuple[bytes | None, str, bool]:
    """Render with an input-digest cache; validate determinism on miss."""
    cache_dir = _artifact_directory(session, "render-cache")
    # Same inputs under a different template/argv/provider can render
    # different bytes, so the declaration joins the cache key.
    decl_key = digest_hex(
        {key: declaration.get(key) for key in
         ("template", "provider_capability", "render_argv")})
    cached = cache_dir / (digest_hex(str(declaration["id"]))[:16] + "."
                            + decl_key + "."
                            + digest.replace(":", "_") + ".bin")
    if cached.is_file():
        try:
            return cached.read_bytes(), "ok", True
        except OSError:
            pass
    first, reason = render_projection(session, declaration, checkout, "a")
    if first is None:
        return None, reason, False
    second, reason = render_projection(session, declaration, checkout, "b")
    if second is None:
        return None, reason, False
    if first != second:
        return None, "render-nondeterministic", False
    try:
        cached.write_bytes(first)
        _prune_cache(cache_dir)
    except OSError:
        pass
    return first, "ok", False


def _prune_cache(cache_dir: Path) -> None:
    members = sorted(cache_dir.glob("*.bin"),
                     key=lambda path: path.stat().st_mtime)
    for stale in members[:-MAX_RENDER_CACHE]:
        try:
            stale.unlink()
        except OSError:
            pass


def classify_output(checkout: Path, output: str, fresh: bytes,
                    row: dict[str, Any] | None) -> tuple[int, str]:
    """Compare on-disk output against the fresh render and baseline."""
    target = checkout / output
    try:
        current = target.read_bytes() if target.is_file() else None
    except OSError:
        return OUTPUT_UNKNOWN, "output-unreadable"
    if current is None:
        return OUTPUT_MISSING, "output-missing"
    if current == fresh:
        return OUTPUT_MATCHES_FRESH, "matches-fresh"
    if row is None or not (row or {}).get("rendered_digest"):
        # First touch of a declared machine-owned output: the
        # declaration authorizes adoption; afterwards the recorded
        # baseline protects against hand-edits. Deferred passes leave
        # rows without a rendered baseline, so absence of a baseline
        # is first touch however the row came to exist.
        return OUTPUT_MISSING, "first-touch-adoption"
    if row.get("rendered_digest") and bytes_digest(current) == row.get(
            "rendered_digest"):
        return OUTPUT_MATCHES_LAST_RENDER, "matches-last-render"
    return OUTPUT_DIVERGED, "output-diverged"


def admit_region_bytes(session, declaration: dict[str, Any],
                       checkout: Path,
                       fresh: bytes) -> tuple[bytes | None, dict | None]:
    """Ask the document provider for admitted region bytes.

    The provider classifies markers and admits natively, then writes
    the full expected file to a session artifact path. Refused
    admissions return no bytes; the caller defers or escalates from
    the admission status. Read-only against repositories.
    """
    admit = declaration.get("admit") or {}
    capability = str(declaration["provider_capability"])
    binding = _session_binding(session, capability)
    if binding is None:
        return None, None
    if binding.get("availability", {}).get("status") != "available":
        return None, None
    directory = _artifact_directory(session, "admits")
    tag = digest_hex(str(declaration["id"]))[:16]
    body_path = directory / f"{tag}.body.bin"
    expect_path = directory / f"{tag}.expected.bin"
    try:
        body_path.write_bytes(fresh)
    except OSError:
        return None, None
    argv = ["project-admit", "--document",
           str(checkout / str(declaration["output"])),
           "--sources", str(int(admit.get("sources", 1))),
           "--template-present",
           "1" if admit.get("template_present", True) else "0",
           "--generated", str(body_path), "--expect-out", str(expect_path)]
    if admit.get("create_allowed", False):
        argv.append("--create")
    try:
        result = session.invoke(capability, argv, timeout_seconds=120)
    except Exception:
        return None, None
    if not isinstance(result, dict) or result.get("status") != "ok":
        return None, None
    try:
        admission = json.loads(result.get("stdout", ""))
    except ValueError:
        return None, None
    if not isinstance(admission, dict) or not admission.get("admit"):
        return None, admission if isinstance(admission, dict) else None
    try:
        return expect_path.read_bytes(), admission
    except OSError:
        return None, admission


def _revalidate_for_commit(session, declaration: dict[str, Any],
                           checkout: Path, digest: str, verdict: int,
                           evidence_id: str | None,
                           preimage: bytes | None) -> tuple[bool, str]:
    """Confirm plan-time facts still hold before bytes or state commit.

    Closes the render/apply TOCTOU: inputs, verification, and the
    classified output preimage are re-observed, and any drift aborts
    the commit so B-derived bytes are never recorded as digest A.
    """
    current = input_digest(
        checkout, [str(item) for item in declaration["inputs"]])
    if current != digest:
        return False, "inputs-moved-before-commit"
    output = str(declaration["output"])
    try:
        on_disk = (checkout / output).read_bytes()
    except OSError:
        on_disk = None
    if on_disk != preimage:
        return False, "output-moved-before-commit"
    reverdict, reevidence, _detail, _seen = (
        verification_module.resolve_declared(
            session, declaration, checkout, digest, allow_run=False))
    if reverdict != verdict or reevidence != evidence_id:
        return False, "verification-moved-before-commit"
    return True, "revalidated"


def apply_expected_bytes(session, declaration: dict[str, Any],
                         checkout: Path,
                         expected: bytes,
                         *, digest: str | None = None,
                         verdict: int | None = None,
                         evidence_id: str | None = None,
                         preimage: bytes | None = None) -> tuple[bool, str]:
    """Write admitted bytes under a narrow path claim. Never commits."""
    repository = str(declaration["repository"])
    output = str(declaration["output"])
    target = checkout / output
    try:
        resolved = target.resolve()
        if not resolved.is_relative_to(checkout.resolve()):
            return False, "output-escapes-checkout"
    except OSError:
        return False, "output-unresolvable"
    try:
        facts = workspace_module.inspect_repo(checkout)
        checkout_facts = {"head": getattr(facts, "head", None),
                          "dirty": bool(getattr(facts, "dirty", False)),
                          "branch": getattr(facts, "branch", None)}
    except (OSError, ValueError):
        return False, "repo-unreadable"
    # Dirty-but-characterized checkouts (dirt confined to declared
    # machine outputs, verified before the native gate proceeded) are
    # adopted explicitly: the provenance records exactly what was
    # known. Unknown dirt never reaches this path.
    basis = (claims_module.BASIS_ADOPTION
             if checkout_facts.get("dirty")
             else claims_module.BASIS_EXPLICIT)
    try:
        claim = claims_module.acquire(
            session.store, repository=repository,
            session_id=session.session_id,
            consumer_id=session.snapshot.get("consumer_id", "environment"),
            basis=basis,
            reason=f"projection-apply:{declaration['id']}",
            ttl_hours=1, scope={"kind": "paths", "paths": [output]},
            checkout_facts=checkout_facts,
            workspace_root=str(checkout.parent))
    except (claims_module.ClaimConflict, claims_module.ClaimAdoptionRequired,
            ValueError) as error:
        return False, f"claim-refused:{error}"
    claim_id = str(claim.get("claim_id", ""))
    _refresh_claim_holders(session)
    try:
        authority = session.check(
            action="write", target=f"{repository}/{output}",
            scope={"kind": "paths", "paths": [output],
                   "checkout": str(checkout)})
        if authority.get("verdict") != "allow":
            return False, f"authority-{authority.get('verdict')}"
        try:
            rechecked = workspace_module.inspect_repo(checkout)
        except (OSError, ValueError):
            return False, "repo-unreadable-under-claim"
        if (getattr(rechecked, "head", None) != checkout_facts["head"]
                or getattr(rechecked, "branch", None)
                != checkout_facts["branch"]):
            return False, "repo-moved-under-claim"
        if digest is not None and verdict is not None:
            # Two-phase commit: the claim is held, so re-observe the
            # authoritative facts the plan used before touching bytes.
            fresh, reason = _revalidate_for_commit(
                session, declaration, checkout, digest, verdict,
                evidence_id, preimage)
            if not fresh:
                return False, reason
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("wb", dir=target.parent,
                                         prefix=f".{target.name}.",
                                         delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(expected)
        os.replace(temporary, target)
        try:
            written = target.read_bytes()
        except OSError:
            return False, "post-write-unreadable"
        if written != expected:
            return False, "post-write-mismatch"
        return True, "applied"
    finally:
        try:
            if claim_id:
                claims_module.release(session.store,
                                      session_id=session.session_id,
                                      claim_id=claim_id)
        except (OSError, ValueError):
            pass
        _refresh_claim_holders(session)


def _state_rows(session) -> dict[str, dict[str, Any]]:
    rows = session.snapshot.get("projection_state")
    return dict(rows) if isinstance(rows, dict) else {}


def _read_shared_row(session, projection_id: str) -> dict[str, Any] | None:
    """Latest shared row, or None when the shared backend is down.

    None is a fail-closed signal: the ambient path defers without
    adopting. A blank version-0 row means the projection simply has
    no recorded state yet.
    """
    if session.store is None:
        return None
    try:
        return store_module.read_row(session.store, projection_id)
    except store_module.SharedStoreUnavailable:
        return None


def _persist_shared_row(session, projection_id: str,
                        fields: dict[str, Any],
                        expected_version: int) -> dict[str, Any] | None:
    """CAS-publish shared fields; None on conflict or outage.

    Conflicts mean another session advanced the row first: the caller
    must re-plan from the latest row instead of overwriting. Like all
    absence here, None never becomes permission.
    """
    if session.store is None:
        return None
    row = dict(fields)
    row["projection"] = projection_id
    row["updated_by"] = session.session_id
    row["updated_at"] = utcnow()
    try:
        return store_module.write_row(
            session.store, row, expected_version=expected_version)
    except (store_module.ProjectionConflict,
            store_module.SharedStoreUnavailable):
        return None


def _lookup_verdict(session, declaration: dict[str, Any],
                    digest: str | None) -> dict[str, Any]:
    """Read-only verdict for epoch invalidation (never runs executors)."""
    checkout = Path(str(declaration["checkout"]))
    verdict, evidence_id, detail, _seen = (
        verification_module.resolve_declared(
            session, declaration, checkout, digest, allow_run=False))
    return {"verdict": verdict, "evidence_id": evidence_id,
            "detail": detail}


def _epoch_inputs(session, declarations: list[dict],
                  digests: dict[str, str | None]) -> dict[str, Any]:
    claims_digest: Any = None
    try:
        records = session.store.read_claims()
        live = claims_module.active_claims(records)
        claims_digest = digest_hex(
            {key: {"repository": value.get("repository"),
                   "session_id": value.get("session_id"),
                   "scope": value.get("scope"),
                   "basis": value.get("basis")}
             for key, value in sorted(live.items())})
    except (OSError, ValueError):
        claims_digest = "claims-unreadable"
    repos: dict[str, Any] = {}
    for declaration in declarations:
        checkout = Path(str(declaration["checkout"]))
        try:
            state = workspace_module.inspect_repo(checkout)
        except (OSError, ValueError):
            repos[declaration["id"]] = "repo-unreadable"
            continue
        repos[declaration["id"]] = {
            "branch": getattr(state, "branch", None),
            "head": getattr(state, "head", None),
            "dirty": _dirty_digest(state),
        }
    obligations: set[str] = set()
    for declaration in declarations:
        spec = declaration.get("verification")
        if isinstance(spec, dict):
            for item in spec.get("obligations") or []:
                if isinstance(item, str) and item:
                    obligations.add(item)
    bindings: dict[str, str] = {}
    for capability in [PLANNER_CAPABILITY] + sorted(
            {str(declaration["provider_capability"])
             for declaration in declarations} | obligations):
        binding = _session_binding(session, capability)
        if binding is None:
            bindings[capability] = "unbound"
        else:
            bindings[capability] = str(binding.get("availability", {})
                                       .get("status"))
    shared: dict[str, Any] = {}
    verdicts: dict[str, Any] = {}
    for declaration in declarations:
        projection_id = str(declaration["id"])
        row = _read_shared_row(session, projection_id)
        if row is None:
            shared[projection_id] = "shared-store-unavailable"
        else:
            # Version and defer counters are write/retry metadata,
            # not world state: a pass that only advances them must
            # still hit the quiet path next time.
            shared[projection_id] = {
                key: row.get(key) for key in
                ("canonical_gen", "observed_gen",
                 "canonical_digest", "rendered_digest", "verdict",
                 "evidence_id", "status")}
        verdicts[projection_id] = _lookup_verdict(
            session, declaration, digests.get(projection_id))
    return {
        "declarations": {declaration["id"]: digest_hex(
            {key: declaration[key] for key in
             ("template", "inputs", "output", "output_kind",
              "provider_capability", "render_argv", "policy")
             if key in declaration} |
            ({"verification": declaration["verification"]}
             if declaration.get("verification") is not None else {}))
            for declaration in declarations},
        "digests": digests,
        "repos": repos,
        "claims": claims_digest,
        "bindings": bindings,
        "shared": shared,
        "verification": verdicts,
        "lifecycle": session.snapshot.get("lifecycle", "active"),
    }


def ambient_pass(session, *, mode: str = "ambient",
                 only: str | None = None) -> dict[str, Any]:
    """Evaluate every declared projection; reconcile only safe ones."""
    started = utcnow()
    clock_started = time.monotonic()
    workspace_root = _workspace_root(session)
    rows = _state_rows(session)
    if workspace_root is None:
        return _finish(session, started, clock_started, [], rows, [], mode,
                       {"reason": "workspace-unavailable"}, "no-workspace")
    declarations, invalid = discover_selected_declarations(
        session, workspace_root)
    if only is not None:
        declarations = [declaration for declaration in declarations
                        if declaration["id"] == only]
        if not declarations:
            return _finish(session, started, clock_started, [], rows,
                           invalid, mode,
                           {"reason": f"unknown-projection:{only}"},
                           f"unknown:{only}")
    digests = {declaration["id"]: input_digest(
        Path(str(declaration["checkout"])),
        [str(item) for item in declaration["inputs"]])
        for declaration in declarations}
    epoch_inputs = _epoch_inputs(session, declarations, digests)
    epoch = digest_hex(epoch_inputs)
    stored = session.snapshot.get("projection_epoch") or {}
    if stored.get("epoch") == epoch and mode == "ambient":
        summary = dict(stored.get("summary") or {})
        summary["epoch_reused"] = True
        summary["elapsed_seconds"] = round(time.monotonic() - clock_started,
                                           3)
        return {"summary": summary, "reused": True,
                "evidence": stored.get("evidence_ref")}
    results: list[dict[str, Any]] = []
    for declaration in declarations:
        results.append(_reconcile_one(session, declaration, digests[
            declaration["id"]], rows, mode))
    # The stored epoch describes the post-pass world: shared rows are
    # both an input and an output of the pass, so caching the
    # pre-pass state would never hit after a pass that wrote.
    post_epoch = digest_hex(_epoch_inputs(session, declarations, digests))
    return _finish(session, started, clock_started, results, rows,
                   invalid, mode, None, post_epoch)


def _reconcile_one(session, declaration: dict[str, Any],
                   digest: str | None, rows: dict[str, dict],
                   mode: str) -> dict[str, Any]:
    projection_id = str(declaration["id"])
    repository = str(declaration["repository"])
    checkout = Path(str(declaration["checkout"]))
    record: dict[str, Any] = {"projection": projection_id,
                              "repository": repository,
                              "mode": mode}
    machine_outputs = _machine_outputs(session, repository, checkout)
    facts = repo_facts(session, repository, checkout, machine_outputs)
    claimed = claim_facts(session, repository, str(declaration["output"]))
    # Durable observed state is shared, not session-local: every agent
    # reads and CAS-writes the same row. The session cache only feeds
    # presentation and the epoch fast path.
    shared = _read_shared_row(session, projection_id)
    record["repo"] = facts
    record["claim"] = claimed
    if shared is None:
        record.update({"verdict": VERDICT_UNKNOWN, "gate": GATE_DEFER,
                       "gate_reason": "shared-store-unavailable",
                       "outcome": "deferred", "digest": digest})
        cached = rows.get(projection_id) or {}
        _retain_row(rows, projection_id,
                    int(cached.get("canonical_gen", 0)),
                    int(cached.get("observed_gen", 0)), digest, cached,
                    STATUS_UNKNOWN, int(cached.get("defer_count", 0)) + 1)
        return record
    base_version = int(shared.get("version", 0))
    observed = int(shared.get("observed_gen", 0))
    defer_count = int(shared.get("defer_count", 0))
    if digest is None:
        record.update({"verdict": VERDICT_UNKNOWN, "gate": GATE_DEFER,
                       "gate_reason": "inputs-unreadable",
                       "outcome": "deferred"})
        _retain_shared(session, rows, projection_id, shared,
                       int(shared.get("canonical_gen", 0)), observed,
                       None, STATUS_UNKNOWN, defer_count + 1,
                       VERDICT_UNKNOWN, None, None)
        return record
    canonical = int(shared.get("canonical_gen", 0))
    if digest != shared.get("canonical_digest"):
        canonical = max(canonical, observed) + 1
    if observed == canonical and digest == shared.get("canonical_digest"):
        # Fast path, guarded by the adopted bytes still being on disk:
        # a hand edit to the output (or broken markers) must fall
        # through to classification, never report current.
        try:
            on_disk = (checkout / str(declaration["output"])).read_bytes()
        except OSError:
            on_disk = None
        if (on_disk is not None
                and shared.get("rendered_digest") is not None
                and bytes_digest(on_disk) == shared.get("rendered_digest")):
            rows[projection_id] = _cache_row(shared)
            record.update(
                {"verdict": int(shared.get("verdict", VERDICT_UNKNOWN)),
                 "evidence_id": shared.get("evidence_id"),
                 "outcome": "current", "digest": digest})
            return record
    # Real generation-bound verification: PASS only when evidence
    # covers this exact subject. Lookup first; when stale and still
    # unknown, run bound executors once to produce it. Rendering
    # success is never consulted as a verdict signal.
    verdict, evidence_id, detail, seen = verification_module.resolve_declared(
        session, declaration, checkout, digest, allow_run=False)
    if verdict == VERDICT_UNKNOWN:
        verdict, evidence_id, detail, seen = (
            verification_module.resolve_declared(
                session, declaration, checkout, digest, allow_run=True))
    spec = declaration.get("verification")
    require_verified = 1
    if isinstance(spec, dict) and spec.get("required") is False:
        require_verified = 0
    source = {"repository": repository, "head": facts.get("head"),
              "branch": facts.get("branch_name"), "input_digest": digest}
    record.update({"verdict": verdict, "evidence_id": evidence_id,
                   "verification": detail,
                   "require_verified": require_verified,
                   "source": source, "digest": digest})
    fresh, reason, cached = render_cached(session, declaration, checkout,
                                          digest)
    if fresh is None:
        # Render health is not a verification verdict: the resolved
        # evidence verdict stands, and the row status carries the
        # render failure.
        record.update({"gate": GATE_DEFER, "gate_reason": f"render-{reason}",
                       "outcome": "deferred", "digest": digest})
        kept = _retain_shared(
            session, rows, projection_id, shared, canonical, observed,
            digest, (STATUS_FAILED if reason == "render-nondeterministic"
                     else STATUS_UNKNOWN), defer_count + 1, verdict,
            evidence_id, seen)
        if not kept:
            record.update({"gate_reason": "projection-state-conflict"})
        return record
    if declaration.get("output_kind", "whole-file") == "region":
        target_kind = TARGET_REGION_IN_FILE
        expected, admission = admit_region_bytes(
            session, declaration, checkout, fresh)
        if admission is None:
            record.update({"gate": GATE_DEFER,
                           "gate_reason": "admit-unavailable",
                           "outcome": "deferred", "digest": digest})
            kept = _retain_shared(
                session, rows, projection_id, shared, canonical,
                observed, digest, STATUS_UNKNOWN, defer_count + 1,
                verdict, evidence_id, seen)
            if not kept:
                record.update({"gate_reason": "projection-state-conflict"})
            return record
        region_code = int(admission.get("status", REGION_INVALID))
        record["admission"] = {key: admission.get(key) for key in
                               ("status", "status_name", "admit", "reason",
                                "reason_name")}
        if expected is None:
            # Refused admission carries no bytes; the native gate
            # escalates invalid markers and defers anything else.
            output_code, output_detail = (OUTPUT_UNKNOWN,
                                          "admission-refused")
        else:
            output_code, output_detail = classify_output(
                checkout, str(declaration["output"]), expected, shared)
    else:
        target_kind = TARGET_WHOLE_FILE
        region_code = REGION_NOT_APPLICABLE
        expected = fresh
        output_code, output_detail = classify_output(
            checkout, str(declaration["output"]), expected, shared)
    try:
        preimage = (checkout / str(declaration["output"])).read_bytes()
    except OSError:
        preimage = None
    splice_ok = 1 if (mode == "explicit"
                      or declaration.get("policy") == "ambient-safe") else 0
    request = {
        "projection": projection_id,
        "canonical_gen": canonical, "observed_gen": observed,
        "inputs_changed": 1 if digest != shared.get(
            "canonical_digest") else 0,
        "verdict": verdict, "require_verified": require_verified,
        "repo": facts["repo"], "branch": facts["branch"],
        "claim": claimed["claim"], "target": target_kind,
        "region": region_code, "output": output_code,
        "splice_ok": splice_ok, "defer_count": defer_count,
        "defer_bound": int(declaration.get("defer_bound", 0)),
        "unpublished": 0, "threshold": 0,
        "oldest_unpublished_ms": 0, "now_ms": 0, "max_latency_ms": 0,
    }
    # Publication is unwired in this slice (unpublished is always 0),
    # so the clock fields stay zero; Forge receipt targets bind them.
    plan = request_plan(session, request)
    record.update({"digest": digest, "render_cached": cached,
                   "output": output_detail, "request": request})
    if plan is None:
        record.update({"gate": GATE_DEFER,
                       "gate_reason": "planner-unavailable",
                       "outcome": "deferred"})
        kept = _retain_shared(
            session, rows, projection_id, shared, canonical, observed,
            digest, STATUS_UNKNOWN, defer_count + 1, verdict,
            evidence_id, seen)
        if not kept:
            record.update({"gate_reason": "projection-state-conflict"})
        return record
    record["plan"] = plan
    gate = int(plan["gate"])
    if gate == GATE_ESCALATE:
        record.update({"outcome": "escalated",
                       "gate_reason": plan.get("gate_reason_name")})
        kept = _retain_shared(
            session, rows, projection_id, shared,
            int(plan["new_canonical"]), observed, digest, STATUS_BLOCKED,
            defer_count + 1, verdict, evidence_id, seen)
        if not kept:
            record.update({"outcome": "deferred",
                           "gate_reason": "projection-state-conflict"})
            return record
        session._emit("projection.escalated", "environment",
                      {"projection": projection_id, "plan": plan})
        return record
    if gate == GATE_DEFER or not plan.get("execute"):
        # execute=false with a non-current action is a native refusal
        # (await/failed verification, blocked): pending, not current.
        current = gate == GATE_PROCEED and int(plan["action"]) == 0
        status = STATUS_CURRENT if current else STATUS_STALE
        record.update({"outcome": "current" if current else "deferred",
                       "gate_reason": plan.get("gate_reason_name")})
        kept = _retain_shared(
            session, rows, projection_id, shared,
            int(plan["new_canonical"]), observed, digest, status,
            0 if current else defer_count + 1, verdict,
            evidence_id, seen)
        if not kept:
            record.update({"outcome": "deferred",
                           "gate_reason": "projection-state-conflict"})
            return record
        if not current:
            session._emit("projection.deferred", "environment",
                          {"projection": projection_id, "plan": plan})
        return record
    if expected is None:
        # Unreachable: output UNKNOWN always defers natively. Fail
        # closed rather than applying absent bytes.
        record.update({"outcome": "escalated",
                       "gate_reason": "missing-expected-bytes"})
        kept = _retain_shared(
            session, rows, projection_id, shared,
            int(plan["new_canonical"]), observed, digest, STATUS_BLOCKED,
            defer_count + 1, verdict, evidence_id, seen)
        if not kept:
            record.update({"outcome": "deferred",
                           "gate_reason": "projection-state-conflict"})
        return record
    if output_code == OUTPUT_MATCHES_FRESH:
        fresh, reason = _revalidate_for_commit(
            session, declaration, checkout, digest, verdict,
            evidence_id, preimage)
        if not fresh:
            record.update({"outcome": "deferred", "gate_reason": reason})
            kept = _retain_shared(
                session, rows, projection_id, shared,
                int(plan["new_canonical"]), observed, digest,
                STATUS_STALE, defer_count + 1, verdict, evidence_id,
                seen)
            if not kept:
                record.update(
                    {"gate_reason": "projection-state-conflict"})
            return record
        adopted = _adopt_shared(
            session, rows, projection_id, shared,
            int(plan["new_canonical"]), digest, bytes_digest(expected),
            verdict, evidence_id, seen, source)
        if not adopted:
            record.update({"outcome": "deferred",
                           "gate_reason": "projection-state-conflict"})
            return record
        record.update({"outcome": "converged"})
        return record
    applied, detail = apply_expected_bytes(
        session, declaration, checkout, expected, digest=digest,
        verdict=verdict, evidence_id=evidence_id, preimage=preimage)
    if not applied:
        record.update({"outcome": "deferred",
                       "gate_reason": f"apply-{detail}"})
        kept = _retain_shared(
            session, rows, projection_id, shared,
            int(plan["new_canonical"]), observed, digest, STATUS_STALE,
            defer_count + 1, verdict, evidence_id, seen)
        if not kept:
            record.update({"gate_reason": "projection-state-conflict"})
            return record
        session._emit("projection.deferred", "environment",
                      {"projection": projection_id, "plan": plan,
                       "apply": detail})
        return record
    adopted = _adopt_shared(
        session, rows, projection_id, shared, int(plan["new_canonical"]),
        digest, bytes_digest(expected), verdict, evidence_id, seen,
        source)
    if not adopted:
        # Bytes landed but another session owns the row now; the next
        # pass re-renders from the latest row and converges. Never
        # claim current on a row we did not publish.
        record.update({"outcome": "deferred",
                       "gate_reason": "projection-state-conflict"})
        return record
    record.update({"outcome": "reconciled"})
    session._emit("projection.reconciled", "environment",
                  {"projection": projection_id, "plan": plan})
    return record


def _refresh_claim_holders(session) -> None:
    """Refresh the snapshot holder view from a live store read."""
    try:
        live = claims_module.active_claims(session.store.read_claims())
    except (OSError, ValueError):
        return
    holders: dict[str, list[dict[str, Any]]] = {}
    for record in live.values():
        holders.setdefault(str(record.get("repository", "")), []).append(
            {"claim_id": str(record.get("claim_id", "")),
             "session_id": str(record.get("session_id", "")),
             "consumer_id": str(record.get("consumer_id", "")),
             "basis": str(record.get("basis", "")),
             "scope": record.get("scope", {"kind": "repository"})})
    session.snapshot["claim_holders"] = holders


def _machine_outputs(session, repository: str,
                     checkout: Path) -> list[str]:
    manifest_path = checkout / ".mncs" / "project.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    outputs = []
    for entry in manifest.get("projections") or []:
        if isinstance(entry, dict) and entry.get("output"):
            outputs.append(str(entry["output"]))
    return outputs


def _retain_row(rows: dict, projection_id: str, canonical: int,
                observed: int, digest: str | None,
                previous: dict | None, status: int,
                defer_count: int,
                verdict: int = VERDICT_UNKNOWN) -> None:
    rows[projection_id] = {
        "canonical_gen": canonical, "observed_gen": observed,
        "canonical_digest": digest if digest is not None
        else (previous or {}).get("canonical_digest"),
        "rendered_digest": (previous or {}).get("rendered_digest"),
        "verdict": verdict, "status": status,
        "defer_count": defer_count, "updated_at": utcnow()}


def _cache_row(shared: dict[str, Any]) -> dict[str, Any]:
    """Session-cache view of a shared row (presentation only)."""
    return {
        "canonical_gen": int(shared.get("canonical_gen", 0)),
        "observed_gen": int(shared.get("observed_gen", 0)),
        "canonical_digest": shared.get("canonical_digest"),
        "rendered_digest": shared.get("rendered_digest"),
        "verdict": int(shared.get("verdict", VERDICT_UNKNOWN)),
        "evidence_id": shared.get("evidence_id"),
        "evidence_ids": list(shared.get("evidence_ids") or []),
        "status": int(shared.get("status", STATUS_UNKNOWN)),
        "defer_count": int(shared.get("defer_count", 0)),
        "store_version": int(shared.get("version", 0)),
        "updated_at": shared.get("updated_at"),
    }


def _shared_fields(shared: dict[str, Any], canonical: int, observed: int,
                   digest: str | None, rendered: str | None, status: int,
                   defer_count: int, verdict: int,
                   evidence_id: str | None,
                   seen: list | None,
                   source: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "canonical_gen": canonical,
        "observed_gen": observed,
        "canonical_digest": (digest if digest is not None
                             else shared.get("canonical_digest")),
        # Source generation identity: which repository state this row
        # was derived from. Never conflated with the row's own
        # projection generation counters.
        "source": source if source is not None else shared.get("source"),
        "rendered_digest": (rendered if rendered is not None
                            else shared.get("rendered_digest")),
        "verdict": verdict,
        "evidence_id": evidence_id,
        "evidence_ids": [str(item.get("evidence_id")) for item in seen or []
                         if isinstance(item, dict)
                         and item.get("evidence_id")],
        "status": status,
        "wait": int(shared.get("wait", 0)),
        "defer_count": defer_count,
    }


def _retain_shared(session, rows: dict, projection_id: str,
                   shared: dict[str, Any], canonical: int, observed: int,
                   digest: str | None, status: int, defer_count: int,
                   verdict: int, evidence_id: str | None,
                   seen: list | None) -> bool:
    """Persist defer/escalate progress to the shared row (CAS).

    Returns False when another session advanced the row first or the
    backend is down; the caller then reports a conflict instead of
    claiming durable pending state it does not own.
    """
    published = _persist_shared_row(
        session, projection_id,
        _shared_fields(shared, canonical, observed, digest, None,
                       status, defer_count, verdict, evidence_id, seen),
        int(shared.get("version", 0)))
    if published is None:
        return False
    rows[projection_id] = _cache_row(published)
    return True


def _adopt_shared(session, rows: dict, projection_id: str,
                  shared: dict[str, Any], canonical: int, digest: str,
                  rendered: str, verdict: int, evidence_id: str | None,
                  seen: list | None,
                  source: dict[str, Any] | None = None) -> bool:
    """Adopt a converged generation through the shared CAS row.

    Monotonicity is enforced by the versioned write: a render based
    on a superseded row cannot publish. Stale bytes therefore fail
    safely here even if every earlier check passed.
    """
    if canonical < int(shared.get("observed_gen", 0)):
        # Regression: never move observed backwards, not even when the
        # version arithmetic would allow it.
        return False
    published = _persist_shared_row(
        session, projection_id,
        _shared_fields(shared, canonical, canonical, digest, rendered,
                       STATUS_CURRENT, 0, verdict, evidence_id, seen,
                       source),
        int(shared.get("version", 0)))
    if published is None:
        return False
    rows[projection_id] = _cache_row(published)
    return True


def _finish(session, started: str, clock_started: float,
            results: list[dict], rows: dict, invalid: list[dict],
            mode: str, failure: dict | None,
            epoch: str) -> dict[str, Any]:
    summary = {"current": 0, "pending": 0, "reconciled": 0, "blockers": 0,
               "degraded": 0, "epoch_reused": False}
    pending_ids: list[str] = []
    for record in results:
        outcome = record.get("outcome")
        if outcome in ("current", "converged"):
            summary["current"] += 1
        elif outcome == "reconciled":
            summary["current"] += 1
            summary["reconciled"] += 1
        elif outcome == "deferred":
            summary["pending"] += 1
            pending_ids.append(str(record.get("projection")))
            if str(record.get("gate_reason", "")).startswith(
                    ("planner-", "render-", "provider-", "shared-store-",
                     "projection-state-conflict")):
                summary["degraded"] += 1
        elif outcome == "escalated":
            summary["blockers"] += 1
            pending_ids.append(str(record.get("projection")))
    summary["invalid"] = len(invalid)
    if invalid:
        summary["blockers"] += len(invalid)
    if failure is not None:
        summary["blockers"] += 1
    evidence = {"schema_version": SCHEMA, "mode": mode,
                "started_at": started, "finished_at": utcnow(),
                "summary": summary, "results": results,
                "invalid": invalid, "failure": failure}
    evidence_ref = _write_evidence(session, evidence, mode)
    summary["pending_ids"] = pending_ids[:8]
    summary["elapsed_seconds"] = round(time.monotonic() - clock_started, 3)
    session.snapshot["projection_state"] = rows
    history = list(session.snapshot.get("projection_history") or [])
    history.append({"at": utcnow(), "mode": mode, "summary": dict(summary),
                    "evidence_ref": evidence_ref})
    session.snapshot["projection_history"] = history[-MAX_HISTORY:]
    # Degraded or failed passes never cache their epoch: a transient
    # provider outage must re-validate on the next pass, not replay
    # stale degraded state. Defers and escalations cache normally
    # because claims, markers, and inputs feed the next fingerprint.
    if mode == "ambient" and summary.get("degraded", 0) == 0 and failure is None:
        stored_epoch = epoch
    else:
        stored_epoch = f"uncached:{mode}:{epoch}"
    session.snapshot["projection_epoch"] = {
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
    ref = f"projection-{mode}-{digest_hex(evidence['finished_at'])[:12]}.json"
    path = directory / ref
    try:
        path.write_text(json.dumps(evidence, indent=2, sort_keys=True),
                        encoding="utf-8")
    except OSError:
        return "projection-evidence-unwritable"
    return f"sessions/{session.session_id}/projection-artifacts/evidence/{ref}"


def _append_session_evidence(session, summary: dict[str, Any],
                             results: list[dict], mode: str) -> None:
    entry = {
        "schema_version": EVIDENCE_SCHEMA,
        "session": session.session_id,
        "consumer": session.snapshot.get("consumer_id"),
        "mode": mode,
        "summary": {key: summary.get(key) for key in
                    ("current", "pending", "reconciled", "blockers",
                     "degraded")},
        "projections": [
            {"projection": record.get("projection"),
             "repository": record.get("repository"),
             "outcome": record.get("outcome"),
             "digest": record.get("digest"),
             "verdict": record.get("verdict"),
             "evidence_id": record.get("evidence_id"),
             "verification": record.get("verification"),
             "source": (record.get("source") or {
                 "head": (record.get("repo") or {}).get("head")}),
             "gate_reason": (record.get("plan") or {}).get(
                 "gate_reason_name", record.get("gate_reason"))}
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
    """Compact projection status for normal agent context."""
    stored = session.snapshot.get("projection_epoch") or {}
    summary = dict(stored.get("summary") or {})
    return {"projection_coherence": {
        "current": summary.get("current", 0),
        "pending": summary.get("pending", 0),
        "reconciled": summary.get("reconciled", 0),
        "blockers": summary.get("blockers", 0),
        "evidence": stored.get("evidence_ref")}}


def read_evidence(session) -> dict[str, Any]:
    """Full projection evidence for explicit inspection."""
    stored = session.snapshot.get("projection_epoch") or {}
    ref = stored.get("evidence_ref", "")
    if not ref or not ref.startswith("sessions/"):
        return {"evidence": None, "history": session.snapshot.get(
            "projection_history") or []}
    path = (session.store.state_dir / ref).resolve()
    try:
        if not path.is_relative_to(session.store.state_dir.resolve()):
            raise OSError("evidence escapes state root")
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        evidence = None
    return {"evidence": evidence, "history": session.snapshot.get(
        "projection_history") or []}
