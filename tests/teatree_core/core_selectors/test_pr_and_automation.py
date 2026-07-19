"""PR action-required selectors and the automation summary.

Split verbatim from the former monolithic ``tests/teatree_core/test_selectors.py`` (souliane/teatree#443).
"""

from typing import TYPE_CHECKING

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import Session, Task, TaskAttempt, Ticket
from teatree.core.selectors import _check_pr, build_action_required, build_automation_summary

if TYPE_CHECKING:
    from teatree.core.models.types import PREntrySerialized


class TestCheckPr(TestCase):
    @classmethod
    def setUpTestData(cls) -> None:
        cls.ticket = Ticket.objects.create(state=Ticket.State.STARTED)

    def test_returns_empty_for_draft(self) -> None:
        assert _check_pr({"draft": True}, self.ticket) == []

    def test_returns_empty_for_non_dict(self) -> None:
        assert _check_pr("not-a-dict", self.ticket) == []

    def test_returns_empty_for_merged(self) -> None:
        """Merged PRs must not surface as action items — bug hunt 2026-04-25 (#455 §2)."""
        assert _check_pr(self._closed_state_pr("merged"), self.ticket) == []

    def test_returns_empty_for_closed(self) -> None:
        """Closed-without-merge PRs must not surface as action items either."""
        assert _check_pr(self._closed_state_pr("closed"), self.ticket) == []

    @staticmethod
    def _closed_state_pr(state: str) -> dict:
        return {
            "draft": False,
            "repo": "backend",
            "iid": 10,
            "url": "https://gitlab.com/org/backend/-/merge_requests/10",
            "pipeline_status": "success",
            "state": state,
            "review_requested": True,
            "approvals": {"count": 0, "required": 2},
            "discussions": [{"status": "needs_reply"}],
        }

    def test_needs_review_request(self) -> None:
        pr = {
            "draft": False,
            "repo": "backend",
            "iid": 10,
            "url": "https://gitlab.com/org/backend/-/merge_requests/10",
            "pipeline_status": "success",
        }
        items = _check_pr(pr, self.ticket)
        assert len(items) == 1
        assert items[0].kind == "needs_review_request"

    def test_needs_reply(self) -> None:
        pr = {
            "draft": False,
            "repo": "backend",
            "iid": 10,
            "url": "https://gitlab.com/org/backend/-/merge_requests/10",
            "pipeline_status": "running",
            "discussions": [
                {"status": "needs_reply"},
                {"status": "needs_reply"},
            ],
        }
        items = _check_pr(pr, self.ticket)
        assert any(item.kind == "needs_reply" for item in items)
        needs_reply_item = next(i for i in items if i.kind == "needs_reply")
        assert "2 comments" in needs_reply_item.label

    def test_needs_reply_singular(self) -> None:
        pr = {
            "draft": False,
            "repo": "backend",
            "iid": 10,
            "url": "https://gitlab.com/org/backend/-/merge_requests/10",
            "discussions": [{"status": "needs_reply"}],
        }
        items = _check_pr(pr, self.ticket)
        assert any("1 comment need reply" in i.label for i in items)

    def test_needs_approval(self) -> None:
        pr = {
            "draft": False,
            "repo": "backend",
            "iid": 10,
            "url": "https://gitlab.com/org/backend/-/merge_requests/10",
            "pipeline_status": "success",
            "review_requested": True,
            "review_permalink": "https://slack.com/x",
            "approvals": {"count": 0, "required": 2},
        }
        items = _check_pr(pr, self.ticket)
        assert any(item.kind == "needs_approval" for item in items)

    def test_non_dict_approvals(self) -> None:
        """Non-dict approvals should be treated as empty."""
        pr = {
            "draft": False,
            "repo": "backend",
            "iid": 10,
            "url": "https://gitlab.com/org/backend/-/merge_requests/10",
            "pipeline_status": "success",
            "review_requested": True,
            "review_permalink": "https://slack.com/x",
            "approvals": "not-a-dict",
        }
        items = _check_pr(pr, self.ticket)
        assert any(item.kind == "needs_approval" for item in items)

    def test_non_list_discussions(self) -> None:
        """When discussions is not a list, the needs_reply check is skipped (branch 464->477)."""
        pr = {
            "draft": False,
            "repo": "backend",
            "iid": 10,
            "url": "https://gitlab.com/org/backend/-/merge_requests/10",
            "discussions": "not-a-list",
        }
        items = _check_pr(pr, self.ticket)
        # No crash; no needs_reply item
        assert all(i.kind != "needs_reply" for i in items)

    def test_review_draft_pending(self) -> None:
        pr = {
            "draft": False,
            "repo": "backend",
            "iid": 10,
            "url": "https://gitlab.com/org/backend/-/merge_requests/10",
            "draft_comments_pending": True,
            "draft_comments_count": 3,
        }
        items = _check_pr(pr, self.ticket)
        assert any(item.kind == "review_draft" for item in items)
        draft_item = next(i for i in items if i.kind == "review_draft")
        assert "3 draft comments" in draft_item.detail
        assert "agent posted review comments" in draft_item.label

    def test_review_draft_singular(self) -> None:
        pr = {
            "draft": False,
            "repo": "backend",
            "iid": 10,
            "url": "https://gitlab.com/org/backend/-/merge_requests/10",
            "draft_comments_pending": True,
            "draft_comments_count": 1,
        }
        items = _check_pr(pr, self.ticket)
        draft_item = next(i for i in items if i.kind == "review_draft")
        assert "1 draft comment need" in draft_item.detail

    def test_review_draft_not_pending(self) -> None:
        pr = {
            "draft": False,
            "repo": "backend",
            "iid": 10,
            "url": "https://gitlab.com/org/backend/-/merge_requests/10",
            "draft_comments_pending": False,
            "draft_comments_count": 0,
        }
        items = _check_pr(pr, self.ticket)
        assert all(i.kind != "review_draft" for i in items)

    def test_fully_typed_pr_entry_flows_through(self) -> None:
        """A ``PREntrySerialized`` populating every key ``_check_pr`` reads yields the expected items (F1.7)."""
        pr: PREntrySerialized = {
            "url": "https://gitlab.com/org/backend/-/merge_requests/10",
            "repo": "backend",
            "iid": 10,
            "draft": False,
            "state": "open",
            "pipeline_status": "success",
            "review_requested": True,
            "review_permalink": "https://slack.com/archives/C1/p2",
            "approvals": {"count": 0, "required": 2},
            "discussions": [{"status": "needs_reply", "detail": "fix the bug"}],
            "draft_comments_pending": True,
            "draft_comments_count": 2,
        }

        items = _check_pr(pr, self.ticket)

        kinds = {item.kind for item in items}
        assert "needs_reply" in kinds
        assert "needs_approval" in kinds
        assert "review_draft" in kinds

    def test_review_draft_missing_count(self) -> None:
        """When draft_comments_pending is True but count is missing, no item."""
        pr = {
            "draft": False,
            "repo": "backend",
            "iid": 10,
            "url": "https://gitlab.com/org/backend/-/merge_requests/10",
            "draft_comments_pending": True,
        }
        items = _check_pr(pr, self.ticket)
        assert all(i.kind != "review_draft" for i in items)


