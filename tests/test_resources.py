"""Exercise actual kernel handles and bounded, read-only observation."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mncs_env import resources

pytestmark = pytest.mark.skipif(not Path('/proc/self/fd').exists(), reason='Linux procfs required')
ROOT = Path(__file__).resolve().parents[1]


def _fake_process(proc_root: Path, pid: int, cgroup: str) -> Path:
    root = proc_root / str(pid)
    root.mkdir(parents=True)
    stat_fields = ["S", *(["0"] * 18), "42"]
    (root / "stat").write_text(f"{pid} (resource-probe) {' '.join(stat_fields)}\n")
    (root / "status").write_text(
        "Name:\tresource-probe\nPPid:\t1\nVmHWM:\t16 kB\nVmRSS:\t8 kB\n"
    )
    (root / "limits").write_text("Max open files            64                  128                 files\n")
    (root / "fd").mkdir()
    (root / "cgroup").write_text(cgroup)
    return root


def test_cgroup_v2_limits_expose_usage_caps_and_derived_cpu_quota(tmp_path):
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    root = _fake_process(proc_root, 123, "0::/user.slice/session-7.scope\n")
    group = cgroup_root / "user.slice" / "session-7.scope"
    group.mkdir(parents=True)
    (group / "memory.current").write_text("4096\n")
    (group / "memory.max").write_text("max\n")
    (group / "cpu.max").write_text("25000 100000\n")
    (group / "pids.current").write_text("7\n")
    (group / "pids.max").write_text("32\n")

    row = resources.process(123, proc_root=proc_root, cgroup_root=cgroup_root)

    assert row["status"] == "observed"
    sample = row["cgroup_resource_limits"]
    assert sample["status"] == "observed"
    assert sample["measurement_basis"] == "read-only-cgroup-v2-files"
    assert sample["session_process_mapping"] == "not-asserted"
    assert sample["memory"] == {
        "current_bytes": 4096,
        "current_status": "observed",
        "max_bytes": None,
        "max_status": "unbounded",
    }
    assert sample["cpu"]["quota_us"] == 25000
    assert sample["cpu"]["period_us"] == 100000
    assert sample["cpu"]["quota_cores"] == 0.25
    assert sample["cpu"]["quota_cores_basis"] == (
        "derived-from-cpu.max-quota-divided-by-period"
    )
    assert sample["pids"] == {
        "current": 7,
        "current_status": "observed",
        "max": 32,
        "max_status": "observed",
    }
    assert root.exists()


def test_cgroup_v1_and_missing_mount_are_explicitly_unavailable(tmp_path):
    proc_root = tmp_path / "proc"
    root = _fake_process(proc_root, 123, "2:memory:/user.slice/session-7.scope\n")

    row = resources.process(123, proc_root=proc_root, cgroup_root=tmp_path / "missing")

    assert row["status"] == "observed"
    assert row["cgroup_resource_limits"]["status"] == "unavailable"
    assert row["cgroup_resource_limits"]["reason"] == (
        "unified-cgroup-v2-membership-not-found"
    )
    assert root.exists()


def test_cgroup_path_escape_and_oversized_values_fail_closed(tmp_path, monkeypatch):
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    escaped = _fake_process(proc_root, 123, "0::/../../etc\n")
    escaped_row = resources.process(123, proc_root=proc_root, cgroup_root=cgroup_root)
    assert escaped_row["cgroup_resource_limits"]["status"] == "unavailable"
    assert escaped_row["cgroup_resource_limits"]["reason"] == "invalid-cgroup-v2-path"
    assert escaped.exists()

    proc_root_link = tmp_path / "proc-link"
    _fake_process(proc_root_link, 125, "0::/user.slice/escape.scope\n")
    outside = tmp_path / "outside-cgroup"
    outside.mkdir()
    linked_group = cgroup_root / "user.slice" / "escape.scope"
    linked_group.parent.mkdir(parents=True)
    linked_group.symlink_to(outside, target_is_directory=True)
    linked_row = resources.process(125, proc_root=proc_root_link, cgroup_root=cgroup_root)
    assert linked_row["cgroup_resource_limits"]["status"] == "unavailable"
    assert linked_row["cgroup_resource_limits"]["reason"] == "cgroup-directory-outside-root"

    proc_root2 = tmp_path / "proc-2"
    _fake_process(proc_root2, 124, "0::/user.slice/session-7.scope\n")
    group = cgroup_root / "user.slice" / "session-7.scope"
    group.mkdir(parents=True)
    (group / "memory.current").write_text("1" * 70 + "\n")
    (group / "memory.max").write_text("max\n")
    (group / "cpu.max").write_text("max 100000\n")
    (group / "pids.current").write_text("7\n")
    (group / "pids.max").write_text("max\n")
    monkeypatch.setattr(resources, "MAX_CGROUP_VALUE_BYTES", 64)

    partial = resources.process(124, proc_root=proc_root2, cgroup_root=cgroup_root)
    sample = partial["cgroup_resource_limits"]
    assert sample["status"] == "partial"
    assert sample["memory"]["current_bytes"] is None
    assert sample["memory"]["current_status"] == "unavailable"
    assert sample["memory"]["max_status"] == "unbounded"

    proc_root3 = tmp_path / "proc-3"
    _fake_process(proc_root3, 126, f"0::/{'x' * 5000}\n")
    oversized_membership = resources.process(
        126, proc_root=proc_root3, cgroup_root=cgroup_root,
    )
    assert oversized_membership["status"] == "observed"
    assert oversized_membership["cgroup"] is None
    assert oversized_membership["cgroup_resource_limits"]["status"] == "unavailable"
    assert oversized_membership["cgroup_resource_limits"]["reason"] == (
        "resource-value-too-large"
    )


def test_repeated_observations_release_all_handles(tmp_path):
    baseline = len(os.listdir('/proc/self/fd'))
    with (tmp_path / 'deleted').open('w'):
        (tmp_path / 'deleted').unlink()
        for _ in range(100):
            row = resources.observe([os.getpid()])['processes'][0]
            assert row['status'] == 'observed'
            assert row['deleted_handles'] >= 1
            assert row['fd_classes']['file_or_device'] >= 1
            assert len(os.listdir('/proc/self/fd')) == baseline + 1
    assert len(os.listdir('/proc/self/fd')) == baseline


def test_real_low_limit_process_reports_headroom_and_pipe():
    code = '''import os, resource, sys
resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
fds = [os.open('/dev/null', os.O_RDONLY) for _ in range(24)]
print('ready', flush=True)
sys.stdin.readline()
for fd in fds: os.close(fd)
print('closed', flush=True)
sys.stdin.readline()
'''
    with subprocess.Popen([sys.executable, '-c', code], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, text=True) as child:
        try:
            assert child.stdout.readline().strip() == 'ready'
            row = resources.observe([child.pid])['processes'][0]
            assert row['fd_limit_soft'] == row['fd_limit_hard'] == 64
            assert row['fd_count'] == 27
            assert row['fd_headroom'] == 37
            assert row['fd_classes']['pipe'] >= 2
            child.stdin.write('\n'); child.stdin.flush()
            assert child.stdout.readline().strip() == 'closed'
            after = resources.observe([child.pid])['processes'][0]
            assert after['fd_count'] == 3
            child.stdin.write('\n'); child.stdin.flush()
            assert child.wait(timeout=5) == 0
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)


def test_explicit_selection_and_scan_are_bounded(monkeypatch):
    with pytest.raises(ValueError):
        resources.observe(list(range(1, 10)))
    with pytest.raises(ValueError):
        resources.observe([0])
    monkeypatch.setattr(resources, 'MAX_DESCRIPTORS', 2)
    row = resources.observe([os.getpid()])['processes'][0]
    assert row['fd_scan_truncated']
    assert row['fd_count'] is None
    assert row['fd_count_observed'] == 2
    assert row['fd_headroom'] is None
    assert row['fd_details_partial']


def test_unavailable_process_is_unknown(tmp_path):
    row = resources.observe([123], proc_root=tmp_path)
    assert row['processes'][0]['status'] == 'unknown'
    assert row['system']['status'] == 'unknown'


def test_cli_does_not_open_or_create_store(tmp_path):
    state = tmp_path / 'must-not-exist'
    result = subprocess.run([sys.executable, str(ROOT / 'scripts/mncs-env'),
                             '--state-dir', str(state), 'resources', '--pid', str(os.getpid())],
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload['processes'][0]['pid'] == os.getpid()
    row = payload['processes'][0]
    assert row['rss_hwm_bytes'] >= row['rss_bytes']
    assert row['executable']
    assert row['executable_sha256'].startswith('sha256:')
    assert payload['session_process_mapping'] == 'not-asserted'
    assert not state.exists()


def test_inotify_entries_are_observed_and_bounded(tmp_path, monkeypatch):
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    descriptor = libc.inotify_init1(os.O_CLOEXEC)
    assert descriptor >= 0
    try:
        assert libc.inotify_add_watch(descriptor, os.fsencode(tmp_path), 0x100) >= 0
        child = tmp_path / 'child'; child.mkdir()
        assert libc.inotify_add_watch(descriptor, os.fsencode(child), 0x100) >= 0
        row = resources.observe([os.getpid()])['processes'][0]
        assert row['inotify_watch_entries'] >= 2
        monkeypatch.setattr(resources, 'MAX_WATCH_ENTRIES', 1)
        row = resources.observe([os.getpid()])['processes'][0]
        assert row['inotify_watch_entries'] == 1
        assert row['watch_scan_truncated']
        assert row['fd_details_partial']
    finally:
        os.close(descriptor)
