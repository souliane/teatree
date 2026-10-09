"""The ``/*/`` wildcard spans one or more namespace segments (subgroup-nested repos).

``_allowed_url_prefixes_for_host`` turns every bare repo slug into a wildcard
claim ``https://host/*/repo/``. Matching that wildcard against a single
namespace segment drops every MR of a subgroup-nested project
(``group/subgroup/repo``) from :class:`MyPrsScanner` — a silent set reduction,
so their red CI never emits ``my_pr.failed`` and no fix is ever dispatched.

The score stays ``len(head) + len(tail)`` at any nesting depth, so
cross-overlay attribution ties are decided exactly as before.
"""

from teatree.loop.url_specificity import best_url_match_specificity, url_match_specificity, url_matches_prefix

_HOST = "https://gitlab.com"
_WILDCARD_PRODUCT = f"{_HOST}/*/product/"
_WILDCARD_SCORE = len(_HOST) + len("product/")

_FLAT_MR = f"{_HOST}/some-namespace/product/-/merge_requests/1"
_SUBGROUP_MR = f"{_HOST}/some-namespace/sub-group/product/-/merge_requests/1"
_DEEP_MR = f"{_HOST}/some-namespace/sub-group/sub-sub-group/product/-/merge_requests/1"


class TestWildcardSpansNestedNamespaces:
    def test_subgroup_nested_project_matches(self) -> None:
        assert url_match_specificity(_SUBGROUP_MR, _WILDCARD_PRODUCT) == _WILDCARD_SCORE

    def test_three_segment_namespace_matches(self) -> None:
        assert url_match_specificity(_DEEP_MR, _WILDCARD_PRODUCT) == _WILDCARD_SCORE

    def test_nested_match_is_reported_by_url_matches_prefix(self) -> None:
        assert url_matches_prefix(_SUBGROUP_MR, _WILDCARD_PRODUCT)

    def test_nested_match_reaches_best_of_competing_claims(self) -> None:
        prefixes = (f"{_HOST}/*/microservice-x/", _WILDCARD_PRODUCT)
        assert best_url_match_specificity(_SUBGROUP_MR, prefixes) == _WILDCARD_SCORE


class TestWildcardScoringUnchanged:
    def test_single_segment_namespace_scores_as_before(self) -> None:
        assert url_match_specificity(_FLAT_MR, _WILDCARD_PRODUCT) == _WILDCARD_SCORE

    def test_nesting_depth_does_not_change_the_score(self) -> None:
        assert url_match_specificity(_SUBGROUP_MR, _WILDCARD_PRODUCT) == url_match_specificity(
            _FLAT_MR, _WILDCARD_PRODUCT
        )

    def test_exact_owner_claim_still_outscores_the_wildcard(self) -> None:
        exact = f"{_HOST}/some-namespace/sub-group/product/"
        assert url_match_specificity(_SUBGROUP_MR, exact) > url_match_specificity(_SUBGROUP_MR, _WILDCARD_PRODUCT)


class TestWildcardBoundaryStaysStrict:
    def test_repo_name_that_is_a_prefix_of_a_longer_name_does_not_match(self) -> None:
        longer = f"{_HOST}/acme/widget-extra/-/merge_requests/1"
        assert url_match_specificity(longer, f"{_HOST}/*/widget/") == 0

    def test_nested_repo_name_that_is_a_prefix_of_a_longer_name_does_not_match(self) -> None:
        longer = f"{_HOST}/acme/sub-group/widget-extra/-/merge_requests/1"
        assert url_match_specificity(longer, f"{_HOST}/*/widget/") == 0

    def test_wildcard_requires_at_least_one_namespace_segment(self) -> None:
        assert url_match_specificity(f"{_HOST}/product/-/merge_requests/1", _WILDCARD_PRODUCT) == 0

    def test_different_repo_at_any_nesting_depth_does_not_match(self) -> None:
        assert url_match_specificity(_SUBGROUP_MR, f"{_HOST}/*/microservice-x/") == 0

    def test_different_host_does_not_match(self) -> None:
        assert url_match_specificity("https://github.com/acme/sub-group/product/pull/1", _WILDCARD_PRODUCT) == 0
