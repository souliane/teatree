"""Governor-gated admission for the HEADLESS lane (#3644 / F9, #4098).

The adaptive admission governor (:mod:`teatree.core.admission_governor`) was
consulted ONLY by the interactive ``/loop`` claim budget
(:func:`teatree.loop.admission.governor_verdict`), yet the measured congestion
collapse — 7,785 attempts at a 2.9% success rate — was on the HEADLESS lane.
This wires the SAME pure :func:`~teatree.core.admission_governor.decide_admission`
into the headless admission chokepoints — the post_save auto-enqueue, the drain
safety net, and issue intake — so a DENY verdict (weekly quota spent, 5h window
spent, machine load over the watermark, or the live headless-agent count at the
governor's ceiling) refuses a NEW headless admission with a VISIBLE log.

**The verdict is per phase COST CLASS, never one answer for the whole queue
(#4098).** A single verdict refused a 3-minute read-only ``reviewing`` task on
exactly the brake a 272-turn ``coding`` agent had caused. The starvation order is
the point: reviewing and shipping are what DRAIN the box — a merged PR retires a
worktree and its agent — so refusing them alongside the expensive class removed
the only work that would have relieved the pressure and the brake held itself on
(measured 2026-08-03: 3h22m of denied admissions, zero review verdicts, no
merges). :func:`headless_admission_verdict` therefore probes ONCE and resolves
that probe per :class:`~teatree.core.modelkit.phases.PhaseCost`, so a drain costs
one probe however many rows it walks.

It lives in ``teatree.core`` (not ``teatree.loop``) so the core chokepoints can
consult it without a backwards dependency edge; the loop-side ``governor_verdict``
is the richer interactive variant carrying the brake-hysteresis sidecar. Both
route through the one pure decision function, so the two lanes can never diverge
on the quota/machine/ceiling verdict.

Fail-OPEN by construction: the kill-switch (``admission_governor_enabled`` false)
or any signal-read failure admits BOTH classes — a governor that cannot read its
own signals must never wedge the factory. A refusal is never silent: this is the
only seam that returns a DENY reason, and every caller logs it at WARNING.
"""

import logging
from dataclasses import dataclass

from teatree.core.admission_governor import (
    MachineBrake,
    decide_admission,
    governor_enabled,
    read_machine_signal,
    read_quota_signal,
)
from teatree.core.modelkit.phases import PhaseCost, phase_cost

logger = logging.getLogger(__name__)


@dataclass
class HeadlessAdmission:
    """One governor probe, resolved per phase cost class (#4098).

    Plain fields rather than a per-phase callable: the verdict is a value a caller holds
    across a whole drain, asking it once per row, which is what makes the classification
    affordable at all — nothing changes between iterations of that loop, so re-probing
    per row would return the same answer N times at N times the cost.

    ``cheap_headroom`` is what keeps the exemption from becoming a second unbounded lane
    WITHIN one pass. The lane's ceiling counts LIVE (claimed) cheap agents, and a row
    this drain enqueues is still PENDING, so a re-probe cannot see it — a single pass
    would otherwise admit every pending cheap row at once however small the ceiling.
    Callers report each admission through :meth:`record_admitted` and the headroom
    decrements locally, at no extra probe. ``None`` is UNBOUNDED, reached only where the
    governor has no opinion at all: the kill-switch, the fail-open path, and the
    zero-ceiling rollback under which cheap simply follows the expensive verdict.
    """

    expensive_denied: str | None
    cheap_denied: str | None
    cheap_headroom: int | None = None
    _cheap_admitted: int = 0

    def denied_for(self, cost: PhaseCost) -> str | None:
        """The reason to refuse one more admission of *cost*, or ``None`` to admit."""
        if cost is not PhaseCost.CHEAP:
            return self.expensive_denied
        if self.cheap_denied is not None:
            return self.cheap_denied
        if self.cheap_headroom is not None and self._cheap_admitted >= self.cheap_headroom:
            return f"cheap-phase headroom spent this pass ({self._cheap_admitted} admitted, lane ceiling reached)"
        return None

    def denied_reason(self, phase: str = "") -> str | None:
        """The reason to refuse one more admission of *phase*, or ``None`` to admit.

        A blank or unregistered phase classifies EXPENSIVE, so a caller that cannot
        name its phase gets the braked answer rather than the exemption.
        """
        return self.denied_for(phase_cost(phase))

    def record_admitted(self, phase: str) -> None:
        """Spend one of the cheap lane's remaining admissions, when *phase* is cheap."""
        if phase_cost(phase) is PhaseCost.CHEAP:
            self._cheap_admitted += 1

    def log_denials(self) -> None:
        """Announce every class this verdict refuses, one WARNING line each.

        Lives on the verdict rather than at each chokepoint so a refusal is worded
        identically wherever it is taken, and so a class added to :class:`PhaseCost`
        cannot acquire a caller that forgets to report it.
        """
        for cost in PhaseCost:
            denied = self.denied_for(cost)
            if denied is not None:
                logger.warning("Governor DENIED headless admission of %s work: %s (rows stay queued)", cost, denied)


