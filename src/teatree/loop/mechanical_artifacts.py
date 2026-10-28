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
import os
import time
from typing import TYPE_CHECKING

from django.utils import timezone

from teatree.config import worktree_root
from teatree.core.cleanup.artifact_eviction import (
    ArtifactEvictionPlan,
    EvictionOutcome,
    evict_artifacts,
    plan_artifact_eviction,
)
from teatree.core.cleanup.isolated_roots import reap_idle_isolated_databases, reap_orphan_isolated_worktree_roots
from teatree.core.cleanup.reclaim_pressure import effective_disk_floor_gb
from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.notify import notify_user
from teatree.core.notify_types import NotifyKind
from teatree.core.retention.scratch import sweep_scratch
from teatree.core.tasks import TeardownDispatch
from teatree.loop.dispatch import ActionPayload
from teatree.loop.mechanical_plan import GIB, FreePlan, append_stopped_deletions, persist_plan, sampled
from teatree.loop.reclaim_yield import pressure_idle_days
from teatree.loop.scanners.resource_pressure import measure_resources

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from teatree.core.models.resource_pressure_marker import ResourcePressureMarker

#: The whole pass — walk, sizing, re-walks, deletions — ends inside this, well under the
#: resource_pressure tick's 300 s deadline, so it finishes as a recorded partial run.
ARTIFACT_PASS_BUDGET_SECONDS = 120.0
#: The orphan env-dir reaper's checkout walk; together with the pass above, inside the 300 s tick.
ENV_DIR_WALK_BUDGET_SECONDS = 60.0


def sweep_artifacts(payload: ActionPayload) -> None:
    """Reclaim dormant build artifacts — the loss-free pass, off the destructive ladder (#4244)."""
    from teatree.core.models.resource_pressure_marker import ResourcePressureMarker  # noqa: PLC0415 — lazy ORM import

    marker = ResourcePressureMarker.load()
    observed_free = payload.get("free_gb", marker.last_disk_free_gb)
    before = measure_resources() if observed_free is not None else None
    free_before = before.disk_free_gb if before is not None and not before.disk_probe_degraded else None
    requested = payload.get("budget_seconds")
    budget = (
        min(float(requested), ARTIFACT_PASS_BUDGET_SECONDS + ENV_DIR_WALK_BUDGET_SECONDS)
        if isinstance(requested, int | float)
        else ARTIFACT_PASS_BUDGET_SECONDS + ENV_DIR_WALK_BUDGET_SECONDS
    )
    deadline = time.monotonic() + budget
    # Stamped first: a pass the tick deadline kills must still consume its cadence.
    # NEVER last_freed_at: that field gates the DESTRUCTIVE ladder's anti-thrash
    # rate-limit, and a throttled CRITICAL band silently degrades to a WARN.
    marker.last_artifact_sweep_at = timezone.now()
    marker.save(update_fields=["last_artifact_sweep_at"])
    plan = FreePlan(resource="artifacts")
    eviction = _surveyed_artifacts(payload, budget_seconds=min(ARTIFACT_PASS_BUDGET_SECONDS, budget * 2 / 3))
    _append_artifact_steps(plan, eviction)
    persist_plan(marker, plan, field_name="last_artifact_plan", caller="sweep_artifacts")
    # A refused survey has already said why in `plan.steps`; running the deletion half over
    # its empty candidate list re-reads the process table only to report the same refusal twice.
    outcome = EvictionOutcome() if eviction.refusal else evict_artifacts(eviction)
    plan.reclaimed_gb += outcome.freed_bytes / GIB
    append_stopped_deletions(plan, "artifact eviction", outcome.refusal, outcome.skipped)
    persist_plan(marker, plan, field_name="last_artifact_plan", caller="sweep_artifacts")
    if time.monotonic() < deadline:
        _release_idle_database_copies(plan, deadline=min(deadline, time.monotonic() + 30))
        persist_plan(marker, plan, field_name="last_artifact_plan", caller="sweep_artifacts")
    else:
        plan.steps.append("DEFER idle database copies — pass budget exhausted")
    if time.monotonic() < deadline:
        _sweep_worker_scratch(plan, deadline=deadline)
        persist_plan(marker, plan, field_name="last_artifact_plan", caller="sweep_artifacts")
    else:
        plan.steps.append("DEFER worker scratch — pass budget exhausted")
    if time.monotonic() < deadline:
        _drain_teardown_backlog(plan)
        persist_plan(marker, plan, field_name="last_artifact_plan", caller="sweep_artifacts")
    else:
        plan.steps.append("DEFER terminal-ticket teardown drain — pass budget exhausted")
    if time.monotonic() < deadline:
        _release_orphan_env_dirs(plan, deadline=min(deadline, time.monotonic() + ENV_DIR_WALK_BUDGET_SECONDS))
    else:
        plan.steps.append("DEFER orphan env dirs — pass budget exhausted")
    persist_plan(marker, plan, field_name="last_artifact_plan", caller="sweep_artifacts")
    if observed_free is not None:
        _record_reclaim_status(marker, plan, free_before=free_before, payload=payload)
    logger.info("sweep_artifacts reclaimed ~%.2f GB", plan.reclaimed_gb)


