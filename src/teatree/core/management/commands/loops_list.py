"""``manage.py loops_list`` — list DB-configured autonomous loops (#1796).

Backs the read-only ``t3 loops list``. Reads :class:`teatree.core.models.Loop`
rows and prints each loop's name, effective admitted state, cadence (interval or
daily schedule), last run, next-due, the loop's code-declared ``[reach … determinism]``
tags (#3959), and a ``colleague-facing`` marker for a row that reaches a colleague on
the owner's behalf. ``--tag`` narrows the listing to the loops carrying every named tag.

The state column is the TICK's verdict — hold, then the manual override, then the active
preset — never one plane read alone, and a ``LoopState`` hold renders ``paused`` rather
than ``disabled`` so ``t3 loop pause`` is confirmable at a glance (#3117). ORM access
lives in a management command (the project's "anything touching the ORM is a management
command" rule).

Strictly read-only: ORM reads only — it never ticks, marks a run, or mutates a
row. Distinct from the singular ``t3 loop`` (the legacy fat-loop status view).
"""

import datetime as dt
from dataclasses import dataclass
from typing import IO, Annotated, Any, cast

import typer
from django.utils import timezone
from django_typer.management import TyperCommand

from teatree.core.machine_output import emit
from teatree.core.models import Loop, LoopState, LoopStatus
from teatree.loops.base import LoopDeterminism, LoopReach, MiniLoop
from teatree.loops.enable_verdict import EnablePlanes, LoopVerdict
from teatree.loops.registry import iter_loops

_NEVER = "—"
_SECONDS_PER_MINUTE = 60
_SECONDS_PER_HOUR = 3600
KNOWN_TAGS: frozenset[str] = frozenset(
    [*(member.value for member in LoopReach), *(member.value for member in LoopDeterminism)]
)
_TAG_CHOICES = "|".join([*(member.value for member in LoopReach), *(member.value for member in LoopDeterminism)])


def _loops_by_name() -> dict[str, MiniLoop]:
    """The registered mini-loops, keyed by the name their DB row carries.

    A ``Loop`` row with no ``MINI_LOOP`` behind it (an operator-created row, a
    retired loop's leftover) is absent here and renders no tags — the registry is
    the only source, so nothing can show a classification the code does not declare.
    """
    return {mini_loop.name: mini_loop for mini_loop in iter_loops()}


def _tags_of(mini_loop: MiniLoop | None) -> frozenset[str]:
    return frozenset(mini_loop.tags) if mini_loop is not None else frozenset()


def _validated_tags(requested: list[str] | None) -> frozenset[str]:
    unknown = sorted(set(requested or ()) - KNOWN_TAGS)
    if unknown:
        message = f"unknown tag(s) {', '.join(unknown)} — choose from {_TAG_CHOICES}"
        raise typer.BadParameter(message)
    return frozenset(requested or ())


@dataclass(frozen=True, slots=True)
class _LoopRow:
    """One loop's render inputs: the row plus every control-plane read resolved in bulk."""

    loop: Loop
    status: LoopStatus
    verdict: LoopVerdict
    mini_loop: MiniLoop | None
    starved: bool


def _effective_state(verdict: LoopVerdict, status: LoopStatus) -> str:
    """The state the TICK would take, keyed on the effective verdict (#4185).

    ``t3 loop pause`` holds a loop via ``LoopState`` WITHOUT touching ``Loop.enabled``, so
    ``paused`` surfaces the hold that ``disabled`` alone would not distinguish (#3117).
    Everything else follows the verdict rather than any single plane: reading the manual
    override column showed ``disabled`` for every loop a preset admits, which is nearly all
    of them. :func:`_deciding_layer_note` carries WHY.
    """
    if status is LoopStatus.PAUSED:
        return "paused"
    return "enabled" if verdict.admitted else "disabled"


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


def _next_label(verdict: LoopVerdict, loop: Loop, status: LoopStatus, now: dt.datetime) -> str:
    # A loop the verdict refuses won't tick — its next-fire is meaningless. Keyed on the
    # verdict, not any single plane: a loop the tick will fire showed no countdown at
    # all (#4185).
    if not verdict.admitted or status is not LoopStatus.ENABLED:
        return _NEVER
    if loop.is_due(now):
        return "due"
    next_at = loop.next_run_at()
    if next_at is None:
        return _NEVER
    return f"in {_human_duration((next_at - now).total_seconds())}"


def _deciding_layer_note(verdict: LoopVerdict) -> str:
    """Why the verdict is what it is, when the state column alone does not say it.

    A manual override is ALWAYS annotated — an override nobody can see is one nobody
    lifts (A7) — and a preset-masked loop reads ``masked (…)`` instead of silently
    vanishing. A preset ADMITTING a loop needs no note: the state column already says so,
    and annotating every admitted row buries the two that matter.
    """
    if verdict.layer == "hold":
        return ""
    if verdict.layer == "manual":
        return f"  {verdict.detail}"
    return "" if verdict.admitted else f"  masked ({verdict.detail})"


