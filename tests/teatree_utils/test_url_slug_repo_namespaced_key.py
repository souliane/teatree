"""``repo_namespaced_key`` — collision-free ``<repo-slug>#<issue-number>`` (#2293).

A bare numeric issue IID collides across repos (``acme-eng/bugs#42`` vs
``acme-product#42``); the repo-namespaced key never does, since the full
repo path is part of it. Issue-only by design — a GitLab MR/issue in the
same project uses a separate numbering sequence, so a PR/MR path must
never feed this key (see the module docstring on the issue-only regexes).
"""

import pytest

from teatree.utils.url_slug import repo_namespaced_key


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/acme-eng/widgets/issues/42", "acme-eng/widgets#42"),
        ("https://github.com/acme-eng/widgets/issues/42/", "acme-eng/widgets#42"),
        ("https://gitlab.com/group/sub/project/-/issues/7", "group/sub/project#7"),
        ("https://gitlab.example.com/team/api/-/work_items/9", "team/api#9"),
    ],
)
def test_parses_known_issue_urls(url: str, expected: str) -> None:
    assert repo_namespaced_key(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        # PR/MR paths never feed the key — a GitLab MR and an issue in the
        # same project share no numbering sequence, so treating them the
        # same way risks a genuine collision.
        "https://github.com/acme-eng/widgets/pull/42",
        "https://gitlab.com/group/project/-/merge_requests/42",
        "https://example.com/issues/1",
        "not-a-url",
        "694",
        "",
    ],
)
def test_returns_empty_for_non_issue_or_unrecognised_urls(url: str) -> None:
    assert repo_namespaced_key(url) == ""


def test_different_repos_sharing_an_issue_number_never_collide() -> None:
    first = repo_namespaced_key("https://github.com/acme-eng/bugs/issues/2242")
    second = repo_namespaced_key("https://github.com/acme-product/repo/issues/2242")
    assert first != second
    assert first == "acme-eng/bugs#2242"
    assert second == "acme-product/repo#2242"
