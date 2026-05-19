"""``review_request_check check`` — CLI dedup gate (#1084).

Backs ``t3 review-request check --mr-url <url>``: the agent runs this in
the SAME turn as a review-request post and aborts on SUPPRESS.
"""

from typing import cast
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import ReviewRequestPost
from teatree.core.review_request_guard import GuardDecision, GuardTarget

_MR_URL = "https://gitlab.com/org/repo/-/merge_requests/385"


class TestReviewRequestCheckCommand(TestCase):
    def test_suppresses_when_no_review_channel_or_token(self) -> None:
        with patch(
            "teatree.core.management.commands.review_request_check.resolve_guard_target",
            return_value=None,
        ):
            result = cast(
                "dict[str, object]",
                call_command("review_request_check", "--mr-url", _MR_URL),
            )
        assert result["action"] == "suppress"
        assert result["reason"] == "no_review_channel_or_token"

    def test_passes_through_post_decision(self) -> None:
        target = GuardTarget(channel_id="C1", channel_name="rev", token="xoxb")
        with (
            patch(
                "teatree.core.management.commands.review_request_check.resolve_guard_target",
                return_value=target,
            ),
            patch(
                "teatree.core.management.commands.review_request_check.should_post_review_request",
                return_value=GuardDecision(action="post"),
            ),
        ):
            result = cast(
                "dict[str, object]",
                call_command("review_request_check", "--mr-url", _MR_URL),
            )
        assert result["action"] == "post"
        assert result["mr_url"] == _MR_URL

    def test_passes_through_suppress_with_permalink(self) -> None:
        target = GuardTarget(channel_id="C1", channel_name="rev", token="xoxb")
        decision = GuardDecision(
            action="suppress",
            permalink="https://team.slack.com/archives/C1/p1",
            author="U_HUMAN",
            reason="already_posted",
        )
        with (
            patch(
                "teatree.core.management.commands.review_request_check.resolve_guard_target",
                return_value=target,
            ),
            patch(
                "teatree.core.management.commands.review_request_check.should_post_review_request",
                return_value=decision,
            ),
        ):
            result = cast(
                "dict[str, object]",
                call_command("review_request_check", "--mr-url", _MR_URL),
            )
        assert result["action"] == "suppress"
        assert result["permalink"] == "https://team.slack.com/archives/C1/p1"
        assert result["author"] == "U_HUMAN"
        assert result["reason"] == "already_posted"
        assert ReviewRequestPost.objects.filter(mr_url=_MR_URL).count() == 0
