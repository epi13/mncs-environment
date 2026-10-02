"""Family shared semantic workspace: changes, presence, convergence.

Worktrees are isolated editable projections; this module is the shared
understanding between them. Producer sessions publish identity-bound
working changes (``mncs.family-change/1``); consumer sessions observe
relevant changes, classify drift through native law, and converge via
admitted deterministic transforms under claim authority.

All shared state flows through the ``SessionStore`` interface
(projection rows + immutable evidence + claims), so the same code runs
on the file backend today and the Store backend when the toolchain
heals. No row format is backend-specific.

Semantic decisions (lifecycle, classification, gating, adoption,
transform admission, revisit) live ONLY in the native law
``mncs.commons.family.change.v1``. Without a toolchain the host
observes facts and fails closed (unknown/deferred), never guesses.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .toolchain import language_library_for
from .identity import digest_hex

#: Shared row identities (projection-row namespace).
GENERATION_ROW = "family:generation"
CHANGE_ROW_PREFIX = "family:change/"
CONTRIBUTOR_ROW_PREFIX = "family:contributor/"
RECON_ROW_PREFIX = "family:recon/"

#: Native law location inside a Commons checkout.
CHANGE_SOURCE = "src/mncs_commons/mesh/mncs/commons/family/change.mncs"
CHANGE_MODULE = "mncs.commons.family.change.v1"

#: Per-invocation timeout for native law calls.
NATIVE_CALL_TIMEOUT_SECS = 30

#: Bounds.
MAX_CHANGES_OBSERVED = 64
MAX_CONSUMERS_PER_CHANGE = 32
MAX_REPAIR_ATTEMPTS = 3
MAX_TRANSFORM_PATHS = 16
MAX_OPERATION_BYTES = 1 << 20
MAX_CAPSULE_ATTENTION = 8

#: Contributor records older than this with no live session read stale.
CONTRIBUTOR_TTL_SECS = 4 * 3600
CONTRIBUTOR_HEARTBEAT_SECS = CONTRIBUTOR_TTL_SECS // 4

#: Revisit backoff base for externally-gated deferrals.
REVISIT_BACKOFF_BASE_SECS = 60

#: File-descriptor pressure: refuse new spawns above this fraction of
#: the process limit (Linux only; unknown elsewhere degrades to allow).
FD_PRESSURE_FRACTION = 0.85


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- Commons vocabulary -------------------------------------------------

_COMMONS_MODULES: dict[tuple[str, str], Any] = {}


def _commons_module(session, filename: str) -> Any:
    """Load a structural authority by exact selected path and content."""
    root = find_commons_root(session)
    if root is None:
        return None
    path = root / "src/mncs_commons" / filename
    try:
        key = (str(path.resolve()), digest_hex(path.read_text()))
    except (OSError, ValueError):
        return None
    if key in _COMMONS_MODULES:
        return _COMMONS_MODULES[key]
    name = "_mncs_commons_selected_" + digest_hex(key)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    __import__("sys").modules[name] = module
    spec.loader.exec_module(module)
    if len(_COMMONS_MODULES) >= 8:
        _COMMONS_MODULES.pop(next(iter(_COMMONS_MODULES)))
    _COMMONS_MODULES[key] = module
    return module


def family_change_module(session=None) -> Any:
    return _commons_module(session, "family_change.py")


def find_commons_root(session=None) -> Path | None:
    """Commons root: explicit MNCS_COMMONS_ROOT wins, else selection."""
    override = os.environ.get("MNCS_COMMONS_ROOT", "")
    if override and (Path(override) / "src" / "mncs_commons").is_dir():
        return Path(override)
    if session is not None:
        paths = _selected_checkout_paths(session)
        if paths.get("MNCS-Commons"):
            candidate = Path(paths["MNCS-Commons"])
            if (candidate / "src" / "mncs_commons").is_dir():
                return candidate
    return None


def _selected_checkout_paths(session) -> dict[str, str]:
    paths: dict[str, str] = {}
    try:
        checkouts = session.snapshot.get("selected_checkouts") or {}
    except AttributeError:
        return paths
    if isinstance(checkouts, dict):
        for name, record in checkouts.items():
            if isinstance(record, dict) and record.get("path"):
                paths[str(name)] = str(record["path"])
    return paths


def find_mncs_binary(session=None) -> str | None:
    """Toolchain binary: explicit MNCS_BIN wins, else session toolchain."""
    override = os.environ.get("MNCS_BIN", "")
    if override and Path(override).is_file():
        return override
    if session is not None:
        try:
            toolchain = session.snapshot.get("toolchain") or {}
        except AttributeError:
            toolchain = {}
        binary = toolchain.get("binary")
        if binary and Path(str(binary)).is_file():
            return str(binary)
    return None


def _typed_integers(*values: int) -> str:
    return json.dumps([{"integer": {"value": int(value)}} for value in values])


def native_call(binary: str, source: Path, libraries: list[Path],
                function: str, args_json: str) -> Any | None:
    """One `mncs call` against the family-change law; None when unusable."""
    if fd_pressure().get("pressured"):
        return None
    command = [binary, "call", str(source), "--module", CHANGE_MODULE,
               "--function", function, "--args-json", args_json]
    for library in libraries:
        command.extend(["--library", str(library)])
    try:
        completed = subprocess.run(command, capture_output=True, text=True,
                                   timeout=NATIVE_CALL_TIMEOUT_SECS,
                                   check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    try:
        document = json.loads(completed.stdout)
    except ValueError:
        return None
    if document.get("status") != "returned":
        return None
    returned = document.get("call", {}).get("returned", [])
    return returned[0] if returned else None


def _native_ready(session) -> tuple[str, Path, list[Path]] | None:
    binary = find_mncs_binary(session)
    commons = find_commons_root(session)
    if not binary or commons is None:
        return None
    source = commons / CHANGE_SOURCE
    library = language_library_for(binary, session)
    mesh = commons / "src" / "mncs_commons" / "mesh"
    if not source.is_file() or library is None or not mesh.is_dir():
        return None
    return binary, source, [library, mesh]


def _finite_discriminant(value: Any) -> int | None:
    if not isinstance(value, dict):
        return None
    finite = value.get("finite")
    if not isinstance(finite, dict):
        return None
    try:
        return int(finite.get("discriminant"))
    except (TypeError, ValueError):
        return None


def _boolean_value(value: Any) -> bool | None:
    if not isinstance(value, dict):
        return None
    boolean = value.get("boolean")
    if not isinstance(boolean, dict) or "value" not in boolean:
        return None
    return bool(boolean["value"])


def native_lifecycle_legal(session, from_code: int, to_code: int) -> bool | None:
    ready = _native_ready(session)
    if ready is None:
        return None
    binary, source, libraries = ready
    return _boolean_value(native_call(
        binary, source, libraries, "lifecycle_legal",
        _typed_integers(from_code, to_code)))


def native_classify(session, facts: list[int]) -> int | None:
    ready = _native_ready(session)
    if ready is None:
        return None
    binary, source, libraries = ready
    return _finite_discriminant(native_call(
        binary, source, libraries, "classify_consumer",
        _typed_integers(*facts)))


def native_classify_batch(session, facts: list[list[int]]) -> list[int | None]:
    if not facts:
        return []
    if len(facts) > MAX_CONSUMERS_PER_CHANGE or any(len(row) != 8 for row in facts):
        raise ValueError("family classification exceeds bounded request")
    ready = _native_ready(session)
    if ready is None:
        return [None] * len(facts)
    binary, source, libraries = ready
    values = [{"sequence": {"values": [{"integer": {"value": int(value)}}
               for value in row]}} for row in facts]
    result = native_call(binary, source, libraries, "classify_consumers",
                         json.dumps([{"sequence": {"values": values}}]))
    returned = (result or {}).get("sequence", {}).get("values")
    if not isinstance(returned, list) or len(returned) != len(facts):
        return [None] * len(facts)
    return [int(value["integer"]["value"]) if isinstance(value, dict)
            and isinstance(value.get("integer", {}).get("value"), int)
            else None for value in returned]


def native_gate(session, facts: list[int]) -> int | None:
    ready = _native_ready(session)
    if ready is None:
        return None
    binary, source, libraries = ready
    return _finite_discriminant(native_call(
        binary, source, libraries, "gate_repair", _typed_integers(*facts)))


def native_adopt(session, facts: list[int]) -> int | None:
    ready = _native_ready(session)
    if ready is None:
        return None
    binary, source, libraries = ready
    return _finite_discriminant(native_call(
        binary, source, libraries, "adopt_post_repair",
        _typed_integers(*facts)))


def native_transform_admit(session, facts: list[int]) -> int | None:
    ready = _native_ready(session)
    if ready is None:
        return None
    binary, source, libraries = ready
    return _finite_discriminant(native_call(
        binary, source, libraries, "transform_admit",
        _typed_integers(*facts)))


def native_revisit_due(session, facts: list[int]) -> bool | None:
    ready = _native_ready(session)
    if ready is None:
        return None
    binary, source, libraries = ready
    return _boolean_value(native_call(
        binary, source, libraries, "revisit_due", _typed_integers(*facts)))


def native_contributor_live(session, session_live: int,
                           within_ttl: int) -> bool | None:
    ready = _native_ready(session)
    if ready is None:
        return None
    binary, source, libraries = ready
    return _boolean_value(native_call(
        binary, source, libraries, "contributor_live",
        _typed_integers(session_live, within_ttl)))


# --- shared rows -----------------------------------------------------------

def _read_row(session, row_id: str) -> tuple[int, dict[str, Any]] | None:
    try:
        found = session.store.read_projection_row(row_id)
    except (OSError, ValueError):
        return None
    if found is None:
        return None
    version, row = found
    if not isinstance(row, dict):
        return None
    return int(version), row


def _write_row(session, row_id: str, version: int,
               row: dict[str, Any]) -> tuple[bool, dict[str, Any] | None]:
    """Write a versioned row; (False, latest) on concurrent advance."""
    from .projection_store import ProjectionConflict
    row = dict(row)
    row["projection"] = row_id
    try:
        session.store.write_projection_row(row_id, version, row)
    except ProjectionConflict as conflict:
        return False, conflict.latest if isinstance(
            conflict.latest, dict) else None
    except (OSError, ValueError):
        return False, None
    return True, None


def read_generation(session) -> tuple[int, dict[str, Any]]:
    found = _read_row(session, GENERATION_ROW)
    if found is None:
        return 0, {"schema_version": "mncs.family-generation/1",
                   "projects": {}, "cursor": 0}
    return found


def bump_generation(session, repository: str, head: str,
                    change: str) -> dict[str, Any]:
    """Advance one project's generation (CAS loop; idempotent converge)."""
    for _ in range(8):
        version, row = read_generation(session)
        projects = dict(row.get("projects") or {})
        entry = dict(projects.get(repository) or {})
        if entry.get("head") == head and entry.get("change", "") == change:
            return {"generation": int(entry.get("generation", 0)),
                    "advanced": False}
        generation = int(entry.get("generation", 0)) + 1
        projects[repository] = {"generation": generation, "head": head,
                                "change": change}
        candidate = {"schema_version": "mncs.family-generation/1",
                     "projects": projects,
                     "cursor": int(row.get("cursor", 0)) + 1}
        ok, _ = _write_row(session, GENERATION_ROW, version + 1, candidate)
        if ok:
            return {"generation": generation, "advanced": True}
    return {"generation": -1, "advanced": False, "conflict": True}


