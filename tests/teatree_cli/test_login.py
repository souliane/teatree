from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from typer.testing import CliRunner

from teatree.cli import app
from teatree.core.models import OperatorGrant
from teatree.core.models.operator_grant import OperatorGrantTokens
from teatree.utils.loopback_forward import ForwardResult
from teatree.utils.sealing import MasterKey

runner = CliRunner()


class OperatorLoginCliTestCase(TestCase):
    @patch("teatree.cli.login.webbrowser.open")
    @patch("teatree.cli.login.httpx.get")
    @patch("teatree.cli.login.client_for_url")
    @patch("teatree.cli.login.running_in_container", return_value=False)
    def test_native_login_opens_the_configured_oauth_start(
        self, container: Mock, transport: Mock, get: Mock, browser: Mock
    ) -> None:
        get.return_value = httpx.Response(
            302, request=httpx.Request("GET", "http://127.0.0.1:8000/login/oauth/github/start/")
        )
        transport.return_value.get.side_effect = get
        result = runner.invoke(app, ["login", "github"])
        assert result.exit_code == 0
        browser.assert_called_once_with("http://127.0.0.1:8000/login/oauth/github/start/")
        container.assert_called_once()

    @patch("teatree.cli.admin.data_dir_root")
    @patch("teatree.cli.login.ensure_admin_forward")
    @patch("teatree.cli.login.running_in_container", return_value=True)
    def test_container_login_records_the_forward_url_for_host_browser(
        self, container: Mock, forward: Mock, data_dir: Mock
    ) -> None:
        with TemporaryDirectory() as directory:
            data_dir.return_value = Path(directory)
            forward.return_value = ForwardResult(url="http://127.0.0.1:8803")
            result = runner.invoke(app, ["login", "gitlab"])
            assert result.exit_code == 0
            assert (Path(directory) / "admin-browse-url").read_text().strip() == (
                "http://127.0.0.1:8803/login/oauth/gitlab/start/"
            )
            container.assert_called_once()

    @patch("teatree.core.operator_auth.operator_oauth.revoke_operator_grant")
    def test_logout_selects_one_operators_grant_and_requires_username_when_ambiguous(self, revoke: Mock) -> None:
        alice = get_user_model().objects.create_superuser("alice", email="", password="unused")
        bob = get_user_model().objects.create_superuser("bob", email="", password="unused")
        tokens = OperatorGrantTokens("access", "refresh", timezone.now())
        key = MasterKey(b"a" * 32)
        OperatorGrant.objects.store(user=alice, forge="gitlab", tokens=tokens, key=key)
        OperatorGrant.objects.store(user=bob, forge="gitlab", tokens=tokens, key=key)

        ambiguous = runner.invoke(app, ["logout", "gitlab"])
        assert ambiguous.exit_code == 1
        revoke.assert_not_called()

        selected = runner.invoke(app, ["logout", "gitlab", "--user", "bob"])
        assert selected.exit_code == 0
        revoke.assert_called_once_with(bob, "gitlab")

    @patch(
        "teatree.core.operator_auth.operator_oauth.revoke_operator_grant",
        side_effect=RuntimeError("operator gitlab grant is still active or could not be verified"),
    )
    def test_a_failed_revoke_exits_one_and_keeps_the_grant(self, revoke: Mock) -> None:
        alice = get_user_model().objects.create_superuser("alice", email="", password="unused")
        tokens = OperatorGrantTokens("access", "refresh", timezone.now())
        OperatorGrant.objects.store(user=alice, forge="gitlab", tokens=tokens, key=MasterKey(b"a" * 32))

        result = runner.invoke(app, ["logout", "gitlab"])

        assert result.exit_code == 1
        assert "still active" in result.output
        assert OperatorGrant.objects.filter(user=alice, forge="gitlab").exists()
        revoke.assert_called_once_with(alice, "gitlab")
