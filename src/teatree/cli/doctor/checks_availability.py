"""`t3 doctor` stale-mode-override finding (#3274, #61).

A no-expiry DEFERRING mode override — an away-class mode (``unattended`` /
``offline`` / …) held with no ``until`` — silently suppresses the colleague-facing
loops (and, for a pump-pausing mode, parks the self-pump) for as long as it sits;
the incident that motivated the finding left one active for ~30h. Post-merge (#61)
the override is the DB :class:`~teatree.core.models.ModeOverride` row, so the finding
keys on ``ModeOverride.set_at`` and the resolved mode's intrinsic
``defers_questions`` / ``pauses_self_pump`` booleans — no availability file mtime.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import typer

# How long a no-expiry DEFERRING override may sit before `t3 doctor` flags it as a
# likely-forgotten footgun (#3274). The incident that motivated the finding left an
# away-class override with no `until` active for ~30h, silently suppressing the
# colleague-facing loops the whole time.
STALE_OVERRIDE_AGE = timedelta(hours=12)


@dataclass(frozen=True, slots=True)
class OverridePosture:
    """The stale-override finding's inputs: the mode override's name + resolved posture."""

    mode_name: str
    defers_questions: bool
    pauses_self_pump: bool
    has_expiry: bool
    set_at: datetime | None


def stale_override_finding(
    posture: OverridePosture,
    *,
    now: datetime,
    colleague_facing_loops: Iterable[str],
    max_age: timedelta = STALE_OVERRIDE_AGE,
) -> str | None:
    """A `t3 doctor` warning when a no-expiry DEFERRING mode override outlives *max_age* (#3274).

    Returns ``None`` (no finding) unless every footgun condition holds: the override
    has NO ``until`` (a bounded override self-clears, so it is not the silent-forever
    footgun), the mode DEFERS questions (a present-class mode suppresses nothing), and
    it was set more than *max_age* ago. The message names the colleague-facing loops
    the mode defers and, for a pump-pausing mode, that the self-pump is parked too.
    """
    if posture.has_expiry or not posture.defers_questions:
        return None
    if posture.set_at is None or now - posture.set_at < max_age:
        return None
    hours = int((now - posture.set_at) / timedelta(hours=1))
    loops = ", ".join(sorted(colleague_facing_loops)) or "the review/followup loops"
    pausing = (
        " It ALSO parks the self-pump — every loop stalls, not just colleague-facing ones."
        if posture.pauses_self_pump
        else ""
    )
    return (
        f"WARN  mode override {posture.mode_name!r} has had NO expiry for ~{hours}h — it "
        f"silently defers questions and suppresses the colleague-facing loops ({loops}).{pausing} "
        f"If unintended, clear it with `t3 loop preset auto`. (#3274)"
    )


def _check_availability_override_staleness() -> None:
    """Warn on a no-expiry deferring mode override active past the threshold (#3274, #61).

    A manual away-class mode override with no ``until`` silently suppresses the
    colleague-facing loops (and, for a pump-pausing mode, parks the self-pump) for as
    long as it sits — the incident that motivated the finding left one active for
    ~30h. Surfacing-only (never gates the exit code), like the sibling ORM-reading
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
            defers_questions=bool(mode.defers_questions) if mode is not None else False,
            pauses_self_pump=bool(mode.pauses_self_pump) if mode is not None else False,
            has_expiry=override.until is not None,
            set_at=override.set_at,
        )
        message = stale_override_finding(posture, now=now, colleague_facing_loops=colleague_facing)
    except Exception as exc:  # noqa: BLE001 — doctor check must never crash the run
        typer.echo(f"WARN  Mode-override check crashed: {exc.__class__.__name__}: {exc}")
        return
    if message:
        typer.echo(message)
