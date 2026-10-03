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
