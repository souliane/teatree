"""``project_slug_from_ref`` — normalize a CLI ref to a canonical repo slug (#2892).

The per-project-learnings analogue of #2293's ``repo_namespaced_key``: instead
of an issue-scoped ``<repo-slug>#<issue-number>`` key, this resolves just the
*repo* identity — accepting either a literal ``owner/repo`` slug or a full
issue/PR/MR URL, reusing the same :func:`slug_from_issue_or_pr_url` parser so
there is exactly one repo-slug extraction mechanism in the codebase.
"""

import pytest

from teatree.utils.url_slug import project_slug_from_ref


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("acme-eng/widgets", "acme-eng/widgets"),
        ("acme-eng/widgets/", "acme-eng/widgets"),
        ("https://github.com/acme-eng/widgets/issues/42", "acme-eng/widgets"),
        ("https://github.com/acme-eng/widgets/pull/42", "acme-eng/widgets"),
        ("https://gitlab.com/group/sub/project/-/issues/7", "group/sub/project"),
        ("https://gitlab.com/group/sub/project/-/merge_requests/7", "group/sub/project"),
    ],
)
def test_resolves_known_ref_shapes(ref: str, expected: str) -> None:
    assert project_slug_from_ref(ref) == expected


@pytest.mark.parametrize(
    "ref",
    [
        "",
        "https://example.com/not-a-forge-path",
        "https://github.com/acme-eng/widgets",
    ],
)
def test_returns_empty_for_unrecognised_ref(ref: str) -> None:
    assert project_slug_from_ref(ref) == ""


def test_two_different_repos_never_collide_on_the_same_slug() -> None:
    first = project_slug_from_ref("https://github.com/acme-eng/bugs/issues/2242")
    second = project_slug_from_ref("https://github.com/acme-product/repo/issues/2242")
    assert first != second
