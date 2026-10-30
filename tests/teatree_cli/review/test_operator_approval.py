import os
from datetime import timedelta
from unittest.mock import patch

from click.testing import Result
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AbstractBaseUser
from django.db import connection
from django.test import TestCase
from django.utils import timezone
from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli.review.operator_approval import OperatorApprover, publish_operator_approval
from teatree.cli.review.service import ReviewService
from teatree.core.modelkit.gate_verdict import ReadOutcome
from teatree.core.models import ConfigSetting, OnBehalfApproval, OnBehalfAudit, OperatorGrant, OperatorIdentity, Ticket
from teatree.core.models.operator_grant import OperatorGrantTokens
from teatree.utils.sealing import MasterKey
from tests.teatree_core._on_behalf_gate_helpers import seed_permitting_posture


class ApprovalAPI:
    def __init__(self, username: str = "alice", *, reviewed: bool = True) -> None:
        self.username = username
        self.reviewed = reviewed
        self.approved = True
        self.posts = 0

    def current_username(self) -> str:
        return self.username

    def get_json_paginated(self, endpoint: str) -> list[dict[str, object]]:
        _ = endpoint
        return [{"notes": [{"author": {"username": self.username}}]}] if self.reviewed else []

    def post_status(self, endpoint: str) -> int:
        self.posts += 1
        self.approved = not endpoint.endswith("/unapprove")
        return 200

    def get_json(self, endpoint: str) -> dict[str, object]:
        return (
            {"approved_by": [{"user": {"username": self.username}}] if self.approved else []}
            if endpoint.endswith("/approvals")
            else {}
        )


