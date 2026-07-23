"""The configuration page renders every tuned dial and leaks no secret value (#3664)."""

from django.test import TestCase
from django.urls import resolve, reverse

from teatree.core.models import ConfigSetting
from teatree.dash.views.config import config, config_bands_partial


class TestConfigPage(TestCase):
    def test_page_renders_every_band(self) -> None:
        response = self.client.get(reverse("dash:config"), REMOTE_ADDR="127.0.0.1")

        body = response.content.decode()
        assert response.status_code == 200
        for heading in ("Model &amp; reasoning effort", "Credentials", "Kill switches", "Self-repairs"):
            assert heading in body

    def test_page_never_renders_a_configured_secret_value(self) -> None:
        ConfigSetting.objects.set_value("github_token_pass_key", "team/internal/token")

        response = self.client.get(reverse("dash:config"), REMOTE_ADDR="127.0.0.1")

        assert "team/internal/token" not in response.content.decode()

    def test_bands_partial_is_pollable_on_its_own(self) -> None:
        response = self.client.get(reverse("dash:config_bands"), REMOTE_ADDR="127.0.0.1")

        assert response.status_code == 200
        assert "Kill switches" in response.content.decode()

    def test_both_view_functions_are_registered_on_their_routes(self) -> None:
        assert resolve(reverse("dash:config")).func is config
        assert resolve(reverse("dash:config_bands")).func is config_bands_partial

    def test_config_is_reachable_from_the_nav(self) -> None:
        response = self.client.get(reverse("dash:health"), REMOTE_ADDR="127.0.0.1")

        assert reverse("dash:config") in response.content.decode()
