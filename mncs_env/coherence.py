"""Event/observation transport and owner-pass composition.

Automation classifies affected passes. Owner passes retain every domain gate.
Repository catalogues and executable byte identities are observation inputs,
not replacements for semantic generations, verdicts or build-origin proof.
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from . import observations, sources, workspace
from .identity import digest_hex
from .persist import write_json

SCHEMA = "mncs.environment.incremental-coherence/1"
INPUTS_PATH = Path(__file__).resolve().parents[1] / ".mncs/coherence-passes.json"
MAX_WAVES = 3


def declarations() -> dict:
    result = json.loads(INPUTS_PATH.read_text())
    if result.get("schema_version") != "mncs.environment.coherence-pass-inputs/1":
        raise ValueError("invalid coherence input declaration")
    if len(result["passes"]) > 16 or len(result["facts"]) > 32 or len(set(result["facts"])) != len(result["facts"]):
        raise ValueError("coherence declaration exceeds native bounds")
    for indices in result["passes"].values():
        if len(set(indices)) != len(indices) or any(type(i) is not int or not 0 <= i < len(result["facts"]) for i in indices):
            raise ValueError("invalid coherence input subscription")
    return result


def _roots(session) -> dict[str, Path]:
    selected = session.snapshot.get("selected_checkouts") or {}
    root = Path(session.snapshot["workspace"]["root"])
    if selected:
        return {name: (root / record["path"]).resolve() for name, record in selected.items()
                if isinstance(record, dict) and record.get("path")}
    return {record["name"]: Path(record["path"]).resolve()
            for record in session.snapshot["workspace"].get("repositories", [])}


def _native(session, function: str, rows: list[list[int]]) -> tuple[list[int], dict]:
    from .family import find_mncs_binary
    selected = _roots(session)
    root = os.environ.get("MNCS_AUTOMATION_ROOT") or selected.get("mncs-automation")
    if root is None:
        raise ValueError("selected native Automation coherence provider unavailable")
    root = Path(root)
    source = root / "native/mncs/automation/coherence.mncs"
    binary = find_mncs_binary(session)
    if not binary or not source.is_file():
        raise ValueError("native Automation coherence artifact unavailable")
    args = [{"sequence": {"values": [{"sequence": {"values": [
        {"integer": {"value": int(value)}} for value in row]}} for row in rows]}}]
    command = [binary, "call", str(source), "--module", "mncs.automation.coherence.v1",
               "--function", function, "--args-json", json.dumps(args),
               "--library", str(root / "native"), "--cache-dir",
               str(session.state_dir / "provider-cache" / "automation-coherence")]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    document = json.loads(result.stdout)
    if result.returncode or document.get("status") != "returned":
        raise ValueError("native Automation coherence decision unavailable")
    values = document["call"]["returned"][0]["sequence"]["values"]
    codes = [int(value["integer"]["value"]) for value in values]
    admitted = {0, 1, 2, 3} if function == "route_batch" else {1, 2, 13}
    if len(codes) != len(rows) or any(code not in admitted for code in codes):
        raise ValueError("incomplete native coherence batch")
    receipt = {key: document["call"].get(key) for key in
               ("artifact_identity", "artifact_sha256", "backend")}
    return codes, receipt


def _policy_identity(session, prior: dict) -> tuple[str, dict]:
    roots = _roots(session)
    automation = os.environ.get("MNCS_AUTOMATION_ROOT") or roots.get("mncs-automation")
    # Entry executes these host adapters in this package. An implementation
    # change cannot reuse results classified by the previous adapter code.
    files = [INPUTS_PATH, *sorted(Path(__file__).parent.glob("*.py"))]
    if automation:
        files.extend(Path(automation) / "native/mncs/automation" / name
                     for name in ("coherence.mncs", "revisit.mncs"))
    from .family import find_mncs_binary
    binary = find_mncs_binary(session)
    if binary:
        files.append(Path(binary))
    artifacts = {str(path): observations.observe_artifact(path, prior.get(str(path))) for path in files}
    return digest_hex(artifacts), artifacts


def _runtime_due(session, definition: dict) -> bool:
    # A provider without an admitted invalidation declaration still needs live
    # observation. Only the checkout-observation service is static today.
    return any(service.get("observation_inputs") != ["selected-repositories"]
               for service in definition.get("services", []))


def _event_mask(events: list[dict], spec: dict) -> int:
    mask = 0
    vocabulary = {name: 1 << index for index, name in enumerate(spec["facts"])}
    for event in events:
        mask |= vocabulary.get(event["kind"], 0)
    return mask


def _file_events(session, events: list[dict]) -> list[dict]:
    files = [event for event in events if event["kind"] == "file.changed"]
    rows = []
    for event in files:
        name = event["path"]
        path = Path(name)
        declared = name in ("family-semantic-contracts-v1.json", "stdlib-manifest.json",
                            "dist/stdlib-bundle.json") or name.startswith(".mncs/")
        rows.append([int(declared), int(path.suffix in (".md", ".txt", ".rst"))])
    # Classification is native; the host supplies filesystem spelling facts.
    codes = []
    for offset in range(0, len(rows), 16):
        batch, _ = _native(session, "classify_files", rows[offset:offset + 16])
        codes.extend(batch)
    names = {1: "declaration.changed", 2: "source.changed", 13: "prose.changed"}
    return [{**event, "kind": names[code]} for event, code in zip(files, codes)] + [
        event for event in events if event["kind"] != "file.changed"]


def _store_events(session, prior: dict) -> tuple[list[dict], str | None, bool]:
    if not callable(getattr(session.store, "generation", None)):
        # Explicit debug backend has no commit stream: read its bounded rows.
        material = {"claims": session.store.read_claims(),
                    "rows": session.store.read_projection_versions()}
        cursor = digest_hex(material)
        changed = prior.get("file_cursor") not in (None, cursor)
        return ([{"kind": "claim.changed"}, {"kind": "family.changed"}] if changed else [], cursor, True)
    result = sources.StoreReplaySource(session.store, session.session_id + ":").observe(prior.get("store_cursor"))
    if result.status != "ok":
        return [], result.cursor, False
    selected = set(_roots(session))
    events = []
    for item in result.events:
        if item.kind == "claim.changed":
            repository = item.relations.get("claim")
            if repository in selected:
                events.append({"kind": item.kind, "repository": repository})
        elif item.kind == "store.changed":
            identity = item.provenance.get("domain_identity", "")
            if identity.startswith("family:"):
                events.append({"kind": "family.changed", "subject": identity})
            elif identity.startswith("entry:index/"):
                continue
            else:
                # Unknown shared writes cannot authorize reuse. Bound the
                # reconciliation rather than inventing their domain meaning.
                return events, result.cursor, False
    return events, result.cursor, True


def _observe(session, prior: dict) -> tuple[dict, list[dict], bool, dict]:
    repositories, events, known = {}, [], True
    metrics = {"metadata_paths": 0, "enumerated_repositories": [], "errors": []}
    for name, path in _roots(session).items():
        try:
            current, changed, counts = observations.observe_repository(
                session, path, prior.get("repositories", {}).get(name))
            repositories[name] = current
            events.extend({**event, "repository": name} for event in changed)
            metrics["metadata_paths"] += counts["metadata_paths"]
            if counts["enumerated"]:
                metrics["enumerated_repositories"].append(name)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            known = False
            metrics["errors"].append({"repository": name, "reason": str(error)[:200]})
            # A missing/corrupt derived catalogue is recoverable, never trusted.
            current, _, counts = observations.observe_repository(session, path, None)
            repositories[name] = current
            metrics["enumerated_repositories"].append(name)
    artifacts = {}
    paths = {Path(sys.executable)}
    for binding in session.snapshot.get("bindings", []):
        for value in (binding.get("address"), binding.get("toolchain_address")):
            if isinstance(value, str):
                path = value.removeprefix("python:")
                if Path(path).is_absolute():
                    paths.add(Path(path))
    toolchain = session.snapshot.get("toolchain") or {}
    if toolchain.get("binary"):
        paths.add(Path(toolchain["binary"]))
        paths.add(Path(toolchain["binary"]).parent / "libmncs_embed.so")
    for path in sorted(paths):
        key = str(path)
        artifact = observations.observe_artifact(path, prior.get("artifacts", {}).get(key))
        artifacts[key] = artifact
        if prior.get("artifacts") and artifact != prior["artifacts"].get(key):
            events.append({"kind": "provider.artifact_changed", "artifact": key})
    from .toolchain import language_library_for
    libraries = {}
    binary = toolchain.get("binary")
    library = language_library_for(binary, session) if binary else None
    if library:
        key = str(library.resolve())
        previous = prior.get("libraries", {}).get(key)
        try:
            libraries[key] = observations.observe_library(session, library, previous)
        except (OSError, ValueError, KeyError):
            known = False
            libraries[key] = observations.observe_library(session, library)
        declarations = {}
        for relative in (".mncs/project.json", "stdlib-manifest.json", "dist/stdlib-bundle.json"):
            path = library.parent / relative
            declarations[relative] = observations.observe_artifact(
                path, (previous or {}).get("provider_declarations", {}).get(relative))
        libraries[key]["provider_declarations"] = declarations
        if prior.get("libraries") and (previous is None or previous.get("content_identity") != libraries[key]["content_identity"]
                                      or previous.get("provider_declarations") != declarations):
            events.append({"kind": "provider.artifact_changed", "artifact": key})
    if prior.get("libraries") and set(prior["libraries"]) != set(libraries):
        events.append({"kind": "provider.artifact_changed", "reason": "effective library roots changed"})
    observed = {"repositories": repositories, "artifacts": artifacts, "libraries": libraries}
    return observed, events, known, metrics


def _reuse(block):
    if block is None:
        return None
    block = copy.deepcopy(block)
    block["reused"] = True
    if "revalidation" in block:
        block["revalidation"] = {"changed": [], "reprobed": 0, "removed": []}
        block["operations"] = []
    if isinstance(block.get("summary"), dict):
        block["summary"]["epoch_reused"] = True
        if "elapsed_seconds" in block["summary"]:
            block["summary"]["elapsed_seconds"] = 0.0
    if "elapsed_seconds" in block:
        block["elapsed_seconds"] = 0.0
    return block


def _domain_signature(value):
    # Transport timestamps do not change a provider's observed meaning.
    def material(item):
        if isinstance(item, dict):
            return {key: material(value) for key, value in item.items()
                    if key not in ("observed_at", "elapsed_seconds", "recorded_at", "updated_at")}
        if isinstance(item, list):
            return [material(value) for value in item]
        return item
    return digest_hex(material(value))


def _result_refs(session, results):
    refs = {}
    for name, block in results.items():
        identity = digest_hex(block, length=64)
        path = session.state_dir / "sessions" / session.session_id / "coherence-results" / (identity + ".json")
        try:
            current = json.loads(path.read_text())
        except (OSError, ValueError):
            current = None
        if current != {"block": block}:
            write_json(path, {"block": block})
        refs[name] = {"identity": identity, "artifact": str(path)}
    return refs


def _load_results(session, refs):
    results, known = {}, True
    expected = session.state_dir / "sessions" / session.session_id / "coherence-results"
    for name, ref in refs.items():
        try:
            path = Path(ref["artifact"])
            if not path.resolve().is_relative_to(expected.resolve()):
                raise ValueError("coherence result escapes session artifacts")
            block = json.loads(path.read_text())["block"]
            if digest_hex(block, length=64) != ref["identity"]:
                raise ValueError("corrupt coherence result")
            results[name] = block
        except (OSError, ValueError, KeyError, TypeError):
            known = False
    return results, known


def tick(session, definition: dict, runners: dict, *, fresh: bool = False,
         now_ms: int | None = None) -> tuple[dict, dict]:
    # Without the native scheduler provider, retain explicit owner recovery.
    # No host policy mirror and no durable "current" receipt is fabricated.
    automation = os.environ.get("MNCS_AUTOMATION_ROOT") or _roots(session).get("mncs-automation")
    if not automation or not (Path(automation) / "native/mncs/automation/coherence.mncs").is_file():
        return {name: run() for name, run in runners.items()}, {
            "mode": "recovery_scan", "reason": "native coherence provider unavailable"}
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    spec = declarations()
    previous = session.snapshot.get("coherence") or {}
    if previous.get("schema_version") != SCHEMA:
        previous = {}
    observed, events, known, metrics = _observe(session, previous)
    store_events, cursor, store_known = _store_events(session, previous)
    events.extend(store_events)
    known = known and store_known and (not previous or previous.get("stable") is True)
    policy, policy_artifacts = _policy_identity(session, previous.get("policy_artifacts", {}))
    if previous and previous.get("policy_identity") != policy:
        known = False
    deadlines = dict(previous.get("deadlines") or {})
    expired = {name for name, value in deadlines.items() if now_ms >= value}
    report = {"mode": "bootstrap_scan" if not previous else "targeted_update",
              "events": events[:32], "scheduled": [], "skipped": [], **metrics}
    results, results_known = _load_results(session, previous.get("result_refs") or {})
    known = known and results_known
    names = list(spec["passes"])
    if set(names) != set(runners):
        raise ValueError("coherence runners must match the declared owner passes")
    try:
        events = _file_events(session, events)
        changes = _event_mask(events, spec)
        rows = [[int(name in results), int(known),
                 sum(1 << index for index in spec["passes"][name]), changes, 1, 0, 0,
                 int(name in expired or (name == "doctor" and _runtime_due(session, definition)))]
                for name in names]
        request = digest_hex({"policy": policy, "facts": rows})
        if request == previous.get("current_request") and known:
            codes, receipt = previous["current_codes"], previous.get("policy_receipt", {})
        else:
            codes, receipt = _native(session, "route_batch", rows)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        codes, receipt = [2] * len(names), {}
        report["mode"] = "bounded_reconciliation"
        known = False
    if any(code == 2 for code in codes):
        report["mode"] = "bounded_reconciliation"
    scheduled = set()
    derived = {"doctor": ("service_observations", "provider.changed"),
               "semantics": ("semantics", "semantic.changed"),
               "actions": ("actions_state", "external-evidence.changed"),
               "verification": ("verification_state", "verification.changed"),
               "diagnostics": ("diagnostic_state", "diagnostic.changed")}
    pending = dict(zip(names, codes))
    for wave in range(MAX_WAVES):
        emitted = []
        for name in names:
            code = pending.get(name, 0)
            if not code or name in scheduled:
                continue
            key, kind = derived.get(name, (None, None))
            before = _domain_signature(session.snapshot.get(key)) if key else None
            results[name] = runners[name]()
            scheduled.add(name)
            deadlines.pop(name, None)
            if name == "doctor":
                leases = [service.get("observation_max_age_ms") for service in definition.get("services", [])]
                if any(value is not None for value in leases):
                    deadlines[name] = now_ms + min(value for value in leases if value is not None)
            if name == "actions":
                summary = (results[name] or {}).get("summary", {})
                if summary.get("pending") or summary.get("blockers") or summary.get("eligible"):
                    # Poll only this external boundary when no receipt feed exists.
                    deadlines[name] = now_ms + 60_000
            if name == "family":
                from .family import scheduler_deadlines
                values = scheduler_deadlines(session)
                if values:
                    deadlines[name] = min(int(datetime.fromisoformat(value).timestamp() * 1000) for value in values)
            report["scheduled"].append({"pass": name, "disposition": code})
            if key and before != _domain_signature(session.snapshot.get(key)):
                emitted.append({"kind": kind, "producer": name})
        if not emitted or len(scheduled) == len(names):
            break
        rows = [[int(name in results), 1, sum(1 << i for i in spec["passes"][name]),
                 _event_mask(emitted, spec), int(name not in scheduled), 0, 0, 0] for name in names]
        next_codes, _ = _native(session, "route_batch", rows)
        pending = dict(zip(names, next_codes))
        report.setdefault("derived_events", []).extend(emitted)
    report["skipped"] = [{"pass": name, "reason": "current inputs"} for name in names if name not in scheduled]
    # Observation and effects are two phases. A moving checkout cannot be
    # certified current using an after-the-fact catalogue of unseen edits.
    after, intervening, after_known, _ = _observe(session, observed)
    # A replay, rather than sampling the latest generation, can acknowledge
    # our own effects without discarding a publication racing those effects.
    if callable(getattr(session.store, "generation", None)) and report["scheduled"]:
        raced, checked_cursor, checked_known = _store_events(session, {"store_cursor": cursor})
        if checked_known and not raced:
            cursor = checked_cursor
        else:
            after_known = False
            intervening.extend(raced)
    stable = after_known and not intervening
    if not stable:
        report["mode"] = "bounded_reconciliation"
        report["pending_events"] = intervening[:32]
    if report["scheduled"] or events or previous.get("repositories") != observed["repositories"]:
        state = {"schema_version": SCHEMA, **observed, "result_refs": _result_refs(session, results),
                 "policy_identity": policy, "policy_artifacts": policy_artifacts,
                 "policy_receipt": receipt, "last_trace": report, "deadlines": deadlines,
                 "stable": stable, "store_cursor": cursor,
                 "file_cursor": cursor}
        # Keep the replay cursor captured BEFORE effects. Own writes are
        # filtered by the feed. A concurrent peer publication must be replayed
        # on the next tick, including publications during a long owner pass.
        current_rows = [[1, int(state["stable"]), sum(1 << index for index in spec["passes"][name]),
                         0, 1, 0, 0, 0] for name in names]
        try:
            current_codes, current_receipt = _native(session, "route_batch", current_rows)
            state.update(current_codes=current_codes,
                         current_request=digest_hex({"policy": policy, "facts": current_rows}),
                         policy_receipt=current_receipt)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            state["stable"] = False
        session.snapshot["coherence"] = state
        session._save()
    report["events"] = events[:32]
    blocks = {name: results[name] if any(item["pass"] == name for item in report["scheduled"])
              else _reuse(results.get(name)) for name in names}
    return blocks, report
