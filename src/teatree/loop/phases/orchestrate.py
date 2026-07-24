"""``orchestrate_phase`` — the wip-driven autonomous fan-out, split by phase (#3634).

The third job of a tick: *drive new work*. It reads the
:class:`~teatree.config.Wip` dial and admits dispatchable Task rows into TWO
independently-budgeted lanes:

*   **WRITE** (coding / testing / reviewing) — parallel, ``write_wip`` wide.
*   **MERGE** (shipping) — single-flight, ``merge_wip`` clamped to 1, so the next
    PR always rebases against what just landed. This is the conflict-safety
    guarantee; a lane budget above 1 is clamped rather than honoured.

Because each lane counts its OWN in-flight rows, a saturated merge lane never
starves implementation, and a full WRITE fan-out never blocks the one merge.

The ``wip`` tier still governs the WRITE lane's ceiling:

*   ``medium`` (default) — a NO-OP. Throughput stays exactly today's: the
    intrinsic loop, the PR sweep, and the per-scanner auto-start cap.
*   ``slow`` — one implementation worker.
*   ``full`` — ``write_wip``, bounded by the summed per-overlay
    ``max_concurrent_auto_starts`` so an overlay's own cap is never exceeded.
*   ``boost`` — with ``boost_concurrency = N`` the WRITE target is ``N``, clamped
    by the PR-01 resource concurrency ceiling (``provision_max_concurrency`` when
    pinned, else ``default_provision_concurrency()``). Each tick recomputes the
    lane's shortfall, so when a worker exits the next tick refills the pool.

Within a lane, rows are admitted in ADMISSION PRIORITY order (a queued
TODO/followup drains before a brand-new-ticket auto-start; the ordering lives in
:mod:`teatree.loop.queue_drain`), then spread round-robin across their CHEAP area
key (:mod:`teatree.loop.phases.conflict_area`) so a fan-out prefers tickets in
different repos. The spread only re-orders — nothing is dropped.

It only **computes + claims + returns** the manifest — it never spawns; that stays
in the session / self-pump half. With ``claim=True`` rows are admitted through the
existing claim-next compare-and-swap (:meth:`TaskQuerySet.claim_next_pending`, the
#786 boundary), so a row it admits is never double-dispatched by a concurrent
tick. The default ``claim=False`` is a read-only plan.
"""

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.db.models import Q

from teatree.config import Wip, get_effective_settings
from teatree.core.modelkit.phases import normalize_phase, subagent_for_phase
from teatree.loop.phases.conflict_area import area_key, spread_by_area
from teatree.loop.queue_drain import ADMISSION_ORDER, admission_claim_order, admission_priority_annotations

if TYPE_CHECKING:
    from teatree.config import UserSettings
    from teatree.core.backend_factory import OverlayBackends
    from teatree.core.models.task import Task

logger = logging.getLogger(__name__)

#: How many extra candidates per lane slot the area spread sees. A spread over the
#: exact cap would only ever reorder within it; a small oversample lets a second
#: repo's ticket displace a same-repo one without paying for a full backlog scan.
_SPREAD_OVERSAMPLE = 4

#: Tasks nearer shipping merge first — a ticket about to land should not
#: wait behind one still in coding. Lower rank merges earlier.
_MERGE_ORDER_RANK: dict[str, int] = {
    "shipping": 0,
    "reviewing": 1,
    "testing": 2,
    "coding": 3,
}

#: The MERGE lane (#3634) — single-flight by design, so the next PR always
#: rebases against what just landed. Everything else is the WRITE lane, which
#: fans out to ``write_wip`` workers.
_MERGE_PHASES: frozenset[str] = frozenset({"shipping"})


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    task_id: int
    ticket_id: int
    phase: str
    subagent: str
    issue_url: str
    role: str
    overlay: str


@dataclass(slots=True)
class OrchestrationManifest:
    wip: Wip
    #: The marginal number of rows THIS planner pass admits (``target - in_flight``).
    cap: int
    #: The standing live-worker ceiling the admit-budget sidecar persists for the
    #: live claimer's ``in_flight >= budget`` gate — the pool-refill target. It is
    #: the SUM of the two lanes below.
    target: int = 0
    #: The #3634 phase split: implementation runs ``write_target`` wide, the merge
    #: lane stays single-flight at ``merge_target``.
    write_target: int = 0
    merge_target: int = 0
    entries: list[ManifestEntry] = field(default_factory=list)
    merge_order: list[int] = field(default_factory=list)


def orchestrate_phase(
    *,
    backends: "list[OverlayBackends] | None" = None,
    claim: bool = False,
    claimed_by: str = "orchestrate-phase",
) -> OrchestrationManifest:
    settings = get_effective_settings()
    wip = settings.wip
    if wip is Wip.MEDIUM:
        return OrchestrationManifest(wip=wip, cap=0, target=0)

    merge_target = merge_lane_target(settings)
    write_target = write_lane_target(settings, wip, backends)
    manifest = OrchestrationManifest(
        wip=wip,
        cap=0,
        target=write_target + merge_target,
        write_target=write_target,
        merge_target=merge_target,
    )
    _admit_lane(manifest, merge=True, target=merge_target, claim=claim, claimed_by=claimed_by)
    _admit_lane(manifest, merge=False, target=write_target, claim=claim, claimed_by=claimed_by)
    manifest.cap = len(manifest.entries)
    manifest.merge_order = [e.task_id for e in sorted(manifest.entries, key=_merge_rank)]
    return manifest


def merge_lane_target(settings: "UserSettings") -> int:
    """The MERGE lane ceiling — clamped to single-flight (#3634).

    A value above 1 would let two merges race, forfeiting the "next PR rebases
    against what just landed" guarantee, so it is clamped rather than honoured.
    """
    return min(max(0, settings.merge_wip), 1)


