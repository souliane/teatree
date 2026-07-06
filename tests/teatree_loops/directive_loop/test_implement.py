"""The IMPLEMENT phase — synthetic mechanism ticket + baseline snapshot (north-star PR-7).

``setting_policy_gate`` anchors a real AUTHOR ticket linked back via
``extra["directive_id"]`` and schedules its coding; ``activation_only`` skips straight
to CONFIGURING. Both snapshot the admission baseline the verify step compares against.
"""

from django.test import TestCase

from teatree.core.models import DeferredQuestion, Directive, FactoryScoreSnapshot, Task, Ticket
from teatree.core.models.mechanism_sketch import sketch_from_envelope
from teatree.loops.directive_loop.implement import (
    DIRECTIVE_IMPL_UMBRELLA_URL,
    schedule_directive_implementation,
    skip_directive_implementation,
)
from tests.teatree_core.models.test_mechanism_sketch import valid_envelope


def _admitted(**sketch_over: object) -> Directive:
    directive = Directive.objects.capture("max 1 MR per repo", source=Directive.Source.CLI, scope_overlay="")
    directive.record_interpretation(sketch_from_envelope(valid_envelope(**sketch_over)), constraint_statement="cst")
    question = DeferredQuestion.record("Ratify?", options_hash=f"directive_ratify:{directive.pk}")
    directive.attach_ratification(question)
    DeferredQuestion.consume(question.pk, answer="approve")
    directive.refresh_from_db()
    directive.admit()
    return directive


class TestScheduleDirectiveImplementation(TestCase):
    def test_anchors_the_ticket_links_it_back_and_moves_to_implementing(self) -> None:
        directive = _admitted()
        task = schedule_directive_implementation(directive)
        directive.refresh_from_db()
        assert directive.state == Directive.State.IMPLEMENTING
        assert directive.ticket is not None
        assert directive.ticket.extra["directive_id"] == directive.pk
        assert directive.baseline_snapshot is not None
        assert task is not None
        assert task.phase == "coding"

    def test_ticket_url_carries_the_directive_fragment_and_one_coding_task(self) -> None:
        directive = _admitted()
        schedule_directive_implementation(directive)
        directive.refresh_from_db()
        assert f"directive-impl={directive.pk}" in directive.ticket.issue_url
        assert Task.objects.pending_in_phase("coding").filter(ticket=directive.ticket).count() == 1

    def test_does_not_double_schedule_or_re_write_an_existing_ticket(self) -> None:
        # A pre-existing synthetic ticket already carrying the directive_id + a coding
        # task (a re-tick after a crash) is neither double-scheduled nor re-written: the
        # directive still advances, task returns None.
        directive = _admitted()
        ticket, _ = Ticket.objects.get_or_create(
            issue_url=f"{DIRECTIVE_IMPL_UMBRELLA_URL}#directive-impl={directive.pk}",
            defaults={"role": Ticket.Role.AUTHOR, "short_description": "pre", "extra": {"directive_id": directive.pk}},
        )
        ticket.schedule_coding()
        task = schedule_directive_implementation(directive)
        assert task is None
        directive.refresh_from_db()
        assert directive.state == Directive.State.IMPLEMENTING


class TestSkipDirectiveImplementation(TestCase):
    def test_activation_only_skips_to_configuring_with_no_ticket(self) -> None:
        directive = _admitted(kind="activation_only", acceptance_tests=[])
        skip_directive_implementation(directive)
        assert directive.state == Directive.State.CONFIGURING
        assert directive.ticket_id is None
        assert directive.baseline_snapshot is not None
        assert FactoryScoreSnapshot.objects.count() == 1
