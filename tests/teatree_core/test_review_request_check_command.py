"""``review_request_check check`` — CLI dedup gate (#1084).

Backs ``t3 review-request check --mr-url <url>``: the agent runs this in
the SAME turn as a review-request post and aborts on SUPPRESS.
"""

import os
from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.backends.slack import http as slack_http
from teatree.core.gates.review_request_guard import GuardDecision, GuardTarget
from teatree.core.models import ReviewRequestPost
from tests.teatree_core.test_review_request_guard import FakeClient

_MR_URL = "https://gitlab.com/org/repo/-/merge_requests/385"


class TestReviewRequestCheckCommand(TestCase):
    def test_refuses_a_draft_mr_before_the_dedup_gate(self) -> None:
        with patch(
            "teatree.core.management.commands.review_request_check.is_draft_mr",
            return_value=True,
        ):
            result = cast(
                "dict[str, object]",
                call_command("review_request_check", "--mr-url", _MR_URL),
            )
        assert result["action"] == "refused"
        assert result["reason"] == "draft_mr"
        assert result["mr_url"] == _MR_URL

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

    def test_threads_the_url_owning_overlay_into_the_guard(self) -> None:
        """With no ``T3_OVERLAY_NAME`` the overlay comes from the MR URL (#1310).

        The MCP surface runs this command IN-PROCESS: it sets no env var and
        every overlay is registered, so the guard resolved no overlay at all
        and answered ``suppress`` / ``no_review_channel_or_token`` while
        ``t3 review-request check`` — whose CLI bridge exports the env var —
        answered truthfully on the very same MR. The two surfaces must agree.
        """
        seen: dict[str, str] = {}
        target = GuardTarget(channel_id="C1", channel_name="rev", token="xoxb")

        with (
            patch.dict(os.environ),
            patch(
                "teatree.core.management.commands.review_request_check.overlay_for_mr_url",
                return_value="acme",
            ),
            patch(
                "teatree.core.management.commands.review_request_check.resolve_guard_target",
                side_effect=lambda **kw: (seen.update(kw), target)[1],
            ),
            patch(
                "teatree.core.management.commands.review_request_check.peek_should_post_review_request",
                return_value=GuardDecision(action="post"),
            ),
        ):
            os.environ.pop("T3_OVERLAY_NAME", None)
            result = cast(
                "dict[str, object]",
                call_command("review_request_check", "--mr-url", _MR_URL),
            )

        assert seen["overlay_name"] == "acme"
        assert result["action"] == "post"

    def test_explicit_env_overlay_defers_to_the_cli_bridge(self) -> None:
        """``T3_OVERLAY_NAME`` wins: pass ``""`` so ``get_overlay`` consumes it."""
        seen: dict[str, str] = {}
        target = GuardTarget(channel_id="C1", channel_name="rev", token="xoxb")

        with (
            patch.dict(os.environ, {"T3_OVERLAY_NAME": "acme"}),
            patch(
                "teatree.core.gates.review_request_guard.infer_overlay_for_url",
                side_effect=AssertionError("must not infer when the env var is set"),
            ),
            patch(
                "teatree.core.management.commands.review_request_check.resolve_guard_target",
                side_effect=lambda **kw: (seen.update(kw), target)[1],
            ),
            patch(
                "teatree.core.management.commands.review_request_check.peek_should_post_review_request",
                return_value=GuardDecision(action="post"),
            ),
        ):
            call_command("review_request_check", "--mr-url", _MR_URL)

        assert seen["overlay_name"] == ""

    def test_passes_through_post_decision(self) -> None:
        target = GuardTarget(channel_id="C1", channel_name="rev", token="xoxb")
        with (
            patch(
                "teatree.core.management.commands.review_request_check.resolve_guard_target",
                return_value=target,
            ),
            patch(
                "teatree.core.management.commands.review_request_check.peek_should_post_review_request",
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
                "teatree.core.management.commands.review_request_check.peek_should_post_review_request",
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

    def test_check_leaves_no_durable_row(self) -> None:
        """Decision-only: a clean live scan must NOT persist a claim (#1103).

        Pre-#1103 the command called ``should_post_review_request`` which
        takes the durable ``ReviewRequestPost`` ``get_or_create`` claim;
        running ``check`` (which never posts) left an orphan row that then
        wedged every later real post on ``already_claimed``. RED on main
        (count == 1); GREEN once ``check`` peeks instead of claiming.
        """
        target = GuardTarget(channel_id="C1", channel_name="rev", token="xoxb")
        fake = FakeClient(pages=[{"ok": True, "messages": [], "has_more": False}])
        with (
            patch(
                "teatree.core.management.commands.review_request_check.resolve_guard_target",
                return_value=target,
            ),
            pytest.MonkeyPatch.context() as mp,
        ):
            mp.setattr(slack_http.httpx, "get", fake.get)
            result = cast(
                "dict[str, object]",
                call_command("review_request_check", "--mr-url", _MR_URL),
            )
        assert result["action"] == "post"
        assert ReviewRequestPost.objects.filter(mr_url=_MR_URL).count() == 0
