"""``t3 loop directives`` delegates to the ``loop_directives`` mgmt command (#4166)."""

from unittest.mock import patch

from typer.testing import CliRunner

from teatree.cli.loop import loop_app
from teatree.cli.loop.directives import directives_command

runner = CliRunner()


class TestLoopDirectivesCommand:
    def test_registered_under_the_loop_group(self) -> None:
        registered = {command.callback for command in loop_app.registered_commands}

        assert directives_command in registered

    def test_delegates_to_management_command(self) -> None:
        with patch("django.setup"), patch("django.core.management.call_command") as call:
            result = runner.invoke(loop_app, ["directives"])

        assert result.exit_code == 0, result.stdout
        call.assert_called_once_with("loop_directives")

    def test_passes_json_flag(self) -> None:
        with patch("django.setup"), patch("django.core.management.call_command") as call:
            result = runner.invoke(loop_app, ["directives", "--json"])

        assert result.exit_code == 0, result.stdout
        call.assert_called_once_with("loop_directives", json_output=True)
