"""Notion file download and API client — uses Brave cookies for the file CDN."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import typer.testing

from teatree.backends.notion import (
    NotionClient,
    NotionFileRef,
    _brave_cookies,
    _is_signed,
    download_notion_file,
    resolve_signed_url,
)
from teatree.cli.tools import tool_app

# A `file://`-prefixed ref as emitted by `notion-fetch` for a <file> block.
VALID_FETCH_SRC = (
    "file://%7B%22source%22%3A%20%22attachment%3Aatt-123%3AMy%20Report.pdf%22%2C"
    "%20%22permissionRecord%22%3A%20%7B%22table%22%3A%20%22block%22%2C%20%22id%22"
    "%3A%20%22blk-9%22%2C%20%22spaceId%22%3A%20%22sp-7%22%7D%7D"
)


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


def _patch_notion_transport(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    original_client_init = httpx.Client.__init__

    def patched_init(self: httpx.Client, **kwargs: Any) -> None:
        kwargs["transport"] = httpx.MockTransport(handler)
        original_client_init(self, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", patched_init)


class TestNotionClient:
    def test_get_page_returns_json_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers.get("Authorization", "")
            seen["version"] = request.headers.get("Notion-Version", "")
            return httpx.Response(200, json={"object": "page", "id": "page-1"})

        _patch_notion_transport(monkeypatch, handler)

        client = NotionClient(token="secret", version="2026-01-01")
        body = client.get_page("page-1")
        assert body == {"object": "page", "id": "page-1"}
        assert seen["url"] == "https://api.notion.com/v1/pages/page-1"
        assert seen["auth"] == "Bearer secret"
        assert seen["version"] == "2026-01-01"

    @pytest.mark.parametrize(
        ("prop", "expected"),
        [
            ({"type": "status", "status": {"name": "In review"}}, "In review"),
            ({"type": "select", "select": {"name": "Done"}}, "Done"),
            ({"type": "status", "status": None}, None),
            ({"type": "select", "select": None}, None),
        ],
    )
    def test_get_page_status_parses_status_and_select(
        self, monkeypatch: pytest.MonkeyPatch, prop: dict[str, Any], expected: str | None
    ) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"object": "page", "properties": {"Status": prop}})

        _patch_notion_transport(monkeypatch, handler)

        status = NotionClient(token="secret").get_page_status("pg-9", property_name="Status")
        assert status == expected
        assert seen["url"] == "https://api.notion.com/v1/pages/pg-9"

    @pytest.mark.parametrize(
        "page",
        [
            {"properties": {}},  # property missing
            {"object": "page"},  # no properties key at all
            {"properties": {"Status": {"status": {"name": None}}}},  # option name not a string
        ],
    )
    def test_get_page_status_returns_none_when_unreadable(
        self, monkeypatch: pytest.MonkeyPatch, page: dict[str, Any]
    ) -> None:
        _patch_notion_transport(monkeypatch, lambda _: httpx.Response(200, json=page))
        assert NotionClient(token="secret").get_page_status("pg-9", property_name="Status") is None

    def test_query_database_stops_when_has_more_but_no_cursor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(200, json={"results": [{"id": "row-1"}], "has_more": True})

        _patch_notion_transport(monkeypatch, handler)

        rows = NotionClient(token="secret").query_database("db-1")

        assert [r["id"] for r in rows] == ["row-1"]
        assert len(calls) == 1

    def test_query_database_posts_and_paginates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[dict[str, Any]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode())
            calls.append({"url": str(request.url), "method": request.method, "body": body})
            if body.get("start_cursor") == "cur-2":
                return httpx.Response(200, json={"results": [{"id": "row-2"}], "has_more": False})
            return httpx.Response(200, json={"results": [{"id": "row-1"}], "has_more": True, "next_cursor": "cur-2"})

        _patch_notion_transport(monkeypatch, handler)

        rows = NotionClient(token="secret").query_database("db-1", db_filter={"property": "Status"})

        assert [r["id"] for r in rows] == ["row-1", "row-2"]
        assert len(calls) == 2
        assert calls[0]["method"] == "POST"
        assert calls[0]["url"] == "https://api.notion.com/v1/databases/db-1/query"
        assert calls[0]["body"]["filter"] == {"property": "Status"}
        assert calls[1]["body"]["start_cursor"] == "cur-2"

    def test_update_page_status_issues_patch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["method"] = request.method
            seen["auth"] = request.headers.get("Authorization", "")
            seen["version"] = request.headers.get("Notion-Version", "")
            seen["body"] = json.loads(request.content.decode())
            return httpx.Response(200, json={"object": "page", "id": "pg-9"})

        _patch_notion_transport(monkeypatch, handler)

        NotionClient(token="secret").update_page_status("pg-9", property_name="Status", value="Merged")

        assert seen["method"] == "PATCH"
        assert seen["url"] == "https://api.notion.com/v1/pages/pg-9"
        assert seen["auth"] == "Bearer secret"
        assert seen["version"]
        assert seen["body"]["properties"]["Status"]["status"]["name"] == "Merged"


class TestNotionFileRef:
    def test_parses_valid_fetch_src(self) -> None:
        ref = NotionFileRef.from_fetch_src(VALID_FETCH_SRC)

        assert ref is not None
        assert ref.space_id == "sp-7"
        assert ref.attachment_id == "att-123"
        assert ref.filename == "My Report.pdf"
        assert ref.block_id == "blk-9"

    def test_stored_url_uses_s3_origin_form(self) -> None:
        ref = NotionFileRef.from_fetch_src(VALID_FETCH_SRC)

        assert ref is not None
        assert ref.stored_url == ("https://prod-files-secure.s3.us-west-2.amazonaws.com/sp-7/att-123/My Report.pdf")

    @pytest.mark.parametrize(
        "bad",
        [
            "file://not-json",
            "file://%7B%22source%22%3A%20%22nope%22%7D",  # not an attachment:
            "https://file.notion.so/f/f/s/a/x.pdf?signature=abc",  # plain URL, not a ref
        ],
    )
    def test_returns_none_for_unparseable(self, bad: str) -> None:
        assert NotionFileRef.from_fetch_src(bad) is None


class TestIsSigned:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://file.notion.so/f/f/s/a/x.pdf?signature=abc", True),
            ("https://x/y?X-Amz-Signature=z", True),
            ("https://x/y?expirationTimestamp=9999", True),
            ("https://prod-files-secure.s3.amazonaws.com/s/a/x.pdf", False),
        ],
    )
    def test_detects_signature_markers(self, url: str, *, expected: bool) -> None:
        assert _is_signed(url) is expected


class TestResolveSignedUrl:
    def _patch(self, monkeypatch: pytest.MonkeyPatch, handler: Any) -> dict[str, Any]:
        original = httpx.Client.__init__

        def patched(self: httpx.Client, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(handler)
            original(self, **kwargs)

        monkeypatch.setattr(httpx.Client, "__init__", patched)
        return {}

    def test_posts_stored_url_and_returns_signed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = request.content.decode()
            return httpx.Response(200, json={"signedUrls": ["https://signed.example/x.pdf?sig=1"]})

        self._patch(monkeypatch, handler)
        ref = NotionFileRef.from_fetch_src(VALID_FETCH_SRC)
        assert ref is not None

        signed = resolve_signed_url(ref, httpx.Cookies())

        assert signed == "https://signed.example/x.pdf?sig=1"
        assert seen["url"] == "https://www.notion.so/api/v3/getSignedFileUrls"
        assert "prod-files-secure.s3" in seen["body"]
        assert "blk-9" in seen["body"]

    def test_raises_when_no_signed_url_in_body(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch(monkeypatch, lambda _: httpx.Response(200, json={"other": []}))
        ref = NotionFileRef.from_fetch_src(VALID_FETCH_SRC)
        assert ref is not None

        with pytest.raises(RuntimeError, match="no signed URL"):
            resolve_signed_url(ref, httpx.Cookies())


class TestDownloadViaRef:
    def test_resolves_ref_then_downloads(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.backends.notion._brave_cookies", lambda _: httpx.Cookies())

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v3/getSignedFileUrls":
                return httpx.Response(200, json={"signedUrls": ["https://signed/x.pdf?signature=ok"]})
            return httpx.Response(200, content=b"REF-BYTES")

        original = httpx.Client.__init__

        def patched(self: httpx.Client, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(handler)
            original(self, **kwargs)

        monkeypatch.setattr(httpx.Client, "__init__", patched)
        ref = NotionFileRef.from_fetch_src(VALID_FETCH_SRC)

        dest = download_notion_file(ref=ref, dest=tmp_path / "out.pdf")

        assert dest.read_bytes() == b"REF-BYTES"

    def test_rejects_unsigned_url_without_ref(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("teatree.backends.notion._brave_cookies", lambda _: httpx.Cookies())

        with pytest.raises(ValueError, match="not signed"):
            download_notion_file(url="https://plain.example/x.pdf", dest=tmp_path / "x.pdf")


class TestNotionDownloadCLIRef:
    def test_detects_fetch_ref_and_resolves(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        def fake_download(*, ref: Any = None, url: str = "", dest: Path) -> Path:
            captured["ref"] = ref
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"PDF")
            return dest

        monkeypatch.setattr("teatree.backends.notion.download_notion_file", fake_download)

        result = typer.testing.CliRunner().invoke(
            tool_app, ["notion-download", VALID_FETCH_SRC, "--dest", str(tmp_path)]
        )

        assert result.exit_code == 0, result.output
        assert captured["ref"].filename == "My Report.pdf"
        assert (tmp_path / "My Report.pdf").read_bytes() == b"PDF"
