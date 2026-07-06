"""Orphan per-worktree docker reaping used by ``t3 teatree workspace clean-all``.

Its own module so :mod:`teatree.core.management.commands._workspace_cleanup`
stays under the module-health function cap. The per-worktree (on-teardown)
half lives on the overlay hook ``reap_worktree_external_resources``; this is
the orphan half — compose projects whose worktree directory is already gone.

Two reaping flavours share the same keep set (:func:`_live_compose_projects`):

- :func:`reap_orphan_worktree_docker` — the ``clean-all`` deep clean: every
    unowned project goes, regardless of age (the user explicitly asked for a
    full cleanup).
- :func:`reap_stale_local_stacks` — the AUTOMATIC pre-start/pre-provision
    sweep (#2207): age-keyed, so a parallel session's fresh hand-rolled stack
    (a ``docker compose -f docker-compose.test.yml`` run minutes ago) is never
    torn down, while an abandoned stack squatting host CPU/RAM for hours is.
"""

from collections.abc import Callable
from pathlib import Path

from teatree.config import get_effective_settings
from teatree.core.models import Worktree
from teatree.core.worktree.worktree_env import compose_project
from teatree.docker.reap import reap_orphan_compose_projects, reap_stale_compose_projects


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


def reap_stale_local_stacks(write_out: Callable[[str], object] | None = None) -> int:
    """Tear down ABANDONED unowned docker stacks before starting/provisioning (#2207).

    The automatic, age-guarded sweep: an unowned compose project (no live
    ``Worktree`` row) is reaped only when its newest container lifecycle event
    is older than ``stale_stack_min_age_minutes`` (``0``, the default, keeps
    the sweep opt-in). Frees the host CPU/RAM that abandoned hand-rolled test
    stacks otherwise squat for the whole day, without ever touching a live
    parallel session's fresh stack (younger than the threshold ⇒ kept;
    unknown age ⇒ kept). Returns the number of projects reaped.
    """
    min_age_minutes = int(get_effective_settings().stale_stack_min_age_minutes)
    if min_age_minutes <= 0:
        return 0
    results = reap_stale_compose_projects(_live_compose_projects(), min_age_minutes=min_age_minutes)
    if write_out is not None:
        for result in results:
            write_out(f"  Reaped stale docker stack (no live worktree, idle > {min_age_minutes}m): {result}")
    return len(results)


def reap_stale_report(*, min_age_minutes: int, dry_run: bool, write_out: Callable[[str], object]) -> list[str]:
    """Engine of ``workspace reap-stale`` — selection, optional teardown, messaging.

    ``min_age_minutes == 0`` falls back to the configured
    ``stale_stack_min_age_minutes``; a non-positive effective threshold
    reports the sweep disabled. ``dry_run`` lists the would-reap candidates
    without removing anything.
    """
    from teatree.docker.reap import stale_compose_projects  # noqa: PLC0415 — only the CLI report needs selection-only

    threshold = min_age_minutes or int(get_effective_settings().stale_stack_min_age_minutes)
    if threshold <= 0:
        write_out("  stale_stack_min_age_minutes <= 0 — stale-stack reaping disabled.")
        return []
    live = _live_compose_projects()
    if dry_run:
        candidates = stale_compose_projects(live, min_age_minutes=threshold)
        for project in candidates:
            write_out(f"  Would reap stale docker stack: {project}")
        return candidates
    results = [str(result) for result in reap_stale_compose_projects(live, min_age_minutes=threshold)]
    for line in results:
        write_out(f"  {line}")
    if not results:
        write_out("  No stale unowned docker stacks found.")
    return results
