"""Real native policy, durable Environment sessions, and concurrent Store rows.

The consumer edit and verification verdict are bounded integration fixtures;
this proves orchestration/persistence, not general remote Test equivalence.
"""
from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import signal
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from mncs_env import family, sessions
from mncs_env.projection_store import ProjectionConflict
from mncs_env.session_store import open_store
from test_family import _draft, _git, _write_repo


def _session(root, state, role, selected, backend):
    definition = {"name": role, "workspace_scope": {"kind": "workspace", "repositories": selected},
                  "intent": {"goal": "bounded collaborative proof", "repositories": selected}}
    environment = sessions.resolve_environment(definition=definition, workspace_root=root,
        state_dir=state, consumer_id=role, backend=backend)
    session = sessions.Session.create(state_dir=state, environment=environment,
                                     consumer_id=role, backend=backend)
    session.transition("resolving", "proof")
    session.transition("ready", "proof")
    session.transition("active", "proof")
    return session


def _participant(state, session_id, backend, operation, change, queue):
    try:
        session = sessions.Session.open(state_dir=state, session_id=session_id, backend=backend)
        try:
            if operation == "repair":
                result = family.converge(session, change, "consumer")
            elif operation == "adopt":
                checkout = Path(session.snapshot["selected_checkouts"]["consumer"]["path"])
                content = (checkout / "data.json").read_bytes()
                assert json.loads(content)["revision"] == "rev-2"
                session.snapshot["verification_state"] = {"consumer:ob-1": {"evidence": {
                    "verdict": "PASS", "evidence_id": hashlib.sha256(content).hexdigest()}}}
                session._save()
                result = family.adopt_pending(session, change, "consumer")
            else:
                result = family.ambient_pass(session, converge_repairs=False)
            queue.put({"session": session_id, "result": result})
        finally:
            session.close()
    except Exception as error:
        queue.put({"session": session_id, "error": repr(error)})
        raise


def _interruptible_participant(state, session_id, backend, ready, release):
    session = sessions.Session.open(state_dir=state, session_id=session_id, backend=backend)
    session.snapshot["coherence_recovery_token"] = "committed-before-process-death"
    session._save()
    session.store.write_projection_row("coherence:recovery-proof", 1,
        {"session": session_id, "token": session.snapshot["coherence_recovery_token"]})
    ready.set()
    release.wait(timeout=60)
    session.close()


def _race(state, backend, owner, barrier, queue):
    store = open_store(state, backend)
    try:
        prior = store.read_projection_row("family:cas-proof")
        version = prior[0] if prior else 0
        barrier.wait(timeout=30)
        try:
            store.write_projection_row("family:cas-proof", version + 1, {"owner": owner})
            result = {"owner": owner, "won": True}
        except ProjectionConflict as conflict:
            latest = store.read_projection_row("family:cas-proof")
            assert latest and latest[1]["owner"] != owner
            store.write_projection_row("family:cas-proof", latest[0] + 1,
                                       {"owner": owner, "observed": latest[1]["owner"]})
            result = {"owner": owner, "won": False, "replanned": True}
        queue.put(result)
    finally:
        close = getattr(store, "close", None)
        if close:
            close()


def _run(ctx, state, backend, requests, change):
    queue = ctx.Queue()
    processes = [ctx.Process(target=_participant,
        args=(state, sid, backend, operation, change, queue)) for sid, operation in requests]
    for process in processes:
        process.start()
    results = [queue.get(timeout=90) for _ in processes]
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0, results
    queue.close()
    queue.join_thread()
    assert not any("error" in result for result in results), results
    return {result["session"]: result["result"] for result in results}


