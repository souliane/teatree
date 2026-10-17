from django.test import TestCase
from django.utils import timezone

from teatree.core.factory.factory_signals import SignalStatus, merge_latency


class MergeLatencyProviderTests(TestCase):
    def test_no_merge_sample_is_insufficient(self) -> None:
        reading = merge_latency(now=timezone.now())

        assert reading.status == SignalStatus.INSUFFICIENT_DATA
