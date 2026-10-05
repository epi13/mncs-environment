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
