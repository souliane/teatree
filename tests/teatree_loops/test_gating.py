"""Shared enable + cadence gate (#1481).

The single decision both ``Orchestrator.tick`` and ``build_default_jobs``
consult so the live tick and the orchestrator never drift on which loops
run a given tick.
"""

import datetime as dt
import os

from django.test import TestCase

from teatree.core.models import LoopState, MiniLoopMarker
from teatree.loops.base import MiniLoop
from teatree.loops.config import LoopsConfig
from teatree.loops.gating import SKIP_CADENCE, SKIP_DISABLED, elapsed_and_enabled


def _loop(name: str, *, cadence: int = 60, always_on: bool = False) -> MiniLoop:
    return MiniLoop(
        name=name,
        default_cadence_seconds=cadence,
        build_jobs=lambda **_: [],
        always_on=always_on,
    )


class ElapsedAndEnabledTestCase(TestCase):
    now = dt.datetime(2026, 5, 28, 12, tzinfo=dt.UTC)

    def test_fires_when_enabled_and_no_marker(self) -> None:
        decision = elapsed_and_enabled(LoopsConfig(), _loop("inbox"), self.now)
        assert decision.should_fire
        assert decision.skip_reason is None

    def test_skips_when_db_disabled(self) -> None:
        LoopState.objects.disable("review")
        decision = elapsed_and_enabled(LoopsConfig(), _loop("review"), self.now)
        assert not decision.should_fire
        assert decision.skip_reason == SKIP_DISABLED

    def test_skips_when_cadence_not_elapsed(self) -> None:
        MiniLoopMarker.objects.mark_fired("inbox", self.now - dt.timedelta(seconds=10))
        decision = elapsed_and_enabled(LoopsConfig(), _loop("inbox", cadence=60), self.now)
        assert not decision.should_fire
        assert decision.skip_reason == SKIP_CADENCE

    def test_fires_when_cadence_elapsed(self) -> None:
        MiniLoopMarker.objects.mark_fired("inbox", self.now - dt.timedelta(seconds=120))
        decision = elapsed_and_enabled(LoopsConfig(), _loop("inbox", cadence=60), self.now)
        assert decision.should_fire

    def test_env_kill_switch_skips_named_loop(self) -> None:
        old = os.environ.get("T3_LOOPS_DISABLED")
        try:
            os.environ["T3_LOOPS_DISABLED"] = "inbox"
            decision = elapsed_and_enabled(LoopsConfig(), _loop("inbox"), self.now)
            assert not decision.should_fire
            assert decision.skip_reason == SKIP_DISABLED
        finally:
            if old is None:
                os.environ.pop("T3_LOOPS_DISABLED", None)
            else:
                os.environ["T3_LOOPS_DISABLED"] = old

    def test_always_on_bypasses_env_disable(self) -> None:
        # The env kill-switch ignores an always_on loop; only a DB hold stops it.
        old = os.environ.get("T3_LOOPS_DISABLED")
        try:
            os.environ["T3_LOOPS_DISABLED"] = "all"
            decision = elapsed_and_enabled(LoopsConfig(), _loop("dispatch", always_on=True), self.now)
            assert decision.should_fire
        finally:
            if old is None:
                os.environ.pop("T3_LOOPS_DISABLED", None)
            else:
                os.environ["T3_LOOPS_DISABLED"] = old
