from unittest.mock import MagicMock, patch

from teatree.backends.gitlab import GitLabCodeHost
from teatree.backends.gitlab.api import GitLabAPI, ProjectInfo
from teatree.core.backend_protocols import PullRequestSpec


def _project() -> ProjectInfo:
    return ProjectInfo(project_id=42, path_with_namespace="org/repo", short_name="repo", default_branch="main")


def _two_page_http_side_effect(page1: list[dict], page2: list[dict]):
    """httpx.get side-effect: page 1 advertises x-next-page=2, page 2 ends it.

    ``get_json_paginated`` appends ``&page=N``; this routes the request by that
    marker so a real ``GitLabAPI`` walks both pages.
    """

    def _side_effect(url: str, **_: object) -> MagicMock:
        resp = MagicMock()
        resp.raise_for_status.return_value = None
        if "page=2" in url:
            resp.json.return_value = page2
            resp.headers = {"x-next-page": ""}
        else:
            resp.json.return_value = page1
            resp.headers = {"x-next-page": "2"}
        return resp

    return _side_effect


def test_create_pr_uses_repo_remote_and_auto_labels(tmp_path) -> None:
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project_from_remote.return_value = _project()
    client.post_json.return_value = {"iid": 7}
    host = GitLabCodeHost(client=client)
    repo_path = tmp_path / "repo"
    repo_path.mkdir()

    result = host.create_pr(
        PullRequestSpec(
            repo=str(repo_path),
            branch="feature-branch",
            title="feat: add labels",
            description="body",
            labels=["Process::Technical review", "customer::foo"],
        ),
    )

    assert result == {"iid": 7}
    client.resolve_project_from_remote.assert_called_once_with(str(repo_path))
    client.post_json.assert_called_once_with(
        "projects/42/merge_requests",
        {
            "source_branch": "feature-branch",
            "target_branch": "main",
            "title": "feat: add labels",
            "description": "body",
            "labels": "Process::Technical review,customer::foo",
        },
    )


def test_create_pr_uses_explicit_target_branch() -> None:
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    client.post_json.return_value = {"iid": 8}
    host = GitLabCodeHost(client=client)

    host.create_pr(
        PullRequestSpec(
            repo="org/repo",
            branch="feature-branch",
            title="feat: add labels",
            description="body",
            target_branch="develop",
        ),
    )

    client.resolve_project.assert_called_once_with("org/repo")
    client.post_json.assert_called_once_with(
        "projects/42/merge_requests",
        {
            "source_branch": "feature-branch",
            "target_branch": "develop",
            "title": "feat: add labels",
            "description": "body",
        },
    )


def test_create_pr_returns_error_when_project_not_resolved() -> None:
    """create_pr returns error dict when _resolve_project returns None."""
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = None
    host = GitLabCodeHost(client=client)

    result = host.create_pr(
        PullRequestSpec(
            repo="org/unknown",
            branch="feat",
            title="test",
            description="desc",
        ),
    )

    assert result == {"error": "Could not resolve project: org/unknown"}
    client.post_json.assert_not_called()


def test_create_issue_posts_to_project_with_labels() -> None:
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    client.post_json.return_value = {"iid": 3, "web_url": "https://gitlab.com/org/repo/-/issues/3"}
    host = GitLabCodeHost(client=client)

    result = host.create_issue(repo="org/repo", title="t", body="b", labels=["enforcement-gap"])

    assert result == {"iid": 3, "web_url": "https://gitlab.com/org/repo/-/issues/3"}
    client.post_json.assert_called_once_with(
        "projects/42/issues",
        {"title": "t", "description": "b", "labels": "enforcement-gap"},
    )


def test_create_issue_returns_error_when_project_not_resolved() -> None:
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = None
    host = GitLabCodeHost(client=client)

    assert host.create_issue(repo="org/unknown", title="t", body="b") == {
        "error": "Could not resolve project: org/unknown"
    }
    client.post_json.assert_not_called()


def test_close_issue_puts_state_event_close() -> None:
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    client.put_json.return_value = {"state": "closed"}
    host = GitLabCodeHost(client=client)

    result = host.close_issue(issue_url="https://gitlab.com/org/repo/-/issues/3")

    assert result == {"state": "closed"}
    client.put_json.assert_called_once_with("projects/42/issues/3", {"state_event": "close"})


def test_close_issue_posts_audit_note_first() -> None:
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    client.put_json.return_value = {"state": "closed"}
    host = GitLabCodeHost(client=client)

    host.close_issue(issue_url="https://gitlab.com/org/repo/-/issues/3", comment="dead")

    client.post_json.assert_called_once_with("projects/42/issues/3/notes", {"body": "dead"})


def test_close_issue_returns_error_when_project_not_resolved() -> None:
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = None
    host = GitLabCodeHost(client=client)

    assert "error" in host.close_issue(issue_url="https://gitlab.com/org/unknown/-/issues/3")
    client.put_json.assert_not_called()


def test_search_open_issues_searches_project() -> None:
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    client.get_json_paginated.return_value = [{"iid": 3}]
    host = GitLabCodeHost(client=client)

    result = host.search_open_issues(repo="org/repo", query="fingerprint:abc")

    assert result == [{"iid": 3}]
    endpoint = client.get_json_paginated.call_args[0][0]
    assert endpoint.startswith("projects/42/issues?state=opened&search=")


