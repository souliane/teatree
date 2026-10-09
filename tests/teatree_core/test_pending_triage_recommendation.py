"""Tests for the needs-triage assessment ask-gate queue.

``PendingTriageRecommendation`` is the durable ask-gate for the triage-assessor
loop: a shell-denied assessor agent returns a keep/close/needs_info verdict per
OPEN ``needs-triage`` issue, and the recorder persists one PENDING row per issue.
Nothing acts autonomously — an interactive skill approves/rejects each row and
runs ``gh`` on approval. Re-assessing the same issue URL must not enqueue a
duplicate candidate, and an unknown verdict fails closed (dropped, never stored).
"""

from django.test import TestCase
from django.utils import timezone

from teatree.core.models import PendingTriageRecommendation

_URL = "https://github.com/souliane/teatree/issues/4242"


class PendingTriageRecommendationTests(TestCase):
    def test_record_candidate_creates_pending_row(self) -> None:
        """A recommendation is enqueued as PENDING — nothing is acted on."""
        row = PendingTriageRecommendation.record_candidate(
            issue_url=_URL,
            title="Flaky login test",
            verdict="close",
            suggested_labels=["stale"],
            priority="low",
            duplicate_of="https://github.com/souliane/teatree/issues/1",
            rationale="Superseded by #1",
            overlay="t3-teatree",
        )

        assert row is not None
        assert row.status == PendingTriageRecommendation.Status.PENDING
        assert row.issue_url == _URL
        assert row.title == "Flaky login test"
        assert row.verdict == "close"
        assert row.suggested_labels == ["stale"]
        assert row.priority == "low"
        assert row.duplicate_of == "https://github.com/souliane/teatree/issues/1"
        assert row.rationale == "Superseded by #1"
        assert row.overlay == "t3-teatree"
        # Nothing is acted on at record time — default is no-op.
        assert row.action_taken == ""
        assert row.decided_at is None

    def test_record_candidate_is_idempotent_by_issue_url(self) -> None:
        """Re-assessing the same issue URL does not enqueue a duplicate."""
        first = PendingTriageRecommendation.record_candidate(issue_url=_URL, verdict="keep")
        assert first is not None

        second = PendingTriageRecommendation.record_candidate(issue_url=_URL, verdict="close")

        assert second is None
        assert PendingTriageRecommendation.objects.filter(url_hash=first.url_hash).count() == 1

    def test_dedup_survives_a_decided_row(self) -> None:
        """A previously approved/rejected issue is not re-enqueued on the next assessment."""
        first = PendingTriageRecommendation.record_candidate(issue_url=_URL, verdict="keep")
        assert first is not None
        first.reject()

        again = PendingTriageRecommendation.record_candidate(issue_url=_URL, verdict="close")

        assert again is None
        assert PendingTriageRecommendation.objects.count() == 1

    def test_blank_issue_url_is_not_enqueued(self) -> None:
        assert PendingTriageRecommendation.record_candidate(issue_url="   ", verdict="keep") is None
        assert PendingTriageRecommendation.objects.count() == 0

    def test_unknown_verdict_is_dropped_fail_closed(self) -> None:
        """A verdict outside keep/close/needs_info is dropped — never stored."""
        row = PendingTriageRecommendation.record_candidate(issue_url=_URL, verdict="delete-everything")

        assert row is None
        assert PendingTriageRecommendation.objects.count() == 0

    def test_valid_verdict_is_recorded_the_anti_vacuity_pair(self) -> None:
        for verdict in ("keep", "close", "needs_info"):
            PendingTriageRecommendation.objects.all().delete()
            row = PendingTriageRecommendation.record_candidate(issue_url=_URL, verdict=verdict)
            assert row is not None
            assert row.verdict == verdict

    def test_approve_marks_row_and_records_action(self) -> None:
        """Approval is the only path that authorizes acting — stamps the action taken."""
        row = PendingTriageRecommendation.record_candidate(issue_url=_URL, verdict="close")
        assert row is not None
        before = timezone.now()

        row.approve(action_taken="closed via gh issue close")

        row.refresh_from_db()
        assert row.status == PendingTriageRecommendation.Status.APPROVED
        assert row.action_taken == "closed via gh issue close"
        assert row.decided_at is not None
        assert row.decided_at >= before

    def test_reject_marks_row_without_action(self) -> None:
        row = PendingTriageRecommendation.record_candidate(issue_url=_URL, verdict="close")
        assert row is not None

        row.reject()

        row.refresh_from_db()
        assert row.status == PendingTriageRecommendation.Status.REJECTED
        assert row.action_taken == ""
        assert row.decided_at is not None

    def test_hash_url_is_stable_and_whitespace_insensitive(self) -> None:
        assert PendingTriageRecommendation.hash_url(_URL) == PendingTriageRecommendation.hash_url(f"  {_URL}  ")

    def test_str_names_status_and_verdict(self) -> None:
        row = PendingTriageRecommendation.record_candidate(issue_url=_URL, verdict="close", title="Flaky login test")
        assert row is not None
        rendered = str(row)
        assert "pending" in rendered
        assert "close" in rendered
