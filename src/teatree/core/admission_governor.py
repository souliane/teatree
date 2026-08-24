"""The adaptive admission governor — one chokepoint every dispatcher asks (#3644).

Concurrency was a hand-set constant, so the only feedback loop was a human watching
load: one session flipped the same knob back and forth while the box melted twice.
This module replaces that with a decision every dispatcher consults BEFORE admitting
work. Static concurrency settings become CEILINGS, not targets.

**Token budget is the PRIMARY signal; machine pressure is secondary.** The budget that
actually runs out is the model quota, on a WEEKLY window — a box with idle CPU and no
weekly quota must admit nothing. Machine load is a second, independent brake.

The probe is deterministic and model-free: cached per-account rate-limit rows, the load
average, the core count, and terminal task counts. Nothing here consults a model, so it
is safe to ask at every admission decision — which is the point, admission is naturally
EVENT-DRIVEN. A polling timer is only a safety net, never the mechanism.

Refusals are visible by construction: every :class:`AdmissionDecision` carries a
``reason``, and the loop-side caller logs it. A governor that refuses silently
recreates the exact class of bug that hid a dead merge loop for weeks.

**Which signals are whole-box, and which are lane-local (#4407).** Load and memory are
whole-box: an orchestrating session's harness sub-agents never claim a ``Task``, so they
are invisible to every count the factory keeps, but they consume the same cores. Counts
— ``claimed_agent_count``, the intake in-flight budget — are lane-local by construction
and named so at each call site. The deliberate split is that foreign occupancy BRAKES the
producer and only REPORTS on the rest: the admission-pressure scalar bounds intake, because
slowing the producer cannot deadlock a factory whose review and ship lanes still drain the
pile, while the agent lanes keep only the binary watermark brake below — scaling their
already-small ``floor(cores * WRITE_CONCURRENCY_PER_CORE)`` ceiling by the same headroom
would leave a 4-core box ONE expensive slot and starve the drain.

**The brakes are ONE scalar, not six ``if``s (#4508).** Every watermark below normalises
to ``1.0`` in :mod:`teatree.core.admission_pressure`, so :func:`decide_admission` refuses
on a single :data:`~teatree.core.admission_pressure.PressureBand.HALT` verdict that
reproduces the pre-#4508 brake set exactly — and the range beneath it, which six separate
``if``s could not express, is what the cheap/expensive shed and the intake term now read.
The signal dataclasses live there for the same reason: they are the scalar's inputs, so
the dependency runs this way only.

Ships behind the default-ON ``admission_governor_enabled`` setting; setting it false is
the kill-switch and the rollback lever (see :func:`governor_enabled`).
"""

import datetime as dt
import logging
import math
import os
from dataclasses import dataclass

from teatree.core.admission_pressure import (
    BRAKE_LOAD_PER_CORE,
    RAM_BRAKE_FLOOR_GB,
    RAM_RESUME_FLOOR_GB,
    SHED_AT_DEFAULT,
    UNBRAKED,
    AdmissionPressure,
    MachineBrake,
    MachineSignal,
    PressureBand,
    QuotaSignal,
    admission_pressure,
    box_load_headroom,
    ram_headroom,
    resolve_shed_at,
    weekly_pace,
)
from teatree.utils import ram_scope

logger = logging.getLogger(__name__)

_MIB_PER_GB = 1024.0

#: WRITE concurrency as a function of cores, not a magic number, so a bigger box scales
#: up automatically. 8 cores → 4.
#:
#: This was 0.25 (8 cores → 2), calibrated against the meltdown recorded on
#: :data:`TOTAL_TEST_WORKERS_PER_CORE` below — which names its own cause: "the per-agent
#: expansion is the melt driver, NOT the agent count". That driver is now bounded
#: independently by :func:`per_agent_test_workers`, which divides a ``cores * 2`` TOTAL
#: worker budget by the active-agent count, so total workers stay bounded however many
#: agents run. The old value was set before that guard existed and priced agent count as
#: if it were the hazard.
#:
#: Raising it is safe to attempt rather than safe by assertion: the load brake still denies
#: above ``BRAKE_LOAD_PER_CORE * cores`` and holds to ``RESUME_LOAD_PER_CORE * cores``, so an
#: over-aggressive value throttles itself instead of melting the box. Measured at the change:
#: load 13.4/15.9/16.5 on 8 cores against a deny watermark of 40, 14 GB RAM free.
WRITE_CONCURRENCY_PER_CORE = 0.5

