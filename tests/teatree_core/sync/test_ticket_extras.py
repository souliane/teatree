"""Ticket update and extra-field merge tests (souliane/teatree#443 split of test_sync.py).

Covers update_ticket field preservation and merge_ticket_extras.
"""

from typing import TYPE_CHECKING

from django.test import TestCase

from teatree.backends.gitlab_sync_prs import merge_ticket_extras, update_ticket
from teatree.core.models import Ticket

if TYPE_CHECKING:
    from teatree.types import PREntryDict


class TestUpdateTicket(TestCase):
    def test_preserves_skill_written_fields(self) -> None:
        """Skill-written fields (review_channel, review_permalink, e2e_test_plan_url) survive sync updates."""
        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/200",
            repos=["repo"],
            extra={
                "prs": {
                    "https://gitlab.com/org/repo/-/merge_requests/50": {
                        "url": "https://gitlab.com/org/repo/-/merge_requests/50",
                        "repo": "repo",
                        "title": "feat: old title",
                        "review_channel": "#backend-review",
                        "review_permalink": "https://slack.com/archives/C123/p456",
                        "e2e_test_plan_url": "https://gitlab.com/org/repo/-/merge_requests/50#note_789",
                    },
                },
            },
        )

        # Simulate a sync update that doesn't include the skill-written fields
        new_mr_entry: PREntryDict = {
            "url": "https://gitlab.com/org/repo/-/merge_requests/50",
            "repo": "repo",
            "title": "feat: new title",
            "pipeline_status": "success",
        }

        mr_url = "https://gitlab.com/org/repo/-/merge_requests/50"
        update_ticket(ticket, new_mr_entry, mr_url, "repo")

        ticket.refresh_from_db()
        mr = ticket.extra["prs"]["https://gitlab.com/org/repo/-/merge_requests/50"]
        assert mr["title"] == "feat: new title"
        assert mr["review_channel"] == "#backend-review"
        assert mr["review_permalink"] == "https://slack.com/archives/C123/p456"
        assert mr["e2e_test_plan_url"] == "https://gitlab.com/org/repo/-/merge_requests/50#note_789"


class TestMergeTicketExtras(TestCase):
    def test_combines_mrs_and_repos(self) -> None:
        """_merge_ticket_extras merges MR entries and repos from source into target."""
        target = Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/900",
            repos=["repo-a"],
            extra={"prs": {"https://mr/1": {"title": "MR 1"}}},
        )
        source = Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/901",
            repos=["repo-b"],
            extra={"prs": {"https://mr/2": {"title": "MR 2"}}},
        )
        merge_ticket_extras(target, source)
        target.refresh_from_db()

        assert "https://mr/1" in target.extra["prs"]
        assert "https://mr/2" in target.extra["prs"]
        assert "repo-a" in target.repos
        assert "repo-b" in target.repos

    def test_handles_non_dict_mrs(self) -> None:
        """Non-dict prs in extras are treated as empty -- repos still merge."""
        target = Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/960",
            repos=["repo-a"],
            extra={"prs": "corrupt"},
        )
        source = Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/961",
            repos=["repo-b"],
            extra={"prs": ["also-corrupt"]},
        )
        merge_ticket_extras(target, source)
        target.refresh_from_db()
        assert target.repos == ["repo-a", "repo-b"]

    def test_skips_overlapping_mrs_and_repos(self) -> None:
        """Overlapping MR URLs and repos are not duplicated."""
        target = Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/950",
            repos=["repo-a", "repo-b"],
            extra={"prs": {"https://mr/1": {"title": "MR 1"}}},
        )
        source = Ticket.objects.create(
            overlay="test",
            issue_url="https://gitlab.com/org/repo/-/issues/951",
            repos=["repo-b", "repo-c"],
            extra={"prs": {"https://mr/1": {"title": "MR 1 dup"}, "https://mr/3": {"title": "MR 3"}}},
        )
        merge_ticket_extras(target, source)
        target.refresh_from_db()

        assert target.extra["prs"]["https://mr/1"]["title"] == "MR 1"
        assert "https://mr/3" in target.extra["prs"]
        assert target.repos == ["repo-a", "repo-b", "repo-c"]
