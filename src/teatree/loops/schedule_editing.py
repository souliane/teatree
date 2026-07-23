"""Weekly-schedule writes — the active calendar and its slots (#3559).

The L2 calendar half of the preset control surface: which schedule governs
(``active_loop_schedule``) and the slots that select a preset from a wall-clock
start on a set of weekdays. Shares :class:`PresetEditError` and the preset lookup
with :mod:`teatree.loops.preset_editing`, so a slot can never name a preset that
does not exist.
"""

import datetime as dt
from dataclasses import dataclass
from typing import Final

from teatree.core.models import ConfigSetting, ModeSchedule, ModeScheduleSlot
from teatree.loop.preset_resolution import ACTIVE_SCHEDULE_SETTING
from teatree.loops.preset_editing import PresetEditError, require_preset

_MAX_WEEKDAY: Final = 6


@dataclass(frozen=True, slots=True)
class SlotSpec:
    """One schedule slot's validated payload — weekdays, wall-clock start, preset."""

    days: tuple[int, ...]
    start_time: dt.time
    preset_name: str


def active_schedule_name() -> str:
    """The active schedule's name, or ``""`` when no L2 calendar governs."""
    raw = ConfigSetting.objects.get_effective(ACTIVE_SCHEDULE_SETTING)
    return raw.strip() if isinstance(raw, str) else ""


def set_active_schedule(name: str) -> None:
    """Activate *name* — the single ``active_loop_schedule`` write that switches calendars."""
    if not ModeSchedule.objects.filter(name=name).exists():
        msg = f"no schedule named {name!r}"
        raise PresetEditError(msg)
    ConfigSetting.objects.set_value(ACTIVE_SCHEDULE_SETTING, name)


def clear_active_schedule() -> bool:
    """Clear the active schedule so no L2 layer applies."""
    return ConfigSetting.objects.clear(ACTIVE_SCHEDULE_SETTING)


def upsert_schedule_slot(
    schedule_name: str,
    *,
    slot_id: int | None = None,
    days: list[int] | tuple[int, ...],
    start_time: str,
    preset_name: str,
) -> ModeScheduleSlot:
    """Create or update one slot of *schedule_name* from validated inputs."""
    schedule = _require_schedule(schedule_name)
    spec = _validated_slot(days, start_time, preset_name)
    if slot_id is None:
        return ModeScheduleSlot.objects.create(
            schedule=schedule, days=list(spec.days), start_time=spec.start_time, preset_name=spec.preset_name
        )
    slot = _require_slot(schedule, slot_id)
    slot.days = list(spec.days)
    slot.start_time = spec.start_time
    slot.preset_name = spec.preset_name
    slot.save(update_fields=["days", "start_time", "preset_name"])
    return slot


def delete_schedule_slot(schedule_name: str, slot_id: int) -> None:
    """Remove one slot from *schedule_name*, refusing a slot owned by another schedule."""
    _require_slot(_require_schedule(schedule_name), slot_id).delete()


def _validated_slot(days: list[int] | tuple[int, ...], start_time: str, preset_name: str) -> SlotSpec:
    weekdays = tuple(sorted({int(day) for day in days if isinstance(day, int)}))
    if not weekdays or any(day < 0 or day > _MAX_WEEKDAY for day in weekdays):
        msg = f"invalid weekdays {list(days)!r}; use Mon=0..Sun=6, at least one"
        raise PresetEditError(msg)
    require_preset(preset_name)
    return SlotSpec(days=weekdays, start_time=_parse_hhmm(start_time), preset_name=preset_name)


def _parse_hhmm(raw: str) -> dt.time:
    try:
        return dt.time.fromisoformat(raw.strip())
    except ValueError as exc:
        msg = f"invalid start time {raw!r}; use HH:MM"
        raise PresetEditError(msg) from exc


def _require_schedule(name: str) -> ModeSchedule:
    schedule = ModeSchedule.objects.filter(name=name).first()
    if schedule is None:
        msg = f"no schedule named {name!r}"
        raise PresetEditError(msg)
    return schedule


def _require_slot(schedule: ModeSchedule, slot_id: int) -> ModeScheduleSlot:
    slot = ModeScheduleSlot.objects.filter(pk=slot_id, schedule=schedule).first()
    if slot is None:
        msg = f"schedule {schedule.name!r} has no slot {slot_id}"
        raise PresetEditError(msg)
    return slot


__all__ = [
    "SlotSpec",
    "active_schedule_name",
    "clear_active_schedule",
    "delete_schedule_slot",
    "set_active_schedule",
    "upsert_schedule_slot",
]
