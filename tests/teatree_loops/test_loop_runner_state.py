"""The three-valued kill-switch reader distinguishes OFF from a read FAILURE (F7).

``read_loop_runner_state`` maps a successful read to ON/OFF and a raising read to
UNREADABLE (never silently OFF). ``_loop_runner_enabled`` keeps its fail-safe boolean
contract for the chain: anything but ON is False.
"""

from unittest.mock import patch

import django.test

from teatree.loops import timer_chains
from teatree.loops.timer_chains import LoopRunnerState, _loop_runner_enabled, read_loop_runner_state


class TestReadLoopRunnerState(django.test.SimpleTestCase):
    def test_on_when_enabled(self) -> None:
        with patch("teatree.config.get_effective_settings") as settings:
            settings.return_value.loop_runner_enabled = True
            assert read_loop_runner_state() is LoopRunnerState.ON

    def test_off_when_disabled(self) -> None:
        with patch("teatree.config.get_effective_settings") as settings:
            settings.return_value.loop_runner_enabled = False
            assert read_loop_runner_state() is LoopRunnerState.OFF

    def test_unreadable_on_read_failure(self) -> None:
        # A raising read is UNREADABLE — the "cannot confirm" case, NOT collapsed to OFF.
        with patch("teatree.config.get_effective_settings", side_effect=RuntimeError("db blip")):
            assert read_loop_runner_state() is LoopRunnerState.UNREADABLE

    def test_read_failure_logs_at_warning_not_debug(self) -> None:
        logger_name = timer_chains.logger.name
        with (
            patch("teatree.config.get_effective_settings", side_effect=RuntimeError("db blip")),
            self.assertLogs(logger_name, level="WARNING") as captured,
        ):
            read_loop_runner_state()
        assert any("kill-switch state" in line for line in captured.output)


class TestLoopRunnerEnabledFailSafe(django.test.SimpleTestCase):
    def test_true_only_when_on(self) -> None:
        with patch("teatree.config.get_effective_settings") as settings:
            settings.return_value.loop_runner_enabled = True
            assert _loop_runner_enabled() is True

    def test_unreadable_maps_to_false_for_the_chain(self) -> None:
        with patch("teatree.config.get_effective_settings", side_effect=RuntimeError("db blip")):
            assert _loop_runner_enabled() is False
