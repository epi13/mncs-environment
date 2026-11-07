#!/usr/bin/env python3
"""Controlled observation-cost / native-join proof. Not an agent utility trial."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mncs_env import capabilities, demand, sessions  # noqa: E402
from mncs_env.identity import canonical_bytes  # noqa: E402
from mncs_env.session_store import open_store  # noqa: E402

FAMILY = next(p for p in ROOT.parents if (p / 'mncs-language').is_dir())
BIN = Path(os.environ.get('MNCS_BIN', FAMILY / 'mncs-language/target/debug/mncs'))
OUT = ROOT / 'evidence' / 'demand-observation-20261107'
OUT.mkdir(parents=True, exist_ok=True)
COMMON = Path(subprocess.check_output(['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'], cwd=ROOT, text=True).strip())
os.environ.setdefault('MNCS_NATIVE_APPLICATION_CACHE_DIR', str(COMMON / 'native-applications'))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def source_hashes():
    names = ['mncs_env/demand.py', 'mncs_env/sessions.py', 'mncs_env/verification.py',
             'mncs_env/events.py', 'mncs_env/capability_intelligence.py', 'mncs_env/cli.py',
             'native/mncs/environment/demand_correlation_v1.mncs',
             'native-applications/demand-correlation.json', 'tests/native/demand.mncs',
             'tests/test_demand.py', 'scripts/demand_proof.py']
    return {name: digest(ROOT / name) for name in names}


initial = source_hashes()
binary_hash = digest(BIN)
measurements = []
with tempfile.TemporaryDirectory(prefix='mncs-demand-proof-', dir='/tmp') as temporary:
    parent = Path(temporary)
    workspace = parent / 'workspace'
    workspace.mkdir()
    for backend in ['file', 'store']:
        for observed in [False, True]:
            state = parent / f'{backend}-{observed}'
            store = open_store(state, backend, verify_on_open=False)
            environment = sessions.resolve_environment(
                definition={'name': 'controlled-resource-fixture', 'intent': {'goal': 'mechanical cost proof'}},
                workspace_root=workspace, state_dir=state, consumer_id='resource-fixture', store=store,
            )
            session = sessions.Session.create(state_dir=state, environment=environment,
                                               consumer_id='resource-fixture', store=store)
            for stage in ['resolving', 'ready', 'active']:
                session.transition(stage, 'fixture')
            session.snapshot['bindings'] = [capabilities.probe_availability(capabilities.bind(
                provider='fixture', capability='noop', contract_revision='1', entrypoint='noop',
                address='/bin/true', effects=['read'],
            ))]
            session._save()
            before = len(session._log())
            before_bytes = sum(p.stat().st_size for p in state.rglob('*') if p.is_file())
            durations = []
            for _ in range(5):
                start = time.perf_counter_ns()
                result = session.invoke('noop', []) if observed else session._invoke('noop', [])
                durations.append(time.perf_counter_ns() - start)
                assert result['status'] == 'ok'
            events = session._log()
            requests = [e for e in events if e['type'] == 'demand.observed']
            after_bytes = sum(p.stat().st_size for p in state.rglob('*') if p.is_file())
            measurements.append({
                'backend': backend, 'instrumented': observed, 'calls': 5,
                'wall_ns': durations, 'median_wall_ns': statistics.median(durations),
                'events_added': len(events) - before,
                'new_event_json_bytes': sum(len(canonical_bytes(e)) for e in events[before:]),
                'state_file_bytes_growth': after_bytes - before_bytes,
                'context_bytes_per_request': [e['payload']['context_bytes'] for e in requests],
                'distinct_context_references': len({e['payload']['context_reference'] for e in requests}) if backend == 'store' else None,
                'observation_pre_emit_wall_ns': [e['payload']['observation_wall_ns'] for e in requests],
            })
            if observed and backend == 'store':
                # A selected test toolchain, not an assertion about live service
                # deployment. All correlation decisions still execute natively.
                session.snapshot['workspace']['root'] = str(FAMILY)
                session.snapshot['selected_checkouts'] = {'mncs-stdlib': {'path': str(FAMILY / 'mncs-stdlib')}}
                session._selected_runtime_environment = lambda binding: {'MNCS_BIN': str(BIN)}
                view = demand.project(session, events)
                assert view['status'] == 'native-projected', view['status']
                assert all(r['correlation']['operation_matched'] for r in view['records'])
                assert all(not r['correlation']['verification_linked'] for r in view['records'])
                (OUT / 'controlled-native-view.json').write_text(json.dumps(view, indent=2) + '\n')
            close = getattr(store, "close", None)
            if callable(close):
                close()

native_result = OUT / 'native-result.json'
command = [str(BIN), 'test', 'tests/native/demand.mncs', '--library', 'native',
           '--library', str(FAMILY / 'mncs-stdlib/library'), '--library', str(FAMILY / 'mncs-test/native'),
           '--step-budget', '3000000', '--format', 'json', '--result', str(native_result)]
run = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=180)
assert run.returncode == 0, run.stdout + run.stderr
native = json.loads(native_result.read_text())
assert native['summary']['verdict'] == 'PASS' and native['summary']['total'] == 11
# A pure native call measures the join kernel separately from capture/storage
# and structured-application JSON transport. Handles have ordinary production
# lengths, but these are explicit mechanical inputs, never utility labels.
def integer(n):
    return {"integer": {"type": {"bits": 64, "signed": False}, "value": n}}
def text(value):
    return {"sequence": {"values": [{"byte": {"value": n}} for n in value.encode()]}}
fields = {name: text(value) for name, value in {
    "demand": "evt_" + "a" * 16, "binding": "cap_" + "b" * 16,
    "start_demand": "evt_" + "a" * 16, "end_demand": "evt_" + "a" * 16,
    "start_binding": "cap_" + "b" * 16, "end_binding": "cap_" + "b" * 16,
    "start_request": "req_" + "c" * 48, "end_request": "req_" + "c" * 48,
    "result_demand": "evt_" + "a" * 16, "result_request": "req_" + "c" * 48,
}.items()}
fields.update({name: integer(n) for name, n in {
    "demand_sequence": 1, "start_sequence": 2, "end_sequence": 3,
    "result_sequence": 4, "starts": 1, "completions": 1, "results": 1,
}.items()})
fields.update({name: {"boolean": {"value": True}} for name in
               ["context_present", "context_complete", "observed_result"]})
arguments = OUT / 'join.args.json'
arguments.write_text(json.dumps([{"record": {"fields": fields}}], indent=2) + '\n')
call_result = OUT / 'join-call.json'
call = subprocess.run([str(BIN), 'call', 'native/mncs/environment/demand_correlation_v1.mncs',
                       '--module', 'mncs.environment.demand_correlation_v1', '--function', 'correlate',
                       '--args', str(arguments), '--library', 'native', '--library', str(FAMILY / 'mncs-stdlib/library'),
                       '--result', str(call_result)], cwd=ROOT, capture_output=True, text=True, timeout=180)
assert call.returncode == 0, call.stderr + call.stdout
call_data = json.loads(call_result.read_text())
assert call_data.get('status') == 'returned'
assert initial == source_hashes() and binary_hash == digest(BIN)
report = {
    'schema_version': 'mncs.environment.demand-observation-proof/1',
    'origin': 'controlled mechanical fixture, not naturally occurring agent behavior',
    'tested_parent': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
    'source_sha256': initial, 'compiler_binary_sha256': binary_hash,
    'native_result_sha256': digest(native_result), 'native_summary': native['summary'],
    'native_execution': native['execution'], 'measurements': measurements,
    'pure_join_vm_steps': call_data['call']['steps'], 'pure_join_arguments_sha256': digest(arguments),
    'pure_join_result_sha256': digest(call_result),
    'pure_join_scope': 'native kernel only; excludes event lookup, evidence reads, context capture and structured JSON application transport',
    'native_view_sha256': digest(OUT / 'controlled-native-view.json'),
    'limitations': ['Five sequential samples per treatment; not randomized and not a latency distribution.',
                   'Wall time covers the full existing admission/provider/persistence path; not agent reasoning time.',
                   'State-file growth is logical file size, not filesystem allocation or attributed physical I/O.',
                   'Native/host peak memory and agent tokens are unmeasured.',
                   'All noop calls are mechanically successful; none has independently verified task usefulness.',
                   'Private _invoke is used only as the controlled pre-observation baseline; it retains the same admission checks.'],
}
(OUT / 'receipt.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps({'native': native['summary'], 'measurements': measurements}, indent=2))