class TestBuildActionRequired(TestCase):
    def test_skips_non_dict_prs(self) -> None:
        """When prs is not a dict, it should be skipped."""
        Ticket.objects.create(
            state=Ticket.State.STARTED,
            extra={"prs": "not-a-dict"},
        )

        items = build_action_required()

        assert all(item.kind == "interactive_task" for item in items) or items == []

    def test_includes_pr_action_items(self) -> None:
        """build_action_required iterates PRs and calls _check_pr (covers line 432)."""
        Ticket.objects.create(
            state=Ticket.State.STARTED,
            extra={
                "prs": {
                    "url1": {
                        "draft": False,
                        "repo": "backend",
                        "iid": 10,
                        "url": "https://gitlab.com/org/backend/-/merge_requests/10",
                        "pipeline_status": "success",
                    },
                },
            },
        )

        items = build_action_required()

        assert any(i.kind == "needs_review_request" for i in items)


class TestReviewCommentsInActionRequired(TestCase):
    """Review comments are now embedded in ActionRequiredItem via build_action_required."""

    def test_needs_reply_includes_review_comments(self) -> None:
        Ticket.objects.create(
            state=Ticket.State.STARTED,
            extra={
                "prs": {
                    "url1": {
                        "url": "https://gitlab.com/org/backend/-/merge_requests/10",
                        "repo": "backend",
                        "iid": "10",
                        "discussions": [
                            {"status": "needs_reply", "detail": "Fix the bug"},
                            {"status": "addressed", "detail": "Done"},
                        ],
                    },
                },
            },
        )

        items = build_action_required()

        reply_items = [i for i in items if i.kind == "needs_reply"]
        assert len(reply_items) == 1
        assert len(reply_items[0].review_comments) == 2
        assert reply_items[0].review_comments[0].status == "Needs reply"
        assert reply_items[0].review_comments[1].status == "Addressed"

    def test_needs_reply_includes_slack_url(self) -> None:
        Ticket.objects.create(
            state=Ticket.State.STARTED,
            extra={
                "prs": {
                    "url1": {
                        "url": "https://gitlab.com/org/backend/-/merge_requests/10",
                        "repo": "backend",
                        "iid": "10",
                        "review_permalink": "https://slack.com/archives/C123/p456",
                        "discussions": [
                            {"status": "needs_reply", "detail": "Fix it"},
                        ],
                    },
                },
            },
        )

        items = build_action_required()

        reply_items = [i for i in items if i.kind == "needs_reply"]
        assert len(reply_items) == 1
        assert reply_items[0].slack_url == "https://slack.com/archives/C123/p456"

    def test_skips_non_dict_discussions(self) -> None:
        Ticket.objects.create(
            state=Ticket.State.STARTED,
            extra={
                "prs": {
                    "url1": {
                        "url": "https://gitlab.com/org/x/-/merge_requests/1",
                        "repo": "x",
                        "iid": "1",
                        "discussions": "not-a-list",
                    },
                },
            },
        )

        items = build_action_required()
        reply_items = [i for i in items if i.kind == "needs_reply"]
        assert reply_items == []

    def test_skips_non_dict_discussion_entries(self) -> None:
        Ticket.objects.create(
            state=Ticket.State.STARTED,
            extra={
                "prs": {
                    "url1": {
                        "url": "https://gitlab.com/org/x/-/merge_requests/1",
                        "repo": "x",
                        "iid": "1",
                        "discussions": ["not-a-dict"],
                    },
                },
            },
        )

        items = build_action_required()
        reply_items = [i for i in items if i.kind == "needs_reply"]
        assert reply_items == []


