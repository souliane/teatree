"""Tests for the shared declaring-overlay → client resolver (#3f-3).

The one resolver the four MCP service groups (forge/slack/notion/sentry) delegate
to: pick the first registered overlay declaring the service with a configured
client, or fail loud naming the service.
"""

from unittest.mock import patch

import pytest

from teatree.backends.types import Service
from teatree.core.overlay import OverlayConfig
from teatree.mcp.service_resolver import resolve_declaring_overlay_client


class _Overlay:
    def __init__(self, *services: Service) -> None:
        self.config = OverlayConfig(required_third_party_services=frozenset(services))


def _overlays(mapping: dict[str, _Overlay]):
    return patch("teatree.mcp.service_resolver.get_all_overlays", return_value=mapping)


class TestResolveDeclaringOverlayClient:
    def test_returns_first_declaring_overlays_configured_client(self) -> None:
        builds: list[str] = []

        def build(name: str) -> str:
            builds.append(name)
            return f"client-for-{name}"

        with _overlays({"a": _Overlay(Service.GITHUB), "b": _Overlay(Service.GITHUB)}):
            client = resolve_declaring_overlay_client(Service.GITHUB, build, description="code host")

        assert client == "client-for-a"
        assert builds == ["a"]  # short-circuits on the first configured declarer

    def test_skips_a_declarer_whose_build_yields_none(self) -> None:
        def build(name: str) -> str | None:
            return None if name == "a" else f"client-for-{name}"

        overlays = {"a": _Overlay(Service.SLACK), "b": _Overlay(Service.SLACK)}
        with _overlays(overlays):
            client = resolve_declaring_overlay_client(Service.SLACK, build, description="Slack messaging backend")

        assert client == "client-for-b"

    def test_ignores_overlays_that_do_not_declare_the_service(self) -> None:
        def build(name: str) -> str:
            return f"client-for-{name}"

        overlays = {"a": _Overlay(Service.NOTION), "b": _Overlay(Service.SENTRY)}
        with _overlays(overlays):
            client = resolve_declaring_overlay_client(Service.SENTRY, build, description="Sentry org")

        assert client == "client-for-b"

    def test_raises_naming_the_description_when_no_declarer_is_configured(self) -> None:
        with (
            _overlays({"a": _Overlay(Service.NOTION)}),
            pytest.raises(RuntimeError, match="No registered overlay declares a configured Notion client"),
        ):
            resolve_declaring_overlay_client(Service.NOTION, lambda _name: None, description="Notion client")

    def test_raises_when_there_are_no_overlays(self) -> None:
        with (
            _overlays({}),
            pytest.raises(RuntimeError, match="github code host"),
        ):
            resolve_declaring_overlay_client(Service.GITHUB, lambda name: name, description="github code host")
