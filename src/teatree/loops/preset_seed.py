"""Idempotent seed of the default loop presets + schedules (#3159).

The 6 curated presets and the two shipped schedules (``standard`` /
``always-unattended``) as owner-editable DB DATA, not code. Named for what the
mode *does*, grounded in the seed taxonomy (:data:`teatree.loops.seed.DEFAULT_LOOPS`).

**Idempotent, never clobbering edits:** ``get_or_create`` by ``name`` so a
re-run creates nothing new and leaves an operator-edited preset/schedule exactly
as-is. Slots are only materialised for a NEWLY-created schedule, so an operator
who re-arranged a schedule's slots keeps that arrangement.

**Fully opt-in:** the seed never writes ``active_loop_schedule`` — a fresh install
has every preset + schedule present but NO active schedule, so loop admission is
byte-for-byte today's two-plane verdict until the owner runs
``t3 loop schedule set-active standard``.

Dark/destructive-opt-in loops (``issue_implementer`` / ``issue_disposition`` /
``backlog_sweep`` / ``outer_loop`` / ``directive_loop``) stay *inherit* in every
preset except ``low-power`` / ``off`` — a mode switch never silently re-enables
the owner's explicit opt-in on a destructive-capable loop.
"""

import datetime as dt
from dataclasses import dataclass

from teatree.loops.seed import DEFAULT_LOOPS

# The deterministic, model-free local loops ``low-power`` keeps up (capture via
# inbox continues, cheap and lossless); every other loop is forced off.
_LOW_POWER_ON: frozenset[str] = frozenset(
    {"inbox", "idle_stack_reaper", "local_stack_queue", "resource_pressure", "pane_reaper", "housekeeping"}
)

# The explicit tri-state entries per preset (absent key = inherit the base config).
# Mirrors the design's curated table; ``low-power`` / ``off`` are built
# programmatically below so they cover EVERY seeded loop.
_ENGAGED = dict.fromkeys(
    (
        "inbox",
        "dispatch",
        "tickets",
        "ship",
        "review",
        "followup",
        "audit",
        "news",
        "arch_review",
        "dream",
        "snapshot_warmer",
        "housekeeping",
        "idle_stack_reaper",
        "local_stack_queue",
        "resource_pressure",
        "pane_reaper",
    ),
    True,
)
_HEADS_DOWN = {
    **dict.fromkeys(
        (
            "inbox",
            "dispatch",
            "tickets",
            "ship",
            "dream",
            "snapshot_warmer",
            "housekeeping",
            "idle_stack_reaper",
            "local_stack_queue",
            "resource_pressure",
            "pane_reaper",
        ),
        True,
    ),
    **dict.fromkeys(("review", "followup", "audit", "news", "arch_review", "eval_local"), False),
}
_UNATTENDED = {
    **dict.fromkeys(
        (
            "inbox",
            "dispatch",
            "tickets",
            "ship",
            # #3569: ``review`` is UNMASKED when unattended — forced ON so self-review
            # keeps reviewing the owner's own PRs autonomously (it is no longer
            # colleague_facing, so the away-gate does not skip it). Colleague
            # admission is gated upstream by ``admit_colleague_prs_to_board``, not
            # by the preset.
            "review",
            "audit",
            "news",
            "arch_review",
            "dream",
            "snapshot_warmer",
            "housekeeping",
            "idle_stack_reaper",
            "local_stack_queue",
            "resource_pressure",
            "pane_reaper",
        ),
        True,
    ),
    # ``followup`` stays masked (its review-request nag is colleague-facing).
    **dict.fromkeys(("followup",), False),
}
_MAINTENANCE = {
    **dict.fromkeys(
        (
            "inbox",
            "dispatch",
            "dream",
            "eval_local",
            "dogfood",
            "arch_review",
            "news",
            "snapshot_warmer",
            "housekeeping",
            "idle_stack_reaper",
            "local_stack_queue",
            "resource_pressure",
            "pane_reaper",
        ),
        True,
    ),
    **dict.fromkeys(("tickets", "ship", "review", "followup", "audit"), False),
}


def _all_loop_names() -> tuple[str, ...]:
    return tuple(spec.name for spec in DEFAULT_LOOPS)


