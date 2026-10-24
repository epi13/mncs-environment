"""Selection advances from committed Store deltas and rebuilds on pressure."""
from mncs_env import selection
from mncs_env.store_backend import SCHEMA_SNAPSHOT


class Store:
    def __init__(self):
        self.snapshots = {}
        self.bindings = []
        self.row = None
        self.reads = []
        self.writes = 0
        self.delta_cursors = []
        self.add_snapshot('a', {'consumer': 'A'})
        self.add_snapshot('b', {'consumer': 'B'})

    def generation(self):
        return len(self.bindings)

    def add_snapshot(self, sid, snapshot):
        self.snapshots[sid] = snapshot
        sequence = 1 + sum(1 for _, schema, identity in self.bindings
                           if schema == SCHEMA_SNAPSHOT
                           and identity.decode().startswith(sid + ':snap:'))
        self.bindings.append((self.generation() + 1, SCHEMA_SNAPSHOT,
                              f'{sid}:snap:{sequence:010d}'.encode()))

    def add_unrelated_records(self, count):
        for _ in range(count):
            self.bindings.append((self.generation() + 1, b'mncs.unrelated/1', b'row'))

    def domain_bindings_at(self, generation):
        return [(schema, identity) for committed, schema, identity in self.bindings
                if committed <= generation]

    def domain_bindings_since(self, generation, *, max_generations):
        self.delta_cursors.append(generation)
        head = self.generation()
        if head - generation > max_generations:
            raise ValueError('delta replay gap exceeds bound')
        return tuple((committed, schema, identity)
                     for committed, schema, identity in self.bindings
                     if committed > generation)

    def load_snapshot(self, sid):
        self.reads.append(sid)
        return self.snapshots[sid]

    def read_projection_row(self, identity):
        return self.row

    def write_projection_row(self, identity, version, row):
        self.row = version, row
        self.writes += 1
        self.bindings.append((self.generation() + 1, b'mncs.environment.projection/1',
                              f'{identity}:{version}'.encode()))


def select(store):
    return selection.select(
        store, {'consumer': 'A'}, lambda row: row.get('consumer') == 'A',
    )


def test_warm_index_replays_only_new_session_snapshots_and_rechecks_matches():
    store = Store()
    assert select(store) == ['a']
    assert store.reads == ['a', 'b']
    store.reads = []

    # The index's own publication is a generation after its saved cursor. It
    # is ignored without causing a write loop; the selected snapshot is still
    # revalidated before it can be returned for entry.
    assert select(store) == ['a']
    assert store.delta_cursors == [2]
    assert store.reads == ['a']
    assert store.writes == 1

    store.snapshots['b'] = {'consumer': 'A'}
    store.add_snapshot('b', store.snapshots['b'])
    store.reads = []
    assert select(store) == ['a', 'b']
    assert store.reads == ['a', 'b']
    assert store.writes == 2


def test_corrupt_or_malformed_index_recovers_from_canonical_bindings():
    store = Store()
    assert select(store) == ['a']
    store.row[1]['selection']['matches'] = ['missing']
    assert select(store) == ['a']
    assert store.writes == 2

    store.row[1]['selection']['heads'] = {'a': True}
    assert select(store) == ['a']
    assert store.writes == 3

    store.add_snapshot('a', {'consumer': 'done'})
    assert select(store) == []


def test_cursor_refresh_is_bounded_and_does_not_loop_on_its_own_write():
    store = Store()
    assert select(store) == ['a']
    store.add_unrelated_records(selection.REFRESH_CURSOR_AFTER)

    assert select(store) == ['a']
    assert store.writes == 2
    refreshed = store.row[1]['selection']['generation']
    assert refreshed == store.generation() - 1

    assert select(store) == ['a']
    assert store.writes == 2


def test_excessive_delta_gap_falls_back_to_a_bounded_full_reindex():
    store = Store()
    assert select(store) == ['a']
    store.add_unrelated_records(selection.MAX_REPLAY_GENERATIONS + 1)

    assert select(store) == ['a']
    assert store.writes == 2
    assert store.row[1]['selection']['generation'] == store.generation() - 1


def test_projection_cas_race_replans_without_becoming_authority(monkeypatch):
    store = Store()
    assert select(store) == ['a']
    store.add_snapshot('b', {'consumer': 'A'})

    original = selection.projection_store.write_row
    calls = []

    def publish(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise selection.projection_store.ProjectionConflict('selection', None)
        return original(*args, **kwargs)

    monkeypatch.setattr(selection.projection_store, 'write_row', publish)
    assert select(store) == ['a', 'b']
    assert len(calls) == 2
