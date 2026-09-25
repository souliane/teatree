"""``t3 <overlay> github_app`` — manifest, registration, confirm-repos, status, set-preset (#4795)."""

import datetime as dt
from io import StringIO
from unittest import mock

import httpx
import pytest
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from teatree.backends.github import app_registration
from teatree.config import get_effective_settings
from teatree.core.models import GitHubAppInstallation


class TestManifest(TestCase):
    def test_prints_deterministic_json_with_no_secrets(self) -> None:
        out = StringIO()
        call_command(
            "github_app",
            "manifest",
            "--name",
            "x",
            "--url",
            "https://e.com",
            "--webhook-url",
            "https://e.com/h/",
            stdout=out,
        )
        body = out.getvalue()
        assert '"name": "x"' in body
        for forbidden in ("private_key", "webhook_secret", "client_secret", "pem"):
            assert forbidden not in body.lower()


class TestRegister(TestCase):
    def test_register_prints_identity_only(self) -> None:
        response = httpx.Response(
            201,
            json={
                "id": 999,
                "slug": "x",
                "name": "x",
                "html_url": "https://github.com/apps/x",
                "client_id": "Iv1.abc",
                "client_secret": "sekrit",
                "pem": "PEMDATA",
                "webhook_secret": "hooksekrit",
            },
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return response

        with (
            mock.patch("httpx.Client", return_value=httpx.Client(transport=httpx.MockTransport(handler))),
            mock.patch.object(app_registration, "write_pass_with_backup", return_value=""),
        ):
            out = StringIO()
            call_command("github_app", "register", "test-code", stdout=out)

        body = out.getvalue()
        assert "app_id=999" in body
        assert "sekrit" not in body
        assert "hooksekrit" not in body
        assert "PEMDATA" not in body

    def test_a_failed_exchange_exits_nonzero(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"message": "Not Found"})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with mock.patch("httpx.Client", return_value=client), pytest.raises(SystemExit):
            call_command("github_app", "register", "bad-code", stderr=StringIO())


class TestConfirmRepos(TestCase):
    def setUp(self) -> None:
        self.installation = GitHubAppInstallation.objects.create(
            installation_id=1, app_id=1, account_login="x", pending_repositories=["a/b", "a/c"]
        )

    def test_confirm_named_repo(self) -> None:
        out = StringIO()
        call_command("github_app", "confirm-repos", "1", "--repo", "a/b", stdout=out)
        self.installation.refresh_from_db()
        assert self.installation.active_repositories == ["a/b"]
        assert "a/b" in out.getvalue()

    def test_confirm_all_when_no_repo_given(self) -> None:
        call_command("github_app", "confirm-repos", "1", stdout=StringIO())
        self.installation.refresh_from_db()
        assert self.installation.active_repositories == ["a/b", "a/c"]

    def test_unknown_installation_refuses(self) -> None:
        with pytest.raises(SystemExit):
            call_command("github_app", "confirm-repos", "999", stderr=StringIO())


class TestStatus(TestCase):
    def test_reports_preset_and_installations(self) -> None:
        GitHubAppInstallation.objects.create(installation_id=1, app_id=1, account_login="x")
        out = StringIO()
        call_command("github_app", "status", stdout=out)
        body = out.getvalue()
        assert "active preset: polling" in body
        assert "installation 1 (x, active)" in body


class TestSetPreset(TestCase):
    def test_switching_to_webhook_with_no_verified_delivery_refuses(self) -> None:
        GitHubAppInstallation.objects.create(installation_id=1, app_id=1, account_login="x")
        with pytest.raises(SystemExit):
            call_command("github_app", "set-preset", "webhook", stderr=StringIO())
        assert get_effective_settings().github_transport_preset.value == "polling"

    def test_switching_to_webhook_with_a_recent_verified_delivery_succeeds(self) -> None:
        installation = GitHubAppInstallation.objects.create(installation_id=1, app_id=1, account_login="x")
        installation.mark_verified_delivery(at=timezone.now())
        call_command("github_app", "set-preset", "webhook", stdout=StringIO())
        assert get_effective_settings().github_transport_preset.value == "webhook"

    def test_a_stale_verified_delivery_still_refuses(self) -> None:
        installation = GitHubAppInstallation.objects.create(installation_id=1, app_id=1, account_login="x")
        installation.mark_verified_delivery(at=timezone.now() - dt.timedelta(hours=48))
        with pytest.raises(SystemExit):
            call_command("github_app", "set-preset", "webhook", stderr=StringIO())

    def test_switching_back_to_polling_never_needs_verification(self) -> None:
        call_command("github_app", "set-preset", "polling", stdout=StringIO())
        assert get_effective_settings().github_transport_preset.value == "polling"

    def test_an_unknown_preset_refuses(self) -> None:
        with pytest.raises(SystemExit):
            call_command("github_app", "set-preset", "bogus", stderr=StringIO())
