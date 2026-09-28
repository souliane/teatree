"""Tests for the news-scan ask-gate candidate queue (#1391).

``PendingArticleSuggestion`` is the durable ask-gate that replaces the
scanning-news skill's old auto-``gh issue create`` behaviour. The skill
records one PENDING row per candidate article; an issue is filed only
when the user approves. With no approval the row stays PENDING and
nothing is created — default is no-op. Re-scanning the same source URL
must not enqueue a duplicate candidate.
"""

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import PendingArticleSuggestion
from teatree.verification.url_check import UrlCheckResult, UrlCheckStatus

_URL = "https://tldr.tech/ai/2026-05-27#some-agent-eval-harness"


def _resolves(url: str) -> UrlCheckResult:
    return UrlCheckResult(url, UrlCheckStatus.OK, http_status=200)


def _unresolved(url: str) -> UrlCheckResult:
    return UrlCheckResult(url, UrlCheckStatus.UNRESOLVED, http_status=404, detail="HTTP 404")


def _network_error(url: str) -> UrlCheckResult:
    return UrlCheckResult(url, UrlCheckStatus.NETWORK_ERROR, detail="timed out")


class PendingArticleSuggestionTests(TestCase):
    def test_record_candidate_creates_pending_row(self) -> None:
        """A candidate is enqueued as PENDING — not auto-filed."""
        row = PendingArticleSuggestion.record_candidate(
            url=_URL,
            title="An agent eval harness",
            summary="Pattern we lack",
            overlay="t3-teatree",
            url_checker=_resolves,
        )

        assert row is not None
        assert row.status == PendingArticleSuggestion.Status.PENDING
        assert row.url == _URL
        assert row.title == "An agent eval harness"
        assert row.overlay == "t3-teatree"
        # No issue is filed at record time — default is no-op.
        assert row.issue_url == ""
        assert row.decided_at is None

    def test_record_candidate_is_idempotent_by_url(self) -> None:
        """Re-scanning the same article URL does not enqueue a duplicate."""
        first = PendingArticleSuggestion.record_candidate(url=_URL, url_checker=_resolves)
        assert first is not None

        second = PendingArticleSuggestion.record_candidate(url=_URL, url_checker=_resolves)

        # Dedup by URL hash — the second scan returns None (already queued).
        assert second is None
        assert PendingArticleSuggestion.objects.filter(url_hash=first.url_hash).count() == 1

    def test_rescan_of_queued_candidate_skips_the_url_probe(self) -> None:
        """Dedup-first: a re-scan of an already-queued article does not re-probe."""
        calls: list[str] = []

        def _counting_checker(url: str) -> UrlCheckResult:
            calls.append(url)
            return UrlCheckResult(url, UrlCheckStatus.OK, http_status=200)

        first = PendingArticleSuggestion.record_candidate(url=_URL, url_checker=_counting_checker)
        assert first is not None

        second = PendingArticleSuggestion.record_candidate(url=_URL, url_checker=_counting_checker)

        assert second is None
        # Probed once — for the genuinely-new candidate — not again on the re-scan.
        assert calls == [_URL]

    def test_record_candidate_dedup_survives_a_decided_row(self) -> None:
        """A previously approved/rejected URL is not re-enqueued on the next scan."""
        first = PendingArticleSuggestion.record_candidate(url=_URL, url_checker=_resolves)
        assert first is not None
        first.reject()

        again = PendingArticleSuggestion.record_candidate(url=_URL, url_checker=_resolves)

        assert again is None
        assert PendingArticleSuggestion.objects.count() == 1

    def test_blank_url_is_not_enqueued(self) -> None:
        """A blank URL never produces a candidate row."""
        assert PendingArticleSuggestion.record_candidate(url="   ") is None
        assert PendingArticleSuggestion.objects.count() == 0

    def test_unresolved_url_is_dropped(self) -> None:
        """A fabricated / 404 URL is dropped — no candidate row (PR-15)."""
        result = PendingArticleSuggestion.record_candidate(url=_URL, url_checker=_unresolved)

        assert result is None
        assert PendingArticleSuggestion.objects.count() == 0

    def test_resolving_url_is_recorded(self) -> None:
        """A URL that resolves is recorded — the anti-vacuity pair for the drop."""
        result = PendingArticleSuggestion.record_candidate(url=_URL, url_checker=_resolves)

        assert result is not None
        assert PendingArticleSuggestion.objects.count() == 1

    def test_network_error_records_anyway(self) -> None:
        """A transient network failure never drops a possibly-real article (fail-open)."""
        result = PendingArticleSuggestion.record_candidate(url=_URL, url_checker=_network_error)

        assert result is not None
        assert result.status == PendingArticleSuggestion.Status.PENDING

    def test_approve_marks_row_and_records_issue_url(self) -> None:
        """Approval is the only path that authorizes filing — stamps the issue URL."""
        row = PendingArticleSuggestion.record_candidate(url=_URL, url_checker=_resolves)
        assert row is not None
        before = timezone.now()

        row.approve(issue_url="https://github.com/souliane/teatree/issues/9999")

        row.refresh_from_db()
        assert row.status == PendingArticleSuggestion.Status.APPROVED
        assert row.issue_url == "https://github.com/souliane/teatree/issues/9999"
        assert row.decided_at is not None
        assert row.decided_at >= before

    def test_reject_marks_row_without_issue(self) -> None:
        """Rejection records the decision and never files an issue."""
        row = PendingArticleSuggestion.record_candidate(url=_URL, url_checker=_resolves)
        assert row is not None

        row.reject()

        row.refresh_from_db()
        assert row.status == PendingArticleSuggestion.Status.REJECTED
        assert row.issue_url == ""
        assert row.decided_at is not None

    def test_hash_url_is_stable_and_whitespace_insensitive(self) -> None:
        """The dedup hash ignores surrounding whitespace on the URL."""
        assert PendingArticleSuggestion.hash_url(_URL) == PendingArticleSuggestion.hash_url(f"  {_URL}  ")

    def test_str_names_status_and_title(self) -> None:
        """The repr surfaces pk, status, and a title slice for admin/log readability."""
        row = PendingArticleSuggestion.record_candidate(url=_URL, title="An agent eval harness", url_checker=_resolves)
        assert row is not None
        rendered = str(row)
        assert "pending" in rendered
        assert "An agent eval harness" in rendered

    def test_record_candidate_defaults_to_news_article_kind(self) -> None:
        row = PendingArticleSuggestion.record_candidate(url=_URL, url_checker=_resolves)
        assert row is not None
        assert row.kind == PendingArticleSuggestion.Kind.NEWS_ARTICLE


