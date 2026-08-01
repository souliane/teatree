"""``manage.py loop_schedule`` — list/show/set-active/set-timezone/clear-active schedules (#3159).

Backs ``t3 loop schedule …``. A schedule is a named weekly calendar of slots; the
active one is the ``active_loop_schedule`` ``ConfigSetting`` (global scope). Setting
it is one write — the whole switch between calendars (normal ↔ holiday). ORM access
lives in a management command (the project's "anything touching the ORM is a
management command" rule).
"""

import zoneinfo
from typing import IO, Annotated, Any, NoReturn, cast

import typer
from django_typer.management import TyperCommand, command

from teatree.core.machine_output import emit
from teatree.core.models import ConfigSetting, ModeSchedule
from teatree.loop.preset_resolution import ACTIVE_SCHEDULE_SETTING
from teatree.loops.preset_editing import PresetEditError
from teatree.loops.schedule_editing import delete_schedule

_WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _active_schedule_name() -> str:
    raw = ConfigSetting.objects.get_effective(ACTIVE_SCHEDULE_SETTING)
    return raw.strip() if isinstance(raw, str) else ""


def _slot_days(slot: object) -> str:
    return ",".join(_WEEKDAY_NAMES[day] for day in sorted(slot.weekdays))  # ty: ignore[unresolved-attribute]


class Command(TyperCommand):
    help = "List/show/set-active/clear-active loop schedules (#3159)."

    @command(name="list")
    def list_schedules(
        self, *, json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False
    ) -> None:
        """List every schedule with its timezone, slot count, and the ACTIVE marker."""
        active = _active_schedule_name()
        schedules = list(ModeSchedule.objects.all())
        payload = [
            {
                "name": schedule.name,
                "timezone": schedule.timezone,
                "slots": schedule.slots.count(),
                "active": schedule.name == active,
            }
            for schedule in schedules
        ]
        if not schedules:
            human = "No schedules defined. Run `t3 setup` to seed the defaults."
        else:
            lines = ["schedules:"]
            for schedule in schedules:
                marker = " *ACTIVE*" if schedule.name == active else ""
                lines.append(
                    f"  {schedule.name:<20} tz={schedule.timezone_label:<18} {schedule.slots.count()} slots{marker}"
                )
            if not active:
                lines.append("  (no active schedule — presets apply only via a manual override)")
            human = "\n".join(lines)
        emit(
            {"active": active, "schedules": payload},
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=human,
        )

    @command(name="show")
    def show(
        self,
        name: Annotated[str, typer.Argument(help="Schedule to show; omit for the active one.")] = "",
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Show a schedule's ordered slots (weekdays at a start time, then the preset)."""
        resolved = name or _active_schedule_name()
        if not resolved:
            self._refuse("no schedule named and none active", json_output=json_output)
        schedule = ModeSchedule.objects.filter(name=resolved).first()
        if schedule is None:
            self._refuse(f"no schedule named {resolved!r}", json_output=json_output)
        slots = list(schedule.slots.all())
        human_lines = [f"schedule {schedule.name} (tz={schedule.timezone_label}): {schedule.description}"]
        human_lines += [
            f"  {_slot_days(slot):<28} {slot.start_time.strftime('%H:%M')} -> {slot.preset_name}" for slot in slots
        ]
        emit(
            {
                "name": schedule.name,
                "timezone": schedule.timezone,
                "slots": [
                    {
                        "days": sorted(slot.weekdays),
                        "start_time": slot.start_time.strftime("%H:%M"),
                        "preset": slot.preset_name,
                    }
                    for slot in slots
                ],
            },
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human="\n".join(human_lines),
        )

    @command(name="set-active")
    def set_active(
        self,
        name: Annotated[str, typer.Argument(help="Schedule to activate.")],
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Activate *name* — the single ``active_loop_schedule`` write that switches calendars."""
        if ModeSchedule.objects.filter(name=name).first() is None:
            self._refuse(f"no schedule named {name!r} — run `t3 loop schedule list`", json_output=json_output)
        ConfigSetting.objects.set_value(ACTIVE_SCHEDULE_SETTING, name)
        self._emit({"active": name}, f"active schedule is now {name!r}.", json_output=json_output)

    @command(name="set-timezone")
    def set_timezone(
        self,
        name: Annotated[str, typer.Argument(help="Schedule to retime.")],
        zone: Annotated[str, typer.Argument(help="IANA zone key, e.g. Europe/Vienna.")],
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Set *name*'s slot timezone — the lever that makes its wall-clock slots fire locally.

        A schedule seeded without a timezone resolves to ``settings.TIME_ZONE``,
        so its 08:00 slot fires at 08:00 UTC rather than 08:00 where the operator
        is. The per-schedule column is the correct lever; the project zone stays
        UTC. Validated here at WRITE time so an unknown key is refused once
        instead of warned about on every tick.
        """
        schedule = ModeSchedule.objects.filter(name=name).first()
        if schedule is None:
            self._refuse(f"no schedule named {name!r} — run `t3 loop schedule list`", json_output=json_output)
        try:
            zoneinfo.ZoneInfo(zone)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            self._refuse(
                f"unknown timezone {zone!r} — expected an IANA key like 'Europe/Vienna'",
                json_output=json_output,
            )
        schedule.timezone = zone
        schedule.save(update_fields=["timezone", "updated_at"])
        self._emit(
            {"name": name, "timezone": zone},
            f"schedule {name!r} now resolves its slots in {zone}.",
            json_output=json_output,
        )

    @command(name="clear-active")
    def clear_active(self, *, json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False) -> None:
        """Clear the active schedule so no L2 layer applies (presets only via override)."""
        cleared = ConfigSetting.objects.clear(ACTIVE_SCHEDULE_SETTING)
        message = "cleared the active schedule." if cleared else "no active schedule was set."
        self._emit({"cleared": cleared}, message, json_output=json_output)

    @command(name="delete")
    def delete(
        self,
        name: Annotated[str, typer.Argument(help="Schedule to delete.")] = "",
        *,
        confirm: Annotated[str, typer.Option("--confirm", help="Typed phrase; required for a shipped schedule.")] = "",
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Delete a calendar and its slots — the ACTIVE one is refused; a shipped one needs ``--confirm``."""
        try:
            delete_schedule(name, confirm=confirm)
        except PresetEditError as exc:
            self._refuse(str(exc), json_output=json_output)
        self._emit({"deleted": name}, f"deleted schedule {name!r}.", json_output=json_output)

    def _emit(self, payload: dict[str, Any], message: str, *, json_output: bool) -> None:
        emit(
            payload,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=message,
        )

    def _refuse(self, message: str, *, json_output: bool) -> NoReturn:
        emit(
            {"error": message},
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=f"ERROR  {message}",
        )
        raise SystemExit(2)
