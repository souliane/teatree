"""Orphan per-worktree docker reaping used by ``t3 teatree workspace clean-all``.

Its own module so :mod:`teatree.core.management.commands._workspace.cleanup`
stays under the module-health function cap. The per-worktree (on-teardown)
half lives on the overlay hook ``provisioning.reap_external_resources``; this is
the orphan half — compose projects nothing is still using.

Two reaping flavours share the same ownership basis
(:func:`_owned_compose_projects`). Both draw candidates from
``teatree.docker.reap._reapable_candidates``, which admits only projects teatree
itself provisioned or this module vouched for, and only while nothing is running
in them — so a stack it did not create, or one still in use, is never reachable
from either:

- :func:`reap_orphan_worktree_docker` — the ``clean-all`` deep clean: every
    teatree-owned project with nothing running in it goes, regardless of age
    (the user explicitly asked for a full cleanup).
- :func:`reap_stale_local_stacks` — the AUTOMATIC pre-start/pre-provision
    sweep (#2207): age-keyed, so a parallel session's fresh in-worktree stack
    (a ``docker compose -f docker-compose.test.yml`` run minutes ago) is never
    torn down, while an abandoned stack squatting host CPU/RAM for hours is.
"""

from collections.abc import Callable

from teatree.config import get_effective_settings
from teatree.core.management.commands._workspace.preview import preview_line
from teatree.core.models import Worktree
from teatree.core.worktree.worktree_env import compose_project
from teatree.core.worktree.worktree_roots import canonical_worktree_root, scanned_worktree_roots
from teatree.docker.reap import (
    OwnedStacks,
    reap_orphan_compose_projects,
    reap_stale_compose_projects,
    sibling_test_project,
)


def _owned_compose_projects() -> OwnedStacks:
    """Every compose project a registered worktree owns — the reapers' ownership proof.

    A worktree owns two: the stack teatree provisions for it
    (:func:`compose_project`) and the ``<checkout-dir>-test`` sibling its repo's
    own test harness brings up beside it. Only the first is self-identifying; the
    second's name is a directory BASENAME, which two unrelated checkouts share — so
    the checkout paths travel with the names, and the reaper admits a ``-test``
    project only once every container of it runs from one of them.

    The ROW is the proof, not the directory: a checkout that has since been
    removed still leaves both stacks behind, and whether either is safe to touch
    is decided by what its containers are doing, not by what is on disk.

    A row cannot speak for a checkout it no longer has, though, and that is exactly
    the leaked stack: a ``<checkout>-test`` sibling that outlived its row was
    unreachable by every reaper at any age. So the ROOTS travel too, and the reaper
    admits a test-harness stack running from inside one on path evidence alone. The
    roots come from the same registry, so they are spelled the way the container
    labels are spelled, and an execution context that provisions under a different
    absolute path contributes its own — no platform knob, and nothing to keep in
    sync with how any one host is mounted.
    """
    names: set[str] = set()
    checkouts: set[str] = set()
    for wt in Worktree.objects.select_related("ticket"):
        names.add(compose_project(wt))
        if wt_path := (wt.extra or {}).get("worktree_path", ""):
            names.add(sibling_test_project(wt_path))
            checkouts.add(wt_path)
    roots = {str(root) for root in scanned_worktree_roots(canonical_worktree_root())}
    return OwnedStacks(
        project_names=frozenset(names),
        checkout_paths=frozenset(checkouts),
        checkout_roots=frozenset(roots),
    )


def reap_orphan_worktree_docker(*, dry_run: bool = False) -> list[str]:
    """Reap docker containers + images for teatree-owned compose projects nothing is using.

    Scoped by the ``com.docker.compose.project`` label, so base/official images
    and the main-clone deps image — none of which carry a worktree project label
    — are never touched.
    """
    owned = _owned_compose_projects()
    if dry_run:
        from teatree.docker.reap import orphan_compose_projects  # noqa: PLC0415 — selection-only, dry-run path

        return [preview_line(f"Reap orphan compose project: {p}", dry_run=True) for p in orphan_compose_projects(owned)]
    return [str(result) for result in reap_orphan_compose_projects(owned)]


def reap_stale_local_stacks(write_out: Callable[[str], object] | None = None) -> int:
    """Tear down ABANDONED docker stacks before starting/provisioning (#2207).

    The automatic, age-guarded sweep: a teatree-provisioned compose project
    with nothing running in it is reaped only when its newest container
    lifecycle event is older than ``stale_stack_min_age_minutes`` (``240`` by
    default; ``0`` disables the sweep). Frees the host CPU/RAM that abandoned
    test stacks otherwise squat for the whole day, without ever touching a live
    parallel session's stack (anything running ⇒ kept; younger than the
    threshold ⇒ kept; unknown age ⇒ kept) or a project teatree cannot prove it
    owns. Returns the number of projects reaped.
    """
    min_age_minutes = int(get_effective_settings().stale_stack_min_age_minutes)
    if min_age_minutes <= 0:
        return 0
    results = reap_stale_compose_projects(_owned_compose_projects(), min_age_minutes=min_age_minutes)
    if write_out is not None:
        for result in results:
            write_out(f"  Reaped stale docker stack (nothing running, idle > {min_age_minutes}m): {result}")
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
    owned = _owned_compose_projects()
    if dry_run:
        candidates = stale_compose_projects(owned, min_age_minutes=threshold)
        for project in candidates:
            write_out(f"  Would reap stale docker stack: {project}")
        return candidates
    results = [str(result) for result in reap_stale_compose_projects(owned, min_age_minutes=threshold)]
    for line in results:
        write_out(f"  {line}")
    if not results:
        write_out("  No stale abandoned docker stacks found.")
    return results
