from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from teatree.core.models import ConfigSetting
from teatree.core.overlay import OverlayConfig


def _declared_overlay(*, route: str = "store/declared", scope: str = "acme") -> SimpleNamespace:
    config = OverlayConfig()
    config._register_secret("notion_token", route)
    config._pass_key_scope = scope
    return SimpleNamespace(config=config)


class ProvisionDeclaredNotionRoutingCommandTests(TestCase):
    def _run(self, overlay: SimpleNamespace) -> None:
        with patch("teatree.core.overlay_loader.get_all_overlays", return_value={"acme": overlay}):
            call_command("provision_declared_notion_routing")

    def test_call_command_persists_a_fresh_declared_route(self) -> None:
        self._run(_declared_overlay())

        row = ConfigSetting.objects.get(scope="acme", key="notion_token_pass_key")
        assert row.value == "store/declared"
        assert row.seeded_by
        assert row.seed_value == "store/declared"

    def test_call_command_is_idempotent(self) -> None:
        overlay = _declared_overlay()

        self._run(overlay)
        self._run(overlay)

        assert ConfigSetting.objects.filter(scope="acme", key="notion_token_pass_key").count() == 1

    def test_call_command_preserves_an_explicit_overlay_override(self) -> None:
        ConfigSetting.objects.set_value("notion_token_pass_key", "operator/notion", scope="acme")

        self._run(_declared_overlay())

        assert ConfigSetting.objects.get_effective("notion_token_pass_key", scope="acme") == "operator/notion"

    def test_call_command_preserves_a_global_override_without_creating_overlay_row(self) -> None:
        ConfigSetting.objects.set_value("notion_token_pass_key", "global/notion")

        self._run(_declared_overlay())

        assert not ConfigSetting.objects.filter(scope="acme", key="notion_token_pass_key").exists()
