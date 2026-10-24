"""Tests for the top-level ``t3 cost`` CLI command (delegates to management command)."""

from unittest.mock import patch

import typer
from typer.testing import CliRunner

from teatree.cli.cost import cost, cost_profile

runner = CliRunner()

_app = typer.Typer()
_app.command()(cost)

_profile_app = typer.Typer()
_profile_app.command()(cost_profile)


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


class TestCostProfileCommandDelegation:
    def test_delegates_to_management_command(self) -> None:
        with (
            patch("django.setup"),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(_profile_app, [])
        assert result.exit_code == 0
        call_mock.assert_called_once_with("cost_profile", json_output=False, since="")

    def test_passes_json_and_since_flags(self) -> None:
        with (
            patch("django.setup"),
            patch("django.core.management.call_command") as call_mock,
        ):
            result = runner.invoke(_profile_app, ["--json", "--since", "2026-10-01"])
        assert result.exit_code == 0
        call_mock.assert_called_once_with("cost_profile", json_output=True, since="2026-10-01")
