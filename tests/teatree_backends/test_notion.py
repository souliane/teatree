"""Notion file download and API client — uses Brave cookies for the file CDN."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from teatree.backends.notion import NotionClient, _brave_cookies, download_notion_file


@dataclass
class FakeCookie:
    name: str
    value: str
    domain: str


class TestBraveCookies:
    def test_translates_browser_cookies_to_httpx_jar(self) -> None:
        fake_jar = [
            FakeCookie("token_v2", "abc", ".notion.so"),
            FakeCookie("file_token", "xyz", ".file.notion.so"),
        ]
        fake_module = type("M", (), {"brave": staticmethod(lambda domain_name: fake_jar)})
        with patch.dict("sys.modules", {"browser_cookie3": fake_module}):
            cookies = _brave_cookies("notion.so")
        assert cookies.get("token_v2", domain=".notion.so") == "abc"
        assert cookies.get("file_token", domain=".file.notion.so") == "xyz"


class TestDownloadNotionFile:
    def test_writes_response_bytes_to_dest(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["user_agent"] = request.headers.get("User-Agent", "")
            return httpx.Response(200, content=b"FILE-BYTES")

        empty_cookies = httpx.Cookies()
        monkeypatch.setattr("teatree.backends.notion._brave_cookies", lambda _: empty_cookies)
        original_client_init = httpx.Client.__init__

        def patched_init(self: httpx.Client, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(handler)
            original_client_init(self, **kwargs)

        monkeypatch.setattr(httpx.Client, "__init__", patched_init)

        dest = tmp_path / "sub" / "file.pdf"
        result = download_notion_file(
            space_id="s",
            attachment_id="a",
            block_id="b",
            filename="file.pdf",
            dest=dest,
        )
        assert result == dest
        assert dest.read_bytes() == b"FILE-BYTES"
        assert "file.notion.so/f/f/s/a/file.pdf" in captured["url"]
        assert "table=block" in captured["url"]
        assert "id=b" in captured["url"]
        assert "spaceId=s" in captured["url"]
        assert "Mozilla" in captured["user_agent"]

    def test_raises_on_http_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(403, content=b"forbidden")

        empty_cookies = httpx.Cookies()
        monkeypatch.setattr("teatree.backends.notion._brave_cookies", lambda _: empty_cookies)
        original_client_init = httpx.Client.__init__

        def patched_init(self: httpx.Client, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(handler)
            original_client_init(self, **kwargs)

        monkeypatch.setattr(httpx.Client, "__init__", patched_init)

        with pytest.raises(httpx.HTTPStatusError):
            download_notion_file(
                space_id="s",
                attachment_id="a",
                block_id="b",
                filename="file.pdf",
                dest=tmp_path / "f.pdf",
            )


class TestNotionClient:
    def test_get_page_returns_json_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("Authorization", "")
            seen["version"] = request.headers.get("Notion-Version", "")
            return httpx.Response(200, json={"object": "page", "id": "page-1"})

        original_client_init = httpx.Client.__init__

        def patched_init(self: httpx.Client, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(handler)
            original_client_init(self, **kwargs)

        monkeypatch.setattr(httpx.Client, "__init__", patched_init)

        client = NotionClient(token="secret", version="2026-01-01")
        body = client.get_page("page-1")
        assert body == {"object": "page", "id": "page-1"}
        assert seen["url"] == "https://api.notion.com/v1/pages/page-1"
        assert seen["auth"] == "Bearer secret"
        assert seen["version"] == "2026-01-01"
