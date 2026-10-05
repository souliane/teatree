"""The seam between the MCP tools and the Notion write service: every failure leaves as a ``NotionToolError``."""

from unittest.mock import patch

import httpx
import pytest

from teatree.backends.notion.errors import NotionAnchorError
from teatree.cli.notion_mcp_seam import register
from teatree.cli.notion_write_service import NotionWrites
from teatree.mcp import services_notion
from teatree.mcp.services_notion import NotionToolError, notion_write_seam


@pytest.fixture(autouse=True)
def _registered(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTION_TOKEN", "test-token")
    original = services_notion._factory_registry.factory
    monkeypatch.setattr(services_notion._factory_registry, "factory", original)
    register()


def _replace() -> None:
    notion_write_seam("").replace("page", "old", "new", dry_run=True)


@pytest.mark.parametrize(
    ("raised", "text"),
    [
        (NotionAnchorError("two matches"), "notion exit 19 (NotionAnchorError): two matches"),
        (ValueError("old_text is empty"), "notion exit 1 (ValueError): old_text is empty"),
        (httpx.ConnectError("no route"), "notion exit 1 (ConnectError): no route"),
        (
            httpx.HTTPStatusError("x", request=httpx.Request("GET", "https://x.test"), response=httpx.Response(502)),
            "notion exit 1 (HTTPStatusError): HTTP 502",
        ),
    ],
)
def test_each_failure_family_keeps_its_own_exit_code_and_condition(raised: Exception, text: str) -> None:
    with patch.object(NotionWrites, "replace", side_effect=raised), pytest.raises(NotionToolError) as caught:
        _replace()

    assert str(caught.value) == text


def test_an_error_outside_the_notion_taxonomy_is_not_swallowed() -> None:
    with patch.object(NotionWrites, "replace", side_effect=KeyError("bug")), pytest.raises(KeyError):
        _replace()
