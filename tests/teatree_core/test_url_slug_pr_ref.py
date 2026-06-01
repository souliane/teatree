"""``pr_ref_from_url`` — parse a PR/MR web URL into (slug, number, host_kind).

Pure parsing logic feeding ``review status``: a GitHub ``/pull/<n>`` and a
GitLab ``/-/merge_requests/<iid>`` URL must both yield the repo slug, the PR
number, and the forge transport switch ``fetch_live_head_sha`` dispatches on.
"""

import pytest

from teatree.utils.url_slug import PrRef, pr_ref_from_url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://github.com/souliane/teatree/pull/1680",
            PrRef(slug="souliane/teatree", number=1680, host_kind="github"),
        ),
        (
            "https://github.com/souliane/teatree/pull/1680/",
            PrRef(slug="souliane/teatree", number=1680, host_kind="github"),
        ),
        (
            "https://gitlab.com/group/sub/project/-/merge_requests/42",
            PrRef(slug="group/sub/project", number=42, host_kind="gitlab"),
        ),
        (
            "https://gitlab.example.com/team/api/-/merge_requests/7",
            PrRef(slug="team/api", number=7, host_kind="gitlab"),
        ),
    ],
)
def test_parses_known_pr_mr_urls(url: str, expected: PrRef) -> None:
    assert pr_ref_from_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/souliane/teatree/issues/1680",
        "https://github.com/souliane/teatree",
        "not-a-url",
        "",
    ],
)
def test_returns_none_for_unrecognised_urls(url: str) -> None:
    assert pr_ref_from_url(url) is None
