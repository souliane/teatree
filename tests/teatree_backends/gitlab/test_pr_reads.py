from unittest.mock import MagicMock

import pytest

from teatree.backends.gitlab import GitLabCodeHost
from teatree.backends.gitlab.api import GitLabAPI, ProjectInfo
from teatree.backends.gitlab.pr_reads import (
    list_project_pr_commits,
    list_project_prs,
    project_pr_diff,
    repo_metadata,
    state_filter,
)
from teatree.core.review.mr_ci_state import carries_pipeline_field, ci_state
from teatree.core.review.mr_triage import CiState
from tests.teatree_backends._gitlab_wire import GitLabWire


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


_PIPELINE_URL = "https://gitlab.example/org/repo/-/pipelines/9"


def _listed(iid: int, *, project_id: int = 42) -> dict[str, object]:
    return {
        "iid": iid,
        "project_id": project_id,
        "title": f"MR {iid}",
        "web_url": f"https://gitlab.example/org/repo/-/merge_requests/{iid}",
    }


def _pipelines_path(project_id: int, iid: int) -> str:
    return f"projects/{project_id}/merge_requests/{iid}/pipelines"


@pytest.mark.parametrize(
    ("status", "expected"),
    [("success", CiState.GREEN), ("failed", CiState.FAILED), ("running", CiState.PENDING)],
)
def test_list_my_prs_reads_each_listed_mrs_pipeline(status: str, expected: CiState) -> None:
    wire = GitLabWire(
        {
            "merge_requests": [_listed(1)],
            _pipelines_path(42, 1): [{"status": status, "web_url": _PIPELINE_URL}],
        }
    )

    (row,) = GitLabCodeHost(client=wire).list_my_prs(author="souliane")

    assert ci_state(row) is expected
    assert row["head_pipeline"] == {"status": status, "web_url": _PIPELINE_URL}


def test_list_my_prs_reads_each_mr_against_its_own_project() -> None:
    wire = GitLabWire(
        {
            "merge_requests": [_listed(1), _listed(2, project_id=7)],
            _pipelines_path(42, 1): [{"status": "success", "web_url": _PIPELINE_URL}],
            _pipelines_path(7, 2): [{"status": "failed", "web_url": _PIPELINE_URL}],
        }
    )

    rows = GitLabCodeHost(client=wire).list_my_prs(author="souliane")

    assert [ci_state(row) for row in rows] == [CiState.GREEN, CiState.FAILED]


def test_list_my_prs_without_enrich_reads_no_pipeline() -> None:
    wire = GitLabWire({"merge_requests": [_listed(1)]})

    (row,) = GitLabCodeHost(client=wire).list_my_prs(author="souliane", enrich=False)

    assert not carries_pipeline_field(row)
    assert wire.paths == ["merge_requests"]


def test_an_unreadable_pipeline_leaves_the_row_unknown_never_green() -> None:
    wire = GitLabWire({"merge_requests": [_listed(1)]}, failures={_pipelines_path(42, 1): 502})

    (row,) = GitLabCodeHost(client=wire).list_my_prs(author="souliane")

    assert not carries_pipeline_field(row)
    assert ci_state(row) is CiState.UNKNOWN


def test_an_mr_with_no_pipeline_stays_unknown() -> None:
    wire = GitLabWire({"merge_requests": [_listed(1)], _pipelines_path(42, 1): []})

    (row,) = GitLabCodeHost(client=wire).list_my_prs(author="souliane")

    assert ci_state(row) is CiState.UNKNOWN


def test_a_row_without_a_project_id_asks_the_forge_nothing() -> None:
    wire = GitLabWire({"merge_requests": [{"iid": 1, "web_url": "https://gitlab.example/org/repo/-/merge_requests/1"}]})

    (row,) = GitLabCodeHost(client=wire).list_my_prs(author="souliane")

    assert ci_state(row) is CiState.UNKNOWN
    assert wire.paths == ["merge_requests"]
