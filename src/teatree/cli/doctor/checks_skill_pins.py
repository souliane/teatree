"""The pin-freshness advisory: is the PIN itself behind the source it names.

The natural neighbour is :mod:`teatree.cli.doctor.checks_skill_supply`, whose
drift gate asks the other staleness question — but that one is deliberately
offline (``git ls-tree`` over a local clone, no fetch), and doctor is the fast
offline lane. Comparing a pin against a source's current head cannot be done
without the network, so putting the measurement here would trade doctor's whole
shape for one advisory.

So the work is split at the seam that costs nothing: ``t3 setup``
(:class:`teatree.cli.setup.skill_pin_audit.SkillPinAuditor`) takes the networked
measurement and records it, and this check reads the record. That leaves one
hazard, which is the reason the record carries a timestamp — a recorded verdict
ages, and an aged verdict presented as today's answer is exactly the silent-pass
this family of checks exists to remove. Past the freshness horizon the finding
becomes UNVERIFIED again rather than staying green.

Nothing here gates. A pin may be held on purpose, so a trailing pin is INFO and
the check's return value is always ``True``.
"""

import datetime as dt
from pathlib import Path

import typer

from teatree.provisioning.skill_pin import MEASUREMENT_HORIZON, default_record_path, pin_advisory_lines, read_pin_audit

_DAY = dt.timedelta(days=1)


def _check_skill_pin_freshness(*, record_path: Path | None = None, now: dt.datetime | None = None) -> bool:
    """INFO-suggest a bump for each declared skill pin its source has moved past.

    Silent only when a RECENT measurement found every pin at its source's head —
    the one state that has actually been verified. Absent, aged, or unmeasurable
    each report themselves. Crash-proof and always ``True``: an advisory may
    never redden a doctor run.
    """
    try:
        path = default_record_path() if record_path is None else record_path
        for line in _freshness_lines(path, dt.datetime.now(tz=dt.UTC) if now is None else now):
            typer.echo(line)
    except Exception as exc:  # noqa: BLE001 — a doctor check must never crash the run
        typer.echo(f"WARN  Skill-pin freshness check crashed ({exc.__class__.__name__}: {exc}) — UNVERIFIED.")
    return True


def _freshness_lines(path: Path, now: dt.datetime) -> list[str]:
    """Everything the recorded measurement at *path* has to say as of *now*."""
    audit = read_pin_audit(path)
    if audit is None:
        return [
            (
                f"WARN  Skill-pin freshness is UNVERIFIED: no measurement is recorded at {path}. Whether the "
                f"declared skill pins have fallen behind their sources is UNKNOWN — run `t3 setup` to measure it."
            )
        ]
    lines: list[str] = []
    if not audit.is_fresh(now=now):
        days = audit.age(now=now) // _DAY
        lines.append(
            f"WARN  Skill-pin freshness is UNVERIFIED: the recorded measurement is {days} days old "
            f"(taken {audit.measured_at.date().isoformat()}), past the {MEASUREMENT_HORIZON.days}-day horizon, "
            f"so it is evidence about then and not about now — re-run `t3 setup` to re-measure."
        )
    return lines + pin_advisory_lines(audit.statuses)
