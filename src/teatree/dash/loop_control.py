"""Loop-control read model: each loop's effective verdict + the layer that decided it (#3162).

The dashboard reads the SAME effective verdict the tick gates on — from the one
shared source ``teatree.loops.enable_verdict.effective_verdicts`` that ``t3 loops
list``, ``t3 loop preset show`` and the statusline also read — so it can never
recompute a verdict that drifts from the fleet. That verdict folds the three layers of
the read order: the durable ``LoopState`` hold, the tri-state manual override
(``Loop.enabled``), and the active preset. The write side goes exclusively through the
``LoopManager`` verbs, so this module only reads.
"""

import datetime as dt
import logging
from dataclasses import dataclass

from teatree.core.mode_resolution import resolve_active_mode
from teatree.core.models.loop import Loop
from teatree.core.models.loop_preset import Mode
from teatree.core.models.loop_state import LoopState, LoopStatus
from teatree.dash.gate_state import dash_gate_fail_open
from teatree.loops.enable_verdict import LoopVerdict, effective_verdicts, fleet_admits_work
from teatree.loops.live import LoopStatusEntry, build_report
from teatree.loops.loop_cadence_editing import CADENCE_STEP_SECONDS, CadenceBounds, cadence_bounds_for, is_off_grid
from teatree.loops.registry import iter_loops

logger = logging.getLogger(__name__)

#: Clears the override so the schedule / default decides again — the one switch
#: value that is not a ``Mode`` row name.
MODE_SWITCH_AUTO = "auto"

# The exact phrase the operator must type to flip the master fail-open switch —
# the one switch that relaxes every over-deny gate must never be a one-click toggle.
GATE_CONFIRM_PHRASE = "fail-open"


@dataclass(frozen=True, slots=True)
class LoopRow:
    """One loop in the unified table: what decides it, how often it fires, and when."""

    name: str
    description: str
    #: The MANUAL override: ``None`` = none set, else what the human forced.
    enabled: bool | None
    override_reason: str
    status: str
    effective: bool
    deciding_layer: str
    cadence_label: str
    delay_seconds: int | None
    daily_at: str
    bounds: CadenceBounds
    last_run_at: dt.datetime | None
    next_run_at: dt.datetime | None
    tags: tuple[str, ...]

    @property
    def is_daily(self) -> bool:
        return bool(self.daily_at)


@dataclass(frozen=True, slots=True)
class LoopControlView:
    loops: tuple["LoopRow", ...]
    infra_slots: tuple[LoopStatusEntry, ...]
    mode_name: str
    mode_source: str
    #: Every defined mode, so the header offers the live set rather than a frozen list.
    mode_names: tuple[str, ...]
    gate_fail_open: bool
    #: Whether the active preset admits ANY loop — the fleet's stop condition, shown read-only.
    fleet_admits: bool
    #: The global cadence grid, stated ONCE as the table's legend (#4079). It is the same for
    #: every ordinary loop, so repeating it per row said nothing about any particular row.
    cadence_step_seconds: int = CADENCE_STEP_SECONDS
    #: Stored intervals that predate the grid — reported so the operator decides, never
    #: rewritten. Empty on a box whose rows are all on the grid, which is the normal case.
    off_grid: tuple[tuple[str, int], ...] = ()


def build_loop_control() -> LoopControlView:
    """The whole loop-control page read model: loop rows + infra slots + header state."""
    resolved = resolve_active_mode()
    loops = build_loop_rows()
    return LoopControlView(
        loops=loops,
        infra_slots=_infra_slots(),
        mode_name=resolved.name,
        mode_source=resolved.source,
        mode_names=tuple(Mode.objects.values_list("name", flat=True)),
        gate_fail_open=dash_gate_fail_open(),
        fleet_admits=_fleet_admits(),
        # Derived from the rows already loaded above rather than re-queried: the page's query
        # count is a pinned budget, and this listing is a property of rows it already holds.
        off_grid=tuple(
            (row.name, row.delay_seconds)
            for row in loops
            if row.delay_seconds is not None and is_off_grid(row.delay_seconds)
        ),
    )


def _infra_slots() -> tuple[LoopStatusEntry, ...]:
    """The worker's infra lease slots — a small distinct section beside the loop table."""
    try:
        return build_report().infra_slots
    except Exception:
        logger.warning("infra-slot read failed — rendering the loop table without it", exc_info=True)
        return ()


def _fleet_admits() -> bool:
    """Whether the active preset admits any loop at all — the fleet's stop condition."""
    return fleet_admits_work()


def build_loop_rows() -> tuple[LoopRow, ...]:
    """Every ``Loop`` row with its effective verdict and deciding layer.

    The verdict + deciding layer come from the shared canonical source
    :func:`teatree.loops.enable_verdict.effective_verdicts`, so the dashboard never
    recomputes an admission verdict that could drift from the tick. Display fields
    (description, cadence, the paused-vs-disabled hold status) are joined by name
    from one ``Loop`` and one ``LoopState`` read; a verdict whose ``Loop`` row
    vanished between the two reads is skipped rather than raising.

    The reach/determinism tags (#3959) are joined from the ``MiniLoop`` registry, not
    the row, so the table cannot render a classification that disagrees with the code;
    a row with no registered loop behind it simply carries none.
    """
    loops = {loop.name: loop for loop in Loop.objects.all()}
    status_by_name = {row.name: row.status for row in LoopState.objects.all()}
    tags_by_name = {mini_loop.name: mini_loop.tags for mini_loop in iter_loops()}
    return tuple(
        _loop_row(
            loop,
            status_by_name.get(verdict.name, LoopStatus.ENABLED.value),
            verdict,
            tags_by_name.get(verdict.name, ()),
        )
        for verdict in effective_verdicts()
        if (loop := loops.get(verdict.name)) is not None
    )


def _loop_row(loop: Loop, status: str, verdict: LoopVerdict, tags: tuple[str, ...]) -> LoopRow:
    return LoopRow(
        name=loop.name,
        description=loop.description,
        enabled=loop.enabled,
        override_reason=loop.override_reason,
        status=status,
        effective=verdict.admitted,
        deciding_layer=_deciding_layer(verdict, status=status),
        cadence_label=loop.cadence_label,
        delay_seconds=loop.delay_seconds,
        daily_at=loop.daily_at.strftime("%H:%M") if loop.daily_at is not None else "",
        bounds=cadence_bounds_for(loop.name),
        last_run_at=loop.last_run_at,
        next_run_at=loop.next_run_at(),
        tags=tags,
    )


#: How the layer that supplied the active preset is named in the table. Keyed on
#: :attr:`~teatree.core.mode_resolution.ResolvedMode.source`.
_PRESET_LAYER_LABELS = {
    "override": "preset (pinned)",
    "schedule": "preset (schedule)",
    "default": "preset (default)",
}


def _deciding_layer(verdict: LoopVerdict, *, status: str) -> str:
    """Which layer decides the loop's verdict — answers "why isn't it running".

    Reads the shared verdict's ``layer`` so the precedence mirrors the resolver exactly:
    hold, then the manual override, then the preset that always answers.
    """
    if verdict.layer == "hold":
        return "hold — paused" if status == LoopStatus.PAUSED.value else "hold — disabled"
    if verdict.layer == "manual":
        return verdict.detail
    return _PRESET_LAYER_LABELS.get(verdict.layer, "preset")
