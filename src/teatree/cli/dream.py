"""``t3 dream`` — drive the idle-time memory-consolidation cron (#1933).

Thin Typer wrapper: ``run`` / ``tick`` bootstrap Django and delegate to the
``dream`` management command via ``call_command`` (the AGENTS.md § "Deciding
Where a New Command Lives" pattern — anything touching the ORM is a management
command). The cron mechanics (in-flight lease, cadence gate, ``DreamRunMarker``
stamping) live in that command.

``t3 dream run [--since <iso>] [--dry-run]`` runs a pass NOW (manual escape
hatch; ignores cadence; ``--dry-run`` does everything except writing rows).
``t3 dream tick`` is the cron entry point; it runs only when the dream cadence
has elapsed — schedule it ~04:00 (decoupled from the live 12-minute loop).

The CLI, the off-live-tick cron, and the 48h staleness alarm (``t3 doctor``) are
the thin surface; the distillation engine (phases 1-3) and the file-side phases
4-6 live behind the ``dream`` management command.
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
) -> None:
    """Run one consolidation pass NOW (ignores cadence)."""
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415

    args: list[str] = ["run"]
    if dry_run:
        args.append("--dry-run")
    if propose_evals:
        args.append("--propose-evals")
    if since:
        args.extend(["--since", since])
    call_command("dream", *args)


@dream_app.command("tick")
def tick_command() -> None:
    """Run one consolidation pass IF the dream cadence has elapsed (cron entry)."""
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415

    call_command("dream", "tick")