class RecordReconciliationCandidateTests(TestCase):
    """The dream loop's conflicting-BINDING-memory-pair candidate (#2663 dream-batch dea750a552f8f2d2).

    Reuses this ask-gate rather than auto-filing an issue.
    """

    def test_records_a_pending_reconciliation_row(self) -> None:
        row = PendingArticleSuggestion.record_reconciliation_candidate(
            key="feedback_bind_one+feedback_bind_two",
            title="Conflicting BINDING memories need reconciliation",
            summary="please decide which rule is canonical",
        )

        assert row is not None
        assert row.status == PendingArticleSuggestion.Status.PENDING
        assert row.kind == PendingArticleSuggestion.Kind.MEMORY_RECONCILIATION
        assert row.url == ""
        assert row.title == "Conflicting BINDING memories need reconciliation"

    def test_is_idempotent_by_key(self) -> None:
        key = "feedback_bind_one+feedback_bind_two"
        first = PendingArticleSuggestion.record_reconciliation_candidate(key=key, title="t", summary="s")
        assert first is not None

        second = PendingArticleSuggestion.record_reconciliation_candidate(key=key, title="t", summary="s")

        assert second is None
        assert PendingArticleSuggestion.objects.count() == 1

    def test_a_reconciliation_key_and_a_news_url_never_collide(self) -> None:
        """Two independent dedup namespaces sharing one hash column."""
        PendingArticleSuggestion.record_candidate(url=_URL, url_checker=_resolves)
        PendingArticleSuggestion.record_reconciliation_candidate(key=_URL, title="t", summary="s")

        assert PendingArticleSuggestion.objects.count() == 2

    def test_blank_key_is_a_no_op(self) -> None:
        assert PendingArticleSuggestion.record_reconciliation_candidate(key="   ", title="t", summary="s") is None
        assert PendingArticleSuggestion.objects.count() == 0
