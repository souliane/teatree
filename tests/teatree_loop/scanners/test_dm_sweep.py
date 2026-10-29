"""The tick-level owner-DM sweep — watermark, live thread read, and silence (#3658)."""

from datetime import timedelta
from unittest import mock

from django.db import OperationalError
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import DeferredQuestion, Loop
from teatree.loop.domain_jobs import _run_job
from teatree.loop.job_identity import _ScannerJob
from teatree.loop.scanners.dm_sweep import DmSweepScanner
from tests._owner_channel import OWNER_DECISION

CHANNEL = "D0OWNER"
TS = "1779990001.000001"


def _thread(text: str = "Ship it?", *, age: timedelta = timedelta(minutes=5)) -> DeferredQuestion:
    row = DeferredQuestion.record(text, slack_channel=CHANNEL, slack_ts=TS, **OWNER_DECISION)
    DeferredQuestion.objects.filter(pk=row.pk).update(created_at=timezone.now() - age)
    row.refresh_from_db()
    return row


class TestSilenceWhenNothingToDo(TestCase):
    def test_an_empty_queue_emits_no_signal(self) -> None:
        assert DmSweepScanner().scan() == []

    def test_a_pass_that_resolves_nothing_emits_no_signal(self) -> None:
        _thread()
        assert DmSweepScanner().scan() == []


class TestScanIsResilient(TestCase):
    def test_unmigrated_tables_are_skipped_not_crashed(self) -> None:
        with mock.patch("teatree.core.owner_dm_sweep.run_sweep", side_effect=OperationalError("no such table")):
            assert DmSweepScanner().scan() == []

    def test_an_unexpected_sweep_failure_reaches_the_tick_error_surface(self) -> None:
        with mock.patch("teatree.core.owner_dm_sweep.run_sweep", side_effect=RuntimeError("boom")):
            _, signals, error = _run_job(_ScannerJob(scanner=DmSweepScanner(), overlay=""))
        assert (signals, error) == ([], "RuntimeError: boom")


class TestWatermark(TestCase):
    def test_the_pass_reads_the_loops_own_last_run_ledger(self) -> None:
        # The migration seeds this row, so pin its watermark rather than re-creating it.
        Loop.objects.update_or_create(
            name="dm_sweep",
            defaults={
                "delay_seconds": 3600,
                "script": "src/teatree/loops/dm_sweep/loop.py",
                "last_run_at": timezone.now() - timedelta(minutes=30),
            },
        )
        _thread(age=timedelta(days=5))
        # Old thread, opened long before the watermark → out of this pass entirely.
        assert DmSweepScanner().scan() == []
