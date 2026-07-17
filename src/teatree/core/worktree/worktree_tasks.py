"""Per-worktree FSM task workers.

These four ``@task`` functions back the
``Worktree.provision/start_services/verify/teardown`` transitions
(BLUEPRINT §4). Each worker takes a SHORT row lock ONLY to claim (re-check
state), then releases it and runs the heavy runner OUTSIDE the transaction.
The control DB is SQLite, whose connection-level write lock would otherwise
freeze the WHOLE control plane ("database is locked") for the minutes a
``uv sync`` / DB import / ``docker compose up`` / health-check pass takes.
At-least-once delivery stays safe because every runner step is idempotent.

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
from teatree.core.runners import (
    WorktreeProvisionRunner,
    WorktreeStartRunner,
    WorktreeTeardownRunner,
    WorktreeVerifyRunner,
)
from teatree.core.runners.worktree_start import docker_compose_down
from teatree.core.worktree.worktree_env import compose_project

logger = logging.getLogger(__name__)


class WorktreeTransitionResult(TypedDict, total=False):
    worktree_id: int
    ok: bool
    skipped: bool
    state: str
    detail: str


def _claim_worktree(
    worktree_id: int, expected_state: Worktree.State, *, verb: str, label: str
) -> "Worktree | WorktreeTransitionResult":
    """Claim a worktree under a SHORT row lock, then release it before the heavy runner.

    Re-checks the FSM state and overlay resolvability while holding the row lock,
    then RETURNS the row (lock released the instant the atomic block exits) so the
    caller runs the minutes-long runner OUTSIDE the transaction — the SQLite write
    lock is never held across provisioning, so the control plane never wedges on
    "database is locked". A stale-state read or an unresolvable overlay short-
    circuits with the result dict to return directly.
    """
    with transaction.atomic():
        worktree = Worktree.objects.select_for_update().select_related("ticket").get(pk=worktree_id)
        if worktree.state != expected_state:
            logger.info(
                "execute_worktree_%s skipped for worktree %s: state=%s (not %s)",
                label,
                worktree_id,
                worktree.state,
                expected_state.name,
            )
            return {"worktree_id": worktree_id, "skipped": True, "state": str(worktree.state)}
        reason = _unknown_overlay_reason(worktree, verb=verb)
        if reason is not None:
            logger.warning("execute_worktree_%s: %s", label, reason)
            return {"worktree_id": worktree_id, "ok": False, "detail": reason}
    return worktree


def _unknown_overlay_reason(worktree: Worktree, *, verb: str) -> str | None:
    """Return why *worktree*'s overlay is unresolvable, or ``None`` when it resolves.

    The shared poison-pill guard (souliane/teatree#1975, mirroring #1959/#1969):
    a worktree whose effective overlay (its own field, falling back to the
    ticket's) names a non-empty overlay that no longer resolves can never run a
    runner that constructs ``get_overlay_for_worktree`` — it raises ``Overlay
    not found`` on every re-fire, crashing the FSM worker forever. The workers
    short-circuit to a recorded ``ok=False`` instead of raising. A blank overlay
    is the ambient single-overlay default and stays dispatchable (``None``).
    """
    from teatree.core.overlay_loader import resolve_overlay_name  # noqa: PLC0415 — deferred: call-time import

    effective_overlay = worktree.overlay or worktree.ticket.overlay
    if effective_overlay and resolve_overlay_name(effective_overlay) is None:
        return f"unknown overlay {effective_overlay!r}: worktree {worktree.pk} cannot be {verb}"
    return None


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
    steps are expected to be re-runnable). The claim (state re-check) holds
    a SHORT row lock; the heavy runner runs OUTSIDE the transaction so the
    minutes-long provisioning never holds the SQLite write lock (see the
    module docstring).

    Poison-pill guard (souliane/teatree#1975, mirroring #1959/#1969): a
    worktree whose effective overlay (its own field, falling back to the
    ticket's) names a non-empty overlay that no longer resolves can never
    provision — ``get_overlay_for_worktree`` raises ``Overlay not found``
    every re-fire. Fail it permanently with a recorded result instead of
    raising forever (:func:`_unknown_overlay_reason`). A blank overlay is the
    ambient single-overlay default and stays dispatchable.
    """
    claim = _claim_worktree(worktree_id, Worktree.State.PROVISIONED, verb="provisioned", label="provision")
    if not isinstance(claim, Worktree):
        return claim

    result = WorktreeProvisionRunner(claim).run()
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

    Poison-pill guard at parity with ``execute_worktree_provision``
    (:func:`_unknown_overlay_reason`): ``WorktreeStartRunner`` resolves the
    overlay in its ``__init__`` (``get_overlay_for_worktree``), so a worktree
    whose overlay was uninstalled would raise ``Overlay not found`` on every
    re-fire. Short-circuit to a recorded ``ok=False`` before constructing the
    runner so one bad worktree never crashes its FSM worker forever.

    The claim (state re-check) holds a SHORT row lock; ``docker compose up`` +
    overlay pre-run steps run OUTSIDE the transaction so the SQLite write lock is
    never held across them (see the module docstring).
    """
    claim = _claim_worktree(worktree_id, Worktree.State.SERVICES_UP, verb="started", label="start")
    if not isinstance(claim, Worktree):
        return claim

    result = WorktreeStartRunner(claim).run()
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

    Poison-pill guard at parity with ``execute_worktree_provision``
    (:func:`_unknown_overlay_reason`): ``WorktreeVerifyRunner`` resolves the
    overlay in its ``__init__`` (``get_overlay_for_worktree``), so a worktree
    whose overlay was uninstalled would raise ``Overlay not found`` on every
    re-fire. Short-circuit to a recorded ``ok=False`` before constructing the
    runner.

    The claim (state re-check) holds a SHORT row lock; the overlay health checks
    run OUTSIDE the transaction so the SQLite write lock is never held across them
    (see the module docstring).
    """
    claim = _claim_worktree(worktree_id, Worktree.State.READY, verb="verified", label="verify")
    if not isinstance(claim, Worktree):
        return claim

    result = WorktreeVerifyRunner(claim).run()
    if not result.ok:
        logger.warning("Worktree verify reported failures for %s: %s", worktree_id, result.detail)
        return {"worktree_id": worktree_id, "ok": False, "detail": result.detail}
    return {"worktree_id": worktree_id, "ok": True, "detail": result.detail}


@task()
def execute_worktree_teardown(worktree_id: int) -> WorktreeTransitionResult:
    """Tear down a single worktree (docker down + DB drop + git worktree remove).

    Fired by ``Worktree.teardown()``'s on_commit. The transition KEEPS
    ``db_name`` and ``extra`` on the row (the recovery pointers naming the DB to
    drop and the worktree to remove), so the runner reads them straight off the
    live row. The runner deletes the Worktree row at the end, so subsequent
    re-fires no-op (the row is gone) and a crash before the delete leaves a
    still-reapable row (pointers intact). Best-effort: per-worktree errors are
    reported in the result detail.
    """
    try:
        worktree = Worktree.objects.get(pk=worktree_id)
    except Worktree.DoesNotExist:
        logger.info("execute_worktree_teardown skipped: worktree %s already gone", worktree_id)
        return {"worktree_id": worktree_id, "skipped": True}

    result = WorktreeTeardownRunner(worktree).run()
    if not result.ok:
        logger.warning("Worktree teardown failed for %s: %s", worktree_id, result.detail)
        return {"worktree_id": worktree_id, "ok": False, "detail": result.detail}
    return {"worktree_id": worktree_id, "ok": True, "detail": result.detail}
