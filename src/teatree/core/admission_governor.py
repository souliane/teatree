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

Ships behind the default-ON ``admission_governor_enabled`` setting; setting it false is
the kill-switch and the rollback lever (see :func:`governor_enabled`).
"""

import datetime as dt
import logging
import math
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

WEEKLY_WINDOW_SECONDS = 7 * 24 * 3600

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

#: Load watermarks, as multiples of the core count. Above ``BRAKE`` new admissions are
#: denied; a braked governor only re-admits once load falls back under ``RESUME``. The
#: gap is the hysteresis that stops it flapping around one threshold.
BRAKE_LOAD_PER_CORE = 5.0
RESUME_LOAD_PER_CORE = 3.0

#: A 5h window this spent is an imminent hard rate-limit; retrying into one is pure burn.
SHORT_WINDOW_BRAKE = 0.95
#: Weekly headroom below this is spent — nothing left to admit against.
WEEKLY_WINDOW_BRAKE = 0.99

#: Pace = weekly headroom / weekly runway. Below 1 the burn outruns the window, so the
#: ceiling is scaled down to land AT the reset instead of sprinting to zero. Below
#: ``PACE_DENY`` there is not enough left to start anything new.
PACE_DENY = 0.1

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
class QuotaSignal:
    """Live model-quota headroom — the PRIMARY admission signal.

    ``fresh`` is False when no account's headroom is known; the decision then keeps the
    operator's static ceiling rather than trusting a guess.
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
class MachineBrake:
    """The caller's two inputs to the LOAD brake, as one value.

    ``braked`` is the previous decision's brake state and supplies the hysteresis — a
    braked governor is held to the lower watermark so it cannot flap.

    ``applies`` is the cheap-phase exemption (#4098): the read-only phases that RETIRE
    work were being refused on the very load their expensive siblings created, which
    removed the only relief available and held the brake on. ``False`` skips the LOAD
    brake for that class alone — never the token brakes, which are a claim about budget
    rather than pressure and so refuse a cheap phase exactly as they refuse an expensive
    one. The caller supplies the exemption's own bound; :func:`decide_admission` never
    widens a lane on its own.
    """

    applies: bool = True
    braked: bool = False


#: The default: the brake applies, with no prior brake state to hold it to the low watermark.
UNBRAKED = MachineBrake()


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

    ``ceiling is None`` means NO clamp — the governor has no ceiling opinion and the
    caller keeps whatever it had. It is never a synonym for zero.
    """

    admit: bool
    reason: str
    ceiling: int | None
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


def per_agent_test_workers(*, cores: int, active_agents: int) -> int:
    """Per-agent pytest worker count, so the TOTAL stays bounded however many agents run.

    Exported to a child agent as ``PYTEST_XDIST_AUTO_NUM_WORKERS``, which is how
    pytest-xdist resolves ``-n auto`` — so the addopts stay untouched and a human
    running the suite alone still gets the whole box.

    The share floors at 1 (an agent with zero test workers cannot run its suite), so the
    total bound holds while *active_agents* stays within the admission ceiling — which
    is the other half of the same governor and is far below ``cores * 2``. Past that the
    floor wins: a 50-agent box is already a governor failure, not a division problem.
    """
    total = max(1, int(cores)) * TOTAL_TEST_WORKERS_PER_CORE
    return max(1, total // max(1, int(active_agents)))


def _quota_brake(quota: QuotaSignal) -> str:
    if quota.all_accounts_exhausted:
        return "every account is quota-exhausted — retrying into a rate limit is pure burn"
    if quota.weekly_utilization >= WEEKLY_WINDOW_BRAKE:
        return f"weekly window spent ({quota.weekly_utilization:.0%}) — no budget left to admit against"
    if quota.short_utilization >= SHORT_WINDOW_BRAKE:
        return f"5h window spent ({quota.short_utilization:.0%}) — a hard rate limit is imminent"
    if weekly_pace(quota) < PACE_DENY:
        return f"weekly burn outruns the reset (pace {weekly_pace(quota):.2f}) — pacing to the window"
    return ""


def _machine_brake(machine: MachineSignal, *, braked: bool) -> str:
    cores = max(1, machine.cores)
    watermark = (RESUME_LOAD_PER_CORE if braked else BRAKE_LOAD_PER_CORE) * cores
    if machine.load1 >= watermark:
        return f"load {machine.load1:.0f} at/over the {watermark:.0f} watermark on {cores} core(s)"
    return ""


def _adaptive_ceiling(quota: QuotaSignal, machine: MachineSignal) -> int:
    """The live ceiling: the core-derived WRITE default, scaled by weekly pace, floored at 1."""
    base = max(1, math.floor(max(1, machine.cores) * WRITE_CONCURRENCY_PER_CORE))
    scaled = math.floor(base * min(1.0, weekly_pace(quota)))
    return max(1, scaled)


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
    applied as an upper BOUND on the adaptive answer rather than as the target — and it
    is what an unreadable quota probe falls back to VERBATIM, ``None`` (no clamp)
    included. The governor tightens only on evidence it actually has.
    """
    ceiling = static_ceiling
    if quota.fresh:
        ceiling = _adaptive_ceiling(quota, machine)
        if static_ceiling is not None:
            ceiling = max(1, min(ceiling, static_ceiling))

    quota_brake = _quota_brake(quota) if quota.fresh else ""
    machine_brake = _machine_brake(machine, braked=load_brake.braked) if load_brake.applies else ""
    for brake in (quota_brake, machine_brake):
        if brake:
            return AdmissionDecision(admit=False, reason=brake, ceiling=ceiling, braked=True)

    if yield_signal is not None and yield_signal.collapsed:
        reason = (
            f"yield collapsed ({yield_signal.completed}/{yield_signal.samples} terminal tasks completed) — "
            "the marginal token is buying zero"
        )
        return AdmissionDecision(admit=False, reason=reason, ceiling=ceiling, braked=True)

    headroom = "unclamped" if ceiling is None else f"up to {ceiling}"
    return AdmissionDecision(
        admit=True, reason=f"admitting {headroom} — signals healthy", ceiling=ceiling, braked=False
    )


