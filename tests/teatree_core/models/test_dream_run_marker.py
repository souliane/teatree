"""Dream run-marker staleness tests (#1933).

The single-name marker stamps attempted/succeeded timestamps and answers
``is_stale`` for the loop's staleness alarm. Staleness keys on the *success*
timestamp: a run that keeps failing bumps only ``last_attempted_at`` and the
engine stays stale. Bootstrap (no row, or a row that never succeeded) is
stale by construction.
"""

import datetime as dt

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import DreamRunMarker


class TestMarkTransitions(TestCase):
    def test_mark_succeeded_stamps_both_timestamps(self) -> None:
        ts = timezone.now()

        DreamRunMarker.objects.mark_succeeded(ts)

        marker = DreamRunMarker.objects.get(name=DreamRunMarker.NAME)
        assert marker.last_succeeded_at == ts
        assert marker.last_attempted_at == ts

    def test_mark_attempted_leaves_success_untouched(self) -> None:
        success_ts = timezone.now()
        DreamRunMarker.objects.mark_succeeded(success_ts)
        later_attempt = success_ts + dt.timedelta(hours=1)

        DreamRunMarker.objects.mark_attempted(later_attempt)

        marker = DreamRunMarker.objects.get(name=DreamRunMarker.NAME)
        assert marker.last_attempted_at == later_attempt
        assert marker.last_succeeded_at == success_ts

    def test_marks_upsert_a_single_row(self) -> None:
        DreamRunMarker.objects.mark_attempted(timezone.now())
        DreamRunMarker.objects.mark_succeeded(timezone.now())
        DreamRunMarker.objects.mark_attempted(timezone.now())

        assert DreamRunMarker.objects.count() == 1


class TestIsStale(TestCase):
    def test_bootstrap_with_no_row_is_stale(self) -> None:
        assert DreamRunMarker.objects.is_stale(timezone.now()) is True

    def test_attempted_but_never_succeeded_is_stale(self) -> None:
        DreamRunMarker.objects.mark_attempted(timezone.now())

        assert DreamRunMarker.objects.is_stale(timezone.now()) is True

    def test_fresh_success_is_not_stale(self) -> None:
        now = timezone.now()
        DreamRunMarker.objects.mark_succeeded(now)

        assert DreamRunMarker.objects.is_stale(now + dt.timedelta(hours=47)) is False

    def test_just_under_threshold_is_not_stale(self) -> None:
        now = timezone.now()
        DreamRunMarker.objects.mark_succeeded(now)

        almost = now + dt.timedelta(hours=48) - dt.timedelta(seconds=1)
        assert DreamRunMarker.objects.is_stale(almost) is False

    def test_at_threshold_boundary_is_stale(self) -> None:
        now = timezone.now()
        DreamRunMarker.objects.mark_succeeded(now)

        assert DreamRunMarker.objects.is_stale(now + dt.timedelta(hours=48)) is True

    def test_custom_threshold_hours(self) -> None:
        now = timezone.now()
        DreamRunMarker.objects.mark_succeeded(now)

        assert DreamRunMarker.objects.is_stale(now + dt.timedelta(hours=2), threshold_hours=1) is True
        assert DreamRunMarker.objects.is_stale(now + dt.timedelta(minutes=30), threshold_hours=1) is False


class TestStr(TestCase):
    def test_renders_succeeded_timestamp_branch(self) -> None:
        ts = timezone.now()
        DreamRunMarker.objects.mark_succeeded(ts)
        marker = DreamRunMarker.objects.get(name=DreamRunMarker.NAME)

        rendered = str(marker)

        assert rendered == f"dream-run<dream:succeeded={ts.isoformat()}>"

    def test_renders_never_branch_when_success_is_null(self) -> None:
        DreamRunMarker.objects.mark_attempted(timezone.now())
        marker = DreamRunMarker.objects.get(name=DreamRunMarker.NAME)

        assert marker.last_succeeded_at is None
        assert str(marker) == "dream-run<dream:succeeded=never>"
