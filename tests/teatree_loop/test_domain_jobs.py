from unittest.mock import patch

from django.test import TestCase

from teatree.core.models import ScannerFault
from teatree.loop.domain_jobs import _run_job
from teatree.loop.job_identity import _ScannerJob
from teatree.loop.overlay_followup_jobs import OverlayHookRefusalError
from teatree.loop.scanners.base import ScannerError, ScannerErrorClass, ScanSignal


class _SwitchingScanner:
    name = "fault_test"
    failing = True

    def scan(self) -> list[ScanSignal]:
        if self.failing:
            raise ScannerError(scanner=self.name, error_class=ScannerErrorClass.AUTH, detail="denied")
        return []


class ScannerFaultRecordingTests(TestCase):
    def test_failure_twice_records_one_fault_and_clean_run_heals(self) -> None:
        scanner = _SwitchingScanner()
        job = _ScannerJob(scanner=scanner, overlay="")
        with patch("teatree.loop.domain_jobs.notify_scanner_error"):
            _run_job(job)
            _run_job(job)
        assert ScannerFault.objects.count() == 1
        assert ScannerFault.objects.get(scanner=job.label).healed_at is None
        scanner.failing = False
        _run_job(job)
        assert ScannerFault.objects.get(scanner=job.label).healed_at is not None

    def test_healthy_unknown_scanner_writes_no_fault(self) -> None:
        scanner = _SwitchingScanner()
        scanner.failing = False
        _run_job(_ScannerJob(scanner=scanner, overlay=""))
        assert ScannerFault.objects.count() == 0

    def test_generic_failure_is_durable_and_database_failure_does_not_raise(self) -> None:
        class BrokenScanner:
            name = "broken"

            def scan(self) -> list[ScanSignal]:
                raise ValueError

        job = _ScannerJob(scanner=BrokenScanner(), overlay="")
        label, signals, error = _run_job(job)
        assert label == job.label
        assert signals == []
        assert error.startswith("ValueError:")
        assert ScannerFault.objects.get(scanner=job.label).error_class == "ValueError"
        with patch.object(ScannerFault.objects, "record_failure", side_effect=OSError):
            _, _, error = _run_job(job)
        assert error.startswith("ValueError:")

    def test_heal_database_failure_does_not_hide_healthy_scan(self) -> None:
        scanner = _SwitchingScanner()
        scanner.failing = False
        job = _ScannerJob(scanner=scanner, overlay="")
        with patch.object(ScannerFault.objects, "heal", side_effect=OSError) as heal:
            label, signals, error = _run_job(job)
        heal.assert_called_once_with(job.label)
        assert label == job.label
        assert signals == []
        assert error == ""

    def test_overlay_hook_refusal_records_fault_and_preserves_error(self) -> None:
        class RefusedScanner:
            name = "refused"

            def scan(self) -> list[ScanSignal]:
                reason = "invalid hook"
                raise OverlayHookRefusalError(reason)

        job = _ScannerJob(scanner=RefusedScanner(), overlay="example", overlay_hook=True)
        label, signals, error = _run_job(job)
        assert (label, signals, error) == (job.label, [], "invalid hook")
        fault = ScannerFault.objects.get(scanner=job.label)
        assert (fault.error_class, fault.detail) == ("OverlayHookRefusalError", "invalid hook")
