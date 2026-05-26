"""``t3 dogfood`` — top-level entry point for overlay-smoke commands (#1308).

Thin Typer group that forwards to the
:mod:`teatree.core.management.commands.dogfood` management command via
:func:`teatree.cli.overlay.managepy_core` (the ``python -m teatree``
path). The command lives in core management commands so the loop
scanner can shell out to the same binary it documents, and the cron
recipe stays one stable invocation: ``t3 dogfood overlay-provision-smoke``.

Room for sibling smokes is the same namespace — add a sub-command
here and in :class:`teatree.core.management.commands.dogfood.Command`.
"""

import typer

from teatree.cli.overlay import managepy_core

dogfood_app = typer.Typer(
    name="dogfood",
    no_args_is_help=True,
    help="Overlay-smoke commands — exercise CLI paths so bugs surface in the loop, not in E2E.",
    context_settings={
        "allow_extra_args": True,
        "allow_interspersed_args": False,
        "ignore_unknown_options": True,
    },
)


@dogfood_app.command(
    name="overlay-provision-smoke",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    add_help_option=False,
)
def _overlay_provision_smoke(ctx: typer.Context) -> None:
    """Forward ``t3 dogfood overlay-provision-smoke [flags]`` to the management command."""
    managepy_core("dogfood", "overlay-provision-smoke", *ctx.args)
