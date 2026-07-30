"""Governor-gated admission for the HEADLESS lane (#3644 / F9).

The adaptive admission governor (:mod:`teatree.core.admission_governor`) was
consulted ONLY by the interactive ``/loop`` claim budget
(:func:`teatree.loop.admission.governor_verdict`), yet the measured congestion
collapse — 7,785 attempts at a 2.9% success rate — was on the HEADLESS lane.
This wires the SAME pure :func:`~teatree.core.admission_governor.decide_admission`
into the headless admission chokepoints — the post_save auto-enqueue, the drain
safety net, and issue intake — so a DENY verdict (weekly quota spent, 5h window
spent, machine load over the watermark, or the live headless-agent count at the
governor's ceiling) refuses a NEW headless admission with a VISIBLE log.

It lives in ``teatree.core`` (not ``teatree.loop``) so the core chokepoints can
consult it without a backwards dependency edge; the loop-side ``governor_verdict``
is the richer interactive variant carrying the brake-hysteresis sidecar. Both
route through the one pure decision function, so the two lanes can never diverge
on the quota/machine/ceiling verdict.

Fail-OPEN by construction: the kill-switch (``admission_governor_enabled`` false)
or any signal-read failure returns ``None`` (admit) — a governor that cannot read
its own signals must never wedge the factory. A refusal is never silent: this is
the only seam that returns a DENY reason, and every caller logs it at WARNING.
"""

import logging

from teatree.core.admission_governor import decide_admission, governor_enabled, read_machine_signal, read_quota_signal

logger = logging.getLogger(__name__)


def headless_admission_denied_reason() -> str | None:
    """The governor's reason to DENY one more headless admission, or ``None`` to admit.

    Consults the pure :func:`decide_admission` on the live quota + machine
    signals, then compares the live headless-agent count against the governor's
    ceiling. Returns the DENY ``reason`` when the governor brakes outright or the
    live count is at/over the ceiling; ``None`` when admission is healthy, the
    kill-switch is off, or a signal read raised (fail-open).
    """
    if not governor_enabled():
        return None
    from teatree.core.models import Task  # noqa: PLC0415 — deferred: Django app-registry read at call time

    try:
        decision = decide_admission(
            quota=read_quota_signal(),
            machine=read_machine_signal(),
            static_ceiling=None,
        )
        live = Task.objects.live_headless_agent_count()
    except Exception:
        logger.exception("headless admission governor probe failed — admitting (fail-open)")
        return None
    if not decision.admit:
        return decision.reason
    if decision.ceiling is not None and live >= decision.ceiling:
        return f"live headless agents {live} at/over governor ceiling {decision.ceiling}"
    return None


__all__ = ["headless_admission_denied_reason"]
