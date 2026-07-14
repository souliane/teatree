import pytest

from teatree.backends.gitlab import api as gitlab_api

# The REST transport (and its ``httpx`` handle) lives in ``http_client`` since the
# api.py transport/domain split; ``api`` is the domain layer on top of it.
from teatree.backends.gitlab import http_client as gitlab_http
from teatree.utils import git


def test_gitlab_api_resolves_remote_project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(git, "remote_url", lambda **kwargs: "git@gitlab.com:acme/platform.git")

    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(
        client,
        "get_json",
        lambda endpoint: {
            "id": 42,
            "path_with_namespace": "acme/platform",
            "path": "platform",
        },
    )

    project = client.resolve_project_from_remote("/tmp/repo")

    assert project is not None
    assert project.project_id == 42
    assert project.short_name == "platform"


def test_gitlab_api_helpers_cover_http_paths_and_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[str] = []

    def fake_get(
        url: str,
        *,
        headers: dict[str, str],
        timeout: float,
    ) -> object:
        requests.append(url)

        class Response:
            def __init__(self) -> None:
                self.headers = {"x-next-page": ""}

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object] | list[dict[str, object]]:
                if url.endswith("projects/acme%2Fplatform"):
                    return {"id": 42, "path_with_namespace": "acme/platform", "path": "platform"}
                if "merge_requests" in url:
                    return [{"iid": 1, "draft": False}, {"iid": 2, "draft": True}]
                return [{"id": 101}]

        return Response()

    def fake_post(
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, object],
        timeout: float,
    ) -> object:
        requests.append(url)

        class Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                return {"ok": True}

        return Response()

    monkeypatch.setattr(gitlab_http.httpx, "get", fake_get)
    monkeypatch.setattr(gitlab_http.httpx, "post", fake_post)
    monkeypatch.setattr(git, "remote_url", lambda **kwargs: "")
    monkeypatch.setattr(git, "current_branch", lambda **kwargs: "")

    client = gitlab_api.GitLabAPI(token="")
    assert client.get_json("projects/x") is None
    assert client.post_json("projects/x") is None

    client = gitlab_api.GitLabAPI(token="test-token")
    assert client.resolve_project("acme/platform") == client.resolve_project("acme/platform")
    assert client.resolve_project("missing/project") is None
    assert client.list_all_open_mrs("adrien") == [{"iid": 1, "draft": False}, {"iid": 2, "draft": True}]
    assert client.list_all_open_mrs("adrien", include_draft=False) == [{"iid": 1, "draft": False}]
    assert client.cancel_pipelines(42, "feature") == [101, 101]
    monkeypatch.setattr(client, "get_json_paginated", lambda endpoint: [])
    assert client.list_all_open_mrs("adrien") == []
    monkeypatch.setattr(client, "get_json", lambda endpoint: {"oops": "bad"})
    assert client.cancel_pipelines(42, "feature") == []
    assert client.resolve_project_from_remote("/tmp/repo") is None
    assert gitlab_api.GitLabAPI.current_branch("/tmp/repo") == ""
    assert requests


def test_gitlab_api_returns_none_for_non_gitlab_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(git, "remote_url", lambda **kwargs: "https://example.com/acme/repo.git")

    assert gitlab_api.GitLabAPI(token="test-token").resolve_project_from_remote("/tmp/repo") is None


def test_gitlab_api_graphql_sends_post_request(monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[dict[str, object]] = []

    def fake_post(
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, object],
        timeout: float,
    ) -> object:
        posted.append({"url": url, "json": json})

        class Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict[str, object]:
                return {"data": {"project": {"workItems": {"nodes": []}}}}

        return Response()

    monkeypatch.setattr(gitlab_http.httpx, "post", fake_post)

    client = gitlab_api.GitLabAPI(token="test-token")
    result = client.graphql("query { project { id } }", {"projectPath": "org/repo"})

    assert result is not None
    assert result["data"] is not None
    assert posted[0]["url"] == "https://gitlab.com/api/graphql"


def test_gitlab_api_graphql_returns_none_without_token() -> None:
    client = gitlab_api.GitLabAPI(token="")

    result = client.graphql("query { project { id } }")

    assert result is None


def test_get_work_item_status_returns_status_name(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(
        client,
        "graphql",
        lambda query, variables: {
            "data": {
                "project": {
                    "workItems": {
                        "nodes": [
                            {
                                "widgets": [
                                    {"type": "DESCRIPTION"},
                                    {"type": "STATUS", "status": {"name": "In Progress"}},
                                ],
                            },
                        ],
                    },
                },
            },
        },
    )

    result = client.get_work_item_status("org/repo", 42)

    assert result == "In Progress"


def test_get_work_item_status_returns_none_when_graphql_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(client, "graphql", lambda query, variables: None)

    result = client.get_work_item_status("org/repo", 42)

    assert result is None


def test_get_work_item_status_returns_none_for_empty_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(
        client,
        "graphql",
        lambda query, variables: {
            "data": {
                "project": {
                    "workItems": {
                        "nodes": [],
                    },
                },
            },
        },
    )

    result = client.get_work_item_status("org/repo", 42)

    assert result is None


def test_get_work_item_status_returns_none_when_no_status_widget(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(
        client,
        "graphql",
        lambda query, variables: {
            "data": {
                "project": {
                    "workItems": {
                        "nodes": [
                            {
                                "widgets": [
                                    {"type": "DESCRIPTION"},
                                ],
                            },
                        ],
                    },
                },
            },
        },
    )

    result = client.get_work_item_status("org/repo", 42)

    assert result is None


def test_get_work_item_status_returns_none_when_widgets_not_a_list(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(
        client,
        "graphql",
        lambda query, variables: {
            "data": {
                "project": {
                    "workItems": {
                        "nodes": [
                            {
                                "widgets": "not-a-list",
                            },
                        ],
                    },
                },
            },
        },
    )

    result = client.get_work_item_status("org/repo", 42)

    assert result is None


@pytest.mark.parametrize(
    "graphql_response",
    [
        {"data": {"project": None}},
        {"data": {"project": {"workItems": None}}},
        {"data": {"project": {"workItems": {"nodes": None}}}},
        {"data": {"project": {"workItems": {"nodes": ["not-a-dict"]}}}},
        {"data": None},
        {},
    ],
)
def test_get_work_item_status_returns_none_when_graphql_hop_is_null(
    graphql_response: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(client, "graphql", lambda query, variables: graphql_response)

    assert client.get_work_item_status("org/repo", 42) is None


def test_get_work_item_status_returns_none_when_status_value_not_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(
        client,
        "graphql",
        lambda query, variables: {
            "data": {
                "project": {
                    "workItems": {
                        "nodes": [
                            {
                                "widgets": [
                                    {"type": "STATUS", "status": "not-a-dict"},
                                ],
                            },
                        ],
                    },
                },
            },
        },
    )

    result = client.get_work_item_status("org/repo", 42)

    assert result is None
