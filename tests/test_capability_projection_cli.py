from __future__ import annotations

import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from unittest.mock import patch

from mncs_env import cli


class EnvironmentalCapabilityProjectionCliTests(unittest.TestCase):
    class Session:
        def close(self) -> None:
            pass

    def _run_projection(self, arguments: list[str]):
        args = cli.build_parser().parse_args(arguments)
        session = self.Session()
        with (
            patch.object(cli, "_open", return_value=session),
            patch.object(cli.projections_module, "interpretation", return_value={}) as project,
            patch.object(cli, "out"),
        ):
            self.assertEqual(cli.cmd_projections(args), 0)
        return project.call_args

    def test_environmental_capability_uses_transport_safe_default_pages(self) -> None:
        call = self._run_projection([
            "projections", "ses_example", "--interpret", "environmental-capability",
        ])

        self.assertEqual(call.kwargs["evidence_limit"], 2)
        self.assertEqual(call.kwargs["session_event_limit"], 4)

    def test_environmental_capability_preserves_explicit_larger_pages(self) -> None:
        call = self._run_projection([
            "projections", "ses_example", "--interpret", "environmental-capability",
            "--evidence-limit", "12", "--session-event-limit", "64",
        ])

        self.assertEqual(call.kwargs["evidence_limit"], 12)
        self.assertEqual(call.kwargs["session_event_limit"], 64)

    def test_environmental_capability_output_stays_within_transport_byte_limit(self) -> None:
        args = cli.build_parser().parse_args([
            "projections", "ses_example", "--interpret", "environmental-capability",
        ])
        payload = {"items": ["x"] * 10_000}
        output = StringIO()
        with (
            patch.object(cli, "_open", return_value=self.Session()),
            patch.object(cli.projections_module, "interpretation", return_value=payload),
            redirect_stdout(output),
        ):
            self.assertEqual(cli.cmd_projections(args), 0)

        encoded = output.getvalue().encode("utf-8")
        self.assertLessEqual(
            len(encoded), cli.capability_intelligence_module.MAX_RESULT_BYTES + 1,
        )
        self.assertEqual(json.loads(output.getvalue()), payload)
        self.assertNotIn("\n  ", output.getvalue())

    def test_environmental_capability_output_refuses_oversized_compact_payload(self) -> None:
        args = cli.build_parser().parse_args([
            "projections", "ses_example", "--interpret", "environmental-capability",
        ])
        output = StringIO()
        errors = StringIO()
        with (
            patch.object(cli, "_open", return_value=self.Session()),
            patch.object(
                cli.projections_module,
                "interpretation",
                return_value={"payload": "x" * cli.capability_intelligence_module.MAX_RESULT_BYTES},
            ),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            self.assertEqual(cli.cmd_projections(args), 2)

        self.assertEqual(output.getvalue(), "")
        self.assertIn("output limit", json.loads(errors.getvalue())["error"])

    def test_default_page_options_remain_valid_for_ambient_projection_reads(self) -> None:
        args = cli.build_parser().parse_args([
            "projections", "ses_example", "--evidence",
        ])
        session = self.Session()

        self.assertIsNone(args.evidence_limit)
        self.assertIsNone(args.session_event_limit)
        with (
            patch.object(cli, "_open", return_value=session),
            patch.object(cli.projections_module, "read_evidence", return_value={}) as read,
            patch.object(cli, "out"),
        ):
            self.assertEqual(cli.cmd_projections(args), 0)
        read.assert_called_once_with(session)
