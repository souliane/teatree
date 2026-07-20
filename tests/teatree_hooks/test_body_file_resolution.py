"""Unit tests for body-file / inline-body resolution (F7.4, F7.5, F7.6).

Direct coverage of the resolution primitives the leak-gate body extractor relies
on: the ``$VAR`` liveness rule (double/unquoted resolve, single-quoted inert),
the exact-line heredoc terminator anchor, and the stale-on-disk-vs-in-command
heredoc superset. Synthetic term ``acmecorp`` only.
"""

from pathlib import Path

from teatree.hooks._body_file_resolution import (
    BodyFileContext,
    _append_file_payload,
    heredoc_files_map,
    unredirected_heredoc_bodies,
)
from teatree.hooks._command_parser import FAIL_CLOSED_SENTINEL
from teatree.hooks._shell_lexer import tokenize


class TestHeredocTerminatorAnchor:
    """F7.5: the terminator must sit ALONE on its line (optionally trailing whitespace)."""

    def test_body_line_beginning_with_delim_word_is_kept(self) -> None:
        command = "cat <<EOF\nfirst\nEOF and the rest: acmecorp\nlast\nEOF\n"
        bodies = unredirected_heredoc_bodies(command)
        assert len(bodies) == 1
        assert "acmecorp" in bodies[0]
        assert "EOF and the rest" in bodies[0]

    def test_true_terminator_with_trailing_whitespace_still_matches(self) -> None:
        command = "cat <<EOF\nbody text\nEOF   \n"
        bodies = unredirected_heredoc_bodies(command)
        assert bodies == ["body text"]


class TestStaleFileVsHeredocSuperset:
    """F7.6: an in-command heredoc body is scanned even when a stale file shadows the path."""

    def test_both_stale_disk_and_heredoc_body_are_appended(self, tmp_path: Path) -> None:
        stale = tmp_path / "msg.txt"
        stale.write_text("old clean content\n", encoding="utf-8")
        command = f"cat > {stale} <<EOF\nnew body acmecorp\nEOF\n"
        ctx = BodyFileContext(heredoc_files=heredoc_files_map(command, tokenize(command)), fail_closed_body_file=True)
        payloads: list[str] = []
        _append_file_payload(str(stale), payloads, ctx, fail_closed=True, leader="gh")
        joined = "\n".join(payloads)
        assert "acmecorp" in joined  # the real in-command body is scanned
        assert "old clean content" in joined  # the stale file is scanned too (superset)

    def test_no_body_anywhere_fails_closed(self, tmp_path: Path) -> None:
        missing = tmp_path / "absent" / "body.md"
        ctx = BodyFileContext(heredoc_files={}, fail_closed_body_file=True)
        payloads: list[str] = []
        _append_file_payload(str(missing), payloads, ctx, fail_closed=True, leader="gh")
        assert payloads == [FAIL_CLOSED_SENTINEL]