def project_generation(session, repository: str) -> dict[str, Any]:
    _, row = read_generation(session)
    entry = (row.get("projects") or {}).get(repository) or {}
    return {"generation": int(entry.get("generation", 0)),
            "head": str(entry.get("head", "")),
            "change": str(entry.get("change", ""))}


def list_row_ids(session, prefix: str) -> list[str]:
    try:
        versions = session.store.read_projection_versions()
    except (OSError, ValueError, AttributeError):
        return []
    if not isinstance(versions, dict):
        return []
    # File backend sanitizes ids ([alnum-_.]); match sanitized prefixes too.
    plain = prefix.replace(":", "_").replace("/", "_")
    return sorted(identity for identity in versions
                  if identity.startswith(prefix) or identity.startswith(plain))


def publish_presence(session, active_changes: list[str]) -> dict[str, Any]:
    """Record this session's contributor presence (structured facts only)."""
    from . import claims as claims_module
    try:
        live = claims_module.active_claims(session.store.read_claims())
    except (OSError, ValueError):
        live = {}
    own = sorted(identity for identity, record in live.items()
                 if record.get("session_id") == session.session_id)
    try:
        consumer_id = str(session.snapshot.get("consumer_id", ""))
    except AttributeError:
        consumer_id = getattr(session, "consumer_id", "")
    record = {
        "schema_version": "mncs.family-contributor/1",
        "session": session.session_id,
        "consumer": consumer_id,
        "workspaces": sorted(_selected_checkout_paths(session).values()),
        "active_changes": list(active_changes)[:16],
        "claims": own[:32],
        "generation": read_generation(session)[1].get("cursor", 0),
        "updated_at": utcnow(),
    }
    row_id = CONTRIBUTOR_ROW_PREFIX + session.session_id
    found = _read_row(session, row_id)
    if found:
        prior = found[1]
        stable = {key: value for key, value in record.items() if key != "updated_at"}
        same = all(prior.get(key) == value for key, value in stable.items())
        try:
            age = (datetime.fromisoformat(record["updated_at"]) -
                   datetime.fromisoformat(str(prior.get("updated_at", "")))).total_seconds()
        except (ValueError, TypeError):
            age = CONTRIBUTOR_HEARTBEAT_SECS
        if same and 0 <= age < CONTRIBUTOR_HEARTBEAT_SECS:
            return {"published": False, "version": found[0], "current": True}
    version = (found[0] if found else 0) + 1
    ok, _ = _write_row(session, row_id, version, record)
    return {"published": ok, "version": version if ok else -1}


def read_contributors(session) -> list[dict[str, Any]]:
    contributors = []
    for row_id in list_row_ids(session, CONTRIBUTOR_ROW_PREFIX):
        found = _read_row(session, row_id)
        if found is not None:
            contributors.append(found[1])
    return contributors


# --- change publication ------------------------------------------------------

class FamilyError(ValueError):
    def __init__(self, message: str, code: str, **details: Any):
        super().__init__(message)
        self.diagnostics = {"code": code, **details}


def _vocabulary(session):
    module = family_change_module(session)
    if module is None:
        raise FamilyError("Commons family-change vocabulary unavailable",
                          "family-vocabulary-unavailable")
    return module


def publish_change(session, draft: dict[str, Any]) -> dict[str, Any]:
    """Validate and publish a draft working change as immutable evidence."""
    vocabulary = _vocabulary(session)
    try:
        record = vocabulary.validate_family_change(dict(draft))
    except Exception as error:
        raise FamilyError(f"family change structurally invalid: {error}",
                          "family-change-invalid") from error
    identity = record["identity"]
    try:
        session.store.write_evidence(f"family-change:{identity}", record)
    except Exception as error:
        raise FamilyError(f"family change evidence unwritable: {error}",
                          "family-evidence-unwritable") from error
    row_id = CHANGE_ROW_PREFIX + identity
    if _read_row(session, row_id) is None:
        row = {"schema_version": "mncs.family-change/1",
               "identity": identity, "state": record["state"],
               "version_hint": 1, "producer": record["producer"],
               "base": record["base"]}
        ok, _ = _write_row(session, row_id, 1, row)
        if not ok:
            raise FamilyError("family change row raced on publish",
                              "family-publish-raced")
    session._emit("family.published", "environment",
                  {"change": identity, "state": record["state"],
                   "repository": record["producer"]["repository"]})
    try:
        produced = list(session.snapshot.get("family_produced") or [])
        if identity not in produced:
            produced.append(identity)
        session.snapshot["family_produced"] = produced[-32:]
    except AttributeError:
        pass
    return {"identity": identity, "state": record["state"]}


