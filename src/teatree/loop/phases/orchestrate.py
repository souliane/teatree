"""``orchestrate_phase`` — the wip-driven autonomous fan-out (#1796).

This is the missing third job of a tick: *drive new work*. It reads the
:class:`~teatree.config.Wip` dial and the per-overlay
``max_concurrent_auto_starts`` cap and decides how many dispatchable Task
rows to admit this tick:

*   ``medium`` (default) — a NO-OP. Throughput stays exactly today's: the
    intrinsic loop, the PR sweep, and the per-scanner auto-start cap. No
    rows are claimed, an empty manifest is returned.
*   ``slow`` — at most one worker in flight: the target is 1.
*   ``full`` — compute a fan-out manifest of autonomous-safe claimable Task
    rows, targeting the summed ``max_concurrent_auto_starts`` budget across
    the scanned overlays.
*   ``boost`` — the pool-refill burst (PR-13). With ``boost_concurrency = N``
    configured, the target is ``N`` live workers, clamped by the PR-01 resource
    concurrency ceiling (``provision_max_concurrency`` when pinned, else
    ``default_provision_concurrency()``); with ``boost_concurrency = 0`` (unset)
    boost keeps ``full``'s summed-overlay target. Each tick recomputes
    ``cap = max(0, target - in_flight)``, so when a worker exits below ``N`` the
    next tick admits the shortfall — the pool refills back to ``N``.

The manifest carries two quantities: ``target`` is the standing live-worker
ceiling (what the live claimer's ``in_flight >= budget`` gate reads via the
admit-budget sidecar), and ``cap`` is the marginal number of rows THIS planner
pass admits (``target - in_flight``).

Rows are admitted in ADMISSION PRIORITY order (PR-13): a queued TODO/followup
drains before a brand-new-ticket auto-start at equal priority — the ordering
lives in :mod:`teatree.loop.queue_drain`.

Admission is a global FIFO over the dispatchable backlog (the same global
claim as ``claim_next_pending``), clamped to the *total* budget — it is a
machine-wide fan-out cap, not a per-overlay split. Per-overlay clamping is
a later-step refinement (#1796 steps 3-4); this PR keeps the dormant
``run_tick`` wiring (``claim=False``) so the distinction has no live effect.

It only **computes + claims + returns** the manifest — it never spawns;
that stays in the session / self-pump half. With ``claim=True`` it admits
rows through the existing claim-next compare-and-swap
(:meth:`TaskQuerySet.claim_next_pending`, the #786 boundary) so a row it
admits is never double-dispatched by a concurrent tick. The default
``claim=False`` is a read-only plan: it returns the same manifest of what
*would* be admitted without mutating any Task row, so wiring the phase
into ``run_tick`` is dormant — it cannot orphan a claim until the spawn
half opts into ``claim=True`` in a later step (#1796 steps 3-4).
"""

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from django.db.models import Q

from teatree.config import Wip, get_effective_settings
from teatree.core.modelkit.phases import normalize_phase, subagent_for_phase
from teatree.loop.queue_drain import ADMISSION_ORDER, admission_claim_order, admission_priority_annotations

if TYPE_CHECKING:
    from teatree.config import UserSettings
    from teatree.core.backend_factory import OverlayBackends
    from teatree.core.models.task import Task

logger = logging.getLogger(__name__)

#: Tasks nearer shipping merge first — a ticket about to land should not
#: wait behind one still in coding. Lower rank merges earlier.
_MERGE_ORDER_RANK: dict[str, int] = {
    "shipping": 0,
    "reviewing": 1,
    "testing": 2,
    "coding": 3,
}


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
    #: live claimer's ``in_flight >= budget`` gate — the pool-refill target.
    target: int = 0
    entries: list[ManifestEntry] = field(default_factory=list)
    merge_order: list[int] = field(default_factory=list)


def orchestrate_phase(
    *,
    backends: "list[OverlayBackends] | None" = None,
    claim: bool = False,
    claimed_by: str = "orchestrate-phase",
) -> OrchestrationManifest:
    from teatree.core.models.task import Task  # noqa: PLC0415 — deferred: ORM import needs the app registry

    settings = get_effective_settings()
    wip = settings.wip
    if wip is Wip.MEDIUM:
        return OrchestrationManifest(wip=wip, cap=0, target=0)

    if wip is Wip.SLOW:
        target = 1
        cap = 1
    else:
        target = _fanout_target(settings, wip, backends)
        cap = max(0, target - Task.objects.in_flight_claimed_count(_dispatchable_filter()))
    manifest = OrchestrationManifest(wip=wip, cap=cap, target=target)
    if cap <= 0:
        return manifest

    _admit_into(manifest, claim=claim, claimed_by=claimed_by)
    manifest.merge_order = [e.task_id for e in sorted(manifest.entries, key=_merge_rank)]
    return manifest


def _admit_into(manifest: OrchestrationManifest, *, claim: bool, claimed_by: str) -> None:
    """Fill the manifest with up to ``manifest.cap`` dispatchable rows.

    Rows are admitted in ADMISSION PRIORITY order (a queued TODO/followup before
    a brand-new-ticket auto-start) — the annotation + ordering live in
    :mod:`teatree.loop.queue_drain`. ``claim=True`` admits each row through the
    existing claim-next CAS (now priority-ordered); ``claim=False`` plans
    read-only, listing what would be admitted without mutating any Task row.
    Fail-open like every other tick phase: a DB-blocked harness or query error
    degrades to whatever was collected so far, never aborting the tick.
    """
    from teatree.core.models.task import Task  # noqa: PLC0415 — deferred: ORM import needs the app registry

    dispatchable = _dispatchable_filter()
    try:
        if claim:
            ordering = admission_claim_order()
            for _ in range(manifest.cap):
                task = Task.objects.claim_next_pending(
                    claimed_by=claimed_by,
                    extra_filter=dispatchable,
                    ordering=ordering,
                )
                if task is None:
                    break
                manifest.entries.append(_entry_for(task))
            return
        candidates = (
            Task.objects.filter(status=Task.Status.PENDING)
            .filter(dispatchable)
            .annotate(**admission_priority_annotations())
            .select_related("ticket")
            .order_by(*ADMISSION_ORDER)[: manifest.cap]
        )
        manifest.entries.extend(_entry_for(task) for task in candidates)
    except Exception:
        logger.exception("orchestrate_phase admit sweep failed — degrading to %d entries", len(manifest.entries))


def _fanout_target(settings: "UserSettings", wip: Wip, backends: "list[OverlayBackends] | None") -> int:
    """The standing live-worker target for a ``full``/``boost`` fan-out.

    ``full`` (and ``boost`` with ``boost_concurrency`` unset) targets the summed
    per-overlay ``max_concurrent_auto_starts``. ``boost`` with a positive
    ``boost_concurrency = N`` targets ``N``, clamped by the PR-01 resource
    concurrency ceiling so a burst never over-subscribes the host.
    """
    overlay_cap = (
        sum(max(0, backend.max_concurrent_auto_starts) for backend in backends)
        if backends is not None
        else max(0, _active_overlay_cap())
    )
    if wip is Wip.BOOST and settings.boost_concurrency > 0:
        return min(settings.boost_concurrency, _concurrency_ceiling(settings))
    return overlay_cap


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
