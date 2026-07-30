"""directive_loop.interpret (north-star PR-6): the headless interpret contract + dispatch.

The contract writes the mechanism-design doctrine down once — duplication-check, the
core-seam-not-overlay rule, constraint-as-data, and the N=2 litmus — and cites the PR-2
exemplar, so the interpreter produces a sketch that names its rejected alternatives.
The dispatch arms exactly one headless interpret task per generation (the CriticDispatch
idiom), never a second on a re-tick.
"""

from django.test import TestCase

from teatree.core.models import DeferredQuestion, Directive, DirectiveDispatch
from teatree.loops.directive_loop.interpret import (
    build_interpreter_contract,
    clarifications_answered,
    dispatch_interpretation,
    reinterpret_after_clarification,
)


def _directive() -> Directive:
    return Directive.objects.capture("max 1 MR per repo for overlay X", source=Directive.Source.CLI)


class TestBuildInterpreterContract(TestCase):
    def test_the_contract_embeds_the_anti_hack_doctrine_and_the_raw_text(self) -> None:
        contract = build_interpreter_contract(_directive())
        assert "DUPLICATION FIRST" in contract
        assert "CORE SEAM, NOT OVERLAY" in contract
        assert "N=2 LITMUS" in contract
        assert "rejected_alternatives" in contract
        assert "max 1 MR per repo for overlay X" in contract  # the verbatim directive

    def test_the_contract_cites_the_pr2_exemplar(self) -> None:
        contract = build_interpreter_contract(_directive())
        assert "pr_budget_gate" in contract
        assert "max_open_prs_per_repo_per_ticket" in contract


class TestDispatchInterpretation(TestCase):
    def test_dispatch_arms_one_headless_interpret_task(self) -> None:
        directive = _directive()
        row = dispatch_interpretation(directive)
        assert row is not None
        assert row.task is not None
        assert row.task.phase == "directive_interpreting"
        assert "DUPLICATION FIRST" in row.task.execution_reason  # the doctrine rode into the task

    def test_dispatch_is_idempotent_within_a_generation(self) -> None:
        directive = _directive()
        assert dispatch_interpretation(directive) is not None
        assert dispatch_interpretation(directive) is None  # no second interpreter this generation
        assert DirectiveDispatch.objects.filter(directive=directive).count() == 1


class TestClarificationRoundTrip(TestCase):
    def test_no_questions_this_generation_is_not_answered(self) -> None:
        directive = _directive()
        directive.mark_clarifying()
        assert clarifications_answered(directive) is False  # no clarify questions recorded yet

    def test_all_answered_then_reinterpret_bumps_the_generation(self) -> None:
        directive = _directive()
        directive.mark_clarifying()
        question = DeferredQuestion.record("which?", options_hash=f"directive_clarify:{directive.pk}:0:0")
        assert clarifications_answered(directive) is False  # still unanswered
        DeferredQuestion.consume(question.pk, answer="this one")
        assert clarifications_answered(directive) is True
        row = reinterpret_after_clarification(directive)
        assert directive.generation == 1
        assert row is not None
