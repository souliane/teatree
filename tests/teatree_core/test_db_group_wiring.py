# test-path: cross-cutting — pins the teatree.cli.django_groups catalogue against the core
# `db` management command's registered subcommands; the contract spans both packages.
"""The ``t3 <overlay> db`` group must re-expose every core subcommand.

The overlay CLI bridges ``t3 <overlay> db <sub>`` through the static
``DJANGO_GROUPS['db']`` catalogue in :mod:`teatree.cli.django_groups`. That
catalogue is hand-maintained, so a core ``@command`` added to the ``db``
management command without a matching catalogue entry is silently unreachable
through ``t3 <overlay>`` — correctly registered, fully tested, and still
uncallable, with nothing reporting the gap.

``db migrate-app`` is the live instance: the verb that catches a worktree's app
database up on schema without re-importing it (``db migrate`` targets the
control DB, ``db refresh`` destroys the data first) is reachable only while its
catalogue entry exists. These tests pin the catalogue to the management
command's actual registered subcommands so the next omission turns the suite red
instead of shipping a documented-but-unreachable command.
"""

import pytest
from django.test import SimpleTestCase

from teatree.cli.django_groups import DJANGO_GROUPS
from teatree.core.management.commands.db import Command

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)


def _catalogue_subcommands() -> set[str]:
    return {name for name, _help in DJANGO_GROUPS["db"].subcommands}


def _core_subcommands() -> set[str]:
    """Every subcommand the core ``db`` management command registers."""
    return {
        (cmd.name or (cmd.callback.__name__ if cmd.callback else "")).replace("_", "-")
        for cmd in Command.typer_app.registered_commands
        if cmd.name or cmd.callback
    }


class DbGroupWiringTest(SimpleTestCase):
    def test_migrate_app_is_wired(self) -> None:
        assert "migrate-app" in _catalogue_subcommands()

    def test_catalogue_covers_every_core_subcommand(self) -> None:
        missing = _core_subcommands() - _catalogue_subcommands()
        assert not missing, f"db group omits core subcommands: {sorted(missing)}"

    def test_catalogue_lists_no_phantom_subcommand(self) -> None:
        phantom = _catalogue_subcommands() - _core_subcommands()
        assert not phantom, f"db group lists subcommands core does not define: {sorted(phantom)}"