class TestOperatorApproval(TestCase):
    def setUp(self) -> None:
        seed_permitting_posture()
        ConfigSetting.objects.create(
            scope="",
            key="operator_oauth_apps",
            value={
                "gitlab": {
                    "client_id": "client",
                    "client_secret_pass_key": "gitlab/oauth-app",
                    "redirect_uri": "https://factory.example/login/oauth/gitlab/callback/",
                }
            },
        )
        self.alice = get_user_model().objects.create_superuser(username="alice", email="", password=None)
        self.bob = get_user_model().objects.create_superuser(username="bob", email="", password=None)
        OperatorIdentity.objects.create(user=self.alice, platform="gitlab", handle="alice", external_id="1")
        OperatorIdentity.objects.create(user=self.bob, platform="gitlab", handle="bob", external_id="2")
        Ticket.objects.create(issue_url="https://gitlab.com/org/repo/-/merge_requests/7", requested_by="gitlab:alice")
        self.key = MasterKey(b"a" * 32)

    def _grant(self, user: AbstractBaseUser, access: str) -> None:
        OperatorGrant.objects.store(
            user=user,
            forge="gitlab",
            tokens=OperatorGrantTokens(
                access_token=access,
                refresh_token=f"{access}-refresh",
                expires_at=timezone.now() + timedelta(hours=1),
                scope="api",
            ),
            key=self.key,
        )

    def _invoke(self, action: str, *, username: str = "alice") -> tuple[Result, list[str]]:
        observed: list[str] = []
        api = ApprovalAPI(username)

        def api_for(service: ReviewService) -> ApprovalAPI:
            assert service.oauth
            observed.append(service.token)
            return api

        with (
            patch.dict(os.environ, {"GITLAB_TOKEN": "owner-personal-token"}),
            patch("teatree.core.operator_auth.operator_oauth.master_key_source") as source,
            patch.object(ReviewService, "_get_api", api_for),
        ):
            source.return_value.load.return_value = self.key
            result = CliRunner().invoke(app, ["review", action, "org/repo", "7"])
        return result, observed

    def test_approval_uses_requesters_grant_and_consumes_that_persons_consent(self) -> None:
        self._grant(self.alice, "alice-access")
        self._grant(self.bob, "bob-access")
        approval = OnBehalfApproval.record("org/repo!7", "approve", "gitlab:alice")

        result, tokens = self._invoke("approve")

        assert result.exit_code == 0, result.output
        assert tokens
        assert set(tokens) == {"alice-access"}
        approval.refresh_from_db()
        assert approval.consumed_at is not None
        assert OnBehalfAudit.objects.get(approval=approval).approver_id == "gitlab:alice"

    def test_other_persons_consent_cannot_use_requesters_grant(self) -> None:
        self._grant(self.alice, "alice-access")
        OnBehalfApproval.record("org/repo!7", "approve", "gitlab:bob")

        result, tokens = self._invoke("approve")

        assert result.exit_code == 1
        assert tokens == []
        assert not OnBehalfAudit.objects.exists()

    def test_missing_requester_grant_never_falls_back_to_owner_token(self) -> None:
        self._grant(self.bob, "bob-access")
        OnBehalfApproval.record("org/repo!7", "approve", "gitlab:alice")

        result, tokens = self._invoke("approve")

        assert result.exit_code == 1
        assert "gitlab:alice" in result.output
        assert "t3 login gitlab" in result.output
        assert tokens == []

    def test_owner_legacy_approval_works_without_a_recorded_requester(self) -> None:
        Ticket.objects.update(requested_by="")
        OnBehalfApproval.record("org/repo!7", "approve", "gitlab:alice")
        api = ApprovalAPI()
        with (
            patch.object(
                ReviewService, "read_gitlab_token", return_value=ReadOutcome(value="owner-token", failed=False)
            ),
            patch.object(ReviewService, "_get_api", return_value=api),
        ):
            result = CliRunner().invoke(app, ["review", "approve", "org/repo", "7"])
        assert result.exit_code == 0, result.output
        assert api.posts == 1

    def test_unapproval_uses_requester_grant_and_consumes_consent(self) -> None:
        self._grant(self.alice, "alice-access")
        approval = OnBehalfApproval.record("org/repo!7", "unapprove", "gitlab:alice")

        result, tokens = self._invoke("unapprove")

        assert result.exit_code == 0, result.output
        assert tokens
        assert set(tokens) == {"alice-access"}
        approval.refresh_from_db()
        assert approval.consumed_at is not None
        assert OnBehalfAudit.objects.get(approval=approval).action == "unapprove"

    def test_slack_requester_approval_uses_linked_gitlab_grant_and_slack_consent(self) -> None:
        Ticket.objects.update(requested_by="slack:U-ALICE")
        OperatorIdentity.objects.create(user=self.alice, platform="slack", handle="U-ALICE")
        self._grant(self.alice, "alice-access")
        approval = OnBehalfApproval.record("org/repo!7", "approve", "slack:U-ALICE")

        result, tokens = self._invoke("approve")

        assert result.exit_code == 0, result.output
        assert tokens
        assert set(tokens) == {"alice-access"}
        approval.refresh_from_db()
        assert approval.consumed_at is not None
        assert OnBehalfAudit.objects.get(approval=approval).approver_id == "slack:U-ALICE"

    def test_slack_requester_unapproval_uses_linked_gitlab_grant(self) -> None:
        Ticket.objects.update(requested_by="slack:U-ALICE")
        OperatorIdentity.objects.create(user=self.alice, platform="slack", handle="U-ALICE")
        self._grant(self.alice, "alice-access")
        approval = OnBehalfApproval.record("org/repo!7", "unapprove", "slack:U-ALICE")

        result, tokens = self._invoke("unapprove")

        assert result.exit_code == 0, result.output
        assert tokens
        assert set(tokens) == {"alice-access"}
        approval.refresh_from_db()
        assert approval.consumed_at is not None

    def test_other_slack_users_consent_cannot_use_requesters_grant(self) -> None:
        Ticket.objects.update(requested_by="slack:U-ALICE")
        OperatorIdentity.objects.create(user=self.alice, platform="slack", handle="U-ALICE")
        OperatorIdentity.objects.create(user=self.bob, platform="slack", handle="U-BOB")
        self._grant(self.alice, "alice-access")
        OnBehalfApproval.record("org/repo!7", "approve", "slack:U-BOB")

        result, tokens = self._invoke("approve")

        assert result.exit_code == 1
        assert tokens == []
        assert not OnBehalfAudit.objects.exists()

    def test_publisher_refuses_a_slack_consent_bound_to_another_forge_user(self) -> None:
        OperatorIdentity.objects.create(user=self.bob, platform="slack", handle="U-BOB")
        approval = OnBehalfApproval.record("org/repo!7", "approve", "slack:U-BOB")
        api = ApprovalAPI(username="alice")
        service = ReviewService("alice-oauth", repo="org/repo", api=api, oauth=True)

        message, code = publish_operator_approval(
            service, "org/repo", 7, "approve", OperatorApprover("slack:U-BOB", "gitlab:alice")
        )

        assert code == 1
        assert "different users" in message
        assert api.posts == 0
        approval.refresh_from_db()
        assert approval.consumed_at is None

    def test_grant_with_another_forge_identity_refuses_and_preserves_consent(self) -> None:
        self._grant(self.alice, "alice-access")
        approval = OnBehalfApproval.record("org/repo!7", "approve", "gitlab:alice")

        result, tokens = self._invoke("approve", username="bob")

        assert result.exit_code == 1
        assert "identity does not match" in result.output
        assert tokens
        assert set(tokens) == {"alice-access"}
        approval.refresh_from_db()
        assert approval.consumed_at is None
        assert not OnBehalfAudit.objects.exists()

    def test_legacy_service_approve_does_not_consume_operator_consent(self) -> None:
        approval = OnBehalfApproval.record("org/repo!7", "approve", "gitlab:alice")
        api = ApprovalAPI()
        service = ReviewService("personal-token", repo="org/repo")

        with (
            patch.object(service, "_get_api", return_value=api),
            patch.object(api, "post_status", wraps=api.post_status) as post,
        ):
            message, code = service.approve("org/repo", 7)

        assert code == 0, message
        post.assert_called_once()
        approval.refresh_from_db()
        assert approval.consumed_at is None

    def test_publisher_refuses_personal_token_even_with_consent(self) -> None:
        approval = OnBehalfApproval.record("org/repo!7", "approve", "gitlab:alice")
        api = ApprovalAPI()
        service = ReviewService("personal-token", repo="org/repo", api=api)

        with patch.object(api, "post_status", wraps=api.post_status) as post:
            message, code = publish_operator_approval(service, "org/repo", 7, "approve", "gitlab:alice")

        assert code == 1
        assert "OAuth grant required" in message
        post.assert_not_called()
        approval.refresh_from_db()
        assert approval.consumed_at is None

    def test_approval_consent_without_same_identity_review_note_cannot_publish(self) -> None:
        approval = OnBehalfApproval.record("org/repo!7", "approve", "gitlab:alice")
        api = ApprovalAPI(reviewed=False)
        service = ReviewService("alice-oauth", repo="org/repo", api=api, oauth=True)

        message, code = publish_operator_approval(service, "org/repo", 7, "approve", "gitlab:alice")

        assert code == 1
        assert "review before approve" in message
        assert api.posts == 0
        approval.refresh_from_db()
        assert approval.consumed_at is None

    def test_forge_approval_runs_without_a_database_write_lock(self) -> None:
        OnBehalfApproval.record("org/repo!7", "approve", "gitlab:alice")
        api = ApprovalAPI()
        service = ReviewService("alice-oauth", repo="org/repo", api=api, oauth=True)
        baseline_savepoints = len(connection.savepoint_ids)
        post_status = api.post_status

        def unlocked_username() -> str:
            assert len(connection.savepoint_ids) == baseline_savepoints
            return "alice"

        def unlocked_post(endpoint: str) -> int:
            assert len(connection.savepoint_ids) == baseline_savepoints
            return post_status(endpoint)

        with (
            patch.object(api, "current_username", side_effect=unlocked_username),
            patch.object(api, "post_status", side_effect=unlocked_post),
        ):
            message, code = publish_operator_approval(service, "org/repo", 7, "approve", "gitlab:alice")
        assert code == 0, message

    def test_legacy_service_unapprove_does_not_consume_operator_consent(self) -> None:
        approval = OnBehalfApproval.record("org/repo!7", "unapprove", "gitlab:alice")
        api = ApprovalAPI()
        service = ReviewService("personal-token", repo="org/repo")

        with (
            patch.object(service, "_get_api", return_value=api),
            patch.object(api, "post_status", wraps=api.post_status) as post,
        ):
            message, code = service.unapprove("org/repo", 7)

        assert code == 0, message
        post.assert_called_once()
        approval.refresh_from_db()
        assert approval.consumed_at is None
