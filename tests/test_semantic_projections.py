"""Semantic dependencies and ownership transport, including hostile boundaries."""
import json
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mncs_env import projection_sources as sources
from mncs_env import projections


def declaration():
    return {'subjects': [{'subject': 'owner:capabilities', 'slot': 'capabilities',
             'path': 'state.json', 'select': '/capabilities'}],
            'output': 'README.md', 'output_kind': 'whole-file',
            'renderer': {'identity': 'test', 'version': '1', 'schema_version': '1'}}


def test_unrelated_state_does_not_invalidate(tmp_path):
    p = tmp_path / 'state.json'
    p.write_text(json.dumps({'capabilities': ['a'], 'internals': 1}))
    first = sources.observe(tmp_path, declaration())
    p.write_text(json.dumps({'internals': 2, 'capabilities': ['a']}))
    assert sources.observe(tmp_path, declaration()) == first
    p.write_text(json.dumps({'internals': 2, 'capabilities': ['a', 'b']}))
    assert sources.observe(tmp_path, declaration())['identity'] != first['identity']


@pytest.mark.parametrize('field', ['version', 'schema_version', 'identity'])
def test_renderer_identity_changes_invalidate(tmp_path, field):
    (tmp_path / 'state.json').write_text('{"capabilities": []}')
    d = declaration()
    first = sources.observe(tmp_path, d)
    d['renderer'][field] = 'changed'
    assert sources.observe(tmp_path, d)['identity'] != first['identity']


def test_cross_repository_subject_resolves_through_selection(tmp_path):
    own = tmp_path / 'own'
    sibling = tmp_path / 'sibling'
    own.mkdir()
    sibling.mkdir()
    (own / 'state.json').write_text('{"capabilities": []}')
    (sibling / 'manifest.json').write_text('{"contracts": ["a"]} ')
    contract = declaration()
    contract['repository'] = 'own-repo'
    contract['subjects'].append({'subject': 'sibling:manifest',
                                 'slot': 'peer', 'repository': 'sibling-repo',
                                 'path': 'manifest.json',
                                 'select': '/contracts'})
    observed = sources.observe(own, contract,
                               resolve=lambda name: sibling if name == 'sibling-repo' else None)
    assert observed['values']['peer'] == ['a']
    assert [entry['repository'] for entry in observed['sources']] == ['own-repo', 'sibling-repo']


def test_unselected_repository_defers_observation(tmp_path):
    (tmp_path / 'state.json').write_text('{"capabilities": []}')
    contract = declaration()
    contract['repository'] = 'own-repo'
    contract['subjects'].append({'subject': 'missing:manifest',
                                 'slot': 'peer', 'repository': 'missing-repo',
                                 'path': 'manifest.json'})
    with pytest.raises(ValueError, match='unavailable'):
        sources.observe(tmp_path, contract, resolve=lambda name: None)


def test_cross_repository_subject_requires_resolver(tmp_path):
    (tmp_path / 'state.json').write_text('{"capabilities": []}')
    contract = declaration()
    contract['subjects'].append({'subject': 'sibling:manifest',
                                 'slot': 'peer', 'repository': 'sibling-repo',
                                 'path': 'manifest.json'})
    with pytest.raises(ValueError, match='without resolver'):
        sources.observe(tmp_path, contract)


def test_optional_subject_tolerates_absence_and_recovers(tmp_path):
    (tmp_path / 'state.json').write_text('{"capabilities": []}')
    contract = declaration()
    contract['subjects'].append({'subject': 'peer:manifest', 'slot': 'peer',
                                 'path': 'absent.json', 'required': False})
    observed = sources.observe(tmp_path, contract)
    assert observed['values']['peer'] is None
    assert observed['sources'][1]['status'] == 'missing'
    (tmp_path / 'absent.json').write_text('{"contracts": []}')
    revived = sources.observe(tmp_path, contract)
    assert revived['values']['peer'] == {'contracts': []}
    assert revived['identity'] != observed['identity']


