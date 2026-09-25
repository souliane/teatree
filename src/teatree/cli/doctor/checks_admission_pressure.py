"""``_check_*`` probes for admission pressure and lane occupancy, invoked by `t3 doctor check`.

Split out of :mod:`teatree.cli.doctor.checks_loop` when the two grew past the module
health cap together (#1983 ratchet). These are the checks that read the admission
governor's view of the box — intake budget, pressure band, lane occupancy and drain
starvation — as opposed to loop *scheduling* staleness, which stays in ``checks_loop``.

Each helper is narrow (single concern, single ``typer.echo`` path) and returns
``bool`` for pass/fail aggregation by :func:`teatree.cli.doctor.run_checks.run_doctor_checks`.
"""

import os
from datetime import timedelta
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from teatree.core.admission_pressure import MachineSignal

_MAX_STALL_MINUTES = 24 * 60


def _check_intake_budget_deadlock() -> bool:
    """FAIL when issue intake sits at a full budget with nothing progressing (#3978).

    Two stalled claims at a budget of two stop the factory admitting any work at all,
    and nothing surfaces it: a full budget makes the scanner factory return ``None``, so
    the tick does nothing and reports success while labelled issues pile up unreachable.

    The signature is a full budget held ENTIRELY by claims with no active task and no
    open PR — busy is normal, going nowhere is not. Unlike ``_check_marker_jam`` (which
    warns only once a release grace has already expired) this HARD-FAILs, and fires
    while the graces are still running. Crash-proof: a broken read degrades to OK with a
    WARN, so a doctor run never reddens on the alarm's own failure.

    Read-only, deliberately: the "is this holder's PR still open?" evidence comes from
    ``PullRequest.state``, and keeping that field honest belongs to the writers — the
    merge chokepoint stamps it the instant a merge lands, and the intake tick reconciles
    the holders' rows against the forge before every budget read (#3984). Probing the
    forge here instead would let a diagnostic mutate the state it reports on.
    """
    from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: keeps CLI startup light
    from teatree.core.intake.budget import (  # noqa: PLC0415 — deferred: keeps CLI startup light
        IntakeBudget,
        read_intake_budget,
    )
    from teatree.core.intake.concurrency import resolve_intake_concurrency  # noqa: PLC0415 — deferred: same
    from teatree.core.models import ImplementedIssueMarker  # noqa: PLC0415 — ORM import needs the app registry

    try:
        occupied = (
            ImplementedIssueMarker.objects.exclude(state__in=ImplementedIssueMarker.State.terminal())
            .values_list("overlay", flat=True)
            .distinct()
        )
        jammed: list[IntakeBudget] = []
        for overlay in sorted(occupied):
            settings = get_effective_settings(overlay)
            # The LIVE limit, never the static setting: the resource loop may have moved
            # it (#3992), and a doctor reading a different number than the gate is the
            # second opinion this whole surface exists to prevent.
            limit = resolve_intake_concurrency(settings.issue_implementer_max_concurrent, overlay=overlay)
            budget = read_intake_budget(overlay, limit, static_limit=settings.issue_implementer_max_concurrent)
            if budget.deadlocked:
                jammed.append(budget)
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(f"WARN  Intake-budget deadlock check crashed: {exc.__class__.__name__}: {exc}")
        return True
    for budget in jammed:
        typer.echo(
            f"FAIL  {budget.report()} — no held slot is progressing, so no new issue can be admitted; "
            "free the budget with `t3 loop reclaim-markers` (#3978)."
        )
    return not jammed


