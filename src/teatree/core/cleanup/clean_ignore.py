"""The single ``clean_ignore`` never-reap predicate, in core so every caller shares it.

A branch matching a ``clean_ignore`` glob (the DB-home ``clean_ignore`` setting,
per-overlay overridable) must NEVER be reaped on any deletion path — the
done-worktree reaper, the branch-prune passes, and the orphan-raw-worktree pass.
Living in :mod:`teatree.core` (not a management command) keeps the dependency
direction clean: :mod:`teatree.core.worktree.worktree_done` and ``core/runners`` reach the
predicate without importing a management-command sibling.
"""

from fnmatch import fnmatch

from teatree.config import get_effective_settings


def is_clean_ignored(branch: str, *, overlay: str | None = None) -> bool:
    """Whether ``branch`` matches a ``clean_ignore`` glob and must never be reaped.

    The DB-home ``clean_ignore`` setting is per-overlay overridable, so the
    patterns are resolved through :func:`get_effective_settings` for the row's own
    overlay (``overlay`` = ``worktree.overlay``) or, on the repo-scoped branch-prune
    path, the active overlay (``overlay=None``). Resolution per pattern set: the
    overlay-scope ``ConfigSetting`` row, then the global-scope row, then the empty
    default. A ``[teatree]`` / ``[overlays.<name>]`` TOML value is ignored on read.
    """
    patterns = get_effective_settings(overlay).clean_ignore
    return any(fnmatch(branch, pattern) for pattern in patterns)
