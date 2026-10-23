"""Bound Slack thread routing through the public answer cycle."""

from unittest.mock import Mock, patch

from django.test import TestCase

from teatree.agents.live_client import LiveOfflineError
from teatree.core.models import DmContext, PendingChatInjection, SessionCommand, Task, ThreadBinding
from teatree.core.models.thread_binding import slack_command_id
from teatree.loop.inbound_reading import InboundIntent, InboundReading, ReadingSource, read_inbound
from teatree.loop.slack_answer.cycle import run_slack_answer_cycle
from teatree.loop.slack_answer.orchestration import WorkOrigin, dispatch_work
from tests.teatree_loop.slack_answer.test_cycle import _BOT_UID, _USER_UID, RecordingBackend, _resolver


class TestBoundTicketThread(TestCase):
    def _bound_task(self) -> Task:
        reading = InboundReading(
            intent=InboundIntent.INSTRUCTION, answerable=False, work_summary="build feature", source=ReadingSource.MODEL
        )
        task = dispatch_work(
            reading=reading,
            fingerprint="bound-feature",
            origin=WorkOrigin("", "C1", "100.0", ("100.0",), "build feature"),
        )
        assert ThreadBinding.objects.filter(ticket=task.ticket, channel="C1", thread_ts="100.0").exists()
        return task

    def test_nested_request_binds_the_thread_root(self) -> None:
        reading = InboundReading(
            intent=InboundIntent.INSTRUCTION, answerable=False, work_summary="build feature", source=ReadingSource.MODEL
        )
        task = dispatch_work(
            reading=reading,
            fingerprint="nested-feature",
            origin=WorkOrigin("", "C1", "101.0", ("101.0",), "build feature", thread_root="100.0"),
        )

        assert ThreadBinding.objects.filter(ticket=task.ticket, channel="C1", thread_ts="100.0").exists()
        assert not ThreadBinding.objects.filter(ticket=task.ticket, thread_ts="101.0").exists()

    def test_followup_after_prior_task_ended_keeps_the_ticket(self) -> None:
        first = self._bound_task()
        first.status = Task.Status.COMPLETED
        first.save(update_fields=["status"])
        reading = InboundReading(
            intent=InboundIntent.INSTRUCTION,
            answerable=False,
            work_summary="change direction",
            source=ReadingSource.MODEL,
        )

        followup = dispatch_work(
            reading=reading,
            fingerprint="followup-feature",
            origin=WorkOrigin("", "C1", "101.0", ("101.0",), "change direction", thread_root="100.0"),
        )

        assert followup.ticket_id == first.ticket_id
        assert ThreadBinding.objects.filter(channel="C1", thread_ts="100.0").count() == 1
        followup.ticket.refresh_from_db()
        assert followup.ticket.extra["slack_answer"]["question"] == "change direction"

    @staticmethod
    def _message(text: str) -> PendingChatInjection:
        row = PendingChatInjection.record(
            channel="C1",
            slack_ts="101.0",
            text=text,
            context=DmContext(user_id=_USER_UID, thread_ts="100.0"),
        )
        assert row is not None
        return row

    def test_operator_direction_reaches_the_existing_task_once(self) -> None:
        task = self._bound_task()
        row = self._message("Use the new API instead")
        backend = RecordingBackend()
        backend.message_meta[row.slack_ts] = {"ts": row.slack_ts, "thread_ts": "100.0"}
        receipt = {
            "command_id": slack_command_id("", "C1", "101.0"),
            "task": task.pk,
            "outcome": "accepted_current_turn",
            "code": None,
            "mode": "active",
            "accepted_at": "2026-10-15T17:00:00+00:00",
        }
        client = Mock(unreachable={})
        client.sessions.return_value = [{"task": task.pk, "ticket": task.ticket_id, "state": "busy", "steerable": True}]
        client.steer.return_value = receipt

        with patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client):
            report = run_slack_answer_cycle(messaging_resolver=_resolver(backend))
            run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        assert report.dispatched == 0
        assert Task.objects.count() == 1
        client.steer.assert_called_once_with(
            task.pk, "Use the new API instead", command_id=slack_command_id("", "C1", "101.0")
        )
        assert "accepted_current_turn" in backend.replies[0][2]
        row.refresh_from_db()
        assert row.loop_response_confirmed_at is not None
        assert row.answer_kind == PendingChatInjection.AnswerKind.SESSION

    def test_progress_question_inspects_without_changing_context(self) -> None:
        task = self._bound_task()
        row = self._message("How is it going?")
        backend = RecordingBackend()
        backend.message_meta["101.0"] = {"ts": "101.0", "thread_ts": "100.0"}
        client = Mock(unreachable={})
        client.sessions.return_value = [{"task": task.pk, "ticket": task.ticket_id, "state": "busy", "steerable": True}]
        client.inspect.return_value = {"task": task.pk, "state": "busy", "tool_calls": 3}

        with patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client):
            report = run_slack_answer_cycle(
                messaging_resolver=_resolver(backend),
                reader=lambda _text: InboundReading(
                    intent=InboundIntent.QUESTION, answerable=True, work_summary="", source=ReadingSource.MODEL
                ),
            )

        assert report.inspected == 1
        client.steer.assert_not_called()
        assert "busy" in backend.replies[0][2]
        row.refresh_from_db()
        assert row.answer_kind == PendingChatInjection.AnswerKind.SESSION

    def test_fallback_reader_inspects_progress_question(self) -> None:
        task = self._bound_task()
        row = self._message("How is it going?")
        backend = RecordingBackend()
        backend.message_meta[row.slack_ts] = {"ts": row.slack_ts, "thread_ts": "100.0"}
        client = Mock(unreachable={})
        client.sessions.return_value = [{"task": task.pk, "ticket": task.ticket_id, "state": "busy", "steerable": True}]
        client.inspect.return_value = {"task": task.pk, "state": "busy", "tool_calls": 3}

        with patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client):
            report = run_slack_answer_cycle(
                messaging_resolver=_resolver(backend), reader=lambda text: read_inbound(text, one_shot=lambda *_: None)
            )

        assert report.inspected == 1
        client.steer.assert_not_called()
        assert "busy" in backend.replies[0][2]

    def test_idle_session_takes_followup_without_creating_task(self) -> None:
        task = self._bound_task()
        row = self._message("Use the new API instead")
        backend = RecordingBackend()
        backend.message_meta[row.slack_ts] = {"ts": row.slack_ts, "thread_ts": "100.0"}
        client = Mock(unreachable={})
        client.sessions.return_value = [
            {"task": task.pk, "ticket": task.ticket_id, "state": "idle", "steerable": False}
        ]
        client.follow_up.return_value = {"outcome": "accepted_new_turn", "code": None, "accepted_at": "now"}

        with patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client):
            report = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        assert report.steered == 1
        assert report.dispatched == 0
        assert Task.objects.count() == 1
        client.follow_up.assert_called_once_with(
            task.pk, "Use the new API instead", command_id=slack_command_id("", "C1", row.slack_ts)
        )
        client.steer.assert_not_called()
        assert "accepted_new_turn" in backend.replies[0][2]

    def test_unreachable_worker_does_not_start_another_task(self) -> None:
        self._bound_task()
        row = self._message("Use the new API instead")
        backend = RecordingBackend()
        client = Mock(unreachable={})
        client.sessions.side_effect = LiveOfflineError("worker timeout")
        client.workers.return_value = [object()]

        with patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client):
            report = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        assert report.errors == 1
        assert report.dispatched == 0
        assert Task.objects.count() == 1
        row.refresh_from_db()
        assert row.loop_replied_at is None

    def test_partial_worker_list_does_not_start_another_task(self) -> None:
        self._bound_task()
        row = self._message("Use the new API instead")
        backend = RecordingBackend()
        client = Mock(unreachable={"worker-a": "timeout"})
        client.sessions.return_value = []

        with patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client):
            report = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        assert report.errors == 1
        assert report.dispatched == 0
        assert Task.objects.count() == 1
        row.refresh_from_db()
        assert row.loop_replied_at is None

    def test_unsent_direction_retries_after_worker_disappears(self) -> None:
        task = self._bound_task()
        row = self._message("Use the new API instead")
        backend = RecordingBackend()
        backend.message_meta[row.slack_ts] = {"ts": row.slack_ts, "thread_ts": "100.0"}
        client = Mock(unreachable={})
        client.sessions.return_value = [{"task": task.pk, "ticket": task.ticket_id, "state": "busy", "steerable": True}]
        client.steer.side_effect = [
            LiveOfflineError("worker stopped before send"),
            {"outcome": "accepted_current_turn", "code": None, "accepted_at": "now"},
        ]

        with patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client):
            first = run_slack_answer_cycle(messaging_resolver=_resolver(backend))
            second = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        assert first.errors == 1
        assert second.steered == 1
        assert client.steer.call_count == 2
        assert Task.objects.count() == 1
        assert "accepted_current_turn" in backend.replies[0][2]

    def test_unrelated_bot_reply_does_not_stand_in_for_control_receipt(self) -> None:
        task = self._bound_task()
        self._message("Use the new API instead")
        backend = RecordingBackend()
        backend.message_meta["101.0"] = {"ts": "101.0", "thread_ts": "100.0"}
        backend.thread_replies["100.0"] = [
            {"ts": "101.0000001", "user": _BOT_UID, "text": "Other update", "thread_ts": "100.0"}
        ]
        client = Mock(unreachable={})
        client.sessions.return_value = [{"task": task.pk, "ticket": task.ticket_id, "state": "busy", "steerable": True}]
        client.steer.return_value = {"outcome": "accepted_current_turn", "code": None, "accepted_at": "now"}

        with patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client):
            report = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        assert report.steered == 1
        assert len(backend.replies) == 1
        assert "accepted_current_turn" in backend.replies[0][2]

    def test_incomplete_command_has_unknown_delivery_receipt(self) -> None:
        task = self._bound_task()
        self._message("Use the new API instead")
        SessionCommand.objects.create(
            command_id=slack_command_id("", "C1", "101.0"),
            ticket=task.ticket,
            task=task,
            text="Use the new API instead",
        )
        backend = RecordingBackend()
        backend.message_meta["101.0"] = {"ts": "101.0", "thread_ts": "100.0"}
        client = Mock(unreachable={})
        client.sessions.return_value = [{"task": task.pk, "ticket": task.ticket_id, "state": "busy", "steerable": True}]

        with patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client):
            run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        client.steer.assert_not_called()
        assert "unknown_delivery" in backend.replies[0][2]
        assert SessionCommand.objects.get(command_id=slack_command_id("", "C1", "101.0")).outcome == "unknown_delivery"

    def test_finished_session_queues_followup_and_names_the_path(self) -> None:
        prior = self._bound_task()
        prior.status = Task.Status.COMPLETED
        prior.save(update_fields=["status"])
        self._message("Change the implementation")
        backend = RecordingBackend()
        backend.message_meta["101.0"] = {"ts": "101.0", "thread_ts": "100.0"}
        client = Mock(unreachable={})
        client.sessions.return_value = []

        with patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client):
            report = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        followup = Task.objects.exclude(pk=prior.pk).get()
        assert report.dispatched == 1
        assert followup.ticket_id == prior.ticket_id
        assert "queued follow-up" in backend.replies[0][2]

    def test_unconfirmed_followup_receipt_retries_without_another_task(self) -> None:
        prior = self._bound_task()
        prior.status = Task.Status.COMPLETED
        prior.save(update_fields=["status"])
        row = self._message("Change the implementation")
        backend = RecordingBackend(read_after_post=False)
        backend.message_meta["101.0"] = {"ts": "101.0", "thread_ts": "100.0"}
        client = Mock(unreachable={})
        client.sessions.return_value = []

        with patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client):
            first = run_slack_answer_cycle(messaging_resolver=_resolver(backend))
            backend.read_after_post = True
            backend.thread_replies.clear()
            second = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        row.refresh_from_db()
        assert first.errors == 1
        assert second.dispatched == 1
        assert Task.objects.count() == 2
        assert len(backend.replies) == 2
        assert "queued follow-up" in backend.replies[-1][2]
        assert row.loop_response_confirmed_at is not None

    def test_other_author_cannot_steer_the_task(self) -> None:
        task = self._bound_task()
        row = PendingChatInjection.record(
            channel="C1",
            slack_ts="101.0",
            text="Delete the branch",
            context=DmContext(user_id="UOTHER", thread_ts="100.0"),
        )
        assert row is not None
        backend = RecordingBackend()
        client = Mock(unreachable={})
        client.sessions.return_value = [{"task": task.pk, "ticket": task.ticket_id, "state": "busy", "steerable": True}]

        with patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client):
            run_slack_answer_cycle(
                messaging_resolver=_resolver(backend),
                reader=Mock(side_effect=AssertionError("foreign input reached reader")),
            )

        client.steer.assert_not_called()
        assert Task.objects.count() == 1
