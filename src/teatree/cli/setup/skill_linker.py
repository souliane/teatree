"""Runtime skill-symlink synchronization for ``t3 setup``."""

import logging
import shutil
from pathlib import Path

from teatree.cli.doctor import DoctorService

logger = logging.getLogger(__name__)

# Skills that conflict with teatree's multi-repo architecture.
# Always excluded — not user-configurable.  Users can add extra
# exclusions via the ``excluded_skills`` setting in the DB config store.
CORE_EXCLUDED_SKILLS = ["using-superpowers", "using-git-worktrees"]


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _ensure_skill_link(
    target: Path,
    link: Path,
    workspace_dir: Path,
) -> tuple[int, int]:
    """Ensure *link* points to *target*, respecting contribute-mode workspace links.

    Returns ``(created, fixed)`` counts.
    """
    if link.is_symlink():
        try:
            resolved = link.resolve()
            if str(resolved).startswith(str(workspace_dir)):
                return 0, 0  # Contribute-mode link, don't touch
        except OSError:
            pass
        if link.resolve() == target.resolve():
            return 0, 0  # Already correct
        link.unlink()
        link.symlink_to(target)
        return 0, 1
    if link.exists():
        return 0, 0  # Real directory, don't touch
    link.symlink_to(target)
    return 1, 0


def _remove_core_skill_links(runtime_skills: Path, core_skills_dir: Path) -> int:
    """Remove symlinks in *runtime_skills* whose names match a core skill.

    Used by runtimes where core skills are delivered via a plugin (e.g. Claude),
    to prune duplicates inherited from earlier symlink-based installs.
    """
    removed = 0
    for skill in sorted(core_skills_dir.iterdir()):
        if not (skill / "SKILL.md").is_file():
            continue
        link = runtime_skills / skill.name
        if link.is_symlink():
            link.unlink()
            removed += 1
    return removed


def _managed_skill_roots(default_skills_dir: Path, overlay_targets: list[Path]) -> list[Path]:
    """Source directories teatree owns: the core skills dir + each overlay ``skills/`` root.

    A runtime symlink resolving under one of these roots was created by teatree
    and is safe to prune when its skill no longer exists in source — a user's
    own hand-placed skill never resolves here.
    """
    roots = [default_skills_dir.resolve()]
    roots.extend({target.parent.resolve() for target in overlay_targets})
    return roots


def _prune_stale_skill_links(
    runtime_skills: Path,
    expected_names: set[str],
    managed_roots: list[Path],
    workspace_dir: Path,
) -> int:
    """Remove teatree-managed runtime skill links no longer backed by source.

    A renamed or deleted upstream skill leaves a runtime link whose name is not
    in *expected_names*: either a broken symlink (target removed) or a symlink
    still resolving into a managed source root under the stale name. Both are
    pruned so the dropped skill stops resolving as available after ``t3 setup``
    (which ``t3 update`` re-runs). Contribute-mode workspace links and a user's
    own real skill directories are left untouched.
    """
    pruned = 0
    for link in sorted(runtime_skills.iterdir()):
        if link.name in expected_names or not link.is_symlink():
            continue
        if not link.exists():
            link.unlink()
            pruned += 1
            continue
        try:
            resolved = link.resolve()
        except OSError:
            continue
        if str(resolved).startswith(str(workspace_dir.resolve())):
            continue
        if any(_is_within(resolved, root) for root in managed_roots):
            link.unlink()
            pruned += 1
    return pruned


class SkillLinker:
    """Sync core + overlay skill symlinks into one runtime skills directory."""

    def __init__(self, runtime_skills: Path, workspace_dir: Path) -> None:
        self.runtime_skills = runtime_skills
        self.workspace_dir = workspace_dir

    def remove_excluded(self, excluded: list[str]) -> int:
        """Remove excluded skill symlinks/directories from the runtime skills dir."""
        removed = 0
        for name in excluded:
            if "/" in name or name.startswith("."):
                logger.warning("Ignoring suspicious excluded_skills entry: %r", name)
                continue
            skill_path = self.runtime_skills / name
            if skill_path.is_symlink():
                skill_path.unlink()
                removed += 1
            elif skill_path.is_dir():
                shutil.rmtree(skill_path)
                removed += 1
        return removed

    def sync(self, *, sync_core: bool = True) -> tuple[int, int]:
        """Create or fix symlinks for core and overlay skills, pruning stale ones.

        When ``sync_core`` is ``False``, core skills are assumed to be delivered via
        a plugin; any existing core symlinks are pruned and only overlays are linked.
        Links for skills removed or renamed upstream — present in the runtime dir but
        absent from the current source — are pruned so they stop resolving.

        Returns ``(created, fixed)`` counts (pruned links are not counted).
        """
        from teatree.agents.skill_bundle import DEFAULT_SKILLS_DIR  # noqa: PLC0415

        created = 0
        fixed = 0

        core_skill_names = {s.name for s in DEFAULT_SKILLS_DIR.iterdir() if (s / "SKILL.md").is_file()}
        if sync_core:
            for skill in sorted(DEFAULT_SKILLS_DIR.iterdir()):
                if not (skill / "SKILL.md").is_file():
                    continue
                c, f = _ensure_skill_link(skill, self.runtime_skills / skill.name, self.workspace_dir)
                created += c
                fixed += f
        else:
            _remove_core_skill_links(self.runtime_skills, DEFAULT_SKILLS_DIR)

        overlay_skills = DoctorService.collect_overlay_skills()
        expected_names: set[str] = set(core_skill_names) if sync_core else set()
        for target, link_name in overlay_skills:
            expected_names.add(link_name)
            if link_name in core_skill_names:
                continue
            c, f = _ensure_skill_link(target, self.runtime_skills / link_name, self.workspace_dir)
            created += c
            fixed += f

        managed_roots = _managed_skill_roots(DEFAULT_SKILLS_DIR, [target for target, _ in overlay_skills])
        _prune_stale_skill_links(self.runtime_skills, expected_names, managed_roots, self.workspace_dir)

        return created, fixed

    def clean_broken(self) -> int:
        """Remove broken symlinks from the runtime skills directory."""
        broken = 0
        for link in self.runtime_skills.iterdir():
            if link.is_symlink() and not link.exists():
                link.unlink()
                broken += 1
        return broken
