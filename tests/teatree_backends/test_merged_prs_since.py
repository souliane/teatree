"""The windowed merged-PR read both code hosts owe the external-outcome measure (#4506).

The window matters: an unwindowed "every merged PR" read grows without bound on a
long-lived repo. And the failure polarity matters more — downstream an empty result
means "the factory shipped nothing", so neither host may answer a failed read with one.
"""

import json
import subprocess
from unittest.mock import patch

import pytest

from teatree.backends.github.client import GitHubCodeHost
from teatree.backends.gitlab import pr_reads as gitlab_pr_reads
from teatree.backends.gitlab.api import ProjectInfo
from teatree.types import RawAPIDict
from teatree.utils.run import CommandFailedError


def _completed(payload: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, json.dumps(payload), "")


class TestGitHubMergedPrsSince:
    def test_query_is_scoped_to_the_repo_state_and_window(self) -> None:
        with patch("teatree.backends.github.api._run_gh", return_value=_completed([[]])) as run:
            GitHubCodeHost(token="t").list_merged_prs_since(repo="acme/app", since="2026-08-11")

        endpoint = run.call_args.args[2]
        assert "repo%3Aacme%2Fapp" in endpoint
        assert "is%3Amerged" in endpoint
        assert "merged%3A%3E%3D2026-08-11" in endpoint

    def test_returns_the_search_hits(self) -> None:
        hits: list[RawAPIDict] = [{"number": 1}, {"number": 2}]
        with patch("teatree.backends.github.api._run_gh", return_value=_completed([{"items": hits}])):
            found = GitHubCodeHost(token="t").list_merged_prs_since(repo="acme/app", since="2026-08-11")

        assert [hit["number"] for hit in found] == [1, 2]

    def test_a_failed_read_raises_rather_than_reporting_no_merges(self) -> None:
        failure = CommandFailedError(["gh"], 1, "", "HTTP 403 rate limited")
        with (
            patch("teatree.backends.github.api._run_gh", side_effect=failure),
            pytest.raises(CommandFailedError),
        ):
            GitHubCodeHost(token="t").list_merged_prs_since(repo="acme/app", since="2026-08-11")

    def test_a_404_also_raises_unlike_the_sibling_pr_reads(self) -> None:
        failure = CommandFailedError(["gh"], 1, "", "HTTP 404")
        with (
            patch("teatree.backends.github.api._run_gh", side_effect=failure),
            pytest.raises(CommandFailedError),
        ):
            GitHubCodeHost(token="t").list_merged_prs_since(repo="acme/app", since="2026-08-11")


class _StubGitLabApi:
    def __init__(self, rows: list[RawAPIDict]) -> None:
        self._rows = rows
        self.endpoints: list[str] = []

    def get_json_paginated(self, endpoint: str) -> list[RawAPIDict]:
        self.endpoints.append(endpoint)
        return self._rows


def _project() -> ProjectInfo:
    return ProjectInfo(
        project_id=7,
        path_with_namespace="acme/app",
        short_name="app",
        default_branch="main",
    )


class TestGitLabMergedPrsSince:
    def test_query_filters_state_and_window_server_side(self) -> None:
        client = _StubGitLabApi([])

        gitlab_pr_reads.list_project_merged_prs_since(client, _project(), repo="acme/app", since="2026-08-11")

        assert client.endpoints == ["projects/7/merge_requests?state=merged&updated_after=2026-08-11&per_page=100"]

    def test_an_mr_touched_after_merging_outside_the_window_is_dropped(self) -> None:
        client = _StubGitLabApi(
            [
                {"iid": 1, "merged_at": "2026-08-12T09:00:00Z"},
                {"iid": 2, "merged_at": "2026-07-01T09:00:00Z"},
            ]
        )

        found = gitlab_pr_reads.list_project_merged_prs_since(client, _project(), repo="acme/app", since="2026-08-11")

        assert [row["iid"] for row in found] == [1]

    def test_an_unresolvable_project_raises_rather_than_reporting_no_merges(self) -> None:
        with pytest.raises(gitlab_pr_reads.ProjectUnresolvedError):
            gitlab_pr_reads.list_project_merged_prs_since(
                _StubGitLabApi([]),
                None,
                repo="acme/app",
                since="2026-08-11",
            )
