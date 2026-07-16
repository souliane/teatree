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

# Presets/schedules are the normal handle (#3248) — the per-loop enable/disable/
# pause/resume verbs are the emergency-only handle, gated behind --emergency.
_EMERGENCY_GUIDANCE = (
    "refused: per-loop enable/disable/pause/resume is EMERGENCY-only. "
    "Normal handle: `t3 loop preset use <preset>` / `t3 loop schedule set-active <schedule>`. "
    "Emergency: `t3 loop override <name> on|off [--for TTL] [--reason ...]`. "
    "To force this per-loop verb anyway, pass --emergency."
)


def _require_emergency(emergency: bool) -> None:
    """Refuse a per-loop control verb unless the operator opted into --emergency."""
    if not emergency:
        typer.echo(_EMERGENCY_GUIDANCE, err=True)
        raise typer.Exit(code=2)


def _delegate(subcommand: str, name: str, *, json_output: bool) -> None:
    """Call ``loop_state <subcommand> <name>``; map a ``SystemExit`` to ``typer.Exit``."""
    ensure_django()
    from django.core.management import call_command  # noqa: PLC0415 — deferred: Django import at call time

    args = ["loop_state", subcommand, name]
    kwargs: dict[str, bool] = {"json_output": True} if json_output else {}
    try:
        call_command(*args, **kwargs)
    except SystemExit as exc:
        raise typer.Exit(code=int(exc.code) if isinstance(exc.code, int) else 1) from exc


def _delegate_override(name: str, state: str, *, for_ttl: str, reason: str, json_output: bool) -> None:
    """Call ``loop_state override <name> <state>`` with the TTL/reason; map ``SystemExit`` to ``typer.Exit``."""
    ensure_django()
    from django.core.management import call_command  # noqa: PLC0415 — deferred: Django import at call time

    kwargs: dict[str, str | bool] = {}
    if for_ttl:
        kwargs["for_ttl"] = for_ttl
    if reason:
        kwargs["reason"] = reason
    if json_output:
        kwargs["json_output"] = True
    try:
        call_command("loop_state", "override", name, state, **kwargs)
    except SystemExit as exc:
        raise typer.Exit(code=int(exc.code) if isinstance(exc.code, int) else 1) from exc


def register(loop_app: typer.Typer) -> None:
    """Attach ``pause`` / ``resume`` / ``disable`` / ``enable`` / ``loop-state`` onto loop_app."""

    @loop_app.command("pause")
    def pause_command(
        name: str = typer.Argument(..., help="Mini-loop name (e.g. review, ship, dispatch)."),
        *,
        emergency: bool = typer.Option(False, "--emergency", help="Required: this per-loop verb is emergency-only."),
        json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
    ) -> None:
        """Pause a mini-loop durably (#1913) — EMERGENCY-only; prefer presets/schedules or `loop override`."""
        _require_emergency(emergency)
        _delegate("pause", name, json_output=json_output)

    @loop_app.command("resume")
    def resume_command(
        name: str = typer.Argument(..., help="Mini-loop name."),
        *,
        emergency: bool = typer.Option(False, "--emergency", help="Required: this per-loop verb is emergency-only."),
        json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
    ) -> None:
        """Resume a paused OR disabled mini-loop — EMERGENCY-only; prefer presets/schedules or `loop override`."""
        _require_emergency(emergency)
        _delegate("resume", name, json_output=json_output)

    @loop_app.command("disable")
    def disable_command(
        name: str = typer.Argument(..., help="Mini-loop name."),
        *,
        emergency: bool = typer.Option(False, "--emergency", help="Required: this per-loop verb is emergency-only."),
        json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
    ) -> None:
        """Disable a mini-loop durably — EMERGENCY-only; prefer presets/schedules or `loop override`."""
        _require_emergency(emergency)
        _delegate("disable", name, json_output=json_output)

    @loop_app.command("enable")
    def enable_command(
        name: str = typer.Argument(..., help="Mini-loop name."),
        *,
        emergency: bool = typer.Option(False, "--emergency", help="Required: this per-loop verb is emergency-only."),
        json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
    ) -> None:
        """Enable a disabled mini-loop — EMERGENCY-only; prefer presets/schedules or `loop override`."""
        _require_emergency(emergency)
        _delegate("enable", name, json_output=json_output)

    @loop_app.command("override")
    def override_command(
        name: str = typer.Argument(..., help="Mini-loop name."),
        state: str = typer.Argument(..., help="on | off | clear."),
        *,
        for_ttl: str = typer.Option("", "--for", help="TTL for the override (2h/30m/1d)."),
        reason: str = typer.Option("", "--reason", help="Why the override is in force."),
        json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
    ) -> None:
        """Emergency per-loop force (on/off/clear) — the handle that beats a preset force-off (#3248)."""
        _delegate_override(name, state, for_ttl=for_ttl, reason=reason, json_output=json_output)

    @loop_app.command("loop-state")
    def loop_state_command(
        name: str = typer.Argument(..., help="Mini-loop name."),
        *,
        json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
    ) -> None:
        """Read a known mini-loop's durable state, read-only (ENABLED when never touched; refuses an unknown name)."""
        _delegate("status", name, json_output=json_output)


__all__ = ["register"]
