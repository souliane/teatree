"""An absent observation is recovery evidence only when its source was read."""

import datetime as dt
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.backend_protocols import PrOpenState
from teatree.core.models import MergeClear, SelfImproveFiring
from teatree.core.models.merge_clear import ClearRequest
from teatree.core.telemetry import admission as telemetry
from teatree.loop.loop_cadences import self_improve_cadence_seconds
from teatree.loop.scanners.resource_pressure import ResourceReading
from teatree.loop.self_improve.budget import BudgetVerdict
from teatree.loop.self_improve.detectors import pressure_incident as pressure_mod
from teatree.loop.self_improve.detectors import stale_statusline as statusline_mod
from teatree.loop.self_improve.detectors.base import ActionRung, DetectorReport
from teatree.loop.self_improve.detectors.forgotten_merge import ForgottenMergeDetector
from teatree.loop.self_improve.detectors.lifecycle_incident import LifecycleIncidentDetector
from teatree.loop.self_improve.detectors.pressure_incident import PressureIncidentDetector
from teatree.loop.self_improve.detectors.stale_statusline import StaleStatuslineEntryDetector
from teatree.loop.self_improve.detectors.telemetry_action_gap import TelemetryActionGapDetector
from teatree.loop.self_improve.persistence import record_firing
from teatree.loop.self_improve.schedule import Tier, run_tier
from tests.factories import TaskFactory


def _firing(detector: str, identity: str, *, now: dt.datetime | None = None) -> SelfImproveFiring:
    return record_firing(
        DetectorReport(
            detector=detector,
            dedup_key=f"{detector}::{identity}",
            state_hash="seed",
            severity="warn",
            max_rung=ActionRung.STATUSLINE,
            summary="seed",
        ),
        action=ActionRung.STATUSLINE,
        now=now,
    )


