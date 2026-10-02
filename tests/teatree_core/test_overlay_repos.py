"""The single answer to "which forge repos does this overlay work in?" (#4506).

Shared by issue intake (which issues to claim) and the external-outcome measure
(whose merges count as output), so the two can never disagree about scope.
"""

from unittest.mock import MagicMock

from teatree.core.overlay_repos import owned_repo_slugs


def _overlay(*, merge_candidates: list[str], followup: list[str]) -> MagicMock:
    overlay = MagicMock()
    overlay.review.merge_candidate_repo_slugs.return_value = merge_candidates
    overlay.metadata.get_followup_repos.return_value = followup
    return overlay


class TestOwnedRepoSlugs:
    def test_unions_merge_candidates_with_followup_repos(self) -> None:
        overlay = _overlay(merge_candidates=["acme/e2e"], followup=["acme/app"])

        assert owned_repo_slugs(overlay) == ("acme/e2e", "acme/app")

    def test_a_repo_declared_on_both_hooks_appears_once(self) -> None:
        overlay = _overlay(merge_candidates=["acme/app"], followup=["acme/app"])

        assert owned_repo_slugs(overlay) == ("acme/app",)

    def test_urls_are_normalised_down_to_owner_slash_repo(self) -> None:
        overlay = _overlay(merge_candidates=["https://github.com/acme/app"], followup=[])

        assert owned_repo_slugs(overlay) == ("acme/app",)

    def test_an_overlay_with_no_declarations_yields_nothing(self) -> None:
        assert owned_repo_slugs(_overlay(merge_candidates=[], followup=[])) == ()

    def test_no_overlay_yields_nothing(self) -> None:
        assert owned_repo_slugs(None) == ()
