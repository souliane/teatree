"""``t3 loop {pause,resume,disable,enable,loop-state}`` — per-loop control plane (#1913).

Split out of ``cli.loop`` (module-health: that file owns the tick / start /
dashboard concerns; the per-loop control plane is a distinct concern).
:func:`register` attaches the flat ``t3 loop pause|resume|disable|enable`` verbs
and ``t3 loop loop-state <name>`` (the per-loop status probe — ``t3 loop status``
already prints the statusline) onto the shared ``loop_app``. Each delegates to
the ``loop_state`` Django management command — anything touching the ORM is a
management command, not a plain typer command.

The DB-backed ``LoopState`` is the canonical control tier (mirrors
``ConfigSetting``): a paused/disabled loop stays held across a session restart,
honoured by BOTH the tick and the in-session Stop self-pump (#1913).
"""

import typer

from teatree.utils.django_bootstrap import ensure_django


def _delegate(subcommand: str, name: str, *, json_output: bool) -> None:
    """Call ``loop_state <subcommand> <name>``; map a ``SystemExit`` to ``typer.Exit``."""
    ensure_django()
    from django.core.management import call_command  # noqa: PLC0415

    args = ["loop_state", subcommand, name]
    kwargs: dict[str, bool] = {"json_output": True} if json_output else {}
    try:
        call_command(*args, **kwargs)
    except SystemExit as exc:
        raise typer.Exit(code=int(exc.code) if isinstance(exc.code, int) else 1) from exc


def register(loop_app: typer.Typer) -> None:
    """Attach ``pause`` / ``resume`` / ``disable`` / ``enable`` / ``loop-state`` onto loop_app."""

    @loop_app.command("pause")
    def pause_command(
        name: str = typer.Argument(..., help="Mini-loop name (e.g. review, ship, dispatch)."),
        *,
        json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
    ) -> None:
        """Pause a mini-loop durably (#1913) — survives restart, honoured by tick + self-pump."""
        _delegate("pause", name, json_output=json_output)

    @loop_app.command("resume")
    def resume_command(
        name: str = typer.Argument(..., help="Mini-loop name."),
        *,
        json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
    ) -> None:
        """Resume a paused OR disabled mini-loop — return it to the ENABLED state."""
        _delegate("resume", name, json_output=json_output)

    @loop_app.command("disable")
    def disable_command(
        name: str = typer.Argument(..., help="Mini-loop name."),
        *,
        json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
    ) -> None:
        """Disable a mini-loop durably — the restart-surviving kill-switch."""
        _delegate("disable", name, json_output=json_output)

    @loop_app.command("enable")
    def enable_command(
        name: str = typer.Argument(..., help="Mini-loop name."),
        *,
        json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
    ) -> None:
        """Enable a disabled mini-loop — return it to the ENABLED state (alias of resume)."""
        _delegate("enable", name, json_output=json_output)

    @loop_app.command("loop-state")
    def loop_state_command(
        name: str = typer.Argument(..., help="Mini-loop name."),
        *,
        json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
    ) -> None:
        """Show a mini-loop's durable state (ENABLED when it has never been touched)."""
        _delegate("status", name, json_output=json_output)


__all__ = ["register"]