@dataclass(frozen=True, slots=True)
class PresetSpec:
    name: str
    description: str
    entries: dict[str, bool]
    availability_mode: str = ""
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


def default_preset_specs() -> tuple[PresetSpec, ...]:
    names = _all_loop_names()
    return (
        PresetSpec(
            "engaged", "Full working-hours mode: deliver, interact, keep improvement loops warm.", dict(_ENGAGED)
        ),
        PresetSpec(
            "heads-down", "Deep work: deliver without touching colleagues (review/followup off).", dict(_HEADS_DOWN)
        ),
        PresetSpec(
            "unattended",
            "The factory keeps producing while the human is unreachable; colleague-facing loops off.",
            dict(_UNATTENDED),
            availability_mode="autonomous_away",
            defers_questions=True,
        ),
        PresetSpec(
            "maintenance",
            "Nights: self-maintenance + self-improvement only, no ticket/colleague/delivery work.",
            dict(_MAINTENANCE),
            defers_questions=True,
        ),
        PresetSpec(
            "low-power",
            "Token-budget guard: only deterministic model-free local loops stay up.",
            {name: name in _LOW_POWER_ON for name in names},
            defers_questions=True,
        ),
        PresetSpec(
            "off",
            "Every Loop-table loop off (the reversible 'calendar says nothing runs' mode).",
            dict.fromkeys(names, False),
        ),
        PresetSpec(
            "offline",
            "Holiday: every loop off, questions defer AND the self-pump pauses (was 'off' preset + 'away').",
            dict.fromkeys(names, False),
            availability_mode="away",
            defers_questions=True,
            pauses_self_pump=True,
            presence_sensitive=False,
        ),
    )


def default_schedule_specs() -> tuple[ScheduleSpec, ...]:
    weekdays = [0, 1, 2, 3, 4]
    all_week = [0, 1, 2, 3, 4, 5, 6]
    return (
        ScheduleSpec(
            "standard",
            "Weekday days → engaged, evenings → maintenance, weekends → unattended.",
            (
                SlotSpec(weekdays, dt.time(8, 0), "engaged"),
                SlotSpec(weekdays, dt.time(19, 0), "maintenance"),
                SlotSpec([5], dt.time(9, 0), "unattended"),
                SlotSpec([6], dt.time(9, 0), "unattended"),
            ),
        ),
        ScheduleSpec(
            "always-unattended",
            "The holiday calendar: unattended all week.",
            (SlotSpec(all_week, dt.time(0, 0), "unattended"),),
        ),
    )


@dataclass(frozen=True, slots=True)
class PresetSeedResult:
    presets_created: int
    schedules_created: int


def seed_default_presets_and_schedules() -> PresetSeedResult:
    """Idempotently seed the default presets + schedules; return the create counts.

    ``get_or_create`` by ``name`` never clobbers an operator-edited row. Slots are
    materialised only for a newly-created schedule (a re-run leaves a re-arranged
    schedule untouched). The active-schedule selector is deliberately NOT written —
    a fresh install is fully opt-in.
    """
    from teatree.core.models import (  # noqa: PLC0415 — deferred import (cycle-safe / pre-app-registry)
        Mode,
        ModeSchedule,
        ModeScheduleSlot,
    )

    presets_created = 0
    for spec in default_preset_specs():
        _, made = Mode.objects.get_or_create(
            name=spec.name,
            defaults={
                "entries": spec.entries,
                "description": spec.description,
                "availability_mode": spec.availability_mode,
                "defers_questions": spec.defers_questions,
                "pauses_self_pump": spec.pauses_self_pump,
                "presence_sensitive": spec.presence_sensitive,
            },
        )
        presets_created += int(made)

    schedules_created = 0
    for spec in default_schedule_specs():
        schedule, made = ModeSchedule.objects.get_or_create(
            name=spec.name, defaults={"description": spec.description, "timezone": ""}
        )
        schedules_created += int(made)
        if made:
            ModeScheduleSlot.objects.bulk_create(
                ModeScheduleSlot(
                    schedule=schedule, days=slot.days, start_time=slot.start_time, preset_name=slot.preset_name
                )
                for slot in spec.slots
            )
    return PresetSeedResult(presets_created=presets_created, schedules_created=schedules_created)
