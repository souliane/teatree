"""``config_store_readable`` — can THIS process open the ``ConfigSetting`` store (#4357).

A health check that reads a setting through an unreadable tier gets the shipped default
and cannot tell it from the operator's stored row, so it emits a confident verdict on a
database it never opened. This probe is what lets such a check say UNVERIFIED instead.

Its side-effect-freedom is the other half: the doctor already FAILs on the degradation
record, so a probe that recorded itself would manufacture the fault it reports.
"""

from unittest import mock

from django.db.utils import OperationalError
from django.test import TestCase

from teatree.config.override_reader import config_store_readable
from teatree.core.models import ConfigSetting


class TestConfigStoreReadable(TestCase):
    def test_a_reachable_store_reads_as_readable(self) -> None:
        ConfigSetting.objects.set_value("contribute", value=True)
        assert config_store_readable() is True

    def test_an_empty_store_is_readable_not_absent(self) -> None:
        assert config_store_readable() is True

    def test_an_unopenable_store_reads_as_unreadable(self) -> None:
        with mock.patch.object(
            ConfigSetting.objects, "exists", side_effect=OperationalError("unable to open database file")
        ):
            assert config_store_readable() is False


class TestProbeIsSideEffectFree(TestCase):
    """The probe must never write the degradation record the doctor then reports."""

    def test_failed_probe_records_no_degradation(self) -> None:
        with (
            mock.patch("teatree.config.override_reader.record_degraded_read") as recorder,
            mock.patch.object(
                ConfigSetting.objects, "exists", side_effect=OperationalError("unable to open database file")
            ),
        ):
            assert config_store_readable() is False
        recorder.assert_not_called()