def test_search_open_issues_returns_empty_when_project_not_resolved() -> None:
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = None
    host = GitLabCodeHost(client=client)

    assert host.search_open_issues(repo="org/unknown", query="x") == []


def test_list_my_prs_delegates_to_client_list_all_open_mrs() -> None:
    """list_my_prs returns the forge-wide list of MRs authored by user."""
    client = MagicMock(spec=GitLabAPI)
    client.list_all_open_mrs.return_value = [
        {"iid": 1, "title": "MR 1", "web_url": "https://gitlab.com/org/repo/-/merge_requests/1"},
        {"iid": 2, "title": "MR 2", "web_url": "https://gitlab.com/org/other/-/merge_requests/2"},
    ]
    host = GitLabCodeHost(client=client)

    result = host.list_my_prs(author="adrien")

    assert len(result) == 2
    assert result[0]["iid"] == 1
    client.list_all_open_mrs.assert_called_once_with("adrien", updated_after=None)


def test_list_my_prs_returns_empty_when_no_mrs() -> None:
    client = MagicMock(spec=GitLabAPI)
    client.list_all_open_mrs.return_value = []
    host = GitLabCodeHost(client=client)

    assert host.list_my_prs(author="adrien") == []


def test_list_review_requested_prs_delegates_to_client() -> None:
    client = MagicMock(spec=GitLabAPI)
    client.list_open_mrs_as_reviewer.return_value = [{"iid": 5, "title": "MR 5"}]
    host = GitLabCodeHost(client=client)

    result = host.list_review_requested_prs(reviewer="adrien")

    assert result == [{"iid": 5, "title": "MR 5"}]
    client.list_open_mrs_as_reviewer.assert_called_once_with("adrien", updated_after=None)


def test_list_assigned_issues_delegates_to_client() -> None:
    client = MagicMock(spec=GitLabAPI)
    client.list_open_issues_for_assignee.return_value = [{"iid": 3, "title": "Issue 3"}]
    host = GitLabCodeHost(client=client)

    result = host.list_assigned_issues(assignee="adrien")

    assert result == [{"iid": 3, "title": "Issue 3"}]
    client.list_open_issues_for_assignee.assert_called_once_with("adrien")


def test_post_pr_comment_returns_error_when_project_not_resolved() -> None:
    """post_pr_comment returns error when project cannot be resolved."""
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = None
    host = GitLabCodeHost(client=client)

    result = host.post_pr_comment(repo="org/unknown", pr_iid=10, body="note")

    assert result == {"error": "Could not resolve project: org/unknown"}
    client.post_json.assert_not_called()


def test_post_pr_comment_posts_to_correct_endpoint() -> None:
    """post_pr_comment posts comment body to the MR notes endpoint."""
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    client.post_json.return_value = {"id": 99}
    host = GitLabCodeHost(client=client)

    result = host.post_pr_comment(repo="org/repo", pr_iid=10, body="Test note")

    assert result == {"id": 99}
    client.post_json.assert_called_once_with(
        "projects/42/merge_requests/10/notes",
        {"body": "Test note"},
    )


def test_post_pr_comment_returns_empty_dict_when_post_returns_none() -> None:
    """post_pr_comment returns {} when post_json returns None."""
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    client.post_json.return_value = None
    host = GitLabCodeHost(client=client)

    result = host.post_pr_comment(repo="org/repo", pr_iid=5, body="note")

    assert result == {}


def test_current_user_proxies_to_api_username() -> None:
    client = MagicMock(spec=GitLabAPI)
    client.current_username.return_value = "adrien.cossa"
    host = GitLabCodeHost(client=client)

    assert host.current_user() == "adrien.cossa"
    client.current_username.assert_called_once_with()


def test_create_pr_falls_back_to_cwd_remote_for_bare_repo_name(tmp_path, monkeypatch) -> None:
    """A bare repo name (no slash, no existing path) resolves via the CWD's git remote.

    Regression guard for overlay issue t3-o.#54: ``Worktree.repo_path`` stores
    a bare repo name, so callers passing it directly must still reach the
    GitLab project via the CWD's ``origin`` remote.
    """
    monkeypatch.chdir(tmp_path)
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project_from_remote.return_value = _project()
    client.post_json.return_value = {"iid": 9}
    host = GitLabCodeHost(client=client)

    host.create_pr(PullRequestSpec(repo="teatree", branch="feat", title="x", description="y"))

    client.resolve_project_from_remote.assert_called_once_with(".")
    client.resolve_project.assert_not_called()


def test_create_pr_uses_explicit_slug_when_repo_has_namespace() -> None:
    """A ``namespace/repo`` slug still hits ``resolve_project`` directly — no CWD fallback."""
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    client.post_json.return_value = {"iid": 11}
    host = GitLabCodeHost(client=client)

    host.create_pr(PullRequestSpec(repo="org/nested/repo", branch="feat", title="x", description="y"))

    client.resolve_project.assert_called_once_with("org/nested/repo")
    client.resolve_project_from_remote.assert_not_called()


