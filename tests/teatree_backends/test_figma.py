from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
from PIL import Image

from teatree.backends.figma import (
    FigmaClient,
    FigmaComponentMetadata,
    FigmaFrameRef,
    build_side_by_side_comparison,
    download_image,
)


def _mock_response(json_data: object, status_code: int = 200) -> httpx.Response:
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.json.return_value = json_data
    response.raise_for_status = MagicMock()
    return response


def _mock_http(get_return: httpx.Response) -> MagicMock:
    mock_http = MagicMock()
    mock_http.__enter__ = MagicMock(return_value=mock_http)
    mock_http.__exit__ = MagicMock(return_value=False)
    mock_http.get.return_value = get_return
    return mock_http


class TestGetFile:
    def test_returns_file_json(self) -> None:
        client = FigmaClient(token="fake")
        mock_http = _mock_http(_mock_response({"name": "My File", "document": {}}))

        with patch.object(client, "_client", return_value=mock_http):
            body = client.get_file("abc123")

        assert body == {"name": "My File", "document": {}}
        mock_http.get.assert_called_once_with("/v1/files/abc123")


class TestGetNode:
    def test_returns_document_subtree(self) -> None:
        client = FigmaClient(token="fake")
        mock_http = _mock_http(
            _mock_response({"nodes": {"1:2": {"document": {"id": "1:2", "name": "Frame", "type": "FRAME"}}}})
        )

        with patch.object(client, "_client", return_value=mock_http):
            document = client.get_node("abc123", "1:2")

        assert document == {"id": "1:2", "name": "Frame", "type": "FRAME"}
        mock_http.get.assert_called_once_with("/v1/files/abc123/nodes", params={"ids": "1:2"})

    def test_raises_when_node_absent(self) -> None:
        client = FigmaClient(token="fake")
        mock_http = _mock_http(_mock_response({"nodes": {}}))

        with patch.object(client, "_client", return_value=mock_http), pytest.raises(ValueError, match="not found"):
            client.get_node("abc123", "9:9")


class TestListFrameChildren:
    def test_returns_frame_refs_for_children(self) -> None:
        client = FigmaClient(token="fake")
        document = {
            "id": "1:1",
            "children": [
                {"id": "1:2", "name": "Header", "type": "FRAME"},
                {"id": "1:3", "name": "Body", "type": "GROUP"},
            ],
        }
        with patch.object(client, "get_node", return_value=document):
            frames = client.list_frame_children("abc123", "1:1")

        assert frames == [
            FigmaFrameRef(node_id="1:2", name="Header", node_type="FRAME"),
            FigmaFrameRef(node_id="1:3", name="Body", node_type="GROUP"),
        ]

    def test_returns_empty_list_when_no_children(self) -> None:
        client = FigmaClient(token="fake")
        with patch.object(client, "get_node", return_value={"id": "1:1"}):
            assert client.list_frame_children("abc123", "1:1") == []


class TestGetImageUrls:
    def test_returns_image_url_map(self) -> None:
        client = FigmaClient(token="fake")
        mock_http = _mock_http(_mock_response({"images": {"1:2": "https://cdn.figma.com/x.png"}, "err": None}))

        with patch.object(client, "_client", return_value=mock_http):
            urls = client.get_image_urls("abc123", ["1:2"], scale=2.0, image_format="png")

        assert urls == {"1:2": "https://cdn.figma.com/x.png"}
        mock_http.get.assert_called_once_with("/v1/images/abc123", params={"ids": "1:2", "scale": 2.0, "format": "png"})

    def test_raises_on_render_error(self) -> None:
        client = FigmaClient(token="fake")
        mock_http = _mock_http(_mock_response({"err": "boom", "images": None}))

        with (
            patch.object(client, "_client", return_value=mock_http),
            pytest.raises(RuntimeError, match="boom"),
        ):
            client.get_image_urls("abc123", ["1:2"])


class TestGetScreenshot:
    def test_downloads_rendered_image(self, tmp_path: Path) -> None:
        client = FigmaClient(token="fake")
        dest = tmp_path / "out.png"
        captured: dict[str, Any] = {}

        def fake_download(url: str, dest_path: Path) -> Path:
            captured["url"] = url
            captured["dest"] = dest_path
            return dest_path

        with (
            patch.object(client, "get_image_urls", return_value={"1:2": "https://cdn.figma.com/x.png"}),
            patch("teatree.backends.figma.download_image", side_effect=fake_download),
        ):
            result = client.get_screenshot("abc123", "1:2", dest, scale=3.0)

        assert result == dest
        assert captured["url"] == "https://cdn.figma.com/x.png"
        assert captured["dest"] == dest

    def test_raises_when_node_has_no_rendered_url(self, tmp_path: Path) -> None:
        client = FigmaClient(token="fake")
        with (
            patch.object(client, "get_image_urls", return_value={}),
            pytest.raises(RuntimeError, match="no rendered image URL"),
        ):
            client.get_screenshot("abc123", "1:2", tmp_path / "out.png")


