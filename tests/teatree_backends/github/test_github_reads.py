import json
from unittest.mock import MagicMock, patch

import pytest

from teatree.backends.errors import IssueNotFoundError
from teatree.backends.github.reads import get_issue, list_pr_reviews, list_repo_open_issues
from teatree.utils.run import CommandFailedError

_RUN_GH = "teatree.backends.github.api._run_gh"


def _gh_answers(payload: object) -> MagicMock:
    return MagicMock(stdout=json.dumps(payload))


def _gh_fails(stderr: str) -> CommandFailedError:
    return CommandFailedError(["gh"], 1, "", stderr)


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


def test_get_issue_propagates_a_failure_that_is_not_a_404() -> None:
    with patch(_RUN_GH, side_effect=_gh_fails("gh: Server Error (HTTP 502)")), pytest.raises(CommandFailedError):
        get_issue(issue_url="https://github.com/o/r/issues/9", token="t")


def test_list_repo_open_issues_drops_pull_requests() -> None:
    issues = [{"number": 1}, {"number": 2, "pull_request": {"url": "https://github.com/o/r/pull/2"}}]
    with patch(_RUN_GH, return_value=_gh_answers([issues])) as run:
        assert list_repo_open_issues(repo="o/r", token="t") == [{"number": 1}]

    assert "repos/o/r/issues?state=open&per_page=100" in run.call_args.args