def read_change(session, identity: str) -> dict[str, Any] | None:
    try:
        record = session.store.read_evidence(f"family-change:{identity}")
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    found = _read_row(session, CHANGE_ROW_PREFIX + identity)
    if found is not None:
        record = dict(record)
        record["state"] = found[1].get("state", record.get("state"))
        record["row_version"] = found[0]
        if found[1].get("established_generation") is not None:
            record["established_generation"] = int(
                found[1]["established_generation"])
    return record


def transition_change(session, identity: str, to_state: str) -> dict[str, Any]:
    """Advance change lifecycle; legality is native law, fail-closed."""
    vocabulary = _vocabulary(session)
    states = vocabulary.CHANGE_STATES
    if to_state not in states:
        raise FamilyError(f"unknown change state: {to_state}",
                          "family-state-unknown")
    row_id = CHANGE_ROW_PREFIX + identity
    for _ in range(8):
        found = _read_row(session, row_id)
        if found is None:
            raise FamilyError("family change row missing",
                              "family-change-missing")
        version, row = found
        from_state = str(row.get("state", "draft"))
        if from_state not in states:
            raise FamilyError("family change row carries unknown state",
                              "family-state-unknown")
        if from_state == to_state:
            return {"identity": identity, "state": to_state,
                    "advanced": False}
        legal = native_lifecycle_legal(session, states[from_state],
                                       states[to_state])
        if legal is None:
            raise FamilyError("toolchain cannot judge the transition",
                              "family-toolchain-unavailable")
        if not legal:
            raise FamilyError(
                f"transition {from_state} -> {to_state} refused by native law",
                "family-transition-refused")
        candidate = dict(row)
        candidate["state"] = to_state
        ok, _ = _write_row(session, row_id, version + 1, candidate)
        if ok:
            session._emit("family.transitioned", "environment",
                          {"change": identity, "from": from_state,
                           "to": to_state})
            return {"identity": identity, "state": to_state, "advanced": True}
    raise FamilyError("family change row raced repeatedly",
                      "family-transition-raced")


def establish_change(session, identity: str) -> dict[str, Any]:
    """Validate then establish a change (draft/observed -> ... -> established)."""
    record = read_change(session, identity)
    if record is None:
        raise FamilyError("family change unknown", "family-change-missing")
    path = {"draft": ["observed", "validated", "established"],
            "observed": ["validated", "established"],
            "validated": ["established"],
            "conflicted": ["validated", "established"]}.get(
                str(record.get("state", "")), [])
    if not path and record.get("state") != "established":
        raise FamilyError(
            f"change in state {record.get('state')} cannot establish",
            "family-transition-refused")
    for state in path:
        transition_change(session, identity, state)
    producer = record.get("producer") or {}
    generation = bump_generation(session, str(producer.get("repository", "")),
                                 str((record.get("base") or {}).get("head", "")),
                                 identity)
    # Pin the establishment generation on the change row: each
    # reconciliation row converges toward its own change's target,
    # never a moving producer HEAD.
    row_id = CHANGE_ROW_PREFIX + identity
    found = _read_row(session, row_id)
    if found is not None:
        candidate = dict(found[1])
        candidate["established_generation"] = int(generation.get(
            "generation", 0))
        _write_row(session, row_id, found[0] + 1, candidate)
    session._emit("family.established", "environment",
                  {"change": identity, "generation": generation.get(
                      "generation")})
    return {"identity": identity, "state": "established",
            "generation": generation.get("generation")}


# --- dependency edges (host-observed manifest facts) --------------------------

def dependency_consumers(session, producer_repository: str,
                         contracts_changed: list[dict[str, Any]]) -> list[str]:
    """Query Commons-declared architecture without semantic source scans.

    Manifest exports and explicit semantic-contract declarations complement
    one another. Observed Language Service impact never invents an edge.
    """
    authority = _commons_module(session, "family_graph.py")
    paths = _selected_checkout_paths(session)
    checkout = paths.get(producer_repository)
    if authority is None or not checkout:
        return []
    try:
        manifest = json.loads((Path(checkout) / ".mncs/project.json").read_text())
        provider = str(manifest["repository"])
        provides, _ = authority.repository_contracts(Path(checkout))
    except (OSError, ValueError, KeyError):
        return []
    changed = set()
    for item in contracts_changed:
        if not isinstance(item, dict):
            continue
        name = str(item.get("contract", ""))
        identity = name if "." in name or "/" in name else f"{provider}.{name}"
        if identity in provides:
            changed.add(identity)
    consumers = []
    for repository, path in sorted(paths.items()):
        if repository == producer_repository:
            continue
        try:
            _, consumes = authority.repository_contracts(Path(path))
        except (OSError, ValueError, KeyError):
            continue
        if consumes & changed:
            consumers.append(repository)
    return consumers[:MAX_CONSUMERS_PER_CHANGE]


# --- drift facts -----------------------------------------------------------------

def _live_claims(session) -> dict[str, dict[str, Any]]:
    from . import claims as claims_module
    try:
        return claims_module.active_claims(session.store.read_claims())
    except (OSError, ValueError):
        return {}


def _own_covering_claim(session, repository: str) -> dict[str, Any] | None:
    live = _live_claims(session)
    for record in live.values():
        if record.get("session_id") != session.session_id:
            continue
        if str(record.get("repository", "")) != repository:
            continue
        scope = record.get("scope") or {}
        if isinstance(scope, dict) and scope.get("kind") == "repository":
            return record
        if isinstance(scope, dict) and scope.get("kind") == "worktree":
            checkout = _selected_checkout_paths(session).get(repository)
            if checkout and Path(str(scope.get("checkout", ""))).resolve() == Path(checkout).resolve():
                return record
    return None


def _occupied_by_other(session, repository: str,
                       paths: list[str]) -> dict[str, Any] | None:
    """Another live session's claim covering the consumer target."""
    from . import claims as claims_module
    live = _live_claims(session)
    for record in live.values():
        if record.get("session_id") == session.session_id:
            continue
        if str(record.get("repository", "")) != repository:
            continue
        scope = record.get("scope") or {}
        if not isinstance(scope, dict):
            continue
        if scope.get("kind") == "repository":
            return record
        if scope.get("kind") == "worktree":
            checkout = _selected_checkout_paths(session).get(repository)
            selected = session.snapshot.get("selected_checkouts", {}).get(repository, {})
            candidate = {"kind": "worktree", "repository": repository,
                         "checkout": checkout, "branch": selected.get("branch"),
                         "paths": list(paths) or None}
            # Missing addressing facts cannot prove isolated authority.
            if not checkout or not scope.get("checkout") or not selected.get("branch") or not scope.get("branch"):
                return record
            if claims_module.scopes_conflict(candidate, scope) is not None:
                return record
            continue
        if scope.get("kind") == "paths":
            claimed = scope.get("paths") or []
            if claims_module.scopes_conflict(
                    {"kind": "paths", "paths": list(paths)},
                    {"kind": "paths", "paths": list(claimed)}) is not None:
                return record
    return None


