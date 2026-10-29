"""Durable lifecycle gaps become actionable, deduplicated factory incidents."""

import json
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from teatree.core.modelkit.reask_cadence import owner_quiet_at
from teatree.core.modelkit.task_failure_taxonomy import STALE_ERROR_AFTER
from teatree.core.models import (
    DeferredQuestion,
    DmContext,
    PendingChatInjection,
    SelfImproveFiring,
    Task,
    TaskAttempt,
    Ticket,
)
from teatree.core.models.task_phase_disposition import record_stuck_transition_question
from teatree.core.question_heal import withhold
from teatree.dash.selectors import STALE_ERROR_AFTER as BOARD_STALE_ERROR_AFTER
from teatree.loop.preset_resolution import owner_zone
from teatree.loop.scanners import owner_dm_rows
from teatree.loop.self_improve.budget import BudgetVerdict
from teatree.loop.self_improve.detectors.base import ActionRung, DetectorReport
from teatree.loop.self_improve.detectors.lifecycle_incident import STALE_ERROR_AFTER as DETECTOR_STALE_ERROR_AFTER
from teatree.loop.self_improve.detectors.lifecycle_incident import LifecycleIncidentDetector
from teatree.loop.self_improve.persistence import record_firing
from teatree.loop.self_improve.schedule import DeliveryRoutes, Tier, TierResult, detectors_for_tier, run_tier
from tests._owner_channel import OWNER_DECISION, OWNER_SLACK_ID
from tests.factories import TaskAttemptFactory, TaskFactory, TicketFactory


def _owner_dm(**fields: str) -> PendingChatInjection | None:
    return PendingChatInjection.record(context=DmContext(user_id=OWNER_SLACK_ID), **fields)


