"""Intake claims nothing new while a started ticket sits idle past the configured window (#5187).

Every case drives the real entry point: the factory builds the scanner from the effective
settings and ``scan()`` is what claims or declines. Each exclusion case is paired with the
seven-hour hold, so the suite cannot pass by the hold never firing.
"""

import itertools
from datetime import datetime, timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.modelkit.task_failure_taxonomy import FailureKind
from teatree.core.models import (
    ImplementedIssueMarker,
    PullRequest,
    Session,
    Task,
    TaskAttempt,
    Ticket,
    UnclaimedIntakeCandidate,
)
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.loop.intake_idle_hold import idle_hold_reason
from teatree.loop.scanner_factories import _issue_intake_scanner_for
from teatree.loop.scanners.issue_intake import IssueIntakeScanner
from teatree.loop.stuck_ticket_redispatch import STUCK_HALT_MARKER
from tests.factories import PullRequestFactory
from tests.teatree_loop._fleet_claim_stub import FleetClaimStub
from tests.teatree_loop.test_issue_intake_wiring import _PATCH_TARGET, _authored_host, _backend_with_host, _enabled

ISSUE = "https://github.com/souliane/teatree/issues/100"
_HOLDER_NUMBERS = itertools.count(900)


def _holder(
    *,
    idle_hours: float,
    overlay: str = "acme",
    role: str = Ticket.Role.AUTHOR,
    state: str = Ticket.State.WORK_STARTED,
    now: datetime | None = None,
) -> Ticket:
    """A started ticket whose only recorded activity is one finished attempt *idle_hours* old."""
    ticket = Ticket.objects.create(
        role=role,
        state=state,
        overlay=overlay,
        issue_url=f"https://github.com/souliane/teatree/issues/{next(_HOLDER_NUMBERS)}",
    )
    session = Session.objects.create(ticket=ticket, agent_id="planning")
    task = Task.objects.create(ticket=ticket, session=session, phase="planning", status=Task.Status.COMPLETED)
    attempt = TaskAttempt.objects.create(task=task, ended_at=timezone.now(), exit_code=0)
    started_at = (now or timezone.now()) - timedelta(hours=idle_hours)
    TaskAttempt.objects.filter(pk=attempt.pk).update(started_at=started_at)
    return ticket


