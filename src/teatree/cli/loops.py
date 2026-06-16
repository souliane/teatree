"""``t3 loops`` — DB-configured autonomous loops (#1796).

``t3 loops list`` prints the loops from the DB (read-only). Per-loop management
— add / edit / enable / disable — is available in the Django admin
(``Loop`` rows: name / prompt / delay / enabled). Delegates to the
``loops_list`` management command (ORM access lives in a management command,
not a plain typer command).

Distinct from the singular ``t3 loop`` (the legacy fat-loop tick surface),
which is retired as #1796 lands.
"""

import typer

from teatree.utils.django_bootstrap import ensure_django

loops_app = typer.Typer(
    name="loops",
    no_args_is_help=True,
    help="Manage DB-configured autonomous loops (#1796).",
)


@loops_app.callback()
def _loops() -> None:
    """Keep ``loops`` a command group (one subcommand would otherwise collapse to single-command)."""


@loops_app.command("list")
def list_command(
    *,
    json_output: bool = typer.Option(False, "--json", help="Emit the loops as JSON."),
) -> None:
    """List DB-configured autonomous loops: name, enabled, delay, last run, next due.

    Read-only: it reads the ``Loop`` table and prints it — never ticks, marks a
    run, or mutates a row.
    """
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415

    kwargs: dict[str, bool] = {}
    if json_output:
        kwargs["json_output"] = True
    call_command("loops_list", **kwargs)


__all__ = ["loops_app"]
