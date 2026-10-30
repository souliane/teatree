"""Review activity claims for both supported Git object formats."""

from django.test import TestCase

from teatree.core.models import RedMrFixAttempt


class TestReviewActivityClaim(TestCase):
    def test_sha256_head_claims_once_and_round_trips(self) -> None:
        url = "https://gitlab.example/group/repo/-/merge_requests/9"
        head = "a" * 64

        row = RedMrFixAttempt.claim_activity(pr_url=url, head_sha=head, note_id=42)

        assert row is not None
        assert RedMrFixAttempt.objects.get(pk=row.pk).review_head_sha == head
        assert RedMrFixAttempt.claim_activity(pr_url=url, head_sha=head, note_id=42) is None
