"""Tests for the top-level ``t3 cost`` CLI command (delegates to management command)."""

from unittest.mock import patch

import typer
from typer.testing import CliRunner

from teatree.cli.cost import cost

runner = CliRunner()

_app = typer.Typer()
_app.command()(cost)


class TestCostCommandDelegation:
    def test_delegates_to_management_command(self) -> None:
        with (
            patch("django.setup"),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(_app, [])
        assert result.exit_code == 0
        call_mock.assert_called_once_with("cost", json_output=False)

    def test_passes_json_flag(self) -> None:
        with (
            patch("django.setup"),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(_app, ["--json"])
        assert result.exit_code == 0
        call_mock.assert_called_once_with("cost", json_output=True)
