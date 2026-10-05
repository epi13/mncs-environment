"""Real Git/Store transport and native routing; domain runners are fixtures."""
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from mncs_env import coherence, family, observations
from mncs_env.session_store import open_store

AUTOMATION = Path(__file__).resolve().parents[2] / 'mncs-automation'
BINARY = os.environ.get('MNCS_BIN') or os.environ.get('MNCS_BINARY')


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], capture_output=True, check=True)


def repository(tmp_path):
    root = tmp_path / 'app'
    root.mkdir()
    git(root, 'init', '-q')
    (root / 'source.mncs').write_text('source one')
    (root / 'notes.md').write_text('prose one')
    (root / '.mncs').mkdir()
    (root / '.mncs/project.json').write_text('{}')
    git(root, 'add', '.')
    git(root, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'bootstrap')
    return root


class Participant:
    def __init__(self, state, root, store=None):
        self.state_dir, self.session_id, self.saves = state, 'ses_incremental_fixture', 0
        self.store = store or SimpleNamespace(read_claims=lambda: {}, read_projection_versions=lambda: {})
        self.snapshot = {'workspace': {'root': str(root.parent)},
                         'selected_checkouts': {'app': {'path': str(root)}}, 'toolchain': {}}

    def _save(self):
        self.saves += 1
        self.snapshot['snapshot_sequence'] = self.saves
        if hasattr(self.store, 'save_snapshot'):
            self.store.save_snapshot(self.session_id, self.snapshot)


@pytest.fixture
def participant(tmp_path, monkeypatch):
    if not BINARY or not AUTOMATION.is_dir():
        pytest.skip('explicit native compiler and Automation checkout required')
    monkeypatch.setenv('MNCS_BIN', BINARY)
    monkeypatch.setenv('MNCS_AUTOMATION_ROOT', str(AUTOMATION))
    monkeypatch.setattr(family, 'scheduler_deadlines', lambda session: [])
    return Participant(tmp_path / 'state', repository(tmp_path))


def runners():
    counts = {name: 0 for name in coherence.declarations()['passes']}
    def run(name):
        counts[name] += 1
        return {'summary': {}, 'reused': False}
    return counts, {name: lambda name=name: run(name) for name in counts}


def test_warm_tick_has_zero_processes_and_writes(participant):
    counts, owned = runners()
    coherence.tick(participant, {}, owned)
    saves = participant.saves
    with patch.object(subprocess, 'Popen', side_effect=AssertionError('quiet tick spawned process')):
        _, trace = coherence.tick(participant, {}, owned)
    assert trace['scheduled'] == []
    assert len(trace['skipped']) == 7
    assert set(counts.values()) == {1}
    assert participant.saves == saves


@pytest.mark.parametrize('path,expected', [
    ('notes.md', {'projections'}),
    ('source.mncs', {'semantics', 'verification', 'diagnostics', 'projections'}),
    ('.mncs/project.json', {'doctor', 'semantics', 'actions', 'verification', 'family', 'projections'}),
])
def test_external_edits_select_only_subscribed_passes(participant, path, expected):
    counts, owned = runners()
    coherence.tick(participant, {}, owned)
    (Path(participant.snapshot['selected_checkouts']['app']['path']) / path).write_text('changed')
    _, trace = coherence.tick(participant, {}, owned)
    assert {item['pass'] for item in trace['scheduled']} == expected
    _, quiet = coherence.tick(participant, {}, owned)
    assert quiet['scheduled'] == []


def test_same_dirty_filename_edited_twice_is_not_cached(participant):
    counts, owned = runners()
    coherence.tick(participant, {}, owned)
    path = Path(participant.snapshot['selected_checkouts']['app']['path']) / 'source.mncs'
    path.write_text('first dirty state')
    coherence.tick(participant, {}, owned)
    path.write_text('second dirty state')
    coherence.tick(participant, {}, owned)
    assert counts['verification'] == 3
    assert counts['doctor'] == 1


def test_missing_catalogue_recovers_once(participant):
    _, owned = runners()
    coherence.tick(participant, {}, owned)
    ref = participant.snapshot['coherence']['repositories']['app']
    Path(ref['artifact']).unlink()
    _, trace = coherence.tick(participant, {}, owned)
    assert trace['mode'] == 'bounded_reconciliation'
    assert len(trace['scheduled']) == 7
    _, quiet = coherence.tick(participant, {}, owned)
    assert quiet['scheduled'] == []


