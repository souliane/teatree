"""``t3 loop list`` — print LIVE loop status from the DB (read-only; #1744).

Split out of ``cli.loop`` (module-health: that file owns the tick / start /
dashboard / self-improve concerns). ``t3 loop status`` prints the cached
statusline file written at the last tick, so its countdowns go stale —
``t3 loop list`` recomputes the state live on every call. Delegates to the
``loop_list`` Django management command (anything touching the ORM is a
management command, not a plain typer command).
"""

import typer

from teatree.utils.django_bootstrap import ensure_django


def list_command(
    *,
    json_output: bool = typer.Option(False, "--json", help="Emit the live loop status as JSON."),
    show_all: bool = typer.Option(
        False,
        "--all",
        help="Also show the per-loop owning sessions (cross-session health view, #1834).",
    ),
) -> None:
    """Print LIVE loop status: each loop's enabled state, cadence, last fire, and next tick.

    Read-only: it computes the report from the DB and prints it — never ticks,
    claims, or mutates anything. Unlike ``t3 loop status`` (the cached
    statusline view), every countdown here is recomputed at call time. With
    ``--all`` it additionally lists each per-loop owning session — the
    cross-session observability view for the dedicated-loop layer (#1834).
    """
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415 — deferred: Django import at call time

    kwargs: dict[str, bool] = {}
    if json_output:
        kwargs["json_output"] = True
    if show_all:
        kwargs["show_all"] = True
    call_command("loop_list", **kwargs)


__all__ = ["list_command"]
