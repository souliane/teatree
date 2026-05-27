"""Tests for :class:`PendingArticleSuggestion` and the ask-gate helper (#1391).

The ask gate is the single chokepoint between news-scan triage and
ticket creation. The tests assert:

* the durable model is the only path to a row (guarded factory),
* idempotency on ``url_hash`` is load-bearing (second scan = no-op),
* the gate enqueues + DMs without calling ``gh issue create`` until
    an explicit approval happens,
* ``approve_and_create_ticket`` calls ``gh issue create`` exactly once,
    stamps the row APPROVED, and records the new issue URL.
"""

from unittest import mock

import pytest

from teatree.core.article_ingestion_gate import (
    APPROVED_ISSUE_LABEL,
    APPROVED_ISSUE_REPO,
    ArticleCandidate,
    approve_and_create_ticket,
    enqueue_candidates_and_notify,
)
from teatree.core.models import PendingArticleSuggestion
from teatree.core.models.pending_article_suggestion import PendingArticleSuggestionError

pytestmark = pytest.mark.django_db


class TestPendingArticleSuggestionRecord:
    def test_record_creates_a_pending_row_with_hash(self) -> None:
        row = PendingArticleSuggestion.record(
            url="https://example.com/a",
            summary="why interesting",
            title="An article",
            source="tldr-ai",
        )
        assert row.pk is not None
        assert row.is_pending is True
        assert row.decision == PendingArticleSuggestion.DECISION_PENDING
        assert row.url_hash == PendingArticleSuggestion.hash_url("https://example.com/a")
        assert row.title == "An article"
        assert row.source == "tldr-ai"
        assert row.created_ticket_url == ""

    def test_record_strips_and_requires_url(self) -> None:
        with pytest.raises(PendingArticleSuggestionError, match="url is required"):
            PendingArticleSuggestion.record(url="   ", summary="x")

    def test_record_strips_and_requires_summary(self) -> None:
        with pytest.raises(PendingArticleSuggestionError, match="summary is required"):
            PendingArticleSuggestion.record(url="https://example.com/a", summary="   ")


class TestRecordIfNewIdempotency:
    def test_first_record_succeeds_second_is_noop(self) -> None:
        first = PendingArticleSuggestion.record_if_new(
            url="https://example.com/a",
            summary="why",
            title="Article A",
        )
        second = PendingArticleSuggestion.record_if_new(
            url="https://example.com/a",
            summary="why again",
            title="Article A (dup)",
        )
        assert first is not None
        assert second is None
        assert (
            PendingArticleSuggestion.objects.filter(
                url_hash=PendingArticleSuggestion.hash_url("https://example.com/a"),
            ).count()
            == 1
        )

    def test_record_if_new_skips_already_rejected_url(self) -> None:
        row = PendingArticleSuggestion.record_if_new(
            url="https://example.com/a",
            summary="why",
        )
        assert row is not None
        PendingArticleSuggestion.reject(row.pk, decider_id="adrien", reason="not relevant")
        assert PendingArticleSuggestion.record_if_new(url="https://example.com/a", summary="why") is None


class TestApproveRejectSingleUse:
    def test_approve_stamps_decision_and_ticket_url(self) -> None:
        row = PendingArticleSuggestion.record(url="https://example.com/a", summary="why")
        consumed = PendingArticleSuggestion.approve(
            row.pk,
            decider_id="adrien",
            ticket_url="https://github.com/souliane/teatree/issues/1234",
        )
        assert consumed is not None
        assert consumed.decision == PendingArticleSuggestion.DECISION_APPROVED
        assert consumed.created_ticket_url == "https://github.com/souliane/teatree/issues/1234"
        assert consumed.decided_at is not None
        assert consumed.decider_id == "adrien"

    def test_reject_stamps_decision_and_reason(self) -> None:
        row = PendingArticleSuggestion.record(url="https://example.com/a", summary="why")
        consumed = PendingArticleSuggestion.reject(
            row.pk,
            decider_id="adrien",
            reason="copycat article",
        )
        assert consumed is not None
        assert consumed.decision == PendingArticleSuggestion.DECISION_REJECTED
        assert consumed.decision_reason == "copycat article"
        assert consumed.decided_at is not None

    def test_decisions_are_single_use(self) -> None:
        row = PendingArticleSuggestion.record(url="https://example.com/a", summary="why")
        assert PendingArticleSuggestion.approve(row.pk, ticket_url="x") is not None
        assert PendingArticleSuggestion.approve(row.pk, ticket_url="y") is None
        assert PendingArticleSuggestion.reject(row.pk) is None