def _verification_verdict(session, obligations: list[str]) -> str:
    """Worst known verdict over owning obligations (fail > unknown > pass)."""
    try:
        rows = session.snapshot.get("verification_state") or {}
    except AttributeError:
        return "unknown"
    if not isinstance(rows, dict):
        return "unknown"
    all_pass = bool(obligations)
    for obligation in obligations:
        entry = rows.get(obligation)
        evidence = (entry or {}).get("evidence") if isinstance(
            entry, dict) else None
        verdict = str((evidence or {}).get("verdict", "UNKNOWN")).upper()
        if verdict == "FAIL":
            return "failed"
        if verdict != "PASS":
            all_pass = False
    return "passed" if all_pass else "unknown"


def _verification_digest(session, obligations: list[str]) -> str:
    """Stable digest of the verdict inputs adoption depends on."""
    import hashlib
    try:
        rows = session.snapshot.get("verification_state") or {}
    except AttributeError:
        rows = {}
    # Bind the whole owning evidence object, including producer evidence IDs
    # and exact subject/request identities. Row timestamps alone are not proof.
    material = {obligation: ((rows.get(obligation) or {}).get("evidence")
                            if isinstance(rows.get(obligation), dict) else None)
                for obligation in sorted(obligations)} if isinstance(rows, dict) else {}
    return digest_hex(material, length=64)


def semantic_impact(session, workspace: str,
                    subjects: list[dict[str, Any]]) -> dict[str, Any]:
    """Language Service impact for changed subjects; honest degradation.

    Queries only a resident that is already live for the workspace;
    starting residents stays explicit (Doctor lifecycle ownership).
    """
    outcome: dict[str, Any] = {"available": False, "affected": [],
                               "detail": "resident-unavailable"}
    bindings = []
    try:
        bindings = session.snapshot.get("bindings") or []
    except AttributeError:
        pass
    available = {binding.get("capability") for binding in bindings
                 if isinstance(binding, dict)}
    if "mncs-language-service:resident-status" not in available:
        return outcome
    try:
        status = session.invoke(
            "mncs-language-service:resident-status",
            ["status", "--workspace", workspace], timeout_seconds=10)
    except (OSError, ValueError):
        return outcome
    try:
        document = json.loads(str(status.get("stdout", "")))
    except ValueError:
        return outcome
    if not isinstance(document, dict) or not document.get("live", False):
        return outcome
    if "mncs-language-service:semantic-query" not in available:
        outcome["detail"] = "query-unavailable"
        return outcome
    affected: list[dict[str, Any]] = []
    for subject in subjects[:8]:
        uri = subject.get("uri") or ""
        identity = subject.get("identity") or ""
        if not uri or not identity:
            continue
        try:
            result = session.invoke(
                "mncs-language-service:semantic-query",
                ["query", "--workspace", workspace,
                 "--method", "semantic_impact",
                 "--params", json.dumps({"uri": uri, "identity": identity})],
                timeout_seconds=10)
            payload = json.loads(str(result.get("stdout", "")))
        except (OSError, ValueError):
            continue
        impact = payload.get("impact") if isinstance(payload, dict) else None
        if isinstance(impact, dict):
            affected.append({"subject": identity, "impact": impact})
    outcome["available"] = True
    outcome["affected"] = affected
    outcome["detail"] = ""
    return outcome


def classify_drift(session, change: dict[str, Any],
                   consumer: str) -> dict[str, Any]:
    """Classify one consumer against one established change (native law).

    Returns the class plus the observed facts; unknown when the
    toolchain cannot judge.
    """
    observation = _drift_observation(session, change, consumer)
    return _classified_observation(session, observation,
                                   native_classify(session, observation["facts"]))


def reconciliation_row_id(session, identity: str, consumer: str) -> str:
    """Address one physical checkout's reconciliation in this Store namespace.

    Family change identity is shared. Adoption is a fact about a checkout,
    so another worktree must establish its own repair and verification.
    Legacy repository-only rows are preserved but cannot authorize adoption.
    This address is not cross-machine semantic/evidence equivalence.
    """
    import hashlib
    checkout = _selected_checkout_paths(session).get(consumer)
    if not checkout:
        raise FamilyError("consumer checkout unavailable", "family-checkout-unavailable")
    projection = hashlib.sha256(str(Path(checkout).resolve()).encode()).hexdigest()
    return RECON_ROW_PREFIX + identity + "/" + consumer + "/wc:" + projection


def _selected_reconciliation(session, row_id: str, row: dict[str, Any]) -> bool:
    try:
        expected = reconciliation_row_id(session, str(row.get("change", "")),
                                          str(row.get("consumer", "")))
    except FamilyError:
        return False
    return row_id == expected or row_id == expected.replace(":", "_").replace("/", "_")


def _drift_observation(session, change: dict[str, Any], consumer: str) -> dict[str, Any]:
    """Collect facts once; the native authority chooses their disposition."""
    vocabulary = _vocabulary(session)
    classes = vocabulary.CONSUMER_CLASSES
    producer = (change.get("producer") or {}).get("repository", "")
    row_id = reconciliation_row_id(session, change["identity"], consumer)
    found = _read_row(session, row_id)
    observed = int((found[1] if found else {}).get(
        "observed_generation", 0))
    # Converge toward this change's own establishment generation;
    # fall back to producer HEAD for rows predating the pin.
    pinned = change.get("established_generation")
    if pinned is None:
        change_row = _read_row(
            session, CHANGE_ROW_PREFIX + change["identity"])
        pinned = (change_row[1].get("established_generation")
                  if change_row else None)
    if pinned is None:
        pinned = project_generation(session, producer)["generation"]
    canonical_generation = int(pinned)
    drift = 1 if canonical_generation > observed else 0
    repair_state = str((found[1] if found else {}).get(
        "repair_state", "no_repair"))
    operations = change.get("operations") or []
    ops_known = 1 if operations and all(
        str(op.get("op", "")) in vocabulary.TRANSFORM_OPS
        for op in operations if isinstance(op, dict)) else 0
    semantic_choice = 1 if change.get("intent", {}).get(
        "semantic_choice_required", False) else 0
    target_paths: list[str] = []
    for subject in change.get("subjects") or []:
        if isinstance(subject, dict):
            target_paths.extend(str(path) for path in
                                subject.get("paths", [])[:8])
    occupied = 1 if _occupied_by_other(
        session, consumer, target_paths[:16]) is not None else 0
    obligations = [str(ob) for ob in
                   (change.get("verification") or {}).get("obligations", [])]
    verification_known = 1 if obligations else 0
    provider_available = 1
    try:
        bindings = session.snapshot.get("bindings") or []
        if isinstance(bindings, list) and not bindings:
            provider_available = 0
    except AttributeError:
        provider_available = 0
    compatible = 0 if change.get("intent", {}).get(
        "breaking", False) else 1
    facts = [drift, compatible,
             vocabulary.REPAIR_STATES.get(repair_state, 0), occupied,
             semantic_choice, ops_known, provider_available,
             verification_known]
    return {"consumer": consumer, "facts": facts,
            "observed_generation": observed,
            "canonical_generation": canonical_generation}


def _classified_observation(session, observation: dict[str, Any],
                            code: int | None) -> dict[str, Any]:
    if code is None:
        return {**observation, "consumer_class": "unknown",
                "detail": "toolchain-unavailable"}
    names = {value: key for key, value in _vocabulary(session).CONSUMER_CLASSES.items()}
    return {**observation,
            "consumer_class": names.get(code, "unknown"),
            "detail": ""}


