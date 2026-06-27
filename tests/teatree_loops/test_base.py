"""MiniLoop contract — immutable, callable build_jobs, optional off_live_tick."""

import dataclasses

import pytest

from teatree.loops.base import MiniLoop


def _build_jobs(**_: object) -> list[object]:
    return []


class TestMiniLoopContract:
    def test_constructs_with_required_fields(self) -> None:
        loop = MiniLoop(name="inbox", default_cadence_seconds=60, build_jobs=_build_jobs)
        assert loop.name == "inbox"
        assert loop.default_cadence_seconds == 60
        assert loop.off_live_tick is False

    def test_off_live_tick_is_optional(self) -> None:
        loop = MiniLoop(
            name="dream",
            default_cadence_seconds=300,
            build_jobs=_build_jobs,
            off_live_tick=True,
        )
        assert loop.off_live_tick is True

    def test_is_frozen(self) -> None:
        loop = MiniLoop(name="x", default_cadence_seconds=60, build_jobs=_build_jobs)
        with pytest.raises(dataclasses.FrozenInstanceError):
            loop.name = "other"  # type: ignore[misc]

    def test_build_jobs_is_callable(self) -> None:
        loop = MiniLoop(name="x", default_cadence_seconds=60, build_jobs=_build_jobs)
        assert callable(loop.build_jobs)
        assert loop.build_jobs() == []
