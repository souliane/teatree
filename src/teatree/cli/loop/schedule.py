"""``t3 loop schedule`` — the weekly schedule control surface (#3159).

Thin typer verbs delegating to the ``loop_schedule`` Django management command
(the ``cli.loop.state`` pattern). :func:`register` attaches the ``schedule``
subgroup onto the shared ``loop_app``.
"""

from typing import Annotated

import typer

from teatree.utils.django_bootstrap import ensure_django


def _delegate(*args: str, json_output: bool = False) -> None:
    ensure_django()
    from django.core.management import call_command  # noqa: PLC0415 — deferred import (cycle-safe / pre-app-registry)

    kwargs: dict[str, bool] = {"json_output": True} if json_output else {}
    try:
        call_command("loop_schedule", *args, **kwargs)
    except SystemExit as exc:
        raise typer.Exit(code=int(exc.code) if isinstance(exc.code, int) else 1) from exc


def register(loop_app: typer.Typer) -> None:
    """Attach the named-schedule and slot-editing verbs onto ``loop_app``."""
    schedule_app = typer.Typer(
        name="schedule", no_args_is_help=True, help="Weekly preset schedules — the L2 calendar (#3159)."
    )

    @schedule_app.command("list")
    def list_command(*, json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
        """List every schedule with its timezone, slot count, and ACTIVE marker."""
        _delegate("list", json_output=json_output)

    @schedule_app.command("show")
    def show_command(
        name: Annotated[str, typer.Argument()] = "",
        *,
        json_output: Annotated[bool, typer.Option("--json")] = False,
    ) -> None:
        """Show a schedule's ordered slots, or (no arg) the active one."""
        args = ["show", name] if name else ["show"]
        _delegate(*args, json_output=json_output)

    @schedule_app.command("set-active")
    def set_active_command(
        name: Annotated[str, typer.Argument()],
        *,
        json_output: Annotated[bool, typer.Option("--json")] = False,
    ) -> None:
        """Activate a schedule — the single write that switches calendars (normal ↔ holiday)."""
        _delegate("set-active", name, json_output=json_output)

    @schedule_app.command("set-timezone")
    def set_timezone_command(
        name: Annotated[str, typer.Argument()],
        zone: Annotated[str, typer.Argument()],
        *,
        json_output: Annotated[bool, typer.Option("--json")] = False,
    ) -> None:
        """Set a schedule's timezone so its wall-clock slots fire locally, not in the project zone."""
        _delegate("set-timezone", name, zone, json_output=json_output)

    @schedule_app.command("set-slot")
    def set_slot_command(
        name: Annotated[str, typer.Argument()],
        days: Annotated[str, typer.Argument(help="Comma-separated weekdays, Mon=0..Sun=6.")],
        start_time: Annotated[str, typer.Argument(help="Local wall-clock start in HH:MM form.")],
        preset_name: Annotated[str, typer.Argument(help="Preset selected from this start point.")],
        *,
        slot_id: Annotated[int | None, typer.Option("--slot-id", help="Existing slot id to update.")] = None,
    ) -> None:
        """Create a schedule slot, or update the slot named by ``--slot-id``."""
        args = ["set-slot", name, days, start_time, preset_name]
        if slot_id is not None:
            args += ["--slot-id", str(slot_id)]
        _delegate(*args)

    @schedule_app.command("delete-slot")
    def delete_slot_command(
        name: Annotated[str, typer.Argument()],
        slot_id: Annotated[int, typer.Argument(help="Slot id to delete.")],
    ) -> None:
        """Delete one slot owned by a schedule."""
        _delegate("delete-slot", name, str(slot_id))

    @schedule_app.command("clear-active")
    def clear_active_command(*, json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
        """Clear the active schedule so no L2 layer applies."""
        _delegate("clear-active", json_output=json_output)

    @schedule_app.command("delete")
    def delete_command(
        name: Annotated[str, typer.Argument()],
        *,
        confirm: Annotated[
            str, typer.Option("--confirm", help="Typed phrase `stop-<name>`; required for a shipped schedule.")
        ] = "",
        json_output: Annotated[bool, typer.Option("--json")] = False,
    ) -> None:
        """Delete a calendar and its slots — the ACTIVE one is refused; shipped needs ``--confirm``."""
        args = ["delete", name] + (["--confirm", confirm] if confirm else [])
        _delegate(*args, json_output=json_output)

    loop_app.add_typer(schedule_app, name="schedule")


__all__ = ["register"]
