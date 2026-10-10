"""Behaviour tests for :class:`DeferredQuestionPosterScanner`.

The tick-level peer of :class:`UndeliveredNotifyScanner`: it drains
un-mirrored ``DeferredQuestion`` rows through
:func:`teatree.core.notify_question_drains.drain_unmirrored_deferred_questions` so a
headless ``needs_user_input`` STOP (and the orphaned stall-escalation
rows) reach the user's Slack DM. Side-effecting; emits a signal only when
it actually mirrors something.
"""

from unittest.mock import MagicMock, patch

from django.db import OperationalError
from django.test import TestCase

from teatree.core.models import DeferredQuestion, Session, Ticket
from teatree.loop.domain_jobs import _global_dispatch_jobs, _run_job
from teatree.loop.job_identity import _ScannerJob
from teatree.loop.question_drain import sweep_owner_questions
from teatree.loop.scanners.deferred_question_poster import DeferredQuestionPosterScanner
from tests._owner_channel import OWNER_DECISION


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

    def test_a_failed_drain_reaches_the_tick_error_surface(self) -> None:
        with patch(
            "teatree.core.notify_question_drains.drain_unmirrored_deferred_questions", side_effect=RuntimeError("boom")
        ):
            _, signals, error = _run_job(_ScannerJob(scanner=DeferredQuestionPosterScanner(), overlay=""))
        assert (signals, error) == ([], "RuntimeError: boom")


class TestSettleRunsBeforeTheMirror(TestCase):
    def _owner_question_whose_ticket_then_merged(self) -> DeferredQuestion:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.CODED)
        session = Session.objects.create(ticket=ticket, agent_id="coding")
        question = DeferredQuestion.record("May I post it?", task_session=session, **OWNER_DECISION)
        Ticket.objects.filter(pk=ticket.pk).update(state=Ticket.State.MERGED)
        return question

    def test_an_owner_question_whose_subject_finished_is_settled_not_posted(self) -> None:
        question = self._owner_question_whose_ticket_then_merged()
        backend = MagicMock()

        signals = DeferredQuestionPosterScanner(backend=backend, user_id="U_ME", settle=sweep_owner_questions).scan()

        assert signals == []
        backend.post_message.assert_not_called()
        question.refresh_from_db()
        assert not question.is_pending

    def test_without_a_settle_step_the_same_question_is_posted(self) -> None:
        self._owner_question_whose_ticket_then_merged()
        backend = MagicMock()

        DeferredQuestionPosterScanner(backend=backend, user_id="U_ME").scan()

        backend.post_message.assert_called_once()

    def test_the_global_dispatch_poster_settles_with_the_owner_sweep(self) -> None:
        poster = next(job.scanner for job in _global_dispatch_jobs() if job.scanner.name == "deferred_question_poster")

        assert poster.settle is sweep_owner_questions
