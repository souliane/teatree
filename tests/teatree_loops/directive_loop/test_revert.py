"""The REVERT flow — human-ratified rollback, terminal REVERTED, config cleared (north-star PR-7).

Mirrors the outer loop's ask/resolve shape: ``ask_revert`` records the question,
``resolve_revert`` consumes it and drives the directive to terminal REVERTED with the
overlay config rolled back — no soft-lock (the slot-release lesson).
"""

from django.test import TestCase

from teatree.core.models import ConfigSetting, DeferredQuestion, Directive, FactoryScoreSnapshot
from teatree.core.models.mechanism_sketch import sketch_from_envelope
from teatree.loops.directive_loop.revert import ask_revert, resolve_revert
from tests.teatree_core.models.test_mechanism_sketch import valid_envelope

_SCOPE = "t3-teatree"
_KEY = "max_open_prs_per_repo_per_ticket"


def _revert_pending() -> Directive:
    directive = Directive.objects.capture("max 1 MR", source=Directive.Source.CLI, scope_overlay=_SCOPE)
    directive.record_interpretation(
        sketch_from_envelope(valid_envelope(kind="activation_only", acceptance_tests=[])), constraint_statement="c"
    )
    question = DeferredQuestion.record("Ratify?", options_hash=f"directive_ratify:{directive.pk}")
    directive.attach_ratification(question)
    DeferredQuestion.consume(question.pk, answer="approve")
    directive.refresh_from_db()
    directive.admit()
    directive.skip_to_configuring(
        baseline_snapshot=FactoryScoreSnapshot.objects.create(
            overlay="", window_days=7, recipe_sha="s", aggregate=0.7, verdict="ok", coverage=1.0, coverage_floor=0.6
        )
    )
    directive.begin_verifying()
    directive.request_revert(reason="regression")
    return directive


class TestRevertFlow(TestCase):
    def test_ask_revert_records_the_question(self) -> None:
        directive = _revert_pending()
        question = ask_revert(directive)
        assert directive.revert_question_id == question.pk
        assert "resolve-revert" in question.question

    def test_resolve_revert_reaches_terminal_reverted_and_clears_config(self) -> None:
        directive = _revert_pending()
        ConfigSetting.objects.set_value(_KEY, 1, scope=_SCOPE)  # a stray row to prove the safety-net clear
        resolve_revert(directive, revert_sha="cafef00d")
        directive.refresh_from_db()
        assert directive.state == Directive.State.REVERTED
        assert directive.is_terminal
        assert directive.extra["revert_sha"] == "cafef00d"
        assert ConfigSetting.objects.get_effective(_KEY, scope=_SCOPE) is None

    def test_resolve_revert_asks_first_if_no_question_yet(self) -> None:
        directive = _revert_pending()
        assert directive.revert_question is None
        resolve_revert(directive)
        directive.refresh_from_db()
        assert directive.state == Directive.State.REVERTED

    def test_resolve_revert_with_an_already_answered_question(self) -> None:
        # The human answered the revert ask out-of-band; resolve-revert closes it out
        # without re-consuming.
        directive = _revert_pending()
        question = ask_revert(directive)
        DeferredQuestion.consume(question.pk, answer="reverted")
        directive.refresh_from_db()
        resolve_revert(directive)
        directive.refresh_from_db()
        assert directive.state == Directive.State.REVERTED
