"""``_check_*`` probes for loop / scheduling staleness invoked by `t3 doctor check`.

Each helper is narrow (single concern, single ``typer.echo`` path) and returns
``bool`` for pass/fail aggregation by :func:`teatree.cli.doctor.run_checks.run_doctor_checks`.
"""

import typer

from teatree.loop.preset_resolution import consistency_findings


def _check_loop_presets() -> bool:
    """Warn on a dangling loop-preset reference (#3159): deleted preset / loop / schedule.

    Presets, slots and the active-schedule selector reference loops and presets BY
    NAME, so a deleted target fails open to base config at read time — but the
    dangling reference should still be surfaced. Reports each such finding (never
    repairs). Crash-proof: any error degrades to OK so a doctor run never aborts,
    same posture as the other DB-reading checks.
    """
    try:
        findings = consistency_findings()
    except Exception as exc:  # noqa: BLE001  # doctor check must never crash the run
        typer.echo(f"WARN  Loop-preset consistency check crashed: {exc.__class__.__name__}: {exc}")
        return True  # degrades to OK: a crashed advisory read never reddens the run
    if not findings:
        return True
    for finding in findings:
        typer.echo(f"WARN  Loop preset: {finding}")
    return False


def _check_marker_jam() -> bool:
    """Warn when orphaned issue-markers strand the intake budget (#3275).

    The jam signature: non-terminal ``ImplementedIssueMarker`` rows whose ticket
    is already terminal, gone, or stalled — they never left ``dispatched`` /
    ``ticket_created`` (release-on-completion only fires on the live transition),
    so they permanently consume the in-flight intake budget and no new issue is
    ever claimed. Reads the non-mutating :meth:`find_stale`
    preview across every
    overlay. A WARN (never a hard FAIL): the loop self-heals each tick, and the
    operator can force it now with ``t3 loop reclaim-markers``. Crash-proof: any
    error degrades to OK so a doctor run never aborts on this check.
    """
    from teatree.core.models import ImplementedIssueMarker  # noqa: PLC0415 — ORM import needs the app registry

    try:
        stale = ImplementedIssueMarker.objects.find_stale()
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(f"WARN  Issue-marker jam check crashed: {exc.__class__.__name__}: {exc}")
        return False
    if stale.released == 0:
        return True
    typer.echo(
        f"WARN  {stale.released} orphaned issue-marker(s) hold intake budget but their tickets are "
        f"terminal, gone, stalled, or cancelled ({len(stale.completed)} completed, "
        f"{len(stale.abandoned)} abandoned, {len(stale.declined)} declined) — "
        "run `t3 loop reclaim-markers` to free the issue_implementer budget (#3275)."
    )
    return False


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
            if not settings.issue_implementer_enabled:
                continue
            # The LIVE limit, never the static setting: the resource loop may have moved
            # it (#3992), and a doctor reading a different number than the gate is the
            # second opinion this whole surface exists to prevent.
            limit = resolve_intake_concurrency(settings.issue_implementer_max_concurrent, overlay=overlay)
            budget = read_intake_budget(overlay, limit)
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
    typer.echo(
        f"WARN  {len(starved)} issue(s) have been admissible and unclaimed past the threshold. "
        "Intake claims oldest-filed first, so these are behind a budget that is not freeing "
        "slots fast enough — raise `issue_implementer_max_concurrent` or clear the in-flight "
        "work (#4238).",
    )
    return False


def _check_dream_staleness() -> bool:
    """Warn when the idle-time dream consolidation cron is stale (#1933).

    The dream pass distils session feedback into the ``ConsolidatedMemory``
    ledger; if it stops succeeding, memories pile up unpromoted unnoticed. The
    alarm keys on the last *successful* run (``DreamRunMarker.is_stale``, 48h):
    a run that keeps failing bumps only the attempt timestamp, so staleness
    keeps firing, and bootstrap (never succeeded) is stale by construction. A
    fresh successful pass clears it; the remedy points at scheduling
    ``t3 dream tick`` (which advances the cadence ledger) rather than a one-off
    ``t3 dream run``. Mirrors the SelfUpdateMarker-style marker-staleness alarms.

    Crash-proof: any error (DB offline, unmigrated self-DB) degrades to OK so a
    doctor run never aborts on this check — same posture as the other
    DB-reading doctor checks.
    """
    from django.utils import timezone  # noqa: PLC0415 — deferred: Django import at call time

    from teatree.core.models import DreamRunMarker  # noqa: PLC0415 — deferred: ORM import needs the app registry

    try:
        stale = DreamRunMarker.objects.is_stale(timezone.now())
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(f"WARN  Dream-staleness check crashed: {exc.__class__.__name__}: {exc}")
        return True  # degrades to OK: a crashed advisory read never reddens the run
    if not stale:
        return True
    typer.echo(
        "WARN  Dream consolidation is stale — no successful pass in 48h. "
        "Memories pile up unpromoted; schedule `t3 dream tick` (~04:00 cron) so "
        "the cadence ledger advances, not just a one-off `t3 dream run` (#1933). "
        "If `t3 dream run` reports 0 members, see the transcript-visibility check.",
    )
    return False


