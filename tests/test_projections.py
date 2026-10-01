"""Ambient projection coherence: composition around native policy.

Fixture sessions drive the REAL automation planner entrypoint and the
REAL doc admission native module through fixture provider stubs, so
gate/proceed/defer/escalate behavior is genuinely native while
workspaces stay hermetic. Renders are deterministic fixture bytes.
Skipped cleanly without the mncs toolchain.
"""

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
CLI = ROOT / "scripts" / "mncs-env"
sys.path.insert(0, str(ROOT))

from mncs_env import claims as claims_module  # noqa: E402
from mncs_env import projections as projections_module  # noqa: E402
from mncs_env.session_store import open_store  # noqa: E402

REAL_ROOT = ROOT.parent
REAL_AUTOMATION_TOOLS = REAL_ROOT / "mncs-automation" / "tools"
REAL_DOC_TOOLS = REAL_ROOT / "mncs-doc" / "tools"


def find_mncs() -> str | None:
    for candidate in (
        os.environ.get("MNCS_BIN"),
        os.environ.get("MNCS_BINARY"),
        str(REAL_ROOT / "mncs-language" / "target" / "release" / "mncs"),
        str(REAL_ROOT / "mncs-language" / "target" / "debug" / "mncs"),
    ):
        if candidate and Path(candidate).is_file():
            return candidate
    return None


MNCS_BIN = find_mncs()
NEED_MNCS = unittest.skipUnless(
    MNCS_BIN is not None
    and REAL_AUTOMATION_TOOLS.is_dir() and REAL_DOC_TOOLS.is_dir(),
    "mncs toolchain or provider checkouts unavailable")


PLANNER_STUB = '''\
import os
import runpy
import sys


def main(argv):
    if os.environ.get("MNCS_FIXTURE_PLANNER_MODE") == "fail":
        print("fixture planner forced failure", file=sys.stderr)
        return 2
    tools = os.environ["MNCS_AUTOMATION_TOOLS"]
    sys.argv = ["reconcile.py", *argv]
    try:
        runpy.run_path(os.path.join(tools, "reconcile.py"),
                       run_name="__main__")
    except SystemExit as done:
        return int(done.code or 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'''

RENDER_STUB = '''\
import hashlib
import os
import runpy
import sys
from pathlib import Path


def render(argv):
    inputs = Path(argv[argv.index("--inputs") + 1])
    output = Path(argv[argv.index("--output") + 1])
    mode = os.environ.get("MNCS_FIXTURE_RENDER_MODE", "ok")
    if mode == "fail":
        print("fixture renderer forced failure", file=sys.stderr)
        return 2
    digest = hashlib.sha256()
    for member in sorted(inputs.rglob("*")):
        if member.is_file() and not member.is_symlink():
            digest.update(str(member.relative_to(inputs)).encode())
            digest.update(b"\\0")
            digest.update(member.read_bytes())
    body = ("RENDER:" + digest.hexdigest() + "\\n").encode()
    if mode == "nondeterministic":
        body += ("NONCE:" + str(os.getpid()) + "\\n").encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(body)
    return 0


def main(argv):
    if argv and argv[0] == "render":
        return render(argv[1:])
    tools = os.environ["MNCS_DOC_TOOLS"]
    sys.argv = ["project.py", *argv]
    try:
        runpy.run_path(os.path.join(tools, "project.py"),
                       run_name="__main__")
    except SystemExit as done:
        return int(done.code or 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'''


def fixture_bytes(inputs: Path) -> bytes:
    digest = hashlib.sha256()
    for member in sorted(inputs.rglob("*")):
        if member.is_file() and not member.is_symlink():
            digest.update(str(member.relative_to(inputs)).encode())
            digest.update(b"\0")
            digest.update(member.read_bytes())
    return ("RENDER:" + digest.hexdigest() + "\n").encode()


def git(repo: Path, *argv: str) -> None:
    subprocess.run(["git", "-C", str(repo), *argv], check=True,
                   capture_output=True, text=True, timeout=60)


def git_init(repo: Path) -> None:
    for argv in (["init", "-q", "-b", "main"], ["add", "."],
                 ["-c", "user.name=Projection Test",
                  "-c", "user.email=projection@example.invalid",
                  "commit", "-qm", "fixture"]):
        git(repo, *argv)


class ProjectionFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(
            prefix="mncs-projections-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.work = self.base / "work"
        self.entry = self.work / "entry"
        self.entry.mkdir(parents=True)
        self.state = self.base / "state"
        self.env = {"MNCS_BIN": MNCS_BIN or "",
                    "MNCS_AUTOMATION_TOOLS": str(REAL_AUTOMATION_TOOLS),
                    "MNCS_DOC_TOOLS": str(REAL_DOC_TOOLS),
                    "MNCS_TEST_NATIVE": str(REAL_ROOT / "mncs-test" / "native"),
                    "MNCS_LANGUAGE_ROOT": str(REAL_ROOT / "mncs-language")}
        self._write_provider(
            "mncs-automation", "automation-reconciliation",
            "tools/stub.py", ["plan"], PLANNER_STUB)
        self._write_provider(
            "mncs-doc", "documentation-projection",
            "tools/stub.py", [], RENDER_STUB)
        self.target_doc = self._write_target(
            "target-doc", "whole-file", "docs/out.generated.md")
        self.target_region = self._write_target(
            "target-region", "region", "README.md")
        entry = self.entry
        (entry / ".mncs").mkdir(parents=True)
        definition = {
            "name": "fixture-projections", "workspace_root": "..",
            "workspace_scope": {
                "kind": "workspace",
                "repositories": ["mncs-automation", "mncs-doc",
                                 "target-doc", "target-region"]},
            "required_capabilities": [
                "mncs-automation:automation-reconciliation",
                "mncs-doc:documentation-projection"],
            "intent": {"goal": "exercise ambient projections",
                       "repositories": []}}
        (entry / ".mncs" / "environment.json").write_text(
            json.dumps(definition))
        self.entry = entry

    def _write_provider(self, repo: str, contract: str, script: str,
                        fixed_argv: list[str], stub: str) -> Path:
        root = self.entry / repo
        (root / ".mncs").mkdir(parents=True)
        (root / "tools").mkdir(parents=True)
        (root / script).write_text(stub)
        manifest = {"schema_version": "mncs-family.repository-manifest/v0alpha1",
                    "repository": repo,
                    "contracts": {
                        "provides": [{
                            "contract": contract, "version": "1",
                            "kind": "test-fixture", "stability": "experimental",
                            "effects": ["read"],
                            "fingerprint_sources": [script],
                            "invocation": {"kind": "python", "path": script,
                                           "fixed_argv": fixed_argv}}]}}
        (root / ".mncs" / "project.json").write_text(json.dumps(manifest))
        git_init(root)
        return root

    def _write_target(self, repo: str, output_kind: str,
                      output: str) -> Path:
        root = self.entry / repo
        (root / ".mncs").mkdir(parents=True)
        inputs = root / "docs" / "rfcs"
        inputs.mkdir(parents=True)
        (inputs / "0001.md").write_text(f"# RFC 0001: {repo} foundation\n")
        declaration: dict = {
            "id": f"{repo}:index", "template": "fixture-render",
            "inputs": ["docs/rfcs"], "output": output,
            "output_kind": output_kind,
            "provider_capability": "mncs-doc:documentation-projection",
            "render_argv": ["render", "--inputs", "{checkout}/docs/rfcs",
                            "--output", "{artifact}/rendered.md"],
            "policy": "ambient-safe"}
        if output_kind == "region":
            declaration["admit"] = {"sources": 1, "template_present": True,
                                    "create_allowed": True}
            body = fixture_bytes(root / "docs" / "rfcs")
            (root / output).write_text(
                f"# {repo}\n\nHuman intro.\n\n"
                "<!-- MNCS:generated:begin -->\n" + body.decode() +
                "<!-- MNCS:generated:end -->\n\nHuman outro.\n")
        else:
            (root / output).parent.mkdir(parents=True, exist_ok=True)
            (root / output).write_bytes(
                fixture_bytes(root / "docs" / "rfcs"))
        manifest = {"schema_version": "mncs-family.repository-manifest/v0alpha1",
                    "repository": repo,
                    "contracts": {"provides": []},
                    "projections": [declaration]}
        (root / ".mncs" / "project.json").write_text(json.dumps(manifest))
        git_init(root)
        return root

    def run_cli(self, *argv: str, extra_env: dict | None = None,
                backend: str = "file"):
        env = dict(os.environ)
        env.update(self.env)
        env.update(extra_env or {})
        result = subprocess.run(
            [sys.executable, str(CLI), "--state-dir", str(self.state),
             "--persistence", backend, *argv], cwd=str(self.entry),
            capture_output=True, text=True, timeout=180, env=env)
        try:
            payload = json.loads(result.stdout or result.stderr)
        except ValueError:
            payload = {"raw_stdout": result.stdout,
                       "raw_stderr": result.stderr}
        return result.returncode, payload

    def enter(self, consumer: str, **kwargs):
        return self.run_cli("enter", "--consumer", consumer, **kwargs)

    def projection_summary(self, payload) -> dict:
        return payload["projection"]["summary"]

    def checkout_state(self, repo: Path) -> dict:
        completed = subprocess.run(
            ["git", "-C", str(repo), "status", "--porcelain=v1"],
            capture_output=True, text=True, check=True, timeout=60)
        return {"porcelain": completed.stdout}

    def acquire_foreign(self, repository: str, paths: list[str]):
        store = open_store(self.state, "file")
        return claims_module.acquire(
            store, repository=repository, session_id="foreign-session",
            consumer_id="other-agent", basis="explicit-claim",
            reason="fixture foreign work", ttl_hours=1,
            scope={"kind": "paths", "paths": paths},
            checkout_facts={"dirty": False})

    def release_foreign(self, repository: str):
        store = open_store(self.state, "file")
        claims_module.release(store, session_id="foreign-session",
                              repository=repository)

    def commit_all(self, repo: Path, message: str):
        git(repo, "add", "-A")
        git(repo, "-c", "user.name=Projection Test",
            "-c", "user.email=projection@example.invalid",
            "commit", "-qm", message)


@NEED_MNCS
class AmbientWholeFileTests(ProjectionFixture):
    def test_current_entry_converges_without_writes(self):
        before_doc = (self.target_doc / "docs/out.generated.md").read_bytes()
        before_region = (self.target_region / "README.md").read_bytes()
        code, first = self.enter("converge")
        self.assertEqual(code, 0, first)
        summary = self.projection_summary(first)
        self.assertEqual(summary["blockers"], 0, summary)
        self.assertEqual(summary["pending"], 0, summary)
        self.assertEqual(summary["current"], 2, summary)
        # First touch adopts without rewriting current bytes.
        self.assertEqual(
            (self.target_doc / "docs/out.generated.md").read_bytes(),
            before_doc)
        self.assertEqual((self.target_region / "README.md").read_bytes(),
                         before_region)
        self.assertEqual(
            self.checkout_state(self.target_doc)["porcelain"], "")
        self.assertEqual(
            self.checkout_state(self.target_region)["porcelain"], "")

    def test_canonical_advance_reconciles_only_affected(self):
        self.enter("narrow")
        # Post-commit reconciliation: the new input is committed first,
        # so the checkout shows no unknown work when the index follows.
        (self.target_doc / "docs" / "rfcs" / "0002.md").write_text(
            "# RFC 0002: follow-up\n")
        self.commit_all(self.target_doc, "add RFC 0002")
        region_before = (self.target_region / "README.md").read_bytes()
        code, second = self.enter("narrow")
        self.assertEqual(code, 0, second)
        summary = self.projection_summary(second)
        self.assertEqual(summary["reconciled"], 1, summary)
        self.assertEqual(summary["pending"], 0, summary)
        rendered = (self.target_doc / "docs/out.generated.md").read_bytes()
        self.assertEqual(
            rendered, fixture_bytes(self.target_doc / "docs" / "rfcs"))
        # The unrelated projection stayed current and untouched.
        self.assertEqual((self.target_region / "README.md").read_bytes(),
                         region_before)

    def test_unrelated_human_edit_does_not_stale_projection(self):
        self.enter("human")
        (self.target_doc / "NOTES.md").write_text("human scratch\n")
        code, second = self.enter("human")
        self.assertEqual(code, 0, second)
        summary = self.projection_summary(second)
        # Human dirt elsewhere defers the target checkout but the
        # unrelated region projection stays current.
        pending = summary["pending_ids"]
        self.assertIn("target-doc:index", pending)
        self.assertNotIn("target-region:index", pending)

    def test_repeated_entry_is_quiet(self):
        _, first = self.enter("quiet")
        first_ref = first["projection"]["evidence"]
        self.assertIn("elapsed_seconds",
                      first["projection"]["summary"])
        code, second = self.enter("quiet")
        self.assertEqual(code, 0, second)
        self.assertIn("elapsed_seconds",
                      second["projection"]["summary"])
        self.assertTrue(second["projection"]["reused"])
        self.assertTrue(
            self.projection_summary(second)["epoch_reused"])
        self.assertEqual(second["projection"]["evidence"], first_ref)

    def test_terse_stays_compact_with_full_evidence_available(self):
        self.enter("compact")
        code, payload = self.run_cli(
            "projections", self._session_id("compact"), "--evidence")
        self.assertEqual(code, 0, payload)
        self.assertIn("evidence", payload)
        self.assertEqual(len(payload["evidence"]["results"]), 2)
        code, terse = self.run_cli(
            "projections", self._session_id("compact"))
        summary = terse["summary"]
        self.assertLess(len(json.dumps(summary)), 800)
        self.assertNotIn("results", summary)

    def test_session_evidence_recorded_per_pass(self):
        _, first = self.enter("evidence")
        session_id = first["session_id"]
        path = (self.state / "sessions" / session_id
                / "projection-artifacts" / "session-evidence.jsonl")
        self.assertTrue(path.is_file())
        entry = json.loads(path.read_text().splitlines()[0])
        self.assertEqual(entry["schema_version"],
                         "mncs.session-evidence/1")
        self.assertEqual(entry["session"], session_id)
        self.assertEqual(len(entry["projections"]), 2)

    def _session_id(self, consumer: str) -> str:
        _, payload = self.enter(consumer)
        return str(payload["session_id"])


@NEED_MNCS
class DeferralTests(ProjectionFixture):
    def test_foreign_claim_defers_then_converges(self):
        _, first = self.enter("claimed")
        session_id = str(first["session_id"])
        (self.target_doc / "docs" / "rfcs" / "0002.md").write_text(
            "# RFC 0002: follow-up\n")
        self.commit_all(self.target_doc, "add RFC 0002")
        self.acquire_foreign("target-doc", ["docs/out.generated.md"])
        code, blocked = self.enter("claimed")
        self.assertEqual(code, 0, blocked)
        summary = self.projection_summary(blocked)
        self.assertIn("target-doc:index", summary["pending_ids"])
        stale = (self.target_doc / "docs/out.generated.md").read_bytes()
        self.assertNotEqual(
            stale, fixture_bytes(self.target_doc / "docs" / "rfcs"))
        code, evidence = self.run_cli("projections", session_id,
                                      "--evidence")
        reasons = {record["projection"]: record.get("gate_reason")
                   for record in evidence["evidence"]["results"]}
        self.assertEqual(reasons["target-doc:index"],
                         "deferred-foreign-claim")
        self.release_foreign("target-doc")
        code, converged = self.enter("claimed")
        self.assertEqual(code, 0, converged)
        summary = self.projection_summary(converged)
        self.assertNotIn("target-doc:index", summary["pending_ids"])
        self.assertEqual(
            (self.target_doc / "docs/out.generated.md").read_bytes(),
            fixture_bytes(self.target_doc / "docs" / "rfcs"))

    def test_dirty_working_tree_is_never_overwritten(self):
        self.enter("dirty")
        human = self.target_doc / "NOTES.md"
        human.write_text("agent work in progress\n")
        (self.target_doc / "docs" / "rfcs" / "0002.md").write_text(
            "# RFC 0002: follow-up\n")
        output_before = (self.target_doc / "docs/out.generated.md").read_bytes()
        code, second = self.enter("dirty")
        self.assertEqual(code, 0, second)
        summary = self.projection_summary(second)
        self.assertIn("target-doc:index", summary["pending_ids"])
        self.assertEqual(human.read_text(), "agent work in progress\n")
        self.assertEqual(
            (self.target_doc / "docs/out.generated.md").read_bytes(),
            output_before)

    def test_hand_edited_output_defers_as_diverged(self):
        self.enter("diverged")
        output = self.target_doc / "docs/out.generated.md"
        output.write_text("hand-edited by a human\n")
        code, second = self.enter("diverged")
        self.assertEqual(code, 0, second)
        summary = self.projection_summary(second)
        self.assertIn("target-doc:index", summary["pending_ids"])
        self.assertEqual(output.read_text(), "hand-edited by a human\n")

    def test_foreign_branch_defers(self):
        self.enter("branchy")
        git(self.target_doc, "checkout", "-qb", "experiment")
        (self.target_doc / "docs" / "rfcs" / "0002.md").write_text(
            "# RFC 0002: follow-up\n")
        self.commit_all(self.target_doc, "add RFC 0002 on branch")
        code, second = self.enter("branchy")
        self.assertEqual(code, 0, second)
        summary = self.projection_summary(second)
        self.assertIn("target-doc:index", summary["pending_ids"])


@NEED_MNCS
class DegradedProviderTests(ProjectionFixture):
    def test_planner_outage_defers_with_compact_degraded_state(self):
        _, first = self.enter("outage")
        session_id = str(first["session_id"])
        # The epoch must miss for the outage to surface: touch inputs.
        (self.target_doc / "docs" / "rfcs" / "0002.md").write_text(
            "# RFC 0002: follow-up\n")
        self.commit_all(self.target_doc, "add RFC 0002")
        code, degraded = self.run_cli(
            "projections", session_id,
            extra_env={"MNCS_FIXTURE_PLANNER_MODE": "fail"})
        self.assertEqual(code, 0, degraded)
        summary = degraded["summary"]
        self.assertGreater(summary["degraded"], 0)
        self.assertIn("target-doc:index", summary["pending_ids"])
        # Provider recovery triggers reconsideration on the next pass.
        code, recovered = self.run_cli("projections", session_id)
        self.assertEqual(code, 0, recovered)
        summary = recovered["summary"]
        self.assertEqual(summary["degraded"], 0)
        self.assertNotIn("target-doc:index", summary["pending_ids"])

    def test_nondeterministic_render_withholds_claim(self):
        self.enter("nondet")
        (self.target_doc / "docs" / "rfcs" / "0002.md").write_text(
            "# RFC 0002: follow-up\n")
        self.commit_all(self.target_doc, "add RFC 0002")
        _, first = self.enter("nondet")
        session_id = str(first["session_id"])
        # Converged above; break determinism for the next render by
        # changing inputs again, then render nondeterministically.
        (self.target_doc / "docs" / "rfcs" / "0003.md").write_text(
            "# RFC 0003: follow-up\n")
        self.commit_all(self.target_doc, "add RFC 0003")
        before = (self.target_doc / "docs/out.generated.md").read_bytes()
        code, payload = self.run_cli(
            "projections", session_id,
            extra_env={"MNCS_FIXTURE_RENDER_MODE": "nondeterministic"})
        self.assertEqual(code, 0, payload)
        summary = payload["summary"]
        self.assertIn("target-doc:index", summary["pending_ids"])
        self.assertEqual(
            (self.target_doc / "docs/out.generated.md").read_bytes(), before)
        code, recovered = self.run_cli("projections", session_id)
        self.assertEqual(code, 0, recovered)
        self.assertNotIn("target-doc:index",
                         recovered["summary"]["pending_ids"])


@NEED_MNCS
class RegionTests(ProjectionFixture):
    def test_region_projection_preserves_prose(self):
        self.enter("prose")
        (self.target_region / "docs" / "rfcs" / "0002.md").write_text(
            "# RFC 0002: follow-up\n")
        self.commit_all(self.target_region, "add RFC 0002")
        code, second = self.enter("prose")
        self.assertEqual(code, 0, second)
        summary = self.projection_summary(second)
        self.assertNotIn("target-region:index", summary["pending_ids"])
        rendered = (self.target_region / "README.md").read_text()
        self.assertIn("Human intro.", rendered)
        self.assertIn("Human outro.", rendered)
        self.assertIn("RENDER:", rendered)

    def test_ambiguous_markers_escalate_without_mutation(self):
        self.enter("ambiguous")
        readme = self.target_region / "README.md"
        readme.write_text(
            "<!-- MNCS:generated:begin -->\na\n<!-- MNCS:generated:end -->\n"
            "<!-- MNCS:generated:begin -->\nb\n<!-- MNCS:generated:end -->\n")
        before = readme.read_bytes()
        code, second = self.enter("ambiguous")
        self.assertEqual(code, 0, second)
        summary = self.projection_summary(second)
        self.assertGreater(summary["blockers"], 0)
        self.assertEqual(readme.read_bytes(), before)


@NEED_MNCS
class HygieneTests(ProjectionFixture):
    def test_no_transient_state_dirties_repositories(self):
        self.enter("hygiene")
        (self.target_doc / "docs" / "rfcs" / "0002.md").write_text(
            "# RFC 0002: follow-up\n")
        self.commit_all(self.target_doc, "add RFC 0002")
        self.enter("hygiene")
        for repo in (self.target_doc, self.target_region):
            porcelain = self.checkout_state(repo)["porcelain"]
            for line in porcelain.splitlines():
                self.assertNotIn(".mncs/", line, porcelain)
                self.assertTrue(
                    line.endswith("out.generated.md")
                    or line.endswith("README.md"),
                    porcelain)
            self.assertFalse((repo / ".mncs" / "cache").exists())

    def test_restart_preserves_pending_then_converges(self):
        self.enter("restart")
        (self.target_doc / "docs" / "rfcs" / "0002.md").write_text(
            "# RFC 0002: follow-up\n")
        self.commit_all(self.target_doc, "add RFC 0002")
        self.acquire_foreign("target-doc", ["docs/out.generated.md"])
        _, blocked = self.enter("restart")
        self.assertIn("target-doc:index",
                      self.projection_summary(blocked)["pending_ids"])
        # A new process generation observes the same pending state.
        _, again = self.enter("restart")
        self.assertIn("target-doc:index",
                      self.projection_summary(again)["pending_ids"])
        self.release_foreign("target-doc")
        _, converged = self.enter("restart")
        self.assertNotIn("target-doc:index",
                         self.projection_summary(converged)["pending_ids"])


class DiscoveryTests(ProjectionFixture):
    def test_invalid_declarations_are_reported_never_run(self):
        root = self.entry / "bad-target"
        (root / ".mncs").mkdir(parents=True)
        (root / ".mncs" / "project.json").write_text(json.dumps({
            "repository": {"name": "bad-target"},
            "projections": [
                {"id": "bad:missing-output", "template": "x",
                 "inputs": [], "provider_capability": "c",
                 "render_argv": [], "policy": "ambient-safe"},
                {"id": "bad:escape", "template": "x", "inputs": [],
                 "output": "../escape.md", "output_kind": "whole-file",
                 "provider_capability": "c", "render_argv": [],
                 "policy": "ambient-safe"},
            ]}))
        declarations, invalid = projections_module.discover_declarations(
            self.entry)
        reasons = {entry.get("reason") for entry in invalid}
        self.assertIn("missing-output", reasons)
        self.assertIn("path-escapes-checkout", reasons)
        self.assertFalse(any(record["repository"] == "bad-target"
                             for record in declarations))

    def test_input_digest_is_deterministic(self):
        first = projections_module.input_digest(
            self.target_doc, ["docs/rfcs"])
        second = projections_module.input_digest(
            self.target_doc, ["docs/rfcs"])
        self.assertIsNotNone(first)
        self.assertEqual(first, second)
        (self.target_doc / "docs" / "rfcs" / "0002.md").write_text("x\n")
        third = projections_module.input_digest(
            self.target_doc, ["docs/rfcs"])
        self.assertNotEqual(first, third)
        self.assertIsNone(
            projections_module.input_digest(self.target_doc, ["nope"]))


if __name__ == "__main__":
    unittest.main()
