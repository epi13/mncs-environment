#!/usr/bin/env python3
"""Repeated full-path cost probe; mechanical evidence, never agent utility."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import resource
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--environment-root', type=Path, required=True)
parser.add_argument('--store-root', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--runs', type=int, default=3)
parser.add_argument('--calls', type=int, default=8)
args = parser.parse_args()
if not 2 <= args.runs <= 10 or not 3 <= args.calls <= 50:
    parser.error('runs must be 2..10; calls must be 3..50')
sys.path.insert(0, str(args.environment_root.resolve()))
from mncs_env import capabilities, demand, sessions  # noqa: E402
from mncs_env.identity import canonical_bytes  # noqa: E402
from mncs_env.session_store import open_store  # noqa: E402


def sources(root):
    names = ('mncs_env/sessions.py', 'mncs_env/demand.py', 'mncs_env/store_backend.py',
             'mncs_env/session_store.py') if root == args.environment_root else (
                 'python/mncs_store/embedded.py', 'python/mncs_store/session.py',
                 'src/store/content.mncs')
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names}


def logical_bytes(root):
    return sum(p.stat().st_size for p in root.rglob('*') if p.is_file())


def counters(store):
    native = getattr(getattr(getattr(store, 'backend', None), '_store', None), 'session', None)
    return {key: getattr(native, key, 0) for key in (
        'call_count', 'semantic_seconds', 'request_transport_bytes', 'response_transport_bytes')}


def usage():
    r = resource.getrusage(resource.RUSAGE_SELF)
    return {'user_seconds': r.ru_utime, 'system_seconds': r.ru_stime,
            'max_rss_kib_process_lifetime': r.ru_maxrss}


initial = {'environment': sources(args.environment_root), 'store': sources(args.store_root)}
rows = []
order = [(run, backend, condition, observed) for run in range(args.runs)
         for backend in ('file', 'store') for condition in ('resident', 'reopen')
         for observed in (False, True)]
random.Random(20261107).shuffle(order)
with tempfile.TemporaryDirectory(prefix='mncs-remediation-cost-', dir='/tmp') as temporary:
    parent = Path(temporary)
    workspace = parent / 'workspace'
    workspace.mkdir()
    for index, (run, backend, condition, observed) in enumerate(order):
        state = parent / str(index)

        def opened():
            return open_store(state, backend, verify_on_open=True,
                              store_package_dir=args.store_root / 'python')

        store = opened()
        environment = sessions.resolve_environment(
            definition={'name': 'remediation-cost', 'intent': {'goal': 'mechanical cost probe'}},
            workspace_root=workspace, state_dir=state, consumer_id='cost-proof', store=store)
        session = sessions.Session.create(state_dir=state, environment=environment,
                                          consumer_id='cost-proof', store=store)
        for stage in ('resolving', 'ready', 'active'):
            session.transition(stage, 'fixture')
        session.snapshot['bindings'] = [capabilities.probe_availability(capabilities.bind(
            provider='fixture', capability='noop', contract_revision='1',
            entrypoint='noop', address='/bin/true', effects=['read']))]
        session._save()
        # Warm the admitted artifact and initial decision context. First call
        # is separately retained; no OS page cache eviction is claimed.
        started = time.perf_counter_ns()
        (session.invoke if observed else session._invoke)('noop', [])
        first_ns = time.perf_counter_ns() - started
        before_events = len(session._log())
        before_bytes = logical_bytes(state)
        before_generation = store.generation() if backend == 'store' else None
        before_usage = usage()
        measured_counters = {key: 0 for key in counters(store)}
        samples = []
        for _ in range(args.calls):
            if condition == 'reopen':
                session.close()
            started = time.perf_counter_ns()
            if condition == 'reopen':
                store = opened()
                session = sessions.Session(store, session.session_id)
            previous = counters(store)
            result = (session.invoke if observed else session._invoke)('noop', [])
            samples.append(time.perf_counter_ns() - started)
            for key, value in counters(store).items():
                measured_counters[key] += value - previous[key]
            assert result['status'] == 'ok', result
        after_usage = usage()
        events = session._log()
        added = events[before_events:]
        expected = (3 if observed else 2) * args.calls
        assert len(added) == expected, (len(added), expected)
        after_generation = store.generation() if backend == 'store' else None
        after_bytes = logical_bytes(state)
        sid = session.session_id
        snapshot_sequence = session.snapshot['snapshot_sequence']
        session.close()
        # Reopen and validate the actual persisted request/context/result
        # records, not a successful exit or only the resident projection.
        store = opened()
        recovered = sessions.Session(store, sid)
        restored = recovered._log()
        assert restored == events
        assert recovered.snapshot['snapshot_sequence'] == snapshot_sequence
        requests = [e for e in restored if e['type'] == 'demand.observed']
        for event in requests:
            assert demand._read_context(recovered, event['payload']) is not None
        recovered.close()
        sorted_samples = sorted(samples)
        rows.append({'run': run, 'backend': backend, 'condition': condition,
                     'observed': observed, 'calls': args.calls, 'wall_ns': samples,
                     'median_ns': statistics.median(samples),
                     'p90_ns': sorted_samples[math.ceil(len(samples) * .9) - 1],
                     'first_call_ns': first_ns, 'events_added': len(added),
                     'event_json_bytes': sum(len(canonical_bytes(e)) for e in added),
                     'logical_state_growth_bytes': after_bytes - before_bytes,
                     'generation_delta': after_generation - before_generation if backend == 'store' else None,
                     'native_counters': measured_counters,
                     'process_usage_before': before_usage, 'process_usage_after': after_usage,
                     'persisted_events_and_contexts_recovered': True})
assert initial == {'environment': sources(args.environment_root), 'store': sources(args.store_root)}
report = {'schema_version': 'mncs.environment.remediation-cost-proof/1',
          'source_sha256': initial, 'rows': rows,
          'scope': 'full admission/provider/persistence path; reopen additionally includes handle/artifact admission',
          'limitations': ['Mechanical no-op fixture, not useful task fulfillment or agent productivity.',
                          'Reopen means a fresh handle, not an OS cold cache or fresh process.',
                          'Randomized fixed-seed treatment order; concurrent machine load is not controlled.',
                          'Native counters exclude reopen/admission; wall samples include it.',
                          'Logical bytes are not physical I/O; RSS is the process lifetime high water mark.',
                          'Private _invoke is only the pre-observation experimental baseline, with identical admission.']}
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps({'output': str(args.output), 'treatments': len(rows)}, sort_keys=True))
