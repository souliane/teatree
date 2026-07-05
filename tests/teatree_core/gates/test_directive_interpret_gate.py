"""directive_interpret_gate (north-star PR-6): recording a headless interpreter's return.

The server-side half of the interpret lane (maker≠checker): a shell-denied interpreter
RETURNS a ``directive_interpretation`` envelope, and this records it — a valid sketch
moves the directive to INTERPRETED, clarifying questions park it in CLARIFYING, an
invalid sketch fails the task (an error string), and a task with no dispatch is a no-op.
"""

from django.test import TestCase

from teatree.core.gates.directive_interpret_gate import (
    record_returned_directive_interpretation,
    validate_activation_scope,
)
from teatree.core.models import DeferredQuestion, Directive, DirectiveDispatch, Session, Task, Ticket
from tests.teatree_core.models.test_mechanism_sketch import valid_envelope


def _dispatched_directive() -> tuple[Directive, Task]:
    directive = Directive.objects.capture("max 1 MR per repo for overlay X", source=Directive.Source.CLI)
    row = DirectiveDispatch.enqueue(directive=directive, contract="c")
    assert row is not None
    assert row.task is not None
    return directive, row.task


class TestRecordSketch(TestCase):
    def test_a_valid_sketch_moves_the_directive_to_interpreted(self) -> None:
        directive, task = _dispatched_directive()
        envelope = {
            "directive_interpretation": {
                "interpreter_identity": "interp-1",
                "constraint_statement": "at most 1 open PR per (ticket, repo)",
                "sketch": valid_envelope(),
            }
        }
        error = record_returned_directive_interpretation(task, envelope)
        assert error == ""
        directive.refresh_from_db()
        assert directive.state == Directive.State.INTERPRETED
        assert directive.constraint_statement == "at most 1 open PR per (ticket, repo)"
        assert directive.sketch is not None
        assert directive.sketch.rejected_alternatives  # the recorded generic-shape decision

    def test_an_invalid_sketch_fails_the_task_with_an_error(self) -> None:
        directive, task = _dispatched_directive()
        envelope = {"directive_interpretation": {"sketch": valid_envelope(rejected_alternatives=[])}}
        error = record_returned_directive_interpretation(task, envelope)
        assert error  # non-empty -> the caller fails the task, the loop redispatches
        assert "N=2" in error
        directive.refresh_from_db()
        assert directive.state == Directive.State.CAPTURED  # unchanged — no garbage recorded

    def test_an_unregistered_activation_scope_fails_the_recorder(self) -> None:
        # The registry half of validation lives in the gate (it needs the overlay
        # registry the pure model layer must not import).
        directive, task = _dispatched_directive()
        envelope = {"directive_interpretation": {"sketch": valid_envelope(activation_scope="no-such-overlay-xyz")}}
        error = record_returned_directive_interpretation(task, envelope)
        assert "registered overlay" in error
        directive.refresh_from_db()
        assert directive.state == Directive.State.CAPTURED


class TestValidateActivationScope(TestCase):
    def test_an_empty_scope_is_a_valid_global_mechanism(self) -> None:
        assert validate_activation_scope(valid_envelope(activation_scope="")) is None

    def test_a_registered_overlay_scope_is_accepted(self) -> None:
        assert validate_activation_scope(valid_envelope(activation_scope="t3-teatree")) is None

    def test_an_unregistered_scope_is_rejected(self) -> None:
        finding = validate_activation_scope(valid_envelope(activation_scope="no-such-overlay-xyz"))
        assert finding is not None
        assert "registered overlay" in finding


class TestRecordClarifications(TestCase):
    def test_clarifying_questions_park_the_directive_and_record_deferred_questions(self) -> None:
        directive, task = _dispatched_directive()
        envelope = {
            "directive_interpretation": {
                "clarifying_questions": ["Does 'max 1 MR' mean open concurrently or ever per ticket?"],
            }
        }
        error = record_returned_directive_interpretation(task, envelope)
        assert error == ""
        directive.refresh_from_db()
        assert directive.state == Directive.State.CLARIFYING
        assert DeferredQuestion.objects.filter(options_hash__startswith=f"directive_clarify:{directive.pk}:").exists()


class TestNoOps(TestCase):
    def test_a_task_with_no_dispatch_is_a_no_op(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree")
        session = Session.objects.create(ticket=ticket, agent_id="x")
        task = Task.objects.create(ticket=ticket, session=session, phase="directive_interpreting")
        result = {"directive_interpretation": {"sketch": valid_envelope()}}
        assert record_returned_directive_interpretation(task, result) == ""

    def test_a_result_without_the_envelope_is_a_no_op(self) -> None:
        _directive, task = _dispatched_directive()
        assert record_returned_directive_interpretation(task, {"summary": "nothing"}) == ""
