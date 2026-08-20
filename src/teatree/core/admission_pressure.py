"""One normalised pressure scalar the admission decision consults (#4508).

Six brakes lived as six independent ``if``s — weekly quota, the 5h window, weekly
pace, account exhaustion, machine load, free memory — each with its own watermark
and its own sentence. That shape can only ever answer *admit* or *refuse*: there
is no way to say "the box is at 0.85 of what it can take", so degradation was a
cliff and the box could read healthy on load while being out of weekly runway.

**1.0 IS each dimension's own existing watermark.** That single choice is the
correctness argument: :data:`HALT_AT` reproduces the pre-#4508 brake set exactly,
so folding them into one value is a re-expression rather than a new opinion
(``tests/teatree_core/test_admission_pressure.py`` pins it against a frozen oracle
of the old predicates). What the fold BUYS is the range below 1.0, which nothing
could read before — see :class:`PressureBand`.

**The signal dataclasses live here, not in the governor.** They are the scalar's
inputs, and the governor's readers produce them; putting them beside the
normalisation is what keeps the dependency one-way
(:mod:`teatree.core.admission_governor` imports this module, never the reverse).
:func:`box_load_headroom` and :func:`ram_headroom` come with them, because each
is exactly ``1 - component`` for its dimension and two unsynchronised readers of
one quantity drift (#4125).

**Nothing here is telemetry.** The scalar is computed at the decision point and
discarded; no table, no span, no emitter. Every value it produces has a named
consumer before it is produced — :func:`~teatree.core.admission_governor.decide_admission`
(HALT), :func:`~teatree.core.agent_admission.agent_admission_verdict` (SHED),
:func:`~teatree.core.intake.concurrency.adapt_concurrency` (the continuous term),
``t3 doctor check``'s box-occupancy line, and the ``admission_pressure`` health
collector that clusters a persistent cause into ONE ``KnownIssue`` row.
"""

import math
from dataclasses import dataclass
from enum import StrEnum

WEEKLY_WINDOW_SECONDS = 7 * 24 * 3600

#: Load watermarks, as multiples of the core count. Above ``BRAKE`` new admissions are
#: denied; a braked governor only re-admits once load falls back under ``RESUME``. The
#: gap is the hysteresis that stops it flapping around one threshold.
BRAKE_LOAD_PER_CORE = 5.0
RESUME_LOAD_PER_CORE = 3.0

#: Memory watermarks in GB, the same shape as the load pair above: below ``RAM_BRAKE``
#: new admissions are denied, and a braked governor re-admits only above ``RAM_RESUME``.
#: Absolute rather than per-core because a pytest worker's footprint is a property of the
#: suite, not of the box that runs it.
#:
#: ``4.0`` is the headroom one p90 worker (1.24 GB) plus the OS and page cache need to
#: survive the burst a fresh admission creates; it matches ``intake_ram_reserve_gb``,
#: which reserves the same margin for the same reason one layer up. ``6.0`` is one more
#: worker's worth above it — the gap is the hysteresis, so a box hovering at the floor
#: cannot flap admissions on and off.
RAM_BRAKE_FLOOR_GB = 4.0
RAM_RESUME_FLOOR_GB = 6.0

#: A 5h window this spent is an imminent hard rate-limit; retrying into one is pure burn.
SHORT_WINDOW_BRAKE = 0.95
#: Weekly headroom below this is spent — nothing left to admit against.
WEEKLY_WINDOW_BRAKE = 0.99

#: Pace = weekly headroom / weekly runway. Below 1 the burn outruns the window, so the
#: ceiling is scaled down to land AT the reset instead of sprinting to zero. At/below
#: ``PACE_DENY`` there is not enough left to start anything new.
PACE_DENY = 0.1

#: Band thresholds. ``DEGRADE_AT`` is where the continuous intake term is materially
#: clamping and nothing is refused yet; ``SHED_AT_DEFAULT`` is where the EXPENSIVE agent
#: class is refused while the cheap drain keeps running; ``HALT_AT`` is the pre-#4508
#: brake set. Documented in BLUEPRINT § "Adaptive admission governor" so the behaviour is
#: predictable rather than emergent.
DEGRADE_AT = 0.7
SHED_AT_DEFAULT = 0.9
HALT_AT = 1.0