def resume_agent_ceiling(machine: MachineSignal) -> int:
    """How many background agents the box can carry RIGHT NOW, floored at 1 (#4108).

    A session resume restores the whole previously-running set in one step: the stagger the
    orchestrator applied was a property of the DISPATCH, not of the agents, so it is not
    replayed. The restore is therefore not covered by any dispatch-side ceiling and needs its
    own bound — and that bound is read live, because the box's spare capacity is what decides
    whether a fleet is survivable, not a number set when it was quiet.

    ``cores * HOST_AGENT_POPULATION_PER_CORE``, scaled by the load headroom still left under
    the same :data:`BRAKE_LOAD_PER_CORE` watermark the dispatch lanes brake on, so the two
    read one machine the same way. The floor of 1 keeps this an admission ceiling rather than
    a kill switch: a wedged box still gets to carry the one agent that might unwedge it.
    """
    cores = max(1, machine.cores)
    watermark = BRAKE_LOAD_PER_CORE * cores
    headroom = max(0.0, watermark - machine.load1) / watermark
    base = max(1, math.floor(cores * HOST_AGENT_POPULATION_PER_CORE))
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
        stuck = sum(
            1 for slug, pr_id in streaks.values_list("slug", "pr_id") if (str(slug).lower(), int(pr_id)) in live_keys
        )
    except Exception:
        logger.exception("merge-throughput probe failed — reporting unknown, which never brakes")
        return MergeSignal(fresh=False, open_prs=0, stuck_prs=0)
    return MergeSignal(fresh=True, open_prs=len(live_keys), stuck_prs=stuck)


def read_machine_signal(*, ram_available_gb: float | None = None) -> MachineSignal:
    """The deterministic, model-free machine probe (stdlib only, no external process)."""
    try:
        load1 = os.getloadavg()[0]
    except OSError:
        load1 = 0.0
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
    accounts' headroom is simply unknown: ``fresh=False``, and the decision keeps the
    operator's static ceiling. Reporting the surviving exhausted row's 100% instead
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


__all__ = [
    "UNBRAKED",
    "AdmissionDecision",
    "MachineBrake",
    "MachineSignal",
    "QuotaSignal",
    "YieldSignal",
    "decide_admission",
    "governor_enabled",
    "per_agent_test_workers",
    "read_machine_signal",
    "read_quota_signal",
    "resume_agent_ceiling",
    "resume_shed_directive",
    "weekly_pace",
]
