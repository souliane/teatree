"""Keep the overlay ticket route aligned with its Django command."""

from django.test import SimpleTestCase

from teatree.cli.django_groups import DJANGO_GROUPS
from teatree.core.management.commands.ticket import Command


class TicketGroupWiringTests(SimpleTestCase):
    def test_every_core_subcommand_is_reachable_from_the_overlay_cli(self) -> None:
        catalogue = {name for name, _help in DJANGO_GROUPS["ticket"].subcommands}
        commands = {
            (command.name or (command.callback.__name__ if command.callback else "")).replace("_", "-")
            for command in Command.typer_app.registered_commands
            if command.name or command.callback
        }
        groups = {group.name for group in Command.typer_app.registered_groups if group.name}
        assert commands | groups == catalogue
