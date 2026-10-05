"""Which overlay's Notion identity a `t3 notion` call runs as when none is named."""

import types

import pytest
from django.core.exceptions import ImproperlyConfigured

from teatree.backends.types import Service
from teatree.core.overlays.notion_identity import notion_overlay, route_notion_token_command

_BOT_ENTRY = "notion/integration-token"


def _overlay(entry: str, *, needs_notion: bool = False) -> types.SimpleNamespace:
    config = types.SimpleNamespace(
        required_third_party_services=frozenset({Service.NOTION}) if needs_notion else frozenset(),
        secret_pass_key=lambda name: entry if name == "notion_token" else "",
    )
    return types.SimpleNamespace(config=config)


@pytest.fixture
def registered(monkeypatch: pytest.MonkeyPatch) -> dict[str, types.SimpleNamespace]:
    overlays: dict[str, types.SimpleNamespace] = {}
    monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.setattr("teatree.core.overlay_loader.get_all_overlays", lambda: overlays)
    return overlays


class TestUnnamed:
    def test_the_only_overlay_routing_notion_is_the_default(self, registered: dict) -> None:
        registered.update({"t3-acme": _overlay(_BOT_ENTRY, needs_notion=True), "t3-teatree": _overlay("")})

        assert notion_overlay("") == "t3-acme"

    def test_overlays_sharing_one_entry_run_as_the_one_that_declares_notion(self, registered: dict) -> None:
        registered.update({"t3-acme": _overlay(_BOT_ENTRY, needs_notion=True), "t3-teatree": _overlay(_BOT_ENTRY)})

        assert notion_overlay("") == "t3-acme"

    def test_two_identities_are_never_picked_between(self, registered: dict) -> None:
        registered.update({"t3-a": _overlay("a/notion", needs_notion=True), "t3-b": _overlay("b/notion")})

        with pytest.raises(ImproperlyConfigured) as caught:
            notion_overlay("")

        message = str(caught.value)
        assert "t3-a (pass a/notion)" in message
        assert "t3-b (pass b/notion)" in message
        assert "--overlay t3-a" in message

    def test_one_entry_with_no_single_declarer_is_refused_too(self, registered: dict) -> None:
        registered.update({"t3-a": _overlay(_BOT_ENTRY), "t3-b": _overlay(_BOT_ENTRY)})

        with pytest.raises(ImproperlyConfigured):
            notion_overlay("")

    def test_no_overlay_routing_notion_leaves_only_the_environment_token(self, registered: dict) -> None:
        registered.update({"t3-teatree": _overlay("")})

        assert notion_overlay("") is None


class TestAnEnvironmentTokenDecidesTheIdentity:
    def test_an_exported_token_is_one_identity_so_the_declaring_overlay_supplies_the_roots(
        self, registered: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registered.update({"t3-a": _overlay("a/notion", needs_notion=True), "t3-b": _overlay("b/notion")})
        monkeypatch.setenv("NOTION_TOKEN", "exported")

        assert notion_overlay("") == "t3-a"

    def test_the_working_directory_never_picks_between_two_identities(
        self, registered: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        registered.update({"t3-a": _overlay("a/notion", needs_notion=True), "t3-b": _overlay("b/notion")})
        monkeypatch.setattr("teatree.config.discover_active_overlay", lambda: types.SimpleNamespace(name="t3-b"))

        with pytest.raises(ImproperlyConfigured):
            notion_overlay("")


class TestNamed:
    def test_a_short_alias_remains_unresolved(self) -> None:
        assert notion_overlay("teatree") == "teatree"

    def test_the_environment_pin_is_an_explicit_name(self, registered: dict, monkeypatch: pytest.MonkeyPatch) -> None:
        registered.update({"t3-acme": _overlay(_BOT_ENTRY, needs_notion=True)})
        monkeypatch.setenv("T3_OVERLAY_NAME", "t3-teatree")

        assert notion_overlay("") == "t3-teatree"


class TestRouteCommand:
    def test_the_command_uses_the_cli_prefix_the_overlay_is_reachable_under(self) -> None:
        assert route_notion_token_command("t3-acme") == (
            "t3 acme config_setting set notion_token_pass_key '\"<entry>\"' --overlay t3-acme"
        )
