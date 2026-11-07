#!/usr/bin/env python3
"""One real native Test operation, durable owner evidence, and strict joins."""
from __future__ import annotations

import hashlib
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAMILY = next(p for p in ROOT.parents if (p / 'mncs-test').is_dir())
sys.path.insert(0, str(ROOT))
from mncs_env import demand, evidence_store, sessions, verification, workspace  # noqa: E402
from mncs_env.session_store import open_store  # noqa: E402

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output', type=Path,
                    default=ROOT / 'outputs/deterministic-remediation-20261107/native-verification-durable')
output = parser.parse_args().output.resolve()
output.mkdir(parents=True, exist_ok=True)
state = output / 'state'
if state.exists():
    raise SystemExit('proof state already exists; retain it and select a new output for a new trial')
common = subprocess.check_output(['git', 'rev-parse', '--path-format=absolute', '--git-common-dir'],
                                 cwd=ROOT, text=True).strip()
os.environ.setdefault('MNCS_NATIVE_APPLICATION_CACHE_DIR', str(Path(common) / 'native-applications'))
store = open_store(state, 'store', verify_on_open=False)
definition = {
    'name': 'remediation-native-verification',
    'workspace_scope': {'repositories': ['mncs-test', 'mncs-language', 'mncs-stdlib']},
    'intent': {'goal': 'Independently check one ordinary supported native test operation',
               'repositories': ['mncs-test']},
    'execution_roles': {'reference': {'capability': 'mncs-language:cli-toolchain',
                                      'use_reference_toolchain': True}},
}
selected = workspace.verify_registered_checkout(FAMILY / 'mncs-test', repository='mncs-test',
                                                 workspace_root=FAMILY)
environment = sessions.resolve_environment(
    definition=definition, working_checkout=selected, workspace_root=FAMILY,
    state_dir=state, consumer_id='remediation-proof', store=store)
session = sessions.Session.create(state_dir=state, environment=environment,
                                  consumer_id='remediation-proof', store=store)
try:
    for lifecycle in ('resolving', 'ready', 'active'):
        session.transition(lifecycle, 'proof')
    response = verification.ambient_pass(
        session, only='mncs-test.cross-module-suite-execution', max_executions=1)
    evidence = evidence_store.read(session, 'verification', response['evidence'])
    events = session._log()
    view = demand.project(session, events)
    assert response['summary']['executed'] == 1 and response['summary']['failed'] == 0, response
    assert any(r['correlation']['verification_linked'] for r in view['records']), view
    selection = {key: environment.get(key) for key in
                 ('definition_id', 'selected_checkouts', 'toolchain', 'execution_roles')}
    for name, record in [('response', response), ('evidence', evidence),
                         ('events', events), ('demand', view), ('selection', selection)]:
        (output / (name + '.json')).write_text(json.dumps(record, indent=2) + '\n')
    sid = session.session_id
    session.complete(outcome='verified', summary='native Test request/result correlation independently checked')
finally:
    session.close()
# Disposable compilation material is not durable evidence. Keep the actual
# Store, snapshots, invocation receipts and native result, with no Cargo target.
cache = state / 'sessions' / sid / 'verification-artifacts' / 'cache'
if cache.exists():
    shutil.rmtree(cache)
reopened = sessions.Session(open_store(state, 'store', verify_on_open=True), sid)
try:
    assert evidence_store.read(reopened, 'verification', response['evidence']) == evidence
    for event in events:
        if event['type'] == 'demand.observed':
            assert demand._read_context(reopened, event['payload']) is not None
    assert any(r['correlation']['verification_linked']
               for r in demand.project(reopened, reopened._log())['records'])
finally:
    reopened.close()
disposable = state / 'native-applications'
if disposable.exists():
    shutil.rmtree(disposable)
manifest = {p.relative_to(output).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in output.rglob('*') if p.is_file() and p.name != 'manifest.json'}
(output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
print(json.dumps({'session_id': sid, 'verification': response['evidence'],
                  'summary': response['summary'], 'durable_restart_readback': True}))
