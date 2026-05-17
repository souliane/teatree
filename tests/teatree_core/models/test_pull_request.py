"""Admin registration and PullRequest model tests.

souliane/teatree#443 split of test_models.py.
"""

from django.contrib import admin
from django.test import TestCase

from teatree.core import admin as core_admin
from teatree.core.models import PullRequest, Session, Task, TaskAttempt, Ticket, Worktree


class TestAdmin(TestCase):
    def test_registers_all_core_models(self) -> None:
        registry = admin.site._registry

        assert Ticket in registry
        assert Worktree in registry
        assert Session in registry
        assert Task in registry
        assert TaskAttempt in registry
        assert core_admin is not None


class TestPullRequestModel(TestCase):
    def test_str_representation(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        pr = PullRequest.objects.create(
            ticket=ticket,
            url="https://example.com/repo/-/merge_requests/1",
            repo="my-repo",
            iid="42",
        )
        assert str(pr) == "my-repo #42"

    def test_request_review_transition(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        pr = PullRequest.objects.create(
            ticket=ticket,
            url="https://example.com/repo/-/merge_requests/2",
            repo="my-repo",
            iid="43",
        )
        pr.request_review(slack_url="https://slack.com/msg/123")
        pr.save()
        pr.refresh_from_db()
        assert pr.state == PullRequest.State.REVIEW_REQUESTED
        assert pr.slack_url == "https://slack.com/msg/123"
        assert pr.review_requested_at is not None

    def test_approve_transition(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        pr = PullRequest.objects.create(
            ticket=ticket,
            url="https://example.com/repo/-/merge_requests/3",
            repo="my-repo",
            iid="44",
            state=PullRequest.State.REVIEW_REQUESTED,
        )
        pr.approve()
        pr.save()
        assert pr.state == PullRequest.State.APPROVED

    def test_mark_merged_transition(self) -> None:
        ticket = Ticket.objects.create(overlay="test")
        pr = PullRequest.objects.create(
            ticket=ticket,
            url="https://example.com/repo/-/merge_requests/4",
            repo="my-repo",
            iid="45",
        )
        pr.mark_merged()
        pr.save()
        assert pr.state == PullRequest.State.MERGED
