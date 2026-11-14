"""Bounded first-contact navigation contracts; no provider semantics duplicated."""
import json
from types import SimpleNamespace
from unittest.mock import Mock
from mncs_env import cli, sessions


def test_selected_projects_precede_historical_worktrees_and_unknown_stays_unknown():
    session = object.__new__(sessions.Session)
    roots = [{'name': 'a', 'path': '/a', 'branch': 'main', 'dirty': False}]
    history = [{'name': f'a@{i}', 'worktree_of': 'a', 'path': f'/a/{i}', 'dirty': False}
               for i in range(80)]
    session.snapshot = {'workspace': {'repositories': roots + history + [
        {'name': 'provider', 'path': '/provider', 'branch': 'main', 'dirty': False}]},
        'selected_checkouts': {
            'provider': {'path': '/provider', 'branch': 'main', 'clean': True},
            'a': {'path': '/a/selected', 'branch': 'work', 'clean': None}}}
    projects = session._context_projects(['a'])
    assert projects[0] == {'repository': 'a', 'path': '/a/selected', 'branch': 'work', 'dirty': None}
    assert projects[1]['repository'] == 'provider'
    assert len(projects) == 20
    assert len({row['path'] for row in projects}) == 20


def test_summary_pages_include_all_availability_states_and_link_exact_details(monkeypatch, capsys):
    bindings = [{'binding_id': f'cap_{i}', 'capability': f'provider:cap-{i}', 'provider': 'provider',
                 'contract_revision': '1', 'availability': {'status': ('available', 'unknown', 'unavailable')[i % 3],
                    'reason': f'provider-owned reason {i}'},
                 'provenance': {'checkout': {'head': f'{i:040x}', 'source_ref': 'owner-manifest'}}}
                for i in range(120)]
    session = SimpleNamespace(session_id='ses_test', snapshot={'bindings': bindings}, close=Mock())
    monkeypatch.setattr(cli, '_open', lambda args: session)
    args = SimpleNamespace(summary=True, limit=25, offset=0, preflight=None,
                           capability=None, effect_repository=None)
    retrieved = []
    while True:
        assert cli.cmd_capabilities(args) == 0
        page = json.loads(capsys.readouterr().out)
        assert page['binding_count'] == 120
        retrieved.extend(page['bindings'])
        if page['next_offset'] is None:
            break
        args.offset = page['next_offset']
    assert [row['binding_id'] for row in retrieved] == [row['binding_id'] for row in bindings]
    assert {row['availability']['status'] for row in retrieved} == {'available', 'unknown', 'unavailable'}
    args.summary, args.offset, args.capability = False, 0, 'cap_1'
    assert cli.cmd_capabilities(args) == 0
    assert json.loads(capsys.readouterr().out) == [bindings[1]]
    # Established consumers keep the full binding list, including provenance.
    args.capability = None
    cli.cmd_capabilities(args)
    assert json.loads(capsys.readouterr().out) == bindings


def test_summary_validation_happens_before_open(monkeypatch):
    monkeypatch.setattr(cli, '_open', Mock(side_effect=AssertionError('Store opened')))
    assert cli.cmd_capabilities(SimpleNamespace(summary=True, limit=101, offset=0)) == 2
    assert cli.cmd_capabilities(SimpleNamespace(summary=False, limit=25, offset=1)) == 2
