"""Filesystem observation distinguishes source edits from Git stat-cache churn."""

import subprocess
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

from mncs_env import observations


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def test_status_index_refresh_is_not_repository_control_drift(tmp_path: Path) -> None:
    checkout = tmp_path / "app"
    checkout.mkdir()
    git(checkout, "init", "-q")
    git(checkout, "config", "user.name", "Test")
    git(checkout, "config", "user.email", "test@example.invalid")
    source = checkout / "source.mncs"
    source.write_text("first source\n")
    git(checkout, "add", "source.mncs")
    git(checkout, "commit", "-qm", "baseline")

    session = SimpleNamespace(
        state_dir=tmp_path / "state",
        session_id="ses_observation_fixture",
    )
    previous, events, _ = observations.observe_repository(session, checkout, None)
    assert events == []

    source.write_text("edited source\n")
    before_index = observations.stamp(checkout / ".git" / "index")
    git(checkout, "status", "--porcelain=v1", "--untracked-files=no")
    after_index = observations.stamp(checkout / ".git" / "index")
    assert before_index != after_index, "fixture must exercise Git's stat-cache rewrite"

    _, events, _ = observations.observe_repository(session, checkout, previous)
    assert [event["kind"] for event in events] == ["file.changed"]
    assert events[0]["path"] == "source.mncs"


def test_python_test_caches_are_not_semantic_repository_inputs(tmp_path: Path) -> None:
    checkout = tmp_path / "app"
    checkout.mkdir()
    git(checkout, "init", "-q")
    git(checkout, "config", "user.name", "Test")
    git(checkout, "config", "user.email", "test@example.invalid")
    source = checkout / "source.mncs"
    source.write_text("source\n")
    git(checkout, "add", "source.mncs")
    git(checkout, "commit", "-qm", "baseline")
    session = SimpleNamespace(
        state_dir=tmp_path / "state",
        session_id="ses_observation_cache_fixture",
    )
    previous, events, _ = observations.observe_repository(session, checkout, None)
    assert events == []

    (checkout / "tools" / "__pycache__").mkdir(parents=True)
    (checkout / "tools" / "__pycache__" / "module.cpython-314.pyc").write_bytes(b"cache")
    (checkout / ".pytest_cache" / "v" / "cache").mkdir(parents=True)
    (checkout / ".pytest_cache" / "v" / "cache" / "nodeids").write_text("[]")

    current, events, _ = observations.observe_repository(session, checkout, previous)
    catalogue = json.loads(Path(current["artifact"]).read_text())
    assert events == []
    assert "tools/__pycache__/module.cpython-314.pyc" not in catalogue["material"]["files"]
    assert ".pytest_cache/v/cache/nodeids" not in catalogue["material"]["files"]

    # A catalogue created by an older observer may already contain these
    # paths. Their cleanup is cache maintenance, not a semantic source edit.
    legacy = json.loads(Path(previous["artifact"]).read_text())
    cache_name = "tools/__pycache__/module.cpython-314.pyc"
    legacy["material"]["files"][cache_name] = observations.stamp(checkout / cache_name)
    for directory in ("tools", "tools/__pycache__"):
        legacy["material"]["directories"][directory] = observations.stamp(checkout / directory)
    legacy_ref = observations._catalogue(session, legacy)
    shutil.rmtree(checkout / "tools" / "__pycache__")
    shutil.rmtree(checkout / ".pytest_cache")
    _, events, _ = observations.observe_repository(session, checkout, legacy_ref)
    assert events == []