class LifecycleIncidentTests(TestCase):
    def setUp(self) -> None:
        owner_backend = SimpleNamespace(user_id=OWNER_SLACK_ID)
        self.enterContext(patch.object(owner_dm_rows, "messaging_from_overlay", return_value=owner_backend))
        quiet = patch("teatree.core.modelkit.reask_cadence.owner_quiet_at", return_value=False)
        quiet.start()
        self.addCleanup(quiet.stop)

    def _reports(self) -> list:
        detector = next(
            detector for detector in detectors_for_tier(Tier.CHEAP) if detector.name == "lifecycle_incident"
        )
        return detector.detect()

    def test_failed_task_is_grouped_by_named_cause(self) -> None:
        for _ in range(3):
            task = TaskFactory()
            task.fail(reason="ProcessError: worker exited", by_holder=True)

        reports = self._reports()

        assert len(reports) == 1
        assert reports[0].payload["kind"] == "task_failed"
        assert reports[0].payload["count"] == 3
        assert reports[0].requested_rung == ActionRung.TICKET
        assert "worker exited" not in str(reports[0].payload)

    def test_recovered_and_cancelled_tasks_are_not_incidents(self) -> None:
        recovered = TaskFactory()
        recovered.fail(reason="ProcessError: worker exited", by_holder=True)
        recovered.reopen()
        cancelled = TaskFactory()
        cancelled.fail(reason="cancelled: operator requested", by_holder=False)

        assert not [report for report in self._reports() if report.payload["kind"] == "task_failed"]

    def test_long_running_task_that_fails_now_is_not_lost_to_creation_window(self) -> None:
        task = TaskFactory()
        Task.objects.filter(pk=task.pk).update(created_at=timezone.now() - timedelta(days=7))
        task.fail(reason="ProcessError: worker exited", by_holder=True)

        assert any(report.payload["kind"] == "task_failed" for report in self._reports())

    def test_failed_task_reports_only_recent_attempts_or_tasks_without_attempts(self) -> None:
        old = TaskFactory()
        recent = TaskFactory()
        without_attempt = TaskFactory()
        retried = TaskFactory()
        for task in (old, recent, without_attempt, retried):
            task.fail(reason="ProcessError: worker exited", by_holder=True)
        old_attempt = TaskAttemptFactory(task=old)
        recent_attempt = TaskAttemptFactory(task=recent)
        retried_old_attempt = TaskAttemptFactory(task=retried)
        retried_new_attempt = TaskAttemptFactory(task=retried)
        TaskAttempt.objects.filter(pk=old_attempt.pk).update(started_at=timezone.now() - timedelta(days=3))
        TaskAttempt.objects.filter(pk=recent_attempt.pk).update(started_at=timezone.now() - timedelta(days=1))
        TaskAttempt.objects.filter(pk=retried_old_attempt.pk).update(started_at=timezone.now() - timedelta(days=3))
        TaskAttempt.objects.filter(pk=retried_new_attempt.pk).update(started_at=timezone.now() - timedelta(days=1))

        reports = [report for report in self._reports() if report.payload["kind"] == "task_failed"]

        assert len(reports) == 1
        assert reports[0].payload["ids"] == sorted([recent.pk, without_attempt.pk, retried.pk])

    def test_board_and_detector_share_failure_age_window(self) -> None:
        assert BOARD_STALE_ERROR_AFTER is STALE_ERROR_AFTER
        assert DETECTOR_STALE_ERROR_AFTER is STALE_ERROR_AFTER

    def test_failed_repair_task_reports_use_the_same_attempt_window(self) -> None:
        old = TaskFactory(ticket=TicketFactory(extra={"source": "self_improve"}))
        recent = TaskFactory(ticket=TicketFactory(extra={"source": "self_improve"}))
        without_attempt = TaskFactory(ticket=TicketFactory(extra={"source": "self_improve"}))
        for task in (old, recent, without_attempt):
            task.fail(reason="ProcessError: worker exited", by_holder=True)
        old_attempt = TaskAttemptFactory(task=old)
        recent_attempt = TaskAttemptFactory(task=recent)
        TaskAttempt.objects.filter(pk=old_attempt.pk).update(started_at=timezone.now() - timedelta(days=3))
        TaskAttempt.objects.filter(pk=recent_attempt.pk).update(started_at=timezone.now() - timedelta(days=1))

        reports = [report for report in self._reports() if report.payload["kind"] == "repair_task_failed"]

        assert len(reports) == 1
        assert reports[0].payload["ids"] == sorted([recent.pk, without_attempt.pk])

    def test_failed_task_firing_resolves_when_its_attempt_ages_out(self) -> None:
        task = TaskFactory()
        task.fail(reason="ProcessError: worker exited", by_holder=True)
        attempt = TaskAttemptFactory(task=task)
        detector = LifecycleIncidentDetector()
        first = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow())
        firing = SelfImproveFiring.objects.get(dedup_key=first.reports[0].dedup_key)
        assert firing.resolved_at is None

        TaskAttempt.objects.filter(pk=attempt.pk).update(started_at=timezone.now() - timedelta(days=3))
        second = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow())

        assert not [report for report in second.reports if report.payload["kind"] == "task_failed"]
        assert SelfImproveFiring.objects.get(pk=firing.pk).resolved_at is not None

    def test_failed_task_on_settled_ticket_does_not_resurface(self) -> None:
        task = TaskFactory()
        task.fail(reason="ProcessError: worker exited", by_holder=True)
        Ticket.objects.filter(pk=task.ticket_id).update(state=Ticket.State.MERGED)

        assert not [report for report in self._reports() if report.payload["kind"] == "task_failed"]

    def test_newer_same_phase_task_supersedes_historical_failure(self) -> None:
        old = TaskFactory()
        old.fail(reason="result_error: old attempt", by_holder=True)
        TaskFactory(ticket=old.ticket, phase=old.phase)
        other_phase = TaskFactory(ticket=old.ticket, phase="reviewing")
        other_phase.fail(reason="result_error: current attempt", by_holder=True)

        reports = [report for report in self._reports() if report.payload["kind"] == "task_failed"]
        assert len(reports) == 1
        assert reports[0].payload["ids"] == [other_phase.pk]

    def test_administrative_newer_failure_does_not_hide_old_failure(self) -> None:
        for reason in ("cancelled: operator stopped", "superseded: rework", "plan_stale: base moved"):
            old = TaskFactory()
            old.fail(reason="result_error: old attempt", by_holder=True)
            newer = TaskFactory(ticket=old.ticket, phase=old.phase)
            newer.fail(reason=reason, by_holder=True)

        reports = [report for report in self._reports() if report.payload["kind"] == "task_failed"]
        assert len(reports) == 1
        assert reports[0].payload["count"] == 3

    def test_a_colleagues_old_unanswered_message_is_not_an_incident(self) -> None:
        row = PendingChatInjection.record(
            channel="D1", slack_ts="100.7", text="Why did my task fail?", context=DmContext(user_id="U_COLLEAGUE")
        )
        assert row is not None
        PendingChatInjection.objects.filter(pk=row.pk).update(received_at=timezone.now() - timedelta(hours=2))

        assert not [report for report in self._reports() if report.payload["kind"] == "inbound_unanswered"]

    def test_later_completed_canonical_phase_recovers_failure(self) -> None:
        for failed_phase, completed_phase in (("testing", "test"), ("test", "testing")):
            failed = TaskFactory(phase=failed_phase)
            failed.fail(reason="ProcessError: worker exited", by_holder=True)
            TaskFactory(ticket=failed.ticket, phase=completed_phase, status=Task.Status.COMPLETED)

        assert not [report for report in self._reports() if report.payload["kind"] == "task_failed"]

    def test_later_completed_same_phase_recovers_failure_with_pending_question(self) -> None:
        failed = TaskFactory(phase="coding")
        failed.fail(reason="ProcessError: worker exited", by_holder=True)
        successor = TaskFactory(ticket=failed.ticket, phase="coding", status=Task.Status.COMPLETED)
        question = DeferredQuestion.record("Which path?", parked_task=successor, **OWNER_DECISION)

        assert question.answered_at is None
        assert not [report for report in self._reports() if report.payload["kind"] == "task_failed"]

    def test_failure_after_completion_remains_current(self) -> None:
        old = TaskFactory(phase="coding")
        old.fail(reason="ProcessError: worker exited", by_holder=True)
        TaskFactory(ticket=old.ticket, phase="coding", status=Task.Status.COMPLETED)
        current = TaskFactory(ticket=old.ticket, phase="coding")
        current.fail(reason="ProcessError: worker exited", by_holder=True)

        reports = [report for report in self._reports() if report.payload["kind"] == "task_failed"]
        assert [task_id for report in reports for task_id in report.payload["ids"]] == [current.pk]

    def test_only_later_same_ticket_same_nonblank_phase_recovers_failure(self) -> None:
        older_completion = TaskFactory(status=Task.Status.COMPLETED)
        after_older = TaskFactory(ticket=older_completion.ticket)
        after_older.fail(reason="ProcessError: worker exited", by_holder=True)

        other_phase = TaskFactory()
        other_phase.fail(reason="ProcessError: worker exited", by_holder=True)
        TaskFactory(ticket=other_phase.ticket, phase="reviewing", status=Task.Status.COMPLETED)

        other_ticket = TaskFactory()
        other_ticket.fail(reason="ProcessError: worker exited", by_holder=True)
        TaskFactory(phase=other_ticket.phase, status=Task.Status.COMPLETED)

        blank_phase = TaskFactory(phase="")
        blank_phase.fail(reason="ProcessError: worker exited", by_holder=True)
        TaskFactory(ticket=blank_phase.ticket, phase="", status=Task.Status.COMPLETED)

        reports = [report for report in self._reports() if report.payload["kind"] == "task_failed"]
        assert {task_id for report in reports for task_id in report.payload["ids"]} == {
            after_older.pk,
            other_phase.pk,
            other_ticket.pk,
            blank_phase.pk,
        }

    def test_newer_claimed_and_substantive_failed_tasks_supersede_old_failure(self) -> None:
        claimed_old = TaskFactory()
        claimed_old.fail(reason="ProcessError: worker exited", by_holder=True)
        TaskFactory(ticket=claimed_old.ticket, status=Task.Status.CLAIMED)

        failed_old = TaskFactory()
        failed_old.fail(reason="ProcessError: worker exited", by_holder=True)
        failed_new = TaskFactory(ticket=failed_old.ticket)
        failed_new.fail(reason="ProcessError: worker exited", by_holder=True)

        reports = [report for report in self._reports() if report.payload["kind"] == "task_failed"]
        assert {task_id for report in reports for task_id in report.payload["ids"]} == {failed_new.pk}

    def test_later_completion_resolves_task_failed_firing(self) -> None:
        failed = TaskFactory(phase="testing")
        failed.fail(reason="ProcessError: worker exited", by_holder=True)
        detector = LifecycleIncidentDetector()
        first = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow())
        firing = SelfImproveFiring.objects.get(dedup_key=first.reports[0].dedup_key)
        assert firing.resolved_at is None

        TaskFactory(ticket=failed.ticket, phase="test", status=Task.Status.COMPLETED)
        second = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow())

        assert not [report for report in second.reports if report.payload["kind"] == "task_failed"]
        assert SelfImproveFiring.objects.get(pk=firing.pk).resolved_at is not None

    def test_later_completion_recovers_repair_task_failure(self) -> None:
        repair_ticket = TicketFactory(extra={"source": "self_improve"})
        failed = TaskFactory(ticket=repair_ticket, phase="testing")
        failed.fail(reason="ProcessError: worker exited", by_holder=True)
        TaskFactory(ticket=repair_ticket, phase="test", status=Task.Status.COMPLETED)

        assert not [report for report in self._reports() if report.payload["kind"] == "repair_task_failed"]

    def test_old_inbound_question_without_answer_is_an_incident(self) -> None:
        row = _owner_dm(channel="D1", slack_ts="100.1", text="Why did my task fail?")
        assert row is not None
        PendingChatInjection.objects.filter(pk=row.pk).update(received_at=timezone.now() - timedelta(hours=2))

        reports = self._reports()

        unanswered = [report for report in reports if report.payload["kind"] == "inbound_unanswered"]
        assert unanswered
        assert unanswered[0].payload["cause"] == "message_without_confirmed_response"
        assert "confirmed response" in unanswered[0].payload["suggested_action"]
        assert "Why did my task fail?" not in str(reports)

    def test_old_inbound_instruction_without_response_is_an_incident(self) -> None:
        row = _owner_dm(channel="D1", slack_ts="100.5", text="Please fix task 42")
        assert row is not None
        PendingChatInjection.objects.filter(pk=row.pk).update(received_at=timezone.now() - timedelta(hours=2))

        assert any(report.payload["kind"] == "inbound_unanswered" for report in self._reports())

    def test_inbound_scan_limits_rows_before_materializing(self) -> None:
        received_at = timezone.now() - timedelta(hours=2)
        PendingChatInjection.objects.bulk_create(
            PendingChatInjection(
                channel="D1",
                slack_ts=f"bulk-{index}",
                text=f"Why did task {index} fail?",
                user_id=OWNER_SLACK_ID,
                received_at=received_at,
            )
            for index in range(150)
        )

        with CaptureQueriesContext(connection) as queries:
            reports = self._reports()

        unanswered = next(report for report in reports if report.payload["kind"] == "inbound_unanswered")
        assert unanswered.payload["count"] == 100
        assert any(
            "teatree_pending_chat_injection" in query["sql"] and "LIMIT 100" in query["sql"] for query in queries
        )

    def test_ack_claim_is_unanswered_until_its_delivery_is_confirmed(self) -> None:
        row = _owner_dm(channel="D1", slack_ts="100.2", text="What happened?")
        assert row is not None
        PendingChatInjection.objects.filter(pk=row.pk).update(received_at=timezone.now() - timedelta(hours=2))
        row.mark_loop_replied(PendingChatInjection.AnswerKind.ACK)
        assert any(report.payload["kind"] == "inbound_unanswered" for report in self._reports())

        row.observe_confirmed_loop_reply()
        assert not [report for report in self._reports() if report.payload["kind"] == "inbound_unanswered"]

    def test_claimed_simple_reply_without_delivery_is_still_unanswered(self) -> None:
        row = _owner_dm(channel="D1", slack_ts="100.3", text="What happened?")
        assert row is not None
        PendingChatInjection.objects.filter(pk=row.pk).update(received_at=timezone.now() - timedelta(hours=2))
        row.mark_loop_replied(PendingChatInjection.AnswerKind.SIMPLE)

        assert any(report.payload["kind"] == "inbound_unanswered" for report in self._reports())

    def test_verified_simple_reply_has_durable_receipt_and_is_not_unanswered(self) -> None:
        row = _owner_dm(channel="D1", slack_ts="100.4", text="What happened?")
        assert row is not None
        PendingChatInjection.objects.filter(pk=row.pk).update(received_at=timezone.now() - timedelta(hours=2))
        row.mark_loop_replied(PendingChatInjection.AnswerKind.SIMPLE)
        row.observe_confirmed_loop_reply()

        row.refresh_from_db()
        assert row.loop_response_confirmed_at is not None
        assert not [report for report in self._reports() if report.payload["kind"] == "inbound_unanswered"]

    def test_old_unposted_question_is_delivery_failure_and_posted_question_waits(self) -> None:
        unposted = DeferredQuestion.record("Which option?", session_id="s", **OWNER_DECISION)
        posted = DeferredQuestion.record(
            "Can you approve?", session_id="s", slack_ts="200.1", slack_channel="D1", **OWNER_DECISION
        )
        DeferredQuestion.objects.filter(pk__in=[unposted.pk, posted.pk]).update(
            created_at=timezone.now() - timedelta(hours=2)
        )

        kinds = {report.payload["kind"] for report in self._reports()}

        assert {"outbound_unposted", "outbound_unanswered"} <= kinds
        unposted.mark_mirrored(channel="D1", slack_ts="200.2")
        DeferredQuestion.consume(posted.pk, answer="yes")
        kinds_after = {report.payload["kind"] for report in self._reports()}
        assert "outbound_unposted" not in kinds_after
        assert "outbound_unanswered" in kinds_after

    def test_quiet_hours_hold_is_not_a_delivery_failure(self) -> None:
        question = DeferredQuestion.record("Which option?", session_id="s", **OWNER_DECISION)
        DeferredQuestion.objects.filter(pk=question.pk).update(created_at=timezone.now() - timedelta(hours=2))
        check_at = timezone.now() - timedelta(hours=3)
        detector = LifecycleIncidentDetector(now=lambda: check_at)

        with patch("teatree.core.modelkit.reask_cadence.owner_quiet_at", return_value=True) as quiet:
            kinds = {report.payload["kind"] for report in detector.detect()}

        assert "outbound_unposted" not in kinds
        assert quiet.call_args.args[0] == check_at.astimezone(owner_zone()).time()

    def test_delivery_incident_waits_ten_minutes_after_quiet_hours(self) -> None:
        check_at = timezone.now().astimezone(owner_zone()).replace(hour=8, minute=5, second=0, microsecond=0)
        question = DeferredQuestion.record("Which option?", session_id="s", **OWNER_DECISION)
        DeferredQuestion.objects.filter(pk=question.pk).update(created_at=check_at - timedelta(hours=2))

        with patch("teatree.core.modelkit.reask_cadence.owner_quiet_at", side_effect=owner_quiet_at):
            early = LifecycleIncidentDetector(now=lambda: check_at).detect()
            later = LifecycleIncidentDetector(now=lambda: check_at + timedelta(minutes=6)).detect()

        assert "outbound_unposted" not in {report.payload["kind"] for report in early}
        assert "outbound_unposted" in {report.payload["kind"] for report in later}

    def test_unposted_incident_across_quiet_hours_boundary(self) -> None:
        noon = timezone.now().astimezone(owner_zone()).replace(hour=12, minute=0, second=0, microsecond=0)
        question = DeferredQuestion.record("Which option?", session_id="s", **OWNER_DECISION)
        DeferredQuestion.objects.filter(pk=question.pk).update(created_at=noon - timedelta(hours=11))

        with patch("teatree.core.modelkit.reask_cadence.owner_quiet_at", side_effect=owner_quiet_at):
            for hour, minute, expected in ((3, 0, False), (8, 5, False), (8, 15, True), (12, 0, True)):
                with self.subTest(hour=hour, minute=minute):
                    now = noon.replace(hour=hour, minute=minute)
                    kinds = {
                        report.payload["kind"]
                        for report in LifecycleIncidentDetector(now=lambda check_at=now: check_at).detect()
                    }
                    assert ("outbound_unposted" in kinds) is expected

    def test_quiet_hours_keep_a_posted_unanswered_question_visible(self) -> None:
        check_at = timezone.now().astimezone(owner_zone()).replace(hour=3, minute=0, second=0, microsecond=0)
        row = DeferredQuestion.record("Which option?", slack_ts="200.1", slack_channel="D1", **OWNER_DECISION)
        DeferredQuestion.objects.filter(pk=row.pk).update(created_at=check_at - timedelta(hours=2))

        with patch("teatree.core.modelkit.reask_cadence.owner_quiet_at", side_effect=owner_quiet_at):
            kinds = {report.payload["kind"] for report in LifecycleIncidentDetector(now=lambda: check_at).detect()}

        assert "outbound_unanswered" in kinds

    def test_growing_unposted_count_reuses_repair_without_immediate_alert(self) -> None:
        first = DeferredQuestion.record("First?", session_id="s", **OWNER_DECISION)
        DeferredQuestion.objects.filter(pk=first.pk).update(created_at=timezone.now() - timedelta(hours=2))
        alerts: list[str] = []
        detector = LifecycleIncidentDetector(overlay_name="t3-teatree")
        delivery = DeliveryRoutes(
            overlay_name="t3-teatree", owner_alert=lambda report, _existing: alerts.append(report.summary) or True
        )

        run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery)
        second = DeferredQuestion.record("Second?", session_id="s", **OWNER_DECISION)
        DeferredQuestion.objects.filter(pk=second.pk).update(created_at=timezone.now() - timedelta(hours=2))
        result = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery)

        assert result.reports[0].payload["count"] == 2
        assert alerts == []
        assert Ticket.objects.filter(extra__source="self_improve").count() == 1

    def test_new_unposted_gap_reuses_open_repair_without_immediate_alert(self) -> None:
        first = DeferredQuestion.record("First?", session_id="s", **OWNER_DECISION)
        DeferredQuestion.objects.filter(pk=first.pk).update(created_at=timezone.now() - timedelta(hours=2))
        alerts: list[str] = []
        detector = LifecycleIncidentDetector(overlay_name="t3-teatree")
        delivery = DeliveryRoutes(
            overlay_name="t3-teatree", owner_alert=lambda report, _existing: alerts.append(report.summary) or True
        )

        run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery)
        DeferredQuestion.consume(first.pk, answer="yes")
        run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery)
        second = DeferredQuestion.record("Second?", session_id="s", **OWNER_DECISION)
        DeferredQuestion.objects.filter(pk=second.pk).update(created_at=timezone.now() - timedelta(hours=2))
        run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery)

        assert alerts == []
        assert Ticket.objects.filter(extra__source="self_improve").count() == 1

    def test_stale_claim_with_no_heartbeat_is_an_incident(self) -> None:
        task = TaskFactory(status=Task.Status.CLAIMED)
        Task.objects.filter(pk=task.pk).update(
            lease_expires_at=timezone.now() - timedelta(minutes=20),
            heartbeat_at=timezone.now() - timedelta(minutes=30),
        )

        assert any(report.payload["kind"] == "task_stalled" for report in self._reports())

    def test_critical_resource_budget_cannot_hide_an_unanswered_message(self) -> None:
        for index in range(3):
            row = _owner_dm(channel="D1", slack_ts=f"100.{index + 6}", text="What is happening?")
            assert row is not None
            PendingChatInjection.objects.filter(pk=row.pk).update(received_at=timezone.now() - timedelta(hours=2))
        detector = next(
            detector for detector in detectors_for_tier(Tier.CHEAP) if detector.name == "lifecycle_incident"
        )

        result = run_tier(
            Tier.CHEAP,
            detectors=[detector],
            budget=BudgetVerdict.skip("low_disk (used=98%)"),
        )

        assert result.skipped is True
        assert any(report.payload["kind"] == "inbound_unanswered" for report in result.reports)
        assert len(result.actions) == 1
        assert result.actions[0].rung == ActionRung.TICKET
        assert Ticket.objects.filter(pk=result.actions[0].firing.ticket_id).exists()

    def test_recovered_incident_can_open_a_fresh_fix_ticket_when_it_recurs(self) -> None:
        detector = next(
            detector for detector in detectors_for_tier(Tier.CHEAP) if detector.name == "lifecycle_incident"
        )
        first_tasks = [TaskFactory() for _ in range(3)]
        for task in first_tasks:
            task.fail(reason="ProcessError: worker exited", by_holder=True)
        first = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow())
        first_ticket_id = first.actions[0].firing.ticket_id
        assert first_ticket_id is not None

        for task in first_tasks:
            task.reopen()
        run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow())
        assert SelfImproveFiring.objects.get(detector="lifecycle_incident").resolved_at is not None
        Ticket.objects.filter(pk=first_ticket_id).update(state=Ticket.State.MERGED)

        for _ in range(3):
            task = TaskFactory()
            task.fail(reason="ProcessError: worker exited", by_holder=True)
        second = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow())

        assert len(second.actions) == 1
        assert second.actions[0].rung == ActionRung.TICKET
        assert second.actions[0].firing.ticket_id != first_ticket_id
        assert Ticket.objects.filter(extra__source="self_improve").count() == 2

    def test_recent_otel_attempt_failures_are_consumed_after_tasks_recover(self) -> None:
        now = timezone.now()
        rows = [
            {
                "epoch": int(now.timestamp()),
                "kind": "attempt.finished",
                "entity_id": index,
                "ticket_id": index,
                "task_id": index,
                "cause": "harness_crash",
            }
            for index in (1, 2, 3)
        ]
        with TemporaryDirectory() as directory:
            path = Path(directory) / f"lifecycle-{now.date().isoformat()}.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            reports = LifecycleIncidentDetector(directory=Path(directory), now=lambda: now).detect()

        assert len(reports) == 1
        assert reports[0].payload["kind"] == "attempt_failure_burst"
        assert reports[0].payload["cause"] == "harness_crash"
        assert reports[0].requested_rung == ActionRung.TICKET

    def _attempt_burst_reports(
        self, errors: list[str], cause: str, *, distinct_fingerprints: bool = False
    ) -> tuple[list[DetectorReport], list[int], list[int]]:
        now = timezone.now()
        TaskAttemptFactory.create_batch(len(errors) + 1, task=TaskFactory())
        tasks = [TaskFactory() for _ in errors]
        attempts = [
            TaskAttemptFactory(task=task, exit_code=1, error=error) for task, error in zip(tasks, errors, strict=True)
        ]
        assert {task.pk for task in tasks}.isdisjoint(attempt.pk for attempt in attempts)
        rows = [
            {
                "epoch": int(now.timestamp()),
                "kind": "attempt.finished",
                "entity_id": attempt.pk,
                "ticket_id": task.ticket_id,
                "task_id": task.pk,
                "cause": cause,
                "error_fingerprint": f"{index:064x}" if distinct_fingerprints else "a" * 64,
                "trace_id": f"{index:032x}",
                "span_id": f"{index:016x}",
            }
            for index, (task, attempt) in enumerate(zip(tasks, attempts, strict=True), start=1)
        ]
        with TemporaryDirectory() as directory:
            path = Path(directory) / f"lifecycle-{now.date().isoformat()}.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            reports = LifecycleIncidentDetector(directory=Path(directory), now=lambda: now).detect()
        return reports, [task.pk for task in tasks], [attempt.pk for attempt in attempts]

    def test_usage_window_attempts_do_not_create_a_failure_burst(self) -> None:
        errors = ["subscription_weekly: seven_day — retry after the weekly reset"] * 3

        reports, _, _ = self._attempt_burst_reports(errors, "usage_limit_parked")

        assert not [report for report in reports if report.payload["kind"] == "attempt_failure_burst"]

    def test_api_credit_attempts_still_create_a_failure_burst(self) -> None:
        reports, task_ids, _ = self._attempt_burst_reports(
            ["api_credit: credit balance is too low"] * 3, "credential_exhausted"
        )

        bursts = [report for report in reports if report.payload["kind"] == "attempt_failure_burst"]
        assert len(bursts) == 1
        assert bursts[0].payload["count"] == 3
        assert bursts[0].payload["ids"] == task_ids

    def test_distinct_attempt_burst_names_each_failure(self) -> None:
        errors = [
            "malformed rubric_grades: verdict merge_safe with FAIL",
            "unexpected keys: review_verdict fields returned flat",
            "owner stop card refused: checked fact carries shorthand",
        ]
        reports, task_ids, attempt_ids = self._attempt_burst_reports(
            errors, "result_schema_invalid", distinct_fingerprints=True
        )

        burst = next(report for report in reports if report.payload["kind"] == "attempt_failure_burst")
        action = burst.payload["suggested_action"]
        for task_id, attempt_id, error in zip(task_ids, attempt_ids, errors, strict=True):
            assert f"attempt {attempt_id}" in action
            assert f"task {task_id}" in action
            assert error in action
        assert "Fingerprint " not in action
        assert burst.payload["recovery_semantics"] == "manual_ticket_review"
        assert burst.requested_rung == ActionRung.TICKET

    def test_distinct_attempt_burst_bounds_error_digest(self) -> None:
        errors = ["x" * 5000 + "\nsecond line"] + [f"error {index}" for index in range(2, 7)]
        reports, _, attempt_ids = self._attempt_burst_reports(
            errors, "result_schema_invalid", distinct_fingerprints=True
        )

        burst = next(report for report in reports if report.payload["kind"] == "attempt_failure_burst")
        action = burst.payload["suggested_action"]
        assert "x" * 100 in action
        assert "x" * 101 not in action
        assert "second line" not in action
        assert all(f"attempt {attempt_id}" in action for attempt_id in attempt_ids[:5])
        assert f"attempt {attempt_ids[5]}" not in action

    def test_provider_budget_and_quoted_window_marker_still_create_failure_bursts(self) -> None:
        cases = (
            ("provider_budget: wallet depleted", "credential_exhausted"),
            ("Traceback: subscription_weekly: marker quoted by crash", "harness_crash"),
        )
        for error, cause in cases:
            with self.subTest(error=error):
                reports, task_ids, _ = self._attempt_burst_reports([error] * 3, cause)
                bursts = [report for report in reports if report.payload["kind"] == "attempt_failure_burst"]
                assert len(bursts) == 1
                assert bursts[0].payload["ids"] == task_ids

    def test_mixed_attempt_burst_names_and_cites_only_real_failures(self) -> None:
        reports, task_ids, attempt_ids = self._attempt_burst_reports(
            ["all Claude accounts are exhausted"] * 2 + ["api_credit: credit balance is too low"] * 3,
            "credential_exhausted",
        )

        bursts = [report for report in reports if report.payload["kind"] == "attempt_failure_burst"]
        assert len(bursts) == 1
        assert bursts[0].payload["count"] == 3
        assert bursts[0].payload["ids"] == task_ids[2:]
        evidence = bursts[0].payload["suggested_action"]
        assert f"Fingerprint {'a' * 64}. Evidence: otel:" in evidence
        assert "Distinct failures:" not in evidence
        assert all(f'"entity_id":{attempt_id}' not in evidence for attempt_id in attempt_ids[:2])
        assert all(f'"entity_id":{attempt_id}' in evidence for attempt_id in attempt_ids[3:])

    def test_failed_usage_window_task_still_creates_task_failed_report(self) -> None:
        task = TaskFactory()
        task.fail(reason="subscription_weekly: weekly limit reached", by_holder=True)

        reports = [report for report in self._reports() if report.payload["kind"] == "task_failed"]

        assert len(reports) == 1
        assert reports[0].payload["cause"] == "usage_limit_parked"
        assert reports[0].payload["ids"] == [task.pk]

    def _burst_ticket_context(self, cause: str) -> str:
        now = timezone.now()
        tasks = [TaskFactory() for _ in range(3)]
        rows = [
            {
                "epoch": int(now.timestamp()),
                "kind": "attempt.finished",
                "entity_id": index,
                "ticket_id": task.ticket_id,
                "task_id": task.pk,
                "cause": cause,
                "error_fingerprint": "a" * 64,
                "trace_id": f"{index:032x}",
                "span_id": f"{index:016x}",
            }
            for index, task in enumerate(tasks, start=1)
        ]
        with TemporaryDirectory() as directory:
            path = Path(directory) / f"lifecycle-{now.date().isoformat()}.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            detector = LifecycleIncidentDetector(directory=Path(directory), now=lambda: now, overlay_name="t3-teatree")
            result = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow())

        assert len(result.actions) == 1
        ticket = result.actions[0].firing.ticket
        assert ticket is not None
        return ticket.context

    def test_attempt_burst_ticket_cites_two_distinct_attempts_and_fingerprint(self) -> None:
        context = self._burst_ticket_context("harness_crash")

        assert "a" * 64 in context
        assert context.count("otel:") == 2
        assert '"entity_id":3' in context
        assert '"entity_id":2' in context

    def test_a_causeless_burst_cites_no_fingerprint(self) -> None:
        context = self._burst_ticket_context("no_result_envelope")

        assert "a" * 64 not in context
        assert "otel:" not in context
        assert "Distinct failures:" not in context

    def test_single_failed_task_repairs_first_and_keeps_one_ticket(self) -> None:
        task = TaskFactory()
        task.fail(reason="ProcessError: worker exited", by_holder=True)
        alerts = []
        detector = LifecycleIncidentDetector(overlay_name="t3-teatree")
        delivery = DeliveryRoutes(
            overlay_name="t3-teatree", owner_alert=lambda report, _: alerts.append(report) or True
        )

        first = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery)
        assert [action.rung for action in first.actions] == [ActionRung.TICKET]
        assert Ticket.objects.filter(extra__source="self_improve").count() == 1
        firing = first.actions[0].firing
        SelfImproveFiring.objects.filter(pk=firing.pk).update(first_fired_at=timezone.now() - timedelta(minutes=31))

        alerted = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery)
        assert [action.rung for action in alerted.actions] == [ActionRung.TICKET]
        assert alerted.actions[0].firing.ticket_id == firing.ticket_id
        assert alerts == []
        assert not run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery).actions

        SelfImproveFiring.objects.filter(pk=firing.pk).update(
            first_fired_at=timezone.now() - timedelta(hours=2, minutes=1)
        )
        ticketed = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery)
        assert ticketed.actions == []
        assert SelfImproveFiring.objects.get(pk=firing.pk).ticket.overlay == "t3-teatree"
        assert Ticket.objects.filter(extra__source="self_improve").count() == 1
        assert not run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery).actions

    def test_single_unanswered_message_reaches_repair_after_failed_alert(self) -> None:
        row = _owner_dm(channel="D1", slack_ts="111.1", text="Please respond", overlay="t3-teatree")
        assert row is not None
        PendingChatInjection.objects.filter(pk=row.pk).update(received_at=timezone.now() - timedelta(hours=2))
        detector = LifecycleIncidentDetector(overlay_name="t3-teatree")
        delivery = DeliveryRoutes(overlay_name="t3-teatree", owner_alert=lambda *_: False)

        first = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery)
        assert [action.rung for action in first.actions] == [ActionRung.TICKET]
        firing = first.actions[0].firing
        SelfImproveFiring.objects.filter(pk=firing.pk).update(first_fired_at=timezone.now() - timedelta(hours=3))

        ticketed = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery)
        assert [action.rung for action in ticketed.actions] == [ActionRung.TICKET]
        assert ticketed.actions[0].firing.ticket_id == firing.ticket_id
        assert Ticket.objects.filter(extra__source="self_improve").count() == 1

    def test_single_failed_task_reaches_repair_without_a_delivered_alert(self) -> None:
        task = TaskFactory()
        task.fail(reason="ProcessError: worker exited", by_holder=True)
        detector = LifecycleIncidentDetector(overlay_name="t3-teatree")
        delivery = DeliveryRoutes(overlay_name="t3-teatree", owner_alert=lambda *_: False)

        first = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery)
        SelfImproveFiring.objects.filter(pk=first.actions[0].firing.pk).update(
            first_fired_at=timezone.now() - timedelta(hours=3)
        )
        ticketed = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery)

        assert [action.rung for action in ticketed.actions] == [ActionRung.TICKET]
        assert ticketed.actions[0].firing.ticket_id == first.actions[0].firing.ticket_id
        assert first.actions[0].firing.ticket_id is not None

    def test_reopened_singleton_starts_new_age_window(self) -> None:
        task = TaskFactory()
        task.fail(reason="ProcessError: worker exited", by_holder=True)
        detector = LifecycleIncidentDetector(overlay_name="t3-teatree")
        delivery = DeliveryRoutes(overlay_name="t3-teatree")
        first = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery)
        firing = first.actions[0].firing
        SelfImproveFiring.objects.filter(pk=firing.pk).update(first_fired_at=timezone.now() - timedelta(days=1))
        task.reopen()
        run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery)
        task.fail(reason="ProcessError: worker exited", by_holder=True)

        reopened = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery)

        assert [action.rung for action in reopened.actions] == [ActionRung.TICKET]
        assert timezone.now() - SelfImproveFiring.objects.get(pk=firing.pk).first_fired_at < timedelta(minutes=1)

    def test_lifecycle_detector_does_not_mix_overlay_owned_rows(self) -> None:
        own_task = TaskFactory(ticket=TicketFactory(overlay="t3-teatree"))
        foreign_task = TaskFactory(ticket=TicketFactory(overlay="foreign"))
        own_task.fail(reason="ProcessError: worker exited", by_holder=True)
        foreign_task.fail(reason="ProcessError: worker exited", by_holder=True)
        own_message = _owner_dm(channel="D1", slack_ts="222.1", text="Own question?", overlay="t3-teatree")
        foreign_message = _owner_dm(channel="D2", slack_ts="222.2", text="Foreign question?", overlay="foreign")
        assert own_message is not None
        assert foreign_message is not None
        PendingChatInjection.objects.filter(pk__in=[own_message.pk, foreign_message.pk]).update(
            received_at=timezone.now() - timedelta(hours=2)
        )

        reports = LifecycleIncidentDetector(overlay_name="t3-teatree").detect()

        assert {report.payload["kind"] for report in reports} == {"task_failed", "inbound_unanswered"}
        assert {tuple(report.payload["ids"]) for report in reports} == {(own_task.pk,), (own_message.pk,)}

    def test_scoped_scan_does_not_resolve_another_overlays_incident(self) -> None:
        task = TaskFactory(ticket=TicketFactory(overlay="foreign"))
        task.fail(reason="ProcessError: worker exited", by_holder=True)
        foreign_report = LifecycleIncidentDetector(overlay_name="foreign").detect()[0]
        foreign_firing = record_firing(foreign_report, action=ActionRung.STATUSLINE)

        own = run_tier(
            Tier.CHEAP,
            detectors=[LifecycleIncidentDetector(overlay_name="t3-teatree")],
            budget=BudgetVerdict.allow(),
        )

        assert own.reports == []
        assert SelfImproveFiring.objects.get(pk=foreign_firing.pk).resolved_at is None

    def test_first_observation_requests_a_ticket_only_when_the_repair_wait_is_zero(self) -> None:
        task = TaskFactory()
        task.fail(reason="ProcessError: worker exited", by_holder=True)

        waiting = LifecycleIncidentDetector(overlay_name="t3-teatree").detect()
        immediate = LifecycleIncidentDetector(overlay_name="t3-teatree", repair_after=timedelta(0)).detect()

        assert [report.requested_rung for report in waiting] == [ActionRung.SLACK]
        assert [report.requested_rung for report in immediate] == [ActionRung.TICKET]

    def test_singleton_age_changes_reuse_early_repair_ticket(self) -> None:
        task = TaskFactory()
        task.fail(reason="ProcessError: worker exited", by_holder=True)
        detector = LifecycleIncidentDetector(
            overlay_name="t3-teatree", owner_alert_after=timedelta(minutes=5), repair_after=timedelta(minutes=10)
        )
        delivery = DeliveryRoutes(overlay_name="t3-teatree", owner_alert=lambda *_: True)
        first = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery)
        firing = first.actions[0].firing
        SelfImproveFiring.objects.filter(pk=firing.pk).update(first_fired_at=timezone.now() - timedelta(minutes=4))

        before = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery)
        assert [action.rung for action in before.actions] == [ActionRung.TICKET]
        assert before.actions[0].firing.ticket_id == firing.ticket_id

        SelfImproveFiring.objects.filter(pk=firing.pk).update(first_fired_at=timezone.now() - timedelta(minutes=6))
        alerted = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery)
        assert alerted.actions == []
        assert SelfImproveFiring.objects.get(pk=firing.pk).ticket_id is not None

    def test_unattributed_outbound_incident_repairs_deduplicates_and_recovers(self) -> None:
        question = DeferredQuestion.record("Which option?", session_id="unknown", **OWNER_DECISION)
        DeferredQuestion.objects.filter(pk=question.pk).update(created_at=timezone.now() - timedelta(hours=2))
        attempts: list[str] = []

        def owner_alert(report: DetectorReport, _existing: SelfImproveFiring | None) -> bool:
            attempts.append(report.dedup_key)
            return False

        detector = LifecycleIncidentDetector(overlay_name="t3-teatree")
        delivery = DeliveryRoutes(overlay_name="t3-teatree", owner_alert=owner_alert)

        def scan() -> TierResult:
            return run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery)

        result = scan()

        assert {report.payload["kind"] for report in result.reports} == {"outbound_unposted"}
        assert result.reports[0].payload["requires_delivery"] is True
        assert "overlay_name" not in result.reports[0].payload
        assert result.reports[0].max_rung == ActionRung.SLACK
        assert [action.rung for action in result.actions] == [ActionRung.TICKET]
        assert SelfImproveFiring.objects.get().ticket_id is not None
        assert scan().actions == []
        assert attempts == []

        question.mark_mirrored(channel="D1", slack_ts="200.2")
        recovered = scan()
        assert {report.payload["kind"] for report in recovered.reports} == {"outbound_unanswered"}
        assert SelfImproveFiring.objects.get(dedup_key=result.reports[0].dedup_key).resolved_at is not None
        assert attempts == []

        recurrence = DeferredQuestion.record("Which next option?", session_id="unknown", **OWNER_DECISION)
        DeferredQuestion.objects.filter(pk=recurrence.pk).update(created_at=timezone.now() - timedelta(hours=2))
        assert [action.rung for action in scan().actions] == [ActionRung.TICKET]
        assert attempts == []
        assert Ticket.objects.filter(extra__source="self_improve").count() == 1

    def test_explicit_detector_cannot_route_foreign_evidence_to_local_owner(self) -> None:
        task = TaskFactory(ticket=TicketFactory(overlay="foreign"))
        task.fail(reason="ProcessError: worker exited", by_holder=True)

        with self.assertRaisesMessage(ImproperlyConfigured, "belongs to overlay"):
            run_tier(
                Tier.CHEAP,
                detectors=[LifecycleIncidentDetector(overlay_name="foreign")],
                budget=BudgetVerdict.allow(),
                delivery=DeliveryRoutes(overlay_name="t3-teatree"),
            )
        assert SelfImproveFiring.objects.count() == 0

    def test_failed_repair_task_remains_visible_without_spawning_another_repair(self) -> None:
        repair_ticket = TicketFactory(overlay="t3-teatree", extra={"source": "self_improve"})
        repair_task = TaskFactory(ticket=repair_ticket)
        repair_task.fail(reason="ProcessError: repair worker exited", by_holder=True)
        detector = LifecycleIncidentDetector(overlay_name="t3-teatree")
        delivery = DeliveryRoutes(overlay_name="t3-teatree", owner_alert=lambda *_: True)

        first = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery)
        assert {report.payload["kind"] for report in first.reports} == {"repair_task_failed"}
        assert [action.rung for action in first.actions] == [ActionRung.STATUSLINE]
        SelfImproveFiring.objects.filter(pk=first.actions[0].firing.pk).update(
            first_fired_at=timezone.now() - timedelta(days=1)
        )

        again = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery)

        assert again.actions == []
        assert not run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery).actions
        assert Ticket.objects.filter(extra__source="self_improve").count() == 1

    def test_stalled_repair_task_does_not_create_recursive_ticket(self) -> None:
        repair_ticket = TicketFactory(overlay="t3-teatree", extra={"source": "self_improve"})
        repair_task = TaskFactory(ticket=repair_ticket, status=Task.Status.CLAIMED)
        Task.objects.filter(pk=repair_task.pk).update(
            lease_expires_at=timezone.now() - timedelta(minutes=20),
            heartbeat_at=timezone.now() - timedelta(minutes=30),
        )

        detector = LifecycleIncidentDetector(overlay_name="t3-teatree")
        delivery = DeliveryRoutes(overlay_name="t3-teatree", owner_alert=lambda *_: True)
        result = run_tier(
            Tier.CHEAP,
            detectors=[detector],
            budget=BudgetVerdict.allow(),
            delivery=delivery,
        )

        assert {report.payload["kind"] for report in result.reports} == {"repair_task_stalled"}
        assert [action.rung for action in result.actions] == [ActionRung.STATUSLINE]
        SelfImproveFiring.objects.filter(pk=result.actions[0].firing.pk).update(
            first_fired_at=timezone.now() - timedelta(minutes=31)
        )
        alerted = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery)
        assert alerted.actions == []
        assert Ticket.objects.filter(extra__source="self_improve").count() == 1

    def test_repair_attempt_burst_does_not_create_recursive_ticket(self) -> None:
        now = timezone.now()
        repair_ticket = TicketFactory(overlay="t3-teatree", extra={"source": "self_improve"})
        tasks = [TaskFactory(ticket=repair_ticket) for _ in range(3)]
        rows = [
            {
                "epoch": int(now.timestamp()),
                "kind": "attempt.finished",
                "entity_id": index,
                "ticket_id": repair_ticket.pk,
                "task_id": task.pk,
                "cause": "harness_crash",
            }
            for index, task in enumerate(tasks, start=1)
        ]
        with TemporaryDirectory() as directory:
            path = Path(directory) / f"lifecycle-{now.date().isoformat()}.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))

            reports = LifecycleIncidentDetector(
                directory=Path(directory), now=lambda: now, overlay_name="t3-teatree"
            ).detect()

        assert not [report for report in reports if report.payload["kind"] == "attempt_failure_burst"]


