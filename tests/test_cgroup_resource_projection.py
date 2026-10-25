"""Verify authentic resource and capability-delivery records through ECI."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

from mncs_env import capability_intelligence

ROOT = Path(__file__).resolve().parents[1]


def _git(checkout: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(checkout), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": os.environ.get("PATH", ""),
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_OPTIONAL_LOCKS": "0",
        },
    )
    return result.stdout.strip()


def _session(tmp_path: Path, records: list[dict]):
    root = tmp_path / "workspace"
    checkout = root / "mncs-environment"
    checkout.mkdir(parents=True)
    _git(checkout, "init", "--quiet")
    _git(checkout, "config", "user.name", "test")
    _git(checkout, "config", "user.email", "test@example.invalid")

    references = []
    for record in records:
        relative = f"evidence/environmental-capability/{record['identity']}.json"
        path = checkout / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2).encode()
        path.write_bytes(raw + b"\n")
        _git(checkout, "add", relative)
        references.append({
            "identity": record["identity"],
            "repository": "mncs-environment",
            "commit": "",
            "path": relative,
            "sha256": hashlib.sha256(raw + b"\n").hexdigest(),
        })
    _git(checkout, "commit", "--quiet", "-m", "test resource observations")
    head = _git(checkout, "rev-parse", "HEAD")
    for reference in references:
        reference["commit"] = head

    state = {
        "checkpoint_id": "chk_resource_observation_projection",
        "recorded_at": "2026-10-25T00:00:00+00:00",
        "recorded_by": {"consumer_id": "recorder", "principal_id": "ctrl_test"},
        "evidence": references,
        "evidence_count": len(references),
        "evidence_truncated": False,
        "delivery": {"status": "pending"},
    }
    session = SimpleNamespace(
        session_id="ses_resource_projection_reader",
        snapshot={
            "environment_id": "renv_resource_projection_reader",
            "authenticated_principal_id": "ctrl_" + "a" * 32,
            "provenance": {"definition_id": "env_resource_projection_reader"},
            "intent": {
                "identity": "int_resource_projection_reader",
                "goal": "inspect resource headroom and capability delivery",
            },
            "campaign": {"identity": "cmp_resource_projection_reader"},
            "workspace": {"root": str(root)},
            "selected_checkouts": {"mncs-environment": {"path": str(checkout)}},
        },
        campaign_state=lambda **_kwargs: state,
        recent_events=lambda *, limit, before_sequence=None: {
            "event_count": 0,
            "events": [],
            "truncated": False,
            "next_before_sequence": None,
        },
    )
    return session


def test_resource_headroom_and_control_delivery_are_queryable_with_provenance(
    tmp_path: Path,
) -> None:
    resource_path = ROOT / (
        "evidence/environmental-capability/cgroup-resource-headroom-20261025.json"
    )
    delivery_path = ROOT / (
        "evidence/environmental-capability/resource-observation-control-delivery-20261025.json"
    )
    records = [
        json.loads(resource_path.read_text(encoding="utf-8")),
        json.loads(delivery_path.read_text(encoding="utf-8")),
    ]
    session = _session(tmp_path, records)

    result = capability_intelligence.project(session)

    assert result["status"] == "ready"
    assert result["observation_count"] == 2
    assert result["pressure_hypothesis_count"] == 1
    observations = {item["identity"]: item for item in result["observations"]}
    resource = observations[records[0]["identity"]]
    assert resource["related_work"]["session_id"] == "ses_b6774545fb790bde"
    resource_outcome = resource["observation"]["outcome"]
    assert resource_outcome["measurement_summary"]["direct_measurements"] == {
        "cgroup_memory_current_bytes": 8404451328,
        "process_rss_bytes": 6864896,
        "system_memory_available_bytes": 24515821568,
        "system_memory_total_bytes": 33411571712,
    }
    assert resource_outcome["measurement_summary"]["measurement_availability"][
        "session_process_mapping"
    ] == "not-asserted"
    assert resource_outcome["session_process_mapping"] == "not-asserted"
    assert "mapping of sampled PID 1908204 to the Environment session" in resource[
        "measurement_limits"
    ]

    delivery = observations[records[1]["identity"]]
    assert delivery["related_work"]["session_id"] == "ses_b6774545fb790bde"
    assert delivery["pressure_hypothesis"]["identity"] == (
        "ph_environment_resource_observation_not_exposed_by_control_20261025"
    )
    hypothesis = result["pressure_hypotheses"][0]
    assert hypothesis["observation_count"] == 1
    assert hypothesis["recurrence_status"] == "not-established"
    assert [source["identity"] for source in hypothesis["evidence"]] == [
        records[1]["identity"]
    ]
