"""Surface the ticket-sweep changed-ticket trend — Rule 4 of #162.

"Each sweep tends to zero changes" is the factory's own health signal: rules 1
and 2 stop requirements landing in comments at the source, so a sweep should
find less to fold every time. A rising count means something started filing
requirements into comments again.

Deliberately observational. There is no threshold and no new setting, because
the useful reading is the SHAPE of the series, not whether today's number
crossed a line somebody picked — a one-off backlog import legitimately spikes it
and must not redden a box. It reports the latest count, the recent series, the
zero streak, and any run that was begun and never finished (a crashed sweep,
whose count is unknown rather than zero).
"""

import typer
from django.db import DatabaseError

_RECENT_LIMIT = 8


def _check_ticket_sweep_trend() -> bool:
    """Print the sweep trend. Always True — surfacing only, never gates the exit code."""
    try:
        from teatree.core.models import TicketSweepRun  # noqa: PLC0415 — deferred: ORM import after ensure_django

        recent = list(TicketSweepRun.objects.recent(limit=_RECENT_LIMIT))
        incomplete = list(TicketSweepRun.objects.incomplete().values_list("run_id", flat=True))
        streak = TicketSweepRun.objects.zero_streak()
    except (DatabaseError, ImportError) as exc:
        typer.echo(f"WARN  Ticket-sweep trend check crashed: {exc.__class__.__name__}: {exc}")
        return True

    if not recent:
        typer.echo("INFO  Ticket sweeps: no finished run recorded yet (t3 <overlay> ticket sweep-begin).")
    else:
        counts = [run.changed_count for run in recent]
        series = " ".join(str(count) for count in reversed(counts))
        typer.echo(f"INFO  Ticket sweeps: latest changed {counts[0]}, last {len(counts)} (oldest first): {series}")
        typer.echo(f"INFO  Ticket sweeps: {streak} consecutive zero-change run(s) — the healthy direction.")
    if incomplete:
        shown = ", ".join(incomplete[:5])
        typer.echo(f"WARN  Ticket sweeps: {len(incomplete)} run(s) begun and never finished — {shown}")
    return True


__all__ = ["_check_ticket_sweep_trend"]
