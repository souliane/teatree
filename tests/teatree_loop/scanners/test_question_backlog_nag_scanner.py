"""The nag scanner bumps questions only inside their answerable threads."""

from unittest.mock import patch

from django.db import OperationalError
from django.test import TestCase

from teatree.loop.domain_jobs import _global_dispatch_jobs, _run_job
from teatree.loop.job_identity import _ScannerJob
from teatree.loop.scanners.question_backlog_nag import QuestionBacklogNagScanner


class TestQuestionBacklogNagScanner(TestCase):
    def test_no_signal_when_no_question_is_bumped(self) -> None:
        with patch("teatree.core.notify_question_drains.reask_escalated_questions", return_value=(0, 7)):
            assert QuestionBacklogNagScanner().scan() == []

    def test_emits_signal_when_questions_are_bumped(self) -> None:
        with patch("teatree.core.notify_question_drains.reask_escalated_questions", return_value=(3, 42)):
            signals = QuestionBacklogNagScanner().scan()
        assert len(signals) == 1
        assert signals[0].kind == "deferred_question.resurfaced"
        assert signals[0].payload == {"pending": 42, "bumped": 3}
        assert "digest" not in signals[0].summary

    def test_db_unavailable_is_silent_noop(self) -> None:
        with patch(
            "teatree.core.notify_question_drains.reask_escalated_questions",
            side_effect=OperationalError("no such table: teatree_deferred_question"),
        ):
            assert QuestionBacklogNagScanner().scan() == []

    def test_failed_reask_reaches_the_tick_error_surface(self) -> None:
        with patch("teatree.core.notify_question_drains.reask_escalated_questions", side_effect=RuntimeError("boom")):
            _, signals, error = _run_job(_ScannerJob(scanner=QuestionBacklogNagScanner(), overlay=""))
        assert (signals, error) == ([], "RuntimeError: boom")


class TestNagRunsOnEveryTick(TestCase):
    def test_the_global_dispatch_set_carries_the_nag_scanner(self) -> None:
        names = [job.scanner.name for job in _global_dispatch_jobs()]
        assert "question_backlog_nag" in names
