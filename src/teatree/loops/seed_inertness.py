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

Sibling of :mod:`teatree.loops.seed_drift`, which reports a row whose *classification*
disagrees with the same shipped table. Two readers, one shipped file, one question each.
"""

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from teatree.loops.preset_seed import default_preset_specs, default_schedule_specs
from teatree.loops.seed import LoopSeedSpec, load_loop_specs

#: A shipped family and the ``defaults.toml`` table it ships in. The ``modes``/``preset``
#: pair is the footgun this mapping exists to contain: the file table is ``[modes.*]``, the
#: model is ``Mode``, and the word every surface says is "preset". Nothing else translates.
FAMILY_TABLES: dict[str, str] = {"loop": "loops", "preset": "modes", "schedule": "schedules"}

KIND_MISSING = "missing"
KIND_DISABLED_VS_SHIPPED = "disabled_vs_shipped"
KIND_DISABLED = "disabled"
KIND_STALE = "stale"
KIND_SUPPRESSED = "suppressed"
KIND_EMPTY_MASK = "empty_mask"
KIND_EMPTY = "empty"
KIND_DANGLING_SLOT = "dangling_slot"
KIND_INACTIVE = "inactive"

__all__ = [
    "FAMILY_TABLES",
    "KIND_DANGLING_SLOT",
    "KIND_DISABLED",
    "KIND_DISABLED_VS_SHIPPED",
    "KIND_EMPTY",
    "KIND_EMPTY_MASK",
    "KIND_INACTIVE",
    "KIND_MISSING",
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
    """Every shipped loop/preset/schedule that is missing, disabled, or not ticking.

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
    from teatree.loops.loop_staleness import (  # noqa: PLC0415 — deferred: ORM-backed read
        STALE_CADENCE_MULTIPLIER,
        stale_loops,
    )

    rows = {row.name: row for row in Loop.objects.all()}
    behind = {loop.name: loop for loop in stale_loops(now)}
    findings = []
    for spec in load_loop_specs(path):
        row = rows.get(spec.name)
        if row is None:
            findings.append(_missing("loop", spec.name, spec.description))
            continue
        if not row.enabled:
            findings.append(_disabled(spec))
            continue
        stale = behind.get(spec.name)
        if stale is not None:
            findings.append(
                InertFinding(
                    family="loop",
                    name=spec.name,
                    kind=KIND_SUPPRESSED if stale.suppressed else KIND_STALE,
                    detail=(
                        f"enabled but {stale.age_label} against a {stale.cadence_seconds}s cadence "
                        f"(over {STALE_CADENCE_MULTIPLIER}x) — "
                        + (
                            "a mode mask, the colleague gate or a LoopState hold accounts for it"
                            if stale.suppressed
                            else "nothing in the mode mask, the colleague gate or a LoopState hold explains it"
                        )
                    ),
                    is_fault=not stale.suppressed,
                )
            )
    return findings


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
    from teatree.core.models import Mode  # noqa: PLC0415 — deferred: ORM import needs the app registry

    rows = {row.name: row for row in Mode.objects.all()}
    findings = []
    for spec in default_preset_specs(path):
        row = rows.get(spec.name)
        if row is None:
            findings.append(_missing("preset", spec.name, spec.description))
        elif spec.entries and not row.entries:
            findings.append(
                InertFinding(
                    family="preset",
                    name=spec.name,
                    kind=KIND_EMPTY_MASK,
                    detail=(
                        f"its mask is empty but ships {len(spec.entries)} loop opinion(s) — every loop now "
                        "inherits its own flag, so activating this preset changes nothing"
                    ),
                    is_fault=True,
                )
            )
    return findings


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
        slots = list(row.slots.all())
        dangling = sorted({slot.preset_name for slot in slots if slot.preset_name not in presets})
        if dangling:
            findings.append(
                InertFinding(
                    family="schedule",
                    name=spec.name,
                    kind=KIND_DANGLING_SLOT,
                    detail=(
                        f"slot(s) name preset(s) that do not exist ({', '.join(dangling)}) — those hours "
                        "fail open to base config instead of the calendar's intent"
                    ),
                    is_fault=True,
                )
            )
        elif not slots:
            findings.append(
                InertFinding(
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
            )
        elif spec.name != active:
            findings.append(
                InertFinding(
                    family="schedule",
                    name=spec.name,
                    kind=KIND_INACTIVE,
                    detail=f"not the active calendar ({active or 'none'} governs) — only one can be",
                    is_fault=False,
                )
            )
    return findings


def _missing(family: str, name: str, description: str) -> InertFinding:
    """The one kind sourcing from the seed exists to catch; recoverable, so say how."""
    return InertFinding(
        family=family,
        name=name,
        kind=KIND_MISSING,
        detail=f"ships in defaults.toml but has no DB row — {description} never happens. `t3 setup` recreates it.",
        is_fault=True,
    )