#: The pre-#4508 brake evaluation order — quota brakes in their ``if`` order, then the
#: machine ones. It survives as the tie-break among HALTing components so a refusal names
#: the cause it always named: an exhausted fleet reports exhaustion, not the collapsed
#: pace that exhaustion necessarily produces.
BRAKE_PRECEDENCE = ("accounts-exhausted", "weekly-quota", "5h-quota", "weekly-pace", "load", "memory")


@dataclass(frozen=True)
class QuotaSignal:
    """Live model-quota headroom — the PRIMARY admission signal.

    ``fresh`` is False when no account's headroom is known; the decision then drops the
    weekly-pace scaling — the only part that needs this signal — rather than trusting a
    guess, and bounds the lane on the machine signal alone.
    Utilizations are the BEST (lowest) across usable accounts: the account selector
    already falls through to a non-exhausted account, so the governor asks what the
    healthiest remaining account has left, and ``all_accounts_exhausted`` is the
    separate signal that the fallthrough has nowhere left to go.
    """

    fresh: bool
    all_accounts_exhausted: bool
    weekly_utilization: float
    short_utilization: float
    seconds_to_weekly_reset: float | None


@dataclass(frozen=True)
class MachineSignal:
    """Box pressure — the SECONDARY brake. ``ram_available_gb`` is ``None`` when unread."""

    cores: int
    load1: float
    ram_available_gb: float | None


@dataclass(frozen=True)
class MachineBrake:
    """The caller's two inputs to the LOAD brake, as one value.

    ``braked`` is the previous decision's brake state and supplies the hysteresis — a
    braked governor is held to the lower watermark so it cannot flap.

    ``applies`` is the cheap-phase exemption (#4098): the read-only phases that RETIRE
    work were being refused on the very load their expensive siblings created, which
    removed the only relief available and held the brake on. ``False`` drops the MACHINE
    components for that class alone — never the token ones, which are a claim about
    budget rather than pressure and so refuse a cheap phase exactly as they refuse an
    expensive one. The caller supplies the exemption's own bound;
    :func:`~teatree.core.admission_governor.decide_admission` never widens a lane on its own.
    """

    applies: bool = True
    braked: bool = False


#: The default: the brake applies, with no prior brake state to hold it to the low watermark.
UNBRAKED = MachineBrake()


class PressureBand(StrEnum):
    """What degrades at each threshold — the ordering the six separate ``if``s could not express.

    ``FULL`` nothing degrades. ``DEGRADED`` intake concurrency tightens on the worst
    dimension (a continuous term, not a cliff — this band names where it starts to bite).
    ``SHED`` refuses the EXPENSIVE agent class while the CHEAP review/ship lanes that
    RETIRE work keep draining; that asymmetry is the whole point, since refusing the
    drain alongside the class that filled the box is what held the brake on in #4098.
    ``HALT`` refuses every class — the pre-#4508 brake set, and in-flight work is never
    killed, only new admissions refused.
    """

    FULL = "full"
    DEGRADED = "degraded"
    SHED = "shed"
    HALT = "halt"

    @classmethod
    def for_value(cls, value: float, *, shed_at: float) -> "PressureBand":
        """Classify *value*; ``shed_at == HALT_AT`` collapses SHED into HALT (the rollback lever)."""
        if value >= HALT_AT:
            return cls.HALT
        if value >= shed_at:
            return cls.SHED
        if value >= DEGRADE_AT:
            return cls.DEGRADED
        return cls.FULL


@dataclass(frozen=True, slots=True)
class PressureComponent:
    """One dimension's normalised utilisation, and the sentence a refusal on it reads.

    ``detail`` is the pre-#4508 brake's own wording, carried rather than regenerated so a
    refusal is worded identically to how it always was.
    """

    name: str
    value: float
    detail: str


