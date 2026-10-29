from unittest.mock import patch

import httpx
import pytest
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from teatree.backends.github import api
from teatree.core.models import ScannerFault
from teatree.loop.domain_jobs import _run_job
from teatree.loop.job_identity import _ScannerJob
from teatree.loop.overlay_followup_jobs import OverlayHookRefusalError
from teatree.loop.scanners.base import ScannerError, ScannerErrorClass, ScanSignal
from teatree.loop.scanners.issue_intake import IssueIntakeScanner
from teatree.utils.run import CommandFailedError
from tests.teatree_loop.scanners.test_issue_intake_fails_loud import _BrokenHost


class _SwitchingScanner:
    name = "fault_test"
    failing = True

    def scan(self) -> list[ScanSignal]:
        if self.failing:
            raise ScannerError(scanner=self.name, error_class=ScannerErrorClass.AUTH, detail="denied")
        return []


class ScannerFaultRecordingTests(TestCase):
    def test_rate_limited_intake_fault_keeps_cause_after_truncation(self) -> None:
        response = httpx.Response(
            403,
            json={"message": "You have exceeded a secondary rate limit " + "x" * 236},
            headers={"X-RateLimit-Resource": "search", "Retry-After": "45"},
            request=httpx.Request("GET", "https://api.github.com/x"),
        )
        with patch.object(httpx.Client, "request", return_value=response), pytest.raises(CommandFailedError) as raised:
            api._run_gh("gh", "api", "x", token="test-token")
        host = _BrokenHost(error=raised.value)
        scanner = IssueIntakeScanner(
            host=host,
            admit_label="t3-auto",
            overlay_name="teatree",
            trusted_authors=("owner",),
            identities=("owner",),
            readback_enabled=False,
        )
        job = _ScannerJob(scanner=scanner, overlay="")

        with patch("teatree.loop.domain_jobs.notify_scanner_error"):
            _run_job(job)

        fault = ScannerFault.objects.get(scanner=job.label)
        assert fault.error_class == "rate_limit"
        assert len(fault.detail) <= 200
        assert "rate limit" in fault.detail
        assert "resource=search" in fault.detail
        assert "retry-after=45" in fault.detail

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

    def test_healthy_scanner_with_nothing_open_issues_no_write(self) -> None:
        scanner = _SwitchingScanner()
        scanner.failing = False
        with CaptureQueriesContext(connection) as queries:
            _run_job(_ScannerJob(scanner=scanner, overlay=""))
        assert ScannerFault.objects.count() == 0
        assert [q["sql"] for q in queries if q["sql"].lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE"))] == []

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