def test_corrupt_cached_result_cannot_authorize_reuse(participant):
    _, owned = runners()
    coherence.tick(participant, {}, owned)
    ref = participant.snapshot['coherence']['result_refs']['verification']
    Path(ref['artifact']).write_text('{"block":{"summary":{"current":999}}}')
    _, trace = coherence.tick(participant, {}, owned)
    assert len(trace['scheduled']) == 7
    _, quiet = coherence.tick(participant, {}, owned)
    assert quiet['scheduled'] == []


def test_edit_during_pass_is_pending_not_current(participant):
    _, owned = runners()
    path = Path(participant.snapshot['selected_checkouts']['app']['path']) / 'source.mncs'
    owned['verification'] = lambda: (path.write_text('moving input'), {'summary': {}})[1]
    _, trace = coherence.tick(participant, {}, owned)
    assert participant.snapshot['coherence']['stable'] is False
    assert trace['pending_events']


def test_owner_boundary_deadline_wakes_only_doctor(participant):
    _, owned = runners()
    definition = {'services': [{'observation_inputs': ['selected-repositories'], 'observation_max_age_ms': 1000}]}
    coherence.tick(participant, definition, owned, now_ms=1000)
    _, quiet = coherence.tick(participant, definition, owned, now_ms=1999)
    assert quiet['scheduled'] == []
    _, due = coherence.tick(participant, definition, owned, now_ms=2000)
    assert {item['pass'] for item in due['scheduled']} == {'doctor'}


def test_verification_change_routes_to_family_and_projection(participant):
    _, owned = runners()
    coherence.tick(participant, {}, owned)
    (Path(participant.snapshot['selected_checkouts']['app']['path']) / 'source.mncs').write_text('new obligation')
    def verify():
        participant.snapshot['verification_state'] = {'ob-1': {'verdict': 'PASS', 'input': 'new obligation'}}
        return {'summary': {'current': 1}}
    owned['verification'] = verify
    _, trace = coherence.tick(participant, {}, owned)
    assert 'family' in {item['pass'] for item in trace['scheduled']}
    assert any(item['kind'] == 'verification.changed' for item in trace['derived_events'])


def test_store_publication_during_pass_is_not_skipped(participant):
    store = open_store(participant.state_dir, 'store')
    participant.store = store
    _, owned = runners()
    fired = False
    def publish():
        nonlocal fired
        if not fired:
            store.write_projection_row('family:change/concurrent-fixture', 1, {'test': True})
            fired = True
        return {'summary': {}}
    owned['verification'] = publish
    try:
        coherence.tick(participant, {}, owned)
        assert participant.snapshot['coherence']['stable'] is False
        _, trace = coherence.tick(participant, {}, owned)
        assert any(event['kind'] == 'family.changed' for event in trace['events'])
        _, quiet = coherence.tick(participant, {}, owned)
        assert quiet['scheduled'] == []
    finally:
        store.close()


def test_store_cursor_waits_for_owner_success(participant):
    store = open_store(participant.state_dir, 'store')
    participant.store = store
    _, owned = runners()
    try:
        coherence.tick(participant, {}, owned)
        previous = participant.snapshot['coherence']['store_cursor']
        store.write_projection_row('family:change/retry-fixture', 1, {'test': True})

        owned['family'] = lambda: {
            'summary': {'blockers': 1}, 'operation_status': 'retry'}
        _, failed = coherence.tick(participant, {}, owned)
        assert failed['owner_failures'] == ['family']
        assert failed['store_cursor_disposition'] == 'held_for_owner_retry'
        assert participant.snapshot['coherence']['store_cursor'] == previous
        assert participant.snapshot['coherence']['stable'] is False

        owned['family'] = lambda: {'summary': {'checked': True}}
        _, retried = coherence.tick(participant, {}, owned)
        assert retried.get('owner_failures', []) == []
        assert any(event.get('kind') == 'family.changed'
                   for event in retried['events']), retried
        assert participant.snapshot['coherence']['store_cursor'] != previous
    finally:
        store.close()


