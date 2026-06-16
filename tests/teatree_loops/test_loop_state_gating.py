"""The tick gate honours the DB-backed LoopState above the toml fallback (#1913).

``LoopsConfig.is_enabled`` is the single enable/disable decision the live tick
and the orchestrator both call (via ``gating.elapsed_and_enabled``). #1913 makes
the DB ``LoopState`` the canonical control tier above that toml/env config:

    An empty ``LoopState`` table leaves every loop resolving exactly as the
    toml/env config dictates (the no-regression invariant). A ``PAUSED`` /
    ``DISABLED`` row skips the loop in the tick — EVEN for an ``always_on`` loop,
    which the toml/env layer cannot stop (the whole point of the 2026-06-03
    'pause everything' incident). An ``ENABLED`` row (or no row) restores the
    toml/env behaviour.
"""

from django.test import TestCase

from teatree.core.models import LoopState
from teatree.loops.base import MiniLoop
from teatree.loops.config import LoopOverride, LoopsConfig


def _build(**_: object) -> list[object]:
    return []


def _loop(name: str, *, always_on: bool = False) -> MiniLoop:
    return MiniLoop(name=name, default_cadence_seconds=60, build_jobs=_build, always_on=always_on)


class TestEmptyTableIsNoRegression(TestCase):
    def test_empty_table_keeps_toml_enabled_loop_enabled(self) -> None:
        config = LoopsConfig(enabled=True)
        assert config.is_enabled(_loop("review")) is True

    def test_empty_table_keeps_toml_disabled_loop_disabled(self) -> None:
        config = LoopsConfig(enabled=False)
        assert config.is_enabled(_loop("review")) is False

    def test_empty_table_keeps_per_loop_override(self) -> None:
        config = LoopsConfig(enabled=True, per_loop={"review": LoopOverride(enabled=False)})
        assert config.is_enabled(_loop("review")) is False

    def test_empty_table_keeps_always_on_loop_enabled(self) -> None:
        config = LoopsConfig(enabled=False)
        assert config.is_enabled(_loop("dispatch", always_on=True)) is True


class TestDbPauseDisableWinsOverToml(TestCase):
    def test_db_pause_skips_a_toml_enabled_loop(self) -> None:
        LoopState.objects.pause("review")
        config = LoopsConfig(enabled=True)
        assert config.is_enabled(_loop("review")) is False

    def test_db_disable_skips_a_toml_enabled_loop(self) -> None:
        LoopState.objects.disable("review")
        config = LoopsConfig(enabled=True)
        assert config.is_enabled(_loop("review")) is False

    def test_db_pause_skips_an_always_on_loop(self) -> None:
        # The incident: toml `[loops] enabled=false` cannot stop an always_on
        # loop, but an explicit DB pause MUST.
        LoopState.objects.pause("dispatch")
        config = LoopsConfig(enabled=True)
        assert config.is_enabled(_loop("dispatch", always_on=True)) is False

    def test_db_disable_skips_an_always_on_loop(self) -> None:
        LoopState.objects.disable("dispatch")
        config = LoopsConfig(enabled=True)
        assert config.is_enabled(_loop("dispatch", always_on=True)) is False


class TestDbEnabledRestoresTomlBehaviour(TestCase):
    def test_db_enabled_row_falls_through_to_toml(self) -> None:
        LoopState.objects.pause("review")
        LoopState.objects.resume("review")
        config = LoopsConfig(enabled=False)
        # Back to ENABLED in the DB → the toml layer is authoritative again,
        # and toml says disabled.
        assert config.is_enabled(_loop("review")) is False

    def test_db_resume_re_runs_a_previously_paused_always_on_loop(self) -> None:
        LoopState.objects.pause("dispatch")
        LoopState.objects.resume("dispatch")
        config = LoopsConfig(enabled=True)
        assert config.is_enabled(_loop("dispatch", always_on=True)) is True
