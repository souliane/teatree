from unittest.mock import MagicMock

from teatree.backends.gitlab import GitLabCodeHost
from teatree.backends.gitlab.api import GitLabAPI, ProjectInfo
from teatree.backends.gitlab.reads import list_pr_commits


def _project() -> ProjectInfo:
    return ProjectInfo(project_id=42, path_with_namespace="org/repo", short_name="repo", default_branch="main")


def test_list_pr_commits_hits_commits_endpoint() -> None:
    client = MagicMock(spec=GitLabAPI)
    client.get_json_paginated.return_value = [{"id": "abc"}]

    result = list_pr_commits(client, _project(), pr_iid=7)

    assert result == [{"id": "abc"}]
    client.get_json_paginated.assert_called_once_with("projects/42/merge_requests/7/commits?per_page=100")


def test_list_pr_commits_unresolvable_project_returns_empty() -> None:
    assert list_pr_commits(MagicMock(spec=GitLabAPI), None, pr_iid=7) == []


def test_code_host_reads_pr_commits_through_the_read_interface() -> None:
    client = MagicMock(spec=GitLabAPI)
    client.resolve_project.return_value = _project()
    client.get_json_paginated.return_value = [{"id": "abc"}]

    assert GitLabCodeHost(client=client).list_pr_commits(repo="org/repo", pr_iid=7) == [{"id": "abc"}]
    client.get_json_paginated.assert_called_once_with("projects/42/merge_requests/7/commits?per_page=100")
