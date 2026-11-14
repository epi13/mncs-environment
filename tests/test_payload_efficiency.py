"""Response contracts, lossless references, early selection and bounded scaling."""
from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mncs_env import capabilities, cli, payloads, sessions, workspace


def expand(document):
    def pointer(path):
        value = document
        for part in path[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            value = value[int(part)] if isinstance(value, list) else value[part]
        return value
    def visit(value):
        if isinstance(value, dict):
            if set(value) == {"$ref"}:
                return visit(pointer(value["$ref"]))
            return {key: visit(item) for key, item in value.items() if key != "representation"}
        if isinstance(value, list):
            return [visit(item) for item in value]
        return value
    return visit(document)


def fixture(repositories, worktrees, bindings):
    rows, bound = [], []
    for i in range(repositories):
        name = f"repo-{i}"
        trees = [{"path": f"/workspace/{name}/{j}", "head": f"{j:040x}",
                  "branch": f"refs/heads/proposal-{j}"} for j in range(worktrees + 1)]
        dependency = {"repository": "mncs-language", "head": "a" * 40,
                      "checkout_identity": "checkout:runtime", "read_only": True,
                      "source_ref": "declared-runtime", "declared_by": [name]}
        for j in range(worktrees + 1):
            rows.append({"name": f"{name}@{j}", "path": trees[j]["path"],
                         "worktrees": copy.deepcopy(trees)})
        for j in range(bindings):
            bound.append({"binding_id": f"{name}:{j}", "provider": name,
                          "availability": {"status": ["unknown", "unavailable", "available"][j % 3],
                                           "reason": "provider observation"},
                          "provenance": {"toolchain_checkouts": [copy.deepcopy(dependency)]}})
    return {"workspace": {"repositories": rows, "scan": {"status": "complete"}},
            "bindings": bound, "readiness": {"status": "UNKNOWN"},
            "claims": [{"owner": "other", "scope": "paths", "status": "held"}],
            "completion_pending": {"request_id": "unfinished-operation"}}


@pytest.mark.parametrize("counts", [(1, 0, 1), (3, 4, 8), (8, 20, 25)])
def test_compact_round_trip_retains_every_fact(counts):
    document = fixture(*counts)
    original = copy.deepcopy(document)
    result = payloads.compact(document)
    assert expand(json.loads(json.dumps(result))) == original
    assert document == original
    assert result["representation"] == "shared-json-pointers"
    if counts[1]:
        assert result["workspace"]["repositories"][1]["worktrees"] == {
            "$ref": "#/workspace/repositories/0/worktrees"}
        assert len(json.dumps(result)) < len(json.dumps(document))


def test_pointer_escaping_and_revision_distinctions():
    document = {"a~/b": {"worktrees": [{"head": "old"} ]},
                "next": {"worktrees": [{"head": "old"}]},
                "changed": {"worktrees": [{"head": "new"}]}}
    result = payloads.compact(document)
    assert result["next"]["worktrees"]["$ref"] == "#/a~0~1b/worktrees"
    assert result["changed"]["worktrees"] == [{"head": "new"}]
    assert expand(result) == document


def test_compact_growth_tracks_unique_information():
    # Compare increments, not an arbitrary response-size success threshold.
    sizes = []
    for count in (10, 20, 40):
        document = fixture(2, count, count)
        compact = payloads.compact(document)
        assert expand(compact) == document
        sizes.append((len(json.dumps(document)), len(json.dumps(compact))))
    full_increment = sizes[2][0] - sizes[1][0]
    compact_increment = sizes[2][1] - sizes[1][1]
    assert compact_increment < full_increment
    assert sizes[2][1] / sizes[1][1] < sizes[2][0] / sizes[1][0]


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True,
                          capture_output=True, text=True).stdout


@pytest.mark.parametrize("count", [0, 3, 8])
def test_discovery_reads_each_registered_inventory_once(tmp_path, monkeypatch, count):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--allow-empty", "-m", "initial")
    for index in range(count):
        git(repo, "worktree", "add", "-b", f"proposal-{index}", str(repo / ".worktrees" / str(index)))
    original = workspace._worktrees
    calls = []
    def observe(path, **kwargs):
        calls.append(str(path))
        return original(path, **kwargs)
    monkeypatch.setattr(workspace, "_worktrees", observe)
    result = workspace.discover_workspace(tmp_path)
    assert result["scan"]["status"] == "complete"
    assert len(result["repositories"]) == count + 1
    assert calls == [str(repo)]
    inventories = [row["worktrees"] for row in result["repositories"]]
    assert all(item is inventories[0] for item in inventories)
    assert expand(payloads.compact(result)) == result
    # A fresh pass after an explicit invalidation must see changed Git state.
    workspace.invalidate_repo_facts(repo)
    git(repo, "worktree", "add", "-b", "later", str(repo / ".worktrees" / "later"))
    changed = workspace.discover_workspace(tmp_path)
    assert len(changed["repositories"][0]["worktrees"]) == count + 2
    assert calls.count(str(repo)) == 2


