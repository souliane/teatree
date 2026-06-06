"""Orphan per-worktree docker reaping used by ``t3 teatree workspace clean-all``.

Its own module so :mod:`teatree.core.management.commands._workspace_cleanup`
stays under the module-health function cap. The per-worktree (on-teardown)
half lives on the overlay hook ``reap_worktree_external_resources``; this is
the orphan half — compose projects whose worktree directory is already gone.
"""

from pathlib import Path

from teatree.core.models import Worktree
from teatree.core.worktree_env import compose_project
from teatree.docker.reap import reap_orphan_compose_projects


def _live_compose_projects() -> set[str]:
    """Compose projects whose worktree row still exists on disk — the keep set.

    A project is live only when a ``Worktree`` row claims it AND that row's
    directory is present; a row whose directory is gone is not live, so its
    docker artifacts fall to :func:`reap_orphan_worktree_docker`.
    """
    live: set[str] = set()
    for wt in Worktree.objects.select_related("ticket"):
        wt_path = (wt.extra or {}).get("worktree_path", "")
        if wt_path and Path(wt_path).is_dir():
            live.add(compose_project(wt))
    return live


def reap_orphan_worktree_docker() -> list[str]:
    """Reap docker containers + images for compose projects with no live worktree.

    Scoped by the ``com.docker.compose.project`` label, so base/official images
    and the main-clone deps image — none of which carry a worktree project label
    — are never touched.
    """
    return [str(result) for result in reap_orphan_compose_projects(_live_compose_projects())]
