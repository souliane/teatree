# test-path: cross-cutting
"""The GitHub polling scanner discovers events via the SAME normalize/identity path the webhook view uses (#4795)."""

from unittest import mock

from django.test import TestCase

from teatree.core.github_app.webhook_normalize import normalize
from teatree.core.models import GitHubAppInstallation, GitHubPollCursor, IncomingEvent
from teatree.core.views._webhook_persistence import persist_incoming_event
from teatree.loop.scanners.github_polling import GitHubPollingScanner


class TestGitHubPollingScanner(TestCase):
    def setUp(self) -> None:
        self.installation = GitHubAppInstallation.objects.create(
            installation_id=42,
            app_id=999,
            account_login="souliane",
            active_repositories=["souliane/teatree"],
        )

    def _scanner(self, *, pulls: list[dict] | None = None, issues: list[dict] | None = None) -> GitHubPollingScanner:
        responses = {
            "repos/souliane/teatree/pulls?state=all&sort=updated&direction=desc": pulls or [],
            "repos/souliane/teatree/issues?state=all&sort=updated&direction=desc": issues or [],
        }

        def fake_get(endpoint: str, *, token: str = "") -> object:
            return responses.get(endpoint, [])

        scanner = GitHubPollingScanner(token_cache=mock.Mock())
        scanner.token_cache.token_for.return_value = "ghs_fake"
        patcher = mock.patch("teatree.loop.scanners.github_polling._gh_api_get", side_effect=fake_get)
        patcher.start()
        self.addCleanup(patcher.stop)
        return scanner

    def test_suspended_installations_are_skipped(self) -> None:
        self.installation.suspended_at = self.installation.created_at
        self.installation.save()
        scanner = self._scanner(pulls=[{"number": 1, "updated_at": "2026-09-25T10:00:00Z", "title": "x"}])

        scanner.scan()

        assert IncomingEvent.objects.count() == 0

    def test_discovered_pull_request_is_persisted_via_the_shared_normalizer(self) -> None:
        scanner = self._scanner(
            pulls=[{"number": 17, "updated_at": "2026-09-25T10:00:00Z", "title": "Add feature"}],
        )

        signals = scanner.scan()

        assert signals == []
        event = IncomingEvent.objects.get()
        assert event.source == IncomingEvent.Source.GITHUB
        assert event.channel_ref == "souliane/teatree"
        assert event.thread_ref == "17"
        assert event.idempotency_key == "github:pr:souliane/teatree:17:2026-09-25T10:00:00Z"

    def test_a_webhook_delivered_pr_and_a_polled_discovery_of_the_same_update_collapse(self) -> None:
        webhook_payload = {
            "repository": {"full_name": "souliane/teatree"},
            "pull_request": {"number": 17, "updated_at": "2026-09-25T10:00:00Z", "title": "Add feature"},
            "sender": {"login": "alice"},
        }
        persist_incoming_event(normalize("pull_request", webhook_payload, delivery_id="webhook-delivery-1"))

        scanner = self._scanner(
            pulls=[{"number": 17, "updated_at": "2026-09-25T10:00:00Z", "title": "Add feature"}],
        )
        scanner.scan()

        assert IncomingEvent.objects.count() == 1

    def test_cursor_advances_and_a_second_scan_skips_already_seen_items(self) -> None:
        scanner = self._scanner(
            pulls=[{"number": 17, "updated_at": "2026-09-25T10:00:00Z", "title": "x"}],
        )
        scanner.scan()
        assert IncomingEvent.objects.count() == 1
        assert (
            GitHubPollCursor.cursor_for(
                installation=self.installation, repo_full_name="souliane/teatree", event_family="pull_request"
            )
            == "2026-09-25T10:00:00Z"
        )

        # Second tick: GitHub returns the SAME (already-seen) item — no new IncomingEvent.
        scanner.scan()
        assert IncomingEvent.objects.count() == 1

    def test_a_newer_item_after_the_cursor_is_persisted(self) -> None:
        scanner = self._scanner(pulls=[{"number": 17, "updated_at": "2026-09-25T10:00:00Z", "title": "x"}])
        scanner.scan()

        scanner2 = self._scanner(pulls=[{"number": 17, "updated_at": "2026-09-25T11:00:00Z", "title": "x"}])
        scanner2.scan()

        assert IncomingEvent.objects.count() == 2

    def test_issues_endpoint_pull_requests_are_skipped_as_covered_by_the_pr_family(self) -> None:
        scanner = self._scanner(
            issues=[{"number": 17, "updated_at": "2026-09-25T10:00:00Z", "title": "x", "pull_request": {}}],
        )

        scanner.scan()

        assert IncomingEvent.objects.count() == 0

    def test_no_active_repositories_polls_nothing(self) -> None:
        self.installation.active_repositories = []
        self.installation.save()
        scanner = self._scanner(pulls=[{"number": 1, "updated_at": "2026-09-25T10:00:00Z", "title": "x"}])

        scanner.scan()

        assert IncomingEvent.objects.count() == 0
