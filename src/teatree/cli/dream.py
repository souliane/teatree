"""``t3 dream`` — drive the idle-time memory-consolidation cron (#1933).

Thin Typer wrapper: ``run`` / ``tick`` bootstrap Django and delegate to the
``dream`` management command via ``call_command`` (the AGENTS.md § "Deciding
Where a New Command Lives" pattern — anything touching the ORM is a management
command). The cron mechanics (in-flight lease, cadence gate, ``DreamRunMarker``
stamping) live in that command.

``t3 dream run [--since <iso>] [--dry-run]`` runs a pass NOW (manual escape
hatch; ignores cadence; ``--dry-run`` does everything except writing rows).
``t3 dream tick`` is the off-live-tick entry point the worker's
:func:`teatree.loops.off_live_tick_driver.drive_off_live_tick_loops` chain fires; it runs
only when the dream cadence has elapsed (the ``dream`` row's ~04:00 slot, decoupled
from the live 12-minute loop).

Both exit NON-ZERO when a pass ran and could not stamp the success marker (#3993) — a
failing acceptance gate or a 0-member replay — so the caller sees a blocked pass rather
than reading its WARN line in a worker log. A SKIP (disabled, not due, lease held) and
a ``--dry-run`` never ran or never write, so both exit 0.

The CLI, the off-live-tick driver, and the staleness alarms (``t3 doctor``: a 48h WARN
plus a hard FAIL once a once-working pass has withheld the marker for
``CRITICAL_STALE_MULTIPLE`` windows) are the thin surface; the distillation engine
(phases 1-3) and the file-side phases 4-6 live behind the ``dream`` management command.
"""

import typer

from teatree.utils.django_bootstrap import ensure_django

dream_app = typer.Typer(
    name="dream",
    help=(
        "Idle-time memory-consolidation (dreaming) cron (#1933). Distils recent "
        "session feedback into the ConsolidatedMemory DB ledger on a low-frequency "
        "schedule, decoupled from the live work loop. `run` is the manual escape "
        "hatch; `tick` is the cadence-gated cron entry point."
    ),
    no_args_is_help=True,
)


@dream_app.command("run")
def run_command(
    *,
    since: str = typer.Option(
        "",
        "--since",
        help="ISO-8601 lower bound for the replay window (default: engine lookback).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Do everything except writing ConsolidatedMemory rows / the marker.",
    ),
    propose_evals: bool = typer.Option(
        False,
        "--propose-evals",
        help="Also derive inert eval candidates from grounded drift clusters (default OFF).",
    ),
    full: bool = typer.Option(
        False,
        "--full",
        help="Run the WHOLE pipeline: also file core-gap tickets and stage LLM-derived evals.",
    ),
) -> None:
    """Run one consolidation pass NOW (ignores cadence)."""
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415 — deferred: Django import at call time

    args: list[str] = ["run"]
    if dry_run:
        args.append("--dry-run")
    if propose_evals:
        args.append("--propose-evals")
    if full:
        args.append("--full")
    if since:
        args.extend(["--since", since])
    call_command("dream", *args)


@dream_app.command("tick")
def tick_command() -> None:
    """Run one consolidation pass IF the dream cadence has elapsed (cron entry)."""
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415 — deferred: Django import at call time

    call_command("dream", "tick")


compliance_app = typer.Typer(
    name="compliance",
    help="Inspect the instruction-compliance accountant (#2663) — the root-KPI metric.",
    no_args_is_help=True,
)
dream_app.add_typer(compliance_app)


@compliance_app.command("show")
def compliance_show_command() -> None:
    """Print the latest compliance snapshot — rate, recurrence count, open escalations."""
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415 — deferred: Django import at call time

    call_command("dream", "compliance")
