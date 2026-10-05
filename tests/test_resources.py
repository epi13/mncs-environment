"""Exercise actual kernel handles and bounded, read-only observation."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from mncs_env import resources

pytestmark = pytest.mark.skipif(not Path('/proc/self/fd').exists(), reason='Linux procfs required')
ROOT = Path(__file__).resolve().parents[1]


def test_repeated_observations_release_all_handles(tmp_path):
    baseline = len(os.listdir('/proc/self/fd'))
    with (tmp_path / 'deleted').open('w') as handle:
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
                            capture_output=True, text=True, timeout=10)
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
