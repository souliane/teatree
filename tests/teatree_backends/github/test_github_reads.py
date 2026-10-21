import json
from collections.abc import Callable
from unittest.mock import MagicMock, patch

import pytest

from teatree.backends.errors import IssueNotFoundError
from teatree.backends.forge_merge_rpc import Runner
from teatree.backends.github import reads as github_reads
from teatree.backends.github.client import GitHubCodeHost
from teatree.backends.github.reads import get_issue, list_pr_comments, list_pr_reviews, list_repo_open_issues
from teatree.utils.run import CommandFailedError

_RUN_GH = "teatree.backends.github.api._run_gh"


def _gh_answers(payload: object) -> MagicMock:
    return MagicMock(stdout=json.dumps(payload))


def _gh_fails(stderr: str) -> CommandFailedError:
    return CommandFailedError(["gh"], 1, "", stderr)


def _merge_runner_answering(stdout: str, seen_tokens: list[str]) -> Callable[[str], Runner]:
    def runner(token: str) -> Runner:
        seen_tokens.append(token)
        return lambda _argv: (0, stdout, "")

    return runner


def test_list_pr_reviews_reads_every_review_page() -> None:
    with patch(_RUN_GH, return_value=_gh_answers([[{"state": "APPROVED"}], [{"state": "PENDING"}]])) as run:
        reviews = list_pr_reviews(repo="o/r", pr_iid=5, token="t")

    assert reviews == [{"state": "APPROVED"}, {"state": "PENDING"}]
    assert "repos/o/r/pulls/5/reviews?per_page=100" in run.call_args.args


def test_list_pr_reviews_reads_an_unknown_pr_as_no_reviews() -> None:
    with patch(_RUN_GH, side_effect=_gh_fails("gh: Not Found (HTTP 404)")):
        assert list_pr_reviews(repo="o/r", pr_iid=5, token="t") == []


def test_list_pr_reviews_raises_on_a_failure_that_is_not_a_404() -> None:
    with patch(_RUN_GH, side_effect=_gh_fails("gh: Bad credentials (HTTP 401)")), pytest.raises(CommandFailedError):
        list_pr_reviews(repo="o/r", pr_iid=5, token="t")


def test_get_issue_raises_issue_not_found_on_a_404() -> None:
    with patch(_RUN_GH, side_effect=_gh_fails("gh: Not Found (HTTP 404)")), pytest.raises(IssueNotFoundError):
        get_issue(issue_url="https://github.com/o/r/issues/9", token="t")


@pytest.mark.parametrize("stderr", ["gh: Server Error (HTTP 500)", "gh: Server Error (HTTP 502)"])
def test_get_issue_propagates_a_failure_that_is_not_a_404(stderr: str) -> None:
    with patch(_RUN_GH, side_effect=_gh_fails(stderr)), pytest.raises(CommandFailedError):
        get_issue(issue_url="https://github.com/o/r/issues/9", token="t")


def test_list_repo_open_issues_drops_pull_requests() -> None:
    issues = [{"number": 1}, {"number": 2, "pull_request": {"url": "https://github.com/o/r/pull/2"}}]
    with patch(_RUN_GH, return_value=_gh_answers([issues])) as run:
        assert list_repo_open_issues(repo="o/r", token="t") == [{"number": 1}]

    assert "repos/o/r/issues?state=open&per_page=100" in run.call_args.args


def test_list_pr_comments_reads_the_paginated_issue_endpoint() -> None:
    with patch.object(github_reads, "_gh_api_get_paginated", return_value=[{"id": 9}]) as get:
        assert list_pr_comments(repo="o/r", pr_iid=5, token="t") == [{"id": 9}]

    get.assert_called_once_with("repos/o/r/issues/5/comments?per_page=100", token="t")


@pytest.mark.parametrize(
    ("method", "arguments", "payload", "transport"),
    [
        ("list_pr_reviews", {"repo": "o/r", "pr_iid": 5}, [[{"id": 9}]], "api"),
        ("list_pr_commits", {"repo": "o/r", "pr_iid": 5}, [[{"id": 9}]], "api"),
        ("list_pr_comments", {"repo": "o/r", "pr_iid": 5}, [[{"id": 9}]], "api"),
        ("list_issue_comments", {"issue_url": "https://github.com/o/r/issues/5"}, [[{"id": 9}]], "api"),
        ("list_repo_open_issues", {"repo": "o/r"}, [[{"id": 9}]], "api"),
        ("fetch_required_checks_rollup", {"slug": "o/r", "pr_id": 5}, [{"id": 9}], "merge"),
        (
            "fetch_workflow_runs_at_head",
            {"slug": "o/r", "head_sha": "a" * 40},
            [{"workflow_runs": [{"id": 9}]}],
            "merge",
        ),
    ],
)
def test_host_delegates_reads_with_its_token(
    method: str, arguments: dict[str, object], payload: object, transport: str
) -> None:
    seen_tokens: list[str] = []

    def api_run(*_argv: str, token: str, **_kwargs: object) -> MagicMock:
        seen_tokens.append(token)
        return _gh_answers(payload)

    if transport == "api":
        with patch(_RUN_GH, side_effect=api_run):
            result = getattr(GitHubCodeHost(token="t"), method)(**arguments)
    else:
        with (
            patch(
                "teatree.backends.forge_merge_rpc.gh_runner",
                side_effect=_merge_runner_answering(json.dumps(payload), seen_tokens),
            ),
            patch.object(
                GitHubCodeHost, "_merge_rpc", side_effect=AssertionError("check reads bypass the reads module")
            ),
        ):
            result = getattr(GitHubCodeHost(token="t"), method)(**arguments)

    assert result == [{"id": 9}]
    assert seen_tokens == ["t"]


def test_host_merge_rpc_passes_its_token_to_the_transport() -> None:
    seen_tokens: list[str] = []
    with patch(
        "teatree.backends.forge_merge_rpc.gh_runner", side_effect=_merge_runner_answering("a" * 40, seen_tokens)
    ):
        result = GitHubCodeHost(token="t").fetch_live_head_sha(slug="o/r", pr_id=5)

    assert result == "a" * 40
    assert seen_tokens == ["t"]