def test_required_subject_still_fails_closed_on_absence(tmp_path):
    (tmp_path / 'state.json').write_text('{"capabilities": []}')
    contract = declaration()
    contract['subjects'].append({'subject': 'peer:manifest', 'slot': 'peer',
                                 'path': 'absent.json'})
    with pytest.raises(OSError):
        sources.observe(tmp_path, contract)


def test_optional_subject_tolerates_unparseable_content(tmp_path):
    (tmp_path / 'state.json').write_text('{"capabilities": []}')
    (tmp_path / 'broken.json').write_text('{nope')
    contract = declaration()
    contract['subjects'].append({'subject': 'peer:manifest', 'slot': 'peer',
                                 'path': 'broken.json', 'required': False})
    observed = sources.observe(tmp_path, contract)
    assert observed['values']['peer'] is None
    assert observed['sources'][1]['status'] == 'invalid'


def test_declaration_bugs_fail_loud_even_when_optional(tmp_path):
    (tmp_path / 'state.json').write_text('{"capabilities": []}')
    contract = declaration()
    contract['subjects'].append({'subject': 'peer:manifest', 'slot': 'peer',
                                 'path': 'state.json', 'format': 'bogus',
                                 'required': False})
    with pytest.raises(ValueError, match='unsupported'):
        sources.observe(tmp_path, contract)


def test_explicit_own_repository_output_is_feedback(tmp_path):
    contract = declaration()
    contract['output'] = 'state.json'
    contract['subjects'][0]['repository'] = 'own-repo'
    contract['repository'] = 'own-repo'
    (tmp_path / 'state.json').write_text('{"capabilities": []}')
    with pytest.raises(ValueError, match='feedback'):
        sources.observe(tmp_path, contract)


def test_adoption_witness_does_not_change_semantic_identity(tmp_path):
    (tmp_path / 'state.json').write_text('{"capabilities": []}')
    first = sources.observe(tmp_path, declaration())
    witnessed = declaration()
    witnessed['bootstrap_digest'] = 'sha256:' + '0' * 64
    assert sources.observe(tmp_path, witnessed) == first


def test_subject_missing_is_not_an_empty_source(tmp_path):
    (tmp_path / 'state.json').write_text('{}')
    with pytest.raises(KeyError):
        sources.observe(tmp_path, declaration())


def test_pointer_preserves_unicode_and_escaped_keys():
    assert sources.select({'a/b': {'~key': ['文書']}}, '/a~1b/~0key/0') == '文書'


@pytest.mark.parametrize('path', ['/tmp/input', '../input', ''])
def test_source_paths_fail_closed(tmp_path, path):
    with pytest.raises(ValueError):
        sources.confined(tmp_path, path)


def test_source_symlink_fails_closed(tmp_path):
    (tmp_path / 'state.json').symlink_to('/etc/passwd')
    with pytest.raises(ValueError):
        sources.observe(tmp_path, declaration())


def test_authored_region_is_excluded_from_owned_identity(tmp_path):
    d = declaration()
    d['output_kind'] = 'region'
    p = tmp_path / 'README.md'
    body = b'<!-- MNCS:generated:begin -->\nowned\n<!-- MNCS:generated:end -->'
    p.write_bytes(b'author A\n' + body + b'\nauthor B')
    first = sources.output_identity(tmp_path, d)
    p.write_bytes(b'different authored bytes\n' + body + b'\n\xe6\x96\x87')
    assert sources.output_identity(tmp_path, d) == first
    p.write_bytes(body.replace(b'owned', b'hand-edit'))
    assert sources.output_identity(tmp_path, d) != first


def test_malformed_regions_have_no_adoptable_identity(tmp_path):
    d = declaration()
    d['output_kind'] = 'region'
    (tmp_path / 'README.md').write_bytes(b'<!-- MNCS:generated:begin -->')
    assert sources.output_identity(tmp_path, d) == 'malformed-region'


