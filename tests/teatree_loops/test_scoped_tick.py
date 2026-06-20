"""Scoped tick — run ONE dedicated loop's members only (#1838 Track-A).

``run_scoped_tick(<name>, request)`` builds an :class:`Orchestrator` whose
``registry_fn`` is filtered to the dedicated loop's member mini-loops, so a
scoped tick dispatches ONLY that group's loops — never the whole fat fan-out.
The dispatched set must be a subset of the group's declared members.
"""

import datetime as dt

from django.test import TestCase

from teatree.core.models import Loop, Prompt
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


def _prompt() -> Prompt:
    prompt, _ = Prompt.objects.get_or_create(name="scoped-prompt", defaults={"body": "do x"})
    return prompt


def _ensure_loop(name: str, *, enabled: bool) -> None:
    """Set *name*'s ``Loop`` row to *enabled* (migration 0078 may already seed it)."""
    Loop.objects.update_or_create(
        name=name,
        defaults={"delay_seconds": 60, "prompt": _prompt(), "script": "", "enabled": enabled, "last_run_at": None},
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
        # Migration 0078/0087 seeds these member rows paused; the scoped tick now
        # honours Loop.enabled (#2584), so enable them for the all-dispatch case.
        for m in members:
            _ensure_loop(m, enabled=True)
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
        for m in members:
            _ensure_loop(m, enabled=True)
        registry = tuple(_stub_loop(m) for m in members)
        outcome = run_scoped_tick(
            "loop:review",
            TickRequest(),
            registry_fn=lambda: registry,
            dispatch_fn=lambda jobs: [],
            clock=lambda: dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        )
        assert set(outcome.dispatched_loops) == set(members)


class TestScopedTickHonoursLoopEnabled(TestCase):
    """The scoped tick also gates on ``Loop.enabled`` (#2584 inverse gap).

    ``run_scoped_tick`` is the per-loop runner entry every ``Loop`` row
    references via ``run.py``. Before #2584 it never read ``Loop.enabled``, so a
    scoped tick for a loop whose row is ``enabled=False`` still dispatched —
    the inverse of the master gap. A member whose ``Loop`` row is disabled is
    now excluded from the scoped tick; a member with NO row is unaffected (the
    existing stub-only tests above have no rows and still dispatch).
    """

    def test_scoped_tick_skips_a_loop_row_disabled(self) -> None:
        members = member_names("followup")  # ("tickets", "followup")
        off, on = members[0], members[1]
        _ensure_loop(off, enabled=False)
        _ensure_loop(on, enabled=True)
        registry = tuple(_stub_loop(m) for m in members)
        outcome = run_scoped_tick(
            "followup",
            TickRequest(),
            registry_fn=lambda: registry,
            dispatch_fn=lambda jobs: [],
            clock=lambda: dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        )
        assert off not in outcome.dispatched_loops
        assert on in outcome.dispatched_loops

    def test_scoped_tick_dispatches_enabled_member(self) -> None:
        # The positive arm: an enabled member dispatches under the new gate, so
        # the Loop.enabled filter is not a blanket suppressor.
        members = member_names("review")
        for m in members:
            _ensure_loop(m, enabled=True)
        registry = tuple(_stub_loop(m) for m in members)
        outcome = run_scoped_tick(
            "review",
            TickRequest(),
            registry_fn=lambda: registry,
            dispatch_fn=lambda jobs: [],
            clock=lambda: dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        )
        assert set(outcome.dispatched_loops) == set(members)
