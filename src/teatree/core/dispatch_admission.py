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

**A `t3 loop claim-next` claim and its own FIRST dispatch are one agent, not
two** (#4129 review). A loop tick claims a ``Task`` (durable,
``claimed_by_session``) and then dispatches THAT unit's sub-agent through the
harness, which seats it (durable, ``session_id``) — both rows now name the same
live agent. Two separate corrections, at two separate call sites:

- :func:`live_agent_count` (reporting) sums each session's INTERACTIVE claims
    and live seats with ``max``, not addition — a session with 4 claims and 1 seat
    is 4 live agents, not 5, because the seat is one of the four, not a fifth.
- The ceiling CHECK inside :func:`dispatch_admission_denied_reason` grants a
    session at most ONE exemption, ever: only its FIRST dispatch, while it still
    holds zero seats, is let through on its own claim's credit. Every dispatch
    after that is fully ceiling-gated with no credit at all — ``session_id`` alone
    cannot tell the gate WHICH of a session's claims a given dispatch is for, so a
    bigger exemption would let a burst of cheap claims buy a burst of dispatches
    the ceiling was written to stop.

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


def _session_has_unseated_claim(session_id: str) -> bool:
    """True iff *session_id* holds an active INTERACTIVE claim and no live seat yet.

    The narrow, ONE-SHOT exemption for the #4129 review's loop-claim pattern: a
    session's own ``Task`` claim exempts only its FIRST dispatch from the ceiling
    (that claim and the resulting seat name the same agent). Any FURTHER dispatch
    from the same session is fully ceiling-gated with no credit at all, even while
    other claims of its own sit unseated — ``session_id`` alone cannot tell the
    gate WHICH claim a given dispatch is for, so a wider exemption would let a
    burst of cheap claims buy a burst of dispatches the ceiling exists to stop.
    """
    if not session_id:
        return False
    tasks, seats = _models()
    if seats.objects.live_seats().filter(session_id=session_id).exists():
        return False
    return (
        tasks.objects.active_claims()
        .filter(
            execution_target=tasks.ExecutionTarget.INTERACTIVE,
            claimed_by_session=session_id,
        )
        .exists()
    )


def live_agent_count() -> int:
    """Live agents in flight across BOTH lanes — durable claims PLUS interactive seats.

    The halves are counted separately because they are RECORDED separately: the factory
    lanes stamp a ``Task`` row and the interactive lane, which creates no ``Task`` at
    all, stamps a seat. Summing them is what makes this the box's whole agent
    population rather than the half that happens to be a work queue (#4129).

    A session's INTERACTIVE claims and its live seats are combined with ``max``,
    per session, not addition (#4129 review): a ``t3 loop claim-next`` claim and
    that SAME unit's own dispatch are ONE agent, so a session with 4 claims and 1
    seat is 4 live agents, not 5 — the seat is one of the four claims made
    concrete, not a fifth agent on top of them. A claim or seat with no
    attributable session (blank ``claimed_by_session`` / ``session_id``) is
    counted on its own, never merged with an unrelated blank-session row.
    """
    tasks, seats = _models()
    claims = tasks.objects.active_claims()
    headless = claims.filter(execution_target=tasks.ExecutionTarget.HEADLESS).count()

    claims_by_session: dict[str, int] = {}
    unattributed_claims = 0
    interactive_sessions = claims.filter(execution_target=tasks.ExecutionTarget.INTERACTIVE).values_list(
        "claimed_by_session", flat=True
    )
    for session_id in interactive_sessions:
        if session_id:
            claims_by_session[session_id] = claims_by_session.get(session_id, 0) + 1
        else:
            unattributed_claims += 1

    seats_by_session: dict[str, int] = {}
    unattributed_seats = 0
    for session_id in seats.objects.live_seats().values_list("session_id", flat=True):
        if session_id:
            seats_by_session[session_id] = seats_by_session.get(session_id, 0) + 1
        else:
            unattributed_seats += 1

    sessions = set(claims_by_session) | set(seats_by_session)
    deduped = sum(max(claims_by_session.get(sid, 0), seats_by_session.get(sid, 0)) for sid in sessions)
    return headless + unattributed_claims + unattributed_seats + deduped


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
        other_agents = _claimed_agent_count()
        if _session_has_unseated_claim(session_id):
            other_agents -= 1
        if _seats().claim_seat(session_id=session_id, ceiling=decision.ceiling, other_agents=other_agents):
            return None
        live = live_agent_count()
    except Exception:
        logger.exception("dispatch admission governor probe failed — admitting (fail-open)")
        return None
    return f"live agents {live} at/over governor ceiling {decision.ceiling}"


__all__ = ["dispatch_admission_denied_reason", "live_agent_count", "release_interactive_dispatch"]
