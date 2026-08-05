"""Which shipped loops/presets/schedules are absent, off, or not actually firing (#3842).

Measured on the live box: 27 shipped loops, 27 present, 0 missing, 10 disabled. **Nothing
had ever been deleted** — every failure that cost time was present-but-inert (a janitor
wired to no loop, a colleague loop held dark by an override, a tick reporting 58 actions
while no ticket advanced). So the guard worth having detects inertness, not absence.

The EXPECTED set is sourced from the shipped seed tables, never from the DB. A check that
reads the DB for both "expected" and "actual" cannot detect a missing row at all — the same
self-referential defect as a golden compared against its own renderer (#3836).

Severity is what makes the report actionable rather than a wall of ten lines nobody reads.
Sourcing ``default_enabled`` from the seed is what buys it: a loop that SHIPS ON and is off
regressed, while a loop that ships off and is off is doing exactly what it shipped doing.
The same "don't cry wolf" doctrine :mod:`teatree.loops.loop_staleness` already applies to a
suppressed stale loop — an operator's deliberate off is a note, not a fault.

A preset is NOT judged on whether anything references it. Four of the seven shipped presets
(``heads-down`` / ``maintenance`` / ``off`` / ``offline``) are named by no slot, override or
setting on a fresh install — they exist to be selected by hand (``t3 loop preset use``), so
"unreferenced" is their shipped state, not a fault. Reporting it would make the report noisy
on every new box, which is how a health surface becomes one people learn to ignore.

Presence alone was not enough (#4096). A live ``standard`` calendar carrying an extra
``Mon-Fri 19:00 -> maintenance`` slot, against a ``maintenance`` mask that stopped delivery
while leaving intake admitted, stalled the merge lane 13h a night — with every row present,
every mask non-empty and every slot naming a real preset, so this report said OK. It now
also compares each live VALUE against the shipped table (:mod:`teatree.loops.seed_drift`)
and judges each live mask against the structural rule in :mod:`teatree.loops.mode_shape`.
A divergence is a NOTE, never a fault and never rewritten — an operator override is a
legitimate per-box decision, and the gap was that nobody could see it. The asymmetry it can
produce IS a fault: a mode that stops the pipeline draining while it keeps filling is
broken whoever wrote it.
"""

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from teatree.loops.mode_shape import INTAKE_LOOPS, intake_without_delivery
from teatree.loops.preset_seed import PresetSpec, ScheduleSpec, default_preset_specs, default_schedule_specs
from teatree.loops.seed import LoopSeedSpec, load_loop_specs
from teatree.loops.seed_drift import SlotShape, mode_entry_drift, schedule_slot_drift

if TYPE_CHECKING:
    from teatree.core.models import Loop, Mode, ModeScheduleSlot

KIND_MISSING = "missing"
KIND_DISABLED_VS_SHIPPED = "disabled_vs_shipped"
KIND_DISABLED = "disabled"
KIND_STALE = "stale"
KIND_SUPPRESSED = "suppressed"
KIND_EMPTY_MASK = "empty_mask"
KIND_EMPTY = "empty"
KIND_DANGLING_SLOT = "dangling_slot"
KIND_INACTIVE = "inactive"
KIND_ENTRIES_OVERRIDDEN = "entries_overridden"
KIND_SLOTS_OVERRIDDEN = "slots_overridden"
KIND_INTAKE_WITHOUT_DELIVERY = "intake_without_delivery"

__all__ = [
    "KIND_DANGLING_SLOT",
    "KIND_DISABLED",
    "KIND_DISABLED_VS_SHIPPED",
    "KIND_EMPTY",
    "KIND_EMPTY_MASK",
    "KIND_ENTRIES_OVERRIDDEN",
    "KIND_INACTIVE",
    "KIND_INTAKE_WITHOUT_DELIVERY",
    "KIND_MISSING",
    "KIND_SLOTS_OVERRIDDEN",
    "KIND_STALE",
    "KIND_SUPPRESSED",
    "InertFinding",
    "shipped_inertness",
]


