"""Bounded observation/encoding of declared semantic subjects (no rendering)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

SCHEMA = 'mncs.semantic-projection/1'
MAX_BYTES = 8 * 1024 * 1024
# Aggregators fan out over many repositories; total cost stays byte-bounded.
MAX_SUBJECTS = 128
# Record directories stay enumerable; growth past the cap fails loud rather
# than silently truncating the observed collection.
MAX_DIR_FILES = 1024


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


def _observe_json_dir(path, budget):
    """Observe a directory of JSON records as sorted [{file, record}].

    Returns (value, consumed, status, reason). Unparseable records are
    counted and excluded, never fabricated; unrelated files are ignored.
    """
    try:
        is_dir = path.is_dir()
    except OSError:
        return None, 0, 'missing', 'directory not present'
    if not is_dir:
        if path.exists():
            return None, 0, 'invalid', 'subject path is not a directory'
        return None, 0, 'missing', 'directory not present'
    try:
        files = sorted(item for item in path.iterdir()
                       if item.is_file() and not item.is_symlink()
                       and item.name.endswith('.json'))
    except OSError:
        return None, 0, 'missing', 'directory not listable'
    if len(files) > MAX_DIR_FILES:
        raise ValueError('subject directory exceeds record bound')
    records, invalid, consumed = [], 0, 0
    for item in files:
        try:
            raw = item.read_bytes()
        except OSError:
            invalid += 1
            continue
        consumed += len(raw)
        if consumed > budget:
            raise ValueError('subject observation exceeds byte bound')
        try:
            records.append({'file': item.name,
                            'record': json.loads(raw.decode('utf-8'))})
        except (ValueError, UnicodeDecodeError):
            invalid += 1
    reason = '%d records' % len(records)
    if invalid:
        reason += '; %d unreadable (excluded, not fabricated)' % invalid
    return records, consumed, 'present', reason


def _decode_subject(raw, kind, pointer, optional):
    """Decode one observed file; returns (value, status)."""
    try:
        if kind == 'json':
            return select(json.loads(raw), pointer), 'present'
        if kind == 'utf-8':
            return raw.decode('utf-8'), 'present'
        return 'sha256:' + hashlib.sha256(raw).hexdigest(), 'present'
    except (ValueError, KeyError, IndexError, TypeError, UnicodeDecodeError):
        if not optional:
            raise
        return None, 'invalid'


def observe(checkout, declaration, renderer_identity=None, resolve=None,
            expand=None):
    """Exact selected values determine epochs; unrelated fields do not.

    Subjects naming another repository resolve through `resolve` (session
    selection first, workspace confinement as fallback); without a
    resolver only own-checkout subjects are observable. A subject with
    repository '*' expands through `expand` over the session's selected
    family membership; the expansion is recorded, so membership changes
    invalidate exactly like content changes.
    """
    subjects = declaration['subjects']
    if not isinstance(subjects, list) or not 0 < len(subjects) <= MAX_SUBJECTS:
        raise ValueError('subject count outside bounded contract')
    own = declaration.get('repository')
    output = declaration.get('output')
    entry = (declaration.get('renderer') or {}).get('entry')
    if entry is not None and (
            not isinstance(entry, dict)
            or not isinstance(entry.get('module'), str) or not entry['module']
            or not isinstance(entry.get('callable'), str) or not entry['callable']):
        raise ValueError('malformed renderer entry')
    values, sources, total = {}, [], 0
    expanded_total = 0
    for subject in subjects:
        slot_repo = subject.get('repository') or own
        kind = subject.get('format', 'json')
        pointer = subject.get('select', '')
        if kind not in ('json', 'utf-8', 'digest', 'json-dir'):
            raise ValueError('unsupported subject observation format')
        if kind == 'json-dir' and pointer:
            raise ValueError('directory observations carry whole records')
        if pointer and kind != 'json':
            raise ValueError('only JSON observations accept a pointer')
        if pointer and not pointer.startswith('/'):
            raise ValueError('subject selector must be an RFC 6901 JSON pointer')
        if slot_repo == '*':
            total, expanded_total = _observe_wildcard(
                subject, kind, pointer, values, sources, total,
                expanded_total, checkout, own, output, resolve, expand)
            continue
        if slot_repo and slot_repo != own and resolve is None:
            raise ValueError('cross-repository subject without resolver')
        slot_checkout = checkout
        if slot_repo and slot_repo != own:
            slot_checkout = resolve(slot_repo)
            if slot_checkout is None:
                raise ValueError('subject repository unavailable: %s' % slot_repo)
        if output and slot_repo == own and subject['path'] == output:
            raise ValueError('projection feedback dependency')
        optional = subject.get('required', True) is False
        status = 'present'
        path = confined(Path(slot_checkout), subject['path'])
        value = None
        if kind == 'json-dir':
            records, consumed, dir_status, reason = _observe_json_dir(
                path, MAX_BYTES - total)
            total += consumed
            if dir_status == 'missing' and not optional:
                raise OSError('subject directory unavailable: %s' % subject['path'])
            if dir_status == 'invalid' and not optional:
                raise ValueError('subject directory invalid: %s' % subject['path'])
            value, status = (records, dir_status) if dir_status == 'present' else (None, dir_status)
        else:
            try:
                with path.open('rb') as handle:
                    raw = handle.read(MAX_BYTES - total + 1)
            except OSError:
                if not optional:
                    raise
                raw = None
                status = 'missing'
            if raw is not None:
                total += len(raw)
                if total > MAX_BYTES:
                    raise ValueError('subject observation exceeds byte bound')
                value, status = _decode_subject(raw, kind, pointer, optional)
        slot = subject['slot']
        if slot in values:
            raise ValueError('duplicate subject slot')
        values[slot] = value
        sources.append({'subject': subject['subject'], 'slot': slot,
                        'repository': slot_repo, 'path': subject['path'],
                        'format': kind, 'select': subject.get('select', ''),
                        'status': status, 'identity': identity(value)})
    # Adoption witnesses are not semantic content: a view embedding its own
    # source identity could otherwise never match its declared preimage.
    contract = {key: value for key, value in declaration.items()
                if key not in ('repository', 'checkout', '_inventory', 'bootstrap_digest')}
    core = {'schema_version': 'mncs.semantic-state/1', 'values': values,
            'sources': sources, 'declaration_identity': identity(contract),
            'renderer_identity': renderer_identity, 'renderer_entry': entry}
    return dict(core, identity=identity(core))


def _observe_wildcard(subject, kind, pointer, values, sources, total,
                      expanded_total, checkout, own, output, resolve, expand):
    """Expand one repository-'*' subject over family membership.

    Rows are individually tolerant: each member reports present/missing/
    invalid with its value or reason, so one unreadable member can neither
    brick the observation nor vanish silently. The sorted expansion is
    recorded in the model, so membership changes invalidate.
    """
    if expand is None or resolve is None:
        raise ValueError('wildcard subject without expansion')
    try:
        members = sorted(set(expand()))
    except (OSError, ValueError):
        raise
    except Exception as error:
        raise ValueError('family expansion failed: %s' % error)
    if not all(isinstance(name, str) and name for name in members):
        raise ValueError('family expansion returned invalid members')
    optional = subject.get('required', True) is False
    slot = subject['slot']
    if slot in values:
        raise ValueError('duplicate subject slot')
    if not members:
        if not optional:
            raise ValueError('wildcard expansion is empty')
        values[slot] = None
        sources.append({'subject': subject['subject'], 'slot': slot,
                        'repository': '*', 'path': subject['path'],
                        'format': kind, 'select': subject.get('select', ''),
                        'status': 'missing', 'identity': identity(None),
                        'expanded': []})
        return total, expanded_total
    if expanded_total + len(members) > MAX_SUBJECTS:
        raise ValueError('wildcard expansion exceeds subject bound')
    rows = []
    for name in members:
        row = {'repository': name, 'status': 'present', 'value': None,
               'reason': 'observed'}
        member_checkout = resolve(name) if name != own else checkout
        if member_checkout is None:
            row.update(status='missing', reason='repository unavailable')
            rows.append(row)
            continue
        if output and name == own and subject['path'] == output:
            raise ValueError('projection feedback dependency')
        try:
            path = confined(Path(member_checkout), subject['path'])
        except ValueError:
            row.update(status='invalid', reason='subject path escapes checkout')
            rows.append(row)
            continue
        if kind == 'json-dir':
            records, consumed, dir_status, reason = _observe_json_dir(
                path, MAX_BYTES - total)
            total += consumed
            if dir_status == 'present':
                row.update(value=records, reason=reason)
            else:
                row.update(status=dir_status, reason=reason)
            rows.append(row)
            continue
        try:
            with path.open('rb') as handle:
                raw = handle.read(MAX_BYTES - total + 1)
        except OSError:
            row.update(status='missing', reason='file not present')
            rows.append(row)
            continue
        total += len(raw)
        if total > MAX_BYTES:
            raise ValueError('subject observation exceeds byte bound')
        # Rows are individually tolerant: a bad member is reported, never fatal.
        value, row_status = _decode_subject(raw, kind, pointer, True)
        if row_status == 'invalid':
            row.update(status='invalid', reason='unparseable subject')
        else:
            row.update(value=value)
        rows.append(row)
    missing = sum(1 for row in rows if row['status'] != 'present')
    reason = '%d members observed' % len(rows)
    if missing:
        reason += '; %d unreadable (reported, not fabricated)' % missing
    values[slot] = rows
    sources.append({'subject': subject['subject'], 'slot': slot,
                    'repository': '*', 'path': subject['path'],
                    'format': kind, 'select': subject.get('select', ''),
                    'status': 'present', 'identity': identity(rows),
                    'expanded': members, 'reason': reason})
    return total, expanded_total + len(members)


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
