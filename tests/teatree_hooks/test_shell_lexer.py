r"""Bash-like shell tokenizer — token shape and the ``raw`` invariant.

The lexer feeds the quote-scanner, banned-terms, publish-surface, and
self-rescue gates. Beyond decoding, one property matters: every emitted
token carries a populated ``raw`` verbatim source span — the self-rescue
env-prefix strip relies on quote-accurate ``raw`` (``'A'=1`` is a command
literal, not an assignment).

The lexer does NOT special-case I/O redirects: it splits on the shell
metacharacters (``; | & && || \n``) and otherwise accumulates word tokens.
self-rescue matching rejects redirects by design (see ``self_rescue``), so
the lexer needs no redirect-operator merging — an ``&`` stays the command
separator the grammar makes it.
"""

import pytest

from teatree.hooks._shell_lexer import TokenKind, split_commands, tokenize


class TestRawSpanInvariant:
    @pytest.mark.parametrize(
        "command",
        [
            "t3 gate disable",
            "a | b && c || d & e ; f",
            "first\nsecond\r\nthird",  # bare \n and \r\n operators
            "cmd 2>&1 > /tmp/out",  # redirect-ish words and operators
            "x 'quoted' \"dq\" $'ansi'",  # quoting forms
            "a > /tmp/o < in >> log",  # redirect words + targets
        ],
    )
    def test_every_token_kind_has_a_populated_raw(self, command: str) -> None:
        tokens = tokenize(command)
        assert tokens
        # The invariant the env-prefix strip relies on: EVERY token kind
        # (WORD and OP, including the newline operator) has a non-empty raw.
        assert all(tok.raw != "" for tok in tokens)

    def test_newline_operator_raw_is_the_verbatim_line_ending(self) -> None:
        tokens = tokenize("a\r\nb")
        newline_ops = [tok for tok in tokens if tok.kind is TokenKind.OP and tok.value == "\n"]
        assert newline_ops
        # value is normalised to "\n"; raw keeps the verbatim "\r\n".
        assert newline_ops[0].raw == "\r\n"


class TestAmpersandSeparator:
    def test_bare_background_ampersand_is_an_operator(self) -> None:
        # The lexer does no redirect merging, so an ``&`` is always the
        # command-separator operator the grammar makes it.
        tokens = tokenize("cmd &")
        assert [(t.value, t.kind) for t in tokens] == [("cmd", TokenKind.WORD), ("&", TokenKind.OP)]

    @pytest.mark.parametrize("command", ["a & b", "a && b"])
    def test_chaining_ampersand_splits_commands(self, command: str) -> None:
        # ``&`` (background, then another command) and ``&&`` (logical-and)
        # split into separate command segments.
        assert len(split_commands(tokenize(command))) == 2