@dataclass(frozen=True, slots=True)
class InertFinding:
    """One shipped definition that is not doing what shipping it was supposed to buy."""

    family: str
    name: str
    kind: str
    #: What is NOT happening, in the operator's terms — never a bare restatement of *kind*.
    detail: str
    #: A deliberate operator choice is reported but never failed on; see the module docstring.
    is_fault: bool

    @property
    def label(self) -> str:
        return f"{self.family} {self.name}: {self.detail}"

    def as_json(self) -> dict[str, Any]:
        return {
            "family": self.family,
            "name": self.name,
            "kind": self.kind,
            "detail": self.detail,
            "is_fault": self.is_fault,
        }


def shipped_inertness(path: Path | None = None, *, now: dt.datetime | None = None) -> tuple[InertFinding, ...]:
    """Every shipped loop/preset/schedule missing, disabled, not ticking, or diverged from shipped.

    *path* re-points the shipped seed away from the packaged file, which is what lets a test
    declare a name that has deliberately never been seeded — the only way to prove the
    expected set is read from the seed rather than from the DB.

    At most ONE finding per name: the kinds form a priority chain per family (absent beats
    inert beats idle), so a deleted row reads as one clear line rather than three derived ones.
    """
    from django.utils import timezone  # noqa: PLC0415 — deferred: Django import needs settings

    return (
        *_loop_findings(path, now or timezone.now()),
        *_preset_findings(path),
        *_schedule_findings(path),
    )


def _loop_findings(path: Path | None, now: dt.datetime) -> list[InertFinding]:
    from teatree.core.models import Loop  # noqa: PLC0415 — deferred: ORM import needs the app registry
    from teatree.loops.enable_verdict import effective_verdicts  # noqa: PLC0415 — deferred: ORM-backed read
    from teatree.loops.loop_staleness import (  # noqa: PLC0415 — deferred: ORM-backed read
        STALE_CADENCE_MULTIPLIER,
        stale_loops,
    )

    rows = {row.name: row for row in Loop.objects.all()}
    behind = {loop.name: loop for loop in stale_loops(now)}
    # The NARROW verdict, not chain membership: the question here is "is the shipped
    # loop actually working", and membership is the deliberately wider persisted-chain
    # set — a loop kept a member across the presence flip is NOT running right now
    # (#4196), so reporting it as live would be the same false-quiet this file exists
    # to surface.
    admitted = {verdict.name for verdict in effective_verdicts(now) if verdict.admitted}
    findings = []
    for spec in load_loop_specs(path):
        row = rows.get(spec.name)
        if row is None:
            findings.append(_missing("loop", spec.name, spec.description))
            continue
        if not row.enabled:
            findings.append(_disabled(spec))
            continue
        # A column-enabled loop the verdict refuses is not measured by ``stale_loops``
        # at all (#4185) — it has no chain to fall behind on. Its standing still is
        # exactly what the mask or hold asked for, so it earns the same non-fault note
        # it always did, sourced from the verdict rather than a staleness arm.
        stale = behind.get(spec.name)
        if spec.name not in admitted:
            findings.append(_suppressed(spec, row, now))
        elif stale is not None:
            findings.append(
                InertFinding(
                    family="loop",
                    name=spec.name,
                    kind=KIND_SUPPRESSED if stale.suppressed else KIND_STALE,
                    detail=(
                        f"enabled but {stale.age_label} against a {stale.cadence_seconds}s cadence "
                        f"(over {STALE_CADENCE_MULTIPLIER}x) — "
                        + (
                            "the colleague gate accounts for it"
                            if stale.suppressed
                            else "the colleague gate does not explain it"
                        )
                    ),
                    is_fault=not stale.suppressed,
                )
            )
    return findings


