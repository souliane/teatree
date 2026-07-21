"""Pure read-time preset resolver — the L3/L2 mask over the base loop config (#3159).

The layered run verdict for a loop at instant *t* is resolved top-down; the first
layer with an opinion wins:

* **L4 LoopState hold** — the emergency brake, applied by the ``held`` arm of
    :func:`teatree.loop.loop_state_db.loop_state_admits`; **never** touched here.
* **L3 manual override** — a live :class:`teatree.core.models.ModeOverride`.
* **L2 active schedule slot** — the ``active_loop_schedule`` calendar's governing
    slot at *t* (the latest slot-start ≤ *t*, searching back across week wrap).
* **L1 base config** — ``Loop.enabled``, the fallback when no preset has an opinion.

This module owns L3 + L2 only, returning a **tri-state per-loop opinion**:
``True`` (force on), ``False`` (force off), or ``None`` (no opinion — inherit L1).
Every caller passes that opinion to ``loop_state_admits(..., preset_state=...)``.

**Empty-table no-op invariant** (#1913 / #1775 shape): with no override, no active
schedule, and no preset, :func:`resolve_active_preset` returns ``None`` and every
loop resolves ``preset_state=None`` — byte-for-byte today's two-plane verdict.

**Fail-open everywhere:** any error (deleted preset, unreadable DB, bad timezone)
resolves to ``None`` (base config) with a WARNING, mirroring the fail-safe doctrine
of :func:`teatree.loop.loop_state_db.loop_held_in_db`. A broken schedule must never
brick the loop fleet.

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

    def state_for(self, loop_name: str) -> bool | None:
        return self.preset.state_for(loop_name)


def resolve_active_preset(now: dt.datetime | None = None) -> ActivePreset | None:
    """The preset governing at *now* (L3 override, then L2 schedule), or ``None``.

    Fail-open: any read error resolves to ``None`` (base config) with a WARNING, so
    an unreadable DB or a broken schedule can never disable the fleet.
    """
    moment = now or timezone.now()
    try:
        return _resolve_active_preset(moment)
    except Exception:
        logger.warning("preset resolution failed — failing open to base config (no preset)", exc_info=True)
        return None


def preset_state_for(active: ActivePreset | None, loop_name: str) -> bool | None:
    """The tri-state opinion an already-resolved *active* preset holds for *loop_name*."""
    return None if active is None else active.state_for(loop_name)


def resolve_preset_state(loop_name: str, now: dt.datetime | None = None) -> bool | None:
    """The single-lookup preset opinion for *loop_name*: ``True``/``False``/``None``.

    The per-loop form the off-live-tick daily gates and connector preflight consume
    through :func:`teatree.loop.loop_state_db.loop_enabled`; the bulk tick resolves
    :func:`resolve_active_preset` once and calls :func:`preset_state_for` per row.
    """
    return preset_state_for(resolve_active_preset(now), loop_name)


def next_boundary(now: dt.datetime | None = None) -> dt.datetime | None:
    """The next active-schedule slot boundary strictly after *now*, or ``None``.

    Used by ``t3 loop preset use`` to bound a manual override "until the next
    scheduled boundary". ``None`` when there is no active schedule with a later
    slot start (so an override with no explicit TTL then holds until cleared).
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


def active_overlay_scope(now: dt.datetime | None = None) -> list[str]:
    """The backend-name allowlist the active preset restricts scanners to (``[]`` = all)."""
    active = resolve_active_preset(now)
    return active.preset.overlay_scope_names if active is not None else []


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


def _resolve_active_preset(now: dt.datetime) -> ActivePreset | None:
    from teatree.core.models import (  # noqa: PLC0415 — deferred import (cycle-safe / pre-app-registry)
        Mode,
        ModeOverride,
    )

    override = ModeOverride.objects.current(now)
    if override is not None:
        preset = Mode.objects.by_name(override.preset_name)
        if preset is None:
            logger.warning(
                "loop preset override names deleted preset %r — failing open to base config", override.preset_name
            )
            return None
        return ActivePreset(preset=preset, layer="override", reason=_override_reason(override), until=override.until)

    schedule = _active_schedule()
    if schedule is None:
        return None
    slot, boundary = _governing_and_next(schedule, now)
    if slot is None:
        return None
    preset = Mode.objects.by_name(slot.preset_name)
    if preset is None:
        logger.warning(
            "loop schedule %r slot names deleted preset %r — failing open to base config",
            schedule.name,
            slot.preset_name,
        )
        return None
    reason = f"schedule {schedule.name} slot {_slot_label(slot)}"
    return ActivePreset(preset=preset, layer="schedule", reason=reason, until=boundary)


def _active_schedule() -> "ModeSchedule | None":
    """The ``ModeSchedule`` the ``active_loop_schedule`` setting selects, or ``None``."""
    from teatree.core.models import (  # noqa: PLC0415 — deferred import (cycle-safe / pre-app-registry)
        ConfigSetting,
        ModeSchedule,
    )

    raw = ConfigSetting.objects.get_effective(ACTIVE_SCHEDULE_SETTING)
    if not isinstance(raw, str) or not raw.strip():
        return None
    schedule = ModeSchedule.objects.filter(name=raw.strip()).first()
    if schedule is None:
        logger.warning("active_loop_schedule names unknown schedule %r — failing open to base config", raw)
    return schedule


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
    slots = list(schedule.slots.all())  # ty: ignore[unresolved-attribute]  # Django reverse FK (related_name="slots")
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
    base = "override"
    reason = getattr(override, "reason", "")
    return f"{base} ({reason})" if reason else base


_WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


__all__ = [
    "ACTIVE_SCHEDULE_SETTING",
    "ActivePreset",
    "active_overlay_scope",
    "consistency_findings",
    "next_boundary",
    "preset_state_for",
    "resolve_active_preset",
    "resolve_preset_state",
]
