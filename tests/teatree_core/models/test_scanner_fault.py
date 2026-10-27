import datetime as dt

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import ScannerFault


class ScannerFaultTests(TestCase):
    def test_failure_upserts_and_clean_run_heals(self) -> None:
        ScannerFault.objects.record_failure("scan[overlay]", "auth", "x" * 300)
        first = ScannerFault.objects.get(scanner="scan[overlay]")
        assert first.error_class == "auth"
        assert len(first.detail) == 200
        ScannerFault.objects.record_failure("scan[overlay]", "network", "offline")
        assert ScannerFault.objects.count() == 1
        assert ScannerFault.objects.get(scanner="scan[overlay]").error_class == "network"
        ScannerFault.objects.heal("scan[overlay]")
        assert ScannerFault.objects.get(scanner="scan[overlay]").healed_at is not None

    def test_only_recent_open_failures_are_incidents(self) -> None:
        ScannerFault.objects.record_failure("recent", "auth", "unauthorized")
        ScannerFault.objects.record_failure("healed", "network", "offline")
        ScannerFault.objects.heal("healed")
        ScannerFault.objects.record_failure("old", "network", "offline")
        ScannerFault.objects.filter(scanner="old").update(last_failed_at=timezone.now() - dt.timedelta(days=2))
        assert list(
            ScannerFault.objects.recent_open(timezone.now() - dt.timedelta(days=1)).values_list("scanner", flat=True)
        ) == ["recent"]
