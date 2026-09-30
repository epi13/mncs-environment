#!/usr/bin/env python3
"""Fresh-agent proof over real selected Forge, Language Service, Test and Store.

Git clones and copying existing binaries are test setup, never provider
discovery. All service operations run through Environment's declared bindings.
The owned campaign is retained on failure for diagnosis.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
CLI = ROOT / "scripts/mncs-env"


def run(argv, *, cwd=Path("/tmp"), codes=(0,), timeout=120):
    result = subprocess.run([str(value) for value in argv], cwd=cwd, capture_output=True,
                            text=True, timeout=timeout)
    if result.returncode not in codes:
        raise AssertionError(f"{argv}: exit {result.returncode}: {result.stderr or result.stdout}")
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--family-root", type=Path, default=ROOT.parent)
    parser.add_argument("--forge-checkout", type=Path, required=True)
    args = parser.parse_args()
    campaign = Path(tempfile.mkdtemp(prefix="mncs-resident-proof-"))
    state = campaign / "state"
    session = None
    prefix = [sys.executable, CLI, "--state-dir", state]
    config = campaign / "mncs-environment/.mncs/forge.toml"
    success = False
    try:
        for name in ("mncs-environment", "mncs-language", "mncs-store", "mncs-test", "mncs-forge", "mncs-language-service"):
            source = ROOT if name == "mncs-environment" else args.forge_checkout if name == "mncs-forge" else args.family_root / name
            subprocess.run(["git", "clone", "--shared", "-q", str(source), str(campaign / name)], check=True)
        for name, relatives in (("mncs-language", ("target/release/mncs", "target/release/libmncs_embed.so")),
                                ("mncs-language-service", ("target/debug/mnls-language-service-host",))):
            for relative in relatives:
                target = campaign / name / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(args.family_root / name / relative, target)
        definition = campaign / "mncs-environment/.mncs/environment.json"
        definition.write_text((ROOT / "samples/forge-resident.environment.json").read_text())
        # The sample is relative to samples/, while the local entry definition
        # is in .mncs/: both have the same depth and authoritative workspace.
        entered = run([*prefix, "enter", "--consumer", "resident-proof"], cwd=definition.parent.parent, codes=(0, 5))
        session = entered["session_id"]
        run([*prefix, "claims", session, "--acquire", "mncs-forge", "--worktree", campaign / "mncs-forge",
             "--adopt", "--reason", "own isolated provider proof"])

        def invoke(capability, *argv):
            result = run([*prefix, "invoke", session, capability, "--", *argv])
            assert result["status"] == "ok", result
            return json.loads(result["stdout"])

        def service():
            return run([*prefix, "health", session], codes=(0, 5))["readiness"]["services"][0]

        def ready():
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                context = run([*prefix, "enter", "--consumer", "resident-proof"], cwd=definition.parent.parent / "docs", codes=(0, 5))
                assert context["session_id"] == session and context["entry"]["reused"]
                observed = service()
                # A live health probe can observe readiness after entry's
                # historical snapshot. Re-enter to persist that transition.
                if observed["status"] == "ready" and not context["readiness"]["blocking"]:
                    return context, observed["provider_observed"]
                if observed.get("observation", {}).get("/state") in {"failed", "incompatible"}:
                    raise AssertionError(observed)
                time.sleep(0.2)
            raise AssertionError(service())

        context, observed = ready()
        assert context["readiness"]["blocking"] == []
        assert context["readiness"]["status"] == "degraded"  # optional gaps do not block
        assert observed["identity"]["checkout"] == str(campaign / "mncs-forge")
        assert observed["identity"]["runtime"]["MNCS_STORE_ROOT"] == str(campaign / "mncs-store")
        for _ in range(2):
            response = invoke("mncs-forge:resident-reconcile", "--config", config)
            assert response["operation"] == "reused"
            assert response["status"]["observed"]["instance"] == observed["instance"]
        run(context["actions"]["health"]["argv"], cwd=Path("/tmp"))
        print("PASS fresh/subdirectory entry, repeated entry/reconcile, cwd-independent actions, optional providers")

        invoke("mncs-forge:resident-stop", "--config", config)
        deadline = time.monotonic() + 10
        while service().get("observation", {}).get("/state") != "stopped" and time.monotonic() < deadline:
            time.sleep(0.1)
        assert service()["status"] != "ready"
        _, restored = ready()
        assert restored["instance"] != observed["instance"]
        print("PASS stopped resident restoration and readiness re-probe")

        # Corruption applies only to the proof's selected provider checkout.
        manifest = campaign / "mncs-forge/.mncs/project.json"
        original = manifest.read_text()
        invalid = json.loads(original)
        for capability in invalid["contracts"]["provides"]:
            if capability["contract"] == "resident-status":
                capability["invocation"]["path"] = "missing-selected-provider.py"
        manifest.write_text(json.dumps(invalid))
        broken = run([*prefix, "enter", "--consumer", "resident-proof"], cwd=definition.parent.parent, codes=(5,))
        assert "mncs-forge:resident-status" in broken["readiness"]["required_unavailable"]
        manifest.write_text(original)
        _, restored = ready()
        print("PASS changed/invalid descriptors rediscovered; required capability blocks without ambient fallback")

        # A separate Forge checkout addressing the same project must reject the
        # running selected instance, even though that instance is healthy.
        alternate = campaign / "alternate-forge"
        subprocess.run(["git", "clone", "--shared", "-q", str(args.forge_checkout), str(alternate)], check=True)
        from mncs_env import capabilities
        bindings = capabilities.discover_capabilities(
            campaign, repository_roots={"mncs-forge": alternate,
                "mncs-language": campaign / "mncs-language", "mncs-language-service": campaign / "mncs-language-service"})
        binding = next(item for item in bindings if item["capability"] == "mncs-forge:resident-status")
        ambient = capabilities.invoke(binding, ["--config", str(config)],
            env=restored["identity"]["runtime"], timeout_seconds=3, output_limit_bytes=16384)
        assert json.loads(ambient["stdout"])["state"] == "incompatible"
        print("PASS healthy other-checkout Forge cannot satisfy selected readiness")

        doctor = invoke("mncs-forge:resident-work", "--config", config, "doctor")
        assert doctor["ledger"]["canonical"] == "mncs-store"
        assert doctor["native_execution"]["binary"].startswith(str(campaign / "mncs-language"))
        epoch = invoke("mncs-forge:resident-work", "--config", config, "epoch", "begin",
                       "--generator", "resident-proof", "--evaluator", "proof-evaluator")
        assert epoch["record_type"] == "epoch"
        native = run([*prefix, "invoke", session, "mncs.test-result/1", "--",
                      campaign / "mncs-environment/tests/fixtures/resident_work.mncs",
                      "--library", campaign / "mncs-language/library", "--library", campaign / "mncs-test/native", "--format", "text"])
        assert native["status"] == "ok" and "PASS" in native["stdout"]
        print("PASS real Forge epoch/Store publication and native MNCS repetition/result-join tests")

        checkpoint = run([*prefix, "checkpoint", session, "--progress", "resident verified"])
        resumed = run([*prefix, "resume", session, "--revalidate"])
        assert resumed["session_id"] == session
        handoff = run([*prefix, "handoff", session, "--to", "resident-successor", "--next", "continue"])
        run([*prefix, "accept", session, handoff["identity"], "--consumer", "resident-successor"])
        assert checkpoint["identity"].startswith("chk_")
        assert service()["status"] == "ready"
        print("PASS Store checkpoint/fresh-process resume/handoff with resident reuse")
        success = True
        print("FORGE RESIDENT PROOF: PASS")
    finally:
        if session:
            try:
                run([*prefix, "invoke", session, "mncs-forge:resident-stop", "--", "--config", config,
                     "--include-language-service"])
                time.sleep(0.5)
            except Exception as error:
                print(f"provider cleanup requires attention at {campaign}: {error}", file=sys.stderr)
                success = False
        if success:
            shutil.rmtree(campaign)
        else:
            print(f"retained owned campaign: {campaign}", file=sys.stderr)


if __name__ == "__main__":
    main()
