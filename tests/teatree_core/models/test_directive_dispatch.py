"""DirectiveDispatch (north-star PR-6): the idempotent enqueue of the headless interpreter.

Mirrors ``CriticDispatch``: one row per ``(directive, purpose, generation)`` linking a
claimable headless ``Task(phase="directive_interpreting")``. A re-fire at the same
generation returns ``None`` (no second interpreter); a bumped generation arms a fresh
one. The interpret task needs a ``Ticket``, so the dispatch anchors a synthetic one.
"""

from django.test import TestCase

from teatree.core.models import Directive, DirectiveDispatch


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
