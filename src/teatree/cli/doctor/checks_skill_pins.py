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

from teatree.provisioning.declared import (
    DeclarationUnreadableError,
    pinned_specs_in_apm_manifest,
    project_root_for_running_code,
)
from teatree.provisioning.skill_pin import (
    MEASUREMENT_HORIZON,
    PinAudit,
    default_record_path,
    pin_advisory_lines,
    read_pin_audit,
)

_DAY = dt.timedelta(days=1)
_APM_MANIFEST = "apm.yml"


def _check_skill_pin_freshness(
    *,
    record_path: Path | None = None,
    now: dt.datetime | None = None,
    manifest: Path | None = None,
) -> bool:
    """INFO-suggest a bump for each declared skill pin its source has moved past.

    Silent only when a RECENT measurement found every DECLARED pin at its source's
    head — the one state that has actually been verified. Absent, aged, unmeasurable,
    or simply never measured each report themselves. Crash-proof and always ``True``:
    an advisory may never redden a doctor run.

    *manifest* is the declaration surface the recorded measurement is checked for
    COVERAGE against, defaulting to the running code's own ``apm.yml``; it is a
    parameter for the same reason *now* and *record_path* are — so a test states its
    whole world instead of asserting against whatever this checkout happens to mandate.
    """
    try:
        path = default_record_path() if record_path is None else record_path
        moment = dt.datetime.now(tz=dt.UTC) if now is None else now
        for line in _freshness_lines(path, moment, manifest):
            typer.echo(line)
    except Exception as exc:  # noqa: BLE001 — a doctor check must never crash the run
        typer.echo(f"WARN  Skill-pin freshness check crashed ({exc.__class__.__name__}: {exc}) — UNVERIFIED.")
    return True


def _freshness_lines(path: Path, now: dt.datetime, manifest: Path | None) -> list[str]:
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
    return lines + _unmeasured_pin_lines(audit, manifest) + pin_advisory_lines(audit.statuses)


def _unmeasured_pin_lines(audit: PinAudit, manifest: Path | None) -> list[str]:
    """One WARN per pin the manifest DECLARES that the recorded measurement never covered.

    The coverage half of the question, and the half whose absence made this check's
    silence a lie. :func:`pin_advisory_lines` speaks only about pins that were MEASURED,
    so a declared pin the measurement never reached produced no line at all — and a
    doctor that prints nothing is read as "every pin is current". Two ways a pin goes
    unmeasured, and neither said anything: a whole-repo bundle spec
    (``obra/superpowers#<sha>`` — this manifest's only third-party pin), which the skill
    enumeration drops because it names no single installable skill, and any pin added to
    ``apm.yml`` since the last ``t3 setup`` recorded a measurement.

    Reads the manifest, which is a local file, so the check stays offline. Only the
    COMPARISON against a source needs the network, and that still lives in ``t3 setup``.
    """
    surface = manifest if manifest is not None else _running_code_manifest()
    if surface is None:
        return [
            (
                "WARN  Skill-pin coverage is UNVERIFIED: no apm.yml was found above the running code, so which "
                "pins are declared is UNKNOWN and the recorded measurement cannot be shown to cover them."
            )
        ]
    try:
        declared = pinned_specs_in_apm_manifest(surface)
    except DeclarationUnreadableError as exc:
        return [
            (
                f"WARN  Skill-pin coverage is UNVERIFIED: {exc}. The declared pins could not be enumerated, "
                "so whether the recorded measurement covers them is UNKNOWN."
            )
        ]
    measured = {status.spec for status in audit.statuses}
    return [
        f"WARN  Skill pin {spec} is UNVERIFIED: {_APM_MANIFEST} declares it but the recorded measurement "
        f"never covered it, so whether it trails its source is UNKNOWN — which is not the same answer as "
        f"current. Run `t3 setup` to measure it."
        for spec in declared
        if spec not in measured
    ]


def _running_code_manifest() -> Path | None:
    root = project_root_for_running_code()
    return None if root is None else root / _APM_MANIFEST
