#!/usr/bin/env python3
"""Persist and selectively retrieve real Environment state through Store bindings.

Host code supplies bounded bytes and independent SHA oracles. Store owns
all representation, codec, selection, closure, plan and integrity decisions.
Failed campaigns are retained for inspection; no selected source is changed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(argv, cwd, codes=(0,), timeout=300):
    value = subprocess.run(
        [str(x) for x in argv],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if value.returncode not in codes:
        raise AssertionError(
            f"{argv}: exit {value.returncode}: {value.stderr or value.stdout}"
        )
    return json.loads(value.stdout)


def payload(result):
    data = result["payload"]
    value = (
        Path(data["file"]).read_bytes()
        if "file" in data
        else base64.b64decode(data["base64"])
    )
    assert hashlib.sha256(value).hexdigest() == data["sha256"]
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--family-root", type=Path, default=ROOT.parent)
    parser.add_argument("--campaign", type=Path)
    args = parser.parse_args()
    family = args.family_root.resolve()
    campaign = args.campaign or Path(tempfile.mkdtemp(prefix="mncs-adaptive-proof-"))
    campaign = campaign.resolve()
    campaign.mkdir(exist_ok=True)
    (campaign / ".mncs").mkdir(exist_ok=True)
    (campaign / "consumer/subdirectory").mkdir(parents=True, exist_ok=True)
    # Clone committed providers into owned checkout scopes. This prevents a
    # proof claim from adopting another agent's selected branch or dirty work.
    for name in ("mncs-environment", "mncs-language", "mncs-store"):
        subprocess.run(
            [
                "git",
                "clone",
                "--shared",
                "--quiet",
                str(family / name),
                str(campaign / name),
            ],
            check=True,
        )
    target = campaign / "mncs-language/target/release"
    target.mkdir(parents=True)
    for name in ("mncs", "libmncs_embed.so"):
        shutil.copy2(family / "mncs-language/target/release" / name, target / name)
    definition = {
        "name": "adaptive-store-consumer",
        "workspace_root": str(campaign),
        "workspace_scope": {
            "kind": "workspace",
            "repositories": ["mncs-environment", "mncs-language", "mncs-store"],
        },
        "intent": {
            "goal": "persist and selectively retrieve actual Environment work context",
            "repositories": ["mncs-store"],
        },
        "required_capabilities": [
            "mncs-store:adaptive-admit",
            "mncs-store:adaptive-inspect-envelope",
            "mncs-store:adaptive-materialize",
            "mncs-store:adaptive-status",
        ],
        "services": [
            {
                "identity": "mncs-store:adaptive",
                "required": True,
                "probe": {"capability": "mncs-store:adaptive-status", "argv": []},
                "response_schema": "mncs.store.provider-result/1",
                "ready_when": {"/state": "ready"},
            }
        ],
    }
    (campaign / ".mncs/environment.json").write_text(json.dumps(definition))
    prefix = [
        sys.executable,
        ROOT / "scripts/mncs-env",
        "--state-dir",
        campaign / "environment-state",
    ]
    readiness_attempts = []
    for _ in range(6):
        context = run(
            [*prefix, "enter", "--consumer", "sol-store-integration"],
            campaign,
            codes=(0, 5),
        )
        readiness_attempts.append(context["readiness"])
        if not context["readiness"]["blocking"]:
            break
        # Retry only an observed bounded timeout, preserving the same durable
        # session. Invalid descriptors or native failures never get hidden.
        assert all(
            item["code"] == "service-probe-timeout"
            for item in context["readiness"]["services"]
        ), context["readiness"]
    assert not context["readiness"]["blocking"], context["readiness"]
    session = context["session_id"]
    provider = campaign / "mncs-store"
    run(
        [
            *prefix,
            "claims",
            session,
            "--acquire",
            "mncs-store",
            "--worktree",
            provider,
            "--reason",
            "Store-owned provider transport writes only this owned campaign",
        ],
        campaign,
    )
    capabilities = run([*prefix, "capabilities", session], campaign)
    inspected = run([*prefix, "inspect", session], campaign)
    # Fixed-size JSON regions are valid documents plus whitespace. Each starts
    # on a Store chunk boundary, exposing real information movement separately.
    regions = []
    for value in [context, capabilities, inspected]:
        raw = json.dumps(value, sort_keys=True, indent=2).encode()
        size = ((len(raw) + 65535) // 65536) * 65536
        regions.append(raw.ljust(size, b" "))
    original = b"".join(regions)
    source = campaign / "environment-work-context.payload"
    source.write_bytes(original)
    synopsis = b"Environment work context: entry identity, provider inventory, durable session state"
    common = {
        "store": str(campaign / "adaptive-store"),
        "domain_schema": {"utf8": "mncs.environment.work-context/1"},
        "domain_identity": {"utf8": session},
    }
    metrics = {}

    def invoke(operation, fields=None, expected="ok", cwd=Path("/tmp")):
        request = campaign / "request.json"
        request.write_text(json.dumps({**common, **(fields or {})}))
        out = run(
            [
                *prefix,
                "invoke",
                session,
                f"mncs-store:adaptive-{operation}",
                "--",
                "--request",
                request,
            ],
            cwd,
            codes=(0, 4),
        )
        result = json.loads(out["stdout"])
        assert result["status"] == expected, result
        metrics[operation] = result.get("metrics", {})
        assert result.get("selected", {}).get("MNCS_STORE_ROOT") == str(provider)
        return result.get("result", result)

    offset = 0
    blocks = []
    for index, region in enumerate(regions):
        blocks.append(
            {"index": index, "tag": index + 1, "start": offset, "length": len(region)}
        )
        offset += len(region)
    admitted = invoke(
        "admit",
        {
            "descriptor": {"utf8": "mncs.environment.work-context/json-regions/1"},
            "payload": {"file": str(source)},
            "expected_generation": 0,
            "synopsis": {"utf8": synopsis.decode()},
            "blocks": blocks,
            "representations": [],
        },
    )
    assert admitted["code"] == "COMMITTED"
    evolved = invoke(
        "add-representation",
        {
            "expected_generation": admitted["generation"],
            "representation": {
                "fidelity": 5,
                "codec": "rle",
                "payload": {"file": str(source)},
            },
        },
    )
    assert evolved["code"] == "COMMITTED"
    assert (
        evolved["logical_id"] == admitted["logical_id"]
        and evolved["content_id"] == admitted["content_id"]
    )
    assert evolved["representation_root"] != admitted["representation_root"]
    repeated = invoke(
        "add-representation",
        {
            "expected_generation": evolved["generation"],
            "representation": {
                "fidelity": 5,
                "codec": "rle",
                "payload": {"file": str(source)},
            },
        },
    )
    assert (
        repeated["code"] == "DUPLICATE"
        and repeated["generation"] == evolved["generation"]
    )
    again = run(
        [*prefix, "enter", "--consumer", "sol-store-integration"],
        campaign / "consumer/subdirectory",
        codes=(0, 5),
    )
    assert again["session_id"] == session and again["entry"]["reused"]
    assert context["toolchain"] == again["toolchain"]
    # Hide an unrelated chunk: metadata and selective retrieval must still work.
    # Its absence proves fresh-process open never eagerly expands all objects.
    missing = hashlib.sha256(regions[-1][-65536:]).hexdigest()
    path = campaign / "adaptive-store/chunks" / f"{missing}.chunk"
    saved = path.read_bytes()
    path.unlink()
    try:
        env = invoke("inspect-envelope")
        reps = invoke("list-representations")
        assert env["fields"]["rep_count"] == 3
        assert env["fields"]["block_count"] == 3
        assert env["stored_bytes_touched"] < len(original)
        syn = invoke("read-synopsis")
        assert base64.b64decode(syn["base64"]) == synopsis
        selection = invoke("select", {"intent": {"fidelity": 5}})
        assert selection["satisfied"] and selection["constraints_satisfied"]
        selective = invoke(
            "materialize", {"intent": {"fidelity": 5, "transfer": 1000}, "tag": 1}
        )
        assert payload(selective) == regions[0]
        assert selective["materialized_bytes"] == len(regions[0])
        assert selective["stored_bytes_touched"] < len(original)
        base = reps[0]["fields"]["root"]
        plan = invoke(
            "materialize-plan",
            {"plan": {"fidelity": 5, "root": base, "mask": 1, "cap": len(regions[0])}},
        )
        assert payload(plan) == regions[0]
        refused = invoke(
            "materialize-plan",
            {"plan": {"fidelity": 5, "root": base, "mask": 1, "cap": 1}},
            expected="error",
        )
        assert refused["diagnostic"]["code"] == "DENIED"
        bad = invoke("select", {"intent": {"fidelity": 6}}, expected="error")
        assert bad["diagnostic"]["code"] == "MALFORMED_INTENT"
    finally:
        path.write_bytes(saved)
    coded = invoke("materialize", {"intent": {"fidelity": 5, "transfer": 1000}})
    assert coded["representation_index"] == 2 and coded["exact_verified"]
    assert payload(coded) == original
    exact = invoke("materialize", {"intent": {"fidelity": 5}})
    assert exact["exact_verified"] and payload(exact) == original
    checkpoint = run(
        [
            *prefix,
            "checkpoint",
            session,
            "--progress",
            "adaptive context retrievable through Store",
        ],
        campaign,
    )
    resumed = run([*prefix, "resume", session], Path("/tmp"))
    assert resumed["session_id"] == session
    handoff = run(
        [*prefix, "handoff", session, "--to", "store-proof-successor"], campaign
    )
    run(
        [
            *prefix,
            "accept",
            session,
            handoff["identity"],
            "--consumer",
            "store-proof-successor",
        ],
        Path("/tmp"),
    )
    assert invoke("inspect-envelope")["fields"]["logical"] == env["fields"]["logical"]
    report = {
        "schema_version": "mncs.environment.adaptive-store-proof/1",
        "campaign": str(campaign),
        "session": session,
        "checkpoint": checkpoint["identity"],
        "selected_providers": again["projects"],
        "initial_readiness_attempts": readiness_attempts,
        "selected_toolchain": context["toolchain"],
        "logical_id": admitted["logical_id"],
        "content_id": admitted["content_id"],
        "payload_bytes": len(original),
        "inspect_bytes": env["stored_bytes_touched"],
        "selective_materialized_bytes": selective["materialized_bytes"],
        "selective_stored_bytes": selective["stored_bytes_touched"],
        "materialized_bytes_avoided": len(original) - selective["materialized_bytes"],
        "coded_transfer_bytes": coded["stored_bytes_touched"],
        "exact_sha256": exact["payload"]["sha256"],
        "representations": reps,
        "metrics": metrics,
        "proofs": [
            "generation-bound-representation-evolution",
            "idempotent-representation-admission",
            "fresh-process",
            "subdirectory-entry-reuse",
            "selected-bindings",
            "absent-unrelated-chunk",
            "synopsis",
            "tag-selection",
            "validated-external-plan",
            "plan-cap-refusal",
            "malformed-intent",
            "coded-exact-reconstruction",
            "base-exact-reconstruction",
            "checkpoint-resume-handoff",
        ],
    }
    (campaign / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
