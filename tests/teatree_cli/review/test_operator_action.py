import os
from collections.abc import Generator
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from click.testing import Result
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from typer.testing import CliRunner

from teatree.cli import app
from teatree.core.models import ConfigSetting, OnBehalfApproval, OperatorGrant, OperatorIdentity
from teatree.core.models.operator_grant import OperatorGrantTokens
from teatree.utils.sealing import MasterKey
from tests.teatree_cli.review._fake_github import FakeGitHub
from tests.teatree_core._on_behalf_gate_helpers import seed_permitting_posture

_PR = "org/repo!7"


@contextmanager
def _declared_hosts(owned: dict[str, list[str]]) -> Generator[None]:
    overlays = {"acme": SimpleNamespace(config=SimpleNamespace(owned_repos=owned))}
    with (
        patch("teatree.core.merge.host_kind.find_project_root", return_value=None),
        patch("teatree.core.merge.host_kind.get_all_overlays", return_value=overlays),
    ):
        yield


class TestDefaultForge(TestCase):
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
        alice = get_user_model().objects.create_superuser(username="alice", email="", password=None)
        OperatorIdentity.objects.create(user=alice, platform="github", handle="alice", external_id="1")
        self.key = MasterKey(b"a" * 32)
        OperatorGrant.objects.store(
            user=alice,
            forge="github",
            tokens=OperatorGrantTokens(
                access_token="alice-access",
                refresh_token="alice-refresh",
                expires_at=timezone.now() + timedelta(hours=8),
                refresh_expires_at=timezone.now() + timedelta(days=180),
            ),
            key=self.key,
        )
        self.approval = OnBehalfApproval.record(_PR, "approve", "github:alice")
        self.forge = FakeGitHub()

    def _approve(self, *flags: str) -> Result:
        with (
            patch.dict(os.environ, {"GITLAB_TOKEN": "owner-gitlab-token"}),
            patch("teatree.core.operator_auth.operator_oauth.master_key_source") as source,
            patch("teatree.backends.github.api.client_for_url", return_value=self.forge),
        ):
            source.return_value.load.return_value = self.key
            return CliRunner().invoke(app, ["review", "approve", "org/repo", "7", *flags])

    def test_namespace_declared_on_github_approves_on_github_without_a_forge_flag(self) -> None:
        with _declared_hosts({"github.com": ["org"]}):
            result = self._approve()

        assert result.exit_code == 0, result.output
        assert self.forge.review_posts == [{"event": "APPROVE"}]
        self.approval.refresh_from_db()
        assert self.approval.consumed_at is not None

    def test_undeclared_namespace_defaults_to_gitlab_and_never_reaches_github(self) -> None:
        with _declared_hosts({}):
            result = self._approve()

        assert result.exit_code == 1, result.output
        assert "github:alice has no GitLab operator login" in result.output
        assert self.forge.requests == []
        self.approval.refresh_from_db()
        assert self.approval.consumed_at is None

    def test_explicit_forge_flag_overrides_the_declared_forge(self) -> None:
        with _declared_hosts({"gitlab.com": ["org"]}):
            result = self._approve("--forge", "github")

        assert result.exit_code == 0, result.output
        assert self.forge.review_posts == [{"event": "APPROVE"}]

    def test_declared_gitlab_namespace_does_not_reach_github(self) -> None:
        with _declared_hosts({"gitlab.com": ["org"]}):
            result = self._approve()

        assert result.exit_code == 1, result.output
        assert "github:alice has no GitLab operator login" in result.output
        assert self.forge.requests == []

    def test_unknown_forge_flag_is_refused_before_any_lookup(self) -> None:
        result = self._approve("--forge", "bitbucket")

        assert result.exit_code == 1
        assert "unknown forge 'bitbucket'; valid: github, gitlab" in result.output
        assert self.forge.requests == []