class PhaseWedgeIncidentTests(TestCase):
    def _wedged(self, *, overlay: str = "t3-teatree", phase: str = "coding", source: str = "") -> Ticket:
        ticket = Ticket.objects.create(overlay=overlay, extra={"source": source} if source else {})
        record_stuck_transition_question(None, phase=phase, ticket=ticket, refusal="FixRecordDodError: no record")
        return ticket

    def _reports(self, overlay: str = "t3-teatree") -> dict[str, DetectorReport]:
        detector = LifecycleIncidentDetector(overlay_name=overlay)
        return {report.payload["kind"]: report for report in detector.detect()}

    def test_a_wedge_is_a_ticket_rung_incident_and_a_repair_wedge_stays_on_the_statusline(self) -> None:
        wedged = self._wedged(phase="retro")
        repair = self._wedged(phase="planning", source="self_improve")

        reports = self._reports()

        assert reports["phase_wedge"].payload["cause"] == "retro"
        assert reports["phase_wedge"].payload["ids"] == [wedged.pk]
        assert reports["phase_wedge"].requested_rung == ActionRung.TICKET
        assert reports["repair_phase_wedge"].payload["ids"] == [repair.pk]
        assert reports["repair_phase_wedge"].requested_rung == ActionRung.STATUSLINE

    def test_each_phase_a_ticket_is_wedged_in_is_reported_including_a_phase_with_a_digit(self) -> None:
        ticket = self._wedged(phase="retro")
        record_stuck_transition_question(None, phase="e2e", ticket=ticket, refusal="no e2e evidence")

        causes = sorted(
            report.payload["cause"]
            for report in LifecycleIncidentDetector(overlay_name="t3-teatree").detect()
            if report.payload["kind"] == "phase_wedge"
        )

        assert causes == ["e2e", "retro"]

    def test_another_overlays_wedge_and_a_dismissed_wedge_are_not_counted(self) -> None:
        self._wedged(overlay="other")
        dismissed = self._wedged()
        DeferredQuestion.objects.get(dedupe_marker__startswith=f"fsm-wedge:{dismissed.pk}:").mark_stale("done")

        assert "phase_wedge" not in self._reports()

    def test_a_wedge_incident_opens_one_repair_ticket_that_reaches_work_started(self) -> None:
        self._wedged(phase="retro")

        result = run_tier(
            Tier.CHEAP,
            detectors=[LifecycleIncidentDetector(overlay_name="t3-teatree")],
            budget=BudgetVerdict.allow(),
        )

        ticketed = [action for action in result.actions if action.rung == ActionRung.TICKET]
        assert len(ticketed) == 1
        repair = Ticket.objects.get(pk=ticketed[0].firing.ticket_id)
        assert repair.state == Ticket.State.WORK_STARTED
        assert repair.tasks.filter(phase="planning", status=Task.Status.PENDING).count() == 1


