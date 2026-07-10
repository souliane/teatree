"""``t3 loops`` CLI group — delegates to the ``loops_list`` mgmt command (#1796)."""

from unittest.mock import patch

from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli.loops import loops_app

runner = CliRunner()


class TestLoopsListCommand:
    def test_delegates_to_management_command(self) -> None:
        with patch("django.setup"), patch("django.core.management.call_command") as call:
            result = runner.invoke(loops_app, ["list"])

        assert result.exit_code == 0, result.stdout
        call.assert_called_once_with("loops_list")

    def test_passes_json_flag(self) -> None:
        with patch("django.setup"), patch("django.core.management.call_command") as call:
            result = runner.invoke(loops_app, ["list", "--json"])

        assert result.exit_code == 0, result.stdout
        call.assert_called_once_with("loops_list", json_output=True)


class TestLoopsGroupRegistered:
    def test_loops_group_wired_onto_root_app(self) -> None:
        names = {group.name for group in app.registered_groups}
        assert "loops" in names


class TestLoopsTick:
    def test_per_loop_tick_delegates_with_the_loop_name(self) -> None:
        with patch("django.setup"), patch("django.core.management.call_command") as call:
            result = runner.invoke(loops_app, ["tick", "--loop", "inbox"])
        assert result.exit_code == 0, result.stdout
        call.assert_called_once_with("loops_tick", loop="inbox")

    def test_per_loop_tick_passes_overlay_and_json(self) -> None:
        with patch("django.setup"), patch("django.core.management.call_command") as call:
            runner.invoke(loops_app, ["tick", "--loop", "inbox", "--overlay", "acme", "--json"])
        call.assert_called_once_with("loops_tick", loop="inbox", overlay="acme", json_output=True)

    def test_run_command_is_gone(self) -> None:
        # The continuous master runner (`t3 loops run`) is retired with the master
        # tick (#2650) — there is no fleet-wide tick to loop over.
        result = runner.invoke(loops_app, ["run", "--once"])
        assert result.exit_code != 0


class TestLoopsToggle:
    def test_enable_delegates_with_the_loop_name(self) -> None:
        with patch("django.setup"), patch("django.core.management.call_command") as call:
            result = runner.invoke(loops_app, ["enable", "tickets"])
        assert result.exit_code == 0, result.stdout
        call.assert_called_once_with("loops_toggle", "enable", "tickets")

    def test_disable_delegates_with_the_loop_name(self) -> None:
        with patch("django.setup"), patch("django.core.management.call_command") as call:
            result = runner.invoke(loops_app, ["disable", "tickets"])
        assert result.exit_code == 0, result.stdout
        call.assert_called_once_with("loops_toggle", "disable", "tickets")

    def test_enable_passes_json_flag(self) -> None:
        with patch("django.setup"), patch("django.core.management.call_command") as call:
            runner.invoke(loops_app, ["enable", "tickets", "--json"])
        call.assert_called_once_with("loops_toggle", "enable", "tickets", json_output=True)

    def test_unknown_name_exit_code_propagates_from_management_command(self) -> None:
        with patch("django.setup"), patch("django.core.management.call_command", side_effect=SystemExit(2)):
            result = runner.invoke(loops_app, ["disable", "no-such-loop"])
        assert result.exit_code == 2