def test_invalid_json_and_schema_never_publish():
    d = {'validation': {'format': 'json', 'schema': {'type': 'object', 'required': ['identity']}}}
    with pytest.raises(ValueError):
        sources.validate_output(b'{broken', d)
    from jsonschema import ValidationError
    with pytest.raises(ValidationError):
        sources.validate_output(b'{}', d)


def test_interrupted_owned_write_remains_recognizable(tmp_path):
    (tmp_path / 'README.md').write_bytes(b'previous admitted output')
    row = {'pending': {'expected_digest': projections.bytes_digest(b'previous admitted output')}}
    code, reason = projections.classify_output(tmp_path, 'README.md', b'new source render', row)
    assert code == projections.OUTPUT_MATCHES_LAST_RENDER
    assert reason == 'interrupted-owned-write'


def test_no_baseline_never_authorizes_occupied_target(tmp_path):
    (tmp_path / 'README.md').write_bytes(b'meaningful authored data')
    code, reason = projections.classify_output(tmp_path, 'README.md', b'generated', {})
    assert code == projections.OUTPUT_DIVERGED
    assert reason == 'target-occupied'


@pytest.mark.parametrize('kind', ['utf-8', 'digest'])
def test_declared_non_json_source_observation(tmp_path, kind):
    p = tmp_path/'source.mncs'; p.write_text('mncs 0.18;\nmodule real.source;\n')
    d = declaration(); d['subjects'] = [{'subject':'owner:source','slot':'source','path':'source.mncs','format':kind}]
    first = sources.observe(tmp_path,d)
    assert first['values']['source'] == (p.read_text() if kind == 'utf-8' else projections.bytes_digest(p.read_bytes()))
    p.write_text(p.read_text()+'// real source change\n')
    assert sources.observe(tmp_path,d)['identity'] != first['identity']


def test_non_json_pointer_and_oversized_input_fail_closed(tmp_path):
    p = tmp_path/'state.json'; p.write_bytes(b'x'*(sources.MAX_BYTES+1))
    with pytest.raises(ValueError,match='byte bound'):
        sources.observe(tmp_path,declaration())
    p.write_bytes(b'not json'); d = declaration(); d['subjects'][0]['format']='digest'
    with pytest.raises(ValueError,match='pointer'):
        sources.observe(tmp_path,d)


def test_non_json_numeric_output_is_rejected():
    with pytest.raises(ValueError,match='non-JSON'):
        sources.validate_output(b'{"value":NaN}', {'validation':{'format':'json'}})


def test_renderer_entry_change_invalidates(tmp_path):
    (tmp_path / 'state.json').write_text('{"capabilities": []}')
    d = declaration()
    first = sources.observe(tmp_path, d)
    d['renderer']['entry'] = {'module': 'projector/ambient.py', 'callable': 'render'}
    second = sources.observe(tmp_path, d)
    assert second['identity'] != first['identity']
    assert second['renderer_entry'] == {'module': 'projector/ambient.py', 'callable': 'render'}
    assert sources.observe(tmp_path, d)['identity'] == second['identity']
    d['renderer']['entry'] = {'module': 'projector/ambient.py', 'callable': 'other'}
    assert sources.observe(tmp_path, d)['identity'] != second['identity']


@pytest.mark.parametrize('entry', [{'module': 'x.py'}, {'callable': 'f'},
                                   {'module': '', 'callable': 'f'}, 'not-a-dict'])
def test_malformed_renderer_entry_fails_loud(tmp_path, entry):
    (tmp_path / 'state.json').write_text('{"capabilities": []}')
    d = declaration()
    d['renderer']['entry'] = entry
    with pytest.raises(ValueError, match='renderer entry'):
        sources.observe(tmp_path, d)


