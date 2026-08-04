"""Governor-gated admission for the INTERACTIVE dispatch path (#4107).

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

The ceiling is compared against the TOTAL live-agent population
(:func:`live_agent_count` — CLAIMED with an unexpired lease across BOTH execution
targets), because a new interactive agent adds to the same box the headless ones
run on. ``apply_ceiling=False`` is for a caller whose own lane ALREADY admitted
it (a sub-agent's onward dispatch): re-clamping it against a ceiling its own
claim is counted in would deadlock it against itself. The brakes still apply
there — box saturation is real whoever dispatched.

Fail-OPEN by construction: the kill-switch or any signal-read failure returns
``None`` (admit). A governor that cannot read its own signals must never wedge
the session. A refusal is never silent — the caller emits the returned reason.
"""

import logging

from teatree.core.admission_governor import decide_admission, governor_enabled, read_machine_signal, read_quota_signal

logger = logging.getLogger(__name__)


def live_agent_count() -> int:
    """Live agents in flight across BOTH lanes — CLAIMED with an unexpired lease.

    :meth:`~teatree.core.managers.TaskQuerySet.live_headless_agent_count` counts
    only the headless half, which is the right divisor for the per-agent test
    worker budget but the wrong number for THIS ceiling: an interactive dispatch
    adds to the population both halves share. ``active_claims`` is the repo's
    single in-flight predicate, so the two can never drift on what "live" means.
    """
    from teatree.core.models import Task  # noqa: PLC0415 — deferred: Django app-registry read at call time

    return Task.objects.active_claims().count()


def dispatch_admission_denied_reason(*, apply_ceiling: bool = True) -> str | None:
    """The governor's reason to DENY one more interactive dispatch, or ``None`` to admit.

    Consults the pure :func:`decide_admission` on the live quota + machine
    signals, then — when *apply_ceiling* — compares the live agent count against
    the governor's ceiling. Returns the DENY ``reason`` when the governor brakes
    outright or the live count is at/over the ceiling; ``None`` when admission is
    healthy, the kill-switch is off, or a signal read raised (fail-open).
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
            return None
        live = live_agent_count()
    except Exception:
        logger.exception("dispatch admission governor probe failed — admitting (fail-open)")
        return None
    return f"live agents {live} at/over governor ceiling {decision.ceiling}" if live >= decision.ceiling else None


__all__ = ["dispatch_admission_denied_reason", "live_agent_count"]
