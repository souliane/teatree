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

**A ceiling on the cheap class is not a reservation for it (#4374).**
``cheap_phase_admission_ceiling`` bounds how much of the box the draining class may
take; nothing kept any of it FOR that class, so the same stall returned by the opposite
route — expensive work held every slot for over half an hour with five reviewing rows
queued behind it and zero reviews running. Coding CREATES pull requests where reviewing
and shipping RETIRE them, so under first-come allocation the producing side can occupy
100% of the factory and the board can only grow. ``drain_slot_reservation`` carves slots
off the TOP of the governor's ceiling that only the cheap class may occupy, clamped so
the expensive class always keeps one.

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
    pressure_for,
    read_machine_signal,
    read_quota_signal,
)
from teatree.core.admission_pressure import AdmissionPressure, PressureBand
from teatree.core.modelkit.phases import PhaseCost, phase_cost

if TYPE_CHECKING:
    from teatree.core.models import Task

logger = logging.getLogger(__name__)


@dataclass
class LaneBound:
    """One cost class's lane width, and what a single pass has already spent of it.

    ``headroom`` is the ceiling minus the lane's occupancy at probe time — what keeps a
    class from becoming an unbounded lane. Callers book every admission through
    :meth:`AgentAdmission.admit`, which BOTH takes the row's durable seat
    (``Task.admitted_at``, so the next probe's occupancy read sees it) and decrements this
    local headroom (so a caller mid-pass need not re-probe to stay bounded). The two
    chokepoints have different shapes — the drain is a loop holding one verdict,
    ``post_save`` is one row with a fresh verdict each time — so the durable seat is what
    they actually share; the local headroom only covers the span of a single pass, between
    the probe that computed it and the seats it is taking. ``None`` is UNBOUNDED, reached
    only where the governor has no opinion at all: the kill-switch, the fail-open path, and
    the zero-ceiling / zero-reservation rollbacks.

    ``ceiling`` is the same bound the probe measured against, carried so the seat write can
    re-check it (#4125). Headroom alone is a number computed BEFORE the write and private
    to one process, which is exactly why it could not stop two of them.
    """

    ceiling: int | None = None
    headroom: int | None = None
    admitted: int = 0

    def spent(self) -> bool:
        return self.headroom is not None and self.admitted >= self.headroom


