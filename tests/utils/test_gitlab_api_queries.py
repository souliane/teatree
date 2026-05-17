import pytest

from teatree.backends import gitlab_api


def test_list_all_open_mrs_with_updated_after(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(
        client,
        "get_json",
        lambda endpoint: [{"iid": 1, "draft": False}],
    )

    result = client.list_all_open_mrs("adrien", updated_after="2024-01-01T00:00:00Z")

    assert result == [{"iid": 1, "draft": False}]


def test_list_open_mrs_as_reviewer(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    captured_endpoints: list[str] = []

    def _capture(endpoint: str) -> list[dict[str, object]]:
        captured_endpoints.append(endpoint)
        return [{"iid": 5, "web_url": "https://gitlab.com/org/repo/-/merge_requests/5"}]

    monkeypatch.setattr(client, "get_json", _capture)

    result = client.list_open_mrs_as_reviewer("adrien")

    assert result == [{"iid": 5, "web_url": "https://gitlab.com/org/repo/-/merge_requests/5"}]
    assert "reviewer_username=adrien" in captured_endpoints[0]
    assert "not%5Bauthor_username%5D=adrien" in captured_endpoints[0]


def test_list_open_issues_for_assignee(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    captured_endpoints: list[str] = []

    def _capture(endpoint: str) -> list[dict[str, object]]:
        captured_endpoints.append(endpoint)
        return [{"iid": 42, "web_url": "https://gitlab.com/org/repo/-/issues/42"}]

    monkeypatch.setattr(client, "get_json", _capture)

    result = client.list_open_issues_for_assignee("adrien", updated_after="2024-01-01T00:00:00Z")

    assert result == [{"iid": 42, "web_url": "https://gitlab.com/org/repo/-/issues/42"}]
    assert captured_endpoints[0].startswith("issues?")
    assert "assignee_username=adrien" in captured_endpoints[0]
    assert "state=opened" in captured_endpoints[0]
    assert "updated_after=2024-01-01T00%3A00%3A00Z" in captured_endpoints[0]


def test_list_open_issues_for_assignee_returns_empty_on_non_list(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(client, "get_json", lambda _endpoint: None)

    assert client.list_open_issues_for_assignee("adrien") == []


def test_list_recently_merged_mrs_returns_data(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(
        client,
        "get_json",
        lambda endpoint: [{"iid": 10, "state": "merged"}],
    )

    result = client.list_recently_merged_mrs("adrien")

    assert result == [{"iid": 10, "state": "merged"}]


def test_list_recently_merged_mrs_with_updated_after(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    captured_endpoints: list[str] = []

    def capture_get_json(endpoint: str) -> list[dict[str, object]]:
        captured_endpoints.append(endpoint)
        return [{"iid": 10}]

    monkeypatch.setattr(client, "get_json", capture_get_json)

    result = client.list_recently_merged_mrs("adrien", updated_after="2024-06-01T00:00:00Z")

    assert result == [{"iid": 10}]
    assert "updated_after" in captured_endpoints[0]


def test_list_recently_merged_mrs_returns_empty_when_not_a_list(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(client, "get_json", lambda endpoint: {"error": "bad"})

    result = client.list_recently_merged_mrs("adrien")

    assert result == []


def test_list_recently_closed_mrs_queries_state_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    captured: list[str] = []

    def capture_get_json(endpoint: str) -> list[dict[str, object]]:
        captured.append(endpoint)
        return [{"iid": 77, "state": "closed"}]

    monkeypatch.setattr(client, "get_json", capture_get_json)

    result = client.list_recently_closed_mrs("adrien", updated_after="2024-06-01T00:00:00Z")

    assert result == [{"iid": 77, "state": "closed"}]
    assert "state=closed" in captured[0]
    assert "author_username=adrien" in captured[0]
    assert "updated_after" in captured[0]


def test_list_recently_closed_mrs_returns_empty_when_not_a_list(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(client, "get_json", lambda _endpoint: {"error": "bad"})

    assert client.list_recently_closed_mrs("adrien") == []


def test_get_mr_pipeline_returns_status_and_url(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(
        client,
        "get_json",
        lambda endpoint: [{"status": "success", "web_url": "https://gitlab.com/pipelines/1"}],
    )

    result = client.get_mr_pipeline(42, 1)

    assert result == {"status": "success", "url": "https://gitlab.com/pipelines/1"}


def test_get_mr_pipeline_returns_none_when_no_pipelines(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(client, "get_json", lambda endpoint: [])

    result = client.get_mr_pipeline(42, 1)

    assert result == {"status": None, "url": None}


def test_get_mr_pipeline_returns_none_when_not_a_list(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(client, "get_json", lambda endpoint: None)

    result = client.get_mr_pipeline(42, 1)

    assert result == {"status": None, "url": None}


def test_get_mr_approvals_returns_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(
        client,
        "get_json",
        lambda endpoint: {
            "approved_by": [{"user": {"username": "reviewer1"}}, {"user": {"username": "reviewer2"}}],
            "approvals_required": 2,
        },
    )

    result = client.get_mr_approvals(42, 1)

    assert result == {"count": 2, "required": 2, "approved_by": ["reviewer1", "reviewer2"]}


def test_get_mr_approvals_returns_defaults_when_not_a_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(client, "get_json", lambda endpoint: None)

    result = client.get_mr_approvals(42, 1)

    assert result == {"count": 0, "required": 1, "approved_by": []}


def test_get_mr_approvals_handles_non_list_approved_by(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(
        client,
        "get_json",
        lambda endpoint: {"approved_by": "not-a-list", "approvals_required": 1},
    )

    result = client.get_mr_approvals(42, 1)

    assert result == {"count": 0, "required": 1, "approved_by": []}


def test_get_mr_approvals_skips_non_dict_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_mr_approvals skips non-dict entries in approved_by list (line 213)."""
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(
        client,
        "get_json",
        lambda endpoint: {
            "approved_by": [
                "not-a-dict",
                {"user": {"username": "reviewer1"}},
            ],
            "approvals_required": 1,
        },
    )

    result = client.get_mr_approvals(42, 1)

    assert result == {"count": 2, "required": 1, "approved_by": ["reviewer1"]}


def test_get_issue_returns_issue_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(
        client,
        "get_json",
        lambda endpoint: {"iid": 5, "title": "Bug"},
    )

    result = client.get_issue(42, 5)

    assert result == {"iid": 5, "title": "Bug"}


def test_get_issue_returns_none_when_not_a_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(client, "get_json", lambda endpoint: None)

    result = client.get_issue(42, 5)

    assert result is None


def test_get_mr_discussions_returns_list(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(
        client,
        "get_json",
        lambda endpoint: [{"id": "d1", "notes": []}],
    )

    result = client.get_mr_discussions(42, 1)

    assert result == [{"id": "d1", "notes": []}]


def test_get_mr_discussions_returns_empty_when_not_a_list(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(client, "get_json", lambda endpoint: None)

    result = client.get_mr_discussions(42, 1)

    assert result == []


def test_get_draft_notes_count_returns_count(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(
        client,
        "get_json",
        lambda endpoint: [{"id": 1}, {"id": 2}, {"id": 3}],
    )

    result = client.get_draft_notes_count(42, 1)

    assert result == 3


def test_get_draft_notes_count_returns_zero_when_not_a_list(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(client, "get_json", lambda endpoint: None)

    result = client.get_draft_notes_count(42, 1)

    assert result == 0
