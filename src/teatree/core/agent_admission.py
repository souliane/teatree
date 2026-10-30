"""Governor-gated admission for the agent lane (#3644 / F9, #4098).

The adaptive admission governor (:mod:`teatree.core.admission_governor`) was
consulted ONLY by the interactive ``/loop`` claim budget
(:func:`teatree.loop.admission.governor_verdict`), yet the measured congestion
collapse — 7,785 attempts at a 2.9% success rate — was on the agent lane.
This wires the SAME pure :func:`~teatree.core.admission_governor.decide_admission`
into the headless admission chokepoints — the post_save auto-enqueue, the drain
safety net, and issue intake — so a DENY verdict (weekly quota spent, 5h window
spent, machine load over the watermark, or the coding lane at the governor's
ceiling) refuses a NEW headless admission with a VISIBLE log.

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

**Cheap work sits outside the coding ceiling (#5051, #5327).** The stored
``cheap_phase_admission_ceiling`` is each cheap class's width. The shared cheap
lane is one seat wider, so two reviews cannot hold shipping at the machine brake.

It lives in ``teatree.core`` (not ``teatree.loop``) so the core chokepoints can
consult it without a backwards dependency edge; the loop-side ``governor_verdict``
is the richer interactive variant carrying the brake-hysteresis sidecar. Both
route through the one pure decision function, so the classes can never diverge
on the quota/machine/ceiling verdict.


Fail-OPEN by construction: any signal-read failure admits ALL classes — a governor that cannot read its
own signals must never wedge the factory. A claim-admission refusal (the ``off`` posture,
a quiescing worker, schema skew) is not a governor signal, so it denies all classes before
the governor is read. A refusal is never silent: this is the
only seam that returns a DENY reason, and every caller logs it at WARNING.
"""

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from teatree.core.admission.dispatch_lane import configured_dispatch_lane
from teatree.core.admission.headless_status_types import HeadlessAdmissionJson
from teatree.core.admission.lanes import LaneBound, LaneOccupancy, LaneWidths
from teatree.core.admission.write_ceiling import review_lane_width
from teatree.core.admission_governor import (
    MachineBrake,
    SupplementalAdmissionSignals,
    admission_ceiling,
    decide_admission,
    pressure_for,
    read_machine_signal,
    read_metered_signal,
    read_quota_signal,
)
from teatree.core.admission_pressure import (
    UNREAD_QUOTA,
    AdmissionPressure,
    MachineSignal,
    MeteredSignal,
    PressureBand,
    QuotaSignal,
)
from teatree.core.managers_task_claim import claim_admission_block_reason
from teatree.core.modelkit.phases import (
    MAINTENANCE_PHASES,
    RETIRING_PHASES,
    REVIEW_PHASES,
    CheapClass,
    PhaseCost,
    phase_cost,
)
from teatree.core.models.task_attempt import TaskAttempt
from teatree.core.telemetry.admission import record_admission_decision
from teatree.request_cache import request_scope

if TYPE_CHECKING:
    from teatree.core.models import Task

logger = logging.getLogger(__name__)


