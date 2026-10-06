"""The general provisioning gate: declared REQUIRED but not provisioned → FAIL (#3652).

Epic #3445's stated line between done and 90%-done. The two predecessors
(:func:`_check_configured_review_skills`, :func:`_check_pyright_lsp_plugin`)
each cover ONE named dependency, so anything mandated later goes unchecked —
which is how the mandated companion skills shipped absent from the published
image with ``t3 doctor`` reporting nothing at all.

This check enumerates from :mod:`teatree.provisioning.declared` (the manifest,
the pyproject table, the enabled-plugin settings), so declaring a new mandate is
enough to have it gated. Silence is not a possible outcome: a gap is a FAIL that
names the dependency, where it is declared, and the exact remediation; a
declaration surface that cannot be read is a WARN that says so.
"""

import re
from collections.abc import Sequence
from pathlib import Path

import typer

from teatree.provisioning.declared import (
    DeclaredDependency,
    declared_dependencies,
    project_root_for_running_code,
    skill_bump_remediation,
)
from teatree.provisioning.probes import BinaryResolver, unprovisioned
from teatree.skill_support.index import harness_skills_dirs, install_roots, locate_skill_md
from teatree.skill_support.pin_shadow import SkillShadowsDeclaredPinError
from teatree.utils import git_run
from teatree.utils.git_remote import slug_from_remote

_FULL_SHA = re.compile(r"[0-9a-f]{40}")


def _render(gap: DeclaredDependency) -> str:
    return (
        f"FAIL  Declared dependency not provisioned: {gap.kind} {gap.name!r} "
        f"(declared in {gap.declared_in}) — the configuration mandates it but nothing installed it, "
        f"so anything depending on it silently does nothing. Fix: {gap.remediation}."
    )


def _unpinned_spec_finding(dependency: DeclaredDependency) -> str:
    path, _, ref = dependency.source.partition("#")
    if _FULL_SHA.fullmatch(ref.strip().lower()):
        return ""
    return (
        f"FAIL  Declared skill {dependency.name!r}: `{dependency.source}` names no 40-hex commit, so what "
        f"`t3 setup` installs moves with its source. Fix: {skill_bump_remediation(f'{path}#<40-hex commit sha>')}."
    )


def _declared_repo(spec: str) -> str:
    return "/".join(spec.split("#", 1)[0].split("/")[:2])


def _symlinked_checkout_slug(skill_dir: Path, *, home: Path) -> str:
    """The ``owner/repo`` of the checkout an install-root symlink points into, or ``""``."""
    if not skill_dir.is_symlink():
        return ""
    top = git_run.run(repo=str(skill_dir.resolve()), args=["rev-parse", "--show-toplevel"])
    if not top or Path(top) == home:
        return ""
    return slug_from_remote(git_run.run(repo=top, args=["remote", "get-url", "origin"]))


def _declared_skill_source_findings(
    dependencies: Sequence[DeclaredDependency], *, roots: Sequence[Path], home: Path
) -> list[str]:
    """FAIL lines for a declared skill pinned to no commit, or satisfied from anything but its declared source."""
    installs = install_roots()
    findings: list[str] = []
    for dependency in dependencies:
        if dependency.kind != "skill":
            continue
        if unpinned := _unpinned_spec_finding(dependency):
            findings.append(unpinned)
        located = locate_skill_md(dependency.name, roots)
        if located is None:
            continue
        root, skill_md = located
        if root not in installs:
            findings.append(f"FAIL  {SkillShadowsDeclaredPinError(dependency.name, dependency.source, skill_md)}")
            continue
        slug = _symlinked_checkout_slug(skill_md.parent, home=home)
        if slug and slug != _declared_repo(dependency.source):
            findings.append(
                f"FAIL  Declared skill {dependency.name!r} resolves to {skill_md.parent} → "
                f"{skill_md.parent.resolve()}, a checkout of {slug}, not the declared `{dependency.source}`. "
                f"Fix: {dependency.remediation}."
            )
    return findings


def _default_search_dirs() -> list[Path]:
    from teatree.skill_support.ref_validator import default_search_dirs  # noqa: PLC0415 — deferred: lazy CLI import

    return default_search_dirs()


def _check_declared_dependencies_provisioned(
    *,
    project_root: Path | None = None,
    home: Path | None = None,
    search_dirs: Sequence[Path] | None = None,
    which: BinaryResolver | None = None,
) -> bool:
    """FAIL when any configuration-declared dependency is not actually provisioned.

    Returns ``True`` when every declared dependency resolves, and when the
    declaration surfaces themselves cannot be read — an unreadable manifest is a
    loud WARN naming the surface, not a gate failure, because a non-source
    install legitimately has no manifest to read.
    """
    root = project_root_for_running_code() if project_root is None else project_root
    if root is None:
        typer.echo(
            "WARN  Provisioning gate: no teatree project root resolved, so the declared "
            "dependencies could not be enumerated — mandated skills/binaries/integrations are UNVERIFIED."
        )
        return True
    enumeration = declared_dependencies(project_root=root, home=Path.home() if home is None else home)
    for reason in enumeration.unreadable:
        typer.echo(f"WARN  Provisioning gate: {reason} — that surface's mandates are UNVERIFIED in this install.")

    gaps = unprovisioned(
        enumeration.dependencies,
        search_dirs=_default_search_dirs() if search_dirs is None else search_dirs,
        home=Path.home() if home is None else home,
        which=which,
    )
    for gap in gaps:
        typer.echo(_render(gap))
    sources = _declared_skill_source_findings(
        enumeration.dependencies,
        roots=harness_skills_dirs() if search_dirs is None else search_dirs,
        home=Path.home() if home is None else home,
    )
    for finding in sources:
        typer.echo(finding)
    return not gaps and not sources
