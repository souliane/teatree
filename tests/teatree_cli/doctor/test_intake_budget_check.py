"""``_check_intake_budget_deadlock`` — the `t3 doctor` intake-deadlock FAIL (#3978).

Two stalled claims at a budget of two stop the factory admitting any work at all, and
every signal an operator has reads normal: the loop is enabled, its last-run stamp
advances, and a full budget makes the scanner factory return ``None`` so the tick
reports success. This is the check that reddens instead. Unlike the WARN-only
``_check_marker_jam`` (which needs a grace to expire first), its verdict IS returned for
the doctor's pass/fail aggregation.
"""

from datetime import timedelta
from unittest.mock import patch

import django.test
from django.utils import timezone

from teatree.cli.doctor.checks_admission_pressure import _check_intake_budget_deadlock
from teatree.config import UserSettings
from teatree.core.models import ImplementedIssueMarker, PullRequest, Task, Ticket
from tests.factories import ImplementedIssueMarkerFactory, PullRequestFactory, TaskFactory, TicketFactory

_SETTINGS_TARGET = "teatree.config.get_effective_settings"


def _settings(*, enabled: bool = True, limit: int = 2) -> UserSettings:
    return UserSettings(issue_implementer_enabled=enabled, issue_implementer_max_concurrent=limit)


def _held(url: str, *, state: str = Ticket.State.NOT_STARTED) -> ImplementedIssueMarker:
    """One aged, occupied slot for overlay ``acme``."""
    ticket = TicketFactory(overlay="acme", issue_url=url, state=state)
    marker = ImplementedIssueMarkerFactory(overlay="acme", issue_url=url, ticket_created=True, ticket=ticket)
    ImplementedIssueMarker.objects.filter(pk=marker.pk).update(dispatched_at=timezone.now() - timedelta(hours=6))
    return ImplementedIssueMarker.objects.get(pk=marker.pk)


class TestIntakeBudgetDoctorCheck(django.test.TestCase):
    def test_no_markers_pass(self) -> None:
        assert _check_intake_budget_deadlock() is True

    def test_budget_with_room_passes(self) -> None:
        _held("https://github.com/o/r/issues/1")
        with patch(_SETTINGS_TARGET, return_value=_settings(limit=2)):
            assert _check_intake_budget_deadlock() is True

    def test_full_budget_with_live_work_passes(self) -> None:
        marker = _held("https://github.com/o/r/issues/2", state=Ticket.State.STARTED)
        TaskFactory(ticket=marker.ticket, status=Task.Status.CLAIMED)
        with patch(_SETTINGS_TARGET, return_value=_settings(limit=1)):
            assert _check_intake_budget_deadlock() is True

    def test_full_budget_with_open_prs_passes(self) -> None:
        marker = _held("https://github.com/o/r/issues/3", state=Ticket.State.SHIPPED)
        PullRequestFactory(ticket=marker.ticket, overlay="acme", state=PullRequest.State.OPEN)
        with patch(_SETTINGS_TARGET, return_value=_settings(limit=1)):
            assert _check_intake_budget_deadlock() is True

    def test_two_stalled_claims_at_a_budget_of_two_fail(self) -> None:
        # The reported deadlock, verbatim: both slots held, nothing progressing.
        merged = _held("https://github.com/o/r/issues/4", state=Ticket.State.SHIPPED)
        PullRequestFactory(ticket=merged.ticket, overlay="acme", state=PullRequest.State.MERGED)
        dead = _held("https://github.com/o/r/issues/5")
        TaskFactory(ticket=dead.ticket, status=Task.Status.FAILED)

        with patch(_SETTINGS_TARGET, return_value=_settings(limit=2)):
            assert _check_intake_budget_deadlock() is False

    def test_the_failure_names_the_overlay_and_the_holders(self) -> None:
        url = "https://github.com/o/r/issues/6"
        _held(url)
        with (
            patch(_SETTINGS_TARGET, return_value=_settings(limit=1)),
            patch("typer.echo") as echo,
        ):
            _check_intake_budget_deadlock()
        reported = "\n".join(str(call.args[0]) for call in echo.call_args_list)
        assert "FAIL" in reported
        assert "acme" in reported
        assert url in reported

    def test_disabled_intake_is_not_a_deadlock(self) -> None:
        _held("https://github.com/o/r/issues/7")
        with patch(_SETTINGS_TARGET, return_value=_settings(enabled=False, limit=1)):
            assert _check_intake_budget_deadlock() is True

    def test_a_crashed_read_never_reddens_the_run(self) -> None:
        _held("https://github.com/o/r/issues/8")
        with patch(_SETTINGS_TARGET, side_effect=RuntimeError("boom")):
            assert _check_intake_budget_deadlock() is True