class TestEnqueueCandidatesAndNotify:
    def test_no_ticket_is_created_on_scan(self) -> None:
        with (
            mock.patch(
                "teatree.core.article_ingestion_gate._notify_user",
                return_value=True,
            ) as notify_mock,
            mock.patch(
                "teatree.core.article_ingestion_gate._gh_issue_create",
            ) as gh_mock,
        ):
            result = enqueue_candidates_and_notify(
                [
                    ArticleCandidate(
                        url="https://example.com/a",
                        title="A",
                        summary="reason A",
                        source="tldr-ai",
                    ),
                    ArticleCandidate(
                        url="https://example.com/b",
                        title="B",
                        summary="reason B",
                        source="rundown-ai",
                    ),
                ],
            )
        # Neither candidate became a GitHub issue.
        gh_mock.assert_not_called()
        # Two pending rows landed in the durable queue.
        assert len(result.new_suggestion_ids) == 2
        assert result.skipped_duplicate_urls == []
        assert result.dm_sent is True
        notify_mock.assert_called_once()
        # The DM listed every new suggestion id.
        dm_text = notify_mock.call_args.kwargs["text"]
        assert "#" + str(result.new_suggestion_ids[0]) in dm_text
        assert "https://example.com/a" in dm_text
        assert "https://example.com/b" in dm_text
        # The presented stamp moved so the next tick won't re-DM.
        for sid in result.new_suggestion_ids:
            row = PendingArticleSuggestion.objects.get(pk=sid)
            assert row.presented is True
            assert row.presented_at is not None

    def test_duplicate_urls_are_skipped_idempotently(self) -> None:
        # Pre-existing row for URL A.
        existing = PendingArticleSuggestion.record(url="https://example.com/a", summary="x")
        with (
            mock.patch(
                "teatree.core.article_ingestion_gate._notify_user",
                return_value=True,
            ),
            mock.patch("teatree.core.article_ingestion_gate._gh_issue_create"),
        ):
            result = enqueue_candidates_and_notify(
                [
                    ArticleCandidate(url="https://example.com/a", title="A", summary="x"),
                    ArticleCandidate(url="https://example.com/b", title="B", summary="y"),
                ],
            )
        # Only B is new; A is reported as a skipped duplicate.
        assert result.skipped_duplicate_urls == ["https://example.com/a"]
        assert len(result.new_suggestion_ids) == 1
        # Existing row is unchanged.
        existing.refresh_from_db()
        assert existing.decision == PendingArticleSuggestion.DECISION_PENDING

    def test_empty_batch_does_not_dm(self) -> None:
        with mock.patch(
            "teatree.core.article_ingestion_gate._notify_user",
            return_value=True,
        ) as notify_mock:
            result = enqueue_candidates_and_notify([])
        notify_mock.assert_not_called()
        assert result.dm_sent is False
        assert result.new_suggestion_ids == []


class TestApproveAndCreateTicket:
    def test_approve_calls_gh_with_label_and_stamps_url(self) -> None:
        row = PendingArticleSuggestion.record(
            url="https://example.com/a",
            summary="why this is interesting",
            title="An article",
        )
        with mock.patch(
            "teatree.core.article_ingestion_gate._gh_issue_create",
            return_value="https://github.com/souliane/teatree/issues/9999",
        ) as gh_mock:
            consumed = approve_and_create_ticket(row.pk, decider_id="adrien")
        assert consumed is not None
        assert consumed.decision == PendingArticleSuggestion.DECISION_APPROVED
        assert consumed.created_ticket_url == "https://github.com/souliane/teatree/issues/9999"
        gh_mock.assert_called_once()
        kwargs = gh_mock.call_args.kwargs
        assert kwargs["repo"] == APPROVED_ISSUE_REPO
        assert kwargs["label"] == APPROVED_ISSUE_LABEL
        # The body cites the source URL — preserves the dedupe contract
        # the legacy skill relied on.
        assert "https://example.com/a" in kwargs["body"]

    def test_approve_missing_row_returns_none(self) -> None:
        assert approve_and_create_ticket(999_999) is None

    def test_approve_already_decided_row_returns_none_and_does_not_call_gh(self) -> None:
        row = PendingArticleSuggestion.record(url="https://example.com/a", summary="x")
        PendingArticleSuggestion.reject(row.pk, decider_id="adrien", reason="dup")
        with mock.patch("teatree.core.article_ingestion_gate._gh_issue_create") as gh_mock:
            assert approve_and_create_ticket(row.pk) is None
        gh_mock.assert_not_called()
