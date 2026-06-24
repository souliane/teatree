"""Scoped tick — fan ONE dedicated loop's members only (#1838 Track-A, #2584).

A scoped ``t3 loop tick --slot <name>`` runs the orchestrator filtered to
the dedicated loop's member mini-loops, instead of the fat ``run_tick``
that fans across ALL registered loops. The filter reuses the orchestrator's
existing ``registry_fn`` seam — there is no parallel dispatch path — so a
scoped tick is the same gate-then-dispatch the live tick uses, just over a
narrowed registry. The per-loop error isolation and summary-DM behaviour are
unchanged: only the *grouping* is new.

#2584 closes the inverse gate gap: ``run_scoped_tick`` is the per-loop runner
entry every ``Loop`` row references via ``run.py``, but it never read
``Loop.enabled``, so a scoped tick for a loop whose row is ``enabled=False``
still dispatched. The scoped registry now intersects its members with the
``enabled`` ``Loop`` rows BEFORE handing them to the orchestrator, so a scoped
tick reaches the same row-level verdict the master does. The orchestrator's
existing :class:`LoopsConfig` gate still layers the env → ``LoopState`` → default
control tier on top of the surviving members (#2702 removed the toml tier).
"""

import datetime as dt
from collections.abc import Callable
from typing import TYPE_CHECKING

from teatree.loops.base import MiniLoop
from teatree.loops.dedicated import member_names
from teatree.loops.orchestrator import Orchestrator, TickOutcome, TickRequest, _default_dispatch, _utc_clock
from teatree.loops.registry import iter_loops

if TYPE_CHECKING:
    from teatree.loop.dispatch import DispatchAction
    from teatree.loop.job_identity import _ScannerJob


def run_scoped_tick(
    slot_or_name: str,
    request: TickRequest,
    *,
    registry_fn: Callable[[], tuple[MiniLoop, ...]] = iter_loops,
    dispatch_fn: "Callable[[list[_ScannerJob]], list[DispatchAction]]" = _default_dispatch,
    clock: Callable[[], dt.datetime] = _utc_clock,
) -> TickOutcome:
    """Run one orchestrator tick scoped to the dedicated loop ``slot_or_name``.

    ``slot_or_name`` is the dedicated-loop name (``dispatch``) or its
    qualified owner slot (``loop:dispatch``) — resolved to the group's
    member mini-loop names. The orchestrator's registry is filtered to those
    members so ONLY they are gated + dispatched this tick; every other
    registered mini-loop is invisible to this scoped run.

    An unknown group (no members) returns an empty outcome without touching
    the dispatch path — the caller has already gated on ownership, so a
    stale ``--slot`` is a no-op tick, not an error.
    """
    members = frozenset(member_names(slot_or_name))
    started_at = clock()
    if not members:
        return TickOutcome(
            started_at=started_at,
            dispatched_loops=[],
            skipped_loops={},
            errors={},
            actions_count=0,
        )

    from teatree.core.models import Loop  # noqa: PLC0415
    from teatree.loops.config import LoopsConfig  # noqa: PLC0415

    # #2584: honour ``Loop.enabled`` — the inverse of the master gap. A member
    # whose Loop row is disabled (or has no enabled row) is excluded here, so a
    # scoped tick reaches the same row-level verdict the master does. The
    # orchestrator's LoopsConfig gate below still applies the env → LoopState →
    # default control tier to the surviving members (#2702 removed the toml tier).
    enabled_members = members & frozenset(
        Loop.objects.filter(name__in=members, enabled=True).values_list("name", flat=True)
    )

    def _scoped_registry() -> tuple[MiniLoop, ...]:
        return tuple(loop for loop in registry_fn() if loop.name in enabled_members)

    orchestrator = Orchestrator(
        config=LoopsConfig.load(),
        registry_fn=_scoped_registry,
        clock=clock,
        dispatch_fn=dispatch_fn,
    )
    return orchestrator.tick(request)
