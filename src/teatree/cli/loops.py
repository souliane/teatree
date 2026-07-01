"""``t3 loops`` — DB-configured autonomous loops (#1796).

``t3 loops list`` prints the loops from the DB (read-only). ``t3 loops tick
--loop <name>`` runs ONE enabled, due loop — the per-loop primitive each native
Claude ``/loop`` fires (#2650). There is NO master tick: ``t3 loops tick`` with no
``--loop`` is a hard error. Per-loop management — add / edit / enable / disable —
is via ``t3 loop enable/disable`` + the Django admin (``Loop`` rows: name /
prompt / delay / enabled). ORM access lives in the ``loops_tick`` / ``loops_list``
management commands, not a plain typer command.
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
    loop: str = typer.Option(
        "",
        "--loop",
        help=(
            "REQUIRED. Run ONE enabled, due DB Loop by name (#2650) — what each native Claude `/loop` "
            "fires, claiming the per-loop `loop:<name>` lease. There is no master tick: omitting --loop "
            "is a hard error."
        ),
    ),
    overlay: str = typer.Option("", "--overlay", help="Restrict scanning to the named overlay (default: all)."),
    json_output: bool = typer.Option(False, "--json", help="Emit the tick report as JSON."),
) -> None:
    """Run ONE enabled, due loop by name — the per-loop primitive each native Claude ``/loop`` fires (#2650).

    Scopes the tick to that single enabled, due ``Loop`` row, claiming the disjoint
    per-loop ``loop:<name>`` lease so the per-loop loops run in parallel. **There is
    no master tick:** omitting ``--loop`` is a hard error (the ``loops_tick``
    management command refuses it). Delegates to that management command.
    """
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415

    kwargs: dict[str, str | bool] = {}
    if loop:
        kwargs["loop"] = loop
    if overlay:
        kwargs["overlay"] = overlay
    if json_output:
        kwargs["json_output"] = True
    call_command("loops_tick", **kwargs)


__all__ = ["loops_app"]
