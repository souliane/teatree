"""directive_loop.ratify (north-star PR-6): the ONLY writer of the ADMITTED state.

Verbatim the outer-loop shape: ``ask_ratification`` renders the FULL sketch (so the
human ratifies the design direction), ``try_admit`` is the sole ``admit()`` call site,
and there is NO auto-admit path — a directive cannot become ADMITTED without a consumed
approval. The rejection path records the human's words.
"""

import pytest
from django.test import TestCase

from teatree.core.models import DeferredQuestion, Directive
from teatree.core.models.mechanism_sketch import sketch_from_envelope
from teatree.loops.directive_loop.ratify import ask_ratification, try_admit
from tests.teatree_core.models.test_mechanism_sketch import valid_envelope


def _interpreted_directive() -> Directive:
    directive = Directive.objects.capture("max 1 MR per repo for overlay X", source=Directive.Source.CLI)
    directive.record_interpretation(sketch_from_envelope(valid_envelope()), constraint_statement="at most 1 open PR")
    return directive


class TestAskRatification(TestCase):
    def test_ask_renders_the_full_sketch_and_moves_to_ratify_pending(self) -> None:
        directive = _interpreted_directive()
        question = ask_ratification(directive)
        assert directive.state == Directive.State.RATIFY_PENDING
        # The human ratifies the DESIGN — setting, chokepoint, and the named rejected alternative.
        assert "max_open_prs_per_repo_per_ticket" in question.question
        assert "pr_budget_gate" in question.question
        assert "rejected alternatives" in question.question

    def test_ask_refuses_a_directive_with_no_sketch(self) -> None:
        directive = Directive.objects.capture("not interpreted", source=Directive.Source.CLI)
        with pytest.raises(ValueError, match="no interpreted sketch"):
            ask_ratification(directive)


class TestTryAdmit(TestCase):
    def test_pending_while_the_question_is_unanswered(self) -> None:
        directive = _interpreted_directive()
        ask_ratification(directive)
        assert try_admit(directive) == "pending"
        assert directive.state == Directive.State.RATIFY_PENDING

    def test_an_approval_admits(self) -> None:
        directive = _interpreted_directive()
        question = ask_ratification(directive)
        DeferredQuestion.consume(question.pk, answer="approve")
        directive.refresh_from_db()
        assert try_admit(directive) == "admitted"
        assert directive.state == Directive.State.ADMITTED

    def test_a_denial_rejects_with_the_humans_words(self) -> None:
        directive = _interpreted_directive()
        question = ask_ratification(directive)
        DeferredQuestion.consume(question.pk, answer="no, scope it to open PRs only")
        directive.refresh_from_db()
        assert try_admit(directive) == "rejected"
        assert directive.state == Directive.State.REJECTED
        assert "scope it to open PRs only" in directive.decision_reason
