"""`t3 doctor` availability-override staleness finding (#3274).

A no-expiry DEFERRING availability override — ``away`` / ``autonomous_away`` with
no ``until`` — silently suppresses the colleague-facing loops (and, under
holiday-``away``, pauses the self-pump) for as long as it sits; the incident that
motivated the finding left one active for ~30h. This module owns the doctor check
plus the pure mtime/finding helpers it keys on, kept out of ``core.availability``
so that module stays a scheduling primitive rather than a doctor presentation
surface. The availability *semantics* the finding depends on — which modes defer
questions / pause the pump — live on :class:`~teatree.core.availability.Override`
in core (``Override.defers_questions`` / ``Override.pauses_self_pump``).
"""

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from teatree.core.availability import Override

# How long a no-expiry DEFERRING override may sit before `t3 doctor` flags it as a
# likely-forgotten footgun (#3274). The incident that motivated the finding left
# an `autonomous_away` override with no `until` active for ~30h, silently
# suppressing the colleague-facing loops the whole time.
STALE_OVERRIDE_AGE = timedelta(hours=12)


def override_set_at(path: Path | None = None) -> datetime | None:
    """When the durable override file was last written (its mtime), or ``None``.

    The override payload carries no created-at timestamp, so the file mtime is
    the signal for "how long has this override been active" the staleness finding
    keys on.
    """
    from teatree.core import availability  # noqa: PLC0415 — deferred: keeps CLI startup light

    target = path or availability.override_path()
    try:
        return datetime.fromtimestamp(target.stat().st_mtime, tz=UTC)
    except OSError:
        return None


def stale_override_finding(
    *,
    override: "Override | None",
    set_at: datetime | None,
    now: datetime,
    colleague_facing_loops: Iterable[str],
    max_age: timedelta = STALE_OVERRIDE_AGE,
) -> str | None:
    """A `t3 doctor` warning when a no-expiry DEFERRING override outlives *max_age* (#3274).

    Returns ``None`` (no finding) unless every footgun condition holds: the
    override is active, has NO ``until`` (a bounded override self-clears, so it is
    not the silent-forever footgun), is a DEFERRING mode (a ``present`` override
    suppresses nothing), and was set more than *max_age* ago. The message names
    the colleague-facing loops the mode defers and, for holiday-``away``, that the
    self-pump is paused too (every loop stalls).
    """
    if override is None or not override.is_active(now):
        return None
    if override.until is not None or not override.defers_questions:
        return None
    if set_at is None or now - set_at < max_age:
        return None
    hours = int((now - set_at) / timedelta(hours=1))
    loops = ", ".join(sorted(colleague_facing_loops)) or "the review/followup loops"
    pausing = (
        " It ALSO pauses the self-pump — every loop stalls, not just colleague-facing ones."
        if override.pauses_self_pump
        else ""
    )
    return (
        f"WARN  availability override mode={override.mode!r} has had NO expiry for ~{hours}h — it "
        f"silently defers questions and suppresses the colleague-facing loops ({loops}).{pausing} "
        f"If unintended, clear it with `t3 teatree availability auto`. (#3274)"
    )


def _check_availability_override_staleness() -> None:
    """Warn on a no-expiry deferring availability override active past the threshold (#3274).

    A manual ``away`` / ``autonomous_away`` override with no ``until`` silently
    suppresses the colleague-facing loops (and, for holiday-``away``, pauses the
    self-pump) for as long as it sits — the incident that motivated the finding
    left one active for ~30h. Surfacing-only (never gates the exit code), like the
    sibling ORM-reading advisories; reads the ``Loop`` table for the deferred loop
    names, so it runs post-``ensure_django``. Crash-proof: any error degrades to a
    silent pass so a doctor run never aborts.
    """
    from teatree.core import availability  # noqa: PLC0415 — deferred: keeps CLI startup light
    from teatree.core.models import Loop  # noqa: PLC0415 — deferred: ORM import needs the app registry

    try:
        override = availability.load_override()
        if override is None:
            return
        colleague_facing = list(Loop.objects.filter(colleague_facing=True).values_list("name", flat=True))
        message = stale_override_finding(
            override=override,
            set_at=override_set_at(),
            now=datetime.now(tz=UTC),
            colleague_facing_loops=colleague_facing,
        )
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(f"WARN  Availability-override check crashed: {exc.__class__.__name__}: {exc}")
        return
    if message:
        typer.echo(message)
