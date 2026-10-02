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
    help="Overlay-smoke commands — exercise CLI paths so bugs surface in the loop, not in E2E.",
    context_settings={
        "allow_extra_args": True,
        "allow_interspersed_args": False,
        "ignore_unknown_options": True,
    },
)


@dogfood_app.callback(invoke_without_command=True)
def _root(ctx: typer.Context) -> None:
    """Print help and exit 0 when invoked with no sub-command.

    The callback (rather than the app's ``no_args_is_help``) is what forces a
    real Typer *group*: a single-command Typer app collapses into that command,
    so bare ``t3 dogfood`` would run ``overlay-provision-smoke`` instead of
    showing help. With the callback present the app stays a group, and a bare
    invocation lands here. ``no_args_is_help`` is not used because a Click group
    exits 2 on the no-command help path; the cron/loop recipe wants a clean 0.
    """
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit(code=0)


@dogfood_app.command(
    name="overlay-provision-smoke",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    add_help_option=False,
)
def _overlay_provision_smoke(ctx: typer.Context) -> None:
    """Forward ``t3 dogfood overlay-provision-smoke [flags]`` to the management command."""
    managepy_core("dogfood", "overlay-provision-smoke", *ctx.args)
