#!/usr/bin/env python3
"""Measure unchanged entry using the existing real-process Doctor fixture."""
import argparse
import hashlib
import json
import statistics
import sys
import time
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--environment-root', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
root = args.environment_root.resolve()
sys.path[:0] = [str(root), str(root / 'tests')]
from test_doctor import EpochTests  # noqa: E402
from mncs_env.session_store import open_store  # noqa: E402

rows = []
for _ in range(7):
    fixture = EpochTests('test_reentry_hits_epoch_and_skips_revalidation')
    fixture.setUp()
    try:
        fixture.ready()
        code, first = fixture.enter()
        assert code == 0, first
        store = open_store(fixture.state, 'file')
        before = store.load_snapshot(first['session_id'])
        events = store.read_events(first['session_id'])
        started = time.perf_counter_ns()
        code, second = fixture.enter()
        wall_ns = time.perf_counter_ns() - started
        assert code == 0 and first['session_id'] == second['session_id'], second
        after = store.load_snapshot(first['session_id'])
        added = store.read_events(first['session_id'])[len(events):]
        rows.append({'wall_ns': wall_ns, 'doctor_reused': second['doctor']['reused'],
                     'doctor_elapsed_seconds': second['doctor']['elapsed_seconds'],
                     'snapshot_saves': after['snapshot_sequence'] - before['snapshot_sequence'],
                     'events_added': [e['type'] for e in added],
                     'readiness': second['readiness']['status'],
                     'service_ready': second['readiness']['services'][0]['status'],
                     'revalidation': second['entry']['revalidation']})
    finally:
        fixture.doCleanups()
report = {'schema_version': 'mncs.environment.remediation-entry-proof/1',
          'source_sha256': {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                            for name in ('mncs_env/sessions.py', 'mncs_env/doctor.py', 'mncs_env/entry.py')},
          'rows': rows, 'median_wall_ns': statistics.median(r['wall_ns'] for r in rows),
          'scope': 'seven unchanged real CLI reentries, file-debug backend, declared live service fixture',
          'limitations': ['Controlled fixture, not agent productivity or a production latency distribution.',
                          'Live readiness checks are retained; required independent verification is not counted as waste.']}
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps({'output': str(args.output), 'median_ms': report['median_wall_ns'] / 1e6,
                  'saves': [r['snapshot_saves'] for r in rows]}))
