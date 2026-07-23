"""Read model for the schedule + preset editor page (#3559).

Renders what an operator needs to reason about the two normal handles — which
schedule is active, which preset it selects, and what each preset says about each
loop. The per-loop opinion is deliberately **tri-state**: ``on`` forces the loop
to run, ``off`` masks it, and *no opinion* hands the decision to the loop's own
base ``enabled`` column — now and in future, so a later base flip silently changes
behaviour. Nothing here recomputes an admission verdict; the effective verdict and
its deciding layer come from :mod:`teatree.loops.preset_status`, the same resolver
``t3 loop preset show`` prints.
"""

from dataclasses import dataclass

from teatree.core.models import Loop, Mode, ModeSchedule, ModeScheduleSlot
from teatree.core.models.loop_preset import PIN_MODES
from teatree.loops.preset_admin import PresetReferrers, preset_referrers
from teatree.loops.preset_editing import ENTRY_INHERIT, ENTRY_OFF, ENTRY_ON, entry_state_of
from teatree.loops.preset_status import PresetSummary, active_summary
from teatree.loops.schedule_editing import active_schedule_name

WEEKDAY_LABELS: tuple[str, ...] = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True, slots=True)
class PresetEntryRow:
    """One loop's row inside a preset tab: what it does, its base, and this preset's opinion."""

    loop_name: str
    description: str
    base_enabled: bool
    state: str


@dataclass(frozen=True, slots=True)
class PresetCard:
    """One preset tab — its identity plus a row per loop and the on/off/no-opinion tally."""

    name: str
    description: str
    active: bool
    availability_pin: str
    entries: tuple[PresetEntryRow, ...]
    referrers: PresetReferrers

    def _count(self, state: str) -> int:
        return sum(1 for row in self.entries if row.state == state)

    @property
    def on_count(self) -> int:
        return self._count(ENTRY_ON)

    @property
    def off_count(self) -> int:
        return self._count(ENTRY_OFF)

    @property
    def inherit_count(self) -> int:
        return self._count(ENTRY_INHERIT)

    @property
    def inherit_loops(self) -> tuple[str, ...]:
        """The loops this preset holds NO opinion on — the gap an operator must see."""
        return tuple(row.loop_name for row in self.entries if row.state == ENTRY_INHERIT)


@dataclass(frozen=True, slots=True)
class SlotRow:
    """One schedule slot — the weekdays and wall-clock start that select a preset."""

    slot_id: int
    days: tuple[int, ...]
    start_time: str
    preset_name: str

    @property
    def days_label(self) -> str:
        return ", ".join(WEEKDAY_LABELS[day] for day in self.days)


@dataclass(frozen=True, slots=True)
class ScheduleCard:
    """One weekly calendar — its timezone, whether it governs, and its ordered slots."""

    name: str
    description: str
    timezone_label: str
    active: bool
    slots: tuple[SlotRow, ...]


@dataclass(frozen=True, slots=True)
class PresetEditorView:
    """Everything the editor page renders: preset tabs, schedules, and the live handles."""

    presets: tuple[PresetCard, ...]
    schedules: tuple[ScheduleCard, ...]
    active_preset: PresetSummary | None
    active_schedule: str
    selected_preset: str
    entry_states: tuple[str, str, str] = (ENTRY_ON, ENTRY_OFF, ENTRY_INHERIT)
    weekdays: tuple[tuple[int, str], ...] = tuple(enumerate(WEEKDAY_LABELS))
    pin_choices: tuple[str, ...] = tuple(sorted(PIN_MODES))


def build_preset_editor(*, selected: str = "") -> PresetEditorView:
    """The whole editor read model, with *selected* naming the open preset tab."""
    summary = active_summary()
    loops = tuple(Loop.objects.all())
    presets = tuple(_preset_card(preset, loops, active_name=summary.name if summary else "") for preset in _presets())
    return PresetEditorView(
        presets=presets,
        schedules=_schedule_cards(),
        active_preset=summary,
        active_schedule=active_schedule_name(),
        selected_preset=_selected_name(selected, presets),
    )


def _presets() -> list[Mode]:
    return list(Mode.objects.all())


def _preset_card(preset: Mode, loops: tuple[Loop, ...], *, active_name: str) -> PresetCard:
    return PresetCard(
        name=preset.name,
        description=preset.description,
        active=preset.name == active_name,
        availability_pin=preset.availability_pin or "",
        entries=tuple(
            PresetEntryRow(
                loop_name=loop.name,
                description=loop.description,
                base_enabled=loop.enabled,
                state=entry_state_of(preset, loop.name),
            )
            for loop in loops
        ),
        referrers=preset_referrers(preset.name),
    )


def _schedule_cards() -> tuple[ScheduleCard, ...]:
    active = active_schedule_name()
    slots_by_schedule: dict[int, list[SlotRow]] = {}
    for slot in ModeScheduleSlot.objects.all():
        slots_by_schedule.setdefault(slot.schedule_id, []).append(_slot_row(slot))
    return tuple(
        ScheduleCard(
            name=schedule.name,
            description=schedule.description,
            timezone_label=schedule.timezone_label,
            active=schedule.name == active,
            slots=tuple(slots_by_schedule.get(schedule.pk, ())),
        )
        for schedule in ModeSchedule.objects.all()
    )


def _slot_row(slot: ModeScheduleSlot) -> SlotRow:
    return SlotRow(
        slot_id=slot.pk,
        days=tuple(sorted(slot.weekdays)),
        start_time=slot.start_time.strftime("%H:%M"),
        preset_name=slot.preset_name,
    )


def _selected_name(selected: str, presets: tuple[PresetCard, ...]) -> str:
    """The open tab: the requested preset, else the active one, else the first."""
    names = [preset.name for preset in presets]
    if selected in names:
        return selected
    active = next((preset.name for preset in presets if preset.active), "")
    return active or (names[0] if names else "")


__all__ = [
    "WEEKDAY_LABELS",
    "PresetCard",
    "PresetEditorView",
    "PresetEntryRow",
    "ScheduleCard",
    "SlotRow",
    "build_preset_editor",
]
