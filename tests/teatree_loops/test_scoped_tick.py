"""Scoped tick — run ONE dedicated loop's members only (#1838 Track-A).

``run_scoped_tick(<name>, request)`` builds an :class:`Orchestrator` whose
``registry_fn`` is filtered to the dedicated loop's member mini-loops, so a
scoped tick dispatches ONLY that group's loops — never the whole fat fan-out.
The dispatched set must be a subset of the group's declared members.
"""

import datetime as dt

from django.test import TestCase

from teatree.loops.base import MiniLoop
from teatree.loops.dedicated import member_names
from teatree.loops.orchestrator import TickRequest
from teatree.loops.scoped_tick import run_scoped_tick


def _stub_loop(name: str, *, always_on: bool = False) -> MiniLoop:
    return MiniLoop(
        name=name,
        default_cadence_seconds=300,
        build_jobs=lambda **_: [],
        always_on=always_on,
    )


class TestRunScopedTick(TestCase):
    def test_unknown_group_returns_empty_outcome(self) -> None:
        outcome = run_scoped_tick("no-such-group", TickRequest())
        assert outcome.dispatched_loops == []

    def test_dispatches_only_group_members(self) -> None:
        # A registry that contains every mini-loop across all groups plus an
        # extra loop in NO group. The scoped "dispatch" tick must dispatch a
        # subset of the dispatch group's members and nothing else.
        members = member_names("dispatch")
        registry = (
            *(_stub_loop(m, always_on=(m == "dispatch")) for m in members),
            _stub_loop("review"),  # belongs to the review group, not dispatch
            _stub_loop("housekeeping"),  # belongs to housekeeping, not dispatch
        )

        captured: list = []

        def _dispatch_fn(jobs: list) -> list:
            captured.append(jobs)
            return []

        outcome = run_scoped_tick(
            "dispatch",
            TickRequest(),
            registry_fn=lambda: registry,
            dispatch_fn=_dispatch_fn,
            clock=lambda: dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        )

        assert set(outcome.dispatched_loops) <= set(members)
        assert "review" not in outcome.dispatched_loops
        assert "housekeeping" not in outcome.dispatched_loops

    def test_member_only_registry_dispatches_all_members(self) -> None:
        members = member_names("dispatch")
        registry = tuple(_stub_loop(m, always_on=(m == "dispatch")) for m in members)
        outcome = run_scoped_tick(
            "dispatch",
            TickRequest(),
            registry_fn=lambda: registry,
            dispatch_fn=lambda jobs: [],
            clock=lambda: dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        )
        assert set(outcome.dispatched_loops) == set(members)

    def test_qualified_slot_name_resolves_to_group(self) -> None:
        members = member_names("review")
        registry = tuple(_stub_loop(m) for m in members)
        outcome = run_scoped_tick(
            "loop:review",
            TickRequest(),
            registry_fn=lambda: registry,
            dispatch_fn=lambda jobs: [],
            clock=lambda: dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        )
        assert set(outcome.dispatched_loops) == set(members)