def _check_dream_consolidation_blocked() -> bool:
    """Hard-FAIL when a once-working dream pass has been unable to stamp success (#3993).

    The escalation tier over :func:`_check_dream_staleness`'s advisory WARN, whose
    verdict is surfacing-only: a pass can run nightly, fail an acceptance gate and
    withhold the marker indefinitely without any operator-visible signal. A pass that
    once succeeded and has not for ``CRITICAL_STALE_MULTIPLE`` staleness windows is
    structurally blocked, not merely behind, so it gates the doctor exit code.
    Bootstrap (never succeeded) is excluded — see
    :meth:`DreamRunMarkerManager.is_critically_stale`.

    Crash-proof: any error degrades to OK, the same posture as every other DB-reading
    doctor check.
    """
    from django.utils import timezone  # noqa: PLC0415 — deferred: Django import at call time

    from teatree.core.models import DreamRunMarker  # noqa: PLC0415 — deferred: ORM import needs the app registry

    try:
        blocked = DreamRunMarker.objects.is_critically_stale(timezone.now())
        marker = DreamRunMarker.objects.filter(name=DreamRunMarker.NAME).first() if blocked else None
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(f"WARN  Dream-blocked check crashed: {exc.__class__.__name__}: {exc}")
        return True
    if not blocked:
        return True
    succeeded = marker.last_succeeded_at.isoformat() if marker and marker.last_succeeded_at else "never"
    typer.echo(
        f"FAIL  Dream consolidation has not stamped success since {succeeded} — every pass is being "
        "withheld, so no memory or eval candidate is promoted. Run `t3 dream run` and read the "
        "gate verdict it now exits non-zero on (#3993).",
    )
    return False


def _check_dream_transcript_visibility() -> bool:
    """Warn when the dream pass can see NO session transcripts at any age.

    Keys on STRUCTURAL absence (projects dir missing, or zero ``*/*.jsonl`` /
    subagent transcripts regardless of mtime) — not the 48h recency window — so a
    genuinely quiet couple of days never false-alarms. In the Docker factory a
    structurally empty projects dir means the ``~/.claude/projects`` bind mount is
    missing from ``deploy/docker-compose.yml``: every dream pass then finds 0
    members and is a permanent no-op (the marker is never stamped succeeded).
    Complements :func:`_check_dream_staleness` (cadence) — this one names the
    mount as the remedy. Crash-proof: any error degrades to OK.
    """
    from teatree.loops.dream.replay import default_projects_dir  # noqa: PLC0415 — deferred import

    try:
        root = default_projects_dir()
        if root.is_dir() and (any(root.glob("*/*.jsonl")) or any(root.glob("*/*/subagents/agent-*.jsonl"))):
            return True
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(f"WARN  Dream-transcript-visibility check crashed: {exc.__class__.__name__}: {exc}")
        return True  # degrades to OK: a crashed advisory read never reddens the run
    typer.echo(
        f"WARN  Dream sees 0 session transcripts under {root} (any age). In the "
        "Docker factory this means the `~/.claude/projects` bind mount is missing "
        "from deploy/docker-compose.yml — every dream pass finds 0 members and is "
        "a permanent no-op (marker never stamped succeeded).",
    )
    return False


