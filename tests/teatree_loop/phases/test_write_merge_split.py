"""The WRITE-parallel / MERGE-serial phase split on the ``wip`` dial (#3634).

Implementation fans out to ``write_wip`` workers; the merge lane stays
single-flight so the next PR always rebases against what just landed.
"""

from operator import itemgetter

import pytest
from django.test import TestCase

from teatree.config import UserSettings, Wip
from teatree.core.backend_factory import OverlayBackends
from teatree.core.models import ConfigSetting, Session, Task, Ticket
from teatree.loop.phases.conflict_area import area_key, spread_by_area
from teatree.loop.phases.orchestrate import merge_lane_target, orchestrate_phase, write_lane_target


def _backends() -> list[OverlayBackends]:
    return [OverlayBackends(name="acme", max_concurrent_auto_starts=8)]


def _task(phase: str, *, repos: list[str] | None = None, url: str = "") -> Task:
    ticket = Ticket.objects.create(
        overlay="acme",
        issue_url=url or f"https://github.com/souliane/teatree/issues/{Ticket.objects.count() + 1}",
        role=Ticket.Role.AUTHOR,
        repos=repos or ["souliane/teatree"],
    )
    session = Session.objects.create(ticket=ticket, agent_id="t3:coder")
    return Task.objects.create(ticket=ticket, session=session, phase=phase, status=Task.Status.PENDING)


class TestPhaseSplitTargets(TestCase):
    def setUp(self) -> None:
        ConfigSetting.objects.set_value("wip", Wip.FULL.value)
        ConfigSetting.objects.set_value("write_wip", 3)
        ConfigSetting.objects.set_value("merge_wip", 1)

    def test_merge_lane_admits_at_most_one_shipping_task(self) -> None:
        for _ in range(4):
            _task("shipping")

        manifest = orchestrate_phase(backends=_backends())

        assert manifest.merge_target == 1
        assert len([e for e in manifest.entries if e.phase == "shipping"]) == 1

    def test_write_lane_admits_up_to_write_wip(self) -> None:
        for _ in range(6):
            _task("coding")

        manifest = orchestrate_phase(backends=_backends())

        assert manifest.write_target == 3
        assert len([e for e in manifest.entries if e.phase == "coding"]) == 3

    def test_merge_and_write_lanes_are_budgeted_independently(self) -> None:
        for _ in range(3):
            _task("shipping")
        for _ in range(3):
            _task("coding")

        manifest = orchestrate_phase(backends=_backends())

        phases = [e.phase for e in manifest.entries]
        assert phases.count("shipping") == 1
        assert phases.count("coding") == 3


class TestLaneTargets:
    """The two lane ceilings, resolved without touching the DB."""

    def test_merge_lane_is_clamped_to_single_flight(self) -> None:
        assert merge_lane_target(UserSettings(merge_wip=5)) == 1

    def test_merge_lane_can_be_closed_entirely(self) -> None:
        assert merge_lane_target(UserSettings(merge_wip=0)) == 0

    def test_write_lane_is_bounded_by_the_summed_overlay_cap(self) -> None:
        settings = UserSettings(wip=Wip.FULL, write_wip=5)
        backends = [OverlayBackends(name="a", max_concurrent_auto_starts=2)]

        assert write_lane_target(settings, Wip.FULL, backends) == 2

    def test_slow_pins_the_write_lane_to_one(self) -> None:
        assert write_lane_target(UserSettings(wip=Wip.SLOW, write_wip=9), Wip.SLOW, _backends()) == 1


class TestConflictAreaHeuristic:
    """A CHEAP area key + spread — deliberately not conflict prediction."""

    def test_area_key_prefers_the_declared_repos(self) -> None:
        assert area_key(repos=["souliane/teatree"], issue_url="https://x/y/z/issues/1") == "souliane/teatree"

    def test_area_key_falls_back_to_the_issue_repo_slug(self) -> None:
        assert area_key(repos=[], issue_url="https://github.com/souliane/teatree/issues/1") == "souliane/teatree"

    def test_multi_repo_ticket_keys_on_the_sorted_repo_set(self) -> None:
        assert area_key(repos=["b/two", "a/one"], issue_url="") == "a/one+b/two"

    @pytest.mark.parametrize("empty", [[], None])
    def test_unknowable_area_is_its_own_bucket(self, empty: list[str] | None) -> None:
        assert area_key(repos=empty or [], issue_url="") == ""

    def test_spread_round_robins_across_areas(self) -> None:
        items = [("a", 1), ("a", 2), ("b", 3), ("a", 4), ("c", 5)]

        assert [n for _, n in spread_by_area(items, key=itemgetter(0))] == [1, 3, 5, 2, 4]

    def test_spread_is_stable_within_an_area(self) -> None:
        items = [("a", 1), ("a", 2), ("a", 3)]

        assert [n for _, n in spread_by_area(items, key=itemgetter(0))] == [1, 2, 3]

    def test_unknown_area_never_starves(self) -> None:
        """An empty key is one bucket, still admitted — never dropped."""
        items = [("", 1), ("", 2)]

        assert [n for _, n in spread_by_area(items, key=itemgetter(0))] == [1, 2]
