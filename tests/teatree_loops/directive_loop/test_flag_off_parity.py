"""Flag-off parity, and the critic's scope — the directive loop's no-op proof (north-star PR-7).

At default config the loop mutates NOTHING — zero dispatches, zero ConfigSetting writes,
zero questions, zero snapshots, and the directive's state is untouched. Anti-vacuous:
remove the flag guard and the tick falls through and the counts move.

#3649 narrowed the critic guard to the post-admission arc, so flag-on-without-a-critic is
no longer a TOTAL no-op — it interprets. What it must still never do is write config or a
score snapshot: the assertions below pin that the effectful counts stay at zero and the
directive cannot pass the human ratify gate.
"""

from types import SimpleNamespace

from django.test import TestCase

from teatree.core.models import ConfigSetting, DeferredQuestion, Directive, DirectiveDispatch, FactoryScoreSnapshot
from teatree.core.models.mechanism_sketch import sketch_from_envelope
from teatree.loops.directive_loop import guards
from teatree.loops.directive_loop.tick import run_tick
from tests.teatree_core.models.test_mechanism_sketch import valid_envelope


def _flag_on(*, score: bool) -> SimpleNamespace:
    return SimpleNamespace(
        directive_loop_enabled=True,
        factory_score_enabled=score,
        directive_verify_days=7,
        directive_intake_per_tick=25,
    )


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

    def test_flag_on_without_a_critic_interprets_but_writes_no_config(self) -> None:
        # #3649: the real critic probe still fails closed, and intake proceeds anyway —
        # it dispatches the interpreter and stops. No config write, no score snapshot.
        directive = Directive.objects.capture("do X", source=Directive.Source.CLI)
        result = run_tick(settings=_flag_on(score=True))
        assert result.action == "interpret_dispatched"
        dispatches, configs, _questions, snapshots = _counts()
        assert (dispatches, configs, snapshots) == (1, 0, 0)
        assert Directive.objects.get(pk=directive.pk).state == Directive.State.CAPTURED

    def test_the_execution_arc_still_refuses_without_a_critic(self) -> None:
        # The guard that bounds self-modification is unchanged where it bites: a
        # directive past the human ratify gate cannot advance under an absent critic.
        directive = Directive.objects.capture("do X", source=Directive.Source.CLI)
        directive.record_interpretation(sketch_from_envelope(valid_envelope()), constraint_statement="c")
        question = DeferredQuestion.record("Ratify?", options_hash=f"directive_ratify:{directive.pk}")
        directive.attach_ratification(question)
        DeferredQuestion.consume(question.pk, answer="approve")
        directive.refresh_from_db()
        directive.admit()
        result = run_tick(settings=_flag_on(score=True))
        assert result.action == "refused"
        assert result.reason == guards.CRITIC_NOT_LIVE
        assert ConfigSetting.objects.count() == 0
        assert Directive.objects.get(pk=directive.pk).state == Directive.State.ADMITTED

    def test_score_off_writes_no_snapshot_at_the_shipped_critic_state(self) -> None:
        # #3643 scoped the score guard to the post-admission arc: score-off no longer
        # refuses intake, and intake never writes a FactoryScoreSnapshot either way.
        Directive.objects.capture("do X", source=Directive.Source.CLI)
        result = run_tick(settings=_flag_on(score=False))
        assert result.action == "interpret_dispatched"
        assert FactoryScoreSnapshot.objects.count() == 0
        assert ConfigSetting.objects.count() == 0