@pytest.mark.parametrize("backend", ["store", "file"])
def test_concurrent_family_sessions_recovery_and_quiet_state(tmp_path, monkeypatch, backend):
    binary = os.environ.get("MNCS_BINARY", "/home/epi13/Documents/Projects/mncs-language/target/release/mncs")
    library = os.environ.get("MNCS_LIBRARY_ROOT", "/home/epi13/Documents/Projects/mncs-stdlib/library")
    commons = os.environ.get("MNCS_COMMONS_ROOT", str(Path(__file__).resolve().parents[2] / "MNCS-Commons"))
    if not Path(binary).is_file() or not Path(library).is_dir() or not (Path(commons) / family.CHANGE_SOURCE).is_file():
        pytest.skip("selected native proof providers unavailable")
    monkeypatch.setenv("MNCS_BIN", binary)
    monkeypatch.setenv("MNCS_LIBRARY_ROOT", library)
    monkeypatch.setenv("MNCS_COMMONS_ROOT", commons)
    root = tmp_path / "workspace"
    state = tmp_path / "state"
    producer = _write_repo(root, "producer", provides=["prod.contract.v1"])
    consumer = _write_repo(root, "consumer", consumes=["prod.contract.v1"])
    _write_repo(root, "unrelated")
    selected = ["producer", "consumer"]
    participants = [_session(root, state, role, ["unrelated"] if role == "C" else selected, backend)
                    for role in ["A", "B", "C", "D"]]
    a, b, c, d = participants
    b.acquire_claim("consumer", reason="bounded proof worktree")
    preimage = "sha256:" + hashlib.sha256((consumer / "data.json").read_bytes()).hexdigest()
    draft = _draft(a.session_id, "producer", _git(producer, "rev-parse", "HEAD"), 0,
        [{"contract": "prod.contract.v1", "from": "rev-1", "to": "rev-2"}],
        [{"op": "set_json_field", "params": {"field": "revision", "to": "rev-2"},
          "paths": ["data.json"], "preimage": {"data.json": preimage}}],
        [{"identity": "prod.subject", "kind": "contract", "paths": ["data.json"]}],
        obligations=["consumer:ob-1"])
    change = family.establish_change(a, family.publish_change(a, draft)["identity"])["identity"]
    ids = [session.session_id for session in participants]
    for session in participants:
        session.close()
    ctx = multiprocessing.get_context("spawn")
    observed = _run(ctx, state, backend, [(sid, "observe") for sid in ids], change)
    assert observed[ids[2]]["summary"]["relevant"] == 0
    assert observed[ids[1]]["summary"]["relevant"] == 1
    repaired = _run(ctx, state, backend, [(ids[1], "repair"), (ids[2], "observe"), (ids[3], "observe")], change)
    assert repaired[ids[1]]["converged"]
    adopted = _run(ctx, state, backend, [(ids[1], "adopt")], change)
    assert adopted[ids[1]]["adopted"]
    e = _session(root, state, "E", selected, backend)
    eid = e.session_id
    e.close()
    late = _run(ctx, state, backend, [(eid, "observe"), (ids[3], "observe")], change)
    assert late[eid]["summary"]["escalated"] == 0
    resumed = sessions.Session.open(state_dir=state, session_id=eid, backend=backend)
    row_id = family.reconciliation_row_id(resumed, change, "consumer")
    version, row = resumed.store.read_projection_row(row_id)
    assert row["consumer_class"] == "current" and row["attempts"] == 1
    assert family.read_change(resumed, change)["state"] == "established"
    assert len(family.read_contributors(resumed)) == 5
    family.ambient_pass(resumed, converge_repairs=False)
    versions = resumed.store.read_projection_versions()
    generation = resumed.store.backend._store.current_generation if backend == "store" else None
    durations = []
    from unittest.mock import patch
    with patch.object(family, "native_call", side_effect=AssertionError("quiet native spawn")):
        for _ in range(5):
            started = time.perf_counter()
            assert family.ambient_pass(resumed, converge_repairs=False)["reused"]
            durations.append(time.perf_counter() - started)
    assert resumed.store.read_projection_versions() == versions
    if backend == "store":
        assert resumed.store.backend._store.current_generation == generation
    resumed.close()
    # Kill only this test-owned Environment participant after a durable commit.
    # No normal close runs; a fresh process/store open must recover exact state.
    ready, release = ctx.Event(), ctx.Event()
    interrupted = ctx.Process(target=_interruptible_participant,
                              args=(state, eid, backend, ready, release))
    interrupted.start()
    assert ready.wait(timeout=60)
    os.kill(interrupted.pid, signal.SIGKILL)
    interrupted.join(timeout=30)
    assert interrupted.exitcode == -signal.SIGKILL
    recovered = sessions.Session.open(state_dir=state, session_id=eid, backend=backend)
    try:
        assert recovered.snapshot["coherence_recovery_token"] == "committed-before-process-death"
        assert recovered.store.read_projection_row("coherence:recovery-proof")[1]["session"] == eid
        assert recovered.store.read_projection_row(row_id)[1] == row
        assert family.read_change(recovered, change)["state"] == "established"
    finally:
        recovered.close()
    # Two writers share exactly one base version; the loser observes and replans.
    barrier = ctx.Barrier(2)
    queue = ctx.Queue()
    racers = [ctx.Process(target=_race, args=(state, backend, owner, barrier, queue)) for owner in ["one", "two"]]
    for process in racers:
        process.start()
    results = [queue.get(timeout=90) for _ in racers]
    for process in racers:
        process.join(timeout=30)
        assert process.exitcode == 0
    assert sum(result["won"] for result in results) == 1
    queue.close()
    queue.join_thread()
    with _closing_store(state, backend) as store:
        assert store.read_projection_row("family:cas-proof")[0] == 2
    print(json.dumps({"backend": backend, "sessions": 5, "repair_attempts": 1,
                      "quiet_seconds": durations, "quiet_writes": 0, "quiet_native_calls": 0}))


