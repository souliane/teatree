"""The single answer to "which forge repos does this overlay work in?" (#4506).

The intake scanner uses this scope by default; external outcomes always use it.
"""

from unittest.mock import MagicMock

import pytest

from teatree.core.overlay_repos import intake_repo_slugs, owned_repo_slugs


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

    def test_case_variants_name_one_owned_repo(self) -> None:
        overlay = _overlay(merge_candidates=["Acme/App"], followup=["acme/app"])

        assert owned_repo_slugs(overlay) == ("Acme/App",)

    def test_urls_are_normalised_down_to_owner_slash_repo(self) -> None:
        overlay = _overlay(merge_candidates=["https://github.com/acme/app"], followup=[])

        assert owned_repo_slugs(overlay) == ("acme/app",)

    def test_an_overlay_with_no_declarations_yields_nothing(self) -> None:
        assert owned_repo_slugs(_overlay(merge_candidates=[], followup=[])) == ()

    def test_no_overlay_yields_nothing(self) -> None:
        assert owned_repo_slugs(None) == ()


class TestIntakeRepoSlugs:
    @pytest.mark.parametrize(
        ("configured", "expected"),
        [
            ("o/app", "o/app"),
            ("group/sub/repo", "group/sub/repo"),
            ("o/my.repo", "o/my.repo"),
            ("o/my_repo", "o/my_repo"),
            ("o/123", "o/123"),
            ("O/App12", "O/App12"),
        ],
    )
    def test_accepts_repo_slug_shapes(self, configured: str, expected: str) -> None:
        assert intake_repo_slugs(None, [configured]) == (expected,)

    @pytest.mark.parametrize("configured", ["o/a b", "o//x", "o/", "o/app?x=1", "o/*", ""])
    def test_rejects_malformed_repo_slug_shapes(self, configured: str, caplog: pytest.LogCaptureFixture) -> None:
        overlay = _overlay(merge_candidates=["o/owned"], followup=[])

        assert intake_repo_slugs(overlay, [configured]) == ("o/owned",)
        assert repr(configured) in caplog.text

    def test_malformed_entry_does_not_widen_a_valid_scope(self, caplog: pytest.LogCaptureFixture) -> None:
        overlay = _overlay(merge_candidates=["o/owned"], followup=[])

        assert intake_repo_slugs(overlay, ["o/app", "typo"]) == ("o/app",)
        assert "'typo'" in caplog.text

    def test_all_malformed_entries_use_owned_scope(self, caplog: pytest.LogCaptureFixture) -> None:
        overlay = _overlay(merge_candidates=["o/owned"], followup=[])

        assert intake_repo_slugs(overlay, ["typo", "o/*"]) == ("o/owned",)
        assert "'typo'" in caplog.text
        assert "'o/*'" in caplog.text

    def test_case_variants_are_one_repo(self) -> None:
        assert intake_repo_slugs(None, ["O/App", "o/app"]) == ("O/App",)
