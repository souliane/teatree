"""This lane runs under its own wall-clock ceiling, and that ceiling is bounded.

The failure being pinned is a gate that kills correct work: a whole-tree item
exceeding the unit-test ``timeout`` is reported as an expiry, on a different
item each run, so the push looks flaky and the lane looks broken. The live-item
lane below reads the ceiling off the item pytest is currently running, so it
observes the wiring rather than the constant.
"""

import pytest

from tests.conformance._lane_ceiling import (
    LANE_TIMEOUT_CAP_SECONDS,
    LANE_TIMEOUT_SECONDS,
    apply_lane_ceiling,
    project_default_timeout_seconds,
)


class _StubItem:
    def __init__(self, declared: pytest.Mark | None = None) -> None:
        self._declared = declared
        self.added: list[pytest.MarkDecorator] = []

    def get_closest_marker(self, name: str) -> pytest.Mark | None:
        return self._declared if self._declared is not None and self._declared.name == name else None

    def add_marker(self, marker: pytest.MarkDecorator) -> None:
        self.added.append(marker)


class TestTheCeilingIsLiveOnThisLane:
    def test_the_running_item_carries_the_lane_ceiling(self, request: pytest.FixtureRequest) -> None:
        marker = request.node.get_closest_marker("timeout")
        assert marker is not None, "conformance items run under the unit-test ceiling; the lane ceiling is not wired"
        assert marker.args[0] == LANE_TIMEOUT_SECONDS


class TestTheCeilingStaysHonest:
    def test_it_clears_the_project_default(self) -> None:
        assert project_default_timeout_seconds() < LANE_TIMEOUT_SECONDS

    def test_a_hang_still_fails_the_push(self) -> None:
        assert 0 < LANE_TIMEOUT_SECONDS <= LANE_TIMEOUT_CAP_SECONDS


class TestTheCeilingDefersToAnItemsOwnMarker:
    def test_an_item_without_one_gets_the_lane_ceiling(self) -> None:
        item = _StubItem()
        apply_lane_ceiling([item])
        assert [marker.args[0] for marker in item.added] == [LANE_TIMEOUT_SECONDS]

    def test_an_item_declaring_its_own_is_left_alone(self) -> None:
        item = _StubItem(declared=pytest.mark.timeout(45).mark)
        apply_lane_ceiling([item])
        assert item.added == []