def write_lane_target(settings: "UserSettings", wip: Wip, backends: "list[OverlayBackends] | None") -> int:
    """The WRITE lane ceiling for this ``wip`` tier.

    ``slow`` pins one implementation worker. ``full`` takes the configured
    ``write_wip``, bounded by the summed per-overlay ``max_concurrent_auto_starts``
    so an overlay's own cap is never exceeded — a zero overlay cap therefore admits
    nothing, never the unclamped dial. ``boost`` with an explicit
    ``boost_concurrency = N`` targets ``N``, clamped by the PR-01 resource ceiling
    so a burst never over-subscribes the host.
    """
    if wip is Wip.SLOW:
        return 1
    if wip is Wip.BOOST and settings.boost_concurrency > 0:
        return min(settings.boost_concurrency, _concurrency_ceiling(settings))
    overlay_cap = (
        sum(max(0, backend.max_concurrent_auto_starts) for backend in backends)
        if backends is not None
        else max(0, _active_overlay_cap())
    )
    return min(max(0, settings.write_wip), overlay_cap)


def _admit_lane(
    manifest: OrchestrationManifest,
    *,
    merge: bool,
    target: int,
    claim: bool,
    claimed_by: str,
) -> None:
    """Fill one lane of the manifest up to its own live-worker *target*.

    Each lane counts its OWN in-flight rows, so a saturated merge lane never
    consumes the WRITE budget (and vice versa). Candidates are listed in ADMISSION
    PRIORITY order and then spread round-robin across their cheap area key
    (:mod:`teatree.loop.phases.conflict_area`), so a WRITE fan-out prefers tickets
    in different repos before taking a second one in the same repo.

    ``claim=True`` admits each row through the existing claim-next CAS, narrowed to
    that row's pk so the spread ordering survives; ``claim=False`` plans read-only.
    Fail-open like every other tick phase: a DB-blocked harness or query error
    degrades to whatever was collected so far, never aborting the tick.
    """
    from teatree.core.models.task import Task  # noqa: PLC0415 — deferred: ORM import needs the app registry

    lane = _lane_filter(merge=merge)
    try:
        cap = max(0, target - Task.objects.in_flight_claimed_count(lane))
        if cap <= 0:
            return
        candidates = (
            Task.objects.filter(status=Task.Status.PENDING)
            .filter(lane)
            .annotate(**admission_priority_annotations())
            .select_related("ticket")
            .order_by(*ADMISSION_ORDER)[: cap * _SPREAD_OVERSAMPLE]
        )
        ordered = spread_by_area(list(candidates), key=_task_area_key)[:cap]
        for task in ordered:
            if not claim:
                manifest.entries.append(_entry_for(task))
                continue
            admitted = Task.objects.claim_next_pending(
                claimed_by=claimed_by,
                extra_filter=lane & Q(pk=task.pk),
                ordering=admission_claim_order(),
            )
            if admitted is not None:
                manifest.entries.append(_entry_for(admitted))
    except Exception:
        logger.exception("orchestrate_phase admit sweep failed — degrading to %d entries", len(manifest.entries))


def _lane_filter(*, merge: bool) -> Q:
    """The dispatchable filter narrowed to one lane's phases."""
    merge_phases = Q(phase__in=sorted(_MERGE_PHASES))
    return _dispatchable_filter() & (merge_phases if merge else ~merge_phases)


def _task_area_key(task: "Task") -> str:
    ticket = task.ticket
    return area_key(repos=ticket.repos or [], issue_url=ticket.issue_url)


def _concurrency_ceiling(settings: "UserSettings") -> int:
    """The PR-01 resource-aware concurrency ceiling (nCPU-derived unless pinned).

    An explicit ``provision_max_concurrency > 0`` pins the ceiling; ``0`` (the
    default) auto-derives from the host via ``default_provision_concurrency()``.
    Shared with parallel worktree provisioning so a boost burst and a cold
    provision honour the same machine-wide bound.
    """
    from teatree.utils.ram_probe import default_provision_concurrency  # noqa: PLC0415 — deferred: loaded at tick time

    if settings.provision_max_concurrency > 0:
        return settings.provision_max_concurrency
    return default_provision_concurrency()


def _active_overlay_cap() -> int:
    from teatree.core.backend_factory import iter_overlay_backends  # noqa: PLC0415 — deferred: loaded at tick time

    return sum(max(0, backend.max_concurrent_auto_starts) for backend in iter_overlay_backends())


def _dispatchable_filter() -> Q:
    """The dispatchable-Task filter — the ``Task.dispatchable_q`` SSOT (#6).

    Role/phase pairs with a registered sub-agent AND not under a live #2104
    external-delivery lease (#2217). The planner counts in-flight and admits
    through this WITHOUT any ``execution_target`` narrowing, so a headless
    in-flight claim consumes the boost budget exactly as an interactive one does
    (``loop_dispatch`` gates its live claim on the same count).
    """
    from teatree.core.models.task import Task  # noqa: PLC0415 — deferred, matches this module's other Task imports

    return Task.dispatchable_q()


def _entry_for(task: "Task") -> ManifestEntry:
    ticket = task.ticket
    return ManifestEntry(
        task_id=int(task.pk),
        ticket_id=int(ticket.pk),
        phase=task.phase,
        subagent=subagent_for_phase(ticket.role, task.phase),
        issue_url=ticket.issue_url,
        role=ticket.role,
        overlay=ticket.overlay,
    )


def _merge_rank(entry: ManifestEntry) -> tuple[int, int]:
    return _MERGE_ORDER_RANK.get(normalize_phase(entry.phase), len(_MERGE_ORDER_RANK)), entry.task_id
