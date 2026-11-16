"""Tests for Environment retention pin composition and non-destructive Store readiness."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mncs_env import sessions
from mncs_env.session_store import open_store


def _store_package_dir() -> Path:
    env_override = os.environ.get("MNCS_STORE_PYTHON")
    if env_override:
        return Path(env_override).resolve()
    return (Path(__file__).resolve().parents[2] / "mncs-store" / "python").resolve()


def test_environment_retention_pins_and_non_destructive_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = _store_package_dir()
    if str(package) not in sys.path:
        sys.path.insert(0, str(package))
    from mncs_store import embedded

    monkeypatch.setattr(embedded, "GENERATION_CHECKPOINT_INTERVAL", 4)
    state = tmp_path / "env-state"
    store = open_store(
        state,
        "store",
        store_package_dir=package,
        verify_on_open=False,
        defer_mutation=False,
    )
    try:
        reconciler_sess = sessions.Session.create(
            state_dir=state,
            environment={
                "identity": "reconciler-fixture",
                "workspace": {"root": str(tmp_path), "repositories": []},
                "intent": {"goal": "background sync", "repositories": []},
                "bindings": [],
            },
            consumer_id="environment-reconciler",
            consumer_kind="service",
            authenticated_principal_id="reconciler-principal",
            store=store,
        )
        reconciler_sess.transition("resolving", "fixture")
        reconciler_sess.transition("ready", "fixture")
        reconciler_sess.transition("active", "fixture")
        pinned_cursor = store.generation()
        reconciler_sess.snapshot["reconciler"] = {
            "cursors": {"store-replay": str(pinned_cursor)},
        }
        reconciler_sess._save()

        # Advance past several checkpoints via ordinary work commits
        worker = sessions.Session.create(
            state_dir=state,
            environment={
                "identity": "work-fixture",
                "workspace": {"root": str(tmp_path), "repositories": []},
                "intent": {"goal": "generate history", "repositories": []},
                "bindings": [],
            },
            consumer_id="worker",
            authenticated_principal_id="worker-principal",
            store=store,
        )
        for i in range(6):
            worker.checkpoint(progress=f"step-{i}", remaining=[])

        pins = store.backend.retention_pins()
        assert pins["pin_discovery_complete"] is True
        assert pins["fail_closed"] is False
        assert pinned_cursor in pins["mandatory_protected_generations"]

        # Without explicit policy window -> never invents 4096; requires policy
        no_policy = store.backend.maintenance_readiness()
        assert no_policy["autonomous_deletion_enabled"] is False
        assert no_policy["dry_run_only"] is True
        assert no_policy["policy_required"] is True
        assert no_policy["disposition"] == "policy-required"
        assert no_policy["inventory"]["floor"] == 0

        # Active claim -> maintenance readiness defers cleanly with zero mutation
        store.put_claim(
            {
                "claim_id": "claim:active-work",
                "version": 1,
                "session_id": worker.session_id,
                "repository": "fixture",
                "status": "held",
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
            }
        )
        deferred_claim = store.backend.maintenance_readiness(keep_last=2)
        assert deferred_claim["disposition"] == "deferred-active-claims"
        assert deferred_claim["dry_run_plan"] is None
        assert "active-workspace-claims" in deferred_claim["blockers"]

        store.put_claim(
            {
                "claim_id": "claim:active-work",
                "version": 2,
                "session_id": worker.session_id,
                "repository": "fixture",
                "status": "released",
            }
        )

        # Low memory threshold triggers resource pressure deferral cleanly
        deferred_mem = store.backend.maintenance_readiness(
            keep_last=2,
            min_available_memory_bytes=1 << 60,
        )
        assert deferred_mem["disposition"] == "deferred-resource-pressure"
        assert "insufficient-available-memory" in deferred_mem["blockers"]

        # Pending staging recovery defers cleanly
        pending_stage = store.backend.path / "staging" / "0000000000000001-0000000000000002"
        pending_stage.mkdir(parents=True)
        deferred_stage = store.backend.maintenance_readiness(keep_last=2)
        assert deferred_stage["disposition"] == "deferred-recovery"
        pending_stage.rmdir()

        # Resumable dry-run planning preserves reconciler pin and completes safely across calls
        mark_dir = state / "maintenance-mark"
        step_plan = store.backend.maintenance_readiness(
            keep_last=2,
            reclaim_chunks=True,
            mark_path=mark_dir,
            max_files=8,
            max_bytes=512 * 1024,
        )
        assert step_plan["autonomous_deletion_enabled"] is False
        assert step_plan["disposition"] in {"incomplete-resumable", "dry-run-planned"}
        assert pinned_cursor in step_plan["protected_generations"]

        head_before = store.generation()
        full_plan = store.backend.maintenance_readiness(
            keep_last=2,
            reclaim_chunks=True,
            mark_path=mark_dir,
            max_files=5000,
            max_bytes=64 * 1024 * 1024,
        )
        assert full_plan["disposition"] == "dry-run-planned"
        assert full_plan["dry_run_plan"]["status"] == "complete"
        assert full_plan["dry_run_plan"]["removed_files"] == 0
        assert full_plan["dry_run_plan"]["floor"] <= pinned_cursor
        assert store.generation() == head_before

        # CLI read-only inspection surfaces work cleanly without mutating Store
        env_root = Path(__file__).resolve().parents[1]
        cli_env = {**os.environ, "MNCS_STORE_PYTHON": str(package), "PYTHONPATH": str(env_root)}
        inv_proc = subprocess.run(
            [sys.executable, "-m", "mncs_env.cli", "--state-dir", str(state), "store", "--inventory"],
            capture_output=True,
            text=True,
            env=cli_env,
            check=True,
        )
        inv_json = json.loads(inv_proc.stdout)
        assert inv_json["head"] == head_before and inv_json["floor"] == 0

        ready_proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "mncs_env.cli",
                "--state-dir",
                str(state),
                "store",
                "--maintenance-readiness",
            ],
            capture_output=True,
            text=True,
            env=cli_env,
            check=True,
        )
        ready_json = json.loads(ready_proc.stdout)
        assert ready_json["autonomous_deletion_enabled"] is False
        assert ready_json["disposition"] == "policy-required"
        assert store.generation() == head_before
    finally:
        store.close()
