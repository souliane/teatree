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


@loops_app.command("tick")
def tick_command(
    *,
    overlay: str = typer.Option("", "--overlay", help="Restrict scanning to the named overlay (default: all)."),
    json_output: bool = typer.Option(False, "--json", help="Emit the tick report as JSON."),
) -> None:
    """Run the master ONCE: run every enabled, due loop (each on its own cadence), then render.

    The master claims the ``t3-master`` lease and dispatches only the loops whose
    DB row is enabled and due. Delegates to the ``loops_tick`` management command.
    """
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415

    kwargs: dict[str, str | bool] = {}
    if overlay:
        kwargs["overlay"] = overlay
    if json_output:
        kwargs["json_output"] = True
    call_command("loops_tick", **kwargs)


@loops_app.command("run")
def run_command(
    *,
    interval: int = typer.Option(
        60, "--interval", help="Seconds between master ticks (each loop gated by its own cadence)."
    ),
    overlay: str = typer.Option("", "--overlay", help="Restrict scanning to the named overlay (default: all)."),
    once: bool = typer.Option(False, "--once", help="Run a single tick and return (test hook)."),
) -> None:
    """Run the master CONTINUOUSLY: tick, wait ``--interval``, tick — until interrupted.

    This is the runner, not a loop itself: each beat it asks the DB which loops
    are due and runs them. Per-loop cadence lives in the ``Loop`` rows, so the
    interval only sets how often the master re-checks.
    """
    ensure_django()

    import time  # noqa: PLC0415

    from django.core.management import call_command  # noqa: PLC0415

    kwargs: dict[str, str] = {"overlay": overlay} if overlay else {}
    while True:
        call_command("loops_tick", **kwargs)
        if once:
            return
        time.sleep(interval)


__all__ = ["loops_app"]
