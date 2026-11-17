"""Focused tests for Environment's provider invocation CLI boundary."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from mncs_env import cli


def _args() -> SimpleNamespace:
    return SimpleNamespace(
        state_dir=Path("/state"),
        session="ses_invoke",
        persistence="store",
        capability="provider:receipt",
        argv=[],
        cwd=None,
        timeout=30,
        output_limit_bytes=32768,
    )


def test_invoke_fails_closed_when_success_output_was_truncated() -> None:
    session = mock.Mock()
    session.invoke.return_value = {
        "status": "ok", "returncode": 0, "stdout": "partial",
        "stderr": "", "truncated": True,
    }
    with (
        mock.patch.object(cli.sessions_module.Session, "resume",
                          return_value=session),
        mock.patch.object(cli, "out") as output,
        mock.patch("sys.stderr") as stderr,
    ):
        result = cli.cmd_invoke(_args())

    assert result == 4
    assert output.call_args.args[0]["truncated"] is True
    assert "truncated" in str(stderr.write.call_args_list)
    session.close.assert_called_once_with()


def test_invoke_closes_session_after_complete_success() -> None:
    session = mock.Mock()
    session.invoke.return_value = {
        "status": "ok", "returncode": 0, "stdout": "complete",
        "stderr": "", "truncated": False,
    }
    with (
        mock.patch.object(cli.sessions_module.Session, "resume",
                          return_value=session),
        mock.patch.object(cli, "out"),
        mock.patch("sys.stderr"),
    ):
        result = cli.cmd_invoke(_args())

    assert result == 0
    session.close.assert_called_once_with()


def test_invoke_reports_coordination_defer_as_handled_without_escalation() -> None:
    session = mock.Mock()
    session.invoke.return_value = {
        "status": "deferred-coordination", "returncode": None,
        "stdout": "", "stderr": "nothing executed", "truncated": False,
    }
    with (
        mock.patch.object(cli.sessions_module.Session, "resume",
                          return_value=session),
        mock.patch.object(cli, "out") as output,
        mock.patch("sys.stderr"),
    ):
        result = cli.cmd_invoke(_args())

    assert result == 0
    assert output.call_args.args[0]["status"] == "deferred-coordination"
    session.close.assert_called_once_with()


def test_invoke_uses_declared_timeout_unless_caller_overrides_it() -> None:
    args = cli.build_parser().parse_args(
        ["invoke", "ses_invoke", "provider:receipt"],
    )
    assert args.timeout is None
    explicit = cli.build_parser().parse_args(
        ["invoke", "ses_invoke", "provider:receipt", "--timeout", "37"],
    )
    assert explicit.timeout == 37

    session = mock.Mock()
    session.invoke.return_value = {
        "status": "ok", "returncode": 0, "stdout": "complete",
        "stderr": "", "truncated": False,
    }
    with (
        mock.patch.object(cli.sessions_module.Session, "resume",
                          return_value=session),
        mock.patch.object(cli, "out"),
        mock.patch("sys.stderr"),
    ):
        result = cli.cmd_invoke(args)

    assert result == 0
    assert session.invoke.call_args.kwargs["timeout_seconds"] is None
    assert session.invoke.call_args.kwargs["request_surface"] == (
        "mncs-env-invoke-cli"
    )


def test_run_bounded_closes_live_consumer_stdin(tmp_path: Path) -> None:
    """Capability invocations must not block on an open consumer stdin pipe."""
    import subprocess
    import sys

    source_root = Path(__file__).resolve().parents[1]
    script = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from mncs_env import capabilities
result = capabilities._run_bounded(
    {"binding_id": "probe", "capability": "test"},
    [sys.executable, "-c", "import sys; print(len(sys.stdin.read()))"],
    cwd=sys.argv[2],
    timeout_seconds=5,
    output_limit_bytes=4096,
)
print(json.dumps(result))
assert result["status"] == "ok", result
assert result["returncode"] == 0, result
assert result["stdout"].strip() == "0", result
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(source_root), str(tmp_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        # Keep parent stdin open; communicate() would close it and mask inheritance.
        process.wait(timeout=8)
        assert process.returncode == 0, process.stderr.read().decode()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdin.close()
        process.stdout.close()
        process.stderr.close()
