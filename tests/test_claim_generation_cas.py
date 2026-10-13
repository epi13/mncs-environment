"""Generation preconditions on canonical Environment claim acquisition."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
from unittest.mock import patch

from mncs_env import claims, cli, rpc_client
from mncs_env.session_store import ClaimBatchConflict


class ClaimStore:
    def __init__(self, generation: int = 0):
        self.generation = generation
        self.records: list[dict] = []
        self.put_calls = 0
        self.race_on_put = False
        self.advance_on_read = False

    def claim_generation(self) -> int:
        return self.generation

    def read_claims(self) -> list[dict]:
        records = [dict(record) for record in self.records]
        if self.advance_on_read:
            self.advance_on_read = False
            self.generation += 1
        return records

    def put_claim_batch(self, records: list[dict], *, expected_generation: int) -> None:
        self.put_calls += 1
        if self.race_on_put:
            self.race_on_put = False
            self.generation += 1
            raise ClaimBatchConflict(expected_generation, self.generation)
        if expected_generation != self.generation:
            raise ClaimBatchConflict(expected_generation, self.generation)
        self.records.extend(dict(record) for record in records)
        self.generation += 1


def acquire(store: ClaimStore, **kwargs):
    return claims.acquire(
        store,
        repository="fixture",
        session_id="ses_fixture",
        consumer_id="test-consumer",
        basis=claims.BASIS_EXPLICIT,
        reason="generation fence test",
        **kwargs,
    )


class ClaimGenerationTests(unittest.TestCase):
    def test_stale_precondition_rejects_without_publication(self):
        store = ClaimStore(generation=12)

        with self.assertRaises(ClaimBatchConflict) as raised:
            acquire(store, expected_store_generation=11)

        self.assertEqual(raised.exception.stage, "generation-precondition")
        self.assertEqual(raised.exception.supplied_generation, 11)
        self.assertEqual(raised.exception.observed_generation, 12)
        self.assertEqual(store.put_calls, 0)
        self.assertEqual(store.records, [])

    def test_generation_change_while_reading_is_a_definite_conflict(self):
        store = ClaimStore(generation=12)
        store.advance_on_read = True

        with self.assertRaises(ClaimBatchConflict) as raised:
            acquire(store, expected_store_generation=12)

        self.assertEqual(raised.exception.stage, "generation-read")
        self.assertEqual(raised.exception.observed_generation, 13)
        self.assertEqual(store.put_calls, 0)

    def test_store_cas_conflict_is_not_retried_for_guarded_request(self):
        store = ClaimStore(generation=12)
        store.race_on_put = True

        with self.assertRaises(ClaimBatchConflict) as raised:
            acquire(
                store,
                expected_store_generation=12,
                request_id="req_guarded_race",
            )

        self.assertEqual(raised.exception.stage, "store-cas")
        self.assertEqual(raised.exception.supplied_generation, 12)
        self.assertEqual(store.put_calls, 1)
        self.assertEqual(store.records, [])

    def test_unguarded_acquisition_retains_bounded_cas_retry(self):
        store = ClaimStore(generation=12)
        store.race_on_put = True

        record = acquire(store)

        self.assertEqual(store.put_calls, 2)
        self.assertEqual(record["status"], "held")
        self.assertEqual(len(store.records), 1)

    def test_request_replay_returns_committed_claim_with_same_generation_token(self):
        store = ClaimStore(generation=12)
        kwargs = {
            "expected_store_generation": 12,
            "request_id": "req_guarded_replay",
        }

        first = acquire(store, **kwargs)
        replay = acquire(store, **kwargs)

        self.assertEqual(first, replay)
        self.assertEqual(store.put_calls, 1)
        self.assertEqual(
            first["provenance"]["expected_store_generation"], 12
        )

    def test_request_digest_binds_the_generation_precondition(self):
        store = ClaimStore(generation=12)
        acquire(
            store,
            expected_store_generation=12,
            request_id="req_generation_binding",
        )

        with self.assertRaises(claims.ClaimConflict):
            acquire(
                store,
                expected_store_generation=13,
                request_id="req_generation_binding",
            )


class ClaimGenerationCliTests(unittest.TestCase):
    def parse(self, *args: str):
        return cli.build_parser().parse_args(["claims", "ses_fixture", *args])

    def test_generation_option_requires_acquire_before_opening_store(self):
        parsed = self.parse("--expected-store-generation", "12")
        stderr = io.StringIO()
        with redirect_stderr(stderr), patch.object(cli, "open_store") as open_store:
            code = cli.cmd_claims(parsed)

        self.assertEqual(code, 2)
        open_store.assert_not_called()
        self.assertIn("requires --acquire", stderr.getvalue())

    def test_stale_generation_has_structured_nonpublication_diagnostic(self):
        parsed = self.parse(
            "--acquire", "fixture", "--expected-store-generation", "11"
        )
        store = SimpleNamespace(backend=SimpleNamespace(generation=lambda: 12))
        session = SimpleNamespace(
            snapshot={"lifecycle": "active", "selected_checkouts": {}},
            acquire_claim=lambda *args, **kwargs: (_ for _ in ()).throw(
                ClaimBatchConflict(
                    11, 12, stage="generation-precondition",
                    supplied_generation=11,
                )
            ),
        )
        stderr = io.StringIO()
        with redirect_stderr(stderr), \
                patch.object(cli, "open_store", return_value=store), \
                patch.object(cli.sessions_module.Session, "open", return_value=session):
            code = cli.cmd_claims(parsed)

        response = json.loads(stderr.getvalue())
        self.assertEqual(code, 3)
        self.assertEqual(response["diagnostics"]["code"], "publication-conflict")
        self.assertEqual(response["diagnostics"]["conflict_kind"], "store-generation")
        self.assertEqual(response["diagnostics"]["conflict_stage"], "generation-precondition")
        self.assertFalse(response["diagnostics"]["publication_completed"])

    def test_rpc_projection_preserves_bounded_generation_conflict_details(self):
        details = rpc_client._domain_diagnostic_details(
            "publication-conflict",
            {
                "code": "publication-conflict",
                "conflict_kind": "store-generation",
                "conflict_stage": "store-cas",
                "expected_store_generation": 28,
                "publication_expected_generation": 28,
                "observed_store_generation": 29,
                "credential": "must not cross the RPC boundary",
            },
        )

        self.assertEqual(details, {
            "cause_code": "publication-conflict",
            "conflict_kind": "store-generation",
            "conflict_stage": "store-cas",
            "expected_store_generation": 28,
            "publication_expected_generation": 28,
            "observed_store_generation": 29,
        })


class StoreGenerationCliTests(unittest.TestCase):
    def test_generation_mode_returns_only_the_canonical_generation(self):
        parsed = cli.build_parser().parse_args([
            "store", "--session", "ses_fixture", "--generation",
        ])
        store = SimpleNamespace(
            backend=SimpleNamespace(generation=lambda: 48240),
        )
        stdout = io.StringIO()
        with redirect_stdout(stdout), patch.object(
            cli, "open_store", return_value=store
        ), patch.object(cli, "close_store"):
            code = cli.cmd_store(parsed)

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), {"generation": 48240})


if __name__ == "__main__":
    unittest.main()
