"""The checkout stall signal is sourced from durable tasks and pull requests."""

from datetime import timedelta
from typing import cast
from unittest.mock import patch

from django.db import OperationalError
from django.test import TestCase
from django.utils import timezone

from teatree.core.factory import operational_health
from teatree.core.factory.health_signal import HealthSignal
from teatree.core.models import Task, TaskAttempt, Ticket
from tests.factories import PullRequestFactory, TaskFactory, TicketFactory


def _parked_ticket() -> tuple[Task, Ticket]:
    ticket = cast("Ticket", TicketFactory(short_description="Retry parked checkout tasks"))
    task = cast("Task", TaskFactory(ticket=ticket, phase="coding"))
    TaskAttempt.objects.create(task=task, ended_at=timezone.now(), exit_code=0, error="checkout_parked: held")
    return task, ticket


def _stall_signals() -> tuple[HealthSignal, ...]:
    return tuple(
        signal for signal in operational_health.collect_signals().signals if signal.kind == "stalled_checkout_pr"
    )


class TestStalledCheckoutPullRequest(TestCase):
    def test_old_open_pull_request_is_linked_in_health(self) -> None:
        _, ticket = _parked_ticket()
        pr = PullRequestFactory(ticket=ticket, create_verified_at=timezone.now() - timedelta(hours=25))

        report = operational_health.reconcile_health()
        issues = [issue for issue in report.open_issues if issue.kind == "stalled_checkout_pr"]
        assert len(issues) == 1
        assert issues[0].evidence_url == pr.url
        assert f"ticket #{ticket.pk} (Retry parked checkout tasks)" in issues[0].summary

    def test_future_checkout_park_raises_stall(self) -> None:
        task, ticket = _parked_ticket()
        PullRequestFactory(ticket=ticket, create_verified_at=timezone.now() - timedelta(hours=25))
        Task.objects.filter(pk=task.pk).update(not_before=timezone.now() + timedelta(minutes=30))
        assert len(_stall_signals()) == 1

    def test_fresh_due_task_without_checkout_park_does_not_raise_stall(self) -> None:
        ticket = cast("Ticket", TicketFactory())
        TaskFactory(ticket=ticket, phase="coding")
        PullRequestFactory(ticket=ticket, create_verified_at=timezone.now() - timedelta(hours=25))
        assert _stall_signals() == ()

    def test_recent_or_settled_pull_request_does_not_raise_stall(self) -> None:
        _, ticket = _parked_ticket()
        pr = PullRequestFactory(ticket=ticket, create_verified_at=timezone.now() - timedelta(hours=23))
        assert _stall_signals() == ()
        pr.create_verified_at = timezone.now() - timedelta(hours=25)
        pr.state = pr.State.MERGED
        pr.save(update_fields=["create_verified_at", "state"])
        assert _stall_signals() == ()

    def test_legacy_undated_pull_requests_are_deduplicated_per_ticket(self) -> None:
        _, ticket = _parked_ticket()
        legacy = PullRequestFactory(ticket=ticket, create_verified_at=None)
        PullRequestFactory(ticket=ticket, create_verified_at=timezone.now() - timedelta(hours=25))
        signals = _stall_signals()
        assert len(signals) == 1
        assert signals[0].evidence_url == legacy.url

    def test_live_claim_or_newer_attempt_suppresses_checkout_stall(self) -> None:
        task, ticket = _parked_ticket()
        PullRequestFactory(ticket=ticket, create_verified_at=timezone.now() - timedelta(hours=25))
        active = TaskFactory(ticket=ticket, phase="testing")
        active.claim(claimed_by="worker", claimed_by_session="s1")
        assert _stall_signals() == ()
        Task.objects.filter(pk=active.pk).update(status=Task.Status.COMPLETED)
        assert len(_stall_signals()) == 1
        TaskAttempt.objects.create(task=task, ended_at=timezone.now(), exit_code=0, result={"summary": "done"})
        assert _stall_signals() == ()

    def test_unreadable_source_is_reported_by_registry(self) -> None:
        with patch(
            "teatree.core.factory.stalled_checkout_pr.PullRequest.objects.live",
            side_effect=OperationalError("down"),
        ):
            assert operational_health.collect_signals().unread == ("stalled_checkout_pr_signals",)
