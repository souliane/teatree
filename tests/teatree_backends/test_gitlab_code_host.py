from unittest.mock import MagicMock

from teatree.backends.gitlab import GitLabCodeHost
from teatree.backends.gitlab_api import GitLabAPI, ProjectInfo
from teatree.backends.protocols import PullRequestSpec


def _project() -> ProjectInfo:
    return ProjectInfo(project_id=42, path_with_namespace="org/repo", short_name="repo", default_branch="main")


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
    client.list_all_open_mrs.assert_called_once_with("adrien")


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
    client.list_open_mrs_as_reviewer.assert_called_once_with("adrien")


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
