import pytest
from django.contrib.auth import get_user_model
from django.test import TestCase

from teatree.cli.review.operator_target import ReviewRequester, review_requester
from teatree.core.models import OperatorIdentity, Ticket


class TestReviewRequester(TestCase):
    def test_unknown_target_keeps_owner_channel(self) -> None:
        assert review_requester("org/repo!7", owner_id="U-OWNER", owner_channel="D-OWNER") == ReviewRequester(
            "", "U-OWNER", "D-OWNER"
        )

    def test_known_requester_without_slack_identity_refuses_owner_fallback(self) -> None:
        user = get_user_model().objects.create_superuser(username="alice", email="", password=None)
        OperatorIdentity.objects.create(user=user, platform="gitlab", handle="alice", external_id="101")
        Ticket.objects.create(issue_url="https://gitlab.com/org/repo/-/merge_requests/7", requested_by="gitlab:alice")

        with pytest.raises(ValueError, match="no registered Slack identity"):
            review_requester("org/repo!7", owner_id="U-OWNER", owner_channel="D-OWNER")
