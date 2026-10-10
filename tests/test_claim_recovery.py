"""Claim recovery: stale/dead claims become recoverable via session liveness.

Doctrine says quiet ownership goes stale and becomes recoverable; these
tests pin the liveness-derived recovery path in `claims.acquire` with the
`recovery` basis: dead owners free their scopes before TTL expiry, live
owners are never stolen from, and concurrent recovery attempts fail safe.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from mncs_env import claims, cli
from mncs_env.session_store import open_store


def _event(sequence: int, observed_at: str) -> dict:
    return {"sequence": sequence, "type": "test.ping",
            "observed_at": observed_at, "payload": {}}


def _snapshot(session_id: str, lifecycle: str) -> dict:
    return {"session_id": session_id, "lifecycle": lifecycle,
            "lifecycle_history": [{"state": lifecycle, "at": claims.utcnow()}]}


class RecoveryFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mncs-claim-recovery-")
        self.addCleanup(self.temp.cleanup)
        self.store = open_store(self.temp.name, "file")

    def seed_session(self, session_id: str, lifecycle: str = "active",
                     last_activity: datetime | None = None,
                     events: bool = True) -> None:
        self.store.save_snapshot(session_id, _snapshot(session_id, lifecycle))
        if events and last_activity is not None:
            self.store.put_event(
                session_id, 1,
                _event(1, last_activity.isoformat(timespec="seconds")))

    def acquire_paths(self, session_id: str, paths, **kwargs):
        kwargs.setdefault("basis", claims.BASIS_EXPLICIT)
        kwargs.setdefault("consumer_id", session_id)
        kwargs.setdefault("reason", "test hold")
        return claims.acquire(
            self.store, repository="r", session_id=session_id,
            scope={"kind": "paths", "paths": list(paths)}, **kwargs)


class OwnerStateTests(RecoveryFixture):
    def test_unknown_session_is_not_known(self):
        owner = claims.owner_state(self.store, "ses_missing")
        self.assertFalse(owner["known"])
        self.assertIsNone(owner["lifecycle"])
        self.assertIsNone(owner["last_activity_at"])

    def test_known_session_reports_lifecycle_and_activity(self):
        moment = datetime.now(timezone.utc) - timedelta(minutes=3)
        self.seed_session("ses_a", "active", moment)
        owner = claims.owner_state(self.store, "ses_a")
        self.assertTrue(owner["known"])
        self.assertEqual(owner["lifecycle"], "active")
        self.assertEqual(claims._parse_time(owner["last_activity_at"]),
                         moment.replace(microsecond=0))

    def test_terminal_session_is_dead_despite_recent_activity(self):
        moment = datetime.now(timezone.utc) - timedelta(seconds=30)
        self.seed_session("ses_dead", "completed", moment)
        record = self.acquire_paths("ses_dead", ["out/gen.json"])
        verdict = claims.classify(record, claims.owner_state(self.store, "ses_dead"))
        self.assertEqual(verdict["verdict"], "recoverable")
        self.assertIn("completed", verdict["reason"])


class ClassifyTests(RecoveryFixture):
    def test_live_owner_is_live(self):
        moment = datetime.now(timezone.utc) - timedelta(minutes=2)
        self.seed_session("ses_live", "active", moment)
        record = self.acquire_paths("ses_live", ["out/gen.json"])
        verdict = claims.classify(record, claims.owner_state(self.store, "ses_live"))
        self.assertEqual(verdict["verdict"], "live")
        self.assertEqual(verdict["freshness"], "active")

    def test_idle_owner_is_still_live(self):
        moment = datetime.now(timezone.utc) - timedelta(minutes=30)
        self.seed_session("ses_idle", "active", moment)
        record = self.acquire_paths("ses_idle", ["out/gen.json"])
        verdict = claims.classify(record, claims.owner_state(self.store, "ses_idle"))
        self.assertEqual(verdict["verdict"], "live")
        self.assertEqual(verdict["freshness"], "idle")

    def test_quiet_owner_is_recoverable_for_paths_scope(self):
        moment = datetime.now(timezone.utc) - timedelta(hours=3)
        self.seed_session("ses_quiet", "active", moment)
        record = self.acquire_paths("ses_quiet", ["out/gen.json"])
        verdict = claims.classify(record, claims.owner_state(self.store, "ses_quiet"))
        self.assertEqual(verdict["verdict"], "recoverable")
        self.assertEqual(verdict["freshness"], "stale")

    def test_quiet_owner_keeps_exclusive_repository_scope(self):
        moment = datetime.now(timezone.utc) - timedelta(hours=3)
        self.seed_session("ses_quiet", "active", moment)
        record = claims.acquire(self.store, repository="r", session_id="ses_quiet",
                                consumer_id="ses_quiet", basis=claims.BASIS_EXPLICIT,
                                reason="broad lock")
        verdict = claims.classify(record, claims.owner_state(self.store, "ses_quiet"))
        self.assertEqual(verdict["verdict"], "not-recoverable")
        self.assertEqual(verdict["freshness"], "stale")

    def test_legacy_overlong_lease_is_reported_without_changing_expiry(self):
        now = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)
        record = {
            "claim_id": "claim:legacy-overlong",
            "session_id": "ses_quiet",
            "status": "held",
            "acquired_at": "2026-01-01T12:00:00+00:00",
            "expires_at": "2026-01-10T12:00:00+00:00",
            "scope": {"kind": "repository", "exclusive": True},
        }
        original_expiry = record["expires_at"]
        owner = {
            "known": True,
            "lifecycle": "active",
            "last_activity_at": "2026-01-02T09:00:00+00:00",
        }

        verdict = claims.classify(record, owner, now=now)

        self.assertEqual(verdict["verdict"], "not-recoverable")
        self.assertEqual(verdict["freshness"], "stale")
        self.assertEqual(verdict["lease"]["duration_hours"], 216.0)
        self.assertEqual(verdict["lease"]["maximum_hours"], claims.MAX_TTL_HOURS)
        self.assertEqual(verdict["lease"]["policy"], "exceeds-current-maximum")
        self.assertEqual(record["expires_at"], original_expiry)

    def test_failed_owner_frees_even_exclusive_scope(self):
        self.seed_session("ses_failed", "failed",
                          datetime.now(timezone.utc) - timedelta(minutes=1))
        record = claims.acquire(self.store, repository="r", session_id="ses_failed",
                                consumer_id="ses_failed", basis=claims.BASIS_EXPLICIT,
                                reason="broad lock")
        verdict = claims.classify(record, claims.owner_state(self.store, "ses_failed"))
        self.assertEqual(verdict["verdict"], "recoverable")

    def test_unknown_owner_fresh_claim_is_not_recoverable(self):
        record = self.acquire_paths("ses_gone", ["out/gen.json"])
        verdict = claims.classify(record, claims.owner_state(self.store, "ses_gone"))
        self.assertEqual(verdict["verdict"], "not-recoverable")

    def test_unknown_owner_quiet_claim_is_recoverable(self):
        record = self.acquire_paths("ses_gone", ["out/gen.json"])
        future = datetime.now(timezone.utc) + timedelta(hours=3)
        verdict = claims.classify(record, claims.owner_state(self.store, "ses_gone"),
                                  now=future)
        self.assertEqual(verdict["verdict"], "recoverable")

    def test_expired_claim_is_expired(self):
        record = self.acquire_paths("ses_old", ["out/gen.json"])
        future = datetime.now(timezone.utc) + timedelta(hours=25)
        verdict = claims.classify(record, claims.owner_state(self.store, "ses_old"),
                                  now=future)
        self.assertEqual(verdict["verdict"], "expired")


class AcquireRecoveryTests(RecoveryFixture):
    def test_live_owner_blocks_recovery_with_explanation(self):
        self.seed_session("ses_live", "active",
                          datetime.now(timezone.utc) - timedelta(minutes=1))
        self.acquire_paths("ses_live", ["out/gen.json"])
        self.seed_session("ses_new", "active", datetime.now(timezone.utc))
        with self.assertRaises(claims.ClaimConflict) as raised:
            self.acquire_paths("ses_new", ["out/gen.json"],
                               basis=claims.BASIS_RECOVERY, reason="takeover bid")
        self.assertIn("live", str(raised.exception))
        holders = claims.holders(self.store.read_claims())["r"]
        self.assertEqual(holders[0]["session_id"], "ses_live")

    def test_dead_owner_releases_scope_before_ttl(self):
        self.seed_session("ses_dead", "failed",
                          datetime.now(timezone.utc) - timedelta(minutes=1))
        victim = self.acquire_paths("ses_dead", ["out/gen.json"])
        self.seed_session("ses_new", "active", datetime.now(timezone.utc))
        record = self.acquire_paths("ses_new", ["out/gen.json"],
                                    basis=claims.BASIS_RECOVERY,
                                    reason="dead owner recovery")
        self.assertEqual(record["status"], "held")
        self.assertEqual(record["basis"], claims.BASIS_RECOVERY)
        self.assertEqual(record["provenance"]["recovered_from"][0]["claim_id"],
                         victim["claim_id"])
        self.assertEqual(record["provenance"]["recovered_from"][0]["version"], 1)
        history = [item for item in self.store.read_claims()
                   if item["claim_id"] == victim["claim_id"]]
        # Same scope: v1 victim held, v2 superseded, v3 new holder.
        self.assertEqual([item["status"] for item in
                          sorted(history, key=lambda item: item["version"])],
                         ["held", "superseded", "held"])
        superseded = [item for item in history if item["status"] == "superseded"][0]
        self.assertEqual(superseded["provenance"]["recovered_by"], "ses_new")
        holders = claims.holders(self.store.read_claims())["r"]
        self.assertEqual(holders[0]["session_id"], "ses_new")

    def test_quiet_owner_paths_scope_recovers(self):
        self.seed_session("ses_quiet", "active",
                          datetime.now(timezone.utc) - timedelta(hours=2))
        self.acquire_paths("ses_quiet", ["out/gen.json"])
        self.seed_session("ses_new", "active", datetime.now(timezone.utc))
        record = self.acquire_paths("ses_new", ["out/gen.json"],
                                    basis=claims.BASIS_RECOVERY,
                                    reason="stale recovery")
        self.assertEqual(record["session_id"], "ses_new")

    def test_explicit_basis_still_fails_closed_on_stale_claim(self):
        self.seed_session("ses_quiet", "active",
                          datetime.now(timezone.utc) - timedelta(hours=2))
        self.acquire_paths("ses_quiet", ["out/gen.json"])
        self.seed_session("ses_new", "active", datetime.now(timezone.utc))
        with self.assertRaises(claims.ClaimConflict):
            self.acquire_paths("ses_new", ["out/gen.json"],
                               basis=claims.BASIS_EXPLICIT, reason="ordinary")

    def test_expired_claim_needs_no_recovery(self):
        record = self.acquire_paths("ses_old", ["out/gen.json"], ttl_hours=24)
        # Age the record past TTL by rewriting it directly (durable log shape).
        expired = dict(record, expires_at=(datetime.now(timezone.utc)
                                           - timedelta(seconds=1)).isoformat(timespec="seconds"))
        store_records = self.store.read_claims()
        self.assertTrue(any(item["claim_id"] == record["claim_id"]
                            for item in store_records))
        import json as _json
        path = Path(self.temp.name) / "claims.jsonl"
        lines = [line for line in path.read_text().splitlines()
                 if _json.loads(line).get("identity") != record["identity"]]
        lines.append(_json.dumps(expired))
        path.write_text("\n".join(lines) + "\n")
        plain = self.acquire_paths("ses_new", ["out/gen.json"],
                                   basis=claims.BASIS_EXPLICIT, reason="after ttl")
        self.assertEqual(plain["status"], "held")

    def test_nested_scopes_supersede_every_overlapping_victim(self):
        # Two mutually disjoint victims nested inside one broader request.
        self.seed_session("ses_a", "completed",
                          datetime.now(timezone.utc) - timedelta(minutes=5))
        first = self.acquire_paths("ses_a", ["out/a.json"])
        self.seed_session("ses_b", "completed",
                          datetime.now(timezone.utc) - timedelta(minutes=4))
        second = self.acquire_paths("ses_b", ["out/b.json"])
        self.seed_session("ses_new", "active", datetime.now(timezone.utc))
        record = self.acquire_paths("ses_new", ["out/a.json", "out/b.json"],
                                    basis=claims.BASIS_RECOVERY,
                                    reason="nested recovery")
        self.assertEqual(record["session_id"], "ses_new")
        self.assertEqual(len(record["provenance"]["recovered_from"]), 2)
        latest = claims._latest_by_identity(self.store.read_claims())
        by_id = {item["claim_id"]: item for item in latest.values()}
        self.assertEqual(by_id[first["claim_id"]]["status"], "superseded")
        self.assertEqual(by_id[second["claim_id"]]["status"], "superseded")
        holders = claims.holders(self.store.read_claims())["r"]
        self.assertEqual(len(holders), 1)
        self.assertEqual(holders[0]["session_id"], "ses_new")

    def test_broad_victim_narrows_to_requested_scope(self):
        self.seed_session("ses_wide", "completed",
                          datetime.now(timezone.utc) - timedelta(minutes=5))
        wide = claims.acquire(
            self.store, repository="r", session_id="ses_wide",
            consumer_id="ses_wide", basis=claims.BASIS_EXPLICIT,
            reason="whole checkout",
            scope={"kind": "worktree", "checkout": self.temp.name})
        self.seed_session("ses_new", "active", datetime.now(timezone.utc))
        record = self.acquire_paths("ses_new", ["out/gen.json"],
                                    basis=claims.BASIS_RECOVERY,
                                    reason="narrow recovery")
        self.assertEqual(record["session_id"], "ses_new")
        latest = claims._latest_by_identity(self.store.read_claims())
        by_id = {item["claim_id"]: item for item in latest.values()}
        self.assertEqual(by_id[wide["claim_id"]]["status"], "superseded")

    def test_second_recovery_after_first_fails_safe(self):
        self.seed_session("ses_dead", "completed",
                          datetime.now(timezone.utc) - timedelta(minutes=5))
        self.acquire_paths("ses_dead", ["out/gen.json"])
        self.seed_session("ses_a", "active", datetime.now(timezone.utc))
        first = self.acquire_paths("ses_a", ["out/gen.json"],
                                   basis=claims.BASIS_RECOVERY, reason="winner")
        self.assertEqual(first["session_id"], "ses_a")
        self.seed_session("ses_b", "active", datetime.now(timezone.utc))
        with self.assertRaises(claims.ClaimConflict):
            self.acquire_paths("ses_b", ["out/gen.json"],
                               basis=claims.BASIS_RECOVERY, reason="loser")
        holders = claims.holders(self.store.read_claims())["r"]
        self.assertEqual(len(holders), 1)
        self.assertEqual(holders[0]["session_id"], "ses_a")

    def test_concurrent_recovery_has_exactly_one_winner(self):
        self.seed_session("ses_dead", "completed",
                          datetime.now(timezone.utc) - timedelta(minutes=5))
        self.acquire_paths("ses_dead", ["out/gen.json"])
        barrier = threading.Barrier(4)
        winners: list[str] = []
        failures: list[str] = []
        lock = threading.Lock()

        def attempt(index: int) -> None:
            session_id = f"ses_racer_{index}"
            self.seed_session(session_id, "active", datetime.now(timezone.utc))
            barrier.wait(timeout=30)
            try:
                self.acquire_paths(session_id, ["out/gen.json"],
                                   basis=claims.BASIS_RECOVERY,
                                   reason=f"racer {index}")
            except claims.ClaimConflict:
                with lock:
                    failures.append(session_id)
            else:
                with lock:
                    winners.append(session_id)

        threads = [threading.Thread(target=attempt, args=(index,))
                   for index in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(len(winners), 1, (winners, failures))
        self.assertEqual(len(failures), 3)
        holders = claims.holders(self.store.read_claims())["r"]
        self.assertEqual(len(holders), 1)
        self.assertEqual(holders[0]["session_id"], winners[0])

    def test_owner_renewal_defeats_inflight_recovery(self):
        self.seed_session("ses_owner", "active",
                          datetime.now(timezone.utc) - timedelta(hours=2))
        victim = self.acquire_paths("ses_owner", ["out/gen.json"])
        self.seed_session("ses_new", "active", datetime.now(timezone.utc))
        reads = {"count": 0}
        real_read = self.store.read_claims

        def renewing_read():
            reads["count"] += 1
            if reads["count"] == 2:
                # Owner renews between the recovery plan and its CAS write.
                claims.acquire(self.store, repository="r", session_id="ses_owner",
                               consumer_id="ses_owner", basis=claims.BASIS_EXPLICIT,
                               reason="renew",
                               scope={"kind": "paths", "paths": ["out/gen.json"]})
            return real_read()

        self.store.read_claims = renewing_read  # type: ignore[method-assign]
        try:
            with self.assertRaises(claims.ClaimConflict) as raised:
                self.acquire_paths("ses_new", ["out/gen.json"],
                                   basis=claims.BASIS_RECOVERY,
                                   reason="raced recovery")
        finally:
            self.store.read_claims = real_read  # type: ignore[method-assign]
        self.assertIn("changed during recovery", str(raised.exception))
        latest = claims._latest_by_identity(self.store.read_claims())
        by_id = {item["claim_id"]: item for item in latest.values()}
        self.assertEqual(by_id[victim["claim_id"]]["session_id"], "ses_owner")
        self.assertEqual(by_id[victim["claim_id"]]["status"], "held")

    def test_interrupted_recovery_leaves_scope_acquirable(self):
        self.seed_session("ses_dead", "completed",
                          datetime.now(timezone.utc) - timedelta(minutes=5))
        victim = self.acquire_paths("ses_dead", ["out/gen.json"])
        self.seed_session("ses_new", "active", datetime.now(timezone.utc))
        writes = {"count": 0}
        real_put = self.store.put_claim

        def crashing_put(record):
            writes["count"] += 1
            if record.get("status") == "held" and record.get("basis") == claims.BASIS_RECOVERY:
                raise RuntimeError("crash before final acquire write")
            return real_put(record)

        self.store.put_claim = crashing_put  # type: ignore[method-assign]
        try:
            with self.assertRaises(RuntimeError):
                self.acquire_paths("ses_new", ["out/gen.json"],
                                   basis=claims.BASIS_RECOVERY, reason="doomed")
        finally:
            self.store.put_claim = real_put  # type: ignore[method-assign]
        # Victim superseded, no new holder: plain acquire now succeeds.
        plain = self.acquire_paths("ses_new", ["out/gen.json"],
                                   basis=claims.BASIS_EXPLICIT, reason="retry")
        self.assertEqual(plain["status"], "held")
        history = [item for item in self.store.read_claims()
                   if item["claim_id"] == victim["claim_id"]]
        self.assertEqual([item["status"] for item in
                          sorted(history, key=lambda item: item["version"])],
                         ["held", "superseded", "held"])

    def test_claims_explain_reports_verdict(self):
        self.seed_session("ses_dead", "failed",
                          datetime.now(timezone.utc) - timedelta(minutes=5))
        victim = self.acquire_paths("ses_dead", ["out/gen.json"])
        parsed = cli.build_parser().parse_args(
            ["claims", "ses_dead", "--explain", victim["claim_id"]])
        parsed.state_dir = self.temp.name
        parsed.persistence = "file"
        with mock.patch.object(cli, "out") as printed:
            self.assertEqual(cli.cmd_claims(parsed), 0)
        verdict = printed.call_args[0][0]
        self.assertEqual(verdict["verdict"], "recoverable")
        self.assertIn("failed", verdict["reason"])

    def test_claims_explain_unknown_id_fails(self):
        parsed = cli.build_parser().parse_args(
            ["claims", "ses_dead", "--explain", "claim:r:paths:deadbeefcafe"])
        parsed.state_dir = self.temp.name
        parsed.persistence = "file"
        with mock.patch.object(cli, "out"):
            self.assertNotEqual(cli.cmd_claims(parsed), 0)

    def test_recovery_survives_store_restart(self):
        self.seed_session("ses_dead", "completed",
                          datetime.now(timezone.utc) - timedelta(minutes=5))
        self.acquire_paths("ses_dead", ["out/gen.json"])
        reopened = open_store(self.temp.name, "file")
        reopened.save_snapshot("ses_new", _snapshot("ses_new", "active"))
        reopened.put_event("ses_new", 1, _event(
            1, datetime.now(timezone.utc).isoformat(timespec="seconds")))
        record = claims.acquire(
            reopened, repository="r", session_id="ses_new",
            consumer_id="ses_new", basis=claims.BASIS_RECOVERY,
            reason="after restart",
            scope={"kind": "paths", "paths": ["out/gen.json"]})
        self.assertEqual(record["session_id"], "ses_new")


if __name__ == "__main__":
    unittest.main()
