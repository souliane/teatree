"""A REQUIRED-tier ``requires:`` that resolves to nothing is a FAIL, not a warning (#4677).

The transitive resolver passes an unresolvable ``requires:`` through so the ``Skill``
tool still loads it, logging a warning nobody reads. That is correct for a genuinely
OPTIONAL external skill and wrong for one the manifest MANDATES: three teatree skills
delegate their methodology to ``obra/superpowers`` — ``skills/code`` to
``test-driven-development``, ``skills/architecture-design`` to ``writing-plans``,
``skills/debug`` to ``systematic-debugging`` — and while the bundle had no installer
every coding and planning dispatch resolved all three to nothing and proceeded
believing it was compliant.

The distinction is the whole check, so it comes from the declaration surface rather
than a list kept here: a name published by a REQUIRED-tier entry (a single-skill spec,
or a skill inside a fetched bundle's checkout) is mandated and FAILs; anything else
WARNs exactly as before.
"""

from collections.abc import Sequence
from pathlib import Path

import typer

from teatree.provisioning.declared import (
    DeclarationUnreadableError,
    bundles_declared_in_apm_manifest,
    project_root_for_running_code,
    skills_declared_in_apm_manifest,
)
from teatree.provisioning.probes import skill_is_provisioned
from teatree.provisioning.skill_bundle import parse_bundle_source, published_bundle_skills
from teatree.skill_support.requires_parser import parse_requires

_SKILL_FILE = "SKILL.md"
_SKILLS_DIR = "skills"


def _check_required_tier_requires_resolve(
    *,
    project_root: Path | None = None,
    cache_root: Path | None = None,
    search_dirs: Sequence[Path] | None = None,
) -> bool:
    """FAIL when a skill's ``requires:`` names a MANDATED skill nothing installed.

    Crash-proof and silent when every edge resolves. Returns ``True`` for an
    unresolvable but UNMANDATED requires — that one only WARNs, because an optional
    external methodology skill legitimately is not on this box.
    """
    try:
        root = project_root_for_running_code() if project_root is None else project_root
        if root is None:
            typer.echo(
                "WARN  Skill `requires:` resolution is UNVERIFIED: no teatree project root resolved, so "
                "which delegations are mandated is UNKNOWN."
            )
            return True
        lines, ok = _requires_report(root, _cache_root(cache_root), _search_dirs(search_dirs))
        for line in lines:
            typer.echo(line)
    except Exception as exc:  # noqa: BLE001 — a doctor check must never crash the run
        typer.echo(f"WARN  Skill `requires:` resolution check crashed ({exc.__class__.__name__}: {exc}) — UNVERIFIED.")
        return True
    return ok


def _cache_root(cache_root: Path | None) -> Path:
    if cache_root is not None:
        return cache_root
    from teatree.paths import get_data_dir  # noqa: PLC0415 — deferred: keeps the import graph off teatree.paths

    return get_data_dir("skill-sources")


def _search_dirs(search_dirs: Sequence[Path] | None) -> Sequence[Path]:
    if search_dirs is not None:
        return search_dirs
    from teatree.skill_support.ref_validator import default_search_dirs  # noqa: PLC0415 — deferred: lazy CLI import

    return default_search_dirs()


def _mandated_names(root: Path, cache_root: Path) -> dict[str, str]:
    """Skill name → the declaration that mandates it, across both REQUIRED-tier shapes."""
    manifest = root / "apm.yml"
    try:
        skills = skills_declared_in_apm_manifest(manifest)
        bundles = bundles_declared_in_apm_manifest(manifest)
    except DeclarationUnreadableError:
        return {}
    mandated = {dependency.name: dependency.source for dependency in skills}
    for bundle in bundles:
        source = parse_bundle_source(bundle.source)
        if source is None:
            continue
        for name in published_bundle_skills(cache_root / source.cache_name):
            mandated.setdefault(name, bundle.source)
    return mandated


def _requires_report(root: Path, cache_root: Path, search_dirs: Sequence[Path]) -> tuple[list[str], bool]:
    """Every unresolvable ``requires:`` edge, split by whether its source is mandated."""
    in_repo = {path.parent.name for path in root.glob(f"{_SKILLS_DIR}/*/{_SKILL_FILE}")}
    mandated = _mandated_names(root, cache_root)
    lines: list[str] = []
    ok = True
    for skill_md in sorted(root.glob(f"{_SKILLS_DIR}/*/{_SKILL_FILE}")):
        declaring = skill_md.parent.name
        for required in parse_requires(skill_md.read_text(encoding="utf-8")) or []:
            if required in in_repo or skill_is_provisioned(required, search_dirs):
                continue
            if required in mandated:
                ok = False
                lines.append(_fail_line(declaring, required, mandated[required]))
            else:
                lines.append(_warn_line(declaring, required))
    return lines, ok


def _fail_line(declaring: str, required: str, source: str) -> str:
    return (
        f"FAIL  Skill {declaring!r} requires {required!r}, which resolves to no SKILL.md — but "
        f"`{source}` mandates it in the REQUIRED tier. The delegation loads nothing and the phase "
        f"proceeds believing it is compliant. Fix: run `t3 setup` to provision it."
    )


def _warn_line(declaring: str, required: str) -> str:
    return (
        f"WARN  Skill {declaring!r} requires {required!r}, which resolves to no SKILL.md and is not "
        f"mandated by any declaration — the delegation loads nothing. Declare it in apm.yml if it is "
        f"meant to be present."
    )
