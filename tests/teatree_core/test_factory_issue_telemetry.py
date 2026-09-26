"""Factory incidents are OTel spans with a bounded, private local reader."""

import datetime as dt
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from teatree.core.models import DeferredQuestion, PendingChatInjection, Task
from teatree.core.telemetry.admission import (
    PressureSpanExporter,
    recent_factory_observations,
    recent_lifecycle_observations,
    record_factory_issue,
)
from teatree.loop.self_improve.budget import BudgetVerdict
from teatree.loop.self_improve.detectors.base import DetectorReport
from teatree.loop.self_improve.detectors.lifecycle_incident import LifecycleIncidentDetector
from teatree.loop.self_improve.detectors.telemetry_action_gap import TelemetryActionGapDetector
from teatree.loop.self_improve.schedule import Tier, run_tier
from teatree.loop.slack_answer.cycle import run_slack_answer_cycle
from tests.factories import TaskAttemptFactory, TaskFactory
from tests.teatree_loop.slack_answer.test_cycle import RecordingBackend


class FactoryIssueTelemetryTests(TestCase):
    def test_successful_boot_info_is_readable_but_not_an_unactioned_issue(self) -> None:
        now = timezone.now()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / f"factory-{now.date().isoformat()}.jsonl").write_text(
                json.dumps(
                    {
                        "epoch": int(now.timestamp()),
                        "kind": "boot",
                        "cause": "ready",
                        "severity": "info",
                        "count": 1,
                        "incident_id": "a" * 16,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            rows = recent_factory_observations(directory=root, now=now)
            assert rows[0]["severity"] == "info"
            assert TelemetryActionGapDetector(directory=root, now=lambda: now + dt.timedelta(minutes=3)).detect() == []

    def test_failed_slack_reply_does_not_emit_an_answer_span(self) -> None:
        with TemporaryDirectory() as directory:
            provider = TracerProvider()
            provider.add_span_processor(SimpleSpanProcessor(PressureSpanExporter(directory=Path(directory))))
            with (
                patch("teatree.core.telemetry.admission._provider", return_value=provider),
                patch(
                    "teatree.loop.slack_answer.simple_answer.statusline_for_slack",
                    return_value="overlay=acme\nticket=#1\n",
                ),
                self.captureOnCommitCallbacks(execute=True),
            ):
                inbound = PendingChatInjection.record(channel="C1", slack_ts="100.9", text="what's the status?")
                assert inbound is not None
                backend = RecordingBackend(post_reply_raises=True)
                run_slack_answer_cycle(messaging_resolver=lambda _overlay: backend)

            rows = recent_lifecycle_observations(directory=Path(directory), now=timezone.now())
            assert not [row for row in rows if row["kind"] == "message.answered" and row["entity_id"] == inbound.pk]
            provider.shutdown()

    def test_verified_slack_reply_emits_an_answer_span(self) -> None:
        with TemporaryDirectory() as directory:
            provider = TracerProvider()
            provider.add_span_processor(SimpleSpanProcessor(PressureSpanExporter(directory=Path(directory))))
            with (
                patch("teatree.core.telemetry.admission._provider", return_value=provider),
                patch(
                    "teatree.loop.slack_answer.simple_answer.statusline_for_slack",
                    return_value="overlay=acme\nticket=#1\n",
                ),
                self.captureOnCommitCallbacks(execute=True),
            ):
                inbound = PendingChatInjection.record(channel="C1", slack_ts="101.0", text="what's the status?")
                assert inbound is not None
                backend = RecordingBackend()
                run_slack_answer_cycle(messaging_resolver=lambda _overlay: backend)

            rows = recent_lifecycle_observations(directory=Path(directory), now=timezone.now())
            assert any(row["kind"] == "message.answered" and row["entity_id"] == inbound.pk for row in rows)
            provider.shutdown()

    def test_untrusted_issue_fields_never_reach_otlp_span(self) -> None:
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        report = DetectorReport(
            detector="test",
            dedup_key="test-key",
            state_hash="state",
            severity="private severity",
            max_rung="log",
            summary="",
            payload={"kind": "private kind", "cause": "private cause", "count": True},
        )

        with patch("teatree.core.telemetry.admission._provider", return_value=provider):
            record_factory_issue(report)

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert "private" not in str(spans[0].attributes)
        provider.shutdown()

    def test_reader_rejects_forged_rows_with_extra_or_invalid_fields(self) -> None:
        now = timezone.now()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / f"factory-{now.date().isoformat()}.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "epoch": int(now.timestamp()),
                        "kind": "task_failed",
                        "cause": "harness_crash",
                        "severity": "error",
                        "count": 1,
                        "incident_id": "a" * 16,
                        "private": "do not return this",
                    }
                )
                + "\n"
            )
            assert recent_factory_observations(directory=root, now=now) == []

    def test_real_detector_emits_safe_issue_span_and_deduplicated_firing(self) -> None:
        now = timezone.now()
        row = PendingChatInjection.record(channel="D1", slack_ts="100.1", text="Why was my secret task ignored?")
        assert row is not None
        PendingChatInjection.objects.filter(pk=row.pk).update(received_at=now - dt.timedelta(hours=2))
        with TemporaryDirectory() as directory:
            provider = TracerProvider()
            provider.add_span_processor(SimpleSpanProcessor(PressureSpanExporter(directory=Path(directory))))
            with patch("teatree.core.telemetry.admission._provider", return_value=provider):
                run_tier(Tier.CHEAP, budget=BudgetVerdict.allow(), detectors=[LifecycleIncidentDetector()])
                run_tier(Tier.CHEAP, budget=BudgetVerdict.allow(), detectors=[LifecycleIncidentDetector()])

            rows = recent_factory_observations(directory=Path(directory), now=timezone.now())
            assert rows
            assert all(set(item) == {"epoch", "kind", "cause", "severity", "count", "incident_id"} for item in rows)
            assert rows[0]["kind"] == "inbound_unanswered"
            assert "secret task" not in str(rows)
            assert rows[0]["incident_id"] == rows[1]["incident_id"]
            provider.shutdown()

    def test_task_and_message_transitions_emit_correlated_private_spans(self) -> None:
        with TemporaryDirectory() as directory:
            provider = TracerProvider()
            provider.add_span_processor(SimpleSpanProcessor(PressureSpanExporter(directory=Path(directory))))
            with (
                patch("teatree.core.telemetry.admission._provider", return_value=provider),
                self.captureOnCommitCallbacks(execute=True),
            ):
                task = TaskFactory()
                task.fail(reason="ProcessError: private worker detail")
                inbound = PendingChatInjection.record(channel="D1", slack_ts="100.4", text="My private question?")
                assert inbound is not None
                PendingChatInjection.agent_answered_question(inbound.slack_ts)
                outbound = DeferredQuestion.record("My private outbound question?", session_id="s")
                outbound.mark_mirrored(channel="D1", slack_ts="200.4")
                DeferredQuestion.consume(outbound.pk, answer="private answer")

            rows = recent_lifecycle_observations(directory=Path(directory), now=timezone.now())
            kinds = {row["kind"] for row in rows}
            assert {
                "task.failed",
                "message.received",
                "message.answered",
                "question.recorded",
                "question.mirrored",
                "question.answered",
            } <= kinds
            assert any(row["entity_id"] == task.pk and row["ticket_id"] == task.ticket_id for row in rows)
            assert "private" not in str(rows)
            provider.shutdown()

    def test_failed_attempt_records_its_outcome_without_error_text(self) -> None:
        with TemporaryDirectory() as directory:
            provider = TracerProvider()
            provider.add_span_processor(SimpleSpanProcessor(PressureSpanExporter(directory=Path(directory))))
            with (
                patch("teatree.core.telemetry.admission._provider", return_value=provider),
                self.captureOnCommitCallbacks(execute=True),
            ):
                task = TaskFactory()
                attempt = TaskAttemptFactory(task=task, exit_code=1, error="ProcessError: private details")

            rows = recent_lifecycle_observations(directory=Path(directory), now=timezone.now())
            assert any(
                row["kind"] == "attempt.finished"
                and row["entity_id"] == attempt.pk
                and row["ticket_id"] == task.ticket_id
                and row["task_id"] == task.pk
                and row["cause"] == "harness_crash"
                for row in rows
            )
            assert "private details" not in str(rows)
            provider.shutdown()

    def test_operator_cancelled_attempts_do_not_become_failure_burst(self) -> None:
        with TemporaryDirectory() as directory:
            provider = TracerProvider()
            provider.add_span_processor(SimpleSpanProcessor(PressureSpanExporter(directory=Path(directory))))
            with (
                patch("teatree.core.telemetry.admission._provider", return_value=provider),
                self.captureOnCommitCallbacks(execute=True),
            ):
                for _ in range(3):
                    task = TaskFactory()
                    TaskAttemptFactory(task=task, exit_code=1, error="cancelled: operator requested")

            rows = recent_lifecycle_observations(directory=Path(directory), now=timezone.now())
            assert len([row for row in rows if row["kind"] == "attempt.finished" and row["cause"] == "cancelled"]) == 3
            reports = LifecycleIncidentDetector(directory=Path(directory)).detect()
            assert not [report for report in reports if report.payload["kind"] == "attempt_failure_burst"]
            provider.shutdown()

    def test_production_claim_next_pending_emits_claim_span(self) -> None:
        with TemporaryDirectory() as directory:
            provider = TracerProvider()
            provider.add_span_processor(SimpleSpanProcessor(PressureSpanExporter(directory=Path(directory))))
            task = TaskFactory()
            with (
                patch("teatree.core.telemetry.admission._provider", return_value=provider),
                self.captureOnCommitCallbacks(execute=True),
            ):
                claimed = Task.objects.claim_next_pending(claimed_by="worker-1")

            assert claimed == task
            rows = recent_lifecycle_observations(directory=Path(directory), now=timezone.now())
            assert any(
                row["kind"] == "task.claimed" and row["task_id"] == task.pk and row["ticket_id"] == task.ticket_id
                for row in rows
            )
            provider.shutdown()
