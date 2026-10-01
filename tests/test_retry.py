"""Recovery backoff: failure identity, native retry gate, bookkeeping."""

from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mncs_env import retry

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent


def _language_checkout() -> Path:
    override = os.environ.get("MNCS_BINARY")
    if override:
        return Path(override).resolve().parents[2]
    return WORKSPACE / "mncs-language"


def _commons_checkout() -> Path:
    override = os.environ.get("MNCS_COMMONS_ROOT")
    if override:
        return Path(override)
    return WORKSPACE / "MNCS-Commons"


LANGUAGE_CHECKOUT = _language_checkout()
COMMONS_CHECKOUT = _commons_checkout()

TOOLCHAIN_PRESENT = (
    (LANGUAGE_CHECKOUT / "target" / "release" / "mncs").is_file()
    or (LANGUAGE_CHECKOUT / "target" / "debug" / "mncs").is_file()
) and (COMMONS_CHECKOUT / "src" / "mncs_commons" / "mesh" / retry.REMEDIATION_SOURCE).is_file()


def observation(**overrides):
    record = {"identity": "svc", "status": "degraded", "code": "service-not-ready",
              "reason": "provider readiness predicates not satisfied",
              "observed_at": "2026-10-01T00:00:00+00:00",
              "observation": {"/ready": False},
              "provider_diagnostics": [{"code": "X1", "detail": "not up"}]}
    record.update(overrides)
    return record


def binding(**overrides):
    record = {"binding_id": "b1", "provider": "fixture", "capability": "fixture.start/1",
              "contract_revision": "1", "address": "python:provider.py",
              "provider_root": "/repo/fixture",
              "availability": {"status": "available"}}
    record.update(overrides)
    return record


class FailureIdentityTests(unittest.TestCase):
    def test_same_observation_is_stable(self):
        first = retry.failure_identity(observation(), binding(), {})
        second = retry.failure_identity(observation(), binding(), {})
        self.assertEqual(first["digest"], second["digest"])

    def test_volatile_bytes_do_not_rotate_identity(self):
        base = retry.failure_identity(observation(), binding(), {})
        noisy = retry.failure_identity(
            observation(observed_at="2026-10-02T00:00:00+00:00",
                        reason="timeout after 3.0s: pid 12345 slow",
                        provider_observed={"at": "now", "pid": 99}),
            binding(), {})
        self.assertEqual(base["digest"], noisy["digest"])

    def test_meaningful_changes_rotate_identity(self):
        base = retry.failure_identity(observation(), binding(), {})["digest"]
        self.assertNotEqual(base, retry.failure_identity(
            observation(code="service-probe-timeout"), binding(), {})["digest"])
        self.assertNotEqual(base, retry.failure_identity(
            observation(observation={"/ready": True, "/mode": "drain"}), binding(), {})["digest"])
        self.assertNotEqual(base, retry.failure_identity(
            observation(provider_diagnostics=[{"code": "X2"}]), binding(), {})["digest"])
        self.assertNotEqual(base, retry.failure_identity(
            observation(), binding(contract_revision="2"), {})["digest"])
        self.assertNotEqual(base, retry.failure_identity(
            observation(), binding(),
            {"fixture": "abc", "/repo/fixture": "abc"})["digest"])
        unavailable = binding()
        unavailable["availability"] = {"status": "unavailable"}
        self.assertNotEqual(base, retry.failure_identity(observation(), unavailable, {})["digest"])

    def test_material_names_every_contract_field(self):
        identity = retry.failure_identity(observation(), binding(), {})
        for field in ("service", "status", "code", "observation_digest",
                      "provider_revision", "recovery_available"):
            self.assertTrue(identity[field], field)


