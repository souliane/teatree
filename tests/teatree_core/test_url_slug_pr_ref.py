"""``pr_ref_from_url`` — parse a PR/MR web URL into the canonical ``PrRef``.

Pure parsing logic feeding ``review status``: a GitHub ``/pull/<n>`` and a
GitLab ``/-/merge_requests/<iid>`` URL must both yield the canonical
:class:`teatree.utils.pr_ref.PrRef` (slug, ``pr_id``, host_kind) — the same forge
transport switch the merge-execution ``CodeHostQuery`` dispatches on.
"""

import pytest

from teatree.utils.pr_ref import PrRef
from teatree.utils.url_slug import pr_ref_from_url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://github.com/souliane/teatree/pull/1680",
            PrRef(slug="souliane/teatree", pr_id=1680, host_kind="github"),
        ),
        (
            "https://github.com/souliane/teatree/pull/1680/",
            PrRef(slug="souliane/teatree", pr_id=1680, host_kind="github"),
        ),
        (
            "https://gitlab.com/group/sub/project/-/merge_requests/42",
            PrRef(slug="group/sub/project", pr_id=42, host_kind="gitlab"),
        ),
        (
            "https://gitlab.example.com/team/api/-/merge_requests/7",
            PrRef(slug="team/api", pr_id=7, host_kind="gitlab"),
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