def _suppressed(spec: LoopSeedSpec, row: "Loop", now: dt.datetime) -> InertFinding:
    """A column-enabled loop the effective verdict refuses — idle exactly as configured."""
    from teatree.loops.loop_staleness import format_age  # noqa: PLC0415 — deferred: sibling read

    anchor = row.last_run_at or row.created_at
    age = format_age((now - anchor).total_seconds())
    ran = f"last ran {age} ago" if row.last_run_at else f"never run (seeded {age} ago)"
    return InertFinding(
        family="loop",
        name=spec.name,
        kind=KIND_SUPPRESSED,
        detail=(
            f"enabled, but the effective verdict does not admit it ({ran}) — "
            "a mode mask or a LoopState hold accounts for it, so it carries no timer chain."
        ),
        is_fault=False,
    )


def _disabled(spec: LoopSeedSpec) -> InertFinding:
    """A shipped-ON loop found off REGRESSED; a shipped-off loop found off is shipping as designed."""
    regressed = spec.default_enabled
    return InertFinding(
        family="loop",
        name=spec.name,
        kind=KIND_DISABLED_VS_SHIPPED if regressed else KIND_DISABLED,
        detail=(
            f"disabled, but ships ENABLED — {spec.description} is not happening. "
            "Re-enable it, or record why the box wants it off."
            if regressed
            else f"disabled, exactly as it ships (opt-in) — {spec.description}"
        ),
        is_fault=regressed,
    )


def _preset_findings(path: Path | None) -> list[InertFinding]:
    """Every shipped preset missing, plus the one finding each LIVE mode earns.

    Iterating the live rows rather than the shipped specs is what puts an operator-written
    mode under the same structural rule — the asymmetry is a property of the mask, not of a
    name that happens to ship.
    """
    from teatree.core.models import Loop, Mode  # noqa: PLC0415 — deferred: ORM import needs the app registry

    specs = {spec.name: spec for spec in default_preset_specs(path)}
    rows = {row.name: row for row in Mode.objects.all()}
    # An absent mask entry INHERITS, so the asymmetry is only real where the base flag is on.
    base_enabled = dict(Loop.objects.filter(name__in=INTAKE_LOOPS).values_list("name", "enabled"))
    findings = [_missing("preset", spec.name, spec.description) for spec in specs.values() if spec.name not in rows]
    findings.extend(
        finding
        for name, row in rows.items()
        if (finding := _live_preset_finding(name, row, specs.get(name), base_enabled=base_enabled))
    )
    return findings


def _live_preset_finding(
    name: str, row: "Mode", spec: PresetSpec | None, *, base_enabled: dict[str, bool]
) -> InertFinding | None:
    """Inert mask, then the stalling asymmetry, then drift — the actionable line wins."""
    mask = row.entries if isinstance(row.entries, dict) else {}
    if spec is not None and spec.entries and not mask:
        return InertFinding(
            family="preset",
            name=name,
            kind=KIND_EMPTY_MASK,
            detail=(
                f"its mask is empty but ships {len(spec.entries)} loop opinion(s) — every loop now "
                "inherits its own flag, so activating this preset changes nothing"
            ),
            is_fault=True,
        )
    asymmetry = intake_without_delivery(mask, base_enabled=base_enabled)
    if asymmetry is not None:
        return InertFinding(
            family="preset", name=name, kind=KIND_INTAKE_WITHOUT_DELIVERY, detail=asymmetry.detail, is_fault=True
        )
    drift = mode_entry_drift(spec.entries, mask) if spec is not None else ()
    if drift:
        return InertFinding(
            family="preset",
            name=name,
            kind=KIND_ENTRIES_OVERRIDDEN,
            detail=_override_detail("its live mask diverges from defaults.toml", drift),
            is_fault=False,
        )
    return None


