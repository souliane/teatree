"""``t3 goal`` (``set`` / ``clear`` / ``list``) — register standing verified-green goals (PR-25).

Thin Typer wrappers delegating to the ``standing_goal`` Django management command
(anything touching the ORM is a management command). Delegation shape mirrors
:mod:`teatree.cli.loop.owner`: map the management command's ``SystemExit`` to a
``typer.Exit`` so the process exit code is preserved.
"""

import typer

from teatree.utils.django_bootstrap import ensure_django

goal_app = typer.Typer(name="goal", no_args_is_help=True, help="Standing verified-green goals (PR-25).")


def _delegate(subcommand: str, *args: str, **kwargs: str | bool) -> None:
    ensure_django()
    from django.core.management import call_command  # noqa: PLC0415 — deferred import

    try:
        call_command("standing_goal", subcommand, *args, **kwargs)
    except SystemExit as exc:
        raise typer.Exit(code=int(exc.code) if isinstance(exc.code, int) else 1) from exc


@goal_app.command("set")
def set_goal(
    name: str = typer.Argument(..., help="Unique goal name (e.g. 'evals-green')."),
    *,
    check: str = typer.Option(..., "--check", help="Shell command that exits 0 when the goal is green."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Register (or re-arm) a standing verified-green goal."""
    _delegate("set", name, check=check, json_output=json_output)


@goal_app.command("clear")
def clear(
    name: str | None = typer.Argument(None, help="Goal name to clear; omit to clear ALL."),
    *,
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Delete one named standing goal, or every goal when no name is given."""
    if name is None:
        _delegate("clear", json_output=json_output)
    else:
        _delegate("clear", name, json_output=json_output)


@goal_app.command("list")
def list_goals(
    *,
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """List every registered standing goal and its active state."""
    _delegate("list", json_output=json_output)


__all__ = ["goal_app"]
