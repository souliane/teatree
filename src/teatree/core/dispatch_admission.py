"""Governor-gated admission for the INTERACTIVE dispatch path (#4107, #4129).

:func:`~teatree.core.admission_governor.decide_admission` is described as "one
chokepoint every dispatcher asks", and had exactly two callers — both factory
lanes (:mod:`teatree.core.headless_admission`, :mod:`teatree.loop.admission`).
An orchestrator session dispatching work through the harness ``Agent``/``Task``
tool asked nothing: no ceiling, no load brake. Every guard the governor provides
therefore governed the headless population only, while the agent population on
the box is the SUM of both — measured at load 58 on 8 cores with 1 GB free while
the factory's own ``issue_implementer_max_concurrent = 3`` was in force.

This is the third caller. It routes through the SAME pure decision function, so
the three lanes can never diverge on the quota/machine/ceiling verdict, and it
reuses the ``admission_governor_enabled`` kill-switch rather than minting a
second flag.

**The ceiling counts the population it admits (#4129).** It was compared against
``Task.objects.active_claims()`` alone — durable, and the right shape — but an
ad-hoc interactive dispatch creates no ``Task`` row at all, so the lane could not
see its own admissions: N rapid dispatches each read the same live count and
every one passed. :class:`~teatree.core.models.InteractiveDispatch` is this
lane's ``Task.admitted_at`` — a seat taken AT the gate, counted by every later
probe, handed back on ``SubagentStop`` with the window as the backstop.

``apply_ceiling=False`` is for a caller whose own lane ALREADY admitted it (a
sub-agent's onward dispatch, the ``TaskCreated`` fan-out): re-clamping it against
a ceiling its own claim is counted in would deadlock it against itself. It still
takes a seat, because it puts an agent on the box either way and the arm that
DOES clamp has to see it — otherwise the burstiest path stays invisible to the
only bounded one, and the two gaps compound. The brakes apply there too: box
saturation is real whoever dispatched.

Fail-OPEN by construction: the kill-switch, any signal-read failure, and a seat
write that raises all return ``None`` (admit). A governor that cannot read its
own signals — or write its own ledger — must never wedge the session. A refusal
is never silent: the caller emits the returned reason.
"""

import logging
from typing import TYPE_CHECKING

from teatree.core.admission_governor import decide_admission, governor_enabled, read_machine_signal, read_quota_signal

if TYPE_CHECKING:
    from teatree.core.models import InteractiveDispatch, InteractiveDispatchManager, Task

logger = logging.getLogger(__name__)


def _models() -> "tuple[type[Task], type[InteractiveDispatch]]":
    """``Task`` and the seat ledger, resolved at call time — the module's ONE core edge.

    The claim count needs one and every seat operation the other, so the deferred import
    lives here once rather than being restated in each: one function-scoped edge hidden
    from tach's acyclic guard, not two (the ``headless_admission._task_model`` shape).
    """
    from teatree.core.models import InteractiveDispatch, Task  # noqa: PLC0415 — deferred: app-registry read

    return Task, InteractiveDispatch


def _seats() -> "InteractiveDispatchManager":
    """The ledger of interactive dispatch seats."""
    _tasks, seats = _models()
    return seats.objects


def _claimed_agent_count() -> int:
    """Live agents holding a durable ``Task`` claim — CLAIMED with an unexpired lease.

    :meth:`~teatree.core.managers.TaskQuerySet.live_headless_agent_count` counts
    only the headless half, which is the right divisor for the per-agent test
    worker budget but the wrong number for THIS ceiling: an interactive dispatch
    adds to the population both halves share. ``active_claims`` is the repo's
    single in-flight predicate, so the two can never drift on what "live" means.
    """
    tasks, _seat_ledger = _models()
    return tasks.objects.active_claims().count()


def live_agent_count() -> int:
    """Live agents in flight across BOTH lanes — durable claims PLUS interactive seats.

    The halves are counted separately because they are RECORDED separately: the factory
    lanes stamp a ``Task`` row and the interactive lane, which creates no ``Task`` at
    all, stamps a seat. Summing them is what makes this the box's whole agent
    population rather than the half that happens to be a work queue (#4129).
    """
    return _claimed_agent_count() + _seats().live_seats().count()


def release_interactive_dispatch(*, session_id: str, agent_id: str) -> bool:
    """Hand *session_id*'s oldest live seat back on *agent_id*'s termination.

    A seat's ordinary end. :data:`~teatree.core.models.SEAT_WINDOW` is only the backstop
    for a release that never arrives, so without this the ceiling would bound a dispatch
    RATE rather than the live population it is written to bound.
    """
    return _seats().release_seat(session_id=session_id, agent_id=agent_id)


def dispatch_admission_denied_reason(*, apply_ceiling: bool = True, session_id: str = "") -> str | None:
    """The governor's reason to DENY one more interactive dispatch, or ``None`` to admit.

    Consults the pure :func:`decide_admission` on the live quota + machine
    signals, then — when *apply_ceiling* — claims this dispatch's seat against the
    governor's ceiling. Returns the DENY ``reason`` when the governor brakes
    outright or the seat was refused; ``None`` when admission is healthy, the
    kill-switch is off, or a probe raised (fail-open). A braked or refused
    dispatch holds no seat, so a refusal never narrows the lane it was refused by.
    """
    if not governor_enabled():
        return None
    try:
        decision = decide_admission(
            quota=read_quota_signal(),
            machine=read_machine_signal(),
            static_ceiling=None,
        )
        if not decision.admit:
            return decision.reason
        if not apply_ceiling:
            _seats().record_seat(session_id=session_id)
            return None
        if _seats().claim_seat(session_id=session_id, ceiling=decision.ceiling, other_agents=_claimed_agent_count()):
            return None
        live = live_agent_count()
    except Exception:
        logger.exception("dispatch admission governor probe failed — admitting (fail-open)")
        return None
    return f"live agents {live} at/over governor ceiling {decision.ceiling}"


__all__ = ["dispatch_admission_denied_reason", "live_agent_count", "release_interactive_dispatch"]