def test_json_dir_observes_sorted_records(tmp_path):
    records = tmp_path / 'records'
    records.mkdir()
    (records / 'b.json').write_text('{"id": "b"}')
    (records / 'a.json').write_text('{"id": "a"}')
    (records / 'notes.txt').write_text('unrelated filesystem file')
    d = declaration()
    d['subjects'] = [{'subject': 'owner:records', 'slot': 'rows',
                      'path': 'records', 'format': 'json-dir'}]
    observed = sources.observe(tmp_path, d)
    assert observed['values']['rows'] == [{'file': 'a.json', 'record': {'id': 'a'}},
                                          {'file': 'b.json', 'record': {'id': 'b'}}]
    assert observed['sources'][0]['status'] == 'present'


def test_json_dir_counts_invalid_and_ignores_unrelated(tmp_path):
    records = tmp_path / 'records'
    records.mkdir()
    (records / 'good.json').write_text('{"id": "good"}')
    (records / 'bad.json').write_text('not json')
    d = declaration()
    d['subjects'] = [{'subject': 'owner:records', 'slot': 'rows',
                      'path': 'records', 'format': 'json-dir'}]
    first = sources.observe(tmp_path, d)
    assert [row['file'] for row in first['values']['rows']] == ['good.json']
    (records / 'ignored.md').write_text('docs')
    assert sources.observe(tmp_path, d)['identity'] == first['identity']
    (records / 'bad.json').write_text('{"id": "fixed"}')
    assert sources.observe(tmp_path, d)['identity'] != first['identity']
    (records / 'good.json').unlink()
    assert sources.observe(tmp_path, d)['values']['rows'] == [
        {'file': 'bad.json', 'record': {'id': 'fixed'}}]


def test_json_dir_missing_and_pointer_rules(tmp_path):
    d = declaration()
    d['subjects'] = [{'subject': 'owner:records', 'slot': 'rows',
                      'path': 'absent', 'format': 'json-dir', 'required': False}]
    assert sources.observe(tmp_path, d)['sources'][0]['status'] == 'missing'
    d['subjects'][0].pop('required')
    with pytest.raises(OSError, match='directory unavailable'):
        sources.observe(tmp_path, d)
    (tmp_path / 'records').mkdir()
    d['subjects'] = [{'subject': 'owner:records', 'slot': 'rows',
                      'path': 'records', 'format': 'json-dir', 'select': '/id'}]
    with pytest.raises(ValueError, match='whole records'):
        sources.observe(tmp_path, d)


def test_wildcard_expands_over_membership(tmp_path):
    repos = {}
    for name in ('mncs-b', 'mncs-a'):
        member = tmp_path / name
        member.mkdir()
        (member / '.mncs-project.json').write_text(json.dumps({'repository': name}))
        repos[name] = member
    d = declaration()
    d['repository'] = 'mncs-atlas'
    d['subjects'] = [{'subject': 'family:manifests', 'slot': 'manifests',
                      'repository': '*', 'path': '.mncs-project.json'}]
    observed = sources.observe(tmp_path, d, resolve=repos.get,
                               expand=lambda: list(repos))
    rows = observed['values']['manifests']
    assert [row['repository'] for row in rows] == ['mncs-a', 'mncs-b']
    assert all(row['status'] == 'present' for row in rows)
    assert rows[0]['value'] == {'repository': 'mncs-a'}
    assert observed['sources'][0]['expanded'] == ['mncs-a', 'mncs-b']


def test_wildcard_membership_change_invalidates(tmp_path):
    repos = {}
    member = tmp_path / 'mncs-a'
    member.mkdir()
    (member / 'manifest.json').write_text('{"v": 1}')
    repos['mncs-a'] = member
    d = declaration()
    d['subjects'] = [{'subject': 'family:manifests', 'slot': 'manifests',
                      'repository': '*', 'path': 'manifest.json'}]
    first = sources.observe(tmp_path, d, resolve=repos.get,
                            expand=lambda: list(repos))
    other = tmp_path / 'mncs-b'
    other.mkdir()
    (other / 'manifest.json').write_text('{"v": 1}')
    repos['mncs-b'] = other
    assert sources.observe(tmp_path, d, resolve=repos.get,
                           expand=lambda: list(repos))['identity'] != first['identity']


