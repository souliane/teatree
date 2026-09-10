"""Collect skill-source checkouts nothing references any more (#4677).

The cache is keyed per ``(source, ref)`` so two pins of one repo can coexist, which
means every bump mints a new directory and leaves the previous one unread forever. On a
live box one had sat unreferenced for seven weeks with nothing to notice it.

Conservative by construction, in the same shape as the worktree reapers: a directory is
collected only when NOTHING can be shown to reach it — no declaration names it, no
installed skill symlink resolves into it, and it is not one of the refresher's own
clones. Anything this venue cannot explain is kept, because the cost of keeping a
directory is disk and the cost of removing a live one is a skill that stops loading.

A ``<name>.partial.<pid>`` staging directory is the one exception, and it is not an
exception to the rule above: it carries no export stamp, so it is provably an export
that died mid-flight and no reader may trust it.
"""

import logging
import shutil
from collections.abc import Iterable, Sequence
from pathlib import Path

logger = logging.getLogger(__name__)

#: The refresher's own fetch clones — keyed by SOURCE, not by ref, so no declared pin
#: ever names one and a reference-only rule would reap them on every pass.
_REFRESH_PREFIX = "refresh-"


def collect_unreferenced(
    cache_root: Path,
    *,
    referenced: Iterable[str],
    link_dirs: Sequence[Path],
) -> list[Path]:
    """Remove every cache checkout nothing reaches; return what was collected."""
    if not cache_root.is_dir():
        return []
    keep = set(referenced) | _resolved_into(cache_root, link_dirs)
    collected: list[Path] = []
    for entry in sorted(cache_root.iterdir()):
        if not entry.is_dir() or entry.name.startswith(_REFRESH_PREFIX) or entry.name in keep:
            continue
        try:
            shutil.rmtree(entry)
        except OSError:
            logger.warning("skill-source GC: could not remove %s", entry, exc_info=True)
            continue
        collected.append(entry)
    return collected


def _resolved_into(cache_root: Path, link_dirs: Sequence[Path]) -> set[str]:
    """Cache checkout names that an installed skill symlink actually resolves into."""
    resolved: set[str] = set()
    for link_dir in link_dirs:
        if not link_dir.is_dir():
            continue
        for entry in link_dir.iterdir():
            target = entry.resolve()
            if cache_root.resolve() in target.parents:
                resolved.add(target.relative_to(cache_root.resolve()).parts[0])
    return resolved
