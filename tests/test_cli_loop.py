"""Tests for the ``t3 loop`` CLI commands."""

import datetime as dt
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from teatree.cli.loop import _cadence_for_loop_slot, _report_to_dict, loop_app
from teatree.loop.dispatch import DispatchAction
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.tick import TickReport

runner = CliRunner()


def _build_report(*, statusline_path: Path | None = None, errors: dict[str, str] | None = None) -> TickReport:
    return TickReport(
        started_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        signals=[ScanSignal(kind="my_pr.open", summary="x")],
        actions=[DispatchAction(kind="statusline", zone="in_flight", detail="x")],
        statusline_path=statusline_path,
        errors=errors or {},
    )


class TestReportToDict:
    def test_serialises_full_report(self, tmp_path: Path) -> None:
        report = _build_report(statusline_path=tmp_path / "sl.txt", errors={"my_prs": "boom"})

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


class TestTickCommand:
    def test_text_output(self, tmp_path: Path) -> None:
        statusline_file = tmp_path / "sl.txt"
        report = _build_report(statusline_path=statusline_file)
        with (
            patch("teatree.cli.loop.code_host_from_overlay", return_value=None),
            patch("teatree.cli.loop.messaging_from_overlay", return_value=None),
            patch("teatree.cli.loop.run_tick", return_value=report) as run_tick_mock,
        ):
            result = runner.invoke(loop_app, ["tick", "--statusline-file", str(statusline_file)])

        assert result.exit_code == 0
        assert "1 signal(s)" in result.stdout
        assert "statusline" in result.stdout
        run_tick_mock.assert_called_once()

    def test_calls_django_setup_before_scanning(self, tmp_path: Path) -> None:
        """Regression: scanners hit Django ORM, so django.setup() must run first."""
        report = _build_report(statusline_path=tmp_path / "sl.txt")
        with (
            patch("teatree.cli.loop.django.setup") as setup_mock,
            patch("teatree.cli.loop.code_host_from_overlay", return_value=None),
            patch("teatree.cli.loop.messaging_from_overlay", return_value=None),
            patch("teatree.cli.loop.run_tick", return_value=report),
        ):
            result = runner.invoke(loop_app, ["tick"])

        assert result.exit_code == 0
        setup_mock.assert_called_once()

    def test_text_output_includes_scanner_errors(self, tmp_path: Path) -> None:
        report = _build_report(errors={"my_prs": "RuntimeError: x"})
        with (
            patch("teatree.cli.loop.code_host_from_overlay", return_value=None),
            patch("teatree.cli.loop.messaging_from_overlay", return_value=None),
            patch("teatree.cli.loop.run_tick", return_value=report),
        ):
            result = runner.invoke(loop_app, ["tick"])

        assert result.exit_code == 0
        assert "WARN  my_prs" in result.stdout

    def test_json_output(self, tmp_path: Path) -> None:
        report = _build_report(statusline_path=tmp_path / "sl.txt")
        with (
            patch("teatree.cli.loop.code_host_from_overlay", return_value=None),
            patch("teatree.cli.loop.messaging_from_overlay", return_value=None),
            patch("teatree.cli.loop.run_tick", return_value=report),
        ):
            result = runner.invoke(loop_app, ["tick", "--json"])

        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["signal_count"] == 1
        assert payload["action_count"] == 1


class TestStatusCommand:
    def test_returns_one_when_no_statusline_file_yet(self, tmp_path: Path) -> None:
        with patch("teatree.cli.loop.default_path", return_value=tmp_path / "missing.txt"):
            result = runner.invoke(loop_app, ["status"])

        assert result.exit_code == 1
        assert "No statusline rendered yet" in result.stdout

    def test_emits_file_contents_when_present(self, tmp_path: Path) -> None:
        statusline_file = tmp_path / "sl.txt"
        statusline_file.write_text("running 0.0.1\n→ check 1\n", encoding="utf-8")
        with patch("teatree.cli.loop.default_path", return_value=statusline_file):
            result = runner.invoke(loop_app, ["status"])

        assert result.exit_code == 0
        assert "running 0.0.1" in result.stdout
        assert "check 1" in result.stdout


class TestCadenceParser:
    @pytest.mark.parametrize(
        ("env_value", "expected"),
        [
            ("720", "12m"),
            ("600", "10m"),
            ("90", "90s"),
            ("", "12m"),
            ("garbage", "12m"),
            ("30", "1m"),  # clamped to 60s minimum, formatted as 1m
        ],
    )
    def test_parses_t3_loop_cadence(self, env_value: str, expected: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_LOOP_CADENCE", env_value)
        assert _cadence_for_loop_slot() == expected

    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_LOOP_CADENCE", raising=False)
        assert _cadence_for_loop_slot() == "12m"


class TestStartCommand:
    def test_print_only_emits_slash_command(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_LOOP_CADENCE", "720")
        result = runner.invoke(loop_app, ["start", "--print-only"])

        assert result.exit_code == 0
        assert "/loop 12m !t3 loop tick" in result.stdout
        assert "T3_LOOP_CADENCE" in result.stdout

    def test_inside_claude_session_falls_back_to_print(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CLAUDECODE", "1")
        result = runner.invoke(loop_app, ["start"])

        assert result.exit_code == 0
        assert "/loop" in result.stdout

    def test_missing_claude_binary_exits_with_instructions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLAUDECODE", raising=False)
        with (
            patch("teatree.cli.loop._stdin_is_terminal", return_value=True),
            patch("teatree.cli.loop.shutil.which", return_value=None),
        ):
            result = runner.invoke(loop_app, ["start"])

        assert result.exit_code == 1
        assert "claude` not found" in result.stdout
        assert "/loop" in result.stdout

    def test_spawns_claude_with_register_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLAUDECODE", raising=False)
        monkeypatch.setenv("T3_LOOP_CADENCE", "600")
        with (
            patch("teatree.cli.loop._stdin_is_terminal", return_value=True),
            patch("teatree.cli.loop.shutil.which", return_value="/usr/bin/claude"),
            patch("teatree.cli.loop.os.execv") as execv_mock,
        ):
            runner.invoke(loop_app, ["start"])

        execv_mock.assert_called_once_with("/usr/bin/claude", ["/usr/bin/claude", "/loop 10m !t3 loop tick"])


class TestStopCommand:
    def test_stop_explains_unregister(self) -> None:
        result = runner.invoke(loop_app, ["stop"])

        assert result.exit_code == 0
        assert "/loop unregister t3-loop" in result.stdout
