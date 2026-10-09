from unittest.mock import patch

import pytest

from teatree.backends.github.client import GitHubCodeHost
from teatree.core.backend_protocols import PrReview, PrReviewComment
from teatree.utils.run import CommandFailedError

_MARKER = "<!-- m -->"


def _review(body: str = "sum", *comments: PrReviewComment) -> PrReview:
    return PrReview(commit_sha="h" * 40, body=body, comments=comments, marker=_MARKER)


def test_submit_pr_review_posts_one_submitted_review_with_inline_comments() -> None:
    host = GitHubCodeHost(token="t")
    review = _review("sum", PrReviewComment(path="a.py", line=9, body="Nit: rename"))
    with patch("teatree.backends.github.pr_notes._gh_api_post", return_value={"html_url": "u"}) as post:
        result = host.submit_pr_review(repo="o/r", pr_iid=5, review=review)

    assert result == {"html_url": "u"}
    post.assert_called_once_with(
        "repos/o/r/pulls/5/reviews",
        {
            "commit_id": "h" * 40,
            "body": f"sum\n\n{_MARKER}",
            "event": "COMMENT",
            "comments": [{"path": "a.py", "line": 9, "side": "RIGHT", "body": "Nit: rename"}],
        },
        token="t",
    )


def test_a_review_with_no_summary_carries_only_the_marker_in_its_body() -> None:
    host = GitHubCodeHost(token="t")
    review = _review("", PrReviewComment(path="a.py", line=9, body="rename"))
    with patch("teatree.backends.github.pr_notes._gh_api_post", return_value={}) as post:
        host.submit_pr_review(repo="o/r", pr_iid=5, review=review)
    assert post.call_args.args[1]["body"] == _MARKER


def test_submit_pr_review_tolerates_a_non_dict_response() -> None:
    host = GitHubCodeHost(token="t")
    with patch("teatree.backends.github.pr_notes._gh_api_post", return_value=None):
        assert host.submit_pr_review(repo="o/r", pr_iid=5, review=_review()) == {}


def test_find_pr_review_matches_the_marker_in_a_review_body() -> None:
    host = GitHubCodeHost(token="t")
    reviews = [{"body": "plain"}, {"body": f"x {_MARKER}"}, {"body": None}]
    with patch("teatree.backends.github.pr_notes._gh_api_get_paginated", return_value=reviews) as get:
        assert host.find_pr_review(repo="o/r", pr_iid=5, marker=_MARKER)
        assert not host.find_pr_review(repo="o/r", pr_iid=5, marker="<!-- z -->")
    assert get.call_args.args[0] == "repos/o/r/pulls/5/reviews?per_page=100"


def test_get_pr_file_diffs_keys_each_patch_by_its_old_and_new_path() -> None:
    files = [
        {"filename": "a.py", "patch": "@@ -1 +1 @@\n+x"},
        {"filename": "new.py", "previous_filename": "old.py", "patch": "@@ -1 +1 @@\n+y"},
        {"filename": "big.bin"},
    ]
    with patch("teatree.backends.github.client._gh_api_get_paginated", return_value=files) as get:
        diffs = GitHubCodeHost(token="t").get_pr_file_diffs(repo="o/r", pr_iid=5)

    assert diffs == {
        "a.py": "@@ -1 +1 @@\n+x",
        "old.py": "@@ -1 +1 @@\n+y",
        "new.py": "@@ -1 +1 @@\n+y",
        "big.bin": None,
    }
    assert get.call_args.args[0] == "repos/o/r/pulls/5/files?per_page=100"


def test_get_pr_file_diffs_raises_on_a_pr_it_cannot_find_rather_than_reading_no_diff() -> None:
    not_found = CommandFailedError(["gh"], 1, "", "gh: Not Found (HTTP 404)")
    with (
        patch("teatree.backends.github.client._gh_api_get_paginated", side_effect=not_found),
        pytest.raises(CommandFailedError),
    ):
        GitHubCodeHost(token="t").get_pr_file_diffs(repo="o/r", pr_iid=5)
