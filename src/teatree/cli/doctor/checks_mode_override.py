"""`t3 doctor` stale-mode-override finding (#3274, #61, #4202).

A no-expiry mode override that masks the colleague-facing loops OFF silently stops
them firing for as long as it sits; the incident that motivated the finding left one
active for ~30h. The override is the DB :class:`~teatree.core.models.ModeOverride`
row, so the finding keys on ``ModeOverride.set_at`` and on the named mode's own
``entries`` table — the single artifact that decides which loops a mode admits, and
the same one the tick's mask reads.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

import typer

# How long a no-expiry SUPPRESSING override may sit before `t3 doctor` flags it as a
# likely-forgotten footgun (#3274). The incident that motivated the finding left one
# with no `until` active for ~30h, silently suppressing the colleague-facing loops.
STALE_OVERRIDE_AGE = timedelta(hours=12)


@dataclass(frozen=True, slots=True)
class OverridePosture:
    """The stale-override finding's inputs: the override's mode + what it masks off."""

    mode_name: str
    #: Colleague-facing loops the named mode forces OFF — empty when it suppresses none.
    suppressed_loops: tuple[str, ...]
    has_expiry: bool
    set_at: datetime | None


def stale_override_finding(
    posture: OverridePosture, *, now: datetime, max_age: timedelta = STALE_OVERRIDE_AGE
) -> str | None:
    """A `t3 doctor` warning when a no-expiry SUPPRESSING mode override outlives *max_age*.

    Returns ``None`` (no finding) unless every footgun condition holds: the override
    has NO ``until`` (a bounded override self-clears, so it is not the silent-forever
    footgun), its mode masks at least one colleague-facing loop OFF, and it was set
    more than *max_age* ago.
    """
    if posture.has_expiry or not posture.suppressed_loops:
        return None
    if posture.set_at is None or now - posture.set_at < max_age:
        return None
    hours = int((now - posture.set_at) / timedelta(hours=1))
    loops = ", ".join(sorted(posture.suppressed_loops))
    return (
        f"WARN  mode override {posture.mode_name!r} has had NO expiry for ~{hours}h — it "
        f"silently suppresses the colleague-facing loops ({loops}). "
        f"If unintended, clear it with `t3 loop preset auto`. (#3274)"
    )


def _suppressed_colleague_loops(entries: object, colleague_facing: Iterable[str]) -> tuple[str, ...]:
    """The colleague-facing loops the mode's ``entries`` table forces OFF."""
    if not isinstance(entries, dict):
        return ()
    table = cast("Mapping[str, object]", entries)
    return tuple(name for name in colleague_facing if table.get(name) is False)


def _check_mode_override_staleness() -> None:
    """Warn on a no-expiry suppressing mode override active past the threshold (#3274, #61).

    Surfacing-only (never gates the exit code), like the sibling ORM-reading
    advisories. Crash-proof: any error degrades to a silent pass so a doctor run never
    aborts.
    """
    from teatree.core.models import Loop, Mode, ModeOverride  # noqa: PLC0415 — deferred: ORM needs the app registry

    try:
        now = datetime.now(tz=UTC)
        override = ModeOverride.objects.current(now)
        if override is None:
            return
        mode = Mode.objects.by_name(override.preset_name)
        colleague_facing = list(Loop.objects.filter(colleague_facing=True).values_list("name", flat=True))
        posture = OverridePosture(
            mode_name=override.preset_name,
            suppressed_loops=_suppressed_colleague_loops(mode.entries if mode is not None else {}, colleague_facing),
            has_expiry=override.until is not None,
            set_at=override.set_at,
        )
        message = stale_override_finding(posture, now=now)
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(f"WARN  Mode-override check crashed: {exc.__class__.__name__}: {exc}")
        return
    if message:
        typer.echo(message)
