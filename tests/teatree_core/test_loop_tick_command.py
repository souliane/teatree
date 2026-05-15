"""Tests for the ``loop_tick`` Django management command."""

import datetime as dt
import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from teatree.core.management.commands.loop_tick import _report_to_dict
from teatree.loop.dispatch import DispatchAction
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.tick import TickReport


def _build_report(*, statusline_path: Path | None = None, errors: dict[str, str] | None = None) -> TickReport:
    return TickReport(
        started_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        signals=[ScanSignal(kind="my_pr.open", summary="x")],
        actions=[DispatchAction(kind="statusline", zone="in_flight", detail="x")],
        statusline_path=statusline_path,
        errors=errors or {},
    )


class TestReportToDict(TestCase):
    def test_serialises_full_report(self) -> None:
        report = _build_report(statusline_path=Path("/tmp/sl.txt"), errors={"my_prs": "boom"})

        data = _report_to_dict(report)

        assert data["started_at"] == "2026-01-01T00:00:00+00:00"
        assert data["signal_count"] == 1
        assert data["action_count"] == 1
        assert data["statusline_path"].endswith("sl.txt")
        assert data["errors"] == {"my_prs": "boom"}
        assert data["actions"][0]["zone"] == "in_flight"

    def test_empty_statusline_path_serialises_to_empty_string(self) -> None:
        report = _build_report(statusline_path=None)

        data = _report_to_dict(report)

        assert data["statusline_path"] == ""


class TestLoopTickCommand(TestCase):
    def test_text_output(self) -> None:
        report = _build_report(statusline_path=Path("/tmp/sl.txt"))
        stdout = StringIO()
        with (
            patch("teatree.core.backend_factory.iter_overlay_backends", return_value=[]),
            patch("teatree.loop.tick.run_tick", return_value=report),
        ):
            call_command("loop_tick", stdout=stdout)

        output = stdout.getvalue()
        assert "1 signal(s)" in output
        assert "statusline" in output

    def test_text_output_includes_scanner_errors(self) -> None:
        report = _build_report(errors={"my_prs": "RuntimeError: x"})
        stdout = StringIO()
        with (
            patch("teatree.core.backend_factory.iter_overlay_backends", return_value=[]),
            patch("teatree.loop.tick.run_tick", return_value=report),
        ):
            call_command("loop_tick", stdout=stdout)

        output = stdout.getvalue()
        assert "WARN  my_prs" in output

    def test_json_output(self) -> None:
        report = _build_report(statusline_path=Path("/tmp/sl.txt"))
        stdout = StringIO()
        with (
            patch("teatree.core.backend_factory.iter_overlay_backends", return_value=[]),
            patch("teatree.loop.tick.run_tick", return_value=report),
        ):
            call_command("loop_tick", "--json", stdout=stdout)

        payload = json.loads(stdout.getvalue())
        assert payload["signal_count"] == 1
        assert payload["action_count"] == 1

    def test_overlay_option_uses_single_overlay_path(self) -> None:
        report = _build_report()
        stdout = StringIO()
        with (
            patch("teatree.core.backend_factory.code_host_from_overlay", return_value=None) as host_mock,
            patch("teatree.core.backend_factory.messaging_from_overlay", return_value=None),
            patch("teatree.loop.tick.run_tick", return_value=report),
        ):
            call_command("loop_tick", "--overlay", "myoverlay", stdout=stdout)

        host_mock.assert_called_once()

    def test_skips_tick_when_singleton_lock_held(self) -> None:
        """Holding the real `loop-tick` lock makes the command skip the tick.

        No mock of the lock itself — a genuine concurrent holder is
        simulated by acquiring `singleton("loop-tick")` in this process
        first, exercising the exact production refusal path.
        """
        from teatree.utils.singleton import singleton  # noqa: PLC0415

        stdout = StringIO()
        with (
            singleton("loop-tick"),
            patch("teatree.loop.tick.run_tick") as run_tick_mock,
        ):
            call_command("loop_tick", stdout=stdout)

        run_tick_mock.assert_not_called()
        output = stdout.getvalue()
        assert "SKIP" in output
        assert "another tick is already running" in output

    def test_skip_emits_json_when_requested(self) -> None:
        from teatree.utils.singleton import singleton  # noqa: PLC0415

        stdout = StringIO()
        with singleton("loop-tick"):
            call_command("loop_tick", "--json", stdout=stdout)

        payload = json.loads(stdout.getvalue())
        assert payload == {"skipped": "another tick is already running"}
