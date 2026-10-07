from unittest.mock import patch

from teatree.backends.github.client import GitHubCodeHost
from teatree.core.backend_protocols import PrReviewComment


def test_submit_pr_review_posts_one_review_with_inline_comments() -> None:
    host = GitHubCodeHost(token="t")
    with patch("teatree.backends.github.pr_notes._gh_api_post", return_value={"html_url": "u"}) as post:
        result = host.submit_pr_review(
            repo="o/r",
            pr_iid=5,
            head_sha="h" * 40,
            summary="sum",
            comments=[PrReviewComment(path="a.py", line=9, body="**[nit]** rename")],
        )

    assert result == {"html_url": "u"}
    post.assert_called_once_with(
        "repos/o/r/pulls/5/reviews",
        {
            "commit_id": "h" * 40,
            "body": "sum",
            "event": "COMMENT",
            "comments": [{"path": "a.py", "line": 9, "side": "RIGHT", "body": "**[nit]** rename"}],
        },
        token="t",
    )


def test_submit_pr_review_tolerates_a_non_dict_response() -> None:
    host = GitHubCodeHost(token="t")
    with patch("teatree.backends.github.pr_notes._gh_api_post", return_value=None):
        assert host.submit_pr_review(repo="o/r", pr_iid=5, head_sha="h", summary="s", comments=[]) == {}


def test_find_pr_review_matches_the_marker_in_a_review_body() -> None:
    host = GitHubCodeHost(token="t")
    reviews = [{"body": "plain"}, {"body": "x <!-- m -->"}, {"body": None}]
    with patch("teatree.backends.github.pr_notes._gh_api_get_paginated", return_value=reviews) as get:
        assert host.find_pr_review(repo="o/r", pr_iid=5, marker="<!-- m -->")
        assert not host.find_pr_review(repo="o/r", pr_iid=5, marker="<!-- z -->")
    assert get.call_args.args[0] == "repos/o/r/pulls/5/reviews?per_page=100"
