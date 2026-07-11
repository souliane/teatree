"""Loop-control read model: each loop's effective verdict + the layer that decided it (#3162).

The dashboard drives the SAME two control planes the tick gates on — ``Loop.enabled``
and the durable ``LoopState`` hold — resolved through the one admission predicate
(``loop_state_admits``), never a second verdict. Until the presets layer (#3159)
lands the deciding layer is either the enabled flag (L1) or the durable hold (L4);
the write side (pause/resume/disable/enable) goes exclusively through the paired
atomic ``LoopManager`` verbs, so this module only reads.
"""

import logging
from dataclasses import dataclass

from teatree.config import get_effective_settings
from teatree.core.availability import resolve_mode
from teatree.core.models.config_setting import ConfigSetting
from teatree.core.models.loop import Loop
from teatree.core.models.loop_state import LoopState, LoopStatus
from teatree.loop.loop_state_db import loop_state_admits

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

    One bulk read of the durable hold table, joined in Python to the loop rows,
    so the table renders in two queries regardless of loop count.
    """
    held = {row.name: row.status for row in LoopState.objects.all()}
    rows = [_loop_row(loop, held.get(loop.name, LoopStatus.ENABLED.value)) for loop in Loop.objects.all()]
    return tuple(rows)


def _loop_row(loop: Loop, status: str) -> LoopRow:
    held = status != LoopStatus.ENABLED.value
    effective = loop_state_admits(configured_enabled=loop.enabled, held=held)
    return LoopRow(
        name=loop.name,
        description=loop.description,
        enabled=loop.enabled,
        status=status,
        effective=effective,
        deciding_layer=_deciding_layer(enabled=loop.enabled, status=status),
        cadence_label=loop.cadence_label,
    )


def _deciding_layer(*, enabled: bool, status: str) -> str:
    """Which control layer decides the loop's verdict — answers "why isn't it running".

    Precedence mirrors the tick's resolution order: the durable ``LoopState`` hold
    (the future L4) is checked first, then the configured ``Loop.enabled`` flag
    (L1). The presets layers (#3159 L3 override / L2 schedule) slot between them
    once that lands.
    """
    if status == LoopStatus.PAUSED.value:
        return "L4 hold — paused"
    if status == LoopStatus.DISABLED.value:
        return "L4 hold — disabled"
    if not enabled:
        return "L1 — Loop.enabled off"
    return "L1 — enabled"


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
