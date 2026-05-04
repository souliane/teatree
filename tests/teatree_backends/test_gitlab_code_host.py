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


def test_list_open_prs_returns_empty_for_unknown_project() -> None:
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = None
    host = GitLabCodeHost(client=client)

    assert host.list_open_prs("org/repo", "adrien") == []


def test_create_pr_returns_error_when_project_not_resolved() -> None:
    """create_pr returns error dict when _resolve_project returns None (line 31)."""
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


def test_list_open_prs_returns_data_for_known_project() -> None:
    """list_open_prs delegates to client.get_json and returns list (lines 49-52)."""
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    client.get_json.return_value = [{"iid": 1, "title": "MR 1"}]
    host = GitLabCodeHost(client=client)

    result = host.list_open_prs("org/repo", "adrien")

    assert result == [{"iid": 1, "title": "MR 1"}]
    client.get_json.assert_called_once_with(
        "projects/42/merge_requests?state=opened&author_username=adrien&per_page=100",
    )


def test_list_open_prs_returns_empty_when_response_not_list() -> None:
    """list_open_prs returns [] when API returns a dict instead of list (line 52)."""
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    client.get_json.return_value = {"error": "something"}
    host = GitLabCodeHost(client=client)

    assert host.list_open_prs("org/repo", "adrien") == []


def test_list_my_open_prs_delegates_to_client_list_all_open_mrs() -> None:
    """list_my_open_prs returns the forge-wide list of MRs authored by user."""
    client = MagicMock(spec=GitLabAPI)
    client.list_all_open_mrs.return_value = [
        {"iid": 1, "title": "MR 1", "web_url": "https://gitlab.com/org/repo/-/merge_requests/1"},
        {"iid": 2, "title": "MR 2", "web_url": "https://gitlab.com/org/other/-/merge_requests/2"},
    ]
    host = GitLabCodeHost(client=client)

    result = host.list_my_open_prs("adrien")

    assert len(result) == 2
    assert result[0]["iid"] == 1
    client.list_all_open_mrs.assert_called_once_with("adrien")


def test_list_my_open_prs_returns_empty_when_no_mrs() -> None:
    client = MagicMock(spec=GitLabAPI)
    client.list_all_open_mrs.return_value = []
    host = GitLabCodeHost(client=client)

    assert host.list_my_open_prs("adrien") == []


def test_post_mr_note_returns_error_when_project_not_resolved() -> None:
    """post_mr_note returns error when project cannot be resolved (lines 55-57)."""
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = None
    host = GitLabCodeHost(client=client)

    result = host.post_mr_note(repo="org/unknown", mr_iid=10, body="note")

    assert result == {"error": "Could not resolve project: org/unknown"}
    client.post_json.assert_not_called()


def test_post_mr_note_posts_to_correct_endpoint() -> None:
    """post_mr_note posts note body to the MR notes endpoint (lines 59-60)."""
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    client.post_json.return_value = {"id": 99}
    host = GitLabCodeHost(client=client)

    result = host.post_mr_note(repo="org/repo", mr_iid=10, body="Test note")

    assert result == {"id": 99}
    client.post_json.assert_called_once_with(
        "projects/42/merge_requests/10/notes",
        {"body": "Test note"},
    )


def test_post_mr_note_returns_empty_dict_when_post_returns_none() -> None:
    """post_mr_note returns {} when post_json returns None (line 60)."""
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    client.post_json.return_value = None
    host = GitLabCodeHost(client=client)

    result = host.post_mr_note(repo="org/repo", mr_iid=5, body="note")

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
