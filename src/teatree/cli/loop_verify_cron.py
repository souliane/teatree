"""``t3 loop verify-cron <name>`` — verify-by-reread a loop's cron registration (#1192).

Split out of ``cli.loop`` (module-health: that file owns the tick / start /
dashboard concerns). :func:`register` attaches the flat ``t3 loop verify-cron
<name>`` verb onto the shared ``loop_app``. It delegates to the
``loop_verify_cron`` Django management command — anything touching the ORM is a
management command, not a plain typer command.

The other half of the ``t3 loop claude-spec`` affordance: after calling
``CronCreate`` with the printed spec, the agent runs ``CronList`` and pipes the
JSON snapshot here to confirm the registration actually landed — a CLI cannot
call ``CronCreate``/``CronList`` itself, so this only ever judges a snapshot
the caller already has in hand.
"""

import typer

from teatree.utils.django_bootstrap import ensure_django


def register(loop_app: typer.Typer) -> None:
    """Attach ``verify-cron`` onto loop_app."""

    @loop_app.command("verify-cron")
    def verify_cron_command(
        name: str = typer.Argument(..., help="DB Loop name (e.g. review, ship, dream)."),
        *,
        cron_list_json: str = typer.Option(
            "-",
            "--cron-list-json",
            help="Path to a CronList JSON snapshot (a bare JSON array of job objects), or '-' for stdin.",
        ),
    ) -> None:
        """Verify-by-reread: confirm NAME's CronCreate registration against a CronList snapshot."""
        ensure_django()

        from django.core.management import call_command  # noqa: PLC0415

        try:
            call_command("loop_verify_cron", name, cron_list_json=cron_list_json)
        except SystemExit as exc:
            raise typer.Exit(code=int(exc.code) if isinstance(exc.code, int) else 1) from exc


__all__ = ["register"]
