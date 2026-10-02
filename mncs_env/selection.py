"""Recoverable session-selection projection; never session authority.

The Store metadata publication snapshot identifies changed session records.
Only those records are decoded again. Candidates are validated against their
actual current snapshot before entry; a malformed index falls back to bounded
selection and is replaced through the existing projection CAS law.
"""
from __future__ import annotations

from .identity import digest_hex
from . import projection_store
from .store_backend import SCHEMA_SNAPSHOT

SCHEMA = 'mncs.environment.session-selection-index/1'


def _heads(store):
    found = {}
    for schema, identity in store.domain_bindings_at(store.generation()):
        if schema != SCHEMA_SNAPSHOT:
            continue
        text = identity.decode('utf-8')
        session, separator, ordinal = text.partition(':snap:')
        if separator:
            sequence = int(ordinal)
            if sequence > found.get(session, -1):
                found[session] = sequence
    return found


def select(store, selector, matches, *, attempt=0):
    if not callable(getattr(store, 'domain_bindings_at', None)):
        return [sid for sid in store.list_sessions() if matches(store.load_snapshot(sid) or {})]
    identity = 'entry:index/' + digest_hex(selector, length=64)
    row = projection_store.read_row(store, identity)
    cached = row.get('selection') or {}
    heads = _heads(store)
    if len(heads) > 4096:
        raise ValueError('session selection exceeds bounded index')
    valid = (cached.get('schema_version') == SCHEMA and cached.get('selector') == selector
             and isinstance(cached.get('heads'), dict) and isinstance(cached.get('matches'), list)
             and all(isinstance(key, str) and type(value) is int and value >= 0 for key, value in cached['heads'].items())
             and all(isinstance(value, str) for value in cached['matches'])
             and set(cached['matches']).issubset(cached['heads'])
             and cached.get('identity') == digest_hex({key: value for key, value in cached.items() if key != 'identity'}, length=64))
    old = cached.get('heads', {}) if valid else {}
    candidates = set(cached.get('matches', [])) & set(heads) if valid else set()
    for sid, sequence in heads.items():
        if old.get(sid) != sequence or sid in candidates:
            snapshot = store.load_snapshot(sid) or {}
            if matches(snapshot):
                candidates.add(sid)
            else:
                candidates.discard(sid)
    material = {'schema_version': SCHEMA, 'selector': selector, 'heads': heads, 'matches': sorted(candidates)}
    current = dict(material, identity=digest_hex(material, length=64))
    if current != cached:
        row['selection'] = current
        try:
            projection_store.write_row(store, row, expected_version=row['version'])
        except projection_store.ProjectionConflict:
            if attempt >= 3:
                raise ValueError('session selection index advanced repeatedly; retry entry')
            return select(store, selector, matches, attempt=attempt + 1)
    return sorted(candidates)