def test_get_issue_parses_url_and_calls_api() -> None:
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    client.get_issue.return_value = {"title": "Bug", "iid": 7}
    host = GitLabCodeHost(client=client)

    result = host.get_issue("https://gitlab.com/org/repo/-/issues/7")

    assert result == {"title": "Bug", "iid": 7}
    client.resolve_project.assert_called_once_with("org/repo")
    client.get_issue.assert_called_once_with(42, 7)


def test_get_issue_rejects_non_issue_url() -> None:
    client = MagicMock(spec=GitLabAPI)
    host = GitLabCodeHost(client=client)

    result = host.get_issue("https://gitlab.com/org/repo/-/merge_requests/12")

    assert "error" in result
    client.resolve_project.assert_not_called()


def test_get_issue_returns_error_when_project_not_resolved() -> None:
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = None
    host = GitLabCodeHost(client=client)

    result = host.get_issue("https://gitlab.com/missing/repo/-/issues/1")

    assert "error" in result


def test_get_issue_returns_error_when_api_returns_none() -> None:
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    client.get_issue.return_value = None
    host = GitLabCodeHost(client=client)

    result = host.get_issue("https://gitlab.com/org/repo/-/issues/9")

    assert "error" in result


def test_get_review_state_returns_approved_when_user_in_approved_by() -> None:
    from teatree.core.backend_protocols import ReviewState  # noqa: PLC0415

    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    client.get_mr_approvals.return_value = {"approved_by": ["adrien", "carol"]}
    host = GitLabCodeHost(client=client)

    result = host.get_review_state(
        pr_url="https://gitlab.com/org/repo/-/merge_requests/12",
        reviewer="adrien",
    )

    assert result == ReviewState.APPROVED
    client.resolve_project.assert_called_once_with("org/repo")
    client.get_mr_approvals.assert_called_once_with(42, 12)
    client.get_json.assert_not_called()


def test_get_review_state_returns_pending_when_assigned_but_not_approved() -> None:
    from teatree.core.backend_protocols import ReviewState  # noqa: PLC0415

    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    client.get_mr_approvals.return_value = {"approved_by": []}
    client.get_json.return_value = {"reviewers": [{"username": "adrien"}]}
    host = GitLabCodeHost(client=client)

    result = host.get_review_state(
        pr_url="https://gitlab.com/org/repo/-/merge_requests/12",
        reviewer="adrien",
    )

    assert result == ReviewState.PENDING
    client.get_json.assert_called_once_with("projects/42/merge_requests/12")


def test_get_review_state_returns_none_when_user_neither_approved_nor_assigned() -> None:
    from teatree.core.backend_protocols import ReviewState  # noqa: PLC0415

    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    client.get_mr_approvals.return_value = {"approved_by": []}
    client.get_json.return_value = {"reviewers": []}
    host = GitLabCodeHost(client=client)

    assert (
        host.get_review_state(
            pr_url="https://gitlab.com/org/repo/-/merge_requests/12",
            reviewer="adrien",
        )
        == ReviewState.NONE
    )


def test_get_review_state_returns_none_for_unparseable_url() -> None:
    from teatree.core.backend_protocols import ReviewState  # noqa: PLC0415

    client = MagicMock(spec=GitLabAPI)
    host = GitLabCodeHost(client=client)

    assert host.get_review_state(pr_url="https://github.com/o/r/pull/7", reviewer="adrien") == ReviewState.NONE
    client.resolve_project.assert_not_called()


def test_get_review_state_returns_none_when_project_unresolved() -> None:
    from teatree.core.backend_protocols import ReviewState  # noqa: PLC0415

    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = None
    host = GitLabCodeHost(client=client)

    assert (
        host.get_review_state(
            pr_url="https://gitlab.com/org/repo/-/merge_requests/12",
            reviewer="adrien",
        )
        == ReviewState.NONE
    )


def test_get_review_state_returns_none_when_reviewer_empty() -> None:
    from teatree.core.backend_protocols import ReviewState  # noqa: PLC0415

    client = MagicMock(spec=GitLabAPI)
    host = GitLabCodeHost(client=client)

    assert (
        host.get_review_state(
            pr_url="https://gitlab.com/org/repo/-/merge_requests/12",
            reviewer="",
        )
        == ReviewState.NONE
    )
    client.resolve_project.assert_not_called()


def test_post_issue_comment_posts_to_issue_notes_endpoint() -> None:
    """post_issue_comment posts the body to the issue notes endpoint."""
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    client.post_json.return_value = {"id": 555}
    host = GitLabCodeHost(client=client)

    result = host.post_issue_comment(
        issue_url="https://gitlab.com/org/repo/-/issues/7",
        body="A clarifying question",
    )

    assert result == {"id": 555}
    client.resolve_project.assert_called_once_with("org/repo")
    client.post_json.assert_called_once_with(
        "projects/42/issues/7/notes",
        {"body": "A clarifying question"},
    )


