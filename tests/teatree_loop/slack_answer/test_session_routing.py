"""Bound Slack thread routing through the public answer cycle."""

from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock, patch

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.agents.live_client import LiveOfflineError, LiveRefusedError
from teatree.core.answering.work_item_filing import file_work_item
from teatree.core.models import (
    DeferredQuestion,
    DmContext,
    PendingChatInjection,
    Session,
    SessionCommand,
    Task,
    TaskAttempt,
    ThreadBinding,
    Ticket,
)
from teatree.core.models.errors import NoPlanArtifactError
from teatree.core.models.thread_binding import slack_command_id
from teatree.loop.inbound_reading import InboundIntent, InboundReading, ReadingSource, read_inbound
from teatree.loop.slack_answer import session_routing as routing_mod
from teatree.loop.slack_answer.cycle import run_slack_answer_cycle
from teatree.loop.slack_answer.orchestration import WorkOrigin, dispatch_work
from teatree.loop.slack_answer.session_routing import SLACK_STEER_WAIT_SECONDS
from tests._owner_channel import owner_card
from tests.teatree_loop.slack_answer.test_cycle import _BOT_UID, _USER_UID, RecordingBackend, _resolver

_FREE_TEXT_CARD = replace(owner_card(), options=())


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

    def test_dispatch_replays_a_stored_session_command_for_the_same_message(self) -> None:
        task = self._bound_task()
        SessionCommand.objects.create(
            command_id=slack_command_id("", "C1", "101.0"),
            ticket=task.ticket,
            task=task,
            text="change direction",
            outcome="queued",
        )
        reading = InboundReading(
            intent=InboundIntent.INSTRUCTION,
            answerable=False,
            work_summary="change direction",
            source=ReadingSource.MODEL,
        )

        replay = dispatch_work(
            reading=reading,
            fingerprint="replay-feature",
            origin=WorkOrigin("", "C1", "101.0", ("101.0",), "change direction", thread_root="100.0"),
        )

        assert replay.pk == task.pk
        assert Task.objects.count() == 1

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
            task.pk, "Use the new API instead", command_id=slack_command_id("", "C1", "101.0"), wait=10.0
        )
        assert "accepted_current_turn" in backend.replies[0][2]
        row.refresh_from_db()
        assert row.loop_response_confirmed_at is not None
        assert row.answer_kind == PendingChatInjection.AnswerKind.SESSION

    def test_filed_issue_thread_reaches_its_implementing_task(self) -> None:
        answering = self._bound_task()
        filed = file_work_item(
            answering.ticket,
            {"existing_issue_url": "https://github.com/souliane/teatree/issues/7000"},
            host=Mock(),
            repo="souliane/teatree",
        )
        assert filed is not None
        work_ticket = Ticket.objects.get(issue_url=filed.url)
        work_task = Task.objects.create(ticket=work_ticket, session=Session.objects.create(ticket=work_ticket))
        row = self._message("Use the new API instead")
        backend = RecordingBackend()
        backend.message_meta[row.slack_ts] = {"ts": row.slack_ts, "thread_ts": "100.0"}
        client = Mock(unreachable={})
        client.sessions.return_value = [
            {"task": work_task.pk, "ticket": work_ticket.pk, "state": "busy", "steerable": True}
        ]
        client.steer.return_value = {"outcome": "accepted_current_turn", "code": None, "accepted_at": "now"}

        with patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client):
            report = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        assert report.steered == 1
        client.steer.assert_called_once_with(
            work_task.pk, "Use the new API instead", command_id=slack_command_id("", "C1", row.slack_ts), wait=10.0
        )
        assert Task.objects.count() == 2

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
            task.pk, "Use the new API instead", command_id=slack_command_id("", "C1", row.slack_ts), wait=10.0
        )
        client.steer.assert_not_called()
        assert "accepted_new_turn" in backend.replies[0][2]

    def test_two_live_sessions_without_a_task_id_get_a_choice_receipt(self) -> None:
        first = self._bound_task()
        second = Task.objects.create(
            ticket=first.ticket, session=first.session, phase=first.phase, status=Task.Status.PENDING
        )
        row = self._message("Use the new API")
        backend = RecordingBackend()
        backend.message_meta[row.slack_ts] = {"ts": row.slack_ts, "thread_ts": "100.0"}
        client = Mock(unreachable={})
        client.sessions.return_value = [
            {"task": first.pk, "ticket": first.ticket_id, "state": "busy", "steerable": True},
            {"task": second.pk, "ticket": first.ticket_id, "state": "busy", "steerable": True},
        ]

        with patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client):
            report = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        assert report.steered == 0
        assert report.dispatched == 0
        assert Task.objects.count() == 2
        client.steer.assert_not_called()
        assert f"#{first.pk}" in backend.replies[0][2]
        assert f"#{second.pk}" in backend.replies[0][2]

    def test_a_selected_session_receives_a_short_wait(self) -> None:
        first = self._bound_task()
        second = Task.objects.create(
            ticket=first.ticket, session=first.session, phase=first.phase, status=Task.Status.PENDING
        )
        row = self._message(f"#{first.pk} Use the new API")
        backend = RecordingBackend()
        backend.message_meta[row.slack_ts] = {"ts": row.slack_ts, "thread_ts": "100.0"}
        client = Mock(unreachable={})
        client.sessions.return_value = [
            {"task": first.pk, "ticket": first.ticket_id, "state": "busy", "steerable": True},
            {"task": second.pk, "ticket": first.ticket_id, "state": "busy", "steerable": True},
        ]
        client.steer.return_value = {"outcome": "accepted_current_turn", "code": None, "accepted_at": "now"}

        with patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client):
            report = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        assert report.steered == 1
        client.steer.assert_called_once_with(
            first.pk,
            f"#{first.pk} Use the new API",
            command_id=slack_command_id("", "C1", row.slack_ts),
            wait=10.0,
        )

    def test_selected_pending_task_queues_while_another_task_is_live(self) -> None:
        live = self._bound_task()
        pending = Task.objects.create(ticket=live.ticket, session=live.session, phase=live.phase)
        row = self._message(f"#{pending.pk} Keep the old adapter")
        backend = RecordingBackend()
        backend.message_meta[row.slack_ts] = {"ts": row.slack_ts, "thread_ts": "100.0"}
        client = Mock(unreachable={})
        client.sessions.return_value = [{"task": live.pk, "ticket": live.ticket_id, "state": "busy", "steerable": True}]

        with patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client):
            run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        command = SessionCommand.objects.get(command_id=slack_command_id("", "C1", row.slack_ts))
        assert command.task_id == pending.pk
        assert command.outcome == "queued"
        client.steer.assert_not_called()

    def test_selected_pending_task_waits_for_complete_live_discovery(self) -> None:
        live = self._bound_task()
        pending = Task.objects.create(ticket=live.ticket, session=live.session, phase=live.phase)
        row = self._message(f"#{pending.pk} Keep the old adapter")
        backend = RecordingBackend()
        backend.message_meta[row.slack_ts] = {"ts": row.slack_ts, "thread_ts": "100.0"}
        client = Mock(unreachable={"worker-a": "timeout"})
        client.sessions.return_value = [{"task": live.pk, "ticket": live.ticket_id, "state": "busy", "steerable": True}]

        with patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client):
            report = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        row.refresh_from_db()
        assert report.errors == 1
        assert row.loop_replied_at is None
        assert not SessionCommand.objects.filter(command_id=slack_command_id("", "C1", row.slack_ts)).exists()
        assert not backend.replies

    def test_unknown_task_selector_names_the_available_live_task(self) -> None:
        live = self._bound_task()
        row = self._message("#999 Keep the old adapter")
        backend = RecordingBackend()
        backend.message_meta[row.slack_ts] = {"ts": row.slack_ts, "thread_ts": "100.0"}
        client = Mock(unreachable={})
        client.sessions.return_value = [{"task": live.pk, "ticket": live.ticket_id, "state": "busy", "steerable": True}]

        with patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client):
            run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        assert f"Select a task for this ticket. Start your reply with #{live.pk}" in backend.replies[0][2]
        client.steer.assert_not_called()

    def test_pause_calls_live_control_and_starts_no_task(self) -> None:
        task = self._bound_task()
        row = self._message("pause")
        backend = RecordingBackend()
        backend.message_meta[row.slack_ts] = {"ts": row.slack_ts, "thread_ts": "100.0"}
        client = Mock(unreachable={})
        client.sessions.return_value = [{"task": task.pk, "ticket": task.ticket_id, "state": "busy", "steerable": True}]
        client.pause.return_value = {"outcome": "paused", "code": None, "accepted_at": "now"}

        with patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client):
            report = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        assert report.dispatched == 0
        assert Task.objects.count() == 1
        client.pause.assert_called_once_with(
            task.pk, "pause", command_id=slack_command_id("", "C1", row.slack_ts), wait=10.0
        )
        client.steer.assert_not_called()
        assert "paused" in backend.replies[0][2]

    def test_selected_pause_controls_only_the_named_live_task(self) -> None:
        first = self._bound_task()
        second = Task.objects.create(ticket=first.ticket, session=first.session, phase=first.phase)
        row = self._message(f"  #{second.pk} pause")
        backend = RecordingBackend()
        backend.message_meta[row.slack_ts] = {"ts": row.slack_ts, "thread_ts": "100.0"}
        client = Mock(unreachable={})
        client.sessions.return_value = [
            {"task": first.pk, "ticket": first.ticket_id, "state": "busy", "steerable": True},
            {"task": second.pk, "ticket": first.ticket_id, "state": "busy", "steerable": True},
        ]
        client.pause.return_value = {"outcome": "paused", "code": None, "accepted_at": "now"}

        with patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client):
            run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        client.pause.assert_called_once_with(
            second.pk, f"  #{second.pk} pause", command_id=slack_command_id("", "C1", row.slack_ts), wait=10.0
        )
        client.steer.assert_not_called()

    def test_selected_resume_releases_only_the_named_paused_task(self) -> None:
        first = self._bound_task()
        second = Task.objects.create(ticket=first.ticket, session=first.session, phase=first.phase)
        for task in (first, second):
            task.not_before = timezone.now() + timedelta(days=3650)
            task.save(update_fields=["not_before"])
            TaskAttempt.objects.create(task=task, error="operator_paused: paused by the operator")
        row = self._message(f"#{second.pk} resume")
        backend = RecordingBackend()
        backend.message_meta[row.slack_ts] = {"ts": row.slack_ts, "thread_ts": "100.0"}

        run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        first.refresh_from_db()
        second.refresh_from_db()
        assert first.not_before is not None
        assert second.not_before is None
        assert "same task" in backend.replies[0][2]

    def test_resume_releases_an_operator_pause_on_the_same_task(self) -> None:
        task = self._bound_task()
        task.not_before = timezone.now() + timedelta(days=3650)
        task.save(update_fields=["not_before"])
        TaskAttempt.objects.create(task=task, error="operator_paused: paused by the operator")
        row = self._message("resume")
        backend = RecordingBackend()
        backend.message_meta[row.slack_ts] = {"ts": row.slack_ts, "thread_ts": "100.0"}

        report = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        task.refresh_from_db()
        assert report.dispatched == 0
        assert Task.objects.count() == 1
        assert task.not_before is None
        assert "same task" in backend.replies[0][2]

    def test_resume_retry_records_receipt_after_pause_release(self) -> None:
        task = self._bound_task()
        TaskAttempt.objects.create(task=task, error="operator_paused: paused by the operator")
        row = self._message("resume")
        command = SessionCommand.objects.create(
            command_id=slack_command_id("", "C1", row.slack_ts), ticket=task.ticket, task=task, text="resume"
        )
        backend = RecordingBackend()
        backend.message_meta[row.slack_ts] = {"ts": row.slack_ts, "thread_ts": "100.0"}

        report = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        command.refresh_from_db()
        assert command.outcome == "queued_resume"
        assert report.dispatched == 0
        assert "same task" in backend.replies[0][2]

    def test_message_to_a_paused_task_is_queued_into_its_resumed_run(self) -> None:
        task = self._bound_task()
        task.not_before = timezone.now() + timedelta(days=3650)
        task.save(update_fields=["not_before"])
        TaskAttempt.objects.create(task=task, error="operator_paused: paused by the operator")
        row = self._message("Keep the old adapter too")
        backend = RecordingBackend()
        backend.message_meta[row.slack_ts] = {"ts": row.slack_ts, "thread_ts": "100.0"}

        report = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        command = SessionCommand.objects.get(command_id=slack_command_id("", "C1", row.slack_ts))
        assert command.task_id == task.pk
        assert command.outcome == "queued"
        assert report.dispatched == 0
        assert Task.objects.count() == 1
        assert "queued" in backend.replies[0][2]

    def test_message_before_pending_task_claim_queues_for_that_task(self) -> None:
        task = self._bound_task()
        row = self._message("Use the new API before you start")
        backend = RecordingBackend()
        backend.message_meta[row.slack_ts] = {"ts": row.slack_ts, "thread_ts": "100.0"}
        client = Mock(unreachable={})
        client.sessions.return_value = []

        with patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client):
            report = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        command = SessionCommand.objects.get(command_id=slack_command_id("", "C1", row.slack_ts))
        assert command.task_id == task.pk
        assert command.outcome == "queued"
        assert report.dispatched == 0
        assert Task.objects.count() == 1

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

    def test_a_refused_session_list_is_a_unit_error_and_starts_no_task(self) -> None:
        self._bound_task()
        row = self._message("Use the new API instead")
        client = Mock(unreachable={})
        client.sessions.side_effect = LiveRefusedError("Operator verbs are refused to agent peers", "permission_denied")

        with patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client):
            report = run_slack_answer_cycle(messaging_resolver=_resolver(RecordingBackend()))

        assert report.errors == 1
        assert report.dispatched == 0
        assert Task.objects.count() == 1
        assert not SessionCommand.objects.exists()
        row.refresh_from_db()
        assert row.loop_replied_at is None

    def test_a_session_that_never_accepts_holds_the_cycle_for_at_most_the_slack_wait(self) -> None:
        stuck = self._bound_task()
        healthy = dispatch_work(
            reading=InboundReading(
                intent=InboundIntent.INSTRUCTION, answerable=False, work_summary="other", source=ReadingSource.MODEL
            ),
            fingerprint="other-feature",
            origin=WorkOrigin("", "C1", "200.0", ("200.0",), "other"),
        )
        first = self._message("Use the new API")
        second = PendingChatInjection.record(
            channel="C1",
            slack_ts="201.0",
            text="Use the old API",
            context=DmContext(user_id=_USER_UID, thread_ts="200.0"),
        )
        assert second is not None
        backend = RecordingBackend()
        for row, root in ((first, "100.0"), (second, "200.0")):
            backend.message_meta[row.slack_ts] = {"ts": row.slack_ts, "thread_ts": root}
        client = Mock(unreachable={})
        client.sessions.return_value = [
            {"task": stuck.pk, "ticket": stuck.ticket_id, "state": "busy", "steerable": True},
            {"task": healthy.pk, "ticket": healthy.ticket_id, "state": "busy", "steerable": True},
        ]
        client.steer.side_effect = [
            {"outcome": "rejected", "code": "not_accepted_in_time", "accepted_at": None},
            {"outcome": "accepted_current_turn", "code": None, "accepted_at": "now"},
        ]

        with patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client):
            report = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        assert [call.kwargs["wait"] for call in client.steer.call_args_list] == [SLACK_STEER_WAIT_SECONDS] * 2
        assert SLACK_STEER_WAIT_SECONDS <= 10.0
        assert report.steered == 2
        assert "not_accepted_in_time" in backend.replies[0][2]
        assert "accepted_current_turn" in backend.replies[1][2]

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
            assert not SessionCommand.objects.exists()
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

    def test_incomplete_command_retries_delivery_with_the_same_id(self) -> None:
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
        client.steer.return_value = {"outcome": "accepted_current_turn", "code": None, "accepted_at": "now"}

        with patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client):
            run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        client.steer.assert_called_once_with(
            task.pk, "Use the new API instead", command_id=slack_command_id("", "C1", "101.0"), wait=10.0
        )
        assert "accepted_current_turn" in backend.replies[0][2]
        command = SessionCommand.objects.get(command_id=slack_command_id("", "C1", "101.0"))
        assert command.outcome == "accepted_current_turn"

    def test_crash_after_creating_command_recovers_the_unsent_direction(self) -> None:
        task = self._bound_task()
        row = self._message("Use the new API instead")
        backend = RecordingBackend()
        backend.message_meta[row.slack_ts] = {"ts": row.slack_ts, "thread_ts": "100.0"}
        client = Mock(unreachable={})
        client.sessions.return_value = [{"task": task.pk, "ticket": task.ticket_id, "state": "busy", "steerable": True}]
        client.steer.side_effect = SystemExit("worker exited before live call")

        with patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client):
            with pytest.raises(SystemExit):
                run_slack_answer_cycle(messaging_resolver=_resolver(backend))
            command = SessionCommand.objects.get(command_id=slack_command_id("", "C1", "101.0"))
            assert command.outcome == ""
            PendingChatInjection.objects.filter(pk=row.pk).update(loop_replied_at=timezone.now() - timedelta(minutes=2))
            client.steer.side_effect = None
            client.steer.return_value = {"outcome": "accepted_current_turn", "code": None, "accepted_at": "now"}
            report = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        assert report.steered == 1
        assert client.steer.call_count == 2
        row.refresh_from_db()
        assert row.loop_response_confirmed_at is not None

    def test_accepted_command_survives_sender_crash_and_session_end_without_reassignment(self) -> None:
        prior = self._bound_task()
        row = self._message("Change the implementation")
        backend = RecordingBackend()
        backend.message_meta[row.slack_ts] = {"ts": row.slack_ts, "thread_ts": "100.0"}
        client = Mock(unreachable={})
        client.sessions.return_value = [
            {"task": prior.pk, "ticket": prior.ticket_id, "state": "busy", "steerable": True}
        ]
        accepted: list[str] = []

        def accept_then_crash(task_pk: int, text: str, *, command_id: str, wait: float) -> None:
            accepted.append(command_id)
            raise SystemExit

        client.steer.side_effect = accept_then_crash

        with patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client):
            with pytest.raises(SystemExit):
                run_slack_answer_cycle(messaging_resolver=_resolver(backend))
            prior.status = Task.Status.COMPLETED
            prior.save(update_fields=["status"])
            PendingChatInjection.objects.filter(pk=row.pk).update(loop_replied_at=timezone.now() - timedelta(minutes=2))
            client.sessions.return_value = []
            first = run_slack_answer_cycle(messaging_resolver=_resolver(backend))
            second = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        command = SessionCommand.objects.get(command_id=slack_command_id("", "C1", row.slack_ts))
        assert accepted == [command.command_id]
        assert Task.objects.count() == 1
        assert command.task_id == prior.pk
        assert command.outcome == "unknown_delivery"
        assert first.dispatched == second.dispatched == 0
        assert "unknown_delivery" in backend.replies[0][2]

    def test_unsettled_command_for_ended_session_is_not_reassigned(self) -> None:
        prior = self._bound_task()
        prior.status = Task.Status.COMPLETED
        prior.save(update_fields=["status"])
        row = self._message("Change the implementation")
        SessionCommand.objects.create(
            command_id=slack_command_id("", "C1", row.slack_ts),
            ticket=prior.ticket,
            task=prior,
            text=row.text,
        )
        backend = RecordingBackend()
        backend.message_meta[row.slack_ts] = {"ts": row.slack_ts, "thread_ts": "100.0"}
        client = Mock(unreachable={})
        client.sessions.return_value = []

        with patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client):
            first = run_slack_answer_cycle(messaging_resolver=_resolver(backend))
            second = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        command = SessionCommand.objects.get(command_id=slack_command_id("", "C1", row.slack_ts))
        assert Task.objects.count() == 1
        assert first.dispatched == 0
        assert second.dispatched == 0
        assert command.task_id == prior.pk
        assert command.outcome == "unknown_delivery"
        assert "unknown_delivery" in backend.replies[0][2]

    def test_unsettled_command_mismatch_without_live_session_is_rejected(self) -> None:
        prior = self._bound_task()
        row = self._message("Change the implementation")
        command = SessionCommand.objects.create(
            command_id=slack_command_id("", "C1", row.slack_ts),
            ticket=prior.ticket,
            task=prior,
            text="Another direction",
        )
        backend = RecordingBackend()
        backend.message_meta[row.slack_ts] = {"ts": row.slack_ts, "thread_ts": "100.0"}
        client = Mock(unreachable={})
        client.sessions.return_value = []

        with patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client):
            report = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        command.refresh_from_db()
        assert report.dispatched == 0
        assert Task.objects.count() == 1
        assert command.outcome == ""
        assert "rejected (duplicate_mismatch)" in backend.replies[0][2]

    def test_coalesced_reply_confirmation_is_atomic_across_a_crash(self) -> None:
        task = self._bound_task()
        first = self._message("Use the new API")
        second = PendingChatInjection.record(
            channel="C1",
            slack_ts="101.0000005",
            text="Keep the old adapter too",
            context=DmContext(user_id=_USER_UID, thread_ts="100.0"),
        )
        assert second is not None
        backend = RecordingBackend()
        backend.message_meta[first.slack_ts] = {"ts": first.slack_ts, "thread_ts": "100.0"}
        client = Mock(unreachable={})
        client.sessions.return_value = [{"task": task.pk, "ticket": task.ticket_id, "state": "busy", "steerable": True}]
        client.steer.return_value = {"outcome": "accepted_current_turn", "code": None, "accepted_at": "now"}
        observe = PendingChatInjection.observe_confirmed_loop_reply
        calls = 0

        def crash_on_second(row: PendingChatInjection) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise SystemExit
            observe(row)

        with patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client):
            with (
                patch.object(PendingChatInjection, "observe_confirmed_loop_reply", crash_on_second),
                pytest.raises(SystemExit),
            ):
                run_slack_answer_cycle(messaging_resolver=_resolver(backend))
            first.refresh_from_db()
            second.refresh_from_db()
            assert first.loop_response_confirmed_at is None
            assert second.loop_response_confirmed_at is None
            PendingChatInjection.objects.filter(pk__in=[first.pk, second.pk]).update(
                loop_replied_at=timezone.now() - timedelta(minutes=2)
            )
            report = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        assert report.steered == 1
        client.steer.assert_called_once()
        first.refresh_from_db()
        second.refresh_from_db()
        assert first.loop_response_confirmed_at is not None
        assert second.loop_response_confirmed_at is not None

    def test_incomplete_command_does_not_move_to_a_different_live_task(self) -> None:
        original = self._bound_task()
        successor = Task.objects.create(ticket=original.ticket, session=original.session, phase="coding")
        row = self._message("Use the new API instead")
        SessionCommand.objects.create(
            command_id=slack_command_id("", "C1", "101.0"),
            ticket=original.ticket,
            task=original,
            text=row.text,
        )
        backend = RecordingBackend()
        backend.message_meta[row.slack_ts] = {"ts": row.slack_ts, "thread_ts": "100.0"}
        client = Mock(unreachable={})
        client.sessions.return_value = [
            {"task": successor.pk, "ticket": successor.ticket_id, "state": "busy", "steerable": True}
        ]

        with patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client):
            report = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        assert report.errors == 1
        client.steer.assert_not_called()
        row.refresh_from_db()
        assert row.loop_replied_at is None

    def test_bound_question_answer_enters_live_session_without_child_task(self) -> None:
        task = self._bound_task()
        question = DeferredQuestion.record(
            "Which API?", slack_ts="100.0", slack_channel="C1", parked_task=task, task_session=task.session
        )
        row = self._message("Use the new API")
        backend = RecordingBackend()
        backend.message_meta[row.slack_ts] = {"ts": row.slack_ts, "thread_ts": "100.0"}
        client = Mock(unreachable={})
        client.sessions.return_value = [{"task": task.pk, "ticket": task.ticket_id, "state": "busy", "steerable": True}]
        client.steer.return_value = {"outcome": "accepted_current_turn", "code": None, "accepted_at": "now"}

        with (
            patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client),
            patch.object(routing_mod, "apply_stop_answer") as stop_answer,
        ):
            run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        question.refresh_from_db()
        client.steer.assert_called_once()
        assert question.answer_text == "Use the new API"
        assert Task.objects.count() == 1
        assert stop_answer.call_args.args[0].pk == question.pk

    def test_question_card_answer_resumes_parked_task_while_sibling_is_live(self) -> None:
        parked = self._bound_task()
        sibling = Task.objects.create(ticket=parked.ticket, session=parked.session, phase="coding")
        question = DeferredQuestion.record(
            "Which API?", slack_ts="100.0", slack_channel="C1", parked_task=parked, task_session=parked.session
        )
        row = self._message("Use the new API")
        backend = RecordingBackend()
        backend.message_meta[row.slack_ts] = {"ts": row.slack_ts, "thread_ts": "100.0"}
        client = Mock(unreachable={})
        client.sessions.return_value = [
            {"task": sibling.pk, "ticket": parked.ticket_id, "state": "busy", "steerable": True}
        ]

        with patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client):
            report = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        question.refresh_from_db()
        assert report.answered_question == 1
        assert question.answer_text == "Use the new API"
        client.steer.assert_not_called()
        assert backend.replies == []

    def test_nested_question_card_in_ticket_thread_answers_parked_task(self) -> None:
        parked = self._bound_task()
        question = DeferredQuestion.record(
            "Which API?", slack_ts="100.2", slack_channel="C1", parked_task=parked, task_session=parked.session
        )
        row = self._message("Use the new API")
        backend = RecordingBackend()
        backend.message_meta[row.slack_ts] = {"ts": row.slack_ts, "thread_ts": "100.0"}
        backend.thread_replies["100.0"] = [{"ts": "100.0"}, {"ts": "100.2", "thread_ts": "100.0"}]
        client = Mock(unreachable={})
        client.sessions.return_value = [
            {"task": parked.pk, "ticket": parked.ticket_id, "state": "busy", "steerable": True}
        ]
        client.steer.return_value = {"outcome": "accepted_current_turn", "code": None, "accepted_at": "now"}

        with (
            patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client),
            patch.object(routing_mod, "apply_stop_answer") as stop_answer,
        ):
            report = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        question.refresh_from_db()
        assert report.steered == 1
        assert question.answer_text == "Use the new API"
        client.steer.assert_called_once()
        assert stop_answer.call_args.args[0].pk == question.pk

    def test_two_pending_cards_in_a_bound_ticket_thread_get_the_choice_before_any_steer(self) -> None:
        parked = self._bound_task()
        cards = [
            DeferredQuestion.record(
                question,
                slack_ts=ts,
                slack_channel="C1",
                parked_task=parked,
                task_session=parked.session,
                card=owner_card(),
            )
            for question, ts in (("Which API?", "100.2"), ("Which database?", "100.3"))
        ]
        row = self._message("Yes")
        backend = RecordingBackend()
        backend.message_meta[row.slack_ts] = {"ts": row.slack_ts, "thread_ts": "100.0"}
        backend.thread_replies["100.0"] = [{"ts": "100.0"}, {"ts": "100.2"}, {"ts": "100.3"}]
        client = Mock(unreachable={})
        client.sessions.return_value = [
            {"task": parked.pk, "ticket": parked.ticket_id, "state": "busy", "steerable": True}
        ]

        with patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client):
            run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        client.steer.assert_not_called()
        assert all(card.is_pending for card in DeferredQuestion.objects.filter(pk__in=[c.pk for c in cards]))
        assert any(all(f"#{card.pk}" in text for card in cards) for _, _, text in backend.replies)

    def test_question_card_answer_resolves_when_sibling_is_pending(self) -> None:
        parked = self._bound_task()
        Task.objects.create(ticket=parked.ticket, session=parked.session, phase="coding")
        question = DeferredQuestion.record(
            "Which API?",
            slack_ts="100.0",
            slack_channel="C1",
            parked_task=parked,
            task_session=parked.session,
            card=_FREE_TEXT_CARD,
        )
        row = self._message("Use the new API")
        backend = RecordingBackend()
        backend.message_meta[row.slack_ts] = {"ts": row.slack_ts, "thread_ts": "100.0"}
        client = Mock(unreachable={})
        client.sessions.return_value = []

        with (
            patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client),
            patch(
                "teatree.core.models.task_handoff.schedule_resume", side_effect=NoPlanArtifactError("no plan")
            ) as resume,
        ):
            report = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        question.refresh_from_db()
        assert report.answered_question == 1
        assert question.answer_text == "Use the new API"
        resume.assert_called_once()
        assert Task.objects.count() == 2
        assert "resume path requested" in backend.replies[0][2].lower()

    def test_question_resume_notice_retries_after_slack_readback_failure(self) -> None:
        parked = self._bound_task()
        question = DeferredQuestion.record(
            "Which API?",
            slack_ts="100.0",
            slack_channel="C1",
            parked_task=parked,
            task_session=parked.session,
            card=_FREE_TEXT_CARD,
        )
        row = self._message("Use the new API")
        backend = RecordingBackend(read_after_post=False)
        backend.message_meta[row.slack_ts] = {"ts": row.slack_ts, "thread_ts": "100.0"}
        client = Mock(unreachable={})
        client.sessions.return_value = []

        with patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client):
            first = run_slack_answer_cycle(messaging_resolver=_resolver(backend))
            row.refresh_from_db()
            assert row.answer_kind == PendingChatInjection.AnswerKind.QUESTION_REPLY
            PendingChatInjection.objects.filter(pk=row.pk).update(loop_replied_at=timezone.now() - timedelta(minutes=2))
            backend.read_after_post = True
            backend.thread_replies.clear()
            second = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        question.refresh_from_db()
        row.refresh_from_db()
        assert first.errors == 1
        assert second.answered_question == 1
        assert question.answer_text == "Use the new API"
        assert Task.objects.count() == 2
        assert Task.objects.exclude(pk=parked.pk).get().parent_task_id == parked.pk
        assert len(backend.replies) == 2
        assert "resum" in backend.replies[-1][2].lower()
        assert row.loop_response_confirmed_at is not None

    def test_question_resume_notice_retries_after_slack_post_exception(self) -> None:
        parked = self._bound_task()
        question = DeferredQuestion.record(
            "Which API?",
            slack_ts="100.0",
            slack_channel="C1",
            parked_task=parked,
            task_session=parked.session,
            card=_FREE_TEXT_CARD,
        )
        row = self._message("Use the new API")
        backend = RecordingBackend(post_reply_raises=True)
        backend.message_meta[row.slack_ts] = {"ts": row.slack_ts, "thread_ts": "100.0"}
        client = Mock(unreachable={})
        client.sessions.return_value = []

        with patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client):
            first = run_slack_answer_cycle(messaging_resolver=_resolver(backend))
            row.refresh_from_db()
            assert row.answer_kind == PendingChatInjection.AnswerKind.QUESTION_REPLY
            PendingChatInjection.objects.filter(pk=row.pk).update(loop_replied_at=timezone.now() - timedelta(minutes=2))
            backend.post_reply_raises = False
            second = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        question.refresh_from_db()
        row.refresh_from_db()
        assert first.errors == 1
        assert second.answered_question == 1
        assert question.answer_text == "Use the new API"
        assert Task.objects.count() == 2
        assert Task.objects.exclude(pk=parked.pk).get().parent_task_id == parked.pk
        assert len(backend.replies) == 1
        assert "resum" in backend.replies[0][2].lower()
        assert row.loop_response_confirmed_at is not None

    def test_question_resume_notice_replays_after_sender_crash(self) -> None:
        parked = self._bound_task()
        question = DeferredQuestion.record(
            "Which API?", slack_ts="100.0", slack_channel="C1", parked_task=parked, card=_FREE_TEXT_CARD
        )
        row = self._message("Use the new API")
        backend = RecordingBackend()
        backend.message_meta[row.slack_ts] = {"ts": row.slack_ts, "thread_ts": "100.0"}
        client = Mock(unreachable={})
        client.sessions.return_value = []

        with patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client):
            with (
                patch("teatree.loop.slack_answer.cycle._post_live_reply", side_effect=SystemExit),
                pytest.raises(SystemExit),
            ):
                run_slack_answer_cycle(messaging_resolver=_resolver(backend))
            PendingChatInjection.objects.filter(pk=row.pk).update(loop_replied_at=timezone.now() - timedelta(minutes=2))
            report = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        question.refresh_from_db()
        row.refresh_from_db()
        assert report.answered_question == 1
        assert question.answer_text == "Use the new API"
        assert Task.objects.count() == 2
        assert "resume path requested" in backend.replies[0][2].lower()
        assert row.loop_response_confirmed_at is not None

    def test_unbound_question_receipt_failure_does_not_dispatch_answer_as_work(self) -> None:
        parked = self._bound_task()
        ThreadBinding.objects.filter(ticket=parked.ticket).delete()
        question = DeferredQuestion.record(
            "Which API?", slack_ts="100.0", slack_channel="C1", parked_task=parked, card=_FREE_TEXT_CARD
        )
        row = self._message("Use the new API")
        backend = RecordingBackend(post_reply_raises=True)
        backend.message_meta[row.slack_ts] = {"ts": row.slack_ts, "thread_ts": "100.0"}

        first = run_slack_answer_cycle(messaging_resolver=_resolver(backend))
        backend.post_reply_raises = False
        second = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        question.refresh_from_db()
        row.refresh_from_db()
        assert first.errors == 1
        assert second.dispatched == 0
        assert Task.objects.count() == 2
        assert question.answer_text == "Use the new API"
        assert row.answer_kind == PendingChatInjection.AnswerKind.QUESTION_REPLY

    def test_replayed_answered_resume_crash_keeps_question_kind(self) -> None:
        parked = self._bound_task()
        SessionCommand.objects.create(
            command_id=slack_command_id("", "C1", "101.0"),
            ticket=parked.ticket,
            task=parked,
            text="Use the new API",
            outcome="answered_resume",
        )
        row = self._message("Use the new API")
        backend = RecordingBackend()
        backend.message_meta[row.slack_ts] = {"ts": row.slack_ts, "thread_ts": "100.0"}

        with (
            patch("teatree.loop.slack_answer.session_routing._post_live_reply", side_effect=SystemExit),
            pytest.raises(SystemExit),
        ):
            run_slack_answer_cycle(messaging_resolver=_resolver(backend))
        ThreadBinding.objects.filter(ticket=parked.ticket).delete()
        PendingChatInjection.objects.filter(pk=row.pk).update(loop_replied_at=timezone.now() - timedelta(minutes=2))
        report = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        row.refresh_from_db()
        assert report.dispatched == 0
        assert Task.objects.count() == 1
        assert "unknown_delivery" in backend.replies[0][2]
        assert row.answer_kind == PendingChatInjection.AnswerKind.QUESTION_REPLY

    def test_replayed_answered_resume_failed_post_keeps_question_kind(self) -> None:
        parked = self._bound_task()
        SessionCommand.objects.create(
            command_id=slack_command_id("", "C1", "101.0"),
            ticket=parked.ticket,
            task=parked,
            text="Use the new API",
            outcome="answered_resume",
        )
        row = self._message("Use the new API")
        backend = RecordingBackend()
        backend.message_meta[row.slack_ts] = {"ts": row.slack_ts, "thread_ts": "100.0"}

        with patch("teatree.loop.slack_answer.session_routing._post_live_reply", return_value=False):
            run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        row.refresh_from_db()
        assert row.loop_replied_at is not None
        assert row.answer_kind == PendingChatInjection.AnswerKind.QUESTION_REPLY

    def test_replayed_answered_resume_post_exception_keeps_question_kind(self) -> None:
        parked = self._bound_task()
        SessionCommand.objects.create(
            command_id=slack_command_id("", "C1", "101.0"),
            ticket=parked.ticket,
            task=parked,
            text="Use the new API",
            outcome="answered_resume",
        )
        row = self._message("Use the new API")
        backend = RecordingBackend()
        backend.message_meta[row.slack_ts] = {"ts": row.slack_ts, "thread_ts": "100.0"}

        with patch("teatree.loop.slack_answer.session_routing._post_live_reply", side_effect=RuntimeError("slack 503")):
            run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        row.refresh_from_db()
        assert row.loop_replied_at is not None
        assert row.answer_kind == PendingChatInjection.AnswerKind.QUESTION_REPLY

    def test_question_answer_crash_before_receipt_reports_uncertainty(self) -> None:
        parked = self._bound_task()
        question = DeferredQuestion.record(
            "Which API?", slack_ts="100.0", slack_channel="C1", parked_task=parked, card=_FREE_TEXT_CARD
        )
        row = self._message("Use the new API")
        backend = RecordingBackend()
        backend.message_meta[row.slack_ts] = {"ts": row.slack_ts, "thread_ts": "100.0"}
        client = Mock(unreachable={})
        client.sessions.return_value = []

        with patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client):
            with (
                patch.object(SessionCommand.objects, "get_or_create", side_effect=SystemExit),
                pytest.raises(SystemExit),
            ):
                run_slack_answer_cycle(messaging_resolver=_resolver(backend))
            PendingChatInjection.objects.filter(pk=row.pk).update(loop_replied_at=timezone.now() - timedelta(minutes=2))
            report = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        question.refresh_from_db()
        row.refresh_from_db()
        assert report.dispatched == 0
        assert question.answer_text == "Use the new API"
        assert Task.objects.count() == 2
        assert "unknown_delivery" in backend.replies[0][2]
        assert row.loop_response_confirmed_at is not None

    def test_stale_question_without_thread_binding_does_not_start_work(self) -> None:
        row = self._message("Use the new API")
        assert row.mark_loop_replied(PendingChatInjection.AnswerKind.QUESTION_REPLY)
        PendingChatInjection.objects.filter(pk=row.pk).update(loop_replied_at=timezone.now() - timedelta(minutes=2))
        backend = RecordingBackend()
        backend.message_meta[row.slack_ts] = {"ts": row.slack_ts, "thread_ts": "100.0"}

        report = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        row.refresh_from_db()
        assert report.dispatched == 0
        assert "unknown_delivery" in backend.replies[0][2]
        assert row.loop_response_confirmed_at is not None

    def test_failed_uncertainty_receipt_without_binding_keeps_question_kind(self) -> None:
        row = self._message("Use the new API")
        assert row.mark_loop_replied(PendingChatInjection.AnswerKind.QUESTION_REPLY)
        PendingChatInjection.objects.filter(pk=row.pk).update(loop_replied_at=timezone.now() - timedelta(minutes=2))
        backend = RecordingBackend(post_reply_raises=True)
        backend.message_meta[row.slack_ts] = {"ts": row.slack_ts, "thread_ts": "100.0"}

        first = run_slack_answer_cycle(messaging_resolver=_resolver(backend))
        backend.post_reply_raises = False
        second = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        row.refresh_from_db()
        assert first.errors == 1
        assert second.dispatched == 0
        assert row.answer_kind == PendingChatInjection.AnswerKind.QUESTION_REPLY

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

    def test_stale_claim_without_command_falls_back_when_session_ended(self) -> None:
        prior = self._bound_task()
        prior.status = Task.Status.COMPLETED
        prior.save(update_fields=["status"])
        row = self._message("Change the implementation")
        assert row.mark_loop_replied(PendingChatInjection.AnswerKind.SESSION)
        PendingChatInjection.objects.filter(pk=row.pk).update(loop_replied_at=timezone.now() - timedelta(minutes=2))
        backend = RecordingBackend()
        backend.message_meta[row.slack_ts] = {"ts": row.slack_ts, "thread_ts": "100.0"}
        client = Mock(unreachable={})
        client.sessions.return_value = []

        with patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client):
            report = run_slack_answer_cycle(messaging_resolver=_resolver(backend))

        assert report.dispatched == 1
        assert Task.objects.exclude(pk=prior.pk).get().ticket_id == prior.ticket_id
        row.refresh_from_db()
        assert row.answer_kind == PendingChatInjection.AnswerKind.DELEGATED

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

    def test_an_unset_operator_id_forwards_nothing(self) -> None:
        task = self._bound_task()
        extra_before = dict(task.ticket.extra)
        row = self._message("Delete the branch")
        client = Mock(unreachable={})
        client.sessions.return_value = [{"task": task.pk, "ticket": task.ticket_id, "state": "busy", "steerable": True}]

        with patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client):
            report = run_slack_answer_cycle(
                messaging_resolver=_resolver(RecordingBackend(user_id="")),
                reader=Mock(side_effect=AssertionError("unset owner reached reader")),
            )

        client.steer.assert_not_called()
        assert report.acked == 1
        assert Task.objects.count() == 1
        assert not SessionCommand.objects.exists()
        task.ticket.refresh_from_db()
        assert task.ticket.extra == extra_before
        row.refresh_from_db()
        assert row.answer_kind == PendingChatInjection.AnswerKind.ACK

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
                reader=lambda _text: InboundReading(
                    intent=InboundIntent.INSTRUCTION,
                    answerable=False,
                    work_summary="delete the branch",
                    source=ReadingSource.MODEL,
                ),
            )

        client.steer.assert_not_called()
        assert Task.objects.count() == 1

    def test_unattributed_row_with_unset_owner_is_never_forwarded(self) -> None:
        task = self._bound_task()
        row = PendingChatInjection.record(
            channel="C1", slack_ts="101.0", text="Delete the branch", context=DmContext(user_id="", thread_ts="100.0")
        )
        assert row is not None
        client = Mock(unreachable={})
        client.sessions.return_value = [{"task": task.pk, "ticket": task.ticket_id, "state": "busy", "steerable": True}]

        with patch("teatree.loop.slack_answer.session_routing.LiveClient", return_value=client):
            report = run_slack_answer_cycle(
                messaging_resolver=_resolver(RecordingBackend(user_id="")),
                reader=lambda _text: InboundReading(
                    intent=InboundIntent.INSTRUCTION,
                    answerable=False,
                    work_summary="delete the branch",
                    source=ReadingSource.MODEL,
                ),
            )

        assert report.acked == 1
        client.steer.assert_not_called()
        assert not SessionCommand.objects.exists()
