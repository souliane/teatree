"""Tests for the ``t3 loop`` CLI commands (non-Django: start, stop, status, cadence).

Tick-specific tests live in ``teatree_core/test_loop_tick_command.py`` since
tick is now a Django management command.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from teatree.cli.loop import _cadence_for_loop_slot, loop_app
from teatree.cli.loop_slack_answer import _slack_answer_cadence_for_loop_slot

runner = CliRunner()


class TestTickCommandDelegation:
    def test_delegates_to_management_command(self, tmp_path: Path) -> None:
        with (
            patch("django.setup"),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(loop_app, ["tick", "--statusline-file", str(tmp_path / "sl.txt")])

        assert result.exit_code == 0
        call_mock.assert_called_once_with("loop_tick", statusline_file=str(tmp_path / "sl.txt"))

    def test_passes_overlay_and_json_flags(self) -> None:
        with (
            patch("django.setup"),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(loop_app, ["tick", "--overlay", "myoverlay", "--json"])

        assert result.exit_code == 0
        call_mock.assert_called_once_with("loop_tick", overlay="myoverlay", json_output=True)

    def test_no_args_calls_with_empty_kwargs(self) -> None:
        with (
            patch("django.setup"),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(loop_app, ["tick"])

        assert result.exit_code == 0
        call_mock.assert_called_once_with("loop_tick")


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
        # #786 WS3/WS1: the registration prompt drives the tick + atomic
        # claim-then-spawn flow (claim-next), NOT the retired
        # pending-spawn/spawn-claim race.
        assert "/loop 12m " in result.stdout
        assert "t3 loop tick" in result.stdout
        assert "t3 loop claim-next" in result.stdout
        assert "spawn-claim" not in result.stdout
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

        assert execv_mock.call_count == 1
        argv = execv_mock.call_args.args[1]
        assert argv[0] == "/usr/bin/claude"
        assert argv[1].startswith("/loop 10m ")
        assert "t3 loop tick" in argv[1]
        assert "t3 loop claim-next" in argv[1]


class TestStopCommand:
    def test_stop_explains_unregister(self) -> None:
        result = runner.invoke(loop_app, ["stop"])

        assert result.exit_code == 0
        assert "/loop unregister t3-loop" in result.stdout


class TestSlackAnswerCadenceParser:
    @pytest.mark.parametrize(
        ("env_value", "expected"),
        [
            ("20", "20s"),
            ("60", "1m"),
            ("", "20s"),
            ("garbage", "20s"),
            ("5", "15s"),  # clamped to 15s floor
            ("15", "15s"),
        ],
    )
    def test_parses_t3_slack_answer_cadence(
        self, env_value: str, expected: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("T3_SLACK_ANSWER_CADENCE", env_value)
        assert _slack_answer_cadence_for_loop_slot() == expected

    def test_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_SLACK_ANSWER_CADENCE", raising=False)
        assert _slack_answer_cadence_for_loop_slot() == "20s"


class TestSlackAnswerStartCommand:
    def test_start_emits_third_slot_line(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("T3_SLACK_ANSWER_CADENCE", "20")
        result = runner.invoke(loop_app, ["slack-answer", "start"])

        assert result.exit_code == 0
        assert "/loop 20s Run `t3 loop slack-answer run`." in result.stdout
        assert "T3_SLACK_ANSWER_CADENCE" in result.stdout

    def test_run_delegates_to_management_command(self) -> None:
        with patch("django.core.management.call_command") as call:
            result = runner.invoke(loop_app, ["slack-answer", "run", "--json"])

        assert result.exit_code == 0
        call.assert_called_once_with("loop_slack_answer", json_output=True)
