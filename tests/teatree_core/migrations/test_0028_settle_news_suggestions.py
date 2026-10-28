from importlib import import_module

from django.apps import apps
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import PendingArticleSuggestion


class TestSettleNewsSuggestions(TestCase):
    def test_pending_rows_retire_without_overwriting_prior_decisions(self) -> None:
        pending = PendingArticleSuggestion.objects.create(url="https://example.com/new", url_hash="new")
        approved = PendingArticleSuggestion.objects.create(
            url="https://example.com/old",
            url_hash="old",
            status=PendingArticleSuggestion.Status.APPROVED,
            decided_at=timezone.now(),
            issue_url="https://github.com/souliane/teatree/issues/1",
        )
        retire = import_module("teatree.core.migrations.0028_settle_news_suggestions").retire_pending_news_suggestions

        retire(apps, None)
        pending.refresh_from_db()
        approved.refresh_from_db()
        assert pending.status == PendingArticleSuggestion.Status.DROPPED
        assert pending.decided_at is not None
        assert pending.decision_reason
        assert approved.status == PendingArticleSuggestion.Status.APPROVED
        assert approved.issue_url == "https://github.com/souliane/teatree/issues/1"
        settled_at = pending.decided_at
        retire(apps, None)
        pending.refresh_from_db()
        assert pending.decided_at == settled_at
