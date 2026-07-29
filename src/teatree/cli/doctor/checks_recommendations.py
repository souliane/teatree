"""INFO advisories for OPTIONAL, provider-specific RECOMMENDED skills (#3668).

Distinct from :mod:`teatree.cli.doctor.checks_provisioning`, whose gate FAILs on a
MANDATED dependency declared but absent. A recommendation is never mandated: its
absence is a surfacing-only INFO (never WARN/FAIL, never gates the exit code). The
vendor architecture skill is Anthropic-specific, so it is OFFERED — not installed —
with its provider caveat, so an operator on another provider is never handed a
Claude-only skill by default.
"""

from collections.abc import Sequence
from pathlib import Path

import typer

from teatree.provisioning.recommended import RecommendedSkill, unprovisioned_recommendations


def _default_search_dirs() -> list[Path]:
    from teatree.skill_support.ref_validator import default_search_dirs  # noqa: PLC0415 — deferred: lazy CLI import

    return default_search_dirs()


def _render(rec: RecommendedSkill) -> str:
    return (
        f"INFO  Recommended OPTIONAL skill {rec.name!r} is not installed. {rec.rationale} "
        f"Caveat: {rec.caveat} Install: `{rec.install_hint}`. Its absence gates nothing."
    )


def _check_recommended_skills(*, search_dirs: Sequence[Path] | None = None) -> bool:
    """INFO-suggest each OPTIONAL recommended skill that is absent; never gates (#3668).

    Silent when every recommendation is already loadable. Crash-proof: any read
    error degrades to a silent pass — an optional advisory must never redden the
    doctor run. Always returns ``True`` (its findings gate nothing).
    """
    try:
        dirs = _default_search_dirs() if search_dirs is None else search_dirs
        for rec in unprovisioned_recommendations(dirs):
            typer.echo(_render(rec))
    except Exception:  # noqa: BLE001 — an optional advisory must never crash or gate the doctor run
        return True
    return True
