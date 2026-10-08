"""The dormant-artifact sweep — the loss-free reclaim, off the destructive ladder (#4244).

It is also the factory's unattended housekeeping caller (#4923): each pass drains the
terminal-ticket teardown backlog and releases orphan per-worktree env dirs, both of which
otherwise waited for an operator to type ``clean-all``.

Its own module because it is its own pass. It has its own scanner
(:class:`~teatree.loop.scanners.artifact_eviction.ArtifactEvictionScanner`), its own
cadence, its own marker fields, and — the reason that matters — its own authority:
nothing it removes is destructive at any fullness, so it is not gated on the
disk-CRIT band. Sharing a module with the ladder is what
made both of those couplings look natural.

What it removes and what protects it is :mod:`teatree.core.cleanup.artifact_eviction`;
this module is the loop-facing wrapper that plans, records, executes and re-records.
"""

import logging
import time

from django.utils import timezone

from teatree.config import worktree_root
from teatree.core.cleanup.artifact_eviction import (
    ArtifactEvictionPlan,
    EvictionOutcome,
    evict_artifacts,
    plan_artifact_eviction,
)
from teatree.core.cleanup.isolated_roots import reap_orphan_isolated_worktree_roots
from teatree.core.tasks import TeardownDispatch
from teatree.loop.dispatch import ActionPayload
from teatree.loop.mechanical_plan import GIB, FreePlan, append_stopped_deletions, persist_plan, sampled
from teatree.loop.reclaim_yield import pressure_idle_days

logger = logging.getLogger(__name__)

#: The whole pass — walk, sizing, re-walks, deletions — ends inside this, well under the
#: resource_pressure tick's 300 s deadline, so it finishes as a recorded partial run.
ARTIFACT_PASS_BUDGET_SECONDS = 120.0
#: The orphan env-dir reaper's checkout walk; together with the pass above, inside the 300 s tick.
ENV_DIR_WALK_BUDGET_SECONDS = 60.0


def sweep_artifacts(payload: ActionPayload) -> None:
    """Reclaim dormant build artifacts — the loss-free pass, off the destructive ladder (#4244)."""
    from teatree.core.models.resource_pressure_marker import ResourcePressureMarker  # noqa: PLC0415 — lazy ORM import

    marker = ResourcePressureMarker.load()
    # Stamped first: a pass the tick deadline kills must still consume its cadence.
    # NEVER last_freed_at: that field gates the DESTRUCTIVE ladder's anti-thrash
    # rate-limit, and a throttled CRITICAL band silently degrades to a WARN.
    marker.last_artifact_sweep_at = timezone.now()
    marker.save(update_fields=["last_artifact_sweep_at"])
    plan = FreePlan(resource="artifacts")
    eviction = _surveyed_artifacts(payload)
    _append_artifact_steps(plan, eviction)
    persist_plan(marker, plan, field_name="last_artifact_plan", caller="sweep_artifacts")
    # A refused survey has already said why in `plan.steps`; running the deletion half over
    # its empty candidate list re-reads the process table only to report the same refusal twice.
    outcome = EvictionOutcome() if eviction.refusal else evict_artifacts(eviction)
    plan.reclaimed_gb += outcome.freed_bytes / GIB
    append_stopped_deletions(plan, "artifact eviction", outcome.refusal, outcome.skipped)
    _drain_teardown_backlog(plan)
    _release_orphan_env_dirs(plan)
    persist_plan(marker, plan, field_name="last_artifact_plan", caller="sweep_artifacts")
    logger.info("sweep_artifacts reclaimed ~%.2f GB", plan.reclaimed_gb)


def _drain_teardown_backlog(plan: FreePlan) -> None:
    try:
        queued = TeardownDispatch.drain_terminal_backlog()
    except Exception:
        logger.exception("sweep_artifacts: teardown drain failed — swallowed")
        plan.steps.append("SKIP terminal-ticket teardown drain — the teardown drain failed (see logs)")
        return
    plan.steps.append(f"DRAIN terminal tickets' leftover worktrees: queued {len(queued)} teardown(s)")


def _release_orphan_env_dirs(plan: FreePlan) -> None:
    deadline = time.monotonic() + ENV_DIR_WALK_BUDGET_SECONDS
    try:
        outcomes = reap_orphan_isolated_worktree_roots(worktree_root(), deadline=deadline)
    except Exception:
        logger.exception("sweep_artifacts: orphan env-dir reap failed — swallowed")
        plan.steps.append("SKIP orphan env-dir release — the reaper raised (see logs)")
        return
    failed = tuple(line for line in outcomes if line.startswith("FAILED"))
    released = tuple(line for line in outcomes if not line.startswith(("KEPT", "FAILED")))
    kept = len(outcomes) - len(released) - len(failed)
    plan.steps.append(f"RELEASE orphan env dirs: {len(released)} released, {len(failed)} failed, {kept} kept")
    for line in sampled(released + failed):
        plan.steps.append(f"  {line}")


def _append_artifact_steps(plan: FreePlan, eviction: ArtifactEvictionPlan) -> None:
    for line in sampled(eviction.excluded):
        plan.steps.append(f"  venue scope: {line}")
    if eviction.refusal:
        plan.steps.append(f"SKIP artifact eviction — {eviction.refusal}")
        return
    plan.steps.append(
        f"EVICT dormant artifacts: considered={eviction.considered} evicting={len(eviction.candidates)} "
        f"kept={len(eviction.kept)} deferred={len(eviction.deferred)}"
    )
    for line in sampled(eviction.kept):
        plan.steps.append(f"  keep {line}")
    for line in sampled(eviction.deferred):
        plan.steps.append(f"  defer {line}")
    for gap in sampled(eviction.gaps):
        plan.steps.append(f"  ERROR checkout enumeration incomplete — {gap}")
    plan.estimated_reclaim_gb += eviction.estimated_bytes / GIB


def _surveyed_artifacts(payload: ActionPayload) -> ArtifactEvictionPlan:
    try:
        return plan_artifact_eviction(
            worktree_root(), idle_days=pressure_idle_days(payload), budget_seconds=ARTIFACT_PASS_BUDGET_SECONDS
        )
    except Exception as exc:
        logger.exception("sweep_artifacts: artifact survey failed — swallowed")
        return ArtifactEvictionPlan(refusal=f"the artifact survey raised ({exc})")


__all__ = ["ARTIFACT_PASS_BUDGET_SECONDS", "sweep_artifacts"]
