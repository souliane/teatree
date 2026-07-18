"""``t3 hook`` — run teatree's portable repo-quality gates by name.

The gates in ``scripts/hooks/`` were repo files wired only into teatree's own
``.pre-commit-config.yaml``; a consuming repo had to shim through
``teatree.__file__`` or body-copy the shell hooks. ``t3 hook run <name>``
resolves a name against the packaged :mod:`teatree.hooks.portable` set so a
downstream ``.pre-commit-config.yaml`` needs zero launcher code — a ``repo:
local`` hook whose ``entry`` is ``t3 hook run <name>``, or a ``repo:`` pin
against teatree's packaged root ``.pre-commit-hooks.yaml``.

Python hooks run in-process, the shell hook via subprocess; the hook's exit code
is passed through unchanged. An unknown or internal-only name refuses loudly and
lists the available portable names.
"""

from typing import Annotated

import typer

hook_app = typer.Typer(
    name="hook",
    no_args_is_help=True,
    help="Run teatree's portable repo-quality hooks in any repo (#3312).",
)


def _print_available(*, err: bool = False) -> None:
    from teatree.hooks.portable import PORTABLE_HOOKS  # noqa: PLC0415 — deferred: keeps CLI startup light

    typer.echo("Available portable hooks:", err=err)
    width = max(len(name) for name in PORTABLE_HOOKS)
    for hook in PORTABLE_HOOKS.values():
        typer.echo(f"  {hook.name.ljust(width)}  {hook.summary}", err=err)


@hook_app.command("list")
def list_hooks() -> None:
    """List the portable hook names ``t3 hook run`` resolves."""
    _print_available()


@hook_app.command(
    "run",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    add_help_option=False,
)
def run(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="Portable hook name, e.g. check_module_health.")],
) -> None:
    """Run the portable hook ``name``; extra args pass through (e.g. ``--from-ref``)."""
    from teatree.hooks.portable import UnknownHookError, run_hook  # noqa: PLC0415 — deferred: keeps CLI startup light

    try:
        code = run_hook(name, ctx.args)
    except UnknownHookError:
        typer.echo(f"t3 hook run: '{name}' is not a portable hook.", err=True)
        _print_available(err=True)
        raise typer.Exit(2) from None
    raise typer.Exit(code)
