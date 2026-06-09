"""The idle-reaper cadence singleton marker (#2190)."""

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import LocalStackReaperMarker


class TestLocalStackReaperMarker(TestCase):
    def test_load_is_singleton(self) -> None:
        a = LocalStackReaperMarker.load()
        b = LocalStackReaperMarker.load()
        assert a.pk == b.pk
        assert LocalStackReaperMarker.objects.count() == 1

    def test_stamp_run_records_now(self) -> None:
        marker = LocalStackReaperMarker.load()
        before = timezone.now()
        marker.stamp_run()
        marker.refresh_from_db()
        assert marker.last_run_at is not None
        assert marker.last_run_at >= before

    def test_str_is_informative(self) -> None:
        marker = LocalStackReaperMarker.load()
        assert "idle-stack-reaper" in str(marker)
