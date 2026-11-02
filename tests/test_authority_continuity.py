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
