"""Materialise a missing source clone so provisioning needs no pre-seeded workspace.

``find_clone_path`` answers "is there a clone here?"; this module answers "make
sure there is one". The distinction is what lets a runtime own its workspace
instead of inheriting one.

The containerized stack is the case that forces it. Its clone root is a
container-owned volume, not a bind of the operator's ``~/workspace``: a git
worktree records an ABSOLUTE ``gitdir`` pointer into its source clone, so sharing
a clone across the boundary works only where the two venues agree on its path,
and a bind that lands the operator's clones at a DIFFERENT container path makes
every worktree structurally unusable on the other side (``fatal: not a git
repository``, naming a gitdir the reading venue has no such path for). So the
container clones what it needs into its own root. What the root then holds for
a repo may be a link to a clone mounted at
PATH IDENTITY — the deploy checkout, which every venue names the same way
(souliane/teatree#4120) — and that is the shape a shared clone has to take.

Cloning is by definition a network operation against a private remote, so it
depends on the runtime's git credential helper being wired (``deploy/entrypoint.sh``
registers ``gh``/``glab`` for their hosts). A failure is reported, never retried
blind and never fatal to the caller — provisioning degrades to the same
"no clone found" it had before.
"""

import logging
from pathlib import Path

from teatree.core.overlay import OverlayBase
from teatree.core.worktree.clone_paths import find_clone_path
from teatree.utils.run import run_allowed_to_fail

logger = logging.getLogger(__name__)

#: A cold clone of a large monorepo over a slow link is minutes, not seconds. Long
#: enough that a healthy clone is never cut off; bounded so a hung transport cannot
#: wedge provisioning forever.
CLONE_TIMEOUT_SECONDS = 1800


def ensure_clone(clone_root: Path, repo_name: str, overlay: OverlayBase | None = None) -> Path | None:
    """Return the source clone for *repo_name*, cloning it into *clone_root* when absent.

    An existing clone is returned untouched — this is idempotent and adds no cost
    to the host-native path, where the operator's clones are already there.

    ``None`` means no clone exists AND none could be made: the overlay declared no
    remote for the repo, or the clone failed. Callers read that exactly as they
    read a :func:`find_clone_path` miss.
    """
    found = find_clone_path(clone_root, repo_name)
    if found is not None:
        return found

    url = overlay.provisioning.repo_clone_url(repo_name) if overlay is not None else ""
    if not url:
        return None

    return _clone(clone_root, repo_name, url)


def _clone(clone_root: Path, repo_name: str, url: str) -> Path | None:
    """``git clone`` *url* to ``clone_root / repo_name``; ``None`` on failure.

    The destination is the LITERAL ``clone_root / repo_name`` because that is the
    first path :func:`find_clone_path` probes, so the next lookup resolves without
    the one-level basename scan. A slug-shaped ``repo_name`` (``owner/repo``)
    therefore lands at ``clone_root/owner/repo``, which is the namespaced layout
    that scan exists to support.

    A destination that already exists but is not a checkout (an interrupted clone
    left a partial tree) is refused rather than cloned over: ``git clone`` fails on
    a non-empty directory anyway, and silently removing a directory teatree did not
    create is not a call this function gets to make.
    """
    destination = clone_root / repo_name
    if destination.exists():
        logger.warning(
            "Not cloning %s: %s already exists but is not a git checkout. Remove or repair it, then retry.",
            repo_name,
            destination,
        )
        return None

    destination.parent.mkdir(parents=True, exist_ok=True)
    logger.info("No local clone for %s — cloning %s into %s", repo_name, url, destination)
    result = run_allowed_to_fail(
        ["git", "clone", "--quiet", url, str(destination)],
        expected_codes=None,
        timeout=CLONE_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        logger.warning("Could not clone %s from %s: %s", repo_name, url, result.stderr.strip())
        return None

    return destination
