"""The dormant-artifact sweep — the loss-free reclaim, off the destructive ladder (#4244).

Its own module because it is its own pass. It has its own scanner
(:class:`~teatree.loop.scanners.artifact_eviction.ArtifactEvictionScanner`), its own
cadence, its own marker fields, and — the reason that matters — its own authority:
nothing it removes is destructive at any fullness, so it is gated on neither the
disk-CRIT band nor ``allow_destructive_disk``. Sharing a module with the ladder is what
made both of those couplings look natural.

What it removes and what protects it is :mod:`teatree.core.cleanup.artifact_eviction`;
this module is the loop-facing wrapper that plans, records, executes and re-records.
"""

import logging

from django.utils import timezone

from teatree.config import worktree_root
from teatree.core.cleanup.artifact_eviction import (
    ArtifactEvictionPlan,
    EvictionOutcome,
    evict_artifacts,
    plan_artifact_eviction,
)
from teatree.loop.dispatch import ActionPayload
from teatree.loop.mechanical_plan import GIB, FreePlan, append_stopped_deletions, persist_plan, sampled
from teatree.loop.reclaim_yield import pressure_idle_days

logger = logging.getLogger(__name__)


def sweep_artifacts(payload: ActionPayload) -> None:
    """Reclaim dormant build artifacts — the loss-free pass, off the destructive ladder (#4244)."""
    try:
        _sweep_artifacts_inner(payload)
    except Exception:
        logger.exception("sweep_artifacts: pass failed — swallowed to protect the tick")


def _sweep_artifacts_inner(payload: ActionPayload) -> None:
    from teatree.core.models.resource_pressure_marker import ResourcePressureMarker  # noqa: PLC0415 — lazy ORM import

    plan = FreePlan(resource="artifacts")
    eviction = _surveyed_artifacts(payload)
    _append_artifact_steps(plan, eviction)
    marker = ResourcePressureMarker.load()
    persist_plan(marker, plan, field_name="last_artifact_plan", caller="sweep_artifacts")
    # A refused survey has already said why in `plan.steps`; running the deletion half over
    # its empty candidate list re-reads the process table only to report the same refusal twice.
    outcome = EvictionOutcome() if eviction.refusal else evict_artifacts(eviction)
    plan.reclaimed_gb += outcome.freed_bytes / GIB
    append_stopped_deletions(plan, "artifact eviction", outcome.refusal, outcome.skipped)
    persist_plan(marker, plan, field_name="last_artifact_plan", caller="sweep_artifacts")
    # NEVER last_freed_at: that field gates the DESTRUCTIVE ladder's anti-thrash
    # rate-limit, and a throttled CRITICAL band silently degrades to a WARN.
    marker.last_artifact_sweep_at = timezone.now()
    marker.save(update_fields=["last_artifact_sweep_at"])
    logger.info("sweep_artifacts reclaimed ~%.2f GB", plan.reclaimed_gb)


def _append_artifact_steps(plan: FreePlan, eviction: ArtifactEvictionPlan) -> None:
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
        return plan_artifact_eviction(worktree_root(), idle_days=pressure_idle_days(payload))
    except Exception as exc:
        logger.exception("sweep_artifacts: artifact survey failed — swallowed")
        return ArtifactEvictionPlan(refusal=f"the artifact survey raised ({exc})")


__all__ = ["sweep_artifacts"]
