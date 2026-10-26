from unittest.mock import patch

from django.test import SimpleTestCase
from typer.testing import CliRunner

from teatree.cli.django_groups import DJANGO_GROUPS
from teatree.cli.overlay import OverlayAppBuilder
from teatree.core.management.commands.secrets import Command


class SecretsGroupWiringTest(SimpleTestCase):
    def test_exec_bridge_preserves_the_command_separator(self) -> None:
        with patch("teatree.cli.overlay.managepy_core") as bridge:
            result = CliRunner().invoke(
                OverlayAppBuilder("teatree", None).build(),
                ["secrets", "exec", "--env", "TOKEN=team/token", "--", "python", "-c", "print(1)"],
            )
        assert result.exit_code == 0, result.output
        bridge.assert_called_once_with(
            "secrets", "exec", "--env", "TOKEN=team/token", "--", "python", "-c", "print(1)", overlay_name="teatree"
        )

    def test_catalogue_matches_every_registered_command(self) -> None:
        catalogue = {name for name, _ in DJANGO_GROUPS["secrets"].subcommands}
        registered = {
            (item.name or item.callback.__name__).replace("_", "-")
            for item in Command.typer_app.registered_commands
            if item.name or item.callback
        }
        assert catalogue == registered
        assert {"set", "rm", "verify", "get", "exec"} <= catalogue
