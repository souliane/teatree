"""``_check_*`` probes for loop / scheduling staleness invoked by `t3 doctor check`.

Each helper is narrow (single concern, single ``typer.echo`` path) and returns
``bool`` for pass/fail aggregation by :func:`teatree.cli.doctor.app.run_doctor_checks`.
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
    is already terminal/gone — they never left ``dispatched`` (release-on-
    completion only fires on the live transition), so they permanently consume
    the ``issue_implementer_max_concurrent`` budget and no new issue is ever
    claimed. Reads the non-mutating :meth:`find_stale` preview across every
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
        f"terminal/gone ({len(stale.completed)} completed, {len(stale.abandoned)} abandoned) — "
        "run `t3 loop reclaim-markers` to free the issue_implementer budget (#3275)."
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
    from teatree.loops.dream.engine import default_projects_dir  # noqa: PLC0415 — deferred import

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
