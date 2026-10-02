import types
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from teatree.config.credential_pass_key import PassKeySource
from teatree.core.models import ConfigSetting
from teatree.core.overlay import OverlayConfig
from teatree.core.overlays.overlay_credentials import known_credentials, known_pass_key_credential, overlay_pass_key


def _declaring(credential: str, entry: str, *, scope: str = "") -> types.SimpleNamespace:
    config = OverlayConfig()
    config._register_secret(credential, entry)
    config._pass_key_scope = scope
    return types.SimpleNamespace(config=config)


class TestKnownCredentials(TestCase):
    def test_core_credentials_and_every_declared_one_are_known(self) -> None:
        declaring = {"acme": _declaring("sharepoint_secret", "store/sp")}

        with patch("teatree.core.overlays.overlay_credentials.get_all_overlays", return_value=declaring):
            known = known_credentials()

        assert {"notion_token", "gitlab_token", "figma_token", "sharepoint_secret"} <= known
        assert "notoin_token" not in known

    def test_only_a_known_credential_names_a_pass_key_setting(self) -> None:
        declaring = {"acme": _declaring("sharepoint_secret", "store/sp")}

        with patch("teatree.core.overlays.overlay_credentials.get_all_overlays", return_value=declaring):
            assert known_pass_key_credential("sharepoint_secret_pass_key") == "sharepoint_secret"
            assert known_pass_key_credential("notoin_token_pass_key") is None


class TestOverlayPassKey(TestCase):
    def test_an_unresolvable_overlay_routes_no_entry(self) -> None:
        assert overlay_pass_key("notion_token", "no-such-overlay") == ""


class TestSetupNotionRouting(TestCase):
    def test_a_fresh_venue_resolves_the_declared_route_from_the_database(self) -> None:
        overlay = _declaring("notion_token", "store/declared", scope="acme")

        with patch("teatree.core.overlay_loader.get_all_overlays", return_value={"acme": overlay}):
            call_command("provision_declared_notion_routing")

        resolution = overlay.config.resolve_pass_key("notion_token")
        assert (resolution.value, resolution.source) == ("store/declared", PassKeySource.OVERLAY_DB)

    def test_repeating_setup_creates_no_second_row(self) -> None:
        overlay = _declaring("notion_token", "store/declared", scope="acme")

        with patch("teatree.core.overlay_loader.get_all_overlays", return_value={"acme": overlay}):
            call_command("provision_declared_notion_routing")
            call_command("provision_declared_notion_routing")

        assert ConfigSetting.objects.filter(scope="acme", key="notion_token_pass_key").count() == 1

    def test_an_existing_overlay_route_is_not_clobbered(self) -> None:
        ConfigSetting.objects.set_value("notion_token_pass_key", "operator/notion", scope="acme")
        overlay = _declaring("notion_token", "store/declared", scope="acme")

        with patch("teatree.core.overlay_loader.get_all_overlays", return_value={"acme": overlay}):
            call_command("provision_declared_notion_routing")

        assert ConfigSetting.objects.get_effective("notion_token_pass_key", scope="acme") == "operator/notion"

    def test_an_existing_global_route_is_not_shadowed(self) -> None:
        ConfigSetting.objects.set_value("notion_token_pass_key", "global/notion")
        overlay = _declaring("notion_token", "store/declared", scope="acme")

        with patch("teatree.core.overlay_loader.get_all_overlays", return_value={"acme": overlay}):
            call_command("provision_declared_notion_routing")

        assert not ConfigSetting.objects.filter(scope="acme", key="notion_token_pass_key").exists()
