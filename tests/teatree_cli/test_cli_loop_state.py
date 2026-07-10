"""``t3 loop pause/resume/disable/enable/status`` delegate to the mgmt command (#1913)."""

from unittest.mock import patch

from django.test import TestCase
from typer.testing import CliRunner

from teatree.cli.loop import loop_app
from teatree.core.models import Loop, LoopState, LoopStatus

runner = CliRunner()


def _seed_loop(name: str) -> Loop:
    """A real ``Loop`` row so the end-to-end paths have a loop to control.

    Idempotent (``update_or_create``): the initial migration already seeds the
    default loops, so a plain ``create`` would collide; and under
    ``--no-migrations`` the seed is absent, so the row is created here instead.
    """
    return Loop.objects.update_or_create(
        name=name, defaults={"script": f"src/teatree/loops/{name}/loop.py", "delay_seconds": 60}
    )[0]


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

    def setUp(self) -> None:
        _seed_loop("review")

    def test_loop_state_does_not_mutate_an_enabled_loop(self) -> None:
        result = runner.invoke(loop_app, ["loop-state", "review"])
        assert result.exit_code == 0, result.stdout
        assert LoopState.objects.status_of("review") is LoopStatus.ENABLED
        assert not LoopState.objects.filter(name="review").exists()

    def test_loop_state_output_reads_as_a_status(self) -> None:
        result = runner.invoke(loop_app, ["loop-state", "review"])
        assert "is now" not in result.stdout
        assert "ENABLED" in result.stdout


class TestUnknownLoopNameRefused(TestCase):
    """#3117: an unknown loop NAME is refused end-to-end — never silently paused, never reported ENABLED.

    A typo in a safety command (``t3 loop pause <typo>``) used to print
    ``OK … is now paused`` and pause nothing, and ``t3 loop loop-state <typo>``
    used to print ``ENABLED`` for a loop that does not exist. Both now refuse
    with a non-zero exit before touching ``LoopState``.
    """

    _BOGUS = "totally_bogus_loop_xyz"

    def test_pause_unknown_name_exits_nonzero_and_writes_no_state(self) -> None:
        result = runner.invoke(loop_app, ["pause", self._BOGUS])
        assert result.exit_code != 0, result.stdout
        assert not LoopState.objects.filter(name=self._BOGUS).exists()

    def test_loop_state_unknown_name_refuses_and_never_reports_enabled(self) -> None:
        result = runner.invoke(loop_app, ["loop-state", self._BOGUS])
        assert result.exit_code != 0, result.stdout
        assert "ENABLED" not in result.stdout

    def test_refusal_names_the_loop_and_suggests_loops_list(self) -> None:
        result = runner.invoke(loop_app, ["pause", self._BOGUS])
        assert self._BOGUS in result.stdout
        assert "t3 loops list" in result.stdout
