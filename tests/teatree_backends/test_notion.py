"""Notion file download and API client — uses Brave cookies for the file CDN."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import typer.testing

from teatree.backends.notion import NotionClient, _brave_cookies, download_notion_file
from teatree.cli.tools import tool_app


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
    SIGNED_URL = (
        "https://file.notion.so/f/f/space-id/attach-id/file.pdf"
        "?table=block&id=block-id&spaceId=space-id"
        "&expirationTimestamp=9999999999&signature=abc"
    )

    def _patch_transport(self, monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
        empty_cookies = httpx.Cookies()
        monkeypatch.setattr("teatree.backends.notion._brave_cookies", lambda _: empty_cookies)
        original_client_init = httpx.Client.__init__

        def patched_init(self: httpx.Client, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(handler)
            original_client_init(self, **kwargs)

        monkeypatch.setattr(httpx.Client, "__init__", patched_init)

    def test_writes_response_bytes_to_dest(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["user_agent"] = request.headers.get("User-Agent", "")
            return httpx.Response(200, content=b"FILE-BYTES")

        self._patch_transport(monkeypatch, handler)

        dest = tmp_path / "sub" / "file.pdf"
        result = download_notion_file(url=self.SIGNED_URL, dest=dest)

        assert result == dest
        assert dest.read_bytes() == b"FILE-BYTES"
        assert "signature=abc" in captured["url"]
        assert "expirationTimestamp=9999999999" in captured["url"]
        assert "Mozilla" in captured["user_agent"]

    def test_raises_on_http_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_transport(monkeypatch, lambda _: httpx.Response(403, content=b"forbidden"))

        with pytest.raises(httpx.HTTPStatusError):
            download_notion_file(url=self.SIGNED_URL, dest=tmp_path / "f.pdf")


class TestNotionDownloadCLI:
    SIGNED_URL = (
        "https://file.notion.so/f/f/space-id/attach-id/Report.pdf"
        "?table=block&id=block-id&spaceId=space-id&signature=abc"
    )

    def test_parses_filename_and_writes_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        def fake_download(*, url: str, dest: Path) -> Path:
            captured["url"] = url
            captured["dest"] = dest
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"PDF")
            return dest

        monkeypatch.setattr("teatree.backends.notion.download_notion_file", fake_download)

        result = typer.testing.CliRunner().invoke(
            tool_app, ["notion-download", self.SIGNED_URL, "--dest", str(tmp_path)]
        )

        assert result.exit_code == 0, result.output
        assert captured["url"] == self.SIGNED_URL
        assert captured["dest"] == tmp_path / "Report.pdf"

    def test_rejects_unparseable_url(self, tmp_path: Path) -> None:
        bogus = "https://file.notion.so/wrong-path?signature=abc"

        result = typer.testing.CliRunner().invoke(tool_app, ["notion-download", bogus, "--dest", str(tmp_path)])

        assert result.exit_code == 1
        assert "Cannot parse file URL" in result.output


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