@contextmanager
def _closing_store(state, backend):
    store = open_store(state, backend)
    try:
        yield store
    finally:
        close = getattr(store, "close", None)
        if close:
            close()


@pytest.mark.parametrize("backend", ["store", "file"])
def test_same_repository_worktrees_do_not_share_adoption(tmp_path, monkeypatch, backend):
    binary = os.environ.get("MNCS_BINARY", "/home/epi13/Documents/Projects/mncs-language/target/release/mncs")
    library = os.environ.get("MNCS_LIBRARY_ROOT", "/home/epi13/Documents/Projects/mncs-stdlib/library")
    commons = os.environ.get("MNCS_COMMONS_ROOT", str(Path(__file__).resolve().parents[2] / "MNCS-Commons"))
    if not Path(binary).is_file() or not Path(library).is_dir():
        pytest.skip("selected native proof providers unavailable")
    monkeypatch.setenv("MNCS_BIN", binary)
    monkeypatch.setenv("MNCS_LIBRARY_ROOT", library)
    monkeypatch.setenv("MNCS_COMMONS_ROOT", commons)
    root, state = tmp_path / "workspace", tmp_path / "state"
    producer = _write_repo(root, "producer", provides=["prod.contract.v1"])
    consumer = _write_repo(root, "consumer", consumes=["prod.contract.v1"])
    second = consumer / ".worktrees" / "second"
    _git(consumer, "worktree", "add", "-b", "proof-second", str(second))
    # Unrelated local work remains isolated and untouched by the transform.
    (consumer / "notes.txt").write_text("worktree one owns this note\n")
    a = _session(root, state, "A", ["producer", "consumer"], backend)
    b = _session(root, state, "B", ["producer", "consumer"], backend)
    c = _session(root, state, "C", ["producer", "consumer"], backend)
    c.snapshot["selected_checkouts"]["consumer"].update(
        {"path": str(second), "branch": "proof-second", "clean": True})
    c._save()
    b.acquire_claim("consumer", basis="explicit-adoption", reason="proof: owned disjoint note")
    preimage = "sha256:" + hashlib.sha256((consumer / "data.json").read_bytes()).hexdigest()
    draft = _draft(a.session_id, "producer", _git(producer, "rev-parse", "HEAD"), 0,
        [{"contract": "prod.contract.v1", "from": "rev-1", "to": "rev-2"}],
        [{"op": "set_json_field", "params": {"field": "revision", "to": "rev-2"},
          "paths": ["data.json"], "preimage": {"data.json": preimage}}],
        [{"identity": "prod.subject", "kind": "contract", "paths": ["data.json"]}],
        obligations=["consumer:ob-1"])
    identity = family.establish_change(a, family.publish_change(a, draft)["identity"])["identity"]
    change = family.read_change(a, identity)
    try:
        family.record_drift(c, change, family.classify_drift(c, change, "consumer"))
        assert not family.converge(c, identity, "consumer")["converged"]
        assert json.loads((second / "data.json").read_text())["revision"] == "rev-1"
        assert family.converge(b, identity, "consumer")["converged"]
        b.snapshot["verification_state"] = {"consumer:ob-1": {"evidence": {"verdict": "PASS"}}}
        assert family.adopt_pending(b, identity, "consumer")["adopted"]
        assert family.classify_drift(b, change, "consumer")["consumer_class"] == "current"
        assert family.reconciliation_row_id(b, identity, "consumer") != family.reconciliation_row_id(c, identity, "consumer")
        b.release_claim("consumer")
        assert family.classify_drift(c, change, "consumer")["consumer_class"] == "reconcilable"
        c.acquire_claim("consumer", scope={"kind": "worktree", "checkout": str(second), "branch": "proof-second"})
        assert family.converge(c, identity, "consumer")["converged"]
        assert json.loads((second / "data.json").read_text())["revision"] == "rev-2"
        # A PASS in B is deliberately not cross-session verification evidence.
        assert not family.adopt_pending(c, identity, "consumer")["adopted"]
        assert (consumer / "notes.txt").read_text() == "worktree one owns this note\n"
        assert (second / "notes.txt").read_text() == "alpha old-value omega\n"
        family.transition_change(a, identity, "superseded")
        assert family.ambient_pass(c, converge_repairs=False)["summary"]["relevant"] == 0
        with pytest.raises(family.FamilyError, match="only established"):
            family.converge(c, identity, "consumer")
    finally:
        for session in (a, b, c):
            session.close()
