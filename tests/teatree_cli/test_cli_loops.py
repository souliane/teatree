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