def _admit_all() -> HeadlessAdmission:
    """Admit everything, unbounded — the kill-switch answer and the fail-open answer alike."""
    return HeadlessAdmission(expensive_denied=None, cheap_denied=None)


def _cheap_lane_ceiling() -> int:
    """The operator's bound on concurrently-admitted cheap agents; ``0`` disables the lane.

    The exemption must never become a second unbounded lane — that is exactly what
    over-admitting the expensive class cost (#4097). Only the WIDTH is data: the
    taxonomy is harness-owned, so no config row can move ``coding`` into the exempt
    class.
    """
    from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: avoids a config import cycle

    return max(0, int(get_effective_settings().cheap_phase_admission_ceiling))


def _ceiling_denial(ceiling: int | None, live: int, *, lane: str = "") -> str | None:
    if ceiling is None or live < ceiling:
        return None
    if lane:
        return f"live {lane} agents {live} at/over the {lane} ceiling {ceiling}"
    return f"live headless agents {live} at/over governor ceiling {ceiling}"


def headless_admission_verdict() -> HeadlessAdmission:
    """Probe the governor ONCE and resolve the verdict for both phase cost classes.

    The EXPENSIVE class is the pre-#4098 answer verbatim: the pure decision over the
    live quota + machine signals, then the live headless-agent count against the
    governor's ceiling. The CHEAP class re-runs the SAME pure decision with the machine
    brake lifted — the token brakes still refuse it, because a review burns quota like
    anything else — bounded by its own small ceiling over the live cheap agents alone,
    and by the headroom that ceiling leaves for the rest of the caller's pass. A ceiling
    of ``0`` collapses cheap onto expensive: the rollback lever.
    """
    if not governor_enabled():
        return _admit_all()
    from teatree.core.models import Task  # noqa: PLC0415 — deferred: Django app-registry read at call time

    try:
        quota = read_quota_signal()
        machine = read_machine_signal()
        cheap_ceiling = _cheap_lane_ceiling()
        decision = decide_admission(quota=quota, machine=machine, static_ceiling=None)
        live = Task.objects.live_headless_agent_count()
        expensive = decision.reason if not decision.admit else _ceiling_denial(decision.ceiling, live)
        if cheap_ceiling <= 0:
            return HeadlessAdmission(expensive_denied=expensive, cheap_denied=expensive)
        exempt = decide_admission(
            quota=quota, machine=machine, static_ceiling=None, load_brake=MachineBrake(applies=False)
        )
        live_cheap = Task.objects.live_cheap_headless_agent_count()
        cheap = exempt.reason if not exempt.admit else _ceiling_denial(cheap_ceiling, live_cheap, lane="cheap-phase")
    except Exception:
        logger.exception("headless admission governor probe failed — admitting (fail-open)")
        return _admit_all()
    return HeadlessAdmission(
        expensive_denied=expensive,
        cheap_denied=cheap,
        cheap_headroom=max(0, cheap_ceiling - live_cheap),
    )


def headless_admission_denied_reason(phase: str = "") -> str | None:
    """The governor's reason to DENY one more headless admission of *phase*, or ``None``.

    The single-shot wrapper for a caller admitting ONE unit of work: it probes and
    resolves in one call. A caller walking a queue holds a
    :func:`headless_admission_verdict` instead, so N rows still cost one probe.
    ``phase`` omitted is the EXPENSIVE class — the pre-#4098 verdict verbatim.
    """
    return headless_admission_verdict().denied_reason(phase)


__all__ = ["HeadlessAdmission", "headless_admission_denied_reason", "headless_admission_verdict"]