def record_drift(session, change: dict[str, Any],
                 classification: dict[str, Any]) -> dict[str, Any]:
    """Persist a reconciliation row (CAS; conflict observes latest)."""
    vocabulary = _vocabulary(session)
    row_id = reconciliation_row_id(session, change["identity"], classification["consumer"])
    for _ in range(8):
        found = _read_row(session, row_id)
        version = found[0] if found else 0
        prior = found[1] if found else {}
        # Single-flight: a row already converged past this canonical
        # generation is observed, never rewritten.
        if int(prior.get("observed_generation", 0)) >= int(
                classification["canonical_generation"]) and \
                classification["canonical_generation"] > 0:
            return {"row": row_id, "version": version,
                    "converged": True,
                    "consumer_class": prior.get("consumer_class")}
        row = {**prior,
            "schema_version": vocabulary.FAMILY_RECONCILIATION_SCHEMA,
            "change": change["identity"],
            "consumer": classification["consumer"],
            "consumer_class": classification["consumer_class"],
            "observed_generation": classification["observed_generation"],
            "canonical_generation": classification["canonical_generation"],
            "repair_state": str(prior.get("repair_state", "no_repair")),
            "attempts": int(prior.get("attempts", 0)),
            "detail": str(classification.get("detail", "")),
            "evidence_ref": str(prior.get("evidence_ref", "")),
        }
        if all(prior.get(key) == value for key, value in row.items()):
            return {"row": row_id, "version": version, "converged": False,
                    "consumer_class": row["consumer_class"], "current": True}
        try:
            vocabulary.validate_reconciliation(row)
        except Exception as error:
            raise FamilyError(f"reconciliation row invalid: {error}",
                              "family-row-invalid") from error
        ok, latest = _write_row(session, row_id, version + 1, row)
        if ok:
            return {"row": row_id, "version": version + 1,
                    "converged": False,
                    "consumer_class": row["consumer_class"]}
        if latest is not None and int(latest.get(
                "observed_generation", 0)) >= int(
                classification["canonical_generation"]) and \
                classification["canonical_generation"] > 0:
            return {"row": row_id, "version": -1, "converged": True,
                    "consumer_class": latest.get("consumer_class")}
    return {"row": row_id, "version": -1, "converged": False,
            "consumer_class": classification["consumer_class"],
            "conflict": True}


# --- resource guard --------------------------------------------------------------

def fd_pressure() -> dict[str, Any]:
    """Own-process file-descriptor pressure (Linux measurable, else unknown)."""
    try:
        import resource
        used = len(list((Path("/proc/self/fd")).iterdir()))
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        limit = soft if soft > 0 else hard if hard > 0 else 1024
        return {"measurable": True, "used": used, "limit": int(limit),
                "pressured": (used / limit) >= FD_PRESSURE_FRACTION}
    except (OSError, ValueError, ImportError):
        return {"measurable": False, "pressured": False}


# --- deterministic transforms ------------------------------------------------------

def _sha256_file(path: Path) -> str | None:
    try:
        digest = __import__("hashlib").sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None
    return "sha256:" + digest


def _preimage_ok(checkout: Path, operation: dict[str, Any]) -> bool:
    preimage = operation.get("preimage") or {}
    if not isinstance(preimage, dict):
        return False
    for rel, expected in preimage.items():
        if _sha256_file(checkout / rel) != expected:
            return False
    return True


def _git_status_paths(checkout: Path) -> set[str]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(checkout), "status", "--porcelain=v1"],
            capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return set()
    if completed.returncode != 0:
        return set()
    paths = set()
    for line in completed.stdout.splitlines():
        rest = line[3:] if len(line) > 3 else ""
        if " -> " in rest:
            rest = rest.split(" -> ", 1)[1]
        rest = rest.strip().strip('"')
        if rest:
            paths.add(rest)
    return paths


def foreign_dirt(checkout: Path, operations: list[dict[str, Any]]) -> list[str]:
    """Target paths dirty with content matching neither preimage nor postimage.

    Postimage match means already converged (not foreign). Anything else
    dirty under a repair path is foreign and refuses the repair.
    """
    dirty = _git_status_paths(checkout)
    foreign = []
    for operation in operations:
        preimage = operation.get("preimage") or {}
        for rel in operation.get("paths", [])[:MAX_TRANSFORM_PATHS]:
            if rel not in dirty:
                continue
            current = _sha256_file(checkout / rel)
            if current == preimage.get(rel):
                continue
            foreign.append(rel)
    return sorted(set(foreign))


def _execute_set_json_field(checkout: Path, operation: dict[str, Any]
                            ) -> tuple[bool, str]:
    params = operation.get("params") or {}
    paths = operation.get("paths") or []
    if len(paths) != 1:
        return False, "set_json_field needs exactly one path"
    field = str(params.get("field", ""))
    if not field or ".." in field:
        return False, "invalid field"
    target = checkout / paths[0]
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return False, f"unreadable json: {error}"
    cursor: Any = document
    parts = field.split(".")
    for part in parts[:-1]:
        if not isinstance(cursor, dict) or part not in cursor:
            return False, f"missing field prefix: {field}"
        cursor = cursor[part]
    if not isinstance(cursor, dict) or parts[-1] not in cursor:
        return False, f"missing field: {field}"
    if cursor[parts[-1]] == params.get("to"):
        return True, "already-converged"
    cursor[parts[-1]] = params.get("to")
    try:
        target.write_text(json.dumps(document, indent=2, sort_keys=True)
                          + "\n", encoding="utf-8")
    except OSError as error:
        return False, f"unwritable: {error}"
    try:
        reread = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, "postimage-unreadable"
    cursor = reread
    for part in parts[:-1]:
        cursor = cursor.get(part, {})
    if not isinstance(cursor, dict) or cursor.get(parts[-1]) != params.get(
            "to"):
        return False, "postimage-mismatch"
    return True, "applied"


def _execute_replace_span(checkout: Path, operation: dict[str, Any]
                          ) -> tuple[bool, str]:
    params = operation.get("params") or {}
    paths = operation.get("paths") or []
    if len(paths) != 1:
        return False, "replace_span needs exactly one path"
    old = params.get("old", "")
    new = params.get("new", "")
    if not isinstance(old, str) or not old or not isinstance(new, str):
        return False, "old/new must be strings"
    if len(old.encode("utf-8")) + len(new.encode("utf-8")) > MAX_OPERATION_BYTES:
        return False, "span exceeds operation bound"
    target = checkout / paths[0]
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, ValueError) as error:
        return False, f"unreadable: {error}"
    occurrences = text.count(old)
    if occurrences == 0 and new in text:
        return True, "already-converged"
    if occurrences != 1:
        return False, f"expected exactly one occurrence, found {occurrences}"
    try:
        target.write_text(text.replace(old, new, 1), encoding="utf-8")
    except OSError as error:
        return False, f"unwritable: {error}"
    return True, "applied"


def _execute_run_capability(session, checkout: Path,
                            operation: dict[str, Any]
                            ) -> tuple[bool, str]:
    params = operation.get("params") or {}
    capability = str(params.get("capability", ""))
    argv = params.get("argv", [])
    if not capability or not isinstance(argv, list):
        return False, "capability/argv invalid"
    allow = set(operation.get("paths") or [])
    before = _git_status_paths(checkout)
    try:
        result = session.invoke(capability, [str(arg) for arg in argv],
                                cwd=str(checkout), timeout_seconds=120)
    except (OSError, ValueError) as error:
        return False, f"invocation failed: {error}"
    if str(result.get("status", "")) not in ("ok", "completed"):
        return False, f"capability status: {result.get('status')}"
    after = _git_status_paths(checkout)
    added = {path for path in after - before}
    outside = sorted(path for path in added if path not in allow)
    if outside:
        # Exact inverse of the observed delta: restore tracked
        # modifications outside the allowlist; remove files the run
        # itself created outside it. All recorded in evidence.
        restored = []
        for path in outside:
            target = checkout / path
            completed = subprocess.run(
                ["git", "-C", str(checkout), "checkout", "--", path],
                capture_output=True, timeout=30, check=False)
            if completed.returncode == 0:
                restored.append(path)
                continue
            try:
                if target.is_file():
                    target.unlink()
                    restored.append(path)
            except OSError:
                pass
        return False, ("output escaped allowlist; restored: "
                       + ",".join(restored))
    return True, "applied"


