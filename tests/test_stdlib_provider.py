"""Standard-library provider discovery (Stage F).

The environment selects `mncs-stdlib` by repository name and binds its
provider contracts through the standard discovery path: no guessed
paths, no special cases. These tests pin that behavior with a
stdlib-shaped fixture plus the real default definition.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from mncs_env import capabilities


REAL_ROOT = Path(__file__).resolve().parents[1]


def _write_stdlib_fixture(repo: Path) -> None:
    """Minimal stdlib-shaped checkout: contracts + manifest + info tool."""
    (repo / "library").mkdir(parents=True)
    (repo / "tools").mkdir(exist_ok=True)
    info = repo / "tools" / "stdlib_info.py"
    info.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "print(json.dumps({'command': sys.argv[1:]}))\n",
        encoding="utf-8",
    )
    (repo / "stdlib-manifest.json").write_text(json.dumps({
        "schema_version": "mncs.stdlib-manifest/1",
        "repository": "mncs-stdlib",
        "bundle_identity": "mncs:stdlib-bundle:fixture",
        "bundle_path": "dist/stdlib-bundle.json",
        "library_path": "library",
        "requires_profile": {"min": "0.5", "max": "0.18"},
        "module_count": 0,
        "modules": [],
    }), encoding="utf-8")
    (repo / "family-semantic-contracts-v1.json").write_text(json.dumps({
        "schema_version": "commons.mncs.semantic-contract-declarations/v1",
        "repository_id": "mncs-stdlib",
        "revision": "1",
        "provides": [{
            "contract_identity": "mncs.stdlib-manifest/1",
            "contract_revision": "1",
            "exported_identity": "mncs-stdlib:stdlib-manifest",
            "evidence": "stdlib-manifest.json",
            "canonical_entrypoint": "stdlib-manifest",
            "status": "native_canonical",
            "invocation": {
                "kind": "python",
                "path": "tools/stdlib_info.py",
                "adapter_library_paths": ["library"],
            },
            "effects": ["read"],
        }],
        "consumes": [],
    }), encoding="utf-8")


class StdlibProviderTests(unittest.TestCase):
    def test_fixture_stdlib_binds_manifest_capability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "mncs-stdlib"
            _write_stdlib_fixture(repo)
            facts = {"mncs-stdlib": {
                "path": str(repo), "head": "fixture-head",
                "branch": "main", "clean": True,
            }}
            bindings = capabilities.discover_capabilities(
                root,
                repository_roots={"mncs-stdlib": repo},
                checkout_facts=facts,
            )
            binding = next(
                item for item in bindings
                if item["capability"] == "mncs.stdlib-manifest/1"
            )
            self.assertEqual(binding["provider"], "mncs-stdlib")
            self.assertEqual(binding["contract_revision"], "1")
            self.assertEqual(
                binding["address"],
                "python:" + str((repo / "tools" / "stdlib_info.py").resolve()),
            )
            result = capabilities.invoke(binding, ["manifest"])
            self.assertEqual(result["status"], "ok")
            self.assertEqual(
                json.loads(result["stdout"])["command"], ["manifest"]
            )

    def test_default_definition_selects_mncs_stdlib(self) -> None:
        definition = json.loads(
            (REAL_ROOT / ".mncs" / "environment.json").read_text(encoding="utf-8")
        )
        repositories = definition["workspace_scope"]["repositories"]
        self.assertIn("mncs-stdlib", repositories)

    def test_language_service_reconcile_waits_for_its_bounded_startup(self) -> None:
        definition = json.loads(
            (REAL_ROOT / ".mncs" / "environment.json").read_text(encoding="utf-8")
        )
        service = next(
            item for item in definition["services"]
            if item["identity"] == "mncs-language-service:resident-workspace"
        )
        argv = service["reconcile"]["argv"]
        start_timeout = int(argv[argv.index("--start-timeout") + 1])
        self.assertGreaterEqual(service["reconcile_timeout_seconds"], start_timeout)

    def test_real_stdlib_contracts_parse_and_name_the_manifest(self) -> None:
        # Sibling-checkout test: skipped clearly outside a family layout.
        # The family root resolves exactly like production: the default
        # definition's workspace_root relative to its .mncs/ directory.
        definition_dir = REAL_ROOT / ".mncs"
        definition = json.loads(
            (definition_dir / "environment.json").read_text(encoding="utf-8")
        )
        family = (
            definition_dir / definition.get("workspace_root", "../..")
        ).resolve()
        if not (family / "mncs-language").is_dir():
            self.skipTest(f"no family layout at {family}")
        stdlib = family / "mncs-stdlib"
        contracts = stdlib / "family-semantic-contracts-v1.json"
        manifest = stdlib / "stdlib-manifest.json"
        if not contracts.is_file() or not manifest.is_file():
            self.skipTest("mncs-stdlib sibling checkout absent")
        declared = json.loads(contracts.read_text(encoding="utf-8"))
        identities = {
            item["contract_identity"] for item in declared.get("provides", [])
        }
        self.assertIn("mncs.stdlib-manifest/1", identities)
        published = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(published["schema_version"], "mncs.stdlib-manifest/1")
        self.assertTrue(published["bundle_identity"].startswith("mncs:stdlib-bundle:"))
        self.assertGreaterEqual(published["module_count"], 50)


if __name__ == "__main__":
    unittest.main()
