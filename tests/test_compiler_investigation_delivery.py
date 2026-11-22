"""Owner-level integration tests for ambient compiler investigation delivery.

Proves on ordinary Environment entry and diagnostic paths:
  1. Concise selected-callable diagnosis, separate compiler provenance, and
     separate bootstrap status (preserving stage1_to_stage2_rebuild=UNKNOWN)
     reach the agent inline on `mncs-env enter` and `mncs-env diagnostic`.
  2. Unchanged re-entry recovers the diagnosis with 0 new provider launches
     and 0 heavyweight compiles.
  3. A source file / git revision change invalidates the old diagnosis.
  4. An unavailable provider produces an explicit partial result.
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

DIAGNOSIS_PROVIDER_STUB = '''\
import hashlib, json, subprocess, sys
from pathlib import Path

def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""

def head(repo: Path) -> str:
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""

def main(argv):
    checkout = Path(__file__).resolve().parents[1]
    if "--checkout" in argv:
        checkout = Path(argv[argv.index("--checkout") + 1]).resolve()
    counter = checkout / ".provider_launches"
    count = int(counter.read_text().strip()) + 1 if counter.is_file() else 1
    counter.write_text(str(count), encoding="utf-8")

    cache = checkout / ".mncs/cache/selected-callable-diagnosis.json"
    doc = json.loads(cache.read_text(encoding="utf-8"))
    inv = doc["invalidation"]["inputs"]
    changed = []
    if inv.get("compiler_revision") != head(checkout):
        changed.append("compiler_revision")
    if inv.get("emitter_source_sha256") != sha(checkout / "src/compiler/vm_emit.mncs"):
        changed.append("src/compiler/vm_emit.mncs")
    for rel, expected in inv.get("source_digests", {}).items():
        if sha(checkout / rel) != expected:
            changed.append(rel)
    if changed:
        doc["freshness"] = "invalidated"
        doc["reused"] = False
        doc["invalidated_reasons"] = changed
    else:
        doc["freshness"] = "current"
        doc["reused"] = True
    doc["measurements"] = {"provider_launches": 0, "elapsed_seconds": 0.01, "heavyweight_compile_launched": False}
    print(json.dumps(doc, sort_keys=True, separators=(",", ":")))
    return 0

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'''


def _git(repo: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=True,
    )
    return res.stdout.strip()


def _init_git(repo: Path) -> str:
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial")
    return _git(repo, "rev-parse", "HEAD")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_sibling_manifest(repo: Path, name: str, contract: str, rel_script: str) -> None:
    (repo / ".mncs").mkdir(parents=True, exist_ok=True)
    script = repo / rel_script
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/usr/bin/env python3\nprint('{}')\n", encoding="utf-8")
    os.chmod(script, 0o755)
    (repo / ".mncs/project.json").write_text(json.dumps({
        "schema_version": "mncs-family.repository-manifest/v0alpha1",
        "repository": name,
        "revision": 1,
        "manifest_location": "in-repository",
        "contracts": {
            "provides": [{
                "contract": contract,
                "version": "1",
                "kind": "provider",
                "stability": "experimental",
                "effects": ["read"],
                "invocation": {
                    "kind": "python",
                    "path": rel_script,
                    "fixed_argv": [],
                },
            }],
        },
    }), encoding="utf-8")
    _init_git(repo)


def _make_workspace(tmp: Path, *, include_ls: bool = True, broken_diag_provider: bool = False) -> tuple[Path, Path]:
    ws = tmp / "ws"
    ws.mkdir()
    comp = ws / "mncs-compiler"
    (comp / "src/compiler").mkdir(parents=True)
    (comp / "tools").mkdir(parents=True)
    (comp / "evidence").mkdir(parents=True)
    (comp / ".mncs/cache").mkdir(parents=True)

    (comp / "src/compiler/vm_emit.mncs").write_text("mncs 0.18;\nmodule mncs.compiler.vm_emit.v1;\n", encoding="utf-8")
    (comp / "src/compiler/lexer.mncs").write_text("mncs 0.19;\nmodule mncs.compiler.lexer.v1;\nenum TokenList { TNil }\n", encoding="utf-8")
    (comp / "src/compiler/segment.mncs").write_text("mncs 0.19;\nmodule mncs.compiler.segment.v1;\n", encoding="utf-8")
    (comp / "evidence/SELF-HOST-MATRIX.json").write_text(json.dumps({
        "schema_version": 1,
        "current_campaign_test": {
            "stage1_to_stage2_rebuild": "UNKNOWN",
            "stage1_claimed": False,
            "stage2_claimed": False,
            "evidence_artifact": "evidence/sample.json",
        },
    }), encoding="utf-8")
    (comp / "evidence/sample.json").write_text("{}\n", encoding="utf-8")

    provider = comp / "tools/compiler_diagnosis.py"
    if broken_diag_provider:
        provider.write_text("import sys; sys.stderr.write('boom'); raise SystemExit(2)\n", encoding="utf-8")
    else:
        provider.write_text(DIAGNOSIS_PROVIDER_STUB, encoding="utf-8")

    repos = ["mncs-compiler", "mncs-doctor", "mncs-debug"]
    if include_ls:
        repos.append("mncs-language-service")
    (comp / ".mncs/environment.json").write_text(json.dumps({
        "name": "compiler-diag-test",
        "working_checkout": "current",
        "workspace_scope": {"kind": "workspace", "repositories": repos},
        "intent": {
            "goal": "verify ambient compiler investigation delivery",
            "repositories": ["mncs-compiler"],
            "protected_repositories": [r for r in repos if r != "mncs-compiler"],
        },
    }), encoding="utf-8")

    (comp / ".mncs/project.json").write_text(json.dumps({
        "schema_version": "mncs-family.repository-manifest/v0alpha1",
        "repository": "mncs-compiler",
        "revision": 26,
        "manifest_location": "in-repository",
        "contracts": {
            "provides": [{
                "contract": "selected-callable-diagnosis",
                "version": "1",
                "kind": "compiler-diagnostic-projection",
                "stability": "experimental",
                "effects": ["read"],
                "artifact_contract": {
                    "response_schema": "mncs.compiler.selected-callable-diagnosis/1",
                },
                "invocation": {
                    "kind": "python",
                    "path": "tools/compiler_diagnosis.py",
                    "fixed_argv": ["project", "--compact"],
                    "timeout_seconds": 15,
                },
            }],
        },
    }), encoding="utf-8")

    head = _init_git(comp)

    # Seed cached refusal witness matching current HEAD and file digests
    cache_doc = {
        "schema_version": "mncs.compiler.selected-callable-diagnosis/1",
        "status": "refused",
        "freshness": "current",
        "reused": False,
        "delivery_completeness": "complete",
        "unavailable_providers": [],
        "compiler_provenance": {
            "compiler": "mncs.compiler.vm_emit.v1::compile_and_emit_project_vm_callable",
            "compiler_repository": "mncs-compiler",
            "compiler_revision": head,
            "emitter_source": "src/compiler/vm_emit.mncs",
            "emitter_source_sha256": _sha(comp / "src/compiler/vm_emit.mncs"),
            "selected_target": "mncs:0.2:function:mncs.test.lex_batch_caller.v1::call_lex_batch_from",
            "selected_specialization": "mncs.compiler.segment.v1::lex_batch_from<4, 32>",
            "last_completed_phase": "ssa_verification",
            "failing_phase": "instruction_lowering",
            "owner": {"repository": "mncs-compiler", "component": "src/compiler/vm_emit.mncs"},
        },
        "bootstrap_status": {
            "stage1_to_stage2_rebuild": "UNKNOWN",
            "stage1_claimed": False,
            "stage2_claimed": False,
            "self_host_matrix": {"path": "evidence/SELF-HOST-MATRIX.json", "sha256": _sha(comp / "evidence/SELF-HOST-MATRIX.json")},
            "supporting_evidence": {"path": "evidence/sample.json", "sha256": _sha(comp / "evidence/sample.json")},
            "missing_rebuild_evidence": ["full_compiler_closure", "stage1_self_compile", "stage2_equivalence"],
        },
        "diagnosis": {
            "ok": False,
            "error_code": 4,
            "error_label": "unsupported_or_unresolved_type_or_schema",
            "error_phase": "instruction_lowering",
            "error_callable": "mncs:0.2:function:mncs.compiler.lexer.v1::lex_tokens_from",
            "error_function_index": 2,
            "error_block": 0,
            "error_instruction": 0,
            "ssa_operation": {"kind_id": 5, "kind": "EnumConstruct", "source_line": 979},
            "types": {
                "expected": "supported_vm_type",
                "observed": "nominal_finite_enum",
                "observed_nominal_identity": "mncs:0.2:finite-type:mncs.compiler.lexer.v1::TokenList",
            },
            "authoritative_declarations": {
                "mncs:0.2:function:mncs.compiler.lexer.v1::lex_tokens_from": {
                    "file": "src/compiler/lexer.mncs", "line": 978, "kind": "function",
                },
                "mncs:0.2:finite-type:mncs.compiler.lexer.v1::TokenList": {
                    "file": "src/compiler/lexer.mncs", "line": 975, "kind": "finite_type",
                },
            },
        },
        "providers": {
            "language_service": {"status": "available"},
            "doctor": {"status": "available", "findings": [{"code": "stale_matrix_blocker_description"}]},
            "debug": {"status": "available_demand_driven"},
        },
        "invalidation": {
            "inputs": {
                "compiler_revision": head,
                "emitter_source_sha256": _sha(comp / "src/compiler/vm_emit.mncs"),
                "source_digests": {
                    "src/compiler/lexer.mncs": _sha(comp / "src/compiler/lexer.mncs"),
                    "src/compiler/segment.mncs": _sha(comp / "src/compiler/segment.mncs"),
                },
            },
        },
    }
    (comp / ".mncs/cache/selected-callable-diagnosis.json").write_text(json.dumps(cache_doc, indent=2), encoding="utf-8")

    _write_sibling_manifest(ws / "mncs-doctor", "mncs-doctor", "compiler-vm-health", "tools/compiler_vm.py")
    _write_sibling_manifest(ws / "mncs-debug", "mncs-debug", "debugger", "bin/mncs-debug.py")
    if include_ls:
        _write_sibling_manifest(ws / "mncs-language-service", "mncs-language-service", "resident-status", "tools/mnls_provider.py")

    return ws, comp


def _run_env(state_dir: Path, cwd: Path, *args: str) -> dict:
    res = subprocess.run(
        [sys.executable, str(CLI), "--state-dir", str(state_dir), "--persistence", "file", *args],
        cwd=str(cwd), capture_output=True, text=True, check=False,
    )
    assert res.returncode == 0, f"rc={res.returncode}\nSTDOUT:{res.stdout}\nSTDERR:{res.stderr}"
    return json.loads(res.stdout)


class CompilerInvestigationDeliveryTests(unittest.TestCase):
    def test_entry_delivers_diagnosis_reuses_on_reentry_and_invalidates_on_source_change(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            state_dir = tmp / "state"
            _ws, comp = _make_workspace(tmp, include_ls=True)

            # 1. Ordinary entry delivers concise diagnosis + provenance + separate UNKNOWN bootstrap
            entry1 = _run_env(state_dir, comp, "enter", "--consumer", "agent-a")
            diag1 = entry1.get("diagnostic")
            self.assertIsInstance(diag1, dict, f"diagnostic missing from entry: {list(entry1.keys())}")
            self.assertFalse(diag1.get("budgeted"), f"diagnostic was budgeted away: {diag1}")
            ci1 = diag1.get("compiler_investigation")
            self.assertIsInstance(ci1, dict, diag1)
            self.assertEqual(ci1["status"], "refused")
            self.assertEqual(ci1["freshness"], "current")
            self.assertEqual(ci1["delivery_completeness"], "complete")
            self.assertEqual(ci1["provenance"]["last_completed_phase"], "ssa_verification")
            self.assertEqual(ci1["provenance"]["failing_phase"], "instruction_lowering")
            self.assertEqual(ci1["provenance"]["owner"], "mncs-compiler:src/compiler/vm_emit.mncs")
            self.assertEqual(ci1["bootstrap"]["stage1_to_stage2_rebuild"], "UNKNOWN")
            self.assertFalse(ci1["bootstrap"]["stage1_claimed"])
            self.assertEqual(ci1["diagnosis"]["callable"], "mncs:0.2:function:mncs.compiler.lexer.v1::lex_tokens_from")
            self.assertEqual(ci1["diagnosis"]["ssa_op"], "EnumConstruct")
            self.assertEqual(ci1["diagnosis"]["observed_nominal"], "mncs:0.2:finite-type:mncs.compiler.lexer.v1::TokenList")
            self.assertEqual(ci1["diagnosis"]["source"], "src/compiler/lexer.mncs:979")
            self.assertFalse(ci1["measurements"]["heavyweight_compile_launched"])
            launches_after_1 = int((comp / ".provider_launches").read_text().strip())
            self.assertEqual(launches_after_1, 1)

            # 2. Unchanged re-entry reuses epoch with 0 new provider launches
            entry2 = _run_env(state_dir, comp, "enter", "--consumer", "agent-a")
            diag2 = entry2["diagnostic"]
            self.assertTrue(diag2["reused"])
            ci2 = diag2["compiler_investigation"]
            self.assertTrue(ci2["reused"])
            self.assertEqual(ci2["measurements"]["provider_launches"], 0)
            self.assertEqual(int((comp / ".provider_launches").read_text().strip()), 1)

            # 3. `mncs-env diagnostic` also recovers it cleanly
            sid = entry1["session_id"]
            dcmd = _run_env(state_dir, comp, "diagnostic", sid)
            self.assertTrue(dcmd["reused"])
            self.assertEqual(dcmd["compiler_investigation"]["diagnosis"]["callable"],
                             "mncs:0.2:function:mncs.compiler.lexer.v1::lex_tokens_from")

            # 4. Source revision change invalidates old diagnosis
            (comp / "src/compiler/lexer.mncs").write_text(
                "mncs 0.19;\nmodule mncs.compiler.lexer.v1;\nenum TokenList { TNil }\n// changed\n",
                encoding="utf-8",
            )
            entry3 = _run_env(state_dir, comp, "enter", "--consumer", "agent-a")
            ci3 = entry3["diagnostic"]["compiler_investigation"]
            self.assertEqual(ci3["freshness"], "invalidated")
            self.assertIn("src/compiler/lexer.mncs", ci3.get("invalidated_reasons", []))
            self.assertEqual(int((comp / ".provider_launches").read_text().strip()), 2)

    def test_unavailable_sibling_provider_yields_explicit_partial_result(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            state_dir = tmp / "state"
            _ws, comp = _make_workspace(tmp, include_ls=False)
            entry = _run_env(state_dir, comp, "enter", "--consumer", "agent-b")
            ci = entry["diagnostic"]["compiler_investigation"]
            self.assertEqual(ci["delivery_completeness"], "partial")
            self.assertIn("mncs-language-service", ci["unavailable_providers"])

    def test_budgeted_entry_replaces_historical_refusal_with_capacity_diagnosis(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            state_dir = tmp / "state"
            _, comp = _make_workspace(tmp, include_ls=True)
            definition_path = comp / ".mncs/environment.json"
            definition = json.loads(definition_path.read_text())
            definition["context_budget"] = {"degraded_bytes": 1200}
            definition_path.write_text(json.dumps(definition))
            first = _run_env(state_dir, comp, "enter", "--consumer", "agent")
            prior = first["diagnostic"]["compiler_investigation"]
            self.assertEqual(prior["diagnosis"]["observed_nominal"],
                             "mncs:0.2:finite-type:mncs.compiler.lexer.v1::TokenList")

            target = "mncs:0.2:function:mncs.test.parse_use_caller.v1::call_parse_use"
            declaration = "mncs:0.2:function:mncs.compiler.decl.v1::parse_use"
            cache = comp / ".mncs/cache/selected-callable-diagnosis.json"
            doc = json.loads(cache.read_text())
            doc["compiler_provenance"].update({
                "selected_target": target,
                "selected_specialization": "mncs.compiler.decl.v1::parse_use<4,32>",
                "declaration_identity": declaration,
                "last_completed_phase": "none",
                "failing_phase": "project_request_capacity",
            })
            capacity = {
                "phase": "project_request_capacity",
                "rejecting_component": "mncs-compiler:tools/test_native_decl_vm_emit.py",
                "supplied_capacity": 1024,
                "required_capacity": 1031,
            }
            doc["diagnosis"] = {
                "ok": False, "error_code": None, "error_label": capacity["phase"],
                "request_failure": capacity, "error_function_index": None,
                "error_block": None, "error_instruction": None,
                "authoritative_declarations": {
                    declaration: {"identity": declaration, "file": "src/compiler/decl.mncs",
                                  "byte_span": [239243, 240406], "provider": "mncs-language-service"},
                },
            }
            cache.write_text(json.dumps(doc))
            entry = _run_env(state_dir, comp, "enter", "--consumer", "agent")
            diagnostic = entry["diagnostic"]
            self.assertTrue(diagnostic.get("budgeted"), diagnostic)
            current = diagnostic["compiler_investigation"]
            self.assertEqual(current["provenance"]["target"], target)
            self.assertEqual(current["provenance"]["revision"], _git(comp, "rev-parse", "HEAD")[:12])
            self.assertEqual(current["diagnosis"]["request_failure"], capacity)
            self.assertNotIn("error_function_index", current["diagnosis"])
            self.assertNotIn("TokenList", json.dumps(current))
            full = json.loads((state_dir / diagnostic["evidence"]).read_text())
            self.assertEqual(full["compiler_investigation"]["declaration"]["byte_span"], [239243, 240406])
            launches = (comp / ".provider_launches").read_text()
            reentry = _run_env(state_dir, comp, "enter", "--consumer", "agent")
            self.assertEqual(reentry["session_id"], entry["session_id"])
            self.assertEqual((comp / ".provider_launches").read_text(), launches)


    def test_healthy_budget_retains_executed_compiler_investigation(self):
        with tempfile.TemporaryDirectory() as td:
            state_dir = Path(td) / "state"
            _, comp = _make_workspace(Path(td), include_ls=True)
            definition_path = comp / ".mncs/environment.json"
            definition = json.loads(definition_path.read_text())
            definition["context_budget"] = {"healthy_bytes": 768}
            definition_path.write_text(json.dumps(definition))

            cache = comp / ".mncs/cache/selected-callable-diagnosis.json"
            doc = json.loads(cache.read_text())
            target = "mncs:0.2:function:mncs.test.parse_use_caller.v1::call_parse_use"
            declaration = "mncs:0.2:function:mncs.compiler.decl.v1::parse_use"
            artifact = "sha256:" + "a" * 64
            doc["status"] = "executed"
            doc["compiler_provenance"].update({
                "selected_target": target,
                "selected_specialization": "mncs.compiler.decl.v1::parse_use<4,32>",
                "declaration_identity": declaration,
                "last_completed_phase": "execution",
                "failing_phase": None,
                "artifact_id": artifact,
            })
            doc["diagnosis"] = {
                "ok": True,
                "error_code": 0,
                "error_label": "ok",
                "authoritative_declarations": {
                    declaration: {"identity": declaration, "file": "src/compiler/decl.mncs",
                                  "byte_span": [239243, 240406], "provider": "mncs-language-service"},
                },
            }
            cache.write_text(json.dumps(doc))

            entry = _run_env(state_dir, comp, "enter", "--consumer", "agent")
            diagnostic = entry["diagnostic"]
            self.assertTrue(diagnostic.get("budgeted"), diagnostic)
            current = diagnostic["compiler_investigation"]
            self.assertEqual(current["status"], "executed")
            self.assertEqual(current["freshness"], "current")
            self.assertEqual(current["provenance"]["target"], target)
            self.assertEqual(current["provenance"]["compiler"], doc["compiler_provenance"]["compiler"])
            self.assertEqual(current["provenance"]["revision"], _git(comp, "rev-parse", "HEAD")[:12])
            self.assertEqual(current["provenance"]["last_completed_phase"], "execution")
            full = json.loads((state_dir / diagnostic["evidence"]).read_text())
            self.assertEqual(full["compiler_investigation"]["provenance"]["artifact_id"], artifact)
            self.assertEqual(full["compiler_investigation"]["declaration"]["byte_span"], [239243, 240406])

            launches = (comp / ".provider_launches").read_text()
            reentry = _run_env(state_dir, comp, "enter", "--consumer", "agent")
            self.assertTrue(reentry["diagnostic"]["compiler_investigation"]["reused"])
            self.assertEqual((comp / ".provider_launches").read_text(), launches)

    def test_failing_compiler_diagnosis_provider_yields_explicit_partial_result(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            state_dir = tmp / "state"
            _ws, comp = _make_workspace(tmp, include_ls=True, broken_diag_provider=True)
            entry = _run_env(state_dir, comp, "enter", "--consumer", "agent-c")
            ci = entry["diagnostic"]["compiler_investigation"]
            self.assertEqual(ci["status"], "provider_unavailable")
            self.assertEqual(ci["delivery_completeness"], "partial")
            self.assertIn("mncs-compiler:selected-callable-diagnosis", ci["unavailable_providers"])
            self.assertEqual(ci["bootstrap"]["stage1_to_stage2_rebuild"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
