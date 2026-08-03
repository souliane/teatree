"""Which repos are review-exempt — a DECLARED table, never an inference.

Repo names here are SYNTHETIC. The overlay that actually declares an exempt set
pins its own real names in its own suite; core only owns the grammar and the
fail-safe direction.

The negative control is the whole point: an exemption read off repo OWNERSHIP
would cover every repo the table does not name, because ownership resolves an
unknown repo to the patient owner. So an unknown repo NOT being exempt is what
separates a declared axis from a derived one, and it is asserted directly.
"""

from unittest.mock import patch

import pytest

from teatree.core.review.repo_exemption import is_review_exempt, mr_url_is_review_exempt, review_exempt_patterns

_EXEMPT = (
    "infra-org/deploy-charts",
    "infra-org/pipeline-templates",
    "infra-org/terraform-estate",
    "infra-org/tenant-config",
)
_PATTERNS = "teatree.core.review.repo_exemption.review_exempt_patterns"


class TestDeclaredPatternsMatch:
    @pytest.mark.parametrize("slug", _EXEMPT)
    def test_every_declared_repo_is_exempt(self, slug: str) -> None:
        assert is_review_exempt(slug, patterns=_EXEMPT)

    def test_a_namespace_pattern_covers_every_repo_under_it(self) -> None:
        assert is_review_exempt("infra-org/anything-at-all", patterns=("infra-org",))

    def test_a_subgroup_path_matches_its_own_declaration(self) -> None:
        pattern = "infra-org/subgroup/deploy-charts"

        assert is_review_exempt(pattern, patterns=(pattern,))

    def test_a_host_qualified_slug_matches_a_bare_declaration(self) -> None:
        """One grammar: the host segment never participates in the match."""
        assert is_review_exempt("gitlab.com/infra-org/deploy-charts", patterns=_EXEMPT)


class TestUndeclaredReposAreNotExempt:
    def test_a_sibling_repo_in_the_same_namespace_is_not_exempt(self) -> None:
        """A repo-scoped declaration must not spill onto its namespace siblings."""
        assert not is_review_exempt("infra-org/shared-skills", patterns=_EXEMPT)

    def test_an_unknown_repo_is_not_exempt(self) -> None:
        """The control a derived exemption would fail.

        Ownership answers the patient owner for an unknown repo, so an exemption
        read off it would silently cover every repo teatree has never heard of.
        """
        assert not is_review_exempt("never-heard-of-it/some-repo", patterns=_EXEMPT)

    def test_no_declared_patterns_exempts_nothing(self) -> None:
        assert not is_review_exempt("infra-org/deploy-charts", patterns=())

    def test_a_superset_namespace_does_not_match(self) -> None:
        assert not is_review_exempt("infra-org-fork/deploy-charts", patterns=_EXEMPT)

    @pytest.mark.parametrize("slug", ["", "   "])
    def test_an_unresolvable_slug_is_not_exempt(self, slug: str) -> None:
        assert not is_review_exempt(slug, patterns=("infra-org",))


class TestPatternSources:
    def test_the_setting_and_the_overlay_hook_are_unioned(self) -> None:
        from teatree.config import UserSettings  # noqa: PLC0415 — deferred: keeps the pure table above import-light

        class _Overlay:
            review = type("_Facet", (), {"review_exempt_repo_slugs": lambda self: ("infra-org/from-overlay",)})()

        with (
            patch(
                "teatree.core.review.repo_exemption.get_effective_settings",
                return_value=UserSettings(review_exempt_repos=["infra-org/from-setting"]),
            ),
            patch("teatree.core.review.repo_exemption.get_overlay", return_value=_Overlay()),
        ):
            patterns = review_exempt_patterns()

        assert patterns == ("infra-org/from-setting", "infra-org/from-overlay")

    def test_an_unresolvable_overlay_leaves_the_setting_standing(self) -> None:
        from django.core.exceptions import ImproperlyConfigured  # noqa: PLC0415 — deferred: Django at call time

        from teatree.config import UserSettings  # noqa: PLC0415 — deferred: keeps the pure table above import-light

        with (
            patch(
                "teatree.core.review.repo_exemption.get_effective_settings",
                return_value=UserSettings(review_exempt_repos=["infra-org/from-setting"]),
            ),
            patch(
                "teatree.core.review.repo_exemption.get_overlay",
                side_effect=ImproperlyConfigured("multiple overlays"),
            ),
        ):
            assert review_exempt_patterns() == ("infra-org/from-setting",)


class TestMrUrlResolution:
    def test_an_exempt_repos_merge_request_url_resolves_exempt(self) -> None:
        with patch(_PATTERNS, return_value=_EXEMPT):
            exempt = mr_url_is_review_exempt("https://gitlab.com/infra-org/deploy-charts/-/merge_requests/7")

        assert exempt

    def test_a_non_exempt_repos_merge_request_url_does_not(self) -> None:
        with patch(_PATTERNS, return_value=_EXEMPT):
            exempt = mr_url_is_review_exempt("https://gitlab.com/infra-org/shared-skills/-/merge_requests/7")

        assert not exempt

    def test_a_pull_request_url_resolves_through_the_same_grammar(self) -> None:
        with patch(_PATTERNS, return_value=("infra-org/deploy-charts",)):
            exempt = mr_url_is_review_exempt("https://github.com/infra-org/deploy-charts/pull/7")

        assert exempt

    def test_an_unparsable_url_is_never_exempt(self) -> None:
        """No slug means no declaration matched it — the side that still posts."""
        with patch(_PATTERNS, return_value=("infra-org",)):
            assert not mr_url_is_review_exempt("not-a-url")
