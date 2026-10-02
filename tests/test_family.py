from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from mncs_env import family as family_module  # noqa: E402
from mncs_env.session_store import FileSessionStore  # noqa: E402

PRISTINE = Path("/home/epi13/Documents/Projects/mncs-language/.worktrees/pristine-ambient-actions")
PRISTINE_BINARY = Path(os.environ.get("MNCS_BINARY", PRISTINE / "target/release/mncs"))
PRISTINE_LIBRARY = Path(os.environ.get("MNCS_LIBRARY_ROOT", PRISTINE / "library"))

COMMONS_WORKTREE = Path("/home/epi13/Documents/Projects/MNCS-Commons/.worktrees/family-collaboration")
COMMONS_MAIN = Path("/home/epi13/Documents/Projects/MNCS-Commons")


def _commons_with_family() -> Path | None:
    for root in (Path(os.environ.get("MNCS_COMMONS_ROOT", COMMONS_WORKTREE)), COMMONS_MAIN):
        if (root / "src/mncs_commons/family_change.py").is_file():
            return root
    return None


def _native_available() -> bool:
    return (PRISTINE_BINARY.is_file() and PRISTINE_LIBRARY.is_dir()
            and _commons_with_family() is not None)


def _git(repo: Path, *argv: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *argv], capture_output=True, text=True,
        timeout=60, check=False)
    assert completed.returncode == 0, (argv, completed.stderr)
    return completed.stdout.strip()


def _write_repo(root: Path, name: str, provides=(), consumes=()) -> Path:
    repo = root / name
    (repo / ".mncs").mkdir(parents=True)
    (repo / ".mncs" / "project.json").write_text(json.dumps({
        "schema_version": "mncs-family.repository-manifest/v0alpha1",
        "repository": name,
        "contracts": {
            "provides": [{"contract": contract} for contract in provides],
            "consumes": [{"contract": contract} for contract in consumes]}}))
    (repo / "data.json").write_text(json.dumps({"revision": "rev-1"}))
    (repo / "notes.txt").write_text("alpha old-value omega\n")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.name=Family Test", "-c",
         "user.email=family@example.invalid", "commit", "-qm", "init")
    return repo


class _StubSession:
    def __init__(self, state_dir: Path, session_id: str,
                 checkouts: dict[str, str], native: bool = False):
        self.store = FileSessionStore(state_dir)
        self.session_id = session_id
        self.consumer_id = f"consumer-{session_id}"
        self.snapshot: dict = {
            "selected_checkouts": {
                name: {"path": path} for name, path in checkouts.items()},
            "bindings": [{"capability": "mncs-test:test-provider"}],
            "verification_state": {},
        }
        if native:
            self.snapshot["toolchain"] = {"binary": str(PRISTINE_BINARY)}
        self.events: list = []
        self.invoked: list = []

    def _emit(self, event_type, producer, payload=None, causes=None):
        self.events.append({"type": event_type, "payload": payload or {}})
        return {}

    def _save(self):
        pass

    def invoke(self, capability, argv, **kwargs):
        self.invoked.append((capability, list(argv)))
        return {"status": "ok", "stdout": "{}", "stderr": ""}


def _draft(session_id: str, producer: str, head: str, generation: int,
           contracts, operations, subjects, obligations=(),
           intent_extra=None) -> dict:
    import importlib.util
    commons = _commons_with_family()
    assert commons is not None
    # Load by file identity: other suites may install mncs_commons stubs.
    name = "_mncs_commons_family_change_test"
    spec = importlib.util.spec_from_file_location(
        name, commons / "src/mncs_commons/family_change.py")
    assert spec is not None and spec.loader is not None
    vocabulary = importlib.util.module_from_spec(spec)
    sys.modules[name] = vocabulary
    spec.loader.exec_module(vocabulary)
    record = {
        "schema_version": "mncs.family-change/1",
        "producer": {"repository": producer, "checkout_kind": "worktree",
                    "session": session_id, "consumer": "agent-a",
                    "claim_id": "clm-1"},
        "base": {"head": head, "generation": generation},
        "state": "draft",
        "supersedes": [],
        "subjects": subjects,
        "contracts_changed": contracts,
        "operations": operations,
        "evidence_refs": {"diff": "ev:diff-1",
                          "coordination_changesets": []},
        "verification": {"state": "unknown",
                         "obligations": list(obligations)},
        "intent": {"summary": "test change", "migration_id": "m-1",
                   **(intent_extra or {})},
    }
    validated_probe = dict(record)
    validated_probe["producer"] = {
        "repository": producer, "checkout_kind": "worktree",
        "session": session_id, "consumer": "agent-a", "claim_id": "clm-1"}
    record["identity"] = vocabulary.change_identity(
        vocabulary.change_core(validated_probe))
    return record


