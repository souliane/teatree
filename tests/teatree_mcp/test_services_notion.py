"""The Notion MCP tool refuses a dead page instead of answering with its status."""

from unittest.mock import patch

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from teatree.mcp import services_notion
from teatree.mcp.services_notion import NotionToolError, _live_page_status


class _StubClient:
    def __init__(self, *, live: bool) -> None:
        self._live = live
        self.reads: list[str] = []

    def page_is_live(self, page_id: str) -> bool:
        _ = page_id
        return self._live

    def get_page_status(self, page_id: str, *, property_name: str = "Status") -> str | None:
        _ = property_name
        self.reads.append(page_id)
        return "Underway (sample)"


def test_a_dead_page_is_refused_and_its_status_never_read() -> None:
    client = _StubClient(live=False)

    with (
        patch("teatree.mcp.services_notion._client", return_value=client),
        pytest.raises(ToolError, match="not a source at all"),
    ):
        _live_page_status("page-1", "Status")

    assert client.reads == [], "the status of a dead page must not even be fetched"


def test_a_live_page_still_answers() -> None:
    client = _StubClient(live=True)

    with patch("teatree.mcp.services_notion._client", return_value=client):
        assert _live_page_status("page-1", "Status") == "Underway (sample)"


class TestWriteSeamRegistration:
    def test_an_unregistered_seam_fails_loud(self) -> None:
        original = services_notion._factory_registry.factory
        services_notion.register_notion_write_seam(services_notion._unregistered_factory)
        try:
            with pytest.raises(RuntimeError, match="not registered"):
                services_notion.notion_write_seam("")
        finally:
            services_notion.register_notion_write_seam(original)


class TestNotionToolError:
    def test_the_text_carries_the_exit_code_and_the_condition_and_survives_as_a_tool_error(self) -> None:
        error = NotionToolError(23, "NotionWriteNotApprovedError", "record one")

        assert isinstance(error, ToolError)
        assert error.exit_code == 23
        assert str(error) == "notion exit 23 (NotionWriteNotApprovedError): record one"