def execute_operation(session, checkout: Path,
                      operation: dict[str, Any]) -> tuple[bool, str]:
    op = str(operation.get("op", ""))
    if op == "set_json_field":
        return _execute_set_json_field(checkout, operation)
    if op == "replace_span":
        return _execute_replace_span(checkout, operation)
    if op == "run_capability":
        return _execute_run_capability(session, checkout, operation)
    return False, f"unknown op: {op}"


# --- two-phase converge -------------------------------------------------------------

def converge(session, identity: str, consumer: str, *,
             dry_run: bool = False) -> dict[str, Any]:
    """Apply admitted deterministic repair to a claimed consumer checkout.

    Two-phase: plan (classify + gate + admit) then revalidate every
    input (producer generation, consumer preimage, claim, row version)
    immediately before each mutation. Any drift aborts to re-plan.
    """
    vocabulary = _vocabulary(session)
    change = read_change(session, identity)
    if change is None:
        raise FamilyError("family change unknown", "family-change-missing")
    if str(change.get("state")) != "established":
        raise FamilyError("only established changes converge",
                          "family-not-established")
    paths = _selected_checkout_paths(session)
    checkout = paths.get(consumer)
    if not checkout or not Path(checkout).is_dir():
        raise FamilyError("consumer checkout unavailable",
                          "family-checkout-unavailable")
    checkout_path = Path(checkout)
    pressure = fd_pressure()
    if pressure.get("pressured"):
        return {"converged": False, "disposition": "deferred",
                "detail": "fd-exhausted"}
    classification = classify_drift(session, change, consumer)
    row_id = reconciliation_row_id(session, identity, consumer)
    found = _read_row(session, row_id)
    row_version = found[0] if found else 0
    attempts = int((found[1] if found else {}).get("attempts", 0))
    operations = [op for op in change.get("operations", [])
                  if isinstance(op, dict)]
    target_paths: list[str] = []
    for operation in operations:
        target_paths.extend(str(path) for path in
                            operation.get("paths", [])[:MAX_TRANSFORM_PATHS])
    dirt = foreign_dirt(checkout_path, operations)
    preimage_ok = 1 if not dirt and all(
        _preimage_ok(checkout_path, operation) or _postimage_converged(
            checkout_path, operation) for operation in operations) else 0
    producer = (change.get("producer") or {}).get("repository", "")
    # The plan targets this change's pinned establishment generation;
    # producer HEAD advancing beyond it never invalidates the plan.
    # What invalidates is the change leaving established (superseded).
    live = read_change(session, identity)
    generation_current = 1 if live is not None and str(
        live.get("state")) == "established" else 0
    claim_live = 1 if _own_covering_claim(session, consumer) is not None else 0
    class_code = vocabulary.CONSUMER_CLASSES.get(
        classification["consumer_class"], 6)
    gate = native_gate(session, [class_code, preimage_ok, generation_current,
                                 claim_live, 1 if dirt else 0, attempts,
                                 MAX_REPAIR_ATTEMPTS])
    gates = vocabulary.GATES
    gate_name = {value: key for key, value in gates.items()}.get(gate, "escalate")
    if gate is None:
        gate_name = "defer"
        detail = "toolchain-unavailable"
    elif gate_name == "proceed":
        detail = ""
    elif gate_name == "defer":
        detail = _defer_detail(preimage_ok, generation_current, claim_live,
                               classification["consumer_class"])
    else:
        detail = _escalate_detail(dirt, attempts, classification)
    if gate_name != "proceed":
        _record_attempt(session, row_id, row_version, classification,
                        attempts, classification["consumer_class"], detail,
                        identity)
        return {"converged": False, "disposition": gate_name,
                "detail": detail}
    if dry_run:
        return {"converged": False, "disposition": "dry-run",
                "operations": len(operations)}
    # Capture proof before effects; a previous PASS cannot adopt this repair.
    obligations = [str(ob) for ob in (change.get("verification") or {}).get("obligations", [])]
    verification_before = {ob: _verification_digest(session, [ob])
                           for ob in obligations}
    # Phase two: revalidate then apply each operation atomically.
    applied = []
    for operation in operations:
        live_state = read_change(session, identity)
        if live_state is None or str(live_state.get("state")) != \
                "established":
            return {"converged": False, "disposition": "deferred",
                    "detail": "change-superseded",
                    "applied": applied}
        if _own_covering_claim(session, consumer) is None:
            return {"converged": False, "disposition": "deferred",
                    "detail": "claim-lost", "applied": applied}
        if not _preimage_ok(checkout_path, operation) and not \
                _postimage_converged(checkout_path, operation):
            return {"converged": False, "disposition": "deferred",
                    "detail": "preimage-changed", "applied": applied}
        admit = native_transform_admit(session, [
            vocabulary.TRANSFORM_OPS.get(str(operation.get("op", "")), 9),
            1, 1,
            1 if len(operation.get("paths", [])) <= MAX_TRANSFORM_PATHS else 0,
            1])
        if admit != 0:
            return {"converged": False, "disposition": "escalated",
                    "detail": "transform-refused", "applied": applied}
        ok, note = execute_operation(session, checkout_path, operation)
        applied.append({"op": operation.get("op"), "ok": ok, "note": note})
        if not ok:
            return {"converged": False, "disposition": "escalated",
                    "detail": f"transform-failed:{note}", "applied": applied}
    _record_attempt(session, row_id, row_version, classification,
                    attempts + 1, "pending_verification", "", identity,
                    repair_state="applied_unknown",
                    verification_before=verification_before)
    session._emit("family.repaired", "environment",
                  {"change": identity, "consumer": consumer,
                   "operations": len(applied)})
    return {"converged": True, "disposition": "applied",
            "applied": applied, "repair_state": "applied_unknown"}


def _postimage_converged(checkout: Path, operation: dict[str, Any]) -> bool:
    """Target already carries the operation's postimage (idempotent skip)."""
    op = str(operation.get("op", ""))
    params = operation.get("params") or {}
    paths = operation.get("paths") or []
    if len(paths) != 1:
        return False
    target = checkout / paths[0]
    try:
        content = target.read_bytes()
    except OSError:
        return False
    if op == "set_json_field":
        try:
            document = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return False
        cursor: Any = document
        for part in str(params.get("field", "")).split(".")[:-1]:
            if not isinstance(cursor, dict):
                return False
            cursor = cursor.get(part)
        if not isinstance(cursor, dict):
            return False
        field = str(params.get("field", "")).split(".")[-1]
        return cursor.get(field) == params.get("to")
    if op == "replace_span":
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            return False
        old, new = params.get("old", ""), params.get("new", "")
        return bool(old) and old not in text and new in text
    return False


def _defer_detail(preimage_ok: int, generation_current: int, claim_live: int,
                  consumer_class: str) -> str:
    if consumer_class == "occupied":
        return "occupied"
    if consumer_class == "blocked":
        return "provider-unavailable"
    if consumer_class == "pending_verification":
        return "awaiting-verdict"
    if not preimage_ok:
        return "replan"
    if not generation_current:
        return "replan"
    if not claim_live:
        return "claim-required"
    return "deferred"


def _escalate_detail(dirt: list[str], attempts: int,
                     classification: dict[str, Any]) -> str:
    if dirt:
        return "foreign-dirt:" + ",".join(dirt[:4])
    if attempts >= MAX_REPAIR_ATTEMPTS:
        return "budget-exhausted"
    return str(classification.get("consumer_class", "unknown"))