def test_post_issue_comment_supports_work_items_url() -> None:
    """GitLab serves the same iid under /-/work_items/<iid>; it must work too."""
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    client.post_json.return_value = {"id": 1}
    host = GitLabCodeHost(client=client)

    result = host.post_issue_comment(
        issue_url="https://gitlab.com/group/sub/repo/-/work_items/469",
        body="note",
    )

    assert result == {"id": 1}
    client.resolve_project.assert_called_once_with("group/sub/repo")
    client.post_json.assert_called_once_with(
        "projects/42/issues/469/notes",
        {"body": "note"},
    )


def test_post_issue_comment_rejects_non_issue_url() -> None:
    client = MagicMock(spec=GitLabAPI)
    host = GitLabCodeHost(client=client)

    result = host.post_issue_comment(
        issue_url="https://gitlab.com/org/repo/-/merge_requests/12",
        body="note",
    )

    assert result == {"error": "Not a GitLab issue URL: https://gitlab.com/org/repo/-/merge_requests/12"}
    client.post_json.assert_not_called()


def test_post_issue_comment_returns_error_when_project_not_resolved() -> None:
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = None
    host = GitLabCodeHost(client=client)

    result = host.post_issue_comment(
        issue_url="https://gitlab.com/org/repo/-/issues/7",
        body="note",
    )

    assert result == {"error": "Could not resolve project: org/repo"}
    client.post_json.assert_not_called()


def test_post_issue_comment_returns_empty_dict_when_post_returns_none() -> None:
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    client.post_json.return_value = None
    host = GitLabCodeHost(client=client)

    result = host.post_issue_comment(
        issue_url="https://gitlab.com/org/repo/-/issues/7",
        body="note",
    )

    assert result == {}


def test_list_issue_comments_hits_notes_endpoint() -> None:
    """list_issue_comments paginates the issue notes endpoint with per_page=100."""
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    client.get_json_paginated.return_value = [{"id": 1, "body": "a"}, {"id": 2, "body": "b"}]
    host = GitLabCodeHost(client=client)

    result = host.list_issue_comments(issue_url="https://gitlab.com/org/repo/-/issues/7")

    assert result == [{"id": 1, "body": "a"}, {"id": 2, "body": "b"}]
    client.get_json_paginated.assert_called_once_with("projects/42/issues/7/notes?per_page=100")


def test_list_issue_comments_supports_work_items_url() -> None:
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    client.get_json_paginated.return_value = []
    host = GitLabCodeHost(client=client)

    result = host.list_issue_comments(issue_url="https://gitlab.com/group/sub/repo/-/work_items/469")

    assert result == []
    client.resolve_project.assert_called_once_with("group/sub/repo")
    client.get_json_paginated.assert_called_once_with("projects/42/issues/469/notes?per_page=100")


def test_list_issue_comments_returns_empty_on_non_issue_url() -> None:
    client = MagicMock(spec=GitLabAPI)
    host = GitLabCodeHost(client=client)

    result = host.list_issue_comments(issue_url="https://gitlab.com/org/repo/-/merge_requests/12")

    assert result == []
    client.get_json_paginated.assert_not_called()


def test_list_issue_comments_returns_empty_when_project_unresolved() -> None:
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = None
    host = GitLabCodeHost(client=client)

    result = host.list_issue_comments(issue_url="https://gitlab.com/org/repo/-/issues/7")

    assert result == []
    client.get_json_paginated.assert_not_called()


def test_list_issue_comments_returns_paginated_result() -> None:
    """The paginated helper owns the list contract; the method returns it verbatim."""
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    client.get_json_paginated.return_value = []
    host = GitLabCodeHost(client=client)

    result = host.list_issue_comments(issue_url="https://gitlab.com/org/repo/-/issues/7")

    assert result == []


def test_update_issue_comment_puts_to_note_endpoint() -> None:
    """update_issue_comment PUTs the new body to the note's endpoint."""
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    client.put_json.return_value = {"id": 55, "body": "new"}
    host = GitLabCodeHost(client=client)

    result = host.update_issue_comment(
        issue_url="https://gitlab.com/org/repo/-/issues/7",
        comment_id=55,
        body="new",
    )

    assert result == {"id": 55, "body": "new"}
    client.put_json.assert_called_once_with(
        "projects/42/issues/7/notes/55",
        {"body": "new"},
    )


def test_update_issue_comment_rejects_non_issue_url() -> None:
    client = MagicMock(spec=GitLabAPI)
    host = GitLabCodeHost(client=client)

    result = host.update_issue_comment(
        issue_url="https://gitlab.com/org/repo/-/merge_requests/12",
        comment_id=55,
        body="new",
    )

    assert result == {"error": "Not a GitLab issue URL: https://gitlab.com/org/repo/-/merge_requests/12"}
    client.put_json.assert_not_called()


def test_update_issue_comment_returns_error_when_project_unresolved() -> None:
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = None
    host = GitLabCodeHost(client=client)

    result = host.update_issue_comment(
        issue_url="https://gitlab.com/org/repo/-/issues/7",
        comment_id=55,
        body="new",
    )

    assert result == {"error": "Could not resolve project: org/repo"}
    client.put_json.assert_not_called()


def test_update_issue_comment_returns_empty_dict_when_put_returns_none() -> None:
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    client.put_json.return_value = None
    host = GitLabCodeHost(client=client)

    result = host.update_issue_comment(
        issue_url="https://gitlab.com/org/repo/-/issues/7",
        comment_id=55,
        body="new",
    )

    assert result == {}