class TestGetComments:
    def test_returns_comment_list(self) -> None:
        client = FigmaClient(token="fake")
        mock_http = _mock_http(_mock_response({"comments": [{"id": "c1", "message": "hi"}]}))

        with patch.object(client, "_client", return_value=mock_http):
            comments = client.get_comments("abc123")

        assert comments == [{"id": "c1", "message": "hi"}]
        mock_http.get.assert_called_once_with("/v1/files/abc123/comments")


class TestGetNodeComments:
    def test_filters_by_client_meta_node_id(self) -> None:
        client = FigmaClient(token="fake")
        comments = [
            {"id": "c1", "client_meta": {"node_id": "1:2"}, "message": "on frame"},
            {"id": "c2", "client_meta": {"node_id": "1:3"}, "message": "elsewhere"},
            {"id": "c3", "message": "no client_meta at all"},
        ]

        with patch.object(client, "get_comments", return_value=comments):
            filtered = client.get_node_comments("abc123", "1:2")

        assert filtered == [comments[0]]


class TestGetComponentMetadata:
    def test_extracts_components_sets_and_styles(self) -> None:
        client = FigmaClient(token="fake")
        file_data = {
            "components": {"1:2": {"name": "Button"}},
            "componentSets": {"1:1": {"name": "Button variants"}},
            "styles": {"S:1": {"name": "Primary/Blue"}},
        }

        with patch.object(client, "get_file", return_value=file_data):
            metadata = client.get_component_metadata("abc123")

        assert metadata == FigmaComponentMetadata(
            components={"1:2": {"name": "Button"}},
            component_sets={"1:1": {"name": "Button variants"}},
            styles={"S:1": {"name": "Primary/Blue"}},
        )

    def test_defaults_to_empty_when_keys_missing(self) -> None:
        client = FigmaClient(token="fake")
        with patch.object(client, "get_file", return_value={"name": "My File"}):
            metadata = client.get_component_metadata("abc123")

        assert metadata == FigmaComponentMetadata(components={}, component_sets={}, styles={})


class TestClientFactory:
    def test_client_method_creates_httpx_client_with_token_header(self) -> None:
        client = FigmaClient(token="my-token", base_url="https://api.figma.example.com/")

        with client._client() as http_client:
            assert isinstance(http_client, httpx.Client)
            assert http_client.headers["x-figma-token"] == "my-token"
        assert client.base_url == "https://api.figma.example.com"


class TestDownloadImage:
    def _patch_transport(self, monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
        original_init = httpx.Client.__init__

        def patched_init(self: httpx.Client, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(handler)
            original_init(self, **kwargs)

        monkeypatch.setattr(httpx.Client, "__init__", patched_init)

    def test_writes_response_bytes_to_dest(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_transport(monkeypatch, lambda _request: httpx.Response(200, content=b"PNG-BYTES"))

        dest = tmp_path / "sub" / "shot.png"
        result = download_image("https://cdn.figma.com/x.png", dest)

        assert result == dest
        assert dest.read_bytes() == b"PNG-BYTES"

    def test_raises_on_http_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_transport(monkeypatch, lambda _request: httpx.Response(404, content=b"not found"))

        with pytest.raises(httpx.HTTPStatusError):
            download_image("https://cdn.figma.com/missing.png", tmp_path / "x.png")


class TestBuildSideBySideComparison:
    def test_combines_two_images_horizontally(self, tmp_path: Path) -> None:
        design = tmp_path / "design.png"
        actual = tmp_path / "actual.png"
        Image.new("RGB", (100, 50), color="red").save(design)
        Image.new("RGB", (60, 80), color="blue").save(actual)

        dest = tmp_path / "out" / "comparison.png"
        result = build_side_by_side_comparison(design, actual, dest)

        assert result == dest
        with Image.open(dest) as combined:
            assert combined.size == (160, 80)
            assert combined.getpixel((10, 10)) == (255, 0, 0)
            assert combined.getpixel((110, 10)) == (0, 0, 255)
