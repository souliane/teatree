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

from collections.abc import Sequence
from pathlib import Path

import typer

from teatree.provisioning.declared import DeclaredDependency, declared_dependencies, project_root_for_running_code
from teatree.provisioning.probes import BinaryResolver, unprovisioned


def _render(gap: DeclaredDependency) -> str:
    return (
        f"FAIL  Declared dependency not provisioned: {gap.kind} {gap.name!r} "
        f"(declared in {gap.declared_in}) — the configuration mandates it but nothing installed it, "
        f"so anything depending on it silently does nothing. Fix: {gap.remediation}."
    )


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
    return not gaps
