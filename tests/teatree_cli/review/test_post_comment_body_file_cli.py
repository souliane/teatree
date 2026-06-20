"""``t3 review post-comment`` accepts ``--body-file`` / ``-m``/``--body`` (#32).

The note may now come from the positional ``NOTE``, an inline ``-m``/``--body``
flag, or a ``--body-file`` whose content is read at the CLI boundary — matching
how the sibling forge comment commands accept a body file. Exactly one source
must be supplied. Whichever source is used, the resolved body reaches the
service as the ``note`` argument, so the publish path and gates are unchanged.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from teatree.cli import app

_runner = CliRunner()


def _invoke(args: list[str]) -> tuple[int, str]:
    """Run ``review post-comment`` with the service patched to a capturing stub."""
    captured: dict[str, str] = {}

    def _fake_post_comment(self: object, repo: str, mr: int, note: str, **_kw: object) -> tuple[str, int]:
        captured["repo"] = repo
        captured["mr"] = str(mr)
        captured["note"] = note
        return "OK note_id=1", 0

    def _make_service() -> "_Svc":
        return _Svc()

    with (
        patch("teatree.cli.review.commands._require_token", _make_service),
        patch.object(_Svc, "post_comment", _fake_post_comment, create=True),
    ):
        result = _runner.invoke(app, ["review", "post-comment", *args])
    return result.exit_code, captured.get("note", "")


class _Svc:
    """A minimal service stand-in; ``post_comment`` is patched per test."""


class TestPostCommentBodySources:
    """The body comes from NOTE, ``-m``/``--body``, or ``--body-file`` — exactly one."""

    def test_positional_note_still_works(self) -> None:
        code, note = _invoke(["org/repo", "7", "lgtm"])
        assert code == 0
        assert note == "lgtm"

    def test_body_flag_supplies_the_note(self) -> None:
        code, note = _invoke(["org/repo", "7", "--body", "from --body flag"])
        assert code == 0
        assert note == "from --body flag"

    def test_short_message_flag_supplies_the_note(self) -> None:
        code, note = _invoke(["org/repo", "7", "-m", "from -m flag"])
        assert code == 0
        assert note == "from -m flag"

    def test_body_file_content_supplies_the_note(self, tmp_path: Path) -> None:
        bf = tmp_path / "evidence.md"
        bf.write_text("## MR-thread evidence\nrow 1\n", encoding="utf-8")
        code, note = _invoke(["org/repo", "7", "--body-file", str(bf)])
        assert code == 0
        assert note == "## MR-thread evidence\nrow 1\n"

    def test_no_body_source_is_rejected(self) -> None:
        result = _runner.invoke(app, ["review", "post-comment", "org/repo", "7"])
        assert result.exit_code != 0
        assert "--body-file" in result.output

    def test_two_body_sources_is_rejected(self, tmp_path: Path) -> None:
        bf = tmp_path / "x.md"
        bf.write_text("file body", encoding="utf-8")
        result = _runner.invoke(app, ["review", "post-comment", "org/repo", "7", "lgtm", "--body-file", str(bf)])
        assert result.exit_code != 0

    def test_missing_body_file_is_rejected(self, tmp_path: Path) -> None:
        result = _runner.invoke(
            app, ["review", "post-comment", "org/repo", "7", "--body-file", str(tmp_path / "absent.md")]
        )
        assert result.exit_code != 0


@pytest.mark.parametrize("variant", ["--body", "-m"])
def test_inline_body_flag_round_trips(variant: str) -> None:
    code, note = _invoke(["org/repo", "9", variant, "round-trip body"])
    assert code == 0
    assert note == "round-trip body"
