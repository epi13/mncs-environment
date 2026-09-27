"""Campaign proofs: two-agent, bypass, restart, language, health, resource,
idle, context, reuse, memory.

Each proof exercises real behavior (real store, real sessions, real sockets)
and asserts the outcome. Proofs that need an absent optional binary report
SKIP with a reason instead of failing. Any FAIL exits nonzero.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mncs_env import (  # noqa: E402
    authority,
    briefing,
    capabilities,
    claims,
    reconciler,
    sessions,
)
from mncs_env.session_store import open_store  # noqa: E402

FAMILY = ROOT.parent
RESULTS: list[tuple[str, str, str]] = []


def check(name: str, condition: bool, detail: str) -> None:
    RESULTS.append((name, "PASS" if condition else "FAIL", detail))
    if not condition:
        print(f"FAIL {name}: {detail}")


def skip(name: str, reason: str) -> None:
    RESULTS.append((name, "SKIP", reason))
    print(f"SKIP {name}: {reason}")


def make_session(state_dir: Path, consumer: str):
    store = open_store(state_dir, "store", verify_on_open=False)
    env = sessions.resolve_environment(
        definition={"name": "proof-env", "intent": {"goal": "campaign proof"}},
        workspace_root=ROOT,
        state_dir=state_dir,
        consumer_id=consumer,
        store=store,
    )
    session = sessions.Session.create(
        state_dir=state_dir, environment=env, consumer_id=consumer, store=store)
    session.transition("resolving", "proof")
    session.transition("ready", "proof")
    session.transition("active", "proof")
    return session


def proof_two_agent() -> None:
    with tempfile.TemporaryDirectory(prefix="proof-two-agent-") as directory:
        state = Path(directory)
        agent_a = make_session(state, "agent-a")
        agent_b = make_session(state, "agent-b")
        record = agent_a.acquire_claim("proof-repo", basis="explicit-claim",
                                       reason="agent a work")
        assert record["session_id"] == agent_a.session_id
        try:
            agent_b.acquire_claim("proof-repo", basis="explicit-claim",
                                  reason="agent b conflicting work")
            conflict = None
        except claims.ClaimConflict as error:
            conflict = str(error)
        check("two-agent.conflict", conflict is not None and
              agent_a.session_id in conflict,
              f"conflicting acquire denied naming holder: {conflict}")
        # Peer session's overlapping mutation is denied by live authority.
        agent_b._refresh_holders()
        verdict = agent_b.check(action="write", target="proof-repo",
                                repo_facts={"proof-repo": {"clean": True,
                                                          "main_branch": True,
                                                          "foreign_signals": []}})
        check("two-agent.authority-deny", verdict["verdict"] == "deny",
              f"peer write verdict: {verdict}")
        # Handoff path: transfer moves the claim; the receiver may proceed.
        moved = agent_a.transfer_claim(record["claim_id"],
                                       to_session=agent_b.session_id,
                                       to_consumer="agent-b", reason="proof")
        agent_b._refresh_holders()
        verdict2 = agent_b.check(action="write", target="proof-repo",
                                 repo_facts={})
        check("two-agent.handoff", moved["session_id"] == agent_b.session_id
              and verdict2["verdict"] in ("allow", "escalate"),
              f"claim moved to {moved['session_id']}, write now {verdict2['verdict']}")


def proof_bypass() -> None:
    with tempfile.TemporaryDirectory(prefix="proof-bypass-") as directory:
        state = Path(directory)
        session = make_session(state, "agent-a")
        canary = state / "planted.txt"
        binding = capabilities.probe_availability(capabilities.bind(
            provider="proof-repo", capability="proof-write",
            contract_revision="1", entrypoint="e", address="/bin/sh",
            effects=["write"]))
        session.snapshot["bindings"].append(binding)
        # No claim: escalation, nothing executes.
        outcome = session.invoke("proof-write", ["-c", f"echo planted > {canary}"])
        check("bypass.no-claim-no-exec",
              outcome["status"] == "pending-escalation" and not canary.exists(),
              f"no-claim invoke -> {outcome['status']}, canary exists: {canary.exists()}")
        # Live claim: the same invoke executes.
        session.acquire_claim("proof-repo", basis="explicit-claim", reason="proof")
        outcome2 = session.invoke("proof-write", ["-c", f"echo planted > {canary}"])
        check("bypass.claim-executes",
              outcome2["status"] == "ok" and canary.read_text().strip() == "planted",
              f"claimed invoke -> {outcome2['status']}")
        # Released claim: authority fails closed again, nothing executes.
        canary.unlink()
        session.release_claim("proof-repo", reason="proof done")
        outcome3 = session.invoke("proof-write", ["-c", f"echo planted > {canary}"])
        check("bypass.release-revokes",
              outcome3["status"] == "pending-escalation" and not canary.exists(),
              f"released invoke -> {outcome3.get('status')}, canary exists: {canary.exists()}")


def proof_restart() -> None:
    with tempfile.TemporaryDirectory(prefix="proof-restart-") as directory:
        state = Path(directory)
        store = open_store(state, "store", verify_on_open=False)
        session, created = reconciler.open_or_create_session(state, store, ROOT)
        assert created
        first = reconciler.reconcile_once(session, store, ROOT)
        assert first["observations"] == []
        # A peer writes a claim through its own handle, then everything closes
        # (simulated observer downtime).
        peer = open_store(state, "store", verify_on_open=False)
        try:
            peer.put_claim({"schema_version": "mncs.environment.claim/1",
                            "claim_id": "claim:restart", "version": 1,
                            "repository": "restart-repo", "session_id": "other",
                            "consumer_id": "other", "basis": "explicit-claim",
                            "reason": "restart probe", "status": "held",
                            "acquired_at": "2026-01-01T00:00:00",
                            "expires_at": "2027-01-02T00:00:00",
                            "provenance": {}, "identity": "clm_restart"})
        finally:
            peer.close()
        store.close()
        # Observer restarts: reopen, resume the same session, missed event replays once.
        reopened = open_store(state, "store", verify_on_open=False)
        resumed, created_again = reconciler.open_or_create_session(state, reopened, ROOT)
        kinds = [o["kind"] for o in
                 reconciler.reconcile_once(resumed, reopened, ROOT)["observations"]]
        again = reconciler.reconcile_once(resumed, reopened, ROOT)
        reopened.close()
        check("restart.resume", not created_again
              and resumed.session_id == session.session_id,
              f"same reconciler session resumed: {resumed.session_id}")
        check("restart.missed-event-once",
              "claim.changed" in kinds and again["observations"] == [],
              f"missed claim replayed once, second pass clean: {kinds}")


def proof_health() -> None:
    with tempfile.TemporaryDirectory(prefix="proof-health-") as directory:
        state = Path(directory)
        store = open_store(state, "store", verify_on_open=False)
        session, _ = reconciler.open_or_create_session(state, store, ROOT)
        reconciler.reconcile_once(session, store, ROOT)
        health = reconciler.session_health(session)
        check("health.shape",
              health["consumer_id"] == "environment-reconciler"
              and "store-replay" in health["cursors"]
              and health["cycles"] == 1,
              f"consumer={health['consumer_id']} cursors={health['cursors']} "
              f"cycles={health['cycles']}")
        store.close()


def proof_idle_resource() -> None:
    with tempfile.TemporaryDirectory(prefix="proof-idle-") as directory:
        state = Path(directory)
        store = open_store(state, "store", verify_on_open=False)
        session, _ = reconciler.open_or_create_session(state, store, ROOT)
        reconciler.reconcile_once(session, store, ROOT)
        generation = store.generation()
        durations = []
        for _ in range(3):
            started = time.monotonic()
            report = reconciler.reconcile_once(session, store, ROOT)
            durations.append(time.monotonic() - started)
            assert report["observations"] == []
        check("idle.noop", store.generation() == generation,
              f"idle cycles wrote nothing (generation {generation})")
        check("resource.bounded",
              max(durations) < 5.0,
              f"3 idle cycles took {[f'{d:.2f}s' for d in durations]}")
        store.close()


def proof_context() -> None:
    with tempfile.TemporaryDirectory(prefix="proof-context-") as directory:
        state = Path(directory)
        session = make_session(state, "agent-a")
        session.checkpoint(progress="half", remaining=["rest"])
        capsule = briefing.build_capsule(session, session.store)
        check("context.bounded",
              len(capsule["items"]) <= briefing.MAX_BRIEF_ITEMS
              and capsule["cursor"]["total"] > 0,
              f"{len(capsule['items'])} items, total events "
              f"{capsule['cursor']['total']}, truncated={capsule['truncated']}")
        acked = briefing.acknowledge(session,
                                     capsule["cursor"]["total"], "agent-a")
        capsule2 = briefing.build_capsule(session, session.store)
        check("context.ack-advances",
              acked["cursor"]["index"] == capsule["cursor"]["total"]
              and capsule2["cursor"]["index"] == acked["cursor"]["index"]
              and len(capsule2["items"]) == 0,
              f"cursor now {capsule2['cursor']['index']}, "
              f"{len(capsule2['items'])} new items")


def proof_reuse() -> None:
    with tempfile.TemporaryDirectory(prefix="proof-reuse-") as directory:
        state = Path(directory)
        session = make_session(state, "agent-a")
        session.checkpoint(progress="half", remaining=["rest"])
        handoff = session.handoff(to_consumer="agent-b", next_actions=["rest"])
        session_id = session.session_id
        resumed = sessions.Session.resume(state_dir=state, session_id=session_id,
                                          store=session.store)
        accepted = resumed.accept_handoff(handoff["identity"],
                                            consumer_id="agent-b")
        check("reuse.resume",
              resumed.session_id == session_id
              and len(resumed.snapshot.get("checkpoints", [])) == 2
              and accepted["consumer_id"] == "agent-b"
              and accepted["handoff_id"] == handoff["identity"],
              f"resumed {resumed.session_id} with checkpoints + handoff accepted")


def proof_language() -> None:
    host = FAMILY / "mncs-language-service" / "target" / "debug" / "mnls-language-service-host"
    if not host.is_file():
        skip("language.live-delta", f"host binary absent: {host}")
        return
    with tempfile.TemporaryDirectory(prefix="proof-language-") as directory:
        work = Path(directory) / "ws"
        work.mkdir()
        sock = str(Path(directory) / "lang.sock")
        env = dict(os.environ, MNLS_WORKSPACE_ROOT=str(work),
                   MNLS_SERVICE_SOCKET=sock)
        child = subprocess.Popen([str(host)], env=env, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            for _ in range(100):
                if Path(sock).exists():
                    break
                time.sleep(0.1)
            from mncs_env import sources as sources_module
            source = sources_module.LanguageServiceSource(sock)
            base = source.observe(None)
            assert base.status == "ok", base.detail

            def call(method: str, params: dict):
                connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                connection.settimeout(20)
                connection.connect(sock)
                try:
                    connection.sendall((json.dumps(
                        {"id": 1, "method": method,
                         "params": params}) + "\n").encode())
                    data = b""
                    while b"\n" not in data:
                        chunk = connection.recv(65536)
                        if not chunk:
                            break
                        data += chunk
                finally:
                    connection.close()
                return json.loads(data.decode())

            opened = call("did_open", {"uri": f"file://{work}/proof.mncs",
                                       "version": 1, "text": "module proof where\n"})
            assert opened.get("ok"), opened
            delta = source.observe(base.cursor)
            kinds = [o.kind for o in delta.events]
            check("language.live-delta",
                  delta.status == "ok" and kinds == ["semantic.changed"],
                  f"host change -> {kinds}: "
                  f"{delta.events[0].summary if delta.events else '-'}")
            quiet = source.observe(delta.cursor)
            check("language.dedup",
                  quiet.status == "ok" and quiet.events == [],
                  "re-poll after cursor is silent")
        finally:
            child.terminate()


def proof_memory() -> None:
    memory = FAMILY / "mncs-memory"
    if not (memory / ".git").is_dir():
        skip("memory.untouched", "mncs-memory checkout absent")
        return
    head = subprocess.run(["git", "-C", str(memory), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    porcelain = subprocess.run(["git", "-C", str(memory), "status", "--porcelain"],
                               capture_output=True, text=True).stdout
    diff = subprocess.run(["git", "-C", str(memory), "diff", "HEAD", "--stat"],
                          capture_output=True, text=True).stdout
    mine = [line for line in (porcelain + diff).splitlines()
            if "mncs-environment" in line or "campaign_proof" in line
            or session_mark() in line]
    check("memory.untouched", not mine,
          f"HEAD={head}; no campaign artifacts in working tree "
          f"(porcelain lines: {len(porcelain.splitlines())}, all foreign)")
    print(f"memory HEAD: {head}")


def session_mark() -> str:
    return "proof-agent"


def main() -> int:
    proofs = [proof_two_agent, proof_bypass, proof_restart, proof_health,
              proof_idle_resource, proof_context, proof_reuse,
              proof_language, proof_memory]
    for proof in proofs:
        try:
            proof()
        except Exception as error:  # one proof never kills the rest
            RESULTS.append((proof.__name__, "FAIL", f"raised: {error!r}"))
            print(f"FAIL {proof.__name__}: raised {error!r}")
    print()
    for name, status, detail in RESULTS:
        print(f"{status:4} {name}")
    failed = [name for name, status, _ in RESULTS if status == "FAIL"]
    print(f"\nCAMPAIGN PROOFS: {'FAIL ' + str(failed) if failed else 'PASS'} "
          f"({len(RESULTS) - len(failed)}/{len(RESULTS)} ok)")
    # Fail-closed authority sanity: unknown actions deny, never execute.
    verdict = authority.evaluate({"denied": [], "claim_holders": {}},
                                 action="bogus-action", target="x",
                                 session_id="s")
    assert verdict["verdict"] == "deny", verdict
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