def _schedule_findings(path: Path | None) -> list[InertFinding]:
    from teatree.core.models import Mode, ModeSchedule  # noqa: PLC0415 — deferred: ORM import needs the app registry
    from teatree.loops.schedule_editing import active_schedule_name  # noqa: PLC0415 — deferred: ORM-backed read

    active = active_schedule_name()
    presets = set(Mode.objects.values_list("name", flat=True))
    rows = {row.name: row for row in ModeSchedule.objects.prefetch_related("slots")}
    findings = []
    for spec in default_schedule_specs(path):
        row = rows.get(spec.name)
        if row is None:
            findings.append(_missing("schedule", spec.name, spec.description))
            continue
        finding = _live_schedule_finding(
            spec, slots=list(row.slots.all()), timezone=row.timezone, presets=presets, active=active
        )
        if finding is not None:
            findings.append(finding)
    return findings


def _live_schedule_finding(
    spec: ScheduleSpec,
    *,
    slots: list["ModeScheduleSlot"],
    timezone: str,
    presets: set[str],
    active: str | None,
) -> InertFinding | None:
    """A divergence outranks "not the active calendar" — both are notes, one is informative."""
    dangling = sorted({slot.preset_name for slot in slots if slot.preset_name not in presets})
    if dangling:
        return InertFinding(
            family="schedule",
            name=spec.name,
            kind=KIND_DANGLING_SLOT,
            detail=(
                f"slot(s) name preset(s) that do not exist ({', '.join(dangling)}) — those hours "
                "fail open to base config instead of the calendar's intent"
            ),
            is_fault=True,
        )
    if not slots:
        return InertFinding(
            family="schedule",
            name=spec.name,
            kind=KIND_EMPTY,
            detail=(
                "has no slots — it governs the box and selects nothing, so no hour of the week picks a preset"
                if spec.name == active
                else "has no slots, and is not the active calendar"
            ),
            is_fault=spec.name == active,
        )
    inactive = f"not the active calendar ({active or 'none'} governs) — only one can be"
    drift = _schedule_drift(spec, slots=slots, timezone=timezone)
    if drift:
        detail = _override_detail("its live calendar diverges from defaults.toml", drift)
        return InertFinding(
            family="schedule",
            name=spec.name,
            kind=KIND_SLOTS_OVERRIDDEN,
            detail=detail if spec.name == active else f"{detail}; {inactive}",
            is_fault=False,
        )
    if spec.name != active:
        return InertFinding(family="schedule", name=spec.name, kind=KIND_INACTIVE, detail=inactive, is_fault=False)
    return None


def _schedule_drift(spec: ScheduleSpec, *, slots: list["ModeScheduleSlot"], timezone: str) -> tuple[str, ...]:
    zone = (
        ()
        if timezone == spec.timezone
        else (f"timezone shipped={spec.timezone or 'unset'} live={timezone or 'unset'}",)
    )
    return zone + schedule_slot_drift(
        [_slot_shape(tuple(slot.days), slot.start_time, slot.preset_name) for slot in spec.slots],
        [_slot_shape(tuple(slot.weekdays), slot.start_time, slot.preset_name) for slot in slots],
    )


def _slot_shape(days: tuple[int, ...], start_time: dt.time, preset_name: str) -> SlotShape:
    return (tuple(sorted(days)), start_time, preset_name)


def _override_detail(headline: str, drift: tuple[str, ...]) -> str:
    """An override is legitimate, so the line says what differs and stops there."""
    return f"{headline} ({'; '.join(drift)}) — an operator override is legitimate, so this is reported, never rewritten"


def _missing(family: str, name: str, description: str) -> InertFinding:
    """The one kind sourcing from the seed exists to catch; recoverable, so say how."""
    return InertFinding(
        family=family,
        name=name,
        kind=KIND_MISSING,
        detail=f"ships in defaults.toml but has no DB row — {description} never happens. `t3 setup` recreates it.",
        is_fault=True,
    )
