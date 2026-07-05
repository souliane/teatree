"""#17: the GitLab assigned-issue sync intake classifies Ticket.kind at create time.

``fetch_assigned_issues`` is the primary real-defect intake — a board issue
carrying a ``bug`` label (or a ``fix …`` title) must be created as FIX, not
FEATURE. Classification is create-only, so a mis-classified sync ticket can never
be reclassified: S2 would stay blind and the fix-record DoD gate would never fire.
RED before the ``Ticket.objects.create`` passed ``kind=`` through.
"""

from unittest.mock import MagicMock

from django.test import TestCase

from teatree.backends.gitlab.sync_issues import fetch_assigned_issues
from teatree.core.models import Ticket
from teatree.types import SyncResult


class TestGitLabAssignedIssueSyncClassifiesKind(TestCase):
    def _synced_ticket(self, *, url: str, title: str, labels: list[str]) -> Ticket:
        host = MagicMock()
        host.list_assigned_issues.return_value = [{"web_url": url, "title": title, "labels": labels}]
        fetch_assigned_issues(host, "me", SyncResult(), overlay_name="acme")
        return Ticket.objects.get(issue_url=url)

    def test_bug_labeled_issue_is_fix(self) -> None:
        ticket = self._synced_ticket(
            url="https://gitlab.com/o/r/-/issues/711",
            title="Login button unresponsive",
            labels=["bug"],
        )
        assert ticket.kind == Ticket.Kind.FIX

    def test_fix_titled_issue_is_fix(self) -> None:
        ticket = self._synced_ticket(
            url="https://gitlab.com/o/r/-/issues/712",
            title="fix: crash on empty export",
            labels=[],
        )
        assert ticket.kind == Ticket.Kind.FIX

    def test_plain_feature_issue_is_feature(self) -> None:
        ticket = self._synced_ticket(
            url="https://gitlab.com/o/r/-/issues/713",
            title="Add dark mode toggle",
            labels=["enhancement"],
        )
        assert ticket.kind == Ticket.Kind.FEATURE

    def test_substring_lookalike_label_stays_feature(self) -> None:
        # A "debug" label must NOT flip a feature to FIX (token-boundary matching).
        ticket = self._synced_ticket(
            url="https://gitlab.com/o/r/-/issues/714",
            title="Improve the debug console",
            labels=["debug"],
        )
        assert ticket.kind == Ticket.Kind.FEATURE
