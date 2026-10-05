"""Event/observation transport and owner-pass composition.

Automation classifies affected passes. Owner passes retain every domain gate.
Repository catalogues and executable byte identities are observation inputs,
not replacements for semantic generations, verdicts or build-origin proof.
"""
from __future__ import annotations

import copy
import importlib.util
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


_resident_adapters = {}


def _resident_provider(session, root, binary):
    """Load only the selected owner's declared resident transport."""
    manifest = json.loads((root / '.mncs/project.json').read_text())
    declaration = next((item for item in manifest.get('contracts', {}).get('provides', [])
                        if item.get('contract') == 'automation-coherence-routing'), {})
    adapter = declaration.get('resident_adapter')
    if not adapter:
        return None
    roots = _roots(session)
    roots['mncs-automation'] = root
    specification = json.loads((root / declaration['artifact_declaration']).read_text())
    repositories = {item['repository'] for item in specification['inputs']}
    repositories.add(adapter['runtime_repository'])
    for repository in repositories:
        if repository not in roots:
            override = os.environ.get('MNCS_' + repository.removeprefix('mncs-').replace('-', '_').upper() + '_ROOT')
            if repository == 'MNCS-Commons':
                override = os.environ.get('MNCS_COMMONS_ROOT')
            if override:
                roots[repository] = Path(override).resolve()
    if not repositories.issubset(roots):
        return None  # older selected owners retain source-call recovery; no exact receipt
    path = (root / adapter['path']).resolve()
    runtime_root = roots[adapter['runtime_repository']]
    runtime_path = (runtime_root / adapter['runtime_path']).resolve()
    if not path.is_relative_to(root) or not runtime_path.is_relative_to(runtime_root):
        raise ValueError('resident adapter escapes selected provider')
    if str(runtime_path) not in sys.path:
        sys.path.insert(0, str(runtime_path))
    # Source changes create a fresh adapter, not a stale module singleton.
    key = (str(path), observations.observe_artifact(path)['artifact_identity'].removeprefix('sha256:'))
    if key not in _resident_adapters:
        spec = importlib.util.spec_from_file_location('mncs_owner_coherence_' + key[1], path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _resident_adapters[key] = module
    return _resident_adapters[key], adapter['function'], roots


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
    retained = _resident_provider(session, root, binary)
    if retained is not None:
        module, entrypoint, roots = retained
        # The canonical embed ABI includes integer types; source-call CLI
        # historically accepted omitted types as its own input shorthand.
        for argument in args:
            for row in argument['sequence']['values']:
                for value in row['sequence']['values']:
                    value['integer']['type'] = {'bits': 64, 'signed': False}
        try:
            returned, execution = getattr(module, entrypoint)(roots=roots, compiler=binary,
                embed=Path(binary).parent / 'libmncs_embed.so',
                cache=session.state_dir / 'provider-cache', function=function, arguments=args)
        except RuntimeError as error:
            raise ValueError('owner artifact reconcile/execution unavailable: ' + str(error)) from error
        if returned.get('status') != 'returned':
            raise ValueError('retained coherence call did not return')
        values = returned['returned'][0]['sequence']['values']
        codes = [int(value['integer']['value']) for value in values]
        admitted = {0, 1} if function == 'scoped_events' else {0, 1, 2, 3} if function == 'route_batch' else {0, 1, 2, 10, 13}
        if len(codes) != len(rows) or any(code not in admitted for code in codes):
            raise ValueError('incomplete retained coherence batch')
        if callable(getattr(session.store, 'put_record', None)):
            for record in (execution.get('build'), execution):
                if not isinstance(record, dict):
                    continue
                address = (session.session_id + ':provider-receipt:' + record['identity']).encode()
                schema = record['schema_version'].encode()
                existing = session.store.get_record(schema, address)
                if existing is None:
                    session.store.put_record(schema, address, record)
                elif existing != record:
                    raise ValueError('immutable owner receipt payload differs')
        return codes, execution
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    document = json.loads(result.stdout)
    if result.returncode or document.get("status") != "returned":
        raise ValueError("native Automation coherence decision unavailable")
    values = document["call"]["returned"][0]["sequence"]["values"]
    codes = [int(value["integer"]["value"]) for value in values]
    admitted = {0, 1} if function == "scoped_events" else {0, 1, 2, 3} if function == "route_batch" else {0, 1, 2, 10, 13}
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
        manifest = Path(automation) / '.mncs/project.json'
        files.append(manifest)
        if manifest.is_file():
            declarations = json.loads(manifest.read_text()).get('contracts', {}).get('provides', [])
            provider = next((item for item in declarations if item.get('contract') == 'automation-coherence-routing'), {})
            if provider.get('artifact_declaration'):
                declared = Path(automation) / provider['artifact_declaration']
                files.append(declared)
                specification = json.loads(declared.read_text())
                bound = dict(roots, **{'mncs-automation': Path(automation)})
                for repository, variable in [('MNCS-Commons', 'MNCS_COMMONS_ROOT'), ('mncs-doctor', 'MNCS_DOCTOR_ROOT'), ('mncs-forge', 'MNCS_FORGE_ROOT')]:
                    if os.environ.get(variable):
                        bound.setdefault(repository, Path(os.environ[variable]))
                files.extend(bound[item['repository']] / item['path'] for item in specification['inputs']
                             if item['repository'] in bound)
                if 'mncs-forge' in bound:
                    files.extend(bound['mncs-forge'] / 'src/mncs_forge' / name for name in
                                 ('provider_artifacts.py', 'retained_embed.py'))
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
    covered = {item.get('service_identity') for item in _streams(session, definition)}
    return any(service.get("observation_inputs") != ["selected-repositories"] and service.get('identity') not in covered
               for service in definition.get("services", []))


def _event_mask(events: list[dict], spec: dict) -> int:
    mask = 0
    vocabulary = {name: 1 << index for index, name in enumerate(spec["facts"])}
    for event in events:
        mask |= vocabulary.get(event["kind"], 0)
    return mask


def _file_events(session, events: list[dict]) -> list[dict]:
    files = [event for event in events if event["kind"] == "file.changed"]
    if not files:
        return [event for event in events if event['kind'] != 'file.changed']
    from . import projections, projection_sources
    root = Path(session.snapshot['workspace']['root'])
    declarations, _ = projections.discover_selected_declarations(session, root)
    targets = {(d['checkout'], d['output']): d for d in declarations}
    rows = []
    for event in files:
        name = event["path"]
        path = Path(name)
        declared = name in ("family-semantic-contracts-v1.json", "stdlib-manifest.json",
                            "dist/stdlib-bundle.json") or name.startswith(".mncs/")
        projection = targets.get((event['checkout'], name))
        current = False
        if projection:
            shared = projections._read_shared_row(session, projection['id']) or {}
            observed = projection_sources.output_identity(Path(event['checkout']), projection)
            expected = (shared.get('source') or {}).get('owned_digest')
            if expected is None:
                expected = shared.get('rendered_digest')
            current = observed is not None and observed == expected
        rows.append([int(declared), int(path.suffix in (".md", ".txt", ".rst")),
                     int(projection is not None), int(current)])
    # Classification is native; the host supplies filesystem spelling facts.
    codes = []
    for offset in range(0, len(rows), 16):
        function = 'classify_projection_files' if targets else 'classify_files'
        batch_rows = rows[offset:offset + 16]
        if not targets:
            batch_rows = [row[:2] for row in batch_rows]
        batch, _ = _native(session, function, batch_rows)
        codes.extend(batch)
    names = {1: "declaration.changed", 2: "source.changed", 10: 'projection.changed', 13: "prose.changed"}
    return [{**event, "kind": names[code]} for event, code in zip(files, codes) if code != 0] + [
        event for event in events if event["kind"] != "file.changed"]


def _store_events(session, prior: dict, definition: dict | None = None) -> tuple[list[dict], str | None, bool]:
    if not callable(getattr(session.store, "generation", None)):
        # Explicit debug backend has no commit stream: read its bounded rows.
        versions = dict(session.store.read_projection_versions())
        # Own contributor bookkeeping is not an invalidation of this session.
        # Peers still observe that row through their respective cursors.
        versions.pop('family:contributor/' + session.session_id, None)
        material = {"claims": session.store.read_claims(), "rows": versions}
        cursor = digest_hex(material)
        changed = prior.get("file_cursor") not in (None, cursor)
        return ([{"kind": "claim.changed"}, {"kind": "family.changed"}] if changed else [], cursor, True)
    result = sources.StoreReplaySource(session.store, session.session_id + ":").observe(prior.get("store_cursor"))
    if result.status == "reset":
        # A bounded Store replay gap is recoverable only after a complete
        # owner pass. Keep the durable cursor at its acknowledged value and
        # carry the observed high-water separately as a candidate.
        return ([{"kind": "store.reconcile_required", "candidate_cursor": result.cursor,
                  "reason": result.detail}], prior.get("store_cursor"), True)
    if result.status != "ok":
        return [], prior.get("store_cursor"), False
    selected = set(_roots(session))
    publications = (definition or {}).get('coherence_publications') or []
    events = []
    for item in result.events:
        if item.kind == "claim.changed":
            repository = item.relations.get("claim")
            if repository in selected:
                events.append({"kind": item.kind, "repository": repository})
        elif item.kind == "store.changed":
            identity = item.provenance.get("domain_identity", "")
            if identity.startswith('family:contributor/' + session.session_id + ':'):
                continue
            if identity.startswith("family:"):
                events.append({"kind": "family.changed", "subject": identity})
            elif identity.startswith("entry:index/"):
                continue
            else:
                declared = next((entry for entry in publications if entry['schema'] == item.provenance.get('domain_schema')), None)
                if declared:
                    row = session.store.get_record(declared['schema'].encode(), identity.encode())
                    if not isinstance(row, dict):
                        return events, result.cursor, False
                    repository = row.get(declared.get('repository_field', 'repository'))
                    if not isinstance(repository, str):
                        return events, result.cursor, False
                    if repository in selected:
                        events.append({'kind': declared['event'], 'repository': repository,
                            'subject': row.get(declared.get('subject_field', 'subject')), 'identity': item.identity})
                    continue
                # Unknown shared writes cannot authorize reuse. Bound the
                # reconciliation rather than inventing their domain meaning.
                return events, result.cursor, False
    return events, result.cursor, True


def _streams(session, definition):
    declared = list(definition.get('coherence_streams') or [])
    for observation in session.snapshot.get('service_observations', []):
        transport = (observation.get('provider_observed') or {}).get('event_transport')
        if isinstance(transport, dict):
            declared.append(dict(transport, service_identity=observation['identity']))
    # One socket/stream even when both the definition and probe declare it.
    return list({item['identity']: item for item in declared}.values())


def _semantic_events(session, prior, definition):
    cursors = dict(prior.get('semantic_cursors') or {})
    events, known = [], True
    for declared in _streams(session, definition)[:8]:
        if declared.get('protocol') != 'mncs.workspace-event-cursor/2':
            known = False
            continue
        identity = declared['identity']
        source = sources.LanguageServiceSource(declared['socket'])
        result = source.observe(cursors.get(identity))
        if result.status != 'ok':
            known = False
            events.append({'kind': 'provider.changed', 'provider': declared.get('provider'),
                           'stream': identity, 'reason': result.detail, 'reset': result.status == 'reset'})
        else:
            for item in result.events:
                for observation in session.snapshot.get('service_observations', []):
                    if observation.get('identity') == declared.get('service_identity'):
                        observed = observation.setdefault('provider_observed', {})
                        observed.update(generation=item.generation, stream_identity=item.stream, event_cursor=item.provenance['cursor'])
                events.append({'kind': item.kind, 'identity': item.identity,
                    'provider': declared.get('provider'), 'stream': identity,
                    'subject': item.subject, 'generation': item.generation,
                    'provenance': item.provenance, **item.payload})
        # On reset acknowledge no new epoch until a bounded pass has run.
        cursors[identity] = result.cursor
    return events, cursors, known


def _pass_masks(session, events, spec, definition):
    masks = {name: _event_mask(events, spec) for name in spec['passes']}
    subscriptions = definition.get('coherence_subscriptions') or {}
    for name, subscription in subscriptions.items():
        if name not in masks:
            raise ValueError('unknown coherence subscription owner')
        rows, considered = [], []
        for event in events:
            if event['kind'] != 'semantic.changed' or event.get('stream') != subscription.get('stream'):
                continue
            subjects = {item.get('identity') for item in event.get('semantic_subjects', []) if isinstance(item, dict)}
            obligations = event.get('obligations') or {}
            identities = set()
            for key in ('added', 'resolved', 'status_changed'):
                identities.update(item.get('identity') for item in obligations.get(key, []) if isinstance(item, dict))
            complete = bool(event.get('impact_complete')) and bool(obligations.get('complete')) and subscription.get('complete') is True
            rows.append([1, int(complete), int(bool(subjects & set(subscription.get('subjects', [])))),
                         int(bool(identities & set(subscription.get('obligations', []))))])
            considered.append(event)
        if rows:
            decisions = []
            for offset in range(0, len(rows), 16):
                codes, _ = _native(session, 'scoped_events', rows[offset:offset + 16])
                decisions.extend(codes)
            retained = [event for event in events if event not in considered]
            retained.extend(event for event, code in zip(considered, decisions) if code == 1)
            masks[name] = _event_mask(retained, spec)
    return masks


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
        for value in binding.get("fixed_env", {}).values():
            if isinstance(value, str) and Path(value).is_absolute() and Path(value).is_file():
                paths.add(Path(value))
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


def _owner_result_failed(value) -> bool:
    """Whether an owner response says its requested work did not complete.

    Domain verdicts such as a failed test remain completed evidence. Only the
    provider/invocation envelope blocks acknowledgement of the input event.
    Environment owner adapters use ``None`` to mean their documented quiet
    no-work case (for example, diagnostics has no failures to explain); that
    is a successful no-op, not an invocation failure.
    """
    if value is None:
        return False
    if not isinstance(value, dict):
        return True
    if value.get("ok") is False or isinstance(value.get("error"), str):
        return True
    status = str(value.get("status", "")).lower()
    if status in {"error", "unavailable", "not_run"}:
        return True
    invocation = value.get("invocation")
    return isinstance(invocation, dict) and str(invocation.get("status", "")).lower() in {
        "error", "unavailable", "not_run"
    }


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
    store_events, cursor, store_known = _store_events(session, previous, definition)
    store_reset = next((item for item in store_events
                        if item.get("kind") == "store.reconcile_required"), None)
    events.extend(item for item in store_events
                  if item.get("kind") != "store.reconcile_required")
    semantic_events, semantic_cursors, semantic_known = _semantic_events(session, previous, definition)
    prior_semantic_cursors = dict(previous.get("semantic_cursors") or {})
    reset_candidate_streams = {
        str(event.get("stream")) for event in semantic_events
        if event.get("kind") == "provider.changed" and event.get("reset")
        and event.get("stream")
        and semantic_cursors.get(str(event.get("stream"))) is not None
        and semantic_cursors.get(str(event.get("stream")))
            != prior_semantic_cursors.get(str(event.get("stream")))
    }
    events.extend(semantic_events)
    known = known and semantic_known and store_known and store_reset is None and (not previous or previous.get("stable") is True)
    policy, policy_artifacts = _policy_identity(session, previous.get("policy_artifacts", {}))
    if previous and previous.get("policy_identity") != policy:
        known = False
    deadlines = dict(previous.get("deadlines") or {})
    expired = {name for name, value in deadlines.items() if now_ms >= value}
    report = {"mode": "bootstrap_scan" if not previous else "targeted_update",
              "events": events[:32], "event_count": len(events), "scheduled": [], "skipped": [], **metrics}
    results, results_known = _load_results(session, previous.get("result_refs") or {})
    known = known and results_known
    names = list(spec["passes"])
    if set(names) != set(runners):
        raise ValueError("coherence runners must match the declared owner passes")
    rows = []
    try:
        events = _file_events(session, events)
        masks = _pass_masks(session, events, spec, definition)
        rows = [[int(name in results), int(known),
                 sum(1 << index for index in spec["passes"][name]), masks[name], 1, 0, 0,
                 int(name in expired or (name == "doctor" and _runtime_due(session, definition)))]
                for name in names]
        request = digest_hex({"policy": policy, "facts": rows})
        if request == previous.get("current_request") and known:
            codes, receipt = previous["current_codes"], previous.get("policy_receipt", {})
        else:
            codes, receipt = _native(session, "route_batch", rows)
        if store_reset is not None:
            # A Store cursor overflow has no safe selective interpretation.
            # Re-run all declared owners against their authoritative current
            # state before adopting the candidate high-water.
            codes = [2] * len(names)
            report["store_replay"] = {
                "disposition": "full_owner_reconciliation_required",
                "candidate_cursor": store_reset.get("candidate_cursor"),
                "reason": store_reset.get("reason", "bounded replay reset"),
            }
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
            report["scheduled"].append({"pass": name, "owner": name, "disposition": code, "wave": wave,
                "owner_reused": bool((results[name] or {}).get("reused", False)),
                "invalidated_input_mask": rows[names.index(name)][3] if rows else None,
                "timer_or_lifecycle_due": bool(rows[names.index(name)][7]) if rows else None,
                "inputs": [event["kind"] for event in events if _event_mask([event], spec) & sum(1 << i for i in spec["passes"][name])],
                "subjects": sorted({item.get("identity", "") for event in events for item in event.get("semantic_subjects", []) if isinstance(item, dict)})[:64]})
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
    owner_failures = sorted(
        name for name in scheduled if _owner_result_failed(results.get(name))
    )
    if store_reset is not None and (scheduled != set(names) or owner_failures):
        after_known = False
        report["store_replay"]["disposition"] = "reconciliation_incomplete_or_store_advanced"
        if owner_failures:
            report["store_replay"]["owner_failures"] = owner_failures
    elif not store_known:
        after_known = False
    if semantic_events and owner_failures:
        # The LS cursor is a single stream acknowledgement shared by all
        # subscribed owners. Keep the prior durable cursor until every owner
        # selected for this semantic batch has completed its invocation. A
        # later tick replays the same source window and retries the failed
        # owner; successful owners are expected to be idempotent.
        semantic_cursors = dict(previous.get("semantic_cursors") or {})
        semantic_known = False
        report["owner_failures"] = owner_failures
        report["mode"] = "bounded_reconciliation"
        report["cursor_disposition"] = "held_for_owner_retry"
    elif reset_candidate_streams and scheduled == set(names):
        # The provider reported that its bounded event window no longer
        # contains the prior cursor. Only a complete successful owner pass
        # can adopt the exact high-water candidate returned with that reset.
        semantic_known = True
        report["cursor_disposition"] = "reconciled_and_advanced"
    # Observation and effects are two phases. A moving checkout cannot be
    # certified current using an after-the-fact catalogue of unseen edits.
    after, intervening, after_known, _ = _observe(session, observed)
    # A replay, rather than sampling the latest generation, can acknowledge
    # our own effects without discarding a publication racing those effects.
    if callable(getattr(session.store, "generation", None)) and report["scheduled"]:
        raced, checked_cursor, checked_known = _store_events(session, {"store_cursor": cursor}, definition)
        raced_reset = next((item for item in raced
                            if item.get("kind") == "store.reconcile_required"), None)
        if (store_reset is not None and raced_reset is not None
                and checked_known
                and raced_reset.get("candidate_cursor") == store_reset.get("candidate_cursor")
                and scheduled == set(names) and not owner_failures):
            cursor = store_reset.get("candidate_cursor")
            report["store_replay"]["disposition"] = "full_owner_reconciliation_complete"
            report["cursor_disposition"] = "store_reconciled_and_advanced"
        elif checked_known and not raced:
            cursor = checked_cursor
        else:
            after_known = False
            intervening.extend(item for item in raced
                               if item.get("kind") != "store.reconcile_required")
            if store_reset is not None:
                report["store_replay"]["disposition"] = "reconciliation_incomplete_or_store_advanced"
    stable = after_known and not intervening
    if not stable:
        report["mode"] = "bounded_reconciliation"
        report["pending_events"] = intervening[:32]
    effects = any(not (results.get(name) or {}).get('reused', False) for name in scheduled)
    if (effects or events or store_reset is not None or not store_known
            or previous.get("repositories") != observed["repositories"]
            or semantic_cursors != previous.get("semantic_cursors", {})
            or previous.get('policy_identity') != policy):
        state = {"schema_version": SCHEMA, **observed, "result_refs": _result_refs(session, results),
                 "policy_identity": policy, "policy_artifacts": policy_artifacts,
                 "policy_receipt": receipt, "last_trace": report, "deadlines": deadlines,
                 "stable": stable and semantic_known, "store_cursor": cursor, "semantic_cursors": semantic_cursors,
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
