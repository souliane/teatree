"""OTel issue spans are reconciled against actual action receipts."""

import hashlib
import importlib
import json
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest
from django.apps import apps
from django.db import connection
from django.db.utils import OperationalError
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from teatree.core.models.self_improve_firing import SelfImproveFiring
from teatree.loop.self_improve.detectors.base import ActionRung, DetectorReport
from teatree.loop.self_improve.detectors.telemetry_action_gap import TelemetryActionGapDetector
from teatree.loop.self_improve.persistence import record_firing


class TelemetryActionGapTests(TestCase):
    def test_reconciliation_queries_only_active_gaps_and_incident_digests(self) -> None:
        now = timezone.now()
        original_key = "pressure_incident::swap"
        incident_id = hashlib.sha256(original_key.encode()).hexdigest()[:16]
        for index in range(100):
            record_firing(
                DetectorReport(
                    detector="historical",
                    dedup_key=f"historical::{index}",
                    state_hash="seen",
                    severity="warn",
                    max_rung=ActionRung.LOG,
                    summary="unrelated",
                ),
                action=ActionRung.LOG,
            )
        row = {
            "epoch": int((now - timedelta(minutes=3)).timestamp()),
            "kind": "pressure_incident",
            "cause": "swap",
            "severity": "critical",
            "count": 1,
            "incident_id": incident_id,
        }
        with TemporaryDirectory() as directory:
            (Path(directory) / f"factory-{now.date().isoformat()}.jsonl").write_text(json.dumps(row) + "\n")
            detector = TelemetryActionGapDetector(directory=Path(directory), now=lambda: now)
            with CaptureQueriesContext(connection) as queries:
                reports = detector.detect()

        assert len(reports) == 1
        firing_reads = [query["sql"] for query in queries if "teatree_self_improve_firing" in query["sql"]]
        assert len(firing_reads) == 2
        assert "telemetry_action_gap" in firing_reads[0]
        assert "resolved_at" in firing_reads[0]
        assert "LIMIT 100" in firing_reads[0]
        assert "dedup_key_digest" in firing_reads[1]
        assert "WHERE" in firing_reads[1]
        assert incident_id in firing_reads[1]

    def test_firing_receipt_stores_the_issue_identity_digest(self) -> None:
        key = "pressure_incident::swap"
        firing = record_firing(
            DetectorReport(
                detector="pressure_incident",
                dedup_key=key,
                state_hash="seen",
                severity="warn",
                max_rung=ActionRung.LOG,
                summary="swap",
            ),
            action=ActionRung.LOG,
        )
        expected = hashlib.sha256(key.encode()).hexdigest()[:16]
        assert SelfImproveFiring.objects.get(pk=firing.pk).dedup_key_digest == expected

    def test_data_migration_backfills_existing_firing_digests(self) -> None:
        key = "pressure_incident::before-migration"
        firing = SelfImproveFiring.objects.create(
            detector="pressure_incident",
            dedup_key=key,
            state_hash="seen",
            severity="warn",
            dedup_key_digest="",
        )
        migration = importlib.import_module("teatree.core.migrations.0113_self_improve_firing_digest")
        migration.backfill_firing_digests(apps, connection.schema_editor())
        firing.refresh_from_db()
        assert firing.dedup_key_digest == hashlib.sha256(key.encode()).hexdigest()[:16]

    def test_schema_not_yet_migrated_skips_only_the_missing_digest_column(self) -> None:
        now = timezone.now()
        row = {
            "epoch": int((now - timedelta(minutes=3)).timestamp()),
            "kind": "pressure_incident",
            "cause": "swap",
            "severity": "critical",
            "count": 1,
            "incident_id": "a" * 16,
        }
        with TemporaryDirectory() as directory:
            (Path(directory) / f"factory-{now.date().isoformat()}.jsonl").write_text(json.dumps(row) + "\n")
            detector = TelemetryActionGapDetector(directory=Path(directory), now=lambda: now)
            missing_column = OperationalError("no such column: dedup_key_digest")
            original_filter = SelfImproveFiring.objects.filter

            def missing_digest_only(*args: object, **kwargs: object) -> object:
                if "dedup_key_digest__in" in kwargs:
                    raise missing_column
                return original_filter(*args, **kwargs)

            with patch.object(SelfImproveFiring.objects, "filter", side_effect=missing_digest_only):
                assert detector.detect() == []
            with (
                patch.object(SelfImproveFiring.objects, "filter", side_effect=OperationalError("database is locked")),
                pytest.raises(OperationalError),
            ):
                detector.detect()

    def test_unactioned_factory_issue_becomes_one_fix_ticket_report(self) -> None:
        now = timezone.now()
        original_key = "pressure_incident::swap"
        incident_id = hashlib.sha256(original_key.encode()).hexdigest()[:16]
        row = {
            "epoch": int((now - timedelta(minutes=3)).timestamp()),
            "kind": "pressure_incident",
            "cause": "swap",
            "severity": "critical",
            "count": 5,
            "incident_id": incident_id,
        }
        with TemporaryDirectory() as directory:
            path = Path(directory) / f"factory-{now.date().isoformat()}.jsonl"
            path.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n")
            detector = TelemetryActionGapDetector(directory=Path(directory), now=lambda: now)

            reports = detector.detect()
            assert len(reports) == 1
            assert reports[0].payload["kind"] == "telemetry_action_gap"
            assert reports[0].requested_rung == ActionRung.TICKET
            assert reports[0].payload["count"] == 2

            record_firing(
                DetectorReport(
                    detector="pressure_incident",
                    dedup_key=original_key,
                    state_hash="seen",
                    severity="warn",
                    max_rung=ActionRung.STATUSLINE,
                    summary="swap pressure",
                ),
                action=ActionRung.STATUSLINE,
            )
            assert detector.detect() == []

    def test_telemetry_gap_reports_do_not_recurse(self) -> None:
        now = timezone.now()
        row = {
            "epoch": int((now - timedelta(minutes=3)).timestamp()),
            "kind": "telemetry_action_gap",
            "cause": "unactioned-issue",
            "severity": "error",
            "count": 1,
            "incident_id": "a" * 16,
        }
        with TemporaryDirectory() as directory:
            path = Path(directory) / f"factory-{now.date().isoformat()}.jsonl"
            path.write_text(json.dumps(row) + "\n")
            assert TelemetryActionGapDetector(directory=Path(directory), now=lambda: now).detect() == []

    def test_resolved_action_covers_old_spans_but_not_new_spans(self) -> None:
        now = timezone.now()
        original_key = "pressure_incident::swap"
        incident_id = hashlib.sha256(original_key.encode()).hexdigest()[:16]
        old = {
            "epoch": int((now - timedelta(minutes=8)).timestamp()),
            "kind": "pressure_incident",
            "cause": "swap",
            "severity": "critical",
            "count": 1,
            "incident_id": incident_id,
        }
        new = old | {"epoch": int((now - timedelta(minutes=3)).timestamp())}
        firing = record_firing(
            DetectorReport(
                detector="pressure_incident",
                dedup_key=original_key,
                state_hash="seen",
                severity="warn",
                max_rung=ActionRung.STATUSLINE,
                summary="swap pressure",
            ),
            action=ActionRung.STATUSLINE,
            now=now - timedelta(minutes=7),
        )
        firing.resolved_at = now - timedelta(minutes=5)
        firing.save(update_fields=["resolved_at"])
        with TemporaryDirectory() as directory:
            path = Path(directory) / f"factory-{now.date().isoformat()}.jsonl"
            path.write_text(json.dumps(old) + "\n")
            detector = TelemetryActionGapDetector(directory=Path(directory), now=lambda: now)
            assert detector.detect() == []

            path.write_text(json.dumps(old) + "\n" + json.dumps(new) + "\n")
            reports = detector.detect()
            assert len(reports) == 1
            assert reports[0].payload["count"] == 1
