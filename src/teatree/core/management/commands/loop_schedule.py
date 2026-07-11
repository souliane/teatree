"""``manage.py loop_schedule`` — list/show/set-active/clear-active loop schedules (#3159).

Backs ``t3 loop schedule …``. A schedule is a named weekly calendar of slots; the
active one is the ``active_loop_schedule`` ``ConfigSetting`` (global scope). Setting
it is one write — the whole switch between calendars (normal ↔ holiday). ORM access
lives in a management command (the project's "anything touching the ORM is a
management command" rule).
"""

import json
from typing import Annotated, Any, NoReturn

import typer
from django_typer.management import TyperCommand, command

from teatree.core.models import ConfigSetting, LoopSchedule
from teatree.loop.preset_resolution import ACTIVE_SCHEDULE_SETTING

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
        schedules = list(LoopSchedule.objects.all())
        if json_output:
            payload = [
                {
                    "name": schedule.name,
                    "timezone": schedule.timezone,
                    "slots": schedule.slots.count(),
                    "active": schedule.name == active,
                }
                for schedule in schedules
            ]
            self.stdout.write(json.dumps({"active": active, "schedules": payload}, indent=2))
            return
        if not schedules:
            self.stdout.write("No schedules defined. Run `t3 setup` to seed the defaults.")
            return
        self.stdout.write("schedules:")
        for schedule in schedules:
            marker = " *ACTIVE*" if schedule.name == active else ""
            self.stdout.write(
                f"  {schedule.name:<20} tz={schedule.timezone or 'local':<18} {schedule.slots.count()} slots{marker}"
            )
        if not active:
            self.stdout.write("  (no active schedule — presets apply only via a manual override)")

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
        schedule = LoopSchedule.objects.filter(name=resolved).first()
        if schedule is None:
            self._refuse(f"no schedule named {resolved!r}", json_output=json_output)
        slots = list(schedule.slots.all())
        if json_output:
            self.stdout.write(
                json.dumps(
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
                    indent=2,
                )
            )
            return
        self.stdout.write(f"schedule {schedule.name} (tz={schedule.timezone or 'local'}): {schedule.description}")
        for slot in slots:
            self.stdout.write(f"  {_slot_days(slot):<28} {slot.start_time.strftime('%H:%M')} -> {slot.preset_name}")

    @command(name="set-active")
    def set_active(
        self,
        name: Annotated[str, typer.Argument(help="Schedule to activate.")],
        *,
        json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Activate *name* — the single ``active_loop_schedule`` write that switches calendars."""
        if LoopSchedule.objects.filter(name=name).first() is None:
            self._refuse(f"no schedule named {name!r} — run `t3 loop schedule list`", json_output=json_output)
        ConfigSetting.objects.set_value(ACTIVE_SCHEDULE_SETTING, name)
        self._emit({"active": name}, f"active schedule is now {name!r}.", json_output=json_output)

    @command(name="clear-active")
    def clear_active(self, *, json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False) -> None:
        """Clear the active schedule so no L2 layer applies (presets only via override)."""
        cleared = ConfigSetting.objects.clear(ACTIVE_SCHEDULE_SETTING)
        message = "cleared the active schedule." if cleared else "no active schedule was set."
        self._emit({"cleared": cleared}, message, json_output=json_output)

    def _emit(self, payload: dict[str, Any], message: str, *, json_output: bool) -> None:
        self.stdout.write(json.dumps(payload, indent=2) if json_output else message)

    def _refuse(self, message: str, *, json_output: bool) -> NoReturn:
        self.stdout.write(json.dumps({"error": message}, indent=2) if json_output else f"ERROR  {message}")
        raise SystemExit(2)
