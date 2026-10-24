"""Issue judgment records stay private and describe the live backlog."""

from django.test import TestCase

from teatree.core.issue_hygiene import CreateDecision, CreateOutcome, IssueDraft
from teatree.core.issue_writes.issue_judgment_audit import record_issue_judgment
from teatree.core.models import SendAudit


class TestIssueJudgmentAudit(TestCase):
    def test_records_live_fit_and_rejection_reasons_only(self) -> None:
        fitting = "https://gitlab.com/acme/widgets/-/issues/1"
        rejected = "https://gitlab.com/acme/widgets/-/issues/2"
        record_issue_judgment(
            draft=IssueDraft(repo="acme/widgets", title="t", body="b"),
            outcome=CreateOutcome(kind="extended_existing", issue_url=fitting),
            decisions=[
                CreateDecision(candidate_url=fitting, fits=True, reason="  "),
                CreateDecision(candidate_url=rejected, reason="  different topic  "),
                CreateDecision(candidate_url="https://gitlab.com/acme/widgets/-/issues/3", reason="stale"),
            ],
            live_urls=(fitting, rejected),
        )
        row = SendAudit.objects.get(action="issue_create_judgment")
        assert row.destination == "private-judgment:gitlab:acme/widgets"
        assert "2 candidates judged" in row.payload_summary
        assert f"{fitting} fits: caller marked as fitting" in row.payload_summary
        assert f"{rejected} rejected: different topic" in row.payload_summary
        assert "issues/3" not in row.payload_summary

    def test_the_drafts_forge_names_the_destination_when_the_url_does_not(self) -> None:
        url = "https://git.example.com/acme/widgets/-/issues/1"
        record_issue_judgment(
            draft=IssueDraft(repo="acme/widgets", title="t", body="b", forge="gitlab"),
            outcome=CreateOutcome(kind="created_new", issue_url=url),
            decisions=[],
            live_urls=(),
        )
        assert (
            SendAudit.objects.get(action="issue_create_judgment").destination == "private-judgment:gitlab:acme/widgets"
        )

    def test_missing_rejection_reason_is_not_labeled_as_a_fit(self) -> None:
        fitting = "https://gitlab.com/acme/widgets/-/issues/1"
        rejected = "https://gitlab.com/acme/widgets/-/issues/2"
        record_issue_judgment(
            draft=IssueDraft(repo="acme/widgets", title="t", body="b"),
            outcome=CreateOutcome(kind="extended_existing", issue_url=fitting),
            decisions=[
                CreateDecision(candidate_url=fitting, fits=True),
                CreateDecision(candidate_url=rejected, reason=" \n"),
            ],
            live_urls=(fitting, rejected),
        )
        row = SendAudit.objects.get(action="issue_create_judgment")
        assert f"{rejected} rejected: caller supplied no rejection reason" in row.payload_summary
