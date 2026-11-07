#!/usr/bin/env python3
"""Outcome-preserving admission fixture; not a trial of agent productivity."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--environment-root', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--advice', action='store_true')
args = parser.parse_args()
root = args.environment_root.resolve()
sys.path[:0] = [str(root), str(root / 'tests')]
from test_doctor import EpochTests  # noqa: E402
from mncs_env import sessions  # noqa: E402
from mncs_env.session_store import open_store  # noqa: E402

fixture = EpochTests('test_reentry_hits_epoch_and_skips_revalidation')
fixture.setUp()
try:
    fixture.ready()
    _, first = fixture.enter()
    session = sessions.Session(open_store(fixture.state, 'file'), first['session_id'])
    (fixture.project / 'notes.txt').write_text('same draft requiring explicit ownership')
    session.revalidate()
    attempts = []
    for stage in ('missing-claim', 'narrow-claim', 'whole-worktree-claim'):
        if stage != 'missing-claim':
            scope = {'kind': 'worktree', 'checkout': str(fixture.project), 'branch': 'main'}
            if stage == 'narrow-claim':
                scope.update(kind='paths', paths=['provider.py'])
            session.acquire_claim('fixture', basis='explicit-adoption', scope=scope)
        if args.advice and stage != 'whole-worktree-claim':
            response = session.capability_preflight('fixture.start/1')
            attempts.append({'stage': stage, 'interface': 'advisory', 'response': response})
            assert response['status'] == 'requirements-unmet', response
        else:
            response = session.invoke('fixture.start/1', [])
            attempts.append({'stage': stage, 'interface': 'invoke', 'response': response})
            assert response['status'] == ('ok' if stage == 'whole-worktree-claim'
                                          else 'deferred-coordination'), response
    outcome = json.loads((fixture.project / 'service-state.json').read_text())
    assert outcome == {'ready': True, 'starts': 1}, outcome
    report = {'schema_version': 'mncs.environment.remediation-admission-proof/1',
              'source_sha256': hashlib.sha256((root / 'mncs_env/sessions.py').read_bytes()).hexdigest(),
              'advice_used': args.advice, 'attempts': attempts, 'independent_fixture_state': outcome,
              'provider_invocation_requests': sum(a['interface'] == 'invoke' for a in attempts),
              'unsuccessful_invocation_requests': sum(a['interface'] == 'invoke' and a['response']['status'] != 'ok'
                                                       for a in attempts),
              'provider_executions': outcome['starts'],
              'limitations': ['Controlled sequence with identical provider and scope prerequisites, not natural agent behavior.',
                              'Advisories add checks; they never replace live admission or grant execution permission.',
                              'Counts come from the explicit harness requests and actual outcomes, not missing observations.']}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({k: report[k] for k in ('provider_invocation_requests', 'unsuccessful_invocation_requests', 'provider_executions')}))
finally:
    fixture.doCleanups()