def test_get_pr_open_state_maps_opened_to_open() -> None:
    from teatree.core.backend_protocols import PrOpenState  # noqa: PLC0415

    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    client.get_json.return_value = {"state": "opened"}
    host = GitLabCodeHost(client=client)

    assert host.get_pr_open_state(pr_url="https://gitlab.com/org/repo/-/merge_requests/12") == PrOpenState.OPEN
    client.get_json.assert_called_once_with("projects/42/merge_requests/12")


def test_get_pr_open_state_maps_merged_to_merged() -> None:
    from teatree.core.backend_protocols import PrOpenState  # noqa: PLC0415

    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    client.get_json.return_value = {"state": "merged"}
    host = GitLabCodeHost(client=client)

    assert host.get_pr_open_state(pr_url="https://gitlab.com/org/repo/-/merge_requests/12") == PrOpenState.MERGED


def test_get_pr_open_state_maps_closed_and_locked_to_closed() -> None:
    from teatree.core.backend_protocols import PrOpenState  # noqa: PLC0415

    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    host = GitLabCodeHost(client=client)

    client.get_json.return_value = {"state": "closed"}
    assert host.get_pr_open_state(pr_url="https://gitlab.com/org/repo/-/merge_requests/12") == PrOpenState.CLOSED
    client.get_json.return_value = {"state": "locked"}
    assert host.get_pr_open_state(pr_url="https://gitlab.com/org/repo/-/merge_requests/12") == PrOpenState.CLOSED


def test_get_pr_open_state_unrecognised_state_is_unknown() -> None:
    from teatree.core.backend_protocols import PrOpenState  # noqa: PLC0415

    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    client.get_json.return_value = {"state": "weird"}
    host = GitLabCodeHost(client=client)

    assert host.get_pr_open_state(pr_url="https://gitlab.com/org/repo/-/merge_requests/12") == PrOpenState.UNKNOWN


def test_get_pr_open_state_non_string_or_missing_state_is_unknown() -> None:
    from teatree.core.backend_protocols import PrOpenState  # noqa: PLC0415

    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    host = GitLabCodeHost(client=client)

    client.get_json.return_value = {}  # state key absent
    assert host.get_pr_open_state(pr_url="https://gitlab.com/org/repo/-/merge_requests/12") == PrOpenState.UNKNOWN
    client.get_json.return_value = {"state": 42}  # non-string state
    assert host.get_pr_open_state(pr_url="https://gitlab.com/org/repo/-/merge_requests/12") == PrOpenState.UNKNOWN


def test_get_pr_open_state_unparsable_url_is_unknown() -> None:
    from teatree.core.backend_protocols import PrOpenState  # noqa: PLC0415

    client = MagicMock(spec=GitLabAPI)
    host = GitLabCodeHost(client=client)

    assert host.get_pr_open_state(pr_url="https://github.com/o/r/pull/7") == PrOpenState.UNKNOWN
    client.resolve_project.assert_not_called()


def test_get_pr_open_state_unresolved_project_is_unknown() -> None:
    from teatree.core.backend_protocols import PrOpenState  # noqa: PLC0415

    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = None
    host = GitLabCodeHost(client=client)

    assert host.get_pr_open_state(pr_url="https://gitlab.com/org/repo/-/merge_requests/12") == PrOpenState.UNKNOWN


def test_get_pr_open_state_non_dict_payload_is_unknown() -> None:
    from teatree.core.backend_protocols import PrOpenState  # noqa: PLC0415

    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    client.get_json.return_value = ["not", "a", "dict"]
    host = GitLabCodeHost(client=client)

    assert host.get_pr_open_state(pr_url="https://gitlab.com/org/repo/-/merge_requests/12") == PrOpenState.UNKNOWN


def test_get_pr_open_state_any_exception_fails_open_to_unknown() -> None:
    from teatree.core.backend_protocols import PrOpenState  # noqa: PLC0415

    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.side_effect = RuntimeError("network down / auth error")
    host = GitLabCodeHost(client=client)

    assert host.get_pr_open_state(pr_url="https://gitlab.com/org/repo/-/merge_requests/12") == PrOpenState.UNKNOWN


# ── #1838 self-author skip: get_pr_author on GitLabCodeHost ─────────────


def test_get_pr_author_returns_username() -> None:
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    client.get_json.return_value = {"author": {"username": "adrien.cossa"}}
    host = GitLabCodeHost(client=client)

    assert host.get_pr_author(pr_url="https://gitlab.com/org/repo/-/merge_requests/12") == "adrien.cossa"
    client.get_json.assert_called_once_with("projects/42/merge_requests/12")


def test_get_pr_author_author_less_payload_is_empty() -> None:
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    client.get_json.return_value = {"state": "opened"}
    host = GitLabCodeHost(client=client)

    assert host.get_pr_author(pr_url="https://gitlab.com/org/repo/-/merge_requests/12") == ""


def test_get_pr_author_unparsable_url_is_empty() -> None:
    client = MagicMock(spec=GitLabAPI)
    host = GitLabCodeHost(client=client)

    assert host.get_pr_author(pr_url="https://github.com/o/r/pull/7") == ""
    client.resolve_project.assert_not_called()