def _pressure_reading(machine: "MachineSignal") -> str:
    """The admission scalar beside the load line, or ``""`` when it cannot be read (#4508).

    Load alone cannot explain a refusal: the dimensions that halt this factory most often
    are quota-shaped and leave the load average looking healthy. Naming the band and its
    dominant cause is what turns "why is nothing being admitted?" into one line.
    """
    from teatree.core.admission_governor import (  # noqa: PLC0415 — deferred: keeps CLI startup light
        pressure_for,
        read_quota_signal,
    )

    try:
        pressure = pressure_for(quota=read_quota_signal(), machine=machine)
    except Exception:  # noqa: BLE001 — doctor check must never crash the run
        return ""
    cause = f", worst: {pressure.dominant.name}" if pressure.dominant is not None else ""
    return f"; admission pressure {pressure.value:.2f} {pressure.band.name}{cause}"


def _check_box_occupancy() -> bool:
    """Report the factory's own agent count BESIDE the whole box's load (#4407).

    Every other intake/agent surface counts what the FACTORY is running, so a box
    saturated by anything else — an orchestrating session's harness sub-agents claim no
    ``Task`` and hold no intake marker — reads healthy on all of them at once. The
    recorded shape is load 14 → 53 and 16 GB → 1 GB free with the factory correctly
    holding intake at 3, every chip green, and zero merges for over an hour.

    Always prints both numbers, because the actionable thing is the DIFFERENCE between
    them, and a check that speaks only when it is unhappy cannot show one. Advisory: a
    WARN at or over the same :data:`~teatree.core.admission_governor.BRAKE_LOAD_PER_CORE`
    watermark the governor brakes on, never a FAIL — foreign load is a fact about the
    machine, not a fault in the factory, and reddening the run on it would train the
    operator to ignore a red run. Crash-proof: any error degrades to OK.
    """
    from teatree.core.admission_governor import (  # noqa: PLC0415 — deferred: keeps CLI startup light
        BRAKE_LOAD_PER_CORE,
        read_machine_signal,
    )
    from teatree.core.models import Task  # noqa: PLC0415 — ORM import needs the app registry

    try:
        machine = read_machine_signal()
        agents = Task.objects.claimed_agent_count()
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(f"WARN  Box-occupancy check crashed: {exc.__class__.__name__}: {exc}")
        return True
    cores = max(1, machine.cores)
    watermark = BRAKE_LOAD_PER_CORE * cores
    reading = (
        f"factory agents in flight: {agents}; box load {machine.load1:.1f} on {cores} core(s) "
        f"({machine.load1 / cores:.1f} per core, brake watermark {watermark:.0f}){_pressure_reading(machine)}"
    )
    if machine.load1 < watermark:
        typer.echo(f"OK    Box occupancy — {reading}")
        return True
    typer.echo(
        f"WARN  Box saturated while the factory's own accounting reads healthy — {reading}. "
        "The surplus is work the factory did not start and cannot retire; shed it before "
        "expecting throughput (#4407)."
    )
    return True


def _check_orphaned_process_groups() -> bool:
    """WARN on each process group whose leader is gone and which is still burning (#4580).

    The explanation ``_check_box_occupancy`` above cannot give. One such group — 37 shells
    left by a fuzz run whose mutation corrupted a loop's own exit keyword — ran 9 days 10
    hours and removed ~58% of admitted capacity. Nothing owned it, nothing reaped it, and
    it read harmless on every per-process metric: niced, mostly sleeping, ~0.0% CPU. Load
    average counts runnable threads rather than CPU, and load average is what the governor
    throttles on, so the report names the load cost beside the numbers that look fine.

    Advisory, exactly like its sibling: a leaderless group is a fact about the machine, and
    reddening the run on one would train the operator to ignore a red run. A gap is
    reported rather than swallowed — an unreadable table must never read as "no orphans".
    Crash-proof: any error degrades to OK.
    """
    from teatree.core.cleanup.orphan_process_groups import (  # noqa: PLC0415 — deferred: keeps CLI startup light
        min_age_seconds_setting,
        survey_orphan_groups,
    )

    try:
        survey = survey_orphan_groups(min_age_seconds=min_age_seconds_setting())
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(f"WARN  Orphaned-process-group check crashed: {exc.__class__.__name__}: {exc}")
        return True
    for gap in survey.gaps:
        typer.echo(f"WARN  Orphaned-group scan is blind here: {gap} (#4580).")
    for group in survey.groups:
        typer.echo(
            f"WARN  Leaderless process group stealing admitted capacity — {group.report()}. "
            "Per-process CPU can read near-zero while the group stays runnable, and load "
            "average — not CPU — is what the admission governor throttles on. Reclaim with: "
            f"{group.remedy()} (#4580).",
        )
    return True