@dataclass
class AgentAdmission:
    """One governor probe, resolved per phase cost class (#4098).

    Plain fields rather than a per-phase callable: the verdict is a value a caller holds
    across a whole drain, asking it once per row, which is what makes the classification
    affordable at all — nothing changes between iterations of that loop, so re-probing
    per row would return the same answer N times at N times the cost.

    Each class carries its own :class:`LaneBound`: the cheap one is
    ``cheap_phase_admission_ceiling``, the expensive one is the governor's ceiling MINUS
    the slots reserved for the draining class (#4374).
    """

    expensive_denied: str | None
    cheap_denied: str | None
    cheap_lane: LaneBound = field(default_factory=LaneBound)
    expensive_lane: LaneBound = field(default_factory=LaneBound)
    seats_released: int = 0
    _announced: set[str] = field(default_factory=set)

    def lane_for(self, cost: PhaseCost) -> LaneBound:
        """The bound governing *cost*'s lane — the one seam both the check and the seat read."""
        return self.cheap_lane if cost is PhaseCost.CHEAP else self.expensive_lane

    def denied_for(self, cost: PhaseCost) -> str | None:
        """The reason to refuse one more admission of *cost*, or ``None`` to admit."""
        denied = self.cheap_denied if cost is PhaseCost.CHEAP else self.expensive_denied
        if denied is not None:
            return denied
        lane = self.lane_for(cost)
        if lane.spent():
            return f"{cost} headroom spent this pass ({lane.admitted} admitted, lane ceiling reached)"
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

        Each class hands its own width to the write, which re-checks that lane's occupancy
        there rather than trusting this verdict's probe (#4125). A width of ``None`` leaves
        only the one-seat-per-row rule, which is what the unbounded paths reduce to.
        """
        cost = phase_cost(phase)
        lane = self.lane_for(cost)
        cheap = cost is PhaseCost.CHEAP
        if not _task_model().objects.record_admission(task_pk, cheap=cheap, lane_ceiling=lane.ceiling):
            if lane.ceiling is None:
                return "already dispatched this window"
            return f"no {cost}-phase lane seat: already dispatched this window, or a racer took the last one"
        lane.admitted += 1
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


def _drain_reservation(ceiling: int) -> int:
    """How many of *ceiling*'s slots only the DRAINING class may occupy (#4374).

    Clamped to at most ``ceiling - 2``, so however large the operator writes it the
    expensive class always keeps TWO slots — a reservation that could reach the whole
    ceiling would trade one starvation for its mirror image and stop the factory writing
    code at all. ``0`` is the rollback lever: first-come allocation, exactly as before.

    Two rather than one because the governor ceiling is ``floor(cores * 0.5)``: a 4-core
    box (the CI runner) has ceiling 2, and a ``ceiling - 1`` clamp there leaves the
    expensive lane a SINGLE slot. §"the admission governor" records that outcome as the
    factory-starves-itself outage that is its own incident (#4407), so the reservation
    must not be the thing that produces it. The clamp binds only at ceiling 2 — at 3+
    (6 cores and up) it is identical to ``ceiling - 1``, so the 4-slots-to-3 behaviour
    this change is actually for is untouched.
    """
    from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: avoids a config import cycle

    return min(max(0, int(get_effective_settings().drain_slot_reservation)), max(0, ceiling - 2))


def _shed_denial(pressure: AdmissionPressure) -> str | None:
    """The reason to stop starting EXPENSIVE work short of a halt, or ``None`` (#4508).

    The band between "healthy" and "refuse everything" had no expression before the
    scalar, so the box kept starting open-ended coding agents against a budget that was
    nearly gone. Shedding here is the cheap/expensive asymmetry #4098 already established
    — the lanes that RETIRE work keep draining, which is what makes the pressure fall.
    """
    if pressure.band is not PressureBand.SHED:
        return None
    return f"pressure {pressure.value:.2f} in the shed band — {pressure.reason}"


def _ceiling_denial(ceiling: int, occupied: int, *, lane: str = "") -> str | None:
    if occupied < ceiling:
        return None
    if lane:
        return f"{lane} lane occupancy {occupied} at/over the {lane} ceiling {ceiling}"
    return f"live headless agents {occupied} at/over governor ceiling {ceiling}"


def _reservation_denial(ceiling: int, reserved: int, occupied: int) -> str | None:
    """Refuse an expensive admission that would spend the draining class's reserved slots."""
    unreserved = ceiling - reserved
    if occupied < unreserved:
        return None
    return (
        f"expensive lane occupancy {occupied} at/over its {unreserved} unreserved slot(s) — "
        f"{reserved} of the {ceiling} reserved for the draining class"
    )


def agent_admission_verdict() -> AgentAdmission:
    """Probe the governor ONCE and resolve the verdict for both phase cost classes.

    The EXPENSIVE class is the pure decision over the live quota + machine signals, then
    the live headless-agent count against the governor's ceiling, then the draining
    class's RESERVATION (#4374): the slots at the top of that ceiling only cheap work may
    occupy. A ceiling on the cheap class bounds how much of the box it may take and
    guarantees nothing about how much is kept for it, so expensive work filled every slot
    and zero reviews ran — the #4098 outcome by a different route. Because coding CREATES
    pull requests and reviewing RETIRES them, first-come allocation lets the producing
    side occupy the whole factory, and the board can then only grow. The reservation is
    measured against the EXPENSIVE lane's own occupancy, never the live population: against
    the latter it would invert into the mirror-image starvation, refusing coding work
    because reviews are running.

    The CHEAP class re-runs the SAME pure decision with the machine brake lifted — the
    token brakes still refuse it, because a review burns quota like anything else —
    bounded by its own small ceiling over the lane's OCCUPANCY: the running cheap agents
    plus the cheap rows already handed to the runner. Counting the latter is what lets a
    chokepoint see admissions it (or the other chokepoint) just made, so the bound is one
    number in the database rather than per-caller state. A ceiling of ``0`` collapses
    cheap onto expensive — the rollback lever, and it lifts the reservation with it, since
    holding slots for a class that no longer exists would just narrow the whole lane.

    Between the two sits the SHED band (#4508): the expensive class is refused while the
    cheap drain runs on, because :func:`decide_admission` is class-BLIND and can only
    speak at HALT. Cheap is deliberately never shed — shedding the lanes that retire work
    is the self-holding brake #4098 records.

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
        expensive = (
            decision.reason
            if not decision.admit
            else _shed_denial(pressure_for(quota=quota, machine=machine)) or _ceiling_denial(decision.ceiling, live)
        )
        if cheap_ceiling <= 0:
            return AgentAdmission(expensive_denied=expensive, cheap_denied=expensive)
        expensive_lane = LaneBound()
        reserved = _drain_reservation(decision.ceiling)
        if reserved and expensive is None:
            unreserved = decision.ceiling - reserved
            occupied = task_model.objects.expensive_lane_occupancy()
            expensive = _reservation_denial(decision.ceiling, reserved, occupied)
            expensive_lane = LaneBound(ceiling=unreserved, headroom=max(0, unreserved - occupied))
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
        cheap_lane=LaneBound(ceiling=cheap_ceiling, headroom=max(0, cheap_ceiling - cheap_occupancy)),
        expensive_lane=expensive_lane,
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


__all__ = ["AgentAdmission", "LaneBound", "agent_admission_denied_reason", "agent_admission_verdict"]
