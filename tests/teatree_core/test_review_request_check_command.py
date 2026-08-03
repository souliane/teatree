"""``review_request_check check`` — CLI dedup gate (#1084).

Backs ``t3 review-request check --mr-url <url>``: the agent runs this in
the SAME turn as a review-request post and aborts on SUPPRESS.
"""

import contextlib
import io
import json
import os
from collections.abc import Iterator
from typing import cast
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.backends.slack import http as slack_http
from teatree.core.backend_protocols import DraftState
from teatree.core.gates.review_request_guard import GuardDecision, GuardTarget
from teatree.core.models import ConfigSetting, ReviewRequestPost
from tests.teatree_core.test_review_request_guard import FakeClient

_MR_URL = "https://gitlab.com/org/repo/-/merge_requests/385"
_FORGE = "teatree.core.backend_factory.code_host_from_overlay"


class _NonDraftHost:
    def fetch_pr_draft_state(self, *, slug: str, pr_id: int) -> DraftState:
        _ = (slug, pr_id)
        return DraftState.NOT_DRAFT


@pytest.fixture(autouse=True)
def _forge_answers_non_draft() -> Iterator[None]:
    """Default every case to a forge that CONFIRMS the MR is not a draft.

    The draft gate fails closed, so with no forge to answer, every command here
    would refuse ``draft_state_unknown`` and drown the behaviour under test.
    Draft-gate cases re-patch this same target with their own host.
    """
    with patch(_FORGE, return_value=_NonDraftHost()):
        yield


class TestReviewExemptRepoIsRefusedFirst(TestCase):
    """A repo the owner reviews in person: refused ahead of every other gate.

    Exit 2 rather than a returned dict, because the verdict is permanent — an
    unattended caller must branch on it, not re-run. The forge patch is a
    tripwire: reaching it at all would mean the refusal came too late.
    """

    def _run(self) -> tuple[int, dict[str, object]]:
        buf = io.StringIO()
        code = 0
        with contextlib.redirect_stdout(buf):
            try:
                call_command("review_request_check", "--mr-url", _MR_URL)
            except SystemExit as exc:
                code = int(exc.code) if isinstance(exc.code, int) else 1
        payload: dict[str, object] = {}
        for raw in buf.getvalue().splitlines():
            # Only a JSON object counts: a non-refusal run returns the dict, which
            # django-typer renders as a python repr — unparsable here, and never a
            # payload. Skipping it keeps a broken refusal failing on the assertion
            # rather than on a decode error.
            with contextlib.suppress(ValueError):
                decoded = json.loads(raw.strip())
                if isinstance(decoded, dict):
                    payload = decoded
        return code, payload

    def test_refuses_with_exit_two_and_takes_no_claim(self) -> None:
        ConfigSetting.objects.set_value("review_exempt_repos", ["org/repo"])

        code, payload = self._run()

        assert code == 2
        assert payload["reason"] == "review_exempt_repo"
        assert payload["action"] == "refused"
        assert payload["mr_url"] == _MR_URL
        assert ReviewRequestPost.objects.filter(mr_url=_MR_URL).count() == 0

    def test_refuses_before_the_draft_probe_and_the_channel_resolve(self) -> None:
        ConfigSetting.objects.set_value("review_exempt_repos", ["org/repo"])

        with (
            patch(
                "teatree.core.management.commands.review_request_check.draft_refusal_reason",
                side_effect=AssertionError("the draft probe must not run for an exempt repo"),
            ),
            patch(
                "teatree.core.management.commands.review_request_check.resolve_guard_target",
                side_effect=AssertionError("the channel must not resolve for an exempt repo"),
            ),
        ):
            code, payload = self._run()

        assert (code, payload["reason"]) == (2, "review_exempt_repo")

    def test_a_repo_outside_the_declared_patterns_still_checks(self) -> None:
        """The control: an undeclared repo must reach the ordinary dedup decision."""
        ConfigSetting.objects.set_value("review_exempt_repos", ["other-org/other-repo"])
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
            result = cast("dict[str, object]", call_command("review_request_check", "--mr-url", _MR_URL))

        assert result["action"] == "post"


class TestReviewRequestCheckCommand(TestCase):
    def test_refuses_a_draft_mr_before_the_dedup_gate(self) -> None:
        with patch(
            "teatree.core.management.commands.review_request_check.draft_refusal_reason",
            return_value="draft_mr",
        ):
            result = cast(
                "dict[str, object]",
                call_command("review_request_check", "--mr-url", _MR_URL),
            )
        assert result["action"] == "refused"
        assert result["reason"] == "draft_mr"
        assert result["mr_url"] == _MR_URL

    def test_refuses_when_the_draft_state_cannot_be_read(self) -> None:
        """An unanswerable probe must refuse, not predict ``post`` (fail CLOSED).

        ``check`` is the same-turn predictor the agent aborts on; answering
        ``post`` for an MR whose draft flag was never read tells the agent to
        broadcast an MR the user may have marked Draft to hold the batch.
        """

        class _UnreachableHost:
            def fetch_pr_draft_state(self, *, slug: str, pr_id: int) -> object:
                _ = (slug, pr_id)
                raise FileNotFoundError(2, "No such file or directory", "glab")

        with patch(_FORGE, return_value=_UnreachableHost()):
            result = cast(
                "dict[str, object]",
                call_command("review_request_check", "--mr-url", _MR_URL),
            )
        assert result["action"] == "refused"
        assert result["reason"] == "draft_state_unknown"
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
