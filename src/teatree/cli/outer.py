"""``t3 outer`` — the T4 autoresearch outer-loop operator surface (T4-PR-3).

Thin Typer wrapper: ``tick`` is the off-live-tick cron entry point (schedule it
low-frequency, decoupled from the live loop); ``status`` / ``history`` are
read-only; ``propose`` records an operator hypothesis. Each bootstraps Django and
delegates to the ``outer`` management command via ``call_command`` (anything
touching the ORM is a management command). The cron mechanics (cadence gate,
in-flight lease) and the guarded experiment FSM live there.
"""

import typer

from teatree.utils.django_bootstrap import ensure_django

outer_app = typer.Typer(
    name="outer",
    help=(
        "T4 autoresearch outer loop — propose → ratify → implement → measure → "
        "keep-only-if-better. Ships QUADRUPLE-OFF (feature flag + disabled loop row + "
        "off_live_tick + critic/signal code guards); a full tick is a no-op at defaults."
    ),
    no_args_is_help=True,
)


@outer_app.command("tick")
def tick_command() -> None:
    """Advance the outer loop one step IF its cadence has elapsed (cron entry)."""
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415 — deferred until ensure_django() bootstraps Django

    call_command("outer", "tick")


@outer_app.command("status")
def status_command() -> None:
    """Print the guard-chain verdict and the active experiment (read-only)."""
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415 — deferred until ensure_django() bootstraps Django

    call_command("outer", "status")


@outer_app.command("propose")
def propose_command(
    *,
    hypothesis: str = typer.Option("", "--hypothesis", help="The operator hypothesis to test."),
    target: str = typer.Option("", "--target", help="The signal provider_id to improve."),
) -> None:
    """Record an operator hypothesis as a PROPOSED experiment (refused while off)."""
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415 — deferred until ensure_django() bootstraps Django

    call_command("outer", "propose", hypothesis=hypothesis, target=target)


@outer_app.command("resolve-revert")
def resolve_revert_command(
    experiment_id: int,
    *,
    revert_sha: str = typer.Option("", "--revert-sha", help="The git revert commit sha (provenance)."),
) -> None:
    """Close a REVERT_PENDING experiment to terminal REVERTED, freeing the slot."""
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415 — deferred until ensure_django() bootstraps Django

    call_command("outer", "resolve-revert", experiment_id, revert_sha=revert_sha)


@outer_app.command("resolve-keep")
def resolve_keep_command(experiment_id: int) -> None:
    """Close a KEEP_PENDING experiment to terminal KEPT, freeing the slot."""
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415 — deferred until ensure_django() bootstraps Django

    call_command("outer", "resolve-keep", experiment_id)


@outer_app.command("history")
def history_command(
    *,
    limit: int = typer.Option(10, "--limit", help="How many recent experiments to show."),
) -> None:
    """Print the recent experiment ledger (read-only)."""
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415 — deferred until ensure_django() bootstraps Django

    call_command("outer", "history", limit=limit)
