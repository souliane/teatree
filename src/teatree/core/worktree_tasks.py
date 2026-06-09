"""Per-worktree FSM task workers.

These four ``@task`` functions back the
``Worktree.provision/start_services/verify/teardown`` transitions
(BLUEPRINT §4). Each worker takes a row lock and re-checks state before
running so at-least-once delivery is safe.

Lives in its own module so ``teatree.core.tasks`` stays under the
module-health function-count cap. Workers are kept as module-level
functions because ``django.tasks`` discovers and serialises them by
qualified name.
"""

import logging
from typing import TypedDict

from django.db import transaction
from django.tasks import task

from teatree.core.models import Worktree
from teatree.core.models.types import WorktreeExtra
from teatree.core.runners import (
    WorktreeProvisionRunner,
    WorktreeStartRunner,
    WorktreeTeardownRunner,
    WorktreeVerifyRunner,
)
from teatree.core.runners.worktree_start import docker_compose_down
from teatree.core.worktree_env import compose_project

logger = logging.getLogger(__name__)


class WorktreeTransitionResult(TypedDict, total=False):
    worktree_id: int
    ok: bool
    skipped: bool
    state: str
    detail: str


@task()
def execute_worktree_provision(worktree_id: int) -> WorktreeTransitionResult:
    """Run heavy provisioning side-effects for a single worktree.

    Fired by ``Worktree.provision()``'s on_commit. The runner writes the env
    cache, configures direnv + prek, runs DB import + overlay setup steps,
    and runs health checks. State stays in ``PROVISIONED`` whether the work
    succeeds or fails — re-firing ``provision()`` (source allows
    PROVISIONED → PROVISIONED) replays the runner.

    At-least-once delivery is safe: every step is idempotent (env cache
    rewrites cleanly, ``db_import`` no-ops when the DB exists, overlay
    steps are expected to be re-runnable).

    Poison-pill guard (souliane/teatree#1975, mirroring #1959/#1969): a
    worktree whose effective overlay (its own field, falling back to the
    ticket's) names a non-empty overlay that no longer resolves can never
    provision — ``get_overlay_for_worktree`` raises ``Overlay not found``
    every re-fire. Fail it permanently with a recorded result instead of
    raising forever. A blank overlay is the ambient single-overlay default
    and stays dispatchable.
    """
    from teatree.core.overlay_loader import resolve_overlay_name  # noqa: PLC0415

    with transaction.atomic():
        worktree = Worktree.objects.select_for_update().select_related("ticket").get(pk=worktree_id)
        if worktree.state != Worktree.State.PROVISIONED:
            logger.info(
                "execute_worktree_provision skipped for worktree %s: state=%s (not PROVISIONED)",
                worktree_id,
                worktree.state,
            )
            return {"worktree_id": worktree_id, "skipped": True, "state": str(worktree.state)}

        effective_overlay = worktree.overlay or worktree.ticket.overlay
        if effective_overlay and resolve_overlay_name(effective_overlay) is None:
            reason = f"unknown overlay {effective_overlay!r}: worktree {worktree_id} cannot be provisioned"
            logger.warning("execute_worktree_provision: %s", reason)
            return {"worktree_id": worktree_id, "ok": False, "detail": reason}

        result = WorktreeProvisionRunner(worktree).run()
        if not result.ok:
            logger.warning("Worktree provision failed for %s: %s", worktree_id, result.detail)
            return {"worktree_id": worktree_id, "ok": False, "detail": result.detail}

    return {"worktree_id": worktree_id, "ok": True, "detail": result.detail}


@task()
def execute_worktree_start(worktree_id: int) -> WorktreeTransitionResult:
    """Boot docker compose for a single worktree.

    Fired by ``Worktree.start_services()``'s on_commit. The runner stops any
    previous containers, refreshes the env cache with allocated ports, runs
    overlay pre-run steps, and starts ``docker compose up -d``. State stays
    in ``SERVICES_UP`` even on failure so re-firing ``start_services()``
    replays the docker cycle.
    """
    with transaction.atomic():
        worktree = Worktree.objects.select_for_update().get(pk=worktree_id)
        if worktree.state != Worktree.State.SERVICES_UP:
            logger.info(
                "execute_worktree_start skipped for worktree %s: state=%s (not SERVICES_UP)",
                worktree_id,
                worktree.state,
            )
            return {"worktree_id": worktree_id, "skipped": True, "state": str(worktree.state)}

        result = WorktreeStartRunner(worktree).run()
        if not result.ok:
            logger.warning("Worktree start failed for %s: %s", worktree_id, result.detail)
            return {"worktree_id": worktree_id, "ok": False, "detail": result.detail}

    return {"worktree_id": worktree_id, "ok": True, "detail": result.detail}


