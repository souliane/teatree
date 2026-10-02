"""Pure read-time preset resolver — which preset governs, and why (#3159).

The layered run verdict for a loop at instant *t* is resolved top-down; the first
layer with an opinion wins:

* **LoopState hold** — the emergency brake, applied by the ``held`` arm of
    :func:`teatree.loop.loop_state_db.loop_state_admits`; **never** touched here.
* **manual override** — ``Loop.enabled``, likewise not this module's business.
* **manual preset override** — a :class:`teatree.core.models.ModeOverride` row.
* **active schedule slot** — the ``active_loop_schedule`` calendar's governing
    slot at *t* (the latest slot-start ≤ *t*, searching back across week wrap).

This module owns the last two, returning the preset that governs. Its per-loop opinion
is total, so a resolved preset always decides; ``None`` here means no preset governs and
:func:`teatree.core.mode_resolution.resolve_active_mode` falls to the configured default.

**Fail-open everywhere:** any error (deleted preset, unreadable DB, bad timezone)
resolves to no preset with a WARNING, mirroring the fail-safe doctrine of
:func:`teatree.loop.loop_state_db.loop_held_in_db`. A broken schedule must never brick
the loop fleet.

The failure is CARRIED alongside that answer (:class:`PresetResolution`) rather than
erased, because absence and failure permit different things. Loop selection is right to
treat both as "no preset governs"; publishing under the owner's identity is not, and a
bare ``None`` let a failed read reach the permitting configured default indistinguishably
from a box that simply has no schedule.

A DOMAIN-layer leaf depending only on :mod:`teatree.core.models`, so
:mod:`teatree.loop.loop_state_db` and the ``teatree.loops`` orchestration tick can
both import it downward.
"""

import datetime as dt
import logging
import zoneinfo
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.utils import timezone

from teatree.request_cache import cached_per_request

if TYPE_CHECKING:
    from teatree.core.models import Mode, ModeSchedule, ModeScheduleSlot

logger = logging.getLogger(__name__)

ACTIVE_SCHEDULE_SETTING = "active_loop_schedule"

# How far back a governing slot start may be — one full week covers the coverage
# model's week-wrap (a Sunday-evening slot still governs Monday morning).
_LOOKBACK_DAYS = 7


@dataclass(frozen=True, slots=True)
class ActivePreset:
    """The preset governing now, the layer that chose it, and when its tenure ends."""

    preset: "Mode"
    layer: str  # "override" | "schedule"
    reason: str
    until: dt.datetime | None

    def state_for(self, loop_name: str) -> bool:
        return self.preset.state_for(loop_name)


@dataclass(frozen=True, slots=True)
class PresetResolution:
    """The governing preset, and whether arriving at it FAILED.

    ``active is None`` alone cannot tell a box with no schedule from one whose override
    names a deleted preset — and the two must not permit the same things.
    """

    active: ActivePreset | None
    #: Resolution could not complete: an unreadable read, or a dangling override /
    #: schedule / slot reference. Loop selection ignores it (a broken config must never
    #: brick the fleet); :func:`teatree.core.mode_resolution.owner_voice_forbidden` does not.
    failed: bool = False


@cached_per_request
def resolve_preset_resolution(now: dt.datetime | None = None) -> PresetResolution:
    """The preset governing at *now*, and whether resolving it failed.

    Fail-open: any read error resolves to no preset with a WARNING, so an unreadable DB or
    a broken schedule can never disable the fleet. The failure travels with the answer.
    """
    moment = now or timezone.now()
    try:
        return _resolve_active_preset(moment)
    except Exception:
        logger.warning("preset resolution failed — failing open to base config (no preset)", exc_info=True)
        return PresetResolution(active=None, failed=True)


def resolve_active_preset(now: dt.datetime | None = None) -> ActivePreset | None:
    """The preset governing at *now* (manual override, then schedule), or ``None``.

    The preset-only reading of :func:`resolve_preset_resolution`, for the loop-selection
    callers that are right to treat a failure as no preset.
    """
    return resolve_preset_resolution(now).active


def preset_state_for(active: ActivePreset | None, loop_name: str) -> bool | None:
    """What an already-resolved *active* preset says about *loop_name*, or ``None`` for no preset.

    ``None`` means no preset GOVERNS — never "the preset has no opinion", which a total
    table cannot express. :func:`teatree.core.mode_resolution.resolve_active_mode` is what
    turns that into the configured default's answer.
    """
    return None if active is None else active.state_for(loop_name)


