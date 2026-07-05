"""Flag-off (and critic-not-live) parity — the outer loop is a total no-op (T4-PR-3).

Layer 1 (the ``outer_loop_enabled`` flag) and Layer 4 (the critic-live code guard)
each make a full tick a proven no-op: zero experiments, zero snapshots, zero
questions, zero dispatches. These are anti-vacuous — if the flag-off / critic
guards were removed the tick would fall through to propose and the row counts
would move.
"""

from types import SimpleNamespace

from django.test import TestCase

from teatree.core.models import DeferredQuestion, FactoryScoreSnapshot, OuterLoopExperiment
from teatree.loops.outer_loop import guards
from teatree.loops.outer_loop.tick import run_tick


def _counts() -> tuple[int, int, int]:
    return (
        OuterLoopExperiment.objects.count(),
        FactoryScoreSnapshot.objects.count(),
        DeferredQuestion.objects.count(),
    )


class TestFlagOffParity(TestCase):
    def test_default_config_tick_is_a_total_no_op(self) -> None:
        # Default config: outer_loop_enabled is False → the tick refuses at G1 and
        # mutates nothing. Removing the flag guard would let it propose.
        before = _counts()
        result = run_tick()
        assert result.action == "refused"
        assert result.reason == guards.FLAG_OFF
        assert _counts() == before == (0, 0, 0)

    def test_flag_on_but_critic_not_live_is_still_a_no_op(self) -> None:
        # Layer 4 is CODE, not config: even with the flag flipped on, the real
        # critic probe fails closed (no CriticVerdict model this session) so the
        # tick refuses critic_not_live and still writes zero rows.
        settings = SimpleNamespace(
            outer_loop_enabled=True,
            outer_loop_measure_days=7,
            outer_loop_max_per_week=1,
            outer_loop_stop_after_consecutive_failures=3,
        )
        result = run_tick(settings=settings)
        assert result.action == "refused"
        assert result.reason == guards.CRITIC_NOT_LIVE
        assert _counts() == (0, 0, 0)
