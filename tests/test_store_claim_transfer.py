"""Claim transfer crash and concurrency proofs on a real EmbeddedStore.

These tests use a temporary Store owned by the test process. They never open
the canonical Environment Store.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from mncs_env import claims, sessions
from mncs_env.session_store import open_store

SOURCE_SESSION = "ses_transfer_source"
RECIPIENT_SESSION = "ses_transfer_recipient"
RECIPIENT_CONSUMER = "consumer_transfer_recipient"


def _seed_store(state_dir: Path):
    store = open_store(
        state_dir, "store", verify_on_open=False, defer_mutation=False
    )
    record = claims.acquire(
        store,
        repository="mncs-environment-test-fixture",
        session_id=SOURCE_SESSION,
        consumer_id="consumer_transfer_source",
        basis=claims.BASIS_EXPLICIT,
        reason="transfer crash test fixture",
    )
    return store, record


def test_targeted_claim_read_returns_latest_selected_identities(tmp_path: Path) -> None:
    store, selected = _seed_store(tmp_path / "targeted-claims")
    claims.release(
        store,
        session_id=SOURCE_SESSION,
        claim_id=selected["claim_id"],
        request_id="targeted-claim-history-release",
    )
    unrelated = claims.acquire(
        store,
        repository="mncs-environment-unrelated-fixture",
        session_id="ses_targeted_unrelated",
        consumer_id="consumer_targeted_unrelated",
        basis=claims.BASIS_EXPLICIT,
        reason="unrelated targeted history fixture",
    )

    latest = store.read_latest_claims_for([selected["claim_id"]])

    assert [record["version"] for record in latest] == [2]
    assert {record["claim_id"] for record in latest} == {selected["claim_id"]}
    assert latest[0]["status"] == "released"
    assert unrelated["claim_id"] not in {record["claim_id"] for record in latest}
    with pytest.raises(ValueError):
        store.read_latest_claims_for([f"claim:{index}" for index in range(9)])


def _transfer_after_restart(store, claim_id: str, request_id: str) -> dict:
    return claims.transfer(
        store,
        claim_id=claim_id,
        from_session=SOURCE_SESSION,
        to_session=RECIPIENT_SESSION,
        to_consumer=RECIPIENT_CONSUMER,
        reason="continue after interrupted publication",
        request_id=request_id,
    )


def _seed_session(store, session_id: str, consumer_id: str,
                  claim_ids: list[str] | None = None) -> None:
    store.save_snapshot(session_id, {
        "schema_version": "mncs.environment.session/5",
        "session_id": session_id,
        "consumer_id": consumer_id,
        "consumer_kind": "agent",
        "lifecycle": "active",
        "snapshot_sequence": 1,
        "campaign": {
            "identity": "cmp_transfer_recovery_test",
            "claim_ids": list(claim_ids or []),
            "repository_refs": [],
        },
        "workspace": {},
        "selected_checkouts": {},
        "authority": {},
        "checkpoints": [],
        "accepted_handoffs": [],
    })


@pytest.mark.parametrize(
    ("interrupt_at", "expected_owner_after_reopen"),
    [
        ("before_batch", SOURCE_SESSION),
        ("before_generation_publication", SOURCE_SESSION),
        ("after_generation_publication", None),
        ("after_head_publication", RECIPIENT_SESSION),
    ],
)
def test_transfer_process_crash_recovers_atomic_owner(
    tmp_path: Path,
    interrupt_at: str,
    expected_owner_after_reopen: str | None,
) -> None:
    state_dir = tmp_path / "environment-state"
    store, source = _seed_store(state_dir)
    store.close()
    request_id = f"req_transfer_crash_{interrupt_at}"
    script = """
import os
import sys

from mncs_env import claims
from mncs_env.session_store import open_store

state_dir, claim_id, interrupt_at, request_id = sys.argv[1:]
store = open_store(
    state_dir, "store", verify_on_open=False, defer_mutation=False
)

def crash(point):
    if point == interrupt_at:
        os._exit(71)

