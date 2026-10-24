"""Recoverable session-selection projection; never session authority.

The Store metadata publication snapshot identifies changed session records.
Only those records are decoded again after the projection has established a
Store-generation cursor. A missing, stale, or malformed cursor rebuilds from
the canonical Store index. Candidates are validated against their current
snapshot before entry.
"""
from __future__ import annotations

from .identity import digest_hex
from . import projection_store
from .store_backend import SCHEMA_SNAPSHOT

SCHEMA = 'mncs.environment.session-selection-index/2'
MAX_SESSIONS = 4096
MAX_REPLAY_GENERATIONS = 4096
# Periodically persist a newer cursor even when unrelated Store records were
# the only additions. The index's own publication leaves the cursor one
# generation behind, so a clean subsequent read does not cause a write loop.
REFRESH_CURSOR_AFTER = 1024


def _add_session_heads(heads, bindings):
    for schema, identity in bindings:
        if schema != SCHEMA_SNAPSHOT:
            continue
        text = identity.decode('utf-8')
        session, separator, ordinal = text.partition(':snap:')
        if separator:
            sequence = int(ordinal)
            if sequence > heads.get(session, -1):
                heads[session] = sequence


def _heads(store, generation=None):
    if generation is None:
        generation = store.generation()
    found = {}
    _add_session_heads(found, store.domain_bindings_at(generation))
    return found


def _valid_cache(cached, selector):
    return (
        isinstance(cached, dict)
        and cached.get('schema_version') == SCHEMA
        and cached.get('selector') == selector
        and (cached.get('generation') is None
             or (type(cached.get('generation')) is int
                 and cached['generation'] >= 0))
        and isinstance(cached.get('heads'), dict)
        and isinstance(cached.get('matches'), list)
        and all(isinstance(key, str) and type(value) is int and value >= 0
                for key, value in cached['heads'].items())
        and all(isinstance(value, str) for value in cached['matches'])
        and set(cached['matches']).issubset(cached['heads'])
        and cached.get('identity') == digest_hex(
            {key: value for key, value in cached.items() if key != 'identity'},
            length=64,
        )
    )


def select(store, selector, matches, *, attempt=0):
    if not callable(getattr(store, 'domain_bindings_at', None)):
        return [sid for sid in store.list_sessions() if matches(store.load_snapshot(sid) or {})]

    identity = 'entry:index/' + digest_hex(selector, length=64)
    row = projection_store.read_row(store, identity)
    cached = row.get('selection')
    valid = _valid_cache(cached, selector)

    generation_at = getattr(store, 'generation', None)
    delta_at = getattr(store, 'domain_bindings_since', None)
    incremental = callable(generation_at) and callable(delta_at)
    try:
        observed_generation = generation_at() if callable(generation_at) else None
        if type(observed_generation) is not int or observed_generation < 0:
            incremental = False
            observed_generation = None
    except Exception:
        incremental = False
        observed_generation = None

    old = cached.get('heads', {}) if valid else {}
    heads = None
    rebuilt = not valid
    if (valid and incremental and cached['generation'] is not None
            and cached['generation'] <= observed_generation):
        if cached['generation'] == observed_generation:
            heads = dict(old)
            rebuilt = False
        else:
            try:
                changes = delta_at(
                    cached['generation'], max_generations=MAX_REPLAY_GENERATIONS,
                )
                heads = dict(old)
                _add_session_heads(
                    heads,
                    ((schema, identity) for _, schema, identity in changes),
                )
                rebuilt = False
            except Exception:
                # A gap outside the Store replay bound or an unavailable delta
                # is explicit pressure for a one-time canonical reindex.
                heads = None
                rebuilt = True
    elif valid and incremental:
        # A cache cursor from a Store generation newer than this selected
        # backend cannot describe its current state.
        rebuilt = True

    if heads is None:
        heads = _heads(store, observed_generation)

    if len(heads) > MAX_SESSIONS:
        raise ValueError('session selection exceeds bounded index')

    candidates = set(cached.get('matches', [])) & set(heads) if valid else set()
    for sid, sequence in heads.items():
        if not valid or old.get(sid) != sequence or sid in candidates:
            snapshot = store.load_snapshot(sid) or {}
            if matches(snapshot):
                candidates.add(sid)
            else:
                candidates.discard(sid)

    if incremental:
        cache_generation = observed_generation
    else:
        # Providers without a bounded delta feed keep a useful head cache but
        # do not rewrite it on every call just to track unrelated generations.
        cache_generation = None

    material = {
        'schema_version': SCHEMA,
        'selector': selector,
        'generation': cache_generation,
        'heads': heads,
        'matches': sorted(candidates),
    }
    current = dict(material, identity=digest_hex(material, length=64))

    selection_changed = (
        not valid
        or heads != old
        or sorted(candidates) != cached.get('matches', [])
    )
    cursor_refresh_due = (
        incremental
        and valid
        and cached['generation'] is not None
        and observed_generation - cached['generation'] >= REFRESH_CURSOR_AFTER
    )
    should_write = selection_changed or rebuilt or cursor_refresh_due
    if should_write:
        row['selection'] = current
        try:
            projection_store.write_row(store, row, expected_version=row['version'])
        except projection_store.ProjectionConflict:
            if attempt >= 3:
                raise ValueError('session selection index advanced repeatedly; retry entry')
            return select(store, selector, matches, attempt=attempt + 1)
    return sorted(candidates)
