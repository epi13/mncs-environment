"""Reconciler socket wiring: CLI flags reach the resident sources.

The reconciler already accepted commons/language sockets in
``reconcile_once`` and ``Daemon``; only the CLI never forwarded them,
so both sources always observed UNKNOWN in production. These tests pin
the flag/env threading without a live daemon.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mncs_env import cli as cli_module  # noqa: E402
from mncs_env import reconciler as reconciler_module  # noqa: E402


class ReconcilerSocketFlagTests(unittest.TestCase):
    def test_flags_parse_with_empty_defaults(self) -> None:
        parser = cli_module.build_parser()
        args = parser.parse_args(["reconciler"])
        self.assertIsNone(args.language_socket)
        self.assertIsNone(args.commons_socket)
        args = parser.parse_args(["reconciler", "--language-socket", "/tmp/l.sock",
                                  "--commons-socket", "/tmp/c.sock"])
        self.assertEqual(args.language_socket, "/tmp/l.sock")
        self.assertEqual(args.commons_socket, "/tmp/c.sock")

    def test_run_once_forwards_sockets_to_the_pass(self) -> None:
        parser = cli_module.build_parser()
        args = parser.parse_args(["--state-dir", "/tmp/state", "--persistence", "file",
                                  "reconciler", "--workspace", "/tmp/ws",
                                  "--language-socket", "/tmp/l.sock"])
        forwarded: dict = {}

        def fake_reconcile_once(session, store, workspace_root, *,
                                commons_socket=None, language_socket=None):
            forwarded["commons_socket"] = commons_socket
            forwarded["language_socket"] = language_socket
            return {"observations": []}

        with mock.patch.object(cli_module, "open_store") as open_store, \
                mock.patch.object(cli_module, "close_store"), \
                mock.patch.object(reconciler_module, "open_or_create_session",
                                  return_value=(mock.sentinel.session, True)), \
                mock.patch.object(reconciler_module, "reconcile_once",
                                  side_effect=fake_reconcile_once), \
                mock.patch.object(cli_module, "out"):
            open_store.return_value = mock.sentinel.store
            code = cli_module.cmd_reconciler(args)
        self.assertEqual(code, 0)
        self.assertEqual(forwarded["language_socket"], "/tmp/l.sock")
        self.assertIsNone(forwarded["commons_socket"])

    def test_environment_provide_socket_defaults(self) -> None:
        parser = cli_module.build_parser()
        args = parser.parse_args(["--state-dir", "/tmp/state", "--persistence", "file",
                                  "reconciler", "--workspace", "/tmp/ws"])
        forwarded: dict = {}

        def fake_reconcile_once(session, store, workspace_root, *,
                                commons_socket=None, language_socket=None):
            forwarded["commons_socket"] = commons_socket
            forwarded["language_socket"] = language_socket
            return {"observations": []}

        env = {"MNLS_SERVICE_SOCKET": "/tmp/env-l.sock",
               "MNCS_COMMONS_SOCKET": "/tmp/env-c.sock"}
        with mock.patch.object(cli_module, "open_store") as open_store, \
                mock.patch.object(cli_module, "close_store"), \
                mock.patch.object(reconciler_module, "open_or_create_session",
                                  return_value=(mock.sentinel.session, True)), \
                mock.patch.object(reconciler_module, "reconcile_once",
                                  side_effect=fake_reconcile_once), \
                mock.patch.object(cli_module, "out"), \
                mock.patch.dict("os.environ", env, clear=False):
            open_store.return_value = mock.sentinel.store
            code = cli_module.cmd_reconciler(args)
        self.assertEqual(code, 0)
        self.assertEqual(forwarded["language_socket"], "/tmp/env-l.sock")
        self.assertEqual(forwarded["commons_socket"], "/tmp/env-c.sock")

    def test_build_sources_binds_the_language_socket(self) -> None:
        sources = reconciler_module.build_sources(
            store=None, workspace_root="/tmp/ws", commons_socket=None,
            language_socket="/tmp/l.sock", own_session_prefix="ses_x")
        language = [source for source in sources
                    if source.name == "language-service"]
        self.assertEqual(len(language), 1)
        self.assertEqual(language[0].socket_path, "/tmp/l.sock")


if __name__ == "__main__":
    unittest.main(verbosity=2)
