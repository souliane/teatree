"""``0102`` attributes only review-request rows whose URL has one overlay answer."""

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

_BEFORE = ("core", "0098_merge_vendor_sync_and_scannedbroadcast")
_AFTER = ("core", "0102_review_request_post_overlay")


@pytest.mark.timeout(240)
class TestReviewRequestPostOverlayBackfill(TransactionTestCase):
    def setUp(self) -> None:
        self.addCleanup(self._restore_head)

    @staticmethod
    def _restore_head() -> None:
        connection.close()
        call_command("migrate", "core", "--no-input", verbosity=0)

    @staticmethod
    def _seed_before() -> None:
        executor = MigrationExecutor(connection)
        executor.migrate([_BEFORE])
        apps = executor.loader.project_state(_BEFORE).apps
        posts = apps.get_model("core", "ReviewRequestPost")
        assignments = apps.get_model("core", "ReviewAssignment")
        for iid in (1, 2, 3):
            posts.objects.create(
                mr_url=f"https://gitlab.example/o/r/-/merge_requests/{iid}",
                slack_channel_id="C1",
                slack_thread_ts=f"ts.{iid}",
            )
        assignments.objects.create(
            overlay="overlay-a",
            mr_url="https://gitlab.example/o/r/-/merge_requests/1",
            user_id="alice",
            channel="C1",
            slack_ts="ts.1",
        )
        for overlay, user_id in (("overlay-a", "alice"), ("overlay-b", "bob")):
            assignments.objects.create(
                overlay=overlay,
                mr_url="https://gitlab.example/o/r/-/merge_requests/2",
                user_id=user_id,
                channel="C1",
                slack_ts="ts.2",
            )

    @staticmethod
    def _migrate_and_read() -> dict[str, str | None]:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate([_AFTER])
        posts = (
            MigrationExecutor(connection)
            .loader.project_state([_AFTER])
            .apps.get_model(
                "core",
                "ReviewRequestPost",
            )
        )
        return {row.mr_url.rsplit("/", 1)[-1]: row.overlay for row in posts.objects.all()}

    def test_only_the_unambiguous_url_is_attributed(self) -> None:
        self._seed_before()

        assert self._migrate_and_read() == {"1": "overlay-a", "2": None, "3": None}
