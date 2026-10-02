"""Failure accounting reads the ``TaskAttempt.outcome`` discriminator, not ``exit_code``.

``outcome`` exists precisely because ``exit_code`` is ambiguous: an envelope
REFUSAL is stamped ``exit_code=0`` with a non-empty error, a SIGKILL is ``-9``,
and an in-flight attempt has none at all. Three surfaces still counted
``exit_code > 0``, so every refusal and every killed attempt reported zero
blockers: the standup blocker counts, ``checking``'s "needs you" group, and
``_checking_gather``'s failure list.

Separately, S5's failure FRACTION kept in-flight attempts in its denominator,
which held a window whose every TERMINAL attempt crashed below the hard-red
threshold.
"""

from datetime import timedelta
from typing import cast

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.checking import gather_checking_report
from teatree.core.factory.factory_signal_queries import S5Evidence, compute_s5, current_window
from teatree.core.models import Session, Task, TaskAttempt, Ticket
from teatree.core.standup import generate_standup

_OVERLAY = "t3-teatree"


def _attempt(ticket: Ticket, *, exit_code: int | None, error: str = "") -> TaskAttempt:
    task = Task.objects.create(
        ticket=ticket,
        session=Session.objects.create(ticket=ticket, agent_id="loop"),
        phase="coding",
        status=Task.Status.COMPLETED,
    )
    return TaskAttempt.objects.create(
        task=task,
        exit_code=exit_code,
        error=error,
        ended_at=timezone.now(),
    )


class TestFailureCountsSeeRefusalsAndKills(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create(overlay=_OVERLAY, issue_url="https://example.com/issues/1")

    def test_standup_counts_a_refusal_and_a_signal_kill_as_blockers(self) -> None:
        _attempt(self.ticket, exit_code=0, error="refused: cannot verify")
        _attempt(self.ticket, exit_code=-9, error="killed")

        report = generate_standup(
            since=timezone.now() - timedelta(days=1),
            overlay_name=_OVERLAY,
            commit_collector=lambda _ticket: [],
        )

        assert report.blockers, "a refusal and a SIGKILL are failures, not clean runs"
        assert report.blockers[0].failure_count == 2

    def test_standup_reports_no_blocker_for_a_clean_run(self) -> None:
        _attempt(self.ticket, exit_code=0)

        report = generate_standup(
            since=timezone.now() - timedelta(days=1),
            overlay_name=_OVERLAY,
            commit_collector=lambda _ticket: [],
        )

        assert report.blockers == []

    def test_checking_needs_you_lists_a_refusal(self) -> None:
        _attempt(self.ticket, exit_code=0, error="refused: cannot verify")
        now = timezone.now()

        report = gather_checking_report(
            since=now - timedelta(days=1),
            now=now + timedelta(seconds=1),
            overlay_name=_OVERLAY,
        )

        assert report.needs_you.total == 1

    def test_checking_needs_you_ignores_a_clean_run(self) -> None:
        _attempt(self.ticket, exit_code=0)
        now = timezone.now()

        report = gather_checking_report(
            since=now - timedelta(days=1),
            now=now + timedelta(seconds=1),
            overlay_name=_OVERLAY,
        )

        assert report.needs_you.total == 0


class TestS5FailureFractionUsesTerminalAttemptsOnly(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create(overlay=_OVERLAY, issue_url="https://example.com/issues/2")

    def test_in_flight_attempts_are_out_of_the_denominator(self) -> None:
        for _ in range(5):
            _attempt(self.ticket, exit_code=1, error="crashed")
        for _ in range(6):
            _attempt(self.ticket, exit_code=None)

        now = timezone.now()
        computation = compute_s5(current_window(now + timedelta(seconds=1), 7), _OVERLAY, now)

        # `Computation.evidence` is the union across all five signals; this one came from compute_s5.
        evidence = cast("S5Evidence", computation.evidence)

        assert evidence["attempts"] == 5
        assert evidence["in_flight"] == 6
        assert evidence["failed_fraction"] == pytest.approx(1.0)
        assert computation.hard_red is True

    def test_a_healthy_window_stays_green(self) -> None:
        for _ in range(6):
            _attempt(self.ticket, exit_code=0)
        _attempt(self.ticket, exit_code=1, error="crashed")

        now = timezone.now()
        computation = compute_s5(current_window(now + timedelta(seconds=1), 7), _OVERLAY, now)

        assert computation.hard_red is False
