"""Per-process git-fact sharing: fewer launches, no stale post-write reads."""
import subprocess
from pathlib import Path

from mncs_env import workspace


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for args in (("init", "-q"), ("add", "-A"),
                 ("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")):
        if args[0] == "add":
            (path / "file.txt").write_text("v1\n")
        subprocess.run(["git", "-C", str(path), *args], check=True,
                       capture_output=True)
    return path


def test_inspect_repo_is_shared_then_invalidated(tmp_path):
    repo = _repo(tmp_path / "ws")
    first = workspace.inspect_repo(repo)
    assert first is not None and not first.dirty
    (repo / "file.txt").write_text("v2\n")
    assert workspace.inspect_repo(repo) is first
    workspace.invalidate_repo_facts(repo)
    second = workspace.inspect_repo(repo)
    assert second is not None and second is not first and second.dirty


def test_quick_facts_are_shared_then_invalidated(tmp_path):
    repo = _repo(tmp_path / "wq")
    first = workspace.quick_repo_facts(repo)
    assert first is not None and not first["dirty"]
    (repo / "file.txt").write_text("v2\n")
    assert workspace.quick_repo_facts(repo) == first
    workspace.invalidate_repo_facts(repo)
    second = workspace.quick_repo_facts(repo)
    assert second is not None and second["dirty"]
    assert second["tracked_digest"] != first["tracked_digest"]


def test_git_spawns_skip_path_search():
    binary = workspace._git_binary()
    assert binary == "git" or Path(binary).is_absolute()


def test_store_fallback_follows_worktree_to_main(tmp_path):
    from mncs_env.store_backend import _store_package_for_checkout
    family = tmp_path / "family"
    main = family / "mncs-environment"
    main.mkdir(parents=True)
    package = family / "mncs-store" / "python" / "mncs_store"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    for args in (("init", "-q", "-b", "main"), ("add", "-A"),
                 ("-c", "user.name=t", "-c", "user.email=t@t",
                  "commit", "-qm", "init")):
        (main / "file.txt").write_text("v1\n")
        subprocess.run(["git", "-C", str(main), *args], check=True,
                       capture_output=True)
    linked_parent = tmp_path / "elsewhere"
    linked_parent.mkdir()
    subprocess.run(["git", "-C", str(main), "worktree", "add", "--detach",
                    str(linked_parent / "mncs-environment")],
                   check=True, capture_output=True)
    assert _store_package_for_checkout(main) == family / "mncs-store" / "python"
    assert (_store_package_for_checkout(linked_parent / "mncs-environment")
            == family / "mncs-store" / "python")
    assert _store_package_for_checkout(tmp_path / "nowhere") is None