def test_wildcard_tolerates_unreadable_members(tmp_path):
    good = tmp_path / 'mncs-good'
    good.mkdir()
    (good / 'manifest.json').write_text('{"v": 1}')
    bad = tmp_path / 'mncs-bad'
    bad.mkdir()
    (bad / 'manifest.json').write_text('not json')
    missing = tmp_path / 'mncs-missing'
    missing.mkdir()
    repos = {'mncs-good': good, 'mncs-bad': bad, 'mncs-missing': missing}
    d = declaration()
    d['subjects'] = [{'subject': 'family:manifests', 'slot': 'manifests',
                      'repository': '*', 'path': 'manifest.json'}]
    observed = sources.observe(tmp_path, d, resolve=repos.get,
                               expand=lambda: list(repos))
    by_repo = {row['repository']: row for row in observed['values']['manifests']}
    assert by_repo['mncs-good']['status'] == 'present'
    assert by_repo['mncs-bad']['status'] == 'invalid'
    assert by_repo['mncs-missing']['status'] == 'missing'
    assert observed['sources'][0]['status'] == 'present'


def test_wildcard_requires_expansion_and_rejects_feedback(tmp_path):
    (tmp_path / 'state.json').write_text('{"capabilities": []}')
    d = declaration()
    d['subjects'] = [{'subject': 'family:all', 'slot': 'all',
                      'repository': '*', 'path': 'state.json'}]
    with pytest.raises(ValueError, match='without expansion'):
        sources.observe(tmp_path, d)
    d['repository'] = 'own-repo'
    d['output'] = 'state.json'
    with pytest.raises(ValueError, match='feedback'):
        sources.observe(tmp_path, d, resolve=lambda name: tmp_path,
                        expand=lambda: ['own-repo'])


def test_wildcard_empty_expansion(tmp_path):
    d = declaration()
    d['subjects'] = [{'subject': 'family:all', 'slot': 'all',
                      'repository': '*', 'path': 'state.json'}]
    with pytest.raises(ValueError, match='empty'):
        sources.observe(tmp_path, d, resolve=lambda name: tmp_path,
                        expand=lambda: [])
    d['subjects'][0]['required'] = False
    observed = sources.observe(tmp_path, d, resolve=lambda name: tmp_path,
                               expand=lambda: [])
    assert observed['values']['all'] is None
    assert observed['sources'][0]['status'] == 'missing'


class _StubSession:
    def __init__(self, snapshot, bindings=None):
        self.snapshot = snapshot
        self._bindings = bindings or {}
        self.store = None

    def _binding(self, capability):
        if capability not in self._bindings:
            raise KeyError(capability)
        return self._bindings[capability]


def _stub_session(root, selected=None, bindings=None):
    return _StubSession({'workspace': {'root': str(root)},
                         'selected_checkouts': selected or {}}, bindings)


def test_family_expand_prefers_selection(tmp_path):
    session = _stub_session(tmp_path, {'b-repo': {'path': 'x'}, 'a-repo': {'path': 'y'}})
    assert projections._family_expand(session) == ['a-repo', 'b-repo']


def test_family_expand_falls_back_to_workspace_scan(tmp_path):
    (tmp_path / 'aaa' / '.git').mkdir(parents=True)
    (tmp_path / 'zzz').mkdir()
    (tmp_path / 'notes.txt').write_text('not a checkout')
    session = _stub_session(tmp_path)
    assert projections._family_expand(session) == ['aaa']


