"""The ``t3 <overlay> workspace`` group must re-expose every core subcommand.

The overlay CLI bridges ``t3 <overlay> workspace <sub>`` through the static
``DJANGO_GROUPS['workspace']`` catalogue in :mod:`teatree.cli.django_groups`.
That catalogue is hand-maintained, so a core ``@command`` added to the
``workspace`` management command without a matching catalogue entry is silently
unreachable through ``t3 <overlay>`` — the documented
``t3 <overlay> workspace reclaim-disk`` was exactly this gap.

These tests pin the catalogue to the management command's actual registered
subcommands so the next omission turns the suite red instead of shipping a
documented-but-unreachable command.
"""

import pytest
from django.test import SimpleTestCase

from teatree.cli.django_groups import DJANGO_GROUPS
from teatree.core.management.commands.workspace import Command

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)


def _catalogue_subcommands() -> set[str]:
    return {name for name, _help in DJANGO_GROUPS["workspace"].subcommands}


def _core_subcommands() -> set[str]:
    """Every subcommand the core ``workspace`` management command registers."""
    return {
        (cmd.name or (cmd.callback.__name__ if cmd.callback else "")).replace("_", "-")
        for cmd in Command.typer_app.registered_commands
        if cmd.name or cmd.callback
    }


class WorkspaceGroupWiringTest(SimpleTestCase):
    def test_reclaim_disk_is_wired(self) -> None:
        assert "reclaim-disk" in _catalogue_subcommands()

    def test_catalogue_covers_every_core_subcommand(self) -> None:
        missing = _core_subcommands() - _catalogue_subcommands()
        assert not missing, f"workspace group omits core subcommands: {sorted(missing)}"

    def test_catalogue_lists_no_phantom_subcommand(self) -> None:
        phantom = _catalogue_subcommands() - _core_subcommands()
        assert not phantom, f"workspace group lists subcommands core does not define: {sorted(phantom)}"
