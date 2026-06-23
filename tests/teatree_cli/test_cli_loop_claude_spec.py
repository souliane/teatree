"""``t3 loop claude-spec <name>`` delegates to the mgmt command (#2650)."""

from unittest.mock import patch

from typer.testing import CliRunner

from teatree.cli.loop import loop_app

runner = CliRunner()


class TestLoopClaudeSpecCli:
    def test_delegates_with_name(self) -> None:
        with patch("django.setup"), patch("django.core.management.call_command") as call:
            result = runner.invoke(loop_app, ["claude-spec", "review"])
        assert result.exit_code == 0, result.stdout
        call.assert_called_once_with("loop_claude_spec", "review")

    def test_passes_json_flag(self) -> None:
        with patch("django.setup"), patch("django.core.management.call_command") as call:
            result = runner.invoke(loop_app, ["claude-spec", "ship", "--json"])
        assert result.exit_code == 0, result.stdout
        call.assert_called_once_with("loop_claude_spec", "ship", json_output=True)