def test_owner_retry_cannot_leave_quiet_state_stable(participant):
    _, owned = runners()
    definition = {'services': [{'observation_inputs': ['selected-repositories'],
                                'observation_max_age_ms': 1000}]}
    coherence.tick(participant, definition, owned, now_ms=1000)
    owned['doctor'] = lambda: {
        'summary': {'blockers': 1}, 'operation_status': 'retry'}
    _, failed = coherence.tick(participant, definition, owned, now_ms=2000)
    assert failed['owner_failures'] == ['doctor']
    assert participant.snapshot['coherence']['stable'] is False


def test_semantic_cursor_waits_for_owner_success_after_reset(participant):
    _, owned = runners()
    coherence.tick(participant, {}, owned)
    slot = "mncs-language-service:workspace-stream"
    old = json.dumps({"stream": "old-stream", "cursor": 8}, sort_keys=True)
    candidate = json.dumps({"stream": "new-stream", "cursor": 23}, sort_keys=True)
    participant.snapshot["coherence"]["semantic_cursors"] = {slot: old}
    reset = [{"kind": "provider.changed", "provider": "mncs-language-service",
              "stream": slot, "reason": "cursor expired", "reset": True}]
    with patch.object(coherence, "_semantic_events",
                      return_value=(reset, {slot: candidate}, False)):
        owned["verification"] = lambda: {
            "summary": {"blockers": 1}, "operation_status": "retry"}
        _, failed = coherence.tick(participant, {}, owned)
    assert failed["cursor_disposition"] == "held_for_owner_retry"
    assert failed["owner_failures"] == ["verification"]
    assert participant.snapshot["coherence"]["semantic_cursors"] == {slot: old}

    with patch.object(coherence, "_semantic_events",
                      return_value=(reset, {slot: candidate}, False)):
        owned["verification"] = lambda: {"summary": {"checked": True}}
        _, retried = coherence.tick(participant, {}, owned)
    assert retried.get("owner_failures", []) == []
    assert participant.snapshot["coherence"]["semantic_cursors"] == {slot: candidate}
    assert retried["cursor_disposition"] == "reconciled_and_advanced"
    assert participant.snapshot["coherence"]["stable"] is True


def test_semantic_cursor_acknowledges_explicit_owner_no_work(participant):
    _, owned = runners()
    coherence.tick(participant, {}, owned)
    slot = "mncs-language-service:workspace-stream"
    old = json.dumps({"stream": "stream-1", "cursor": 4}, sort_keys=True)
    candidate = json.dumps({"stream": "stream-1", "cursor": 5}, sort_keys=True)
    participant.snapshot["coherence"]["semantic_cursors"] = {slot: old}
    event = {"kind": "semantic.changed", "stream": slot,
             "identity": "event-5", "generation": "generation-5"}
    with patch.object(coherence, "_semantic_events",
                      return_value=([event], {slot: candidate}, True)):
        owned["actions"] = lambda: None
        owned["diagnostics"] = lambda: None
        owned["family"] = lambda: None
        _, trace = coherence.tick(participant, {}, owned)
    assert trace.get("owner_failures", []) == []
    assert participant.snapshot["coherence"]["semantic_cursors"] == {slot: candidate}


def test_store_replay_reset_holds_cursor_until_full_owner_reconciliation(participant):
    _, owned = runners()
    coherence.tick(participant, {}, owned)
    participant.store = SimpleNamespace(generation=lambda: 999)
    old = participant.snapshot["coherence"].get("store_cursor")
    candidate = "999"
    reset = [{"kind": "store.reconcile_required", "candidate_cursor": candidate,
              "reason": "observation bound exceeded"}]
    with patch.object(coherence, "_store_events", return_value=(reset, old, True)):
        owned["verification"] = lambda: {
            "summary": {"blockers": 1}, "operation_status": "retry"}
        _, failed = coherence.tick(participant, {}, owned)
    assert len(failed["scheduled"]) == 7
    assert failed["store_replay"]["disposition"] == "reconciliation_incomplete_or_store_advanced"
    assert participant.snapshot["coherence"]["store_cursor"] == old

    with patch.object(coherence, "_store_events", return_value=(reset, old, True)):
        owned["verification"] = lambda: {"summary": {"checked": True}}
        _, retried = coherence.tick(participant, {}, owned)
    assert len(retried["scheduled"]) == 7
    assert retried["store_replay"]["disposition"] == "full_owner_reconciliation_complete"
    assert participant.snapshot["coherence"]["store_cursor"] == candidate


