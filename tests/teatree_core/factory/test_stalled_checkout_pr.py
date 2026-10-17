"""The checkout stall signal is sourced from durable tasks and pull requests."""

from datetime import timedelta
from typing import cast
from unittest.mock import patch

from django.db import OperationalError
from django.test import TestCase
from django.utils import timezone

from teatree.core.factory import operational_health
from teatree.core.factory.stalled_checkout_pr import stalled_checkout_pr_signals
from teatree.core.models import Task, TaskAttempt, Ticket
from tests.factories import PullRequestFactory, TaskFactory, TicketFactory


def _parked_ticket() -> tuple[Task, Ticket]:
    ticket = cast("Ticket", TicketFactory(short_description="Retry parked checkout tasks"))
    task = cast("Task", TaskFactory(ticket=ticket, phase="coding"))
    TaskAttempt.objects.create(task=task, ended_at=timezone.now(), exit_code=0, error="checkout_parked: held")
    return task, ticket


class TestStalledCheckoutPullRequest(TestCase):
    def test_old_open_pull_request_is_linked_in_health(self) -> None:
        _, ticket = _parked_ticket()
        pr = PullRequestFactory(ticket=ticket, create_verified_at=timezone.now() - timedelta(hours=25))

        with patch.object(operational_health, "_COLLECTORS", (stalled_checkout_pr_signals,)):
            report = operational_health.reconcile_health()
        issues = [issue for issue in report.open_issues if issue.kind == "stalled_checkout_pr"]
        assert len(issues) == 1
        assert issues[0].evidence_url == pr.url
        assert f"ticket #{ticket.pk} (Retry parked checkout tasks)" in issues[0].summary
        assert issues[0].summary.endswith("its next task parked at the checkout fence: held")

    def test_recent_or_settled_pull_request_does_not_raise_stall(self) -> None:
        _, ticket = _parked_ticket()
        pr = PullRequestFactory(ticket=ticket, create_verified_at=timezone.now() - timedelta(hours=23))
        assert stalled_checkout_pr_signals().signals == ()
        pr.create_verified_at = timezone.now() - timedelta(hours=25)
        pr.state = pr.State.MERGED
        pr.save(update_fields=["create_verified_at", "state"])
        assert stalled_checkout_pr_signals().signals == ()

    def test_legacy_undated_pull_requests_are_deduplicated_per_ticket(self) -> None:
        _, ticket = _parked_ticket()
        legacy = PullRequestFactory(ticket=ticket, create_verified_at=None)
        PullRequestFactory(ticket=ticket, create_verified_at=timezone.now() - timedelta(hours=25))
        signals = stalled_checkout_pr_signals().signals
        assert len(signals) == 1
        assert signals[0].evidence_url == legacy.url

    def test_active_claim_or_newer_nonpark_attempt_suppresses_stall(self) -> None:
        task, ticket = _parked_ticket()
        PullRequestFactory(ticket=ticket, create_verified_at=timezone.now() - timedelta(hours=25))
        active = TaskFactory(ticket=ticket, phase="testing")
        active.claim(claimed_by="worker", claimed_by_session="s1")
        assert stalled_checkout_pr_signals().signals == ()
        Task.objects.filter(pk=active.pk).update(status=Task.Status.COMPLETED)
        TaskAttempt.objects.create(task=task, ended_at=timezone.now(), exit_code=0, result={"summary": "done"})
        assert stalled_checkout_pr_signals().signals == ()

    def test_unreadable_source_is_reported_by_registry(self) -> None:
        with (
            patch.object(operational_health, "_COLLECTORS", (stalled_checkout_pr_signals,)),
            patch(
                "teatree.core.factory.stalled_checkout_pr.PullRequest.objects.live",
                side_effect=OperationalError("down"),
            ),
        ):
            assert operational_health.collect_signals().unread == ("stalled_checkout_pr_signals",)
