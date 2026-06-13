"""Dedicated-loop grouping — the single declarative source (#1838 Track-A).

A *dedicated loop* is a named GROUP of registered mini-loops driven by one
scoped ``/loop <cadence>`` slot (``t3 loop tick --slot <name>`` claiming the
per-loop owner key ``loop:<name>``). This module is the SINGLE place that
declares which mini-loop belongs to which group, the group's slot cadence,
and whether it is always-on.

The load-bearing invariant is TOTAL COVERAGE: every registered
:func:`teatree.loops.registry.iter_loops` name maps to EXACTLY one
dedicated group — no orphan, no double-assignment — so replacing the single
fat ``loop-owner`` slot (one ``run_tick`` fanning across ALL mini-loops)
with N dedicated slots drops nothing. The fitness test
(``tests/teatree_loops/test_dedicated.py``) enforces it; a newly-registered
mini-loop that is not added to exactly one group below turns that test RED.

The grouping rationale (cohesion by domain + compatible cadence). Each line
is ``<group> (<cadence>): <members> — <why>``:

dispatch (300s, always_on): dispatch + issue_implementer — issue_implementer
discovers + claims labelled issues and kicks off the same maker pipeline.
review (300s): review — the reviewer fan-out.
ship (300s): ship — the shipper fan-out.
inbox (60s): inbox — user-facing inbox lag is the most latency-sensitive.
followup (600s): tickets + followup — intake/disposition, not bursty.
housekeeping (3600s): news + audit + arch_review + housekeeping + dream +
dogfood + eval_local — the low-frequency periodic maintenance loops.
resource (60s): resource_pressure + idle_stack_reaper + local_stack_queue +
pane_reaper — host-resource / local-stack management; each carries its own
internal cadence and a 60s registry floor, a cohesive set distinct from inbox
and the slow housekeeping. ``pane_reaper`` (#1838 PR#7b) is the idle-maker-pane
sibling of ``idle_stack_reaper`` — both demote an idle resource; it is a no-op
until ``teams_enabled``.

This layer is OPT-IN behind the default-off ``[loops] dedicated_loops`` toggle;
under the default the single fat slot stays the driver and this map is inert.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from teatree.core.loop_lease_manager import per_loop_owner_slot


@dataclass(frozen=True, slots=True)
class DedicatedLoop:
    """One dedicated loop — a named group of mini-loops with one scoped slot.

    ``name`` is the dedicated-loop identity; it is qualified UP to the
    canonical per-loop owner key ``loop:<name>`` via :attr:`owner_slot`
    (the single normalization seam from #2318). ``members`` are the
    registered mini-loop names this group drives — the scoped tick runs
    ONLY these. ``cadence_seconds`` is the ``/loop <cadence>`` slot interval.
    ``always_on`` mirrors the mini-loop flag — reserved for the ``dispatch``
    group, whose factory feeder has no graceful-degradation path.
    """

    name: str
    members: tuple[str, ...]
    cadence_seconds: int
    always_on: bool = False

    @property
    def owner_slot(self) -> str:
        """The canonical per-loop owner key ``loop:<name>`` (#2318)."""
        return per_loop_owner_slot(self.name)


DEDICATED_LOOPS: tuple[DedicatedLoop, ...] = (
    DedicatedLoop(
        name="dispatch",
        members=("dispatch", "issue_implementer"),
        cadence_seconds=300,
        always_on=True,
    ),
    DedicatedLoop(name="review", members=("review",), cadence_seconds=300),
    DedicatedLoop(name="ship", members=("ship",), cadence_seconds=300),
    DedicatedLoop(name="inbox", members=("inbox",), cadence_seconds=60),
    DedicatedLoop(name="followup", members=("tickets", "followup"), cadence_seconds=600),
    DedicatedLoop(
        name="housekeeping",
        members=("news", "audit", "arch_review", "housekeeping", "dream", "dogfood", "eval_local"),
        cadence_seconds=3600,
    ),
    DedicatedLoop(
        name="resource",
        members=("resource_pressure", "idle_stack_reaper", "local_stack_queue", "pane_reaper"),
        cadence_seconds=60,
    ),
)


def iter_dedicated_loops() -> Iterable[DedicatedLoop]:
    """Iterate the declarative dedicated-loop map."""
    return DEDICATED_LOOPS


def dedicated_loop_by_name(name: str) -> DedicatedLoop | None:
    """The dedicated loop named ``name`` (bare or ``loop:``-qualified), or ``None``.

    Accepts either the bare group name (``dispatch``) or the qualified owner
    slot (``loop:dispatch``) so a caller resolving a ``--slot loop:<name>``
    argument back to its group never has to strip the prefix itself.
    """
    slot = per_loop_owner_slot(name)
    for dl in DEDICATED_LOOPS:
        if dl.owner_slot == slot:
            return dl
    return None


def member_names(name: str) -> tuple[str, ...]:
    """The mini-loop member names of dedicated loop ``name`` — ``()`` if unknown."""
    dl = dedicated_loop_by_name(name)
    return dl.members if dl is not None else ()
