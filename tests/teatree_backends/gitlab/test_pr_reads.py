from unittest.mock import MagicMock

from teatree.backends.gitlab.api import GitLabAPI, ProjectInfo
from teatree.backends.gitlab.pr_reads import (
    list_project_pr_commits,
    list_project_prs,
    project_pr_diff,
    repo_metadata,
    state_filter,
)


def _project() -> ProjectInfo:
    return ProjectInfo(project_id=42, path_with_namespace="org/repo", short_name="repo", default_branch="main")


def test_state_filter_translates_open_to_opened() -> None:
    assert state_filter("open") == "opened"


def test_state_filter_passes_native_states_verbatim() -> None:
    assert state_filter("merged") == "merged"


def test_list_prs_builds_state_and_author_query() -> None:
    client = MagicMock(spec=GitLabAPI)
    client.get_json_paginated.return_value = [{"iid": 5}]

    result = list_project_prs(client, _project(), state="open", author="alice")

    assert result == [{"iid": 5}]
    client.get_json_paginated.assert_called_once_with(
        "projects/42/merge_requests?per_page=100&state=opened&author_username=alice"
    )


def test_list_prs_unresolvable_project_returns_empty() -> None:
    client = MagicMock(spec=GitLabAPI)
    assert list_project_prs(client, None, state="", author="") == []
    client.get_json_paginated.assert_not_called()


def test_get_pr_diff_hits_diffs_endpoint() -> None:
    client = MagicMock(spec=GitLabAPI)
    client.get_json_paginated.return_value = [{"new_path": "a.py"}]

    result = project_pr_diff(client, _project(), pr_iid=7)

    assert result == [{"new_path": "a.py"}]
    client.get_json_paginated.assert_called_once_with("projects/42/merge_requests/7/diffs?per_page=100")


def test_get_pr_diff_unresolvable_project_returns_empty() -> None:
    assert project_pr_diff(MagicMock(spec=GitLabAPI), None, pr_iid=7) == []


def test_list_pr_commits_hits_commits_endpoint() -> None:
    client = MagicMock(spec=GitLabAPI)
    client.get_json_paginated.return_value = [{"id": "abc"}]

    result = list_project_pr_commits(client, _project(), pr_iid=7)

    assert result == [{"id": "abc"}]
    client.get_json_paginated.assert_called_once_with("projects/42/merge_requests/7/commits?per_page=100")


def test_list_pr_commits_unresolvable_project_returns_empty() -> None:
    assert list_project_pr_commits(MagicMock(spec=GitLabAPI), None, pr_iid=7) == []


def test_repo_metadata_returns_project_fields() -> None:
    assert repo_metadata(_project(), repo="org/repo") == {
        "id": 42,
        "path_with_namespace": "org/repo",
        "short_name": "repo",
        "default_branch": "main",
    }


def test_repo_metadata_unresolvable_project_returns_structured_error() -> None:
    assert repo_metadata(None, repo="org/missing") == {"error": "Could not resolve project: org/missing"}
