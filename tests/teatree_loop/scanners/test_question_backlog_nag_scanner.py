"""Behaviour tests for :class:`QuestionBacklogNagScanner`.

The recurring caller the resurface mechanism never had: it drives
:func:`teatree.core.notify_question_drains.resurface_question_backlog` once per
tick, and that function's interval bucket decides whether a message actually
goes out. Side-effecting; emits a signal only when a digest was delivered.
"""

from unittest.mock import patch

from django.db import OperationalError
from django.test import TestCase

from teatree.loop.domain_jobs import _global_dispatch_jobs
from teatree.loop.scanners.question_backlog_nag import QuestionBacklogNagScanner


class TestQuestionBacklogNagScanner(TestCase):
    def test_no_signal_when_the_interval_suppressed_the_digest(self) -> None:
        with patch("teatree.core.notify_question_drains.resurface_question_backlog", return_value=(False, 7)):
            assert QuestionBacklogNagScanner().scan() == []

    def test_emits_signal_when_the_digest_was_delivered(self) -> None:
        with patch("teatree.core.notify_question_drains.resurface_question_backlog", return_value=(True, 42)):
            signals = QuestionBacklogNagScanner().scan()
        assert len(signals) == 1
        assert signals[0].kind == "deferred_question.resurfaced"
        assert signals[0].payload == {"pending": 42}

    def test_db_unavailable_is_silent_noop(self) -> None:
        with patch(
            "teatree.core.notify_question_drains.resurface_question_backlog",
            side_effect=OperationalError("no such table: teatree_deferred_question"),
        ):
            assert QuestionBacklogNagScanner().scan() == []

    def test_unexpected_error_never_raises(self) -> None:
        with patch(
            "teatree.core.notify_question_drains.resurface_question_backlog",
            side_effect=RuntimeError("boom"),
        ):
            assert QuestionBacklogNagScanner().scan() == []


class TestNagRunsOnEveryTick(TestCase):
    """The whole point: something calls the resurface. Without this it is dead code."""

    def test_the_global_dispatch_set_carries_the_nag_scanner(self) -> None:
        names = [job.scanner.name for job in _global_dispatch_jobs()]

        assert "question_backlog_nag" in names
