"""Test D — what a fresh install actually does at DEFAULT resolution (PR-8, #3895).

#3895 graduated ``directive_loop_enabled`` to default-ON, so the tick is no longer a
TOTAL no-op: at default resolution the INTAKE arc runs and a captured directive IS
interpreted. What keeps a fresh install inert is now structural rather than a flag —
the seeded ``Loop`` row ships disabled (so nothing ticks unprompted), the ``DIRECTIVE``
router DROPs (so nothing is captured unprompted), and the human ratify gate stops the
arc before anything effectful.

These pin exactly that, honestly: intake advances, and the EFFECTFUL counts stay zero.
Delta vs ``tests/teatree_loops/directive_loop/test_flag_off_parity.py`` (which passes a
``SimpleNamespace``): this pins the RESOLVER-LEVEL default, on real components, from a
pristine test DB.
"""

from django.test import TestCase

from teatree.config import get_effective_settings
from teatree.core.intake.event_router import RoutedAction, route_event
from teatree.core.models import ConfigSetting, DirectiveDispatch, IncomingEvent, IntentClassification, Loop
from teatree.core.models.directive import Directive
from teatree.loops.directive_loop.guards import SCORE_OFF, evaluate_execution_guards
from teatree.loops.directive_loop.loop import DIRECTIVE_LOOP_NAME
from teatree.loops.seed import seed_default_loops_and_prompts
from tests.integration.directive_dogfood.exemplar import PROOF_CASE_TEXT, SCOPE, tick


class TestDefaultResolutionInertness(TestCase):
    def test_default_resolution_interprets_but_writes_no_config(self) -> None:
        directive = Directive.objects.capture(PROOF_CASE_TEXT, source=Directive.Source.CLI, scope_overlay=SCOPE)

        result = tick()  # settings=None → the REAL default resolution (no enablement rows)

        # The shipped flag lets the intake arc run; it dispatches the interpreter and
        # stops. Nothing EFFECTFUL happens — no config write, no score snapshot — and
        # the directive stays CAPTURED behind the human ratify gate.
        assert result.action == "interpret_dispatched"
        assert DirectiveDispatch.objects.count() == 1
        assert ConfigSetting.objects.count() == 0
        assert Directive.objects.get(pk=directive.pk).state == Directive.State.CAPTURED

    def test_the_execution_arc_still_refuses_at_the_score_guard(self) -> None:
        # The arc that CHANGES config did not graduate with the intake flag:
        # ``factory_score_enabled`` stays DARK and off, so the post-admission chain
        # refuses at G1b and no snapshot row is ever written.
        assert not ConfigSetting.objects.filter(key="factory_score_enabled").exists()
        assert evaluate_execution_guards(settings=get_effective_settings()).reason == SCORE_OFF

    def test_seeded_loop_row_ships_disabled(self) -> None:
        seed_default_loops_and_prompts()
        assert Loop.objects.get(name=DIRECTIVE_LOOP_NAME).enabled is False

    def test_directive_intent_drops_at_default_routing(self) -> None:
        # #105: ambient directive detection is deleted — a DIRECTIVE-classified event is
        # unrouteable and DROPs; the only Directive producer is the explicit capture CLI.
        event = IncomingEvent(source=IncomingEvent.Source.SLACK, channel_ref="C1", body=PROOF_CASE_TEXT)
        classification = IntentClassification(event=event, intent=IntentClassification.Intent.DIRECTIVE)

        assert route_event(event, classification).kind == RoutedAction.Kind.DROP
