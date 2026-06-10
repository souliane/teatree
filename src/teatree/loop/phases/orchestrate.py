"""``orchestrate_phase`` — the speed-driven autonomous fan-out (#1796).

This is the missing third job of a tick: *drive new work*. It reads the
:class:`~teatree.config.Speed` dial and the per-overlay
``max_concurrent_auto_starts`` cap and decides how many dispatchable Task
rows to admit this tick:

*   ``medium`` (default) — a NO-OP. Throughput stays exactly today's: the
    intrinsic loop, the PR sweep, and the per-scanner auto-start cap. No
    rows are claimed, an empty manifest is returned.
*   ``slow`` — at most one worker in flight: the cap is clamped to 1.
*   ``full`` / ``boost`` — compute a fan-out manifest of autonomous-safe
    claimable Task rows, clamped to the summed ``max_concurrent_auto_starts``
    budget across the scanned overlays.

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

from teatree.config import Speed, get_effective_settings
from teatree.core.phases import SUBAGENT_BY_PHASE, normalize_phase, phase_spellings, subagent_for_phase

if TYPE_CHECKING:
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
    speed: Speed
    cap: int
    entries: list[ManifestEntry] = field(default_factory=list)
    merge_order: list[int] = field(default_factory=list)


def orchestrate_phase(
    *,
    backends: "list[OverlayBackends] | None" = None,
    claim: bool = False,
    claimed_by: str = "orchestrate-phase",
) -> OrchestrationManifest:
    speed = get_effective_settings().speed
    if speed is Speed.MEDIUM:
        return OrchestrationManifest(speed=speed, cap=0)

    cap = 1 if speed is Speed.SLOW else _fanout_budget(backends)
    manifest = OrchestrationManifest(speed=speed, cap=cap)
    if cap <= 0:
        return manifest

    _admit_into(manifest, claim=claim, claimed_by=claimed_by)
    manifest.merge_order = [e.task_id for e in sorted(manifest.entries, key=_merge_rank)]
    return manifest


def _admit_into(manifest: OrchestrationManifest, *, claim: bool, claimed_by: str) -> None:
    """Fill the manifest with up to ``manifest.cap`` dispatchable rows.

    ``claim=True`` admits each row through the existing claim-next CAS;
    ``claim=False`` plans read-only, listing what would be admitted without
    mutating any Task row. Fail-open like every other tick phase: a
    DB-blocked harness or query error degrades to whatever was collected
    so far, never aborting the tick.
    """
    from teatree.core.models.task import Task  # noqa: PLC0415

    dispatchable = _dispatchable_filter()
    try:
        if claim:
            for _ in range(manifest.cap):
                task = Task.objects.claim_next_pending(claimed_by=claimed_by, extra_filter=dispatchable)
                if task is None:
                    break
                manifest.entries.append(_entry_for(task))
            return
        candidates = (
            Task.objects.filter(status=Task.Status.PENDING)
            .filter(dispatchable)
            .select_related("ticket")
            .order_by("pk")[: manifest.cap]
        )
        manifest.entries.extend(_entry_for(task) for task in candidates)
    except Exception:
        logger.exception("orchestrate_phase admit sweep failed — degrading to %d entries", len(manifest.entries))


def _fanout_budget(backends: "list[OverlayBackends] | None") -> int:
    from teatree.core.models.task import Task  # noqa: PLC0415

    raw_cap = (
        sum(max(0, backend.max_concurrent_auto_starts) for backend in backends)
        if backends is not None
        else max(0, _active_overlay_cap())
    )
    in_flight = Task.objects.in_flight_claimed_count(_dispatchable_filter())
    return max(0, raw_cap - in_flight)


def _active_overlay_cap() -> int:
    from teatree.core.backend_factory import iter_overlay_backends  # noqa: PLC0415

    return sum(max(0, backend.max_concurrent_auto_starts) for backend in iter_overlay_backends())


def _dispatchable_filter() -> Q:
    from teatree.core.models.external_delivery import not_under_external_delivery_q  # noqa: PLC0415

    q = Q(pk__in=[])
    for role, phase in SUBAGENT_BY_PHASE:
        q |= Q(ticket__role=role, phase__in=phase_spellings(phase))
    # #2217: a unit under a live #2104 external-delivery lease is being
    # hand-delivered; exclude EVERY phase on it so the loop never claims a
    # second coder/reviewer the directly-implementing owner will never consume.
    return q & not_under_external_delivery_q()


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
