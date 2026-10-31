import json
import os
from datetime import timedelta
from unittest.mock import patch

import httpx
from click.testing import Result
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AbstractBaseUser
from django.test import TestCase
from django.utils import timezone
from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli.review.github_operator_approval import publish_github_approval
from teatree.cli.review.operator_approval import OperatorApprover
from teatree.core.models import ConfigSetting, OnBehalfApproval, OnBehalfAudit, OperatorGrant, OperatorIdentity, Ticket
from teatree.core.models.operator_grant import OperatorGrantTokens
from teatree.utils.sealing import MasterKey
from tests.teatree_core._on_behalf_gate_helpers import seed_permitting_posture

_PR = "org/repo!7"


class FakeGitHub:
    """The forge HTTP boundary: records every request and answers like api.github.com."""

    def __init__(
        self, login: str = "alice", *, post_status: int = 200, recorded_state: str = "APPROVED", user_status: int = 200
    ) -> None:
        self.login = login
        self.user_status = user_status
        self.post_status = post_status
        self.recorded_state = recorded_state
        self.requests: list[tuple[str, str, dict[str, str], object]] = []
        self.reviews: list[dict[str, object]] = []

    def request(
        self, method: str, url: str, *, headers: dict[str, str], payload: object = None, timeout: float | None = None
    ) -> httpx.Response:
        _ = timeout
        self.requests.append((method, url, headers, payload))
        request = httpx.Request(method, url)
        path = httpx.URL(url).path
        if method == "POST" and path.endswith("/pulls/7/reviews"):
            if self.post_status != httpx.codes.OK:
                return httpx.Response(
                    self.post_status, json={"message": "Can not approve your own pull request"}, request=request
                )
            self.reviews.append({"user": {"login": self.login}, "state": self.recorded_state})
            return httpx.Response(200, json={"id": 1, "state": self.recorded_state}, request=request)
        if path.endswith("/pulls/7/reviews"):
            return httpx.Response(200, json=self.reviews, request=request)
        if path == "/user":
            if self.user_status != httpx.codes.OK:
                return httpx.Response(self.user_status, json={"message": "Bad credentials"}, request=request)
            return httpx.Response(200, json={"login": self.login}, request=request)
        return httpx.Response(200, json={"requested_reviewers": []}, request=request)

    @property
    def review_posts(self) -> list[dict[str, object]]:
        return [
            json.loads(str(payload))
            for method, url, _headers, payload in self.requests
            if method == "POST" and url.endswith("/pulls/7/reviews")
        ]

    @property
    def bearer_tokens(self) -> set[str]:
        return {headers["Authorization"] for _method, _url, headers, _payload in self.requests}


