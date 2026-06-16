"""``manage.py loops_list`` — list DB-configured autonomous loops (#1796).

Backs the read-only ``t3 loops list``. Reads :class:`teatree.core.models.Loop`
rows and prints each loop's name, enabled state, cadence (interval or daily
schedule), last run, and next-due. ORM access lives in a management command
(the project's "anything touching the ORM is a management command" rule).

Strictly read-only: ORM reads only — it never ticks, marks a run, or mutates a
row. Distinct from the singular ``t3 loop`` (the legacy fat-loop status view).
"""

import datetime as dt
import json
from typing import Annotated, Any

import typer
from django.utils import timezone
from django_typer.management import TyperCommand

from teatree.core.models import Loop

_NEVER = "—"
_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600


def _human_duration(seconds: float | None) -> str:
    """Render a duration as ``45s`` / ``5m00s`` / ``1h00m``; ``—`` for ``None``."""
    if seconds is None:
        return _NEVER
    total = int(seconds)
    if total < _SECONDS_PER_MINUTE:
        return f"{total}s"
    if total < _SECONDS_PER_HOUR:
        return f"{total // _SECONDS_PER_MINUTE}m{total % _SECONDS_PER_MINUTE:02d}s"
    hours, remainder = divmod(total, _SECONDS_PER_HOUR)
    return f"{hours}h{remainder // _SECONDS_PER_MINUTE:02d}m"


def _next_label(loop: Loop, now: dt.datetime) -> str:
    if not loop.enabled:
        return _NEVER
    if loop.is_due(now):
        return "due"
    next_at = loop.next_run_at()
    if next_at is None:
        return _NEVER
    return f"in {_human_duration((next_at - now).total_seconds())}"


def _line(loop: Loop, now: dt.datetime) -> str:
    enabled = "enabled" if loop.enabled else "disabled"
    last = _human_duration(loop.seconds_since_run(now))
    return f"  {loop.name:<22} {enabled:<8} {loop.cadence_label:<13} last {last:<10} next {_next_label(loop, now)}"


def _payload(loop: Loop, now: dt.datetime) -> dict[str, Any]:
    next_at = loop.next_run_at()
    return {
        "name": loop.name,
        "enabled": loop.enabled,
        "delay_seconds": loop.delay_seconds,
        "daily_at": loop.daily_at.strftime("%H:%M") if loop.daily_at else "",
        "cadence": loop.cadence_label,
        "last_run_at": loop.last_run_at.isoformat() if loop.last_run_at else "",
        "next_run_at": next_at.isoformat() if next_at else "",
        "due": loop.is_due(now),
    }


class Command(TyperCommand):
    help = "List DB-configured autonomous loops (read-only; #1796)."

    def handle(
        self,
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit the loops as JSON.")] = False,
    ) -> None:
        now = timezone.now()
        loops = list(Loop.objects.all())
        if json_output:
            self.stdout.write(json.dumps({"loops": [_payload(loop, now) for loop in loops]}, indent=2))
            return
        self.stdout.write("loops:")
        for loop in loops:
            self.stdout.write(_line(loop, now))
