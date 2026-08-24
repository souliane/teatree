"""``_check_starved_intake_candidates`` — the doctor's intake-starvation surface (#4238).

Three issues sat admissible and unadmitted for a full day while every health surface
read green, because a passed-over candidate left no trace anywhere. This check reads
the ledger the intake scanner syncs and names each issue with how long it has waited.
"""

import datetime as dt
import io
from contextlib import redirect_stdout

from django.test import TestCase
from django.utils import timezone

from teatree.cli.doctor.checks_admission_pressure import _check_starved_intake_candidates
from teatree.core.models import STARVED_AFTER, UnclaimedIntakeCandidate, WaitingCandidate

STARVED_URL = "https://github.com/souliane/teatree/issues/4188"


def _sync(*, waited: dt.timedelta, title: str = "", open_for: dt.timedelta | None = None) -> None:
    now = timezone.now()
    UnclaimedIntakeCandidate.objects.sync(
        "acme",
        [
            WaitingCandidate(
                issue_url=STARVED_URL,
                title=title,
                issue_created_at=now - open_for if open_for else None,
            ),
        ],
        now=now - waited,
    )


class StarvedIntakeDoctorCheckTests(TestCase):
    def test_an_empty_ledger_is_ok(self) -> None:
        assert _check_starved_intake_candidates() is True

    def test_a_candidate_inside_the_threshold_is_ok(self) -> None:
        _sync(waited=STARVED_AFTER / 2)

        assert _check_starved_intake_candidates() is True

    def test_a_candidate_past_the_threshold_warns(self) -> None:
        _sync(waited=STARVED_AFTER * 2)

        assert _check_starved_intake_candidates() is False

    def test_the_warning_names_the_issue_and_how_long_it_has_waited(self) -> None:
        _sync(waited=dt.timedelta(days=2), title="intake has no age ordering", open_for=dt.timedelta(days=5))

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            _check_starved_intake_candidates()
        output = buffer.getvalue()

        assert STARVED_URL in output
        assert "intake has no age ordering" in output
        assert "unclaimed for 2d" in output
        assert "open 5d" in output