if interrupt_at == "before_batch":
    store.put_claim_batch = lambda *_args, **_kwargs: os._exit(71)
else:
    store.backend._store._failpoint = crash

claims.transfer(
    store,
    claim_id=claim_id,
    from_session="ses_transfer_source",
    to_session="ses_transfer_recipient",
    to_consumer="consumer_transfer_recipient",
    reason="continue after interrupted publication",
    request_id=request_id,
)
raise SystemExit(0)
"""
    environment = dict(os.environ)
    repository_root = str(Path(__file__).resolve().parents[1])
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = ":".join(
        value for value in (repository_root, existing_pythonpath) if value
    )
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(state_dir),
            source["claim_id"],
            interrupt_at,
            request_id,
        ],
        cwd=repository_root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 71, (child.returncode, child.stdout, child.stderr)

    reopened = open_store(
        state_dir, "store", verify_on_open=False, defer_mutation=False
    )
    try:
        versions = [
            item for item in reopened.read_claims()
            if item.get("claim_id") == source["claim_id"]
        ]
        matching_transfer = [
            item for item in versions
            if item.get("provenance", {}).get("transfer_request_id") == request_id
        ]
        assert len(matching_transfer) in (0, 2)
        active_before_retry = claims.active_claims(versions)
        assert set(active_before_retry) == {source["claim_id"]}
        owner_before_retry = active_before_retry[source["claim_id"]]["session_id"]
        assert owner_before_retry in (SOURCE_SESSION, RECIPIENT_SESSION)
        if expected_owner_after_reopen is not None:
            assert owner_before_retry == expected_owner_after_reopen

        moved = _transfer_after_restart(reopened, source["claim_id"], request_id)
        assert moved["session_id"] == RECIPIENT_SESSION

        versions = [
            item for item in reopened.read_claims()
            if item.get("claim_id") == source["claim_id"]
        ]
        matching_transfer = [
            item for item in versions
            if item.get("provenance", {}).get("transfer_request_id") == request_id
        ]
        assert len(matching_transfer) == 2
        active = claims.active_claims(versions)
        assert set(active) == {source["claim_id"]}
        assert active[source["claim_id"]]["session_id"] == RECIPIENT_SESSION
        assert moved["provenance"]["transferred_from"] == SOURCE_SESSION
        assert moved["provenance"]["transferred_by"] == SOURCE_SESSION
        assert moved["provenance"]["transfer_request_id"] == request_id
    finally:
        reopened.close()


def test_concurrent_store_transfers_admit_one_recipient(tmp_path: Path) -> None:
    state_dir = tmp_path / "environment-state"
    seed, source = _seed_store(state_dir)
    seed.close()

    stores = [
        open_store(state_dir, "store", verify_on_open=False, defer_mutation=False)
        for _ in range(2)
    ]
    barrier = threading.Barrier(2)

    class FirstWriteBarrier:
        def __init__(self, store):
            self.store = store
            self.lock = threading.Lock()
            self.wait_once = True

        def __getattr__(self, name):
            return getattr(self.store, name)

        def put_claim_batch(self, batch, *, expected_generation):
            with self.lock:
                should_wait = self.wait_once
                self.wait_once = False
            if should_wait:
                barrier.wait(timeout=15)
            return self.store.put_claim_batch(
                batch, expected_generation=expected_generation
            )

    participants = [FirstWriteBarrier(store) for store in stores]

    def attempt(participant, consumer: str):
        try:
            return claims.transfer(
                participant,
                claim_id=source["claim_id"],
                from_session=SOURCE_SESSION,
                to_session=f"ses_{consumer}",
                to_consumer=consumer,
                reason="competing continuation",
                request_id=f"req_transfer_{consumer}",
            )
        except claims.ClaimConflict as error:
            return error

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(
                lambda pair: attempt(*pair),
                zip(participants, ("consumer_left", "consumer_right"), strict=True),
            ))
        winners = [value for value in outcomes if isinstance(value, dict)]
        conflicts = [
            value for value in outcomes if isinstance(value, claims.ClaimConflict)
        ]
        assert len(winners) == 1, outcomes
        assert len(conflicts) == 1, outcomes

        reopened = open_store(
            state_dir, "store", verify_on_open=False, defer_mutation=False
        )
        try:
            versions = [
                item for item in reopened.read_claims()
                if item.get("claim_id") == source["claim_id"]
            ]
            active = claims.active_claims(versions)
            assert set(active) == {source["claim_id"]}
            assert active[source["claim_id"]]["session_id"] == winners[0]["session_id"]
            transfer_requests = {
                item.get("provenance", {}).get("transfer_request_id")
                for item in versions
                if item.get("provenance", {}).get("transfer_request_id")
            }
            assert transfer_requests == {
                winners[0]["provenance"]["transfer_request_id"]
            }
        finally:
            reopened.close()
    finally:
        for store in stores:
            store.close()


def test_claim_request_identity_rejects_changed_acquire_release_and_transfer_payloads(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "environment-state"
    store, source = _seed_store(state_dir)
    request_id = "req_operation_payload_binding"
    try:
        acquired = claims.acquire(
            store,
            repository="mncs-independent-fixture",
            session_id=SOURCE_SESSION,
            consumer_id="consumer_transfer_source",
            basis=claims.BASIS_EXPLICIT,
            reason="acquire payload",
            request_id=request_id,
        )
        replayed = claims.acquire(
            store,
            repository="mncs-independent-fixture",
            session_id=SOURCE_SESSION,
            consumer_id="consumer_transfer_source",
            basis=claims.BASIS_EXPLICIT,
            reason="acquire payload",
            request_id=request_id,
        )
        assert replayed == acquired
        with pytest.raises(claims.ClaimConflict, match="another payload"):
            claims.acquire(
                store,
                repository="mncs-independent-fixture",
                session_id=SOURCE_SESSION,
                consumer_id="consumer_transfer_source",
                basis=claims.BASIS_EXPLICIT,
                reason="acquire payload",
                ttl_hours=48,
                request_id=request_id,
            )

        released = claims.release(
            store, session_id=SOURCE_SESSION, repository="mncs-independent-fixture",
            reason="release payload", request_id=request_id,
        )
        replayed_release = claims.release(
            store, session_id=SOURCE_SESSION, repository="mncs-independent-fixture",
            reason="release payload", request_id=request_id,
        )
        assert replayed_release == released
        with pytest.raises(claims.ClaimConflict, match="another payload"):
            claims.release(
                store, session_id=SOURCE_SESSION,
                repository="mncs-independent-fixture", reason="changed release payload",
                request_id=request_id,
            )

        transfer_id = "req_transfer_payload_binding"
        moved = _transfer_after_restart(store, source["claim_id"], transfer_id)
        replayed_transfer = _transfer_after_restart(store, source["claim_id"], transfer_id)
        assert replayed_transfer == moved
        with pytest.raises(claims.ClaimConflict, match="another payload"):
            claims.transfer(
                store,
                claim_id=source["claim_id"],
                from_session=SOURCE_SESSION,
                to_session=RECIPIENT_SESSION,
                to_consumer="different-recipient-label",
                reason="continue after interrupted publication",
                request_id=transfer_id,
            )
    finally:
        store.close()


def test_session_claim_release_retry_after_snapshot_failure_emits_once(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "environment-state"
    store, source = _seed_store(state_dir)
    _seed_session(store, SOURCE_SESSION, "consumer_transfer_source", [source["claim_id"]])
    session = sessions.Session(store, SOURCE_SESSION)

    def interrupted_save() -> None:
        raise OSError("caller interrupted after claim release publication")

    session._save = interrupted_save
    with pytest.raises(OSError, match="after claim release publication"):
        session.release_claim(
            "mncs-environment-test-fixture",
            reason="session claim release retry",
            request_id="req_session_claim_release_retry",
        )

    resumed = sessions.Session.open(
        state_dir=state_dir, session_id=SOURCE_SESSION, store=store,
    )
    try:
        assert resumed.release_claim(
            "mncs-environment-test-fixture",
            reason="session claim release retry",
            request_id="req_session_claim_release_retry",
        )
        release_events = [
            event for event in store.read_events(SOURCE_SESSION)
            if event.get("type") == "lease.released"
            and event.get("payload", {}).get("request_id")
            == "req_session_claim_release_retry"
        ]
        assert len(release_events) == 1
        assert not claims.active_claims(store.read_claims())
        assert source["claim_id"] not in resumed.snapshot["campaign"]["claim_ids"]
    finally:
        resumed.close()
        store.close()


def test_noop_release_request_is_durable_and_cannot_release_later_claim(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "environment-state"
    store = open_store(state_dir, "store", verify_on_open=False, defer_mutation=False)
    _seed_session(store, SOURCE_SESSION, "consumer_transfer_source")
    session = sessions.Session(store, SOURCE_SESSION)
    request_id = "req_noop_claim_release_identity"
    try:
        assert not session.release_claim(
            "mncs-later-fixture", reason="no claim yet", request_id=request_id,
        )
        later = claims.acquire(
            store,
            repository="mncs-later-fixture",
            session_id=SOURCE_SESSION,
            consumer_id="consumer_transfer_source",
            basis=claims.BASIS_EXPLICIT,
            reason="later acquisition",
        )
        assert not session.release_claim(
            "mncs-later-fixture", reason="no claim yet", request_id=request_id,
        )
        with pytest.raises(claims.ClaimConflict, match="another payload"):
            session.release_claim(
                "mncs-other-fixture", reason="changed no-op payload",
                request_id=request_id,
            )
        assert claims.active_claims(store.read_claims())[later["claim_id"]]["session_id"] == SOURCE_SESSION
    finally:
        store.close()


def test_session_transfer_retry_after_snapshot_failure_emits_one_transition(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "environment-state"
    store, source = _seed_store(state_dir)
    _seed_session(store, SOURCE_SESSION, "consumer_transfer_source", [source["claim_id"]])
    _seed_session(store, RECIPIENT_SESSION, RECIPIENT_CONSUMER)
    sender = sessions.Session(store, SOURCE_SESSION)
    request_id = "req_transfer_session_snapshot_retry"

    def interrupted_snapshot_save() -> None:
        raise OSError("caller interrupted before sender snapshot publication")

    sender._save = interrupted_snapshot_save
    with pytest.raises(OSError, match="before sender snapshot publication"):
        sender.transfer_claim(
            source["claim_id"], RECIPIENT_SESSION, RECIPIENT_CONSUMER,
            reason="session-level recovery proof", request_id=request_id,
        )
    sender.close()

    reopened = open_store(
        state_dir, "store", verify_on_open=False, defer_mutation=False
    )
    try:
        resumed_sender = sessions.Session(reopened, SOURCE_SESSION)
        moved = resumed_sender.transfer_claim(
            source["claim_id"], RECIPIENT_SESSION, RECIPIENT_CONSUMER,
            reason="session-level recovery proof", request_id=request_id,
        )
        assert moved["session_id"] == RECIPIENT_SESSION
        transfer_events = [
            event for event in reopened.read_events(SOURCE_SESSION)
            if (event.get("type") == "lease.transferred"
                or event.get("provider_type") == "lease.transferred")
            and event.get("payload", {}).get("transfer_request_id") == request_id
        ]
        assert len(transfer_events) == 1
        active = claims.active_claims(reopened.read_claims())
        assert active[source["claim_id"]]["session_id"] == RECIPIENT_SESSION
        assert source["claim_id"] not in resumed_sender.snapshot["campaign"]["claim_ids"]

        resumed_recipient = sessions.Session(reopened, RECIPIENT_SESSION)
        reconciled = resumed_recipient.reconcile_campaign_continuity()
        assert reconciled["claim_ids"] == [source["claim_id"]]
        assert resumed_recipient.snapshot["campaign"]["claim_ids"] == [source["claim_id"]]
    finally:
        reopened.close()
