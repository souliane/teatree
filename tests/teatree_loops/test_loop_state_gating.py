"""The tick gate resolves enable/disable through the DB-backed LoopState only (#1913).

``LoopsConfig.is_enabled`` is the single enable/disable decision the live tick
and the orchestrator both call (via ``gating.elapsed_and_enabled``). The DB
``LoopState`` tier is the canonical (and only) control authority — there is no
env kill-switch and no ``[loops]`` toml disabled-state fallback:

    An empty ``LoopState`` table leaves every loop running (the default). A
    ``PAUSED`` / ``DISABLED`` row skips the loop in the tick — including the
    core ``dispatch`` loop (the whole point of the 2026-06-03 'pause
    everything' incident). An ``ENABLED`` row (or no row) defaults to enabled.
"""

from django.test import TestCase

from teatree.core.models import LoopState
from teatree.loops.base import MiniLoop
from teatree.loops.config import LoopsConfig


def _build(**_: object) -> list[object]:
    return []


def _loop(name: str) -> MiniLoop:
    return MiniLoop(name=name, default_cadence_seconds=60, build_jobs=_build)


class TestEmptyTableIsNoRegression(TestCase):
    def test_empty_table_keeps_loop_enabled_by_default(self) -> None:
        config = LoopsConfig()
        assert config.is_enabled(_loop("review")) is True

    def test_empty_table_keeps_dispatch_loop_enabled(self) -> None:
        config = LoopsConfig()
        assert config.is_enabled(_loop("dispatch")) is True


class TestDbPauseDisableWinsOverDefault(TestCase):
    def test_db_pause_skips_a_loop(self) -> None:
        LoopState.objects.pause("review")
        config = LoopsConfig()
        assert config.is_enabled(_loop("review")) is False

    def test_db_disable_skips_a_loop(self) -> None:
        LoopState.objects.disable("review")
        config = LoopsConfig()
        assert config.is_enabled(_loop("review")) is False

    def test_db_pause_skips_the_dispatch_loop(self) -> None:
        # An explicit DB pause MUST stop even the core ``dispatch`` loop.
        LoopState.objects.pause("dispatch")
        config = LoopsConfig()
        assert config.is_enabled(_loop("dispatch")) is False

    def test_db_disable_skips_the_dispatch_loop(self) -> None:
        LoopState.objects.disable("dispatch")
        config = LoopsConfig()
        assert config.is_enabled(_loop("dispatch")) is False


class TestDbEnabledRowDefersToDefault(TestCase):
    def test_db_enabled_row_falls_through_to_default(self) -> None:
        LoopState.objects.pause("review")
        LoopState.objects.resume("review")
        config = LoopsConfig()
        # Back to ENABLED in the DB → defaults to enabled.
        assert config.is_enabled(_loop("review")) is True

    def test_db_resume_re_runs_a_previously_paused_dispatch_loop(self) -> None:
        LoopState.objects.pause("dispatch")
        LoopState.objects.resume("dispatch")
        config = LoopsConfig()
        assert config.is_enabled(_loop("dispatch")) is True
