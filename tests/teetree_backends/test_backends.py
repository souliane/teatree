from typing import Self

import httpx
import pytest
from django.test import override_settings

from teetree.backends import gitlab, notion, slack


def test_slack_backend_posts_webhook_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: dict[str, object] = {}

    def fake_post(url: str, *, json: dict[str, object], timeout: float) -> httpx.Response:
        sent["url"] = url
        sent["json"] = json
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

    monkeypatch.setattr(slack.httpx, "post", fake_post)

    assert slack.post_webhook_message("https://hooks.slack.test/123", "TeaTree ready") == {"ok": True}
    assert sent["json"] == {"text": "TeaTree ready"}


def test_notion_backend_fetches_page_with_version_header(monkeypatch: pytest.MonkeyPatch) -> None:
    class DummyClient:
        def __init__(self, *, headers: dict[str, str], timeout: float) -> None:
            self.headers = headers
            self.timeout = timeout

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def get(self, path: str) -> httpx.Response:
            assert path == "https://api.notion.com/v1/pages/page-123"
            return httpx.Response(200, json={"id": "page-123"}, request=httpx.Request("GET", path))

    monkeypatch.setattr(notion.httpx, "Client", DummyClient)

    client = notion.NotionClient(token="secret", version="2022-06-28")

    assert client.get_page("page-123") == {"id": "page-123"}


@override_settings(TEATREE_GITLAB_URL="https://gitlab.example.com/api/v4", TEATREE_GITLAB_TOKEN="gl-token")
def test_gitlab_backend_builds_client_from_settings() -> None:
    client = gitlab.get_client()

    assert client.base_url == "https://gitlab.example.com/api/v4"
    assert client.token == "gl-token"