def _check_compose_output_root_pinned() -> bool:
    """Warn when a compose service does not pin the agent output root (#3641).

    ``deploy/entrypoint.sh`` exports ``TMPDIR`` for a service's MAIN process, but
    ``docker exec`` does not run the entrypoint — an exec'd agent's transcripts
    then land under a second, ephemeral root, so every transcript consumer must
    scan both or silently miss half the data. Only an inline ``environment``
    entry reaches an exec'd process. Crash-proof: any error degrades to OK.
    """
    from teatree.cli.doctor.self_heal import _Probe  # noqa: PLC0415 — deferred: lazy CLI import
    from teatree.docker.output_root import (  # noqa: PLC0415 — deferred: lazy CLI import
        OUTPUT_ROOT_ENV,
        services_missing_output_root,
    )
    from teatree.docker.workflow import compose_path  # noqa: PLC0415 — deferred: lazy CLI import

    try:
        clone = _Probe.runtime_clone_root()
        if clone is None:
            return True
        missing = services_missing_output_root(compose_path(clone))
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(f"WARN  Compose-output-root check crashed: {exc.__class__.__name__}: {exc}")
        return True
    if not missing:
        return True
    typer.echo(
        f"WARN  {len(missing)} compose service(s) do not pin {OUTPUT_ROOT_ENV} in their "
        f"`environment` ({', '.join(missing)}). A `docker exec` into them bypasses the "
        f"entrypoint export, so agent output splits across two roots and every "
        f"transcript consumer must scan both. Declare {OUTPUT_ROOT_ENV} per service in "
        "deploy/docker-compose.yml.",
    )
    return False


def _check_loop_classification_drift() -> bool:
    """Warn when a ``Loop`` row's classification disagrees with the shipped table.

    A row is seeded once and never re-read, so a ``colleague_facing`` value that
    outlived a shipped change keeps winning at read time — and a stale ``True``
    is skipped by the away-class admission gate, so the loop stops firing while
    every surface still reports it enabled. The field is admin-editable, so this
    reports rather than repairs; ``seed_loops --reconcile-classification`` writes
    the shipped value back. Crash-proof: any error degrades to OK.
    """
    from teatree.loops.seed_drift import classification_drift  # noqa: PLC0415 — deferred: ORM-reading import

    try:
        findings = classification_drift()
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(f"WARN  Loop-classification drift check crashed: {exc.__class__.__name__}: {exc}")
        return True
    if not findings:
        return True
    for finding in findings:
        typer.echo(f"WARN  Loop classification drift: {finding}")
    typer.echo(
        "WARN  Run `python -m teatree seed_loops --reconcile-classification` to write the shipped values back.",
    )
    return False


def _check_shipped_seed_inertness() -> bool:
    """Warn on each shipped loop/preset/schedule that is missing, disabled, or not ticking.

    The expected set is sourced from the shipped seed tables, not the DB, so a row somebody
    deleted is visible at all. Only FAULTS are echoed — a shipped-off loop or an inactive
    calendar is a deliberate choice, and a check that reports those every hour is one people
    learn to ignore. Crash-proof: any error degrades to OK.
    """
    from teatree.loops.seed_inertness import shipped_inertness  # noqa: PLC0415 — deferred: ORM-reading import

    try:
        faults = [finding for finding in shipped_inertness() if finding.is_fault]
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(f"WARN  Shipped-seed inertness check crashed: {exc.__class__.__name__}: {exc}")
        return True
    if not faults:
        return True
    for finding in faults:
        typer.echo(f"WARN  Shipped seed inert: {finding.label}")
    typer.echo("WARN  Run `t3 loops audit` for the full report, including the deliberate ones.")
    return False


def _check_aged_sweep_skips() -> bool:
    """Warn on each PR the merge sweep has skipped for the same reason N ticks running.

    A sweep skip is log-only, so a PR held by ``ci_red`` / ``no_clear_for_head`` /
    a fork provenance hold sits indefinitely with nobody told. The DM fires once
    when the streak ages; this is the standing view — every aged streak, announced
    or not, with the PR, the reason and how long it has been stuck. Crash-proof:
    any error degrades to OK.
    """
    from teatree.core.models import SweepSkipStreak  # noqa: PLC0415 — deferred: ORM import needs the app registry
    from teatree.loop.pr_sweep_skip_surface import SURFACE_AFTER_TICKS  # noqa: PLC0415 — deferred: lazy CLI import

    try:
        aged = list(SweepSkipStreak.objects.aged(threshold=SURFACE_AFTER_TICKS))
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(f"WARN  Aged-sweep-skip check crashed: {exc.__class__.__name__}: {exc}")
        return True
    if not aged:
        return True
    for row in aged:
        typer.echo(
            f"WARN  PR {row.ref} skipped by the merge sweep {row.tick_count}x "
            f"({row.age_label()}) — reason `{row.reason}`. {row.url}",
        )
    return False


