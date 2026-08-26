"""The skill-supply gates: what the lifecycle dispatches must exist, and be current.

Two failures share one cause — an installed skill is a physical copy of a source
tree, and a copy cannot announce that it went out of date or was never made.

*   :func:`_check_dispatched_overlay_skills` covers ABSENCE at the dispatch site.
    An overlay's ``stage_skills`` / ``companion_skills`` / ``pr_review_companion``
    name skills the lifecycle loads on every ticket that reaches the phase. When
    one resolves to no ``SKILL.md`` the stage loads nothing and says nothing: the
    loader logs a warning nobody reads and the phase proceeds, so the work simply
    happens without the discipline the skill carried. Enumeration comes from the
    overlay config itself — the surface that DECLARES the dispatch — so a skill
    wired in later is gated with no change here. (:mod:`teatree.provisioning.declared`
    covers the same class for ``apm.yml`` / ``pyproject.toml`` / plugin settings;
    an overlay's dispatch map is a fourth declaration surface those readers never see.)
*   :func:`_check_skill_source_drift` covers STALENESS. A merged fix reaches a box
    only when somebody re-installs, and until then nothing anywhere reports the
    gap — the incident this gate exists for is a skill whose instructions had gone
    wrong, fixed upstream, still being followed everywhere.

Both hard-FAIL and name the exact remediation; neither can report silence. A
comparison that cannot be made (no source clone on this box) is a WARN saying so,
never a pass — "it matches" and "I could not check" are different answers.
"""

from pathlib import Path

import typer

from teatree.core.skill_sources import declared_skill_sources
from teatree.provisioning.skill_drift import SkillDrift, measure_skill_drift

_MAX_NAMED = 8


def _search_dirs() -> list[Path]:
    from teatree.skill_support.ref_validator import default_search_dirs  # noqa: PLC0415 — deferred: lazy CLI import

    return default_search_dirs()


def _named(skills: tuple[str, ...]) -> str:
    """The finding's evidence: every name when few, a capped sample when many."""
    if len(skills) <= _MAX_NAMED:
        return ", ".join(skills)
    return f"{', '.join(skills[:_MAX_NAMED])} (+{len(skills) - _MAX_NAMED} more)"


def _dispatched_skills_by_overlay() -> dict[str, list[tuple[str, str]]]:
    """Overlay name → the ``(declaring field, skill name)`` pairs it dispatches."""
    from teatree.core.overlay_loader import get_all_overlays  # noqa: PLC0415 — deferred: keeps CLI startup light

    dispatched: dict[str, list[tuple[str, str]]] = {}
    for overlay_name, overlay in get_all_overlays().items():
        config = getattr(overlay, "config", None)
        if config is None:
            continue
        declared: list[tuple[str, str]] = [
            (f"stage_skills[{phase}]", skill)
            for phase, skills in getattr(config, "stage_skills", {}).items()
            for skill in skills
        ]
        declared.extend(("companion_skills", skill) for skill in getattr(config, "companion_skills", []))
        companion = getattr(config, "pr_review_companion", "")
        if companion:
            declared.append(("pr_review_companion", companion))
        dispatched[overlay_name] = declared
    return dispatched


def _dispatched_skill_gaps() -> list[str]:
    """A FAIL line per dispatched skill that resolves to no installed ``SKILL.md``."""
    from teatree.skill_support.ref_validator import (  # noqa: PLC0415 — deferred: keeps CLI startup light
        canonical_skill_names,
        resolves_to_canonical,
    )

    canonical = canonical_skill_names(_search_dirs())
    gaps: list[str] = []
    for overlay_name, declared in sorted(_dispatched_skills_by_overlay().items()):
        for field, skill in declared:
            if resolves_to_canonical(skill, canonical):
                continue
            gaps.append(
                f"FAIL  Overlay {overlay_name} dispatches {skill!r} via {field}, and it resolves to no "
                f"installed skill — the stage loads nothing and continues, so the discipline it carries "
                f"is silently skipped. Fix: install the skill (re-run the team skill install, or "
                f"`t3 setup`), then re-run `t3 doctor check`."
            )
    return gaps


def _drift_lines(drift: SkillDrift) -> list[str]:
    """The doctor lines one source's verdict produces — empty when it is clean."""
    lines: list[str] = []
    if drift.unmeasurable:
        lines.append(
            f"WARN  Skill-source drift for {drift.label} is UNVERIFIED: {drift.unmeasurable}. "
            f"Installed skills on this box cannot be compared against their reviewed source."
        )
        return lines
    if drift.stale:
        lines.append(
            f"FAIL  {len(drift.stale)} installed skill(s) differ from {drift.label} at {drift.ref}: "
            f"{_named(drift.stale)}. Installs are physical copies, so a merged fix reaches this box only "
            f"on re-install — until then every agent runs the old instructions. Fix: re-run the skill "
            f"install for this source (`/skills-update` on a Claude box), then re-run `t3 doctor check`."
        )
    if drift.absent:
        lines.append(
            f"FAIL  {len(drift.absent)} skill(s) published by {drift.label} at {drift.ref} are not installed "
            f"here: {_named(drift.absent)}. Anything dispatching one of them loads nothing. "
            f"Fix: re-run the skill install for this source, then re-run `t3 doctor check`."
        )
    return lines


def _check_dispatched_overlay_skills() -> bool:
    """FAIL when an overlay dispatches a skill that is not installed on this box.

    Crash-proof: any enumeration error degrades to a WARN so a doctor run never
    aborts here, and the WARN says the surface went unverified rather than
    implying it passed.
    """
    try:
        gaps = _dispatched_skill_gaps()
    except Exception as exc:  # noqa: BLE001 — a doctor check must never crash the run
        typer.echo(f"WARN  Dispatched-skill check crashed ({exc.__class__.__name__}: {exc}) — UNVERIFIED.")
        return True
    for gap in gaps:
        typer.echo(gap)
    return not gaps


def _check_skill_source_drift() -> bool:
    """FAIL when installed skills no longer match the reviewed source they came from.

    Overlays that declare no skill source are inert here: nothing to compare is a
    genuine empty, unlike a declared source whose clone is missing (a WARN naming
    the reason). Crash-proof for the same reason as its sibling above.
    """
    try:
        search_dirs = _search_dirs()
        lines = [
            line
            for clone in declared_skill_sources()
            for line in _drift_lines(measure_skill_drift(clone, search_dirs=search_dirs))
        ]
    except Exception as exc:  # noqa: BLE001 — a doctor check must never crash the run
        typer.echo(f"WARN  Skill-source drift check crashed ({exc.__class__.__name__}: {exc}) — UNVERIFIED.")
        return True
    for line in lines:
        typer.echo(line)
    return not any(line.startswith("FAIL") for line in lines)
