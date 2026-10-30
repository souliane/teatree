"""The nag scanner bumps questions only inside their answerable threads."""

import datetime as dt
from unittest.mock import MagicMock, patch

from django.db import OperationalError
from django.test import TestCase
from django.utils import timezone

from teatree.core import notify as notify_module
from teatree.core.models import DeferredQuestion
from teatree.loop.domain_jobs import _global_dispatch_jobs, _run_job
from teatree.loop.job_identity import _ScannerJob
from teatree.loop.scanners.question_backlog_nag import QuestionBacklogNagScanner
from teatree.loop.scanners.scan_deadline import scanner_deadline
from tests._owner_channel import OWNER_DECISION


class TestQuestionBacklogNagScanner(TestCase):
    def test_mirrored_backlog_posts_only_per_question_bumps(self) -> None:
        posted_at = timezone.now() - dt.timedelta(hours=3)
        card_ts = [f"{posted_at.timestamp() + index:.6f}" for index in range(3)]
        for index, ts in enumerate(card_ts):
            row = DeferredQuestion.record(
                f"Which route {index}?",
                session_id=f"scanner-{index}",
                slack_channel="D-USER",
                slack_ts=ts,
                **OWNER_DECISION,
            )
            DeferredQuestion.objects.filter(pk=row.pk).update(created_at=posted_at)
        backend = MagicMock()
        backend.open_dm.return_value = "D-USER"
        backend.post_message.side_effect = [
            {"ok": True, "ts": f"{posted_at.timestamp() + 100 + index:.6f}"} for index in range(3)
        ]
        backend.get_permalink.return_value = "https://slack.example/archives/D-USER/p123"

        with patch.object(notify_module, "messaging_from_overlay", return_value=backend):
            signals = QuestionBacklogNagScanner(backend=backend, user_id="U_ME").scan()

        posts = backend.post_message.call_args_list
        assert len(posts) == 3
        assert {call.kwargs["thread_ts"] for call in posts} == set(card_ts)
        assert all("decisions are waiting" not in call.kwargs["text"].lower() for call in posts)
        assert signals[0].payload == {"mirrored_pending": 3, "bumped": 3}

    def test_no_signal_when_no_question_is_bumped(self) -> None:
        with patch("teatree.core.notify_question_drains.reask_escalated_questions", return_value=(0, 7)):
            assert QuestionBacklogNagScanner().scan() == []

    def test_emits_signal_when_questions_are_bumped(self) -> None:
        deadline = 123.0
        with (
            patch("teatree.core.notify_question_drains.reask_escalated_questions", return_value=(3, 42)) as drain,
            scanner_deadline(deadline),
        ):
            signals = QuestionBacklogNagScanner().scan()
        assert drain.call_args.kwargs["deadline"] is deadline
        assert len(signals) == 1
        assert signals[0].kind == "deferred_question.resurfaced"
        assert signals[0].payload == {"mirrored_pending": 42, "bumped": 3}
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
