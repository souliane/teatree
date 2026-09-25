"""Behaviour tests for the GitHub App installation/cursor/rejection models (#4795)."""

import datetime as dt

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import GitHubAppInstallation, GitHubPollCursor, WebhookRejection


class TestGitHubAppInstallationRecordSelection(TestCase):
    def setUp(self) -> None:
        self.installation = GitHubAppInstallation.objects.create(
            installation_id=1,
            app_id=100,
            account_login="souliane",
        )

    def test_added_repos_land_in_pending_not_active(self) -> None:
        self.installation.record_selection(added=["souliane/teatree"], removed=[])
        self.installation.refresh_from_db()
        assert self.installation.pending_repositories == ["souliane/teatree"]
        assert self.installation.active_repositories == []

    def test_removed_repos_drop_from_both_sets(self) -> None:
        self.installation.active_repositories = ["souliane/teatree", "souliane/other"]
        self.installation.pending_repositories = ["souliane/pending"]
        self.installation.save()
        self.installation.record_selection(added=[], removed=["souliane/teatree", "souliane/pending"])
        self.installation.refresh_from_db()
        assert self.installation.active_repositories == ["souliane/other"]
        assert self.installation.pending_repositories == []

    def test_an_already_active_repo_re_added_does_not_land_in_pending(self) -> None:
        self.installation.active_repositories = ["souliane/teatree"]
        self.installation.save()
        self.installation.record_selection(added=["souliane/teatree"], removed=[])
        self.installation.refresh_from_db()
        assert self.installation.pending_repositories == []
        assert self.installation.active_repositories == ["souliane/teatree"]


class TestGitHubAppInstallationConfirmRepositories(TestCase):
    def setUp(self) -> None:
        self.installation = GitHubAppInstallation.objects.create(
            installation_id=2,
            app_id=100,
            account_login="souliane",
            pending_repositories=["souliane/a", "souliane/b"],
        )

    def test_confirm_named_repo_moves_only_that_one(self) -> None:
        moved = self.installation.confirm_repositories(["souliane/a"])
        self.installation.refresh_from_db()
        assert moved == ["souliane/a"]
        assert self.installation.active_repositories == ["souliane/a"]
        assert self.installation.pending_repositories == ["souliane/b"]

    def test_confirm_all_moves_every_pending_repo(self) -> None:
        moved = self.installation.confirm_repositories()
        self.installation.refresh_from_db()
        assert moved == ["souliane/a", "souliane/b"]
        assert self.installation.pending_repositories == []

    def test_confirming_an_unknown_repo_is_a_no_op(self) -> None:
        moved = self.installation.confirm_repositories(["souliane/not-pending"])
        assert moved == []
        self.installation.refresh_from_db()
        assert self.installation.active_repositories == []


class TestGitHubAppInstallationVerifiedDelivery(TestCase):
    def setUp(self) -> None:
        self.installation = GitHubAppInstallation.objects.create(
            installation_id=3, app_id=100, account_login="souliane"
        )

    def test_no_delivery_ever_recorded_is_not_recent(self) -> None:
        assert self.installation.has_recent_verified_delivery(within=dt.timedelta(hours=1)) is False

    def test_a_delivery_within_the_window_is_recent(self) -> None:
        self.installation.mark_verified_delivery()
        assert self.installation.has_recent_verified_delivery(within=dt.timedelta(hours=1)) is True

    def test_a_delivery_outside_the_window_is_not_recent(self) -> None:
        stale = timezone.now() - dt.timedelta(hours=2)
        self.installation.mark_verified_delivery(at=stale)
        assert self.installation.has_recent_verified_delivery(within=dt.timedelta(hours=1)) is False


class TestGitHubPollCursor(TestCase):
    def setUp(self) -> None:
        self.installation = GitHubAppInstallation.objects.create(
            installation_id=4, app_id=100, account_login="souliane"
        )

    def test_cursor_for_unpolled_repo_is_empty(self) -> None:
        assert (
            GitHubPollCursor.cursor_for(
                installation=self.installation, repo_full_name="souliane/teatree", event_family="pull_request"
            )
            == ""
        )

    def test_advance_then_cursor_for_round_trips(self) -> None:
        GitHubPollCursor.advance(
            installation=self.installation,
            repo_full_name="souliane/teatree",
            event_family="pull_request",
            cursor_value="2026-09-25T00:00:00Z",
        )
        assert (
            GitHubPollCursor.cursor_for(
                installation=self.installation, repo_full_name="souliane/teatree", event_family="pull_request"
            )
            == "2026-09-25T00:00:00Z"
        )

    def test_advance_twice_upserts_the_same_row(self) -> None:
        GitHubPollCursor.advance(
            installation=self.installation,
            repo_full_name="souliane/teatree",
            event_family="pull_request",
            cursor_value="a",
        )
        GitHubPollCursor.advance(
            installation=self.installation,
            repo_full_name="souliane/teatree",
            event_family="pull_request",
            cursor_value="b",
        )
        assert (
            GitHubPollCursor.objects.filter(
                installation=self.installation, repo_full_name="souliane/teatree", event_family="pull_request"
            ).count()
            == 1
        )
        assert (
            GitHubPollCursor.cursor_for(
                installation=self.installation, repo_full_name="souliane/teatree", event_family="pull_request"
            )
            == "b"
        )

    def test_distinct_event_families_get_independent_cursors(self) -> None:
        GitHubPollCursor.advance(
            installation=self.installation,
            repo_full_name="souliane/teatree",
            event_family="pull_request",
            cursor_value="pr-cursor",
        )
        GitHubPollCursor.advance(
            installation=self.installation,
            repo_full_name="souliane/teatree",
            event_family="issues",
            cursor_value="issue-cursor",
        )
        assert (
            GitHubPollCursor.cursor_for(
                installation=self.installation, repo_full_name="souliane/teatree", event_family="pull_request"
            )
            == "pr-cursor"
        )
        assert (
            GitHubPollCursor.cursor_for(
                installation=self.installation, repo_full_name="souliane/teatree", event_family="issues"
            )
            == "issue-cursor"
        )


class TestWebhookRejection(TestCase):
    def test_record_creates_a_row(self) -> None:
        row = WebhookRejection.record(source="github", reason=WebhookRejection.Reason.OVERSIZED, delivery_id="abc123")
        assert row.pk is not None
        assert row.source == "github"
        assert row.reason == "oversized"
        assert row.delivery_id == "abc123"

    def test_most_recent_first_ordering(self) -> None:
        older = WebhookRejection.record(source="github", reason=WebhookRejection.Reason.NO_SECRET)
        older.occurred_at = timezone.now() - dt.timedelta(hours=1)
        older.save(update_fields=["occurred_at"])
        newer = WebhookRejection.record(source="github", reason=WebhookRejection.Reason.STALE_REPLAY)
        rows = list(WebhookRejection.objects.all())
        assert rows[0].pk == newer.pk
        assert rows[-1].pk == older.pk
