"""Bounded observation/encoding of declared semantic subjects (no rendering)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

SCHEMA = 'mncs.semantic-projection/1'
MAX_BYTES = 8 * 1024 * 1024
MAX_SUBJECTS = 64


def identity(value):
    return 'sha256:' + hashlib.sha256(json.dumps(value, sort_keys=True,
        separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def confined(root, relative):
    path = Path(relative)
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise ValueError('subject/target path escapes checkout')
    result = root / path
    if result.is_symlink() or not result.resolve().is_relative_to(root.resolve()):
        raise ValueError('subject/target symlink escapes checkout')
    return result


def select(value, pointer):
    if pointer == '':
        return value
    if not pointer.startswith('/'):
        raise ValueError('subject selector must be an RFC 6901 JSON pointer')
    for token in pointer[1:].split('/'):
        token = token.replace('~1', '/').replace('~0', '~')
        value = value[int(token)] if isinstance(value, list) else value[token]
    return value


def observe(checkout, declaration, renderer_identity=None):
    """Exact selected values determine epochs; unrelated fields do not."""
    subjects = declaration['subjects']
    if not isinstance(subjects, list) or not 0 < len(subjects) <= MAX_SUBJECTS:
        raise ValueError('subject count outside bounded contract')
    values, sources, total = {}, [], 0
    for subject in subjects:
        path = confined(checkout, subject['path'])
        with path.open('rb') as handle:
            raw = handle.read(MAX_BYTES - total + 1)
        total += len(raw)
        if total > MAX_BYTES:
            raise ValueError('subject observation exceeds byte bound')
        kind = subject.get('format', 'json')
        if kind == 'json':
            value = select(json.loads(raw), subject.get('select', ''))
        else:
            if subject.get('select'):
                raise ValueError('only JSON observations accept a pointer')
            if kind == 'utf-8':
                value = raw.decode('utf-8')
            elif kind == 'digest':
                value = 'sha256:' + hashlib.sha256(raw).hexdigest()
            else:
                raise ValueError('unsupported subject observation format')
        slot = subject['slot']
        if slot in values:
            raise ValueError('duplicate subject slot')
        values[slot] = value
        sources.append({'subject': subject['subject'], 'slot': slot,
                        'path': subject['path'], 'format': kind, 'select': subject.get('select', ''),
                        'identity': identity(value)})
    contract = {key: value for key, value in declaration.items()
                if key not in ('repository', 'checkout', '_inventory')}
    core = {'schema_version': 'mncs.semantic-state/1', 'values': values,
            'sources': sources, 'declaration_identity': identity(contract),
            'renderer_identity': renderer_identity}
    return dict(core, identity=identity(core))


def output_identity(checkout, declaration):
    try:
        raw = confined(checkout, declaration['output']).read_bytes()
        if declaration.get('output_kind') == 'region':
            begin, end = b'<!-- MNCS:generated:begin -->', b'<!-- MNCS:generated:end -->'
            if raw.count(begin) != 1 or raw.count(end) != 1:
                return 'malformed-region'
            first, last = raw.index(begin) + len(begin), raw.index(end)
            if first > last:
                return 'malformed-region'
            raw = raw[first:last]
        return 'sha256:' + hashlib.sha256(raw).hexdigest()
    except OSError:
        return None


def validate_output(data, declaration, *, schema=None):
    """Syntax/schema transport check; never an execution verdict."""
    validation = declaration.get('validation') or {}
    kind = validation.get('format', 'utf-8')
    decoded = data.decode('utf-8')
    if kind == 'json':
        def invalid_constant(value):
            raise ValueError('non-JSON numeric constant: ' + value)
        value = json.loads(decoded, parse_constant=invalid_constant)
        if schema or validation.get('schema'):
            from jsonschema import Draft202012Validator
            Draft202012Validator(schema or validation['schema']).validate(value)
    elif kind not in ('utf-8', 'markdown'):
        raise ValueError('unsupported output validation format')
    if not data or len(data) > MAX_BYTES:
        raise ValueError('output empty or outside byte bound')
