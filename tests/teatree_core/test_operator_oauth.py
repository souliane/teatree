import os
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.utils import timezone

from teatree.core.models import ConfigSetting, OnBehalfApproval, OperatorGrant, OperatorIdentity, SealedSecret, Ticket
from teatree.core.models.operator_grant import OperatorGrantTokens
from teatree.core.operator_auth.operator_oauth import (
    OperatorOAuthUnavailableError,
    operator_access_token,
    revoke_operator_grant,
)
from teatree.utils.master_key import MasterKeySourceError
from teatree.utils.sealing import MasterKey
from teatree.utils.secrets import SecretStoreError


class OperatorOAuthTestCase(TestCase):
    def setUp(self) -> None:
        class MockTransport:
            def request(
                self, method: str, url: str, *, payload: object, headers: dict[str, str], timeout: float
            ) -> httpx.Response:
                if method == "POST":
                    assert isinstance(payload, str)
                    return httpx.post(
                        url,
                        data={key: values[0] for key, values in parse_qs(payload).items()},
                        headers=headers,
                        timeout=timeout,
                    )
                assert method == "DELETE"
                assert isinstance(payload, dict)
                return httpx.request(method, url, json=payload, headers=headers, timeout=timeout)

            def get(self, url: str, *, headers: dict[str, str], timeout: float) -> httpx.Response:
                return httpx.get(url, headers=headers, timeout=timeout)

        transport = patch("teatree.core.operator_auth.operator_oauth.client_for_url", return_value=MockTransport())
        transport.start()
        self.addCleanup(transport.stop)
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

    def test_admin_login_offers_both_forge_grants(self) -> None:
        for path in ("/login/", "/admin/login/"):
            body = self.client.get(path).content.decode()
            assert "/login/oauth/github/start/" in body
            assert "/login/oauth/gitlab/start/" in body

    def test_start_without_a_configured_app_reports_configuration_action(self) -> None:
        ConfigSetting.objects.filter(key="operator_oauth_apps").delete()

        response = self.client.get("/login/oauth/github/start/")

        assert response.status_code == 503
        assert b"operator_oauth_apps" in response.content

    @patch("teatree.core.operator_auth.operator_oauth.read_pass", side_effect=SecretStoreError("store unavailable"))
    def test_secret_store_failure_is_reported_without_server_error(self, secret: Mock) -> None:
        start = self.client.get("/login/oauth/github/start/")
        state = parse_qs(urlsplit(start.headers["Location"]).query)["state"][0]

        response = self.client.get("/login/oauth/github/callback/", {"code": "code", "state": state})

        assert response.status_code == 502
        assert b"SecretStoreError" in response.content
        secret.assert_called_once_with("github/oauth-app")

    def test_non_ascii_state_is_refused_without_server_error(self) -> None:
        self.client.get("/login/oauth/github/start/")

        response = self.client.get("/login/oauth/github/callback/", {"code": "code", "state": "é"})

        assert response.status_code == 403
        assert not OperatorGrant.objects.exists()

    @patch("teatree.core.operator_auth.operator_oauth.read_pass", return_value="app-secret")
    @patch("teatree.core.operator_auth.operator_oauth.httpx.post")
    def test_token_exchange_without_refresh_grant_reports_relogin_action(self, post: Mock, secret: Mock) -> None:
        post.return_value = httpx.Response(
            200,
            request=httpx.Request("POST", "https://github.com/login/oauth/access_token"),
            json={"error": "access_denied"},
        )
        start = self.client.get("/login/oauth/github/start/")
        state = parse_qs(urlsplit(start.headers["Location"]).query)["state"][0]

        response = self.client.get("/login/oauth/github/callback/", {"code": "denied", "state": state})

        assert response.status_code == 502
        assert b"t3 login github" in response.content
        assert not OperatorGrant.objects.exists()
        secret.assert_called_once_with("github/oauth-app")

    @patch("teatree.core.operator_auth.operator_oauth.master_key_source")
    @patch("teatree.core.operator_auth.operator_oauth.read_pass", return_value="app-secret")
    @patch("teatree.core.operator_auth.operator_oauth.httpx.get")
    @patch("teatree.core.operator_auth.operator_oauth.httpx.post")
    def test_uninvited_forge_account_cannot_sign_in(self, post: Mock, get: Mock, secret: Mock, source: Mock) -> None:
        source.return_value.load.return_value = self.key
        post.return_value = httpx.Response(
            200,
            request=httpx.Request("POST", "https://github.com/login/oauth/access_token"),
            json={"access_token": "access-secret", "refresh_token": "refresh-secret", "expires_in": 3600},
        )
        get.return_value = httpx.Response(
            200, request=httpx.Request("GET", "https://api.github.com/user"), json={"id": 123, "login": "Alice"}
        )
        state = parse_qs(urlsplit(self.client.get("/login/oauth/github/start/").headers["Location"]).query)["state"][0]

        response = self.client.get("/login/oauth/github/callback/", {"code": "code", "state": state})

        assert response.status_code == 403
        assert b"administrator" in response.content
        assert not get_user_model().objects.filter(username="github:123").exists()
        assert not OperatorIdentity.objects.filter(platform="github", external_id="123").exists()
        assert not OperatorGrant.objects.exists()
        secret.assert_called_once_with("github/oauth-app")
        source.assert_not_called()

    @patch("teatree.core.operator_auth.operator_oauth.master_key_source")
    @patch("teatree.core.operator_auth.operator_oauth.read_pass", return_value="app-secret")
    @patch("teatree.core.operator_auth.operator_oauth.httpx.get")
    @patch("teatree.core.operator_auth.operator_oauth.httpx.post")
    def test_signed_in_admin_cannot_link_an_uninvited_forge_account(
        self, post: Mock, get: Mock, secret: Mock, source: Mock
    ) -> None:
        source.return_value.load.return_value = self.key
        user = get_user_model().objects.create_superuser("operator", email="", password="password")
        self.client.force_login(user)
        post.return_value = httpx.Response(
            200,
            request=httpx.Request("POST", "https://github.com/login/oauth/access_token"),
            json={"access_token": "access-secret", "refresh_token": "refresh-secret", "expires_in": 3600},
        )
        get.return_value = httpx.Response(
            200, request=httpx.Request("GET", "https://api.github.com/user"), json={"id": 123, "login": "Alice"}
        )
        state = parse_qs(urlsplit(self.client.get("/login/oauth/github/start/").headers["Location"]).query)["state"][0]

        response = self.client.get("/login/oauth/github/callback/", {"code": "code", "state": state})

        assert response.status_code == 403
        assert not OperatorIdentity.objects.filter(platform="github", external_id="123").exists()
        assert not OperatorGrant.objects.filter(user=user, forge="github").exists()
        secret.assert_called_once_with("github/oauth-app")
        source.assert_not_called()

    @patch("teatree.core.operator_auth.operator_oauth.master_key_source")
    @patch("teatree.core.operator_auth.operator_oauth.read_pass", return_value="app-secret")
    @patch("teatree.core.operator_auth.operator_oauth.httpx.get")
    @patch("teatree.core.operator_auth.operator_oauth.httpx.post")
    def test_github_pkce_login_uses_identity_scope_without_storing_a_grant(
        self, post: Mock, get: Mock, secret: Mock, source: Mock
    ) -> None:
        source.return_value.load.return_value = self.key
        user = get_user_model().objects.create_superuser("github:123", email="", password=None)
        OperatorIdentity.objects.create(user=user, platform="github", handle="alice", external_id="123")
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
        assert query["scope"] == ["read:user"]
        assert "client_secret" not in start.headers["Location"]
        state = query["state"][0]

        callback = self.client.get("/login/oauth/github/callback/", {"code": "one-use-code", "state": state})
        assert callback.status_code == 302
        assert callback.headers["Location"] == "/admin/"
        user.refresh_from_db()
        assert user.is_superuser
        assert user.is_staff
        assert not user.has_usable_password()
        assert OperatorIdentity.objects.get(platform="github", external_id="123").user == user
        assert not OperatorGrant.objects.filter(user=user, forge="github").exists()
        assert not SealedSecret.objects.exists()
        assert not ConfigSetting.objects.filter(value__icontains="refresh-secret").exists()
        assert not any("refresh-secret" in value or "access-secret" in value for value in os.environ.values())
        assert self.client.get("/admin/").status_code == 200
        assert post.call_args.kwargs["data"]["code_verifier"]
        assert post.call_args.kwargs["data"]["client_secret"] == "app-secret"
        secret.assert_called_once_with("github/oauth-app")
        source.assert_not_called()
        assert self.client.get("/login/oauth/github/callback/", {"code": "replay", "state": state}).status_code == 403

    @patch("teatree.core.operator_auth.operator_oauth.httpx.post")
    def test_wrong_state_never_exchanges_code(self, post: Mock) -> None:
        self._start_github_login()
        assert self.client.get("/login/oauth/github/callback/", {"code": "x", "state": "wrong"}).status_code == 403
        post.assert_not_called()

    def test_approval_consent_is_separate_and_seals_only_the_approved_grant(self) -> None:
        user = self._invited_github_operator("alice", "123")
        self.client.force_login(user)
        with self._forge_account({"id": 123, "login": "alice"}) as post:
            start = self.client.get("/login/oauth/github/approvals/")
            query = parse_qs(urlsplit(start.headers["Location"]).query)
            assert query["scope"] == ["repo read:user offline_access"]
            response = self.client.get(
                "/login/oauth/github/callback/", {"code": "approval-code", "state": query["state"][0]}
            )
        assert response.status_code == 302
        grant = OperatorGrant.objects.get(user=user, forge="github")
        assert grant.reveal(self.key).refresh_token == "refresh-secret"
        assert b"refresh-secret" not in bytes(grant.ciphertext)
        assert post.call_args.kwargs["data"]["code"] == "approval-code"

    def test_approval_consent_without_master_key_reports_remedy_and_stores_nothing(self) -> None:
        user = self._invited_github_operator("alice", "123")
        self.client.force_login(user)
        with self._forge_account(
            {"id": 123, "login": "alice"}, key_error=MasterKeySourceError("test", "is unavailable")
        ):
            state = parse_qs(urlsplit(self.client.get("/login/oauth/github/approvals/").headers["Location"]).query)[
                "state"
            ][0]
            response = self.client.get("/login/oauth/github/callback/", {"code": "code", "state": state})

        assert response.status_code == 503
        assert b"master key" in response.content
        assert not OperatorGrant.objects.exists()

    def test_failed_approval_exchange_names_approval_consent_remedy(self) -> None:
        user = self._invited_github_operator("alice", "123")
        self.client.force_login(user)
        with self._forge_account({"id": 123, "login": "alice"}) as post:
            post.return_value = httpx.Response(
                200,
                request=httpx.Request("POST", "https://github.com/login/oauth/access_token"),
                json={"error": "access_denied"},
            )
            state = parse_qs(urlsplit(self.client.get("/login/oauth/github/approvals/").headers["Location"]).query)[
                "state"
            ][0]
            response = self.client.get("/login/oauth/github/callback/", {"code": "denied", "state": state})

        assert response.status_code == 502
        assert b"t3 login github --approvals" in response.content
        assert not OperatorGrant.objects.exists()

    def test_approval_consent_without_refresh_grant_stores_nothing(self) -> None:
        user = self._invited_github_operator("alice", "123")
        self.client.force_login(user)
        with self._forge_account({"id": 123, "login": "alice"}) as post:
            post.return_value = httpx.Response(
                200,
                request=httpx.Request("POST", "https://github.com/login/oauth/access_token"),
                json={"access_token": "access-secret", "expires_in": 3600},
            )
            state = parse_qs(urlsplit(self.client.get("/login/oauth/github/approvals/").headers["Location"]).query)[
                "state"
            ][0]
            response = self.client.get("/login/oauth/github/callback/", {"code": "code", "state": state})

        assert response.status_code == 403
        assert b"refresh grant" in response.content
        assert not OperatorGrant.objects.exists()

    def test_classic_github_identity_token_without_expiry_can_sign_in(self) -> None:
        self._invited_github_operator("alice", "123")
        with self._forge_account({"id": 123, "login": "alice"}) as post:
            post.return_value = httpx.Response(
                200,
                request=httpx.Request("POST", "https://github.com/login/oauth/access_token"),
                json={"access_token": "access-secret", "scope": "read:user"},
            )
            response = self.client.get(
                "/login/oauth/github/callback/", {"code": "code", "state": self._start_github_login()}
            )

        assert response.status_code == 302
        assert "_auth_user_id" in self.client.session
        assert not OperatorGrant.objects.exists()

    def test_missing_grant_names_the_approval_consent_command(self) -> None:
        assert "t3 login gitlab --approvals" in str(OperatorOAuthUnavailableError("gitlab"))

    def test_approval_consent_refuses_a_different_signed_in_operator(self) -> None:
        alice = self._invited_github_operator("alice", "123")
        bob = self._invited_github_operator("bob", "456")
        self.client.force_login(alice)
        with self._forge_account({"id": 123, "login": "alice"}):
            state = parse_qs(urlsplit(self.client.get("/login/oauth/github/approvals/").headers["Location"]).query)[
                "state"
            ][0]
            self.client.force_login(bob)
            response = self.client.get("/login/oauth/github/callback/", {"code": "code", "state": state})
        assert response.status_code == 403
        assert not OperatorGrant.objects.exists()

    def test_provisioned_handle_binds_forge_id_on_first_sign_in(self) -> None:
        user = self._invited_github_operator("alice", "")
        with self._forge_account({"id": 123, "login": "Alice"}):
            response = self.client.get(
                "/login/oauth/github/callback/", {"code": "code", "state": self._start_github_login()}
            )
        assert response.status_code == 302
        assert OperatorIdentity.objects.get(user=user, platform="github").external_id == "123"
        assert not OperatorGrant.objects.exists()

    def test_invited_non_superuser_cannot_sign_in(self) -> None:
        user = get_user_model().objects.create_user("alice", password=None)
        OperatorIdentity.objects.create(user=user, platform="github", handle="alice", external_id="123")
        with self._forge_account({"id": 123, "login": "alice"}):
            response = self.client.get(
                "/login/oauth/github/callback/", {"code": "code", "state": self._start_github_login()}
            )
        assert response.status_code == 403
        assert "_auth_user_id" not in self.client.session

    @contextmanager
    def _forge_account(self, profile: dict[str, object], *, key_error: Exception | None = None) -> Generator[Mock]:
        tokens = {"access_token": "access-secret", "refresh_token": "refresh-secret", "expires_in": 3600}
        with (
            patch("teatree.core.operator_auth.operator_oauth.master_key_source") as source,
            patch("teatree.core.operator_auth.operator_oauth.read_pass", return_value="app-secret"),
            patch(
                "teatree.core.operator_auth.operator_oauth.httpx.get",
                return_value=httpx.Response(
                    200, request=httpx.Request("GET", "https://api.github.com/user"), json=profile
                ),
            ),
            patch(
                "teatree.core.operator_auth.operator_oauth.httpx.post",
                return_value=httpx.Response(
                    200, request=httpx.Request("POST", "https://github.com/login/oauth/access_token"), json=tokens
                ),
            ) as post,
        ):
            source.return_value.load.return_value = self.key
            source.return_value.load.side_effect = key_error
            yield post

    def _start_github_login(self, query: str = "") -> str:
        location = self.client.get(f"/login/oauth/github/start/{query}").headers["Location"]
        return parse_qs(urlsplit(location).query)["state"][0]

    @staticmethod
    def _invited_github_operator(handle: str, external_id: str) -> object:
        user = get_user_model().objects.create_superuser(f"github:{external_id}", email="", password=None)
        OperatorIdentity.objects.create(user=user, platform="github", handle=handle, external_id=external_id)
        return user

    def test_callback_without_state_exchanges_nothing_and_signs_nobody_in(self) -> None:
        self._invited_github_operator("alice", "123")
        self._start_github_login()
        with self._forge_account({"id": 123, "login": "alice"}) as post:
            response = self.client.get("/login/oauth/github/callback/", {"code": "code"})
        assert response.status_code == 403
        assert "_auth_user_id" not in self.client.session
        post.assert_not_called()

    def test_callback_without_the_session_verifier_signs_nobody_in(self) -> None:
        self._invited_github_operator("alice", "123")
        state = self._start_github_login()
        session = self.client.session
        del session["operator_oauth_pending"]["verifier"]
        session.save()
        with self._forge_account({"id": 123, "login": "alice"}) as post:
            response = self.client.get("/login/oauth/github/callback/", {"code": "code", "state": state})
        assert response.status_code == 502
        assert "_auth_user_id" not in self.client.session
        post.assert_not_called()
        assert not OperatorGrant.objects.exists()

    def test_a_renamed_forge_handle_signs_in_the_same_user(self) -> None:
        user = self._invited_github_operator("alice", "123")
        ticket = Ticket.objects.create(issue_url="https://github.com/org/repo/issues/7", requested_by="github:alice")
        consent = OnBehalfApproval.record("org/repo!7", "approve", "github:alice")
        with self._forge_account({"id": 123, "login": "Alice-Renamed"}):
            response = self.client.get(
                "/login/oauth/github/callback/", {"code": "code", "state": self._start_github_login()}
            )
        assert response.status_code == 302
        identity = OperatorIdentity.objects.get(platform="github", external_id="123")
        assert (identity.user, identity.handle) == (user, "alice-renamed")
        assert self.client.session["_auth_user_id"] == str(user.pk)
        assert get_user_model().objects.count() == 1
        ticket.refresh_from_db()
        consent.refresh_from_db()
        assert ticket.requested_by == "github:alice-renamed"
        assert consent.approver_id == "github:alice-renamed"

    def test_a_recycled_handle_with_another_forge_id_is_refused(self) -> None:
        self._invited_github_operator("alice", "123")
        with self._forge_account({"id": 999, "login": "alice"}):
            response = self.client.get(
                "/login/oauth/github/callback/", {"code": "code", "state": self._start_github_login()}
            )
        assert response.status_code == 403
        assert "_auth_user_id" not in self.client.session
        assert OperatorIdentity.objects.get(platform="github", handle="alice").external_id == "123"
        assert get_user_model().objects.count() == 1
        assert not OperatorGrant.objects.exists()

    def test_a_rename_onto_another_operators_handle_is_refused_and_changes_nothing(self) -> None:
        self._invited_github_operator("alice", "123")
        self._invited_github_operator("carol", "999")
        with self._forge_account({"id": 123, "login": "carol"}):
            response = self.client.get(
                "/login/oauth/github/callback/", {"code": "code", "state": self._start_github_login()}
            )
        assert response.status_code == 403
        assert "_auth_user_id" not in self.client.session
        assert OperatorIdentity.objects.get(external_id="123").handle == "alice"
        assert OperatorIdentity.objects.get(external_id="999").handle == "carol"
        assert not OperatorGrant.objects.exists()

    def test_sign_in_without_a_master_key_still_authenticates(self) -> None:
        self._invited_github_operator("alice", "123")
        missing = MasterKeySourceError("file:/etc/teatree/master.key", "is unreadable: FileNotFoundError")
        with self._forge_account({"id": 123, "login": "alice-renamed"}, key_error=missing):
            response = self.client.get(
                "/login/oauth/github/callback/", {"code": "code", "state": self._start_github_login()}
            )
        assert response.status_code == 302
        assert self.client.session["_auth_user_id"]
        assert OperatorIdentity.objects.get(external_id="123").handle == "alice-renamed"
        assert not OperatorGrant.objects.exists()

    def test_only_a_same_host_next_path_survives_sign_in(self) -> None:
        self._invited_github_operator("alice", "123")
        for requested, landing in (
            ("https://evil.example/steal", "/admin/"),
            ("//evil.example/steal", "/admin/"),
            ("/dash/board/", "/dash/board/"),
        ):
            with self.subTest(requested=requested), self._forge_account({"id": 123, "login": "alice"}):
                state = self._start_github_login(f"?next={requested}")
                response = self.client.get("/login/oauth/github/callback/", {"code": "code", "state": state})
                assert response.headers["Location"] == landing

    @patch("teatree.core.operator_auth.operator_oauth.master_key_source")
    @patch("teatree.core.operator_auth.operator_oauth.read_pass", return_value="app-secret")
    @patch("teatree.core.operator_auth.operator_oauth.httpx.get")
    @patch("teatree.core.operator_auth.operator_oauth.httpx.post")
    def test_gitlab_login_uses_invited_operator_and_configured_forge(
        self, post: Mock, get: Mock, secret: Mock, source: Mock
    ) -> None:
        source.return_value.load.return_value = self.key
        user = get_user_model().objects.create_superuser("gitlab:456", email="", password=None)
        OperatorIdentity.objects.create(user=user, platform="gitlab", handle="bob", external_id="456")
        post.return_value = httpx.Response(
            200,
            request=httpx.Request("POST", "https://gitlab.example/oauth/token"),
            json={"access_token": "gitlab-access", "refresh_token": "gitlab-refresh", "expires_in": 3600},
        )
        get.return_value = httpx.Response(
            200,
            request=httpx.Request("GET", "https://gitlab.example/api/v4/user"),
            json={"id": 456, "username": "bob"},
        )
        start = self.client.get("/login/oauth/gitlab/start/")
        query = parse_qs(urlsplit(start.headers["Location"]).query)
        assert urlsplit(start.headers["Location"]).netloc == "gitlab.example"
        assert query["scope"] == ["read_user"]
        callback = self.client.get("/login/oauth/gitlab/callback/", {"code": "gitlab-code", "state": query["state"][0]})
        assert callback.status_code == 302
        user.refresh_from_db()
        assert not OperatorGrant.objects.filter(user=user, forge="gitlab").exists()
        assert get.call_args.args[0] == "https://gitlab.example/api/v4/user"
        secret.assert_called_once_with("gitlab/oauth-app")
        source.assert_not_called()

    @patch("teatree.core.operator_auth.operator_oauth.master_key_source")
    @patch("teatree.core.operator_auth.operator_oauth.read_pass", return_value="app-secret")
    @patch("teatree.core.operator_auth.operator_oauth.httpx.get")
    @patch("teatree.core.operator_auth.operator_oauth.httpx.post")
    def test_logout_revokes_at_forge_then_deletes_only_that_users_grant(
        self, post: Mock, get: Mock, secret: Mock, source: Mock
    ) -> None:
        source.return_value.load.return_value = self.key
        user = get_user_model().objects.create_superuser("alice", email="", password="unused")
        other = get_user_model().objects.create_superuser("bob", email="", password="unused")
        tokens = self._tokens("access", "refresh", timezone.now() + timedelta(hours=1))
        OperatorGrant.objects.store(user=user, forge="gitlab", tokens=tokens, key=self.key)
        OperatorGrant.objects.store(user=other, forge="gitlab", tokens=tokens, key=self.key)
        post.return_value = httpx.Response(200, request=httpx.Request("POST", "https://gitlab.example/oauth/revoke"))
        get.return_value = httpx.Response(401, request=httpx.Request("GET", "https://gitlab.example/api/v4/user"))

        revoke_operator_grant(user, "gitlab")

        assert not OperatorGrant.objects.filter(user=user, forge="gitlab").exists()
        assert OperatorGrant.objects.filter(user=other, forge="gitlab").exists()
        assert [call.kwargs["data"]["token"] for call in post.call_args_list] == ["access", "refresh"]
        assert get.call_args.args[0] == "https://gitlab.example/api/v4/user"
        secret.assert_called_once_with("gitlab/oauth-app")

    @patch("teatree.core.operator_auth.operator_oauth.master_key_source")
    @patch("teatree.core.operator_auth.operator_oauth.read_pass", return_value="app-secret")
    @patch("teatree.core.operator_auth.operator_oauth.httpx.get")
    @patch("teatree.core.operator_auth.operator_oauth.httpx.request")
    def test_github_logout_revokes_the_whole_grant(self, request: Mock, get: Mock, secret: Mock, source: Mock) -> None:
        source.return_value.load.return_value = self.key
        user = get_user_model().objects.create_superuser("alice", email="", password="unused")
        OperatorGrant.objects.store(
            user=user,
            forge="github",
            tokens=self._tokens("access", "refresh", timezone.now() + timedelta(hours=1)),
            key=self.key,
        )
        request.return_value = httpx.Response(
            204, request=httpx.Request("DELETE", "https://api.github.com/applications/github-client/grant")
        )
        get.return_value = httpx.Response(401, request=httpx.Request("GET", "https://api.github.com/user"))

        revoke_operator_grant(user, "github")

        assert request.call_args.args == ("DELETE", "https://api.github.com/applications/github-client/grant")
        assert not OperatorGrant.objects.filter(user=user, forge="github").exists()
        secret.assert_called_once_with("github/oauth-app")

    @patch("teatree.core.operator_auth.operator_oauth.master_key_source")
    @patch("teatree.core.operator_auth.operator_oauth.read_pass", return_value="app-secret")
    @patch("teatree.core.operator_auth.operator_oauth.httpx.get")
    @patch("teatree.core.operator_auth.operator_oauth.httpx.request")
    @patch("teatree.core.operator_auth.operator_oauth.httpx.post")
    def test_github_logout_refreshes_expired_access_before_revoking(
        self, post: Mock, request: Mock, get: Mock, secret: Mock, source: Mock
    ) -> None:
        source.return_value.load.return_value = self.key
        user = get_user_model().objects.create_superuser("alice", email="", password="unused")
        OperatorGrant.objects.store(
            user=user,
            forge="github",
            tokens=self._tokens("expired-access", "refresh", timezone.now() - timedelta(minutes=1)),
            key=self.key,
        )
        post.return_value = httpx.Response(
            200,
            request=httpx.Request("POST", "https://github.com/login/oauth/access_token"),
            json={"access_token": "fresh-access", "refresh_token": "fresh-refresh", "expires_in": 3600},
        )
        request.return_value = httpx.Response(
            204, request=httpx.Request("DELETE", "https://api.github.com/applications/github-client/grant")
        )
        get.return_value = httpx.Response(401, request=httpx.Request("GET", "https://api.github.com/user"))

        revoke_operator_grant(user, "github")

        assert post.call_args.kwargs["data"]["refresh_token"] == "refresh"
        assert request.call_args.kwargs["json"] == {"access_token": "fresh-access"}
        assert get.call_args.kwargs["headers"]["Authorization"] == "Bearer fresh-access"
        assert not OperatorGrant.objects.filter(user=user, forge="github").exists()
        secret.assert_called_with("github/oauth-app")

    @patch("teatree.core.operator_auth.operator_oauth.master_key_source")
    @patch("teatree.core.operator_auth.operator_oauth.read_pass", return_value="app-secret")
    @patch("teatree.core.operator_auth.operator_oauth.httpx.get")
    @patch("teatree.core.operator_auth.operator_oauth.httpx.post")
    def test_logout_keeps_grant_when_forge_still_accepts_it(
        self, post: Mock, get: Mock, secret: Mock, source: Mock
    ) -> None:
        source.return_value.load.return_value = self.key
        user = get_user_model().objects.create_superuser("alice", email="", password="unused")
        OperatorGrant.objects.store(
            user=user,
            forge="gitlab",
            tokens=self._tokens("access", "refresh", timezone.now() + timedelta(hours=1)),
            key=self.key,
        )
        post.return_value = httpx.Response(200, request=httpx.Request("POST", "https://gitlab.example/oauth/revoke"))
        get.return_value = httpx.Response(200, request=httpx.Request("GET", "https://gitlab.example/api/v4/user"))

        with pytest.raises(RuntimeError, match="still active"):
            revoke_operator_grant(user, "gitlab")

        assert OperatorGrant.objects.filter(user=user, forge="gitlab").exists()
        secret.assert_called_once_with("gitlab/oauth-app")

    @patch("teatree.core.operator_auth.operator_oauth.master_key_source")
    @patch("teatree.core.operator_auth.operator_oauth.read_pass", return_value="app-secret")
    @patch("teatree.core.operator_auth.operator_oauth.httpx.post")
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
        refreshed = httpx.Response(
            200,
            request=httpx.Request("POST", "https://gitlab.example/oauth/token"),
            json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600},
        )
        baseline_savepoints = len(connection.savepoint_ids)

        def refresh_without_a_write_lock(*args: object, **kwargs: object) -> httpx.Response:
            assert len(connection.savepoint_ids) == baseline_savepoints
            return refreshed

        post.side_effect = refresh_without_a_write_lock

        assert operator_access_token(user, "gitlab") == "new-access"
        assert operator_access_token(other, "gitlab") == "other-access"
        assert OperatorGrant.objects.get(user=user, forge="gitlab").reveal(self.key).refresh_token == "new-refresh"
        assert post.call_count == 1
        assert post.call_args.kwargs["data"]["refresh_token"] == "old-refresh"
        secret.assert_called_once_with("gitlab/oauth-app")

    @staticmethod
    def _tokens(access: str, refresh: str, expires_at: datetime) -> OperatorGrantTokens:
        return OperatorGrantTokens(access_token=access, refresh_token=refresh, expires_at=expires_at)
