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

What a trailing pin MEANS depends on its trust class (#4677). A THIRD-PARTY pin
may be held on purpose, so it stays an INFO that gates nothing. A FIRST-PARTY one
— the owner's own repo, which nobody chose to trail — is drift: it WARNs, and once
it has trailed past ``skill_pin_stale_fail_days`` it FAILs. An age the record never
carried is UNKNOWN, and unknown never gates.
"""

import datetime as dt
from pathlib import Path

import typer

from teatree.provisioning.declared import (
    DeclarationUnreadableError,
    pinned_specs_in_apm_manifest,
    project_root_for_running_code,
    skill_bump_remediation,
)
from teatree.provisioning.skill_pin import (
    MEASUREMENT_HORIZON,
    PinAudit,
    SkillPinStatus,
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
    stale_fail_days: int | None = None,
) -> bool:
    """Report each declared skill pin its source has moved past, per trust class.

    Silent only when a RECENT measurement found every DECLARED pin at its source's
    head — the one state that has actually been verified. Absent, aged, unmeasurable,
    or simply never measured each report themselves. Crash-proof: a check that could
    not run reports UNVERIFIED and passes, because a crash proves nothing about a pin.

    Returns ``False`` only for a FIRST-PARTY pin that has trailed its source past
    *stale_fail_days*. Every other finding here is advisory.

    *manifest* is the declaration surface the recorded measurement is checked for
    COVERAGE against, defaulting to the running code's own ``apm.yml``; it is a
    parameter for the same reason *now* and *record_path* are — so a test states its
    whole world instead of asserting against whatever this checkout happens to mandate.
    """
    try:
        path = default_record_path() if record_path is None else record_path
        moment = dt.datetime.now(tz=dt.UTC) if now is None else now
        threshold = _stale_fail_days() if stale_fail_days is None else stale_fail_days
        lines, ok = _freshness_report(path, moment, manifest, threshold)
        for line in lines:
            typer.echo(line)
    except Exception as exc:  # noqa: BLE001 — a doctor check must never crash the run
        typer.echo(f"WARN  Skill-pin freshness check crashed ({exc.__class__.__name__}: {exc}) — UNVERIFIED.")
        return True
    return ok


def _stale_fail_days() -> int:
    from teatree.config import load_config  # noqa: PLC0415 — deferred: keeps CLI startup light

    return load_config().user.skill_pin_stale_fail_days


def _freshness_report(
    path: Path,
    now: dt.datetime,
    manifest: Path | None,
    stale_fail_days: int,
) -> tuple[list[str], bool]:
    """Everything the recorded measurement at *path* says as of *now*, and whether it passes."""
    audit = read_pin_audit(path)
    if audit is None:
        return (
            [
                (
                    f"WARN  Skill-pin freshness is UNVERIFIED: no measurement is recorded at {path}. Whether the "
                    f"declared skill pins have fallen behind their sources is UNKNOWN — run `t3 setup` to measure it."
                )
            ],
            True,
        )
    lines: list[str] = []
    if not audit.is_fresh(now=now):
        days = audit.age(now=now) // _DAY
        lines.append(
            f"WARN  Skill-pin freshness is UNVERIFIED: the recorded measurement is {days} days old "
            f"(taken {audit.measured_at.date().isoformat()}), past the {MEASUREMENT_HORIZON.days}-day horizon, "
            f"so it is evidence about then and not about now — re-run `t3 setup` to re-measure."
        )
    lines += _unmeasured_pin_lines(audit, manifest)
    stale = _stale_first_party_lines(audit, now, stale_fail_days)
    advisories = [
        line
        for status, line in _advisory_by_status(audit)
        if not (status.first_party and _is_stale(audit, status, now, stale_fail_days))
    ]
    return lines + advisories + stale, not stale


def _advisory_by_status(audit: PinAudit) -> list[tuple[SkillPinStatus, str]]:
    """Each status paired with the line it produces, so one can be swapped for a FAIL."""
    return [(status, line) for status in audit.statuses for line in pin_advisory_lines([status])]


def _is_stale(audit: PinAudit, status: SkillPinStatus, now: dt.datetime, stale_fail_days: int) -> bool:
    days = audit.days_behind(status.spec, now=now)
    return status.is_behind and days is not None and days >= stale_fail_days


def _stale_first_party_lines(audit: PinAudit, now: dt.datetime, stale_fail_days: int) -> list[str]:
    """One FAIL per first-party pin that has trailed its source past the threshold."""
    return [
        (
            f"FAIL  Skill pin {status.name!r} has trailed its FIRST-PARTY source for "
            f"{audit.days_behind(status.spec, now=now)} days (threshold {stale_fail_days}): "
            f"{status.spec} against {status.source_repo}. Every consumer has been reading the pre-drift "
            f"version that whole time. Fix: {skill_bump_remediation(status.bumped_spec)}."
        )
        for status in audit.statuses
        if status.first_party and _is_stale(audit, status, now, stale_fail_days)
    ]


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
