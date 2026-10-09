"""Self-heal detector for a loop fleet a posture stopped and nobody restarted.

A preset admitting zero loops is a sanctioned, documented stop. What has no owner is the
stop nobody remembers taking: ``loop_timer`` step 0 returns ``halted`` before the
admission check and re-enqueues no successor, while ``ensure_loop_timers`` — which the
posture does not gate — re-heads each drained chain every five minutes. So every timer
fire keeps going RUNNING -> SUCCESSFUL, the READY rows stay fresh, the worker keeps taking
the flock, and the box reports a full heartbeat while producing nothing. A fleet ran that
way for a fortnight.

Every sibling detector in :mod:`teatree.cli.doctor.self_heal` is blind to it by
construction: ``_check_worker_running``, ``_check_compose_stack`` and
``_check_loop_worker_alive`` are each gated on the fleet admitting work, and
``_check_stale_loop_timer`` measures the timers the reconciler keeps refreshing rather
than the anchors that stopped moving. So the whole doctor surface was silent.

The finding is the CONJUNCTION, never the posture alone — a gate that reddens the moment
an operator picks a documented posture is one people learn to ignore. Both halves come
from the single :func:`~teatree.loops.loop_staleness.loop_health` reading ``t3 worker
status`` exits on, so this detector and that command can never hold two opinions about
whether the fleet is ticking.
"""

from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from teatree.loops.loop_staleness import LoopHealth


def check_frozen_fleet_under_kill_switch() -> bool:
    """FAIL when the active preset admits zero loops and every measured loop is past its cadence."""
    try:
        health = _loop_health()
    except Exception as exc:  # noqa: BLE001 — a self-heal probe must never crash the doctor run
        typer.echo(f"WARN  Frozen-fleet posture check crashed: {exc.__class__.__name__}: {exc}")
        return True
    if health.fleet_admits or not health.frozen_fleet:
        return True
    typer.echo(
        f"FAIL  the active preset {health.admission.mode!r} admits ZERO loops and every measured loop is "
        "behind its cadence — the fleet was stopped by a posture, not by a dead worker. Each `loop_timer` "
        "fire still goes RUNNING -> SUCCESSFUL having done nothing, so the heartbeat reads healthy while "
        "the factory produces nothing. Pick a posture that runs something (`t3 loop preset use <name> "
        '--reason "<why>"`), or confirm the stop is intended.'
    )
    return False


def _loop_health() -> "LoopHealth":
    from django.utils import timezone  # noqa: PLC0415 — deferred: Django import at call time

    from teatree.loops.loop_staleness import loop_health  # noqa: PLC0415 — deferred: pulls the loop machinery

    return loop_health(timezone.now())
