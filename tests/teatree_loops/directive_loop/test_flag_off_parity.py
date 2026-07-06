"""Flag-off (and critic-not-live) parity — the directive loop is a total no-op (north-star PR-7).

The QUADRUPLE-OFF proof: at default config the loop mutates NOTHING — zero dispatches,
zero ConfigSetting writes, zero questions, zero snapshots, and the directive's state is
untouched. These are anti-vacuous: remove the flag / critic guard and the tick would
fall through to advance the directive and the counts would move.
"""

from types import SimpleNamespace

from django.test import TestCase

from teatree.core.models import ConfigSetting, DeferredQuestion, Directive, DirectiveDispatch, FactoryScoreSnapshot
from teatree.loops.directive_loop import guards
from teatree.loops.directive_loop.tick import run_tick


def _counts() -> tuple[int, int, int, int]:
    return (
        DirectiveDispatch.objects.count(),
        ConfigSetting.objects.count(),
        DeferredQuestion.objects.count(),
        FactoryScoreSnapshot.objects.count(),
    )


class TestFlagOffParity(TestCase):
    def test_default_config_tick_is_a_total_no_op(self) -> None:
        # A CAPTURED directive is present; the default-config tick refuses at G1 and
        # advances NOTHING. Removing the flag guard would dispatch the interpreter.
        directive = Directive.objects.capture("do X", source=Directive.Source.CLI)
        before = _counts()
        result = run_tick()
        assert result.action == "refused"
        assert result.reason == guards.FLAG_OFF
        assert _counts() == before == (0, 0, 0, 0)
        assert Directive.objects.get(pk=directive.pk).state == Directive.State.CAPTURED

    def test_flag_on_but_critic_not_live_is_still_a_no_op(self) -> None:
        # Layer 4 is CODE, not config: even with the flag flipped on, the real critic
        # probe fails closed (no live critic) so the tick refuses critic_not_live and
        # writes zero rows.
        directive = Directive.objects.capture("do X", source=Directive.Source.CLI)
        settings = SimpleNamespace(directive_loop_enabled=True, factory_score_enabled=True, directive_verify_days=7)
        result = run_tick(settings=settings)
        assert result.action == "refused"
        assert result.reason == guards.CRITIC_NOT_LIVE
        assert _counts() == (0, 0, 0, 0)
        assert Directive.objects.get(pk=directive.pk).state == Directive.State.CAPTURED

    def test_score_off_is_a_no_op_and_writes_no_snapshot(self) -> None:
        settings = SimpleNamespace(directive_loop_enabled=True, factory_score_enabled=False, directive_verify_days=7)
        result = run_tick(settings=settings)
        assert result.action == "refused"
        assert result.reason == guards.SCORE_OFF
        assert _counts() == (0, 0, 0, 0)