def _record_reclaim_status(
    marker: "ResourcePressureMarker", plan: FreePlan, *, free_before: float | None, payload: ActionPayload
) -> None:
    reading = measure_resources()
    free_after = reading.disk_free_gb
    if free_after is None or reading.disk_probe_degraded:
        plan.steps.append("NEEDS OWNER: free disk could not be measured after reclaim")
        persist_plan(marker, plan, field_name="last_artifact_plan", caller="sweep_artifacts")
        return
    marker.last_reclaimed_gb = max(free_after - free_before, 0.0) if free_before is not None else plan.reclaimed_gb
    configured = payload.get("disk_crit_free_gb")
    floor = effective_disk_floor_gb(float(configured) if isinstance(configured, int | float) else 10.0)
    if free_after >= floor:
        marker.reclaim_blocked_since = None
        marker.last_reclaim_notice_at = None
        marker.save(update_fields=["last_reclaimed_gb", "reclaim_blocked_since", "last_reclaim_notice_at"])
        return
    blocked_since = marker.reclaim_blocked_since or timezone.now()
    marker.reclaim_blocked_since = blocked_since
    marker.save(update_fields=["last_reclaimed_gb", "reclaim_blocked_since"])
    if marker.last_reclaim_notice_at is not None:
        return
    reasons = [
        line.strip()
        for line in plan.steps
        if "REFUSED" in line or "NEEDS OWNER" in line or "SKIP artifact eviction" in line
    ]
    detail = "; ".join(reasons[:3]) if reasons else "protected or unproven items need review"
    message = (
        f"Disk reclaim left {free_after:.1f} GB free below the {floor:.1f} GB build floor "
        f"(last reclaimed {marker.last_reclaimed_gb:.2f} GB). "
        f"Reasons: {detail}. Can you restore the blocked probes or add space?"
    )
    delivered = notify_user(
        message,
        kind=NotifyKind.QUESTION,
        idempotency_key=f"resource-reclaim-blocked:{blocked_since.isoformat()}",
        audience=NotifyAudience.OWNER_QUESTION,
    )
    if delivered:
        marker.last_reclaim_notice_at = timezone.now()
        marker.save(update_fields=["last_reclaim_notice_at"])


def _release_idle_database_copies(plan: FreePlan, *, deadline: float) -> None:
    try:
        outcomes = reap_idle_isolated_databases(worktree_root(), deadline=deadline)
    except Exception:
        logger.exception("sweep_artifacts: idle database reclaim failed")
        plan.steps.append("FAILED idle database reclaim — see logs")
        return
    plan.steps.append(f"IDLE database copies: {sum(line.startswith('RECLAIMED') for line in outcomes)} reclaimed")
    plan.steps.extend(sampled(tuple(outcomes)))


def _sweep_worker_scratch(plan: FreePlan, *, deadline: float) -> None:
    if os.environ.get("TEATREE_ROLE") != "worker":
        return
    for root in ("/tmp", "/var/tmp"):  # noqa: S108 — the worker's two fixed scratch roots
        if time.monotonic() >= deadline:
            plan.steps.append(f"DEFER worker scratch {root} — pass budget exhausted")
            break
        try:
            result = sweep_scratch(configured_root=root, retention_days=1 / 6, apply=True, deadline=deadline)
        except Exception:
            logger.exception("sweep_artifacts: worker scratch sweep failed at %s", root)
            plan.steps.append(f"FAILED worker scratch {root} — see logs")
            continue
        plan.reclaimed_gb += result.reclaimed_bytes / GIB
        plan.steps.append(f"worker scratch {result.summary}")
        if result.refused:
            plan.steps.append(f"NEEDS OWNER: worker scratch {root} could not be checked — {result.probe_gap}")


def _drain_teardown_backlog(plan: FreePlan) -> None:
    try:
        queued = TeardownDispatch.drain_terminal_backlog()
    except Exception:
        logger.exception("sweep_artifacts: teardown drain failed — swallowed")
        plan.steps.append("SKIP terminal-ticket teardown drain — the teardown drain failed (see logs)")
        return
    plan.steps.append(f"DRAIN terminal tickets' leftover worktrees: queued {len(queued)} teardown(s)")


def _release_orphan_env_dirs(plan: FreePlan, *, deadline: float) -> None:
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


def _surveyed_artifacts(
    payload: ActionPayload, *, budget_seconds: float = ARTIFACT_PASS_BUDGET_SECONDS
) -> ArtifactEvictionPlan:
    try:
        return plan_artifact_eviction(
            worktree_root(), idle_days=pressure_idle_days(payload), budget_seconds=budget_seconds
        )
    except Exception as exc:
        logger.exception("sweep_artifacts: artifact survey failed — swallowed")
        return ArtifactEvictionPlan(refusal=f"the artifact survey raised ({exc})")


__all__ = ["ARTIFACT_PASS_BUDGET_SECONDS", "sweep_artifacts"]