def _record_attempt(session, row_id: str, row_version: int,
                    classification: dict[str, Any], attempts: int,
                    consumer_class: str, detail: str, identity: str,
                    repair_state: str | None = None,
                    verification_before: dict[str, str] | None = None) -> None:
    vocabulary = _vocabulary(session)
    found = _read_row(session, row_id)
    if found is None:
        if row_version != 0:
            return
        prior: dict[str, Any] = {}
    elif found[0] != row_version:
        return
    else:
        prior = found[1]
    row = {**prior,
        "schema_version": vocabulary.FAMILY_RECONCILIATION_SCHEMA,
        "change": identity,
        "consumer": classification["consumer"],
        "consumer_class": consumer_class,
        "observed_generation": classification["observed_generation"],
        "canonical_generation": classification["canonical_generation"],
        "repair_state": repair_state or str(prior.get("repair_state",
                                                      "no_repair")),
        "attempts": attempts,
        "detail": detail,
        "evidence_ref": str(prior.get("evidence_ref", "")),
    }
    if verification_before is not None:
        row["verification_before"] = verification_before
    try:
        vocabulary.validate_reconciliation(row)
    except Exception:
        return
    _write_row(session, row_id, row_version + 1, row)


# --- adoption + revisit -------------------------------------------------------------

def adopt_pending(session, identity: str, consumer: str) -> dict[str, Any]:
    """Adopt a verified repair into the reconciliation row (CAS, native law).

    Reads the owning verification verdicts; only PASS adopts. FAIL
    reclassifies to semantic_required (needs thought); UNKNOWN stays
    pending. Returns the disposition, never raises on races.
    """
    vocabulary = _vocabulary(session)
    change = read_change(session, identity)
    if change is None:
        return {"adopted": False, "detail": "change-missing"}
    row_id = reconciliation_row_id(session, identity, consumer)
    found = _read_row(session, row_id)
    if found is None:
        return {"adopted": False, "detail": "row-missing"}
    version, row = found
    if str(row.get("repair_state", "no_repair")) == "no_repair":
        return {"adopted": False, "detail": "nothing-applied"}
    obligations = [str(ob) for ob in
                   (change.get("verification") or {}).get("obligations", [])]
    verdict = _verification_verdict(session, obligations)
    before = row.get("verification_before")
    proof_renewed = (isinstance(before, dict) and bool(obligations)
                     and set(before) == set(obligations)
                     and all(before[ob] != _verification_digest(session, [ob])
                             for ob in obligations))
    adopt = native_adopt(session, [
        version, version, vocabulary.VERDICTS.get(verdict, 0),
        int(row.get("observed_generation", 0)),
        int(row.get("canonical_generation", 0)), 1 if proof_renewed else 0])
    if not proof_renewed:
        return {"adopted": False, "detail": "awaiting-verdict" if verdict == "unknown" else "awaiting-post-repair-verdict"}
    if verdict == "failed":
        candidate = dict(row)
        candidate["consumer_class"] = "semantic_required"
        candidate["repair_state"] = "applied_fail"
        candidate["detail"] = "repair-verification-failed"
        ok, _ = _write_row(session, row_id, version + 1, candidate)
        session._emit("family.escalated", "environment",
                      {"change": identity, "consumer": consumer,
                       "detail": "repair-verification-failed"})
        return {"adopted": False, "detail": "repair-verification-failed",
                "recorded": ok}
    if verdict != "passed":
        return {"adopted": False, "detail": "awaiting-verdict"}
    if adopt != 0:
        return {"adopted": False, "detail": "adopt-refused"}
    candidate = dict(row)
    candidate["observed_generation"] = int(row.get(
        "canonical_generation", 0))
    candidate["consumer_class"] = "current"
    candidate["repair_state"] = "applied_pass"
    candidate["detail"] = ""
    ok, latest = _write_row(session, row_id, version + 1, candidate)
    if not ok:
        # Lost the race: if the winner converged, observe it.
        if latest is not None and str(latest.get(
                "consumer_class")) == "current":
            return {"adopted": True, "detail": "observed-winner"}
        return {"adopted": False, "detail": "row-raced"}
    session._emit("family.converged", "environment",
                  {"change": identity, "consumer": consumer})
    return {"adopted": True, "detail": ""}


def _backoff_eligible(session, row_id: str, attempts: int) -> int:
    try:
        backoff = session.snapshot.get("family_backoff") or {}
    except AttributeError:
        return 1
    last = float(backoff.get(row_id, 0)) if isinstance(backoff, dict) else 0
    return 1 if (time.time() - last) >= REVISIT_BACKOFF_BASE_SECS * (
        2 ** min(attempts, 4)) else 0


def _note_backoff(session, row_id: str) -> None:
    try:
        backoff = dict(session.snapshot.get("family_backoff") or {})
    except AttributeError:
        return
    backoff[row_id] = time.time()
    pruned = dict(list(backoff.items())[-64:])
    session.snapshot["family_backoff"] = pruned


def revisit_deferred(session, identity: str,
                     consumer: str) -> dict[str, Any]:
    """Reconsider one deferred row; native law decides eligibility."""
    row_id = reconciliation_row_id(session, identity, consumer)
    found = _read_row(session, row_id)
    if found is None:
        return {"reconsidered": False, "detail": "row-missing"}
    row = found[1]
    detail = str(row.get("detail", ""))
    reason = {"replan": 0, "claim-required": 1, "occupied": 2,
              "provider-unavailable": 3, "awaiting-verdict": 4,
              "change-superseded": 0, "claim-lost": 1,
              "preimage-changed": 0}.get(detail, 4)
    if str(row.get("consumer_class")) in ("current", "semantic_required",
                                          "incompatible"):
        return {"reconsidered": False, "detail": "not-deferred"}
    if str(row.get("repair_state")) == "applied_unknown":
        return adopt_pending(session, identity, consumer)
    due = native_revisit_due(session, [
        reason, int(row.get("attempts", 0)), MAX_REPAIR_ATTEMPTS,
        _backoff_eligible(session, row_id, int(row.get("attempts", 0)))])
    if due is None:
        return {"reconsidered": False, "detail": "toolchain-unavailable"}
    if not due:
        return {"reconsidered": False, "detail": "backoff"}
    _note_backoff(session, row_id)
    change = read_change(session, identity)
    if change is None:
        return {"reconsidered": False, "detail": "change-missing"}
    classification = classify_drift(session, change, consumer)
    record_drift(session, change, classification)
    return {"reconsidered": True,
            "consumer_class": classification["consumer_class"]}


# --- observation + ambient pass --------------------------------------------------------

def observe_changes(session) -> list[dict[str, Any]]:
    """Established + draft changes relevant to the selected workspace."""
    paths = _selected_checkout_paths(session)
    selected = set(paths)
    changes = []
    for row_id in list_row_ids(session, CHANGE_ROW_PREFIX)[
            :MAX_CHANGES_OBSERVED]:
        found = _read_row(session, row_id)
        if found is None:
            continue
        row = found[1]
        identity = str(row.get("identity", ""))
        producer = (row.get("producer") or {}).get("repository", "")
        record = read_change(session, identity)
        if record is None:
            continue
        state = str(record.get("state", "draft"))
        if state in ("superseded", "abandoned", "invalid", "published"):
            continue
        consumers = dependency_consumers(
            session, producer, record.get("contracts_changed", []) or [])
        relevant = producer in selected or any(
            consumer in selected for consumer in consumers)
        if not relevant:
            continue
        changes.append({"identity": identity, "state": state,
                        "producer": producer, "consumers": consumers,
                        "record": record})
    return changes


