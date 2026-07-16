"""Effective-verdict surface shared by ``preset show``, ``loops list``, and the statusline (#3159).

One source of truth for "which preset governs now, why, and what each loop's
effective verdict is" so the three observability surfaces can never drift. The
per-loop deciding layer mirrors the resolution order exactly:

* ``hold`` — a ``LoopState`` PAUSE/DISABLE (L4, always wins)
* ``override`` / ``schedule`` — the active preset holds an opinion for this loop (L3/L2)
* ``base`` — no preset opinion; ``Loop.enabled`` decides (L1)

Fails open: a resolver error degrades to the base config verdict (no preset), so a
broken schedule can never blank these read-only surfaces.
"""

import datetime as dt
from dataclasses import dataclass

from django.utils import timezone

from teatree.loop.loop_state_db import control_planes_in_db, loop_state_admits
from teatree.loop.preset_resolution import ActivePreset, preset_state_for, resolve_active_preset


@dataclass(frozen=True, slots=True)
class PresetSummary:
    """The active preset plus the human 'why' the WHY-line and statusline render."""

    name: str
    layer: str  # "override" | "schedule"
    reason: str
    until: dt.datetime | None
    availability_pin: str | None


@dataclass(frozen=True, slots=True)
class LoopVerdict:
    """One loop's effective run verdict and the layer that decided it."""

    name: str
    admitted: bool
    layer: str  # "hold" | "override" | "schedule" | "base"
    detail: str


def active_summary(now: dt.datetime | None = None) -> PresetSummary | None:
    """The active preset summary, or ``None`` when no preset governs."""
    active = resolve_active_preset(now)
    if active is None:
        return None
    return PresetSummary(
        name=active.preset.name,
        layer=active.layer,
        reason=active.reason,
        until=active.until,
        availability_pin=active.preset.availability_pin,
    )


def effective_verdicts(now: dt.datetime | None = None) -> list[LoopVerdict]:
    """The effective run verdict + deciding layer for every ``Loop`` row, sorted by name."""
    from teatree.core.models import Loop  # noqa: PLC0415 — deferred import (cycle-safe / pre-app-registry)

    moment = now or timezone.now()
    active = resolve_active_preset(moment)
    held, forced = control_planes_in_db()
    verdicts = [
        _verdict_for(loop, held=loop.name in held, forced=forced.get(loop.name), active=active)
        for loop in Loop.objects.all()
    ]
    return sorted(verdicts, key=lambda verdict: verdict.name)


def statusline_chunk(now: dt.datetime | None = None) -> str:
    """The one-chunk preset segment, or ``""`` when no preset governs.

    Schedule-governed → ``preset engaged →19:00``. A MANUAL override (#3248) is
    flagged ``preset ⚠heads-down (manual →21:00)`` so the operator sees the
    active schedule is NOT the one governing. Sourced from the SAME resolver as
    ``preset show`` so the two never disagree.
    """
    summary = active_summary(now)
    if summary is None:
        return ""
    boundary = _boundary_hhmm(summary.until)
    if summary.layer == "override":
        return f"preset ⚠{summary.name} (manual{boundary})"
    return f"preset {summary.name}{boundary}"


def schedule_chunk() -> str:
    """The active-schedule segment ``sched standard``, or ``""`` when no schedule is active."""
    from teatree.core.models import ConfigSetting  # noqa: PLC0415 — deferred import (cycle-safe / pre-app-registry)
    from teatree.loop.preset_resolution import ACTIVE_SCHEDULE_SETTING  # noqa: PLC0415 — deferred: cycle-safe

    try:
        raw = ConfigSetting.objects.get_effective(ACTIVE_SCHEDULE_SETTING)
    except Exception:  # noqa: BLE001 — rendering is best-effort; a broken read degrades to no segment
        return ""
    return f"sched {raw.strip()}" if isinstance(raw, str) and raw.strip() else ""


def manual_override_entries(now: dt.datetime | None = None) -> list[tuple[str, bool]]:
    """Per-loop manual FORCED overrides that DIVERGE from the preset/base verdict (#3248).

    Returns ``(loop_name, forced_on)`` for every loop whose live FORCED value
    differs from what the preset (else base ``Loop.enabled``) would decide - the
    ``ovr:`` statusline section. A force that agrees with the underlying verdict
    is not surfaced (it changes nothing). Sorted by name; fails open to ``[]``.
    """
    from teatree.core.models import Loop  # noqa: PLC0415 — deferred import (cycle-safe / pre-app-registry)

    moment = now or timezone.now()
    active = resolve_active_preset(moment)
    _, forced = control_planes_in_db()
    entries: list[tuple[str, bool]] = []
    for loop in Loop.objects.all():
        value = forced.get(loop.name)
        if value is None:
            continue
        opinion = preset_state_for(active, loop.name)
        base = opinion if opinion is not None else loop.enabled
        if value != base:
            entries.append((loop.name, value))
    return sorted(entries)


def manual_override_chunk(now: dt.datetime | None = None) -> str:
    """The ``ovr: review- news+`` per-loop manual-override segment, or ``""`` when none diverge."""
    entries = manual_override_entries(now)
    if not entries:
        return ""
    return "ovr: " + " ".join(f"{name}{'+' if on else '-'}" for name, on in entries)


def preset_line_chunk(now: dt.datetime | None = None) -> str:
    """The composed schedule/preset/override statusline segment (#3248), or ``""`` when nothing governs.

    Joins the non-empty ``sched``, ``preset`` (with the ⚠manual marker), and
    ``ovr:`` sub-segments with the mid-dot. The single injected preset-segment
    reader the loop line renders, replacing the bare ``statusline_chunk``.
    """
    parts = [chunk for chunk in (schedule_chunk(), statusline_chunk(now), manual_override_chunk(now)) if chunk]
    return " · ".join(parts)


def _verdict_for(loop: object, *, held: bool, forced: bool | None, active: ActivePreset | None) -> LoopVerdict:
    name: str = loop.name  # ty: ignore[unresolved-attribute]
    configured: bool = loop.enabled  # ty: ignore[unresolved-attribute]
    opinion = preset_state_for(active, name)
    admitted = loop_state_admits(configured_enabled=configured, held=held, preset_state=opinion, forced=forced)
    if held:
        return LoopVerdict(name=name, admitted=admitted, layer="hold", detail="LoopState hold")
    if forced is not None:
        return LoopVerdict(name=name, admitted=admitted, layer="forced", detail=f"override {'on' if forced else 'off'}")
    if opinion is not None and active is not None:
        return LoopVerdict(name=name, admitted=admitted, layer=active.layer, detail=active.reason)
    return LoopVerdict(name=name, admitted=admitted, layer="base", detail="Loop.enabled")


def _boundary_hhmm(until: dt.datetime | None) -> str:
    if until is None:
        return ""
    return " →" + timezone.localtime(until).strftime("%H:%M")


__all__ = [
    "LoopVerdict",
    "PresetSummary",
    "active_summary",
    "effective_verdicts",
    "manual_override_chunk",
    "manual_override_entries",
    "preset_line_chunk",
    "schedule_chunk",
    "statusline_chunk",
]
