"""``t3 directive`` — the directive self-modification operator surface (north-star PR-6 + PR-7).

Thin Typer wrapper: ``capture`` records a plain-language directive; ``tick`` is the
off-live-tick cron entry point (schedule it low-frequency, decoupled from the live
loop); ``resolve-revert`` closes a reverted directive; ``list`` / ``status`` /
``history`` are read-only. Each bootstraps Django and delegates to the ``directive``
management command via ``call_command`` (anything touching the ORM is a management
command). The cron mechanics (cadence gate, in-flight lease) and the guarded directive
FSM live there.
"""

import typer

from teatree.utils.django_bootstrap import ensure_django

directive_app = typer.Typer(
    name="directive",
    help=(
        "Directive-driven self-modification — capture → interpret → human-ratify → "
        "implement → configure → verify → keep-or-revert. Ships QUADRUPLE-OFF (feature "
        "flag + disabled loop row + off_live_tick + critic/signal code guards); a full "
        "tick is a no-op at defaults."
    ),
    no_args_is_help=True,
)


@directive_app.command("capture")
def capture_command(
    text: str,
    *,
    scope: str = typer.Option("", "--scope", help="The overlay the directive is scoped to (blank = global)."),
) -> None:
    """Record a plain-language directive verbatim as a CAPTURED row."""
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415 — deferred until ensure_django() bootstraps Django

    call_command("directive", "capture", text, scope=scope)


@directive_app.command("tick")
def tick_command() -> None:
    """Advance the directive loop one step IF its cadence has elapsed (cron entry)."""
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415 — deferred until ensure_django() bootstraps Django

    call_command("directive", "tick")


@directive_app.command("status")
def status_command(directive_id: int) -> None:
    """Print one directive's state, sketch, and ratification (read-only)."""
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415 — deferred until ensure_django() bootstraps Django

    call_command("directive", "status", directive_id)


@directive_app.command("list")
def list_command(
    *,
    limit: int = typer.Option(20, "--limit", help="How many recent directives to show."),
) -> None:
    """Print the recent directive ledger (read-only)."""
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415 — deferred until ensure_django() bootstraps Django

    call_command("directive", "list", limit=limit)


@directive_app.command("resolve-revert")
def resolve_revert_command(
    directive_id: int,
    *,
    revert_sha: str = typer.Option("", "--revert-sha", help="The git revert commit sha (provenance)."),
) -> None:
    """Close a REVERT_PENDING directive to terminal REVERTED (config already rolled back)."""
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415 — deferred until ensure_django() bootstraps Django

    call_command("directive", "resolve-revert", directive_id, revert_sha=revert_sha)


@directive_app.command("history")
def history_command(
    *,
    limit: int = typer.Option(10, "--limit", help="How many recent directives to show."),
) -> None:
    """Print the recent directive ledger with decisions (read-only)."""
    ensure_django()

    from django.core.management import call_command  # noqa: PLC0415 — deferred until ensure_django() bootstraps Django

    call_command("directive", "history", limit=limit)
