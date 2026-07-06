"""Test D — the no-prod-flip pin: the loop is inert at DEFAULT resolution (PR-8).

The merge-is-a-no-op proof. With NO enablement rows written, the REAL default
settings resolution refuses the tick, the seeded ``Loop`` row is disabled, and the
``DIRECTIVE`` router DROPs — the exact properties that make shipping PR-8 change
nothing in production. Delta vs ``tests/teatree_loops/directive_loop/test_flag_off_parity.py``
(which passes a ``SimpleNamespace``): this pins the RESOLVER-LEVEL default, on real
components, from a pristine test DB.
"""

from django.test import TestCase

from teatree.core.event_router import RoutedAction, route_event
from teatree.core.models import ConfigSetting, DirectiveDispatch, IncomingEvent, IntentClassification, Loop, Task
from teatree.core.models.directive import Directive
from teatree.loops.directive_loop.guards import FLAG_OFF
from teatree.loops.directive_loop.loop import DIRECTIVE_LOOP_NAME
from teatree.loops.seed import seed_default_loops_and_prompts
from tests.integration.directive_dogfood.exemplar import PROOF_CASE_TEXT, SCOPE, tick


class TestQuadrupleOffDefault(TestCase):
    def test_default_resolution_refuses_and_mutates_nothing(self) -> None:
        directive = Directive.objects.capture(PROOF_CASE_TEXT, source=Directive.Source.CLI, scope_overlay=SCOPE)

        result = tick()  # settings=None → the REAL default resolution (no enablement rows)

        assert result.action == "refused"
        assert result.reason == FLAG_OFF
        assert DirectiveDispatch.objects.count() == 0
        assert Task.objects.count() == 0
        assert not ConfigSetting.objects.filter(key="directive_loop_enabled").exists()
        assert Directive.objects.get(pk=directive.pk).state == Directive.State.CAPTURED

    def test_seeded_loop_row_ships_disabled(self) -> None:
        seed_default_loops_and_prompts()
        assert Loop.objects.get(name=DIRECTIVE_LOOP_NAME).enabled is False

    def test_directive_intent_drops_at_default_routing(self) -> None:
        # Flag-off parity on the real router: a DIRECTIVE-classified event DROPs while
        # directive_routing_enabled is off (its default), so intake is inert.
        event = IncomingEvent(source=IncomingEvent.Source.SLACK, channel_ref="C1", body=PROOF_CASE_TEXT)
        classification = IntentClassification(event=event, intent=IntentClassification.Intent.DIRECTIVE)

        assert route_event(event, classification).kind == RoutedAction.Kind.DROP
