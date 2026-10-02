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
    there before the skills CLI installs the selected names.
*   **Never displace what is already loadable.** A name the runtime can already
    resolve is left exactly as it is. That keeps the step idempotent for the
    container entrypoint, which runs ``t3 setup`` on every start, and keeps a
    deliberately overridden local skill from being silently replaced.
"""

import logging
import os
import shutil
import tarfile
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from teatree.harness_skills import SkillsHarness
from teatree.provisioning.skill_drift import SkillSourceClone, resolve_published_skills
from teatree.provisioning.skills_cli import SkillAddResult, SkillAddStatus, SkillsCli, SkillsCliError
from teatree.utils.run import run_allowed_to_fail

logger = logging.getLogger(__name__)

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
    excluded: tuple[str, ...] = ()
    unavailable: str = ""

    def render(self) -> str:
        """The one line ``t3 setup`` prints for this source."""
        if self.unavailable:
            return f"WARN  Skill source {self.label} not provisioned: {self.unavailable}."
        where = f"Skill source {self.label} at {self.ref}"
        if not self.installed:
            if self.excluded:
                return f"OK    {where}: {len(self.excluded)} harness skill(s) excluded."
            if self.already_loadable:
                return f"OK    {where}: {len(self.already_loadable)} harness skill(s) already installed."
            return f"OK    {where}: no demanded skills published."
        sample = ", ".join(self.installed[:_MAX_NAMED])
        more = f" (+{len(self.installed) - _MAX_NAMED} more)" if len(self.installed) > _MAX_NAMED else ""
        return (
            f"OK    {where}: installed {len(self.installed)} harness skill(s) — {sample}{more}; "
            f"{len(self.already_loadable)} already installed."
        )


def _selection_by_harness(
    demand_names: set[str],
    harness_exclusions: list[str],
) -> tuple[dict[SkillsHarness, tuple[str, ...]], tuple[str, ...]]:
    # Declared demands are runtime requirements, not optional harness inventory.
    # Exclusions may remove optional skills elsewhere, but cannot disable workflow
    # context the active overlay says every dispatch needs.
    del harness_exclusions
    demands = {name.rsplit(":", 1)[-1].strip().casefold() for name in demand_names if name.strip()}
    return {harness: tuple(sorted(demands)) for harness in SkillsHarness}, ()


def _record_results(
    results: tuple[SkillAddResult, ...],
    harnesses: Sequence[SkillsHarness],
    installed: list[str],
    already: list[str],
    failed: list[str],
) -> None:
    for result in results:
        targets = [f"{harness.value}:{result.name}" for harness in harnesses]
        if result.status is SkillAddStatus.INSTALLED:
            installed.extend(targets)
        elif result.status is SkillAddStatus.SKIPPED:
            already.extend(targets)
        else:
            failed.extend(targets)


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


def _install_selected(
    export: Path,
    selected_by_harness: dict[SkillsHarness, tuple[str, ...]],
    cli: SkillsCli | None,
) -> tuple[tuple[str, ...], tuple[str, ...], str]:
    grouped: dict[tuple[str, ...], list[SkillsHarness]] = defaultdict(list)
    for harness, selected in selected_by_harness.items():
        if selected:
            grouped[selected].append(harness)
    installed: list[str] = []
    already: list[str] = []
    failed: list[str] = []
    client = cli or SkillsCli()
    unavailable = ""
    try:
        for selected, harnesses in grouped.items():
            results = client.add_selected(str(export), tuple(harnesses), selected)
            _record_results(results, harnesses, installed, already, failed)
    except SkillsCliError as error:
        unavailable = str(error)
    if failed:
        unavailable = f"skills CLI failed to install: {', '.join(sorted(failed))}"
    return tuple(sorted(installed)), tuple(sorted(already)), unavailable


def install_published_skills(
    clone: SkillSourceClone,
    *,
    cache_root: Path,
    demand_names: set[str],
    harness_exclusions: list[str],
    cli: SkillsCli | None = None,
) -> CloneInstall:
    selected_by_harness, applied_exclusions = _selection_by_harness(demand_names, harness_exclusions)
    if not any(selected_by_harness.values()):
        return CloneInstall(label=clone.label or "skill source", excluded=applied_exclusions)

    published = resolve_published_skills(clone)
    if published.unmeasurable or published.repo is None:
        return CloneInstall(label=published.label, ref=published.ref, unavailable=published.unmeasurable)

    published_names = {name.casefold(): name for name in published.names.values()}
    selected_by_harness = {
        harness: tuple(published_names[name] for name in selected if name in published_names)
        for harness, selected in selected_by_harness.items()
    }
    if not any(selected_by_harness.values()):
        return CloneInstall(label=published.label, ref=published.ref, excluded=applied_exclusions)

    export = cache_root / _cache_name(published.label, published.repo, published.ref)
    if not _export_ref(published.repo, published.ref, export):
        return CloneInstall(
            label=published.label,
            ref=published.ref,
            unavailable=f"could not export {published.ref} from {published.repo}",
        )

    installed, already, unavailable = _install_selected(export, selected_by_harness, cli)
    if unavailable:
        return CloneInstall(
            label=published.label,
            ref=published.ref,
            already_loadable=already,
            excluded=applied_exclusions,
            unavailable=unavailable,
        )
    return CloneInstall(
        label=published.label,
        ref=published.ref,
        installed=installed,
        already_loadable=already,
        excluded=applied_exclusions,
    )
