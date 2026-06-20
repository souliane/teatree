"""``review post-comment`` body source resolution (souliane/teatree#32).

``t3 <overlay> review post-comment REPO MR NOTE`` historically took the body
as the positional ``NOTE`` only. Large MR-thread evidence is awkward to pass
as a single shell-quoted argument, and the #1415 banned-terms PreToolUse gate
only scans the well-known body flags (``-m``/``--body``/``--body-file``) on a
``t3`` segment by walking the flag — a positional was a second, separate code
path. This module pins the resolver that lets the body come from ANY of the
three sources, exactly one of which must be supplied.
"""

from pathlib import Path

import pytest

from teatree.cli.review.body_source import PostBodyError, resolve_post_body


class TestResolvePostBody:
    """Exactly one of NOTE / ``-m``/``--body`` / ``--body-file`` supplies the body."""

    def test_positional_note_is_returned(self) -> None:
        assert resolve_post_body(note="lgtm", body="", body_file="") == "lgtm"

    def test_body_flag_is_returned(self) -> None:
        assert resolve_post_body(note=None, body="from flag", body_file="") == "from flag"

    def test_body_file_content_is_read(self, tmp_path: Path) -> None:
        bf = tmp_path / "evidence.md"
        bf.write_text("## evidence\nrow 1\nrow 2\n", encoding="utf-8")
        assert resolve_post_body(note=None, body="", body_file=str(bf)) == "## evidence\nrow 1\nrow 2\n"

    def test_no_source_is_an_error(self) -> None:
        with pytest.raises(PostBodyError):
            resolve_post_body(note=None, body="", body_file="")

    def test_empty_positional_with_no_flags_is_an_error(self) -> None:
        # A literal empty string is no body — same as omitting it.
        with pytest.raises(PostBodyError):
            resolve_post_body(note="", body="", body_file="")

    def test_two_sources_is_an_error(self) -> None:
        with pytest.raises(PostBodyError):
            resolve_post_body(note="lgtm", body="dup", body_file="")

    def test_body_and_body_file_together_is_an_error(self, tmp_path: Path) -> None:
        bf = tmp_path / "x.md"
        bf.write_text("file body", encoding="utf-8")
        with pytest.raises(PostBodyError):
            resolve_post_body(note=None, body="flag body", body_file=str(bf))

    def test_missing_body_file_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(PostBodyError):
            resolve_post_body(note=None, body="", body_file=str(tmp_path / "absent.md"))

    def test_error_message_names_the_flags(self) -> None:
        with pytest.raises(PostBodyError) as exc:
            resolve_post_body(note=None, body="", body_file="")
        message = str(exc.value)
        assert "--body-file" in message
        assert "--body" in message