#: Total test workers across ALL concurrent agents, as a multiple of cores. The measured
#: meltdown was 12 agents x auto-detected 8 workers ≈ 96 workers at load ~70: the
#: per-agent expansion is the melt driver, not the agent count.
TOTAL_TEST_WORKERS_PER_CORE = 2

#: TOTAL host agent population per core — deliberately its own constant rather than the
#: per-lane :data:`WRITE_CONCURRENCY_PER_CORE`. A lane's concurrency bounds that lane; the
#: population a session RESTORE re-creates is a whole-box fact, and pricing it off a lane
#: setting is exactly the conflation #4108 records (a lane capped at 3 while the box carried
#: enough agents to reach load 58 on 8 cores).
HOST_AGENT_POPULATION_PER_CORE = 1.0

#: Yield-per-token: high burn producing nothing is the waste that matters. Below this
#: completion ratio the marginal token buys zero, so the governor STOPS admitting rather
#: than throttling slightly. Fewer than ``YIELD_MIN_SAMPLES`` terminal tasks is unknown,
#: and unknown never brakes.
YIELD_COLLAPSE_RATIO = 0.2
YIELD_MIN_SAMPLES = 5

#: Open unmerged PRs above which a zero-merge window is inventory pile-up rather than a
#: quiet day. Below it "nothing merged" is unremarkable and must never brake.
MERGE_STALL_MIN_OPEN_PRS = 3

#: Consecutive merge-sweep refusals before a PR counts as STUCK rather than merely
#: waiting. Matches the aged-skip surfacing threshold, so the two agree on the word.
MERGE_STUCK_AFTER_TICKS = 3


@dataclass(frozen=True)
class YieldSignal:
    """Terminal task outcomes over the recent window — merged work per token spent."""

    completed: int
    failed: int

    @property
    def samples(self) -> int:
        return self.completed + self.failed

    @property
    def collapsed(self) -> bool:
        if self.samples < YIELD_MIN_SAMPLES:
            return False
        return self.completed / self.samples < YIELD_COLLAPSE_RATIO


@dataclass(frozen=True)
class MergeSignal:
    """Merge throughput — whether produced work is actually LANDING.

    :class:`YieldSignal` asks "did tasks finish?", and a task that finished by opening a
    PR nothing can merge answers yes. This asks what that cannot: did anything merge? A
    factory whose tasks all complete while its PRs pile up is producing inventory, and
    each further coding admission deepens the pile without making any of it likelier to
    land — the state that held four PRs for a day while every loop read green.

    Consumed by the ISSUE-INTAKE gate, not by :func:`decide_admission`: it must stop new
    work being started without touching the ship and review lanes that drain the pile,
    and intake is the only decision point where that distinction exists.

    ``fresh`` is False when the rows could not be read. Unknown never brakes, the same
    rule :func:`read_quota_signal` follows and for the same reason: a probe that cannot
    answer must not be able to halt the factory.
    """

    fresh: bool
    open_prs: int
    stuck_prs: int

    @property
    def stalled(self) -> bool:
        """Every open PR is one the sweep keeps refusing — nothing can land at all."""
        if not self.fresh:
            return False
        return self.open_prs >= MERGE_STALL_MIN_OPEN_PRS and self.stuck_prs >= self.open_prs


@dataclass(frozen=True)
class AdmissionDecision:
    """The verdict a dispatcher acts on. ``reason`` is never empty — refusals are visible.

    ``ceiling`` is always a positive bound, never ``None``: an unbounded lane is not a
    state the governor can express (#4097), and the floor of 1 means it can never
    deadlock the factory to zero either. "The governor has no opinion" is the ABSENCE of
    a decision — :func:`teatree.loop.admission.governor_verdict` returns ``None`` for the
    kill-switch and the failed-probe paths — not a decision carrying an absent ceiling.
    """

    admit: bool
    reason: str
    ceiling: int
    braked: bool


def governor_enabled() -> bool:
    """The default-ON flag; setting ``admission_governor_enabled`` false is the kill-switch.

    Fails OPEN (enabled) is wrong here and fails CLOSED is worse — an unreadable setting
    resolves through the ordinary config resolver, which already degrades to the
    dataclass default (``True``). The kill-switch is an explicit operator row, so it is
    never the accidental answer.
    """
    from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: avoids a config import cycle

    return bool(get_effective_settings().admission_governor_enabled)


