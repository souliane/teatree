"""Tests for the top-level ``t3 speak-dm`` CLI command (delegates to the management command, #2171)."""

from unittest.mock import patch

import typer
from typer.testing import CliRunner

from teatree.cli.speak_dm import speak_dm

runner = CliRunner()

_app = typer.Typer()
_app.command(name="speak-dm")(speak_dm)


class TestSpeakDmCommandDelegation:
    def test_delegates_to_management_command(self) -> None:
        with (
            patch("django.setup"),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(_app, ["--channel", "D-USER", "--text", "Ship it?"])
        assert result.exit_code == 0
        call_mock.assert_called_once_with("speak_dm", "D-USER", "Ship it?", thread_ts="", overlay="")

    def test_passes_thread_and_overlay(self) -> None:
        with (
            patch("django.setup"),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(
                _app, ["--channel", "D", "--text", "hi", "--thread-ts", "1700.1", "--overlay", "teatree"]
            )
        assert result.exit_code == 0
        call_mock.assert_called_once_with("speak_dm", "D", "hi", thread_ts="1700.1", overlay="teatree")
