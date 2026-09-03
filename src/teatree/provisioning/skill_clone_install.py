"""Install the skills a declared source clone publishes.

An overlay declares two things about the skills it dispatches, and until now they
never met. ``stage_skills`` / ``companion_skills`` / ``pr_review_companion`` name
what every ticket loads; ``skill_source_clones`` names where those skills are
published. The first was gated, the second was measured — and *nothing installed
from it*. So an overlay could gate on skills its own provisioning surface never
supplied, and the failure was silent by construction: an unresolvable dispatch
loads nothing and the phase continues.

This module is the missing edge. It reads the SAME resolution the drift gate
reads (:func:`teatree.provisioning.skill_drift.resolve_published_skills`), so
"install it" and "it is installed" can never disagree about which clone, which
ref, or which install names are in play.

Two properties worth stating, because both were failure modes on the way here:

*   **The reviewed ref, never the working tree.** A clone sits on whatever branch
    its owner last checked out, so linking straight at it would serve WIP to every
    agent — and would make the drift gate compare a tree against itself. The ref is
    exported into a per-``(source, commit)`` cache directory and the links point
    there, matching what :class:`~teatree.provisioning.skill_source.MandatedSkillInstaller`
    already does for ``apm``-declared sources.
*   **Never displace what is already loadable.** A name the runtime can already
    resolve is left exactly as it is. That keeps the step idempotent for the
    container entrypoint, which runs ``t3 setup`` on every start, and keeps a
    deliberately overridden local skill from being silently replaced.
"""

import logging
import os
import shutil
import tarfile
from dataclasses import dataclass
from operator import itemgetter
from pathlib import Path

from teatree.provisioning.skill_drift import SkillSourceClone, resolve_published_skills
from teatree.utils.run import run_allowed_to_fail

logger = logging.getLogger(__name__)

_SKILL_FILE = "SKILL.md"
_ARCHIVE_TIMEOUT_SECONDS = 120
_STAMP = ".teatree-export-ref"
_PARTIAL_SUFFIX = ".partial."
_MAX_NAMED = 8


@dataclass(frozen=True, slots=True)
class CloneInstall:
    """What one declared source contributed to a runtime skills dir."""

    label: str
    ref: str = ""
    installed: tuple[str, ...] = ()
    already_loadable: tuple[str, ...] = ()
    unavailable: str = ""

    def render(self) -> str:
        """The one line ``t3 setup`` prints for this source."""
        if self.unavailable:
            return f"WARN  Skill source {self.label} not provisioned: {self.unavailable}."
        where = f"Skill source {self.label} at {self.ref}"
        if not self.installed:
            return f"OK    {where}: {len(self.already_loadable)} skill(s) already loadable."
        sample = ", ".join(self.installed[:_MAX_NAMED])
        more = f" (+{len(self.installed) - _MAX_NAMED} more)" if len(self.installed) > _MAX_NAMED else ""
        return (
            f"OK    {where}: installed {len(self.installed)} skill(s) — {sample}{more}; "
            f"{len(self.already_loadable)} already loadable."
        )


def _export_ref(repo: Path, ref: str, destination: Path) -> bool:
    """Materialise *ref* of *repo* under *destination*; ``False`` when it fails.

    Staged INSIDE the destination's own directory, never a system temp dir: the
    final step is a rename, and a data dir on a different filesystem from ``/tmp``
    (every container bind-mount) makes a cross-device rename raise. ``git archive``
    writes a tar rather than a pipe because the archive is binary and the command
    runner decodes stdout as text. The stamp makes re-export a no-op, so the
    entrypoint's every-start ``t3 setup`` costs one ``stat`` after the first run.
    """
    if (destination / _STAMP).is_file():
        return True
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(f"{destination.name}{_PARTIAL_SUFFIX}{os.getpid()}")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        archive = staging / "source.tar"
        result = run_allowed_to_fail(
            ["git", "-C", str(repo), "archive", "--format=tar", "-o", str(archive), ref],
            expected_codes=None,
            timeout=_ARCHIVE_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            logger.warning("Could not export %s from %s: %s", ref, repo, result.stderr.strip())
            return False
        tree = staging / "tree"
        tree.mkdir()
        with tarfile.open(archive) as tar:
            tar.extractall(tree, filter="data")
        archive.unlink()
        # An export that died before stamping leaves a directory that would
        # otherwise be trusted forever. It has no stamp, so it is provably
        # incomplete: replace it rather than reading half a source tree.
        if destination.exists():
            shutil.rmtree(destination)
        tree.rename(destination)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    (destination / _STAMP).write_text(f"{ref}\n", encoding="utf-8")
    return True


def _cache_name(label: str, repo: Path, ref: str) -> str:
    """``<slug>@<commit>`` — per source AND per commit, like the apm-source cache."""
    commit = run_allowed_to_fail(
        ["git", "-C", str(repo), "rev-parse", f"{ref}^{{commit}}"],
        expected_codes=None,
        timeout=_ARCHIVE_TIMEOUT_SECONDS,
    )
    resolved = commit.stdout.strip() if commit.returncode == 0 else ""
    slug = label.replace("/", "-").replace(" ", "-")
    return f"{slug}@{resolved or ref.replace('/', '-')}"


def _link(link: Path, target: Path) -> None:
    """Point *link* at *target*, replacing a stale link but never a real directory."""
    if link.is_symlink():
        link.unlink()
    link.symlink_to(target)


def install_published_skills(clone: SkillSourceClone, *, link_dir: Path, cache_root: Path) -> CloneInstall:
    """Make every skill *clone* publishes loadable from *link_dir*.

    Returns what happened rather than raising: a source that cannot be resolved on
    this box is a WARN the caller reports, not a reason to fail ``t3 setup`` — the
    drift gate is what turns a still-absent skill into a FAIL.
    """
    published = resolve_published_skills(clone)
    if published.unmeasurable or published.repo is None:
        return CloneInstall(label=published.label, ref=published.ref, unavailable=published.unmeasurable)

    export = cache_root / _cache_name(published.label, published.repo, published.ref)
    if not _export_ref(published.repo, published.ref, export):
        return CloneInstall(
            label=published.label,
            ref=published.ref,
            unavailable=f"could not export {published.ref} from {published.repo}",
        )

    link_dir.mkdir(parents=True, exist_ok=True)
    installed: list[str] = []
    already: list[str] = []
    for skill_md_path, name in sorted(published.names.items(), key=itemgetter(1)):
        if (link_dir / name / _SKILL_FILE).is_file():
            already.append(name)
            continue
        target = export / Path(skill_md_path).parent
        if not (target / _SKILL_FILE).is_file():
            continue
        _link(link_dir / name, target)
        installed.append(name)
    return CloneInstall(
        label=published.label,
        ref=published.ref,
        installed=tuple(installed),
        already_loadable=tuple(already),
    )
