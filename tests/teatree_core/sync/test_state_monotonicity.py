"""External trackers are advance-only signals — a sync never rewinds a ticket.

A board column and a PR-inferred state both describe where an EXTERNAL system
thinks a ticket sits, which routinely lags the ticket's own FSM. Writing such a
value unconditionally resets live work: a shipped ticket with an open PR drops
back to not-started and the loop's scanners re-arm against delivered work.

Both code hosts share the one ordering (``Ticket.state_advances``); the last
class pins that shared contract so a third backend cannot reintroduce the
asymmetry.
"""

from unittest.mock import patch

from django.test import TestCase

from teatree.backends.github import ProjectItem
from teatree.backends.github.sync import GitHubSyncBackend
from teatree.backends.gitlab.sync_prs import update_ticket
from teatree.core.models import Ticket
from teatree.core.sync import sync_followup  # noqa: F401 — pins this suite to the sync package
from tests.teatree_core.sync._overlays import SyncOverlay, _patch_overlay

_ISSUE_URL = "https://github.com/souliane/teatree/issues/60"


def _board_item(status: str, *, url: str = _ISSUE_URL) -> ProjectItem:
    return ProjectItem(
        issue_number=60,
        title="Board item",
        url=url,
        status=status,
        position=1,
        labels=[],
        updated_at="2026-05-01T00:00:00Z",
    )


class _GitHubBoardSync(TestCase):
    """Runs one GitHub board sync against a single project item."""

    def _sync(self, item: ProjectItem) -> None:
        overlay = SyncOverlay(
            gitlab_token="",
            gitlab_username="",
            github_token="gh-test-token",
            github_owner="souliane",
            github_project_number=1,
        )
        with (
            _patch_overlay(overlay),
            patch("teatree.backends.github.fetch_project_items", return_value=[item]),
            patch.object(GitHubSyncBackend, "_sync_reviewer_prs"),
            patch("teatree.backends.github.sync.cleanup_worktree"),
        ):
            GitHubSyncBackend().sync(overlay)


class TestGitHubBoardStatusIsMonotonic(_GitHubBoardSync):
    def test_unmapped_column_leaves_the_ticket_state_untouched(self) -> None:
        ticket = Ticket.objects.create(issue_url=_ISSUE_URL, state=Ticket.State.SHIPPED)

        with self.assertLogs("teatree.backends.github.sync", level="WARNING") as logs:
            self._sync(_board_item("In Review"))

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SHIPPED
        assert "In Review" in "\n".join(logs.output)
        # The rest of the board payload still lands — only ``state`` is withheld.
        assert ticket.extra["board_status"] == "In Review"

    def test_unmapped_column_on_a_new_ticket_starts_at_not_started(self) -> None:
        self._sync(_board_item("Backlog"))

        ticket = Ticket.objects.get(issue_url=_ISSUE_URL)
        assert ticket.state == Ticket.State.NOT_STARTED

    def test_mapped_column_that_advances_is_applied(self) -> None:
        ticket = Ticket.objects.create(issue_url=_ISSUE_URL, state=Ticket.State.NOT_STARTED)

        self._sync(_board_item("In Progress"))

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.STARTED

    def test_mapped_column_that_would_regress_is_withheld(self) -> None:
        ticket = Ticket.objects.create(issue_url=_ISSUE_URL, state=Ticket.State.DELIVERED)

        self._sync(_board_item("Todo"))

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.DELIVERED


class TestBothBackendsShareOneMonotonicContract(_GitHubBoardSync):
    """Same lagging signal, same verdict — whichever code host reports it."""

    def test_github_board_does_not_rewind_a_shipped_ticket(self) -> None:
        ticket = Ticket.objects.create(issue_url=_ISSUE_URL, state=Ticket.State.SHIPPED)

        self._sync(_board_item("Todo"))

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SHIPPED

    def test_gitlab_pr_inference_does_not_rewind_a_shipped_ticket(self) -> None:
        pr_url = "https://gitlab.com/org/repo/-/merge_requests/60"
        ticket = Ticket.objects.create(
            issue_url="https://gitlab.com/org/repo/-/issues/60",
            state=Ticket.State.SHIPPED,
            repos=["repo"],
        )

        update_ticket(
            ticket,
            {"title": "MR60"},
            pr_url,
            "repo",
            inferred_state=Ticket.State.NOT_STARTED,
        )

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SHIPPED
        assert pr_url in ticket.extra["prs"]

    def test_state_advances_orders_the_lifecycle(self) -> None:
        assert Ticket.state_advances(Ticket.State.NOT_STARTED, Ticket.State.SHIPPED)
        assert not Ticket.state_advances(Ticket.State.SHIPPED, Ticket.State.NOT_STARTED)
        assert not Ticket.state_advances(Ticket.State.SHIPPED, Ticket.State.SHIPPED)
