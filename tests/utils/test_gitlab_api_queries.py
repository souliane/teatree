import pytest

from teatree.backends.gitlab import api as gitlab_api


class _PagedResponse:
    """Minimal httpx.Response stand-in for a single GitLab REST list page."""

    def __init__(self, body: list[dict[str, object]], next_page: str) -> None:
        self._body = body
        self.headers = {"x-next-page": next_page}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> list[dict[str, object]]:
        return self._body


def _two_page_httpx_get(
    pages: dict[str, _PagedResponse],
    requested: list[str],
) -> object:
    """Build a fake ``httpx.get`` that serves *pages* keyed by ``page=`` value.

    Page 1 is served when no explicit ``page=`` query is present (GitLab
    defaults to page 1); later pages match on the ``page=N`` parameter.
    """

    def fake_get(url: str, *, headers: dict[str, str], timeout: float) -> object:
        requested.append(url)
        if "page=2" in url:
            return pages["2"]
        return pages["1"]

    return fake_get


def test_get_json_paginated_returns_empty_without_token() -> None:
    client = gitlab_api.GitLabAPI(token="")

    assert client.get_json_paginated("merge_requests?per_page=100") == []


def test_get_json_paginated_breaks_on_non_list_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-array page body (e.g. a GitLab error object) stops the walk."""

    class _ErrorResponse:
        def __init__(self) -> None:
            self.headers = {"x-next-page": "2"}

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"message": "403 Forbidden"}

    monkeypatch.setattr(gitlab_api.httpx, "get", lambda url, *, headers, timeout: _ErrorResponse())
    client = gitlab_api.GitLabAPI(token="test-token")

    assert client.get_json_paginated("merge_requests?per_page=100") == []


def test_get_json_paginated_appends_page_with_question_mark_separator(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the endpoint already has a query string, ``page`` joins with ``&``."""
    requested: list[str] = []

    def fake_get(url: str, *, headers: dict[str, str], timeout: float) -> object:
        requested.append(url)
        return _PagedResponse([{"iid": 1}], next_page="")

    monkeypatch.setattr(gitlab_api.httpx, "get", fake_get)
    client = gitlab_api.GitLabAPI(token="test-token")
    client.get_json_paginated("issues")

    assert requested[0].endswith("issues?page=1")


def test_get_json_paginated_stops_at_max_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """A never-emptying ``x-next-page`` is bounded by ``_MAX_PAGES``."""
    requested: list[str] = []

    def fake_get(url: str, *, headers: dict[str, str], timeout: float) -> object:
        requested.append(url)
        return _PagedResponse([{"iid": 1}], next_page="999")

    monkeypatch.setattr(gitlab_api.httpx, "get", fake_get)
    client = gitlab_api.GitLabAPI(token="test-token")
    result = client.get_json_paginated("merge_requests?per_page=100")

    assert len(requested) == gitlab_api._MAX_PAGES
    assert len(result) == gitlab_api._MAX_PAGES


def test_list_all_open_mrs_follows_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    """list_all_open_mrs accumulates items from every page, not just page 1.

    RED on the pre-fix code (which read only the first page via get_json):
    only the two page-1 items were returned, so the page-2 item (iid 3) was
    silently dropped. GREEN after the method follows ``x-next-page``.
    """
    requested: list[str] = []
    pages = {
        "1": _PagedResponse([{"iid": 1, "draft": False}, {"iid": 2, "draft": False}], next_page="2"),
        "2": _PagedResponse([{"iid": 3, "draft": False}], next_page=""),
    }
    monkeypatch.setattr(gitlab_api.httpx, "get", _two_page_httpx_get(pages, requested))

    client = gitlab_api.GitLabAPI(token="test-token")
    result = client.list_all_open_mrs("adrien")

    assert [mr["iid"] for mr in result] == [1, 2, 3]
    assert any("page=2" in url for url in requested)


def test_list_open_issues_for_assignee_follows_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    """list_open_issues_for_assignee returns issues from both pages."""
    requested: list[str] = []
    pages = {
        "1": _PagedResponse([{"iid": 10}, {"iid": 11}], next_page="2"),
        "2": _PagedResponse([{"iid": 12}], next_page=""),
    }
    monkeypatch.setattr(gitlab_api.httpx, "get", _two_page_httpx_get(pages, requested))

    client = gitlab_api.GitLabAPI(token="test-token")
    result = client.list_open_issues_for_assignee("adrien")

    assert [issue["iid"] for issue in result] == [10, 11, 12]


def test_list_open_mrs_as_reviewer_follows_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    """list_open_mrs_as_reviewer returns reviewer MRs from both pages."""
    requested: list[str] = []
    pages = {
        "1": _PagedResponse([{"iid": 20}, {"iid": 21}], next_page="2"),
        "2": _PagedResponse([{"iid": 22}], next_page=""),
    }
    monkeypatch.setattr(gitlab_api.httpx, "get", _two_page_httpx_get(pages, requested))

    client = gitlab_api.GitLabAPI(token="test-token")
    result = client.list_open_mrs_as_reviewer("adrien")

    assert [mr["iid"] for mr in result] == [20, 21, 22]


