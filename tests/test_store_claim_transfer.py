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

from mncs_env import claims
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
