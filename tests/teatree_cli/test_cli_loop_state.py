"""``t3 loop pause/resume/disable/enable/status`` delegate to the mgmt command (#1913)."""

from unittest.mock import patch

from typer.testing import CliRunner

from teatree.cli.loop import loop_app

runner = CliRunner()


class TestLoopStateCli:
    def test_pause_delegates_with_name(self) -> None:
        with patch("django.setup"), patch("django.core.management.call_command") as call:
            result = runner.invoke(loop_app, ["pause", "review"])
        assert result.exit_code == 0, result.stdout
        call.assert_called_once_with("loop_state", "pause", "review")

    def test_resume_delegates_with_name(self) -> None:
        with patch("django.setup"), patch("django.core.management.call_command") as call:
            result = runner.invoke(loop_app, ["resume", "review"])
        assert result.exit_code == 0, result.stdout
        call.assert_called_once_with("loop_state", "resume", "review")

    def test_disable_delegates_with_name(self) -> None:
        with patch("django.setup"), patch("django.core.management.call_command") as call:
            result = runner.invoke(loop_app, ["disable", "ship"])
        assert result.exit_code == 0, result.stdout
        call.assert_called_once_with("loop_state", "disable", "ship")

    def test_enable_delegates_with_name(self) -> None:
        with patch("django.setup"), patch("django.core.management.call_command") as call:
            result = runner.invoke(loop_app, ["enable", "ship"])
        assert result.exit_code == 0, result.stdout
        call.assert_called_once_with("loop_state", "enable", "ship")

    def test_pause_passes_json_flag(self) -> None:
        with patch("django.setup"), patch("django.core.management.call_command") as call:
            result = runner.invoke(loop_app, ["pause", "review", "--json"])
        assert result.exit_code == 0, result.stdout
        call.assert_called_once_with("loop_state", "pause", "review", json_output=True)

    def test_loop_state_status_delegates(self) -> None:
        with patch("django.setup"), patch("django.core.management.call_command") as call:
            result = runner.invoke(loop_app, ["loop-state", "review"])
        assert result.exit_code == 0, result.stdout
        call.assert_called_once_with("loop_state", "status", "review")