def test_store_snapshot_conflict_replays_semantic_work_until_retry_commits(participant):
    from mncs_env.session_store import SnapshotConflict

    store = open_store(participant.state_dir, "store")
    participant.store = store
    _, owned = runners()
    try:
        coherence.tick(participant, {}, owned)
        slot = "mncs-language-service:workspace-stream"
        old = json.dumps({"stream": "stream-1", "cursor": 4}, sort_keys=True)
        candidate = json.dumps({"stream": "stream-1", "cursor": 5}, sort_keys=True)
        participant.snapshot["coherence"]["semantic_cursors"] = {slot: old}
        participant.snapshot["snapshot_sequence"] += 1
        store.save_snapshot(participant.session_id, participant.snapshot)
        participant.saves = participant.snapshot["snapshot_sequence"]
        event = {"kind": "semantic.changed", "stream": slot,
                 "identity": "event-5", "generation": "generation-5"}
        with patch.object(coherence, "_semantic_events",
                          return_value=([event], {slot: candidate}, True)):
            def race_snapshot():
                competing = store.load_snapshot(participant.session_id)
                competing["snapshot_sequence"] += 1
                competing["concurrent_owner_receipt"] = "committed-first"
                store.save_snapshot(participant.session_id, competing)
                return {"summary": {"handled": True}}

            owned["verification"] = race_snapshot
            with pytest.raises(SnapshotConflict):
                coherence.tick(participant, {}, owned)

        durable = store.load_snapshot(participant.session_id)
        assert durable["concurrent_owner_receipt"] == "committed-first"
        assert durable["coherence"]["semantic_cursors"] == {slot: old}

        participant.snapshot = durable
        participant.saves = durable["snapshot_sequence"]
        owned["verification"] = lambda: {"summary": {"handled": True}}
        with patch.object(coherence, "_semantic_events",
                          return_value=([event], {slot: candidate}, True)):
            coherence.tick(participant, {}, owned)
        retried = store.load_snapshot(participant.session_id)
        assert retried["coherence"]["semantic_cursors"] == {slot: candidate}
    finally:
        store.close()


def test_unrelated_repository_is_outside_observation(participant, tmp_path):
    _, owned = runners()
    coherence.tick(participant, {}, owned)
    (tmp_path / 'unrelated').mkdir()
    (tmp_path / 'unrelated/source.mncs').write_text('independent')
    _, trace = coherence.tick(participant, {}, owned)
    assert trace['scheduled'] == []


def test_library_content_changes_even_with_restored_mtime(tmp_path):
    root = tmp_path / 'library'
    root.mkdir()
    path = root / 'module.mncs'
    path.write_text('first content')
    session = SimpleNamespace(state_dir=tmp_path / 'state', session_id='ses_library_fixture')
    first = observations.observe_library(session, root)
    original = path.stat()
    path.write_text('other content')
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    second = observations.observe_library(session, root, first)
    assert first['content_identity'] != second['content_identity']


def test_executable_identity_binds_actual_bytes(tmp_path):
    path = tmp_path / 'provider'
    path.write_bytes(b'old executable')
    first = observations.observe_artifact(path)
    path.write_bytes(b'new executable')
    second = observations.observe_artifact(path, first)
    assert first['artifact_identity'] != second['artifact_identity']
    assert second['build_origin'] == 'unknown'


def test_store_reopen_resumes_exact_observations_without_processes(participant):
    store = open_store(participant.state_dir, 'store')
    participant.store = store
    _, owned = runners()
    coherence.tick(participant, {}, owned)
    store.close()
    reopened = open_store(participant.state_dir, 'store')
    restored = Participant(participant.state_dir,
        Path(participant.snapshot['selected_checkouts']['app']['path']), reopened)
    restored.snapshot = reopened.load_snapshot(participant.session_id)
    restored.saves = restored.snapshot['snapshot_sequence']
    try:
        with patch.object(subprocess, 'Popen', side_effect=AssertionError('restart rediscovered current state')):
            _, trace = coherence.tick(restored, {}, owned)
        assert trace['scheduled'] == []
        assert restored.saves == participant.saves
    finally:
        reopened.close()