def test_get_pr_author_unresolved_project_is_empty() -> None:
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = None
    host = GitLabCodeHost(client=client)

    assert host.get_pr_author(pr_url="https://gitlab.com/org/repo/-/merge_requests/12") == ""


def test_get_pr_author_non_dict_payload_is_empty() -> None:
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    client.get_json.return_value = ["not", "a", "dict"]
    host = GitLabCodeHost(client=client)

    assert host.get_pr_author(pr_url="https://gitlab.com/org/repo/-/merge_requests/12") == ""


def test_get_pr_author_any_exception_fails_safe_to_empty() -> None:
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.side_effect = RuntimeError("network down / auth error")
    host = GitLabCodeHost(client=client)

    assert host.get_pr_author(pr_url="https://gitlab.com/org/repo/-/merge_requests/12") == ""


# ── #1295 cap B: assign_reviewer on GitLabCodeHost ──────────────────────


def test_assign_reviewer_returns_false_on_blank_inputs() -> None:
    host = GitLabCodeHost(client=MagicMock(spec=GitLabAPI))
    assert host.assign_reviewer(pr_url="", username="alice") is False
    assert host.assign_reviewer(pr_url="https://gitlab.com/o/r/-/merge_requests/1", username="") is False


def test_assign_reviewer_returns_false_on_unparseable_url() -> None:
    host = GitLabCodeHost(client=MagicMock(spec=GitLabAPI))
    assert host.assign_reviewer(pr_url="https://gitlab.com/not-an-mr", username="alice") is False


def test_assign_reviewer_returns_false_when_project_unresolved() -> None:
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = None
    host = GitLabCodeHost(client=client)

    assert host.assign_reviewer(pr_url="https://gitlab.com/org/repo/-/merge_requests/9", username="alice") is False


def test_assign_reviewer_returns_false_when_user_lookup_fails() -> None:
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    client.resolve_user_id_by_username.return_value = 0
    host = GitLabCodeHost(client=client)

    assert host.assign_reviewer(pr_url="https://gitlab.com/org/repo/-/merge_requests/9", username="ghost") is False


def test_assign_reviewer_delegates_to_client_on_success() -> None:
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    client.resolve_user_id_by_username.return_value = 77
    client.assign_reviewer.return_value = True
    host = GitLabCodeHost(client=client)

    assert host.assign_reviewer(pr_url="https://gitlab.com/org/repo/-/merge_requests/9", username="alice") is True
    client.assign_reviewer.assert_called_once_with(42, 9, 77)


def test_assign_reviewer_swallows_exception_and_returns_false() -> None:
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.side_effect = RuntimeError("API down")
    host = GitLabCodeHost(client=client)

    assert host.assign_reviewer(pr_url="https://gitlab.com/org/repo/-/merge_requests/9", username="alice") is False


def test_get_mr_approvals_uses_canonical_approvals_left_not_fallback() -> None:
    # On a multi-rule repo the upstream approvals_left is authoritative.
    # required - count (here 1 - 1 = 0) would wrongly say "no approvals left".
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    client.get_mr_approvals.return_value = {
        "count": 1,
        "required": 1,
        "approved_by": ["reviewer1"],
        "approvals_left": 2,
    }
    client.get_mr_discussions.return_value = []
    host = GitLabCodeHost(client=client)

    state = host.get_mr_approvals(repo="org/repo", pr_iid=12)

    assert state["approvals_left"] == 2


def test_list_issue_comments_returns_notes_from_page_two() -> None:
    """A note that sits exclusively on page 2 must be returned, not truncated.

    A non-paginated GET caps at per_page=100, so a ``## Test Plan`` evidence
    note older than the 100 most-recent notes goes unseen and the poster
    duplicates it. Pagination must surface the full note history (>100).
    """
    page1 = [{"id": i, "body": f"c{i}"} for i in range(100)]
    page2 = [{"id": 100, "body": "## Test Plan"}]
    api = GitLabAPI(token="tok", base_url="https://gitlab.example.com/api/v4")
    host = GitLabCodeHost(client=api)
    with (
        patch("httpx.get", side_effect=_two_page_http_side_effect(page1, page2)),
        patch.object(api, "resolve_project", return_value=_project()),
    ):
        result = host.list_issue_comments(issue_url="https://gitlab.com/org/repo/-/issues/7")

    assert len(result) == 101
    assert {"id": 100, "body": "## Test Plan"} in result


def test_search_open_issues_returns_issues_from_page_two() -> None:
    """An open issue past the first page must be found by the dedup search.

    A non-paginated GET caps the matched-issue list at per_page=100; a
    previously-filed enforcement issue on page 2 would be missed and refiled.
    """
    page1 = [{"iid": i} for i in range(100)]
    page2 = [{"iid": 100, "title": "fingerprint:abc"}]
    api = GitLabAPI(token="tok", base_url="https://gitlab.example.com/api/v4")
    host = GitLabCodeHost(client=api)
    with (
        patch("httpx.get", side_effect=_two_page_http_side_effect(page1, page2)),
        patch.object(api, "resolve_project", return_value=_project()),
    ):
        result = host.search_open_issues(repo="org/repo", query="fingerprint:abc")

    assert len(result) == 101
    assert {"iid": 100, "title": "fingerprint:abc"} in result