def per_agent_test_workers(
    *,
    cores: int,
    active_agents: int,
    ram_available_gb: float | None = None,
    per_worker_gb: float = 0.0,
) -> int:
    """Per-agent pytest worker count, so the TOTAL stays bounded however many agents run.

    Exported to a child agent as ``PYTEST_XDIST_AUTO_NUM_WORKERS``, which is how
    pytest-xdist resolves ``-n auto`` — so the addopts stay untouched and a human
    running the suite alone still gets the whole box.

    Cores alone sized the TOTAL at ``cores * 2``, which on 8 cores is 16 workers — 10.4 GB
    at the measured p50 worker RSS but 19.8 GB at the p90, against 19.7 GB usable (#4163).
    The bound is safe at the median and over the line at the tail, which is why the OOMs
    were relentless but intermittent. *ram_available_gb* (the live cgroup-aware reading)
    and *per_worker_gb* (the operator's ``test_worker_ram_gb``) add the term that makes
    the total respond to the resource that actually saturates: whichever of the two totals
    is smaller wins. Both are applied to the TOTAL, never to the per-agent share — a
    per-agent memory budget would hand each of N agents the whole box.

    Omitting either is the fail-safe path, and it is BOUNDED rather than closed: the
    result is exactly the cores-derived bound, never a manufactured clamp to 1. Denying
    on an unreadable ``/proc`` file is a kill switch operated by a missing file (#4097),
    and the pure function is deliberately config-free — the consumer resolves the setting
    and passes it in.

    The share floors at 1 (an agent with zero test workers cannot run its suite), so the
    total bound holds while *active_agents* stays within the admission ceiling — which
    is the other half of the same governor and is far below ``cores * 2``. Past that the
    floor wins: a 50-agent box is already a governor failure, not a division problem.
    """
    total = max(1, int(cores)) * TOTAL_TEST_WORKERS_PER_CORE
    if ram_available_gb is not None and per_worker_gb > 0:
        budget = math.floor(max(0.0, ram_available_gb - RAM_BRAKE_FLOOR_GB) / per_worker_gb)
        total = min(total, budget)
    return max(1, total // max(1, int(active_agents)))


def _machine_ceiling(machine: MachineSignal) -> int:
    """The core-derived WRITE default, floored at 1 — the part that needs NO quota signal."""
    return max(1, math.floor(max(1, machine.cores) * WRITE_CONCURRENCY_PER_CORE))


def _adaptive_ceiling(quota: QuotaSignal, machine: MachineSignal) -> int:
    """The live ceiling: the core-derived WRITE default, scaled by weekly pace, floored at 1."""
    return max(1, math.floor(_machine_ceiling(machine) * min(1.0, weekly_pace(quota))))


def _shed_at() -> float:
    """The operator's SHED threshold, clamped; an unreadable setting keeps the shipped default."""
    from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: avoids a config import cycle

    try:
        configured = float(get_effective_settings().admission_pressure_shed_at)
    except Exception:
        logger.exception("admission_pressure_shed_at unreadable — keeping the shipped default")
        return SHED_AT_DEFAULT
    return resolve_shed_at(configured)


def pressure_for(
    *, quota: QuotaSignal, machine: MachineSignal, load_brake: MachineBrake = UNBRAKED
) -> AdmissionPressure:
    """The live scalar for these signals, carrying the operator's SHED threshold.

    The ONE seam every consumer of the band reads, so the threshold is resolved in one
    place and a lane cannot end up judging its own band against a different one. The
    pure arithmetic stays config-free in :mod:`teatree.core.admission_pressure`; this
    wrapper is only the config half plus the :class:`MachineBrake` translation.
    """
    return admission_pressure(
        quota=quota,
        machine=machine,
        braked=load_brake.braked,
        machine_applies=load_brake.applies,
        shed_at=_shed_at(),
    )


def decide_admission(
    *,
    quota: QuotaSignal,
    machine: MachineSignal,
    yield_signal: YieldSignal | None = None,
    load_brake: MachineBrake = UNBRAKED,
    static_ceiling: int | None = None,
) -> AdmissionDecision:
    """Decide whether to admit new work now, and at what ceiling.

    Order is the owner's: token budget first, machine pressure second, yield third.
    Merge throughput is deliberately NOT here — see :class:`MergeSignal`: it gates new
    ISSUE INTAKE only, because the ship and review lanes are what CLEAR a backed-up
    pipeline and braking them on the backlog they exist to drain would deadlock the
    factory against itself.
    *load_brake* carries the caller's two machine-brake inputs (see :class:`MachineBrake`)
    — the previous decision's brake state, for hysteresis, and whether the brake applies
    to this class at all. *static_ceiling* is the operator's configured concurrency,
    applied as an upper BOUND on whichever ceiling the signals produce rather than as
    the target.

    An UNKNOWN quota is the conservative case, never the unbounded one (#4097): the
    ceiling falls back to :func:`_machine_ceiling`, which reads only the machine signal
    the governor DID read successfully, so not knowing the budget can never buy more
    concurrency than knowing it is healthy. Only the weekly-pace scaling on top of that
    base genuinely needs a fresh quota, and that is exactly what is dropped. The load
    brake reads its own signal and still applies either way.

    The refusal is ONE ``HALT`` test rather than six, and reproduces the pre-#4508 brake
    set exactly — ``1.0`` is each dimension's own watermark (#4508). Only ``HALT`` is
    read here: this decision is class-BLIND, so the ``SHED`` band that refuses the
    expensive class alone is resolved by
    :func:`~teatree.core.agent_admission.agent_admission_verdict`, the seam that already
    owns the cheap/expensive split.
    """
    ceiling = _adaptive_ceiling(quota, machine) if quota.fresh else _machine_ceiling(machine)
    if static_ceiling is not None:
        ceiling = max(1, min(ceiling, static_ceiling))

    pressure = pressure_for(quota=quota, machine=machine, load_brake=load_brake)
    if pressure.band is PressureBand.HALT:
        return AdmissionDecision(admit=False, reason=pressure.reason, ceiling=ceiling, braked=True)

    if yield_signal is not None and yield_signal.collapsed:
        reason = (
            f"yield collapsed ({yield_signal.completed}/{yield_signal.samples} terminal tasks completed) — "
            "the marginal token is buying zero"
        )
        return AdmissionDecision(admit=False, reason=reason, ceiling=ceiling, braked=True)

    return AdmissionDecision(
        admit=True, reason=f"admitting up to {ceiling} — signals healthy", ceiling=ceiling, braked=False
    )


def resume_agent_ceiling(machine: MachineSignal) -> int:
    """How many background agents the box can carry RIGHT NOW, floored at 1 (#4108).

    A session resume restores the whole previously-running set in one step: the stagger the
    orchestrator applied was a property of the DISPATCH, not of the agents, so it is not
    replayed. The restore is therefore not covered by any dispatch-side ceiling and needs its
    own bound — and that bound is read live, because the box's spare capacity is what decides
    whether a fleet is survivable, not a number set when it was quiet.

    ``cores * HOST_AGENT_POPULATION_PER_CORE``, scaled by the SMALLER of two headrooms so
    the tighter resource decides: the load still left under the same
    :data:`BRAKE_LOAD_PER_CORE` watermark the dispatch lanes brake on, and the memory still
    left above the same :data:`RAM_BRAKE_FLOOR_GB` floor they refuse at — the recorded
    incident was load 58 AND 1 GB free, and a bound that reads only load calls that half
    healthy. The memory term ramps between the two RAM watermarks and is inert above the
    upper one, so it lowers the ceiling only on a box that is genuinely tight; an unread
    reading leaves the ceiling load-derived, never lower.

    Deliberately NOT the whole #4508 scalar: its consumer ``hooks/scripts/resume_admission.py``
    reads Django-free on the ``SessionStart`` hot path, and the quota half needs the ORM. So
    this path keeps the two machine terms, which are exactly ``1 - `` their components.

    The floor of 1 keeps this an admission ceiling rather than a kill switch: a wedged box
    still gets to carry the one agent that might unwedge it.
    """
    base = max(1, math.floor(max(1, machine.cores) * HOST_AGENT_POPULATION_PER_CORE))
    headroom = min(
        box_load_headroom(load1=machine.load1, cores=machine.cores),
        ram_headroom(machine.ram_available_gb),
    )
    return max(1, math.floor(base * headroom))


def resume_shed_directive(*, restored: int, machine: MachineSignal) -> str:
    """The shed instruction for an over-ceiling restore, or ``""`` when the fleet fits.

    Empty at or under the ceiling, so a resume on an idle host is unchanged. Over it the
    string names the restored count AND the live ceiling that count is being judged against —
    a refusal that does not say what it measured is the silent-brake failure the rest of this
    module exists to avoid.
    """
    ceiling = resume_agent_ceiling(machine)
    if restored <= ceiling:
        return ""
    return (
        f"ADMISSION — RESTORED FLEET OVER CEILING. This resume brought back {restored} background "
        f"agents; the live machine carries {ceiling} (load {machine.load1:.0f} on {max(1, machine.cores)} "
        "cores). The ramp that paced this fleet belonged to the dispatch, not the agents, so the "
        "restore replayed it in one step. Shed down to the ceiling — stop or collect the surplus "
        "agents — BEFORE dispatching anything new."
    )


def read_merge_signal(*, overlay: str = "", stuck_after: int = MERGE_STUCK_AFTER_TICKS) -> MergeSignal:
    """Open PRs, and how many of them the merge sweep keeps refusing.

    Scoped to *overlay* because the gate it feeds is per-overlay: counting globally lets a
    stall in one overlay brake intake in another, which is a silent cross-tenant brake that
    a single-overlay box can never show.

    A streak only counts when it names a PR that is STILL LIVE. ``SweepSkipStreak.resolve``
    fires only on a live ``pr_sweep.*`` signal for that exact ``(slug, pr_id)``, so a PR that
    merged or closed outside the sweep leaves its row behind forever. Counting rows
    independently of the live set lets those fossils outnumber real PRs and brake a pipeline
    in which every open PR is healthy.

    Both slugs are folded to lower case before they are compared, the repo-wide rule
    :meth:`~teatree.core.models.pull_request.PullRequestQuerySet.for_pr` states: a forge slug
    is case-insensitive, so a streak recorded as ``Owner/Repo`` names the very PR the live
    set holds as ``owner/repo``. Matching exactly drops every such streak, ``stuck_prs``
    undercounts and the brake never fires — failing toward MORE claiming, the one direction
    this gate exists to prevent.

    Both sides are then counted over that ONE de-duplicated key space. ``SweepSkipStreak``
    is unique on ``(slug, pr_id)`` case-SENSITIVELY, so ``Owner/Repo``#800 and
    ``owner/repo``#800 are two legal rows naming one PR: counting rows against a live set
    counted by key lets that PR contribute 2 to ``stuck_prs`` and 1 to ``open_prs``, and
    enough such pairs push ``stuck_prs >= open_prs`` while healthy PRs are still moving —
    a brake fired on arithmetic, which is the silent stall this gate exists to catch.

    A read that raises returns ``fresh=False`` — unknown never brakes, because a probe that
    cannot answer must not be able to halt the factory.
    """
    from teatree.core.models import PullRequest, SweepSkipStreak  # noqa: PLC0415 — deferred: ORM app registry

    try:
        live = PullRequest.objects.live()
        streaks = SweepSkipStreak.objects.aged(threshold=stuck_after)
        if overlay:
            live = live.filter(overlay=overlay)
            streaks = streaks.filter(overlay=overlay)
        live_keys = {
            (str(repo).lower(), int(iid)) for repo, iid in live.values_list("repo", "iid") if str(iid).isdigit()
        }
        streak_keys = {(str(slug).lower(), int(pr_id)) for slug, pr_id in streaks.values_list("slug", "pr_id")}
        stuck = len(streak_keys & live_keys)
    except Exception:
        logger.exception("merge-throughput probe failed — reporting unknown, which never brakes")
        return MergeSignal(fresh=False, open_prs=0, stuck_prs=0)
    return MergeSignal(fresh=True, open_prs=len(live_keys), stuck_prs=stuck)


def read_machine_signal(*, ram_available_gb: float | None = None) -> MachineSignal:
    """The deterministic, model-free machine probe (stdlib only, no external process).

    Memory comes from :attr:`~teatree.utils.ram_scope.RamHeadroom.box_watermark_mib`, the ONE
    cgroup-aware reader. Reading ``/proc/meminfo`` here instead would report the HOST figure
    inside a container and would be invisible to that reader's own fix (#4118).

    That reading is SCOPE-QUALIFIED, which the watermarks this signal feeds require (#4217):
    :data:`RAM_BRAKE_FLOOR_GB` and :data:`RAM_RESUME_FLOOR_GB` are absolute box-wide numbers,
    so a reading taken in a cgroup too small to host an agent workload is not theirs to judge
    — the admin sidecar read 1.65 GB at the same instant the worker read 15.88 GB, and its
    fixed 2 GiB cap put every dispatch under a floor it could never rise above. Such a scope
    falls back to the host component, which is box-wide at any cap, so a genuinely starved box
    still brakes from inside a sidecar (#4252). ``None`` (nothing box-scoped readable) is a
    different answer from ``0`` (readable, nothing left), carried through rather than collapsed
    to a number nobody measured.

    An explicit *ram_available_gb* wins, so a caller holding a reading of its own is never
    made to pay for a second probe — which is also how a caller wanting only the load
    average gets it without a second ``/proc`` read, since this is its one reader. A
    platform with no load average reads ``0.0``: an unknown load is inert wherever it is
    consumed (no brake, full :func:`box_load_headroom`), never a manufactured clamp.
    """
    try:
        load1 = os.getloadavg()[0]
    except OSError:
        load1 = 0.0
    if ram_available_gb is None:
        available_mib = ram_scope.read_ram_headroom().box_watermark_mib
        ram_available_gb = None if available_mib is None else available_mib / _MIB_PER_GB
    return MachineSignal(cores=os.cpu_count() or 1, load1=load1, ram_available_gb=ram_available_gb)


def read_quota_signal(now: dt.datetime | None = None) -> QuotaSignal:
    """The cached per-account rate-limit health, folded into one signal.

    Reads the ``AnthropicTokenUsage`` cache the routing selector already maintains — no
    network probe, no model. Two questions with two different evidence bars:

    HEADROOM (utilization, pace, ceiling) comes from the FRESH, non-exhausted rows. A
    usable account knows its own week whatever a lapsed peer is doing, so one unknown
    row must not blank the pace brake and the adaptive ceiling.

    EXHAUSTION (``all_accounts_exhausted``) needs the WHOLE fleet fresh, because it is a
    claim about EVERY account and no such claim survives an unknown. The asymmetry is
    forced by ``valid_until``, the routing cache's "may I skip a re-probe?" rule: a
    healthy verdict lapses after ``HEALTH_TTL`` (minutes), an exhausted one is trusted
    until its blocking window resets (days). So an exhausted row is fresh by
    construction, and a STALE row means "not currently known-blocked" — safe to admit
    against, never evidence that the fallthrough has nowhere left to go.

    When no fresh row is usable and the fleet is not known-exhausted, the healthy
    accounts' headroom is simply unknown: ``fresh=False``, and the decision falls back to
    the machine-derived ceiling. Reporting the surviving exhausted row's 100% instead
    would brake the lane on a row that proves nothing about the account being used.
    """
    from django.utils import timezone  # noqa: PLC0415 — deferred: Django app-registry read at call time

    from teatree.core.models.anthropic_token_usage import AnthropicTokenUsage  # noqa: PLC0415 — deferred: same

    moment = now or timezone.now()
    rows = list(AnthropicTokenUsage.objects.all())
    fresh_rows = [row for row in rows if row.is_fresh(moment)]
    all_exhausted = bool(rows) and len(fresh_rows) == len(rows) and all(row.is_exhausted for row in rows)
    sample = [row for row in fresh_rows if not row.is_exhausted] or (rows if all_exhausted else [])
    if not sample:
        return QuotaSignal(
            fresh=False,
            all_accounts_exhausted=False,
            weekly_utilization=0.0,
            short_utilization=0.0,
            seconds_to_weekly_reset=None,
        )
    best = min(sample, key=lambda row: row.utilization_7d)
    reset = best.reset_7d
    return QuotaSignal(
        fresh=True,
        all_accounts_exhausted=all_exhausted,
        weekly_utilization=best.utilization_7d,
        short_utilization=min(row.utilization_5h for row in sample),
        seconds_to_weekly_reset=(reset - moment).total_seconds() if reset is not None else None,
    )


#: Re-exported from :mod:`teatree.core.admission_pressure`, which owns the signals and the
#: watermarks they normalise against: this stays the one import site every caller already
#: uses, so the #4508 split moved no consumer.
__all__ = [
    "BRAKE_LOAD_PER_CORE",
    "RAM_BRAKE_FLOOR_GB",
    "RAM_RESUME_FLOOR_GB",
    "UNBRAKED",
    "AdmissionDecision",
    "AdmissionPressure",
    "MachineBrake",
    "MachineSignal",
    "PressureBand",
    "QuotaSignal",
    "YieldSignal",
    "box_load_headroom",
    "decide_admission",
    "governor_enabled",
    "per_agent_test_workers",
    "pressure_for",
    "ram_headroom",
    "read_machine_signal",
    "read_quota_signal",
    "resume_agent_ceiling",
    "resume_shed_directive",
    "weekly_pace",
]
