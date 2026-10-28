from datetime import datetime, timedelta
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import ConfigSetting, OperatorGrant, OperatorIdentity
from teatree.core.models.operator_grant import OperatorGrantTokens
from teatree.core.operator_oauth import operator_access_token
from teatree.utils.sealing import MasterKey


class OperatorOAuthTestCase(TestCase):
    def setUp(self) -> None:
        ConfigSetting.objects.create(
            scope="",
            key="operator_oauth_apps",
            value={
                "github": {
                    "client_id": "github-client",
                    "client_secret_pass_key": "github/oauth-app",
                    "redirect_uri": "https://factory.example/login/oauth/github/callback/",
                },
                "gitlab": {
                    "client_id": "gitlab-client",
                    "client_secret_pass_key": "gitlab/oauth-app",
                    "redirect_uri": "https://factory.example/login/oauth/gitlab/callback/",
                    "base_url": "https://gitlab.example",
                },
            },
        )
        self.key = MasterKey(b"a" * 32)

    @patch("teatree.core.operator_oauth.master_key_source")
    @patch("teatree.core.operator_oauth.read_pass", return_value="app-secret")
    @patch("teatree.core.operator_oauth.httpx.get")
    @patch("teatree.core.operator_oauth.httpx.post")
    def test_github_pkce_login_creates_a_superuser_and_sealed_per_user_grant(
        self, post: Mock, get: Mock, secret: Mock, source: Mock
    ) -> None:
        source.return_value.load.return_value = self.key
        post.return_value = httpx.Response(
            200,
            request=httpx.Request("POST", "https://github.com/login/oauth/access_token"),
            json={
                "access_token": "access-secret",
                "refresh_token": "refresh-secret",
                "expires_in": 3600,
                "refresh_token_expires_in": 86400,
                "scope": "repo read:user",
            },
        )
        get.return_value = httpx.Response(
            200, request=httpx.Request("GET", "https://api.github.com/user"), json={"id": 123, "login": "Alice"}
        )

        start = self.client.get("/login/oauth/github/start/")
        assert start.status_code == 302
        query = parse_qs(urlsplit(start.headers["Location"]).query)
        assert urlsplit(start.headers["Location"]).netloc == "github.com"
        assert query["code_challenge_method"] == ["S256"]
        assert query["scope"] == ["repo read:user"]
        assert "client_secret" not in start.headers["Location"]
        state = query["state"][0]

        callback = self.client.get("/login/oauth/github/callback/", {"code": "one-use-code", "state": state})
        assert callback.status_code == 302
        assert callback.headers["Location"] == "/admin/"
        user = get_user_model().objects.get(username="github:123")
        assert user.is_superuser
        assert user.is_staff
        assert not user.has_usable_password()
        assert OperatorIdentity.objects.get(platform="github", external_id="123").user == user
        grant = OperatorGrant.objects.get(user=user, forge="github")
        assert b"refresh-secret" not in bytes(grant.ciphertext)
        assert grant.reveal(self.key).refresh_token == "refresh-secret"
        assert self.client.get("/admin/").status_code == 200
        assert post.call_args.kwargs["data"]["code_verifier"]
        assert post.call_args.kwargs["data"]["client_secret"] == "app-secret"
        secret.assert_called_once_with("github/oauth-app")
        assert self.client.get("/login/oauth/github/callback/", {"code": "replay", "state": state}).status_code == 403

    @patch("teatree.core.operator_oauth.httpx.post")
    def test_wrong_state_never_exchanges_code(self, post: Mock) -> None:
        assert self.client.get("/login/oauth/github/callback/", {"code": "x", "state": "wrong"}).status_code == 403
        post.assert_not_called()

    @patch("teatree.core.operator_oauth.master_key_source")
    @patch("teatree.core.operator_oauth.read_pass", return_value="app-secret")
    @patch("teatree.core.operator_oauth.httpx.post")
    def test_expired_grant_refreshes_under_its_own_operator(self, post: Mock, secret: Mock, source: Mock) -> None:
        source.return_value.load.return_value = self.key
        user = get_user_model().objects.create_superuser("alice", email="", password="unused")
        other = get_user_model().objects.create_superuser("bob", email="", password="unused")
        expired = timezone.now() - timedelta(minutes=1)
        OperatorGrant.objects.store(
            user=user,
            forge="gitlab",
            tokens=self._tokens("old-access", "old-refresh", expired),
            key=self.key,
        )
        OperatorGrant.objects.store(
            user=other,
            forge="gitlab",
            tokens=self._tokens("other-access", "other-refresh", timezone.now() + timedelta(hours=1)),
            key=self.key,
        )
        post.return_value = httpx.Response(
            200,
            request=httpx.Request("POST", "https://gitlab.example/oauth/token"),
            json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600},
        )

        assert operator_access_token(user, "gitlab") == "new-access"
        assert operator_access_token(other, "gitlab") == "other-access"
        assert OperatorGrant.objects.get(user=user, forge="gitlab").reveal(self.key).refresh_token == "new-refresh"
        assert post.call_count == 1
        assert post.call_args.kwargs["data"]["refresh_token"] == "old-refresh"
        secret.assert_called_once_with("gitlab/oauth-app")

    @staticmethod
    def _tokens(access: str, refresh: str, expires_at: datetime) -> OperatorGrantTokens:
        return OperatorGrantTokens(access_token=access, refresh_token=refresh, expires_at=expires_at)
