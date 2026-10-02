"""``teatree.url_classify`` — the single forge-classification + PR/MR-URL parser.

Consolidates the forge ``in``-check dispatch, the GitLab-MR ``(project, iid)``
splitters, and the second ``parse_pr_url`` that had drifted across the scanners
onto one home built on :func:`teatree.utils.url_slug.pr_ref_from_url`.
"""

import pytest

from teatree.url_classify import Forge, forge_of, is_github_pr_url, is_gitlab_mr_url, pr_ref, repo_and_iid

_GITHUB_PR = "https://github.com/souliane/teatree/pull/1573"
_GITHUB_PR_PLURAL = "https://github.com/souliane/teatree/pulls/1573"
_GITLAB_MR = "https://gitlab.com/acme/backend/-/merge_requests/42"
_GITLAB_NESTED = "https://gitlab.example.com/team/sub/api/-/merge_requests/7"
_ISSUE = "https://github.com/souliane/teatree/issues/1573"


class TestForgeOf:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            (_GITHUB_PR, Forge.GITHUB),
            (_GITHUB_PR_PLURAL, Forge.GITHUB),
            (_GITLAB_MR, Forge.GITLAB),
            (_GITLAB_NESTED, Forge.GITLAB),
            (_ISSUE, Forge.UNKNOWN),
            ("", Forge.UNKNOWN),
            ("https://example.com/whatever", Forge.UNKNOWN),
        ],
    )
    def test_classifies_by_path_shape(self, url: str, expected: Forge) -> None:
        assert forge_of(url) is expected

    def test_predicates_match_classification(self) -> None:
        assert is_github_pr_url(_GITHUB_PR)
        assert not is_github_pr_url(_GITLAB_MR)
        assert is_gitlab_mr_url(_GITLAB_MR)
        assert not is_gitlab_mr_url(_GITHUB_PR)


class TestPrRefAndRepoAndIid:
    def test_parses_github_pr(self) -> None:
        ref = pr_ref(_GITHUB_PR)
        assert ref is not None
        assert ref.slug == "souliane/teatree"
        assert ref.pr_id == 1573
        assert ref.host_kind == "github"

    def test_repo_and_iid_github(self) -> None:
        assert repo_and_iid(_GITHUB_PR) == ("souliane/teatree", 1573)

    def test_repo_and_iid_gitlab(self) -> None:
        assert repo_and_iid(_GITLAB_MR) == ("acme/backend", 42)

    def test_repo_and_iid_nested_gitlab_subgroups(self) -> None:
        assert repo_and_iid(_GITLAB_NESTED) == ("team/sub/api", 7)

    def test_returns_none_for_non_pr_url(self) -> None:
        assert pr_ref(_ISSUE) is None
        assert repo_and_iid(_ISSUE) is None
