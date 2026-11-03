"""A campaign groups independently authorized repository Work Intents."""
import json

import pytest

from mncs_env import authority, claims, entry, sessions
from test_authority_scope_grants import _checkout, _git


@pytest.mark.parametrize("backend", ["file", "store"])
def test_campaign_enters_distinct_repository_after_release(tmp_path, backend):
    root = tmp_path / "workspace"
    state = tmp_path / "state"
    definitions = []
    for name in ("first", "second", "third"):
        _checkout(root, name)
        definition = {
            "name": name, "working_checkout": "current", "workspace_root": "../..",
            "workspace_scope": {"kind": "workspace", "repositories": [name]},
            "intent": {"goal": f"bounded development in {name}", "repositories": [name]},
        }
        path = root / name / ".mncs/environment.json"
        path.write_text(json.dumps(definition))
        _git(root / name, "add", ".mncs/environment.json")
        _git(root / name, "commit", "-m", "declare development scope")
        definitions.append((definition, path))

    def enter(index, principal="owner", campaign="cmp_continuity"):
        definition, path = definitions[index]
        return entry.enter(
            definition=definition, definition_path=path, workspace_root=str(root),
            state_dir=state, backend=backend, consumer_id="agent", consumer_kind="agent",
            authenticated_principal_id=principal, campaign_id=campaign,
        )

    first = enter(0)
    session = sessions.Session.open(state_dir=state, session_id=first["session_id"], backend=backend)
    try:
        session.acquire_claim("first", reason="bounded operation")
        session.release_claim("first", reason="operation released")
        session.checkpoint(progress="first operation released", remaining=["next repository"])
    finally:
        session.close()
    # Regression trigger: a released/checkpointed operation must not impose its
    # definition or writable repository as the ceiling of the whole campaign.
    second = enter(1)
    assert second["session_id"] != first["session_id"]
    session = sessions.Session.open(state_dir=state, session_id=second["session_id"], backend=backend)
    try:
        assert session.effective_authority()["writable"] == ["second"]
        assert authority.evaluate(session.effective_authority(), action="merge", target="first", session_id=session.session_id)["verdict"] == "deny"
        session.acquire_claim("second", reason="second operation")
        contender_context = enter(1, campaign="cmp_concurrent")
        contender = sessions.Session.open(
            state_dir=state, session_id=contender_context["session_id"], backend=backend,
        )
        try:
            with pytest.raises(claims.ClaimConflict):
                contender.acquire_claim("second", reason="must respect live owner")
        finally:
            contender.close()
        session.complete(outcome="success", summary="second operation completed")
    finally:
        session.close()
    third = enter(2)
    assert third["session_id"] not in {first["session_id"], second["session_id"]}
    assert enter(0)["session_id"] == first["session_id"]
    with pytest.raises(entry.EntryError) as denied:
        enter(2, principal="foreign")
    assert denied.value.diagnostics["code"] == "campaign-owner-conflict"


@pytest.mark.parametrize("backend", ["file", "store"])
def test_campaign_continues_across_standalone_workspaces(tmp_path, backend):
    """The incident: development -> standalone orientation -> development."""
    state = tmp_path / "state"
    family = tmp_path / "family"
    other_family = tmp_path / "other-family"
    for root, name in ((family, "first"), (other_family, "actions"),
                       (other_family, "debug")):
        _checkout(root, name)
    definition = {
        "name": "first-development", "working_checkout": "current",
        "workspace_scope": {"repositories": ["first"]},
        "intent": {"goal": "first operation", "repositories": ["first"]},
    }
    path = family / "first/.mncs/environment.json"
    path.write_text(json.dumps(definition))
    _git(family / "first", "add", ".mncs/environment.json")
    _git(family / "first", "commit", "-m", "development entry")

    def enter(d, p, root, principal="owner"):
        return entry.enter(
            definition=d, definition_path=p, workspace_root=str(root),
            state_dir=state, backend=backend, consumer_id="agent", consumer_kind="agent",
            authenticated_principal_id=principal, campaign_id="cmp_cross_workspace",
        )

    first = enter(definition, path, family)
    session = sessions.Session.open(state_dir=state, session_id=first["session_id"], backend=backend)
    try:
        session.acquire_claim("first", reason="isolated first operation")
        session.release_claim("first", reason="operation done")
        session.checkpoint(progress="first operation done")
    finally:
        session.close()
    orientation, _ = entry.select_definition(None, str(other_family / "actions"))
    second = enter(orientation, None, other_family / "actions")
    assert second["writable_repositories"] == []
    assert second["development_entry"]["status"] == "orientation-only"
    observed = entry.workspace.verify_registered_checkout(other_family / "actions")
    session = sessions.Session.open(state_dir=state, session_id=second["session_id"], backend=backend)
    try:
        assert entry._working_checkout_matches_snapshot(session.snapshot, observed)
        refs = session.snapshot["campaign"]["repository_refs"]
        assert len(refs) == 1
        assert refs[0]["observed_path"] == "."
        # Older sessions excluded the workspace's own repository reference.
        historical = dict(session.snapshot)
        historical["campaign"] = dict(historical["campaign"], repository_refs=[])
        assert entry._working_checkout_matches_snapshot(historical, observed)
        wrong = dict(observed, path=str(other_family / "debug"))
        assert not entry._working_checkout_matches_snapshot(historical, wrong)
        with pytest.raises(sessions.AuthorityDenied):
            session.acquire_claim("first", reason="campaign membership cannot grant rights")
        session.checkpoint(progress="orientation is durable and read-only")
    finally:
        session.close()
    debug, _ = entry.select_definition(None, str(other_family / "debug"))
    third = enter(debug, None, other_family / "debug")
    assert third["session_id"] != second["session_id"]
    assert enter(orientation, None, other_family / "actions")["session_id"] == second["session_id"]
    assert enter(definition, path, family)["session_id"] == first["session_id"]
    # A separately defined writable workspace needs its checkout references
    # durably indexed on creation, even without working_checkout=current.
    development = {"name": "debug-development", "workspace_scope": {"repositories": ["debug"]},
                   "intent": {"goal": "independent debug operation", "repositories": ["debug"]}}
    development_path = other_family / "debug/.mncs/development.json"
    development_path.write_text(json.dumps(development))
    fourth = enter(development, development_path, other_family)
    session = sessions.Session.open(state_dir=state, session_id=fourth["session_id"], backend=backend)
    try:
        assert session.effective_authority()["writable"] == ["debug"]
        assert entry._working_checkout_matches_snapshot(
            session.snapshot, entry.workspace.verify_registered_checkout(other_family / "debug"))
        session.acquire_claim("debug", reason="independent writable workspace operation",
                              basis="explicit-adoption")
        session.release_claim("debug", reason="isolated operation done")
    finally:
        session.close()
    with pytest.raises(entry.EntryError) as denied:
        enter(debug, None, other_family / "debug", principal="foreign")
    assert denied.value.diagnostics["code"] == "campaign-owner-conflict"
