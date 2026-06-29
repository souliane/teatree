"""Behaviour tests for :class:`DeferredQuestionPosterScanner`.

The tick-level peer of :class:`UndeliveredNotifyScanner`: it drains
un-mirrored ``DeferredQuestion`` rows through
:func:`teatree.core.notify_question_drains.drain_unmirrored_deferred_questions` so a
headless ``needs_user_input`` STOP (and the orphaned stall-escalation
rows) reach the user's Slack DM. Side-effecting; emits a signal only when
it actually mirrors something.
"""

from unittest.mock import patch

from django.db import OperationalError
from django.test import TestCase

from teatree.loop.scanners.deferred_question_poster import DeferredQuestionPosterScanner


class TestDeferredQuestionPosterScanner(TestCase):
    def test_no_signal_when_nothing_mirrored(self) -> None:
        with patch(
            "teatree.core.notify_question_drains.drain_unmirrored_deferred_questions",
            return_value=(0, 0),
        ):
            assert DeferredQuestionPosterScanner().scan() == []

    def test_emits_signal_when_questions_mirrored(self) -> None:
        with patch(
            "teatree.core.notify_question_drains.drain_unmirrored_deferred_questions",
            return_value=(1, 2),
        ):
            signals = DeferredQuestionPosterScanner().scan()
        assert len(signals) == 1
        assert signals[0].kind == "deferred_question.mirrored"
        assert signals[0].payload == {"mirrored": 1, "total": 2}

    def test_db_unavailable_is_silent_noop(self) -> None:
        with patch(
            "teatree.core.notify_question_drains.drain_unmirrored_deferred_questions",
            side_effect=OperationalError("no such table: teatree_deferred_question"),
        ):
            assert DeferredQuestionPosterScanner().scan() == []

    def test_unexpected_error_never_raises(self) -> None:
        with patch(
            "teatree.core.notify_question_drains.drain_unmirrored_deferred_questions",
            side_effect=RuntimeError("boom"),
        ):
            assert DeferredQuestionPosterScanner().scan() == []