def test_external_head_advance_invalidates_git_facts(participant):
    _, owned = runners()
    coherence.tick(participant, {}, owned)
    root = Path(participant.snapshot['selected_checkouts']['app']['path'])
    (root / 'source.mncs').write_text('committed change')
    git(root, 'add', 'source.mncs')
    git(root, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'advance')
    _, trace = coherence.tick(participant, {}, owned)
    assert any(event['kind'] == 'repository.control_changed' for event in trace['events'])
    assert len(trace['scheduled']) == 7
    assert trace['enumerated_repositories'] == ['app']


def test_warm_store_entry_handle_promotes_only_when_state_changes(participant):
    store = open_store(participant.state_dir, 'store')
    participant.store = store
    _, owned = runners()
    coherence.tick(participant, {}, owned)
    store.close()
    reader = open_store(participant.state_dir, 'store', defer_mutation=True)
    participant.store = reader
    participant.snapshot = reader.load_snapshot(participant.session_id)
    assert reader.backend._store.read_only is True
    try:
        _, quiet = coherence.tick(participant, {}, owned)
        assert quiet['scheduled'] == []
        assert reader.backend._store.read_only is True
        (Path(participant.snapshot['selected_checkouts']['app']['path']) / 'source.mncs').write_text('promote for changed state')
        coherence.tick(participant, {}, owned)
        assert reader.backend._store.read_only is False
        assert reader.load_snapshot(participant.session_id)['coherence']['stable'] is True
    finally:
        reader.close()


def test_replay_identity_includes_domain_schema():
    from mncs_env import sources
    class Store:
        generation = lambda self: 2
        def domain_bindings_at(self, generation):
            return ((b'owner-a/1', b'same-id'),) if generation == 1 else (
                (b'owner-a/1', b'same-id'), (b'owner-b/1', b'same-id'))
    replay = sources.StoreReplaySource(Store(), 'ses_self:').observe('1')
    assert replay.status == 'ok'
    assert len(replay.events) == 1
    assert replay.events[0].provenance['domain_identity'] == 'same-id'


def test_store_replay_prefers_verified_bounded_generation_deltas():
    from mncs_env import sources

    class Store:
        def generation(self):
            return 3

        def domain_bindings_since(self, cursor, *, max_generations):
            assert cursor == 1
            assert max_generations == sources.MAX_REPLAY_WALK
            return (
                (2, b'claim/1', b'claim:peer'),
                (3, b'snapshot/1', b'ses_peer:snap:2'),
                (3, b'event/1', b'ses_self:evt:7'),
            )

        def domain_bindings_at(self, _generation):
            raise AssertionError('delta-aware Store replay must not rescan historical generations')

    replay = sources.StoreReplaySource(Store(), 'ses_self:').observe('1')
    assert replay.status == 'ok'
    assert [event.kind for event in replay.events] == ['claim.changed', 'session.changed']
    assert [event.generation for event in replay.events] == ['2', '3']


def test_canonical_session_store_forwards_bounded_delta_contract():
    from mncs_env.session_store import StoreSessionStore

    class Backend:
        def domain_bindings_since(self, generation, *, max_generations):
            assert generation == 10
            assert max_generations == 4096
            return ((11, b'claim/1', b'claim:peer'),)

    store = StoreSessionStore.__new__(StoreSessionStore)
    store.backend = Backend()
    assert store.domain_bindings_since(10, max_generations=4096) == (
        (11, b'claim/1', b'claim:peer'),
    )


def test_store_backend_forwards_provider_owned_reclaim():
    import pytest

    from mncs_env.store_backend import StoreBackend

    class Provider:
        def generation_inventory(self, *, keep_last):
            assert keep_last == 4096
            return {'head': 5000, 'floor': 768, 'prunable_files': 10}

        def prune_generations(self, *, keep_last, dry_run):
            assert keep_last == 4096
            assert dry_run is True
            return {'head': 5000, 'floor': 768, 'removed_files': 0}

    backend = StoreBackend.__new__(StoreBackend)
    backend._store = Provider()
    assert backend.generation_inventory()['prunable_files'] == 10
    assert backend.reclaim_generations()['removed_files'] == 0

    backend._store = object()
    with pytest.raises(RuntimeError, match='retention inventory'):
        backend.generation_inventory()
    with pytest.raises(RuntimeError, match='generation reclaim'):
        backend.reclaim_generations()