@task()
def execute_worktree_stop(worktree_id: int) -> WorktreeTransitionResult:
    """Bring the WHOLE compose project down for one worktree (reversible).

    Fired by ``Worktree.stop_services()``'s on_commit (the idle-stack reaper's
    demotion path, souliane/teatree#2190). The transition has already advanced
    the FSM to ``PROVISIONED``; this worker stops every container in the
    compose project via ``docker compose -p <project> down --remove-orphans``
    so NO stray container survives — a leaked ``db-1`` left running after the
    app tier went down (the wt595 partial-stack class) is removed too.

    Distinct from teardown: the DB is NOT dropped and the git worktree is NOT
    removed, so a later ``start_services`` is a fast resume. State stays in
    ``PROVISIONED`` whether the down succeeds or fails — ``docker_compose_down``
    is itself best-effort and idempotent (a re-fire compose-downs an already
    down project as a no-op).

    The state guard re-reads PROVISIONED under a row lock: a row that is no
    longer PROVISIONED (a concurrent ``start_services`` revived it between the
    transition and this worker) is a stale read — skip rather than stop a
    freshly-restarted stack (fail-CLOSED stale-read guard).
    """
    with transaction.atomic():
        try:
            worktree = Worktree.objects.select_for_update().select_related("ticket").get(pk=worktree_id)
        except Worktree.DoesNotExist:
            logger.info("execute_worktree_stop skipped: worktree %s already gone", worktree_id)
            return {"worktree_id": worktree_id, "skipped": True}
        if worktree.state != Worktree.State.PROVISIONED:
            logger.info(
                "execute_worktree_stop skipped for worktree %s: state=%s (not PROVISIONED)",
                worktree_id,
                worktree.state,
            )
            return {"worktree_id": worktree_id, "skipped": True, "state": str(worktree.state)}
        project = compose_project(worktree)
        docker_compose_down(project)
    return {"worktree_id": worktree_id, "ok": True, "detail": f"stopped compose project {project}"}


@task()
def execute_worktree_verify(worktree_id: int) -> WorktreeTransitionResult:
    """Run overlay health checks for a single worktree.

    Fired by ``Worktree.verify()``'s on_commit. Health checks are
    best-effort — failures are reported in the result detail but the
    worker does not bounce the FSM back to SERVICES_UP.
    """
    with transaction.atomic():
        worktree = Worktree.objects.select_for_update().get(pk=worktree_id)
        if worktree.state != Worktree.State.READY:
            logger.info(
                "execute_worktree_verify skipped for worktree %s: state=%s (not READY)",
                worktree_id,
                worktree.state,
            )
            return {"worktree_id": worktree_id, "skipped": True, "state": str(worktree.state)}

        result = WorktreeVerifyRunner(worktree).run()
        if not result.ok:
            logger.warning("Worktree verify reported failures for %s: %s", worktree_id, result.detail)
            return {"worktree_id": worktree_id, "ok": False, "detail": result.detail}

    return {"worktree_id": worktree_id, "ok": True, "detail": result.detail}


@task()
def execute_worktree_teardown(
    worktree_id: int,
    snapshot_db_name: str,
    snapshot_extra: WorktreeExtra,
) -> WorktreeTransitionResult:
    """Tear down a single worktree (docker down + DB drop + git worktree remove).

    Fired by ``Worktree.teardown()``'s on_commit. The transition body has
    cleared ``db_name`` and ``extra`` to satisfy the FSM contract (the row
    is in CREATED state), so the worker receives a snapshot of those fields
    captured before the reset — that's what the runner needs to know which
    DB to drop and which git worktree to remove. The runner deletes the
    Worktree row at the end, so subsequent re-fires no-op (the row is gone).
    Best-effort: per-worktree errors are reported in the result detail.
    """
    try:
        worktree = Worktree.objects.get(pk=worktree_id)
    except Worktree.DoesNotExist:
        logger.info("execute_worktree_teardown skipped: worktree %s already gone", worktree_id)
        return {"worktree_id": worktree_id, "skipped": True}

    result = WorktreeTeardownRunner(
        worktree,
        snapshot_db_name=snapshot_db_name,
        snapshot_extra=snapshot_extra,
    ).run()
    if not result.ok:
        logger.warning("Worktree teardown failed for %s: %s", worktree_id, result.detail)
        return {"worktree_id": worktree_id, "ok": False, "detail": result.detail}
    return {"worktree_id": worktree_id, "ok": True, "detail": result.detail}
