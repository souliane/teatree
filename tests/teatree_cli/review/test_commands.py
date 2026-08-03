"""``_require_token`` — the repo-scoped credential gate every review command passes through.

Two behaviours, driven through the real ``t3 review`` typer surface:

* The target repo (the first CLI argument) is the overlay-resolution context, so the
    service is built with the credential of the overlay that OWNS that repo.
* An empty credential is diagnosed, not guessed at. ``guarded_read`` degrades a failed
    overlay read to ``""`` so one broken config cannot take the review surface down —
    which means "the read broke" and "there is genuinely no credential" arrive at the
    caller looking identical. Printing ``glab auth login`` for the first one sends the
    operator after the one thing that is not broken.
"""

import pytest
from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli.review.service import ReviewService

_runner = CliRunner()

_GLAB_ADVICE = "glab auth login"


@pytest.mark.usefixtures("two_overlays", "no_glab_login")
class TestRequireTokenDiagnosesAnEmptyCredential:
    def test_a_failed_overlay_read_names_the_real_cause(self) -> None:
        # ``acme/unclaimed`` is owned by no overlay, so the read falls through to the
        # ambient lookup and fails — with `glab` unauthenticated the credential is
        # empty for a reason that has nothing to do with `glab`.
        result = _runner.invoke(app, ["review", "post-comment", "acme/unclaimed", "1", "note"])
        assert result.exit_code == 1
        assert "Multiple overlays found" in result.output
        assert _GLAB_ADVICE not in result.output

    def test_a_genuinely_absent_credential_still_advises_glab_login(self) -> None:
        # ``acme/bravo`` resolves cleanly to an overlay that simply has no credential
        # configured — nothing failed, so the login advice is the right advice.
        result = _runner.invoke(app, ["review", "post-comment", "acme/bravo", "1", "note"])
        assert result.exit_code == 1
        assert result.output.strip() == f"No GitLab token found. Run: {_GLAB_ADVICE}"


@pytest.mark.usefixtures("two_overlays", "no_glab_login")
class TestRequireTokenBindsTheServiceToTheTargetRepo:
    def test_the_service_carries_the_owning_overlays_credential_and_the_repo(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, str] = {}

        def _record(self: ReviewService, repo: str, mr: int, note: str, **_kw: object) -> tuple[str, int]:
            captured["token"] = self.token
            captured["bound_repo"] = self.repo
            captured["posted_repo"] = repo
            del mr, note
            return "OK note_id=1", 0

        monkeypatch.setattr(ReviewService, "post_comment", _record)
        result = _runner.invoke(app, ["review", "post-comment", "acme/alpha", "7", "note"])

        assert result.exit_code == 0, result.output
        assert captured == {"token": "glpat-ALPHA", "bound_repo": "acme/alpha", "posted_repo": "acme/alpha"}
