"""Tests for the top-level ``t3 speak`` CLI command (delegates to management command)."""

from unittest.mock import patch

import typer
from typer.testing import CliRunner

from teatree.cli.speak import speak

runner = CliRunner()

_app = typer.Typer()
_app.command()(speak)


class TestSpeakCommandDelegation:
    def test_delegates_to_management_command(self) -> None:
        with (
            patch("django.setup"),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(_app, ["tests are green"])
        assert result.exit_code == 0
        call_mock.assert_called_once_with("speak", "tests are green", overlay="")

    def test_passes_overlay_flag(self) -> None:
        with (
            patch("django.setup"),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(_app, ["hi", "--overlay", "teatree"])
        assert result.exit_code == 0
        call_mock.assert_called_once_with("speak", "hi", overlay="teatree")
