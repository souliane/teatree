"""``t3 loop list`` CLI wrapper delegates to the ``loop_list`` mgmt command (#1744)."""

from unittest.mock import patch

from typer.testing import CliRunner

from teatree.cli.loop import loop_app

runner = CliRunner()


class TestLoopListCommand:
    def test_delegates_to_management_command(self) -> None:
        with patch("django.setup"), patch("django.core.management.call_command") as call:
            result = runner.invoke(loop_app, ["list"])

        assert result.exit_code == 0, result.stdout
        call.assert_called_once_with("loop_list")

    def test_passes_json_flag(self) -> None:
        with patch("django.setup"), patch("django.core.management.call_command") as call:
            result = runner.invoke(loop_app, ["list", "--json"])

        assert result.exit_code == 0, result.stdout
        call.assert_called_once_with("loop_list", json_output=True)

    def test_passes_all_flag(self) -> None:
        with patch("django.setup"), patch("django.core.management.call_command") as call:
            result = runner.invoke(loop_app, ["list", "--all"])

        assert result.exit_code == 0, result.stdout
        call.assert_called_once_with("loop_list", show_all=True)

    def test_passes_all_and_json_flags(self) -> None:
        with patch("django.setup"), patch("django.core.management.call_command") as call:
            result = runner.invoke(loop_app, ["list", "--all", "--json"])

        assert result.exit_code == 0, result.stdout
        call.assert_called_once_with("loop_list", json_output=True, show_all=True)
