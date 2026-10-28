import io
from contextlib import redirect_stdout

from django.test import TestCase

from teatree.cli.doctor.checks_admission_pressure import _check_merge_brake
from teatree.core.admission.pr_start_cap import OPEN_PR_START_CAP
from teatree.core.models import PullRequest, Ticket


class OpenPrStartCapDoctorTests(TestCase):
    def test_doctor_names_the_same_live_pull_requests_that_hold_intake(self) -> None:
        urls = [
            f"https://github.com/example-org/example-repo/pull/{index + 1000}" for index in range(OPEN_PR_START_CAP)
        ]
        for url in urls:
            PullRequest.objects.record_opened(ticket=Ticket.objects.create(), url=url)
        output = io.StringIO()
        with redirect_stdout(output):
            assert _check_merge_brake() is True
        assert "open pull requests fill the start cap" in output.getvalue()
        assert all(url in output.getvalue() for url in urls)

    def test_doctor_has_no_cap_warning_below_the_cap(self) -> None:
        PullRequest.objects.record_opened(
            ticket=Ticket.objects.create(), url="https://github.com/example-org/example-repo/pull/1100"
        )
        output = io.StringIO()
        with redirect_stdout(output):
            assert _check_merge_brake() is True
        assert "start cap" not in output.getvalue()
