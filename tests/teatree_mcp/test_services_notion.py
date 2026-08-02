"""The Notion MCP tool refuses a dead page instead of answering with its status."""

from unittest.mock import patch

import pytest

from teatree.mcp.services_notion import _live_page_status
from teatree.types import RawAPIDict


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
        return "In Progress (dev/config)"

    def update_page_status(self, page_id: str, *, property_name: str, value: str) -> RawAPIDict:
        _ = (page_id, property_name, value)
        return {}


def test_a_dead_page_is_refused_and_its_status_never_read() -> None:
    client = _StubClient(live=False)

    with (
        patch("teatree.mcp.services_notion._client", return_value=client),
        pytest.raises(RuntimeError, match="not a source at all"),
    ):
        _live_page_status("page-1", "Status")

    assert client.reads == [], "the status of a dead page must not even be fetched"


def test_a_live_page_still_answers() -> None:
    client = _StubClient(live=True)

    with patch("teatree.mcp.services_notion._client", return_value=client):
        assert _live_page_status("page-1", "Status") == "In Progress (dev/config)"
