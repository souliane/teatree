"""The allowlisted command runner: allowlist gate, argv build, timeout handling (#3162)."""

from subprocess import CompletedProcess, TimeoutExpired
from unittest.mock import patch

import pytest

from teatree.dash import commands


def test_unknown_key_is_refused() -> None:
    with pytest.raises(commands.CommandNotAllowedError):
        commands.run_allowlisted("rm-rf")


def test_loop_tick_requires_a_loop_name() -> None:
    with pytest.raises(commands.CommandNotAllowedError):
        commands.run_allowlisted("loop-tick", loop_name="")


def test_allowlisted_command_captures_output() -> None:
    completed = CompletedProcess(args=["t3", "doctor", "check"], returncode=0, stdout="ok\n", stderr="")
    with patch.object(commands, "run_allowed_to_fail", return_value=completed) as run:
        result = commands.run_allowlisted("doctor")
    assert run.call_args.args[0] == ["t3", "doctor", "check"]
    assert result.exit_code == 0
    assert result.output == "ok\n"
    assert result.timed_out is False


def test_loop_tick_appends_the_loop_name() -> None:
    completed = CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch.object(commands, "run_allowed_to_fail", return_value=completed) as run:
        commands.run_allowlisted("loop-tick", loop_name="review")
    assert run.call_args.args[0] == ["t3", "loops", "tick", "--loop", "review"]


def test_timeout_returns_flagged_result_not_raise() -> None:
    with patch.object(
        commands, "run_allowed_to_fail", side_effect=TimeoutExpired(cmd=["t3"], timeout=1, output="partial")
    ):
        result = commands.run_allowlisted("doctor")
    assert result.timed_out is True
    assert "partial" in result.output


def test_command_buttons_lists_the_allowlist() -> None:
    keys = {spec.key for spec in commands.command_buttons()}
    assert "doctor" in keys
    assert "loop-tick" in keys