def test_source_state_combines_renderer_sources(tmp_path):
    (tmp_path / 'state.json').write_text('{"capabilities": []}')
    (tmp_path / 'render.py').write_text('# renderer')
    d = declaration()
    d['checkout'] = str(tmp_path)
    d['repository'] = 'own-repo'
    d['provider_capability'] = 'test:cap'
    d['renderer']['entry'] = {'module': 'render.py', 'callable': 'render'}
    d['renderer_sources'] = ['render.py']
    session = _stub_session(tmp_path)
    first = projections.source_state(session, d)
    (tmp_path / 'render.py').write_text('# renderer changed')
    assert projections.source_state(session, d)['identity'] != first['identity']


def test_source_state_rejects_entry_without_sources(tmp_path):
    (tmp_path / 'state.json').write_text('{"capabilities": []}')
    d = declaration()
    d['checkout'] = str(tmp_path)
    d['repository'] = 'own-repo'
    d['provider_capability'] = 'test:cap'
    d['renderer']['entry'] = {'module': 'render.py', 'callable': 'render'}
    session = _stub_session(tmp_path)
    with pytest.raises(ValueError, match='without renderer_sources'):
        projections.source_state(session, d)


def test_optional_subject_tolerates_unavailable_repository(tmp_path):
    (tmp_path / 'state.json').write_text('{"capabilities": []}')
    contract = declaration()
    contract['repository'] = 'own-repo'
    contract['subjects'].append({'subject': 'missing:manifest',
                                 'slot': 'peer', 'repository': 'missing-repo',
                                 'path': 'manifest.json', 'required': False})
    observed = sources.observe(tmp_path, contract, resolve=lambda name: None)
    assert observed['values']['peer'] is None
    assert observed['sources'][1]['status'] == 'missing'
    assert observed['sources'][1]['reason'] == 'repository unavailable'


def test_sources_carry_reasons(tmp_path):
    (tmp_path / 'state.json').write_text('{"capabilities": []}')
    (tmp_path / 'bad.json').write_text('not json')
    records = tmp_path / 'records'
    records.mkdir()
    (records / 'a.json').write_text('{"id": "a"}')
    (records / 'bad.json').write_text('not json')
    d = declaration()
    d['subjects'] = [{'subject': 'o:s', 'slot': 'good', 'path': 'state.json'},
                     {'subject': 'o:b', 'slot': 'bad', 'path': 'bad.json',
                      'required': False},
                     {'subject': 'o:m', 'slot': 'gone', 'path': 'absent.json',
                      'required': False},
                     {'subject': 'o:r', 'slot': 'rows', 'path': 'records',
                      'format': 'json-dir'}]
    observed = sources.observe(tmp_path, d)
    by_slot = {entry['slot']: entry for entry in observed['sources']}
    assert by_slot['good']['reason'] == 'readable'
    assert by_slot['bad']['reason'] == 'unparseable subject'
    assert by_slot['gone']['reason'] == 'file not present'
    assert by_slot['rows']['reason'] == '1 records; 1 unreadable (excluded, not fabricated)'


def test_declaration_validation_for_repo_owned_renderers():
    base = {'schema_version': 'mncs.semantic-projection/1', 'id': 'x',
            'owner': 'o', 'subjects': [{'subject': 's', 'slot': 'v', 'path': 'p'}],
            'renderer': {'identity': 'r', 'version': '1', 'schema_version': 's'},
            'output': 'out.md', 'output_kind': 'whole-file', 'policy': 'ambient-safe',
            'manual_edit_policy': 'protected', 'validation': {'format': 'utf-8'},
            'provider_capability': 'c', 'render_argv': [], 'inputs': [], 'template': 't'}
    assert projections._validate_declaration(base) is None
    missing_sources = dict(base, renderer=dict(
        base['renderer'], entry={'module': 'r.py', 'callable': 'f'}))
    assert projections._validate_declaration(missing_sources) == 'renderer-entry-without-sources'
    bad_entry = dict(base, renderer=dict(base['renderer'], entry={'module': 'r.py'}),
                     renderer_sources=['r.py'])
    assert projections._validate_declaration(bad_entry) == 'bad-renderer-entry'
    escape = dict(base, renderer=dict(
        base['renderer'], entry={'module': '../r.py', 'callable': 'f'}),
        renderer_sources=['ok.py'])
    assert projections._validate_declaration(escape) == 'renderer-entry-escapes-checkout'
    good = dict(base, renderer=dict(
        base['renderer'], entry={'module': 'sub/r.py', 'callable': 'f'}),
        renderer_sources=['sub/r.py'])
    assert projections._validate_declaration(good) is None