def _check_starved_intake_candidates() -> bool:
    """WARN on each issue intake keeps judging admissible and never has the budget to claim.

    The complement of :func:`_check_intake_budget_deadlock`, which fires only when the
    held slots are going nowhere. A budget that turns over correctly and hands every
    freed slot to a newer issue is a healthy-looking factory with an issue in it that
    never starts — three of them sat a full day with every surface green (#4238).

    Reads the ledger the intake scanner syncs on each discovery pass, so it costs no
    forge call and names an issue only while it is STILL waiting. Advisory: age ordering
    is what stops the starvation, and a long wait behind a full budget is legitimate —
    the operator needs to see it, not have the doctor go red over it. Crash-proof: any
    error degrades to OK.
    """
    from teatree.config import get_effective_settings  # noqa: PLC0415 — lazy CLI import
    from teatree.core.intake.budget import read_intake_budget  # noqa: PLC0415 — lazy CLI import
    from teatree.core.intake.concurrency import resolve_intake_concurrency  # noqa: PLC0415 — lazy CLI import
    from teatree.core.models import UnclaimedIntakeCandidate  # noqa: PLC0415 — ORM import needs the app registry

    try:
        starved = list(UnclaimedIntakeCandidate.objects.starved())
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(f"WARN  Starved-intake check crashed: {exc.__class__.__name__}: {exc}")
        return True
    if not starved:
        return True
    for row in starved:
        typer.echo(f"WARN  Intake starvation: {row.report()}")
    for overlay in sorted({row.overlay for row in starved}):
        try:
            settings = get_effective_settings(overlay)
            static = settings.issue_implementer_max_concurrent
            limit = resolve_intake_concurrency(static, overlay=overlay)
            budget = read_intake_budget(overlay, limit, static_limit=static)
            typer.echo(
                f"WARN  {overlay} intake: effective concurrency {limit} "
                f"(configured {static}), {budget.in_flight} in-flight slot(s); "
                f"{budget.report()}"
            )
        except Exception as exc:  # noqa: BLE001 — context failure must not hide the starved issues
            typer.echo(f"WARN  {overlay} intake: effective concurrency/in-flight unreadable ({exc.__class__.__name__})")
    typer.echo(
        f"WARN  {len(starved)} issue(s) have been admissible and unclaimed past the threshold. "
        "Intake claims oldest-filed first; inspect the effective concurrency and in-flight "
        "holders before changing the throttle (#4238).",
    )
    return False


def _check_drain_lane_starved() -> bool:
    """WARN when reviewing/shipping work is queued, none of it is running, and it has waited (#4374).

    The signature of a factory that has stopped moving while every surface reads healthy:
    the worker is busy, the loop ticks, no error is raised — and the queued work that would
    RETIRE a pull request cannot get in behind expensive work that only creates more.
    Advisory: the reservation is what prevents the state, this only names it, and a box run
    deliberately at ``drain_slot_reservation = 0`` should not go red for it. Crash-proof:
    any error degrades to OK.
    """
    from teatree.core.factory.drain_starvation import read_drain_lane_state  # noqa: PLC0415 — ORM read at call time

    try:
        state = read_drain_lane_state()
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(f"WARN  Drain-lane starvation check crashed: {exc.__class__.__name__}: {exc}")
        return True
    if not state.starved:
        return True
    typer.echo(
        f"WARN  {state.report()}. Reserve more capacity for it with "
        "`t3 <overlay> config_setting set drain_slot_reservation <n>` (#4374).",
    )
    return False


