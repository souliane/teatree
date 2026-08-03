"""Token resolution: env first, then the overlay's `pass` entry, then fail loud."""

import pytest

from teatree.backends.notion.credentials import build_notion_client, resolve_notion_token
from teatree.backends.notion.errors import NotionTokenMissingError


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

    def test_the_default_pass_entry_is_the_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NOTION_TOKEN", raising=False)
        monkeypatch.setattr("teatree.backends.notion.credentials.overlay_notion_pass_key", lambda _: "")
        monkeypatch.setattr(
            "teatree.llm.credentials.read_pass",
            lambda key: "from-default-entry" if key == "notion/integration-token" else "",
        )

        assert resolve_notion_token() == "from-default-entry"


class TestFailLoud:
    def test_an_absent_token_names_the_whole_one_time_setup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NOTION_TOKEN", raising=False)
        monkeypatch.setattr("teatree.backends.notion.credentials.overlay_notion_pass_key", lambda _: "")
        monkeypatch.setattr("teatree.llm.credentials.read_pass", lambda _: "")

        with pytest.raises(NotionTokenMissingError) as caught:
            build_notion_client()

        message = str(caught.value)
        assert "pass insert notion/integration-token" in message
        assert "share every page and database" in message.lower()
        assert caught.value.exit_code == 3

    def test_the_token_value_never_appears_in_the_failure_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("NOTION_TOKEN", raising=False)
        monkeypatch.setattr("teatree.backends.notion.credentials.overlay_notion_pass_key", lambda _: "")
        monkeypatch.setattr("teatree.llm.credentials.read_pass", lambda _: "")

        with pytest.raises(NotionTokenMissingError) as caught:
            resolve_notion_token()

        assert "ntn_" not in str(caught.value), "a diagnostic must never carry a secret shape"
