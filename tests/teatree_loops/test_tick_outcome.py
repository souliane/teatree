"""Periodic tick outcomes keep health rows aligned with observed work."""

import datetime as dt

from django.test import TestCase

from teatree.core.factory.health_signal import HealthSignal
from teatree.core.models.known_issue import KnownIssue
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.tick import TickReport
from teatree.loops.tick_outcome import periodic_tick_is_silent


class PeriodicTickOutcomeTests(TestCase):
    def setUp(self) -> None:
        KnownIssue.objects.record_reactive(
            HealthSignal(
                fingerprint="loop-silent:news",
                severity=KnownIssue.Severity.WARNING,
                kind="loop_silent",
                summary="previous silence",
            )
        )

    def test_queued_task_resolves_previous_silence(self) -> None:
        report = TickReport(
            started_at=dt.datetime.now(dt.UTC),
            signals=[ScanSignal(kind="scanning_news.queued", summary="queued", payload={"task_id": 9})],
        )

        assert periodic_tick_is_silent("news", jobs_built=True, report=report) is False
        assert KnownIssue.objects.get(fingerprint="loop-silent:news").resolved_at is not None

    def test_named_hold_resolves_previous_silence(self) -> None:
        report = TickReport(
            started_at=dt.datetime.now(dt.UTC),
            signals=[ScanSignal(kind="scanning_news.held", summary="held", payload={"reason": "machine HALT"})],
        )

        assert periodic_tick_is_silent("news", jobs_built=True, report=report) is False
        assert KnownIssue.objects.get(fingerprint="loop-silent:news").resolved_at is not None

    def test_scan_error_keeps_previous_silence_open(self) -> None:
        report = TickReport(started_at=dt.datetime.now(dt.UTC), errors={"scanning_news": "forge unavailable"})

        assert periodic_tick_is_silent("news", jobs_built=True, report=report) is False
        assert KnownIssue.objects.open().filter(fingerprint="loop-silent:news").exists()