def test_unregistered_nested_clone_keeps_its_own_inventory(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--allow-empty", "-m", "initial")
    other = repo / ".worktrees" / "foreign-clone"
    other.parent.mkdir()
    git(repo, "clone", str(repo), str(other))
    result = workspace.discover_workspace(tmp_path)
    main, nested = result["repositories"]
    assert main["worktrees"][0]["path"] == str(repo)
    assert nested["worktrees"][0]["path"] == str(other)


@pytest.mark.parametrize("section", ["workspace", "bindings", "authority", "readiness", "events"])
def test_selected_inspection_does_not_load_full_history(section):
    session = object.__new__(sessions.Session)
    session.session_id = "ses_test"
    session.snapshot = {"workspace": {"scan": {"status": "timed_out"}}, "bindings": [], "requirements": {}}
    session._log = Mock(side_effect=AssertionError("full history was read"))
    session.effective_authority = Mock(return_value={"writable": [], "claim_holders": {"other": [{}]}})
    session.recent_events = Mock(return_value={"event_count": 10000, "events": [], "truncated": True})
    result = session.inspect(section=section)
    assert result["session_id"] == "ses_test"
    assert section in result
    session._log.assert_not_called()
    if section == "events":
        session.recent_events.assert_called_once_with(limit=25)


def test_status_inventory_page_uses_ids_without_hydrating_sessions(monkeypatch, capsys):
    store = SimpleNamespace(list_sessions=Mock(return_value=[f"ses_{i}" for i in range(1000)]), close=Mock())
    monkeypatch.setattr(cli, "open_store", lambda *a: store)
    args = SimpleNamespace(session=None, summary=False, offset=25, limit=25, state_dir=Path("/unused"), persistence="file")
    assert cli.cmd_status(args) == 0
    page = json.loads(capsys.readouterr().out)
    assert page["sessions"] == [f"ses_{i}" for i in range(25, 50)]
    assert page["session_count"] == 1000 and page["next_offset"] == 50
    args.summary, args.offset, args.limit = True, 0, None
    cli.cmd_status(args)
    assert json.loads(capsys.readouterr().out)["sessions"] == []
    del args.summary, args.limit, args.offset
    cli.cmd_status(args)
    legacy = json.loads(capsys.readouterr().out)
    assert len(legacy["sessions"]) == 1000 and "next_offset" not in legacy


def test_binding_detail_selects_before_inspection(monkeypatch, capsys):
    binding = {"capability": "provider:test", "binding_id": "cap_test", "availability": {"status": "unknown"}}
    session = SimpleNamespace(snapshot={"bindings": [binding, {"capability": "other"}]}, close=Mock())
    monkeypatch.setattr(cli, "_open", lambda *a: session)
    args = SimpleNamespace(preflight=None, effect_repository=None, capability="cap_test")
    assert cli.cmd_capabilities(args) == 0
    assert json.loads(capsys.readouterr().out) == [binding]


def test_discovered_bindings_share_observations_and_preserve_provenance(tmp_path, monkeypatch):
    repo, runtime = tmp_path / 'provider', tmp_path / 'runtime'
    repo.mkdir()
    runtime.mkdir()
    monkeypatch.setattr(capabilities, '_from_semantic_contracts', lambda *a: [
        {'provider': 'provider', 'capability': f'provider:action-{i}',
         'address': str(runtime / 'bin'), 'binding_id': f'cap_{i}'} for i in range(40)])
    monkeypatch.setattr(capabilities, '_from_manifest', lambda *a: [])
    monkeypatch.setattr(capabilities, '_from_verification_inventory', lambda *a: [])
    facts = {'path': str(repo), 'head': 'a' * 40, 'source_ref': 'selected-checkout'}
    runtime_facts = {'path': str(runtime), 'observed_path': str(runtime),
                     'repository': 'runtime', 'head': 'b' * 40, 'read_only': True,
                     'checkout_identity': 'checkout:runtime', 'source_ref': 'declared-runtime'}
    bindings = capabilities.discover_capabilities(tmp_path,
        repository_roots={'provider': repo}, checkout_facts={'provider': facts},
        runtime_toolchain_roots={'runtime': runtime}, runtime_toolchain_facts={'runtime': runtime_facts})
    first = bindings[0]['provenance']
    for binding in bindings:
        assert binding['provenance']['checkout'] is first['checkout']
        assert binding['provenance']['toolchain_checkouts'] is first['toolchain_checkouts']
        assert binding['provenance']['toolchain_checkouts'][0]['head'] == 'b' * 40
    assert expand(payloads.compact({'bindings': bindings})) == {'bindings': bindings}


@pytest.mark.parametrize('backend', ['file', 'store'])
def test_compact_and_selected_reads_do_not_publish_store_generations(tmp_path, backend):
    from mncs_env.session_store import open_store
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-b', 'main')
    git(repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
        'commit', '--allow-empty', '-m', 'initial')
    store = open_store(tmp_path / 'state', backend)
    session = None
    try:
        environment = sessions.resolve_environment(definition={'name': 'read-proof',
            'intent': {'goal': 'read only compact evidence'}}, workspace_root=repo,
            state_dir=tmp_path / 'state', consumer_id='proof', store=store)
        session = sessions.Session.create(state_dir=tmp_path / 'state',
            environment=environment, consumer_id='proof', store=store)
        generation = store.generation() if backend == 'store' else None
        sequence = session.snapshot['snapshot_sequence']
        for section in ('workspace', 'bindings', 'authority', 'readiness', 'events'):
            detail = session.inspect(section=section)
            assert expand(payloads.compact(detail)) == detail
        assert session.snapshot['snapshot_sequence'] == sequence
        if generation is not None:
            assert store.generation() == generation
    finally:
        if session:
            session.close()
        elif hasattr(store, 'close'):
            store.close()
