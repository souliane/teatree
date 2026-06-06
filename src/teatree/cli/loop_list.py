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
) -> None:
    """Print LIVE loop status: each loop's enabled state, cadence, last fire, and next tick.

    Read-only: it computes the report from the DB and prints it — never ticks,
    claims, or mutates anything. Unlike ``t3 loop status`` (the cached
    statusline view), every countdown here is recomputed at call time.
    """
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415

    kwargs: dict[str, bool] = {}
    if json_output:
        kwargs["json_output"] = True
    call_command("loop_list", **kwargs)


__all__ = ["list_command"]
