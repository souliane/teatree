"""Dedicated-loop grouping — the single declarative map (#1838 Track-A).

A dedicated loop is a named GROUP of registered mini-loops driven by one
scoped `/loop <cadence>` slot. The map in :mod:`teatree.loops.dedicated`
is the single source of which mini-loop belongs to which group. The
coverage fitness test is the load-bearing invariant: every registered
``iter_loops()`` name maps to EXACTLY one dedicated group — no orphan, no
double-assignment — so the fat loop can be split with nothing dropped.
"""

from teatree.core.loop_lease_manager import per_loop_owner_slot
from teatree.loops.dedicated import (
    DEDICATED_LOOPS,
    DedicatedLoop,
    dedicated_loop_by_name,
    iter_dedicated_loops,
    member_names,
)
from teatree.loops.registry import iter_loops


class TestDedicatedLoopMap:
    def test_every_entry_is_a_dedicated_loop(self) -> None:
        for dl in DEDICATED_LOOPS:
            assert isinstance(dl, DedicatedLoop)

    def test_names_are_unique(self) -> None:
        names = [dl.name for dl in DEDICATED_LOOPS]
        assert len(names) == len(set(names))

    def test_each_group_has_at_least_one_member(self) -> None:
        for dl in DEDICATED_LOOPS:
            assert dl.members, f"dedicated loop {dl.name!r} has no members"

    def test_owner_slot_is_per_loop_namespaced(self) -> None:
        for dl in DEDICATED_LOOPS:
            assert dl.owner_slot == per_loop_owner_slot(dl.name)
            assert dl.owner_slot.startswith("loop:")

    def test_dispatch_group_is_always_on(self) -> None:
        dispatch = dedicated_loop_by_name("dispatch")
        assert dispatch is not None
        assert dispatch.always_on is True

    def test_cadences_are_positive(self) -> None:
        for dl in DEDICATED_LOOPS:
            assert dl.cadence_seconds > 0


class TestCoverageFitness:
    """The load-bearing invariant — total coverage, no orphan, no double."""

    def test_every_registered_loop_maps_to_exactly_one_group(self) -> None:
        registered = {loop.name for loop in iter_loops()}
        assignment: dict[str, list[str]] = {}
        for dl in DEDICATED_LOOPS:
            for member in dl.members:
                assignment.setdefault(member, []).append(dl.name)

        # No mini-loop assigned to two groups.
        doubled = {m: groups for m, groups in assignment.items() if len(groups) > 1}
        assert not doubled, f"mini-loops assigned to >1 dedicated group: {doubled}"

        mapped = set(assignment)
        # No member that is not a registered mini-loop (stale map entry).
        stale = mapped - registered
        assert not stale, f"dedicated map references unregistered mini-loops: {stale}"

        # Every registered mini-loop is covered by some group.
        orphans = registered - mapped
        assert not orphans, f"registered mini-loops mapped to NO dedicated group: {orphans}"

    def test_member_names_returns_group_members(self) -> None:
        dispatch = dedicated_loop_by_name("dispatch")
        assert dispatch is not None
        assert set(member_names("dispatch")) == set(dispatch.members)

    def test_member_names_unknown_group_is_empty(self) -> None:
        assert member_names("no-such-group") == ()


class TestIterDedicatedLoops:
    def test_returns_the_map(self) -> None:
        assert tuple(iter_dedicated_loops()) == DEDICATED_LOOPS

    def test_lookup_unknown_is_none(self) -> None:
        assert dedicated_loop_by_name("no-such-group") is None
