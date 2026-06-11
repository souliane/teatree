"""``_check_dream_staleness`` — the `t3 doctor` dream-staleness alarm (#1933).

The dream consolidation cron needs a staleness alarm so memories never pile up
unpromoted unnoticed: the doctor surfaces a WARN when the last *successful*
dream run is older than the 48h threshold (or has never succeeded). A fresh
successful run clears it. Mirrors the SelfUpdateMarker/MiniLoopMarker-style
marker-staleness alarms.
"""

import datetime as dt
import io
from contextlib import redirect_stdout
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.cli._doctor_checks import _check_dream_staleness
from teatree.core.models import DreamRunMarker


class DreamStalenessDoctorCheckTestCase(TestCase):
    def test_bootstrap_never_succeeded_warns(self) -> None:
        ok = _check_dream_staleness()
        assert ok is False

    def test_recent_success_is_ok(self) -> None:
        DreamRunMarker.objects.mark_succeeded(timezone.now())
        assert _check_dream_staleness() is True

    def test_stale_success_warns(self) -> None:
        DreamRunMarker.objects.mark_succeeded(timezone.now() - dt.timedelta(hours=49))
        assert _check_dream_staleness() is False

    def test_fresh_run_clears_the_alarm(self) -> None:
        DreamRunMarker.objects.mark_succeeded(timezone.now() - dt.timedelta(hours=49))
        assert _check_dream_staleness() is False
        DreamRunMarker.objects.mark_succeeded(timezone.now())
        assert _check_dream_staleness() is True

    def test_failing_attempts_stay_stale(self) -> None:
        # Attempts that keep failing bump last_attempted_at but not
        # last_succeeded_at — the alarm keeps firing.
        DreamRunMarker.objects.mark_succeeded(timezone.now() - dt.timedelta(hours=49))
        DreamRunMarker.objects.mark_attempted(timezone.now())
        assert _check_dream_staleness() is False


class DreamStalenessOutputTestCase(TestCase):
    def test_warn_message_names_dream(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            _check_dream_staleness()
        out = buf.getvalue()
        assert "WARN" in out
        assert "dream" in out.lower()


class DreamStalenessCrashTestCase(TestCase):
    def test_is_stale_crash_degrades_to_ok_with_warn(self) -> None:
        # A DB read error (offline / unmigrated self-DB) must never abort the
        # doctor run — it degrades to True with a WARN naming the crash.
        buf = io.StringIO()
        with (
            patch.object(
                DreamRunMarker.objects,
                "is_stale",
                side_effect=RuntimeError("db offline"),
            ),
            redirect_stdout(buf),
        ):
            result = _check_dream_staleness()
        assert result is True
        out = buf.getvalue()
        assert "WARN" in out
        assert "RuntimeError" in out
