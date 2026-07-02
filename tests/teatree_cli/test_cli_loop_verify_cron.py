"""``t3 loop verify-cron <name>`` delegates to the mgmt command (#1192)."""

from unittest.mock import patch

import typer
from typer.testing import CliRunner

from teatree.cli.loop import loop_app
from teatree.cli.loop_verify_cron import register

runner = CliRunner()


class TestRegister:
    def test_attaches_verify_cron_command_onto_the_given_app(self) -> None:
        app = typer.Typer()
        register(app)
        names = {command.name for command in app.registered_commands}
        assert "verify-cron" in names


class TestLoopVerifyCronCli:
    def test_delegates_with_name_and_default_stdin(self) -> None:
        with patch("django.setup"), patch("django.core.management.call_command") as call:
            result = runner.invoke(loop_app, ["verify-cron", "review"])
        assert result.exit_code == 0, result.stdout
        call.assert_called_once_with("loop_verify_cron", "review", cron_list_json="-")

    def test_passes_cron_list_json_path(self) -> None:
        with patch("django.setup"), patch("django.core.management.call_command") as call:
            result = runner.invoke(loop_app, ["verify-cron", "ship", "--cron-list-json", "/tmp/crons.json"])
        assert result.exit_code == 0, result.stdout
        call.assert_called_once_with("loop_verify_cron", "ship", cron_list_json="/tmp/crons.json")

    def test_management_command_system_exit_becomes_cli_exit_code(self) -> None:
        with (
            patch("django.setup"),
            patch("django.core.management.call_command", side_effect=SystemExit(1)),
        ):
            result = runner.invoke(loop_app, ["verify-cron", "ship"])
        assert result.exit_code == 1
