"""``t3 loop claude-spec <name>`` — print one loop's native Claude ``/loop`` spec (#2650).

Split out of ``cli.loop`` (module-health: that file owns the tick / start /
dashboard concerns). :func:`register` attaches the flat ``t3 loop claude-spec
<name>`` verb onto the shared ``loop_app``. It delegates to the
``loop_claude_spec`` Django management command — anything touching the ORM is a
management command, not a plain typer command.

This is the affordance the ``/t3:loops`` enable/disable skill reads: print the
stable ``slot_id`` + ``cron`` + ``prompt`` for a loop so the agent can mirror the
DB row into Claude Code — ``CronCreate`` on enable, ``CronList``→``CronDelete`` on
disable. A CLI cannot call ``CronCreate`` itself; the skill drives the harness
tool with the spec this prints.
"""

import typer

from teatree.utils.django_bootstrap import ensure_django


def register(loop_app: typer.Typer) -> None:
    """Attach ``claude-spec`` onto loop_app."""

    @loop_app.command("claude-spec")
    def claude_spec_command(
        name: str = typer.Argument(..., help="DB Loop name (e.g. review, ship, dream)."),
        *,
        json_output: bool = typer.Option(False, "--json", help="Emit the spec as JSON."),
    ) -> None:
        """Print the native Claude `/loop` spec (slot_id, cron, prompt) for one DB Loop."""
        ensure_django()

        from django.core.management import call_command  # noqa: PLC0415

        kwargs: dict[str, bool] = {"json_output": True} if json_output else {}
        try:
            call_command("loop_claude_spec", name, **kwargs)
        except SystemExit as exc:
            raise typer.Exit(code=int(exc.code) if isinstance(exc.code, int) else 1) from exc


__all__ = ["register"]
