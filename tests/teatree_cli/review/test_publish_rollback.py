"""A publish body that REPORTS failure rolls the on-behalf consume + audit back (#1879 class).

The publish bodies signal failure by returning ``(message, 1)`` rather than
raising — the GitLab POST returning a falsy body is exactly that shape. Only a
raise used to roll the enclosing ``transaction.atomic`` back, so a returned
failure committed the single-use approval's consume and wrote an audit for a post
that never landed: the operator's approval was burned and the ledger lied.
"""

import pytest
from django.test import TestCase

from teatree.cli.review.default_draft import publish_live_post
from teatree.cli.review.on_behalf import publish_or_blocked, publish_or_blocked_issue
from teatree.cli.review.service import ReviewService
from teatree.config import OnBehalfPostMode
from teatree.core.models import ConfigSetting, LivePostApproval, OnBehalfApproval, OnBehalfAudit

_APPROVER = "human-operator"


class TestReturnedFailureRollsBackTheApproval(TestCase):
    @pytest.fixture(autouse=True)
    def _blocking_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for env in ("T3_OVERLAY_NAME", "T3_ON_BEHALF_POST_MODE", "T3_ON_BEHALF_AUTO_ACTIONS"):
            monkeypatch.delenv(env, raising=False)
        ConfigSetting.objects.set_value("on_behalf_post_mode", OnBehalfPostMode.ASK.value)

    def test_a_failed_post_leaves_the_approval_unconsumed_and_writes_no_audit(self) -> None:
        OnBehalfApproval.record("acme/alpha!7", "post_comment", _APPROVER)

        message, code = publish_or_blocked("acme/alpha", 7, "post_comment", lambda: ("Failed to post comment", 1))

        assert (message, code) == ("Failed to post comment", 1)
        assert OnBehalfApproval.objects.filter(consumed_at__isnull=True).count() == 1
        assert OnBehalfAudit.objects.count() == 0

    def test_a_successful_post_still_consumes_the_approval_and_audits(self) -> None:
        OnBehalfApproval.record("acme/alpha!7", "post_comment", _APPROVER)

        message, code = publish_or_blocked("acme/alpha", 7, "post_comment", lambda: ("OK note_id=1", 0))

        assert (message, code) == ("OK note_id=1", 0)
        assert OnBehalfApproval.objects.filter(consumed_at__isnull=False).count() == 1
        assert OnBehalfAudit.objects.count() == 1

    def test_the_issue_scoped_twin_rolls_back_identically(self) -> None:
        OnBehalfApproval.record("acme/alpha#7", "post_issue_comment", _APPROVER)

        message, code = publish_or_blocked_issue(
            "acme/alpha", 7, "post_issue_comment", lambda: ("Failed to post comment", 1)
        )

        assert (message, code) == ("Failed to post comment", 1)
        assert OnBehalfApproval.objects.filter(consumed_at__isnull=True).count() == 1
        assert OnBehalfAudit.objects.count() == 0


class _FailingPostAPI:
    """A GitLab API whose comment POST returns an empty body — the reported-failure shape."""

    def post_json(self, endpoint: str, payload: object) -> dict[str, object]:
        del endpoint, payload
        return {}

    def get_json(self, endpoint: str) -> object:
        if endpoint.endswith("/approvals"):
            return {"approved_by": []}
        return []


class TestTheSingleUseLivePostTokenSurvivesAFailedPost(TestCase):
    """``--live`` burns the Slack-recorded one-shot token only when the post actually lands."""

    @pytest.fixture(autouse=True)
    def _blocking_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for env in ("T3_OVERLAY_NAME", "T3_ON_BEHALF_POST_MODE", "T3_ON_BEHALF_AUTO_ACTIONS"):
            monkeypatch.delenv(env, raising=False)
        ConfigSetting.objects.set_value("on_behalf_post_mode", OnBehalfPostMode.ASK.value)

    def test_a_failed_live_post_leaves_the_approval_unconsumed(self) -> None:
        OnBehalfApproval.record("org/repo!7", "post_comment", _APPROVER)
        LivePostApproval.record(mr_url="org/repo!7", slack_ts="1700000000.0001", slack_user_id="U-OPERATOR")
        service = ReviewService(token="t")
        service._get_api = _FailingPostAPI  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]

        _msg, code = service.post_comment("org/repo", 7, "lgtm", live=True)

        assert code == 1
        assert LivePostApproval.objects.filter(consumed_at__isnull=True).count() == 1


class TestTheLiveTokenSurvivesAFailedPostUnderImmediateMode(TestCase):
    """IMMEDIATE runs the publish untransacted, so the live-post consume needs its own atomic."""

    @pytest.fixture(autouse=True)
    def _immediate_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for env in ("T3_OVERLAY_NAME", "T3_ON_BEHALF_POST_MODE", "T3_ON_BEHALF_AUTO_ACTIONS"):
            monkeypatch.delenv(env, raising=False)
        ConfigSetting.objects.set_value("on_behalf_post_mode", OnBehalfPostMode.IMMEDIATE.value)

    def test_a_failed_live_post_leaves_the_approval_unconsumed(self) -> None:
        LivePostApproval.record(mr_url="org/repo!7", slack_ts="1700000000.0001", slack_user_id="U-OPERATOR")
        service = ReviewService(token="t")
        service._get_api = _FailingPostAPI  # type: ignore[method-assign]  # ty: ignore[invalid-assignment]

        _msg, code = service.post_comment("org/repo", 7, "lgtm", live=True)

        assert code == 1
        assert LivePostApproval.objects.filter(consumed_at__isnull=True).count() == 1


class _TransportDiedError(RuntimeError):
    """A publish body that blows up mid-post rather than reporting a failure tuple."""


class TestTheLivePostConsumeIsScopedToALandedPost(TestCase):
    def test_a_landed_post_consumes_the_token(self) -> None:
        LivePostApproval.record(mr_url="org/repo!7", slack_ts="1700000000.0001", slack_user_id="U-OPERATOR")

        result = publish_live_post(repo="org/repo", mr=7, publish=lambda: ("OK note_id=1", 0))

        assert result == ("OK note_id=1", 0)
        assert LivePostApproval.objects.filter(consumed_at__isnull=False).count() == 1

    def test_a_raising_post_leaves_the_token_unconsumed(self) -> None:
        LivePostApproval.record(mr_url="org/repo!7", slack_ts="1700000000.0001", slack_user_id="U-OPERATOR")

        def boom() -> tuple[str, int]:
            raise _TransportDiedError

        with pytest.raises(_TransportDiedError):
            publish_live_post(repo="org/repo", mr=7, publish=boom)

        assert LivePostApproval.objects.filter(consumed_at__isnull=True).count() == 1

    def test_a_missing_token_refuses_without_running_the_publish(self) -> None:
        ran: list[str] = []

        def post() -> tuple[str, int]:
            ran.append("posted")
            return "OK note_id=1", 0

        message, code = publish_live_post(repo="org/repo", mr=7, publish=post)

        assert code == 1
        assert "live post blocked" in message
        assert ran == []