class _IdleHoldCase(TestCase):
    def setUp(self) -> None:
        for patcher in (
            patch("teatree.core.review.author_trust.repo_is_internal", return_value=False),
            patch.object(IssueIntakeScanner, "_governor_denied", return_value=False),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        FleetClaimStub().install(self)

    def _claimed(self, *, window: int = 6) -> list[str]:
        host = _authored_host(ISSUE)
        with patch(_PATCH_TARGET, return_value=_enabled(issue_intake_idle_hold_hours=window)):
            scanner = _issue_intake_scanner_for(_backend_with_host(host))
        assert isinstance(scanner, IssueIntakeScanner)
        return [str(signal.payload["url"]) for signal in scanner.scan()]

    def _assert_held(self, holder: Ticket, *, window: int = 6) -> None:
        with self.assertLogs("teatree.loop.scanner_factories", level="WARNING") as logs:
            claimed = self._claimed(window=window)
        assert claimed == []
        assert not ImplementedIssueMarker.objects.exists()
        assert list(UnclaimedIntakeCandidate.objects.values_list("issue_url", flat=True)) == [ISSUE]
        assert f"ticket {holder.pk}" in "\n".join(logs.output)


class AnIdleStartedTicketHoldsIntakeTests(_IdleHoldCase):
    def test_a_seven_hour_idle_ticket_holds_a_six_hour_window_and_is_named(self) -> None:
        self._assert_held(_holder(idle_hours=7))

    def test_a_holder_in_another_overlay_still_holds(self) -> None:
        self._assert_held(_holder(idle_hours=7, overlay="elsewhere"))

    def test_a_merged_pull_request_does_not_excuse_an_idle_ticket(self) -> None:
        holder = _holder(idle_hours=7)
        PullRequestFactory(ticket=holder, overlay="acme", state=PullRequest.State.MERGED)
        self._assert_held(holder)

    def test_an_answered_question_no_longer_excuses_an_idle_ticket(self) -> None:
        holder = _holder(idle_hours=7)
        question = DeferredQuestion.record("Which way?", dedupe_marker=STUCK_HALT_MARKER.format(pk=holder.pk))
        DeferredQuestion.objects.filter(pk=question.pk).update(answered_at=timezone.now())
        self._assert_held(holder)

    def test_a_holder_that_gains_active_work_releases_intake_on_the_next_scan(self) -> None:
        holder = _holder(idle_hours=7)
        assert self._claimed() == []

        Task.objects.create(
            ticket=holder,
            session=Session.objects.create(ticket=holder, agent_id="coding"),
            phase="coding",
            status=Task.Status.PENDING,
        )

        assert self._claimed() == [ISSUE]


class ManyLivePullRequestsAllowIntakeTests(_IdleHoldCase):
    def test_twenty_live_pull_requests_allow_claiming(self) -> None:
        for _ in range(20):
            PullRequestFactory(ticket=Ticket.objects.create(role=Ticket.Role.AUTHOR))
        assert self._claimed(window=0) == [ISSUE]

    def test_twentieth_pull_request_after_scanner_build_still_allows_claiming(self) -> None:
        for _ in range(19):
            PullRequestFactory(ticket=Ticket.objects.create(role=Ticket.Role.AUTHOR))
        host = _authored_host(ISSUE)
        with patch(_PATCH_TARGET, return_value=_enabled(issue_intake_idle_hold_hours=0)):
            scanner = _issue_intake_scanner_for(_backend_with_host(host))
        assert isinstance(scanner, IssueIntakeScanner)
        assert scanner.can_claim is True
        PullRequestFactory(ticket=Ticket.objects.create(role=Ticket.Role.AUTHOR))
        assert [str(signal.payload["url"]) for signal in scanner.scan()] == [ISSUE]


class NoHoldTests(_IdleHoldCase):
    def test_an_hour_of_idleness_leaves_claiming_unchanged(self) -> None:
        _holder(idle_hours=1)
        assert self._claimed() == [ISSUE]

    def test_an_unset_window_never_holds(self) -> None:
        _holder(idle_hours=7)
        assert self._claimed(window=0) == [ISSUE]

    def test_a_negative_window_is_off_rather_than_holding_everything(self) -> None:
        _holder(idle_hours=1)
        assert self._claimed(window=-1) == [ISSUE]

    def test_a_ticket_with_an_active_task_does_not_hold(self) -> None:
        holder = _holder(idle_hours=7)
        Task.objects.create(
            ticket=holder,
            session=Session.objects.create(ticket=holder, agent_id="coding"),
            phase="coding",
            status=Task.Status.CLAIMED,
        )
        assert self._claimed() == [ISSUE]

    def test_a_ticket_with_a_live_pull_request_does_not_hold(self) -> None:
        holder = _holder(idle_hours=7)
        PullRequestFactory(ticket=holder, overlay="acme", state=PullRequest.State.OPEN)
        assert self._claimed() == [ISSUE]

    def test_a_ticket_with_a_pending_halt_question_does_not_hold(self) -> None:
        holder = _holder(idle_hours=7)
        DeferredQuestion.record("Which way?", dedupe_marker=STUCK_HALT_MARKER.format(pk=holder.pk))
        assert self._claimed() == [ISSUE]

    def test_a_ticket_with_a_pending_question_parked_on_its_task_does_not_hold(self) -> None:
        holder = _holder(idle_hours=7)
        DeferredQuestion.record("Which way?", parked_task=holder.tasks.get())
        assert self._claimed() == [ISSUE]

    def test_a_ticket_whose_newest_task_was_cancelled_does_not_hold(self) -> None:
        holder = _holder(idle_hours=7)
        Task.objects.create(
            ticket=holder,
            session=Session.objects.create(ticket=holder, agent_id="coding"),
            phase="coding",
            status=Task.Status.FAILED,
            failure_kind=FailureKind.CANCELLED,
        )
        assert self._claimed() == [ISSUE]

    def test_a_ticket_whose_implied_phase_was_cancelled_does_not_hold(self) -> None:
        holder = _holder(idle_hours=7)
        Task.objects.filter(ticket=holder, phase="planning").update(failure_kind=FailureKind.CANCELLED)
        Task.objects.create(
            ticket=holder,
            session=Session.objects.create(ticket=holder, agent_id="coding"),
            phase="coding",
            status=Task.Status.COMPLETED,
        )
        assert self._claimed() == [ISSUE]

    def test_a_merge_safe_review_after_plan_does_not_hold(self) -> None:
        _holder(idle_hours=7, state=Ticket.State.PLAN_RECORDED)
        with patch("teatree.loop.stuck_ticket_redispatch._merge_safe_review_after_plan", return_value=True):
            assert self._claimed() == [ISSUE]

    def test_a_reviewer_ticket_does_not_hold(self) -> None:
        _holder(idle_hours=7, role=Ticket.Role.REVIEWER, state=Ticket.State.NOT_STARTED)
        assert self._claimed() == [ISSUE]

    def test_a_ticket_that_has_not_started_does_not_hold(self) -> None:
        _holder(idle_hours=7, state=Ticket.State.NOT_STARTED)
        assert self._claimed() == [ISSUE]

    def test_a_ticket_with_no_recorded_activity_cannot_be_aged_and_does_not_hold(self) -> None:
        Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED, overlay="acme")
        assert self._claimed() == [ISSUE]


class TheWindowBoundaryTests(TestCase):
    NOW = timezone.now().replace(microsecond=0)

    def test_a_ticket_idle_for_exactly_the_window_holds(self) -> None:
        holder = _holder(idle_hours=6, now=self.NOW)
        assert f"ticket {holder.pk}" in (idle_hold_reason(6, now=self.NOW) or "")

    def test_a_ticket_one_second_short_of_the_window_does_not(self) -> None:
        _holder(idle_hours=6, now=self.NOW + timedelta(seconds=1))
        assert idle_hold_reason(6, now=self.NOW) is None
