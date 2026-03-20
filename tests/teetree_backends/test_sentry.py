from unittest.mock import MagicMock, patch

import httpx

from teetree.backends.sentry import SentryClient


def _mock_response(json_data: object, status_code: int = 200) -> httpx.Response:
    response = MagicMock(spec=httpx.Response)
    response.status_code = status_code
    response.json.return_value = json_data
    response.raise_for_status = MagicMock()
    return response


def test_get_top_issues_returns_unresolved_issues() -> None:
    client = SentryClient(token="fake", org="myorg")
    mock_http = MagicMock()
    mock_http.__enter__ = MagicMock(return_value=mock_http)
    mock_http.__exit__ = MagicMock(return_value=False)
    mock_http.get.return_value = _mock_response([{"id": "1", "title": "Error"}])

    with patch.object(client, "_client", return_value=mock_http):
        issues = client.get_top_issues(project="my-project", limit=5)

    assert len(issues) == 1
    assert issues[0]["title"] == "Error"
    mock_http.get.assert_called_once_with(
        "/api/0/projects/myorg/my-project/issues/",
        params={"query": "is:unresolved", "sort": "freq", "limit": 5},
    )


def test_get_issue_fetches_single_issue() -> None:
    client = SentryClient(token="fake", org="myorg")
    mock_http = MagicMock()
    mock_http.__enter__ = MagicMock(return_value=mock_http)
    mock_http.__exit__ = MagicMock(return_value=False)
    mock_http.get.return_value = _mock_response({"id": "42", "title": "NPE"})

    with patch.object(client, "_client", return_value=mock_http):
        issue = client.get_issue("42")

    assert issue["id"] == "42"


def test_list_projects_returns_org_projects() -> None:
    client = SentryClient(token="fake", org="myorg")
    mock_http = MagicMock()
    mock_http.__enter__ = MagicMock(return_value=mock_http)
    mock_http.__exit__ = MagicMock(return_value=False)
    mock_http.get.return_value = _mock_response([{"slug": "proj-a"}, {"slug": "proj-b"}])

    with patch.object(client, "_client", return_value=mock_http):
        projects = client.list_projects()

    assert len(projects) == 2


def test_get_issue_events_returns_event_list() -> None:
    client = SentryClient(token="fake", org="myorg")
    mock_http = MagicMock()
    mock_http.__enter__ = MagicMock(return_value=mock_http)
    mock_http.__exit__ = MagicMock(return_value=False)
    mock_http.get.return_value = _mock_response([{"eventID": "abc"}, {"eventID": "def"}])

    with patch.object(client, "_client", return_value=mock_http):
        events = client.get_issue_events("42", limit=5)

    assert len(events) == 2
    assert events[0]["eventID"] == "abc"
    mock_http.get.assert_called_once_with(
        "/api/0/issues/42/events/",
        params={"limit": 5},
    )


def test_client_method_creates_httpx_client() -> None:
    client = SentryClient(token="my-token", org="myorg", base_url="https://sentry.example.com/")

    with client._client() as http_client:
        assert isinstance(http_client, httpx.Client)
    assert client.base_url == "https://sentry.example.com"


def test_base_url_trailing_slash_is_stripped() -> None:
    client = SentryClient(token="t", org="o", base_url="https://sentry.io///")
    assert client.base_url == "https://sentry.io"
