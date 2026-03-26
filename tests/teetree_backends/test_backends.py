from typing import Self
from unittest.mock import MagicMock

import httpx
import pytest
from django.test import override_settings

from teetree.backends import gitlab, notion, slack
from teetree.utils.gitlab_api import GitLabAPI


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


def test_gitlab_code_host_update_mr_note(monkeypatch: pytest.MonkeyPatch) -> None:

    mock_client = MagicMock()
    mock_client.resolve_project.return_value = MagicMock(project_id=42, default_branch="main")
    mock_client.put_json.return_value = {"id": 99}

    host = gitlab.GitLabCodeHost(client=mock_client)
    result = host.update_mr_note(repo="org/repo", mr_iid=5, note_id=99, body="Updated")

    assert result == {"id": 99}
    mock_client.put_json.assert_called_once()


def test_gitlab_code_host_update_mr_note_no_project(monkeypatch: pytest.MonkeyPatch) -> None:

    mock_client = MagicMock()
    mock_client.resolve_project.return_value = None

    host = gitlab.GitLabCodeHost(client=mock_client)
    result = host.update_mr_note(repo="bad/repo", mr_iid=5, note_id=99, body="x")

    assert "error" in result


def test_gitlab_code_host_list_mr_notes() -> None:

    mock_client = MagicMock()
    mock_client.resolve_project.return_value = MagicMock(project_id=42, default_branch="main")
    mock_client.get_json.return_value = [{"id": 1, "body": "note"}]

    host = gitlab.GitLabCodeHost(client=mock_client)
    result = host.list_mr_notes(repo="org/repo", mr_iid=5)

    assert len(result) == 1


def test_gitlab_code_host_list_mr_notes_no_project() -> None:

    mock_client = MagicMock()
    mock_client.resolve_project.return_value = None

    host = gitlab.GitLabCodeHost(client=mock_client)
    assert host.list_mr_notes(repo="bad/repo", mr_iid=5) == []


def test_gitlab_code_host_upload_file() -> None:

    mock_client = MagicMock()
    mock_client.resolve_project.return_value = MagicMock(project_id=42, default_branch="main")
    mock_client.upload_file.return_value = {"markdown": "![img](/uploads/abc/img.png)"}

    host = gitlab.GitLabCodeHost(client=mock_client)
    result = host.upload_file(repo="org/repo", filepath="/tmp/img.png")

    assert result["markdown"] == "![img](/uploads/abc/img.png)"


def test_gitlab_code_host_upload_file_no_project() -> None:

    mock_client = MagicMock()
    mock_client.resolve_project.return_value = None

    host = gitlab.GitLabCodeHost(client=mock_client)
    result = host.upload_file(repo="bad/repo", filepath="/tmp/img.png")

    assert "error" in result


def test_gitlab_api_put_json(monkeypatch: pytest.MonkeyPatch) -> None:

    def fake_put(url: str, *, headers: dict, json: dict, timeout: float) -> httpx.Response:
        return httpx.Response(200, json={"id": 1}, request=httpx.Request("PUT", url))

    monkeypatch.setattr(httpx, "put", fake_put)

    api = GitLabAPI(token="test", base_url="https://gl.test/api/v4")
    result = api.put_json("projects/1/notes/2", {"body": "x"})
    assert result == {"id": 1}


def test_gitlab_api_put_json_no_token() -> None:

    api = GitLabAPI(token="", base_url="https://gl.test/api/v4")
    assert api.put_json("endpoint") is None


def test_gitlab_api_upload_file(monkeypatch: pytest.MonkeyPatch, tmp_path: pytest.TempPathFactory) -> None:

    img = tmp_path / "test.png"
    img.write_bytes(b"PNG")

    def fake_post(url: str, *, headers: dict, files: dict, timeout: float) -> httpx.Response:
        return httpx.Response(
            200, json={"markdown": "![test](/uploads/x/test.png)"}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx, "post", fake_post)

    api = GitLabAPI(token="test", base_url="https://gl.test/api/v4")
    result = api.upload_file(42, str(img))
    assert result is not None
    assert result["markdown"] == "![test](/uploads/x/test.png)"


def test_gitlab_api_upload_file_no_token() -> None:

    api = GitLabAPI(token="", base_url="https://gl.test/api/v4")
    assert api.upload_file(42, "/tmp/x.png") is None
