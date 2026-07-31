"""DirectiveDispatch (north-star PR-6): the idempotent enqueue of the headless interpreter.

Mirrors ``CriticDispatch``: one row per ``(directive, purpose, generation)`` linking a
claimable headless ``Task(phase="directive_interpreting")``. A re-fire at the same
generation returns ``None`` (no second interpreter); a bumped generation arms a fresh
one. The interpret task needs a ``Ticket``, so the dispatch anchors a synthetic one.
"""

from django.test import TestCase

from teatree.core.models import Directive, DirectiveDispatch, Task


def _directive() -> Directive:
    return Directive.objects.capture("max 1 MR per repo for overlay X", source=Directive.Source.CLI)


class TestDirectiveDispatchEnqueue(TestCase):
    def test_enqueue_creates_a_headless_interpret_task(self) -> None:
        row = DirectiveDispatch.enqueue(directive=_directive(), contract="interpret this directive")
        assert row is not None
        assert row.task is not None
        # Its OWN phase so the result is measured against the interpret evidence
        # contract; the execution lane is the runtime's routing decision (Task.save).
        assert row.task.phase == "directive_interpreting"
        assert "interpret this directive" in row.task.execution_reason

    def test_enqueue_anchors_a_synthetic_ticket_for_the_interpret_task(self) -> None:
        directive = _directive()
        row = DirectiveDispatch.enqueue(directive=directive, contract="c")
        assert row is not None
        assert row.task is not None
        assert row.task.ticket is not None
        assert f"directive={directive.pk}" in row.task.ticket.issue_url

    def test_enqueue_is_idempotent_per_generation(self) -> None:
        directive = _directive()
        first = DirectiveDispatch.enqueue(directive=directive, contract="c")
        second = DirectiveDispatch.enqueue(directive=directive, contract="c")
        assert first is not None
        assert second is None  # the same generation arms no second interpreter
        assert DirectiveDispatch.objects.filter(directive=directive).count() == 1

    def test_a_bumped_generation_arms_a_fresh_interpreter(self) -> None:
        directive = _directive()
        first = DirectiveDispatch.enqueue(directive=directive, contract="c")
        directive.bump_generation()
        second = DirectiveDispatch.enqueue(directive=directive, contract="c")
        assert first is not None
        assert second is not None
        assert second.generation == 1
        assert DirectiveDispatch.objects.filter(directive=directive).count() == 2

    def test_the_recorder_reaches_the_directive_from_the_task(self) -> None:
        # The reverse link the server-side recorder walks: task -> dispatch -> directive.
        directive = _directive()
        row = DirectiveDispatch.enqueue(directive=directive, contract="c")
        assert row is not None
        assert row.task is not None
        assert row.task.directive_dispatches.first().directive_id == directive.pk


class TestDirectiveDispatchReArm(TestCase):
    """A prior interpreter that recorded no interpretation must not strand the directive.

    The dedup that keeps a re-tick from spawning a second interpreter must hold ONLY
    while an interpreter is in flight. Once the prior interpret task reaches a terminal
    status without an interpretation being recorded — the governor refused it and the
    artifact sweep completed the PENDING task, or the run itself failed the evidence
    gate — the directive is still awaiting interpretation, so a re-tick RE-ARMS a fresh
    interpreter rather than dedup-returning ``None`` forever.
    """

    def test_a_live_pending_interpreter_is_not_rearmed(self) -> None:
        directive = _directive()
        first = DirectiveDispatch.enqueue(directive=directive, contract="c")
        assert first is not None
        assert first.task is not None
        assert first.task.status == Task.Status.PENDING  # in flight
        assert DirectiveDispatch.enqueue(directive=directive, contract="c") is None  # dedup holds

    def test_a_completed_interpreter_that_recorded_nothing_is_rearmed(self) -> None:
        directive = _directive()
        first = DirectiveDispatch.enqueue(directive=directive, contract="c")
        assert first is not None
        assert first.task is not None
        old_task = first.task
        old_task.complete()  # the observed defect: swept-complete with no envelope
        assert directive.state == Directive.State.CAPTURED  # never advanced past intake

        rearmed = DirectiveDispatch.enqueue(directive=directive, contract="c")
        assert rearmed is not None  # NOT dedup-suppressed
        assert rearmed.task is not None
        assert rearmed.task.pk != old_task.pk  # a fresh interpreter task
        assert rearmed.task.status == Task.Status.PENDING
        # Same generation → one dispatch row, its task re-pointed (no unique-constraint churn).
        assert DirectiveDispatch.objects.filter(directive=directive).count() == 1
        first.refresh_from_db()
        assert first.task_id == rearmed.task.pk

    def test_a_failed_interpreter_that_recorded_nothing_is_rearmed(self) -> None:
        directive = _directive()
        first = DirectiveDispatch.enqueue(directive=directive, contract="c")
        assert first is not None
        assert first.task is not None
        first.task.fail(reason="missing required evidence: bad envelope")  # run-then-fail
        rearmed = DirectiveDispatch.enqueue(directive=directive, contract="c")
        assert rearmed is not None
        assert rearmed.task is not None
        assert rearmed.task.status == Task.Status.PENDING

    def test_rearm_leaves_a_bumped_generation_as_its_own_fresh_row(self) -> None:
        # A clarification bump is orthogonal to re-arm: it still arms its own row.
        directive = _directive()
        first = DirectiveDispatch.enqueue(directive=directive, contract="c")
        assert first is not None
        assert first.task is not None
        first.task.complete()
        directive.bump_generation()
        second = DirectiveDispatch.enqueue(directive=directive, contract="c")
        assert second is not None
        assert second.generation == 1
        assert DirectiveDispatch.objects.filter(directive=directive).count() == 2
