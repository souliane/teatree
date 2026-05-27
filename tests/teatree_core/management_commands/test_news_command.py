"""Tests for ``t3 manage news`` (pending / approve / reject) — #1391."""

from io import StringIO
from unittest import mock

import pytest
from django.core.management import call_command

from teatree.core.models import PendingArticleSuggestion

pytestmark = pytest.mark.django_db


def _call(*args: str) -> str:
    buf = StringIO()
    call_command(*args, stdout=buf)
    return buf.getvalue()


class TestNewsPendingCommand:
    def test_pending_lists_only_undecided_rows(self) -> None:
        a = PendingArticleSuggestion.record(url="https://example.com/a", summary="why A", title="A")
        PendingArticleSuggestion.record(url="https://example.com/b", summary="why B", title="B")
        c = PendingArticleSuggestion.record(url="https://example.com/c", summary="why C", title="C")
        PendingArticleSuggestion.reject(c.pk, reason="dup")

        out = _call("news", "pending")
        assert f"#{a.pk}" in out
        assert f"#{c.pk}" not in out
        assert "A" in out
        assert "https://example.com/a" in out

    def test_pending_with_all_includes_decided(self) -> None:
        a = PendingArticleSuggestion.record(url="https://example.com/a", summary="why A")
        PendingArticleSuggestion.reject(a.pk, reason="dup")
        out = _call("news", "pending", "--all")
        assert f"#{a.pk}" in out
        assert "rejected" in out

    def test_pending_handles_empty_queue(self) -> None:
        out = _call("news", "pending")
        assert "no pending article suggestions" in out


class TestNewsApproveCommand:
    def test_approve_calls_gh_and_stamps_url(self) -> None:
        row = PendingArticleSuggestion.record(
            url="https://example.com/a",
            summary="why this article matters",
            title="An interesting article",
        )
        with mock.patch(
            "teatree.core.article_ingestion_gate._gh_issue_create",
            return_value="https://github.com/souliane/teatree/issues/9001",
        ) as gh_mock:
            out = _call("news", "approve", str(row.pk), "--decider", "adrien")
        gh_mock.assert_called_once()
        row.refresh_from_db()
        assert row.decision == PendingArticleSuggestion.DECISION_APPROVED
        assert row.created_ticket_url == "https://github.com/souliane/teatree/issues/9001"
        assert "9001" in out

    def test_approve_unknown_id_exits_nonzero(self) -> None:
        with pytest.raises(SystemExit):
            _call("news", "approve", "999999")


class TestNewsRejectCommand:
    def test_reject_stamps_decision_with_reason(self) -> None:
        row = PendingArticleSuggestion.record(url="https://example.com/a", summary="why")
        _call("news", "reject", str(row.pk), "--reason", "copycat article")
        row.refresh_from_db()
        assert row.decision == PendingArticleSuggestion.DECISION_REJECTED
        assert row.decision_reason == "copycat article"

    def test_reject_unknown_id_exits_nonzero(self) -> None:
        with pytest.raises(SystemExit):
            _call("news", "reject", "999999")
