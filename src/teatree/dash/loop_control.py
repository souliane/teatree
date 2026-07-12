"""Loop-control read model: each loop's effective verdict + the layer that decided it (#3162).

The dashboard reads the SAME effective verdict the tick gates on — from the one
shared source ``teatree.loops.preset_status.effective_verdicts`` that ``t3 loops
list``, ``t3 loop preset show`` and the statusline also read — so it can never
recompute a verdict that drifts from the fleet. That verdict folds all four layers:
the durable ``LoopState`` hold (L4), the active preset's L3 override / L2 schedule
mask (#3159), and the base ``Loop.enabled`` flag (L1). The write side
(pause/resume/disable/enable) goes exclusively through the paired atomic
``LoopManager`` verbs, so this module only reads.
"""

import logging
from dataclasses import dataclass

from teatree.config import get_effective_settings
from teatree.core.availability import resolve_mode
from teatree.core.models.config_setting import ConfigSetting
from teatree.core.models.loop import Loop
from teatree.core.models.loop_state import LoopState, LoopStatus
from teatree.loops.preset_status import LoopVerdict, effective_verdicts

logger = logging.getLogger(__name__)

# The four legal per-loop control verbs. Each dispatches to the EXACT same manager
# method the `manage.py loop_state` command calls, never a raw field write: `pause`
# is the reversible LoopState hold (Loop.enabled untouched), while resume/disable/
# enable move BOTH planes atomically through the paired LoopManager verbs.
LOOP_ACTIONS: frozenset[str] = frozenset({"pause", "resume", "disable", "enable"})


# Availability modes the header switch offers: the three write_override targets
# plus "auto" (clear the override so the schedule decides again).
AVAILABILITY_ACTIONS: frozenset[str] = frozenset({"present", "away", "autonomous_away", "auto"})

# The exact phrase the operator must type to flip the master fail-open switch —
# the one switch that relaxes every over-deny gate must never be a one-click toggle.
GATE_CONFIRM_PHRASE = "fail-open"


@dataclass(frozen=True, slots=True)
class LoopRow:
    name: str
    description: str
    enabled: bool
    status: str
    effective: bool
    deciding_layer: str
    cadence_label: str


@dataclass(frozen=True, slots=True)
class LoopControlView:
    loops: tuple["LoopRow", ...]
    availability_mode: str
    availability_source: str
    gate_fail_open: bool
    runner_enabled: bool


def build_loop_control() -> LoopControlView:
    """The whole loop-control page read model: loop rows + header control state."""
    resolution = resolve_mode()
    return LoopControlView(
        loops=build_loop_rows(),
        availability_mode=resolution.mode,
        availability_source=resolution.source,
        gate_fail_open=_gate_fail_open(),
        runner_enabled=_runner_enabled(),
    )


def _gate_fail_open() -> bool:
    """The master ``danger_gate_fail_open`` DB-home switch state (a red banner when on)."""
    return bool(ConfigSetting.objects.get_effective("danger_gate_fail_open"))


def _runner_enabled() -> bool:
    """The global ``loop_runner_enabled`` kill-switch state (shown read-only)."""
    return get_effective_settings().loop_runner_enabled


def build_loop_rows() -> tuple[LoopRow, ...]:
    """Every ``Loop`` row with its effective verdict and deciding layer.

    The verdict + deciding layer come from the shared canonical source
    :func:`teatree.loops.preset_status.effective_verdicts`, so the dashboard never
    recomputes an admission verdict that could drift from the tick. Display fields
    (description, cadence, the paused-vs-disabled hold status) are joined by name
    from one ``Loop`` and one ``LoopState`` read; a verdict whose ``Loop`` row
    vanished between the two reads is skipped rather than raising.
    """
    loops = {loop.name: loop for loop in Loop.objects.all()}
    status_by_name = {row.name: row.status for row in LoopState.objects.all()}
    return tuple(
        _loop_row(loop, status_by_name.get(verdict.name, LoopStatus.ENABLED.value), verdict)
        for verdict in effective_verdicts()
        if (loop := loops.get(verdict.name)) is not None
    )


def _loop_row(loop: Loop, status: str, verdict: LoopVerdict) -> LoopRow:
    return LoopRow(
        name=loop.name,
        description=loop.description,
        enabled=loop.enabled,
        status=status,
        effective=verdict.admitted,
        deciding_layer=_deciding_layer(verdict, enabled=loop.enabled, status=status),
        cadence_label=loop.cadence_label,
    )


def _deciding_layer(verdict: LoopVerdict, *, enabled: bool, status: str) -> str:
    """Which control layer decides the loop's verdict — answers "why isn't it running".

    Reads the shared verdict's ``layer`` so the precedence mirrors the resolver
    exactly: an L4 ``LoopState`` hold (paused/disabled) always wins, then the active
    preset's L3 override / L2 schedule mask (#3159), else the base L1 ``Loop.enabled``.
    """
    if verdict.layer == "hold":
        return "L4 hold — paused" if status == LoopStatus.PAUSED.value else "L4 hold — disabled"
    if verdict.layer == "override":
        return f"L3 override — {_preset_effect(verdict)}"
    if verdict.layer == "schedule":
        return f"L2 schedule — {_preset_effect(verdict)}"
    if not enabled:
        return "L1 — Loop.enabled off"
    return "L1 — enabled"


def _preset_effect(verdict: LoopVerdict) -> str:
    """How the active preset flipped this loop: ``masked`` (forced off) or ``forced-on``."""
    return "masked" if not verdict.admitted else "forced-on"


def apply_loop_action(action: str, name: str) -> str:
    """Apply a control verb to *name* via the same manager method the CLI uses; return the landed status.

    Refuses an unknown action or a name with no ``Loop`` row (mirroring the
    command's ``_require_known_loop`` guard) so a typo can never silently pause
    nothing. ``pause`` calls ``LoopState.objects.pause`` (the reversible hold);
    resume/disable/enable call the paired ``LoopManager`` verbs that move both
    planes. Raises :class:`LoopActionError` on a bad action/name.
    """
    if action not in LOOP_ACTIONS:
        msg = f"unknown loop action {action!r}"
        raise LoopActionError(msg)
    if not Loop.objects.filter(name=name).exists():
        msg = f"no loop named {name!r}"
        raise LoopActionError(msg)

    if action == "pause":
        LoopState.objects.pause(name)
    elif action == "resume":
        Loop.objects.resume(name)
    elif action == "disable":
        Loop.objects.disable(name)
    else:
        Loop.objects.enable(name)
    _reconcile_timers()
    return LoopState.objects.status_of(name).value


def _reconcile_timers() -> None:
    """Best-effort loop-timer reconcile after a control change (mirrors the CLI path)."""
    try:
        # deferred + best-effort like the loop_state command: a reconcile failure never fails the control write.
        from teatree.loops.timer_reconciler import ensure_loop_timers  # noqa: PLC0415 — deferred best-effort reconcile

        ensure_loop_timers()
    except Exception:
        logger.debug("ensure_loop_timers after dash loop-state change failed — reconciler will catch up", exc_info=True)


class LoopActionError(ValueError):
    """A dashboard loop-control POST named an unknown action or loop."""