def test_get_mr_approvals_falls_back_when_left_absent() -> None:
    # When the payload omits approvals_left (sentinel -1), the code-host
    # recomputes required - count.
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    client.get_mr_approvals.return_value = {
        "count": 1,
        "required": 3,
        "approved_by": ["reviewer1"],
        "approvals_left": -1,
    }
    client.get_mr_discussions.return_value = []
    host = GitLabCodeHost(client=client)

    state = host.get_mr_approvals(repo="org/repo", pr_iid=12)

    assert state["approvals_left"] == 2


_PARENT_URL = "https://gitlab.com/org/repo/-/work_items/8545"
_PARENT_GID = "gid://gitlab/WorkItem/100"
_CHILD_GID = "gid://gitlab/WorkItem/200"
_TASK_TYPE_GID = "gid://gitlab/WorkItems::Type/5"


def _graphql_router(*, child_iid: int, convert_errors=None, link_errors=None):
    """Route create_sub_issue's GraphQL calls by query/mutation content."""

    def _route(query: str, variables: dict | None = None) -> dict:
        iid = (variables or {}).get("iid")
        if "workItemTypes" in query:
            return {"data": {"workspace": {"workItemTypes": {"nodes": [{"id": _TASK_TYPE_GID, "name": "Task"}]}}}}
        if "workItems(iids" in query:
            gid = _PARENT_GID if iid == "8545" else _CHILD_GID
            return {"data": {"project": {"workItems": {"nodes": [{"id": gid}]}}}}
        if "workItemConvert" in query:
            return {"data": {"workItemConvert": {"workItem": {"id": _CHILD_GID}, "errors": convert_errors or []}}}
        return {"data": {"workItemUpdate": {"workItem": {"id": _CHILD_GID}, "errors": link_errors or []}}}

    _ = child_iid
    return _route


def _host_for_create_sub(child_iid: int = 8546, **kwargs) -> tuple[GitLabCodeHost, MagicMock]:
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    client.post_json.return_value = {
        "iid": child_iid,
        "web_url": f"https://gitlab.com/org/repo/-/work_items/{child_iid}",
    }
    client.graphql.side_effect = _graphql_router(child_iid=child_iid, **kwargs)
    return GitLabCodeHost(client=client), client


def test_create_sub_issue_creates_converts_and_links() -> None:
    host, client = _host_for_create_sub()

    result = host.create_sub_issue(parent_url=_PARENT_URL, title="Finding 1", body="desc", labels=["sec"])

    assert result["iid"] == 8546
    assert result["web_url"] == "https://gitlab.com/org/repo/-/work_items/8546"
    client.post_json.assert_called_once_with(
        "projects/42/issues",
        {"title": "Finding 1", "description": "desc", "labels": "sec"},
    )
    convert_call = next(c for c in client.graphql.call_args_list if "workItemConvert" in c.args[0])
    assert convert_call.args[1] == {"id": _CHILD_GID, "typeId": _TASK_TYPE_GID}
    link_call = next(c for c in client.graphql.call_args_list if "workItemUpdate" in c.args[0])
    assert link_call.args[1] == {"id": _CHILD_GID, "parentId": _PARENT_GID}


def test_create_sub_issue_rejects_non_gitlab_url() -> None:
    host, _ = _host_for_create_sub()
    result = host.create_sub_issue(parent_url="https://example.com/foo", title="t", body="")
    assert result == {"error": "Not a GitLab issue URL: https://example.com/foo"}


def test_create_sub_issue_errors_on_unknown_type() -> None:
    host, _ = _host_for_create_sub()
    result = host.create_sub_issue(parent_url=_PARENT_URL, title="t", body="", child_type="Bogus")
    assert result == {"error": "Unknown work item type: Bogus"}


def test_create_sub_issue_surfaces_convert_errors() -> None:
    host, _ = _host_for_create_sub(convert_errors=["not allowed"])
    result = host.create_sub_issue(parent_url=_PARENT_URL, title="t", body="")
    assert result == {"error": "Convert to Task failed: not allowed"}


def test_create_sub_issue_surfaces_link_errors() -> None:
    host, _ = _host_for_create_sub(link_errors=["it's not allowed to add this type of parent item"])
    result = host.create_sub_issue(parent_url=_PARENT_URL, title="t", body="")
    assert result == {"error": "Parent link failed: it's not allowed to add this type of parent item"}


def test_create_sub_issue_errors_when_create_returns_no_iid() -> None:
    host, client = _host_for_create_sub()
    client.post_json.return_value = {"web_url": "https://gitlab.com/org/repo/-/issues/9"}
    result = host.create_sub_issue(parent_url=_PARENT_URL, title="t", body="")
    assert "no iid" in result["error"]


def test_create_sub_issue_errors_when_project_unresolved() -> None:
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = None
    host = GitLabCodeHost(client=client)
    result = host.create_sub_issue(parent_url=_PARENT_URL, title="t", body="")
    assert result == {"error": "Could not resolve project: org/repo"}


