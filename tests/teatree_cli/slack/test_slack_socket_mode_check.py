"""``_check_slack_socket_mode`` doctor renderer — surfacing-only, crash-proof (#106)."""

from unittest.mock import patch

import pytest

from teatree.cli.doctor.checks import _check_slack_socket_mode
from teatree.cli.slack.socket_doctor import Level, SocketModeFinding, SocketModeOutcome


class TestRenderer:
    def test_prints_each_finding_with_level_and_overlay(self, capsys: pytest.CaptureFixture[str]) -> None:
        outcome = SocketModeOutcome(
            findings=(
                SocketModeFinding("t3", Level.ACTION, "mint an app-level token"),
                SocketModeFinding("t3", Level.OK, "manifest current"),
            )
        )
        with patch("teatree.cli.slack.socket_doctor.check_slack_socket_mode", return_value=outcome):
            assert _check_slack_socket_mode() is True
        out = capsys.readouterr().out
        assert "ACTION" in out
        assert "mint an app-level token" in out
        assert "[t3]" in out

    def test_never_gates_exit_code_even_on_fail_findings(self, capsys: pytest.CaptureFixture[str]) -> None:
        outcome = SocketModeOutcome(findings=(SocketModeFinding("t3", Level.FAIL, "token lacks connections:write"),))
        with patch("teatree.cli.slack.socket_doctor.check_slack_socket_mode", return_value=outcome):
            assert _check_slack_socket_mode() is True
        assert "FAIL" in capsys.readouterr().out

    def test_crash_degrades_to_warn(self, capsys: pytest.CaptureFixture[str]) -> None:
        with patch("teatree.cli.slack.socket_doctor.check_slack_socket_mode", side_effect=RuntimeError("boom")):
            assert _check_slack_socket_mode() is True
        assert "WARN" in capsys.readouterr().out
