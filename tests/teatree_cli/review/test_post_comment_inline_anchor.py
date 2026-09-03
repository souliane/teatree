"""``t3 review post-comment`` refuses a half-specified ``--file``/``--line`` pair.

``post_comment_impl`` branches on ``if not (file and line)``, so exactly one of the
two reaches the general-note endpoint and the anchor the caller asked for is
dropped with no error — the #72 silent-degradation shape the draft command already
refuses. Omitting BOTH stays the documented general-note path.
"""

import pytest
from typer.testing import CliRunner

from teatree.cli import app
from teatree.cli.review.bloat_gate import check_review_bloat
from teatree.cli.review.commands import _ALLOW_BLOAT_HELP
from teatree.cli.review.service import ReviewService

_runner = CliRunner()

_REFUSAL = "--file AND --line must be given together"


@pytest.mark.usefixtures("two_overlays", "no_glab_login")
class TestHalfSpecifiedInlineAnchorIsRefused:
    def test_a_lone_file_is_refused_before_any_credential_read(self) -> None:
        result = _runner.invoke(app, ["review", "post-comment", "acme/alpha", "7", "note", "--file", "a.py"])
        assert result.exit_code == 2, result.output
        assert _REFUSAL in result.output

    def test_a_lone_line_is_refused_before_any_credential_read(self) -> None:
        result = _runner.invoke(app, ["review", "post-comment", "acme/alpha", "7", "note", "--line", "42"])
        assert result.exit_code == 2, result.output
        assert _REFUSAL in result.output

    def test_both_omitted_still_posts_a_general_note(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        def _record(_self: ReviewService, repo: str, mr: int, note: str, **kw: object) -> tuple[str, int]:
            captured.update({"repo": repo, "mr": mr, "note": note, "file": kw.get("file"), "line": kw.get("line")})
            return "OK note_id=1", 0

        monkeypatch.setattr(ReviewService, "post_comment", _record)
        result = _runner.invoke(app, ["review", "post-comment", "acme/alpha", "7", "note"])

        assert result.exit_code == 0, result.output
        assert captured["file"] == ""
        assert captured["line"] == 0

    def test_both_given_reaches_the_service_with_the_anchor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, object] = {}

        def _record(_self: ReviewService, repo: str, mr: int, note: str, **kw: object) -> tuple[str, int]:
            del repo, mr, note
            captured.update({"file": kw.get("file"), "line": kw.get("line")})
            return "OK discussion_id=1", 0

        monkeypatch.setattr(ReviewService, "post_comment", _record)
        result = _runner.invoke(
            app, ["review", "post-comment", "acme/alpha", "7", "note", "--file", "a.py", "--line", "42"]
        )

        assert result.exit_code == 0, result.output
        assert captured == {"file": "a.py", "line": 42}


class TestAllowBloatHelpDescribesTheGateItActuallyEscapes:
    """The bloat gate deliberately adds NO length cap — length is ``--allow-long-review``."""

    def test_help_does_not_promise_a_length_escape(self) -> None:
        assert "longer than" not in _ALLOW_BLOAT_HELP
        assert "sentence cap" not in _ALLOW_BLOAT_HELP
        assert "--allow-long-review" in _ALLOW_BLOAT_HELP

    def test_a_long_chatter_free_note_is_not_refused_by_the_bloat_gate(self) -> None:
        assert check_review_bloat(body="word " * 500) == ""