def test_store_replay_ignores_derived_products_and_resolves_claim_repository():
    from mncs_env import sources

    class Store:
        def generation(self):
            return 9

        def domain_bindings_since(self, cursor, *, max_generations):
            assert cursor == 7
            return (
                (8, b'mncs.environment.projection-state/1', b'mncs-compiler:overview:0000000031'),
                (8, b'mncs.provider-execution-provenance/1', b'projection-receipt:abc'),
                (9, b'mncs.environment.workspace-claim/2', b'claim:claim:mncs-language-service:0000000009'),
            )

    result = sources.StoreReplaySource(Store(), 'ses_self:').observe('7')
    assert result.status == 'ok'
    assert len(result.events) == 1
    assert result.events[0].kind == 'claim.changed'
    assert result.events[0].relations['claim'] == 'mncs-language-service'


def mutation_session(tmp_path, root, claims):
    from mncs_env.sessions import Session
    session = Session.__new__(Session)
    session.session_id = 'ses_mutation_fixture'
    session.store = SimpleNamespace(read_claims=lambda: claims)
    session.snapshot = {'workspace': {'root': str(tmp_path)},
        'selected_checkouts': {'app': {'path': str(root)}},
        'claim_holders': {}, 'repo_facts': {'app': {'clean': True, 'main_branch': True}},
        'authority': {'readable': ['app'], 'writable': ['app'], 'invocable': []}}
    session._emit = lambda *args, **kwargs: None
    return session


def test_mutation_boundary_reads_foreign_claim_instead_of_cached_permission(tmp_path):
    from datetime import datetime, timedelta, timezone
    root = repository(tmp_path)
    record = {'claim_id': 'claim:app', 'repository': 'app', 'session_id': 'ses_foreign',
              'consumer_id': 'foreign', 'status': 'held', 'version': 1,
              'expires_at': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
              'scope': {'kind': 'repository', 'repository': 'app', 'exclusive': True}}
    session = mutation_session(tmp_path, root, [record])
    result = session.check(action='write', target='app')
    assert result['verdict'] != 'allow'
    assert session.snapshot['claim_holders']['app'][0]['session_id'] == 'ses_foreign'


def test_mutation_boundary_observes_new_dirty_target(tmp_path):
    root = repository(tmp_path)
    session = mutation_session(tmp_path, root, {})
    assert session.check(action='write', target='app')['verdict'] == 'allow'
    (root / 'source.mncs').write_text('foreign external edit')
    assert session.check(action='write', target='app')['verdict'] != 'allow'


def test_missing_mutation_target_cannot_use_cached_clean_facts(tmp_path):
    from mncs_env.sessions import LifecycleError
    session = mutation_session(tmp_path, tmp_path / 'missing-checkout', {})
    with pytest.raises(LifecycleError, match='observation unavailable'):
        session.check(action='write', target='app')


def test_bounded_workspace_readiness_preserves_discovery_contract():
    from mncs_env import cli
    manifest = json.loads((Path(__file__).parents[1] / '.mncs/project.json').read_text())
    provided = {value['contract']: value for value in manifest['contracts']['provides']}
    assert '--summary' not in provided['workspace-discovery']['invocation']['fixed_argv']
    assert '--summary' in provided['workspace-readiness']['invocation']['fixed_argv']
    observed = {'schema_version': 'mncs.environment.workspace-discovery/1',
                'root': '/workspace', 'repository_count': 100, 'scan': {'complete': True},
                'repositories': [{'name': 'large-detail'}] * 100}
    parser = cli.build_parser()
    with patch.object(cli.workspace_module, 'discover_workspace', return_value=observed), patch.object(cli, 'out') as out:
        cli.cmd_workspace(parser.parse_args(['workspace', '--root', '/workspace', '--summary']))
        response = out.call_args.args[0]
        assert response['schema_version'] == 'mncs.environment.workspace-readiness/1'
        assert response['repository_count'] == 100 and 'repositories' not in response
        cli.cmd_workspace(parser.parse_args(['workspace', '--root', '/workspace']))
        assert out.call_args.args[0] == observed