class InternalEscalationIncidentTests(TestCase):
    def _aged(self, question: str, *, age: timedelta = timedelta(hours=2), **fields) -> DeferredQuestion:
        row = DeferredQuestion.record(question, **fields)
        DeferredQuestion.objects.filter(pk=row.pk).update(created_at=timezone.now() - age)
        return row

    def _reports(self) -> dict[str, DetectorReport]:
        return {
            report.payload["kind"]: report for report in LifecycleIncidentDetector(overlay_name="t3-teatree").detect()
        }

    def test_internal_escalation_reported_at_ticket_rung(self) -> None:
        stall = self._aged(
            "Repair-loop stall on ticket 1: how should it proceed?", dedupe_marker="repair-stall:1:coding"
        )
        alerts: list[DetectorReport] = []

        def owner_alert(report: DetectorReport, _existing: SelfImproveFiring | None) -> bool:
            alerts.append(report)
            return True

        result = run_tier(
            Tier.CHEAP,
            detectors=[LifecycleIncidentDetector(overlay_name="t3-teatree")],
            budget=BudgetVerdict.allow(),
            delivery=DeliveryRoutes(overlay_name="t3-teatree", owner_alert=owner_alert),
        )

        [report] = result.reports
        assert (report.payload["kind"], report.payload["cause"]) == ("internal_escalation", "operational_question")
        assert report.payload["ids"] == [stall.pk]
        assert report.max_rung == ActionRung.TICKET
        assert report.payload["suggested_action"] == (
            "investigate from live state; fix, or `questions dismiss <ids> --reason '<evidence>'`; never ask the owner"
        )
        assert [action.rung for action in result.actions] == [ActionRung.TICKET]
        assert alerts == []

    def test_fresh_internal_row_not_reported(self) -> None:
        old = self._aged("Loop 'a' tick exceeded its deadline", dedupe_marker="loop-tick-timeout loop=a")
        self._aged(
            "Loop 'b' tick exceeded its deadline", age=timedelta(minutes=5), dedupe_marker="loop-tick-timeout loop=b"
        )
        self._aged("Which option?", **OWNER_DECISION)

        assert self._reports()["internal_escalation"].payload["ids"] == [old.pk]

    def test_fsm_wedge_not_double_reported(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree")
        record_stuck_transition_question(None, phase="retro", ticket=ticket, refusal="FixRecordDodError: no record")
        DeferredQuestion.objects.update(created_at=timezone.now() - timedelta(hours=2))
        stall = self._aged(
            "Repair-loop stall on ticket 1: how should it proceed?", dedupe_marker="repair-stall:1:coding"
        )

        reports = self._reports()

        assert reports["phase_wedge"].payload["ids"] == [ticket.pk]
        assert reports["internal_escalation"].payload["ids"] == [stall.pk]

    def test_a_withheld_question_is_asked_again_not_dismissed(self) -> None:
        withheld = self._aged("Is the acme profile right?", dedupe_marker="profile:acme", **OWNER_DECISION)
        withhold(withheld, ["the question carries internal shorthand (acme_only)"])
        stall = self._aged(
            "Repair-loop stall on ticket 1: how should it proceed?", dedupe_marker="repair-stall:1:coding"
        )

        reports = self._reports()

        report = reports["withheld_owner_question"]
        assert report.payload["cause"] == "owner_question_failed_plain_language_check"
        assert report.payload["ids"] == [withheld.pk]
        assert report.max_rung == ActionRung.TICKET
        assert report.payload["suggested_action"].startswith(
            "ask the owner again as a card under the same dedupe marker"
        )
        assert "never dismiss it unanswered" in report.payload["suggested_action"]
        assert reports["internal_escalation"].payload["ids"] == [stall.pk]
        assert "never ask the owner" not in report.payload["suggested_action"]

    def test_a_withheld_question_is_reported_at_once_not_after_an_hour(self) -> None:
        fresh = self._aged("Is the acme profile right?", age=timedelta(minutes=1), **OWNER_DECISION)
        withhold(fresh, ["a problem"])

        reports = self._reports()

        assert reports["withheld_owner_question"].payload["ids"] == [fresh.pk]
        assert "internal_escalation" not in reports

    def test_a_withheld_question_once_asked_again_is_no_longer_reported(self) -> None:
        withheld = self._aged("Is the acme profile right?", dedupe_marker="profile:acme", **OWNER_DECISION)
        withhold(withheld, ["a problem"])

        DeferredQuestion.record("Is the acme profile fine?", dedupe_marker="profile:acme", **OWNER_DECISION)

        assert "withheld_owner_question" not in self._reports()

    def test_internal_escalation_names_at_most_100_rows(self) -> None:
        old = timezone.now() - timedelta(hours=2)
        DeferredQuestion.objects.bulk_create(
            DeferredQuestion(question=f"stall {n}", audience=DeferredQuestion.Audience.INTERNAL, created_at=old)
            for n in range(101)
        )

        assert self._reports()["internal_escalation"].payload["count"] == 100