def ambient_pass(session, *, mode: str = "ambient",
                 converge_repairs: bool = True) -> dict[str, Any]:
    """Observe family state, classify drift, revisit, converge (bounded).

    Read-only observation always runs. Repair converges only inside
    this session's own claimed checkouts, at most two per pass, and
    never on a change first observed in this same pass.
    """
    started = utcnow()
    clock_started = time.monotonic()
    vocabulary_available = family_change_module(session) is not None
    summary: dict[str, Any] = {
        "observed_changes": 0, "relevant": 0, "reconciled": 0,
        "deferred": 0, "escalated": 0, "attention": [],
        "contributors": 0, "epoch_reused": False,
        "elapsed_seconds": 0.0,
    }
    if not vocabulary_available:
        summary["detail"] = "vocabulary-unavailable"
        return {"summary": summary, "reused": False, "started_at": started}
    try:
        produced = session.snapshot.get("family_produced") or []
    except AttributeError:
        produced = []
    active = []
    for item in produced:
        if not isinstance(item, str):
            continue
        record = read_change(session, item)
        state = str((record or {}).get("state", "draft"))
        if state not in ("superseded", "abandoned", "invalid", "published"):
            active.append(item)
    try:
        session.snapshot["family_produced"] = active[-32:]
    except AttributeError:
        pass
    publish_presence(session, active[:16])
    contributors = read_contributors(session)
    summary["contributors"] = len(contributors)
    changes = observe_changes(session)
    summary["observed_changes"] = len(changes)
    epoch_key, revisit_due = _observation_epoch(session, changes, contributors)
    try:
        stored = session.snapshot.get("family_epoch") or {}
    except AttributeError:
        stored = {}
    if (not revisit_due and stored.get("key") == epoch_key
            and isinstance(stored.get("summary"), dict)):
        cached = dict(stored["summary"])
        cached["contributors"] = len(contributors)
        cached["epoch_reused"] = True
        cached["elapsed_seconds"] = round(time.monotonic() - clock_started, 3)
        try:
            session.snapshot["family_last_summary"] = cached
        except AttributeError:
            pass
        return {"summary": cached, "reused": True, "started_at": started}
    selected = set(_selected_checkout_paths(session))
    repairs_this_pass = 0
    for entry in changes:
        identity = entry["identity"]
        record, state = entry["record"], entry["state"]
        if state != "established":
            # Drafts are visible, never authoritative.
            summary["relevant"] += 1
            continue
        consumers = [consumer for consumer in entry["consumers"] if consumer in selected]
        observations = [_drift_observation(session, record, consumer) for consumer in consumers]
        codes = native_classify_batch(session, [item["facts"] for item in observations])
        for consumer, observation, code in zip(consumers, observations, codes):
            summary["relevant"] += 1
            row_id = reconciliation_row_id(session, identity, consumer)
            row_known = _read_row(session, row_id) is not None
            classification = _classified_observation(session, observation, code)
            record_drift(session, record, classification)
            consumer_class = classification["consumer_class"]
            if consumer_class in ("semantic_required", "incompatible",
                                  "unknown"):
                summary["escalated"] += 1
                attention = summary["attention"]
                if len(attention) < MAX_CAPSULE_ATTENTION:
                    attention.append({"change": identity,
                                      "consumer": consumer,
                                      "class": consumer_class})
            elif consumer_class in ("occupied", "blocked",
                                    "pending_verification"):
                summary["deferred"] += 1
                revisit_deferred(session, identity, consumer)
            elif consumer_class == "reconcilable":
                if not row_known and mode == "ambient":
                    # First sight: capsule visibility now, repair next pass.
                    summary["deferred"] += 1
                    continue
                if converge_repairs and repairs_this_pass < 2 and \
                        _own_covering_claim(session, consumer) is not None:
                    repairs_this_pass += 1
                    outcome = converge(session, identity, consumer)
                    if outcome.get("converged"):
                        summary["reconciled"] += 1
                    elif outcome.get("disposition") == "escalated":
                        summary["escalated"] += 1
                    else:
                        summary["deferred"] += 1
                else:
                    summary["deferred"] += 1
    # Adopt repairs whose verification has since landed.
    for row_id in list_row_ids(session, RECON_ROW_PREFIX):
        found = _read_row(session, row_id)
        if found is None:
            continue
        row = found[1]
        if not _selected_reconciliation(session, row_id, row):
            continue
        if str(row.get("repair_state")) != "applied_unknown":
            continue
        consumer = str(row.get("consumer", ""))
        if consumer not in selected:
            continue
        adopt_pending(session, str(row.get("change", "")), consumer)
    post_key, _ = _observation_epoch(
        session, observe_changes(session), read_contributors(session))
    try:
        session.snapshot["family_cursor"] = {
            "cursor": read_generation(session)[1].get("cursor", 0),
            "observed_at": utcnow()}
        session.snapshot["family_last_summary"] = summary
        session.snapshot["family_epoch"] = {"key": post_key,
                                            "summary": dict(summary)}
    except AttributeError:
        pass
    summary["elapsed_seconds"] = round(time.monotonic() - clock_started, 3)
    try:
        session.snapshot["family_epoch"]["summary"] = dict(summary)
    except (AttributeError, KeyError, TypeError):
        pass
    # Persist a changed epoch once. Without this boundary, a fresh entry
    # process reconstructs and reclassifies even though shared rows are current.
    session._save()
    return {"summary": summary, "reused": False, "started_at": started}


def _observation_epoch(session, changes, contributors) -> tuple[str, bool]:
    """Cheap epoch key over observed family state (no native calls).

    Returns (key, revisit_due). A revisit whose backoff has expired
    forces a full pass even when the key is unchanged.
    """
    import hashlib
    parts: list[str] = []
    try:
        _, generation = read_generation(session)
        parts.append("gen=%s" % generation.get("cursor", 0))
    except FamilyError:
        parts.append("gen=?")
    for entry in sorted(changes, key=lambda item: item["identity"]):
        record = entry.get("record") or {}
        parts.append("%s:%s:%s" % (
            entry["identity"], entry.get("state", "?"),
            record.get("established_generation", "?")))
    selected = set(_selected_checkout_paths(session))
    revisit_due = False
    recon: list[str] = []
    for row_id in sorted(list_row_ids(session, RECON_ROW_PREFIX)):
        found = _read_row(session, row_id)
        if found is None:
            continue
        version, row = found
        if not _selected_reconciliation(session, row_id, row):
            continue
        if str(row.get("consumer", "")) not in selected:
            continue
        recon.append("%s:v%s:%s:%s" % (
            row_id, version, row.get("consumer_class", "?"),
            row.get("repair_state", "?")))
        due_at = str(row.get("next_revisit_at", ""))
        if due_at and due_at <= utcnow():
            revisit_due = True
        if str(row.get("repair_state", "")) == "applied_unknown":
            # Adoption depends only on the owning verdicts; fold them
            # into the key so an unchanged verdict does not bust the
            # epoch, while a landed verdict invalidates immediately.
            change = read_change(session, str(row.get("change", "")))
            obligations = [str(ob) for ob in
                           ((change or {}).get("verification") or {}).get(
                               "obligations", [])]
            recon.append("verdict=%s:%s" % (
                _verification_verdict(session, obligations),
                _verification_digest(session, obligations)))
    parts.append("recon=[%s]" % ",".join(recon))
    # Foreign claim release and transfer are wake inputs too. Observing only
    # our own covering claim leaves an occupied consumer cached indefinitely.
    live_claims = _live_claims(session)
    claims = sorted((key, item.get("version"), item.get("session_id"),
                     item.get("scope")) for key, item in live_claims.items()
                    if item.get("repository") in selected)
    parts.append("claims=" + json.dumps(claims, sort_keys=True))
    sessions = sorted(str(item.get("session", "?")) for item in contributors)
    parts.append("contributors=[%s]" % ",".join(sessions))
    digest = hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:32]
    return digest, revisit_due


def capsule(session) -> dict[str, Any]:
    """Tiny relevant collaboration capsule for agent context."""
    try:
        last = session.snapshot.get("family_last_summary") or {}
    except AttributeError:
        last = {}
    _, generation = read_generation(session)
    return {
        "generation": int(generation.get("cursor", 0)),
        "contributors": int(last.get("contributors", 0)),
        "relevant_changes": int(last.get("relevant", 0)),
        "reconciled": int(last.get("reconciled", 0)),
        "attention": list(last.get("attention", []))[:MAX_CAPSULE_ATTENTION],
    }