@dataclass(frozen=True, slots=True)
class AdmissionPressure:
    """The scalar, its inputs named, and the band it lands in.

    ``components`` is empty when nothing was readable — a probe that cannot answer must
    never raise the pressure, so that reads ``0.0`` / ``FULL`` and the caller falls back
    to whatever it does without an opinion. That is the same direction every unknown
    takes here, and the reason ``reason`` degrades to ``""`` rather than inventing one.
    """

    components: tuple[PressureComponent, ...]
    shed_at: float = SHED_AT_DEFAULT

    @property
    def value(self) -> float:
        return max((component.value for component in self.components), default=0.0)

    @property
    def dominant(self) -> PressureComponent | None:
        """The one cause a refusal names, so N observations of it report as ONE incident.

        Among components that have REACHED :data:`HALT_AT`, :data:`BRAKE_PRECEDENCE`
        decides — the pre-#4508 brakes were an ordered ``if`` chain, so a spent fleet was
        always reported as exhaustion even though its pace had also collapsed, and
        picking the numeric maximum would silently rename it to the derived symptom.
        Below HALT nothing was ever named, so there is no order to preserve and the
        worst dimension is simply the worst one.
        """
        if not self.components:
            return None
        halted = [component for component in self.components if component.value >= HALT_AT]
        if halted:
            return min(halted, key=lambda component: BRAKE_PRECEDENCE.index(component.name))
        return max(self.components, key=lambda component: component.value)

    @property
    def band(self) -> PressureBand:
        return PressureBand.for_value(self.value, shed_at=self.shed_at)

    @property
    def reason(self) -> str:
        dominant = self.dominant
        return dominant.detail if dominant is not None else ""


def resolve_shed_at(configured: float) -> float:
    """Clamp the operator's ``admission_pressure_shed_at`` into the documented range.

    A typo must not be able to wedge the expensive lane shut, so the floor is
    :data:`DEGRADE_AT` rather than zero; the ceiling :data:`HALT_AT` is the rollback
    lever (SHED collapses into HALT and admission is byte-identical to pre-#4508). A
    non-finite value is not a threshold at all and falls back to the shipped default.
    """
    if not math.isfinite(configured):
        return SHED_AT_DEFAULT
    return min(HALT_AT, max(DEGRADE_AT, float(configured)))


def weekly_pace(quota: QuotaSignal) -> float:
    """Weekly headroom divided by weekly runway — 1.0 is exactly on pace.

    Above 1 the window is being under-spent (there is room to raise admissions); below 1
    the burn outruns the reset and admissions are paced down to land at it. An unknown
    reset is treated as a FULL window remaining, the conservative reading: it makes the
    runway look long, so the pace looks tight, so the ceiling tightens.
    """
    headroom = max(0.0, 1.0 - quota.weekly_utilization)
    seconds = WEEKLY_WINDOW_SECONDS if quota.seconds_to_weekly_reset is None else quota.seconds_to_weekly_reset
    runway = min(1.0, max(seconds, 0.0) / WEEKLY_WINDOW_SECONDS)
    if runway <= 0:
        return 1.0
    return headroom / runway


def box_load_headroom(*, load1: float | None, cores: int) -> float:
    """The fraction of the box's load budget still free — ``1.0`` idle, ``0.0`` saturated.

    Exactly ``1 - `` the ``load`` component below, clamped the way a ceiling multiplier
    must be. It lives beside that component so the two cannot drift (#4125), and it is
    consumed by :func:`~teatree.core.admission_governor.resume_agent_ceiling`, which
    reads load through a Django-free hook path and so cannot ask for the whole scalar.

    Load is whole-box by construction, which is the point of consuming it: a harness
    sub-agent an orchestrating session dispatched claims no ``Task`` and appears in no
    factory count, yet it runs a test suite on the same cores. A bound derived only from
    what the factory itself is running reads healthy on a box at load 53 (#4407).

    ``None`` (nothing readable) is ``1.0`` for the reason every unknown here is: a probe
    that cannot answer must not be able to lower a ceiling.
    """
    if load1 is None:
        return 1.0
    return _headroom(_load_pressure(load1=load1, cores=cores, braked=False))


def ram_headroom(ram_available_gb: float | None) -> float:
    """The fraction of the agent population the live memory reading still supports.

    ``1.0`` at or above :data:`RAM_RESUME_FLOOR_GB` (inert on a healthy box), ramping to
    ``0.0`` at :data:`RAM_BRAKE_FLOOR_GB`, which is where the brake refuses outright.
    ``None`` is ``1.0`` for the reason every unknown here is: a probe that cannot answer
    must not be able to lower a ceiling.
    """
    if ram_available_gb is None:
        return 1.0
    return _headroom(_ram_pressure(ram_available_gb, braked=False))