def _withholding_note(loop: Loop, now: dt.datetime) -> str:
    """``attempted <age> ago`` when a tick ran more recently than the anchor it advanced.

    The whole visible difference between a loop nothing drives and one whose every pass
    declines to stamp. Without it, ``last 257h`` on a loop being driven every ten minutes
    reads as "nothing drives this" — which is what #4355 was filed as.
    """
    attempted = loop.last_attempt_at
    if attempted is None or (loop.last_run_at is not None and attempted <= loop.last_run_at):
        return ""
    return f"  (attempted {_human_duration((now - attempted).total_seconds())} ago)"


def _line(row: "_LoopRow", now: dt.datetime) -> str:
    loop = row.loop
    state = _effective_state(row.verdict, row.status)
    last = _human_duration(loop.seconds_since_run(now))
    nxt = _next_label(row.verdict, loop, row.status, now)
    line = f"  {loop.name:<22} {state:<8} {loop.cadence_label:<13} last {last:<10} next {nxt}"
    line += _withholding_note(loop, now)
    if row.mini_loop is not None and row.mini_loop.tags:
        line += f"  [{' '.join(row.mini_loop.tags)}]"
    if loop.colleague_facing:
        line += "  colleague-facing"
    if row.starved:
        line += "  starved"
    return line + _deciding_layer_note(row.verdict)


def _description_line(loop: Loop) -> str | None:
    """The loop's description as an indented continuation line, or ``None`` if blank.

    Kept on its own line below the status row so the fixed-width status columns
    stay aligned regardless of description length.
    """
    if not loop.description:
        return None
    return f"      {loop.description}"


def _payload(row: "_LoopRow", now: dt.datetime) -> dict[str, Any]:
    loop, verdict, mini_loop = row.loop, row.verdict, row.mini_loop
    next_at = loop.next_run_at()
    return {
        "name": loop.name,
        "enabled": loop.enabled,
        "status": _effective_state(verdict, row.status),
        "description": loop.description,
        "delay_seconds": loop.delay_seconds,
        "daily_at": loop.daily_at.strftime("%H:%M") if loop.daily_at else "",
        "cadence": loop.cadence_label,
        "last_run_at": loop.last_run_at.isoformat() if loop.last_run_at else "",
        "last_attempt_at": loop.last_attempt_at.isoformat() if loop.last_attempt_at else "",
        "next_run_at": next_at.isoformat() if next_at else "",
        "due": loop.is_due(now),
        "colleague_facing": loop.colleague_facing,
        "reach": [member.value for member in LoopReach if mini_loop is not None and member in mini_loop.reach],
        "determinism": mini_loop.determinism.value if mini_loop is not None and mini_loop.determinism else "",
        "tags": list(mini_loop.tags) if mini_loop is not None else [],
        "effective_admitted": verdict.admitted,
        "effective_layer": verdict.layer,
        "starved": row.starved,
    }


class Command(TyperCommand):
    help = "List DB-configured autonomous loops (read-only; #1796)."

    def handle(
        self,
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit the loops as JSON.")] = False,
        tag: Annotated[
            list[str] | None,
            typer.Option("--tag", help=f"Keep only loops carrying this tag ({_TAG_CHOICES}); repeatable, ANDed."),
        ] = None,
    ) -> None:
        now = timezone.now()
        registered = _loops_by_name()
        wanted = _validated_tags(tag)
        loops = [row for row in Loop.objects.all() if wanted <= _tags_of(registered.get(row.name))]
        # One read of the LoopState control plane; an absent name → ENABLED default.
        held = {row.name: LoopStatus(row.status) for row in LoopState.objects.all()}
        # One read of every enable plane: the per-loop effective verdict + deciding layer.
        planes = EnablePlanes.resolve(now)
        # One read of the admitted-but-driverless set (#4185).
        from teatree.loops.chain_membership import starved_loop_names  # noqa: PLC0415 — deferred: resolved at call time

        starved = starved_loop_names()
        rows = [
            _LoopRow(
                loop=loop,
                status=held.get(loop.name, LoopStatus.ENABLED),
                verdict=planes.verdict_for(loop.name, reason=loop.override_reason),
                mini_loop=registered.get(loop.name),
                starved=loop.name in starved,
            )
            for loop in loops
        ]
        payload = [_payload(row, now) for row in rows]
        human_lines = ["loops:"]
        for row in rows:
            human_lines.append(_line(row, now))
            description_line = _description_line(row.loop)
            if description_line is not None:
                human_lines.append(description_line)
        emit(
            {"loops": payload},
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human="\n".join(human_lines),
        )
