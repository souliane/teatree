"""The ratify decision rests on the provenance the server recorded for the answer, and on nothing else."""

from unittest.mock import patch

import pytest
from django.db import DatabaseError
from django.test import TestCase

from teatree.core.models import DeferredQuestion, Directive, DirectiveError
from teatree.core.models.mechanism_sketch import sketch_from_envelope
from teatree.loops.directive_loop.ratify import ask_ratification, try_admit
from tests.teatree_core.models.test_mechanism_sketch import valid_envelope

_NON_OWNER_PROVENANCE = (
    DeferredQuestion.ResolvedVia.AGENT,
    DeferredQuestion.ResolvedVia.LOCAL,
    DeferredQuestion.ResolvedVia.STALE,
    "unknown",
)
_DECISIVE_ANSWERS = ("approve", "reject")


def _answered(resolved_via: str | None, answer: str = "approve") -> Directive:
    directive = Directive.objects.capture("max 1 MR per repo", source=Directive.Source.CLI)
    directive.record_interpretation(sketch_from_envelope(valid_envelope()), constraint_statement="at most 1 open PR")
    question = ask_ratification(directive)
    if resolved_via is None:
        DeferredQuestion.consume(question.pk, answer=answer)
    else:
        question.apply_answer(answer, resolved_via=resolved_via)
    directive.refresh_from_db()
    assert directive.ratify_question is not None
    return directive


class _UnreadableOwnerSet:
    def __contains__(self, item: object) -> bool:
        msg = "owner set unreadable"
        raise DatabaseError(msg)


def _unreadable_provenance(_question: DeferredQuestion) -> str:
    msg = "provenance unreadable"
    raise DatabaseError(msg)


class TestRatifyDecisionRestsOnRecordedProvenance(TestCase):
    def _assert_refused(self, directive: Directive) -> None:
        with pytest.raises(DirectiveError, match="owner channel"):
            directive.admit()
        first = directive.ratify_question
        assert try_admit(directive) == "reasked"
        directive.refresh_from_db()
        assert directive.state == Directive.State.RATIFY_PENDING
        assert directive.ratify_question is not None
        assert directive.ratify_question.pk != first.pk

    def _assert_never_admits(self, directive: Directive) -> None:
        with pytest.raises(DatabaseError):
            directive.admit()
        with pytest.raises(DatabaseError):
            try_admit(directive)

    def test_an_answer_with_no_recorded_provenance_is_refused(self) -> None:
        self._assert_refused(_answered(None))

    def test_every_non_owner_provenance_is_refused(self) -> None:
        for resolved_via in _NON_OWNER_PROVENANCE:
            with self.subTest(resolved_via=resolved_via):
                self._assert_refused(_answered(resolved_via))

    def test_a_failed_provenance_fetch_never_admits(self) -> None:
        for answer in _DECISIVE_ANSWERS:
            with self.subTest(answer=answer):
                directive = _answered(DeferredQuestion.ResolvedVia.SLACK, answer)
                with patch.object(DeferredQuestion, "resolved_via", new=property(_unreadable_provenance)):
                    self._assert_never_admits(directive)
                directive.refresh_from_db()
                assert directive.state == Directive.State.RATIFY_PENDING

    def test_a_failed_owner_set_read_never_admits(self) -> None:
        for answer in _DECISIVE_ANSWERS:
            with self.subTest(answer=answer):
                directive = _answered(DeferredQuestion.ResolvedVia.SLACK, answer)
                with patch.object(DeferredQuestion, "OWNER_CHANNELS", new=_UnreadableOwnerSet()):
                    self._assert_never_admits(directive)
                directive.refresh_from_db()
                assert directive.state == Directive.State.RATIFY_PENDING

    def test_the_re_ask_never_mentions_the_command_line(self) -> None:
        directive = _answered(DeferredQuestion.ResolvedVia.AGENT)
        assert try_admit(directive) == "reasked"
        directive.refresh_from_db()
        assert directive.ratify_question is not None
        text = directive.ratify_question.question
        assert "reply to this Slack DM" in text
        assert "questions answer" not in text
        assert "terminal" not in text