class TestBuildAutomationSummary(TestCase):
    def test_counts_headless_activity(self) -> None:
        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        running_task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.CLAIMED,
        )
        completed_task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.COMPLETED,
        )
        failed_task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.FAILED,
        )
        # Successful attempt
        TaskAttempt.objects.create(
            task=completed_task,
            execution_target="headless",
            exit_code=0,
            ended_at=timezone.now(),
        )
        # Failed attempt
        TaskAttempt.objects.create(
            task=failed_task,
            execution_target="headless",
            exit_code=1,
            ended_at=timezone.now(),
        )
        # Running attempt (no ended_at)
        TaskAttempt.objects.create(
            task=running_task,
            execution_target="headless",
        )

        summary = build_automation_summary()

        assert summary.running == 1
        assert summary.completed_24h == 2
        assert summary.succeeded_24h == 1
        assert summary.failed_24h == 1

    def test_excludes_old_attempts(self) -> None:
        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.COMPLETED,
        )
        old_time = timezone.now() - timezone.timedelta(hours=25)
        TaskAttempt.objects.create(
            task=task,
            execution_target="headless",
            exit_code=0,
            ended_at=old_time,
        )

        summary = build_automation_summary()

        assert summary.completed_24h == 0
        assert summary.succeeded_24h == 0

    def test_last_completed_at(self) -> None:
        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        task = Task.objects.create(
            ticket=ticket,
            session=session,
            execution_target=Task.ExecutionTarget.HEADLESS,
            status=Task.Status.COMPLETED,
        )
        now = timezone.now()
        TaskAttempt.objects.create(
            task=task,
            execution_target="headless",
            exit_code=0,
            ended_at=now,
        )

        summary = build_automation_summary()

        assert summary.last_completed_at == now.isoformat()

    def test_aggregates_token_usage(self) -> None:
        ticket = Ticket.objects.create(state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id="agent")
        for input_t, output_t, cost in [(1000, 500, 0.01), (2000, 800, 0.02)]:
            task = Task.objects.create(
                ticket=ticket,
                session=session,
                execution_target=Task.ExecutionTarget.HEADLESS,
                status=Task.Status.COMPLETED,
            )
            TaskAttempt.objects.create(
                task=task,
                execution_target="headless",
                exit_code=0,
                ended_at=timezone.now(),
                input_tokens=input_t,
                output_tokens=output_t,
                cost_usd=cost,
            )

        summary = build_automation_summary()

        assert summary.total_tokens_24h == 4300
        assert summary.total_cost_24h == pytest.approx(0.03)

    def test_empty_state(self) -> None:
        summary = build_automation_summary()

        assert summary.running == 0
        assert summary.completed_24h == 0
        assert summary.succeeded_24h == 0
        assert summary.failed_24h == 0
        assert summary.last_completed_at == ""
        assert summary.total_tokens_24h == 0
        assert summary.total_cost_24h == pytest.approx(0.0)
