"""``t3 loop directives`` delegates to the standing-directive mgmt commands (#4166)."""

from unittest.mock import patch

from typer.testing import CliRunner

from teatree.cli.loop import loop_app

runner = CliRunner()


class TestLoopDirectivesGroup:
    def test_registered_under_the_loop_group(self) -> None:
        registered = {group.name for group in loop_app.registered_groups}

        assert "directives" in registered

    def test_show_delegates_to_the_read_command(self) -> None:
        with patch("django.setup"), patch("django.core.management.call_command") as call:
            result = runner.invoke(loop_app, ["directives", "show"])

        assert result.exit_code == 0, result.stdout
        call.assert_called_once_with("loop_directives")

    def test_show_passes_the_json_flag(self) -> None:
        with patch("django.setup"), patch("django.core.management.call_command") as call:
            result = runner.invoke(loop_app, ["directives", "show", "--json"])

        assert result.exit_code == 0, result.stdout
        call.assert_called_once_with("loop_directives", json_output=True)

    def test_disable_passes_the_named_slots(self) -> None:
        with patch("django.setup"), patch("django.core.management.call_command") as call:
            result = runner.invoke(loop_app, ["directives", "disable", "standing-pr-board"])

        assert result.exit_code == 0, result.stdout
        call.assert_called_once_with("loop_directive_set", "disable", "standing-pr-board", all_slots=False)

    def test_disable_all_is_the_whole_feature_kill(self) -> None:
        with patch("django.setup"), patch("django.core.management.call_command") as call:
            result = runner.invoke(loop_app, ["directives", "disable", "--all"])

        assert result.exit_code == 0, result.stdout
        call.assert_called_once_with("loop_directive_set", "disable", all_slots=True)

    def test_enable_passes_the_named_slots(self) -> None:
        with patch("django.setup"), patch("django.core.management.call_command") as call:
            result = runner.invoke(loop_app, ["directives", "enable", "standing-pr-board"])

        assert result.exit_code == 0, result.stdout
        call.assert_called_once_with("loop_directive_set", "enable", "standing-pr-board", all_slots=False)
