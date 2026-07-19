"""Unit tests for body-file / inline-body resolution (F7.4, F7.5, F7.6).

Direct coverage of the resolution primitives the leak-gate body extractor relies
on: the ``$VAR`` liveness rule (double/unquoted resolve, single-quoted inert),
the exact-line heredoc terminator anchor, and the stale-on-disk-vs-in-command
heredoc superset. Synthetic term ``acmecorp`` only.
"""

from pathlib import Path

import pytest

from teatree.hooks._body_file_resolution import (
    BodyFileContext,
    _append_file_payload,
    _var_ref_is_live,
    heredoc_files_map,
    resolve_inline_body_value,
    unredirected_heredoc_bodies,
)
from teatree.hooks._command_parser import (
    FAIL_CLOSED_SENTINEL,
    UNAVAILABLE_BODY_SOURCE_SENTINEL,
    is_unavailable_body_source_sentinel,
)
from teatree.hooks._shell_lexer import tokenize


class TestVarRefIsLive:
    """F7.4: which ``$VAR`` raw spans bash would expand (env-resolvable)."""

    @pytest.mark.parametrize("raw", ["$VAR", "${VAR}", '"$VAR"', '"${VAR}"', ""])
    def test_live_forms(self, raw: str) -> None:
        assert _var_ref_is_live(raw) is True

    @pytest.mark.parametrize("raw", ["'$VAR'", "'${VAR}'"])
    def test_single_quoted_is_inert(self, raw: str) -> None:
        assert _var_ref_is_live(raw) is False


class TestResolveInlineBodyValue:
    """F7.4: resolution of a whole-value ``$VAR`` body from the hook env."""

    def test_unquoted_present_var_resolves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BODYV", "ship to acmecorp")
        assert resolve_inline_body_value("$BODYV", None, raw="$BODYV") == "ship to acmecorp"

    def test_double_quoted_present_var_resolves(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BODYV", "ship to acmecorp")
        assert resolve_inline_body_value("$BODYV", None, raw='"$BODYV"') == "ship to acmecorp"

    def test_unquoted_absent_var_is_unavailable_sentinel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BODYV", raising=False)
        out = resolve_inline_body_value("$BODYV", None, raw="$BODYV")
        assert out == UNAVAILABLE_BODY_SOURCE_SENTINEL
        assert is_unavailable_body_source_sentinel(out)

    def test_single_quoted_var_returned_verbatim(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A single-quoted '$BODYV' is the literal published body, NOT an env
        # reference -- even with the env set, it is scanned verbatim.
        monkeypatch.setenv("BODYV", "ship to acmecorp")
        assert resolve_inline_body_value("$BODYV", None, raw="'$BODYV'") == "$BODYV"


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
