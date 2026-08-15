"""Whether the newest SCHEDULED CI run passed (#4477).

The sibling checks in :mod:`teatree.cli.doctor.checks_test_durations` read the durations
artifact — the *symptom* of a maintenance job that stopped. This one reads the job. Both
halves of the artifact reading were reporting for eleven days while the cause, a scheduled
run failing every night, was visible to nobody: a scheduled run appears on no PR, so the
only workflow anyone watches never showed it.

A FAIL is what makes it visible. ``deploy/watchdog.sh`` execs ``t3 doctor check --json``
inside the stack every ``TEATREE_WATCHDOG_INTERVAL`` and DMs the owner each FAIL line,
re-keyed per day — so the next occurrence surfaces within the day rather than after three
weeks. Unlike the coverage shortfall next door, this one is not a standing page for
something nobody caused: it is actionable, and the next green scheduled run clears it.
"""

import typer

from teatree.utils.run import CommandFailedError, TimeoutExpired

_TEATREE_REPO = "souliane/teatree"
_CI_WORKFLOW = "ci.yml"


def check_scheduled_ci_run_health() -> bool:
    """FAIL when the newest ``schedule``-triggered CI run concluded in failure."""
    from teatree.backends.github import api  # noqa: PLC0415 — deferred: keeps CLI startup light
    from teatree.quality import scheduled_ci_health  # noqa: PLC0415 — deferred: keeps CLI startup light

    try:
        payload = api.list_workflow_runs(_TEATREE_REPO, workflow=_CI_WORKFLOW, event="schedule")
        run = scheduled_ci_health.newest_scheduled_run(payload)
    except (CommandFailedError, OSError, TimeoutExpired) as exc:
        typer.echo(f"WARN  Newest scheduled CI run is UNVERIFIED — the forge read failed: {_reason(exc)}")
        return True
    except scheduled_ci_health.ScheduledRunUnreadableError as exc:
        typer.echo(f"WARN  Newest scheduled CI run is UNVERIFIED — {exc}")
        return True
    except Exception as exc:  # noqa: BLE001 — a doctor check must never crash the run
        typer.echo(f"WARN  Scheduled CI run check crashed: {exc.__class__.__name__}: {exc}")
        return True

    if run is None:
        typer.echo(
            f"WARN  `{_CI_WORKFLOW}` reports no scheduled run at all on {_TEATREE_REPO} — the daily "
            "maintenance cron is what keeps `dev/.test_durations` from rotting, and a cron that never "
            "fires reads the same as one that always passes."
        )
        return True

    started = run.created_at.date().isoformat()
    if not run.failed:
        typer.echo(f"OK    Newest scheduled CI run ({started}) is {run.conclusion or run.status}")
        return True

    typer.echo(
        f"FAIL  The newest scheduled CI run ({started}) concluded `{run.conclusion}` — {run.url}. "
        "Nothing else surfaces this: a scheduled run appears on no PR, so its daily failure is "
        "invisible while the maintenance it owns silently stops."
    )
    typer.echo("      Open the run, read its failing job, and fix it — the next green scheduled run clears this.")
    return False


def _reason(exc: Exception) -> str:
    if isinstance(exc, CommandFailedError):
        return (exc.stderr or "").strip() or f"gh exited {exc.returncode}"
    return f"{exc.__class__.__name__}: {exc}"