class BackoffStateTests(unittest.TestCase):
    def test_round_trip_and_entry_cap(self):
        session = SimpleNamespace(snapshot={})
        state = {f"fri_{index:04d}": {"service": "svc", "attempts": 1,
                                      "last_attempt_at": f"2026-10-01T00:{index:02d}:00+00:00"}
                 for index in range(70)}
        retry.store_backoff_state(session, state)
        stored = retry.backoff_state(session)
        self.assertEqual(len(stored), retry.MAX_BACKOFF_ENTRIES)
        self.assertIn("fri_0069", stored)
        self.assertNotIn("fri_0000", stored)

    def test_seconds_since_parses_iso_and_clamps(self):
        now = datetime(2026, 10, 1, 0, 1, 0, tzinfo=timezone.utc)
        self.assertEqual(retry.seconds_since("2026-10-01T00:00:00+00:00", now), 60)
        self.assertEqual(retry.seconds_since("not-a-time", now), 0)
        future = (now + timedelta(seconds=10)).isoformat()
        self.assertEqual(retry.seconds_since(future, now), 0)


class RetryPolicyTests(unittest.TestCase):
    def test_defaults_when_commons_is_absent(self):
        session = SimpleNamespace(snapshot={"workspace": {"root": "/nonexistent"}})
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MNCS_COMMONS_ROOT", None)
            os.environ.pop("MNCS_BINARY", None)
            policy = retry.load_retry_policy(session)
        self.assertEqual((policy["base_delay_secs"], policy["max_delay_secs"]), (60, 900))
        self.assertEqual(policy["source"], "contract-default")

    @unittest.skipUnless((COMMONS_CHECKOUT / "family" / "remediation-policy-v1.json").is_file(),
                         "sibling MNCS-Commons checkout required")
    def test_family_policy_file_is_loaded(self):
        session = SimpleNamespace(snapshot={
            "workspace": {"root": str(WORKSPACE)},
            "selected_checkouts": {"MNCS-Commons": {"path": str(COMMONS_CHECKOUT)}},
            "toolchain": {"checkout": str(LANGUAGE_CHECKOUT)}})
        policy = retry.load_retry_policy(session)
        self.assertTrue(policy["source"].endswith("remediation-policy-v1.json"))
        expected = json.loads((COMMONS_CHECKOUT / "family" / "remediation-policy-v1.json").read_text())
        self.assertEqual(policy["base_delay_secs"], expected["retry"]["base_delay_secs"])
        self.assertEqual(policy["max_delay_secs"], expected["retry"]["max_delay_secs"])


@unittest.skipUnless(TOOLCHAIN_PRESENT, "mncs toolchain + Commons mesh required")
class NativeGateTests(unittest.TestCase):
    def session(self):
        return SimpleNamespace(snapshot={
            "workspace": {"root": str(WORKSPACE)},
            "selected_checkouts": {
                "MNCS-Commons": {"path": str(COMMONS_CHECKOUT)},
                "mncs-language": {"path": str(LANGUAGE_CHECKOUT)}},
            "toolchain": {"checkout": str(LANGUAGE_CHECKOUT)}})

    def test_native_gate_suppresses_and_readmits(self):
        gate = retry.default_retry_gate(self.session())
        eligible, detail = gate(base=60, max_delay=900, attempts=1, elapsed=0)
        self.assertTrue(detail["native"])
        self.assertFalse(eligible)
        self.assertEqual(detail["delay_secs"], 60)
        self.assertEqual(detail["retry_in_secs"], 60)
        eligible, detail = gate(base=60, max_delay=900, attempts=1, elapsed=61)
        self.assertTrue(detail["native"])
        self.assertTrue(eligible)

    def test_native_gate_follows_exponential_law(self):
        gate = retry.default_retry_gate(self.session())
        eligible, detail = gate(base=60, max_delay=900, attempts=3, elapsed=30)
        self.assertFalse(eligible)
        self.assertEqual(detail["delay_secs"], 240)
        eligible, _ = gate(base=60, max_delay=900, attempts=3, elapsed=240)
        self.assertTrue(eligible)

    def test_broken_toolchain_fails_open_to_attempt(self):
        session = SimpleNamespace(snapshot={
            "workspace": {"root": "/nonexistent"}, "toolchain": {"checkout": "/nonexistent"}})
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MNCS_BINARY", None)
            os.environ.pop("MNCS_COMMONS_ROOT", None)
            gate = retry.default_retry_gate(session)
            eligible, detail = gate(base=60, max_delay=900, attempts=5, elapsed=0)
        self.assertTrue(eligible)
        self.assertFalse(detail["native"])
        self.assertIn("fallback", detail)


if __name__ == "__main__":
    unittest.main()