def resolve_preset_state(loop_name: str, now: dt.datetime | None = None) -> bool | None:
    """The single-lookup form of :func:`preset_state_for` — resolves the preset, then asks it."""
    return preset_state_for(resolve_active_preset(now), loop_name)


def next_boundary(now: dt.datetime | None = None) -> dt.datetime | None:
    """The next active-schedule slot boundary strictly after *now*, or ``None``.

    What the transitions pass stamps against, and what a surface renders as "this posture
    holds until". ``None`` when there is no active schedule with a later slot start.
    """
    moment = now or timezone.now()
    try:
        schedule = _active_schedule()
        if schedule is None:
            return None
        _, boundary = _governing_and_next(schedule, moment)
    except Exception:
        logger.warning("preset next-boundary resolution failed — treating as no boundary", exc_info=True)
        return None
    return boundary


def consistency_findings() -> list[str]:
    """Dangling-reference findings for ``t3 doctor``: deleted presets, loops, schedules.

    Reports (never repairs) a slot/override pointing at a deleted preset, a preset
    entry naming a deleted loop, and an ``active_loop_schedule`` naming an unknown
    schedule — the graceful-degradation surface for the by-name references that
    fail open at read time. Returns an empty list when everything resolves.
    """
    from teatree.core.models import (  # noqa: PLC0415 — deferred import (cycle-safe / pre-app-registry)
        ConfigSetting,
        Loop,
        Mode,
        ModeOverride,
        ModeSchedule,
        ModeScheduleSlot,
    )

    findings: list[str] = []
    preset_names = set(Mode.objects.values_list("name", flat=True))
    loop_names = set(Loop.objects.values_list("name", flat=True))

    override = ModeOverride.objects.order_by("-set_at").first()
    if override is not None and override.preset_name not in preset_names:
        findings.append(f"manual override names deleted preset {override.preset_name!r} (fails open to base config)")

    findings.extend(
        f"schedule {slot.schedule.name!r} slot names deleted preset {slot.preset_name!r} (fails open to base config)"
        for slot in ModeScheduleSlot.objects.exclude(preset_name__in=preset_names).select_related("schedule")
    )

    for preset in Mode.objects.all():
        unknown = sorted(name for name in preset.entries if name not in loop_names)
        if unknown:
            findings.append(f"preset {preset.name!r} entries name unknown loops: {', '.join(unknown)}")

    active = ConfigSetting.objects.get_effective(ACTIVE_SCHEDULE_SETTING)
    if isinstance(active, str) and active.strip() and not ModeSchedule.objects.filter(name=active.strip()).exists():
        findings.append(f"active_loop_schedule names unknown schedule {active.strip()!r} (no L2 layer applies)")

    return findings


def _resolve_active_preset(now: dt.datetime) -> PresetResolution:
    override = _override_resolution()
    return override if override is not None else _schedule_resolution(now)


def _override_resolution() -> PresetResolution | None:
    """The manual override layer's answer, or ``None`` when nobody has set an override."""
    from teatree.core.models import (  # noqa: PLC0415 — deferred import (cycle-safe / pre-app-registry)
        Mode,
        ModeOverride,
    )

    override = ModeOverride.objects.current()
    if override is None:
        return None
    preset = Mode.objects.by_name(override.preset_name)
    if preset is None:
        logger.warning(
            "loop preset override names deleted preset %r — failing open to base config", override.preset_name
        )
        return PresetResolution(active=None, failed=True)
    active = ActivePreset(preset=preset, layer="override", reason=_override_reason(override), until=None)
    return PresetResolution(active=active)


