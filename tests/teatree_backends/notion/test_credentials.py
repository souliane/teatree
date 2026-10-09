"""Token resolution: env first, then the overlay's `pass` entry, then fail loud."""

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase

from teatree.backends.notion.credentials import build_notion_client, resolve_notion_token
from teatree.backends.notion.errors import NotionTokenMissingError
from teatree.core.models import ConfigSetting
from teatree.core.overlay_loader import get_overlay


class TestResolution:
    def test_the_environment_wins_so_a_rotated_value_beats_a_stale_store(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NOTION_TOKEN", "rotated")
        monkeypatch.setattr("teatree.backends.notion.credentials.overlay_notion_pass_key", lambda _: "notion/stale")

        assert resolve_notion_token() == "rotated"

    def test_the_overlays_own_pass_entry_is_used_when_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NOTION_TOKEN", raising=False)
        monkeypatch.setattr("teatree.backends.notion.credentials.overlay_notion_pass_key", lambda _: "acme/notion")
        monkeypatch.setattr(
            "teatree.llm.credentials.read_pass", lambda key: "from-overlay-entry" if key == "acme/notion" else ""
        )

        assert resolve_notion_token("acme") == "from-overlay-entry"

    def test_an_unconfigured_overlay_reads_no_guessed_entry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NOTION_TOKEN", raising=False)
        monkeypatch.setattr("teatree.backends.notion.credentials.overlay_notion_pass_key", lambda _: "")
        read: list[str] = []
        monkeypatch.setattr("teatree.llm.credentials.read_pass", lambda key: read.append(key) or "guessed")

        with pytest.raises(NotionTokenMissingError):
            resolve_notion_token()

        with pytest.raises(NotionTokenMissingError):
            resolve_notion_token()

        assert read == []


class TestFailLoud:
    def test_an_absent_token_names_the_whole_one_time_setup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NOTION_TOKEN", raising=False)
        monkeypatch.setattr("teatree.backends.notion.credentials.overlay_notion_pass_key", lambda _: "")
        monkeypatch.setattr("teatree.llm.credentials.read_pass", lambda _: "")

        with pytest.raises(NotionTokenMissingError) as caught:
            build_notion_client()

        message = str(caught.value)
        assert "config_setting set notion_token_pass_key" in message
        assert "share every page and database" in message.lower()
        assert caught.value.exit_code == 3

    def test_the_token_value_never_appears_in_the_failure_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NOTION_TOKEN", raising=False)
        monkeypatch.setattr("teatree.backends.notion.credentials.overlay_notion_pass_key", lambda _: "")
        monkeypatch.setattr("teatree.llm.credentials.read_pass", lambda _: "")

        with pytest.raises(NotionTokenMissingError) as caught:
            resolve_notion_token()

        assert "ntn_" not in str(caught.value), "a diagnostic must never carry a secret shape"


class TestTheMissingTokenNamesTheOverlayAndItsFix:
    @pytest.fixture(autouse=True)
    def _no_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NOTION_TOKEN", raising=False)
        monkeypatch.setattr("teatree.llm.credentials.read_pass", lambda _: "")

    def test_an_overlay_routing_no_entry_is_named_with_the_command_that_routes_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("teatree.backends.notion.credentials.overlay_notion_pass_key", lambda _: "")

        with pytest.raises(NotionTokenMissingError) as caught:
            build_notion_client("t3-teatree")

        message = str(caught.value)
        assert "overlay 't3-teatree' routes no notion_token_pass_key" in message
        assert "`t3 teatree config_setting set notion_token_pass_key '\"<entry>\"' --overlay t3-teatree`" in message

    def test_an_empty_routed_entry_is_named_with_the_command_that_stores_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("teatree.backends.notion.credentials.overlay_notion_pass_key", lambda _: "venue/notion")

        with pytest.raises(NotionTokenMissingError) as caught:
            build_notion_client("t3-teatree")

        message = str(caught.value)
        assert "`pass venue/notion`, which overlay 't3-teatree' routes, is empty" in message
        assert "`t3 notion setup --overlay t3-teatree`" in message

    def test_an_unregistered_name_lists_the_overlays_that_exist(self) -> None:
        with pytest.raises(NotionTokenMissingError) as caught:
            build_notion_client("typo")

        assert "no overlay named 'typo' is registered (registered: " in str(caught.value)
        assert "t3-teatree" in str(caught.value)

    def test_two_identities_refuse_with_the_token_missing_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def ambiguous(_requested: str | None) -> str:
            msg = "pick one: --overlay t3-a"
            raise ImproperlyConfigured(msg)

        monkeypatch.setattr("teatree.backends.notion.credentials.notion_overlay", ambiguous)

        with pytest.raises(NotionTokenMissingError) as caught:
            build_notion_client()

        assert "--overlay t3-a" in str(caught.value)
        assert caught.value.exit_code == 3


class TestEveryReadPathResolvesTheSameEntry(TestCase):
    def test_headless_reads_and_the_status_sync_read_the_db_routed_entry(self) -> None:
        ConfigSetting.objects.set_value("notion_token_pass_key", "venue/notion", scope="t3-teatree")
        read: list[str] = []

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.delenv("NOTION_TOKEN", raising=False)
            monkeypatch.setattr("teatree.llm.credentials.read_pass", lambda key: read.append(key) or "tok")
            monkeypatch.setattr("teatree.utils.secrets.read_pass", lambda key: read.append(key) or "tok")
            resolve_notion_token("t3-teatree")
            get_overlay("t3-teatree").config.get_notion_token()

        assert read == ["venue/notion", "venue/notion"]