def _claim(session_id: str, repository: str, kind="repository") -> dict:
    return {
        "schema_version": "mncs.environment.workspace-claim/2",
        "claim_id": f"claim:{repository}:{session_id}",
        "version": 1, "session_id": session_id, "repository": repository,
        "scope": {"kind": kind, "repository": repository, "checkout": None,
                  "branch": None, "paths": None,
                  "exclusive": kind == "repository"},
        "status": "held", "basis": "explicit-claim",
        "expires_at": "2030-01-01T00:00:00+00:00",
    }


class FamilyNativeTest(unittest.TestCase):
    def setUp(self) -> None:
        if not _native_available():
            self.skipTest("pristine toolchain or family vocabulary unavailable")
        family_module._FAMILY_CHANGE_MODULE = None
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.commons = str(_commons_with_family())
        self.producer_repo = _write_repo(
            self.root, "prod-repo", provides=["prod.contract.v1"])
        self.consumer_repo = _write_repo(
            self.root, "cons-repo", consumes=["prod.contract.v1"])
        self.head = _git(self.producer_repo, "rev-parse", "HEAD")

    def tearDown(self) -> None:
        family_module._FAMILY_CHANGE_MODULE = None
        self.temporary.cleanup()

    def _session(self, session_id: str) -> _StubSession:
        return _StubSession(
            self.state, session_id,
            {"MNCS-Commons": self.commons,
             "prod-repo": str(self.producer_repo),
             "cons-repo": str(self.consumer_repo)}, native=True)

    def _change(self, session, **overrides) -> dict:
        target = self.consumer_repo / "data.json"
        preimage = "sha256:" + hashlib.sha256(
            target.read_bytes()).hexdigest()
        record = _draft(
            session.session_id, "prod-repo", self.head, 0,
            [{"contract": "prod.contract.v1", "from": "rev-1",
              "to": "rev-2"}],
            [{"op": "set_json_field",
              "params": {"field": "revision", "to": "rev-2"},
              "paths": ["data.json"], "preimage": {"data.json": preimage}}],
            [{"identity": "prod.subject", "kind": "contract",
              "paths": ["data.json"]}],
            obligations=["cons-repo:ob-1"])
        record.update(overrides)
        published = family_module.publish_change(session, record)
        return family_module.establish_change(session,
                                              published["identity"])

    def test_publish_establish_lifecycle(self) -> None:
        session = self._session("ses_prod")
        target = self.consumer_repo / "data.json"
        preimage = "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
        record = _draft(
            "ses_prod", "prod-repo", self.head, 0,
            [{"contract": "prod.contract.v1", "from": "rev-1",
              "to": "rev-2"}],
            [{"op": "set_json_field",
              "params": {"field": "revision", "to": "rev-2"},
              "paths": ["data.json"], "preimage": {"data.json": preimage}}],
            [{"identity": "s", "kind": "contract", "paths": ["data.json"]}])
        published = family_module.publish_change(session, record)
        self.assertEqual(published["state"], "draft")
        with self.assertRaises(family_module.FamilyError):
            family_module.transition_change(session, published["identity"],
                                            "established")
        established = family_module.establish_change(
            session, published["identity"])
        self.assertEqual(established["state"], "established")
        self.assertEqual(established["generation"], 1)
        reread = family_module.read_change(session, published["identity"])
        assert reread is not None
        self.assertEqual(reread["state"], "established")

    def test_generation_bump_idempotent(self) -> None:
        session = self._session("ses_prod")
        first = family_module.bump_generation(session, "prod-repo",
                                              self.head, "fc:a")
        second = family_module.bump_generation(session, "prod-repo",
                                               self.head, "fc:a")
        self.assertTrue(first["advanced"])
        self.assertFalse(second["advanced"])
        self.assertEqual(first["generation"], second["generation"])

    def test_dependency_consumers_found_structurally(self) -> None:
        session = self._session("ses_prod")
        consumers = family_module.dependency_consumers(
            session, "prod-repo", [{"contract": "prod.contract.v1"}])
        self.assertEqual(consumers, ["cons-repo"])
        self.assertEqual(family_module.dependency_consumers(
            session, "prod-repo", [{"contract": "other.contract"}]), [])

    def test_classify_reconcilable_and_record(self) -> None:
        producer = self._session("ses_prod")
        established = self._change(producer)
        consumer = self._session("ses_cons")
        change = family_module.read_change(consumer,
                                           established["identity"])
        assert change is not None
        classification = family_module.classify_drift(
            consumer, change, "cons-repo")
        self.assertEqual(classification["consumer_class"], "reconcilable")
        recorded = family_module.record_drift(consumer, change,
                                              classification)
        self.assertFalse(recorded["converged"])
        self.assertEqual(recorded["consumer_class"], "reconcilable")

    def test_classify_occupied_and_semantic(self) -> None:
        producer = self._session("ses_prod")
        established = self._change(producer)
        consumer = self._session("ses_cons")
        other = self._session("ses_other")
        other.store.put_claim(_claim("ses_other", "cons-repo"))
        change = family_module.read_change(consumer,
                                           established["identity"])
        assert change is not None
        classification = family_module.classify_drift(
            consumer, change, "cons-repo")
        self.assertEqual(classification["consumer_class"], "occupied")

    def test_converge_end_to_end_with_claim(self) -> None:
        producer = self._session("ses_prod")
        established = self._change(producer)
        identity = established["identity"]
        consumer = self._session("ses_cons")
        consumer.store.put_claim(_claim("ses_cons", "cons-repo"))
        change = family_module.read_change(consumer, identity)
        assert change is not None
        classification = family_module.classify_drift(
            consumer, change, "cons-repo")
        family_module.record_drift(consumer, change, classification)
        outcome = family_module.converge(consumer, identity, "cons-repo")
        self.assertTrue(outcome["converged"], outcome)
        document = json.loads(
            (self.consumer_repo / "data.json").read_text())
        self.assertEqual(document["revision"], "rev-2")
        # Repair awaits verification: UNKNOWN stays pending.
        adopted = family_module.adopt_pending(consumer, identity,
                                              "cons-repo")
        self.assertFalse(adopted["adopted"])
        self.assertEqual(adopted["detail"], "awaiting-verdict")
        # Owning verdict lands: PASS adopts to current.
        consumer.snapshot["verification_state"] = {
            "cons-repo:ob-1": {"evidence": {"verdict": "PASS"}}}
        adopted = family_module.adopt_pending(consumer, identity,
                                              "cons-repo")
        self.assertTrue(adopted["adopted"], adopted)
        change2 = family_module.read_change(consumer, identity)
        assert change2 is not None
        classification2 = family_module.classify_drift(
            consumer, change2, "cons-repo")
        self.assertEqual(classification2["consumer_class"], "current")

    def test_pre_repair_pass_or_fail_cannot_establish_post_repair_proof(self) -> None:
        for verdict in ("PASS", "FAIL"):
            with self.subTest(verdict=verdict):
                (self.consumer_repo / "data.json").write_text(json.dumps({"revision": "rev-1"}))
                producer = self._session("ses_prod_" + verdict)
                established = self._change(producer)
                identity = established["identity"]
                consumer = self._session("ses_cons_" + verdict)
                consumer.store.put_claim(_claim(consumer.session_id, "cons-repo"))
                consumer.snapshot["verification_state"] = {"cons-repo:ob-1": {
                    "evidence": {"verdict": verdict, "evidence_id": "pre-repair"}}}
                self.assertTrue(family_module.converge(consumer, identity, "cons-repo")["converged"])
                before = family_module.adopt_pending(consumer, identity, "cons-repo")
                self.assertFalse(before["adopted"], before)
                self.assertEqual(before["detail"], "awaiting-post-repair-verdict")
                consumer.snapshot["verification_state"]["cons-repo:ob-1"]["evidence"] = {
                    "verdict": "PASS", "evidence_id": "post-repair"}
                self.assertTrue(family_module.adopt_pending(consumer, identity, "cons-repo")["adopted"])
                claim = _claim(consumer.session_id, "cons-repo")
                consumer.store.put_claim({**claim, "version": 2, "status": "released"})

    def test_every_owning_obligation_requires_renewed_proof(self) -> None:
        producer = self._session("ses_prod")
        obligations = ["cons-repo:ob-1", "cons-repo:ob-2"]
        change = self._change(producer, verification={
            "state": "unknown", "obligations": obligations})
        consumer = self._session("ses_cons")
        consumer.store.put_claim(_claim(consumer.session_id, "cons-repo"))
        consumer.snapshot["verification_state"] = {ob: {
            "evidence": {"verdict": "PASS", "evidence_id": "pre-repair"}}
            for ob in obligations}
        self.assertTrue(family_module.converge(
            consumer, change["identity"], "cons-repo")["converged"])
        consumer.snapshot["verification_state"][obligations[0]]["evidence"] = {
            "verdict": "PASS", "evidence_id": "post-repair"}
        partial = family_module.adopt_pending(consumer, change["identity"], "cons-repo")
        self.assertFalse(partial["adopted"], partial)
        self.assertEqual(partial["detail"], "awaiting-post-repair-verdict")
        consumer.snapshot["verification_state"][obligations[1]]["evidence"] = {
            "verdict": "PASS", "evidence_id": "post-repair"}
        self.assertTrue(family_module.adopt_pending(
            consumer, change["identity"], "cons-repo")["adopted"])

    def test_converge_refuses_foreign_dirt(self) -> None:
        producer = self._session("ses_prod")
        established = self._change(producer)
        identity = established["identity"]
        consumer = self._session("ses_cons")
        consumer.store.put_claim(_claim("ses_cons", "cons-repo"))
        (self.consumer_repo / "data.json").write_text(
            json.dumps({"revision": "foreign-hack"}))
        outcome = family_module.converge(consumer, identity, "cons-repo")
        self.assertFalse(outcome["converged"])
        self.assertEqual(outcome["disposition"], "escalate")
        self.assertTrue(outcome["detail"].startswith("foreign-dirt"))
        document = json.loads(
            (self.consumer_repo / "data.json").read_text())
        self.assertEqual(document["revision"], "foreign-hack")

    def test_converge_defers_without_claim(self) -> None:
        producer = self._session("ses_prod")
        established = self._change(producer)
        consumer = self._session("ses_cons")
        outcome = family_module.converge(consumer, established["identity"],
                                         "cons-repo")
        self.assertFalse(outcome["converged"])
        self.assertEqual(outcome["disposition"], "defer")
        self.assertEqual(outcome["detail"], "claim-required")

    def test_epoch_reuse_skips_repeated_observation(self) -> None:
        producer = self._session("ses_prod")
        self._change(producer)
        consumer = self._session("ses_cons")
        first = family_module.ambient_pass(consumer)
        self.assertFalse(first["reused"])
        self.assertFalse(first["summary"]["epoch_reused"])
        self.assertGreater(first["summary"]["relevant"], 0)
        second = family_module.ambient_pass(consumer)
        self.assertTrue(second["reused"])
        self.assertTrue(second["summary"]["epoch_reused"])
        self.assertEqual(second["summary"]["relevant"],
                         first["summary"]["relevant"])

    def test_new_change_invalidates_epoch(self) -> None:
        producer = self._session("ses_prod")
        self._change(producer)
        consumer = self._session("ses_cons")
        family_module.ambient_pass(consumer)
        self.assertTrue(
            family_module.ambient_pass(consumer)["summary"]["epoch_reused"])
        (self.consumer_repo / "data.json").write_text(
            json.dumps({"revision": "rev-2"}))
        self._change(producer)
        third = family_module.ambient_pass(consumer)
        self.assertFalse(third["summary"]["epoch_reused"])

    def test_claim_in_a_distinct_branch_worktree_does_not_occupy_this_target(self) -> None:
        producer = self._session("ses_prod")
        established = self._change(producer)
        change = family_module.read_change(producer, established["identity"])
        assert change is not None
        consumer = self._session("ses_cons")
        consumer.snapshot["selected_checkouts"]["cons-repo"]["branch"] = "current-branch"
        other = self.root / "other-worktree"
        _git(self.consumer_repo, "worktree", "add", "-b", "other-branch", str(other))
        claim = _claim("ses_other", "cons-repo", kind="worktree")
        claim["scope"].update({"checkout": str(other), "branch": "other-branch"})
        consumer.store.put_claim(claim)
        self.assertEqual(family_module.classify_drift(consumer, change, "cons-repo")["consumer_class"], "reconcilable")
        claim.update({"version": 2, "session_id": consumer.session_id})
        consumer.store.put_claim(claim)
        self.assertIsNone(family_module._own_covering_claim(consumer, "cons-repo"))
        # Sharing a branch keeps ref mutation protection even across paths.
        claim.update({"version": 3, "session_id": "ses_other"})
        claim["scope"]["branch"] = "current-branch"
        consumer.store.put_claim(claim)
        self.assertEqual(family_module.classify_drift(consumer, change, "cons-repo")["consumer_class"], "occupied")

    def test_claim_acquisition_invalidates_epoch(self) -> None:
        producer = self._session("ses_prod")
        self._change(producer)
        consumer = self._session("ses_cons")
        family_module.ambient_pass(consumer)
        self.assertTrue(
            family_module.ambient_pass(consumer)["summary"]["epoch_reused"])
        consumer.store.put_claim(_claim("ses_cons", "cons-repo"))
        third = family_module.ambient_pass(consumer)
        self.assertFalse(third["summary"]["epoch_reused"])

    def test_foreign_claim_release_invalidates_epoch(self) -> None:
        producer = self._session("ses_prod")
        self._change(producer)
        consumer = self._session("ses_cons")
        claim = _claim("ses_other", "cons-repo")
        consumer.store.put_claim(claim)
        family_module.ambient_pass(consumer)
        self.assertTrue(family_module.ambient_pass(consumer)["reused"])
        consumer.store.put_claim({**claim, "version": 2, "status": "released"})
        self.assertFalse(family_module.ambient_pass(consumer)["reused"])

    def test_identical_drift_does_not_publish_another_row(self) -> None:
        producer = self._session("ses_prod")
        change = self._change(producer)
        consumer = self._session("ses_cons")
        classification = family_module.classify_drift(consumer, change, "cons-repo")
        first = family_module.record_drift(consumer, change, classification)
        second = family_module.record_drift(consumer, change, classification)
        self.assertTrue(second["current"])
        self.assertEqual(first["version"], second["version"])

    def test_converge_dry_run_mutates_nothing(self) -> None:
        producer = self._session("ses_prod")
        established = self._change(producer)
        consumer = self._session("ses_cons")
        consumer.store.put_claim(_claim("ses_cons", "cons-repo"))
        before = (self.consumer_repo / "data.json").read_bytes()
        outcome = family_module.converge(consumer, established["identity"],
                                         "cons-repo", dry_run=True)
        self.assertEqual(outcome["disposition"], "dry-run")
        self.assertEqual((self.consumer_repo / "data.json").read_bytes(),
                         before)

    def test_single_flight_second_session_observes(self) -> None:
        producer = self._session("ses_prod")
        established = self._change(producer)
        identity = established["identity"]
        first = self._session("ses_one")
        first.store.put_claim(_claim("ses_one", "cons-repo"))
        change = family_module.read_change(first, identity)
        assert change is not None
        classification = family_module.classify_drift(
            first, change, "cons-repo")
        family_module.record_drift(first, change, classification)
        outcome = family_module.converge(first, identity, "cons-repo")
        self.assertTrue(outcome["converged"], outcome)
        second = self._session("ses_two")
        change2 = family_module.read_change(second, identity)
        assert change2 is not None
        classification2 = family_module.classify_drift(
            second, change2, "cons-repo")
        recorded = family_module.record_drift(second, change2,
                                              classification2)
        # Same shared row: second session observes, never double-applies.
        self.assertEqual(classification2["consumer_class"],
                         "pending_verification")

    def test_superseded_change_never_converges(self) -> None:
        producer = self._session("ses_prod")
        established = self._change(producer)
        identity = established["identity"]
        consumer = self._session("ses_cons")
        consumer.store.put_claim(_claim("ses_cons", "cons-repo"))
        # Producer supersedes the change before repair: the stale plan
        # must refuse, never apply.
        family_module.transition_change(producer, identity, "superseded")
        with self.assertRaises(family_module.FamilyError):
            family_module.converge(consumer, identity, "cons-repo")
        document = json.loads(
            (self.consumer_repo / "data.json").read_text())
        self.assertEqual(document["revision"], "rev-1")

    def test_draft_visible_never_converged(self) -> None:
        producer = self._session("ses_prod")
        target = self.consumer_repo / "data.json"
        preimage = "sha256:" + hashlib.sha256(target.read_bytes()).hexdigest()
        record = _draft(
            "ses_prod", "prod-repo", self.head, 0,
            [{"contract": "prod.contract.v1"}],
            [{"op": "set_json_field",
              "params": {"field": "revision", "to": "rev-2"},
              "paths": ["data.json"], "preimage": {"data.json": preimage}}],
            [{"identity": "s", "kind": "contract", "paths": ["data.json"]}])
        published = family_module.publish_change(producer, record)
        consumer = self._session("ses_cons")
        changes = family_module.observe_changes(consumer)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["state"], "draft")
        with self.assertRaises(family_module.FamilyError):
            family_module.converge(consumer, published["identity"],
                                   "cons-repo")

    def test_doctor_consumes_change_with_remediation_envelope(self) -> None:
        from mncs_env import doctor as doctor_module
        producer = self._session("ses_prod")
        established = self._change(producer)
        identity = established["identity"]
        consumer = self._session("ses_cons")
        consumer.store.put_claim(_claim("ses_cons", "cons-repo"))
        result = doctor_module.remediate_family_change(
            consumer, identity, "cons-repo")
        self.assertEqual(result["summary"]["repaired"], 1)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["evidence"]["change"], identity)
        history = consumer.snapshot["doctor"]["history"]
        self.assertEqual(history[-1]["provider_status"], "family-converge")
        # Escalation path carries a blocker, not a repair.
        (self.consumer_repo / "data.json").write_text(
            json.dumps({"revision": "rev-1"}))
        producer2 = self._session("ses_prod2")
        established2 = self._change(producer2)
        (self.consumer_repo / "data.json").write_text(
            json.dumps({"revision": "foreign-hack"}))
        released = _claim("ses_cons", "cons-repo")
        released["version"] = 2
        released["status"] = "released"
        producer2.store.put_claim(released)
        consumer2 = self._session("ses_cons2")
        consumer2.store.put_claim(_claim("ses_cons2", "cons-repo"))
        result2 = doctor_module.remediate_family_change(
            consumer2, established2["identity"], "cons-repo")
        self.assertEqual(result2["summary"]["blockers"], 1)
        self.assertEqual(result2["exit_code"], 1)

    def test_failed_verification_escalates_repair(self) -> None:
        producer = self._session("ses_prod")
        established = self._change(producer)
        identity = established["identity"]
        consumer = self._session("ses_cons")
        consumer.store.put_claim(_claim("ses_cons", "cons-repo"))
        outcome = family_module.converge(consumer, identity, "cons-repo")
        self.assertTrue(outcome["converged"], outcome)
        consumer.snapshot["verification_state"] = {
            "cons-repo:ob-1": {"evidence": {"verdict": "FAIL"}}}
        adopted = family_module.adopt_pending(consumer, identity,
                                              "cons-repo")
        self.assertFalse(adopted["adopted"])
        self.assertEqual(adopted["detail"], "repair-verification-failed")


class FamilyHostTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.repo = _write_repo(self.root, "cons-repo")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _session(self, session_id="ses_host") -> _StubSession:
        return _StubSession(self.state, session_id,
                            {"cons-repo": str(self.repo)})

    def test_replace_span_apply_and_refusals(self) -> None:
        session = self._session()
        operation = {"op": "replace_span",
                     "params": {"old": "old-value", "new": "new-value"},
                     "paths": ["notes.txt"], "preimage": {}}
        ok, note = family_module.execute_operation(session, self.repo,
                                                   operation)
        self.assertTrue(ok, note)
        self.assertIn("new-value", (self.repo / "notes.txt").read_text())
        ok, note = family_module.execute_operation(session, self.repo,
                                                   operation)
        self.assertTrue(ok)
        self.assertEqual(note, "already-converged")
        (self.repo / "notes.txt").write_text("dup dup\n")
        operation2 = {"op": "replace_span",
                      "params": {"old": "dup", "new": "x"},
                      "paths": ["notes.txt"], "preimage": {}}
        ok, note = family_module.execute_operation(session, self.repo,
                                                   operation2)
        self.assertFalse(ok)
        self.assertIn("exactly one", note)

    def test_set_json_field_missing_field_refuses(self) -> None:
        session = self._session()
        operation = {"op": "set_json_field",
                     "params": {"field": "absent", "to": "x"},
                     "paths": ["data.json"], "preimage": {}}
        ok, note = family_module.execute_operation(session, self.repo,
                                                   operation)
        self.assertFalse(ok)

    def test_run_capability_confines_outputs(self) -> None:
        session = self._session()
        operation = {"op": "run_capability",
                     "params": {"capability": "test.echo", "argv": []},
                     "paths": ["notes.txt"], "preimage": {}}
        ok, note = family_module.execute_operation(session, self.repo,
                                                   operation)
        self.assertTrue(ok, note)
        self.assertEqual(session.invoked[0][0], "test.echo")

    def test_foreign_dirt_detection(self) -> None:
        target = self.repo / "data.json"
        import hashlib as hashlib_module
        preimage = "sha256:" + hashlib_module.sha256(
            target.read_bytes()).hexdigest()
        operations = [{"op": "set_json_field", "params": {},
                       "paths": ["data.json"],
                       "preimage": {"data.json": preimage}}]
        self.assertEqual(family_module.foreign_dirt(self.repo, operations),
                         [])
        target.write_text(json.dumps({"revision": "foreign"}))
        self.assertEqual(family_module.foreign_dirt(self.repo, operations),
                         ["data.json"])

    def test_fd_pressure_shape(self) -> None:
        pressure = family_module.fd_pressure()
        self.assertIn("pressured", pressure)
        self.assertIn("measurable", pressure)

    def test_quiet_ambient_pass_and_capsule(self) -> None:
        session = self._session()
        outcome = family_module.ambient_pass(session)
        self.assertEqual(outcome["summary"]["relevant"], 0)
        capsule = family_module.capsule(session)
        self.assertEqual(capsule["relevant_changes"], 0)
        self.assertEqual(capsule["attention"], [])
        self.assertLess(len(json.dumps(capsule)), 400)

    def test_presence_round_trip(self) -> None:
        session = self._session("ses_a")
        result = family_module.publish_presence(session, ["fc:x"])
        self.assertTrue(result["published"])
        contributors = family_module.read_contributors(session)
        self.assertEqual(len(contributors), 1)
        self.assertEqual(contributors[0]["session"], "ses_a")
        self.assertEqual(contributors[0]["active_changes"], ["fc:x"])

    def test_presence_is_write_free_until_facts_change_or_heartbeat(self) -> None:
        from unittest.mock import patch
        session = self._session("ses_presence")
        with patch.object(family_module, "utcnow", return_value="2026-10-02T00:00:00+00:00"):
            first = family_module.publish_presence(session, [])
        with patch.object(family_module, "utcnow", return_value="2026-10-02T00:05:00+00:00"):
            quiet = family_module.publish_presence(session, [])
            changed = family_module.publish_presence(session, ["fc:x"])
        self.assertTrue(quiet["current"])
        self.assertEqual(first["version"], quiet["version"])
        self.assertTrue(changed["published"])
        with patch.object(family_module, "utcnow", return_value="2026-10-02T01:05:00+00:00"):
            self.assertTrue(family_module.publish_presence(session, ["fc:x"])["published"])

    def test_fd_pressure_uses_effective_soft_limit(self) -> None:
        from unittest.mock import patch
        with patch("resource.getrlimit", return_value=(1, 1048576)):
            pressure = family_module.fd_pressure()
        self.assertEqual(pressure["limit"], 1)
        self.assertTrue(pressure["pressured"])

    def test_family_adoption_requires_every_declared_verdict(self) -> None:
        session = self._session()
        session.snapshot["verification_state"] = {"one": {"evidence": {"verdict": "PASS"}}}
        self.assertEqual(family_module._verification_verdict(session, ["one", "missing"]), "unknown")
        session.snapshot["verification_state"]["missing"] = {"evidence": {"verdict": "FAIL"}}
        self.assertEqual(family_module._verification_verdict(session, ["one", "missing"]), "failed")
        session.snapshot["verification_state"]["missing"]["evidence"]["verdict"] = "PASS"
        self.assertEqual(family_module._verification_verdict(session, ["one", "missing"]), "passed")

    def test_unknown_without_toolchain(self) -> None:
        session = self._session()
        # A suite-wide explicit compiler selection must not leak into this
        # unavailable-provider fixture. Keep both refusal assertions intact.
        from unittest.mock import patch
        with patch.dict(os.environ, {"MNCS_BIN": "", "MNCS_BINARY": ""}):
            self.assertIsNone(family_module.native_gate(session, [1, 1, 1, 1, 0, 0, 3]))
            self.assertIsNone(family_module.native_classify(session, [1] * 8))


if __name__ == "__main__":
    unittest.main()
