"""Resident deltas through real native routing/Store; socket IO is substituted.

The live host cannot bind a Unix socket in the execution sandbox. No semantic
service, routing policy, Store publication or cursor semantics are mirrored.
"""
from pathlib import Path
from unittest.mock import patch
from mncs_env import coherence, sources
from mncs_env.session_store import open_store
from test_incremental import participant, runners, Participant


def event(subject, cursor='1'):
    return sources.Observation('language-service', 'actual-stream', cursor, 'event:' + cursor,
        subject, 'observation-only', '2', 'semantic.changed', provenance={'cursor': int(cursor)},
        payload={'semantic_subjects': [{'identity': subject}], 'impact_complete': True,
                 'obligations': {'complete': True, 'added': [], 'resolved': [], 'status_changed': []}})


def test_complete_subject_subscriptions_skip_unrelated_owner_work(participant):
    counts, owned = runners()
    definition = {'coherence_streams': [{'identity': 'LS', 'protocol': 'mncs.workspace-event-cursor/2', 'socket': '/declared/socket'}],
                  'coherence_subscriptions': {name: {'stream': 'LS', 'complete': True, 'subjects': ['subject:A'], 'obligations': []}
                                             for name in ('verification', 'diagnostics', 'family')}}
    with patch.object(sources.LanguageServiceSource, 'observe', return_value=sources.SourceResult('ok', [], '0')):
        coherence.tick(participant, definition, owned)
    with patch.object(sources.LanguageServiceSource, 'observe', return_value=sources.SourceResult('ok', [event('subject:B')], '1')):
        _, trace = coherence.tick(participant, definition, owned)
    assert {item['pass'] for item in trace['scheduled']} == {'semantics'}
    with patch.object(sources.LanguageServiceSource, 'observe', return_value=sources.SourceResult('ok', [event('subject:A', '2')], '2')):
        _, trace = coherence.tick(participant, definition, owned)
    assert {item['pass'] for item in trace['scheduled']} == {'semantics', 'verification', 'diagnostics', 'family'}


def test_incomplete_impact_and_stream_reset_never_authorize_quiet_reuse(participant):
    counts, owned = runners()
    definition = {'coherence_streams': [{'identity': 'LS', 'protocol': 'mncs.workspace-event-cursor/2', 'socket': '/declared/socket'}],
                  'coherence_subscriptions': {'verification': {'stream': 'LS', 'complete': True, 'subjects': ['A']}}}
    with patch.object(sources.LanguageServiceSource, 'observe', return_value=sources.SourceResult('ok', [], '0')):
        coherence.tick(participant, definition, owned)
    incomplete = event('B'); incomplete.payload['impact_complete'] = False
    with patch.object(sources.LanguageServiceSource, 'observe', return_value=sources.SourceResult('ok', [incomplete], '1')):
        _, trace = coherence.tick(participant, definition, owned)
    assert 'verification' in {item['pass'] for item in trace['scheduled']}
    with patch.object(sources.LanguageServiceSource, 'observe', return_value=sources.SourceResult('reset', [], None, 'new stream')):
        _, trace = coherence.tick(participant, definition, owned)
    assert trace['mode'] == 'bounded_reconciliation' and len(trace['scheduled']) == 7


def test_real_store_two_sessions_route_relevant_rows_and_ignore_unrelated_rows(participant):
    state = participant.state_dir
    left = open_store(state, 'store')
    right = open_store(state, 'store')
    participant.store = right
    counts, owned = runners()
    definition = {'coherence_publications': [{'schema': 'proof.verification/1', 'event': 'verification.changed'}]}
    try:
        coherence.tick(participant, definition, owned)
        left.put_record(b'proof.verification/1', b'unrelated', {'repository': 'other', 'subject': 'other'})
        _, trace = coherence.tick(participant, definition, owned)
        assert trace['scheduled'] == []
        left.put_record(b'proof.verification/1', b'relevant', {'repository': 'app', 'subject': 'A'})
        _, trace = coherence.tick(participant, definition, owned)
        assert {item['pass'] for item in trace['scheduled']} == {'verification', 'diagnostics', 'family', 'projections'}
        right.close()
        right = open_store(state, 'store')
        participant.store = right
        participant.snapshot = right.load_snapshot(participant.session_id)
        _, trace = coherence.tick(participant, definition, owned)
        assert trace['scheduled'] == []
    finally:
        left.close(); right.close()
