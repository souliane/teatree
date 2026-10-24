"""Issue judgment records stay private and describe the live backlog."""

from django.test import TestCase

from teatree.core.issue_hygiene import CreateDecision, CreateOutcome, IssueDraft
from teatree.core.issue_judgment_audit import record_issue_judgment
from teatree.core.models import SendAudit


class TestIssueJudgmentAudit(TestCase):
    def test_records_live_fit_and_rejection_reasons_only(self) -> None:
        fitting = "https://gitlab.com/acme/widgets/-/issues/1"
        rejected = "https://gitlab.com/acme/widgets/-/issues/2"
        record_issue_judgment(
            draft=IssueDraft(repo="acme/widgets", title="t", body="b"),
            outcome=CreateOutcome(kind="extended_existing", issue_url=fitting),
            decisions=[
                CreateDecision(candidate_url=fitting, fits=True),
                CreateDecision(candidate_url=rejected, reason="different topic"),
                CreateDecision(candidate_url="https://gitlab.com/acme/widgets/-/issues/3", reason="stale"),
            ],
            live_urls=(fitting, rejected),
        )
        row = SendAudit.objects.get(action="issue_create_judgment")
        assert "2 candidates judged" in row.payload_summary
        assert f"{fitting} fits: caller marked as fitting" in row.payload_summary
        assert f"{rejected} rejected: different topic" in row.payload_summary
        assert "issues/3" not in row.payload_summary

    def test_missing_rejection_reason_is_not_labeled_as_a_fit(self) -> None:
        fitting = "https://gitlab.com/acme/widgets/-/issues/1"
        rejected = "https://gitlab.com/acme/widgets/-/issues/2"
        record_issue_judgment(
            draft=IssueDraft(repo="acme/widgets", title="t", body="b"),
            outcome=CreateOutcome(kind="extended_existing", issue_url=fitting),
            decisions=[
                CreateDecision(candidate_url=fitting, fits=True),
                CreateDecision(candidate_url=rejected),
            ],
            live_urls=(fitting, rejected),
        )
        row = SendAudit.objects.get(action="issue_create_judgment")
        assert f"{rejected} rejected: caller supplied no rejection reason" in row.payload_summary
