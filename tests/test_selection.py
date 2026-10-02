"""Selection index validates canonical snapshots and survives index loss."""
from mncs_env import selection
from mncs_env.store_backend import SCHEMA_SNAPSHOT


class Store:
    def __init__(self):
        self.snapshots = {'a': {'consumer': 'A'}, 'b': {'consumer': 'B'}}
        self.heads = {'a': 1, 'b': 1}
        self.row = None
        self.reads = []
        self.writes = 0
    def generation(self):
        return sum(self.heads.values())
    def domain_bindings_at(self, generation):
        return [(SCHEMA_SNAPSHOT, f'{sid}:snap:{seq:010d}'.encode()) for sid, seq in self.heads.items()]
    def load_snapshot(self, sid):
        self.reads.append(sid)
        return self.snapshots[sid]
    def read_projection_row(self, identity):
        return self.row
    def write_projection_row(self, identity, version, row):
        self.row = version, row
        self.writes += 1


def test_index_skips_unrelated_snapshots_and_validates_selected_record():
    store = Store()
    assert selection.select(store, {'consumer': 'A'}, lambda row: row.get('consumer') == 'A') == ['a']
    assert store.reads == ['a', 'b']
    store.reads = []
    assert selection.select(store, {'consumer': 'A'}, lambda row: row.get('consumer') == 'A') == ['a']
    assert store.reads == ['a'] and store.writes == 1
    store.snapshots['b']['consumer'] = 'A'
    store.heads['b'] += 1
    assert selection.select(store, {'consumer': 'A'}, lambda row: row.get('consumer') == 'A') == ['a', 'b']


def test_corrupt_index_recovers_and_terminal_selected_snapshot_is_refused():
    store = Store()
    select = lambda: selection.select(store, {'consumer': 'A'}, lambda row: row.get('consumer') == 'A')
    assert select() == ['a']
    store.row[1]['selection']['matches'] = ['missing']
    assert select() == ['a']
    store.snapshots['a']['consumer'] = 'done'
    store.heads['a'] += 1
    assert select() == []


def test_malformed_index_and_cas_race_replan_without_authority(monkeypatch):
    store = Store()
    select = lambda: selection.select(store, {'consumer': 'A'}, lambda row: row.get('consumer') == 'A')
    assert select() == ['a']
    store.row[1]['selection']['matches'] = [{}]
    assert select() == ['a']
    store.heads['b'] += 1
    original = selection.projection_store.write_row
    calls = []
    def publish(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise selection.projection_store.ProjectionConflict('selection', None)
        return original(*args, **kwargs)
    monkeypatch.setattr(selection.projection_store, 'write_row', publish)
    assert select() == ['a']
    assert len(calls) == 2