def _schedule_resolution(now: dt.datetime) -> PresetResolution:
    """The active schedule layer's answer: its governing slot at *now*, or no preset."""
    from teatree.core.models import Mode  # noqa: PLC0415 — deferred import (cycle-safe / pre-app-registry)

    name = _active_schedule_name()
    if not name:
        return PresetResolution(active=None)
    schedule = _schedule_by_name(name)
    if schedule is None:
        return PresetResolution(active=None, failed=True)
    slot, boundary = _governing_and_next(schedule, now)
    if slot is None:
        return PresetResolution(active=None)
    preset = Mode.objects.by_name(slot.preset_name)
    if preset is None:
        logger.warning(
            "loop schedule %r slot names deleted preset %r — failing open to base config",
            schedule.name,
            slot.preset_name,
        )
        return PresetResolution(active=None, failed=True)
    reason = f"schedule {schedule.name} slot {_slot_label(slot)}"
    return PresetResolution(active=ActivePreset(preset=preset, layer="schedule", reason=reason, until=boundary))


def _active_schedule_name() -> str:
    """The schedule ``active_loop_schedule`` names, or ``""`` when none is configured."""
    from teatree.core.models import ConfigSetting  # noqa: PLC0415 — deferred import (cycle-safe / pre-app-registry)

    raw = ConfigSetting.objects.get_effective(ACTIVE_SCHEDULE_SETTING)
    return raw.strip() if isinstance(raw, str) and raw.strip() else ""


def _schedule_by_name(name: str) -> "ModeSchedule | None":
    """The named ``ModeSchedule``, or ``None`` when the setting points at a deleted one."""
    from teatree.core.models import ModeSchedule  # noqa: PLC0415 — deferred import (cycle-safe / pre-app-registry)

    schedule = ModeSchedule.objects.filter(name=name).first()
    if schedule is None:
        logger.warning("active_loop_schedule names unknown schedule %r — failing open to base config", name)
    return schedule


def _active_schedule() -> "ModeSchedule | None":
    """The ``ModeSchedule`` the ``active_loop_schedule`` setting selects, or ``None``."""
    name = _active_schedule_name()
    return _schedule_by_name(name) if name else None


def _governing_and_next(
    schedule: "ModeSchedule", now: dt.datetime
) -> "tuple[ModeScheduleSlot | None, dt.datetime | None]":
    """The slot governing *now* (latest start ≤ now) and the next start after it.

    Slot starts are the schedule's local wall-clock times materialised as aware
    instants over a ±7-day window, so the governing/next pair is found by a single
    min/max over that window — the coverage model, no cron span arithmetic.
    """
    tz = _schedule_zone(schedule.timezone)
    now_local = now.astimezone(tz)
    governing: tuple[dt.datetime, ModeScheduleSlot] | None = None
    boundary: dt.datetime | None = None
    for slot_start, slot in _candidate_starts(schedule, now_local, tz):
        if slot_start <= now and (governing is None or slot_start > governing[0]):
            governing = (slot_start, slot)
        elif slot_start > now and (boundary is None or slot_start < boundary):
            boundary = slot_start
    return (governing[1] if governing is not None else None), boundary


def _candidate_starts(
    schedule: "ModeSchedule", now_local: dt.datetime, tz: dt.tzinfo
) -> "list[tuple[dt.datetime, ModeScheduleSlot]]":
    slots = list(schedule.slots.all())  # Django reverse FK (related_name="slots")
    days = [(now_local + dt.timedelta(days=offset)).date() for offset in range(-_LOOKBACK_DAYS, _LOOKBACK_DAYS + 1)]
    return [
        (dt.datetime.combine(day, slot.start_time, tzinfo=tz), slot)
        for day in days
        for slot in slots
        if day.weekday() in slot.weekdays
    ]


def _schedule_zone(name: str) -> dt.tzinfo:
    """The slot timezone: the schedule's validated zoneinfo key, else the project zone."""
    if name:
        try:
            return zoneinfo.ZoneInfo(name)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            logger.warning("loop schedule timezone %r invalid — using the project timezone", name)
    return timezone.get_current_timezone()


def _slot_label(slot: "ModeScheduleSlot") -> str:
    days = ",".join(_WEEKDAY_NAMES[day] for day in sorted(slot.weekdays))
    return f"{days} {slot.start_time.strftime('%H:%M')}"


def _override_reason(override: "object") -> str:
    return f"manual override — {getattr(override, 'reason', '')}"


_WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


__all__ = [
    "ACTIVE_SCHEDULE_SETTING",
    "ActivePreset",
    "PresetResolution",
    "consistency_findings",
    "next_boundary",
    "preset_state_for",
    "resolve_active_preset",
    "resolve_preset_resolution",
    "resolve_preset_state",
]
