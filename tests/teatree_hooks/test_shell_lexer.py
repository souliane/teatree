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

The ONE redirect form that DOES need special handling is the heredoc
(``<<``/``<<-``): its body's own newlines are literal data, not command
separators, so without heredoc consumption a ``cat > file << 'EOF'`` write
followed by a real ``gh pr create --body-file`` fragments into one bogus
"command segment" per body line -- each independently fed through the
#1415/#1213 all-segments destination classifier, which over-fires SCAN on
prose that merely resembles command words. See
:class:`TestHeredocBodyConsumption`.
"""

import pytest

from teatree.hooks._gh_glab_hiding import command_segments
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


class TestHeredocBodyConsumption:
    """A heredoc body's own newlines/words are literal data, never separators.

    Regression for the bug where a ``cat > file << 'EOF' ... EOF`` write,
    followed on the same Bash tool call by the real posting command, split
    into one bogus command segment PER HEREDOC BODY LINE — each of which the
    #1415/#1213 all-segments classifier then evaluated as if it were an
    independent shell command, over-firing SCAN on ordinary prose words.
    """

    def test_body_with_multiple_lines_is_not_split_into_extra_segments(self) -> None:
        command = (
            "cat > /tmp/pr_body.md << 'BODYEOF'\n"
            "## Summary\n"
            "- removes the unauthorized feature\n"
            "- see acme/widgets#8521 / TICKET-1665\n"
            "BODYEOF\n"
            "gh pr create --repo acme/private-repo --title x --body-file /tmp/pr_body.md"
        )
        segments = command_segments(command)
        # Exactly TWO real commands: the heredoc-carrying write, and the post.
        # Pre-fix, each heredoc body line became its own bogus extra segment.
        assert len(segments) == 2
        assert segments[0][:2] == ["cat", ">"]
        assert segments[1][:3] == ["gh", "pr", "create"]

    def test_body_lines_never_appear_as_their_own_words(self) -> None:
        command = "cat > /tmp/x << 'EOF'\nfirst line\nsecond line\nEOF\necho done"
        segments = command_segments(command)
        assert len(segments) == 2
        assert segments[1] == ["echo", "done"]
        # The body text is swallowed whole -- it never surfaces as WORD
        # tokens a downstream consumer could mis-tokenize as commands.
        flat_words = [w for seg in segments for w in seg]
        assert "first" not in flat_words
        assert "line" not in flat_words

    @pytest.mark.parametrize(
        "opener",
        [
            "<< 'EOF'",  # space-separated, quoted delimiter
            "<<EOF",  # glued, bare delimiter
            "<<'EOF'",  # glued, single-quoted delimiter
            '<<"EOF"',  # glued, double-quoted delimiter
        ],
    )
    def test_every_heredoc_opener_shape_consumes_its_body(self, opener: str) -> None:
        command = f"cat > /tmp/x {opener}\nbody line\nEOF\necho done"
        segments = command_segments(command)
        assert len(segments) == 2
        assert segments[1] == ["echo", "done"]

    def test_dash_variant_strips_leading_tabs_from_terminator(self) -> None:
        # ``<<-`` permits the closing delimiter line to be tab-indented.
        command = "cat > /tmp/x <<-EOF\nbody line\n\t\tEOF\necho done"
        segments = command_segments(command)
        assert len(segments) == 2
        assert segments[1] == ["echo", "done"]

    def test_unterminated_heredoc_stops_at_end_of_input_without_hanging(self) -> None:
        command = "cat > /tmp/x << 'EOF'\nbody line one\nbody line two"
        # Must terminate promptly (no matching delimiter) rather than loop.
        segments = command_segments(command)
        assert len(segments) == 1
        assert segments[0][:2] == ["cat", ">"]

    def test_heredoc_body_text_resembling_a_publish_command_is_inert(self) -> None:
        # A heredoc body that happens to CONTAIN text resembling a gh/glab
        # invocation is file content, not a second command — it must never
        # surface as its own resolvable segment.
        command = "cat > /tmp/x << 'EOF'\ngh pr create --repo other/repo --title leak\nEOF\n"
        segments = command_segments(command)
        assert len(segments) == 1
        assert segments[0][:2] == ["cat", ">"]

    def test_non_heredoc_redirects_are_unaffected(self) -> None:
        # Plain redirects (no heredoc) keep their prior behaviour: no body
        # consumption is triggered, words stay as-is.
        command = "cmd > /tmp/out && echo done"
        segments = command_segments(command)
        assert len(segments) == 2
        assert segments[0] == ["cmd", ">", "/tmp/out"]
        assert segments[1] == ["echo", "done"]