class _RenderSession(_StubSession):
    def __init__(self, snapshot, state_dir, bindings, payload,
                 status='ok', truncated=False):
        super().__init__(snapshot, bindings)
        from types import SimpleNamespace
        self.store = SimpleNamespace(state_dir=Path(state_dir))
        self.session_id = 'ses_render_test'
        self._payload = payload
        self._status = status
        self._truncated = truncated
        self.invoke_calls = []

    def invoke(self, capability, argv, timeout_seconds=None,
               output_limit_bytes=None, env=None):
        self.invoke_calls.append({'capability': capability,
                                  'output_limit_bytes': output_limit_bytes})
        return {'status': self._status, 'stdout': self._payload,
                'truncated': self._truncated}


def _render_declaration(tmp_path):
    (tmp_path / 'state.json').write_text('{"capabilities": []}')
    return {'schema_version': 'mncs.semantic-projection/1', 'id': 'own:big',
            'repository': 'own-repo', 'checkout': str(tmp_path),
            'owner': 'own:state',
            'subjects': [{'subject': 'own:capabilities', 'slot': 'capabilities',
                          'path': 'state.json', 'select': '/capabilities'}],
            'renderer': {'identity': 'test-route', 'version': '1',
                         'schema_version': 'mncs.semantic-state/1'},
            'output': 'out.md', 'output_kind': 'whole-file',
            'policy': 'ambient-safe', 'manual_edit_policy': 'protected',
            'validation': {'format': 'utf-8'},
            'provider_capability': 'test:cap', 'render_argv': [],
            'inputs': [], 'template': 'test-route'}


def test_render_requests_full_transport_limit(tmp_path):
    from mncs_env import capabilities as capabilities_module
    content = 'x' * (64 * 1024 + 1024)
    session = _RenderSession(
        {'workspace': {'root': str(tmp_path)}, 'selected_checkouts': {}},
        tmp_path / 'state',
        {'test:cap': {'availability': {'status': 'available'}}}, '')
    declaration = _render_declaration(tmp_path)
    identity = projections.source_state(session, declaration)['identity']
    session._payload = json.dumps(
        {'schema_version': 'mncs.projection-render-result/1',
         'content': content, 'source_identity': identity,
         'renderer': 'test-route', 'version': '1', 'native': []})
    data, reason = projections.render_projection(session, declaration,
                                                 tmp_path, 'a')
    assert reason == 'ok'
    assert data == content.encode('utf-8')
    assert session.invoke_calls[0]['output_limit_bytes'] == \
        projections.RENDER_OUTPUT_LIMIT_BYTES
    assert session.invoke_calls[0]['output_limit_bytes'] > 64 * 1024
    assert projections.RENDER_OUTPUT_LIMIT_BYTES == \
        capabilities_module.MAX_OUTPUT_LIMIT_BYTES


def test_render_reports_truncation_distinctly(tmp_path):
    session = _RenderSession(
        {'workspace': {'root': str(tmp_path)}, 'selected_checkouts': {}},
        tmp_path / 'state',
        {'test:cap': {'availability': {'status': 'available'}}},
        '{"schema_version": "mncs.projection-render-result/1", "partial": true',
        truncated=True)
    declaration = _render_declaration(tmp_path)
    data, reason = projections.render_projection(session, declaration,
                                                 tmp_path, 'a')
    assert data is None
    assert reason == 'semantic-render-truncated'
