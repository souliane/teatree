# test-path: cross-cutting
import pytest
from django.test import SimpleTestCase

from teatree.cli.django_groups import DJANGO_GROUPS
from teatree.core.management.commands.retention import Command

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)


def _catalogue_subcommands() -> set[str]:
    return {name for name, _help in DJANGO_GROUPS["retention"].subcommands}


def _core_subcommands() -> set[str]:
    return {
        (registered.name or (registered.callback.__name__ if registered.callback else "")).replace("_", "-")
        for registered in Command.typer_app.registered_commands
        if registered.name or registered.callback
    }


class RetentionGroupWiringTest(SimpleTestCase):
    def test_catalogue_matches_the_core_command(self) -> None:
        assert _catalogue_subcommands() == _core_subcommands()
