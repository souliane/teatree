"""Governor-gated admission for the agent lane (#3644 / F9, #4098).

The adaptive admission governor (:mod:`teatree.core.admission_governor`) was
consulted ONLY by the interactive ``/loop`` claim budget
(:func:`teatree.loop.admission.governor_verdict`), yet the measured congestion
collapse — 7,785 attempts at a 2.9% success rate — was on the agent lane.
This wires the SAME pure :func:`~teatree.core.admission_governor.decide_admission`
into the headless admission chokepoints — the post_save auto-enqueue, the drain
safety net, and issue intake — so a DENY verdict (weekly quota spent, 5h window
spent, machine load over the watermark, or the live headless-agent count at the
governor's ceiling) refuses a NEW headless admission with a VISIBLE log.

**The verdict is per phase COST CLASS, never one answer for the whole queue
(#4098).** A single verdict refused a 3-minute ``reviewing`` task on
exactly the brake a 272-turn ``coding`` agent had caused. The starvation order is
the point: reviewing and shipping are what DRAIN the box — a merged PR retires a
worktree and its agent — so refusing them alongside the expensive class removed
the only work that would have relieved the pressure and the brake held itself on
(measured 2026-08-03: 3h22m of denied admissions, zero review verdicts, no
merges). :func:`agent_admission_verdict` therefore probes ONCE and resolves
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
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from teatree.core.admission_governor import (
    MachineBrake,
    decide_admission,
    governor_enabled,
    read_machine_signal,
    read_quota_signal,
)
from teatree.core.modelkit.phases import PhaseCost, phase_cost

if TYPE_CHECKING:
    from teatree.core.models import Task

logger = logging.getLogger(__name__)


@dataclass
class AgentAdmission:
    """One governor probe, resolved per phase cost class (#4098).

    Plain fields rather than a per-phase callable: the verdict is a value a caller holds
    across a whole drain, asking it once per row, which is what makes the classification
    affordable at all — nothing changes between iterations of that loop, so re-probing
    per row would return the same answer N times at N times the cost.

    ``cheap_headroom`` is the ceiling minus the lane's occupancy at probe time — what
    keeps the exemption from becoming a second unbounded lane. Callers book every
    admission through :meth:`admit`, which BOTH takes the row's durable seat
    (``Task.admitted_at``, so the next probe's occupancy read sees it) and decrements
    this local headroom (so a caller mid-pass need not re-probe to stay bounded). The
    two chokepoints have different shapes — the drain is a loop holding one verdict,
    ``post_save`` is one row with a fresh verdict each time — so the durable seat is
    what they actually share; the local headroom only covers the span of a single pass,
    between the probe that computed it and the seats it is taking. ``None`` is
    UNBOUNDED, reached only where the governor has no opinion at all: the kill-switch,
    the fail-open path, and the zero-ceiling rollback under which cheap simply follows
    the expensive verdict.

    ``cheap_ceiling`` is the same bound the probe measured against, carried so the seat
    write can re-check it (#4125). Headroom alone is a number computed BEFORE the write
    and private to one process, which is exactly why it could not stop two of them.
    """

    expensive_denied: str | None
    cheap_denied: str | None
    cheap_headroom: int | None = None
    cheap_ceiling: int | None = None
    seats_released: int = 0
    _cheap_admitted: int = 0
    _announced: set[str] = field(default_factory=set)

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

    def admit(self, task_pk: int, phase: str, *, at: str) -> bool:
        """True when *task_pk* now holds a seat and may be dispatched — else refuse, at *at*.

        The single seam every chokepoint routes its admission through, so no caller can
        dispatch a row it never booked: a chokepoint that enqueued without the durable
        seat would leave its own admission invisible to every later probe, which is
        exactly how a one-row-at-a-time burst outran the ceiling. The seat is therefore
        taken BEFORE the dispatch, and a dispatch that then fails costs at most one
        :data:`~teatree.core.managers.ADMITTED_INFLIGHT_WINDOW` of under-admission — the
        direction that is safe, and what that window already exists to recover.

        Deciding, booking and announcing are ONE call so a chokepoint cannot skip a row
        quietly. A reason :meth:`log_denials` already announced for the whole pass drops
        to DEBUG rather than repeating per held row: on the measured shape (18 rows, one
        braked drain) that repetition was 19 lines every cadence saying one thing.
        """
        denied = self.denied_reason(phase) or self._book(task_pk, phase)
        if denied is None:
            return True
        level = logging.DEBUG if denied in self._announced else logging.WARNING
        logger.log(level, "Governor DENIED %s of task %s: %s (staying PENDING)", at, task_pk, denied)
        return False

    def _book(self, task_pk: int, phase: str) -> str | None:
        """Take *task_pk*'s durable seat — ``None`` when granted, else why it was refused.

        The expensive class has no lane width of its own, so only the one-seat-per-row
        rule can refuse it; the cheap class hands its ceiling to the write, which
        re-checks occupancy there rather than trusting this verdict's probe (#4125).
        """
        cheap = phase_cost(phase) is PhaseCost.CHEAP
        if not _task_model().objects.record_admission(task_pk, cheap_ceiling=self.cheap_ceiling if cheap else None):
            if not cheap:
                return "already dispatched this window"
            return "no cheap-phase lane seat: already dispatched this window, or a racer took the last one"
        if cheap:
            self._cheap_admitted += 1
        return None

    def log_denials(self) -> None:
        """Announce every class this verdict refuses, one WARNING line each.

        Lives on the verdict rather than at each chokepoint so a refusal is worded
        identically wherever it is taken, and so a class added to :class:`PhaseCost`
        cannot acquire a caller that forgets to report it. What is announced here is
        remembered, so :meth:`admit` can hold a row against it without saying it again.

        A released seat is announced alongside: it is the state in which the ceiling is
        SOFT, and it arrives with no refusal of its own to carry it.
        """
        for cost in PhaseCost:
            denied = self.denied_for(cost)
            if denied is not None:
                self._announced.add(denied)
                logger.warning("Governor DENIED headless admission of %s work: %s (rows stay queued)", cost, denied)
        if self.seats_released:
            logger.warning(
                "Cheap lane released %s unclaimed seat(s) — the runner backlog outran the seat window, "
                "so the ceiling is soft until it drains",
                self.seats_released,
            )


def _task_model() -> "type[Task]":
    """The ``Task`` model, resolved at call time — the module's ONE intra-core edge.

    Both the occupancy probe and the admission stamp need it, so the deferred import
    lives here once rather than being restated in each: one function-scoped edge hidden
    from tach's acyclic guard, not two.
    """
    from teatree.core.models import Task  # noqa: PLC0415 — deferred: Django app-registry read at call time

    return Task


def _admit_all() -> AgentAdmission:
    """Admit everything, unbounded — the kill-switch answer and the fail-open answer alike."""
    return AgentAdmission(expensive_denied=None, cheap_denied=None)


def _cheap_lane_ceiling() -> int:
    """The operator's bound on concurrently-admitted cheap agents; ``0`` disables the lane.

    The exemption must never become a second unbounded lane — that is exactly what
    over-admitting the expensive class cost (#4097). Only the WIDTH is data: the
    taxonomy is harness-owned, so no config row can move ``coding`` into the exempt
    class.
    """
    from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: avoids a config import cycle

    return max(0, int(get_effective_settings().cheap_phase_admission_ceiling))


def _ceiling_denial(ceiling: int, occupied: int, *, lane: str = "") -> str | None:
    if occupied < ceiling:
        return None
    if lane:
        return f"{lane} lane occupancy {occupied} at/over the {lane} ceiling {ceiling}"
    return f"live headless agents {occupied} at/over governor ceiling {ceiling}"


def agent_admission_verdict() -> AgentAdmission:
    """Probe the governor ONCE and resolve the verdict for both phase cost classes.

    The EXPENSIVE class is the pre-#4098 answer verbatim: the pure decision over the
    live quota + machine signals, then the live headless-agent count against the
    governor's ceiling. The CHEAP class re-runs the SAME pure decision with the machine
    brake lifted — the token brakes still refuse it, because a review burns quota like
    anything else — bounded by its own small ceiling over the lane's OCCUPANCY: the
    running cheap agents plus the cheap rows already handed to the runner. Counting the
    latter is what lets a chokepoint see admissions it (or the other chokepoint) just
    made, so the bound is one number in the database rather than per-caller state. A
    ceiling of ``0`` collapses cheap onto expensive: the rollback lever.

    ``static_ceiling=None`` says the operator has configured no cap for THIS lane,
    which is not the same as no cap at all: the governor always derives one from the
    signals it has, so a stale quota cache — the steady state, since healthy health
    rows expire in minutes and are written only reactively — bounds the lane at the
    machine-derived ceiling rather than leaving it unbounded (#4097).
    """
    if not governor_enabled():
        return _admit_all()
    task_model = _task_model()
    try:
        quota = read_quota_signal()
        machine = read_machine_signal()
        cheap_ceiling = _cheap_lane_ceiling()
        decision = decide_admission(quota=quota, machine=machine, static_ceiling=None)
        live = task_model.objects.claimed_agent_count()
        expensive = decision.reason if not decision.admit else _ceiling_denial(decision.ceiling, live)
        if cheap_ceiling <= 0:
            return AgentAdmission(expensive_denied=expensive, cheap_denied=expensive)
        exempt = decide_admission(
            quota=quota, machine=machine, static_ceiling=None, load_brake=MachineBrake(applies=False)
        )
        cheap_occupancy = task_model.objects.cheap_lane_occupancy()
        cheap = (
            exempt.reason if not exempt.admit else _ceiling_denial(cheap_ceiling, cheap_occupancy, lane="cheap-phase")
        )
        seats_released = task_model.objects.cheap_lane_seats_released()
    except Exception:
        logger.exception("headless admission governor probe failed — admitting (fail-open)")
        return _admit_all()
    return AgentAdmission(
        expensive_denied=expensive,
        cheap_denied=cheap,
        cheap_headroom=max(0, cheap_ceiling - cheap_occupancy),
        cheap_ceiling=cheap_ceiling,
        seats_released=seats_released,
    )


def agent_admission_denied_reason(phase: str = "") -> str | None:
    """The governor's reason to DENY one more headless admission of *phase*, or ``None``.

    The single-shot wrapper for a caller admitting ONE unit of work: it probes and
    resolves in one call. A caller walking a queue holds a
    :func:`agent_admission_verdict` instead, so N rows still cost one probe.
    ``phase`` omitted is the EXPENSIVE class — the pre-#4098 verdict verbatim.
    """
    return agent_admission_verdict().denied_reason(phase)


__all__ = ["AgentAdmission", "agent_admission_denied_reason", "agent_admission_verdict"]
