from django.test import SimpleTestCase

from teatree.cli.django_groups import DJANGO_GROUPS
from teatree.core.management.commands.secrets import Command


class SecretsGroupWiringTest(SimpleTestCase):
    def test_catalogue_matches_every_registered_command(self) -> None:
        catalogue = {name for name, _ in DJANGO_GROUPS["secrets"].subcommands}
        registered = {
            (item.name or item.callback.__name__).replace("_", "-")
            for item in Command.typer_app.registered_commands
            if item.name or item.callback
        }
        assert catalogue == registered
        assert {"set", "rm", "verify", "get", "exec"} <= catalogue
