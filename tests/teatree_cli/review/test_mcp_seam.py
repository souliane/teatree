"""The MCP review-post seam builds its service per call, bound to the target repo.

``register_review_post_seam`` takes a factory of the TARGET REPO, so the seam is
built per repo rather than once per process, and that repo is the overlay-resolution
context for both the credential and the base URL. Binding it per repo is what keeps
the MCP write tools working on a multi-overlay install, where an ambient resolution
empties the credential and refuses the base-URL read (souliane/teatree#1814,
refined upstream as #3793/#3794).
"""

import pytest

from teatree.cli.review.mcp_seam import register
from teatree.cli.review.service import ReviewService
from teatree.mcp import review_seam


@pytest.fixture(autouse=True)
def registered_seam() -> None:
    """Install the real seam factory (``teatree.cli`` does this at import time)."""
    register()


@pytest.mark.usefixtures("two_overlays", "no_glab_login")
class TestSeamBindsTheServiceToTheTargetRepo:
    def test_post_comment_builds_a_service_scoped_to_the_posted_repo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        def _record(self: ReviewService, repo: str, mr: int, note: str, *, live: bool = False) -> tuple[str, int]:
            captured.update(token=self.token, bound_repo=self.repo, posted_repo=repo, mr=mr, note=note, live=live)
            return "OK note_id=1", 0

        monkeypatch.setattr(ReviewService, "post_comment", _record)
        seam = review_seam.review_post_seam("acme/alpha")
        message, code = seam.post_comment("acme/alpha", 7, "blocker: bug", live=True)

        assert (message, code) == ("OK note_id=1", 0)
        assert captured == {
            "token": "glpat-ALPHA",
            "bound_repo": "acme/alpha",
            "posted_repo": "acme/alpha",
            "mr": 7,
            "note": "blocker: bug",
            "live": True,
        }

    def test_post_draft_note_builds_a_service_scoped_to_the_posted_repo(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        def _record(self: ReviewService, repo: str, mr: int, note: str) -> tuple[str, int]:
            captured.update(token=self.token, bound_repo=self.repo, posted_repo=repo, mr=mr, note=note)
            return "OK draft_note_id=9", 0

        monkeypatch.setattr(ReviewService, "post_draft_note", _record)
        message, code = review_seam.review_post_seam("acme/alpha").post_draft_note("acme/alpha", 7, "nit: naming")

        assert (message, code) == ("OK draft_note_id=9", 0)
        assert captured["token"] == "glpat-ALPHA"
        assert captured["bound_repo"] == "acme/alpha"

    def test_two_posts_to_different_repos_each_get_their_own_overlay_credential(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[str, str]] = []

        def _record(self: ReviewService, repo: str, mr: int, note: str) -> tuple[str, int]:
            del mr, note
            seen.append((repo, self.token))
            return "OK draft_note_id=9", 0

        monkeypatch.setattr(ReviewService, "post_draft_note", _record)
        review_seam.review_post_seam("acme/alpha").post_draft_note("acme/alpha", 1, "nit: naming")
        review_seam.review_post_seam("acme/bravo").post_draft_note("acme/bravo", 2, "nit: naming")

        # ``bravo`` holds no credential, so its posts carry none — the point is that
        # each call reads from ITS repo's overlay, not from one process-wide guess.
        assert seen == [("acme/alpha", "glpat-ALPHA"), ("acme/bravo", "")]
