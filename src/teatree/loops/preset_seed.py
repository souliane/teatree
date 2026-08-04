"""Idempotent seed of the default loop modes + schedules (#3159).

The 7 curated modes and the two shipped schedules (``standard`` /
``always-unattended``) as owner-editable data. The shipped VALUES live in the
``[modes.<name>]`` / ``[schedules.<name>]`` tables of
``src/teatree/config/defaults.toml`` — the same packaged file every other shipped
default an operator tunes lives in — and this module seeds them into owner-editable
DB rows. Named for what the mode *does*, grounded in the seed taxonomy
(``[loops]``, read by :data:`teatree.loops.seed.DEFAULT_LOOPS`).

**Idempotent, never clobbering edits:** ``get_or_create`` by ``name`` so a
re-run creates nothing new and leaves an operator-edited mode/schedule exactly
as-is. Slots are only materialised for a NEWLY-created schedule, so an operator
who re-arranged a schedule's slots keeps that arrangement.

**``standard`` ships active:** the seed pins ``active_loop_schedule`` to
``standard`` through the provenance-aware :meth:`ConfigSetting.objects.seed`, so a
fresh install runs the owner's working-hours calendar out of the box — Mon-Fri
09:00-16:00 ``Europe/Vienna`` → ``engaged`` (attended), every other hour →
``unattended`` (autonomous-away). The provenance seed CREATES the pin on a fresh
box and PRESERVES an operator who switched to another calendar (or cleared it),
so a re-seed never overrides the owner's choice.

A loop ABSENT from a mode's ``entries`` table INHERITS its own enabled flag, which is
how the dark/destructive-opt-in loops (``issue_implementer`` / ``issue_disposition`` /
``backlog_sweep`` / ``outer_loop`` / ``directive_loop``) stay untouched by every mode
except ``low-power`` / ``off`` / ``offline`` — a mode switch never silently re-enables
the owner's explicit opt-in on a destructive-capable loop.
"""

import datetime as dt
from dataclasses import dataclass
from pathlib import Path

from teatree.config.seed_defaults import shipped_seed_table

_MODES_TABLE = "modes"
_SCHEDULES_TABLE = "schedules"


@dataclass(frozen=True, slots=True)
class PresetSpec:
    name: str
    description: str
    entries: dict[str, bool]
    # The intrinsic availability posture (#61 merge, design §7-A). ``present_sensitive``
    # defaults True so any scheduled away honours a live keystroke (today's behaviour).
    defers_questions: bool = False
    pauses_self_pump: bool = False
    presence_sensitive: bool = True


@dataclass(frozen=True, slots=True)
class SlotSpec:
    days: list[int]
    start_time: dt.time
    preset_name: str


@dataclass(frozen=True, slots=True)
class ScheduleSpec:
    name: str
    description: str
    slots: tuple[SlotSpec, ...]
    # An IANA zone key resolves each slot's wall-clock start locally with DST
    # handled (CET/CEST); "" falls back to ``settings.TIME_ZONE`` at read time.
    timezone: str = ""


def default_preset_specs(path: Path | None = None) -> tuple[PresetSpec, ...]:
    """The shipped ``[modes]`` table as specs, in the file's table order."""
    return tuple(
        PresetSpec(
            name=name,
            description=entry["description"],
            entries={loop: bool(value) for loop, value in entry.get("entries", {}).items()},
            defers_questions=entry.get("defers_questions", False),
            pauses_self_pump=entry.get("pauses_self_pump", False),
            presence_sensitive=entry.get("presence_sensitive", True),
        )
        for name, entry in shipped_seed_table(_MODES_TABLE, path).items()
    )


def default_schedule_specs(path: Path | None = None) -> tuple[ScheduleSpec, ...]:
    """The shipped ``[schedules]`` table as specs, each slot in its declared order."""
    return tuple(
        ScheduleSpec(
            name=name,
            description=entry["description"],
            slots=tuple(
                SlotSpec(days=slot["days"], start_time=slot["start_time"], preset_name=slot["preset_name"])
                for slot in entry.get("slots", ())
            ),
            timezone=entry.get("timezone", ""),
        )
        for name, entry in shipped_seed_table(_SCHEDULES_TABLE, path).items()
    )


#: The calendar ``active_loop_schedule`` is pinned to on a fresh install.
DEFAULT_ACTIVE_SCHEDULE = "standard"


@dataclass(frozen=True, slots=True)
class PresetSeedResult:
    presets_created: int
    schedules_created: int


def seed_default_presets_and_schedules() -> PresetSeedResult:
    """Idempotently seed the default presets + schedules; return the create counts.

    ``get_or_create`` by ``name`` never clobbers an operator-edited row. Slots are
    materialised only for a newly-created schedule (a re-run leaves a re-arranged
    schedule untouched). The ``active_loop_schedule`` pin is written through the
    provenance-aware :meth:`ConfigSetting.objects.seed` so ``standard`` ships active
    on a fresh box while an operator who switched calendars (or cleared the pin) is
    never overridden on a re-seed.
    """
    from teatree.core.models import (  # noqa: PLC0415 — deferred import (cycle-safe / pre-app-registry)
        ConfigSetting,
        Mode,
        ModeSchedule,
        ModeScheduleSlot,
    )
    from teatree.loop.preset_resolution import (  # noqa: PLC0415 — deferred import (cycle-safe / pre-app-registry)
        ACTIVE_SCHEDULE_SETTING,
    )

    presets_created = 0
    for spec in default_preset_specs():
        _, made = Mode.objects.get_or_create(
            name=spec.name,
            defaults={
                "entries": spec.entries,
                "description": spec.description,
                "defers_questions": spec.defers_questions,
                "pauses_self_pump": spec.pauses_self_pump,
                "presence_sensitive": spec.presence_sensitive,
            },
        )
        presets_created += int(made)

    schedules_created = 0
    for spec in default_schedule_specs():
        schedule, made = ModeSchedule.objects.get_or_create(
            name=spec.name, defaults={"description": spec.description, "timezone": spec.timezone}
        )
        schedules_created += int(made)
        if made:
            ModeScheduleSlot.objects.bulk_create(
                ModeScheduleSlot(
                    schedule=schedule, days=slot.days, start_time=slot.start_time, preset_name=slot.preset_name
                )
                for slot in spec.slots
            )

    # A fresh sentinel never equals a real schedule name, so the provenance seed
    # always CREATES the pin when no row exists and PRESERVES an operator's switch.
    ConfigSetting.objects.seed(ACTIVE_SCHEDULE_SETTING, DEFAULT_ACTIVE_SCHEDULE, code_default=object())
    return PresetSeedResult(presets_created=presets_created, schedules_created=schedules_created)
