"""``manage.py loops_list`` — list DB-configured autonomous loops (#1796).

Backs the read-only ``t3 loops list``. Reads :class:`teatree.core.models.Loop`
rows and prints each loop's name, effective admitted state, cadence (interval or
daily schedule), last run, next-due, and a ``[colleague-facing]`` tag (#2904)
when the row is gated off during any availability-deferring mode. The state
column folds a :class:`teatree.core.models.LoopState` pause/disable hold into the
row's ``enabled`` flag (#3117) — ``t3 loop pause`` holds a loop WITHOUT flipping
``Loop.enabled``, so a pause is now confirmable at a glance. ORM access lives in
a management command (the project's "anything touching the ORM is a management
command" rule).

Strictly read-only: ORM reads only — it never ticks, marks a run, or mutates a
row. Distinct from the singular ``t3 loop`` (the legacy fat-loop status view).
"""

import datetime as dt
import json
from typing import Annotated, Any

import typer
from django.utils import timezone
from django_typer.management import TyperCommand

from teatree.core.models import Loop, LoopState, LoopStatus
from teatree.loops.preset_status import LoopVerdict, effective_verdicts

_NEVER = "—"
_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600


def _effective_state(loop: Loop, status: LoopStatus) -> str:
    """The admitted state, folding a ``LoopState`` hold into the row's ``enabled`` flag.

    ``t3 loop pause`` holds a loop via ``LoopState`` WITHOUT flipping
    ``Loop.enabled``, so the row alone still reads ``enabled`` — this surfaces the
    hold so a pause is confirmable at a glance (#3117).
    """
    if not loop.enabled or status is LoopStatus.DISABLED:
        return "disabled"
    if status is LoopStatus.PAUSED:
        return "paused"
    return "enabled"


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


def _next_label(loop: Loop, status: LoopStatus, now: dt.datetime) -> str:
    # A held loop (paused/disabled) won't tick — its next-fire is meaningless.
    if not loop.enabled or status is not LoopStatus.ENABLED:
        return _NEVER
    if loop.is_due(now):
        return "due"
    next_at = loop.next_run_at()
    if next_at is None:
        return _NEVER
    return f"in {_human_duration((next_at - now).total_seconds())}"


def _preset_note(verdict: LoopVerdict | None) -> str:
    """The masked/forced note when a preset (not base/hold) decides the loop, else ``""``.

    A masked-off loop reads ``masked (preset heads-down)`` instead of silently
    vanishing; a preset that forces a base-disabled loop on reads ``forced-on``.
    """
    if verdict is None or verdict.layer in {"base", "hold"}:
        return ""
    tag = "masked" if not verdict.admitted else "forced-on"
    return f"  {tag} ({verdict.detail})"


def _line(loop: Loop, status: LoopStatus, now: dt.datetime, verdict: LoopVerdict | None) -> str:
    state = _effective_state(loop, status)
    last = _human_duration(loop.seconds_since_run(now))
    nxt = _next_label(loop, status, now)
    line = f"  {loop.name:<22} {state:<8} {loop.cadence_label:<13} last {last:<10} next {nxt}"
    if loop.colleague_facing:
        line += "  [colleague-facing]"
    return line + _preset_note(verdict)


def _description_line(loop: Loop) -> str | None:
    """The loop's description as an indented continuation line, or ``None`` if blank.

    Kept on its own line below the status row so the fixed-width status columns
    stay aligned regardless of description length.
    """
    if not loop.description:
        return None
    return f"      {loop.description}"


def _payload(loop: Loop, status: LoopStatus, now: dt.datetime, verdict: LoopVerdict | None) -> dict[str, Any]:
    next_at = loop.next_run_at()
    return {
        "name": loop.name,
        "enabled": loop.enabled,
        "status": _effective_state(loop, status),
        "description": loop.description,
        "delay_seconds": loop.delay_seconds,
        "daily_at": loop.daily_at.strftime("%H:%M") if loop.daily_at else "",
        "cadence": loop.cadence_label,
        "last_run_at": loop.last_run_at.isoformat() if loop.last_run_at else "",
        "next_run_at": next_at.isoformat() if next_at else "",
        "due": loop.is_due(now),
        "colleague_facing": loop.colleague_facing,
        "effective_admitted": verdict.admitted if verdict is not None else None,
        "effective_layer": verdict.layer if verdict is not None else "base",
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
        # One read of the LoopState control plane; an absent name → ENABLED default.
        held = {row.name: LoopStatus(row.status) for row in LoopState.objects.all()}
        # One read of the preset mask (L3/L2): the per-loop effective verdict + layer.
        verdicts = {verdict.name: verdict for verdict in effective_verdicts(now)}
        if json_output:
            payload = [
                _payload(loop, held.get(loop.name, LoopStatus.ENABLED), now, verdicts.get(loop.name)) for loop in loops
            ]
            self.stdout.write(json.dumps({"loops": payload}, indent=2))
            return
        self.stdout.write("loops:")
        for loop in loops:
            self.stdout.write(_line(loop, held.get(loop.name, LoopStatus.ENABLED), now, verdicts.get(loop.name)))
            description_line = _description_line(loop)
            if description_line is not None:
                self.stdout.write(description_line)