@dataclass
class AgentAdmission:
    """One probe held across a drain; cheap work has a total bound and class bounds."""

    expensive_denied: str | None
    review_denied: str | None
    retiring_denied: str | None
    cheap_denied: str | None
    answering_denied: str | None = None
    architectural_review_denied: str | None = None
    maintenance_denied: str | None = None
    cheap_lane: LaneBound = field(default_factory=LaneBound)
    architectural_review_lane: LaneBound = field(default_factory=LaneBound)
    maintenance_lane: LaneBound = field(default_factory=LaneBound)
    review_lane: LaneBound = field(default_factory=LaneBound)
    retiring_lane: LaneBound = field(default_factory=LaneBound)
    expensive_lane: LaneBound = field(default_factory=LaneBound)
    answering_lane: LaneBound = field(default_factory=LaneBound)
    expensive_guard_denied: str | None = None
    cheap_guard_denied: str | None = None
    architectural_review_guard_denied: str | None = None
    maintenance_guard_denied: str | None = None
    seats_released: int = 0
    _announced: set[str] = field(default_factory=set)

    def lane_for(self, cost: PhaseCost, *, cheap_class: CheapClass | None = None) -> LaneBound:
        """The class bound used for headroom and booking; cheap total is checked separately."""
        if cost is PhaseCost.EXPENSIVE:
            return self.expensive_lane
        if cost is PhaseCost.ANSWERING:
            return self.answering_lane
        if cost is PhaseCost.ARCHITECTURAL_REVIEW:
            return self.architectural_review_lane
        if cost is PhaseCost.MAINTENANCE:
            return self.maintenance_lane
        return self.retiring_lane if cheap_class is CheapClass.RETIRING else self.review_lane

    def denied_for(self, cost: PhaseCost, *, cheap_class: CheapClass | None = None) -> str | None:
        """The reason to refuse one more admission of *cost*, or ``None`` to admit."""
        if cost is PhaseCost.EXPENSIVE:
            denied = self.expensive_denied
            lane = self.expensive_lane
        elif cost is PhaseCost.ANSWERING:
            denied = self.answering_denied
            lane = self.answering_lane
        elif cost is PhaseCost.ARCHITECTURAL_REVIEW:
            denied = self.architectural_review_denied
            lane = self.architectural_review_lane
        elif cost is PhaseCost.MAINTENANCE:
            denied = self.maintenance_denied
            lane = self.maintenance_lane
        else:
            denied = self.cheap_denied
            lane = self.cheap_lane
        if denied is not None:
            return denied
        if lane.spent():
            return f"{cost} headroom spent this pass ({lane.admitted} admitted, lane ceiling reached)"
        if cost is PhaseCost.CHEAP and cheap_class is not None:
            denied = self.retiring_denied if cheap_class is CheapClass.RETIRING else self.review_denied
            if denied is not None:
                return denied
            lane = self.lane_for(cost, cheap_class=cheap_class)
            if lane.spent():
                return f"{cheap_class} headroom spent this pass ({lane.admitted} admitted, lane ceiling reached)"
        return None

    def denied_reason(self, phase: str = "") -> str | None:
        """The reason to refuse one more admission of *phase*, or ``None`` to admit.

        A blank or unregistered phase classifies EXPENSIVE, so a caller that cannot
        name its phase gets the braked answer rather than the exemption.
        """
        return self.denied_for(phase_cost(phase), cheap_class=CheapClass.of(phase))

    def claim_denied_reason(self, phase: str, *, seated: bool) -> str | None:
        """A seated row has paid for its lane; independent brakes can still refuse its claim."""
        if not seated:
            return self.denied_reason(phase)
        cost = phase_cost(phase)
        if cost is PhaseCost.ARCHITECTURAL_REVIEW:
            return self.architectural_review_guard_denied
        if cost is PhaseCost.MAINTENANCE:
            return self.maintenance_guard_denied
        return (
            self.cheap_guard_denied if cost in {PhaseCost.CHEAP, PhaseCost.ANSWERING} else self.expensive_guard_denied
        )

    def admit(self, task_pk: int, phase: str, *, at: str) -> bool:
        """True when *task_pk* now holds a seat and may be dispatched — else refuse, at *at*.

        The single seam every chokepoint routes its admission through, so no caller can
        dispatch a row it never booked: a chokepoint that enqueued without the durable
        seat would leave its own admission invisible to every later probe, which is
        exactly how a one-row-at-a-time burst outran the ceiling. The seat is therefore
        taken BEFORE the dispatch, and a dispatch that then fails costs at most one
        :data:`~teatree.core.managers_admission.ADMITTED_INFLIGHT_WINDOW` of under-admission — the
        direction that is safe, and what that window already exists to recover.

        Deciding, booking and announcing are ONE call so a chokepoint cannot skip a row
        quietly. A reason already announced this pass — by :meth:`log_denials` or for an
        earlier row — drops to DEBUG rather than repeating per held row: on the measured
        shape (18 rows, one braked drain) that repetition was 19 lines every cadence saying
        one thing.
        """
        denied = self.denied_reason(phase) or self._book(task_pk, phase)
        if denied is None:
            return True
        level = logging.DEBUG if denied in self._announced else logging.WARNING
        self._announced.add(denied)
        logger.log(level, "Governor DENIED %s of task %s: %s (staying PENDING)", at, task_pk, denied)
        return False

    def _book(self, task_pk: int, phase: str) -> str | None:
        """Book both the lane-total and class bounds in the conditional write."""
        cost = phase_cost(phase)
        cheap_class = CheapClass.of(phase)
        lane = self.lane_for(cost, cheap_class=cheap_class)
        total_lane = self.cheap_lane if cheap_class is not None else lane
        if not _task_model().objects.record_admission(
            task_pk,
            cheap_class=cheap_class,
            lane=cost,
            lane_ceiling=total_lane.ceiling,
            class_ceiling=lane.ceiling if cheap_class is not None else None,
        ):
            if total_lane.ceiling is None:
                return "already dispatched this window"
            return f"no {cheap_class or cost}-phase seat: already dispatched this window, or a racer took the last one"
        if cheap_class is not None:
            total_lane.admitted += 1
        lane.admitted += 1
        return None

    def log_denials(self) -> None:
        """Announce each refused cost or cheap class once.

        Lives on the verdict rather than at each chokepoint so a refusal is worded
        identically wherever it is taken, and so a class added to :class:`PhaseCost`
        cannot acquire a caller that forgets to report it. What is announced here is
        remembered, so :meth:`admit` can hold a row against it without saying it again.

        A released seat is announced alongside: it is the state in which the ceiling is
        SOFT, and it arrives with no refusal of its own to carry it.
        """
        for cost in PhaseCost:
            classes = tuple(CheapClass) if cost is PhaseCost.CHEAP and self.cheap_denied is None else (None,)
            for cheap_class in classes:
                denied = self.denied_for(cost, cheap_class=cheap_class)
                if denied is not None:
                    self._announced.add(denied)
                    logger.warning(
                        "Governor DENIED headless admission of %s work: %s (rows stay queued)",
                        cheap_class or cost,
                        denied,
                    )
        if self.seats_released:
            logger.warning(
                "Cheap lanes released %s unclaimed seat(s) — the runner backlog outran the seat window, "
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
    return AgentAdmission(
        expensive_denied=None,
        cheap_denied=None,
        review_denied=None,
        retiring_denied=None,
    )


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


def _ceiling_denial(ceiling: int, occupied: int, *, lane: str) -> str | None:
    if occupied < ceiling:
        return None
    return f"{lane} lane occupancy {occupied} at/over the {lane} ceiling {ceiling}"


def _lane_budget() -> tuple[QuotaSignal, MeteredSignal]:
    """The budget this box's CONFIGURED lane would actually spend, and only that one.

    ``BRAKE_PRECEDENCE`` leads with ``accounts-exhausted``, so before #4816 an exhausted
    OAuth fleet HALTed metered dispatches that would never have touched it — 307 tasks
    refused against a budget they did not draw on.
    """
    if configured_dispatch_lane() == TaskAttempt.Lane.METERED:
        return UNREAD_QUOTA, read_metered_signal()
    return read_quota_signal(), MeteredSignal(fresh=False)


def _lane_widths(quota: QuotaSignal, machine: MachineSignal) -> LaneWidths:
    return LaneWidths(
        ceiling=admission_ceiling(quota, machine), review=review_lane_width(), maintenance=len(MAINTENANCE_PHASES)
    )


@dataclass(frozen=True)
class HeadlessAdmissionStatus:
    """Every input the headless verdict is decided on, read once; the verdict and ``t3 worker status`` resolve it."""

    widths: LaneWidths
    pressure: AdmissionPressure
    exempt: AdmissionPressure
    occupied: LaneOccupancy
    seats_released: int
    blocked: str = ""

    def verdict(self) -> AgentAdmission:
        widths, occupied = self.widths, self.occupied
        cheap_lane = LaneBound.from_width(widths.cheap, occupied.cheap)
        review_lane = LaneBound.from_width(widths.review, occupied.review)
        retiring_lane = LaneBound.from_width(widths.retiring, occupied.retiring)
        expensive_lane = LaneBound.from_width(widths.expensive, occupied.expensive)
        answering_lane = LaneBound.from_width(widths.answering, occupied.answering)
        architectural_review_lane = LaneBound.from_width(widths.architectural_review, occupied.architectural_review)
        maintenance_lane = LaneBound.from_width(widths.maintenance, occupied.maintenance)
        if self.blocked:
            return AgentAdmission(
                expensive_denied=self.blocked,
                cheap_denied=self.blocked,
                review_denied=self.blocked,
                retiring_denied=self.blocked,
                answering_denied=self.blocked,
                architectural_review_denied=self.blocked,
                maintenance_denied=self.blocked,
                cheap_lane=cheap_lane,
                review_lane=review_lane,
                retiring_lane=retiring_lane,
                expensive_lane=expensive_lane,
                answering_lane=answering_lane,
                architectural_review_lane=architectural_review_lane,
                maintenance_lane=maintenance_lane,
                expensive_guard_denied=self.blocked,
                cheap_guard_denied=self.blocked,
                architectural_review_guard_denied=self.blocked,
                maintenance_guard_denied=self.blocked,
            )
        expensive_guard = (
            self.pressure.reason if self.pressure.band is PressureBand.HALT else _shed_denial(self.pressure)
        )
        cheap_guard = self.exempt.reason if self.exempt.band is PressureBand.HALT else None
        expensive = expensive_guard or _ceiling_denial(widths.expensive, occupied.expensive, lane="expensive")
        cheap = cheap_guard or _ceiling_denial(widths.cheap, occupied.cheap, lane="cheap")
        review = cheap_guard or _ceiling_denial(widths.review, occupied.review, lane="review")
        retiring = cheap_guard or _ceiling_denial(widths.retiring, occupied.retiring, lane="retiring")
        return AgentAdmission(
            expensive_denied=expensive,
            cheap_denied=cheap,
            review_denied=review,
            retiring_denied=retiring,
            answering_denied=cheap_guard or _ceiling_denial(widths.answering, occupied.answering, lane="answering"),
            architectural_review_denied=expensive_guard
            or _ceiling_denial(widths.architectural_review, occupied.architectural_review, lane="architectural review"),
            maintenance_denied=expensive_guard
            or _ceiling_denial(widths.maintenance, occupied.maintenance, lane="maintenance"),
            cheap_lane=cheap_lane,
            review_lane=review_lane,
            retiring_lane=retiring_lane,
            expensive_lane=expensive_lane,
            answering_lane=answering_lane,
            architectural_review_lane=architectural_review_lane,
            maintenance_lane=maintenance_lane,
            expensive_guard_denied=expensive_guard,
            cheap_guard_denied=cheap_guard,
            architectural_review_guard_denied=expensive_guard,
            maintenance_guard_denied=expensive_guard,
            seats_released=self.seats_released,
        )

    def line(self) -> str:
        ceiling, widths, verdict = self.widths.ceiling, self.widths, self.verdict()
        if ceiling.pace is None:
            pace = "weekly pace unread (unscaled)"
        else:
            pace = f"weekly pace {ceiling.pace:.2f}" + (" (scaling capped at 1)" if ceiling.pace > 1 else "")
        denied = (
            ("expensive", verdict.expensive_denied),
            ("cheap", verdict.cheap_denied),
            ("review", verdict.review_denied),
            ("retiring", verdict.retiring_denied),
            ("architectural review", verdict.architectural_review_denied),
            ("maintenance", verdict.maintenance_denied),
        )
        denials = "".join(f"; {name} denied: {reason}" for name, reason in denied if reason is not None)
        return (
            f"agent admission: ceiling {ceiling.value} = {ceiling.cores} cores x {ceiling.per_core:g} per core "
            f"({ceiling.machine}) x {pace}; lanes {widths.expensive} expensive + {widths.cheap} cheap "
            f"+ {widths.architectural_review} architectural review + {widths.maintenance} maintenance "
            f"(up to {widths.review} review ({', '.join(sorted(REVIEW_PHASES))}) + up to {widths.retiring} retiring "
            f"({', '.join(sorted(RETIRING_PHASES))})); occupied {self.occupied.expensive} expensive + "
            f"{self.occupied.cheap} cheap ({self.occupied.review} review + {self.occupied.retiring} retiring) "
            f"+ {self.occupied.architectural_review} architectural review + {self.occupied.maintenance} maintenance; "
            f"pressure {self.pressure.value:.2f} {self.pressure.band}{denials}"
        )

    def as_json(self) -> HeadlessAdmissionJson:
        ceiling, verdict = self.widths.ceiling, self.verdict()
        return HeadlessAdmissionJson(
            ceiling=ceiling.value,
            cores=ceiling.cores,
            per_core=ceiling.per_core,
            machine_ceiling=ceiling.machine,
            weekly_pace=ceiling.pace,
            expensive_lane=self.widths.expensive,
            cheap_lane=self.widths.cheap,
            review_lane=self.widths.review,
            retiring_lane=self.widths.retiring,
            expensive_occupied=self.occupied.expensive,
            cheap_occupied=self.occupied.cheap,
            review_occupied=self.occupied.review,
            retiring_occupied=self.occupied.retiring,
            review_phases=sorted(REVIEW_PHASES),
            retiring_phases=sorted(RETIRING_PHASES),
            pressure=self.pressure.value,
            band=str(self.pressure.band),
            expensive_denied=verdict.expensive_denied,
            cheap_denied=verdict.cheap_denied,
            review_denied=verdict.review_denied,
            retiring_denied=verdict.retiring_denied,
            architectural_review_lane=self.widths.architectural_review,
            architectural_review_occupied=self.occupied.architectural_review,
            architectural_review_denied=verdict.architectural_review_denied,
            maintenance_lane=self.widths.maintenance,
            maintenance_occupied=self.occupied.maintenance,
            maintenance_denied=verdict.maintenance_denied,
        )


def _read_admission(
    quota: QuotaSignal, machine: MachineSignal, metered: MeteredSignal, *, blocked: str = ""
) -> HeadlessAdmissionStatus:
    task_model = _task_model()
    return HeadlessAdmissionStatus(
        widths=_lane_widths(quota, machine),
        pressure=pressure_for(quota=quota, machine=machine, metered=metered),
        exempt=pressure_for(quota=quota, machine=machine, metered=metered, load_brake=MachineBrake(applies=False)),
        occupied=LaneOccupancy(
            expensive=task_model.objects.expensive_lane_occupancy(),
            review=task_model.objects.cheap_class_occupancy(CheapClass.REVIEW),
            retiring=task_model.objects.cheap_class_occupancy(CheapClass.RETIRING),
            answering=task_model.objects.lane_occupancy(PhaseCost.ANSWERING),
            architectural_review=task_model.objects.lane_occupancy(PhaseCost.ARCHITECTURAL_REVIEW),
            maintenance=task_model.objects.lane_occupancy(PhaseCost.MAINTENANCE),
        ),
        seats_released=task_model.objects.cheap_lane_seats_released(),
        blocked=blocked,
    )


@request_scope()  # every Task creation runs a verdict, so each settings re-read is paid per row
def agent_admission_verdict() -> AgentAdmission:
    """Probe the governor once and resolve expensive and cheap class bounds.

    The EXPENSIVE (coding) class is the pure decision over the live quota + machine
    signals, then the expensive lane's OCCUPANCY against the governor's ceiling — never
    the whole live population, which would refuse coding work because reviews are running.

    CHEAP review and retiring classes rerun the same decision with the machine brake lifted;
    token brakes still apply. Each class has W seats, with W from :func:`review_lane_width`,
    inside a shared W+1 cheap lane. Occupancy includes running agents and recent pending
    seats, so both chokepoints see each other's admissions. Cheap work is bounded beside
    coding rather than by the expensive ceiling (#5051).

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
    if blocked := claim_admission_block_reason():
        return AgentAdmission(
            expensive_denied=blocked,
            cheap_denied=blocked,
            review_denied=blocked,
            retiring_denied=blocked,
            answering_denied=blocked,
            architectural_review_denied=blocked,
            maintenance_denied=blocked,
            expensive_guard_denied=blocked,
            cheap_guard_denied=blocked,
            architectural_review_guard_denied=blocked,
            maintenance_guard_denied=blocked,
        )
    try:
        quota, metered = _lane_budget()
        machine = read_machine_signal()
        decision = decide_admission(
            quota=quota,
            machine=machine,
            signals=SupplementalAdmissionSignals(metered=metered),
            static_ceiling=None,
        )
        status = _read_admission(quota, machine, metered)
        verdict = status.verdict()
    except Exception:
        logger.exception("headless admission governor probe failed — admitting (fail-open)")
        return _admit_all()
    try:
        record_admission_decision(decision=decision, pressure=status.pressure, lane="headless")
    except Exception:
        logger.exception("headless admission telemetry failed; retaining the computed verdict")
    return verdict


@request_scope()
def headless_admission_status() -> HeadlessAdmissionStatus:
    """The verdict's own read, including a claim-admission block, without booking a seat or recording a span."""
    quota, metered = _lane_budget()
    return _read_admission(quota, read_machine_signal(), metered, blocked=claim_admission_block_reason())


@request_scope()
def headless_lane_widths() -> LaneWidths:
    """The lane widths the verdict admits up to, read without counting occupancy — what the worker sizes to."""
    quota, _metered = _lane_budget()
    return _lane_widths(quota, read_machine_signal())


def agent_admission_denied_reason(phase: str = "") -> str | None:
    """The governor's reason to DENY one more headless admission of *phase*, or ``None``.

    The single-shot wrapper for a caller admitting ONE unit of work: it probes and
    resolves in one call. A caller walking a queue holds a
    :func:`agent_admission_verdict` instead, so N rows still cost one probe.
    ``phase`` omitted is the EXPENSIVE class — the pre-#4098 verdict verbatim.
    """
    return agent_admission_verdict().denied_reason(phase)


def pending_lane_denial(task_obj: "Task") -> str | None:
    if task_obj.status != task_obj.Status.PENDING:
        return None
    return agent_admission_verdict().claim_denied_reason(task_obj.phase, seated=task_obj.admitted_at is not None)


__all__ = [
    "AgentAdmission",
    "HeadlessAdmissionStatus",
    "LaneBound",
    "LaneWidths",
    "agent_admission_denied_reason",
    "agent_admission_verdict",
    "headless_admission_status",
    "headless_lane_widths",
    "pending_lane_denial",
    "review_lane_width",
]