def _queue_stall_minutes() -> int:
    """Bound a configurable floor so an invalid override cannot disable the alarm."""
    try:
        minutes = int(os.environ.get("TEATREE_QUEUE_STALL_MINUTES", "30"))
    except ValueError:
        return 30
    return minutes if 1 <= minutes <= _MAX_STALL_MINUTES else 30


def _check_queue_stall() -> bool:
    """FAIL when queued work has gone unclaimed for a sustained interval.

    The oldest pending row supplies the age floor. A current claim or a recently
    finished real attempt is evidence of claim progress, so neither state is a
    stall. The reason is diagnostic only: missing OTel data cannot hide a stalled
    queue, and a specific brake is never baked into the stall predicate.
    """
    from django.db.models import Count, Min  # noqa: PLC0415 — ORM at check time
    from django.utils import timezone  # noqa: PLC0415 — ORM at check time

    from teatree.core.models import Task, TaskAttempt  # noqa: PLC0415 — ORM import needs app registry
    from teatree.core.models.usage_window_state import LIMIT_PARKED_PREFIX  # noqa: PLC0415
    from teatree.core.telemetry.admission import latest_admission_reason  # noqa: PLC0415 — lazy CLI import

    now = timezone.now()
    minutes = _queue_stall_minutes()
    cutoff = now - timedelta(minutes=minutes)
    try:
        queue = Task.objects.filter(status=Task.Status.PENDING).aggregate(count=Count("pk"), oldest=Min("created_at"))
        pending = queue["count"]
        oldest = queue["oldest"]
        if not pending or oldest is None or oldest >= cutoff:
            return True
        if Task.objects.filter(status=Task.Status.CLAIMED).exists():
            return True
        if TaskAttempt.objects.filter(started_at__gte=cutoff).exclude(error__startswith=LIMIT_PARKED_PREFIX).exists():
            return True
    except Exception as exc:  # noqa: BLE001 — an unreadable DB is diagnosed by its own doctor gate
        typer.echo(f"WARN  Queue-stall check crashed: {exc.__class__.__name__}: {exc}")
        return True

    try:
        reason = latest_admission_reason()
    except Exception:  # noqa: BLE001 — no telemetry must not mask the stalled queue
        reason = None
    explanation = reason or "no recent admission decision recorded"
    age_minutes = int((now - oldest).total_seconds() // 60)
    typer.echo(
        f"FAIL  Queue stalled: {pending} pending task(s), oldest waiting {age_minutes}m, "
        f"zero claimed/running for at least {minutes}m; latest admission decision: {explanation}"
    )
    return False


def _check_merge_brake() -> bool:
    """FAIL when a whole overlay's live PR board is stopping new issue intake."""
    from teatree.core.admission_governor import read_merge_signal  # noqa: PLC0415 — lazy CLI import
    from teatree.core.models import PullRequest  # noqa: PLC0415 — ORM import needs app registry

    try:
        overlays = set(PullRequest.objects.live().values_list("overlay", flat=True).distinct())
        stalled = [(overlay, read_merge_signal(overlay=overlay)) for overlay in sorted(overlays)]
    except Exception as exc:  # noqa: BLE001 — an unreadable probe is not proof of a halt
        typer.echo(f"WARN  Merge-brake check crashed: {exc.__class__.__name__}: {exc}")
        return True
    failures = [(overlay, signal) for overlay, signal in stalled if signal.stalled]
    for overlay, signal in failures:
        refs = ", ".join(signal.stuck_refs)
        typer.echo(
            f"FAIL  Issue intake halted by merge brake for {overlay or 'default'}: "
            f"{signal.stuck_prs}/{signal.open_prs} live PRs stuck — {refs}. "
            "Clear or recheck these PRs before claiming new issues."
        )
    return not failures
