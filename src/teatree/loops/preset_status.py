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
from teatree.loop.statusline_loops import PresetLineHandles


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
    """The one-chunk preset segment, or ``""`` when no preset governs (#3494).

    Spelled out for the loop line: a MANUAL override renders ``preset: manual``
    (the layer, not the underlying preset name — the operator's cue that the
    active schedule is NOT the one governing); a schedule-driven preset renders
    ``preset: <name>``. A boundary is appended when the preset expires at a known
    time (``preset: manual →21:00``). Sourced from the SAME resolver as ``preset
    show`` so the two never disagree.
    """
    summary = active_summary(now)
    if summary is None:
        return ""
    boundary = _boundary_hhmm(summary.until)
    if summary.layer == "override":
        return f"preset: manual{boundary}"
    return f"preset: {summary.name}{boundary}"


def schedule_chunk() -> str:
    """The active-schedule segment, always spelled out (#3494).

    ``schedule: <name>`` when a weekly schedule is active, else ``schedule: none
    active`` — the schedule handle is always shown so the operator reads the
    schedule state at a glance even when none governs. Fails open to ``schedule:
    none active`` on a broken read.
    """
    from teatree.core.models import ConfigSetting  # noqa: PLC0415 — deferred import (cycle-safe / pre-app-registry)
    from teatree.loop.preset_resolution import ACTIVE_SCHEDULE_SETTING  # noqa: PLC0415 — deferred: cycle-safe

    try:
        raw = ConfigSetting.objects.get_effective(ACTIVE_SCHEDULE_SETTING)
    except Exception:  # noqa: BLE001 — rendering is best-effort; a broken read degrades to "none active"
        return "schedule: none active"
    name = raw.strip() if isinstance(raw, str) else ""
    return f"schedule: {name}" if name else "schedule: none active"


def manual_override_entries(now: dt.datetime | None = None) -> list[tuple[str, bool]]:
    """Per-loop manual FORCED overrides that DIVERGE from the preset/base verdict (#3248).

    Returns ``(loop_name, forced_on)`` for every loop whose live FORCED value
    differs from what the preset (else base ``Loop.enabled``) would decide - the
    ``forced ON:`` / ``forced OFF:`` statusline section. A force that agrees with
    the underlying verdict is not surfaced (it changes nothing). Sorted by name;
    fails open to ``[]``.
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
    """The spelled-out per-loop manual-override segment, or ``""`` when none diverge (#3494).

    Splits the diverging forces into ``forced ON: <names>`` and ``forced OFF:
    <names>`` (each a comma-separated, name-sorted list), joined with the loop
    line's mid-dot when both are present — e.g. ``forced ON: triage_assessor``.
    """
    entries = manual_override_entries(now)
    if not entries:
        return ""
    on = [name for name, forced_on in entries if forced_on]
    off = [name for name, forced_on in entries if not forced_on]
    parts = []
    if on:
        parts.append("forced ON: " + ", ".join(on))
    if off:
        parts.append("forced OFF: " + ", ".join(off))
    return " · ".join(parts)


def overridden_loop_names(now: dt.datetime | None = None) -> set[str]:
    """The bare names of every loop manually overridden away from the preset/base verdict (#3248).

    The statusline per-loop-lease collapse
    (:func:`teatree.loop.statusline_loops.live_loops_anchor`) keeps a
    ``loop:<name>`` chunk only for a manually-overridden loop; this is the
    injected selector it reads. Sharing :func:`manual_override_entries`'
    divergence logic keeps the surfaced lease chunks and the ``forced ON:`` /
    ``forced OFF:`` segment from ever disagreeing about which loops diverge from
    the handle.
    """
    return {name for name, _ in manual_override_entries(now)}


def preset_line_handles(now: dt.datetime | None = None) -> PresetLineHandles:
    """The three ordered loop-line handles (#3494): schedule, preset, per-loop overrides.

    The injected reader the statusline loop line renders (installed by the
    ``loops_tick`` per-loop command). Sourced from the SAME resolvers as
    ``preset show`` / ``loops list``, so the observability surfaces never
    disagree. The renderer places the schedule and preset handles ahead of the
    loop chunks and the ``forced ON:`` / ``forced OFF:`` overrides after them.
    """
    return PresetLineHandles(
        schedule=schedule_chunk(),
        preset=statusline_chunk(now),
        override=manual_override_chunk(now),
    )


def preset_line_chunk(now: dt.datetime | None = None) -> str:
    """The composed schedule/preset/override statusline segment (#3248, #3494).

    Joins the non-empty ``schedule:``, ``preset:``, and ``forced ON:`` /
    ``forced OFF:`` sub-segments with the mid-dot — the single-string bundled
    view of :func:`preset_line_handles` for surfaces that want one flat segment.
    Always at least ``schedule: none active`` (the schedule handle is always
    shown).
    """
    handles = preset_line_handles(now)
    parts = [chunk for chunk in (handles.schedule, handles.preset, handles.override) if chunk]
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
    "overridden_loop_names",
    "preset_line_chunk",
    "preset_line_handles",
    "schedule_chunk",
    "statusline_chunk",
]
