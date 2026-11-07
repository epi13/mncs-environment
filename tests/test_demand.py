from __future__ import annotations

import copy
import json
import time
from pathlib import Path

import pytest

from mncs_env import capabilities, capability_intelligence, demand, sessions, verification
from mncs_env.session_store import open_store


@pytest.fixture
def session(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    state = tmp_path / 'state'
    store = open_store(state, 'file')
    environment = sessions.resolve_environment(
        definition={'name': 'demand-fixture', 'intent': {'goal': 'mechanical fixture'}},
        workspace_root=workspace, state_dir=state, consumer_id='fixture', store=store,
    )
    s = sessions.Session.create(state_dir=state, environment=environment,
                                consumer_id='fixture', store=store)
    for stage in ['resolving', 'ready', 'active']:
        s.transition(stage, 'fixture')
    s.snapshot['bindings'] = [capabilities.probe_availability(capabilities.bind(
        provider='fixture', capability='echo', contract_revision='1',
        entrypoint='echo', address='/bin/echo', effects=['read'],
    ))]
    s._save()
    return s


def test_request_context_is_immutable_and_operation_ids_are_exact(session, monkeypatch):
    monkeypatch.setattr(demand.native_readiness, '_toolchain_paths', lambda s: None)
    r = session.invoke('echo', ['hello'])
    assert r['status'] == 'ok'
    s = sessions.Session(session.store, session.session_id)
    events = s._log()
    request = next(e for e in events if e['type'] == 'demand.observed')
    assert 'argv' not in request['payload']
    reference = r['demand_reference']
    assert reference['identity'] == request['identity']
    assert request['sequence'] < r['completion_reference']['sequence']
    assert r['demand_finish_reference']['sequence'] > r['completion_reference']['sequence']
    s.snapshot['bindings'] = []  # No current-state backfill into history.
    p = demand.project(s, events)
    assert p['status'] == 'native-unavailable'
    assert list(p['contexts'].values())[0]['bindings'][0]['capability'] == 'echo'
    assert p['records'][0]['context_status'] == 'integrity-verified'
    assert p['records'][0]['operation_status'] == 'ok'
    assert p['records'][0]['usefulness'] == 'not-established'
    assert p['eligible_need_denominator'] is None


def test_unknown_capability_and_authority_refusal_remain_observed(session):
    with pytest.raises((sessions.AuthorityDenied, sessions.LifecycleError, ValueError, KeyError)):
        session.invoke('unbound', [])
    events = session._log()
    assert len([e for e in events if e['type'] == 'demand.observed']) == 1
    assert len([e for e in events if e['type'] == 'demand.finished']) == 1
    assert not any(e['type'] == 'capability.invoked' for e in events)
    session.snapshot['bindings'] = [capabilities.probe_availability(capabilities.bind(
        provider='fixture', capability='guarded', contract_revision='1',
        entrypoint='guarded', address='/bin/echo', effects=['write'],
    ))]
    session.snapshot['authority']['protected_repositories'] = ['fixture']
    with pytest.raises(sessions.AuthorityDenied):
        session.invoke('guarded', ['must-not-run'])
    finish = [e for e in session._log() if e['type'] == 'demand.finished'][-1]
    assert finish['payload']['status'] == 'refused'
    assert finish['payload']['agent_receipt'] == 'not-established'


def test_context_tamper_and_partial_window_are_explicit(session, monkeypatch):
    monkeypatch.setattr(demand.native_readiness, '_toolchain_paths', lambda s: None)
    session.invoke('echo', ['hello'])
    events = session._log()
    p = demand.project(session, events[-1:])
    assert p['recorded_demand_count'] == 0 and p['orphan_observation_count'] == 1
    changed = copy.deepcopy(events)
    request = next(e for e in changed if e['type'] == 'demand.observed')
    request['payload']['context_inline']['bindings'][0]['capability'] = 'other'
    p = demand.project(session, changed)
    assert p['records'][0]['context_status'] == 'unavailable-or-integrity-failure'
    assert not p['contexts']


def test_no_private_arguments_in_additional_demand_events_and_no_extra_snapshots(session):
    # The original capability event still follows its established argv policy.
    # New demand records add no command/prompt interception.
    original = session.store.save_snapshot
    saves = []
    def save(sid, value):
        saves.append(value['snapshot_sequence'])
        return original(sid, value)
    session.store.save_snapshot = save
    session.invoke('echo', ['private-fixture-text'])
    assert len(saves) == 2  # Existing pre/post invocation saves only.
    for e in session._log():
        if e['type'].startswith('demand.'):
            assert 'private-fixture-text' not in json.dumps(e)


def test_budgeted_context_does_not_expand_retention_or_become_current_truth(session, monkeypatch):
    monkeypatch.setattr(demand, 'MAX_CONTEXT_BYTES', 1)
    result = session.invoke('echo', ['hello'])
    assert result['status'] == 'ok'
    e = next(e for e in session._log() if e['type'] == 'demand.observed')
    assert e['payload']['context_status'] == 'over-budget'
    assert 'context_inline' not in e['payload']


def test_native_application_projects_real_session_events_and_rejects_predictions(session, monkeypatch):
    family = next(p for p in Path(__file__).resolve().parents if (p / 'mncs-language').is_dir())
    binary, stdlib = family / 'mncs-language/target/debug/mncs', family / 'mncs-stdlib/library'
    if not binary.is_file():
        pytest.skip('native toolchain not present')
    monkeypatch.setattr(demand.native_readiness, '_toolchain_paths', lambda s: (binary, stdlib))
    session._selected_runtime_environment = lambda _binding: {'MNCS_BIN': str(binary)}
    result = session.invoke('echo', ['hello'])
    baseline = demand.project(session, session._log())
    assert baseline['status'] == 'native-projected'
    assert baseline['records'][0]['correlation'] == {'operation_matched': True, 'verification_linked': False, 'reason': 0}
    payload = {'demand_reference': result['demand_reference'],
               'provider_request_id': result['provider_request_id'],
               'evidence_id': 'mechanical-verified-result', 'result_kind': 'prediction'}
    # Synthetic outcome fixture tests mechanical joins, never operational benefit.
    session._emit('verification.verified', 'environment', payload)
    prediction = demand.project(session, session._log())
    assert prediction['records'][0]['correlation']['verification_linked'] is False
    payload['result_kind'] = 'observed'
    payload['obligation'] = 'mechanical-obligation'
    payload['outcome'] = 'passed'
    record = {'obligation': payload['obligation'], 'evidence_id': payload['evidence_id'],
              'outcome': 'passed', 'verification_event': {'type': 'verification.verified', 'payload': payload}}
    verification._finish(session, verification.utcnow(), time.monotonic(), [record], {}, [],
                         'fixture', None, 'fixture-epoch', {'summary': {}})
    events = [e for e in session._log() if not (e['type'] == 'verification.verified' and e['payload'].get('result_kind') == 'prediction')]
    verified = demand.project(session, events)
    assert verified['records'][0]['correlation']['verification_linked'] is True
    view = capability_intelligence.project_session_activity(session, activity_section='demand')
    assert 'decision_opportunities' in view['session_activity']


def test_invalid_surface_is_refused_without_copying_arbitrary_private_text(session):
    with pytest.raises(sessions.LifecycleError):
        session.invoke('echo', [], request_surface='private noncontract text')
    events = [e for e in session._log() if e['type'].startswith('demand.')]
    assert len(events) == 2
    assert 'private noncontract text' not in json.dumps(events)
    assert events[0]['payload']['request_surface'] == 'unsupported-surface'
    assert not any(e['type'] == 'capability.invoked' for e in session._log())


def test_store_context_reuse_restart_and_own_feed_do_not_create_new_demand(session, tmp_path, monkeypatch):
    monkeypatch.setattr(demand.native_readiness, '_toolchain_paths', lambda s: None)
    store = open_store(tmp_path / 'canonical-state', 'store', verify_on_open=False)
    store.save_snapshot(session.session_id, session.snapshot)
    s = sessions.Session(store, session.session_id)
    s.invoke('echo', ['one'])
    s.invoke('echo', ['two'])
    assert s.observe_store() is None
    events = s._log()
    requests = [e for e in events if e['type'] == 'demand.observed']
    assert len(requests) == 2
    references = {e['payload']['context_reference'] for e in requests}
    assert len(references) == 1 and next(iter(references)).startswith('mncs-store:')
    assert not any('context_inline' in e['payload'] for e in requests)
    restarted = sessions.Session(store, session.session_id)
    view = demand.project(restarted, restarted._log())
    assert len(view['contexts']) == 1
    assert all(r['context_status'] == 'integrity-verified' for r in view['records'])


def test_ambient_calls_keep_existing_owner_trace_without_duplicate_agent_demand(session):
    from mncs_env.invocation_context import ambient_owner_pass_context
    with ambient_owner_pass_context(session.session_id, 'fixture-pass', 'ase_' + 'a' * 32):
        result = session.invoke('echo', ['ambient'])
    assert result['status'] == 'ok'
    events = session._log()
    assert not any(e['type'].startswith('demand.') for e in events)
    start = next(e for e in events if e['type'] == 'capability.invoked')
    assert start['payload']['ambient_owner_pass_context']['owner_pass'] == 'fixture-pass'
    assert start['payload']['demand_reference'] is None