def admission_pressure(
    *,
    quota: QuotaSignal,
    machine: MachineSignal,
    braked: bool = False,
    machine_applies: bool = True,
    shed_at: float = SHED_AT_DEFAULT,
) -> AdmissionPressure:
    """Fold every readable dimension into one scalar, each normalised to its own watermark.

    *braked* is the previous decision's brake state and moves the load and memory
    watermarks to their hysteresis values, so the scalar inherits the flap protection the
    separate brakes had. *machine_applies* is the cheap-phase exemption: it drops the two
    machine components and leaves the token ones, which is exactly what that exemption
    always meant.

    An unreadable dimension contributes NO component rather than a zero, so it can
    neither raise the pressure nor be mistaken for a healthy reading — a stale quota
    cache is the steady state here, not an exception.
    """
    components: list[PressureComponent] = []
    if quota.fresh:
        components.extend(_quota_components(quota))
    if machine_applies:
        components.extend(_machine_components(machine, braked=braked))
    return AdmissionPressure(components=tuple(components), shed_at=shed_at)


def _quota_components(quota: QuotaSignal) -> list[PressureComponent]:
    pace = weekly_pace(quota)
    return [
        PressureComponent(
            name="accounts-exhausted",
            value=1.0 if quota.all_accounts_exhausted else 0.0,
            detail="every account is quota-exhausted — retrying into a rate limit is pure burn",
        ),
        PressureComponent(
            name="weekly-quota",
            value=_clamp(quota.weekly_utilization / WEEKLY_WINDOW_BRAKE),
            detail=f"weekly window spent ({quota.weekly_utilization:.0%}) — no budget left to admit against",
        ),
        PressureComponent(
            name="5h-quota",
            value=_clamp(quota.short_utilization / SHORT_WINDOW_BRAKE),
            detail=f"5h window spent ({quota.short_utilization:.0%}) — a hard rate limit is imminent",
        ),
        PressureComponent(
            name="weekly-pace",
            value=_clamp((1.0 - pace) / (1.0 - PACE_DENY)),
            detail=f"weekly burn outruns the reset (pace {pace:.2f}) — pacing to the window",
        ),
    ]


def _machine_components(machine: MachineSignal, *, braked: bool) -> list[PressureComponent]:
    cores = max(1, machine.cores)
    watermark = _load_watermark(cores=cores, braked=braked)
    components = [
        PressureComponent(
            name="load",
            value=_load_pressure(load1=machine.load1, cores=cores, braked=braked),
            detail=f"load {machine.load1:.0f} at/over the {watermark:.0f} watermark on {cores} core(s)",
        )
    ]
    if machine.ram_available_gb is not None:
        floor = _ram_floor(braked=braked)
        components.append(
            PressureComponent(
                name="memory",
                value=_ram_pressure(machine.ram_available_gb, braked=braked),
                detail=f"{machine.ram_available_gb:.1f} GB available at/under the {floor:.0f} GB watermark",
            )
        )
    return components


def _load_watermark(*, cores: int, braked: bool) -> float:
    return (RESUME_LOAD_PER_CORE if braked else BRAKE_LOAD_PER_CORE) * max(1, cores)


def _ram_floor(*, braked: bool) -> float:
    return RAM_RESUME_FLOOR_GB if braked else RAM_BRAKE_FLOOR_GB


def _load_pressure(*, load1: float, cores: int, braked: bool) -> float:
    return _clamp(load1 / _load_watermark(cores=cores, braked=braked))


def _ram_pressure(ram_available_gb: float, *, braked: bool) -> float:
    span = RAM_RESUME_FLOOR_GB - RAM_BRAKE_FLOOR_GB
    return _clamp(1.0 + (_ram_floor(braked=braked) - ram_available_gb) / span)


def _clamp(value: float) -> float:
    """Floor at zero only: an over-1.0 reading is a louder HALT and stays legible as one."""
    return max(0.0, value)


def _headroom(pressure: float) -> float:
    return min(1.0, max(0.0, 1.0 - pressure))


__all__ = [
    "BRAKE_LOAD_PER_CORE",
    "DEGRADE_AT",
    "HALT_AT",
    "PACE_DENY",
    "RAM_BRAKE_FLOOR_GB",
    "RAM_RESUME_FLOOR_GB",
    "RESUME_LOAD_PER_CORE",
    "SHED_AT_DEFAULT",
    "SHORT_WINDOW_BRAKE",
    "UNBRAKED",
    "WEEKLY_WINDOW_BRAKE",
    "WEEKLY_WINDOW_SECONDS",
    "AdmissionPressure",
    "MachineBrake",
    "MachineSignal",
    "PressureBand",
    "PressureComponent",
    "QuotaSignal",
    "admission_pressure",
    "box_load_headroom",
    "ram_headroom",
    "resolve_shed_at",
    "weekly_pace",
]
