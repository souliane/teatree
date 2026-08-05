"""Measure the declared skill pins against their sources during ``t3 setup``.

The comparison needs the network, and ``t3 setup`` is where the network is
already expected — it clones, installs and registers. So setup is where the
measurement is TAKEN, and it does two things with it: suggests the bump to the
operator standing there, and records the verdict so ``t3 doctor check`` can
report it later without a round trip of its own.

Its sibling :class:`teatree.cli.setup.mandated_skills.MandatedSkillProvisioner`
covers the absence of a mandated skill; this covers the age of the pin that
mandates it. Neither can fail a setup run: a skill nobody could install is the
doctor gate's business, and a pin held deliberately is nobody's business at all.
"""

import datetime as dt
from collections.abc import Callable
from pathlib import Path

from teatree.provisioning.declared import DeclarationUnreadableError, skills_declared_in_apm_manifest
from teatree.provisioning.skill_pin import (
    DEFAULT_REMOTE_BASE,
    PinAudit,
    measure_skill_pins,
    pin_advisory_lines,
    write_pin_audit,
)

type Echo = Callable[[str], None]

_APM_MANIFEST = "apm.yml"


class SkillPinAuditor:
    """Compare each declared skill pin with its source, then suggest and record."""

    def __init__(self, repo: Path, record_path: Path, *, remote_base: str = DEFAULT_REMOTE_BASE) -> None:
        self.repo = repo
        self.record_path = record_path
        self.remote_base = remote_base

    def audit(self, echo: Echo) -> None:
        """Measure, report, and record. Never raises, never gates the setup run.

        An unreadable declaration surface ends the pass with its own finding
        rather than an empty record: nothing was measured, so nothing may be
        recorded as measured, and a later doctor run must see "never taken"
        instead of "every pin was current".
        """
        try:
            declared = skills_declared_in_apm_manifest(self.repo / _APM_MANIFEST)
        except DeclarationUnreadableError as exc:
            echo(
                f"WARN  Skill-pin freshness is UNVERIFIED: {exc}. The declared pins could not be "
                f"enumerated, so whether any has fallen behind its source is UNKNOWN."
            )
            return

        statuses = measure_skill_pins(declared, remote_base=self.remote_base)
        for line in pin_advisory_lines(statuses):
            echo(line)
        audit = PinAudit(measured_at=dt.datetime.now(tz=dt.UTC), statuses=tuple(statuses))
        if not write_pin_audit(audit, self.record_path):
            echo(
                f"WARN  Skill-pin measurement could not be recorded at {self.record_path} — "
                f"`t3 doctor check` will report pin freshness as UNVERIFIED until a setup run records one."
            )
