"""Surface — and, on request, reconcile — a live row that disagrees with ``defaults.toml``.

Every seeded row (``Loop``, ``Mode``, ``ModeSchedule``) is written once and never re-read
from its shipped table, so a value that was true when the row was created outlives the
shipped change. The consequence is silent and one-directional: a ``Loop`` stuck at
``colleague_facing=1`` is skipped by the away-class admission gate, so the loop stops
firing while every surface still reports it enabled — the shape that starved cold review,
and with it the ``merge_safe`` verdict the PR sweep merges on. A ``Mode`` whose mask was
edited, or a calendar that gained a slot, hid a 13h-a-night merge stall the same way
(#4096): ``t3 loops audit`` asked only whether each row was PRESENT.

Every one of these values IS operator-editable (the Django admin lists them as editable),
so the seed must not clobber them and the reconcile is explicit
(``seed_loops --reconcile-classification``, for the classification half). Detection is the
standing part: the disagreement is reported by ``t3 doctor check`` and ``t3 loops audit``
with both values named, rather than being inferable only from work that mysteriously never
lands.

The ``Loop`` half reads the ORM; the mode/schedule comparisons are pure over plain values
so the same predicate serves the audit and a shipped-data test.
"""

import datetime as dt
from collections import Counter
from collections.abc import Iterable, Mapping

from teatree.loops.mode_shape import loop_opinion
from teatree.loops.seed import DEFAULT_LOOPS

#: Fields whose shipped value is the classification the away-gate reads. Operator-
#: editable via the admin, so drift is REPORTED always and written back only on an
#: explicit reconcile.
CLASSIFICATION_FIELDS: tuple[str, ...] = ("colleague_facing",)

#: One weekly start point: the weekdays it fires on, its local wall clock, the preset it activates.
type SlotShape = tuple[tuple[int, ...], dt.time, str]

_INHERITS = "absent (inherits Loop.enabled)"
_WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def mode_entry_drift(shipped: Mapping[str, object], live: Mapping[str, object]) -> tuple[str, ...]:
    """One line per loop whose live tri-state opinion differs from the shipped mask."""
    lines = []
    for loop in sorted(set(shipped) | set(live)):
        was, now = _entry_label(shipped, loop), _entry_label(live, loop)
        if was != now:
            lines.append(f"{loop} shipped={was} live={now}")
    return tuple(lines)


def schedule_slot_drift(shipped: Iterable[SlotShape], live: Iterable[SlotShape]) -> tuple[str, ...]:
    """The slots the live calendar adds, then the shipped ones it no longer has.

    Counted rather than set-compared, so a slot row duplicated on the live calendar is a
    divergence and not a value that happens to compare equal to one already there.
    """
    was, now = Counter(shipped), Counter(live)
    return tuple(
        [f"adds {_slot_label(slot)}" for slot in sorted((now - was).elements())]
        + [f"drops {_slot_label(slot)}" for slot in sorted((was - now).elements())]
    )


def _entry_label(entries: Mapping[str, object], loop: str) -> str:
    opinion = loop_opinion(entries, loop)
    if opinion is None:
        return _INHERITS
    return "true" if opinion else "false"


def _slot_label(slot: SlotShape) -> str:
    days, start, preset = slot
    named = ",".join(_weekday_label(day) for day in days) or "no valid weekday"
    return f"{named} {start:%H:%M} -> {preset}"


def _weekday_label(day: int) -> str:
    return _WEEKDAY_NAMES[day] if 0 <= day < len(_WEEKDAY_NAMES) else str(day)


def classification_drift() -> list[str]:
    """One finding per ``Loop`` row disagreeing with its shipped classification."""
    from teatree.core.models import Loop  # noqa: PLC0415 — deferred: ORM import needs the app registry

    stored = {row.name: row for row in Loop.objects.all()}
    findings: list[str] = []
    for spec in DEFAULT_LOOPS:
        row = stored.get(spec.name)
        if row is None:
            continue
        findings.extend(
            f"loop '{spec.name}': DB {field}={getattr(row, field)!r} but shipped defaults.toml "
            f"declares {getattr(spec, field)!r} — the stale row wins at read time"
            for field in CLASSIFICATION_FIELDS
            if getattr(row, field) != getattr(spec, field)
        )
    return findings


def reconcile_classification() -> list[str]:
    """Write the shipped classification back onto every drifting row; return what changed."""
    from teatree.core.models import Loop  # noqa: PLC0415 — deferred: ORM import needs the app registry

    reconciled: list[str] = []
    for spec in DEFAULT_LOOPS:
        for field in CLASSIFICATION_FIELDS:
            shipped = getattr(spec, field)
            updated = Loop.objects.filter(name=spec.name).exclude(**{field: shipped}).update(**{field: shipped})
            if updated:
                reconciled.append(f"loop '{spec.name}': {field} → {shipped!r}")
    return reconciled