def _check_unconsumed_merge_clears() -> bool:
    """Hard-FAIL on a standing merge authorisation whose PR the forge still reports OPEN (#4250).

    A ``MergeClear`` is a durable authorisation to merge exactly one diff. One that is
    never consumed while its PR is still open is a finished, reviewed branch that
    silently never lands — and no surface reported it: the S4 age signal joined
    ``ticket__overlay`` while ticket-less is the norm, the sweep logged an unrelated
    reason at INTERNAL audience, and ``MergeAudit`` was correctly empty.

    A missing local ``MergeAudit`` is NOT evidence that no merge happened, and reading
    it as one made this check 6/6 false on live data. The forge decides: only a PR it
    reports OPEN is a stall; one that merged or closed outside the keystone is a spent
    authorisation reported as a self-clearing WARN, and a PR whose state cannot be read
    produces no finding at all.

    Deliberately GLOBAL: a CLEAR whose repo no overlay declares is still a stalled merge,
    and scoping this report per overlay is how such a row would go unreported again.

    Crash-proof: any error degrades to OK with a WARN, so a doctor run never reddens on
    the alarm's own failure.
    """
    from django.utils import timezone  # noqa: PLC0415 — deferred: Django import at call time

    from teatree.backends.loader import pr_open_state  # noqa: PLC0415 — deferred: keeps CLI startup light
    from teatree.core.factory.clear_liveness_report import stale_clear_report  # noqa: PLC0415 — deferred: same

    try:
        report = stale_clear_report("", timezone.now(), read=pr_open_state)
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(f"WARN  Unconsumed-merge-CLEAR check crashed: {exc.__class__.__name__}: {exc}")
        return True
    for line in report.lines():
        typer.echo(line)
    return not report.stalled


def _check_t3_master_unheld_while_loops_tick() -> bool:
    """Hard-FAIL when ``t3-master`` is unheld while loops are still ticking (#4253).

    The owner-gated reactive cycles (``loop_slack_answer`` / ``loop_self_improve``) skip
    every beat on an unheld lease, and the only notice is a log line. Meanwhile
    ``t3 worker status`` exits 0 and every cadence surface reads healthy, because none of
    them knows about this lease — so the degraded state reached nobody for the whole
    night that produced the ticket.

    Silent when nothing is ticking: an unheld lease on an idle box is honest, and a
    stopped chain is ``_check_loop_schedule_liveness``'s finding, not this one.

    Crash-proof: any error degrades to OK with a WARN, so a doctor run never reddens on
    the alarm's own failure.
    """
    from django.utils import timezone  # noqa: PLC0415 — deferred: Django import at call time

    from teatree.loops.master_lease_contradiction import (  # noqa: PLC0415 — deferred: keeps CLI startup light
        unheld_master_lease_with_live_ticks,
    )

    try:
        finding = unheld_master_lease_with_live_ticks(timezone.now())
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(f"WARN  t3-master owner-lease check crashed: {exc.__class__.__name__}: {exc}")
        return True
    if finding is None:
        return True
    typer.echo(
        f"FAIL  The `t3-master` owner lease is unheld while {finding.describe()} — every owner-gated "
        "reactive cycle (`t3 loop slack-answer run`, `t3 loop self-improve run`) is skipping its beat. "
        "Inspect with `t3 loop owner`; a running worker re-claims the slot on its next refresh, so a "
        "lease that stays unheld means the claim itself is failing (#4253).",
    )
    return False


def _check_loop_schedule_liveness() -> bool:
    """Hard-FAIL when an enabled, timer-chained loop is carrying no live timer (#4140).

    The reading no other surface has: ``t3 loop list`` reports a manually-poked loop
    as scheduled, because a manual ``t3 loops tick`` bumps ``Loop.last_run_at``
    without restoring the chain. During the 61-minute ``issue_implementer`` outage
    every cadence surface read healthy while no successor existed at all.

    Crash-proof: any error degrades to OK with a WARN, so a doctor run never reddens
    on the alarm's own failure.
    """
    from django.utils import timezone  # noqa: PLC0415 — deferred: Django import at call time

    from teatree.loops.schedule_liveness import unscheduled_loops  # noqa: PLC0415 — deferred: keeps CLI startup light

    try:
        stalled = unscheduled_loops(timezone.now())
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(f"WARN  Loop schedule-liveness check crashed: {exc.__class__.__name__}: {exc}")
        return True
    for loop in stalled:
        typer.echo(
            f"FAIL  Loop `{loop.name}` is enabled but nothing is scheduled to fire it — {loop.reason}. "
            "The periodic reconciler re-heads the chain on its next pass; if this persists, "
            "restart the worker with `t3 worker ensure` (#4140).",
        )
    return not stalled
