"""``t3 loop pause/resume/disable/enable/status`` delegate to the mgmt command (#1913)."""

from unittest.mock import patch

from django.test import TestCase
from typer.testing import CliRunner

from teatree.cli.loop import loop_app
from teatree.core.models import LoopState, LoopStatus

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


class TestLoopStateCommandIsReadOnly(TestCase):
    """``t3 loop loop-state <name>`` is a strict READ, end-to-end (no mock).

    It must report the durable status WITHOUT mutating it, and its output must
    read as a status — not the mutation-verb "is now <status>" phrasing that an
    operator can mistake for a pause/enable that just happened.
    """

    def test_loop_state_does_not_mutate_an_enabled_loop(self) -> None:
        result = runner.invoke(loop_app, ["loop-state", "review"])
        assert result.exit_code == 0, result.stdout
        assert LoopState.objects.status_of("review") is LoopStatus.ENABLED
        assert not LoopState.objects.filter(name="review").exists()

    def test_loop_state_output_reads_as_a_status(self) -> None:
        result = runner.invoke(loop_app, ["loop-state", "review"])
        assert "is now" not in result.stdout
        assert "ENABLED" in result.stdout