def test_create_sub_issue_errors_when_parent_gid_unresolved() -> None:
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    client.graphql.return_value = {"data": {"project": {"workItems": {"nodes": []}}}}
    host = GitLabCodeHost(client=client)
    result = host.create_sub_issue(parent_url=_PARENT_URL, title="t", body="")
    assert result == {"error": f"Could not resolve parent work item: {_PARENT_URL}"}


def test_create_sub_issue_propagates_create_issue_error() -> None:
    host, _ = _host_for_create_sub()
    with patch.object(host, "create_issue", return_value={"error": "Could not resolve project: org/repo"}):
        result = host.create_sub_issue(parent_url=_PARENT_URL, title="t", body="")
    assert result == {"error": "Could not resolve project: org/repo"}


def test_create_sub_issue_errors_when_child_gid_unresolved() -> None:
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    client.post_json.return_value = {"iid": 8546, "web_url": "https://gitlab.com/org/repo/-/work_items/8546"}

    def _route(query: str, variables: dict | None = None) -> dict:
        if "workItemTypes" in query:
            return {"data": {"workspace": {"workItemTypes": {"nodes": [{"id": _TASK_TYPE_GID, "name": "Task"}]}}}}
        if (variables or {}).get("iid") == "8545":
            return {"data": {"project": {"workItems": {"nodes": [{"id": _PARENT_GID}]}}}}
        return {"data": {"project": {"workItems": {"nodes": []}}}}

    client.graphql.side_effect = _route
    host = GitLabCodeHost(client=client)
    result = host.create_sub_issue(parent_url=_PARENT_URL, title="t", body="")
    assert "Could not resolve created child work item" in result["error"]


# --- verify_upload: the post-upload self-verification gate (#2156) ----------

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n" + b"rest"
_WEBM_MAGIC = b"\x1a\x45\xdf\xa3" + b"webm-rest"
_HTML_404 = b"<!DOCTYPE html><html>404</html>"
_UPLOAD = {
    "url": "/uploads/deadbeefcafe/shot.png",
    "full_path": "/-/project/42/uploads/deadbeefcafe/shot.png",
    "markdown": "![shot](/uploads/deadbeefcafe/shot.png)",
}


def _verify_host(*, status: int, content: bytes) -> GitLabCodeHost:
    client = MagicMock(spec=GitLabAPI)
    client.base_url = "https://gitlab.com/api/v4"
    client.resolve_project.return_value = _project()
    client.resolve_project_from_remote.return_value = _project()
    client.fetch_upload.return_value = (status, content)
    return GitLabCodeHost(client=client)


def test_verify_upload_returns_absolute_embed_and_ok_on_200_image() -> None:
    host = _verify_host(status=200, content=_PNG_MAGIC)
    result = host.verify_upload(repo="org/repo", upload=_UPLOAD)
    assert result.ok is True
    # The ABSOLUTE full_path form GitLab renders context-independently — NOT
    # the relative /uploads/... path that breaks in the work-items UI.
    assert result.embed_url == "https://gitlab.com/-/project/42/uploads/deadbeefcafe/shot.png"
    assert "/uploads/deadbeefcafe" in result.embed_url
    assert not result.embed_url.startswith("/uploads/")


def test_verify_upload_fails_on_non_200() -> None:
    host = _verify_host(status=404, content=_HTML_404)
    result = host.verify_upload(repo="org/repo", upload=_UPLOAD)
    assert result.ok is False
    assert "404" in result.detail


def test_verify_upload_fails_when_bytes_are_not_the_expected_medium() -> None:
    # 200 but the route served an HTML error page, not the image.
    host = _verify_host(status=200, content=_HTML_404)
    result = host.verify_upload(repo="org/repo", upload=_UPLOAD)
    assert result.ok is False
    assert "not a renderable image" in result.detail


def test_verify_upload_accepts_webm_video_bytes() -> None:
    upload = {
        "url": "/uploads/abc/clip.webm",
        "full_path": "/-/project/42/uploads/abc/clip.webm",
        "markdown": "![clip](/uploads/abc/clip.webm)",
    }
    host = _verify_host(status=200, content=_WEBM_MAGIC)
    result = host.verify_upload(repo="org/repo", upload=upload)
    assert result.ok is True
    assert result.embed_url == "https://gitlab.com/-/project/42/uploads/abc/clip.webm"


def test_verify_upload_fails_on_unparseable_response() -> None:
    host = _verify_host(status=200, content=_PNG_MAGIC)
    result = host.verify_upload(repo="org/repo", upload={"error": "Could not resolve project: org/repo"})
    assert result.ok is False
    assert "unparsable" in result.detail


def test_verify_upload_fails_on_cross_project_upload() -> None:
    # full_path says project 99 but the repo resolves to project 42 — a
    # relative /uploads upload that silently landed on the wrong project.
    host = _verify_host(status=200, content=_PNG_MAGIC)
    cross = {
        "url": "/uploads/deadbeefcafe/shot.png",
        "full_path": "/-/project/99/uploads/deadbeefcafe/shot.png",
        "markdown": "![shot](/uploads/deadbeefcafe/shot.png)",
    }
    result = host.verify_upload(repo="org/repo", upload=cross)
    assert result.ok is False
    assert "expected 42" in result.detail
