"""Durable lifecycle gaps become actionable, deduplicated factory incidents."""

import json
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from teatree.core.models import DeferredQuestion, PendingChatInjection, SelfImproveFiring, Task, Ticket
from teatree.loop.self_improve.budget import BudgetVerdict
from teatree.loop.self_improve.detectors.base import ActionRung, DetectorReport
from teatree.loop.self_improve.detectors.lifecycle_incident import LifecycleIncidentDetector
from teatree.loop.self_improve.persistence import record_firing
from teatree.loop.self_improve.schedule import DeliveryRoutes, Tier, TierResult, detectors_for_tier, run_tier
from tests.factories import TaskFactory, TicketFactory


class LifecycleIncidentTests(TestCase):
    def _reports(self) -> list:
        detector = next(
            detector for detector in detectors_for_tier(Tier.CHEAP) if detector.name == "lifecycle_incident"
        )
        return detector.detect()

    def test_failed_task_is_grouped_by_named_cause(self) -> None:
        for _ in range(3):
            task = TaskFactory()
            task.fail(reason="ProcessError: worker exited")

        reports = self._reports()

        assert len(reports) == 1
        assert reports[0].payload["kind"] == "task_failed"
        assert reports[0].payload["count"] == 3
        assert reports[0].requested_rung == ActionRung.TICKET
        assert "worker exited" not in str(reports[0].payload)

    def test_recovered_and_cancelled_tasks_are_not_incidents(self) -> None:
        recovered = TaskFactory()
        recovered.fail(reason="ProcessError: worker exited")
        recovered.reopen()
        cancelled = TaskFactory()
        cancelled.fail(reason="cancelled: operator requested")

        assert not [report for report in self._reports() if report.payload["kind"] == "task_failed"]

    def test_long_running_task_that_fails_now_is_not_lost_to_creation_window(self) -> None:
        task = TaskFactory()
        Task.objects.filter(pk=task.pk).update(created_at=timezone.now() - timedelta(days=7))
        task.fail(reason="ProcessError: worker exited")

        assert any(report.payload["kind"] == "task_failed" for report in self._reports())

    def test_failed_task_on_settled_ticket_does_not_resurface(self) -> None:
        task = TaskFactory()
        task.fail(reason="ProcessError: worker exited")
        Ticket.objects.filter(pk=task.ticket_id).update(state=Ticket.State.MERGED)

        assert not [report for report in self._reports() if report.payload["kind"] == "task_failed"]

    def test_old_inbound_question_without_answer_is_an_incident(self) -> None:
        row = PendingChatInjection.record(channel="D1", slack_ts="100.1", text="Why did my task fail?")
        assert row is not None
        PendingChatInjection.objects.filter(pk=row.pk).update(received_at=timezone.now() - timedelta(hours=2))

        reports = self._reports()

        unanswered = [report for report in reports if report.payload["kind"] == "inbound_unanswered"]
        assert unanswered
        assert unanswered[0].payload["cause"] == "message_without_confirmed_response"
        assert "confirmed response" in unanswered[0].payload["suggested_action"]
        assert "Why did my task fail?" not in str(reports)

    def test_old_inbound_instruction_without_response_is_an_incident(self) -> None:
        row = PendingChatInjection.record(channel="D1", slack_ts="100.5", text="Please fix task 42")
        assert row is not None
        PendingChatInjection.objects.filter(pk=row.pk).update(received_at=timezone.now() - timedelta(hours=2))

        assert any(report.payload["kind"] == "inbound_unanswered" for report in self._reports())

    def test_inbound_scan_limits_rows_before_materializing(self) -> None:
        received_at = timezone.now() - timedelta(hours=2)
        PendingChatInjection.objects.bulk_create(
            PendingChatInjection(
                channel="D1", slack_ts=f"bulk-{index}", text=f"Why did task {index} fail?", received_at=received_at
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

    def test_ack_is_not_an_answer_but_substantive_reply_is(self) -> None:
        row = PendingChatInjection.record(channel="D1", slack_ts="100.2", text="What happened?")
        assert row is not None
        PendingChatInjection.objects.filter(pk=row.pk).update(received_at=timezone.now() - timedelta(hours=2))
        row.mark_loop_replied(PendingChatInjection.AnswerKind.ACK)
        assert any(report.payload["kind"] == "inbound_unanswered" for report in self._reports())

        PendingChatInjection.objects.filter(pk=row.pk).update(answer_kind=PendingChatInjection.AnswerKind.SIMPLE)
        row.observe_confirmed_loop_reply()
        assert not [report for report in self._reports() if report.payload["kind"] == "inbound_unanswered"]

    def test_claimed_simple_reply_without_delivery_is_still_unanswered(self) -> None:
        row = PendingChatInjection.record(channel="D1", slack_ts="100.3", text="What happened?")
        assert row is not None
        PendingChatInjection.objects.filter(pk=row.pk).update(received_at=timezone.now() - timedelta(hours=2))
        row.mark_loop_replied(PendingChatInjection.AnswerKind.SIMPLE)

        assert any(report.payload["kind"] == "inbound_unanswered" for report in self._reports())

    def test_verified_simple_reply_has_durable_receipt_and_is_not_unanswered(self) -> None:
        row = PendingChatInjection.record(channel="D1", slack_ts="100.4", text="What happened?")
        assert row is not None
        PendingChatInjection.objects.filter(pk=row.pk).update(received_at=timezone.now() - timedelta(hours=2))
        row.mark_loop_replied(PendingChatInjection.AnswerKind.SIMPLE)
        row.observe_confirmed_loop_reply()

        row.refresh_from_db()
        assert row.loop_response_confirmed_at is not None
        assert not [report for report in self._reports() if report.payload["kind"] == "inbound_unanswered"]

    def test_old_unposted_question_is_delivery_failure_and_posted_question_waits(self) -> None:
        unposted = DeferredQuestion.record("Which option?", session_id="s")
        posted = DeferredQuestion.record("Can you approve?", session_id="s", slack_ts="200.1", slack_channel="D1")
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

    def test_stale_claim_with_no_heartbeat_is_an_incident(self) -> None:
        task = TaskFactory(status=Task.Status.CLAIMED)
        Task.objects.filter(pk=task.pk).update(
            lease_expires_at=timezone.now() - timedelta(minutes=20),
            heartbeat_at=timezone.now() - timedelta(minutes=30),
        )

        assert any(report.payload["kind"] == "task_stalled" for report in self._reports())

    def test_critical_resource_budget_cannot_hide_an_unanswered_message(self) -> None:
        for index in range(3):
            row = PendingChatInjection.record(channel="D1", slack_ts=f"100.{index + 6}", text="What is happening?")
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
            task.fail(reason="ProcessError: worker exited")
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
            task.fail(reason="ProcessError: worker exited")
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

    def test_single_failed_task_ages_through_delivered_alert_to_one_ticket(self) -> None:
        task = TaskFactory()
        task.fail(reason="ProcessError: worker exited")
        alerts = []
        detector = LifecycleIncidentDetector(overlay_name="t3-teatree")
        delivery = DeliveryRoutes(
            overlay_name="t3-teatree", owner_alert=lambda report, _: alerts.append(report) or True
        )

        first = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery)
        assert [action.rung for action in first.actions] == [ActionRung.STATUSLINE]
        assert Ticket.objects.filter(extra__source="self_improve").count() == 0
        firing = first.actions[0].firing
        SelfImproveFiring.objects.filter(pk=firing.pk).update(first_fired_at=timezone.now() - timedelta(minutes=31))

        alerted = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery)
        assert [action.rung for action in alerted.actions] == [ActionRung.SLACK]
        assert len(alerts) == 1
        assert not run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery).actions

        SelfImproveFiring.objects.filter(pk=firing.pk).update(
            first_fired_at=timezone.now() - timedelta(hours=2, minutes=1)
        )
        ticketed = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery)
        assert [action.rung for action in ticketed.actions] == [ActionRung.TICKET]
        assert ticketed.actions[0].firing.ticket.overlay == "t3-teatree"
        assert Ticket.objects.filter(extra__source="self_improve").count() == 1
        assert not run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery).actions

    def test_single_unanswered_message_ages_and_does_not_ticket_before_alert(self) -> None:
        row = PendingChatInjection.record(channel="D1", slack_ts="111.1", text="Please respond", overlay="t3-teatree")
        assert row is not None
        PendingChatInjection.objects.filter(pk=row.pk).update(received_at=timezone.now() - timedelta(hours=2))
        detector = LifecycleIncidentDetector(overlay_name="t3-teatree")
        delivery = DeliveryRoutes(overlay_name="t3-teatree", owner_alert=lambda *_: False)

        first = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery)
        assert [action.rung for action in first.actions] == [ActionRung.STATUSLINE]
        firing = first.actions[0].firing
        SelfImproveFiring.objects.filter(pk=firing.pk).update(first_fired_at=timezone.now() - timedelta(hours=3))

        failed_delivery = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery)
        assert failed_delivery.actions == []
        assert Ticket.objects.filter(extra__source="self_improve").count() == 0
        delivered = run_tier(
            Tier.CHEAP,
            detectors=[detector],
            budget=BudgetVerdict.allow(),
            delivery=DeliveryRoutes(overlay_name="t3-teatree", owner_alert=lambda *_: True),
        )
        assert [action.rung for action in delivered.actions] == [ActionRung.SLACK]
        ticketed = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery)
        assert [action.rung for action in ticketed.actions] == [ActionRung.TICKET]

    def test_reopened_singleton_starts_new_age_window(self) -> None:
        task = TaskFactory()
        task.fail(reason="ProcessError: worker exited")
        detector = LifecycleIncidentDetector(overlay_name="t3-teatree")
        delivery = DeliveryRoutes(overlay_name="t3-teatree")
        first = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery)
        firing = first.actions[0].firing
        SelfImproveFiring.objects.filter(pk=firing.pk).update(first_fired_at=timezone.now() - timedelta(days=1))
        task.reopen()
        run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery)
        task.fail(reason="ProcessError: worker exited")

        reopened = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery)

        assert [action.rung for action in reopened.actions] == [ActionRung.STATUSLINE]
        assert timezone.now() - SelfImproveFiring.objects.get(pk=firing.pk).first_fired_at < timedelta(minutes=1)

    def test_lifecycle_detector_does_not_mix_overlay_owned_rows(self) -> None:
        own_task = TaskFactory(ticket=TicketFactory(overlay="t3-teatree"))
        foreign_task = TaskFactory(ticket=TicketFactory(overlay="foreign"))
        own_task.fail(reason="ProcessError: worker exited")
        foreign_task.fail(reason="ProcessError: worker exited")
        own_message = PendingChatInjection.record(
            channel="D1", slack_ts="222.1", text="Own question?", overlay="t3-teatree"
        )
        foreign_message = PendingChatInjection.record(
            channel="D2", slack_ts="222.2", text="Foreign question?", overlay="foreign"
        )
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
        task.fail(reason="ProcessError: worker exited")
        foreign_report = LifecycleIncidentDetector(overlay_name="foreign").detect()[0]
        foreign_firing = record_firing(foreign_report, action=ActionRung.STATUSLINE)

        own = run_tier(
            Tier.CHEAP,
            detectors=[LifecycleIncidentDetector(overlay_name="t3-teatree")],
            budget=BudgetVerdict.allow(),
        )

        assert own.reports == []
        assert SelfImproveFiring.objects.get(pk=foreign_firing.pk).resolved_at is None

    def test_singleton_waits_are_configurable_without_premature_alert(self) -> None:
        task = TaskFactory()
        task.fail(reason="ProcessError: worker exited")
        detector = LifecycleIncidentDetector(
            overlay_name="t3-teatree", owner_alert_after=timedelta(minutes=5), repair_after=timedelta(minutes=10)
        )
        delivery = DeliveryRoutes(overlay_name="t3-teatree", owner_alert=lambda *_: True)
        first = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery)
        firing = first.actions[0].firing
        SelfImproveFiring.objects.filter(pk=firing.pk).update(first_fired_at=timezone.now() - timedelta(minutes=4))

        before = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery)
        assert before.actions == []

        SelfImproveFiring.objects.filter(pk=firing.pk).update(first_fired_at=timezone.now() - timedelta(minutes=6))
        alerted = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery)
        assert [action.rung for action in alerted.actions] == [ActionRung.SLACK]

    def test_unattributed_outbound_alert_retries_deduplicates_and_recovers_without_ticket(self) -> None:
        question = DeferredQuestion.record("Which option?", session_id="unknown")
        DeferredQuestion.objects.filter(pk=question.pk).update(created_at=timezone.now() - timedelta(hours=2))
        delivered = False
        attempts: list[str] = []

        def owner_alert(report: DetectorReport, _existing: SelfImproveFiring | None) -> bool:
            attempts.append(report.dedup_key)
            return delivered

        detector = LifecycleIncidentDetector(overlay_name="t3-teatree")
        delivery = DeliveryRoutes(overlay_name="t3-teatree", owner_alert=owner_alert)

        def scan() -> TierResult:
            return run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery)

        result = scan()

        assert {report.payload["kind"] for report in result.reports} == {"outbound_unposted"}
        assert result.reports[0].payload["requires_delivery"] is True
        assert "overlay_name" not in result.reports[0].payload
        assert result.reports[0].max_rung == ActionRung.SLACK
        assert result.actions == []
        assert not SelfImproveFiring.objects.exists()
        delivered = True
        assert [action.rung for action in scan().actions] == [ActionRung.SLACK]
        assert scan().actions == []
        assert len(attempts) == 2

        question.mark_mirrored(channel="D1", slack_ts="200.2")
        recovered = scan()
        assert {report.payload["kind"] for report in recovered.reports} == {"outbound_unanswered"}
        assert SelfImproveFiring.objects.get(dedup_key=attempts[0]).resolved_at is not None
        assert len(attempts) == 2

        recurrence = DeferredQuestion.record("Which next option?", session_id="unknown")
        DeferredQuestion.objects.filter(pk=recurrence.pk).update(created_at=timezone.now() - timedelta(hours=2))
        assert [action.rung for action in scan().actions] == [ActionRung.SLACK]
        assert len(attempts) == 3
        assert not Ticket.objects.filter(extra__source="self_improve").exists()

    def test_explicit_detector_cannot_route_foreign_evidence_to_local_owner(self) -> None:
        task = TaskFactory(ticket=TicketFactory(overlay="foreign"))
        task.fail(reason="ProcessError: worker exited")

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
        repair_task.fail(reason="ProcessError: repair worker exited")
        detector = LifecycleIncidentDetector(overlay_name="t3-teatree")
        delivery = DeliveryRoutes(overlay_name="t3-teatree", owner_alert=lambda *_: True)

        first = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery)
        assert {report.payload["kind"] for report in first.reports} == {"repair_task_failed"}
        assert [action.rung for action in first.actions] == [ActionRung.STATUSLINE]
        SelfImproveFiring.objects.filter(pk=first.actions[0].firing.pk).update(
            first_fired_at=timezone.now() - timedelta(days=1)
        )

        again = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow(), delivery=delivery)

        assert [action.rung for action in again.actions] == [ActionRung.SLACK]
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
        assert [action.rung for action in alerted.actions] == [ActionRung.SLACK]
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