class TestGitHubOperatorApproval(TestCase):
    def setUp(self) -> None:
        seed_permitting_posture()
        ConfigSetting.objects.create(
            scope="",
            key="operator_oauth_apps",
            value={
                "github": {
                    "client_id": "client",
                    "client_secret_pass_key": "github/oauth-app",
                    "redirect_uri": "https://factory.example/login/oauth/github/callback/",
                }
            },
        )
        self.alice = get_user_model().objects.create_superuser(username="alice", email="", password=None)
        OperatorIdentity.objects.create(user=self.alice, platform="github", handle="alice", external_id="1")
        self.key = MasterKey(b"a" * 32)

    def _grant(self, user: AbstractBaseUser | None = None, *, expires_in: timedelta = timedelta(hours=8)) -> None:
        OperatorGrant.objects.store(
            user=user or self.alice,
            forge="github",
            tokens=OperatorGrantTokens(
                access_token="alice-access",
                refresh_token="alice-refresh",
                expires_at=timezone.now() + expires_in,
                refresh_expires_at=timezone.now() + timedelta(days=180),
            ),
            key=self.key,
        )

    def _invoke(self, forge: FakeGitHub, action: str = "approve") -> Result:
        with (
            patch.dict(os.environ, {"GH_TOKEN": "owner-personal-token", "GITLAB_TOKEN": "owner-gitlab-token"}),
            patch("teatree.core.operator_auth.operator_oauth.master_key_source") as source,
            patch("teatree.backends.github.api.client_for_url", return_value=forge),
        ):
            source.return_value.load.return_value = self.key
            return CliRunner().invoke(app, ["review", action, "org/repo", "7", "--forge", "github"])

    def test_blank_requester_approval_submits_one_approve_review_with_the_grant_token(self) -> None:
        self._grant()
        approval = OnBehalfApproval.record(_PR, "approve", "github:alice")
        forge = FakeGitHub()

        result = self._invoke(forge)

        assert result.exit_code == 0, result.output
        assert forge.review_posts == [{"event": "APPROVE"}]
        assert forge.bearer_tokens == {"Bearer alice-access"}
        approval.refresh_from_db()
        assert approval.consumed_at is not None
        assert OnBehalfAudit.objects.get(approval=approval).approver_id == "github:alice"

    def test_requester_recorded_on_the_ticket_approves_through_their_grant(self) -> None:
        Ticket.objects.create(issue_url="https://github.com/org/repo/pull/7", requested_by="github:alice")
        self._grant()
        approval = OnBehalfApproval.record(_PR, "approve", "github:alice")
        forge = FakeGitHub()

        result = self._invoke(forge)

        assert result.exit_code == 0, result.output
        assert forge.bearer_tokens == {"Bearer alice-access"}
        approval.refresh_from_db()
        assert approval.consumed_at is not None

    def test_slack_consent_resolves_through_the_linked_github_identity(self) -> None:
        OperatorIdentity.objects.create(user=self.alice, platform="slack", handle="U-ALICE")
        self._grant()
        approval = OnBehalfApproval.record(_PR, "approve", "slack:U-ALICE")
        forge = FakeGitHub()

        result = self._invoke(forge)

        assert result.exit_code == 0, result.output
        approval.refresh_from_db()
        assert approval.consumed_at is not None
        assert OnBehalfAudit.objects.get(approval=approval).approver_id == "slack:U-ALICE"

    def _assert_refused_untouched(self, result: Result, forge: FakeGitHub, approval: OnBehalfApproval) -> None:
        assert result.exit_code == 1, result.output
        assert forge.review_posts == []
        approval.refresh_from_db()
        assert approval.consumed_at is None
        assert not OnBehalfAudit.objects.exists()

    def test_missing_consent_refuses_before_any_request(self) -> None:
        self._grant()
        approval = OnBehalfApproval.record("org/repo!8", "approve", "github:alice")
        forge = FakeGitHub()

        result = self._invoke(forge)

        self._assert_refused_untouched(result, forge, approval)
        assert "approve-on-behalf org/repo!7 approve --approver" in result.output
        assert forge.requests == []

    def test_login_that_differs_from_the_consenting_identity_refuses_before_the_review(self) -> None:
        self._grant()
        approval = OnBehalfApproval.record(_PR, "approve", "github:alice")
        forge = FakeGitHub(login="mallory")

        result = self._invoke(forge)

        self._assert_refused_untouched(result, forge, approval)
        assert "identity does not match" in result.output

    def test_missing_grant_names_the_login_remedy(self) -> None:
        approval = OnBehalfApproval.record(_PR, "approve", "github:alice")
        forge = FakeGitHub()

        result = self._invoke(forge)

        self._assert_refused_untouched(result, forge, approval)
        assert "t3 login github --approvals" in result.output
        assert forge.requests == []

    def test_expired_grant_without_a_working_refresh_names_the_login_remedy(self) -> None:
        self._grant(expires_in=-timedelta(minutes=1))
        OperatorGrant.objects.update(refresh_expires_at=timezone.now() - timedelta(minutes=1))
        approval = OnBehalfApproval.record(_PR, "approve", "github:alice")
        forge = FakeGitHub()

        result = self._invoke(forge)

        self._assert_refused_untouched(result, forge, approval)
        assert "t3 login github --approvals" in result.output

    def test_unreadable_github_login_refuses_before_the_review(self) -> None:
        self._grant()
        approval = OnBehalfApproval.record(_PR, "approve", "github:alice")
        for forge in (FakeGitHub(user_status=httpx.codes.UNAUTHORIZED), FakeGitHub(login="")):
            with self.subTest(user_status=forge.user_status, login=forge.login):
                result = self._invoke(forge)

                self._assert_refused_untouched(result, forge, approval)
                assert "GitHub login behind the grant" in result.output

    def test_publisher_refuses_when_the_consent_was_spent_after_resolution(self) -> None:
        forge = FakeGitHub()

        with patch("teatree.backends.github.api.client_for_url", return_value=forge):
            message, code = publish_github_approval(
                "alice-access", "org/repo", 7, "approve", OperatorApprover("github:alice", "github:alice")
            )

        assert code == 1
        assert "No consent from github:alice" in message
        assert forge.requests == []

    def test_operator_without_a_github_login_cannot_approve_on_github(self) -> None:
        OperatorIdentity.objects.filter(user=self.alice, platform="github").delete()
        OperatorIdentity.objects.create(user=self.alice, platform="slack", handle="U-ALICE")
        approval = OnBehalfApproval.record(_PR, "approve", "slack:U-ALICE")
        forge = FakeGitHub()

        result = self._invoke(forge)

        self._assert_refused_untouched(result, forge, approval)
        assert "no GitHub operator login" in result.output

    def test_forge_refusal_releases_the_consent_and_writes_no_audit(self) -> None:
        self._grant()
        approval = OnBehalfApproval.record(_PR, "approve", "github:alice")
        forge = FakeGitHub(post_status=httpx.codes.UNPROCESSABLE_ENTITY)

        result = self._invoke(forge)

        assert result.exit_code == 1, result.output
        assert "HTTP 422" in result.output
        approval.refresh_from_db()
        assert approval.consumed_at is None
        assert not OnBehalfAudit.objects.exists()

    def test_review_that_does_not_read_back_as_approved_releases_the_consent(self) -> None:
        self._grant()
        approval = OnBehalfApproval.record(_PR, "approve", "github:alice")
        forge = FakeGitHub(recorded_state="COMMENTED")

        result = self._invoke(forge)

        assert result.exit_code == 1, result.output
        assert "not APPROVED" in result.output
        approval.refresh_from_db()
        assert approval.consumed_at is None
        assert not OnBehalfAudit.objects.exists()

    def test_unapprove_is_refused_and_leaves_the_consent_unconsumed(self) -> None:
        self._grant()
        approval = OnBehalfApproval.record(_PR, "unapprove", "github:alice")
        forge = FakeGitHub()

        result = self._invoke(forge, "unapprove")

        assert result.exit_code == 1, result.output
        assert "GitHub" in result.output
        assert forge.requests == []
        approval.refresh_from_db()
        assert approval.consumed_at is None
