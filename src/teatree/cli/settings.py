"""``t3 settings`` — thin CLI access to the settings management commands.

``compare`` is the Compare Instances page's own question asked from a terminal: what differs
between the boxes, and does it matter? It answers from
:func:`~teatree.core.settings.settings_compare.build_compare_view` — the SAME call the page makes — so
there is one comparison in the codebase and two ways to read it, never two implementations
that agree until one of them is changed.

The comparison touches ORM-backed snapshot state, so this module only bootstraps Django and
delegates to ``settings_compare``. The management command owns the CLI options, comparison and rendering.
"""

import typer

from teatree.utils.django_bootstrap import ensure_django

settings_app = typer.Typer(
    name="settings",
    no_args_is_help=True,
    help="This box's settings beside its peers' — the Compare Instances page, in a terminal.",
)


@settings_app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True, "help_option_names": []}
)
def compare(ctx: typer.Context) -> None:
    """What differs between this box and its peers, and whether it can be read as drift.

    Exits non-zero only when there was nothing to compare — fewer than two instances answered. A
    comparison that RAN is a success whatever it found, "not comparable" included: that verdict is
    a true reading of boxes running different code, not a failure of this command.

    The table is laid out in the terminal's own width, so ``COLUMNS`` sets it for a redirected run.
    """
    ensure_django()

    from django.core.management import (  # noqa: PLC0415 — deferred until Django is ready
        call_command,
        get_commands,
        load_command_class,
    )
    from django.core.management.base import CommandError  # noqa: PLC0415 — deferred until Django is ready

    command = load_command_class(get_commands()["settings_compare"], "settings_compare")
    parser = command.create_parser("t3 settings", "compare")
    if "--help" in ctx.args:
        parser.print_help()
        return
    try:
        parser.parse_args(ctx.args)
    except CommandError as exc:
        parser.print_help()
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=2) from None
    call_command("settings_compare", *ctx.args)


__all__ = ["settings_app"]