class IncidentConfidenceTests(TestCase):
    def test_missing_lifecycle_file_preserves_attempt_incident_but_resolves_db_incident(self) -> None:
        task = TaskFactory()
        task.fail(reason="ProcessError: worker exited")
        detector = LifecycleIncidentDetector()
        db_report = next(report for report in detector.detect() if report.payload["kind"] == "task_failed")
        db_firing = record_firing(db_report, action=ActionRung.STATUSLINE)
        otel_firing = _firing("lifecycle_incident", "attempt_failure_burst:harness_crash")
        task.reopen()

        with TemporaryDirectory() as directory:
            result = run_tier(
                Tier.CHEAP,
                detectors=[LifecycleIncidentDetector(directory=Path(directory))],
                budget=BudgetVerdict.allow(),
            )

        assert result.reports == []
        assert SelfImproveFiring.objects.get(pk=otel_firing.pk).resolved_at is None
        assert SelfImproveFiring.objects.get(pk=db_firing.pk).resolved_at is not None
        assert result.degraded_scans == [("lifecycle_incident", "otel_missing")]

    def test_missing_pressure_file_preserves_otel_incident(self) -> None:
        firing = _firing("pressure_incident", "swap")
        local_firing = _firing("pressure_incident", "ram-probe-inert")
        with (
            TemporaryDirectory() as directory,
            patch.object(pressure_mod, "cgroup_memory_probe_inert", return_value=False),
        ):
            detector = PressureIncidentDetector(
                directory=Path(directory),
                read_resources=lambda: ResourceReading(disk_free_gb=100.0, ram_avail_gb=20.0),
            )
            result = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow())

        assert SelfImproveFiring.objects.get(pk=firing.pk).resolved_at is None
        assert SelfImproveFiring.objects.get(pk=local_firing.pk).resolved_at is not None
        assert result.degraded_scans == [("pressure_incident", "otel_missing")]

    def test_empty_pressure_scan_without_explicit_recovery_keeps_prior_incident(self) -> None:
        firing = _firing("pressure_incident", "swap")
        with (
            TemporaryDirectory() as directory,
            patch.object(pressure_mod, "cgroup_memory_probe_inert", return_value=False),
        ):
            now = dt.datetime(2026, 9, 25, 6, tzinfo=dt.UTC)
            (Path(directory) / f"admission-{now.date().isoformat()}.jsonl").write_text("")
            detector = PressureIncidentDetector(
                directory=Path(directory),
                now=lambda: now,
                read_resources=lambda: ResourceReading(disk_free_gb=100.0, ram_avail_gb=20.0),
            )
            result = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow())

        assert SelfImproveFiring.objects.get(pk=firing.pk).resolved_at is None
        assert result.degraded_scans == []

    def test_same_cause_full_admission_explicitly_recovers_pressure_incident(self) -> None:
        now = dt.datetime(2026, 9, 25, 6, tzinfo=dt.UTC)
        recovered = _firing("pressure_incident", "swap", now=now - dt.timedelta(minutes=5))
        other = _firing("pressure_incident", "memory", now=now - dt.timedelta(minutes=5))
        with (
            TemporaryDirectory() as directory,
            patch.object(pressure_mod, "cgroup_memory_probe_inert", return_value=False),
        ):
            row = {
                "epoch": int(now.timestamp()),
                "cause": "swap",
                "pressure": 0.1,
                "band": "full",
                "admit": True,
                "reason": "unknown",
                "lane": "headless",
            }
            (Path(directory) / f"admission-{now.date().isoformat()}.jsonl").write_text(json.dumps(row) + "\n")
            detector = PressureIncidentDetector(
                directory=Path(directory),
                now=lambda: now,
                read_resources=lambda: ResourceReading(disk_free_gb=100.0, ram_avail_gb=20.0),
            )
            result = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow())

        assert SelfImproveFiring.objects.get(pk=recovered.pk).resolved_at is not None
        assert SelfImproveFiring.objects.get(pk=other.pk).resolved_at is None
        assert result.degraded_scans == []

    def test_full_pressure_row_in_incomplete_stream_cannot_recover_prior_incident(self) -> None:
        now = dt.datetime(2026, 9, 25, 6, tzinfo=dt.UTC)
        firing = _firing("pressure_incident", "swap", now=now - dt.timedelta(minutes=5))
        row = {
            "epoch": int(now.timestamp()),
            "cause": "swap",
            "pressure": 0.1,
            "band": "full",
            "admit": True,
            "reason": "unknown",
            "lane": "headless",
        }
        with (
            TemporaryDirectory() as directory,
            patch.object(pressure_mod, "cgroup_memory_probe_inert", return_value=False),
        ):
            (Path(directory) / f"admission-{now.date().isoformat()}.jsonl").write_text(
                json.dumps(row) + "\n{malformed\n"
            )
            detector = PressureIncidentDetector(
                directory=Path(directory),
                now=lambda: now,
                read_resources=lambda: ResourceReading(disk_free_gb=100.0, ram_avail_gb=20.0),
            )
            result = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow())

        assert SelfImproveFiring.objects.get(pk=firing.pk).resolved_at is None
        assert result.degraded_scans == [("pressure_incident", "otel_malformed")]

    def test_full_pressure_row_before_last_firing_cannot_recover_incident(self) -> None:
        now = dt.datetime(2026, 9, 25, 6, tzinfo=dt.UTC)
        firing = _firing("pressure_incident", "swap", now=now - dt.timedelta(minutes=1))
        row = {
            "epoch": int((now - dt.timedelta(minutes=2)).timestamp()),
            "cause": "swap",
            "pressure": 0.1,
            "band": "full",
            "admit": True,
            "reason": "unknown",
            "lane": "headless",
        }
        with (
            TemporaryDirectory() as directory,
            patch.object(pressure_mod, "cgroup_memory_probe_inert", return_value=False),
        ):
            (Path(directory) / f"admission-{now.date().isoformat()}.jsonl").write_text(json.dumps(row) + "\n")
            detector = PressureIncidentDetector(
                directory=Path(directory),
                now=lambda: now,
                read_resources=lambda: ResourceReading(disk_free_gb=100.0, ram_avail_gb=20.0),
            )
            run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow())

        assert SelfImproveFiring.objects.get(pk=firing.pk).resolved_at is None

    def test_later_denial_overrides_healthy_pressure_recovery(self) -> None:
        now = dt.datetime(2026, 9, 25, 6, tzinfo=dt.UTC)
        firing = _firing("pressure_incident", "swap", now=now - dt.timedelta(minutes=5))
        rows = [
            {
                "epoch": int((now - dt.timedelta(minutes=2)).timestamp()),
                "cause": "swap",
                "pressure": 0.1,
                "band": "full",
                "admit": True,
                "reason": "unknown",
                "lane": "headless",
            },
            {
                "epoch": int((now - dt.timedelta(minutes=1)).timestamp()),
                "cause": "swap",
                "pressure": 0.95,
                "band": "halt",
                "admit": False,
                "reason": "unknown",
                "lane": "headless",
            },
        ]
        with (
            TemporaryDirectory() as directory,
            patch.object(pressure_mod, "cgroup_memory_probe_inert", return_value=False),
        ):
            (Path(directory) / f"admission-{now.date().isoformat()}.jsonl").write_text(
                "".join(json.dumps(row) + "\n" for row in rows)
            )
            detector = PressureIncidentDetector(
                directory=Path(directory),
                now=lambda: now,
                read_resources=lambda: ResourceReading(disk_free_gb=100.0, ram_avail_gb=20.0),
            )
            run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow())

        assert SelfImproveFiring.objects.get(pk=firing.pk).resolved_at is None

    def test_empty_lifecycle_ledger_does_not_close_historical_attempt_burst(self) -> None:
        firing = _firing("lifecycle_incident", "attempt_failure_burst:harness_crash")
        with TemporaryDirectory() as directory:
            now = dt.datetime(2026, 9, 25, 6, tzinfo=dt.UTC)
            (Path(directory) / f"lifecycle-{now.date().isoformat()}.jsonl").write_text("")
            result = run_tier(
                Tier.CHEAP,
                detectors=[LifecycleIncidentDetector(directory=Path(directory), now=lambda: now)],
                budget=BudgetVerdict.allow(),
            )

        assert SelfImproveFiring.objects.get(pk=firing.pk).resolved_at is None
        assert result.degraded_scans == []

    def test_malformed_lifecycle_tail_still_acts_on_positive_attempt_evidence(self) -> None:
        now = dt.datetime(2026, 9, 25, 6, tzinfo=dt.UTC)
        rows = [
            {
                "epoch": int(now.timestamp()),
                "kind": "attempt.finished",
                "entity_id": task_id,
                "ticket_id": task_id,
                "task_id": task_id,
                "cause": "harness_crash",
            }
            for task_id in (1, 2, 3)
        ]
        with TemporaryDirectory() as directory:
            path = Path(directory) / f"lifecycle-{now.date().isoformat()}.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows) + "{bad\n")
            result = LifecycleIncidentDetector(directory=Path(directory), now=lambda: now).detect_checked()

        assert result.complete is False
        assert result.reason == "otel_malformed"
        assert [report.payload["kind"] for report in result.reports] == ["attempt_failure_burst"]
        assert result.reports[0].payload["recovery_semantics"] == "manual_ticket_review"

    def test_manual_attempt_burst_resolution_is_terminal_until_new_positive_evidence(self) -> None:
        now = dt.datetime(2026, 9, 24, 12, tzinfo=dt.UTC)
        firing = _firing("lifecycle_incident", "attempt_failure_burst:harness_crash", now=now - dt.timedelta(minutes=3))
        SelfImproveFiring.objects.filter(pk=firing.pk).update(resolved_at=now - dt.timedelta(minutes=1))

        def failure_rows(ids: tuple[int, ...], epoch: int) -> str:
            return "".join(
                json.dumps(
                    {
                        "epoch": epoch,
                        "kind": "attempt.finished",
                        "entity_id": task_id,
                        "ticket_id": task_id,
                        "task_id": task_id,
                        "cause": "harness_crash",
                    }
                )
                + "\n"
                for task_id in ids
            )

        with TemporaryDirectory() as directory:
            path = Path(directory) / f"lifecycle-{now.date().isoformat()}.jsonl"
            path.write_text(failure_rows((1, 2, 3), int((now - dt.timedelta(minutes=2)).timestamp())))
            old = run_tier(
                Tier.CHEAP,
                detectors=[LifecycleIncidentDetector(directory=Path(directory), now=lambda: now)],
                budget=BudgetVerdict.allow(),
            )
            assert old.reports == []
            assert SelfImproveFiring.objects.get(pk=firing.pk).resolved_at is not None

            with path.open("a") as stream:
                stream.write(failure_rows((4, 5, 6), int((now - dt.timedelta(seconds=30)).timestamp())))
            fresh = run_tier(
                Tier.CHEAP,
                detectors=[LifecycleIncidentDetector(directory=Path(directory), now=lambda: now)],
                budget=BudgetVerdict.allow(),
            )

        assert [report.payload["ids"] for report in fresh.reports] == [[4, 5, 6]]
        assert SelfImproveFiring.objects.get(pk=firing.pk).resolved_at is None

    def test_missing_factory_file_preserves_action_gap_incident(self) -> None:
        firing = _firing("telemetry_action_gap", "a" * 16)
        with TemporaryDirectory() as directory:
            result = run_tier(
                Tier.CHEAP,
                detectors=[TelemetryActionGapDetector(directory=Path(directory))],
                budget=BudgetVerdict.allow(),
            )

        assert SelfImproveFiring.objects.get(pk=firing.pk).resolved_at is None
        assert result.degraded_scans == [("telemetry_action_gap", "otel_missing")]

    def test_unrelated_factory_write_after_missing_file_cannot_resolve_old_gap(self) -> None:
        firing = _firing("telemetry_action_gap", "a" * 16)
        with TemporaryDirectory() as directory:
            detector = TelemetryActionGapDetector(directory=Path(directory))
            first = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow())
            assert first.degraded_scans == [("telemetry_action_gap", "otel_missing")]
            unrelated = DetectorReport(
                detector="lifecycle_incident",
                dedup_key="lifecycle_incident::other",
                state_hash="other",
                severity="warn",
                max_rung=ActionRung.LOG,
                summary="unrelated",
                payload={"kind": "task_failed", "cause": "other"},
            )
            with patch.object(telemetry, "_directory", return_value=Path(directory)):
                telemetry.record_factory_issue(unrelated)
            assert (Path(directory) / f"factory-{timezone.now().date().isoformat()}.jsonl").exists()
            second = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow())

        assert second.reports == []
        assert SelfImproveFiring.objects.get(pk=firing.pk).resolved_at is None

    def test_later_action_receipt_resolves_gap_without_ledger_continuity(self) -> None:
        source_key = "pressure_incident::swap"
        incident_id = hashlib.sha256(source_key.encode()).hexdigest()[:16]
        gap = record_firing(
            DetectorReport(
                detector="telemetry_action_gap",
                dedup_key=f"telemetry_action_gap::{incident_id}",
                state_hash="unactioned",
                severity="error",
                max_rung=ActionRung.TICKET,
                summary="unactioned",
                payload={"incident_id": incident_id},
            ),
            action=ActionRung.STATUSLINE,
        )
        record_firing(
            DetectorReport(
                detector="pressure_incident",
                dedup_key=source_key,
                state_hash="repaired",
                severity="warn",
                max_rung=ActionRung.STATUSLINE,
                summary="actioned",
            ),
            action=ActionRung.STATUSLINE,
            now=gap.first_fired_at + dt.timedelta(seconds=1),
        )
        with TemporaryDirectory() as directory:
            result = run_tier(
                Tier.CHEAP,
                detectors=[
                    TelemetryActionGapDetector(
                        directory=Path(directory), now=lambda: gap.first_fired_at + dt.timedelta(seconds=2)
                    )
                ],
                budget=BudgetVerdict.allow(),
            )

        assert result.degraded_scans == [("telemetry_action_gap", "otel_missing")]
        assert SelfImproveFiring.objects.get(pk=gap.pk).resolved_at is not None

    def test_action_receipt_beyond_first_active_gap_batch_is_not_starved(self) -> None:
        now = timezone.now()
        SelfImproveFiring.objects.bulk_create(
            [
                SelfImproveFiring(
                    detector="telemetry_action_gap",
                    dedup_key=f"telemetry_action_gap::{index:016x}",
                    state_hash="unactioned",
                    severity="error",
                    first_fired_at=now - dt.timedelta(hours=1),
                    last_fired_at=now - dt.timedelta(hours=1),
                )
                for index in range(100)
            ]
        )
        source_key = "pressure_incident::swap"
        incident_id = hashlib.sha256(source_key.encode()).hexdigest()[:16]
        gap = record_firing(
            DetectorReport(
                detector="telemetry_action_gap",
                dedup_key=f"telemetry_action_gap::{incident_id}",
                state_hash="unactioned",
                severity="error",
                max_rung=ActionRung.TICKET,
                summary="unactioned",
            ),
            action=ActionRung.STATUSLINE,
            now=now - dt.timedelta(minutes=30),
        )
        record_firing(
            DetectorReport(
                detector="pressure_incident",
                dedup_key=source_key,
                state_hash="actioned",
                severity="warn",
                max_rung=ActionRung.STATUSLINE,
                summary="actioned",
            ),
            action=ActionRung.STATUSLINE,
            now=now - dt.timedelta(minutes=1),
        )
        with TemporaryDirectory() as directory:
            for tick in range(3):
                run_tier(
                    Tier.CHEAP,
                    detectors=[
                        TelemetryActionGapDetector(
                            directory=Path(directory),
                            now=lambda tick=tick: now + dt.timedelta(seconds=tick * self_improve_cadence_seconds()),
                        )
                    ],
                    budget=BudgetVerdict.allow(),
                )
                if SelfImproveFiring.objects.get(pk=gap.pk).resolved_at is not None:
                    break

        assert SelfImproveFiring.objects.get(pk=gap.pk).resolved_at is not None

    def test_action_gap_recovery_progresses_with_skipped_ticks_and_new_gaps(self) -> None:
        cadence = self_improve_cadence_seconds()
        slot = int(timezone.now().timestamp()) // cadence
        now = dt.datetime.fromtimestamp((slot // 2 * 2) * cadence, tz=dt.UTC)
        SelfImproveFiring.objects.bulk_create(
            [
                SelfImproveFiring(
                    detector="telemetry_action_gap",
                    dedup_key=f"telemetry_action_gap::{index:016x}",
                    state_hash="unactioned",
                    severity="error",
                    first_fired_at=now - dt.timedelta(hours=1),
                    last_fired_at=now - dt.timedelta(hours=1),
                )
                for index in range(199)
            ]
        )
        source_key = "pressure_incident::swap"
        incident_id = hashlib.sha256(source_key.encode()).hexdigest()[:16]
        gap = _firing("telemetry_action_gap", incident_id, now=now - dt.timedelta(hours=1))
        SelfImproveFiring.objects.filter(pk=gap.pk).update(payload={"incident_id": incident_id})
        record_firing(
            DetectorReport(
                detector="pressure_incident",
                dedup_key=source_key,
                state_hash="actioned",
                severity="warn",
                max_rung=ActionRung.STATUSLINE,
                summary="actioned",
            ),
            action=ActionRung.STATUSLINE,
            now=now - dt.timedelta(minutes=1),
        )
        with TemporaryDirectory() as directory:
            run_tier(
                Tier.CHEAP,
                detectors=[TelemetryActionGapDetector(directory=Path(directory), now=lambda: now)],
                budget=BudgetVerdict.allow(),
            )
            assert SelfImproveFiring.objects.get(pk=gap.pk).resolved_at is None
            run_tier(
                Tier.CHEAP,
                detectors=[
                    TelemetryActionGapDetector(
                        directory=Path(directory), now=lambda: now + dt.timedelta(seconds=2 * cadence)
                    )
                ],
                budget=BudgetVerdict.allow(),
            )
            assert SelfImproveFiring.objects.get(pk=gap.pk).resolved_at is not None
            SelfImproveFiring.objects.bulk_create(
                [
                    SelfImproveFiring(
                        detector="telemetry_action_gap",
                        dedup_key=f"telemetry_action_gap::{index:016x}",
                        state_hash="unactioned",
                        severity="error",
                        first_fired_at=now - dt.timedelta(minutes=30),
                        last_fired_at=now - dt.timedelta(minutes=30),
                    )
                    for index in range(300, 310)
                ]
            )
            run_tier(
                Tier.CHEAP,
                detectors=[
                    TelemetryActionGapDetector(
                        directory=Path(directory), now=lambda: now + dt.timedelta(seconds=4 * cadence)
                    )
                ],
                budget=BudgetVerdict.allow(),
            )

        final = SelfImproveFiring.objects.get(pk=gap.pk)
        assert final.resolved_at is not None
        assert final.payload["incident_id"] == incident_id

    def test_unknown_forge_state_preserves_only_that_merge_incident(self) -> None:
        for pr_id in (501, 502):
            clear = MergeClear.issue(
                ClearRequest(
                    pr_id=pr_id,
                    slug="souliane/teatree",
                    reviewed_sha="deadbeef0123456789abcdef0123456789abcdef",
                    reviewer_identity="reviewer@example.com",
                    gh_verify_result="green",
                    blast_class="logic",
                )
            )
            MergeClear.objects.filter(pk=clear.pk).update(issued_at=timezone.now() - dt.timedelta(hours=1))
        unknown = _firing("forgotten_merge", "souliane/teatree#501")
        merged = _firing("forgotten_merge", "souliane/teatree#502")
        detector = ForgottenMergeDetector(
            read_state=lambda url: PrOpenState.UNKNOWN if url.endswith("/501") else PrOpenState.MERGED
        )

        result = run_tier(Tier.CHEAP, detectors=[detector], budget=BudgetVerdict.allow())

        assert SelfImproveFiring.objects.get(pk=unknown.pk).resolved_at is None
        assert SelfImproveFiring.objects.get(pk=merged.pk).resolved_at is not None
        assert result.degraded_scans == [("forgotten_merge", "forge_unknown")]

    def test_unreadable_statusline_preserves_stale_entry_incident(self) -> None:
        firing = _firing("stale_statusline_entry", "render")
        with TemporaryDirectory() as directory:
            missing = Path(directory) / "statusline"
            with patch.object(statusline_mod, "default_path", return_value=missing):
                result = run_tier(
                    Tier.CHEAP,
                    detectors=[StaleStatuslineEntryDetector()],
                    budget=BudgetVerdict.allow(),
                )
            assert SelfImproveFiring.objects.get(pk=firing.pk).resolved_at is None
            assert result.degraded_scans == [("stale_statusline_entry", "statusline_unreadable")]

            missing.write_text("")
            with patch.object(statusline_mod, "default_path", return_value=missing):
                run_tier(Tier.CHEAP, detectors=[StaleStatuslineEntryDetector()], budget=BudgetVerdict.allow())

        assert SelfImproveFiring.objects.get(pk=firing.pk).resolved_at is not None
