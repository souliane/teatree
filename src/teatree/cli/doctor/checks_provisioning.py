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
declaration surface that cannot be read is a WARN that says so, except an
``apm.yml`` that exists in the wrong shape, which every dispatch parks on: a FAIL.
"""

import os
from collections.abc import Sequence
from pathlib import Path

import typer

from teatree.provisioning.declared import (
    DeclarationUnreadableError,
    DeclaredDependency,
    declared_dependencies,
    project_root_for_running_code,
    skill_bump_remediation,
    unpinned_apm_entries,
)
from teatree.provisioning.probes import BinaryResolver, unprovisioned
from teatree.provisioning.skill_source import owner_repo, pinned_commit
from teatree.provisioning.skills_lock import lock_path, read_install_refs
from teatree.skill_support.index import harness_skills_dirs, install_roots, locate_skill_md
from teatree.skill_support.pin_shadow import SkillPinsUnreadableError, SkillShadowsDeclaredPinError


def _render(gap: DeclaredDependency) -> str:
    return (
        f"FAIL  Declared dependency not provisioned: {gap.kind} {gap.name!r} "
        f"(declared in {gap.declared_in}) — the configuration mandates it but nothing installed it, "
        f"so anything depending on it silently does nothing. Fix: {gap.remediation}."
    )


def _why_not_installer_managed(entry: Path, *, home: Path) -> str:
    """Why *entry* is not a copy an installer wrote under an install root, or ``""``."""
    real = entry.resolve()
    if not any(real.is_relative_to(root.resolve()) for root in install_roots()):
        return f"resolves to {real}, outside every install root"
    for folder in (real, *real.parents):
        if folder == home.resolve():
            break
        if (folder / ".git").exists():
            return f"resolves to {real}, inside the git checkout {folder}"
    for walked, directories, files in os.walk(real):
        for name in (*directories, *files):
            link = Path(walked, name)
            if link.is_symlink() and not link.resolve().is_relative_to(real):
                return f"holds {link}, a link to {link.resolve()}, outside the skill directory"
    return ""


def _record_mismatch(dependency: DeclaredDependency, refs: dict[str, tuple[str, str]], lock: Path) -> str:
    pin = pinned_commit(dependency.source)
    recorded = refs.get(dependency.name)
    if not pin or recorded == (owner_repo(dependency.source).lower(), pin):
        return ""
    held = f"holds no entry for {dependency.name!r}"
    if recorded is not None:
        held = f"names {recorded[0]}@{recorded[1] or 'no ref'}"
    return f"the install record at {lock} {held}; it proves the requested ref, not the installed commit"


def _declared_skill_source_findings(
    dependencies: Sequence[DeclaredDependency], *, roots: Sequence[Path], home: Path
) -> list[str]:
    """FAIL lines for a declared skill satisfied from anything but an installer-managed copy at its pinned commit."""
    installs = install_roots()
    refs = read_install_refs(home)
    if refs is None:
        typer.echo(
            f"WARN  Skill install record is UNVERIFIED: no readable schema-3 record at {lock_path(home)}, so which "
            f"ref each declared skill was installed from is UNKNOWN."
        )
    findings: list[str] = []
    for dependency in dependencies:
        located = locate_skill_md(dependency.name, roots) if dependency.kind == "skill" else None
        if located is None:
            continue
        root, skill_md = located
        if root not in installs:
            findings.append(f"FAIL  {SkillShadowsDeclaredPinError(dependency.name, dependency.source, skill_md)}")
            continue
        findings.extend(
            f"FAIL  Declared skill {dependency.name!r} at {entry} {why}, not the installed `{dependency.source}`. "
            f"Fix: remove the link, then {dependency.remediation}."
            for install in installs
            if (entry := install / dependency.name).exists() and (why := _why_not_installer_managed(entry, home=home))
        )
        if refs is not None and (mismatch := _record_mismatch(dependency, refs, lock_path(home))):
            findings.append(
                f"FAIL  Declared skill {dependency.name!r} is pinned at `{dependency.source}`, but {mismatch}. "
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
    install legitimately has no manifest to read. A malformed ``apm.yml`` fails.
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
    try:
        unpinned = unpinned_apm_entries(root / "apm.yml")
    except DeclarationUnreadableError:
        unpinned = []  # already reported once by the enumeration above
    findings = [f"FAIL  {SkillPinsUnreadableError(root / 'apm.yml', reason)}" for reason in enumeration.malformed]
    findings += [
        f"FAIL  Declared apm entry `{spec}` names no 40-hex commit, so what `t3 setup` installs moves. "
        f"Fix: {skill_bump_remediation(spec.partition('#')[0] + '#<40-hex commit sha>')}."
        for spec in unpinned
    ] + _declared_skill_source_findings(
        enumeration.dependencies,
        roots=harness_skills_dirs() if search_dirs is None else search_dirs,
        home=Path.home() if home is None else home,
    )
    for finding in findings:
        typer.echo(finding)
    return not gaps and not findings
