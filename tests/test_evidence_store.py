from __future__ import annotations

import unittest

from mncs_env import evidence_store
from mncs_env.store_backend import StoreIntegrityFailure


class MemoryStore:
    def __init__(self):
        self.records = {}
        self.fail_after_commit = False

    def get_record_strict(self, schema, identity):
        return self.records.get((schema, identity))

    def put_record(self, schema, identity, record):
        self.records[(schema, identity)] = record
        if self.fail_after_commit:
            self.fail_after_commit = False
            raise OSError("caller lost the publication response")


class Session:
    def __init__(self, session_id, store):
        self.session_id = session_id
        self.store = store


class StoreEvidenceTests(unittest.TestCase):
    def test_publish_and_read_immutable_owner_record(self):
        store = MemoryStore()
        session = Session("ses_example", store)
        record = {"schema_version": "mncs.environment.projections/1",
                  "results": [{"projection": "overview"}]}

        reference = evidence_store.publish(session, "projections", record)

        self.assertEqual(len(store.records), 1)
        self.assertTrue(reference.startswith("mncs-store:ses_example:projections:"))
        self.assertEqual(evidence_store.read(session, "projections", reference), record)
        self.assertEqual(evidence_store.publish(session, "projections", record), reference)
        self.assertEqual(len(store.records), 1)

    def test_publish_reconciles_response_lost_after_commit(self):
        store = MemoryStore()
        store.fail_after_commit = True
        session = Session("ses_example", store)
        record = {"finished_at": "2026-10-10T00:00:00+00:00"}

        reference = evidence_store.publish(session, "verification", record)

        self.assertEqual(len(store.records), 1)
        self.assertEqual(evidence_store.read(session, "verification", reference), record)

    def test_read_rejects_another_session_or_owner_reference(self):
        session = Session("ses_example", MemoryStore())
        with self.assertRaises(StoreIntegrityFailure):
            evidence_store.read(
                session, "projections", "mncs-store:ses_other:projections:deadbeef")
        with self.assertRaises(StoreIntegrityFailure):
            evidence_store.read(
                session, "projections", "mncs-store:ses_example:actions:deadbeef")

    def test_missing_store_binding_is_integrity_failure(self):
        session = Session("ses_example", MemoryStore())
        with self.assertRaises(StoreIntegrityFailure):
            evidence_store.read(
                session, "projections", "mncs-store:ses_example:projections:deadbeef")

    def test_unwritable_legacy_reference_is_not_reused_as_current(self):
        session = Session("ses_example", MemoryStore())
        self.assertFalse(evidence_store.cacheable(
            session, "projections", "projection-evidence-unwritable"))


if __name__ == "__main__":
    unittest.main()