def test_list_recently_merged_mrs_follows_pagination(monkeypatch: pytest.MonkeyPatch) -> None:
    """_list_terminal_mrs (via list_recently_merged_mrs) returns both pages."""
    requested: list[str] = []
    pages = {
        "1": _PagedResponse([{"iid": 30}, {"iid": 31}], next_page="2"),
        "2": _PagedResponse([{"iid": 32}], next_page=""),
    }
    monkeypatch.setattr(gitlab_api.httpx, "get", _two_page_httpx_get(pages, requested))

    client = gitlab_api.GitLabAPI(token="test-token")
    result = client.list_recently_merged_mrs("adrien")

    assert [mr["iid"] for mr in result] == [30, 31, 32]


def test_list_all_open_mrs_with_updated_after(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(
        client,
        "get_json_paginated",
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

    monkeypatch.setattr(client, "get_json_paginated", _capture)

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

    monkeypatch.setattr(client, "get_json_paginated", _capture)

    result = client.list_open_issues_for_assignee("adrien", updated_after="2024-01-01T00:00:00Z")

    assert result == [{"iid": 42, "web_url": "https://gitlab.com/org/repo/-/issues/42"}]
    assert captured_endpoints[0].startswith("issues?")
    assert "assignee_username=adrien" in captured_endpoints[0]
    assert "state=opened" in captured_endpoints[0]
    assert "updated_after=2024-01-01T00%3A00%3A00Z" in captured_endpoints[0]


def test_list_open_issues_for_assignee_returns_empty_on_no_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(client, "get_json_paginated", lambda _endpoint: [])

    assert client.list_open_issues_for_assignee("adrien") == []


def test_list_recently_merged_mrs_returns_data(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(
        client,
        "get_json_paginated",
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

    monkeypatch.setattr(client, "get_json_paginated", capture_get_json)

    result = client.list_recently_merged_mrs("adrien", updated_after="2024-06-01T00:00:00Z")

    assert result == [{"iid": 10}]
    assert "updated_after" in captured_endpoints[0]


def test_list_recently_merged_mrs_returns_empty_when_no_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(client, "get_json_paginated", lambda endpoint: [])

    result = client.list_recently_merged_mrs("adrien")

    assert result == []


def test_list_recently_closed_mrs_queries_state_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    captured: list[str] = []

    def capture_get_json(endpoint: str) -> list[dict[str, object]]:
        captured.append(endpoint)
        return [{"iid": 77, "state": "closed"}]

    monkeypatch.setattr(client, "get_json_paginated", capture_get_json)

    result = client.list_recently_closed_mrs("adrien", updated_after="2024-06-01T00:00:00Z")

    assert result == [{"iid": 77, "state": "closed"}]
    assert "state=closed" in captured[0]
    assert "author_username=adrien" in captured[0]
    assert "updated_after" in captured[0]


def test_list_recently_closed_mrs_returns_empty_when_no_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(client, "get_json_paginated", lambda _endpoint: [])

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
            "approvals_left": 0,
        },
    )

    result = client.get_mr_approvals(42, 1)

    assert result == {"count": 2, "required": 2, "approved_by": ["reviewer1", "reviewer2"], "approvals_left": 0}


def test_get_mr_approvals_passes_through_approvals_left(monkeypatch: pytest.MonkeyPatch) -> None:
    # On multi-rule repos the upstream approvals_left is canonical and must not be
    # recomputed from required - count. The client must surface it to callers.
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(
        client,
        "get_json",
        lambda endpoint: {
            "approved_by": [{"user": {"username": "reviewer1"}}],
            "approvals_required": 2,
            "approvals_left": 3,
        },
    )

    result = client.get_mr_approvals(42, 1)

    assert result["approvals_left"] == 3


def test_get_mr_approvals_returns_defaults_when_not_a_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(client, "get_json", lambda endpoint: None)

    result = client.get_mr_approvals(42, 1)

    assert result == {"count": 0, "required": 1, "approved_by": [], "approvals_left": -1}


def test_get_mr_approvals_handles_non_list_approved_by(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(
        client,
        "get_json",
        lambda endpoint: {"approved_by": "not-a-list", "approvals_required": 1},
    )

    result = client.get_mr_approvals(42, 1)

    assert result == {"count": 0, "required": 1, "approved_by": [], "approvals_left": -1}


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

    assert result == {"count": 2, "required": 1, "approved_by": ["reviewer1"], "approvals_left": -1}


def test_cancel_pipelines_url_encodes_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    # A '+' in a ref decodes to a space server-side, so it must be percent-encoded.
    client = gitlab_api.GitLabAPI(token="test-token")
    seen: list[str] = []

    def fake_get_json(endpoint: str) -> list[dict[str, object]]:
        seen.append(endpoint)
        return []

    monkeypatch.setattr(client, "get_json", fake_get_json)

    client.cancel_pipelines(42, "release+1.2", statuses=("running",))

    assert "ref=release%2B1.2" in seen[0]
    assert "ref=release+1.2" not in seen[0]


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
        "get_json_paginated",
        lambda endpoint: [{"id": "d1", "notes": []}],
    )

    result = client.get_mr_discussions(42, 1)

    assert result == [{"id": "d1", "notes": []}]


def test_get_mr_discussions_returns_empty_when_no_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(client, "get_json_paginated", lambda endpoint: [])

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


# ── #1295 cap B: resolve_user_id_by_username + assign_reviewer ──────────


def test_resolve_user_id_by_username_returns_zero_on_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    assert client.resolve_user_id_by_username("") == 0


def test_resolve_user_id_by_username_returns_id_from_response(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(client, "get_json", lambda endpoint: [{"id": 42, "username": "alice"}])

    assert client.resolve_user_id_by_username("alice") == 42


def test_resolve_user_id_by_username_returns_zero_when_response_not_list(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(client, "get_json", lambda endpoint: None)

    assert client.resolve_user_id_by_username("alice") == 0


def test_resolve_user_id_by_username_returns_zero_when_response_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(client, "get_json", lambda endpoint: [])

    assert client.resolve_user_id_by_username("ghost") == 0


def test_resolve_user_id_by_username_returns_zero_when_first_entry_not_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(client, "get_json", lambda endpoint: ["not a dict"])

    assert client.resolve_user_id_by_username("alice") == 0


def test_assign_reviewer_returns_false_on_non_positive_ids() -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    assert client.assign_reviewer(0, 1, 5) is False
    assert client.assign_reviewer(1, 0, 5) is False
    assert client.assign_reviewer(1, 1, 0) is False


def test_assign_reviewer_returns_false_when_mr_lookup_not_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(client, "get_json", lambda endpoint: None)

    assert client.assign_reviewer(42, 9, 5) is False


def test_assign_reviewer_short_circuits_when_user_already_a_reviewer(monkeypatch: pytest.MonkeyPatch) -> None:
    """Idempotent: user_id already in reviewers list → True without PUT."""
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(
        client,
        "get_json",
        lambda endpoint: {"reviewers": [{"id": 5, "username": "alice"}, {"id": 6, "username": "bob"}]},
    )
    put_calls: list[tuple[str, dict[str, object]]] = []

    def _put(endpoint: str, payload: dict[str, object]) -> int:
        put_calls.append((endpoint, payload))
        return 200

    monkeypatch.setattr(client, "put_status", _put)

    assert client.assign_reviewer(42, 9, 5) is True
    assert put_calls == []  # no network call when already a reviewer


def test_assign_reviewer_appends_existing_reviewers_on_put(monkeypatch: pytest.MonkeyPatch) -> None:
    """The PUT payload preserves existing reviewer ids and appends the new one."""
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(
        client,
        "get_json",
        lambda endpoint: {"reviewers": [{"id": 5, "username": "alice"}]},
    )
    captured: dict[str, object] = {}

    def _put(endpoint: str, payload: dict[str, object]) -> int:
        captured["endpoint"] = endpoint
        captured["payload"] = payload
        return 200

    monkeypatch.setattr(client, "put_status", _put)

    assert client.assign_reviewer(42, 9, 7) is True
    assert captured["endpoint"] == "projects/42/merge_requests/9"
    assert captured["payload"] == {"reviewer_ids": [5, 7]}


def test_assign_reviewer_returns_false_on_non_2xx_status(monkeypatch: pytest.MonkeyPatch) -> None:
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(client, "get_json", lambda endpoint: {"reviewers": []})
    monkeypatch.setattr(client, "put_status", lambda endpoint, payload: 500)

    assert client.assign_reviewer(42, 9, 7) is False


def test_assign_reviewer_tolerates_non_list_reviewers_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``reviewers`` is missing or not a list, treat as empty and PUT just the new id."""
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(client, "get_json", lambda endpoint: {"reviewers": "weird-value"})
    captured: dict[str, object] = {}

    def _put(endpoint: str, payload: dict[str, object]) -> int:
        captured["payload"] = payload
        return 201

    monkeypatch.setattr(client, "put_status", _put)

    assert client.assign_reviewer(42, 9, 7) is True
    assert captured["payload"] == {"reviewer_ids": [7]}


def test_assign_reviewer_skips_non_dict_reviewer_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bogus non-dict entry inside ``reviewers`` is skipped, not crashy."""
    client = gitlab_api.GitLabAPI(token="test-token")
    monkeypatch.setattr(
        client,
        "get_json",
        lambda endpoint: {"reviewers": [{"id": 5}, "bad-entry", {"id": 6}]},
    )
    captured: dict[str, object] = {}

    def _put(endpoint: str, payload: dict[str, object]) -> int:
        captured["payload"] = payload
        return 200

    monkeypatch.setattr(client, "put_status", _put)

    assert client.assign_reviewer(42, 9, 7) is True
    # Bad string entry skipped; the two valid existing ids are preserved.
    assert captured["payload"] == {"reviewer_ids": [5, 6, 7]}
