"""The tick gate honours the DB-backed LoopState as the disable authority (#1913, #2702).

``LoopsConfig.is_enabled`` is the single enable/disable decision the live tick
and the orchestrator both call (via ``gating.elapsed_and_enabled``). #1913 makes
the DB ``LoopState`` the canonical control tier; #2702 removes the former
``[loops]`` toml disabled-state fallback, so the resolution is now env → DB
``LoopState`` → default:

    An empty ``LoopState`` table leaves every loop running (the default). A
    ``PAUSED`` / ``DISABLED`` row skips the loop in the tick — EVEN for an
    ``always_on`` loop, which the env layer cannot stop (the whole point of the
    2026-06-03 'pause everything' incident). An ``ENABLED`` row (or no row)
    defers to the env layer, which defaults to enabled.
"""

from django.test import TestCase

from teatree.core.models import LoopState
from teatree.loops.base import MiniLoop
from teatree.loops.config import LoopsConfig


def _build(**_: object) -> list[object]:
    return []


def _loop(name: str, *, always_on: bool = False) -> MiniLoop:
    return MiniLoop(name=name, default_cadence_seconds=60, build_jobs=_build, always_on=always_on)


class TestEmptyTableIsNoRegression(TestCase):
    def test_empty_table_keeps_loop_enabled_by_default(self) -> None:
        config = LoopsConfig()
        assert config.is_enabled(_loop("review")) is True

    def test_empty_table_keeps_always_on_loop_enabled(self) -> None:
        config = LoopsConfig()
        assert config.is_enabled(_loop("dispatch", always_on=True)) is True


class TestDbPauseDisableWinsOverDefault(TestCase):
    def test_db_pause_skips_a_loop(self) -> None:
        LoopState.objects.pause("review")
        config = LoopsConfig()
        assert config.is_enabled(_loop("review")) is False

    def test_db_disable_skips_a_loop(self) -> None:
        LoopState.objects.disable("review")
        config = LoopsConfig()
        assert config.is_enabled(_loop("review")) is False

    def test_db_pause_skips_an_always_on_loop(self) -> None:
        # The incident: the env layer cannot stop an always_on loop, but an
        # explicit DB pause MUST.
        LoopState.objects.pause("dispatch")
        config = LoopsConfig()
        assert config.is_enabled(_loop("dispatch", always_on=True)) is False

    def test_db_disable_skips_an_always_on_loop(self) -> None:
        LoopState.objects.disable("dispatch")
        config = LoopsConfig()
        assert config.is_enabled(_loop("dispatch", always_on=True)) is False


class TestDbEnabledRowDefersToDefault(TestCase):
    def test_db_enabled_row_falls_through_to_default(self) -> None:
        LoopState.objects.pause("review")
        LoopState.objects.resume("review")
        config = LoopsConfig()
        # Back to ENABLED in the DB → the env/default layer is authoritative
        # again, which defaults to enabled.
        assert config.is_enabled(_loop("review")) is True

    def test_db_resume_re_runs_a_previously_paused_always_on_loop(self) -> None:
        LoopState.objects.pause("dispatch")
        LoopState.objects.resume("dispatch")
        config = LoopsConfig()
        assert config.is_enabled(_loop("dispatch", always_on=True)) is True
